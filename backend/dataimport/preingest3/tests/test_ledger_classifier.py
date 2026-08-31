"""Reddening controls for the universal ledger reader, the fail-closed domain
classifier, and multi-domain SHEET SEGMENTATION.

These prove the gates FIRE, not just that they pass on clean data:
  • the capital-calls / distributions look-alike is separated (each routes to ONE domain);
  • a sheet carrying BOTH domains' markers+columns is AMBIGUOUS (≥2) → the pipeline HOLDs it;
  • a ledger whose rows don't sum to its stated total is HELD, never shipped looking-full;
  • a single sheet that STACKS two domains is SEGMENTED into two blocks (both extracted),
    and a top block never BLEEDS into the block below it;
  • an ambiguous SINGLE header extracts nothing (held), never guessed;
  • the FEES part-year rounding is closed by a NAMED, CITED reconciling line — and an
    unexplained residual (no part-year cause) stays HELD, never plugged;
  • a count-mode domain (a filing calendar) emits with NO sum tie — and its look-alike
    (a compliance checklist with no dates) is never misrouted into it.

Run:
  pytest backend/dataimport/preingest3/tests/test_ledger_classifier.py -p no:cacheprovider -o addopts="" -m ""
"""
from decimal import Decimal as D
from types import SimpleNamespace

from backend.dataimport.preingest3 import ledger
from backend.dataimport.preingest3.ledger import (CAPITAL_CALLS, DISTRIBUTIONS, FEES,
                                                  SEBI_CALENDAR, LEDGERS)
from backend.dataimport.preingest3.ratecard import default_inr_card

RC = default_inr_card('2026-06-30')


def _prof(rows):
    return {'sheets': [SimpleNamespace(sheet='S1')], 'grid': {'S1': rows}}


def _seg(rows, name='S1'):
    return ledger.segment_sheet(rows, name, LEDGERS, label='t.xlsx', content_fp='fp', rate_card=RC)


# ── look-alike separation (calls vs distributions) ────────────────────────────
def test_distributions_sheet_not_misrouted_to_capital_calls():
    rows = [['Distributions to LPs'],
            ['Dist', 'date', 'type', 'gross', 'net'],
            ['D1', '15-Jul-25', 'RoC', 40, 40]]
    assert ledger.sheet_matches(rows, DISTRIBUTIONS) is True
    assert ledger.sheet_matches(rows, CAPITAL_CALLS) is False           # the look-alike separation
    assert ledger.classify_sheets(_prof(rows), LEDGERS)['S1'] == ['distributions']


def test_capital_calls_sheet_not_misrouted_to_distributions():
    rows = [['Capital call ledger', '(Rs Cr)'],
            ['Call no', 'date', 'amount'],
            ['C1', '30-Sep-21', 150]]
    assert ledger.classify_sheets(_prof(rows), LEDGERS)['S1'] == ['capital_calls']


def test_ambiguous_sheet_matches_two_domains_so_pipeline_holds():
    """A sheet carrying BOTH domains' markers AND both key+amount columns is genuinely
    ambiguous — the classifier must return ≥2 domains so the pipeline holds it."""
    rows = [['Capital call and distribution ledger'],
            ['Call no', 'Dist', 'date', 'amount', 'net'],
            ['C1', 'D1', 'x', 150, 40]]
    doms = ledger.classify_sheets(_prof(rows), LEDGERS)['S1']
    assert set(doms) >= {'capital_calls', 'distributions'}
    assert len(doms) >= 2                                                # ≥2 → HELD by the pipeline


# ── control-total tie (within-sheet) ──────────────────────────────────────────
def test_tie_reddens_when_rows_dont_sum_to_total():
    rows = [['Capital call ledger', '(Rs Cr)'],
            ['Call no', 'date', 'amount'],
            ['C1', 'a', 150], ['C2', 'b', 140],
            ['TOTAL CALLED', '', 999]]
    lr = ledger.extract_from_sheet(rows, 'S1', CAPITAL_CALLS, label='t', content_fp='fp', rate_card=RC)
    assert lr is not None and lr.held is True
    tie = next(c for c in lr.checks if c['id'] == 'capital_calls_rows_sum_to_total')
    assert tie['status'] == 'fail'
    assert all(f.held for rec in lr.records for f in rec.figures())      # every row figure held


