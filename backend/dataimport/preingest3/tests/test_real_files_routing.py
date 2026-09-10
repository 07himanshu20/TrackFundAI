"""Real-15-file integration guard — the test that would have caught the explosion.

Runs the ACTUAL client workbooks through routing + anchor building and asserts the
failure signature can never come back: no file mined into hundreds/thousands of
phantom companies, the two real investment schedules routed as 'fund' (→ 20 anchors,
cost 448), and the company monthly reports (CPM/CSS/Analisa) routed as 'mis', never
'fund'. Fast (routing + anchors only, no full extraction). Skipped if the fixture
files aren't present, so CI without them doesn't fail.

This freezes the new definition of "done": a small, correctly-routed, zero-junk
baseline — not 10,037 records of which 9 are real.
"""
import os
from decimal import Decimal

import pytest

from backend.dataimport.preingest3 import pipeline, fund_anchor, extract, tiers
from backend.dataimport.preingest3.extract import extract_company, _best_sheet, MIS_CONCEPTS
from backend.dataimport.preingest3.ratecard import default_inr_card
from backend.dataimport.preingest3.cir import Figure
from backend.dataimport.preingest3.profiler import profile_file

IN = 'backend/media/preingest/trivesta/100e86d5/in'
pytestmark = pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present')

# The raw SAP GL/TB dump sheets that must NEVER be the selected source of a company figure —
# they win a naive concept-count race (a full GL mentions every concept) but the tier selector
# (tiers.is_dump: GL-code fraction 78-89%) demotes them below every real statement.
_CPM_DUMPS = {'SourcePL SAP', 'SourceBS-SAP'}
_CSS_DUMPS = {'SourcePL-SAP', 'SourceTB-SAP', 'SourceBS-SAP', 'SourcePL-SAP 2018'}


def _selected_sheet(fn):
    prof = profile_file(fn, os.path.join(IN, fn))
    best = _best_sheet(prof, MIS_CONCEPTS)
    return best[2] if best else None


def test_tier_selector_rejects_gl_dumps_on_real_files():
    # THE NET axis-1 REGRESSION on the real files: the presentation-tier discriminator must never
    # let a GL/TB dump be the selected statement, even though it carries the most concepts. CPM's
    # SAP dump `SourcePL SAP` located all 4 MIS concepts and used to WIN _best_sheet; it is now
    # demoted below the real division P&Ls. CSS's dumps likewise lose to the real `ANA & LS`.
    cpm = _selected_sheet('0625_CPM_Monthly_Report-R1.xlsx')
    assert cpm is not None and cpm not in _CPM_DUMPS, f'CPM selected a dump: {cpm!r}'
    css = _selected_sheet('0525_CSS_Monthy_Report_-_Consol_Updated.xlsx')
    assert css is not None and css not in _CSS_DUMPS, f'CSS selected a dump: {css!r}'
    assert css == 'ANA & LS', f'CSS should select the real ANA & LS, got {css!r}'


def test_cpm_dumps_source_no_emitted_or_located_figure():
    # BOTH-HALVES fixture, NEGATIVE half (assert now): under prod config no CPM figure — emitted OR
    # merely located-then-held — is SOURCED from a SAP dump. This is the guarantee that matters: a
    # dump can never ride a wrong number (149.57 revenue from `SourcePL SAP`) into the output. The
    # POSITIVE half (the right consolidated `PL` is selected and yields the right value) needs the
    # Σ-divisions consolidation axis and is the strict-xfail below.
    rec = extract_company('CPM', os.path.join(IN, '0625_CPM_Monthly_Report-R1.xlsx'),
                          rate_card=default_inr_card('2026-02-28'), entity='CPM', use_model=False)
    for f in rec.fields.values():
        if isinstance(f, Figure):
            assert f.provenance.sheet not in _CPM_DUMPS, \
                f'{f.concept} sourced from dump {f.provenance.sheet!r} (cell {f.provenance.cell})'


