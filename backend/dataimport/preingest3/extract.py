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
from . import llm, locator, reader, statements, templates, triangulate
from .cir import Figure, Provenance, Record
from .contract import CONCEPT_EQUIVALENCE, IDENTITIES, ROUNDING_DP, concept_measure, concept_nature
from .gate import AUTO
from .identity import compute_identity
from .profiler import _CCY_HINTS, _UNIT_HINTS, _cell_type, profile_file
from .profiler import token_present as _token_present
from .quantity import EXCEL_ERRORS, Quantity, to_decimal

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


def _figure_anchor_hold_reason(concept, value_cr, anchor_cr):
    """The ONE figure-anchor magnitude sanity: a money value >_FIGURE_ANCHOR_ORDERS orders of magnitude
    from the whole-company scale is a likely wrong-row bind → return its hold_reason string; else None.
    EXEMPT (_FIGURE_SANITY_EXEMPT): profit/burn concepts are legitimately near-zero or negative — a real
    breakeven EBITDA is 'orders below company scale' by ratio (Aliste FYTD EBITDA = −₹0.01 Cr vs ₹82 Cr) —
    so magnitude is never a wrong-row signal for them. CENTRALISED so the two emit paths (deterministic
    collapse + re-source) apply one identical rule and cannot DRIFT — the drift that silently held Aliste
    EBITDA (this predicate lived in the re-source path with the exemption, but the collapse path had a
    duplicate WITHOUT it)."""
    if (anchor_cr and value_cr is not None and value_cr != 0 and concept not in _FIGURE_SANITY_EXEMPT and
            abs(math.log10(abs(float(value_cr)) / float(anchor_cr))) > _FIGURE_ANCHOR_ORDERS):
        return (f'₹{value_cr}Cr is >{_FIGURE_ANCHOR_ORDERS:.0f} orders from '
                f'company scale ₹{float(anchor_cr):.0f}Cr — likely wrong row')[:90]
    return None


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


def _find_equivalent_stock_row(rows, label_col, eq_concept, r0, r1, axis_cols) -> Optional[int]:
    """Locate the balance row for a CONCEPT_EQUIVALENCE anchor (e.g. closing_cash → 'Closing
    balance') that the cross-concept _DISAMBIG picker cannot reach because the anchor is NOT a
    disambiguation concept. Deliberately a DIRECT match on the anchor's OWN synonyms, not the
    cross-concept contest — so it never perturbs how the MIS targets bind (adding closing_cash
    to _DISAMBIG would tie 'Closing Cash Balance' against `cash` and regress that bind). The
    _AGG_ANTI guard still blocks an opening line. Fail-closed: needs axis data and a NON-flow
    label; picks the best synonym match; None if absent so the caller's hold simply stays."""
    if label_col is None:
        return None
    best_row, best_score = None, 0.0
    for r in range(r0, min(r1, len(rows))):
        if not (label_col < len(rows[r]) and _cell_type(rows[r][label_col]) == 'text'):
            continue
        label = rows[r][label_col]
        if _is_flow_label(label):
            continue
        if set(lexicon.normalise_label(label).split()) & _AGG_ANTI:   # opening/beginning line
            continue
        if not any(pc.col < len(rows[r]) and _cell_type(rows[r][pc.col]) == 'num' for pc in axis_cols):
            continue
        strength, coverage = lexicon.match_detail(label, eq_concept)
        if strength == 'none':
            continue
        score = coverage + (2.0 if strength == 'exact' else 1.0)
        if score > best_score:
            best_row, best_score = r, score
    return best_row


def _reconciled_cash_rebind(rows, label_col, acts, as_of, require_bound):
    """Lever 2a — reconciliation-guided closing-cash SELECTION on a held cash STOCK.

    A cash stock is often held as "wrong row / implausibly small" because a tiny bank-only
    sub-line out-scored the true aggregate on label keywords alone — and the label itself can
    LIE ('Closing Cash Balance including Fixed deposits' on a value that EXCLUDES them). Label
    wording cannot be trusted; only the DATA can dispose. So take the closing/total cash line
    that TWO INDEPENDENT reconciliation identities confirm on the SAME statement & column:
      • roll-forward  opening_cash + net change in cash = closing   (contract.cash_flow_identity)
      • component Σ    Σ(the contiguous component rows above a total) = that total
    Return (total_row, Collapsed) for the aggregate BOTH identities confirm to within the
    declared tolerance, else None. Purely arithmetic over located rows via the shared lexicon —
    no per-file sheet/label spellings; fail-closed on any missing or mismatched part, and ≥2
    agreeing anchors are REQUIRED (a single identity never rebinds), so the caller's hold simply
    stays whenever the statement does not over-determine the answer."""
    if label_col is None or not acts:
        return None
    spec = next((i for i in IDENTITIES if i['name'] == 'cash_flow_identity'), None)
    tol_rel = Decimal(str(spec['tol_rel'])) if spec else Decimal('0.02')
    num_cols = [pc.col for pc in acts]

    def _cell(r, c):
        return to_decimal(rows[r][c]) if (0 <= r < len(rows) and c < len(rows[r])) else None

    def _toks(r):
        return (set(lexicon.normalise_label(rows[r][label_col]).split())
                if (label_col < len(rows[r]) and _cell_type(rows[r][label_col]) == 'text') else set())

    def _is_data(r):
        return (label_col < len(rows[r]) and _cell_type(rows[r][label_col]) == 'text'
                and any(_cell(r, c) is not None for c in num_cols))

    # the two roll-forward inputs — an opening balance and a net change in cash. These belong to
    # the roll-forward identity ONLY; a component subtotal must be INDEPENDENT of them (never sweep
    # them as "components", or the subtotal degenerates into the roll-forward and the ≥2-anchor bar
    # is defeated). Defined once, reused to locate the inputs AND to bound the component walk.
    def _is_open(r):
        t = _toks(r)
        return bool(t & _AGG_ANTI) and lexicon.match_detail(rows[r][label_col], 'opening_cash')[0] != 'none'

    def _is_net(r):
        t = _toks(r)
        return 'net' in t and 'cash' in t and bool(t & {'increase', 'decrease', 'change', 'movement'})

    def _first(pred):
        for r in range(len(rows)):
            if _is_data(r) and pred(r):
                return r
        return None

    open_r, net_r = _first(_is_open), _first(_is_net)

    # aggregate cash lines (a total/closing/overall cash line, never an opening line)
    totals = [r for r in range(len(rows)) if _is_data(r)
              and not (_toks(r) & _AGG_ANTI)
              and (_toks(r) & _AGG_STRONG or 'balance' in _toks(r))
              and (lexicon.match_detail(rows[r][label_col], 'cash')[0] != 'none'
                   or lexicon.match_detail(rows[r][label_col], 'closing_cash')[0] != 'none')]

    for tot_r in sorted(totals, reverse=True):        # bottom-up: the grand total sits lowest
        vals = {pc.col: rows[tot_r][pc.col] for pc in acts
                if pc.col < len(rows[tot_r]) and _cell_type(rows[tot_r][pc.col]) == 'num'}
        col = periods.collapse('cash', concept_nature('cash'), acts, vals,
                               as_of=as_of, require_bound=require_bound)
        if col is None or col.value is None or col.escalate or not col.source_cols:
            continue
        V = to_decimal(col.value)
        if V is None or V == 0:
            continue
        k, tol = col.source_cols[0], abs(V) * tol_rel

        # anchor 1 — component subtotal: the contiguous balance component rows above the total sum
        # to it. Stop at the first non-data row AND at any roll-forward input (opening / net change)
        # so this stays INDEPENDENT of anchor 2 — a total sitting just below opening+net must never
        # be "confirmed" by re-deriving the roll-forward.
        parts, r = [], tot_r - 1
        while r >= 0 and len(parts) < 12 and _is_data(r) and not (_is_open(r) or _is_net(r)):
            pv = _cell(r, k)
            if pv is None:
                break
            parts.append(pv)
            r -= 1
        subtotal_ok = len(parts) >= 2 and abs(sum(parts) - V) <= tol

        # anchor 2 — roll-forward: opening + net change = closing, in the SAME column
        ov = _cell(open_r, k) if open_r is not None else None
        nv = _cell(net_r, k) if net_r is not None else None
        rollfwd_ok = ov is not None and nv is not None and abs((ov + nv) - V) <= tol

        if subtotal_ok and rollfwd_ok:                # ≥2 independent identities agree → STRONG
            return tot_r, col
    return None


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
        _st, _fw, _d, _nf, _nc, sheet, rows, ax, lc, found = c
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


def _concept_row_all_error(rows, r: int, columns) -> bool:
    """True iff a located concept row's period-axis cells are error-dominated — at least one
    spreadsheet error (#REF!/#DIV!/…) and NO real (non-zero) number. Such a row carries no
    extractable figure: a deleted-range #REF! or a severed external link, the hallmark of a
    DEFUNCT sheet (a hidden, abandoned working copy the file author left behind).

    It must not count as a 'found' concept when ranking sheets: `_best_sheet` scores a sheet by
    how many target concepts its LABELS match, then breaks ties on column count — so a wider
    sheet whose concept rows are all #REF! out-ranks a narrow CLEAN sheet, and the run holds a
    figure that is cleanly present elsewhere in the same file (Aliste: the hidden, stale
    'Profit & Loss' EBITDA row = 54 #REF! / 0 numbers, out-columns the live, clean 'P&L').

    NB visibility is NOT the discriminator — a CORRECT sheet can also be hidden (Analisa's
    ground-truthed 'ProfitLoss (23)'). VALUE VALIDITY is: this keys only on whether the row
    holds a real figure. A stray numeric 0 amid the #REF!s (a broken formula openpyxl read as 0)
    is breakage residue, not a reported figure. A CLEAN all-zero row (no errors) is left
    untouched — a legitimately-zero line is never disqualified — so behaviour changes ONLY where
    a concept row is genuinely error-dominated: a sheet-choice signal, never a value decision."""
    row = rows[r] if r < len(rows) else []
    n_err = n_real = 0
    for col in columns:
        ci = col.col
        v = row[ci] if ci < len(row) else None
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            if v != 0:
                n_real += 1                       # a real (non-zero) reported figure
        elif isinstance(v, str) and v.strip() in EXCEL_ERRORS:
            n_err += 1
    return n_err > 0 and n_real == 0


# The true point-in-time period kinds — a column whose `order` is a genuine reporting date, so
# comparing it to a reporting as-of is meaningful. YTD/TOTAL are DELIBERATELY excluded: they carry a
# cumulative sort-SENTINEL order ((yr,12) / (9999,12)) that straddles/overstates the as-of, exactly as
# `periods.collapse` documents — using them for vintage would false-classify a legitimate YTD column.
_POINT_IN_TIME_KINDS = (periods.MONTH, periods.QUARTER, periods.YEAR)


def _axis_cadence_months(ax) -> Optional[int]:
    """The reporting cadence of a period axis in MONTHS — the modal gap between consecutive DISTINCT
    point-in-time period orders (1 monthly, 3 quarterly, 12 annual). None when the axis cannot establish
    a cadence (<2 distinct datable periods): the caller must then fail-safe (do not judge vintage). Pure
    function of the axis; sorted() before the modal pick keeps it deterministic (PYTHONHASHSEED-free)."""
    idxs = sorted({pc.order[0] * 12 + pc.order[1] for pc in ax.columns
                   if pc.kind in _POINT_IN_TIME_KINDS and 2000 <= pc.order[0] < 9000})
    if len(idxs) < 2:
        return None
    gaps = [b - a for a, b in zip(idxs, idxs[1:]) if b - a > 0]
    if not gaps:
        return None
    return max(sorted(set(gaps)), key=gaps.count)


