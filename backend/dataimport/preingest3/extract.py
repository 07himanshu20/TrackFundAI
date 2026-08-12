"""
Per-file extraction — DETERMINISTIC-FIRST.

The deep-dive proved the time axis is a sheet-level horizontal header (the P&L's
47 months live at row 5, above every line-item section). So extraction is:

  1. detect the sheet's PERIOD AXIS (periods.detect_period_axis) — code, no model.
  2. find each concept's ROW by lexicon match on the label column — code, no model.
  3. read the concept row across the axis columns → CF1 collapse → CF2 normalise.

Most figures resolve with ZERO model calls (fast, offline-testable on all files).
The model is used ONLY as a router/locator when code is ambiguous (a concept the
lexicon can't place, or several candidate sheets). Every model call returns a
TYPED result: an ERROR (429/timeout/garbled) HOLDS the file — it can never become
a blank figure.

Comparison grids (FY-vs-FY, budget-vs-actual) are NOT time series and are
skipped for time-series extraction (their columns repeat).
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from openpyxl.utils import get_column_letter

import math
from decimal import Decimal, ROUND_HALF_UP

from . import family, gate, lexicon, periods, reconcile, tiers, units
from . import llm, locator, reader, statements, triangulate
from .cir import Figure, Provenance, Record
from .contract import CONCEPT_EQUIVALENCE, ROUNDING_DP, concept_measure, concept_nature
from .gate import AUTO
from .identity import compute_identity
from .profiler import _CCY_HINTS, _UNIT_HINTS, _cell_type, profile_file
from .profiler import token_present as _token_present
from .quantity import Quantity

_CR = Decimal('10000000')
_Q = Decimal('1').scaleb(-ROUNDING_DP)

MIS_CONCEPTS = ['revenue', 'ebitda', 'cash', 'headcount']

# Per-RUN model-call budget for the locator fallback. When metrics.calls reaches
# this, remaining held concepts are held with a DISCLOSED reason in deterministic
# order — never silently skipped — so a re-run with the same budget reproduces the
# same partial result. A cost backstop, not a quality knob.
_CALL_BUDGET = int(os.environ.get('PREINGEST3_CALL_BUDGET', '80'))
_BUDGET_HOLD = 'run call-budget exhausted — held; re-run to continue'

# ── model EMIT POLICY — a DELIBERATE choice (advisor 2026-07-23), never a default ──
# PROVING (default): maximally fail-closed. A model-located figure emits ONLY on a
# gate-AUTO verdict, and an UNPROVEN layout is graded one notch stricter (all-PASS →
# AUDIT → HUMAN → HELD), so NOTHING the model locates on a brand-new layout auto-emits
# until that layout earns a reliability track record or a human confirms it. The first
# live run is therefore hold-heavy BY DESIGN — its correctness is read from the
# triangulation VERDICT (located cell + 3 signals), not from the emit count.
#
# PRODUCTION LOOSENING (opt-in, only after CPM/CSS proves the brain): set
# PREINGEST3_EMIT_UNPROVEN=1 to treat a first-seen all-PASS as emittable (the 3-tier
# 'populate on first run + async spot-audit' policy). This exists so the loosening is
# an EXPLICIT decision, never a silent drift into 'the model can never emit'.
def _first_seen(context: str) -> bool:
    if os.environ.get('PREINGEST3_EMIT_UNPROVEN', '0') == '1':
        return False
    return gate.reliability_mode(context) != 'relaxed'

# post-bind verification: a bound SCALE-TRACKING figure that lands more than this
# many orders from the company's own scale anchor is not plausible for THIS
# company (a ₹15,000 'cash' on a ₹132 Cr company) — hold that figure (not the
# statement). A generous 1000× sanity bound, not a tuned match: it only rejects
# the absurd, catching a wrong-row bind the statement scale happened to resolve.
# It NEVER runs on profit/burn concepts — EBITDA and burn are legitimately small
# or negative (a real breakeven EBITDA is 'orders below company scale' by ratio
# and must not be held). Same principle as judging scale against the top line.
_FIGURE_ANCHOR_ORDERS = 3.0
_FIGURE_SANITY_EXEMPT = {'ebitda', 'net_income', 'gross_profit', 'operating_profit',
                         'pbt', 'opex', 'cogs'}

# ── stock-vs-flow bind guard (U5, advisor 2026-07-23) ────────────────────────
# A balance-sheet STOCK (cash, assets, liabilities, equity, opening/closing cash)
# must bind to a BALANCE row, never a period FLOW. The Hubler cash case proved the
# danger: the deterministic path bound "cash" to "Cash Collected" (a flow), which
# ties within-column and slips past triangulation — a wrong-grain bind no arithmetic
# signal can catch. If the label the model READ reads like a flow, HOLD the stock.
# Tokens are chosen not to hit stock labels ("closing balance", "cash & bank",
# "bank balance", "cash and cash equivalents" carry none of them).
_FLOW_LABEL_TOKENS = (
    'collect', 'received', 'receipt', 'payment', 'disburse', 'movement',
    'inflow', 'outflow', 'during the', 'for the period', 'for the month',
    'net cash', 'net change', 'change in', 'net increase', 'net decrease',
    'increase in', 'decrease in', 'cash generated', 'cash used', 'addition',
)


def _is_flow_label(label) -> bool:
    t = (label or '').strip().lower()
    return bool(t) and any(tok in t for tok in _FLOW_LABEL_TOKENS)


def _zero_stock_hold(concept, value_cr) -> bool:
    """A money STOCK that rounds to exactly ₹0 Cr is an empty / wrong-row bind — a
    going concern's cash / assets / equity are never zero, so a 0 means the located
    row was an empty GL line (CPM cash → '…Sponsorship-cash-OP' = ₹0.0000). HOLD it.
    Stock-only: flows (revenue, EBITDA) can legitimately be zero; and a tiny non-zero
    is already caught by the figure-anchor orders check, so this closes only the
    exact-zero gap that check skips."""
    return (value_cr is not None and value_cr == 0
            and concept_measure(concept) == 'money' and concept_nature(concept) == 'stock')


def _a1(col0, row0) -> str:
    return f'{get_column_letter(col0 + 1)}{row0 + 1}'


def _cite_value_cells(prov, collapsed, row, colmap) -> None:
    """UNIVERSAL provenance: make `prov` cite the cell(s) that RECONSTRUCT the value, not
    the label cell. A collapse reports `source_cols` — the columns whose cells produce the
    figure: [latest] for a stock, [total] for a stated total, the summed run for a flow.
      • single source col (point-in-time / stated total) → `cell` = that VALUE cell.
      • multiple source cols (a TTM/partial SUM) → `cell` = '' (no single value cell) and
        `derived_from` = every summed cell; `basis` (on the Figure) carries the operation.
    `derived_from` is ALWAYS the full reconstruction set, so a reviewer (and the cite-
    evidence meta-test) can re-add the cited cells and get the number. The label text stays
    in `row_label`; the label CELL moves to `note` so it is not lost."""
    refs = [_a1(sc, row) for sc in collapsed.source_cols]
    if not refs:                                   # escalate / not-found → keep the label cell
        return
    prov.note = (prov.note + f' label@{prov.cell}').strip() if prov.cell else prov.note
    prov.derived_from = refs
    prov.cell = refs[0] if len(refs) == 1 else ''
    last = colmap.get(collapsed.source_cols[-1])
    if last is not None:
        prov.col_label = str(last.label)


def _sheet_label_col(rows, axis_row: int) -> Optional[int]:
    """The label column = the left-most column that is text-dense BELOW the axis
    row (where the line-item names live). Universal, no fixed column."""
    n_cols = max((len(rows[r]) for r in range(axis_row + 1, len(rows))), default=0)
    best, best_score = None, 0
    for c in range(min(n_cols, 6)):
        texts = sum(1 for r in range(axis_row + 1, len(rows))
                    if c < len(rows[r]) and _cell_type(rows[r][c]) == 'text')
        if texts > best_score:
            best, best_score = c, texts
    return best


def _local_currency_unit(rows, ax) -> Tuple[Optional[str], Optional[str]]:
    """Read the unit + currency from THIS statement's own header — the axis
    header row(s) and the banner rows just above (where a sheet declares
    'in ₹ Cr' / '(₹ Lakhs)'). NOT a global soup of every sheet's hints (that is
    what let a stray 'billion'/'EUR' from an unrelated sheet corrupt the scale).
    Matches WHOLE tokens (so 'cr' is crore, never the 'cr' inside 'description'),
    longest unit word first so 'crore' beats 'cr'."""
    ccy = unit = None
    scan = set()
    for ar in ax.axis_rows:
        # ar-3 .. ar+1: the banner rows above AND the 'Particulars (unit)' sub-header
        # that commonly sits one row BELOW the period-header row (matches the
        # model-path _region_ccy_unit scan, which already reads hdr+1).
        for rr in range(max(0, ar - 3), ar + 2):
            scan.add(rr)
    ordered_units = sorted(_UNIT_HINTS, key=len, reverse=True)
    for r in sorted(scan):
        if r >= len(rows):
            continue
        for v in rows[r]:
            if _cell_type(v) != 'text':
                continue
            low = str(v).lower()
            if ccy is None:
                for tok, code in _CCY_HINTS.items():
                    if _token_present(tok, low):
                        ccy = code
                        break
            if unit is None:
                for u in ordered_units:
                    if _token_present(u, low):
                        unit = u
                        break
    return ccy, unit


# aggregate / balance keywords used to prefer a TOTAL or period-END line over a
# component or opening line — universal accounting vocabulary, not fund-specific.
_AGG_STRONG = {'total', 'net', 'gross', 'closing', 'ending', 'overall', 'aggregate'}
_AGG_WEAK = {'balance'}
_AGG_ANTI = {'opening', 'beginning'}
# the concepts a label is tested against for cross-concept disambiguation: a row
# is only bound to concept X if X is the SOLE strongest match. 'Sales - Others /
# Cash' matches revenue ('sales') as strongly as cash → ambiguous, skip; 'Cash in
# Bank' matches only cash → bind. Prevents cross-concept contamination universally.
_DISAMBIG = ['revenue', 'cogs', 'gross_profit', 'opex', 'ebitda', 'net_income',
             'cash', 'headcount', 'commitment', 'called', 'distributed',
             'fair_value', 'cost', 'proceeds', 'assets', 'liabilities', 'equity']
# a bound row must clear this minimal match score (a real match is ≥1.0), and its
# concept must beat the runner-up concept by this margin (else the label is a
# cross-concept ambiguity like 'Sales - Others / Cash' → skip). Both fed to the
# SHARED gate.rank() — the same floor+margin rule U4 uses for entity resolution.
_CONCEPT_ROW_FLOOR = 1.0
_CONCEPT_GAP = 0.25


# a money row is a level (₹), a ratio row is dimensionless — 'EBITDA Margin',
# 'Current Ratio', 'Revenue per employee' are rates/proportions, never the money
# figure. Linguistic ratio markers (universal), checked as WHOLE normalised tokens
# so they never substring-match a real money word ('per' ∉ 'operating').
_RATIO_TOKENS = {'margin', 'margins', 'ratio', 'ratios', 'per'}


def _is_ratio_label(label) -> bool:
    s = str(label)
    if '%' in s or 'percent' in s.lower():
        return True
    return bool(set(lexicon.normalise_label(label).split()) & _RATIO_TOKENS)


_DANDA_TOKENS = {'depreciation', 'amortisation', 'amortization', 'depreciaton',
                 'dna', 'd&a'}
_OPERATING_TOKENS = {'operating', 'operational'}


def _ebitda_label_class(label) -> str:
    """Classify a bound 'ebitda' row by its LABEL — the row-picker half of the U2
    identity. EBITDA = EBIT + D&A, and EBIT = operating profit (before interest AND
    tax). So a D&A add-back alone is NOT sufficient: 'before … depreciation' on a
    POST-interest/tax base (PBT, net profit) is EBITDA minus interest/tax — a proxy,
    not EBITDA. Four classes:
      'ebitda'            — explicitly labelled EBITDA;
      'operating_addback' — operating-profit/EBIT before D&A (= EBITDA by identity);
      'proxy_addback'     — a post-interest/tax profit + D&A add-back (e.g. LDC's
                            'Profit Before Tax, depreciation and ESOP' — for an NBFC
                            interest is the main cost, so this is a pre-tax operating
                            PROXY, arithmetic-verifiable but not EBITDA on its face);
      'not_ebitda'        — EBIT / PBT / operating profit with NO D&A add-back.
    Linguistic/definitional, no per-file spellings."""
    toks = set(lexicon.normalise_label(label).split())
    if 'ebitda' in toks:
        return 'ebitda'
    if not (toks & _DANDA_TOKENS):
        return 'not_ebitda'
    if toks & _OPERATING_TOKENS:               # operating base → before interest & tax
        return 'operating_addback'
    return 'proxy_addback'                      # D&A on a post-interest/tax base


def _ebitda_is_clean(label) -> bool:
    """Clean-by-LABEL (no arithmetic needed): explicit EBITDA or an operating-base
    D&A add-back. A proxy add-back (PBT/net + D&A) is NOT clean by label — it must
    pass the arithmetic identity (_verify_ebitda_arithmetic) or be held."""
    return _ebitda_label_class(label) in ('ebitda', 'operating_addback')


def _row_nums(rows, r, num_cols):
    return {c: rows[r][c] for c in num_cols
            if c < len(rows[r]) and _cell_type(rows[r][c]) == 'num'}


def _find_label_row(rows, label_col, num_cols, r0, r1, predicate) -> Optional[int]:
    for r in range(r0, min(r1, len(rows))):
        if not (label_col < len(rows[r]) and _cell_type(rows[r][label_col]) == 'text'):
            continue
        toks = set(lexicon.normalise_label(rows[r][label_col]).split())
        if predicate(toks) and _row_nums(rows, r, num_cols):
            return r
    return None


def _verify_ebitda_arithmetic(rows, label_col, num_cols, ebitda_row):
    """The U2 identity as VERIFIER: where an operating-profit (EBIT) line and a D&A
    line both sit on the same statement, EBITDA must reconcile to EBIT + D&A on a
    shared column. Returns True (identity holds → really EBITDA), False (identity
    broke → a different metric), or None (the parts aren't both present → can't
    check, caller holds as an unverified proxy). Never raises."""
    if label_col is None:
        return None
    try:
        ebit_row = _find_label_row(
            rows, label_col, num_cols, 0, len(rows),
            lambda t: (t & _OPERATING_TOKENS) and ('profit' in t or 'income' in t)
            and not (t & _DANDA_TOKENS))
        da_row = _find_label_row(
            rows, label_col, num_cols, 0, len(rows),
            lambda t: (t & _DANDA_TOKENS) and 'before' not in t and 'ebitda' not in t)
        if ebit_row is None or da_row is None:
            return None
        e = _row_nums(rows, ebitda_row, num_cols)
        a = _row_nums(rows, ebit_row, num_cols)
        d = _row_nums(rows, da_row, num_cols)
        cols = sorted(set(e) & set(a) & set(d))
        if not cols:
            return None
        c = cols[-1]                                    # latest shared period
        lhs, rhs = float(a[c]) + float(d[c]), float(e[c])
        if abs(rhs) < 1e-9:
            return None
        return abs(lhs - rhs) / abs(rhs) <= 0.05
    except Exception:  # noqa: BLE001 — verification must never crash the extract
        return None


def _label_score(label, concept, toks) -> float:
    """Match score of a label for a concept: base (exact 2 / contains 1) + synonym
    coverage + aggregate/anti keyword adjustment. Aggregate terms are concept-
    neutral, so they cancel in cross-concept comparison and only rank rows of the
    SAME concept (total over component, closing over opening)."""
    strength, coverage = lexicon.match_detail(label, concept)
    if strength == 'none':
        return 0.0
    score = (2.0 if strength == 'exact' else 1.0) + coverage
    if toks & _AGG_STRONG:
        score += 1.0
    if toks & _AGG_WEAK:
        score += 0.5
    if toks & _AGG_ANTI:
        score -= 0.5
    return score


def _find_concept_row(rows, label_col, concept, r0, r1, axis_cols) -> Optional[int]:
    """The best DATA-BEARING row whose label denotes `concept`, chosen by score,
    not first-match. Universal binding rules, no fund-specific header spellings:
      • a row with no value on the period axis is a section HEADER, not the metric;
      • a ratio row ('% of revenue') is never the money figure — disqualified;
      • cross-concept: bind only if `concept` is the confident UNIQUE winner over
        all concepts for the label — decided by the shared gate.rank() (floor +
        margin), the same rule U4 uses. A near-tie ('Sales …/ Cash' matches revenue
        and cash equally) fails the margin → skipped;
      • among the concept's own rows, prefer the TOTAL / period-END line (aggregate
        keywords + synonym coverage) over a component or opening line.
    Returns None (GAP) rather than bind the wrong row."""
    if label_col is None:
        return None
    money = concept_measure(concept) == 'money'

    def has_axis_data(r):
        return any(pc.col < len(rows[r]) and _cell_type(rows[r][pc.col]) == 'num'
                   for pc in axis_cols)

    best_row, best_score = None, 0.0
    for r in range(r0, min(r1, len(rows))):
        if not (label_col < len(rows[r]) and _cell_type(rows[r][label_col]) == 'text'):
            continue
        label = rows[r][label_col]
        if not has_axis_data(r):
            continue
        if money and _is_ratio_label(label):
            continue
        toks = set(lexicon.normalise_label(label).split())
        cands = [(c, _label_score(label, c, toks)) for c in _DISAMBIG]
        cands = [(c, s) for c, s in cands if s > 0]
        if not cands:
            continue
        chosen, ok, _margin, _reason = gate.rank(cands, floor=_CONCEPT_ROW_FLOOR, gap=_CONCEPT_GAP)
        if not ok or chosen != concept:     # weak, ambiguous, or belongs to another concept
            continue
        sx = dict(cands)[concept]
        if sx > best_score:
            best_row, best_score = r, sx
    # Fail-closed period-boundary guard (3c): an 'opening/beginning' line is the OPENING
    # balance — last period's close, a wrong-period value for any as-of stock. Only the
    # opening_cash concept legitimately denotes it. The _AGG_ANTI penalty RANKS a closing
    # line above an opening one when both exist, but does not BLOCK a LONE opening row from
    # binding (nothing out-scores it), so generic `cash` on an opening-only sheet would emit
    # last period's close as this period's stock. HOLD instead. Prod-neutral: no current
    # emit binds an opening line (opening-vs-closing census, 15 files — 0 opening-bound).
    if best_row is not None and concept != 'opening_cash':
        blab = set(lexicon.normalise_label(rows[best_row][label_col]).split())
        if blab & _AGG_ANTI:
            return None
    return best_row


def _sheet_verdict_rank(rows, ax, label_col, found) -> int:
    """Axis-2 for the selector: the best family verdict among the families the FOUND concepts
    belong to. Consulted ONLY to break a (tier, n_found, n_cols) tie — verdict must NEVER override
    concept-count (Finding B: Agnikul's three verified emits come from a DERIVED summary tab that is
    INSUFFICIENT; the raw Balance Sheet is CONFIRMED — letting verdict win would re-source cash off
    the correct sheet). A concept with no financial family is neutral (skipped)."""
    verdicts = _family_verdicts(rows, ax, label_col)
    present = [verdicts.get(family.CONCEPT_FAMILY[c]) for c in found if c in family.CONCEPT_FAMILY]
    return tiers.verdict_rank([v for v in present if v is not None])


def _sigma_consolidated_pick(top):
    """Increment 3b — the deterministic ENTRY POINT to the Σ-divisions roll-up identity that
    reconcile owns as the ONE SHARED VERIFIER (a permanent deterministic tier, NOT scaffolding).
    For a tie of same-shape sibling tabs, build each candidate's revenue vector, collapse exact
    value-duplicates (guardrail 1 — value-based, ALL populated columns, never names), take the
    latest-period representative, and ask reconcile.sigma_consolidated_pick for the UNIQUE
    consolidated. Returns the winning candidate tuple, or None to ABSTAIN to the deterministic
    tie-break (FAIL-CLOSED: a non-unique result holds here today; the S4 model-locator resolves
    that residual at Step 6, its proposal validated by the SAME reconcile verifier — it augments
    the residual, it never overturns a confirmed deterministic pick).

    Revenue is the additive roll-up axis (a flow that sums across divisions); a candidate whose
    revenue row isn't located can't participate and is skipped.

    D2 — CONSISTENT COLUMN VIEW (the path-unification fix). The value path (`_actual_columns` +
    `collapse`) already excludes scenario columns and reasons about cumulatives; the selection path
    read RAW `ax.columns`. That disagreement is the real defect — it let a cumulative YTD interloper
    (CPM 'YTD Dec-24') into the dedup vectors so `Healthcare≡HC` differed on one column and wouldn't
    collapse. So build the roll-up vectors from the SAME view the value path counts: scenario columns
    excluded (`_actual_columns`), restricted to the FLOW-additive MONTH/QUARTER basis (the Σ-divisions
    identity holds per period, not on a cumulative). Keyed by COLUMN index (not (year,month) order) so
    repeated-period columns never collide. Deterministic: profile order, sorted-min dedup rep."""
    vectors, by_label, rep_col = {}, {}, {}
    for c in top:
        _d, _nf, _nc, sheet, rows, ax, lc, found = c
        rrow = found.get('revenue')
        if rrow is None:
            continue
        view = [pc for pc in _actual_columns(rows, ax) if pc.kind in (periods.MONTH, periods.QUARTER)]
        vec = {pc.col: Decimal(str(rows[rrow][pc.col])) for pc in view
               if pc.col < len(rows[rrow]) and _cell_type(rows[rrow][pc.col]) == 'num'}
        if not vec:
            continue
        vectors[sheet] = vec
        by_label[sheet] = c
        rep_col[sheet] = max((pc for pc in view if pc.col in vec), key=lambda pc: pc.order).col
    if len(vectors) < 3:                                      # need a roll-up + ≥2 parts to be a Σ
        return None
    kept = reconcile.collapse_value_duplicates(vectors)
    values = {lab: vec[rep_col[lab]] for lab, vec in kept.items()}   # representative = latest period
    pick = reconcile.sigma_consolidated_pick(values).get('pick')
    return by_label.get(pick) if pick else None


def _best_sheet(prof, concepts) -> Optional[Tuple]:
    """Pick the sheet whose TIME-SERIES axis carries the most target concepts — now TIER-AWARE.
    Comparison grids are excluded (not a time series).

    RANK KEY (a STABLE TOTAL ORDER, ascending = better):
        (is_dump, -n_found, -n_axis_cols, verdict_rank, structure_rank, sheet_name)
      • is_dump (axis-1, tiers.is_dump) — a raw GL/TB DUMP ranks BELOW every real statement, even
        one with FEWER concepts (a full GL mentions every concept, so it wins a naive count race —
        CPM's SAP dump found all 4, the real P&Ls only 3). This is a DEMOTION, sorted first.
      • -n_found, -n_axis_cols — the EXISTING primary order, kept ABOVE verdict per Finding B so the
        concept-count winner (often the company's own clean summary tab) is never overturned.
      • Σ-divisions consolidation (Increment 3b) — consulted FIRST on a tie: reconcile's shared
        roll-up verifier picks the UNIQUE tab whose revenue ≈ Σ(sibling revenues), i.e. the
        consolidated over its divisions (CPM: PL over the per-division P&Ls). It ABSTAINS (→ None)
        unless the consolidated is unique, so it never forces a wrong pick — a non-unique result
        falls through to the signals below, fail-closed.
      • verdict_rank, structure_rank — axis-2 (family confirmation) + the co-CONFIRMED structural
        tiebreak, inserted ABOVE lex-min: a meaningful signal always outranks the arbitrary one.
        Inert on all 15 real files (they only fire on a (n_found, n_cols) tie); they exist to stop a
        name-only dump that slipped axis-1 from winning a tie by lex-min alone.
      • sheet_name — the deterministic lex-min backstop (kills the last of the input-order class).

    DETERMINISM INVARIANT: every key component is a pure function of sheet content; `prof['sheets']`
    order is stable (from wb.sheetnames); the min is over a total order — so the pick never depends
    on the order sheets are presented in. verdict/structure are computed only for the top tie-group
    (cost), which cannot change the winner because a non-tied top candidate is already unique."""
    cands = []   # (is_dump, -n_found, -n_cols, sheet, rows, ax, label_col, found)
    for s in prof['sheets']:
        rows = prof['grid'][s.sheet]
        ax = periods.detect_period_axis(rows)
        if not ax.is_time_series or not ax.columns:
            continue
        lc = _sheet_label_col(rows, ax.axis_rows[0])
        found = {}
        for concept in concepts:
            row = _find_concept_row(rows, lc, concept, ax.axis_rows[0] + 1, len(rows), ax.columns)
            if row is not None:
                found[concept] = row
        if not found:
            continue
        dump = tiers.is_dump(rows, ax.axis_rows[0] + 1, lc)
        cands.append((dump, -len(found), -len(ax.columns), s.sheet, rows, ax, lc, found))
    if not cands:
        return None
    prim = min(c[:3] for c in cands)                          # best (tier, coverage, columns)
    top = [c for c in cands if c[:3] == prim]
    if len(top) == 1:
        _d, _nf, _nc, sheet, rows, ax, lc, found = top[0]
    else:                                                     # tie: Σ-divisions → verdict → structure → lex-min
        pick = _sigma_consolidated_pick(top)                  # unique roll-up, or None (abstain)
        if pick is not None:
            _d, _nf, _nc, sheet, rows, ax, lc, found = pick
        else:
            def _tiekey(c):
                _d, _nf, _nc, sheet, rows, ax, lc, found = c
                return (_sheet_verdict_rank(rows, ax, lc, found),
                        tiers.structure_rank(rows, ax.axis_rows[0] + 1, lc), sheet)
            _d, _nf, _nc, sheet, rows, ax, lc, found = min(top, key=_tiekey)
    return (len(found), len(ax.columns), sheet, rows, ax, lc, found)


# The identity inputs each family's confirmer needs, located on a candidate sheet.
_FAMILY_IDENTITY_CONCEPTS = {
    family.INCOME_STATEMENT: ('revenue', 'cogs', 'gross_profit', 'opex', 'ebitda'),
    family.BALANCE_SHEET: ('assets', 'liabilities', 'equity'),
    family.CASH_FLOW: ('opening_cash', 'closing_cash', 'receipts', 'payments'),
}


def _family_verdicts(rows, ax, label_col) -> dict:
    """ATTACH-ONLY (Increment 1): for one sheet, locate each family's identity inputs and compute
    the family verdict → {family: verdict}. ADDS a signal; changes NO emit (the Increment-2 tier
    selector CONSUMES it), so coverage/determinism/cite-evidence stay byte-for-byte unchanged.

    COMMON-COLUMN check (fix A): an accounting identity holds PER PERIOD, so all inputs are read
    from the SAME period column — the latest actual column in which the most located rows are
    numeric. Collapsing each row independently would sum slightly different trailing windows and
    fake a CONTRADICTED (Hubler: revenue−cogs=gross_profit holds every month but the TTM sums
    diverge). Locate-confidence (refinement 3): a concept whose row isn't located is simply absent
    from the values dict, so a family missing any identity input is INSUFFICIENT, never a false
    CONTRADICTED. Sorted iteration keeps the determinism invariant on this new selection site."""
    out = {}
    for fam in sorted(_FAMILY_IDENTITY_CONCEPTS):
        rowmap = {}
        for concept in _FAMILY_IDENTITY_CONCEPTS[fam]:
            r = _find_concept_row(rows, label_col, concept, ax.axis_rows[0] + 1, len(rows), ax.columns)
            if r is not None:
                rowmap[concept] = r
        values = {}
        for pc in sorted(ax.columns, key=lambda c: c.order, reverse=True):   # latest period first
            cand = {concept: Decimal(str(rows[r][pc.col]))
                    for concept, r in rowmap.items()
                    if pc.col < len(rows[r]) and _cell_type(rows[r][pc.col]) == 'num'}
            if len(cand) > len(values):
                values = cand
            if rowmap and len(cand) == len(rowmap):          # a column with ALL located rows → use it
                break
        out[fam] = family.confirm_family(fam, values)
    return out


def _alt_stock_sources(prof, concept, selected_sheet, rate_card, geo_ccy, inr_mentioned):
    """Every OTHER sheet that independently carries `concept` (a stock) on a time axis, as
    (sheet, declared_scale, {scale: value_cr}) for the cross-sheet reconciler. Each candidate
    scale's ₹Cr is computed from the alt sheet's OWN declared currency (FX-normalised), so a
    foreign alt is comparable; the anchor then picks the plausible decade. A flow-labelled row is
    skipped (a stock never reconciles against a period flow). Deterministic: sheets in profile
    order, scales in units._ALT_SCALES order — no hash-seed dependence."""
    out = []
    for s in prof['sheets']:
        sheet = s.sheet
        if sheet == selected_sheet or sheet not in prof['grid']:
            continue
        rows = prof['grid'][sheet]
        ax = periods.detect_period_axis(rows)
        if not ax.is_time_series or not ax.columns:
            continue
        lc = _sheet_label_col(rows, ax.axis_rows[0])
        r = _find_concept_row(rows, lc, concept, ax.axis_rows[0] + 1, len(rows), ax.columns)
        if r is None:
            continue
        row_label = str(rows[r][lc]) if (lc is not None and lc < len(rows[r])) else ''
        if _is_flow_label(row_label):                       # a stock must not reconcile against a flow
            continue
        alt_ccy, alt_unit = _local_currency_unit(rows, ax)
        ccy, esc, _reason, _flags = units.resolve_currency(
            stmt_currency=alt_ccy, geo_currency=geo_ccy, inr_mentioned=inr_mentioned)
        if esc or ccy is None:
            continue
        values = {pc.col: rows[r][pc.col] for pc in ax.columns
                  if pc.col < len(rows[r]) and _cell_type(rows[r][pc.col]) == 'num'}
        col = periods.collapse(concept, concept_nature(concept), ax.columns, values)
        if col.value is None or col.escalate:
            continue
        scale_crs, ok = {}, True
        for sc in units._ALT_SCALES:
            try:
                inr = rate_card.to_inr(Decimal(str(col.value)) * Decimal(str(units.SCALE_TO_ABS[sc])), ccy)
            except Exception:  # noqa: BLE001 — no FX for this currency → drop the alt, never crash
                ok = False
                break
            scale_crs[sc] = (inr / _CR).quantize(_Q, rounding=ROUND_HALF_UP)
        if ok and scale_crs:
            out.append((sheet, alt_unit or 'absolute', scale_crs))
    return out


# ── SOURCE-STATEMENT APPROPRIATENESS (company-side guard #1) ──────────────────
# A P&L number (revenue, EBITDA, opex) must be read from an INCOME statement — never a
# cash-flow or balance-sheet tab. CPC shipped EBITDA lifted from its Cash Flow Statement
# (the YTD 'EBITDA' line, tagged monthly). The fund side is trustworthy because every
# ledger ties to a control; a company KPI ties to nothing, so a wrong-source bind ships
# a plausible-but-wrong number with no net. This guard is that net — purely additive: it
# only turns a wrong-source emit into a HOLD, never a hold into an emit.
#
# Scope is deliberately NARROW (only the confirmed failure mode): an INCOME_STATEMENT
# concept from a cash-flow/balance tab is rejected, but a cash line on a P&L tab is NOT
# (startup MIS 'P&L' tabs often carry a cash/runway line — recovering cash from its own
# home statement is the job of the per-concept-family selector, not this guard). The kind
# is read from the sheet's self-declared TITLE; unknown kind → allowed (never over-reject
# an unlabelled sheet). Cash-flow is matched FIRST — its title also contains 'cash'.
_STMT_KIND_MARKERS = (
    ('cash_flow', ('cash flow statement', 'statement of cash flow', 'cash flow from operating',
                   'cash flows from operating', 'statement of cash flows')),
    ('balance',   ('balance sheet', 'statement of financial position')),
    ('income',    ('profit and loss', 'profit & loss', 'income statement', 'statement of operations',
                   'statement of profit and loss', 'statement of profit or loss',
                   'statement of comprehensive income')),
)
_KIND_ALLOWED_FAMILIES = {
    # income tabs may legitimately carry a cash line → BALANCE_SHEET allowed here (no over-reject)
    'income':    {family.INCOME_STATEMENT, family.BALANCE_SHEET},
    'cash_flow': {family.CASH_FLOW, family.BALANCE_SHEET},   # a CFS also carries the closing cash balance
    'balance':   {family.BALANCE_SHEET, family.CASH_FLOW},
}


def _statement_kind(rows, sheet_name: str = '', scan_rows: int = 25):
    """The statement KIND a sheet self-declares — in its title/section text, or (for the
    RESTRICTIVE kinds only) in its sheet NAME. Returns None when it can't be told (→ the
    guard fails open on the classifier, never over-rejecting an unlabelled sheet).

    Sheet-NAME detection is limited to cash-flow/balance (the kinds that RESTRICT P&L
    concepts) with high-confidence tokens, so a mislabel can only ever cause a safe HOLD,
    never a wrong emit; an income sheet name is irrelevant (income allows P&L + cash)."""
    nm = str(sheet_name).lower()
    if any(t in nm for t in ('cash flow', 'cashflow', 'cash-flow', 'cfs')):
        return 'cash_flow'
    if 'balance sheet' in nm or nm.strip() in ('bs', 'balancesheet'):
        return 'balance'
    text = ' '.join(str(c).lower() for r in rows[:scan_rows] for c in r if isinstance(c, str))
    for kind, markers in _STMT_KIND_MARKERS:
        if any(m in text for m in markers):
            return kind
    return None


def _concept_allowed_on_kind(concept: str, kind) -> bool:
    """Fail-closed source guard: may `concept` be read from a sheet of this statement
    `kind`? Unknown kind → yes; a concept with no home statement (headcount) → yes."""
    if kind is None:
        return True
    fam = family.CONCEPT_FAMILY.get(concept)
    if fam is None:
        return True
    return fam in _KIND_ALLOWED_FAMILIES.get(kind, set())


def _emit_from_collapse(concept, col, prov, *, stmt_kind, frame, rows, ax, label_col,
                        ebitda_row, anchor_cr, rate_card):
    """Turn ONE collapsed concept (from ANY sheet) into a Figure under the full emit
    discipline: statement-appropriateness, stock/flow grain, period-escalation, monetary
    frame, EBITDA metric-class, zero-stock & anchor sanity. Source-sheet-agnostic so the
    primary best-sheet path AND the fork-b per-concept-family re-source share ONE emit rule
    (no drift, no duplicated guard). Behaviour is byte-identical to the inlined loop it
    replaces — proven by the A/B extraction gate."""
    if col is not None and not _concept_allowed_on_kind(concept, stmt_kind):
        return Figure(concept, None, None, prov, held=True, basis=col.basis, months=col.months,
            hold_reason=(f'wrong-source: {concept} from {stmt_kind} tab '
                         f'(P&L concept off non-P&L statement) — held')[:90])
    if concept_nature(concept) == 'stock' and _is_flow_label(prov.row_label):
        return Figure(concept, None, None, prov, held=True,
            hold_reason=(f'stock {concept} bound to flow-labelled row '
                         f'{(prov.row_label or "")[:32]!r} — held')[:90])
    if col is None:
        return Figure(concept, None, None, prov, gap=True)
    if col.escalate or col.value is None:                        # period ambiguity → hold
        return Figure(concept, None, None, prov, held=True, basis=col.basis, months=col.months,
                      hold_reason=col.reason[:90])
    if concept_measure(concept) != 'money':                      # count/ratio → raw, no scaling
        if concept_measure(concept) == 'count' and (col.value is None or col.value <= 0):
            return Figure(concept, None, None, prov, held=True, basis=col.basis, months=col.months,
                          hold_reason='count ≤ 0 is not a valid headcount — likely a wrong-row bind')
        return Figure(concept, col.value, None, prov, basis=col.basis, months=col.months)
    if frame.escalate or frame.scale is None:                    # frame unresolved → hold money
        return Figure(concept, None, None, prov, held=True, basis=col.basis, months=col.months,
                      hold_reason=f'monetary frame unresolved: {frame.reason}'[:90])
    if concept == 'ebitda':                                      # metric-definition consistency (U2)
        cls = _ebitda_label_class(prov.row_label)
        verified, why = cls in ('ebitda', 'operating_addback'), None
        if cls == 'not_ebitda':
            why = f"'{prov.row_label[:30]}' is EBIT/PBT (no D&A add-back), not EBITDA"
        elif cls == 'proxy_addback':
            chk = _verify_ebitda_arithmetic(rows, label_col, [pc.col for pc in ax.columns], ebitda_row)
            if chk is True:
                verified = True
            else:
                why = (f"'{prov.row_label[:30]}' is a post-interest/tax + D&A proxy; "
                       + ('EBIT+D&A identity broke' if chk is False else 'no EBIT/D&A lines to verify'))
        if not verified:
            return Figure(concept, None, None, prov, held=True, basis=col.basis, months=col.months,
                          hold_reason=(why + ' — held to avoid metric-mix')[:90])
    try:
        q = Quantity(amount=col.value, currency=frame.currency, scale=frame.scale,
                     nature=concept_nature(concept), concept=concept)
        inr = rate_card.to_inr(q.absolute_native(), frame.currency)
        value_cr = (inr / _CR).quantize(_Q, rounding=ROUND_HALF_UP)
        if _zero_stock_hold(concept, value_cr):
            return Figure(concept, None, None, prov, held=True, basis=col.basis, months=col.months,
                          hold_reason='money stock rounds to ₹0 Cr — empty/wrong-row bind — held')
        if (anchor_cr and value_cr != 0 and
                abs(math.log10(abs(float(value_cr)) / float(anchor_cr))) > _FIGURE_ANCHOR_ORDERS):
            return Figure(concept, None, None, prov, held=True, basis=col.basis, months=col.months,
                          hold_reason=(f'₹{value_cr}Cr is >{_FIGURE_ANCHOR_ORDERS:.0f} orders '
                                       f'from company scale ₹{float(anchor_cr):.0f}Cr — likely wrong row')[:90])
        return Figure(concept, value_cr, None, prov, basis=col.basis, months=col.months)
    except Exception as e:  # noqa: BLE001 — never crash; hold
        return Figure(concept, None, None, prov, held=True, basis=col.basis, months=col.months,
                      hold_reason=str(e)[:90])


def _family_carriers(prof, concept, exclude_sheet):
    """Every OTHER family-appropriate time-series sheet carrying `concept`, ranked best-first.
    Family-appropriate = _concept_allowed_on_kind (a P&L concept only off an income tab, a stock
    only off balance/cash-flow) — the architectural truth the single-best-sheet model ignores.
    Rank (a deterministic TOTAL order, ascending=better): non-dump first; the concept's HOME
    statement kind first (income for a P&L line, balance for a stock); most period columns; then
    lexical sheet name. Returns [(dump, kind_rank, -ncols, sheet, rows, ax, label_col, row), ...]."""
    fam = family.CONCEPT_FAMILY.get(concept)
    home = {family.INCOME_STATEMENT: 'income', family.BALANCE_SHEET: 'balance',
            family.CASH_FLOW: 'cash_flow'}.get(fam)
    cands = []
    for s in prof['sheets']:
        if s.sheet == exclude_sheet or s.sheet not in prof['grid']:
            continue
        rows = prof['grid'][s.sheet]
        ax = periods.detect_period_axis(rows)
        if not ax.is_time_series or not ax.columns:
            continue
        kind = _statement_kind(rows, s.sheet)
        if not _concept_allowed_on_kind(concept, kind):
            continue
        lc = _sheet_label_col(rows, ax.axis_rows[0])
        row = _find_concept_row(rows, lc, concept, ax.axis_rows[0] + 1, len(rows), ax.columns)
        if row is None:
            continue
        # A P&L concept (revenue/EBITDA) must come from a GENUINE income statement — a self-
        # declared P&L (kind=='income') or one that STRUCTURALLY confirms the income identity.
        # This bars a metrics/KPI grid (kind=None, identity-insufficient) from sourcing revenue:
        # InstaAstro's 'KPIs' Revenue is a gross-consulting metric ~8× the P&L's Total Revenues,
        # and emitting it is exactly the wrong-source the coverage gate refuses. Cash (balance)
        # and headcount (operational) are unaffected — this gate is INCOME_STATEMENT-only.
        if fam == family.INCOME_STATEMENT and kind != 'income':
            if _family_verdicts(rows, ax, lc).get('income_statement') != family.CONFIRMED:
                continue
        dump = tiers.is_dump(rows, ax.axis_rows[0] + 1, lc)
        kind_rank = 0 if (home is not None and kind == home) else 1
        cands.append((dump, kind_rank, -len(ax.columns), s.sheet, rows, ax, lc, row))
    cands.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
    return cands


def _resource_value_cr(concept, rows, ax, lc, row, *, geo_ccy, inr_mentioned, anchor_cr, rate_card):
    """₹Cr for a single concept row on an alternate sheet, via that sheet's own frame — used by
    the cross-tab consistency check. None if it can't be resolved cleanly (never guesses)."""
    acts = _actual_columns(rows, ax)
    vals = {pc.col: rows[row][pc.col] for pc in acts
            if pc.col < len(rows[row]) and _cell_type(rows[row][pc.col]) == 'num'}
    if not vals:
        return None
    col = periods.collapse(concept, concept_nature(concept), acts, vals)
    if col.escalate or col.value is None:
        return None
    ccy, unit = _local_currency_unit(rows, ax)
    frame = units.resolve_monetary_frame(stmt_currency=ccy, geo_currency=geo_ccy,
                inr_mentioned=inr_mentioned, declared_unit=unit, sample_values=[col.value],
                anchor_cr=anchor_cr, ratecard=rate_card)
    if frame.escalate or frame.scale is None:
        return None
    try:
        q = Quantity(amount=col.value, currency=frame.currency, scale=frame.scale,
                     nature=concept_nature(concept), concept=concept)
        return (rate_card.to_inr(q.absolute_native(), frame.currency) / _CR).quantize(_Q, rounding=ROUND_HALF_UP)
    except Exception:  # noqa: BLE001
        return None


def _sheet_corroborated(prof, source_sheet, *, geo_ccy, inr_mentioned, anchor_cr, rate_card):
    """Is `source_sheet` corroborated as THIS company's OWN statement? True iff at least one of its
    money concepts AGREES (same ₹Cr, within a rounding tolerance) with the same concept on ANOTHER
    income-genuine sheet. Cross-sheet agreement is format-agnostic evidence the sheet is the real
    statement; its ABSENCE means a lone statement that may be a subsidiary/stray (InstaAstro's
    'Trishona - P&L') — the caller then HOLDS. The decision is 'do two independent sheets show the
    same figure', NOT 'how big is the number' — so it carries no magnitude/emit-vs-hold threshold."""
    _AGREE = 0.1     # ≤0.1 orders (~within 26%) = the same figure allowing rounding/minor def. diffs
    if source_sheet not in prof['grid']:
        return False
    rows = prof['grid'][source_sheet]
    ax = periods.detect_period_axis(rows)
    if not ax.is_time_series or not ax.columns:
        return False
    lc = _sheet_label_col(rows, ax.axis_rows[0])
    for concept in MIS_CONCEPTS:
        if concept_measure(concept) != 'money':
            continue
        r = _find_concept_row(rows, lc, concept, ax.axis_rows[0] + 1, len(rows), ax.columns)
        if r is None:
            continue
        v0 = _resource_value_cr(concept, rows, ax, lc, r, geo_ccy=geo_ccy,
                                inr_mentioned=inr_mentioned, anchor_cr=anchor_cr, rate_card=rate_card)
        if not v0:
            continue
        for _d, _k, _n, s2, rows2, ax2, lc2, row2 in _family_carriers(prof, concept, source_sheet):
            v2 = _resource_value_cr(concept, rows2, ax2, lc2, row2, geo_ccy=geo_ccy,
                                    inr_mentioned=inr_mentioned, anchor_cr=anchor_cr, rate_card=rate_card)
            if v2 and abs(math.log10(abs(float(v0)) / abs(float(v2)))) <= _AGREE:
                return True
    return False


def _family_resource(prof, concept, primary_sheet, *, ident, label, geo_ccy, inr_mentioned,
                     anchor_cr, rate_card):
    """fork-b: deterministically re-source ONE concept from its best family-appropriate OTHER
    sheet — the architectural fix for 'a company's KPIs span P&L / balance-sheet / cash-flow, so
    one tab cannot source them all'. Resolves THAT sheet's own monetary frame from all its money
    lines, collapses over its actual columns, and emits via the shared _emit_from_collapse rule.
    CROSS-TAB consistency (money): if the best ALTERNATE family carrier yields a confirmed value
    >1 order of magnitude away, HOLD (wrong-row / cumulative-mislabel) rather than trust one sheet.
    Returns a Figure (confirmed emit, or an informative held), or None if no carrier exists."""
    carriers = _family_carriers(prof, concept, primary_sheet)
    if not carriers:
        return None
    _dump, _kr, _nc, sheet, rows, ax, lc, row = carriers[0]
    found = {c: _find_concept_row(rows, lc, c, ax.axis_rows[0] + 1, len(rows), ax.columns)
             for c in MIS_CONCEPTS}
    acts = _actual_columns(rows, ax)
    collapsed = {}
    for c in MIS_CONCEPTS:
        r = found.get(c)
        if r is None:
            continue
        vals = {pc.col: rows[r][pc.col] for pc in acts
                if pc.col < len(rows[r]) and _cell_type(rows[r][pc.col]) == 'num'}
        if vals:
            collapsed[c] = periods.collapse(c, concept_nature(c), acts, vals)
    local_ccy, local_unit = _local_currency_unit(rows, ax)
    money_samples = [cc.value for cn, cc in collapsed.items()
                     if concept_measure(cn) == 'money' and not cc.escalate and cc.value is not None]
    frame = units.resolve_monetary_frame(stmt_currency=local_ccy, geo_currency=geo_ccy,
                inr_mentioned=inr_mentioned, declared_unit=local_unit, sample_values=money_samples,
                anchor_cr=anchor_cr, ratecard=rate_card)
    prov = Provenance(source_file=label, content_fingerprint=ident.content_fp, sheet=sheet,
                      cell=_a1(lc, row), row_label=str(rows[row][lc]).strip())
    col = collapsed.get(concept)
    if col is not None:
        _cite_value_cells(prov, col, row, {pc.col: pc for pc in ax.columns})
    fig = _emit_from_collapse(concept, col, prov, stmt_kind=_statement_kind(rows, sheet),
            frame=frame, rows=rows, ax=ax, label_col=lc, ebitda_row=found.get('ebitda'),
            anchor_cr=anchor_cr, rate_card=rate_card)
    if not (isinstance(fig, Figure) and fig.confirmed):
        return fig
    # SHEET-TRUST (universal, no magnitude threshold): a re-sourced INCOME-STATEMENT figure ships only
    # if its SOURCE sheet is corroborated as this company's own statement — at least one of the sheet's
    # money lines AGREES, across sheets, with the same concept on another income-genuine sheet. A lone,
    # uncorroborated statement may be a subsidiary/stray (InstaAstro's 'Trishona - P&L', unmatched by
    # any other sheet) → HOLD and route to the model (location), never ship an unverifiable re-source.
    # Cash uses the existing cross-sheet STOCK reconciliation (later in extract_company); headcount is
    # a count with no cross-sheet money check. Applies to revenue/EBITDA (INCOME_STATEMENT) only.
    if (family.CONCEPT_FAMILY.get(concept) == family.INCOME_STATEMENT
            and not _sheet_corroborated(prof, sheet, geo_ccy=geo_ccy, inr_mentioned=inr_mentioned,
                                        anchor_cr=anchor_cr, rate_card=rate_card)):
        return Figure(concept, None, None, prov, held=True, basis=fig.basis, months=fig.months,
            hold_reason=(f're-source {sheet} not cross-sheet-corroborated as own income statement '
                         f'(possible subsidiary/stray) — held')[:90])
    return fig


def extract_company(label: str, path: str, *, rate_card, entity: str = None,
                    domicile=None, anchor_cr=None, as_of_year: int = None,
                    use_model: bool = False) -> Record:
    """Extract ONE Portfolio_KPI record from a company MIS file, DETERMINISTIC-FIRST.
    Never raises on a bad figure — it holds/gaps that figure and keeps the rest.

    `use_model=True` adds the LOCATOR fallback (S3→S4→S5→S5b): for every concept the
    code path could not bind (held/gap), the model LOCATES an address, code READS it,
    code TRIANGULATES it, and only an all-signal-PASS (gate AUTO) verdict emits. This
    is PURELY ADDITIVE — it can only turn a hold into a verified emit, never overturn
    a deterministic emit (coverage, not correction). Bounded by the per-run call
    budget; beyond it, remaining concepts hold with a disclosed, reproducible reason."""
    ident = compute_identity(label, path)
    prof = profile_file(label, path)
    entity = entity or label
    _model = use_model and llm._resolve_provider() is not None

    best = _best_sheet(prof, MIS_CONCEPTS)
    if best is None:
        if not _model:
            return Record('mis', entity_id=entity,
                          fields={'company': entity, '_note': 'no time-series statement found'})
        # no code-detected time-series axis → let the model try to locate a statement
        fields = {'company': entity}
        for c in MIS_CONCEPTS:
            fields[c] = Figure(c, None, None, Provenance(
                source_file=label, content_fingerprint=ident.content_fp,
                sheet='', cell='', row_label=''), gap=True)
        _model_fill(prof, ident, list(MIS_CONCEPTS), entity=entity, domicile=domicile,
                    anchor_cr=anchor_cr, rate_card=rate_card, fields=fields, source_label=label)
        if all(not isinstance(fields[c], Figure) or (fields[c].held or fields[c].gap)
               for c in MIS_CONCEPTS):
            fields['_note'] = 'no time-series statement found'
        _finalize_terminal_state(fields)             # choke: no figure ships in limbo
        return Record('mis', entity_id=entity, fields=fields)
    _n, _c, sheet, rows, ax, label_col, found = best
    # The monetary frame (currency + scale) is resolved ONCE for this statement,
    # geography-gated. Currency comes from THIS statement's own header first; the
    # workbook-wide 'rupees/INR' mention is only a fallback and can NEVER override
    # a known foreign domicile (that would mis-currency a Singapore/Malaysia co).
    local_ccy, local_unit = _local_currency_unit(rows, ax)
    geo_ccy = units.expected_currency(domicile)
    inr_mentioned = _workbook_mentions_inr(prof)

    # ── Pass 1: CF1-collapse every located concept (native, no scaling yet) ──
    # Increment 4 (i): the deterministic emit path now collapses over the SCENARIO-FILTERED view
    # (_actual_columns — Budget/Target/Forecast/PY excluded), UNIFYING it with the model path (@_model_fill)
    # which already did. Previously it collapsed over raw ax.columns, so the 12 prod emits were clean of
    # scenario contamination by LUCK (no emitting file had a per-column-labeled scenario column in its
    # window), not by construction. Predict-audited PROD-NEUTRAL (0 collapse diffs on all 15). NOTE: this
    # closes the PER-COLUMN-labeled scenario form; BANNER-labeled blocks (CPM 'Budget 2025') need the
    # deferred banner-reader, and an UNLABELED-non-duplicate scenario column summed into a flow is the
    # documented open residual (guard A catches only DUPLICATE-period conflicts).
    acts = _actual_columns(rows, ax)
    collapsed = {}
    provs = {}
    colmap = {pc.col: pc for pc in ax.columns}
    for concept in MIS_CONCEPTS:
        row = found.get(concept)
        provs[concept] = Provenance(
            source_file=label, content_fingerprint=ident.content_fp, sheet=sheet,
            cell=_a1(label_col, row) if row is not None else '',
            row_label=str(rows[row][label_col]).strip() if row is not None else '')
        if row is None:
            continue
        values = {pc.col: rows[row][pc.col] for pc in acts
                  if pc.col < len(rows[row]) and _cell_type(rows[row][pc.col]) == 'num'}
        if not values:
            continue
        collapsed[concept] = periods.collapse(concept, concept_nature(concept), acts, values)
        _cite_value_cells(provs[concept], collapsed[concept], row, colmap)

    # ── Resolve the STATEMENT monetary frame ONCE (money concepts) → apply to ALL ──
    money_samples = [c.value for cn, c in collapsed.items()
                     if concept_measure(cn) == 'money' and not c.escalate and c.value is not None]
    frame = units.resolve_monetary_frame(stmt_currency=local_ccy, geo_currency=geo_ccy,
                                         inr_mentioned=inr_mentioned, declared_unit=local_unit,
                                         sample_values=money_samples, anchor_cr=anchor_cr,
                                         ratecard=rate_card)

    fields = {'company': entity}
    stmt_kind = _statement_kind(rows, sheet)         # the source sheet's self-declared statement type
    for concept in MIS_CONCEPTS:
        fields[concept] = _emit_from_collapse(
            concept, collapsed.get(concept), provs[concept], stmt_kind=stmt_kind, frame=frame,
            rows=rows, ax=ax, label_col=label_col, ebitda_row=found.get('ebitda'),
            anchor_cr=anchor_cr, rate_card=rate_card)

    # ── fork-b: per-concept-family DETERMINISTIC re-source (deterministic-first, before model) ─
    # The single best sheet cannot source every KPI — a company's numbers legitimately span the
    # P&L, balance sheet and cash-flow statement. For any concept the primary sheet GAPPED or held
    # as WRONG-SOURCE, re-source it from its family-appropriate sheet (revenue/EBITDA off the P&L,
    # cash off the balance/cash-flow). ADDITIVE: only a re-source that FINDS the concept elsewhere
    # (confirmed emit, or an informative cross-tab/metric hold) replaces the gap/wrong-source hold;
    # a correct primary emit is never touched. This is the architectural fix, not a patch.
    for concept in MIS_CONCEPTS:
        fig = fields.get(concept)
        if not (isinstance(fig, Figure) and (fig.gap or
                (fig.held and 'wrong-source' in (fig.hold_reason or '')))):
            continue
        newfig = _family_resource(prof, concept, sheet, ident=ident, label=label, geo_ccy=geo_ccy,
                                  inr_mentioned=inr_mentioned, anchor_cr=anchor_cr, rate_card=rate_card)
        if isinstance(newfig, Figure) and not newfig.gap:        # found on a family sheet (emit or informative hold)
            fields[concept] = newfig

    if _model:                                       # locator fallback on holds/gaps only
        held = [c for c in MIS_CONCEPTS
                if isinstance(fields.get(c), Figure) and (fields[c].held or fields[c].gap)]
        if held:
            _model_fill(prof, ident, held, entity=entity, domicile=domicile,
                        anchor_cr=anchor_cr, rate_card=rate_card, fields=fields, source_label=label)
    # ── Cross-sheet STOCK reconciliation (Increment 3a, LOAD-BEARING) ─────────
    # Every emitted money STOCK is cross-checked against the same concept independently
    # sourced on OTHER sheets — ANCHOR-GATED and scale-AWARE (reconcile.stock_corroboration):
    # a lying declared unit on an alt sheet is repaired by the anchor's plausible decade; a
    # genuine cross-sheet divergence HOLDS the emit (never ships an unreconciled number); a
    # single-source stock is DISCLOSED as un-cross-checked (advisor catch A), never silently
    # counted as verified. Deterministic (sheets in profile order). The predict-audit proved
    # this coverage-neutral on the 10 real files (Agnikul cross-checks on 3 sheets; LDC &
    # InstaAstro single-source-disclose; zero false-holds) — 12/0/28 unchanged.
    stock_recon = {}
    for c in MIS_CONCEPTS:
        if concept_measure(c) != 'money' or concept_nature(c) != 'stock':
            continue
        fig = fields.get(c)
        if not (isinstance(fig, Figure) and fig.confirmed):
            continue
        alts = _alt_stock_sources(prof, c, sheet, rate_card, geo_ccy, inr_mentioned)
        res = reconcile.stock_corroboration(f'{entity}_{c}', fig, alts, anchor_cr=anchor_cr)
        stock_recon[c] = res
        if reconcile.blocks_run([res]):                     # genuine divergence → HOLD the emit
            fields[c] = Figure(c, None, None, fig.provenance, held=True, basis=fig.basis,
                               months=fig.months,
                               hold_reason=('cross-sheet stock divergence: '
                                            + (res.get('detail') or ''))[:90])
    if stock_recon:
        fields['_stock_recon'] = stock_recon
    # ATTACH (Increment 1): compute the selected sheet's family verdict as a NON-Figure
    # diagnostic. It adds signal for the Increment-2 tier selector to CONSUME; it does not
    # re-source anything here, so every emit — and thus coverage/determinism/cite-evidence —
    # is byte-for-byte unchanged. Coverage must stay EXACTLY 12/0/28 (neutral, both directions).
    fields['_family_verdicts'] = _family_verdicts(rows, ax, label_col)
    _finalize_terminal_state(fields)                 # choke: no figure ships in limbo
    return Record('mis', entity_id=entity, fields=fields)


def _workbook_mentions_inr(prof) -> bool:
    """Per user rule: if the workbook mentions rupees / INR / ₹ ANYWHERE, treat
    that file's money as INR (the safe default for an Indian company MIS)."""
    for s in prof['sheets']:
        if 'INR' in (s.currency_hints or []):
            return True
    return False


# ── model-locator fallback (S3→S4→S5→S5b) ─────────────────────────────────────
def _region_ccy_unit(rows, st) -> Tuple[Optional[str], Optional[str]]:
    """Currency + unit declared in THIS statement region's own header/banner rows —
    the same whole-token scan as _local_currency_unit, scoped to the region so a
    stray unit from an unrelated sheet can never set this statement's scale."""
    ccy = unit = None
    ordered_units = sorted(_UNIT_HINTS, key=len, reverse=True)
    hdr = st.header_row if st.header_row is not None else st.start_row
    for r in range(max(0, hdr - 3), min(hdr + 2, st.end_row + 1)):
        if r >= len(rows):
            continue
        for v in rows[r]:
            if _cell_type(v) != 'text':
                continue
            low = str(v).lower()
            if ccy is None:
                for tok, code in _CCY_HINTS.items():
                    if _token_present(tok, low):
                        ccy = code
                        break
            if unit is None:
                for u in ordered_units:
                    if _token_present(u, low):
                        unit = u
                        break
    return ccy, unit


# ── scenario axis (U6 sibling of period) — Actual only, never Budget/Target/PY ──
# The wrong-PERIOD lesson has a twin: a model row read against a BUDGET / TARGET /
# FORECAST / prior-year column passes triangulation identically to Actual (the
# column is internally coherent). Period and scenario are the SAME deterministic
# stage, so code excludes non-Actual columns BEFORE collapse. Detected on the
# column's own header + the banner cells above the axis row in that column.
_SCENARIO_RE = re.compile(
    r'\b(budget|bdgt|target|forecast|fcst|plan(?:ned)?|prior\s*year|last\s*year|'
    r'\bpy\b|\bly\b|variance|\bvar\b|vs\b)\b', re.I)


def _is_scenario_col(rows, ax, pc) -> bool:
    texts = [pc.label]
    for ar in ax.axis_rows:
        for rr in range(max(0, ar - 2), ar + 1):
            if rr < len(rows) and pc.col < len(rows[rr]) and _cell_type(rows[rr][pc.col]) == 'text':
                texts.append(str(rows[rr][pc.col]))
    return any(_SCENARIO_RE.search(t) for t in texts if t)


def _actual_columns(rows, ax) -> list:
    """Period columns with Budget/Target/Forecast/prior-year excluded. Never returns
    empty (if everything looked like a scenario, fall back to all — better a period
    figure than none; the frame/triangulation still guard it)."""
    keep = [pc for pc in ax.columns if not _is_scenario_col(rows, ax, pc)]
    return keep or list(ax.columns)


def _collapse_row(rows, cols, concept, rec):
    """Read a MODEL-LOCATED row across the actual period columns and CF1-collapse it
    — the identical machinery the deterministic path runs, so a model-found row and a
    code-found row produce the same figure. Expression form sums its component rows
    per column first. Returns a periods.Collapsed, or None if no period value."""
    if rec.form == 'expression':
        values = {}
        for pc in cols:
            acc, seen = Decimal('0'), False
            for orow in rec.operand_rows:
                if orow < len(rows) and pc.col < len(rows[orow]) and _cell_type(rows[orow][pc.col]) == 'num':
                    acc += Decimal(str(rows[orow][pc.col]))
                    seen = True
            if seen:
                values[pc.col] = acc
    else:
        row = rec.row
        if row is None or row >= len(rows):
            return None
        values = {pc.col: rows[row][pc.col] for pc in cols
                  if pc.col < len(rows[row]) and _cell_type(rows[row][pc.col]) == 'num'}
    if not values:
        return None
    return periods.collapse(concept, concept_nature(concept), cols, values)


def _emit_from_collapsed(concept, col, frame, prov, rate_card, anchor_cr, *,
                         rows=None, label_col=None, num_cols=None, ebitda_row=None) -> Figure:
    """Normalise ONE CF1-collapsed, triangulation-confirmed figure to ₹Cr — the SAME
    tail as the deterministic path (count/frame/EBITDA-class/scale/anchor-sanity), so
    a model-sourced number is handled byte-identically. Code owns every step; the
    model only supplied the row."""
    if col is None or col.escalate or col.value is None:
        return Figure(concept, None, None, prov, held=True,
                      basis=getattr(col, 'basis', None), months=getattr(col, 'months', 0),
                      hold_reason=((col.reason if col else 'no period value')[:90]))
    basis, months = col.basis, col.months
    if concept_measure(concept) != 'money':
        if concept_measure(concept) == 'count' and (col.value is None or col.value <= 0):
            return Figure(concept, None, None, prov, held=True, basis=basis, months=months,
                          hold_reason='count ≤ 0 is not a valid headcount — likely wrong-row bind')
        return Figure(concept, col.value, None, prov, basis=basis, months=months)
    if frame.escalate or frame.scale is None:
        return Figure(concept, None, None, prov, held=True, basis=basis, months=months,
                      hold_reason=f'monetary frame unresolved: {frame.reason}'[:90])
    if concept == 'ebitda' and rows is not None:                     # U2 metric-definition consistency
        cls = _ebitda_label_class(prov.row_label)
        verified, why = cls in ('ebitda', 'operating_addback'), None
        if cls == 'not_ebitda':
            why = f"'{prov.row_label[:30]}' is EBIT/PBT (no D&A add-back), not EBITDA"
        elif cls == 'proxy_addback':
            chk = (_verify_ebitda_arithmetic(rows, label_col, num_cols, ebitda_row)
                   if ebitda_row is not None else None)
            verified = chk is True
            if not verified:
                why = (f"'{prov.row_label[:30]}' is a post-interest/tax + D&A proxy; "
                       + ('EBIT+D&A identity broke' if chk is False else 'no EBIT/D&A lines to verify'))
        if not verified:
            return Figure(concept, None, None, prov, held=True, basis=basis, months=months,
                          hold_reason=(why + ' — held to avoid metric-mix')[:90])
    try:
        q = Quantity(amount=col.value, currency=frame.currency, scale=frame.scale,
                     nature=concept_nature(concept), concept=concept)
        inr = rate_card.to_inr(q.absolute_native(), frame.currency)
        value_cr = (inr / _CR).quantize(_Q, rounding=ROUND_HALF_UP)
        if _zero_stock_hold(concept, value_cr):
            return Figure(concept, None, None, prov, held=True, basis=basis, months=months,
                          hold_reason='money stock rounds to ₹0 Cr — empty/wrong-row bind — held')
        if (anchor_cr and value_cr != 0 and concept not in _FIGURE_SANITY_EXEMPT and
                abs(math.log10(abs(float(value_cr)) / float(anchor_cr))) > _FIGURE_ANCHOR_ORDERS):
            return Figure(concept, None, None, prov, held=True, basis=basis, months=months,
                          hold_reason=(f'₹{value_cr}Cr is >{_FIGURE_ANCHOR_ORDERS:.0f} orders from '
                                       f'company scale ₹{float(anchor_cr):.0f}Cr — likely wrong row')[:90])
        return Figure(concept, value_cr, None, prov, basis=basis, months=months)
    except Exception as e:  # noqa: BLE001 — never crash; hold
        return Figure(concept, None, None, prov, held=True, basis=basis, months=months,
                      hold_reason=str(e)[:90])


def _ref_value(rows, rr, ref_col):
    """The located row's value at ONE shared reference column (latest Actual period).
    Expression sums its component rows at that same column. This is what the identity
    is evaluated on — a single aligned period — NOT the per-row CF1 collapse (whose
    per-concept TTM windows can differ and silently break a valid identity)."""
    if ref_col is None:
        return None
    if rr.form == 'expression':
        acc, seen = Decimal('0'), False
        for orow in rr.operand_rows:
            if orow < len(rows) and ref_col < len(rows[orow]) and _cell_type(rows[orow][ref_col]) == 'num':
                acc += Decimal(str(rows[orow][ref_col]))
                seen = True
        return acc if seen else None
    row = rr.row
    if row is None or row >= len(rows) or ref_col >= len(rows[row]) or _cell_type(rows[row][ref_col]) != 'num':
        return None
    return Decimal(str(rows[row][ref_col]))


def _tri_inputs(recs, frame, sheet, label_col, rows, ref_col):
    """Build the {concept: ReadFigure} + {concept: LocatorRecord} triangulation needs.
    Figs carry the value at the SINGLE reference column (all rows aligned to one
    period), so the identity chain (revenue−cogs=gross_profit, opening+receipts−
    payments=closing) is evaluated across rows in ONE code-chosen column — the reason
    row-locating hardens the identity signal instead of just relocating it. The EMITTED
    value is the CF1 collapse, resolved separately."""
    from .reader import ReadFigure
    from .locator_schema import LocatorRecord, FORM_EXPRESSION, FORM_DIRECT
    scale = frame.scale if (not frame.escalate and frame.scale) else 'absolute'
    ccy = frame.currency or 'INR'
    figs, recs_tri = {}, {}
    for concept, rr in recs.items():
        row = rr.row if rr.row is not None else (rr.operand_rows[0] if rr.operand_rows else None)
        if row is None:
            continue
        raw = _ref_value(rows, rr, ref_col)
        recs_tri[concept] = LocatorRecord(
            concept=concept, form=(FORM_EXPRESSION if rr.form == 'expression' else FORM_DIRECT),
            resolved=[(sheet, label_col, row)])
        q = None
        if raw is not None:
            try:
                q = Quantity(amount=raw, currency=ccy, scale=scale,
                             nature=concept_nature(concept), concept=concept)
            except Exception:  # noqa: BLE001
                q = None
        figs[concept] = ReadFigure(concept, raw, q, [_a1(label_col, row)], read_ok=(raw is not None))
    return figs, recs_tri


# ── Increment 5: the verify-or-hold CHOKE — the single seam every emit passes through ────────
# v2 trust model: the DETERMINISTIC path IS the verifier (its confirmed emits are trusted and
# independently backstopped by the cite-evidence ruler, which reconstructs every value from its
# cited cells) — it needs no self-certifying token. The MODEL is the thing being verified (code
# checks the model's pointer), so a model figure emits ONLY through _model_emit, on a passing
# verdict. Relocating the model AUTO-gate into this one door makes bypass impossible by
# construction: a future model emit path cannot reach an emit except through here.
def _model_emit(concept, verdict, prov, *, build):
    """The ONLY door to a MODEL-sourced emit (the Step-6 seam). Emits ONLY on a passing
    triangulation verdict (status AUTO); an absent or non-AUTO verdict → NOT emitted (returns
    None, the concept stays held). `build` is invoked lazily, only on a passing verdict, so no
    normalisation is wasted on a held figure. Relocates the model AUTO-gate from its call site
    into one choke — it re-implements NO hold, it moves an existing gate so nothing can bypass it."""
    if verdict is None or getattr(verdict, 'status', None) != AUTO:
        return None
    return build()


def _finalize_terminal_state(fields):
    """The choke's terminal-state invariant: every emitted Figure must be confirmed / held(with a
    disclosed reason) / gap — NO limbo (a value-less figure that is neither held nor a disclosed
    gap, or a held figure with no reason). A limbo figure is fail-closed to a disclosed hold
    rather than shipped. This ASSERTS the invariant; it does not re-verify (deterministic emits
    are trusted + cite-evidence-backstopped). Inert on today's emits (0 violations measured) — a
    forward-guard that becomes load-bearing when a new emit source (the model at Step-6) turns on."""
    for c, f in list(fields.items()):
        if not isinstance(f, Figure) or f.confirmed or f.gap:
            continue
        if f.held and (f.hold_reason or '').strip():
            continue                                       # a properly disclosed hold — fine
        reason = ('held (unspecified) — fail-closed disclosure' if f.held
                  else 'no verified value and no disclosed gap — fail-closed hold')
        fields[c] = Figure(f.concept, None, f.native, f.provenance, held=True,
                           basis=f.basis, months=f.months, hold_reason=reason)


def _model_fill(prof, ident, held, *, entity, domicile, anchor_cr, rate_card,
                fields, source_label) -> list:
    """S4-REFINED fallback: the model returns a ROW per held concept; CODE resolves the
    period column (CF1 over Actual-only columns), scale, and value. Mutates `fields` IN
    PLACE and returns per-concept DIAGNOSTICS (row, cell, signals, tier). Emit happens
    ONLY on a gate-AUTO verdict; a wrong row is rejected by the code-read signals, and
    because the model never picks a column there is no wrong-period to slip through.
    Deterministic region order; the per-run budget makes over-spend a disclosed hold."""
    geo_ccy = units.expected_currency(domicile)
    inr_mentioned = _workbook_mentions_inr(prof)
    context = ident.layout_fp
    first_seen = _first_seen(context)
    grid = prof['grid']
    # DETERMINISM INVARIANT: `remaining` is a set for O(1) membership, but it feeds
    # SELECTION decisions (which concepts to locate, budget-hold, emit) — so it is only
    # ever ITERATED via sorted(remaining). Set iteration order varies with PYTHONHASHSEED
    # across processes; sorting makes the class impossible. The cross-process seed test
    # (test_determinism_gate) is the tripwire if this discipline ever lapses. Same rule
    # holds in locator.request_concepts (sorted targets) and locate_rows (sorted absent).
    remaining = set(held)
    diagnostics = []
    recall_missing = {}                           # concept → last sheet the locator left it undetermined
    for s in prof['sheets']:
        if not remaining:
            break
        rows = grid[s.sheet]
        ax = periods.detect_period_axis(rows)
        if not ax.is_time_series or not ax.columns:
            continue                                  # no time axis on this sheet → CF1 can't run
        cols = _actual_columns(rows, ax)              # scenario-filtered (Actual only)
        label_col = _sheet_label_col(rows, ax.axis_rows[0])
        if label_col is None:
            continue
        num_cols = [pc.col for pc in cols]
        regs = [r for r in statements.segment_regions(s.sheet, rows) if r.kind == statements.STATEMENT]
        if not regs:
            continue
        m = llm.current_metrics()
        if m is not None and m.calls >= _CALL_BUDGET:
            for c in sorted(remaining):               # disclosed, reproducible budget hold
                fields[c] = Figure(c, None, None, fields[c].provenance, held=True,
                                   hold_reason=_BUDGET_HOLD)
            return diagnostics
        # ONE statement window per sheet — a single-company statement's SECTION
        # sub-headers ("Cost of Goods Sold", "Digital marketing") must not fragment
        # the identity chain (revenue−cogs=gross_profit). Scope stays THIS sheet's
        # statement: identity is evaluated within the window, never across the
        # workbook, so it composes with the Σ-divisions statement-selection net
        # (which picks the sheet for a consolidation before the model ever locates).
        stmt = statements.Statement(
            s.sheet, min(r.start_row for r in regs), max(r.end_row for r in regs),
            header_row=(ax.axis_rows[0] if ax.axis_rows else None), label_col=label_col)
        out = locator.locate_rows(stmt, grid, sorted(remaining), content_fp=ident.content_fp)
        for c in out.get('missing', []):          # locator disclosed a silent drop — record it
            if c in remaining:
                recall_missing[c] = s.sheet
        if out.get('error') or not out.get('records'):
            continue
        recs = {r.concept: r for r in out['records']}
        for tgt, eq in CONCEPT_EQUIVALENCE.items():   # target ⇐ located anchor equivalent
            # e.g. cash ⇐ closing_cash: the model routed the BS stock precisely onto
            # the cash-flow "closing balance" and called generic `cash` absent — bind
            # the target from the anchor's row (accounting identity, not a patch).
            if tgt in remaining and tgt not in recs and eq in recs:
                recs[tgt] = recs[eq]
        collapses = {}
        for concept, rr in recs.items():
            col = _collapse_row(rows, cols, concept, rr)
            if col is not None:
                collapses[concept] = col
        money_samples = [collapses[c].value for c in collapses
                         if concept_measure(c) == 'money'
                         and collapses[c].value is not None and not collapses[c].escalate]
        local_ccy, local_unit = _region_ccy_unit(rows, stmt)
        frame = units.resolve_monetary_frame(
            stmt_currency=local_ccy, geo_currency=geo_ccy, inr_mentioned=inr_mentioned,
            declared_unit=local_unit, sample_values=money_samples,
            anchor_cr=anchor_cr, ratecard=rate_card)
        ref_col = max(cols, key=lambda c: c.order).col if cols else None   # latest Actual period
        figs, recs_tri = _tri_inputs(recs, frame, s.sheet, label_col, rows, ref_col)
        tri = triangulate.triangulate(stmt, recs_tri, figs, grid,
                                      context=context, first_seen=first_seen)
        verdicts = tri['verdicts']
        mcolmap = {pc.col: pc for pc in cols}
        for concept in sorted(remaining):
            rr = recs.get(concept)
            col = collapses.get(concept)
            if rr is None:
                continue
            row = rr.row if rr.row is not None else (rr.operand_rows[0] if rr.operand_rows else None)
            if row is None:
                continue
            v = verdicts.get(concept)
            prov = Provenance(source_file=source_label, content_fingerprint=ident.content_fp,
                              sheet=s.sheet, cell=_a1(label_col, row), row_label=(rr.row_label or '')[:60])
            if col is not None:                       # cite the VALUE cell(s), not the label cell
                _cite_value_cells(prov, col, row, mcolmap)
            diagnostics.append({
                'concept': concept, 'sheet': s.sheet, 'row': row + 1, 'row_label': rr.row_label,
                'native': None if (col is None or col.value is None) else str(col.value),
                'basis': (col.basis if col else None), 'tier': (v.status if v else None),
                'signals': {sg.name: sg.outcome for sg in (v.signals if v else [])},
            })
            if concept_nature(concept) == 'stock' and _is_flow_label(rr.row_label):
                # a stock bound to a flow line ties within-column and would slip past
                # triangulation — fail-closed OVERRIDE, before the verdict gate, so a
                # flow-labelled stock is held whether or not it would have passed.
                fields[concept] = Figure(concept, None, None, prov, held=True,
                                         hold_reason=(f'stock {concept} bound to flow-labelled row '
                                                      f'{(rr.row_label or "")[:32]!r} — held')[:90])
                continue
            ebitda_row = row if concept == 'ebitda' else None
            # RELOCATED (Inc-5): the model AUTO-gate is now the choke _model_emit — the model
            # reaches an emit ONLY through this door, and only on a passing verdict. A non-AUTO
            # verdict returns None → not emitted (the concept stays held), exactly as before.
            emit = _model_emit(
                concept, v, prov,
                build=lambda concept=concept, col=col, prov=prov, ebitda_row=ebitda_row:
                    _emit_from_collapsed(concept, col, frame, prov, rate_card, anchor_cr,
                                         rows=rows, label_col=label_col, num_cols=num_cols,
                                         ebitda_row=ebitda_row))
            if emit is None:
                continue
            fields[concept] = emit
            if not (emit.held or emit.gap):
                remaining.discard(concept)
    # recall disclosure: any target the locator never determined (a silent drop the
    # focused retry could not recover) is HELD with a disclosed reason and a
    # diagnostic — a requested concept is never silently lost.
    diagnosed = {d['concept'] for d in diagnostics}
    for c in sorted(remaining):
        if c in recall_missing and c not in diagnosed:
            fld = fields.get(c)
            prov = (fld.provenance if isinstance(fld, Figure) else
                    Provenance(source_file=source_label, content_fingerprint=ident.content_fp,
                               sheet='', cell='', row_label=''))
            fields[c] = Figure(c, None, None, prov, held=True,
                               hold_reason=f'locator did not return {c} on {recall_missing[c]} (recall) — held')
            diagnostics.append({'concept': c, 'sheet': recall_missing[c], 'row': None,
                                'row_label': None, 'native': None, 'basis': None,
                                'tier': None, 'signals': {}, 'recall': 'undetermined'})
    return diagnostics