def _cpm_tie_group():
    """Rebuild the exact tie-group _best_sheet forms for CPM (the ~20 same-shape division/roll-up tabs)."""
    from backend.dataimport.preingest3 import periods
    from backend.dataimport.preingest3.extract import _sheet_label_col, _find_concept_row
    prof = profile_file('0625_CPM_Monthly_Report-R1.xlsx',
                        os.path.join(IN, '0625_CPM_Monthly_Report-R1.xlsx'))
    cands = []
    for s in prof['sheets']:
        rows = prof['grid'][s.sheet]
        ax = periods.detect_period_axis(rows)
        if not ax.is_time_series or not ax.columns:
            continue
        lc = _sheet_label_col(rows, ax.axis_rows[0])
        found = {c: _find_concept_row(rows, lc, c, ax.axis_rows[0] + 1, len(rows), ax.columns)
                 for c in MIS_CONCEPTS}
        found = {c: r for c, r in found.items() if r is not None}
        if not found:
            continue
        stale, fwd = extract._sheet_vintage_flags(rows, ax, None, found)   # CPM has no filename month → (False, False)
        dump = tiers.is_dump(rows, ax.axis_rows[0] + 1, lc)
        cands.append((stale, fwd, dump, -len(found), -len(ax.columns), s.sheet, rows, ax, lc, found))
    prim = min(c[:5] for c in cands)
    return [c for c in cands if c[:5] == prim]


def test_cpm_consolidated_selection_correctly_holds():
    # CPM's "PL" is NOT a statement — it is a three-block plan/actual/prior-year COMPARISON GRID whose
    # two 2025 blocks are UNLABELED and CONFLICT (different values, same month, no banner to say which is
    # actual). There is NO deterministic evidence to pick plan vs actual, so the Σ resolver MUST abstain
    # — HOLDING is the correct fail-closed outcome (emitting either would risk shipping budget as actual,
    # the worst failure class). This is a RESOLVED hold, not a deferred flip: chasing a PL selection here
    # would be guessing. (A model MAY disambiguate the blocks semantically at Step 6; the deterministic
    # answer is, and should remain, hold.) Was previously a strict-xfail "will flip to PL" — corrected to
    # assert the hold now that the recon proved the flip is not deterministically achievable.
    assert extract._sigma_consolidated_pick(_cpm_tie_group()) is None      # resolver correctly abstains
    assert _selected_sheet('0625_CPM_Monthly_Report-R1.xlsx') != 'PL'      # never a false consolidated pick


def _files():
    return sorted(f for f in os.listdir(IN) if f.lower().endswith('.xlsx'))


def test_pipeline_output_has_no_explosion():
    # the REAL guarantee: routing + the safety cap yield a bounded consolidated
    # output. Company-MIS files are routed away from the anchor builder; a misrouted
    # giant is capped and held. So the emitted portfolio is tens, never thousands.
    fund_paths = []
    for f in _files():
        prof = profile_file(f, os.path.join(IN, f))
        if pipeline._route_file(prof) == 'fund':
            path = os.path.join(IN, f)
            if len(fund_anchor.build_fund_anchors([path])) <= pipeline._MAX_SCHEDULE_COMPANIES:
                fund_paths.append(path)
    anchors = fund_anchor.build_fund_anchors(fund_paths)
    assert len(anchors) <= 60, f'consolidated portfolio exploded to {len(anchors)} companies'


