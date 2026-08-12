"""Phase D — fund LPA economic terms: the bundle (value + unit + base + phase + cell
+ verdict), format-aware unit normalization, sanity bands, multi-source agreement,
and base/phase fail-closed UNSPECIFIED.

Proves on the real fund files + synthetic negative controls that a term is never a
bare number, that the number FORMAT disambiguates the unit (catch-up 1 == 100%, not
1%), that a rate outside its band or of an unconfirmable unit is HELD, and that a
term disagreeing across two sources holds both (never silently picks one).
"""
import os
from decimal import Decimal

import pytest

from backend.dataimport.preingest3 import fund_terms, assemble
from backend.dataimport.preingest3.fund_terms import FundTerm, normalize_percent, _fmt_pct
from backend.dataimport.preingest3.profiler import profile_file
from backend.dataimport.preingest3.cir import CIR, Record, Provenance

IN = 'backend/media/preingest/trivesta/100e86d5/in'
TERMS = 'TFAI_Fund_Terms_and_LP_Register_wip.xlsx'
ACCOUNTS = 'TFAI_Fund_Accounts_Fees_Budget_Compliance.xlsx'
_real = pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present')


def _extract(fn):
    return fund_terms.extract_fund_terms(fn, os.path.join(IN, fn), profile_file(fn, os.path.join(IN, fn)), content_fp='fp')


# ── unit normalization: VALUE + NUMBER FORMAT together (the core rule) ────────
def test_percent_format_reads_fraction_as_percent():
    assert normalize_percent(Decimal('0.02'), '0.0%', 'Mgmt fee %')[0] == Decimal('0.02')   # 2%
    assert normalize_percent(Decimal('1'), '0.0%', 'GP catch up %')[0] == Decimal('1')       # 100%, NOT 1%
    assert normalize_percent(Decimal('0.2'), '0.00%', 'Carry %')[0] == Decimal('0.2')         # 20%


def test_string_and_bps_units():
    assert normalize_percent('2%', 'General', 'Fee')[0] == Decimal('0.02')
    assert normalize_percent('200 bps', 'General', 'Fee')[0] == Decimal('0.02')
    assert normalize_percent('0.02', 'General', 'Fee %')[0] == Decimal('0.02')   # 0<v<1 → fraction


def test_bare_number_unconfirmable_is_held_NEGATIVE_CONTROL():
    # a bare "2" in a plain cell whose label carries no % → unit cannot be confirmed → None.
    frac, unit, note = normalize_percent(Decimal('2'), 'General', 'Mgmt fee')
    assert frac is None and unit == 'UNSPECIFIED' and 'unconfirmable' in note
    # but the SAME 2 with a %-bearing label resolves to 2% (bare-number %-label path)
    assert normalize_percent(Decimal('2'), 'General', 'Mgmt fee %')[0] == Decimal('0.02')


def test_fmt_pct_is_clean():
    assert (_fmt_pct(Decimal('0.02')), _fmt_pct(Decimal('0.2')), _fmt_pct(Decimal('1')),
            _fmt_pct(Decimal('0.025'))) == ('2%', '20%', '100%', '2.5%')


# ── real file: bundle to source cell, format-aware ───────────────────────────
@_real
def test_terms_extracted_to_source_cell_format_aware():
    rec = _extract(TERMS)
    t = {k: v for k, v in rec.fields.items() if isinstance(v, FundTerm)}
    assert t['management_fee'].value == Decimal('0.02') and t['management_fee'].provenance.cell == 'C18'
    assert t['carried_interest'].display == '20%' and t['hurdle_rate'].display == '8%'
    # THE FORMAT-DISAMBIGUATION: catch-up cell holds 1, %-formatted → 100% (never 1%)
    assert t['catch_up'].value == Decimal('1') and t['catch_up'].display == '100%'
    assert t['catch_up'].qualifiers['catch_up_exists'] is True
    assert t['waterfall'].text_value == 'European whole fund' and t['waterfall'].unit == 'label'
    assert all(v.verdict == 'confirmed' for v in t.values())


@_real
def test_mgmt_base_phase_unspecified_from_terms_file_alone():
    # the terms sheet states only 'Mgmt fee % p.a.' — NO base. Fail-closed: UNSPECIFIED,
    # never a silent 'of committed capital' default.
    m = _extract(TERMS).fields['management_fee']
    assert m.base == 'UNSPECIFIED' and m.phase == 'UNSPECIFIED' and m.verdict == 'confirmed'