def test_tie_passes_when_rows_sum_to_total():
    rows = [['Capital call ledger', '(Rs Cr)'],
            ['Call no', 'date', 'amount'],
            ['C1', 'a', 150], ['C2', 'b', 140],
            ['TOTAL CALLED', '', 290]]
    lr = ledger.extract_from_sheet(rows, 'S1', CAPITAL_CALLS, label='t', content_fp='fp', rate_card=RC)
    assert lr is not None and lr.held is False
    tie = next(c for c in lr.checks if c['id'] == 'capital_calls_rows_sum_to_total')
    assert tie['status'] == 'pass'


# ── multi-domain SHEET SEGMENTATION (the user's make-or-break case) ────────────
def test_stacked_sheet_segments_into_two_blocks_both_extracted():
    rows = [['Fund cash movements (Rs Cr)'],
            ['Capital calls / drawdowns'],
            ['Call no', 'date', 'amount'],
            ['C1', 'a', 300], ['C2', 'b', 300],
            ['Total called', '', 600],
            [''],
            ['Distributions to LPs'],
            ['Dist', 'date', 'type', 'gross', 'net'],
            ['D1', 'x', 'RoC', 40, 40], ['D2', 'y', 'Div', 32, 30],
            ['Total distributed', '', '', 72, 70]]
    results, disc = _seg(rows, 'Cash movements')
    by = {r.domain: r for r in results}
    assert set(by) == {'capital_calls', 'distributions'}                # BOTH blocks extracted
    assert len(by['capital_calls'].records) == 2
    assert len(by['distributions'].records) == 2
    assert sum((f.value_cr for f in by['capital_calls'].amount_figures), D('0')) == D('600')
    assert sum((f.value_cr for f in by['distributions'].amount_figures), D('0')) == D('70')
    assert not by['capital_calls'].held and not by['distributions'].held


def test_top_block_does_not_bleed_into_the_block_below():
    """Reddening control for the [header, next-header) bound: the distributions block's
    'net' sits at the SAME column index as the calls block's 'amount', so an UNBOUNDED
    calls reader would swallow D1/D2 (→ 4 rows, Σ370). Bounded, calls sees only its 2."""
    rows = [['Capital call ledger', '(Rs Cr)'],
            ['Call no', 'date', 'amount'],
            ['C1', 'a', 150], ['C2', 'b', 150],
            ['Distributions to LPs'],
            ['Dist', 'date', 'net'],                       # 'net' at col index 2 == calls 'amount'
            ['D1', 'x', 40], ['D2', 'y', 30]]
    results, _ = _seg(rows)
    by = {r.domain: r for r in results}
    assert len(by['capital_calls'].records) == 2                        # NOT 4 — no bleed
    assert sum((f.value_cr for f in by['capital_calls'].amount_figures), D('0')) == D('300')  # NOT 370
    assert len(by['distributions'].records) == 2
    assert sum((f.value_cr for f in by['distributions'].amount_figures), D('0')) == D('70')


def test_ambiguous_single_header_extracts_nothing_and_is_disclosed():
    rows = [['Capital call and distribution ledger'],
            ['Call no', 'Dist', 'date', 'amount', 'net'],
            ['X1', 'D1', 'a', 150, 40]]
    results, disc = _seg(rows, 'Ambiguous')
    assert results == []                                                # nothing extracted (held)
    assert any(d['status'] == 'indeterminate' for d in disc)            # and disclosed


def test_single_domain_sheet_still_segments_to_one_block():
    rows = [['Capital call ledger', '(Rs Cr)'],
            ['Call no', 'date', 'amount'],
            ['C1', 'a', 150], ['C2', 'b', 150],
            ['Total called', '', 300]]
    results, _ = _seg(rows)
    assert len(results) == 1 and results[0].domain == 'capital_calls'
    assert len(results[0].records) == 2 and not results[0].held


# ── frame anchor: a LABEL-LESS block resolves from its trusted control (the deep fix) ──
_UNLABELLED_CALLS = [['Capital call ledger'],                 # NO 'Rs Cr' anywhere on the sheet
                     ['Call no', 'date', 'amount'],
                     ['C1', 'a', 150], ['C2', 'b', 150], ['C3', 'c', 150], ['C4', 'd', 150],
                     ['Total called', '', 600]]


