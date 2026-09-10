"""U6 Phase 2 — the uncovered-currency report and its single complete choke.

The report is a detection-AGGREGATION: it is correct only if it collects the currency verdict from
EVERY path a figure's currency can be decided in. The Phase-2 enumeration proved there is ONE such
choke — units.resolve_currency — so these controls prove (a) the choke records, (b) each live path
reaches it, (c) a foreign currency implied by DOMICILE (which a token scan would miss) surfaces, and
(d) the recording is LOAD-BEARING: with no active ledger the same foreign figure VANISHES from the
report (the reddening control — a report that is only ever green proves nothing). Plus the fail-closed
partition (uncovered/covered/conflict/ambiguous), empty-when-all-INR, and the self-contained
foreign-domicile disclosure that stops 'covered the rate-actionable currency' from reading as 'all
foreign exposure handled'.

Run:
  pytest backend/dataimport/preingest3/tests/test_currency_ledger.py -p no:cacheprovider -o addopts="" -m ""
"""
import os

import pytest

from backend.dataimport.preingest3 import units, currency_ledger, ledger
from backend.dataimport.preingest3.ledger import CAPITAL_CALLS
from backend.dataimport.preingest3.ratecard import RateCard, default_inr_card

RC = default_inr_card('2026-06-30')
MYR_CARD = RateCard.from_input({'as_of': '2026-06-30', 'rates': [
    {'currency': 'MYR', 'inr_per_unit': 18.6, 'rate_date': '2026-06-30', 'source': 'RBI ref'}]})


def _under_ledger(fn):
    """Activate a run-scoped ledger, run fn(ledger), always release. Mirrors the pipeline
    lifecycle so the choke is exercised exactly as in production."""
    led = currency_ledger.CurrencyLedger()
    currency_ledger.set_active(led)
    try:
        fn(led)
    finally:
        currency_ledger.set_active(None)
    return led


# ── the choke records, and DOMICILE-implied foreign currency surfaces (the token-scan gap) ──────────
def test_domicile_implied_foreign_surfaces_a_token_scan_would_miss():
    # no statement token, Malaysian DOMICILE → resolve_currency returns MYR (rule ii). A lexical token
    # scan (₹/rm/$) sees NO token here and would miss it; the choke captures it.
    led = _under_ledger(lambda L: (L.context(entity='Foreign Co', source_file='f.xlsx'),
                                   units.resolve_currency(stmt_currency=None, geo_currency='MYR',
                                                          inr_mentioned=False)))
    rep = led.uncovered_report(RC)
    assert [u['currency'] for u in rep['uncovered']] == ['MYR']
    assert rep['prompt_required'] is True
    assert rep['uncovered'][0]['sites'] == [{'entity': 'Foreign Co', 'file': 'f.xlsx'}]


def test_reddening_no_active_ledger_the_foreign_figure_vanishes():
    # THE load-bearing proof: the SAME foreign resolution, but no run activated a ledger → nothing is
    # recorded → an empty report. If the recording ever stops being the report's source, this reddens.
    currency_ledger.set_active(None)                       # ensure inactive
    units.resolve_currency(stmt_currency=None, geo_currency='MYR', inr_mentioned=False)
    led = currency_ledger.CurrencyLedger()                 # a fresh, never-activated ledger
    rep = led.uncovered_report(RC)
    assert rep['uncovered'] == [] and rep['prompt_required'] is False


def test_covered_when_the_card_carries_the_rate():
    led = _under_ledger(lambda L: (L.context(entity='Foreign Co', source_file='f.xlsx'),
                                   units.resolve_currency(stmt_currency='MYR', geo_currency='MYR',
                                                          inr_mentioned=False)))
    rep = led.uncovered_report(MYR_CARD)
    assert rep['uncovered'] == []                          # a rate exists → not uncovered
    assert [c['currency'] for c in rep['covered']] == ['MYR']
    assert rep['covered'][0]['inr_per_unit'] == '18.6' and rep['prompt_required'] is False