def _sheet_vintage_flags(rows, ax, as_of, found) -> Tuple[bool, bool]:
    """(is_stale, is_forward_projecting) for a candidate sheet, judged against the file's STATED reporting
    as-of. VINTAGE-RANKING ONLY — never a value/column decision. The value path keeps its own projection
    guard (collapse's `as_of` MONTH bound + `_filename_understated`); the <=as_of cap used here to read a
    sheet's latest actual is local to these flags and must NOT leak into column-emit selection (else it
    would chop legitimately-newer-than-filename actuals, e.g. Aliste gap -1). Both flags are False when the
    as-of is unknown (guard OFF — fail-safe to today's behaviour) or the sheet carries no datable actual
    period (vintage unassessable).

      is_stale  — the sheet's latest ACTUAL point-in-time period (month/quarter/year) AT-OR-BEFORE the
                  as-of is more than ONE reporting cadence-step behind it: an old-vintage statement whose
                  data never reaches the reporting date (Analisa's 'ProfitLoss (23)', latest Mar-2024 on a
                  May-2025 file). The tolerance is the axis's own cadence (1 mo monthly / 3 quarterly / 12
                  annual), so a legitimately one-period-behind file is NOT demoted and an annual file is
                  not given a 12-month pass. Scenario columns are excluded; the <=as_of cap stops a
                  forward plan column from masking staleness.
      is_forward_projecting — a found concept's OWN row carries a real non-zero value in a non-scenario
                  point-in-time column (month/quarter/year, NOT a YTD/TOTAL sentinel) dated strictly AFTER
                  the as-of: a budget/forecast/AOP plan sheet. Demoting it stops a naive stale-demotion
                  from promoting a plan sheet as the actual source (the Annual-Operating-Plan
                  'PNL AOP 2025R' that a stale-only demotion surfaced)."""
    if as_of is None:
        return (False, False)
    acts = _actual_columns(rows, ax)
    mq = [pc for pc in acts if pc.kind in _POINT_IN_TIME_KINDS and 2000 <= pc.order[0] < 9000]
    if not mq:
        return (False, False)
    cad = _axis_cadence_months(ax)
    if cad is None:
        stale = False
    else:
        within = [pc.order for pc in mq if pc.order <= as_of]
        latest = max(within) if within else None
        stale = (latest is None) or ((as_of[0] - latest[0]) * 12 + (as_of[1] - latest[1]) > cad)
    fwd = any(pc.order > as_of and pc.col < len(rows[row])
              and _cell_type(rows[row][pc.col]) == 'num' and rows[row][pc.col] != 0
              for row in found.values() for pc in mq)
    return (stale, fwd)


def _best_sheet(prof, concepts, as_of=None) -> Optional[Tuple]:
    """Pick the sheet whose TIME-SERIES axis carries the most target concepts — now TIER-AWARE.
    Comparison grids are excluded (not a time series).

    RANK KEY (a STABLE TOTAL ORDER, ascending = better):
        (is_stale, is_fwd_projecting, is_dump, -n_found, -n_axis_cols, verdict_rank, structure_rank, sheet_name)
      • is_stale, is_fwd_projecting (VINTAGE, axis-0) — demoted ABOVE everything else because a
        wrong-PERIOD figure is the cardinal never-a-wrong-number failure: an old-vintage statement whose
        data never reaches the file's stated reporting as-of (Analisa's 'ProfitLoss (23)', 14 months
        stale) and a budget/forecast/AOP plan sheet both rank below every current-actual statement, even
        one with FEWER concepts. Judged only when the as-of is known (`as_of` param, the filename month);
        as_of None → both False → this axis is inert and behaviour is byte-identical to before (the guard
        fails OFF, never guessing). See `_sheet_vintage_flags`. A wrong/over-stated as-of demotes a
        current sheet → the concept simply HOLDS (a missing number), never a wrong one — the right
        failure direction for this product.
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
    cands = []   # (is_stale, is_fwd, is_dump, -n_found, -n_cols, sheet, rows, ax, label_col, found)
    for s in prof['sheets']:
        rows = prof['grid'][s.sheet]
        ax = periods.detect_period_axis(rows)
        if not ax.is_time_series or not ax.columns:
            continue
        lc = _sheet_label_col(rows, ax.axis_rows[0])
        found = {}
        for concept in concepts:
            row = _find_concept_row(rows, lc, concept, ax.axis_rows[0] + 1, len(rows), ax.columns)
            # a located row whose axis cells are ENTIRELY #REF!/error (no real number) carries no
            # figure — it must not count toward this sheet's concept coverage, else a wider
            # #REF!-riddled (often hidden, defunct) sheet out-columns a narrow clean one and a
            # recoverable figure is held. R1a — value validity, NOT sheet visibility.
            if row is not None and not _concept_row_all_error(rows, row, ax.columns):
                found[concept] = row
        if not found:
            continue
        stale, fwd = _sheet_vintage_flags(rows, ax, as_of, found)
        dump = tiers.is_dump(rows, ax.axis_rows[0] + 1, lc)
        cands.append((stale, fwd, dump, -len(found), -len(ax.columns), s.sheet, rows, ax, lc, found))
    if not cands:
        return None
    prim = min(c[:5] for c in cands)                          # best (vintage, tier, coverage, columns)
    top = [c for c in cands if c[:5] == prim]
    if len(top) == 1:
        _st, _fw, _d, _nf, _nc, sheet, rows, ax, lc, found = top[0]
    else:                                                     # tie: Σ-divisions → verdict → structure → lex-min
        pick = _sigma_consolidated_pick(top)                  # unique roll-up, or None (abstain)
        if pick is not None:
            _st, _fw, _d, _nf, _nc, sheet, rows, ax, lc, found = pick
        else:
            def _tiekey(c):
                _st, _fw, _d, _nf, _nc, sheet, rows, ax, lc, found = c
                return (_sheet_verdict_rank(rows, ax, lc, found),
                        tiers.structure_rank(rows, ax.axis_rows[0] + 1, lc), sheet)
            _st, _fw, _d, _nf, _nc, sheet, rows, ax, lc, found = min(top, key=_tiekey)
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


def _alt_stock_sources(prof, concept, selected_sheet, rate_card, geo_ccy, inr_mentioned, as_of=None,
                       require_bound=False, base_currency=None):
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
        stale, fwd = _sheet_vintage_flags(rows, ax, as_of, {concept: r})
        if stale or fwd:                    # never reconcile/rescue a stock off a stale-vintage or forward-plan sheet
            continue                        # (centralised vintage guard — the CPC cash corroboration rescue inherits it)
        row_label = str(rows[r][lc]) if (lc is not None and lc < len(rows[r])) else ''
        if _is_flow_label(row_label):                       # a stock must not reconcile against a flow
            continue
        alt_ccy, alt_unit = _local_currency_unit(rows, ax)
        ccy, esc, _reason, _flags = units.resolve_currency(
            stmt_currency=alt_ccy, geo_currency=geo_ccy, inr_mentioned=inr_mentioned,
            base_currency=base_currency)
        if esc or ccy is None:
            continue
        values = {pc.col: rows[r][pc.col] for pc in ax.columns
                  if pc.col < len(rows[r]) and _cell_type(rows[r][pc.col]) == 'num'}
        col = periods.collapse(concept, concept_nature(concept), ax.columns, values, as_of=as_of,
                               require_bound=require_bound)
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
    _disclose_currency_basis(prov, frame)                        # audit: file / user-confirmed / rate
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
        _anchor_reason = _figure_anchor_hold_reason(concept, value_cr, anchor_cr)
        if _anchor_reason:
            return Figure(concept, None, None, prov, held=True, basis=col.basis, months=col.months,
                          hold_reason=_anchor_reason)
        return Figure(concept, value_cr, None, prov, basis=col.basis, months=col.months)
    except Exception as e:  # noqa: BLE001 — a genuine code fault: FX-uncovered is held earlier at the
        return Figure(concept, None, None, prov, held=True, basis=col.basis, months=col.months,  # frame
                      hold_reason=f'UNEXPECTED_ERROR: {type(e).__name__}: {e}'[:90])


def _family_carriers(prof, concept, exclude_sheet, as_of=None):
    """Every OTHER family-appropriate time-series sheet carrying `concept`, ranked best-first.
    Family-appropriate = _concept_allowed_on_kind (a P&L concept only off an income tab, a stock
    only off balance/cash-flow) — the architectural truth the single-best-sheet model ignores.

    VINTAGE EXCLUSION (centralised guard): a stale-vintage or forward-plan carrier is DROPPED
    entirely (not merely ranked last) — the re-source fallback must NEVER reach back to a sheet the
    primary path rejected as stale/plan, or a held concept would silently emit a wrong-period /
    budget figure (refinement #3: never fall back to an older-vintage sheet). This is STRICTER than
    `_best_sheet`, which only DEMOTES: the primary path must always offer its best available
    statement, but a fallback that would re-source from a stale sheet must yield nothing (→ HOLD).
    Same `_sheet_vintage_flags` signal, per-consumer policy. `as_of` None → exclusion inert (guard
    OFF, byte-identical to before).

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
        stale, fwd = _sheet_vintage_flags(rows, ax, as_of, {concept: row})
        if stale or fwd:                    # centralised vintage guard — never re-source from a stale/plan sheet
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


def _resource_value_cr(concept, rows, ax, lc, row, *, geo_ccy, inr_mentioned, anchor_cr, rate_card,
                       as_of=None, require_bound=False, base_currency=None):
    """₹Cr for a single concept row on an alternate sheet, via that sheet's own frame — used by
    the cross-tab consistency check. None if it can't be resolved cleanly (never guesses)."""
    acts = _actual_columns(rows, ax)
    vals = {pc.col: rows[row][pc.col] for pc in acts
            if pc.col < len(rows[row]) and _cell_type(rows[row][pc.col]) == 'num'}
    if not vals:
        return None
    col = periods.collapse(concept, concept_nature(concept), acts, vals, as_of=as_of,
                           require_bound=require_bound)
    if col.escalate or col.value is None:
        return None
    ccy, unit = _local_currency_unit(rows, ax)
    frame = units.resolve_monetary_frame(stmt_currency=ccy, geo_currency=geo_ccy,
                inr_mentioned=inr_mentioned, declared_unit=unit, sample_values=[col.value],
                anchor_cr=anchor_cr, ratecard=rate_card, base_currency=base_currency)
    if frame.escalate or frame.scale is None:
        return None
    try:
        q = Quantity(amount=col.value, currency=frame.currency, scale=frame.scale,
                     nature=concept_nature(concept), concept=concept)
        return (rate_card.to_inr(q.absolute_native(), frame.currency) / _CR).quantize(_Q, rounding=ROUND_HALF_UP)
    except Exception:  # noqa: BLE001
        return None


def _sheet_corroborated(prof, source_sheet, *, geo_ccy, inr_mentioned, anchor_cr, rate_card, as_of=None,
                        require_bound=False, base_currency=None):
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
                                inr_mentioned=inr_mentioned, anchor_cr=anchor_cr, rate_card=rate_card,
                                as_of=as_of, require_bound=require_bound, base_currency=base_currency)
        if not v0:
            continue
        for _d, _k, _n, s2, rows2, ax2, lc2, row2 in _family_carriers(prof, concept, source_sheet, as_of=as_of):
            v2 = _resource_value_cr(concept, rows2, ax2, lc2, row2, geo_ccy=geo_ccy,
                                    inr_mentioned=inr_mentioned, anchor_cr=anchor_cr, rate_card=rate_card,
                                    as_of=as_of, require_bound=require_bound, base_currency=base_currency)
            if v2 and reconcile.agree_within_orders(v0, v2, _AGREE):   # shared orders-band primitive
                return True
    return False


