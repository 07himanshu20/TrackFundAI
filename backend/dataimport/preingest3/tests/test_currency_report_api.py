"""U6 Phase 5 (backend contract) — the two API surfaces the conditional prompt binds to:
  • the run's serialized payload carries the uncovered-currency REPORT (`currency_report`) — the contract
    that decides whether the prompt fires and what it REQUESTS (the `uncovered` set + top-level as_of) vs
    merely DISCLOSES (the foreign-domicile buckets); empty ⇒ an all-INR run ⇒ no prompt.
  • the accept-card path routes a user-supplied card through the VALIDATED intake gate (NOT raw
    RateCard.from_input), so a mistyped rate is refused with a reason, and a valid rate the run does not
    need is accepted-and-noted (the standing/superset-card case), never a card-refusing error.

These test the wiring helpers directly (the HTTP endpoints are thin wrappers over them), so the proof runs
at unit speed on the REAL intake — matching the engine-level test pattern in this suite.

Run:
  pytest backend/dataimport/preingest3/tests/test_currency_report_api.py -p no:cacheprovider -o addopts="" -m ""
"""
import os
from types import SimpleNamespace

import pytest

# NB: import via the Django-registered `dataimport.` path (NOT `backend.dataimport.`). preingest3_views
# imports Django models, which must resolve to the app_label in INSTALLED_APPS; and IntakeError must be
# the SAME class object _accept_card raises internally (via its relative import), so the prefix must match.
from dataimport.preingest3_views import _accept_card, _serialize
from dataimport.preingest3.ratecard import default_inr_card
from dataimport.preingest3.ratecard_intake import IntakeError

AS_OF = '2026-06-30'


def _entry(**kw):
    e = {'currency': 'MYR', 'rate': '18.6', 'date': AS_OF, 'source': 'RBI ref'}
    e.update(kw)
    return e


def _fake_result(currency_report):
    # the minimal RunResult shape _serialize reads (no engine run needed to test pass-through)
    return SimpleNamespace(
        cir=SimpleNamespace(records=[], disclosures=[]),
        files=[], review_queue=[], model_metrics=None, model_health=None,
        currency_report=currency_report)


# ── the report reaches the UI contract verbatim (prompt gate + request/disclose buckets) ─────────────
def test_serialize_passes_the_currency_report_through():
    rep = {'prompt_required': True, 'as_of': AS_OF,
           'uncovered': [{'currency': 'MYR', 'sites': [{'entity': 'Analisa', 'file': 'f'}], 'count': 1}],
           'covered': [], 'conflicts': [], 'ambiguous': [],
           'foreign_domicile_unresolved': [{'entity': 'Chemoscience Pte Ltd', 'implied_currency': 'SGD'}],
           'foreign_domicile_currency_unmapped': []}
    out = _serialize(_fake_result(rep), AS_OF, default_inr_card(AS_OF))
    assert out['currency_report'] == rep                        # passed straight through, no lossy reshape
    assert out['currency_report']['prompt_required'] is True    # the prompt gate the UI reads
    assert [u['currency'] for u in out['currency_report']['uncovered']] == ['MYR']  # what it REQUESTS
    assert out['currency_report']['as_of'] == AS_OF             # the date the prompt shows per uncovered ccy


def test_serialize_all_inr_run_carries_an_empty_report_no_prompt():
    rep = {'prompt_required': False, 'as_of': AS_OF, 'uncovered': [], 'covered': [],
           'conflicts': [], 'ambiguous': [], 'foreign_domicile_unresolved': [],
           'foreign_domicile_currency_unmapped': []}
    out = _serialize(_fake_result(rep), AS_OF, default_inr_card(AS_OF))
    assert out['currency_report']['prompt_required'] is False   # all-INR ⇒ UI shows nothing
    assert out['currency_report']['uncovered'] == []


# ── accept-card routes through the VALIDATED intake gate, and discloses actionability ────────────────
def test_accept_card_manual_valid_superset_accepted_and_noop_disclosed():
    card, noop, still = _accept_card(manual=[_entry(), _entry(currency='SGD', rate='63')],
                                     rows=None, as_of=AS_OF, uncovered={'MYR'})
    assert set(card.rates) == {'MYR', 'SGD'}                    # superset accepted whole (nothing dropped)
    assert [n['currency'] for n in noop] == ['SGD']            # SGD supplied but not needed this run
    assert still == []                                         # MYR (the only need) is now covered


