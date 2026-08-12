"""Phase D — deterministic fund-financials extractor (MVP slice: capital-account flows).

The fund-level files (capital calls/distributions, fund accounts, terms, LP register)
are SINGLE-FUND, small, standard-labelled — a LOCATION problem, not the MIS
disambiguation problem the deterministic resolvers already solve. This first slice
extracts the capital-account FLOWS (`called`, `distributed`) as fund-level totals,
each PROVEN by the Σ(events)=stated-total identity (the fund analog of Σ-divisions)
and fail-closed on any mismatch, an unresolved unit, or an ambiguous source.

Scope guards (fail-closed, universal — no per-file layout):
  • only the capital-account EVENT sheets are mined — the LP register (per-LP rows)
    is the NEXT slice, and a budget/forecast/variance sheet is NEVER mined for actuals
    (the Budget-vs-Actual trap);
  • the value is the STATED TOTAL only when Σ(event rows) reconciles to it — a total
    row is excluded from the event sum (the junk/total-row discipline), so the amount
    column is never double-counted;
  • scale+currency are RESOLVED (via the shared monetary-frame resolver), never assumed.

Per-event register detail, fund terms, NAV and waterfall are later slices; the
multi-entity group files (CSS/CPM) are the final, hardest slice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Tuple

from . import lexicon, reconcile, units
from .cir import Figure, Provenance, Record
from .contract import TOLERANCES
from .quantity import SCALE_TO_ABS, to_decimal

_HARD_EPS = Decimal(str(TOLERANCES['hard_abs_cr']))   # rounding-aware ₹Cr epsilon, defined once

# capital-account flow concepts this slice extracts (both already natured 'flow')
FUND_FLOW_CONCEPTS = ['called', 'distributed']

# amount-column header synonyms per concept, in PRIORITY order. 'net' beats 'gross'
# for distributions — `distributed` is what LPs received, net of GP carry.
_AMOUNT_HEADERS = {
    'called': ['amount', 'call amount', 'called', 'drawdown'],
    'distributed': ['net', 'net amount', 'net distribution', 'amount', 'gross'],
}
# a sheet is a concept's source ONLY if it is that concept's EVENT account (not the
# LP register, not a forecast). Substring markers on the normalised sheet text.
_SOURCE_CONTEXT = {
    'called': ['capital call', 'drawdown', 'call no'],
    'distributed': ['distribution', 'distributions'],
}
_LP_REGISTER_MARKERS = ('lp name', 'lp list', 'investor name', 'investor list', 'lp register')
_FORECAST_MARKERS = ('budget', 'forecast', 'projection', 'projected', 'variance', 'vs act', 'plan')

_TOTAL_TOL = Decimal('0.01')     # Σ-events vs stated total: 1% fractional tolerance


# ── small grid helpers ────────────────────────────────────────────────────
def _a1(col: Optional[int], row: Optional[int]) -> str:
    if col is None or row is None:
        return ''
    from openpyxl.utils import get_column_letter
    return f'{get_column_letter(col + 1)}{row + 1}'


def _norm_cells(rows) -> str:
    """The sheet's whole text, normalised — for substring context/marker scans."""
    out = []
    for row in rows:
        for v in row:
            if isinstance(v, str):
                out.append(lexicon.normalise_label(v))
    return ' \x1f '.join(out)


def _has_word(text_low: str, token: str) -> bool:
    return re.search(rf'(?<![a-z]){re.escape(token)}(?![a-z])', text_low) is not None


def _is_forecast(text_norm: str) -> bool:
    return any(m in text_norm for m in _FORECAST_MARKERS)


def _is_lp_register(text_norm: str) -> bool:
    return any(m in text_norm for m in _LP_REGISTER_MARKERS)


def _is_source(text_norm: str, concept: str) -> bool:
    return any(m in text_norm for m in _SOURCE_CONTEXT[concept])