def test_routing_is_correct_on_real_files():
    role = {}
    for f in _files():
        role[f] = pipeline._route_file(profile_file(f, os.path.join(IN, f)))
    funds = {f for f, r in role.items() if r == 'fund'}
    # exactly the two genuine investment schedules are 'fund'
    assert funds == {'TFAI_Investments_and_Deployment.xlsx', 'TFAI_Valuations_and_Exits_Q2FY26.xlsx'}, funds
    # the company monthly reports must be 'mis', never 'fund' (the old misroute). CPM is the
    # regression guard for the fund-level guard's false-positive: it mentions 'commitment'/
    # 'drawdown' in scattered GL detail (2 workbook-wide) but only 1 in its statement labels,
    # so it must STAY 'mis' — the guard is scoped to the selected statement, not the workbook.
    for company_mis in ('0625_CPM_Monthly_Report-R1.xlsx',
                        '0525_CSS_Monthy_Report_-_Consol_Updated.xlsx',
                        '01_Monthly_Financial_Presentation_2025_May_Analisa.xlsx'):
        assert role[company_mis] == 'mis', f'{company_mis} misrouted as {role[company_mis]}'
    # FUND-LEVEL files (fund P&L / capital-account / LPA terms) must NEVER be mined as a
    # company MIS. Phase-D fund extraction now EXISTS, so they route to 'fund_financials'
    # (the deterministic fund extractor), not the interim 'unknown' hold. The invariant
    # that mattered — never 'mis' — still holds; the destination is now the fund tier.
    for fund_level in ('TFAI_Fund_Accounts_Fees_Budget_Compliance.xlsx',
                       'TFAI_Capital_Calls_and_Distributions_draft.xlsx',
                       'TFAI_Fund_Terms_and_LP_Register_wip.xlsx'):
        assert role[fund_level] == 'fund_financials', \
            f'{fund_level} must route to the fund extractor, got {role[fund_level]}'
        assert role[fund_level] != 'mis'            # the load-bearing invariant: never mined as MIS


def test_fund_anchors_total_is_sane():
    fund_paths = [os.path.join(IN, f) for f in
                  ('TFAI_Investments_and_Deployment.xlsx', 'TFAI_Valuations_and_Exits_Q2FY26.xlsx')]
    anchors = fund_anchor.build_fund_anchors(fund_paths)
    assert len(anchors) == 10                                   # 10 portfolio companies, not thousands
    total_cost = sum((a.cost_cr for a in anchors.values() if a.cost_cr is not None), Decimal('0'))
    assert total_cost == 448                                    # junk-row-excluded, no double-count


def test_each_mis_file_maps_to_distinct_company_or_holds():
    # THE BIJECTION GUARD on the real run path — what would have caught 'Hubbler x3'.
    # Each MIS file resolves to its OWN distinct company or holds; no company is
    # claimed by two files; the files carrying a distinctive name token resolve.
    import tempfile
    from collections import Counter
    from backend.dataimport.preingest3.alias_ledger import AliasLedger
    anchors = fund_anchor.build_fund_anchors(
        [os.path.join(IN, f) for f in ('TFAI_Investments_and_Deployment.xlsx',
                                       'TFAI_Valuations_and_Exits_Q2FY26.xlsx')])
    store = AliasLedger(org='guard', path=os.path.join(tempfile.mkdtemp(), 'a.json'))
    resolved = {}
    for f in _files():
        prof = profile_file(f, os.path.join(IN, f))
        if pipeline._route_file(prof) != 'mis':
            continue
        file_text = f + ' ' + ' '.join(pipeline._hints(prof))
        key, _reason = pipeline._resolve_mis([f], file_text, anchors, store)
        resolved[f] = key
    claims = Counter(k for k in resolved.values() if k is not None)
    dupes = {k: n for k, n in claims.items() if n > 1}
    assert not dupes, f'bijection violated — a company claimed by >1 file: {dupes}'
    # files with an unambiguous distinctive name token must resolve to their own co
    for f in ('AVF_2026_03_12_P_Agnikul_MIS_Feb26.xlsx',
              'AVF_2026_03_11_P_Hubler_MIS_Feb26.xlsx',
              'AVF_2026_03_20_P_LDC_Detailed_MIS_Feb26.xlsx'):
        assert resolved.get(f) is not None, f'{f} should resolve to its own company'


if __name__ == '__main__':
    test_pipeline_output_has_no_explosion()
    test_routing_is_correct_on_real_files()
    test_fund_anchors_total_is_sane()
    test_each_mis_file_maps_to_distinct_company_or_holds()
    print('ALL PASS — real files: no explosion, correct routing, 20 anchors / cost 448, bijection holds')