def test_conflict_surfaces_and_does_not_prompt_for_a_rate():
    # a statement token that contradicts a known domicile is a currency CONFLICT (disambiguate), NOT a
    # missing rate — it must not drive the rate prompt.
    led = _under_ledger(lambda L: units.resolve_currency(stmt_currency='MYR', geo_currency='INR',
                                                         inr_mentioned=False))
    rep = led.uncovered_report(RC)
    assert rep['uncovered'] == [] and rep['prompt_required'] is False
    assert len(rep['conflicts']) == 1
    assert any('currency_conflict' in f for f in rep['conflicts'][0]['flags'])


def test_ambiguous_no_evidence_surfaces_but_is_not_a_rate_request():
    led = _under_ledger(lambda L: units.resolve_currency(stmt_currency=None, geo_currency=None,
                                                         inr_mentioned=True))
    rep = led.uncovered_report(RC)
    assert rep['uncovered'] == [] and rep['prompt_required'] is False
    assert len(rep['ambiguous']) == 1                      # unknown currency — disclosed, cannot rate-prompt


def test_all_inr_report_is_empty():
    # every observation resolves INR (token or domicile) → nothing foreign → an empty report, no prompt.
    def go(L):
        units.resolve_currency(stmt_currency='INR', geo_currency='INR', inr_mentioned=True)
        units.resolve_currency(stmt_currency=None, geo_currency='INR', inr_mentioned=False)
    rep = _under_ledger(go).uncovered_report(RC)
    assert rep['uncovered'] == [] and rep['covered'] == [] and rep['conflicts'] == []
    assert rep['ambiguous'] == [] and rep['prompt_required'] is False


# ── the FUND-LEDGER path (site 8) genuinely reaches the choke ────────────────────────────────────────
# foreign currency token but NO scale unit and NO anchor → the frame RESOLVES the currency (USD) then
# escalates on SCALE, so the block holds BEFORE any conversion. This proves the ledger PATH reaches the
# currency choke (USD is recorded) without depending on emit-time conversion — which, for a foreign-
# resolved-but-unrated figure, currently RAISES rather than fail-closed holds (a real gap, deferred to
# Phase 4's FX_UNCOVERED handling; see the ledger note). Deliberately kept off that path here.
_USD_LEDGER = [['Capital call ledger (USD)'],
               ['Call no', 'date', 'amount'],
               ['C1', 'a', 150], ['C2', 'b', 150], ['Total called', '', 300]]


def test_fund_ledger_path_reaches_the_choke():
    def go(L):
        L.context(entity='(fund ledger)', source_file='fund.xlsx')
        ledger.extract_from_sheet(_USD_LEDGER, 'S1', CAPITAL_CALLS, anchor_cr=None,
                                  label='t', content_fp='fp', rate_card=RC)
    rep = _under_ledger(go).uncovered_report(RC)
    # USD had a positive statement token and no domicile → resolved USD → uncovered (no rate on RC).
    # The LEDGER path reached resolve_currency: proof this path is not an un-enumerated escape.
    assert 'USD' in [u['currency'] for u in rep['uncovered']]


# ── self-contained foreign-exposure honesty (the SGD/Chemoscience class) ──────────────────────────────
def test_foreign_domicile_unresolved_flags_an_entity_held_upstream():
    # a foreign-domiciled portfolio entity whose statement never reached a currency decision (held
    # upstream) is disclosed — so covering the rate-actionable currency can't read as 'all handled'.
    led = _under_ledger(lambda L: None)                    # no observations at all this run
    rep = led.uncovered_report(RC, foreign_domiciles={'Chemoscience Pte Ltd': 'SGD'})
    fdu = rep['foreign_domicile_unresolved']
    assert len(fdu) == 1 and fdu[0]['entity'] == 'Chemoscience Pte Ltd'
    assert fdu[0]['implied_currency'] == 'SGD'
    assert rep['prompt_required'] is False                 # a rate would NOT surface it