# ── monetary frame (scale+currency) for a fund sheet ──────────────────────
def _declared_unit_currency(rows) -> Tuple[Optional[str], Optional[str]]:
    """Scan the top of the sheet for a declared scale unit + currency. Fail-closed:
    a None unit makes the caller HOLD (never assume crore)."""
    unit = ccy = None
    for row in rows[:8]:
        for v in row:
            if not isinstance(v, str):
                continue
            low = v.lower()
            if unit is None:
                for tok in ('crore', 'crores', 'cr', 'lakh', 'lakhs', 'million',
                            'millions', 'mn', 'thousand', 'thousands'):
                    if _has_word(low, tok):
                        unit = tok
                        break
            if ccy is None:
                if _has_word(low, 'rs') or _has_word(low, 'inr') or _has_word(low, 'rupees') or '₹' in v:
                    ccy = 'INR'
                elif '$' in v or _has_word(low, 'usd'):
                    ccy = 'USD'
    return unit, ccy


def _to_cr(value_native: Decimal, frame, rate_card) -> Decimal:
    inr = rate_card.to_inr(value_native * Decimal(SCALE_TO_ABS[frame.scale]), frame.currency)
    return Decimal(str(inr)) / Decimal('10000000')


# ── the Σ-identity total extractor ─────────────────────────────────────────
def _extract_total(rows, concept) -> Tuple[Optional[Decimal], dict]:
    """Find the amount column, sum the EVENT rows, read the STATED TOTAL row, and
    return (native_value, info) ONLY if Σ(events) reconciles to the total. Any
    missing column / no events / >1 total / Σ-mismatch → (None, {reason}) → HOLD."""
    amount_col = header_row = None
    for syn in _AMOUNT_HEADERS[concept]:            # priority order: first synonym found wins
        target = lexicon.normalise_label(syn)
        for ri, row in enumerate(rows):
            for ci, v in enumerate(row):
                if isinstance(v, str) and lexicon.normalise_label(v) == target:
                    amount_col, header_row = ci, ri
                    break
            if amount_col is not None:
                break
        if amount_col is not None:
            break
    if amount_col is None:
        return None, {'reason': f'{concept}: no amount-column header found'}

    events: List[Tuple[int, Decimal]] = []
    totals: List[Tuple[int, Decimal]] = []
    for ri in range(header_row + 1, len(rows)):
        row = rows[ri]
        amt = to_decimal(row[amount_col]) if amount_col < len(row) else None
        if amt is None:
            continue                                # non-numeric row (note / blank) — skip
        is_total = any(isinstance(v, str) and 'total' in lexicon.normalise_label(v).split()
                       for v in row)
        (totals if is_total else events).append((ri, amt))

    if not events:
        return None, {'reason': f'{concept}: no event rows under the amount column'}
    sum_events = sum((a for _, a in events), Decimal('0'))
    if len(totals) > 1:
        return None, {'reason': f'{concept}: {len(totals)} total rows — ambiguous, held'}
    if totals:
        trow, stated = totals[0]
        if stated == 0 or abs(sum_events - stated) / abs(stated) > _TOTAL_TOL:
            return None, {'reason': f'{concept}: Σevents {sum_events} != total {stated} — held'}
        return stated, {'amount_col': amount_col, 'row': trow, 'n_events': len(events),
                        'sum_events': str(sum_events), 'confirmed_by': 'sum==total'}
    # no explicit total row → the reconciled event sum (softer, disclosed)
    return sum_events, {'amount_col': amount_col, 'row': events[-1][0], 'n_events': len(events),
                        'sum_events': str(sum_events), 'confirmed_by': 'sum_only_no_total'}