@_real
def test_na_versus_unspecified_is_encoded_per_concept():
    # THE SILENT-DROP GUARD: an applicable-but-absent qualifier must be UNSPECIFIED (a
    # disclosed hole), not n/a. carry's GAIN base applies (total vs realized gains) →
    # UNSPECIFIED; catch-up has no base concept → n/a; hurdle compounding lives in prose
    # → UNSPECIFIED. If these collapsed to one state a reader couldn't tell a structural
    # absence from a data hole, and a future carry-with-a-base would be silently stamped n/a.
    t = {k: v for k, v in _extract(TERMS).fields.items() if isinstance(v, FundTerm)}
    assert t['carried_interest'].base == 'UNSPECIFIED' and t['carried_interest'].phase == 'n/a'
    assert t['catch_up'].base == 'n/a' and t['clawback_holdback'].base == 'n/a'
    assert t['hurdle_rate'].qualifiers['compounding'] == 'UNSPECIFIED'


@_real
def test_tightness_anchors_labeled_row_not_heading():
    # on the Fees sheet a HEADING mentions 'performance fee'; the real 'Carry %' row is
    # C14. Tightest-match must bind carry to C14 (20%), never the heading→2% sentence.
    fees = _extract(ACCOUNTS).fields['carried_interest']
    assert fees.value == Decimal('0.2') and fees.provenance.cell == 'C14'


@_real
def test_pipeline_enriches_base_and_corroborates():
    from backend.dataimport.preingest3 import pipeline
    from backend.dataimport.preingest3.ratecard import default_inr_card
    FUND = [TERMS, ACCOUNTS, 'TFAI_Capital_Calls_and_Distributions_draft.xlsx']
    res = pipeline.run([(f, os.path.join(IN, f)) for f in FUND], as_of='2026-06-30', org='t',
                       rate_card=default_inr_card('2026-06-30'))
    rec = next(r for r in res.cir.records if r.domain == 'fund_terms')
    m = rec.fields['management_fee']
    # rate from the terms sheet, base/phase ENRICHED from the Fees sheet — cited to both
    assert m.value == Decimal('0.02') and m.provenance.cell == 'C18'
    assert m.base == 'committed_capital' and m.phase == 'investment_period'
    assert m.qualifiers['base_source'] == 'Fees!B3'
    # the four shared terms corroborated across two files
    assert rec.fields['carried_interest'].qualifiers.get('corroborated_by') == 'Fees!C14'
    ids = {c['id']: c['status'] for c in res.cir.checks}
    assert ids.get('term_agrees_hurdle_rate') == 'pass'


# ── synthetic negative controls ──────────────────────────────────────────────
def _pv(cell='C1'):
    return Provenance(source_file='s', content_fingerprint='', sheet='S', cell=cell)


def _term(concept, value, cell='C1', verdict='confirmed'):
    return FundTerm(concept, value, 'percent', 'n/a', 'n/a', None, _pv(cell), verdict=verdict)


def test_out_of_band_rate_is_held_NEGATIVE_CONTROL():
    # a 2% carry (below the 5% floor) is almost certainly a mis-bind → held, not shipped.
    from backend.dataimport.preingest3.fund_terms import _extract_concept, _SPEC
    grid = [[('Carry %', 'General'), (Decimal('0.02'), '0.0%')]]
    term = _extract_concept(_SPEC['carried_interest'], grid, source_label='s', sheet='S', content_fp='')
    assert term.verdict == 'held' and 'outside band' in term.hold_reason


def test_multi_source_disagreement_holds_both_NEGATIVE_CONTROL():
    a = Record('fund_terms', 'fund', {'fund': 'fund',
        'carried_interest': _term('carried_interest', Decimal('0.20'), 'C19'),
        'hurdle_rate': _term('hurdle_rate', Decimal('0.08'), 'C20')})
    b = Record('fund_terms', 'fund', {'fund': 'fund',
        'carried_interest': _term('carried_interest', Decimal('0.25'), 'C14')})   # 25% ≠ 20%
    canonical, checks = fund_terms.reconcile_fund_terms([a, b])
    ids = {c['id']: c['status'] for c in checks}
    assert ids['term_agrees_carried_interest'] == 'fail'
    assert canonical.fields['carried_interest'].verdict == 'held'                 # never silently picked