def test_unlabelled_block_holds_without_a_frame_anchor():
    """Reddening control: a monetary block with NO unit label AND no anchor MUST hold —
    an unscaled magnitude is untrustworthy. This proves the fail-closed frame gate fires
    (the very condition the anchor fix relaxes only when an anchor is present)."""
    lr = ledger.extract_from_sheet(_UNLABELLED_CALLS, 'S1', CAPITAL_CALLS,
                                   label='t', content_fp='fp', rate_card=RC)   # anchor_cr=None
    assert lr.held is True
    assert all(f.held for rec in lr.records for f in rec.figures())


def test_unlabelled_block_resolves_from_its_control_anchor():
    """Positive control: the SAME label-less block, given its trusted control (called=600
    ₹Cr) as the frame anchor, recovers the crore scale and ties — coverage recovered
    without a label, and the exact Σ tie still verifies the scale to the rupee."""
    lr = ledger.extract_from_sheet(_UNLABELLED_CALLS, 'S1', CAPITAL_CALLS, anchor_cr=D('600'),
                                   label='t', content_fp='fp', rate_card=RC)
    assert lr.held is False
    assert sum((f.value_cr for f in lr.amount_figures), D('0')) == D('600')
    tie = next(c for c in lr.checks if c['id'] == 'capital_calls_rows_sum_to_total')
    assert tie['status'] == 'pass'


def test_wrong_magnitude_anchor_still_holds_the_block():
    """A control wildly off the rows' magnitude (anchor 6 ₹Cr vs rows ~150) leaves more
    than one scale in-band → the block still HOLDS. The anchor never FORCES a resolution;
    ambiguity is fail-closed, so a wrong control can never scale a block into a false tie."""
    lr = ledger.extract_from_sheet(_UNLABELLED_CALLS, 'S1', CAPITAL_CALLS, anchor_cr=D('6'),
                                   label='t', content_fp='fp', rate_card=RC)
    assert lr.held is True


_FOREIGN_TOKEN_CALLS = [['Capital call ledger (USD Cr)'],      # foreign token + a scale unit
                        ['Call no', 'date', 'amount'],
                        ['C1', 'a', 150], ['C2', 'b', 150], ['C3', 'c', 150], ['C4', 'd', 150],
                        ['Total called', '', 600]]


def test_foreign_token_vs_inr_anchor_is_a_conflict_and_holds():
    """Reddening control for the anchor→INR fix: the anchor's INR is POSITIVE currency
    evidence, but it must NEVER blindly override a declared FOREIGN token. A USD-tokened
    block tied to a ₹Cr control is a real currency conflict → HELD (on currency, not
    scale: the block even declares 'Cr'). This reddens the instant the anchor is made to
    force INR over a statement token, or the symmetric conflict-hold is removed."""
    lr = ledger.extract_from_sheet(_FOREIGN_TOKEN_CALLS, 'S1', CAPITAL_CALLS, anchor_cr=D('600'),
                                   label='t', content_fp='fp', rate_card=RC)
    assert lr.held is True
    # held on CURRENCY, not scale: the reason names BOTH sides of the mismatch (USD token vs
    # INR anchor) — a scale hold would never name two currencies. (The literal word 'conflict'
    # is past the 90-char hold_reason cap; USD/INR survive and are conflict-specific.)
    reasons = [f.hold_reason or '' for rec in lr.records for f in rec.figures()]
    assert any('USD' in r and 'INR' in r for r in reasons)


# ── FEES part-year reconciliation (named/cited, not a plug) ────────────────────
def _fees_rows(partyear_fee, itd_total):
    return [['Management & performance fees', '(Rs Cr)'],
            ['FY', 'committed base', 'rate', 'annual fee'],
            ['FY22', 1000, 0.02, 20], ['FY23', 1000, 0.02, 20],
            ['FY24', 1000, 0.02, 20], ['FY25', 1000, 0.02, 20],
            ['FY26 (part)', 1000, 0.02, partyear_fee],
            ['', '', 'ITD ~', itd_total]]