def _family_resource(prof, concept, primary_sheet, *, ident, label, geo_ccy, inr_mentioned,
                     anchor_cr, rate_card, as_of=None, require_bound=False, base_currency=None):
    """fork-b: deterministically re-source ONE concept from its best family-appropriate OTHER
    sheet — the architectural fix for 'a company's KPIs span P&L / balance-sheet / cash-flow, so
    one tab cannot source them all'. Resolves THAT sheet's own monetary frame from all its money
    lines, collapses over its actual columns, and emits via the shared _emit_from_collapse rule.
    CROSS-TAB consistency (money): if the best ALTERNATE family carrier yields a confirmed value
    >1 order of magnitude away, HOLD (wrong-row / cumulative-mislabel) rather than trust one sheet.
    Returns a Figure (confirmed emit, or an informative held), or None if no carrier exists."""
    carriers = _family_carriers(prof, concept, primary_sheet, as_of=as_of)
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
            collapsed[c] = periods.collapse(c, concept_nature(c), acts, vals, as_of=as_of,
                                            require_bound=require_bound)
    local_ccy, local_unit = _local_currency_unit(rows, ax)
    money_samples = [cc.value for cn, cc in collapsed.items()
                     if concept_measure(cn) == 'money' and not cc.escalate and cc.value is not None]
    frame = units.resolve_monetary_frame(stmt_currency=local_ccy, geo_currency=geo_ccy,
                inr_mentioned=inr_mentioned, declared_unit=local_unit, sample_values=money_samples,
                anchor_cr=anchor_cr, ratecard=rate_card, base_currency=base_currency)
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
                                        anchor_cr=anchor_cr, rate_card=rate_card, as_of=as_of,
                                        require_bound=require_bound, base_currency=base_currency)):
        return Figure(concept, None, None, prov, held=True, basis=fig.basis, months=fig.months,
            hold_reason=(f're-source {sheet} not cross-sheet-corroborated as own income statement '
                         f'(possible subsidiary/stray) — held')[:90])
    return fig


# ── grid-statement fallback (tight KIND-GATE) ────────────────────────────────────────────────────
# A comparison-GRID that is a self-declared statement (or income-genuine CONFIRMED) is a LEGITIMATE
# source the `is_time_series` exclusion wrongly dropped from every path (deterministic AND model). The
# fix is universal-but-TIGHT: eligibility keys on a name-free ACCOUNTING property — statement KIND or the
# income identity — never on "is it a grid". The blanket flip was measured to expose 63 junk grids vs 6
# real-statement grids (10:1); the kind-gate excludes junk AT THE DOOR (defense-in-depth, not reliance on
# downstream guards). Fail-CLOSED on kind RECALL: a statement-SHAPED grid that fails BOTH routes is
# HELD-and-DISCLOSED (a kind-miss surfaces as a recoverable hold, never a silent skip — otherwise the tight
# gate just moves the exclusion bug from is_time_series to kind=None, trading a caught over-inclusion for an
# invisible under-inclusion). ISOLATED: grids only, still-held/gapped concepts only, after fork-b + before
# the model — so every series emit stays byte-identical.
_GRID_STMT_KINDS = ('income', 'balance', 'cash_flow')


def _is_statement_grid(rows, ax, label_col, sheet) -> bool:
    """Sheet-level tight gate (concept-agnostic): a GRID is a statement source iff it self-declares a
    statement KIND, or the income identity is genuinely CONFIRMED on it. Name-free, universal."""
    if _statement_kind(rows, sheet) in _GRID_STMT_KINDS:
        return True
    return _family_verdicts(rows, ax, label_col).get('income_statement') == family.CONFIRMED


def _grid_eligible(rows, ax, label_col, sheet, concept) -> Tuple[bool, object]:
    """Per-concept tight kind-gate for a GRID source. Eligible iff the grid self-declares an ALLOWED
    statement kind for the concept, OR (income concept) the income identity is CONFIRMED on it.
    Returns (eligible, kind). ASSUMPTION-PROOF #1: the income-CONFIRMED route reuses the series-path
    _family_verdicts primitive on GRID shape — verified by test, not assumed to carry over."""
    kind = _statement_kind(rows, sheet)
    if kind in _GRID_STMT_KINDS and _concept_allowed_on_kind(concept, kind):
        return True, kind
    if (family.CONCEPT_FAMILY.get(concept) == family.INCOME_STATEMENT
            and _family_verdicts(rows, ax, label_col).get('income_statement') == family.CONFIRMED):
        return True, kind
    return False, kind


def _grid_resource(prof, concept, primary_sheet, *, ident, label, geo_ccy, inr_mentioned,
                   anchor_cr, rate_card, as_of=None, require_bound=False, base_currency=None):
    """Deterministic GRID-statement fallback for a still-held/gapped concept. Returns:
      • a CONFIRMED Figure — the best eligible grid statement collapsed + emitted (disclosed grid source);
      • a HELD Figure (§4 guard) — a statement-SHAPED grid carries the concept but is NOT kind-eligible
        (kind-recall miss → recoverable hold, never a silent skip); or the emit-attempt's own hold;
      • None — no grid carries the concept at all (nothing to surface).
    Multi-scope / comparative conflicts fail-close INSIDE collapse (→ held). GRIDS ONLY — series sheets
    are never read here, so every series emit is byte-identical."""
    candidate_held = None                        # §4: a statement-shaped-but-unconfirmed grid, if any
    best = None                                  # (dump, -ncols, sheet, rows, ax, lc, row, kind)
    for s in prof['sheets']:
        if s.sheet == primary_sheet or s.sheet not in prof['grid']:
            continue
        rows = prof['grid'][s.sheet]
        ax = periods.detect_period_axis(rows)
        if ax.is_time_series or not ax.columns:      # GRIDS ONLY (series handled upstream)
            continue
        lc = _sheet_label_col(rows, ax.axis_rows[0])
        if lc is None:
            continue
        row = _find_concept_row(rows, lc, concept, ax.axis_rows[0] + 1, len(rows), ax.columns)
        if row is None:
            continue
        stale, fwd = _sheet_vintage_flags(rows, ax, as_of, {concept: row})
        if stale or fwd:                    # centralised vintage guard — never re-source from a stale/plan grid
            continue
        eligible, kind = _grid_eligible(rows, ax, lc, s.sheet, concept)
        if not eligible:
            # §4: a NON-dump grid that carries the concept row is statement-SHAPED → surface a
            # recoverable hold (a dump is genuinely junk, not a mis-classified statement → no bucket).
            if candidate_held is None and not tiers.is_dump(rows, ax.axis_rows[0] + 1, lc):
                prov = Provenance(source_file=label, content_fingerprint=ident.content_fp, sheet=s.sheet,
                                  cell=_a1(lc, row), row_label=str(rows[row][lc]).strip())
                candidate_held = Figure(concept, None, None, prov, held=True,
                    hold_reason=(f'grid statement candidate on {s.sheet!r} not kind-confirmed '
                                 f'(kind={kind}) — held for review, not skipped')[:90])
            continue
        dump = tiers.is_dump(rows, ax.axis_rows[0] + 1, lc)
        key = (dump, -len(ax.columns), s.sheet)
        if best is None or key < best[0]:
            best = (key, s.sheet, rows, ax, lc, row, kind)
    if best is None:
        return candidate_held
    _key, sheet, rows, ax, lc, row, kind = best
    acts = _actual_columns(rows, ax)
    found = {c: _find_concept_row(rows, lc, c, ax.axis_rows[0] + 1, len(rows), ax.columns)
             for c in MIS_CONCEPTS}
    collapsed = {}
    for c in MIS_CONCEPTS:
        r = found.get(c)
        if r is None:
            continue
        vals = {pc.col: rows[r][pc.col] for pc in acts
                if pc.col < len(rows[r]) and _cell_type(rows[r][pc.col]) == 'num'}
        if vals:
            collapsed[c] = periods.collapse(c, concept_nature(c), acts, vals, as_of=as_of,
                                            require_bound=require_bound)
    local_ccy, local_unit = _local_currency_unit(rows, ax)
    money_samples = [cc.value for cn, cc in collapsed.items()
                     if concept_measure(cn) == 'money' and not cc.escalate and cc.value is not None]
    frame = units.resolve_monetary_frame(stmt_currency=local_ccy, geo_currency=geo_ccy,
                inr_mentioned=inr_mentioned, declared_unit=local_unit, sample_values=money_samples,
                anchor_cr=anchor_cr, ratecard=rate_card, base_currency=base_currency)
    prov = Provenance(source_file=label, content_fingerprint=ident.content_fp, sheet=sheet,
                      cell=_a1(lc, row), row_label=str(rows[row][lc]).strip())
    col = collapsed.get(concept)
    scope = ''
    if col is not None:
        _cite_value_cells(prov, col, row, {pc.col: pc for pc in ax.columns})
        if col.source_cols:                      # disclose the scope banner of the column actually read
            banner = _col_banner(rows, ax, col.source_cols[-1])
            scope = f'; scope={banner}' if banner else ''
    prov.note = (prov.note + '; ' if prov.note else '') + f'grid statement source (kind={kind}{scope})'
    fig = _emit_from_collapse(concept, col, prov, stmt_kind=kind, frame=frame, rows=rows, ax=ax,
                              label_col=lc, ebitda_row=found.get('ebitda'), anchor_cr=anchor_cr,
                              rate_card=rate_card)
    if isinstance(fig, Figure) and fig.confirmed:
        return fig
    # emit attempt held (multi-scope/frame/etc.) — return the more informative of it vs the §4 candidate
    return fig if isinstance(fig, Figure) else candidate_held


# ── operating-revenue disposition (UNIVERSAL, structure-keyed — never a sheet/file name) ─────────────
# "Total Income" / "Total revenue" is an AGGREGATE that, by the Ind-AS identity, folds Revenue-from-
# operations TOGETHER WITH non-operating "Other Income"; the reportable figure is OPERATING revenue, so
# emitting the aggregate AS revenue is a silent relabel and forbidden. Three universal dispositions,
# keyed only on label semantics + the presence of sibling rows in the section:
#   • RELOCATE — the aggregate has a directly-stated "Revenue from operations" row in-section → emit that
#     row (a real cell whose value reconstructs cleanly, Σ cells × scale = value; cite-evidence honest).
#   • HOLD     — the aggregate has NO stated RfO row but an "Other Income" row IS present in-section →
#     positive evidence it folds in non-operating income. Operating revenue = Total Income − Other Income
#     is a SUBTRACTION the additive provenance model cannot yet cite (summing the cited cells gives
#     TI + OI, not TI − OI), so emitting any number here would break never-a-wrong-number. FAIL-CLOSED:
#     emit nothing, disclose why. (A signed-expression provenance extension is the committed universal fix
#     that later recovers the citable derived figure — for the CLASS where RfO exists but only the total is
#     stated. Companies whose entire "Total Income" is itself non-operating, e.g. a pre-revenue firm living
#     on treasury interest, have NO operating revenue and stay correctly held even after that lands.)
#   • KEEP     — not an aggregate ("Gross Revenue", or a Σ of pure operating sub-lines with no Other-Income
#     sibling) → emit the located row (no evidence of contamination; a known-operating figure).
_TOTAL_INCOME_RE = re.compile(r'\btotal\s+(income|revenue|revenues)\b', re.I)
_REV_FROM_OPS_RE = re.compile(r'\brevenue\s+from\s+operation', re.I)
# 'Other income' is UNAMBIGUOUSLY non-operating in every industry — unlike "interest income", which IS
# operating revenue for a lender/NBFC — so it is the one safe positive-evidence discriminator here.
_OTHER_INCOME_RE = re.compile(r'\bother\s+income\b', re.I)
_REV_AGG_HOLD = ("aggregate 'Total Income' folds in non-operating income (Other Income row present) and no "
                 "stated Revenue-from-operations row exists — operating revenue = Total Income − Other "
                 "Income is a subtraction not yet cell-citable → held (fail-closed; not a wrong number)")


def _is_total_income_aggregate(rows, label_col, r):
    """True iff row r is a 'Total Income'/'Total Revenue' AGGREGATE (folds in Other Income by the Ind-AS
    identity) and is NOT itself an operating-revenue row. Label semantics only — universal."""
    if not (r is not None and 0 <= r < len(rows) and label_col < len(rows[r])):
        return False
    lab = str(rows[r][label_col] or '')
    return bool(_TOTAL_INCOME_RE.search(lab)) and not _REV_FROM_OPS_RE.search(lab)