def _extract_concept(concept, prof, rate_card, *, source_label, content_fp) -> Optional[Figure]:
    grid = prof['grid']
    candidates = []
    for s in prof['sheets']:
        text = _norm_cells(grid[s.sheet])
        if _is_forecast(text) or _is_lp_register(text):
            continue                                # never actuals from forecast / LP register
        if _is_source(text, concept):
            candidates.append((s.sheet, grid[s.sheet]))
    if not candidates:
        return None                                 # concept not in this file — caller notes a gap
    if len(candidates) > 1:                          # >1 event account claims it → disambiguation HOLD
        prov = Provenance(source_file=source_label, content_fingerprint=content_fp,
                          sheet=','.join(c[0] for c in candidates), cell='')
        return Figure(concept, None, None, prov, held=True,
                      hold_reason=f'{concept}: {len(candidates)} candidate sheets — ambiguous, held')

    sheet, rows = candidates[0]
    value_native, info = _extract_total(rows, concept)
    prov = Provenance(source_file=source_label, content_fingerprint=content_fp, sheet=sheet,
                      cell=_a1(info.get('amount_col'), info.get('row')),
                      row_label=info.get('confirmed_by', ''))
    if value_native is None:
        return Figure(concept, None, None, prov, held=True, hold_reason=info['reason'][:90])

    unit, ccy = _declared_unit_currency(rows)
    frame = units.resolve_monetary_frame(
        stmt_currency=ccy, geo_currency=None, inr_mentioned=(ccy == 'INR'),
        declared_unit=unit, sample_values=[value_native], anchor_cr=None, ratecard=rate_card)
    if frame.escalate or not frame.scale:
        return Figure(concept, None, None, prov, held=True,
                      hold_reason=f'{concept}: monetary frame unresolved — {frame.reason}'[:90])
    # `called`/`distributed` are CUMULATIVE-to-date flows — never annualised (basis
    # carries that so a future fund aggregation cannot treat them as a period run-rate).
    return Figure(concept, _to_cr(value_native, frame, rate_card), None, prov, basis='cumulative')


def extract_fund_financials(label, path, prof, *, rate_card, content_fp='') -> Optional[Record]:
    """Extract the capital-account flow concepts from ONE fund-financials file.
    Returns a Record (domain 'fund_financials') when ≥1 concept is found/held, else
    None (this file carries no flows-slice concepts — later slices cover terms/NAV/LP)."""
    fields = {}
    for concept in FUND_FLOW_CONCEPTS:              # deterministic order
        fig = _extract_concept(concept, prof, rate_card, source_label=label, content_fp=content_fp)
        if fig is not None:
            fields[concept] = fig
    if not fields:
        return None
    # single-fund MVP: entity_id is a placeholder until fund-name resolution (a later
    # slice reading Fund_Terms); the capital account belongs to the one fund.
    return Record('fund_financials', entity_id='fund', fields={'fund': 'fund', **fields})


# ══════════════════════════════════════════════════════════════════════════
# LP register — the per-ROW extraction shape (this slice's genuinely-new piece).
# ══════════════════════════════════════════════════════════════════════════
# The LP register lists each investor's commitment and cumulative called /
# distributed. Three things make it new vs the single-fund capital account:
#   • a VARIABLE number of per-LP ROWS (iterate to end; total-row excluded);
#   • an LP-NAME concept that is a STRING — cited & confirmed (name present, not
#     a total, not a note), never summed into a value;
#   • a TOTAL row that, on the real file, is COLUMN-SHIFTED one left of the header
#     (its commitment total lands under `type`, called under `Commitment`, …).
#     So columns are bound from the HEADER row; the total row is used ONLY to
#     exclude that row and as an ALIGNMENT-ROBUST multiset confirmation of the
#     per-column sums — never for column identity (reading it by header position
#     would pull the called total as the commitment total: a plausible wrong number).
#
# GP/sponsor commitment sits WHERE the recon says it sits: on the real file the GP
# is a PARTICIPANT ROW (type 'GP Commitment'), so Σ(all rows) = corpus with NO
# separate additive term — adding one would double-count to 1025 ≠ 1000. Extraction
# stays layout-agnostic: it sums every participant row and lets reconcile assert
# Σ == corpus, which is true whether or not one of the rows happens to be the GP.
_LP_NAME_HEADERS = ('lp name', 'investor name', 'investor', 'partner name',
                    'participant', 'name', 'lp')
_LP_TYPE_HEADERS = ('type', 'investor type', 'lp type', 'category', 'class')
_LP_COMMIT_HEADERS = ('commitment', 'committed', 'commitment amount', 'committed capital')
# per-LP money concepts, header synonyms (priority order), basis. commitment is a
# standing STOCK (point-in-time); called/distributed are CUMULATIVE-to-date flows.
_LP_MONEY = (
    ('commitment', _LP_COMMIT_HEADERS, 'point_in_time'),
    ('called', ('called', 'capital called', 'drawn', 'drawdown', 'paid in', 'contributed'), 'cumulative'),
    ('distributed', ('distributed', 'distributions', 'distribution', 'returned'), 'cumulative'),
)
_CORPUS_MARKERS = ('target corpus', 'total commitment', 'total commitments',
                   'fund corpus', 'aggregate commitment')