def test_fees_partyear_rounding_closes_to_the_rupee():
    """Σ rows = 95 vs ITD 94.95: a NAMED reconciling line (−0.05, derived from the
    part-year cell + the ITD) closes it exactly. Source rows keep their cited values."""
    lr = ledger.extract_from_sheet(_fees_rows(15, D('94.95')), 'S1', FEES,
                                   label='t', content_fp='fp', rate_card=RC)
    lr = ledger.reconcile_fee_schedule(lr, label='t', content_fp='fp')
    assert lr.held is False
    tie = next(c for c in lr.checks if c['id'] == 'fees_rows_sum_to_total')
    assert tie['status'] == 'pass'
    assert any(c['id'] == 'fees_partyear_reconciliation' for c in lr.checks)
    recon = [rec for rec in lr.records if 'rounding' in str(rec.fields.get('key', ''))]
    assert len(recon) == 1 and recon[0].fields['annual_fee'].value_cr == D('-0.05')
    # the five source rows are untouched at their cited values (20/20/20/20/15)
    src = [rec.fields['annual_fee'].value_cr for rec in lr.records
           if 'rounding' not in str(rec.fields.get('key', ''))]
    assert src == [D('20'), D('20'), D('20'), D('20'), D('15')]


def test_fees_unexplained_residual_stays_held():
    """Reddening control: a residual with NO part-year cause (all rows full-year, yet
    Σ 100 vs ITD 94.95) is NOT closeable — it must stay HELD, never plugged."""
    rows = [['Management & performance fees', '(Rs Cr)'],
            ['FY', 'committed base', 'rate', 'annual fee'],
            ['FY22', 1000, 0.02, 20], ['FY23', 1000, 0.02, 20], ['FY24', 1000, 0.02, 20],
            ['FY25', 1000, 0.02, 20], ['FY26', 1000, 0.02, 20],
            ['', '', 'ITD ~', D('94.95')]]
    lr = ledger.extract_from_sheet(rows, 'S1', FEES, label='t', content_fp='fp', rate_card=RC)
    lr = ledger.reconcile_fee_schedule(lr, label='t', content_fp='fp')
    assert lr.held is True
    tie = next(c for c in lr.checks if c['id'] == 'fees_rows_sum_to_total')
    assert tie['status'] == 'fail'


def test_fees_row_sized_gap_is_not_reconciled_away():
    """Reddening control: a whole missing FY row (part-year present but Σ short by ~20,
    far beyond rounding scale) must NOT be silently closed — stays HELD."""
    lr = ledger.extract_from_sheet(_fees_rows(15, D('114.95')), 'S1', FEES,
                                   label='t', content_fp='fp', rate_card=RC)   # ITD implies a 6th ~20 row
    lr = ledger.reconcile_fee_schedule(lr, label='t', content_fp='fp')
    assert lr.held is True


# ── SEBI count-mode + its look-alike (compliance checklist) ───────────────────
def _sebi_calendar_rows():
    return [['Statutory / regulatory calendar (SEBI)'],
            ['#', 'activity', 'freq', 'authority', 'period', 'due', 'status'],
            [1, 'Quarterly report', 'Quarterly', 'SEBI', 'Q1 FY26', '10-Jul-25', 'Filed'],
            [2, 'Independent valuation', 'Half-yearly', 'Reg 23(1)', 'H1 FY26', '31-Oct-25', 'Done']]


def test_sebi_calendar_emits_in_count_mode_without_a_sum_tie():
    lr = ledger.extract_from_sheet(_sebi_calendar_rows(), 'S1', SEBI_CALENDAR,
                                   label='t', content_fp='fp', rate_card=RC)
    assert lr is not None and lr.held is False
    assert len(lr.records) == 2
    cnt = next(c for c in lr.checks if c['id'] == 'sebi_calendar_row_count')
    assert cnt['status'] == 'disclosed' and cnt['count'] == 2            # count-not-tie, disclosed
    assert not any(c['id'].endswith('_rows_sum_to_total') for c in lr.checks)   # NO sum tie claimed


def test_sebi_compliance_checklist_not_misrouted_to_calendar():
    """The compliance checklist (requirement/ref/norm/status — NO dates) must NOT match
    the calendar: it lacks the due-date anchor that distinguishes the two look-alikes."""
    rows = [['SEBI (AIF) Regn 2012 - compliance'],
            ['#', 'requirement', 'ref', 'norm', 'status', 'remark'],
            [1, 'Registration as Cat II', 'Reg 3(4)(b)', 'Registered', 'Compliant', 'ok']]
    assert ledger.sheet_matches(rows, SEBI_CALENDAR) is False
    assert 'sebi_calendar' not in ledger.classify_sheets(_prof(rows), LEDGERS).get('S1', [])