def _operating_revenue_row(rows, label_col, rev_row):
    """If the located revenue row is a TOTAL-INCOME aggregate and a directly-stated 'Revenue from
    operations' row exists in the same section above it, return that row (the clean, citable operating
    figure); else None. Universal — label semantics only, no sheet/file assumptions."""
    if not _is_total_income_aggregate(rows, label_col, rev_row):
        return None
    for r in range(rev_row - 1, max(-1, rev_row - 20), -1):         # scan up, within the section
        lab = str(rows[r][label_col] or '') if label_col < len(rows[r]) else ''
        if _REV_FROM_OPS_RE.search(lab):
            return r
    return None


def _other_income_present(rows, label_col, rev_row):
    """True iff an 'Other income' row exists in the located row's section (scan up, section-window).
    Positive evidence that a 'Total Income' aggregate folds in NON-operating income. Universal."""
    if rev_row is None:
        return False
    for r in range(rev_row - 1, max(-1, rev_row - 20), -1):
        lab = str(rows[r][label_col] or '') if label_col < len(rows[r]) else ''
        if _OTHER_INCOME_RE.search(lab):
            return True
    return False


def _operating_revenue_disposition(rows, label_col, rev_row):
    """UNIVERSAL disposition for a located revenue row (structure-keyed, never a file/sheet name).
    Returns one of ('relocate', rfo_row) | ('hold', reason) | ('keep', None). See the block comment."""
    if not _is_total_income_aggregate(rows, label_col, rev_row):
        return ('keep', None)
    rfo = _operating_revenue_row(rows, label_col, rev_row)
    if rfo is not None:
        return ('relocate', rfo)
    if _other_income_present(rows, label_col, rev_row):
        return ('hold', _REV_AGG_HOLD)
    return ('keep', None)


def extract_company(label: str, path: str, *, rate_card, entity: str = None,
                    domicile=None, base_currency=None, anchor_cr=None, as_of_year: int = None,
                    use_model: bool = False, use_finder: bool = False) -> Record:
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
    _finder = use_finder and llm._resolve_provider() is not None   # whole-file finder emit path (Step 4)
    # The source's own stated reporting boundary (filename month; header-cell deferred). Threaded into
    # every value-selecting collapse so a forward-projection column past this date is never picked/summed.
    # When NO boundary resolves (a file whose name carries no month), the value path must NOT fall back to
    # the old unguarded max(order) — that is the projection hole this rung closes. `require_bound` makes
    # collapse fail-CLOSED in that case (hold a projection-ambiguous month selection). Flow-derived tier
    # (derive the bound from a flow's own latest actual month, flagged) is deferred to the U6 turn-on,
    # where the currently-None-bound files (CSS/CPM, numeric-prefix names) first begin to emit.
    stated_asof = _stated_as_of(prof, path)
    require_bound = stated_asof is None

    # Vintage-aware sheet selection: the stated as-of demotes an old-vintage or forward-plan statement
    # below every current-actual one (see _best_sheet / _sheet_vintage_flags). Emit path ONLY — routing
    # (_classify_file) and the model probe-order keep the vintage-blind call so a stale-but-comprehensive
    # sheet still proves 'this is an MIS file' and is still probed.
    best = _best_sheet(prof, MIS_CONCEPTS, as_of=stated_asof)
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
                    anchor_cr=anchor_cr, rate_card=rate_card, fields=fields, source_label=label,
                    as_of=stated_asof, require_bound=require_bound)
        if all(not isinstance(fields[c], Figure) or (fields[c].held or fields[c].gap)
               for c in MIS_CONCEPTS):
            fields['_note'] = 'no time-series statement found'
        _finalize_terminal_state(fields)             # choke: no figure ships in limbo
        return Record('mis', entity_id=entity, fields=fields)
    _n, _c, sheet, rows, ax, label_col, found = best
    # ── ALL-STALE guard (fail-CLOSED) ────────────────────────────────────────────────────────────────
    # `_best_sheet` only DEMOTES a stale/forward sheet (so the primary path always offers its best
    # available statement). The consequence the demotion alone does NOT cover: when EVERY candidate is
    # stale-vintage or a forward plan, the winner is itself stale — and emitting it would ship a
    # wrong-PERIOD / budget figure AS current (the exact defect this guard exists to prevent). The winner
    # being stale/forward ⟺ no current-actual time-series statement exists (a non-stale sheet would have
    # out-ranked it). So the primary sheet must source NOTHING: zero out `found` → the primary loop emits
    # only gaps; the vintage-EXCLUDING fallbacks (fork-b family / grid) then still recover any concept that
    # lives on a CURRENT grid or carrier, and whatever stays unrecovered is disclosed as an all-stale HOLD
    # below. A stale number therefore never ships silently. (DELIBERATE fail-CLOSED, not disclose-emit: a
    # held+disclosed figure surfaces at the review gate for a human to resolve — wrong file, or a
    # not-yet-closed month — whereas a disclose-emitted value can be silently consumed as current
    # downstream; the review-gate reporting period is the Option-3 backlog fix for the filename-less case.)
    all_source_stale = any(_sheet_vintage_flags(rows, ax, stated_asof, found))
    if all_source_stale:
        found = {}
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
    # ── Rung-2 bound-resolution ladder (now the sheet + concept rows are known) ──────────────────────
    # filename (stated_asof) → under-statement distrust → flow-derived (flagged) → fail-closed floor.
    # `require_bound` stays True ONLY when NO bound resolves, so the value path is never left fail-open.
    flow_derived = False
    if stated_asof is not None and _filename_understated(rows, acts, found, stated_asof):
        # The filename is a single point of trust; ≥2 valued MONTH columns dated AFTER it on a concept's
        # own row means the file was re-saved with newer data under a stale name. The bound would silently
        # drop legitimate recent actuals and bind an older column — a WRONG value staleness can't catch.
        # Distrust the name and fall to the fail-closed floor (NOT flow-derived: on a contradicted file the
        # 'newer' columns are themselves unproven). A lone forecast stub (<2) never trips this.
        stated_asof, require_bound = None, True
    elif stated_asof is None:
        # No filename month at all → derive an in-DOCUMENT boundary from a flow's own latest actual month
        # (tier 3, FLAGGED — not projection-immune). Only when even that is absent does the floor hold.
        fd = _flow_derived_asof(rows, acts, found)
        if fd is not None:
            stated_asof, require_bound, flow_derived = fd, False, True
    collapsed = {}
    provs = {}
    rev_hold = None                                    # operating-revenue fail-closed disclosure (if any)
    colmap = {pc.col: pc for pc in ax.columns}
    for concept in MIS_CONCEPTS:
        row = found.get(concept)
        rfo = None
        if concept == 'revenue':                       # operating-revenue disposition (universal, structure-keyed)
            disp, target = _operating_revenue_disposition(rows, label_col, row)
            if disp == 'relocate':
                rfo, row = target, target              # emit the directly-stated Revenue-from-operations row
            elif disp == 'hold':
                rev_hold = target                      # aggregate folds in non-op income, no RfO → HOLD
        provs[concept] = Provenance(                   # for a revenue HOLD, prov points at the aggregate row
            source_file=label, content_fingerprint=ident.content_fp, sheet=sheet,
            cell=_a1(label_col, row) if row is not None else '',
            row_label=str(rows[row][label_col]).strip() if row is not None else '')
        if row is None:
            continue
        if concept == 'revenue' and rev_hold:          # never collapse → no value is emitted (fail-closed)
            continue
        values = {pc.col: rows[row][pc.col] for pc in acts
                  if pc.col < len(rows[row]) and _cell_type(rows[row][pc.col]) == 'num'}
        if not values:
            continue
        collapsed[concept] = periods.collapse(concept, concept_nature(concept), acts, values,
                                              as_of=stated_asof, require_bound=require_bound)
        _cite_value_cells(provs[concept], collapsed[concept], row, colmap)
        if rfo is not None:                            # disclose the relocation to operating revenue
            provs[concept].note = (provs[concept].note + '; ' if provs[concept].note else '') + \
                'operating revenue (relocated from total-income aggregate to Revenue-from-operations row)'

    # ── Resolve the STATEMENT monetary frame ONCE (money concepts) → apply to ALL ──
    money_samples = [c.value for cn, c in collapsed.items()
                     if concept_measure(cn) == 'money' and not c.escalate and c.value is not None]
    frame = units.resolve_monetary_frame(stmt_currency=local_ccy, geo_currency=geo_ccy,
                                         inr_mentioned=inr_mentioned, declared_unit=local_unit,
                                         sample_values=money_samples, anchor_cr=anchor_cr,
                                         ratecard=rate_card, base_currency=base_currency)

    fields = {'company': entity}
    stmt_kind = _statement_kind(rows, sheet)         # the source sheet's self-declared statement type
    for concept in MIS_CONCEPTS:
        if concept == 'revenue' and rev_hold:        # operating revenue not cell-citable → held, disclosed
            fields[concept] = Figure(concept, None, None, provs[concept], held=True, hold_reason=rev_hold)
            continue
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
                                  inr_mentioned=inr_mentioned, anchor_cr=anchor_cr, rate_card=rate_card,
                                  as_of=stated_asof, require_bound=require_bound, base_currency=base_currency)
        if isinstance(newfig, Figure) and not newfig.gap:        # found on a family sheet (emit or informative hold)
            fields[concept] = newfig

    # ── R4: deterministic cash ⇐ closing_cash recovery (advisor 2026-08-14) ────────
    # The best sheet's row-picker binds `cash` (a STOCK) to a FLOW line ("Cash Collected"/
    # "Cash outflow") because the cash lexicon matches "cash*" flows, while the true bank stock
    # ("Closing balance") lacks a 'cash' token and is reachable ONLY via the closing_cash
    # synonym. closing_cash is not a MIS target and never enters `remaining`, so the model-path
    # cash⇐closing_cash equivalence can never locate it — the recovery is absent from the
    # deterministic path entirely. When a stock target is HELD on a flow row, locate its
    # balance-sheet equivalent on the SAME statement and rebind. Fail-closed both ways: the
    # equivalent row must itself be a proper NON-flow balance, and the rebound figure must clear
    # the full _emit_from_collapse gate (stock/flow, zero-stock, anchor-orders) or the hold
    # stays. Universal — keys off concept_nature+flow-label, no sheet/label hardcoding.
    for tgt, eq in CONCEPT_EQUIVALENCE.items():
        fig = fields.get(tgt)
        if not (isinstance(fig, Figure) and fig.held and concept_nature(tgt) == 'stock'
                and _is_flow_label(fig.provenance.row_label)):
            continue
        eq_row = _find_equivalent_stock_row(rows, label_col, eq, ax.axis_rows[0] + 1, len(rows), ax.columns)
        if eq_row is None:
            continue
        eq_label = str(rows[eq_row][label_col]).strip()
        eq_vals = {pc.col: rows[eq_row][pc.col] for pc in acts
                   if pc.col < len(rows[eq_row]) and _cell_type(rows[eq_row][pc.col]) == 'num'}
        if not eq_vals:
            continue
        eq_prov = Provenance(source_file=label, content_fingerprint=ident.content_fp, sheet=sheet,
                             cell=_a1(label_col, eq_row), row_label=eq_label)
        eq_col = periods.collapse(tgt, concept_nature(tgt), acts, eq_vals, as_of=stated_asof,
                                  require_bound=require_bound)
        _cite_value_cells(eq_prov, eq_col, eq_row, colmap)
        newfig = _emit_from_collapse(tgt, eq_col, eq_prov, stmt_kind=stmt_kind, frame=frame,
                                     rows=rows, ax=ax, label_col=label_col, ebitda_row=None,
                                     anchor_cr=anchor_cr, rate_card=rate_card)
        if isinstance(newfig, Figure) and not (newfig.held or newfig.gap):
            fields[tgt] = newfig

    # ── Lever 2a: reconciliation-guided cash rebind (2026-09-11) ─────────────────────────────────
    # A cash STOCK held as "wrong row / >3 orders from company scale" is the tiny bank-only sub-line
    # that out-scored the true TOTAL on label keywords alone ('Closing Cash Balance including Fixed
    # deposits' 2.23 Mn beats 'TOTAL CASH AND CASH EQUIVALENT' 86.33 Mn — the label LIES; only the
    # data exposes it). Names propose the candidate rows; two INDEPENDENT identities on the SAME
    # statement dispose (roll-forward opening+net=closing; Σ components = total). Rebind to the
    # aggregate BOTH confirm, then run the full _emit_from_collapse gate. ADDITIVE + fail-closed:
    # fires only on an already-held cash stock and needs ≥2 agreeing anchors, so it can only turn a
    # HELD cash into an emit — every existing emit is byte-identical by construction.
    cfig = fields.get('cash')
    if isinstance(cfig, Figure) and cfig.held and concept_nature('cash') == 'stock':
        rb = _reconciled_cash_rebind(rows, label_col, acts, stated_asof, require_bound)
        if rb is not None:
            rb_row, rb_col = rb
            rb_label = str(rows[rb_row][label_col]).strip()
            rb_prov = Provenance(source_file=label, content_fingerprint=ident.content_fp, sheet=sheet,
                                 cell=_a1(label_col, rb_row), row_label=rb_label)
            _cite_value_cells(rb_prov, rb_col, rb_row, colmap)
            newfig = _emit_from_collapse('cash', rb_col, rb_prov, stmt_kind=stmt_kind, frame=frame,
                                         rows=rows, ax=ax, label_col=label_col, ebitda_row=None,
                                         anchor_cr=anchor_cr, rate_card=rate_card)
            if isinstance(newfig, Figure) and not (newfig.held or newfig.gap):
                fields['cash'] = newfig

    # ── grid-statement fallback (tight kind-gate, DETERMINISTIC-first, before the model) ──────────
    # A self-declared (or income-CONFIRMED) comparison-GRID is a legitimate source the is_time_series
    # exclusion dropped. ADDITIVE + ISOLATED (grids only, still-held/gapped only) → every series emit
    # is byte-identical. A confirmed grid figure UPGRADES a hold/gap; a §4 held-candidate replaces only
    # a bare GAP (surfaces a kind-recall miss for review), never clobbering a more-informative hold.
    grid_candidates = []
    for concept in MIS_CONCEPTS:
        fig = fields.get(concept)
        if not (isinstance(fig, Figure) and (fig.gap or fig.held)):
            continue
        if concept == 'revenue' and rev_hold:        # a fail-closed operating-revenue hold is authoritative:
            continue                                 # a grid 'Total Income' aggregate would re-relabel it
        gfig = _grid_resource(prof, concept, sheet, ident=ident, label=label, geo_ccy=geo_ccy,
                              inr_mentioned=inr_mentioned, anchor_cr=anchor_cr, rate_card=rate_card,
                              as_of=stated_asof, require_bound=require_bound, base_currency=base_currency)
        if isinstance(gfig, Figure) and gfig.confirmed:
            fields[concept] = gfig
        elif isinstance(gfig, Figure) and fields[concept].gap:
            fields[concept] = gfig                # gap → grid emit-attempt/held-candidate (§4, more informative)
        if isinstance(gfig, Figure) and gfig.held and 'not kind-confirmed' in (gfig.hold_reason or ''):
            grid_candidates.append(concept)
    if grid_candidates:
        fields['_grid_held_candidates'] = grid_candidates   # §4 recall bucket (reported in re-measure)

    if all_source_stale:
        # DISCLOSE the all-stale HOLD: the primary sourced nothing (stale/forward); the vintage-excluding
        # fallbacks recovered whatever lives on a CURRENT grid/carrier. Every concept STILL unresolved
        # (a bare gap, or a reason-less hold from the zeroed primary) is tagged with the vintage reason so
        # the audit trail explains WHY it is empty — never silently. A confirmed fallback emit or an
        # informative fallback hold (a current-sheet conflict) is MORE specific and is left untouched.
        la = max((pc.order for pc in _actual_columns(rows, ax) if pc.kind in _POINT_IN_TIME_KINDS
                  and 2000 <= pc.order[0] < 9000 and pc.order <= stated_asof), default=None)
        gap_mo = (stated_asof[0] - la[0]) * 12 + (stated_asof[1] - la[1]) if la else None
        reason = (f'no current-actual statement in file: every source sheet is stale-vintage or a forward '
                  f'plan vs stated reporting period {stated_asof[0]}-{stated_asof[1]:02d}'
                  + (f' (latest actual {gap_mo} months earlier)' if gap_mo else '')
                  + ' — held, not emitted as current')[:140]
        for concept in MIS_CONCEPTS:
            fig = fields.get(concept)
            if isinstance(fig, Figure) and (fig.gap or (fig.held and not (fig.hold_reason or '').strip())):
                fields[concept] = Figure(concept, None, None, fig.provenance, held=True, hold_reason=reason)

    # ⚠️ VINTAGE-GUARD CHECKLIST (AI ACTIVATION) — the model/finder locators select & read sheets
    # INDEPENDENTLY of the four deterministic selectors, so each is a fresh stale/forward-plan route once
    # the AI is enabled. Both are inert today (OFF by default → deterministic spine byte-identical). STATUS:
    #  • `_model_fill` (use_model): GUARDED — the vintage location-filter runs at the `_model_emit` choke
    #    (`_location_is_stale`), refusing a passing verdict whose located sheet is stale/plan. Deterministically
    #    tested (test_vintage_guard), so this is CODE-ENFORCED, not a promise. FILTERS THE RETURNED LOCATION,
    #    never the inventory (which must stay exhaustive).
    #  • `_model_find_across_file` (use_finder): NOT YET GUARDED — it has its OWN emit path (multi-region
    #    Σ-evidence, candidate aggregation), not the `_model_emit` choke. BEFORE enabling use_finder in prod:
    #    apply the SAME `_location_is_stale` filter at ITS emit point (again: filter the returned LOCATION, not
    #    `_finder_inventory`'s list), WITH a reddening control + a live end-to-end check. Tracked in memory:
    #    project_vintage_guard. Coverage is PARTIAL by design — do not read "model guarded" as "finder guarded".
    if _model or _finder:                            # locator fallback on holds/gaps only
        held = [c for c in MIS_CONCEPTS
                if isinstance(fields.get(c), Figure) and (fields[c].held or fields[c].gap)]
        if held:
            if _finder:                              # whole-file scope resolver (Step 4)
                _model_find_across_file(prof, ident, held, entity=entity, domicile=domicile,
                    anchor_cr=anchor_cr, rate_card=rate_card, fields=fields, source_label=label,
                    as_of=stated_asof, require_bound=require_bound, base_currency=base_currency,
                    boundary_verified=(not require_bound and not flow_derived))   # stocks hold on a weak boundary
            else:
                _model_fill(prof, ident, held, entity=entity, domicile=domicile,
                    anchor_cr=anchor_cr, rate_card=rate_card, fields=fields, source_label=label,
                    as_of=stated_asof, require_bound=require_bound, base_currency=base_currency)
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
        alts = _alt_stock_sources(prof, c, sheet, rate_card, geo_ccy, inr_mentioned, as_of=stated_asof,
                                  require_bound=require_bound, base_currency=base_currency)
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
    # Tier-3 disclosure: when the reporting boundary was FLOW-DERIVED (no filename month), flag every
    # emitted figure so the weaker (not projection-immune) boundary is auditable, never silent.
    if flow_derived:
        for c in MIS_CONCEPTS:
            f = fields.get(c)
            if isinstance(f, Figure) and f.confirmed and f.provenance is not None:
                f.provenance.note = (f.provenance.note + '; ' if f.provenance.note else '') + \
                    'as-of flow-derived (no stated/filename reporting date) — period boundary unverified'
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