@dataclass
class LPRegister:
    """A located per-LP register: one Record per participant, plus the run inputs
    reconcile needs — the stated fund corpus and the total-row numbers, both already
    in ₹Cr under the register's resolved frame. `held` when extraction itself had to
    escalate (ambiguous sheet / unresolved frame)."""
    source: str
    records: List[Record]
    corpus_cr: Optional[Decimal] = None
    total_numbers_cr: List[Decimal] = field(default_factory=list)
    held: bool = False


def _first_col(norm, headers) -> Optional[int]:
    """Column index of the first header (priority order) whose normalised label
    EXACTLY matches a cell on the header row. Exact — not substring — so a short
    token like 'name' never rides 'fund name'."""
    for h in headers:
        for ci, lab in norm:
            if lab == h:
                return ci
    return None


def _lp_header(rows) -> Tuple[Optional[int], Optional[list]]:
    """The register header = the first row carrying BOTH a name column and a
    commitment column. Returns (row_index, [(ci, norm_label)]) or (None, None)."""
    for ri, row in enumerate(rows):
        norm = [(ci, lexicon.normalise_label(v)) for ci, v in enumerate(row) if isinstance(v, str)]
        if _first_col(norm, _LP_NAME_HEADERS) is not None and _first_col(norm, _LP_COMMIT_HEADERS) is not None:
            return ri, norm
    return None, None


def _find_lp_sheets(prof) -> list:
    """Every non-forecast sheet that carries an LP-register header. >1 ⇒ ambiguous
    (the caller holds); a forecast/budget sheet is never an LP register."""
    hits = []
    for s in prof['sheets']:
        rows = prof['grid'][s.sheet]
        if _is_forecast(_norm_cells(rows)):
            continue
        ri, norm = _lp_header(rows)
        if ri is not None:
            hits.append((s.sheet, rows, ri, norm))
    return hits


def _fund_corpus(prof) -> Optional[Decimal]:
    """The stated fund corpus / total commitment (native) — the RHS of the
    commitment identity. First label matching a corpus marker, value to its right."""
    for s in prof['sheets']:
        for row in prof['grid'][s.sheet]:
            for ci, v in enumerate(row):
                if isinstance(v, str) and any(m in lexicon.normalise_label(v) for m in _CORPUS_MARKERS):
                    for v2 in row[ci + 1:]:
                        d = to_decimal(v2)
                        if d is not None:
                            return d
    return None


