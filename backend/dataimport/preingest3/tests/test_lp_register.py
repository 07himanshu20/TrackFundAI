"""Phase D — LP-register slice: per-LP rows + the reconciliation identities.

Proves, on the REAL fund file and with synthetic negative controls:
  • the register extracts one row PER PARTICIPANT to its source cell (10 LPs), with
    the GP/sponsor INCLUDED as a register row (Σ(all rows) = corpus, no separate term),
    the total row and the footnote EXCLUDED (zero-phantom-LP), and the LP name carried
    as a cited string concept (entity_id + each figure's row_label);
  • every fail-closed identity REDDENS against the bug it targets, not just greens:
      - lp_total_row_multiset  — a corrupted total row holds the column (defends the
        real file's COLUMN-SHIFTED total row: bind columns from the header, not the total);
      - commitments_sum        — Σ commitment ≠ corpus holds commitment;
      - called_le_committed     — an LP with called > committed holds it (the exact
        symptom of a one-column-shifted binding);
      - cross-slice ties        — Σ called/distributed ≠ the capital-account total holds it;
  • extraction fail-closed: an ambiguous (>1) register sheet or an unresolved monetary
    frame holds every figure.
"""
import os
from decimal import Decimal

import pytest

from backend.dataimport.preingest3 import fund_extract
from backend.dataimport.preingest3.profiler import profile_file
from backend.dataimport.preingest3.ratecard import default_inr_card
from backend.dataimport.preingest3.cir import Figure, Provenance, Record

IN = 'backend/media/preingest/trivesta/100e86d5/in'
TERMS = 'TFAI_Fund_Terms_and_LP_Register_wip.xlsx'
CAPITAL = 'TFAI_Capital_Calls_and_Distributions_draft.xlsx'
ACCOUNTS = 'TFAI_Fund_Accounts_Fees_Budget_Compliance.xlsx'
_RC = default_inr_card('2026-06-30')
_real = pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present')


def _prof(name):
    return profile_file(name, os.path.join(IN, name))


def _reg(name=TERMS):
    return fund_extract.extract_lp_register(name, os.path.join(IN, name), _prof(name),
                                            rate_card=_RC, content_fp='fp')


def _capital():
    return fund_extract.extract_fund_financials(CAPITAL, os.path.join(IN, CAPITAL), _prof(CAPITAL), rate_card=_RC)


# ── REAL FILE: per-LP extraction to source cell ──────────────────────────────
@_real
def test_lp_register_extracts_ten_participants_to_source_cell():
    reg = _reg()
    assert len(reg.records) == 10 and not reg.held      # 10 LPs — footnote + total row excluded
    aditya = reg.records[0]
    assert aditya.entity_id == 'Aditya Pension Trust'
    c = aditya.fields['commitment']
    assert c.confirmed and c.value_cr == Decimal('180')
    assert c.provenance.sheet == 'LP list' and c.provenance.cell == 'E6'
    assert aditya.fields['called'].value_cr == Decimal('108') and aditya.fields['called'].provenance.cell == 'F6'
    assert aditya.fields['distributed'].value_cr == Decimal('12.6') and aditya.fields['called'].basis == 'cumulative'


@_real
def test_gp_is_a_register_row_included_not_a_separate_term():
    # THE GP-LOCATION CALIBRATION: the GP is a PARTICIPANT ROW, so Σ(all rows incl GP) =
    # corpus, with NO separate additive term. Excluding the GP would give 975 ≠ 1000;
    # adding it a second time would give 1025 ≠ 1000. The extractor does neither.
    reg = _reg()
    gp = [r for r in reg.records if r.fields.get('lp_type') == 'GP Commitment']
    assert len(gp) == 1 and gp[0].fields['commitment'].value_cr == Decimal('25')
    total = sum((r.fields['commitment'].value_cr for r in reg.records), Decimal('0'))
    without_gp = total - gp[0].fields['commitment'].value_cr
    assert total == reg.corpus_cr == Decimal('1000') and without_gp == Decimal('975')


@_real
def test_all_identities_pass_and_cross_slice_ties_to_capital_account():
    reg = _reg()
    checks = fund_extract.reconcile_lp_register(reg, capital=_capital())
    ids = {c['id']: c['status'] for c in checks}
    for cid in ('lp_total_row_multiset', 'commitments_sum', 'called_le_committed',
                'lp_called_ties_to_capital_account', 'lp_distributed_ties_to_capital_account'):
        assert ids.get(cid) == 'pass', f'{cid} did not pass: {ids.get(cid)}'
    # nothing held on the happy path
    assert not any(f.held for r in reg.records for f in r.figures())