def _col_banner(rows, ax, col) -> str:
    """The nearest non-empty TEXT banner just above the axis in column `col` — its scope/scenario
    label (e.g. 'ACTUAL', 'Consolidated', 'Budget'). Used to DISCLOSE which column a grid emit came
    from, so a reviewer sees the scope explicitly (the discriminator a budget/variance mis-pick would
    otherwise hide). '' when the column carries no banner."""
    for ar in ax.axis_rows:
        for rr in range(ar - 1, max(-1, ar - 4), -1):
            if 0 <= rr < len(rows) and col < len(rows[rr]) and _cell_type(rows[rr][col]) == 'text':
                t = str(rows[rr][col]).strip()
                if t:
                    return t[:24]
    return ''


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


# ── Stated reporting as-of (Rung 2): a NON-CIRCULAR reporting boundary ────────────────────────────
# The value path selects the latest / summed period column. To exclude a forward PROJECTION column
# (an unlabeled future month the scenario filter misses), collapse needs the source's own STATED
# reporting date — read from a DECLARED source, NEVER from a data column (that would be circular: a
# projected column would inflate the very bound meant to exclude it). Returns (year, month) or None;
# None → the caller keeps today's behaviour (no bound), so a source without a declared date is never
# corrupted — it simply gets no projection guard.
#
# The bound is the FILENAME's reporting month. A DELIBERATELY-REJECTED alternative was an in-cell
# 'as on <date>' header scan: measured on the real files it FALSE-POSITIVES on operational notes and
# prior-year comparatives that share the cue words — Hubler's only 'as on' cell is a stock NOTE
# ("Orders under delivery - as on 1 march 2023") that would bound out every Feb-2026 actual, and
# Agnikul carries both true statement titles AND note/comparative dates ("299 employees as on 28th
# Feb 2025", "For YE as on 31st March 2025"). A reliable header as-of needs a statement-TITLE gate
# (the date must sit in a 'BALANCE SHEET / INCOME STATEMENT / CASH FLOW … as at' title, not a note),
# which is a separately-validated enhancement. The filename month is present and consistent across
# every file and is immune to that in-cell noise, so it is the operative bound today.
_FILE_PREPDATE_RE = re.compile(r'20\d\d[_\-]\d{1,2}(?:[_\-]\d{1,2})?')   # yyyy_mm_dd prep/version stamp


def _filename_month(path) -> Optional[tuple]:
    """The reporting month declared in the FILENAME as (year, month) — the LAST spelled month-name +
    year token (…_MIS_Feb26 / _MIS_Feb_2026), after removing the numeric yyyy_mm_dd prep-date the fund
    prefixes (so 'AVF_2026_03_12_…_Feb26' reads Feb-2026, not Mar). None if no spelled month present.
    Separators (._-) are normalised to spaces first because '_' is a regex word char — anchoring on
    \\b would never fire inside 'mis_feb26'. The digit requirement after the month name stops a
    company name from false-matching; the year window rejects a stray count read as a year."""
    base = _FILE_PREPDATE_RE.sub(' ', os.path.basename(path).lower())
    base = re.sub(r'[^a-z0-9]+', ' ', base)          # ._- → space: 'mis_feb26' → 'mis feb26'
    best = None

    def _yy(s):
        y = int(s)
        return y + 2000 if y < 100 else y

    # month-then-year ('Feb26', 'Feb 2026') AND year-then-month ('2025 May', '2025_May_Analisa') —
    # both orders occur across fund conventions; the LAST spelled month-year token wins.
    for m in re.finditer(r"([a-z]{3,9})\s*(\d{2,4})", base):          # month → year
        mon = m.group(1)[:3]
        if mon in periods._MONTHS and 2000 <= _yy(m.group(2)) <= 2100:
            best = (_yy(m.group(2)), periods._MONTHS[mon])
    for m in re.finditer(r"(\d{4})\s+([a-z]{3,9})", base):            # year → month (4-digit year only)
        mon = m.group(2)[:3]
        if mon in periods._MONTHS and 2000 <= int(m.group(1)) <= 2100:
            best = (int(m.group(1)), periods._MONTHS[mon])
    return best