def test_period_select_picks_annual_actual_excludes_itd_and_balance():
    # THE PERIOD-SELECT (the muscle NAV reuses): among annual 20 / ITD 94.95 / payable 4,
    # the actual-annual resolver must pick 20 and exclude the cumulative + balance figures.
    from backend.dataimport.preingest3 import fund_terms as ft

    class S:
        def __init__(s, n):
            s.sheet = n
    rows = [
        [None, 'line item', 'budget', 'actual'],
        [None, 'Mgmt fee payable', None, Decimal('4')],       # balance → excluded
        [None, 'Mgmt fee (ITD, approx)', None, Decimal('94.95')],   # itd → excluded
        [None, 'Management fee', Decimal('20'), Decimal('20')],     # annual actual → chosen
    ]
    prof = {'sheets': [S('Budget vs Act')], 'grid': {'Budget vs Act': rows}}
    val, prov, cands = ft.resolve_actual_annual_fee('acc', prof, content_fp='fp')
    assert val == Decimal('20') and prov.cell == 'D4'
    periods = {c['period'] for c in cands}
    assert 'balance' in periods and 'itd' in periods            # both seen AND classified out


def test_fee_vs_actual_proves_base_when_rate_times_committed_matches():
    mt = FundTerm('management_fee', Decimal('0.02'), 'percent', 'committed_capital', 'investment_period',
                  None, _pv('C18'), qualifiers={'base_source': 'Fees!B3'})
    rec = Record('fund_terms', 'fund', {'fund': 'fund', 'management_fee': mt})
    _, checks = fund_terms.reconcile_fund_terms([rec], fee_actual=(Decimal('20'), _pv('D9'), []),
                                                committed_base=Decimal('1000'))
    assert next(c for c in checks if c['id'] == 'management_fee_vs_actual')['status'] == 'pass'
    assert mt.qualifiers['base_proven'] == 'fee_vs_actual' and mt.base == 'committed_capital'


def test_fee_vs_actual_reddens_on_wrong_actual_NEGATIVE_CONTROL():
    # THE REDDENING CONTROL: a deliberately wrong actual (30 ≠ 2%×1000=20) must FAIL the
    # tie-out, HOLD the term, and RETRACT the label-only base to UNSPECIFIED (arithmetic
    # beats a single label read — a term is not true just because two sheets copied it).
    mt = FundTerm('management_fee', Decimal('0.02'), 'percent', 'committed_capital', 'investment_period',
                  None, _pv('C18'), qualifiers={'base_source': 'Fees!B3', 'corroborated_by': 'Fees!B3'})
    rec = Record('fund_terms', 'fund', {'fund': 'fund', 'management_fee': mt})
    _, checks = fund_terms.reconcile_fund_terms([rec], fee_actual=(Decimal('30'), _pv('D9'), []),
                                                committed_base=Decimal('1000'))
    assert next(c for c in checks if c['id'] == 'management_fee_vs_actual')['status'] == 'fail'
    assert mt.verdict == 'held' and mt.base == 'UNSPECIFIED'
    assert mt.qualifiers.get('base_retracted')
    # PROPORTIONATE RETRACTION: the mismatch impugns the BASE, not the rate. The corroborated
    # rate SURVIVES (2% intact) — the term is flagged-and-disclosed, never withheld wholesale.
    assert mt.value == Decimal('0.02') and mt.qualifiers.get('corroborated_by') == 'Fees!B3'


def test_fee_vs_actual_tolerance_band_is_disclosed_and_pins_the_threshold():
    # THE BAND (Calibration 1b): the 50%-off reddening control proves the tie-out reddens
    # SOMEWHERE — this pins WHERE. rate×committed=20; actual 20.35 (1.72% off) is INSIDE the
    # ±2% band → PASS; actual 20.5 (2.44% off) is OUTSIDE → FAIL. Two straddling points pin the
    # threshold to ≈2% (not 50%), and the band is DISCLOSED in the check detail, not implicit.
    def _run(actual):
        mt = FundTerm('management_fee', Decimal('0.02'), 'percent', 'committed_capital',
                      'investment_period', None, _pv('C18'), qualifiers={'base_source': 'Fees!B3'})
        rec = Record('fund_terms', 'fund', {'fund': 'fund', 'management_fee': mt})
        _, checks = fund_terms.reconcile_fund_terms([rec], fee_actual=(actual, _pv('D9'), []),
                                                    committed_base=Decimal('1000'))
        return next(c for c in checks if c['id'] == 'management_fee_vs_actual')
    inside, outside = _run(Decimal('20.35')), _run(Decimal('20.5'))
    assert inside['status'] == 'pass' and outside['status'] == 'fail'   # threshold pinned to ≈2%
    assert '±2%' in inside['detail'] and '±2%' in outside['detail']     # band disclosed both ways