def test_unmapped_foreign_domicile_is_disclosed_never_invisible():
    # an entity domiciled where expected_currency has no mapping (Indonesia), held upstream, would appear
    # in NEITHER uncovered NOR foreign_domicile_unresolved → invisible. It gets its own disclosure so no
    # unmapped geography hides behind green, for any future fund. Not rate-actionable (can't name the ccy).
    led = _under_ledger(lambda L: None)
    rep = led.uncovered_report(RC, unmapped_foreign_domiciles={'PT Nusantara Tbk': 'Indonesia'})
    fdcu = rep['foreign_domicile_currency_unmapped']
    assert len(fdcu) == 1 and fdcu[0]['entity'] == 'PT Nusantara Tbk' and fdcu[0]['domicile'] == 'Indonesia'
    assert rep['prompt_required'] is False
    assert rep['uncovered'] == [] and rep['foreign_domicile_unresolved'] == []


def test_mapped_foreign_domicile_is_NOT_in_the_unmapped_bucket():
    # negative control: a MAPPED foreign domicile (SGD) belongs in foreign_domicile_unresolved, never the
    # unmapped bucket — reddens if the two disclosures ever cross-contaminate.
    led = _under_ledger(lambda L: None)
    rep = led.uncovered_report(RC, foreign_domiciles={'Chemoscience Pte Ltd': 'SGD'},
                               unmapped_foreign_domiciles={})
    assert rep['foreign_domicile_currency_unmapped'] == []
    assert len(rep['foreign_domicile_unresolved']) == 1


def test_foreign_domicile_that_WAS_resolved_is_not_flagged():
    # negative control: the same entity, but its statement DID resolve its currency this run → it is
    # covered by the resolution path, so it must NOT appear as unresolved (else every foreign co double-
    # counts). This reddens if the 'observed' exclusion ever breaks.
    led = _under_ledger(lambda L: (L.context(entity='Chemopharm Sdn Bhd', source_file='cpm.xlsx'),
                                   units.resolve_currency(stmt_currency=None, geo_currency='MYR',
                                                          inr_mentioned=False)))
    rep = led.uncovered_report(RC, foreign_domiciles={'Chemopharm Sdn Bhd': 'MYR'})
    assert rep['foreign_domicile_unresolved'] == []
    assert [u['currency'] for u in rep['uncovered']] == ['MYR']


# ── condition #2: the fund-base review surface (a fund-base resolution is never silently swept in) ─────
def test_base_currency_applied_surfaces_user_confirmed_sites_for_review():
    # a figure resolved by the fund's user-confirmed base (no token, no domicile) is SURFACED for
    # per-batch review — the catch that stops a new foreign entrant being swept into the base unseen.
    led = _under_ledger(lambda L: (L.context(entity='Hubler', source_file='h.xlsx'),
                                   units.resolve_currency(stmt_currency=None, geo_currency=None,
                                                          inr_mentioned=False, base_currency='INR')))
    rep = led.uncovered_report(RC)
    assert rep['base_currency_applied'] == [{'entity': 'Hubler', 'file': 'h.xlsx'}]
    assert rep['prompt_required'] is False                 # a review surface, NOT a rate request


def test_base_currency_applied_is_empty_when_evidence_is_from_the_file_REDDENING():
    # NEGATIVE CONTROL: currency from FILE evidence (a token OR a domicile) is NOT the fund-base path,
    # so it must NOT surface for base-review. Reddens if the surface ever widens to every emit — which
    # would drown the review list and defeat its purpose (spotting exactly the fund-base resolutions).
    def go(L):
        L.context(entity='Tokened', source_file='t.xlsx')
        units.resolve_currency(stmt_currency='INR', geo_currency=None, inr_mentioned=False, base_currency='INR')
        L.context(entity='Domiciled', source_file='d.xlsx')
        units.resolve_currency(stmt_currency=None, geo_currency='INR', inr_mentioned=False, base_currency='INR')
    rep = _under_ledger(go).uncovered_report(RC)
    assert rep['base_currency_applied'] == []