def _stated_as_of(prof, path) -> Optional[tuple]:
    """The source's own stated reporting boundary as (year, month) — the filename's reporting month —
    or None when the filename declares no month (caller then keeps today's unbounded behaviour). See
    the module note above for why the in-cell header scan is deliberately excluded until title-gated."""
    return _filename_month(path)


def _flow_derived_asof(rows, cols, found) -> Optional[tuple]:
    """Tier-3 bound (the agreed ladder's rung below the filename): the latest MONTH period carrying a
    value on a FLOW concept's row (revenue/EBITDA) — an IN-DOCUMENT reporting boundary when the filename
    declares no month. FLAGGED, because it is not projection-immune: a flow can itself carry a forecast
    column, so the derived date can be an over-statement (never an under-statement — a flow's actuals only
    run to its latest real month). None when no flow row has a dated month value → caller fails closed."""
    best = None
    for concept in ('revenue', 'ebitda'):
        row = found.get(concept)
        if row is None:
            continue
        for pc in cols:
            if (pc.kind == periods.MONTH and pc.order != (0, 0)
                    and pc.col < len(rows[row]) and _cell_type(rows[row][pc.col]) == 'num'):
                if best is None or pc.order > best:
                    best = pc.order
    return best


def _filename_understated(rows, cols, found, asof) -> bool:
    """True when the DATA contradicts the filename as-of — a valued MONTH column dated after `asof`, on a
    located concept's own row, that is NOT a cadence-peel outlier. The discriminator is CADENCE, not a
    count: a genuine off-by-one under-statement (a file re-saved ONE month forward under a stale name) is
    a single cadence-CONSISTENT plausible-next-month — a count threshold would miss it — whereas a typo
    tail like Clientell's '2026-12-25' is a gross-jump outlier the cadence-peel already removes, so it is
    excluded here and never trips the guard. The caller then distrusts the filename and fails closed."""
    peeled = periods._period_outlier_cols([c for c in cols if c.kind in (periods.MONTH, periods.QUARTER)])
    for row in found.values():
        if row is None:
            continue
        for pc in cols:
            # a column dated AFTER the filename as-of contradicts the name ONLY if it carries a
            # real (NON-ZERO) figure. A future-month column holding 0 is an empty placeholder the
            # template pre-lays (Aliste 'P&L' Mar-2026 = 0 after a Feb-2026 as-of), NOT newer
            # data — treating it as data distrusts a correct filename and fail-closes a live
            # figure. (Same principle _conflicting_periods already applies: a zero/blank is not
            # a real value.) A genuine one-month-forward re-save still carries real actuals here.
            if (pc.kind == periods.MONTH and pc.order != (0, 0) and pc.order > asof
                    and pc.col not in peeled and pc.col < len(rows[row])
                    and _cell_type(rows[row][pc.col]) == 'num' and rows[row][pc.col] != 0):
                return True
    return False


def _collapse_row(rows, cols, concept, rec, as_of=None, require_bound=False):
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
    return periods.collapse(concept, concept_nature(concept), cols, values, as_of=as_of,
                            require_bound=require_bound)


def _disclose_currency_basis(prov, frame):
    """Tag an emit whose currency came from a USER-CONFIRMED base currency (the batch/fund assertion)
    on its provenance — the non-negotiable audit trail: a figure resting on the user's assertion must
    be distinguishable from a file-detected one so a wrong assertion on any single file is catchable.
    File-detected (statement token) and rate-converted emits are left unchanged (byte-identical) — the
    ABSENCE of this tag means the currency was positively determined from the file itself."""
    if ('currency_user_confirmed' in (getattr(frame, 'flags', None) or [])
            and 'user-confirmed base currency' not in (prov.note or '')):
        prov.note = ((prov.note + '; ') if prov.note else '') + \
            f'ccy: user-confirmed base currency {frame.currency}'


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
    _disclose_currency_basis(prov, frame)                            # audit: file / user-confirmed / rate
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
        _anchor_reason = _figure_anchor_hold_reason(concept, value_cr, anchor_cr)
        if _anchor_reason:
            return Figure(concept, None, None, prov, held=True, basis=basis, months=months,
                          hold_reason=_anchor_reason)
        return Figure(concept, value_cr, None, prov, basis=basis, months=months)
    except Exception as e:  # noqa: BLE001 — a genuine code fault: FX-uncovered is held earlier at the
        return Figure(concept, None, None, prov, held=True, basis=basis, months=months,  # frame, not here
                      hold_reason=f'UNEXPECTED_ERROR: {type(e).__name__}: {e}'[:90])


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
def _location_is_stale(prof, prov, concept, as_of) -> bool:
    """True iff a MODEL-located value's SHEET is vintage-stale or a forward plan vs the stated as-of.
    Filters the model's RETURNED LOCATION at the emit choke — NOT the finder's inventory (which must
    stay EXHAUSTIVE so nothing is silently dropped) — so the model may never SOURCE what the
    deterministic path rejects as stale/plan (the SAME `_sheet_vintage_flags` signal). `as_of` None →
    always False (guard off, fail-safe). Deterministically testable with a synthetic prov — no AI run."""
    sheet = getattr(prov, 'sheet', None)
    grid = prof.get('grid', {}) if isinstance(prof, dict) else {}
    if as_of is None or not sheet or sheet not in grid:
        return False
    rows = grid[sheet]
    ax = periods.detect_period_axis(rows)
    if not ax.columns:
        return False
    lc = _sheet_label_col(rows, ax.axis_rows[0]) if ax.axis_rows else None
    row = (_find_concept_row(rows, lc, concept, ax.axis_rows[0] + 1, len(rows), ax.columns)
           if lc is not None else None)
    found = {concept: row} if row is not None else {}
    return any(_sheet_vintage_flags(rows, ax, as_of, found))


def _model_emit(concept, verdict, prov, *, build, prof=None, as_of=None):
    """The ONLY door to a MODEL-sourced emit via `_model_fill` (the Step-6 seam). Emits ONLY on a
    passing triangulation verdict (status AUTO); an absent or non-AUTO verdict → NOT emitted (returns
    None, the concept stays held). `build` is invoked lazily, only on a passing verdict, so no
    normalisation is wasted on a held figure. Relocates the model AUTO-gate from its call site
    into one choke — it re-implements NO hold, it moves an existing gate so nothing can bypass it.

    VINTAGE LOCATION-FILTER: a passing verdict whose located SHEET is stale/forward (vs `as_of`) is
    REFUSED here — the model may not source what the deterministic path rejected as stale/plan. Filters
    the RETURNED LOCATION, never the inventory (see `_location_is_stale`). `prof`/`as_of` None → inert,
    so model-OFF and the existing model tests are byte-identical unless a located sheet is actually
    stale. NB this covers the `_model_fill` path only; the whole-file FINDER (`_model_find_across_file`)
    has its own emit path and is NOT yet filtered — see the AI-activation marker at its call site."""
    if verdict is None or getattr(verdict, 'status', None) != AUTO:
        return None
    if _location_is_stale(prof, prov, concept, as_of):
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


def _model_probe_order(prof):
    """The sheet order the model path probes — the deterministic BEST MIS statement sheet FIRST, then
    every other sheet in profile order.

    WHY: the model path re-discovers the statement sheet from scratch, one locate call per statement-shaped
    sheet, in profile order — even though _best_sheet already RANKS the best MIS statement sheet for free.
    On a workbook with many statement-shaped tabs (a monthly report can carry 50+) that spends a model call
    on every tab BEFORE the real statement, then early-breaks once the gaps resolve. Visiting the best sheet
    first collapses that to ~one call for a well-formed file. This is a REORDER, never a RESTRICT: every
    other sheet still follows in its original order, so a gap that lives OFF the best sheet (e.g. a cash
    stock on a balance-sheet tab) is still found by the fallback — coverage is unchanged, only wasted probes
    are removed. Pure function of sheet content (best_sheet is a stable total order; sort is stable), so the
    probe order is deterministic and PYTHONHASHSEED-independent."""
    sheets = list(prof['sheets'])
    best = _best_sheet(prof, MIS_CONCEPTS)
    if best is None:
        return sheets                                 # no deterministic best → probe in profile order (unchanged)
    best_name = best[2]
    return sorted(sheets, key=lambda s: (s.sheet != best_name,))   # stable: best first, rest keep profile order


# ── FINDER inventory (Step 4, §3a) — FAIL-OPEN: keep on doubt, drop only PROVABLE junk ───────────────
# The opposite bias to a fit-to-size filter (which silently dropped CSS's SG-division P&Ls). A
# statement-shaped sheet (period axis + a statement region) is KEPT unless it is PROVABLY not a
# statement source: it carries NONE of the core statement concepts AND has no resolved statement kind
# AND is not income-CONFIRMED. A real financial statement always shows at least one core concept, so
# "carries zero core concepts" is a safe drop. Every drop is RETURNED (logged/disclosed, never silent),
# so a wrongly-dropped sheet is auditable — the same fail-closed-visibility rule as the grid §4 bucket.
_FINDER_PRESENCE_CONCEPTS = ('revenue', 'ebitda', 'cash', 'assets', 'liabilities', 'equity',
                             'net_income', 'gross_profit', 'cogs', 'opex')


def _finder_inventory(prof):
    """(inventory:[Statement], dropped:[(sheet, reason)]) for the one-pass finder. FAIL-OPEN (see above);
    inventory is in workbook order so token-budgeted chunking is deterministic."""
    grid = prof['grid']
    inventory, dropped = [], []
    for s in prof['sheets']:
        if s.sheet not in grid:
            continue
        rows = grid[s.sheet]
        ax = periods.detect_period_axis(rows)
        if not ax.columns:
            continue                                       # no period axis → cannot source a period figure
        lc = _sheet_label_col(rows, ax.axis_rows[0])
        if lc is None:
            continue
        if not ax.is_time_series and not _is_statement_grid(rows, ax, lc, s.sheet):
            continue
        regs = [r for r in statements.segment_regions(s.sheet, rows) if r.kind == statements.STATEMENT]
        if not regs:
            continue
        kind = _statement_kind(rows, s.sheet)
        inc_conf = _family_verdicts(rows, ax, lc).get('income_statement') == family.CONFIRMED
        r0 = ax.axis_rows[0] + 1
        has_concept = any(_find_concept_row(rows, lc, c, r0, len(rows), ax.columns) is not None
                          for c in _FINDER_PRESENCE_CONCEPTS)
        if kind in ('income', 'balance', 'cash_flow') or inc_conf or has_concept:
            inventory.append(statements.Statement(
                s.sheet, min(r.start_row for r in regs), max(r.end_row for r in regs),
                header_row=(ax.axis_rows[0] if ax.axis_rows else None), label_col=lc))
        else:
            dropped.append((s.sheet, 'provable non-statement: no core concept, no statement kind, not income-confirmed'))
    return inventory, dropped