@_real
def test_lp_name_is_a_cited_confirmed_string_concept():
    # the first NON-NUMERIC concept: the name is the entity identity AND is cited on
    # every numeric figure's row_label — you confirm the label, you don't sum it.
    reg = _reg()
    for r in reg.records:
        assert isinstance(r.entity_id, str) and r.entity_id.strip()
        for f in r.figures():
            assert f.provenance.row_label == r.entity_id       # number tied to its named row
    # the footnote string is NEVER an entity
    assert all('meets sebi min' not in (r.entity_id or '').lower() for r in reg.records)


@_real
def test_capital_and_accounts_files_carry_no_lp_register():
    assert fund_extract.extract_lp_register(CAPITAL, os.path.join(IN, CAPITAL), _prof(CAPITAL), rate_card=_RC, content_fp='') is None
    assert fund_extract.extract_lp_register(ACCOUNTS, os.path.join(IN, ACCOUNTS), _prof(ACCOUNTS), rate_card=_RC, content_fp='') is None


_CSS = '0525_CSS_Monthy_Report_-_Consol_Updated.xlsx'
_CPM = '0625_CPM_Monthly_Report-R1.xlsx'


@_real
@pytest.mark.parametrize('fn', [_CSS, _CPM])
def test_group_consolidation_mis_files_are_never_mined_as_lp_register(fn):
    # THE FUND-ROUTER-GREED GUARD (advisor 2026-08-06): a multi-entity GROUP MIS —
    # CSS's 84-sheet consolidation, CPM's 68-sheet group — must NEVER be captured as one
    # fund and mined for a per-LP register (the wrong-scope error the group-split slice is
    # deferred to). Two independent layers keep it out, both pinned here:
    #   Layer 1 — routing: MIS-FIRST wins. Both route 'mis', so they never enter fund
    #     handling. CPM is the sharp case: its workbook-wide GL detail makes
    #     _is_fund_financials TRUE in isolation, yet _route_structural still returns 'mis'
    #     because the MIS check fires FIRST. The guarantee is the ORDER — this reddens if a
    #     future reorder consults the fund test before the MIS test.
    #   Layer 2 — extraction: even if fed to the fund extractor, a group MIS has no
    #     name+commitment LP header, so extract_lp_register returns None (no phantom LPs).
    from backend.dataimport.preingest3 import pipeline
    prof = _prof(fn)
    assert pipeline._route_structural(prof) == 'mis'                                    # Layer 1
    assert fund_extract.extract_lp_register(fn, os.path.join(IN, fn), prof, rate_card=_RC, content_fp='') is None  # Layer 2


# ── SYNTHETIC grids: full control for the negative controls ──────────────────
_UNIT = [None, '(all figures in Rs Cr)', None, None, None, None, None]
_HEADER = [None, 'Sr', 'LP name', 'type', 'Commitment', 'Called', 'Distributed']


def _lp(sr, name, typ, commit, called, dist):
    return [None, sr, name, typ, Decimal(str(commit)), Decimal(str(called)), Decimal(str(dist))]


def _grid(data_rows, total=None, *, unit=True, shift_total=False, extra_rows=()):
    rows = ([_UNIT] if unit else [[None, 'Investor register', None, None, None, None, None]]) + [_HEADER]
    rows += data_rows
    if total is not None:
        tc, tk, td = (Decimal(str(x)) for x in total)
        rows.append([None, None, 'Total', tc, tk, td, None] if shift_total          # totals under D,E,F
                    else [None, None, 'Total', None, tc, tk, td])                     # totals under E,F,G
    rows += [list(r) for r in extra_rows]
    return rows


def _prof_synth(rows, corpus=None, second_lp_sheet=False):
    sheets = [type('S', (), {'sheet': 'LP list'})()]
    grid = {'LP list': rows}
    if corpus is not None:
        grid['terms'] = [[None, 'Target corpus (Rs Cr)', Decimal(str(corpus))]]
        sheets.append(type('S', (), {'sheet': 'terms'})())
    if second_lp_sheet:
        grid['LP list 2'] = list(rows)
        sheets.append(type('S', (), {'sheet': 'LP list 2'})())
    return {'sheets': sheets, 'grid': grid}


def _extract(rows, corpus=None, **kw):
    prof = _prof_synth(rows, corpus=corpus, **kw)
    return fund_extract.extract_lp_register('synth', 'synth.xlsx', prof, rate_card=_RC, content_fp='fp')


def _cap(called=None, distributed=None):
    pv = Provenance(source_file='cap', content_fingerprint='', sheet='Drawdowns', cell='D1')
    fields = {'fund': 'fund'}
    if called is not None:
        fields['called'] = Figure('called', Decimal(str(called)), None, pv, basis='cumulative')
    if distributed is not None:
        fields['distributed'] = Figure('distributed', Decimal(str(distributed)), None, pv, basis='cumulative')
    return Record('fund_financials', entity_id='fund', fields=fields)


_THREE = [_lp(1, 'Alpha LP', 'Pension', 100, 60, 10),
          _lp(2, 'Beta LP', 'Bank', 200, 120, 20),
          _lp(3, 'Gamma GP', 'GP Commitment', 300, 180, 30)]   # Σ commit 600 / called 360 / dist 60