def test_accept_card_still_uncovered_reports_the_gap():
    card, noop, still = _accept_card(manual=[_entry()], rows=None, as_of=AS_OF,
                                     uncovered={'MYR', 'SGD'})
    assert set(card.rates) == {'MYR'} and still == ['SGD']     # run still needs an SGD rate (fail-closed)


def test_accept_card_refuses_a_mistyped_rate_with_a_reason():
    # reddening: the endpoint MUST reject via the intake gate, not silently coerce — this is the whole
    # point of routing through card_from_manual rather than RateCard.from_input directly.
    with pytest.raises(IntakeError):
        _accept_card(manual=[_entry(rate='-1')], rows=None, as_of=AS_OF, uncovered={'MYR'})
    with pytest.raises(IntakeError):
        _accept_card(manual=[_entry(date='2020-01-01')], rows=None, as_of=AS_OF, uncovered={'MYR'})


def test_accept_card_schedule_path_is_co_equal():
    card, _, _ = _accept_card(manual=None,
                              rows=[['Currency', 'INR per unit', 'Rate Date', 'Source'],
                                    ['MYR', 18.6, AS_OF, 'RBI ref']],
                              as_of=AS_OF, uncovered={'MYR'})
    assert 'MYR' in card.rates and str(card.rates['MYR'].inr_per_unit) == '18.6'


# ── the uploaded-schedule path parses server-side and hits the SAME gate (co-equal third input) ──────
def test_schedule_file_upload_parses_to_rows_then_gates():
    import io
    import openpyxl
    from django.core.files.uploadedfile import SimpleUploadedFile
    from dataimport.preingest3_views import _rows_from_schedule_upload

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['FX reference rates'])                 # a title row above the header (must be skipped)
    ws.append(['Currency', 'INR per unit', 'as of', 'Provider'])
    ws.append(['MYR', 18.6, AS_OF, 'RBI'])
    buf = io.BytesIO()
    wb.save(buf)
    up = SimpleUploadedFile('sched.xlsx', buf.getvalue(),
                            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    rows = _rows_from_schedule_upload(up)
    assert rows is not None                            # a currency+rate header was found in the workbook
    card, _, _ = _accept_card(manual=None, rows=rows, as_of=AS_OF, uncovered={'MYR'})
    assert 'MYR' in card.rates and str(card.rates['MYR'].inr_per_unit) == '18.6'


def test_schedule_file_with_no_header_returns_none():
    import io
    import openpyxl
    from django.core.files.uploadedfile import SimpleUploadedFile
    from dataimport.preingest3_views import _rows_from_schedule_upload

    wb = openpyxl.Workbook()
    wb.active.append(['some notes', 'not', 'a', 'rate sheet'])
    buf = io.BytesIO()
    wb.save(buf)
    up = SimpleUploadedFile('x.xlsx', buf.getvalue())
    assert _rows_from_schedule_upload(up) is None      # no schedule header ⇒ endpoint returns 400, not a guess


# ── E2E on the REAL foreign files: the serialized payload exposes the prompt contract (slow) ──────────
_IN = 'backend/media/preingest/trivesta/100e86d5/in'


@pytest.mark.slow
@pytest.mark.skipif(not os.path.isdir(_IN), reason='real fixture files not present')
def test_serialize_on_real_foreign_files_exposes_the_prompt_contract():
    # THE Phase-5 proof at the API-contract level: run the REAL pipeline on the REAL files (INR-only card),
    # then assert the serialized run payload the UI binds to carries a report that FIRES the prompt and
    # names MYR to REQUEST — on the production path, not a reconstruction.
    from dataimport.preingest3 import pipeline
    from dataimport.preingest3_views import _serialize
    files = [(f, os.path.join(_IN, f)) for f in sorted(os.listdir(_IN)) if f.lower().endswith('.xlsx')]
    as_of = '2026-06-30'
    result = pipeline.run(files, as_of=as_of, org='p5report', rate_card=default_inr_card(as_of))
    payload = _serialize(result, as_of, default_inr_card(as_of))
    rep = payload['currency_report']
    assert rep is not None and rep['prompt_required'] is True         # foreign detected ⇒ UI shows the prompt
    assert 'MYR' in [u['currency'] for u in rep['uncovered']]         # and REQUESTS an MYR rate
    assert rep['as_of'] == as_of                                     # for the run's as-of date
    # SGD/Chemoscience is only DISCLOSED (blocked upstream), never requested — a rate can't surface it
    assert 'Chemoscience Pte Ltd' in [e['entity'] for e in rep['foreign_domicile_unresolved']]


if __name__ == '__main__':
    print('run via pytest')