def _model_fill(prof, ident, held, *, entity, domicile, anchor_cr, rate_card,
                fields, source_label, as_of=None, require_bound=False, base_currency=None) -> list:
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
    # ── ABSENT-CONCEPT PRUNE (AI-on latency root cause) ───────────────────────────────────
    # A concept leaves `remaining` ONLY on a successful EMIT (below). So a concept ABSENT from
    # the file keeps `remaining` non-empty and the loop probes EVERY statement-grid sheet for it
    # — one serial locate call each (measured cold: one file made 24/43 calls, 500/665s, finding
    # nothing). Cross-file concurrency cannot break a single file's serial in-file chain, so this
    # futile hunt is the AI-on latency floor. FIX: recognise confident absence from the locator's
    # OWN `missing` signal. A concept's home statement-kinds = the recognised kinds present in the
    # file that `_concept_allowed_on_kind` permits (the SAME guard the emit path uses — generous:
    # cash on income/BS/CFS, revenue/ebitda on income, headcount everywhere). Once a concept has
    # been located-MISSING on the BEST (first-probed) sheet of each present home-kind, it is absent
    # for the locator and pruned — deep whole-file search is the FINDER's job (guarded, and off).
    # CONSCIOUS SPEED-FOR-REACH TRADE: coverage is unchanged where a concept sits on its primary
    # statement (proven byte-identical on the 15-file emit gate — necessary, NOT sufficient for
    # unseen layouts); a concept hiding on a SECONDARY same-kind tab is deferred to the finder.
    # A concept with NO present home-kind is NEVER pruned (full probe, byte-identical) — safe
    # fallback on any file the kind classifier can't read.
    order = list(_model_probe_order(prof))        # best MIS statement sheet first (reorder, not restrict)
    _present_kinds = {kk for s in order if s.sheet in grid
                      for kk in (_statement_kind(grid[s.sheet], s.sheet),) if kk in _KIND_ALLOWED_FAMILIES}
    _home_kinds = {c: {kk for kk in _present_kinds if _concept_allowed_on_kind(c, kk)} for c in held}
    _tried_best = {c: set() for c in held}        # present home-kinds whose BEST sheet was tried w/o a clean emit
    _seen_best_kind = set()                        # recognised kinds whose best (first) sheet is now probed
    for s in order:
        if not remaining:
            break
        rows = grid[s.sheet]
        _kind = _statement_kind(rows, s.sheet)
        ax = periods.detect_period_axis(rows)
        if not ax.columns:
            continue                                  # no period axis at all → CF1 can't run
        label_col = _sheet_label_col(rows, ax.axis_rows[0])
        if label_col is None:
            continue
        # TIGHT kind-gate (Part B, foundational for the model phase): a comparison-GRID is probed
        # ONLY if it is a statement grid (self-declared kind or income-CONFIRMED) — the same gate the
        # deterministic grid pass uses. A series sheet is unaffected (is_time_series short-circuits),
        # so model-OFF is byte-identical and a non-statement junk grid is never handed to the model.
        if not ax.is_time_series and not _is_statement_grid(rows, ax, label_col, s.sheet):
            continue
        cols = _actual_columns(rows, ax)              # scenario-filtered (Actual only)
        num_cols = [pc.col for pc in cols]
        regs = [r for r in statements.segment_regions(s.sheet, rows) if r.kind == statements.STATEMENT]
        if not regs:
            continue
        # ONE statement window per sheet — a single-company statement's SECTION
        # sub-headers ("Cost of Goods Sold", "Digital marketing") must not fragment
        # the identity chain (revenue−cogs=gross_profit). Scope stays THIS sheet's
        # statement: identity is evaluated within the window, never across the
        # workbook, so it composes with the Σ-divisions statement-selection net
        # (which picks the sheet for a consolidation before the model ever locates).
        stmt = statements.Statement(
            s.sheet, min(r.start_row for r in regs), max(r.end_row for r in regs),
            header_row=(ax.axis_rows[0] if ax.axis_rows else None), label_col=label_col)
        # LAYOUT-TEMPLATE cache (U3, cache A): a KNOWN layout reuses the model's proven row locations
        # and SKIPS the locate call — model cost then scales with distinct LAYOUTS, not file count. A hit
        # serves LOCATIONS ONLY; the collapse+triangulate+emit below still re-reads THIS file's cells and
        # re-runs all three signals, so a stale/wrong/cross-tenant location can never emit a wrong number —
        # it fails re-verify and holds (why this cache needs no org-scoping the golden store needs). On a
        # MISS the model locates as before, gated by the per-run call budget (a budget hold is a model
        # concern — a cache hit never touches it).
        _stored = templates.get_statement_rows(context, stmt)
        _template_hit = _stored is not None
        if _template_hit:
            # Rebuild the FULL cached record set (targets AND the identity intermediates) — the emit loop
            # below still only emits `remaining`, exactly like the miss path, but triangulation needs the
            # whole chain to re-verify. Filtering to `remaining` here would starve identity → SOFT → no emit.
            out = {'records': [locator.RowRecord(concept=d['concept'], form=d['form'], row=d['row'],
                                                 operand_rows=list(d['operand_rows'] or []),
                                                 row_label=d['row_label'])
                               for d in _stored],
                   'missing': [], 'error': None}
        else:
            m = llm.current_metrics()
            if m is not None and m.calls >= _CALL_BUDGET:
                for c in sorted(remaining):           # disclosed, reproducible budget hold
                    fields[c] = Figure(c, None, None, fields[c].provenance, held=True,
                                       hold_reason=_BUDGET_HOLD)
                return diagnostics
            out = locator.locate_rows(stmt, grid, sorted(remaining), content_fp=ident.content_fp)
        missing_here = set(out.get('missing', ()))
        for c in missing_here:                    # locator disclosed a silent drop — record it
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
            col = _collapse_row(rows, cols, concept, rr, as_of=as_of, require_bound=require_bound)
            if col is not None:
                collapses[concept] = col
        money_samples = [collapses[c].value for c in collapses
                         if concept_measure(c) == 'money'
                         and collapses[c].value is not None and not collapses[c].escalate]
        local_ccy, local_unit = _region_ccy_unit(rows, stmt)
        frame = units.resolve_monetary_frame(
            stmt_currency=local_ccy, geo_currency=geo_ccy, inr_mentioned=inr_mentioned,
            declared_unit=local_unit, sample_values=money_samples,
            anchor_cr=anchor_cr, ratecard=rate_card, base_currency=base_currency)
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
                concept, v, prov, prof=prof, as_of=as_of,   # vintage location-filter at the choke
                build=lambda concept=concept, col=col, prov=prov, ebitda_row=ebitda_row:
                    _emit_from_collapsed(concept, col, frame, prov, rate_card, anchor_cr,
                                         rows=rows, label_col=label_col, num_cols=num_cols,
                                         ebitda_row=ebitda_row))
            if emit is None:
                continue
            fields[concept] = emit
            if not (emit.held or emit.gap):
                remaining.discard(concept)
        # absent-concept prune (see loop head): record which concepts this sheet — if it is the
        # BEST (first) sheet of its recognised kind — located as MISSING, then prune any concept
        # whose every present home-kind's best sheet has now missed it (deep hunt → finder's job).
        # Anything still in `remaining` here was NOT cleanly emitted on this sheet (missing OR
        # located-but-held) — re-probing more sheets for it is the futile hunt. On the BEST (first)
        # sheet of a recognised kind, mark that kind tried for every such concept it is a home of.
        if _kind in _KIND_ALLOWED_FAMILIES and _kind not in _seen_best_kind:
            _seen_best_kind.add(_kind)
            for c in sorted(remaining):
                if _kind in _home_kinds.get(c, ()):
                    _tried_best[c].add(_kind)
        for c in sorted(remaining):               # prune once EVERY present home-kind's best sheet was tried
            hk = _home_kinds.get(c)
            if hk and _tried_best[c] >= hk:
                remaining.discard(c)
                recall_missing.setdefault(c, s.sheet)
        # U3: freeze the FULL located set this layout produced (exactly what locate_rows returned) — the emit
        # TARGETS *and* every identity intermediate (cogs/gross_profit/opex …), whatever their signals. The
        # registry is a TRANSPARENT SUBSTITUTE for the locate call: a hit replays this same set through the
        # SAME collapse+triangulate+emit path, so a hit emits the IDENTICAL CIR a miss would (hit==miss), incl.
        # a target that only verifies via an imperfect intermediate. Safety is the RE-VERIFY, not a put filter:
        # every location is re-read + re-triangulated on the hit, so a stale/wrong/imperfect one can never emit
        # a wrong number — it fails re-verify and holds (test_cached_wrong_row_still_fails_closed, D2). Only on
        # a fresh locate (a hit is already stored; rewriting churns). Sorted by concept for a deterministic store.
        if not _template_hit and out.get('records'):
            templates.put_statement_rows(context, stmt, sorted(out['records'], key=lambda r: r.concept))
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


# ── WHOLE-FILE FINDER emit path (Step 4 emit-wiring) ─────────────────────────────────────────────
# The per-statement _model_fill re-discovers ONE statement at a time and early-breaks once the gaps
# resolve, so it never sees the SAME concept at other scopes. The finder path instead LOCATES every
# occurrence across the whole file (locate_across_file_chunked, targets-only), code-reads each value
# with the identical collapse+frame machinery (never the model's number — locator-only), re-enters the
# per-statement identity anchors via locate_rows BOUNDED to each statement (triangulation), and hands
# the many scope-tagged ₹Cr readings for a concept to reconcile_locations, which resolves them to ONE
# consolidated figure or HOLDS. Every emit is fail-closed: a value ships only if it is triangulated on
# its own statement AND survives cross-scope reconciliation; anything else holds with a disclosed reason.
# Additive + flag-gated — model-off and the per-statement path are untouched (byte-identical).
def _region_id(sheet, reg) -> str:
    """A statement region's STRUCTURAL scope identity — sheet + region start row, NEVER a parsed name.
    A stacked multi-division sheet yields one id per block (N scopes); a plain statement yields one.
    Scope is used only to COUNT distinct scopes and group the Σ-identity test — it never ADMITS a value
    (only the Σ-consolidated identity does), so embedding the sheet here does not make names load-bearing."""
    return f'{sheet}#{reg.start_row}'


def _rr_in_region(rr, reg) -> bool:
    r = rr.row if rr.row is not None else (rr.operand_rows[0] if rr.operand_rows else None)
    return r is not None and reg.start_row <= r <= reg.end_row


def _period_key(col, acts):
    """The reporting period-key (a PeriodColumn.order tuple) the collapse landed on — so the SAME
    concept is aligned across sheets by DATE, not column position (a balance-sheet 'current' column and
    a cash-flow 'prior' column that hold the same figure must not reconcile as one period). None when
    unresolvable → the caller keeps it out of period bucketing but still lets it corroborate."""
    if col is None or not getattr(col, 'source_cols', None):
        return None
    by_col = {pc.col: pc for pc in acts}
    orders = [by_col[c].order for c in col.source_cols if c in by_col]
    return max(orders) if orders else None


def _collapsed_to_cr(concept, col, frame, rate_card):
    """₹Cr for one collapsed reading through a resolved frame — the SAME normalisation tail as
    _emit_from_collapsed / _resource_value_cr, so a finder reading and its final emit are one number.
    A count (headcount) passes through unscaled. None when the value can't be resolved cleanly."""
    if col is None or col.escalate or col.value is None:
        return None
    if concept_measure(concept) != 'money':
        return col.value
    if frame.escalate or frame.scale is None:
        return None
    try:
        q = Quantity(amount=col.value, currency=frame.currency, scale=frame.scale,
                     nature=concept_nature(concept), concept=concept)
        return (rate_card.to_inr(q.absolute_native(), frame.currency) / _CR).quantize(_Q, rounding=ROUND_HALF_UP)
    except Exception:  # noqa: BLE001
        return None