def test_synthetic_happy_path_all_pass():
    reg = _extract(_grid(_THREE, total=(600, 360, 60)), corpus=600)
    assert len(reg.records) == 3 and not reg.held
    checks = fund_extract.reconcile_lp_register(reg, capital=_cap(called=360, distributed=60))
    assert all(c['status'] == 'pass' for c in checks if c['class'] == 'hard')


def test_shifted_total_row_still_reconciles_via_multiset():
    # the REAL file's total row is shifted one column left. Header-binding + the
    # order-agnostic multiset check accept it; a header-POSITION read of the total
    # would misread the called-total as the commitment-total (the trap we avoid).
    reg = _extract(_grid(_THREE, total=(600, 360, 60), shift_total=True), corpus=600)
    checks = {c['id']: c['status'] for c in fund_extract.reconcile_lp_register(reg)}
    assert checks['lp_total_row_multiset'] == 'pass'
    assert reg.records[0].fields['commitment'].value_cr == Decimal('100')   # from the data row, not the total


def test_total_row_multiset_reddens_on_bad_total_NEGATIVE_CONTROL():
    # a total row whose numbers do NOT reconcile to the column sums must HOLD, never
    # ship. dist Σ=60 but the stated dist total is 99 → multiset fail → dist held.
    reg = _extract(_grid(_THREE, total=(600, 360, 99)), corpus=600)
    checks = {c['id']: c['status'] for c in fund_extract.reconcile_lp_register(reg)}
    assert checks['lp_total_row_multiset'] == 'fail'
    assert all(r.fields['distributed'].held for r in reg.records)


def test_commitments_sum_reddens_on_wrong_corpus_NEGATIVE_CONTROL():
    reg = _extract(_grid(_THREE, total=(600, 360, 60)), corpus=999)     # Σ commit 600 ≠ 999
    checks = {c['id']: c['status'] for c in fund_extract.reconcile_lp_register(reg)}
    assert checks['commitments_sum'] == 'fail'
    assert all(r.fields['commitment'].held for r in reg.records)


def test_called_le_committed_reddens_NEGATIVE_CONTROL():
    # an LP whose called exceeds its commitment — the exact symptom of a one-column
    # shifted binding — must redden and hold. (no corpus/total/capital: isolate the check)
    bad = [_lp(1, 'Alpha LP', 'Pension', 100, 150, 10)]                 # 150 > 100
    reg = _extract(_grid(bad))
    checks = {c['id']: c['status'] for c in fund_extract.reconcile_lp_register(reg)}
    assert checks['called_le_committed'] == 'fail'
    assert reg.records[0].fields['called'].held and reg.records[0].fields['commitment'].held


def test_cross_slice_reddens_on_capital_mismatch_NEGATIVE_CONTROL():
    # Σ per-LP called = 360, but the capital account says 500 → the breakdown does not
    # tie to the fund total → held (never shipped). The fund analog of Σ-divisions.
    reg = _extract(_grid(_THREE, total=(600, 360, 60)), corpus=600)
    checks = {c['id']: c['status'] for c in fund_extract.reconcile_lp_register(reg, capital=_cap(called=500, distributed=60))}
    assert checks['lp_called_ties_to_capital_account'] == 'fail'
    assert all(r.fields['called'].held for r in reg.records)
    assert checks['lp_distributed_ties_to_capital_account'] == 'pass'    # dist still ties → not held
    assert not any(r.fields['distributed'].held for r in reg.records)


def test_footnote_and_blank_rows_excluded():
    # a name-only footnote (no money) and a blank row are NOT participants.
    rows = _grid(_THREE, total=(600, 360, 60),
                 extra_rows=([None, None, 'note: GP meets SEBI min', None, None, None, None],
                             [None, None, None, None, None, None, None]))
    reg = _extract(rows, corpus=600)
    assert len(reg.records) == 3
    assert all('note' not in (r.entity_id or '').lower() for r in reg.records)


def test_frame_unresolved_holds_all_figures_fail_closed():
    # no declared unit + no anchor → the monetary frame is unresolved → every figure
    # held (never assume crore).
    reg = _extract(_grid(_THREE, total=(600, 360, 60), unit=False), corpus=600)
    assert reg.held and all(f.held for r in reg.records for f in r.figures())


def test_ambiguous_two_register_sheets_holds_fail_closed():
    reg = _extract(_grid(_THREE, total=(600, 360, 60)), corpus=600, second_lp_sheet=True)
    assert reg.held and reg.records[0].entity_id == '(ambiguous)'
    assert reg.records[0].fields['commitment'].held


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn) and not name.startswith('test_lp_register_extracts'):
            try:
                fn()
                print(f'ok  {name}')
            except Exception as e:  # noqa: BLE001
                print(f'FAIL {name}: {e}')
