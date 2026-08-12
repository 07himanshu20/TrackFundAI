"""Phase D — fund NAV, struck by the capital-account ROLL-FORWARD.

The governing recon finding (measured, not assumed): NAV is NOT a stated scalar on the
real fund files. Investments-at-fair-value — the largest balance-sheet term — is never
stated in any fund file (only P&L flows are), so there is structurally no second path to
NAV. The one deterministic path is the roll-forward, every input a cited stated cell:

    gross NAV  = called − distributed + net ITD P&L (before carry)
    LP    NAV  = gross NAV − accrued carry provision

Per the NAV design ruling, NAV is emitted as a LABELED BOUNDED RANGE = two DEFINED bases,
not a fuzzy band and not a silently-picked point:
  • LP NAV (net of accrued carry)   — PRIMARY   (what LPs hold a claim to)
  • Gross NAV (before carry)         — SECONDARY (gross/total NAV)
The span between them is the one bounded soft input (the 'approx' carry). The range handles
the carry fork; it does NOT fix single-sourcing — the anchor's POSITION is uncorroborated
in the fund files, a PERMANENT label on this fund's NAV (not 'pending more data').

The emit-vs-hold RULE (universal, so the coming files resolve themselves):
    EMIT (two bases) when the roll-forward is COMPLETE — every hard component a cited
    stated cell — and the only residual uncertainty is BOUNDED soft inputs.
    HOLD when any hard component is missing, the identity does not reconcile (the as-of
    cutoffs disagree), or the uncertainty is unbounded.

Two guards land here, both fail-closed and both with negative controls:
  • AS-OF CONSISTENCY — every roll-forward input struck to the SAME cutoff as the NAV
    date. A flow dated AFTER the as-of, or a stated total not fully explained by dated
    flows within the cutoff, silently mixes periods → HOLD.
  • CARRY CROSS-CHECK — the 'approx' accrued carry must sit in (0, ceiling], where
    ceiling = the already-VERIFIED carry rate × total ITD gain (realised + unrealised).
    This is fee-vs-actual's spirit applied to carry: the soft input is corroborated by a
    term we already proved. Exact hurdle-weighted recompute is deferred to the waterfall
    slice; here we bound it.

The model is OFF (build rules #1-3): every figure is a label-anchored read of a stated
cell, and every derived figure cites the cells it came from (Provenance.derived_from).
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Tuple

from . import lexicon, reconcile
from .cir import Figure, Provenance, Record
from .quantity import format_pct, to_decimal

_FEE_TOL = Decimal('0.02')          # fee-vs-actual NAV-leg: same ±band the terms slice discloses
_DATE_FORMATS = ('%d-%b-%y', '%d-%b-%Y', '%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y')

# labels that anchor the roll-forward inputs — authored in NATURAL form (with '&', case,
# punctuation). lexicon.label_matches_any normalises both sides and matches WHOLE words, so
# 'Fund P&L' matches 'Fund P&L (inception to date)' but NOT a stray '…ITD P&L below' as-of
# line (which lacks the whole 'Fund P&L' run) — no hand-normalisation, no greedy substring.
_PNL_HEADING = ('Fund P&L', 'Fund PnL', 'Fund Profit and Loss', 'Profit and Loss',
                'Income Statement', 'Statement of Operations', 'Statement of Profit and Loss')
_NAV_ASOF_HINT = ('for NAV', 'NAV as at', 'Net Asset Value as', 'balances as on', 'NAV component')
_REALISED = ('realised gain', 'realized gain', 'realised gains', 'realized gains')
_UNREALISED = ('unrealised', 'unrealized', 'change in fair value', 'change in FV')
_CARRY_NOTE = ('carry provision', 'carried interest provision', 'accrued carry', 'carry accrual')

# ── §5.1 balance-sheet NAV build — the SECOND, independent path to NAV. Its largest term
# (investments at fair value) is NOT in the fund file (nav.py's governing finding), but IS
# available cross-file as Σ per-company FV (the Total-FV=Σ check ties it). The remaining
# terms — cash, receivables, payables, other liabilities — sit in the fund file's 'NAV
# component balances' block, read here as label-anchored cited cells (model OFF). The two
# paths CORROBORATE; their delta is a soft, always-published reconciling item (U7). ──
_NAV_COMP_HEADING = ('NAV component balances', 'NAV components', 'Net asset value components',
                     'NAV component', 'Balance sheet', 'Statement of assets and liabilities')
_BS_LIABILITY = ('payable', 'payables', 'liabilities', 'liability', 'borrowings', 'borrowing',
                 'accruals', 'provisions', 'creditors')
_BS_ASSET = ('cash', 'bank', 'receivable', 'receivables', 'debtors', 'deposits', 'accrued income')


def _bs_role(label) -> Optional[str]:
    """Asset(+) or liability(−) for a NAV-component line, by whole-word label match — the
    universal shared matcher, never a hand-rolled substring. Liability checked first so
    'Mgmt fee payable' is a liability, not mis-read as an asset."""
    if _matches(label, _BS_LIABILITY):
        return 'liability'
    if _matches(label, _BS_ASSET):
        return 'asset'
    return None


def _read_nav_components(grid):
    """The 'NAV component balances' block (cash, receivables, payables, other liabilities) —
    each a label-anchored cited cell. Stops at the next section heading (the P&L block).
    Returns [(label, value, cell)]; empty if the file bears no such block."""
    start = None
    for r, row in enumerate(grid):
        for v in row:
            if isinstance(v, str) and _matches(v, _NAV_COMP_HEADING):
                start = r
                break
        if start is not None:
            break
    if start is None:
        return []
    comps = []
    for r in range(start + 1, len(grid)):
        row = grid[r]
        label = next((v for v in row if isinstance(v, str) and v.strip()), '')
        if label and _matches(label, _PNL_HEADING):      # next section — stop
            break
        cell = next(((to_decimal(v), c) for c, v in enumerate(row) if isinstance(v, (int, float))), None)
        if cell is None:
            continue
        comps.append((str(label).strip(), cell[0], _a1(cell[1], r)))
    return comps


# ── small helpers ────────────────────────────────────────────────────────────
def _a1(col: Optional[int], row: Optional[int]) -> str:
    if col is None or row is None:
        return ''
    from openpyxl.utils import get_column_letter
    return f'{get_column_letter(col + 1)}{row + 1}'


def _matches(label, synonyms) -> bool:
    """Whole-word, normalise-both-sides label match — the ONE shared matcher. No module
    hand-rolls its own (that is how the raw-vs-normalised/substring bug classes recur)."""
    return lexicon.label_matches_any(label, synonyms)


def _num_in(text) -> Optional[Decimal]:
    m = re.search(r'-?\d[\d,]*\.?\d*', str(text).replace('–', '-'))
    return to_decimal(m.group(0).replace(',', '')) if m else None


def _parse_date(v) -> Optional[_dt.date]:
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    if not isinstance(v, str):
        return None
    s = v.strip()
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _as_of_date(as_of: str) -> Optional[_dt.date]:
    return _parse_date(as_of)


# ── per-file NAV signals ─────────────────────────────────────────────────────
_EXCEL_ERRORS = {'#DIV/0!', '#REF!', '#N/A', '#VALUE!', '#NAME?', '#NULL!', '#NUM!',
                 '#ERROR!', '#SPILL!', '#CALC!'}


@dataclass
class NAVInputs:
    source: str
    sheet: str = ''
    pnl: List[Tuple[str, Decimal, str]] = field(default_factory=list)      # (label, value, cell)
    carry: Optional[Tuple[Decimal, str]] = None                            # (value, cell)
    carry_unreadable: bool = False                                         # note present but no parseable number
    realised: Optional[Tuple[Decimal, str]] = None
    unrealised: Optional[Tuple[Decimal, str]] = None
    nav_asof_label: Optional[Tuple[str, str]] = None                       # (text, cell)
    flows: List[Tuple[_dt.date, Decimal, str, str]] = field(default_factory=list)  # (date, amt, cell, sheet)
    pnl_hole: bool = False                                                 # a line has an error / missing value
    pnl_hole_reason: str = ''
    nav_components: List[Tuple[str, Decimal, str]] = field(default_factory=list)  # §5.1 BS block (label,val,cell)

    @property
    def has_pnl(self) -> bool:
        return bool(self.pnl)


def _read_pnl_section(grid):
    """Read the P&L line items (each a cited numeric cell), plus realised / unrealised /
    carry-note, from the sheet that bears a P&L heading. The carry NOTE is EXCLUDED from the
    P&L sum (deducted separately in the waterfall) and returned on its own.

    HOLE-DISCIPLINE (the same guarantee the MIS side gives its sums): the net P&L is a sum of
    every line, so a single hole makes it INCOMPLETE, never a silent short-sum. A hole is
    either (a) an Excel ERROR value in a line's value cell (a broken formula), or (b) a
    LABELLED line-item BETWEEN real lines with no value (a genuine missing value, distinct
    from a fully-blank separator row which is legitimately skipped). Returns
    (pnl, realised, unrealised, carry, carry_unreadable, hole, hole_reason)."""
    pnl_start = None
    for r, row in enumerate(grid):
        for v in row:
            if isinstance(v, str) and _matches(v, _PNL_HEADING):
                pnl_start = r
                break
        if pnl_start is not None:
            break
    if pnl_start is None:
        return [], None, None, None, False, False, ''
    pnl, realised, unrealised, carry = [], None, None, None
    carry_unreadable = False
    error_hole = None
    labelled_no_value = []          # (row, label) — candidate holes (missing value on a named line)
    last_numeric_row = pnl_start
    for r in range(pnl_start + 1, len(grid)):
        row = grid[r]
        for v in row:               # an Excel error anywhere in a P&L row is a hole
            if isinstance(v, str) and v.strip() in _EXCEL_ERRORS and error_hole is None:
                error_hole = (r, v.strip())
        label = next((v for v in row if isinstance(v, str)), '')
        # carry NOTE: value lives in prose ('approx Rs 61.6 Cr') or a cell — take it out of the sum
        if _matches(label, _CARRY_NOTE):
            cval = next(((to_decimal(v), c) for c, v in enumerate(row) if isinstance(v, (int, float))), None)
            if cval is not None:
                carry = (cval[0], _a1(cval[1], r))
            else:
                n = _num_in(label)
                carry = (n, '') if n is not None else None
                carry_unreadable = n is None          # note matched but no number → unreadable
            continue
        cell = next(((to_decimal(v), c) for c, v in enumerate(row) if isinstance(v, (int, float))), None)
        if cell is None:
            if label and str(label).strip():          # a NAMED line with no value — candidate hole
                labelled_no_value.append((r, label))
            continue
        val, c = cell
        pnl.append((label, val, _a1(c, r)))
        last_numeric_row = r
        if realised is None and _matches(label, _REALISED):
            realised = (val, _a1(c, r))
        if unrealised is None and _matches(label, _UNREALISED):
            unrealised = (val, _a1(c, r))
    # holes: an Excel error anywhere, or a NAMED line with no value BEFORE the last real line
    # (a trailing named row after the last value is section-end, not a hole).
    reasons = []
    if error_hole is not None:
        reasons.append(f'error {error_hole[1]} at row {error_hole[0] + 1}')
    for r, lbl in labelled_no_value:
        if r < last_numeric_row:
            reasons.append(f'line "{str(lbl)[:24]}" (row {r + 1}) has no value')
    return pnl, realised, unrealised, carry, carry_unreadable, bool(reasons), '; '.join(reasons)


def _read_flows(grid, sheet: str) -> List[Tuple[_dt.date, Decimal, str, str]]:
    """Dated flow rows (a parseable date + a numeric amount to its right) — the calls and
    distributions ledgers, for the as-of cutoff guard. Non-hardcoded: any dated+amount row."""
    out = []
    for r, row in enumerate(grid):
        for c, v in enumerate(row):
            d = _parse_date(v)
            if d is None:
                continue
            amt = next((to_decimal(x) for x in row[c + 1:] if isinstance(x, (int, float))), None)
            if amt is not None:
                out.append((d, amt, _a1(c, r), sheet))
            break
    return out


def extract_nav_inputs(label, path, prof, *, as_of: str, content_fp='') -> Optional[NAVInputs]:
    """Collect this file's NAV-relevant signals: the P&L section (if the file bears one),
    the NAV as-of line, and any dated flow rows. Returns None if the file has neither a P&L
    section nor dated flows (nothing NAV can use)."""
    ni = NAVInputs(source=label)
    for s in prof['sheets']:
        grid = prof['grid'][s.sheet]
        pnl, realised, unrealised, carry, carry_unreadable, hole, hole_reason = _read_pnl_section(grid)
        if pnl and not ni.pnl:
            ni.sheet, ni.pnl, ni.realised, ni.unrealised, ni.carry = s.sheet, pnl, realised, unrealised, carry
            ni.carry_unreadable, ni.pnl_hole, ni.pnl_hole_reason = carry_unreadable, hole, hole_reason
            for r, row in enumerate(grid):
                for c, v in enumerate(row):
                    if isinstance(v, str) and _matches(v, _NAV_ASOF_HINT) and ni.nav_asof_label is None:
                        ni.nav_asof_label = (v.strip(), f'{s.sheet}!{_a1(c, r)}')
        comps = _read_nav_components(grid)                # §5.1 balance-sheet block (second path)
        if comps and not ni.nav_components:
            ni.nav_components = [(lbl, v, f'{s.sheet}!{c}') for lbl, v, c in comps]
        ni.flows.extend(_read_flows(grid, s.sheet))
    return ni if (ni.has_pnl or ni.flows or ni.nav_components) else None


# ── roll-forward + guards (post-loop, cross-file) ────────────────────────────
def _pv(source, fp, sheet, cell, *, row_label='', derived=None) -> Provenance:
    return Provenance(source_file=source, content_fingerprint=fp, sheet=sheet, cell=cell,
                      row_label=row_label, derived_from=derived or [])


def _flow_cutoff_ok(flows, as_of_d, total, *, tol=Decimal('0.01')):
    """A stated total is 'clean to the cutoff' when the dated flows that sum to it are all
    on/before the as-of. Returns (ok, after_asof, matched_sum). A flow after as-of, or no
    flow-group summing to the total, means the cutoff cannot be confirmed → caller HOLDs."""
    if not flows:
        return None, [], None                     # no dated flows seen for this total
    by_sheet = {}
    for d, amt, cell, sheet in flows:
        by_sheet.setdefault(sheet, []).append((d, amt))
    for sheet, rows in by_sheet.items():
        ssum = sum((a for _, a in rows), Decimal('0'))
        if abs(ssum - total) <= tol:
            after = [(str(d)) for d, _ in rows if d > as_of_d]
            return (not after), after, ssum
    return None, [], None


def reconcile_nav(inputs_list, *, capital, carry_rate, carry_rate_prov, as_of,
                  fee_actual=None, committed_base=None, source_fp='', portfolio_fv=None):
    """Strike NAV from the roll-forward and materialise its guards. Returns
    (nav_record | None, checks, disclosures). When the §5.1 balance-sheet components and the
    Σ portfolio FV are both present, ALSO strikes the independent balance-sheet NAV and
    publishes the two-path reconciliation (always, per U7 — the delta is the finding)."""
    inputs_list = [i for i in inputs_list if i is not None]
    pnl_src = next((i for i in inputs_list if i.has_pnl), None)
    if pnl_src is None or capital is None:
        return None, [], []
    checks: List[dict] = []
    disc: List[dict] = []
    as_of_d = _as_of_date(as_of)

    # ── hard components: called, distributed (from the capital account, another file) ──
    called_fig = capital.fields.get('called')
    dist_fig = capital.fields.get('distributed')
    called = called_fig.value_cr if isinstance(called_fig, Figure) and called_fig.confirmed else None
    distributed = dist_fig.value_cr if isinstance(dist_fig, Figure) and dist_fig.confirmed else None
    net_pnl = sum((v for _, v, _ in pnl_src.pnl), Decimal('0'))
    carry = pnl_src.carry[0] if pnl_src.carry else None

    hold_reasons: List[str] = []
    if called is None:
        hold_reasons.append('called capital missing/held')
    if distributed is None:
        hold_reasons.append('distributions missing/held')
    if not pnl_src.pnl:
        hold_reasons.append('P&L incomplete')
    if pnl_src.pnl_hole:                              # FOLD 3a: a hole in the 8-line P&L sum
        hold_reasons.append(f'P&L has a hole ({pnl_src.pnl_hole_reason}) — net P&L not a clean sum')

    # ── GUARD 1: as-of consistency across every input. An input whose cutoff cannot be
    #    CONFIRMED holds the roll-forward, fail-closed — a silently short-summed or
    #    period-mixed total is worse than an honest INCOMPLETE. ──
    asof_ok, asof_detail = True, []
    if pnl_src.nav_asof_label is not None and as_of_d is not None:
        stated = _parse_date_from_text(pnl_src.nav_asof_label[0])
        if stated is not None and stated != as_of_d:
            asof_ok = False
            asof_detail.append(f'P&L stated as-of {stated} ≠ NAV as-of {as_of_d}')
    all_flows = pnl_src.flows or _all_flows(inputs_list)
    for total, name in ((called, 'called'), (distributed, 'distributed')):
        if total is None or as_of_d is None:
            continue
        ok, after, ssum = _flow_cutoff_ok(all_flows, as_of_d, total)
        if ok is False:                              # a tying group straddles the as-of
            asof_ok = False
            asof_detail.append(f'{name}: flow(s) dated after {as_of_d}: {after}')
        elif ok is None and all_flows:               # FOLD 3b: flows exist but none ties — an
            asof_ok = False                          # unparsed date or a missing flow → cannot
            asof_detail.append(f'{name}: no dated-flow group sums to {total} — cutoff '  # confirm → HOLD
                               'unconfirmed (an unparsed date format or a missing flow)')
        elif ok is None:                             # no dated ledger at all — disclose, do not hold
            disc.append({'kind': 'nav_cutoff_unconfirmed', 'entity': 'fund',
                         'detail': f'{name}: no dated ledger to confirm the {as_of_d} cutoff'})
    checks.append(reconcile._result('nav_as_of_consistency', reconcile.HARD,
                  reconcile.PASS if asof_ok else reconcile.FAIL,
                  detail=('all roll-forward inputs share the ' + str(as_of_d) + ' cutoff'
                          if asof_ok else '; '.join(asof_detail))))
    if not asof_ok:
        hold_reasons.append('as-of cutoffs disagree/unconfirmed — roll-forward would mix periods')
    # date-format coverage boundary, disclosed so a wave of as-of HOLDs on real files is not a
    # surprise: the accepted formats are finite and GROW (each addition confirmed + tested).
    disc.append({'kind': 'nav_date_format_boundary', 'entity': 'fund',
                 'detail': 'as-of cutoff dates parsed from a fixed format set '
                           f'({", ".join(_DATE_FORMATS)}); an unrecognised format fail-closes to HOLD '
                           '(known coverage boundary — the set grows as real files reveal formats)'})

    # ── GUARD 2: carry cross-check — a one-sided PLAUSIBILITY CEILING, not a carry proof.
    #    It rules out a carry that is too HIGH (> the no-hurdle ceiling of the VERIFIED rate);
    #    it cannot catch one that is too LOW. And with a whole-fund waterfall and LPs far from
    #    capital-back (called ≫ distributed), this carry is necessarily an UNREALISED fair-value
    #    ACCRUAL — its basis (hurdle applied? gross/net of fee?) is a convention the waterfall
    #    slice reconciles. So a PASS corroborates the magnitude is not absurd; it does NOT
    #    validate the number. ──
    carry_corroborated = None
    ceiling = None
    if carry is not None and carry_rate is not None and pnl_src.realised and pnl_src.unrealised:
        ceiling = carry_rate * (pnl_src.realised[0] + pnl_src.unrealised[0])
        carry_corroborated = Decimal('0') < carry <= ceiling
        checks.append(reconcile._result('nav_carry_plausibility_ceiling', reconcile.SOFT,
                      reconcile.PASS if carry_corroborated else reconcile.FAIL,
                      lhs=str(carry), rhs=str(ceiling),
                      detail=(f'accrued carry {carry} within (0, ceiling {ceiling}] = '
                              f'{_pct(carry_rate)}×(realised {pnl_src.realised[0]}+unrealised '
                              f'{pnl_src.unrealised[0]}) — magnitude plausible vs the verified rate '
                              f'(ONE-SIDED: rules out too-high, not too-low; full validation → waterfall)'
                              if carry_corroborated else
                              f'accrued carry {carry} exceeds ceiling {ceiling} — inconsistent '
                              f'with the verified {_pct(carry_rate)} carry rate')))
        disc.append({'kind': 'nav_carry_basis', 'entity': 'fund',
                     'detail': f'accrued carry {carry} is an UNREALISED fair-value accrual (whole-fund '
                               'waterfall, LPs not at capital-back — no carry crystallised); its basis '
                               '(hurdle applied, gross/net of fee) is reconciled by the waterfall slice, '
                               'not validated here'})
    elif carry is not None:
        disc.append({'kind': 'nav_carry_uncorroborated', 'entity': 'fund',
                     'detail': 'carry plausibility ceiling indeterminate — verified carry rate or ITD gains absent'})

    # ── roll-forward ──
    can_emit_gross = not hold_reasons
    gross_nav = (called - distributed + net_pnl) if can_emit_gross else None
    # LP NAV also needs the carry; gross NAV never does. FOLD 2: carry is the first PROSE-sourced
    # figure (a note), so 'absent' and 'present-but-unreadable' are both live — either holds LP NAV
    # while gross still emits (proportionate: the unknown carry impugns only the net-of-carry base).
    lp_hold = list(hold_reasons)
    if carry is None and pnl_src.carry_unreadable:
        lp_hold.append('accrued carry note present but unreadable — LP NAV cannot net an unknown carry')
        disc.append({'kind': 'nav_carry_unreadable', 'entity': 'fund',
                     'detail': 'the accrued-carry note was found but no figure could be parsed from its '
                               'prose — LP NAV held, gross NAV (carry-independent) still emitted'})
    elif carry is None:
        lp_hold.append('accrued carry absent — LP NAV cannot net an unknown carry')
    if carry_corroborated is False:
        lp_hold.append('carry exceeds plausibility ceiling — inconsistent with verified rate')
    lp_nav = (gross_nav - carry) if (gross_nav is not None and not lp_hold) else None

    # ── §5.1 balance-sheet NAV — the second, independent path (Assets − Liabilities). Its FV
    #    term is Σ per-company FV (cross-file, cited by the Total-FV=Σ check); the other terms
    #    are the 'NAV component balances' cells. The two paths corroborate; the delta is a soft,
    #    ALWAYS-published reconciling item — NOT hidden inside a tolerance (U7 / D7). The
    #    balance-sheet FV aggregates per-company marks across as-ofs, so the roll-forward stays
    #    the operative NAV for downstream metrics; the §5.1 build is the corroboration. ──
    bs_components = list(pnl_src.nav_components)
    bs_assets = sum((v for lbl, v, _ in bs_components if _bs_role(lbl) == 'asset'), Decimal('0'))
    bs_liabs = sum((v for lbl, v, _ in bs_components if _bs_role(lbl) == 'liability'), Decimal('0'))
    bs_gross = bs_lp = None
    if bs_components and portfolio_fv is not None:
        pf = to_decimal(portfolio_fv) if not isinstance(portfolio_fv, Decimal) else portfolio_fv
        bs_gross = pf + bs_assets - bs_liabs
        bs_lp = (bs_gross - carry) if carry is not None else None
        if gross_nav is not None:
            delta = (gross_nav - bs_gross)
            rel = (abs(delta) / abs(gross_nav)) if gross_nav else None
            checks.append(reconcile._result('nav_two_path_reconciliation', reconcile.SOFT,
                          reconcile.PASS if (rel is not None and rel <= Decimal('0.05')) else reconcile.FAIL,
                          lhs=str(gross_nav.quantize(Decimal('0.01'))), rhs=str(bs_gross.quantize(Decimal('0.01'))),
                          detail=(f'gross NAV: roll-forward {gross_nav.quantize(Decimal("0.01"))} vs §5.1 '
                                  f'balance-sheet {bs_gross.quantize(Decimal("0.01"))} '
                                  f'(ΣFV {pf}+assets {bs_assets}−liab {bs_liabs}); '
                                  f'Δ={delta.quantize(Decimal("0.01"))} '
                                  f'({(rel*100).quantize(Decimal("0.1")) if rel is not None else "?"}%) '
                                  '— two independent paths, published regardless (the Δ is the reconciling item: '
                                  'roll-forward unrealised-FV mark vs Σ per-company FV)')))
        disc.append({'kind': 'nav_balance_sheet_path', 'entity': 'fund',
                     'detail': f'§5.1 NAV = ΣFV {pf} + cash/receivables {bs_assets} − payables/liabilities '
                               f'{bs_liabs} = gross {bs_gross}; − accrued carry {carry if carry is not None else "—"} '
                               f'= LP {bs_lp}. Components: '
                               + '; '.join(f'{lbl}={v}@{c}' for lbl, v, c in bs_components)
                               + '. FV term aggregates per-company marks across as-ofs (dual-as-of caveat).'})

    # ── build the NAV record: two DEFINED bases, each derived-from its cited cells ──
    pnl_cells = [f'{pnl_src.sheet}!{c}' for _, _, c in pnl_src.pnl]
    called_cell = f'{called_fig.provenance.sheet}!{called_fig.provenance.cell}' if isinstance(called_fig, Figure) else ''
    dist_cell = f'{dist_fig.provenance.sheet}!{dist_fig.provenance.cell}' if isinstance(dist_fig, Figure) else ''
    derived = [called_cell, dist_cell] + pnl_cells

    gross_prov = _pv(pnl_src.source, source_fp, pnl_src.sheet, '', row_label='gross NAV (before carry)', derived=derived)
    lp_prov = _pv(pnl_src.source, source_fp, pnl_src.sheet, '', row_label='LP NAV (net of accrued carry)',
                  derived=derived + ([pnl_src.carry[1]] if pnl_src.carry and pnl_src.carry[1] else []))

    fields = {
        'fund': 'fund',
        'gross_nav': Figure('gross_nav', gross_nav, None, gross_prov, basis='point_in_time',
                            held=not can_emit_gross,
                            hold_reason='; '.join(hold_reasons)[:90] if hold_reasons else ''),
        'lp_nav': Figure('lp_nav', lp_nav, None, lp_prov, basis='point_in_time',
                         held=lp_nav is None,
                         hold_reason='; '.join(lp_hold)[:90] if lp_nav is None else ''),
        'as_of': as_of,
        'single_source': True,          # PERMANENT for this fund (investments-at-FV never stated)
        'carry_value': str(carry) if carry is not None else '',
        'carry_ceiling': str(ceiling) if ceiling is not None else '',
        'carry_corroborated': carry_corroborated,
        'derivation': 'called − distributed + net ITD P&L (− accrued carry for LP NAV)',
        'components': [('called', str(called), called_cell), ('distributed', str(distributed), dist_cell),
                       ('net ITD P&L (before carry)', str(net_pnl), ','.join(pnl_cells)),
                       ('accrued carry', str(carry) if carry is not None else '—',
                        pnl_src.carry[1] if pnl_src.carry else '')],
        # §5.1 balance-sheet path (the corroborating second path) — cited components + result
        'bs_components': [(lbl, str(v), c) for lbl, v, c in bs_components],
        'bs_portfolio_fv': str(portfolio_fv) if portfolio_fv is not None else '',
        'bs_gross_nav': str(bs_gross) if bs_gross is not None else '',
        'bs_lp_nav': str(bs_lp) if bs_lp is not None else '',
    }
    rec = Record('nav', entity_id='fund', fields=fields)

    # ── the fee-vs-actual NAV LEG: does 2%×NAV reproduce the actual fee? It must NOT —
    #    that is what positively rules NAV out as the fee base and leaves committed unique. ──
    if fee_actual is not None and committed_base is not None and gross_nav is not None:
        actual_val, actual_prov, _ = fee_actual
        rate = _fee_rate_from_committed(committed_base, actual_val)
        if actual_val not in (None, 0) and rate is not None:
            legs = []
            ruled_out_all = True
            for base_name, base_val in (('LP NAV', lp_nav), ('gross NAV', gross_nav)):
                if base_val is None:
                    continue
                fee_on = rate * base_val
                dev = abs(fee_on - actual_val) / abs(actual_val)
                out = dev > _FEE_TOL
                ruled_out_all = ruled_out_all and out
                legs.append(f'{_pct(rate)}×{base_name} {base_val}={fee_on.quantize(Decimal("0.01"))} '
                            f'({(dev*100).quantize(Decimal("0.1"))}% off actual {actual_val}) '
                            f'→ {"ruled out" if out else "cannot separate"}')
            checks.append(reconcile._result('fee_base_vs_nav', reconcile.HARD,
                          reconcile.PASS if ruled_out_all else reconcile.INDETERMINATE,
                          detail=('NAV ruled out as the fee base (committed uniquely confirmed) within '
                                  f'±{_pct(_FEE_TOL)}: ' + '; '.join(legs) if ruled_out_all
                                  else 'NAV too close to actual to separate from committed: ' + '; '.join(legs))))

    # ── disclosures: single-source is PERMANENT, and the derivation is fully cited ──
    disc.append({'kind': 'nav_single_source', 'entity': 'fund',
                 'detail': 'NAV is single-source (capital-account roll-forward); investments-at-fair-value '
                           'is never stated in the fund files, so there is no independent second path. The '
                           'two-base range bounds the carry fork; the NAV POSITION remains uncorroborated '
                           '(a permanent label, not pending data).'})
    disc.append({'kind': 'nav_derivation', 'entity': 'fund',
                 'detail': f'as-of {as_of}: gross NAV = called {called} − distributed {distributed} + net '
                           f'ITD P&L {net_pnl} = {gross_nav}; LP NAV = gross − accrued carry '
                           f'{carry if carry is not None else "—"} = {lp_nav}. '
                           f'Inputs: called@{called_cell}, distributed@{dist_cell}, P&L@[{",".join(pnl_cells)}].'})
    return rec, checks, disc


def _all_flows(inputs_list):
    out = []
    for i in inputs_list:
        out.extend(i.flows)
    return out


def _parse_date_from_text(text) -> Optional[_dt.date]:
    m = re.search(r'\d{1,2}[-/. ][A-Za-z0-9]{2,4}[-/. ]\d{2,4}', str(text))
    return _parse_date(m.group(0).replace(' ', '-')) if m else None


def _pct(frac: Decimal) -> str:
    return format_pct(frac)          # canonical (rounds + guards integer zeros); see quantity.format_pct


def _fee_rate_from_committed(committed_base, actual_val) -> Optional[Decimal]:
    """The fee rate implied by the confirmed committed base — used only to ORIENT the NAV
    leg (2%×NAV). The rate itself is the verified terms rate; here we recover it from the
    committed tie-out so the NAV leg reuses the same anchor the terms slice proved."""
    try:
        return (Decimal(str(actual_val)) / Decimal(str(committed_base))) if committed_base else None
    except Exception:
        return None