def test_pipeline_fund_base_surfaces_applied_sites_and_is_inert_by_default():
    # END-TO-END plumbing + condition #2: base_currency is threaded run → worker → resolve_currency.
    # A file with NO currency token and NO domicile holds its currency without a fund base; supplying
    # base='INR' resolves it via the fund base AND surfaces the site for review. Default (no base) is
    # inert — nothing is force-resolved, nothing surfaced (the byte-identical guarantee, observably).
    import tempfile

    import openpyxl

    from backend.dataimport.preingest3 import pipeline
    from backend.dataimport.preingest3.alias_ledger import AliasLedger

    def _xlsx(path, rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        for r in rows:
            ws.append(r)
        wb.save(path)
        return path

    with tempfile.TemporaryDirectory() as d:
        fund = _xlsx(os.path.join(d, 'fund_schedule.xlsx'),
                     [['Company', 'Cost', 'Fair Value', 'Ownership %'],
                      ['Acme Labs', 40, 90, 25],
                      ['Zephyr Diagnostics', 60, 150, 18]])
        mis = _xlsx(os.path.join(d, 'Zephyr Diagnostics monthly.xlsx'),
                    [['Particulars', 'Apr-25', 'May-25', 'Jun-25'],
                     ['Revenue', 10, 11, 12], ['EBITDA', 2, 2, 3],
                     ['Closing Cash', 5, 6, 7], ['Headcount', 20, 21, 22]])
        files = [('fund_schedule', fund), ('Zephyr Diagnostics monthly', mis)]
        card = default_inr_card('2026-06-30')

        r_none = pipeline.run(files, as_of='2026-06-30', org='fbtest', rate_card=card,
                              alias_store=AliasLedger(org='fbtest', path=os.path.join(d, 'a0.json')))
        assert r_none.currency_report['base_currency_applied'] == [], 'default base=None must be inert'

        r_inr = pipeline.run(files, as_of='2026-06-30', org='fbtest', rate_card=card,
                             base_currency='INR',
                             alias_store=AliasLedger(org='fbtest', path=os.path.join(d, 'a1.json')))
        applied = {s['entity'] for s in r_inr.currency_report['base_currency_applied']}
        assert 'Zephyr Diagnostics' in applied, \
            'fund base=INR did not reach resolve_currency through the pipeline (plumbing gap) ' \
            'or was not surfaced for review (condition #2)'


# ── real-files integration: the per-path proof on production data (slow) ──────────────────────────────
_IN = 'backend/media/preingest/trivesta/100e86d5/in'


@pytest.mark.slow
@pytest.mark.skipif(not os.path.isdir(_IN), reason='real fixture files not present')
def test_real_files_report_surfaces_MYR_via_company_mis_and_discloses_SGD():
    from backend.dataimport.preingest3 import pipeline
    files = [(f, os.path.join(_IN, f)) for f in sorted(os.listdir(_IN)) if f.lower().endswith('.xlsx')]
    rep = pipeline.run(files, as_of='2026-06-30', org='u6p2t',
                       rate_card=default_inr_card('2026-06-30')).currency_report
    # 1. the COMPANY-MIS path surfaces MYR (Analisa + Chemopharm), rate-actionable
    myr = [u for u in rep['uncovered'] if u['currency'] == 'MYR']
    assert len(myr) == 1 and rep['prompt_required'] is True
    entities = {s['entity'] for s in myr[0]['sites']}
    assert {'Analisa Resources Sdn Bhd', 'Chemopharm Sdn Bhd'} <= entities
    # 2. MYR is attributed to the MIS files, NOT the fund valuation/investment schedule — the FV path
    #    (INR-by-construction) fabricates no foreign entry.
    src_files = {s['file'] for s in myr[0]['sites']}
    assert not any('Valuation' in f or 'Investments_and_Deployment' in f for f in src_files)
    # 3. SGD/Chemoscience is disclosed as foreign-domiciled-but-unresolved (CSS held upstream), NOT as a
    #    rate request — the self-contained honesty that stops a false 'all covered'.
    fdu = {e['entity']: e['implied_currency'] for e in rep['foreign_domicile_unresolved']}
    assert fdu.get('Chemoscience Pte Ltd') == 'SGD'
    assert 'SGD' not in [u['currency'] for u in rep['uncovered']]


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn) and 'real_files' not in name:
            fn()
            print(f'ok  {name}')
    print('ALL FAST PASS')