def _model_find_across_file(prof, ident, held, *, entity, domicile, anchor_cr, rate_card,
                            fields, source_label, as_of=None, require_bound=False,
                            base_currency=None, boundary_verified=True) -> list:
    """Whole-file finder emit path. Mutates `fields` in place; returns per-concept diagnostics
    (disposition, sources, scope, period, emitted/held). See the block comment above."""
    grid = prof['grid']
    geo_ccy = units.expected_currency(domicile)
    inr_mentioned = _workbook_mentions_inr(prof)
    context = ident.layout_fp
    first_seen = _first_seen(context)
    targets = sorted(c for c in set(held) if c in MIS_CONCEPTS)
    diagnostics = []
    if not targets:
        return diagnostics
    inventory, _dropped = _finder_inventory(prof)
    if not inventory:
        return diagnostics
    m = llm.current_metrics()
    if m is not None and m.calls >= _CALL_BUDGET:
        for c in targets:
            fields[c] = Figure(c, None, None, fields[c].provenance, held=True, hold_reason=_BUDGET_HOLD)
        return diagnostics
    finder = locator.locate_across_file_chunked(inventory, grid, targets, content_fp=ident.content_fp)
    by_sheet = {}
    for sheet, rr in finder.get('records', []):
        by_sheet.setdefault(sheet, []).append(rr)
    candidates = {c: [] for c in targets}
    for sheet in sorted(by_sheet):
        rows = grid.get(sheet)
        if not rows:
            continue
        ax = periods.detect_period_axis(rows)
        if not ax.columns:
            continue
        label_col = _sheet_label_col(rows, ax.axis_rows[0])
        if label_col is None:
            continue
        acts = _actual_columns(rows, ax)
        num_cols = [pc.col for pc in acts]
        ref_col = max(acts, key=lambda c: c.order).col if acts else None
        regs = [r for r in statements.segment_regions(sheet, rows) if r.kind == statements.STATEMENT]
        if not regs:
            continue
        # G2 (non-statement noise): reconcile admits GENUINE statement sheets ONLY — a sheet with a
        # resolved statement kind, or income-CONFIRMED. This is STRICTER than the fail-open finder
        # inventory (which keeps on doubt so nothing is missed): a working/aux tab the inventory kept
        # (a bonus-provision guideline, a 'Main' check cell that reads 'Chk Total Sales' = value + the
        # word 'sales', a ratio/margin scratch sheet) carries no statement identity and must never feed
        # a company figure. Keyed on the accounting property (kind/identity), never a sheet name.
        _kind = _statement_kind(rows, sheet)
        _inc_conf = _family_verdicts(rows, ax, label_col).get('income_statement') == family.CONFIRMED
        if _kind not in ('income', 'balance', 'cash_flow') and not _inc_conf:
            diagnostics.append({'sheet': sheet, 'rejected': 'non-statement-sheet',
                                'concepts': sorted({rr.concept for rr in by_sheet[sheet]})})
            continue
        sheet_rrs = by_sheet[sheet]
        multi_region = len(regs) > 1                # a stacked multi-division sheet: divisions are
        for reg in regs:                            # Σ-EVIDENCE (value-read only), never emitted, so
            in_reg = [rr for rr in sheet_rrs if _rr_in_region(rr, reg)]   # they need no identity chain
            if not in_reg:
                continue
            stmt = statements.Statement(sheet, reg.start_row, reg.end_row,
                                        header_row=(ax.axis_rows[0] if ax.axis_rows else None),
                                        label_col=label_col)
            recs = {}
            verdicts = {}
            if not multi_region:                    # G1: per-statement anchors re-enter, bounded here
                out = locator.locate_rows(stmt, grid, targets, content_fp=ident.content_fp)
                if not out.get('error'):
                    for rr in out.get('records', []):
                        recs[rr.concept] = rr        # locate_rows is AUTHORITATIVE for the identity chain — a
            for rr in in_reg:                        # second same-concept finder row (Total Other Income under
                recs.setdefault(rr.concept, rr)      # revenue) must not become the triangulated row; it fills a gap only
            collapses0 = {}
            for c0, rr0 in recs.items():
                col0 = _collapse_row(rows, acts, c0, rr0, as_of=as_of, require_bound=require_bound)
                if col0 is not None:
                    collapses0[c0] = col0
            money_samples = [collapses0[c0].value for c0 in collapses0
                             if concept_measure(c0) == 'money' and collapses0[c0].value is not None
                             and not collapses0[c0].escalate]
            local_ccy, local_unit = _region_ccy_unit(rows, stmt)
            frame = units.resolve_monetary_frame(stmt_currency=local_ccy, geo_currency=geo_ccy,
                        inr_mentioned=inr_mentioned, declared_unit=local_unit,
                        sample_values=money_samples, anchor_cr=anchor_cr, ratecard=rate_card,
                        base_currency=base_currency)
            if not multi_region:
                figs, recs_tri = _tri_inputs(recs, frame, sheet, label_col, rows, ref_col)
                tri = triangulate.triangulate(stmt, recs_tri, figs, grid,
                                              context=context, first_seen=first_seen)
                verdicts = tri['verdicts']
            for rr in in_reg:                        # EACH finder location — a concept can appear >1× per region
                concept = rr.concept
                if concept not in targets:
                    continue
                row = rr.row if rr.row is not None else (rr.operand_rows[0] if rr.operand_rows else None)
                if row is None:
                    continue
                if not boundary_verified and concept_measure(concept) == 'money' and concept_nature(concept) == 'stock':
                    # Guard 4 (period-binding): a money STOCK is a point-in-time reading, so it is only trustworthy
                    # at a VERIFIED reporting date. When the date is absent (require_bound) OR merely FLOW-DERIVED
                    # (latest month carrying a flow value — not projection-immune; CSS's Dec-column cash on a May
                    # file), the stock's latest column may be a projected balance → fail-close (hold). A FLOW may
                    # still flow-derive-emit (its sum is actuals-to-date, disclosed); a stock balance may not.
                    # Structural (nature=stock, measure=money), name-free; finder-only (deterministic unchanged).
                    diagnostics.append({'concept': concept, 'sheet': sheet, 'scope': _region_id(sheet, reg),
                                        'rejected': 'money-stock-on-unverified-reporting-boundary'})
                    continue
                if concept == 'revenue':                        # G2: operating-revenue disposition
                    disp, alt = _operating_revenue_disposition(rows, label_col, row)
                    if disp == 'hold':
                        diagnostics.append({'concept': concept, 'sheet': sheet,
                                            'scope': _region_id(sheet, reg), 'rejected': 'operating-revenue-hold'})
                        continue
                    if disp == 'relocate' and alt is not None:
                        row = alt
                        rr = locator.RowRecord(concept, 'direct', alt, [],
                                               str(rows[alt][label_col]).strip()
                                               if alt < len(rows) and label_col < len(rows[alt]) else '')
                if concept == 'revenue' and _OTHER_INCOME_RE.search(rr.row_label or ''):   # G2: not revenue
                    diagnostics.append({'concept': concept, 'sheet': sheet, 'scope': _region_id(sheet, reg),
                                        'rejected': 'other-income-not-revenue', 'row_label': rr.row_label})
                    continue
                if concept == 'ebitda' and _ebitda_label_class(rr.row_label or '') == 'not_ebitda':
                    diagnostics.append({'concept': concept, 'sheet': sheet, 'scope': _region_id(sheet, reg),
                                        'rejected': 'not-ebitda-metric-class', 'row_label': rr.row_label})
                    continue                                    # metric-class: PBT/EBIT ≠ EBITDA — never pool it
                col = _collapse_row(rows, acts, concept, rr, as_of=as_of, require_bound=require_bound)
                value_cr = _collapsed_to_cr(concept, col, frame, rate_card)
                if value_cr is None:
                    continue
                prov = Provenance(source_file=source_label, content_fingerprint=ident.content_fp,
                                  sheet=sheet, cell=_a1(label_col, row), row_label=(rr.row_label or '')[:60])
                if col is not None:
                    _cite_value_cells(prov, col, row, {pc.col: pc for pc in ax.columns})
                # (iii) deterministic-locator-match: does CODE's own fuzzy locator find this concept at the
                # SAME row the finder returned? If so the finder never under-performs the deterministic path,
                # so its row is trusted on the SAME basis the grid-flip already emits on.
                code_row = _find_concept_row(rows, label_col, concept, ax.axis_rows[0] + 1, len(rows), ax.columns)
                candidates[concept].append({
                    'sheet': sheet, 'cell': _a1(label_col, row), 'value_cr': value_cr,
                    'scope': _region_id(sheet, reg), 'kind': _kind,
                    'period_key': _period_key(col, acts), 'code_agree': (code_row == row),
                    'verdict': verdicts.get(concept),
                    'ctx': {'col': col, 'frame': frame, 'prov': prov, 'rows': rows, 'ax': ax,
                            'label_col': label_col, 'stmt_kind': _kind,
                            'ebitda_row': (row if concept == 'ebitda' else None)}})
    for concept in targets:
        cand = candidates[concept]
        if not cand:
            continue
        keyed = [c for c in cand if c['period_key'] is not None]
        pool = cand
        if keyed:                                    # G4: reconcile only the LATEST period bucket (+ undated)
            latest = max(c['period_key'] for c in keyed)
            pool = [c for c in cand if c['period_key'] in (latest, None)]
        rl = reconcile.reconcile_locations(ident.content_fp, concept,
                [{'sheet': c['sheet'], 'cell': c['cell'], 'value_cr': c['value_cr'],
                  'scope': c['scope'], 'kind': c['kind']} for c in pool], anchor_cr=anchor_cr)
        base = {'concept': concept, 'disposition': rl.get('disposition'), 'n': len(pool),
                'value_cr': (str(rl['value_cr']) if rl.get('value_cr') is not None else None),
                'sources': rl.get('sources')}
        _held_prov = lambda: (fields[concept].provenance if isinstance(fields.get(concept), Figure)
                              else pool[0]['ctx']['prov'])
        if rl.get('value_cr') is None:               # G3: no consolidated resolution → HOLD
            fields[concept] = Figure(concept, None, None, _held_prov(), held=True,
                hold_reason=(f'finder {rl.get("disposition")}: {rl.get("detail", "")}')[:90])
            diagnostics.append({**base, 'emitted': False})
            continue
        winners = sorted((c for c in pool if reconcile.scale_aware_agree(c['value_cr'], rl['value_cr'])),
                         key=lambda c: (c['sheet'], c['cell']))
        win = winners[0] if winners else None
        if win is None:                              # reconciled a value but no candidate carries it — fail-closed
            fields[concept] = Figure(concept, None, None, _held_prov(), held=True,
                hold_reason=(f'finder {rl.get("disposition")}: reconciled value has no source candidate — held')[:90])
            diagnostics.append({**base, 'emitted': False, 'reason': 'no-source-candidate'})
            continue
        # CORROBORATION — the emit needs ONE of three legitimate never-a-wrong-number bases; none → HOLD.
        # This UNIFIES the finder onto the deterministic emit discipline (no triangulation-ONLY gate):
        #   (i) identity-chain (triangulation AUTO) · (ii) cross-sheet reconcile (agree / multiscope-Σ) ·
        #   (iii) the located row IS what the deterministic code locator finds (finder never under-performs code).
        _v = win['verdict']
        if _v is not None and getattr(_v, 'status', None) == AUTO:
            corrob = 'identity-chain'
        elif rl.get('disposition') in ('agree', 'multiscope'):
            corrob = 'cross-sheet-reconcile'
        elif win.get('code_agree'):
            corrob = 'deterministic-locator-match'
        else:
            corrob = None
        if corrob is None:                           # fail-closed: a lone, unverifiable model row never emits
            fields[concept] = Figure(concept, None, None, win['ctx']['prov'], held=True,
                hold_reason=('finder %s uncorroborated (no identity/cross-sheet/locator basis) — held'
                             % rl.get('disposition'))[:90])
            diagnostics.append({**base, 'emitted': False, 'reason': 'uncorroborated'})
            continue
        ctx = win['ctx']
        # SHARED emit discipline (the grid-flip path): statement-kind appropriateness + stock/flow grain +
        # EBITDA metric-class + monetary frame + zero-stock + anchor sanity. A guard here can still HOLD
        # (PBT proxy, frame/currency unresolved, projection-ambiguous period) — that hold is PRESERVED.
        emit = _emit_from_collapse(concept, ctx['col'], ctx['prov'], stmt_kind=ctx['stmt_kind'],
                    frame=ctx['frame'], rows=ctx['rows'], ax=ctx['ax'], label_col=ctx['label_col'],
                    ebitda_row=ctx['ebitda_row'], anchor_cr=anchor_cr, rate_card=rate_card)
        fields[concept] = emit
        diagnostics.append({**base, 'emitted': not (emit.held or emit.gap), 'corroboration': corrob,
                            'sheet': win['sheet'], 'cell': win['cell'],
                            'scope': win['scope'], 'period_key': str(win['period_key'])})
    _finalize_terminal_state(fields)
    return diagnostics