def extract_lp_register(label, path, prof, *, rate_card, content_fp='') -> Optional[LPRegister]:
    """Extract per-LP participant rows from ONE fund file, or None if it has no LP
    register. Extraction LOCATES (rows, values, frame, corpus, total row); it does
    NOT verify — reconcile_lp_register asserts the identities. Fail-closed at the
    extraction layer: an ambiguous (>1) register sheet, or an unresolved monetary
    frame, HOLDS every figure (value kept for the reviewer, excluded from sums)."""
    hits = _find_lp_sheets(prof)
    if not hits:
        return None
    if len(hits) > 1:
        prov = Provenance(source_file=label, content_fingerprint=content_fp,
                          sheet=','.join(h[0] for h in hits), cell='')
        rec = Record('lp_register', entity_id='(ambiguous)', fields={
            'lp_name': '(ambiguous)',
            'commitment': Figure('commitment', None, None, prov, held=True,
                                 hold_reason=f'{len(hits)} LP-register sheets — ambiguous, held')})
        return LPRegister(label, [rec], held=True)

    sheet, rows, hdr, norm = hits[0]
    name_c = _first_col(norm, _LP_NAME_HEADERS)
    type_c = _first_col(norm, _LP_TYPE_HEADERS)
    money_cols = {c: _first_col(norm, hs) for c, hs, _ in _LP_MONEY}

    # resolve the monetary frame ONCE for the sheet (fail-closed: unresolved → held)
    unit, ccy = _declared_unit_currency(rows)
    sample = []
    for ri in range(hdr + 1, len(rows)):
        for col in money_cols.values():
            if col is not None and col < len(rows[ri]):
                d = to_decimal(rows[ri][col])
                if d is not None:
                    sample.append(d)
    frame = units.resolve_monetary_frame(
        stmt_currency=ccy, geo_currency=None, inr_mentioned=(ccy == 'INR'),
        declared_unit=unit, sample_values=sample[:12], anchor_cr=None, ratecard=rate_card)
    frame_ok = not (frame.escalate or not frame.scale)
    hold_reason = '' if frame_ok else f'LP register: monetary frame unresolved — {frame.reason}'[:90]

    records: List[Record] = []
    total_numbers: List[Decimal] = []
    for ri in range(hdr + 1, len(rows)):
        row = rows[ri]
        if any(isinstance(v, str) and 'total' in lexicon.normalise_label(v).split() for v in row):
            total_numbers = [to_decimal(v) for v in row if to_decimal(v) is not None]
            continue                                    # total row — excluded from participants
        name = row[name_c] if name_c is not None and name_c < len(row) else None
        vals = {c: (to_decimal(row[col]) if (col is not None and col < len(row)) else None)
                for c, col in money_cols.items()}
        # participant = a NAME (the string concept — cited & confirmed, never summed)
        # AND ≥1 money value. A name with no money is a footnote/section note; a blank
        # row has no name — both skipped (zero-phantom-LP discipline).
        if not isinstance(name, str) or not name.strip():
            continue
        if all(v is None for v in vals.values()):
            continue
        fields: dict = {'lp_name': name.strip()}
        if type_c is not None and type_c < len(row) and isinstance(row[type_c], str) and row[type_c].strip():
            fields['lp_type'] = row[type_c].strip()
        for c, _hs, basis in _LP_MONEY:
            native = vals[c]
            if native is None:
                continue                                # sparse cell — omit (not a gap figure)
            prov = Provenance(source_file=label, content_fingerprint=content_fp, sheet=sheet,
                              cell=_a1(money_cols[c], ri), row_label=name.strip())
            if frame_ok:
                fields[c] = Figure(c, _to_cr(native, frame, rate_card), None, prov, basis=basis)
            else:
                fields[c] = Figure(c, None, None, prov, held=True, hold_reason=hold_reason)
        records.append(Record('lp_register', entity_id=name.strip(), fields=fields))

    if not records:
        return None
    corpus = _fund_corpus(prof)
    corpus_cr = _to_cr(corpus, frame, rate_card) if (corpus is not None and frame_ok) else None
    total_cr = [_to_cr(n, frame, rate_card) for n in total_numbers] if frame_ok else []
    return LPRegister(label, records, corpus_cr, total_cr, held=not frame_ok)