def test_held_mgmt_fee_still_emits_rate_in_fund_terms_sheet():
    # PROPORTIONATE RETRACTION at the OUTPUT layer (Calibration 2): a held mgmt-fee is FLAGGED,
    # not suppressed. The row still emits with the corroborated rate 2% visible, base UNSPECIFIED,
    # verdict 'held' — a reader sees the rate AND that its base did not tie out.
    cir = CIR(as_of='2026-06-30', rate_card_id='rc')
    cir.add(Record('fund_terms', 'fund', {'fund': 'fund',
        'management_fee': FundTerm('management_fee', Decimal('0.02'), 'percent', 'UNSPECIFIED',
                                   'investment_period', None, _pv('C18'), verdict='held',
                                   hold_reason='2%×committed 1000=20 ≠ actual 30',
                                   qualifiers={'base_retracted': 'x', 'corroborated_by': 'Fees!B3'})}))
    rows = [tuple(r) for r in assemble.build(cir)['Fund_Terms'].iter_rows(values_only=True)]
    m = next(r for r in rows if r and r[0] == 'management fee')
    assert m[1] == '2%' and m[3] == 'UNSPECIFIED' and m[6] == 'held'   # rate shown, base gone, flagged


@_real
def test_fee_vs_actual_proves_base_on_real_files():
    from backend.dataimport.preingest3 import pipeline
    from backend.dataimport.preingest3.ratecard import default_inr_card
    FUND = [TERMS, ACCOUNTS, 'TFAI_Capital_Calls_and_Distributions_draft.xlsx']
    res = pipeline.run([(f, os.path.join(IN, f)) for f in FUND], as_of='2026-06-30', org='t',
                       rate_card=default_inr_card('2026-06-30'))
    m = next(r for r in res.cir.records if r.domain == 'fund_terms').fields['management_fee']
    assert m.verdict == 'confirmed' and m.base == 'committed_capital'
    assert m.qualifiers['base_proven'] == 'fee_vs_actual'      # 2%×1000=20 reproduces the actual fee
    ids = {c['id']: c['status'] for c in res.cir.checks}
    assert ids['management_fee_vs_actual'] == 'pass'


def test_multi_source_agreement_corroborates():
    a = Record('fund_terms', 'fund', {'fund': 'fund', 'hurdle_rate': _term('hurdle_rate', Decimal('0.08'), 'C20')})
    b = Record('fund_terms', 'fund', {'fund': 'fund', 'hurdle_rate': _term('hurdle_rate', Decimal('0.08'), 'C15')})
    canonical, checks = fund_terms.reconcile_fund_terms([a, b])
    assert checks[0]['status'] == 'pass'
    assert canonical.fields['hurdle_rate'].qualifiers['corroborated_by'] == 'S!C15'


# ── output: Fund_Terms sheet + MIS byte-identical ────────────────────────────
def _terms_cir():
    cir = CIR(as_of='2026-06-30', rate_card_id='rc')
    cir.add(Record('fund_terms', 'fund', {'fund': 'fund',
        'management_fee': FundTerm('management_fee', Decimal('0.02'), 'percent', 'committed_capital',
                                   'investment_period', None, _pv('C18'), qualifiers={'base_source': 'Fees!B3'}),
        'waterfall': FundTerm('waterfall', None, 'label', 'n/a', 'whole_life', 'European whole fund', _pv('C24'))}))
    return cir


def test_fund_terms_sheet_renders_bundle_with_source_cell():
    wb = assemble.build(_terms_cir())
    rows = [tuple(r) for r in wb['Fund_Terms'].iter_rows(values_only=True)]
    m = next(r for r in rows if r and r[0] == 'management fee')
    assert m[1] == '2%' and m[3] == 'committed_capital' and m[4] == 'investment_period' and m[5] == 'S!C18'
    assert any(r and r[0] == 'waterfall' and r[1] == 'European whole fund' for r in rows)


def test_mis_only_workbook_has_no_fund_terms_sheet():
    from backend.dataimport.preingest3.cir import Figure
    mis = CIR(as_of='2026-02-28', rate_card_id='rc')
    mis.add(Record('mis', 'Acme', {'company': 'Acme',
        'revenue': Figure('revenue', Decimal('10'), None, _pv('A1'), basis='YTD', months=12)}))
    assert 'Fund_Terms' not in assemble.build(mis).sheetnames


if __name__ == '__main__':
    for n, fn in sorted(globals().items()):
        if n.startswith('test_') and callable(fn) and 'real' not in n and not n.startswith('test_terms_extracted'):
            fn(); print(f'ok  {n}')