def reconcile_lp_register(reg: LPRegister, capital: Optional[Record] = None) -> List[dict]:
    """Verify a located LP register against its own totals and the capital account.
    Materialises HARD identities (returned as check rows); each HOLDS the affected
    column on failure, so a breakdown that does not reconcile is never shipped:
      • lp_total_row_multiset : the per-column sums ARE the stated total-row numbers,
                                in ANY column order (survives the shifted total row);
      • commitments_sum       : Σ(commitment, GP row included) == fund corpus;
      • called_le_committed   : per-LP called ≤ committed (a shifted binding reddens);
      • lp_{called,distributed}_ties_to_capital_account : Σ == the capital-account
                                total — the fund analog of Σ-divisions == consolidated,
                                free because the MVP already produced the totals.
    Mutates figures to held on any hard FAIL / INDETERMINATE."""
    checks: List[dict] = []
    by_concept = {c: [r.fields[c] for r in reg.records if isinstance(r.fields.get(c), Figure)]
                  for c in ('commitment', 'called', 'distributed')}

    def _hold(concept, reason):
        for f in by_concept[concept]:
            f.held = True
            if not f.hold_reason:
                f.hold_reason = reason[:90]

    if reg.held:                                        # escalated at extraction — nothing to assert
        checks.append(reconcile._result('lp_register_located', reconcile.HARD, reconcile.INDETERMINATE,
                      detail='register held at extraction (ambiguous/unresolved) — not reconciled'))
        return checks

    confirmed = {c: [f for f in by_concept[c] if f.confirmed] for c in by_concept}
    sums = {c: sum((f.value_cr for f in confirmed[c]), Decimal('0')) for c in confirmed}

    # 1 — total-row multiset: each per-column sum is SOME number on the stated total
    #     row (alignment-robust). Confirms the sums without trusting the total row's
    #     columns — the exact defence against the shifted total row the recon found.
    if reg.total_numbers_cr:
        remaining = list(reg.total_numbers_cr)
        unmatched = []
        for c in ('commitment', 'called', 'distributed'):
            if not confirmed[c]:
                continue
            hit = next((t for t in remaining if abs(t - sums[c]) <= _HARD_EPS), None)
            if hit is None:
                unmatched.append(c)
            else:
                remaining.remove(hit)
        if unmatched:
            for c in unmatched:
                _hold(c, f'LP {c} Σ {sums[c]} not among total row {reg.total_numbers_cr} — held')
            checks.append(reconcile._result('lp_total_row_multiset', reconcile.HARD, reconcile.FAIL,
                          detail=f'column sums {unmatched} absent from the stated total row — held',
                          sums={k: str(v) for k, v in sums.items()},
                          total_row=[str(t) for t in reg.total_numbers_cr]))
        else:
            checks.append(reconcile._result('lp_total_row_multiset', reconcile.HARD, reconcile.PASS,
                          detail='per-LP column sums reconcile to the stated total row (order-agnostic)'))

    # 2 — commitments_sum: Σ(commitment, GP row included) == fund corpus
    if reg.corpus_cr is not None and confirmed['commitment']:
        r = reconcile.hard_sum_equal('commitments_sum', confirmed['commitment'], reg.corpus_cr,
                                     lhs_label='Σ per-LP commitment', rhs_label='fund corpus')
        checks.append(r)
        if r['status'] in (reconcile.FAIL, reconcile.INDETERMINATE):
            _hold('commitment', f'Σ commitment != corpus {reg.corpus_cr} — held')

    # 3 — called_le_committed: per-LP, called ≤ committed (a one-column-shifted binding
    #     makes a called value exceed its commitment → reddens here, not silently).
    if any(isinstance(r.fields.get('called'), Figure) for r in reg.records):
        violations = [r.entity_id for r in reg.records
                      if isinstance(r.fields.get('called'), Figure) and r.fields['called'].confirmed
                      and isinstance(r.fields.get('commitment'), Figure) and r.fields['commitment'].confirmed
                      and r.fields['called'].value_cr - r.fields['commitment'].value_cr > _HARD_EPS]
        if violations:
            _hold('called', f'called exceeds commitment for {violations} — held')
            _hold('commitment', f'called exceeds commitment for {violations} — held')
            checks.append(reconcile._result('called_le_committed', reconcile.HARD, reconcile.FAIL,
                          detail=f'{len(violations)} LP(s) with called > committed — held', lps=violations))
        else:
            checks.append(reconcile._result('called_le_committed', reconcile.HARD, reconcile.PASS,
                          detail='every LP: capital called ≤ committed'))

    # 4 — cross-slice: Σ(per-LP called/distributed) == capital-account total
    if capital is not None:
        for concept, cid in (('called', 'lp_called_ties_to_capital_account'),
                             ('distributed', 'lp_distributed_ties_to_capital_account')):
            cap_fig = capital.fields.get(concept)
            if isinstance(cap_fig, Figure) and cap_fig.confirmed and confirmed[concept]:
                r = reconcile.hard_sum_equal(cid, confirmed[concept], cap_fig.value_cr,
                                             lhs_label=f'Σ per-LP {concept}',
                                             rhs_label=f'capital-account {concept}')
                checks.append(r)
                if r['status'] in (reconcile.FAIL, reconcile.INDETERMINATE):
                    _hold(concept, f'Σ {concept} != capital-account {cap_fig.value_cr} — held')
    return checks
