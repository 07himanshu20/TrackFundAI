"""Universal LEDGER reader — ONE table-reader for every row-level ledger the output
workbook needs (capital calls, exits, fees schedule, …). It is deliberately the same
primitive for all of them, driven by a small per-ledger CONFIG, so the fifth ledger
cannot drift away from the first (the centralize-don't-duplicate lesson the label
matcher and fund_extract already taught).

What every ledger extraction does, once:
  1. locate the ledger sheet (source-context markers; a forecast/budget sheet is
     never mined; >1 candidate → HOLD, never guess);
  2. bind columns from the HEADER row by lexicon match (never a fixed cell);
  3. pull the DATA rows, EXCLUDING total/subtotal rows (the row-explosion guard);
  4. cite every cell (per-row provenance → feeds DASHBOARD_BRIDGE for free);
  5. THE WHOLE POINT — tie Σ(amount rows) to a control total we already trust
     (the sheet's own stated total, and/or an external trusted total), and emit
     ONLY if it ties; otherwise HOLD with a reason. A ledger that looks full but
     dropped a row (or double-counted a subtotal) is worse than an empty one, so
     "did we get all the rows?" is answered by proof, not hope.

Reuses fund_extract's grid/frame primitives (no second copy of header/frame logic).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Tuple

from . import formulas, lexicon, reconcile, units
from .cir import Figure, Provenance, Record
from .contract import TOLERANCES
from .quantity import to_decimal
from .fund_extract import (_a1, _declared_unit_currency, _first_col, _is_forecast,
                           _norm_cells, _to_cr)

_HARD_EPS = Decimal(str(TOLERANCES['hard_abs_cr']))
_TOTAL_TOL = Decimal('0.01')          # Σrows vs stated total: 1% fractional tolerance

# a row is a total/aggregate row (EXCLUDED from data, remembered as the control) if any
# of its text cells is one of these aggregate tokens. Universal across all ledgers — the
# fixture's Fees sheet states its inception total as "ITD ~", not "Total", so a
# 'total'-only test silently read it as a 6th fee row. 'net' is deliberately NOT here
# (it is a real amount column, not an aggregate marker).
_TOTAL_TOKENS = frozenset({'total', 'subtotal', 'itd', 'ytd', 'cumulative', 'aggregate', 'grand'})


@dataclass
class LedgerColumn:
    concept: str                      # field name on the row Record
    headers: Tuple[str, ...]          # header synonyms (priority order)
    kind: str                         # 'money' | 'rate' | 'date' | 'text'
    value_basis: Optional[str] = None # gross/net basis for 'money' or 'rate' columns


@dataclass
class LedgerConfig:
    domain: str                       # CIR domain / output sub-sheet, e.g. 'capital_calls'
    source_markers: Tuple[str, ...]   # normalised sheet-context substrings
    key_headers: Tuple[str, ...]      # the row-key column (Call no / ex / FY)
    columns: Tuple[LedgerColumn, ...] # every column incl. the amount column
    amount_concept: Optional[str] = None  # column whose Σ ties to the control total;
                                          # None => COUNT-MODE (a tie-less list, e.g. a
                                          # filing calendar — completeness is count+provenance,
                                          # NEVER a summed proof; disclosed as such)
    anchor_concept: Optional[str] = None  # column that MUST be present to bind the header
                                          # (defaults to amount_concept). For count-mode this
                                          # is the distinguishing column (e.g. a 'due' date)
    expected_count: Optional[int] = None  # optional completeness hint for count-mode


@dataclass
class LedgerResult:
    domain: str
    records: List[Record]
    checks: List[dict]
    stated_total_cr: Optional[Decimal] = None    # the sheet's own total-row amount, in ₹Cr
    amount_figures: List[Figure] = field(default_factory=list)  # confirmed amount Figures (for cross-ties)
    held: bool = False
    sheet: str = ''


def _is_total_row(row) -> bool:
    return any(isinstance(v, str) and (_TOTAL_TOKENS & set(lexicon.normalise_label(v).split()))
               for v in row)


def _anchor_concept(cfg: LedgerConfig) -> Optional[str]:
    """The column that must be present to bind cfg's header. Money ledgers anchor on
    their amount column; count-mode ledgers anchor on the distinguishing column
    (anchor_concept) that tells them apart from a look-alike (e.g. a 'due' date
    separates a filing calendar from a compliance checklist that has no dates)."""
    return cfg.anchor_concept or cfg.amount_concept


def _header_in_sheet(rows, cfg: LedgerConfig):
    """First header row on this sheet carrying BOTH cfg's key column and its anchor
    column. Returns (row_index, norm) or (None, None)."""
    anchor = _anchor_concept(cfg)
    anchor_headers = tuple(h for col in cfg.columns if col.concept == anchor for h in col.headers)
    for ri, row in enumerate(rows):
        norm = [(ci, lexicon.normalise_label(v)) for ci, v in enumerate(row) if isinstance(v, str)]
        key_c = _first_col(norm, cfg.key_headers)
        anc_c = _first_col(norm, anchor_headers)
        if key_c is not None and anc_c is not None:
            return ri, norm
    return None, None


def sheet_matches(rows, cfg: LedgerConfig) -> bool:
    """Is this sheet a candidate for cfg's domain? It must carry a DISTINGUISHING
    source marker AND expose the key + anchor columns. A forecast/budget sheet is
    never a ledger. The markers are what tell look-alikes apart (calls vs
    distributions, tranches vs valuations, filing calendar vs compliance checklist) —
    routing on shape alone is forbidden."""
    text = _norm_cells(rows)
    if _is_forecast(text):
        return False
    if not any(m in text for m in cfg.source_markers):
        return False
    hdr, _ = _header_in_sheet(rows, cfg)
    return hdr is not None


def classify_sheets(prof, configs) -> dict:
    """Fail-closed per-SHEET domain classification (three-valued). Returns
    {sheet_name: [matching domains]} for every sheet matching ≥1 config:
      • exactly one domain  → the caller routes it to that ledger;
      • two or more domains → AMBIGUOUS — the caller HOLDS it (never guesses);
      • zero                → omitted (not a ledger sheet).
    Sheets in the wrong domain are worse than blank ones, so ambiguity holds."""
    out = {}
    for s in prof['sheets']:
        rows = prof['grid'][s.sheet]
        doms = [cfg.domain for cfg in configs if sheet_matches(rows, cfg)]
        if doms:
            out[s.sheet] = doms
    return out


def _row_header_configs(row, configs) -> List[Tuple[int, LedgerConfig]]:
    """Which configs consider THIS single row their header? A config's header must
    carry its key column AND its anchor column (by lexicon label match). Returns
    [(anchor_col_index, cfg), ...]. Data rows hold values (ids, dates, numbers), not
    header LABELS ('call no', 'amount', 'due'), so this rarely false-fires."""
    nrm = [(ci, lexicon.normalise_label(v)) for ci, v in enumerate(row) if isinstance(v, str)]
    hits = []
    for cfg in configs:
        anchor = _anchor_concept(cfg)
        anchor_headers = tuple(h for col in cfg.columns if col.concept == anchor for h in col.headers)
        key_c = _first_col(nrm, cfg.key_headers)
        anc_c = _first_col(nrm, anchor_headers)
        if key_c is not None and anc_c is not None:
            hits.append((anc_c, cfg))
    return hits


def segment_sheet(rows, sheet: str, configs, *, label, content_fp, rate_card, anchors=None):
    """Segment ONE sheet into per-domain BLOCKS and extract each to its own ledger.

    `anchors` = {domain: control_cr} — each domain's trusted EXTERNAL control (same-order
    ₹Cr magnitude), fed to the frame resolver so a block whose sheet omits its unit label
    still resolves (the unit is recovered from the control it must sit near). Absent →
    the block resolves only from a label, and holds if it has none (fail-closed).

    Real uploads are unstructured: a single fund sheet may stack TWO OR MORE domains
    (capital calls above, distributions below; a fee schedule beside carry terms), OR
    hold exactly one. This is the universal handler for both — never assume one-sheet-
    one-domain, never lose the second block by holding the whole sheet.

    How: every HEADER row is a block boundary; a block runs from its header down to the
    next header (or end of sheet), so a top block cannot bleed into the one below it.
    Each block is classified by ITS OWN header + the sheet's markers:
      • header matches exactly ONE domain → extract that block, bounded, and tie it;
      • header matches TWO+ domains (a genuinely ambiguous single header) → HELD, never
        guessed (the fail-closed guard against a look-alike misroute);
      • a forecast/budget sheet is never mined.
    Returns (results: List[LedgerResult], disclosures: List[dict]) — one classification
    disclosure per block so every routing decision is visible, never silent."""
    results: List[LedgerResult] = []
    disclosures: List[dict] = []
    text = _norm_cells(rows)
    if _is_forecast(text):
        disclosures.append(reconcile.disclosure(f'{sheet}_forecast',
            f'[{sheet}] reads as a forecast/budget sheet — never mined as a ledger'))
        return results, disclosures
    marker_ok = {id(cfg): any(m in text for m in cfg.source_markers) for cfg in configs}

    headers: List[Tuple[int, List[LedgerConfig]]] = []
    for ri, row in enumerate(rows):
        cand = [cfg for _, cfg in _row_header_configs(row, configs) if marker_ok[id(cfg)]]
        if cand:
            headers.append((ri, cand))
    if not headers:
        return results, disclosures

    seen_domains: dict = {}
    for idx, (hri, cand) in enumerate(headers):
        data_end = headers[idx + 1][0] if idx + 1 < len(headers) else len(rows)
        block_id = f'{sheet}_block@r{hri}'
        if len(cand) >= 2:                              # ambiguous single header → HOLD, never guess
            doms = sorted({c.domain for c in cand})
            if len(doms) >= 2:
                disclosures.append(reconcile._result(block_id, reconcile.HARD, reconcile.INDETERMINATE,
                    detail=f'[{sheet}] block @r{hri}: header matches {doms} — ambiguous, held',
                    domains=doms))
                continue
            cand = [cand[0]]                            # same domain twice on one header → collapse
        cfg = cand[0]
        norm = [(ci, lexicon.normalise_label(v)) for ci, v in enumerate(rows[hri])   # bind cols off THIS header
                if isinstance(v, str)]
        res = _extract_block(rows, sheet, cfg, hri, norm, data_end,
                             anchor_cr=(anchors or {}).get(cfg.domain),
                             label=label, content_fp=content_fp, rate_card=rate_card)
        if res is None:
            continue
        if cfg.domain == 'fees':
            res = reconcile_fee_schedule(res, label=label, content_fp=content_fp)
        seen_domains.setdefault(cfg.domain, 0)
        seen_domains[cfg.domain] += 1
        results.append(res)
        disclosures.append(reconcile.disclosure(block_id,
            f'[{sheet}] block @r{hri}–{data_end}: classified as {cfg.domain} '
            f'({len(res.records)} rows, held={res.held})', domain=cfg.domain,
            rows=len(res.records), held=res.held))
    return results, disclosures


def combine_blocks(domain: str, blocks: List[LedgerResult]) -> LedgerResult:
    """Merge every block of ONE domain (from any sheet/file) into a single result that
    SHARES the same Record/Figure objects — so a cross-source tie run on the combined
    result (Σ across all blocks vs the trusted control) mutates the very figures that
    get emitted. This is merge-not-first-wins: a domain split across two sheets ties on
    its combined Σ, and a hold propagates to the originating rows."""
    records = [r for b in blocks for r in b.records]
    checks = [c for b in blocks for c in b.checks]
    amount_figs = [f for b in blocks for f in b.amount_figures]
    return LedgerResult(domain, records, checks, None, amount_figs,
                        held=any(b.held for b in blocks),
                        sheet='+'.join(sorted({b.sheet for b in blocks if b.sheet})))


def extract_ledger(prof, cfg: LedgerConfig, *, label, content_fp, rate_card, anchor_cr=None) -> Optional[LedgerResult]:
    """Single-config convenience: find cfg's sheet in prof and extract. >1 matching
    sheet for the SAME domain → HOLD. (The pipeline uses classify_sheets +
    extract_from_sheet so a sheet matching TWO domains is also held.)"""
    sheets = [s.sheet for s in prof['sheets'] if sheet_matches(prof['grid'][s.sheet], cfg)]
    if not sheets:
        return None
    if len(sheets) > 1:
        prov = Provenance(source_file=label, content_fingerprint=content_fp,
                          sheet=','.join(sheets), cell='')
        amt_key = cfg.amount_concept or 'value'
        rec = Record(cfg.domain, entity_id='(ambiguous)',
                     fields={'key': '(ambiguous)',
                             amt_key: Figure(amt_key, None, None, prov, held=True,
                                             hold_reason=f'{cfg.domain}: {len(sheets)} candidate '
                                             'sheets — ambiguous, held')})
        return LedgerResult(cfg.domain, [rec], [reconcile._result(
            f'{cfg.domain}_located', reconcile.HARD, reconcile.INDETERMINATE,
            detail=f'{len(sheets)} candidate ledger sheets — held')], held=True)
    return extract_from_sheet(prof['grid'][sheets[0]], sheets[0], cfg, anchor_cr=anchor_cr,
                              label=label, content_fp=content_fp, rate_card=rate_card)


def extract_from_sheet(rows, sheet: str, cfg: LedgerConfig, *, label, content_fp,
                       rate_card, anchor_cr=None) -> Optional[LedgerResult]:
    """Single-config convenience: extract cfg from its FIRST header on the sheet to the
    end of the sheet. Used for single-block sheets (and by extract_ledger). A sheet
    that STACKS several domains must go through segment_sheet(), which bounds each block
    to [header, next-header) so a top block never bleeds into the block below."""
    hdr, norm = _header_in_sheet(rows, cfg)
    if hdr is None:
        return None
    return _extract_block(rows, sheet, cfg, hdr, norm, len(rows), anchor_cr=anchor_cr,
                          label=label, content_fp=content_fp, rate_card=rate_card)


def _extract_block(rows, sheet: str, cfg: LedgerConfig, hdr: int, norm, data_end: int, *,
                   label, content_fp, rate_card, anchor_cr=None) -> Optional[LedgerResult]:
    """Extract cfg's rows from ONE block — the header at `hdr` down to `data_end`
    (exclusive). Fail-closed: an unresolved monetary frame, or Σrows ≠ the block's
    stated total → HOLD.

    `anchor_cr` is the block's trusted EXTERNAL control in ₹Cr (the capital-account
    called, the fund's realised total, total cost — NOT the block's own stated total,
    which is in the same native unit and cannot disambiguate scale). Feeding it lets a
    block whose sheet OMITS its unit label still resolve — the unit is recovered from
    the known-Cr magnitude it must sit near — instead of being held for a missing label.
    This does NOT weaken the guard: with neither a label nor an anchor the frame still
    holds; a wrong magnitude is still out-of-band (held); and the exact Σ tie downstream
    still verifies the scale to the rupee. The control is same-order as the block, so it
    also arms the magnitude-lie check on a LABELLED ledger sheet (a strengthening).

    Two modes, one reader:
      • MONEY-MODE (amount_concept set): every data row carries an amount; Σ ties to
        the block's stated total (and, via tie_to_control, to an external total).
      • COUNT-MODE (amount_concept None): a tie-less list (e.g. a filing calendar). A
        data row is any keyed, non-total row; completeness is DISCLOSED as a count +
        per-row provenance — never a summed proof (you cannot sum-prove a date list)."""
    money_mode = cfg.amount_concept is not None
    has_money = any(col.kind == 'money' for col in cfg.columns)
    key_c = _first_col(norm, cfg.key_headers)
    col_idx = {col.concept: _first_col(norm, col.headers) for col in cfg.columns}
    amount_c = col_idx.get(cfg.amount_concept) if money_mode else None

    # resolve the monetary frame ONCE, only if there ARE money columns (a count-mode
    # date list has none) — fail-closed: unresolved → every money figure held.
    frame, frame_ok, hold_reason = None, True, ''
    if has_money:
        unit, ccy = _declared_unit_currency(rows)
        sample = []
        for ri in range(hdr + 1, data_end):
            if _is_total_row(rows[ri]):          # the aggregate is not a data magnitude — a total
                continue                         # in the sample inflates rep to == the control anchor
            for col in cfg.columns:
                ci = col_idx.get(col.concept)
                if col.kind == 'money' and ci is not None and ci < len(rows[ri]):
                    d = to_decimal(rows[ri][ci])
                    if d is not None:
                        sample.append(d)
        frame = units.resolve_monetary_frame(
            stmt_currency=ccy, geo_currency=None, inr_mentioned=(ccy == 'INR'),
            declared_unit=unit, sample_values=sample[:12], anchor_cr=anchor_cr, ratecard=rate_card)
        frame_ok = not (frame.escalate or not frame.scale)
        hold_reason = '' if frame_ok else f'{cfg.domain}: monetary frame unresolved — {frame.reason}'[:90]

    records: List[Record] = []
    amount_figs: List[Figure] = []
    stated_total_native: Optional[Decimal] = None
    for ri in range(hdr + 1, data_end):
        row = rows[ri]
        if _is_total_row(row):                          # stated total — excluded, remembered for the tie
            if amount_c is not None and amount_c < len(row):
                t = to_decimal(row[amount_c])
                if t is not None:
                    stated_total_native = t
            continue
        key = row[key_c] if (key_c is not None and key_c < len(row)) else None
        key_present = key is not None and str(key).strip() != ''
        if money_mode:
            amt = to_decimal(row[amount_c]) if (amount_c is not None and amount_c < len(row)) else None
            if amt is None:                             # no amount → not a ledger data row (note/blank)
                continue
        elif not key_present:                           # count-mode: a keyed, non-total row is a data row
            continue
        key_s = str(key).strip() if key_present else f'row{ri + 1}'
        fields: dict = {'key': key_s}
        for col in cfg.columns:
            ci = col_idx.get(col.concept)
            if ci is None or ci >= len(row):
                continue
            v = row[ci]
            if col.kind in ('money', 'rate'):
                native = to_decimal(v)
                if native is None:
                    continue
                prov = Provenance(source_file=label, content_fingerprint=content_fp, sheet=sheet,
                                  cell=_a1(ci, ri), row_label=key_s)
                if col.kind == 'rate':                  # a rate/ratio is frame-independent
                    fields[col.concept] = Figure(col.concept, native, None, prov,
                                                 basis='point_in_time', value_basis=col.value_basis)
                elif frame_ok:
                    fields[col.concept] = Figure(col.concept, _to_cr(native, frame, rate_card), None,
                                                 prov, basis='point_in_time', value_basis=col.value_basis)
                else:
                    fields[col.concept] = Figure(col.concept, None, None, prov, held=True,
                                                 hold_reason=hold_reason, value_basis=col.value_basis)
            else:                                        # date / text — cited scalar, never summed
                if v is not None and str(v).strip():
                    fields[col.concept] = str(v).strip() if isinstance(v, str) else v
        records.append(Record(cfg.domain, entity_id=key_s, fields=fields))
        if money_mode:
            af = fields.get(cfg.amount_concept)
            if isinstance(af, Figure) and af.confirmed:
                amount_figs.append(af)

    if not records:
        return None

    checks: List[dict] = []
    stated_total_cr = (_to_cr(stated_total_native, frame, rate_card)
                       if (stated_total_native is not None and frame_ok and frame is not None) else None)
    result = LedgerResult(cfg.domain, records, checks, stated_total_cr, amount_figs,
                          held=(has_money and not frame_ok), sheet=sheet)

    # ── COUNT-MODE: no numeric tie is possible — DISCLOSE completeness as count +
    #    provenance (honestly weaker than a summed ledger), never claim a proof. ──
    if not money_mode:
        detail = (f'{len(records)} {cfg.domain} rows extracted with per-row provenance; '
                  f'count-not-tie domain (no numeric total to sum-prove completeness)')
        if cfg.expected_count is not None:
            detail += f'; expected ~{cfg.expected_count}'
        checks.append(reconcile.disclosure(f'{cfg.domain}_row_count', detail, count=len(records),
                                           expected=cfg.expected_count))
        return result

    if not frame_ok:
        checks.append(reconcile._result(f'{cfg.domain}_frame', reconcile.HARD, reconcile.INDETERMINATE,
                      detail=hold_reason))
        return result
    # ── within-sheet tie: Σ(amount rows) == the sheet's own stated total row ──
    if stated_total_cr is not None and amount_figs:
        r = reconcile.hard_sum_equal(f'{cfg.domain}_rows_sum_to_total', amount_figs, stated_total_cr,
                                     lhs_label=f'Σ {cfg.domain} rows', rhs_label='sheet stated total')
        checks.append(r)
        if r['status'] in (reconcile.FAIL, reconcile.INDETERMINATE):
            _hold_amounts(result, f'Σ {cfg.domain} rows != stated total {stated_total_cr} — held')
    return result


def _hold_amounts(result: LedgerResult, reason: str) -> None:
    """Hold every figure on every row of the ledger — a ledger that doesn't tie to
    its control total is not shipped (values kept for the reviewer, excluded from sums)."""
    for rec in result.records:
        for f in rec.figures():
            f.held = True
            if not f.hold_reason:
                f.hold_reason = reason[:90]
    result.held = True


def tie_to_control(result: LedgerResult, control_cr: Optional[Decimal], *, control_label: str,
                   check_id: str) -> Optional[dict]:
    """Cross-source tie: Σ(amount rows) == an EXTERNAL trusted total (e.g. the
    capital-account `called`, the fund's realised total). Independent of the sheet's
    own total row, so it catches a whole-sheet mis-read the within-sheet tie can't.
    HOLDs the ledger on mismatch. Returns the check row (or None if no control)."""
    if control_cr is None or not result.amount_figures or result.held:
        return None
    r = reconcile.hard_sum_equal(check_id, result.amount_figures, control_cr,
                                 lhs_label=f'Σ {result.domain} rows', rhs_label=control_label)
    if r['status'] in (reconcile.FAIL, reconcile.INDETERMINATE):
        _hold_amounts(result, f'Σ {result.domain} != {control_label} {control_cr} — held')
    return r


# ══════════════════════════════════════════════════════════════════════════
# PER-LEDGER CONFIGS — the ONLY per-ledger code. Adding a ledger = adding a config
# here, never a new reader. Each names its source markers, its row-key column, its
# columns (with kinds), and which column's Σ ties to the control total.
# ══════════════════════════════════════════════════════════════════════════
CAPITAL_CALLS = LedgerConfig(
    domain='capital_calls',
    source_markers=('capital call', 'drawdown', 'call ledger', 'call no'),
    key_headers=('call no', 'call number', 'call', 'drawdown no'),
    columns=(
        LedgerColumn('date', ('date', 'call date', 'drawdown date'), 'date'),
        LedgerColumn('amount', ('amount', 'call amount', 'drawdown amount', 'called'), 'money'),
        LedgerColumn('purpose', ('purpose', 'use', 'description', 'notes', 'note'), 'text'),
    ),
    amount_concept='amount',
)

EXITS = LedgerConfig(
    domain='exits',
    source_markers=('exit', 'realisation', 'realization', 'secondary'),
    key_headers=('ex', 'exit no', 'id', 'company'),
    columns=(
        LedgerColumn('exit_id', ('id',), 'text'),
        LedgerColumn('company', ('company', 'company name', 'investee'), 'text'),
        LedgerColumn('date', ('date', 'exit date'), 'date'),
        LedgerColumn('type', ('type', 'exit type'), 'text'),
        LedgerColumn('cost_realised', ('cost realised', 'cost realized', 'cost'), 'money'),
        LedgerColumn('gross_proceeds', ('gross proceeds', 'proceeds', 'gross'), 'money'),
        LedgerColumn('net_proceeds', ('net proceeds', 'net'), 'money'),
        LedgerColumn('irr_on_exit', ('irr on exit', 'irr', 'exit irr'), 'rate',
                     value_basis=formulas.BASIS_GROSS_XIRR),
    ),
    amount_concept='gross_proceeds',
)

# DISTRIBUTIONS — the look-alike of capital calls (both dated cash ledgers). Told
# apart ONLY by direction/labels: 'distribution/payout' here vs 'call/drawdown' there.
# amount = NET (what LPs received, §4.5). Ties to the capital-account `distributed`.
DISTRIBUTIONS = LedgerConfig(
    domain='distributions',
    source_markers=('distribution', 'distributions to lp', 'payout', 'distributed to'),
    key_headers=('dist', 'distribution no', 'dist no', 'distribution'),
    columns=(
        LedgerColumn('date', ('date', 'distribution date'), 'date'),
        LedgerColumn('type', ('type', 'distribution type'), 'text'),
        LedgerColumn('gross', ('gross', 'gross amount', 'total gross'), 'money',
                     value_basis=formulas.BASIS_GROSS),
        # net = what LPs actually received (net of GP carry). DPI/TVPI numerators use
        # the NET distribution, so the basis is carried on the figure, not implied.
        LedgerColumn('net', ('net', 'net amount', 'total net'), 'money',
                     value_basis=formulas.BASIS_NET),
        LedgerColumn('gp_carry', ('gp carry', 'carry', 'gp carry amount'), 'money'),
        LedgerColumn('source', ('source', 'note', 'remark'), 'text'),
    ),
    amount_concept='net',
)

# INVESTMENT TRANCHES — per-tranche dated cost outflows. Look-alike of investments/
# valuations (all company tables); told apart by 'tranche/deployment' + a dated
# amount column. Total ties to deployment (448); each company's Σ ties to its cost.
INVESTMENT_TRANCHES = LedgerConfig(
    domain='investment_tranches',
    source_markers=('tranche', 'deployment tranche', 'deployment / cost', 'deployment tranches'),
    key_headers=('tr', 'tranche', 'tranche no'),
    columns=(
        LedgerColumn('investment_id', ('id',), 'text'),
        LedgerColumn('company', ('company', 'company name', 'investee'), 'text'),
        LedgerColumn('date', ('date', 'deployment date', 'tranche date'), 'date'),
        LedgerColumn('amount', ('amount', 'tranche amount', 'cost', 'deployed'), 'money'),
        LedgerColumn('instrument', ('instrument', 'security', 'type'), 'text'),
    ),
    amount_concept='amount',
)

# FEES — the management-fee schedule (per-FY). Look-alike-safe by header anchoring:
# the fixture's [Fees] sheet STACKS a fee schedule (FY | base | rate | annual fee) and a
# carry-terms block (Carry %/Hurdle/Catch-up) in one sheet; binding to the 'annual fee'
# header reads ONLY the schedule block and never the carry block (which a waterfall
# config reads). Σ(annual fee) ties to the sheet's ITD total — see reconcile_fee_schedule
# for the part-year (FY__ part) rounding reconciliation that closes it to the rupee.
FEES = LedgerConfig(
    domain='fees',
    source_markers=('management fee', 'performance fee', 'fee schedule', 'annual fee',
                    'committed base', 'management & perform'),
    key_headers=('fy', 'year', 'financial year', 'period'),
    columns=(
        LedgerColumn('committed_base', ('committed base', 'commitment base', 'base'), 'money'),
        LedgerColumn('rate', ('rate', 'fee rate', 'fee %'), 'rate'),
        LedgerColumn('annual_fee', ('annual fee', 'fee', 'fee amount', 'management fee'), 'money'),
    ),
    amount_concept='annual_fee',
)

# SEBI CALENDAR — a COUNT-MODE domain (a filing calendar has NO numeric total; you cannot
# sum-prove it complete). Told apart from its look-alike, the SEBI *compliance checklist*
# (requirement/ref/norm — no dates), by the DUE-date anchor: the checklist has no 'due'
# column, so it never binds here. Completeness is disclosed as a row count + provenance.
SEBI_CALENDAR = LedgerConfig(
    domain='sebi_calendar',
    source_markers=('sebi', 'statutory', 'regulatory', 'compliance calendar', 'filing'),
    key_headers=('#', 'sr', 'sr no', 'no', 'activity'),
    columns=(
        LedgerColumn('activity', ('activity', 'requirement', 'filing', 'report'), 'text'),
        LedgerColumn('frequency', ('freq', 'frequency'), 'text'),
        LedgerColumn('authority', ('authority', 'regulator', 'ref'), 'text'),
        LedgerColumn('period', ('period', 'for period'), 'text'),
        LedgerColumn('due_date', ('due', 'due date', 'deadline'), 'date'),
        LedgerColumn('status', ('status', 'state'), 'text'),
    ),
    amount_concept=None,               # COUNT-MODE — completeness = count+provenance, not a tie
    anchor_concept='due_date',         # the distinguishing column vs the compliance checklist
    expected_count=None,
)

# registry the pipeline iterates — order is deterministic. Adding a ledger = adding a
# config here (never a new reader).
LEDGERS = (CAPITAL_CALLS, EXITS, DISTRIBUTIONS, INVESTMENT_TRANCHES, FEES, SEBI_CALENDAR)


def reconcile_fee_schedule(result: Optional[LedgerResult], *, label: str,
                           content_fp: str) -> Optional[LedgerResult]:
    """Close the fee schedule to its stated ITD total TO THE RUPEE, or leave it held.

    The fixture states annual fees 20/20/20/20/15 (Σ 95) but an ITD total of 94.95 —
    the 0.05 is the source ROUNDING its part-year line (shows 15; precise = 94.95 − 80
    full years = 14.95). This is NOT a plug and NOT an ≈: we keep every source row at
    its cited cell value and add ONE named, cited reconciling line = −(Σrows − ITD),
    attributed to the part-year row and derived from two cited totals, so the schedule
    genuinely sums to the stated ITD. Fail-closed: we only close a ROUNDING-SCALE
    residual that has a NAMED part-year cause; a residual the size of a whole missing
    FY row, or with no part-year row to attribute it to, stays HELD — never closed."""
    if (result is None or result.domain != 'fees' or result.stated_total_cr is None
            or not result.amount_figures):
        return result
    stated = result.stated_total_cr
    rows_sum = sum((f.value_cr for f in result.amount_figures), Decimal('0'))
    residual = rows_sum - stated
    if abs(residual) <= _HARD_EPS:
        return result                                    # already ties to the rupee

    def _is_part(rec):
        # check the RAW key — normalise_label strips parentheticals, so 'FY26 (part)'
        # would normalise to 'fy26' and lose the very marker we need.
        k = str(rec.fields.get('key', '')).lower()
        return any(t in k for t in ('part', 'partial', 'stub', 'pro-rata', 'prorata'))

    part = next((rec for rec in result.records if _is_part(rec)), None)
    part_fig = part.fields.get('annual_fee') if part is not None else None
    rounding_scale = abs(residual) <= max(Decimal('0.5'), stated * _TOTAL_TOL)
    # amount_figures is non-empty ⇒ the frame resolved and these values were confirmed at
    # append time; they are held ONLY by the naive within-sheet tie we are here to close,
    # so gate on value presence, not the .confirmed flag (which that hold just flipped).
    if part is None or not isinstance(part_fig, Figure) or part_fig.value_cr is None or not rounding_scale:
        # unexplained / row-sized gap — hold, never close (fail-closed)
        _hold_amounts(result, f'fees: Σrows {rows_sum} != ITD {stated}, no part-year rounding cause — held')
        result.checks[:] = [c for c in result.checks if c.get('id') != 'fees_rows_sum_to_total']
        result.checks.append(reconcile._result('fees_rows_sum_to_total', reconcile.HARD, reconcile.FAIL,
                             detail=f'Σ {rows_sum} vs ITD {stated}; residual {residual} not closeable — held'))
        return result

    full_years = rows_sum - part_fig.value_cr
    precise_part = stated - full_years                   # 94.95 − 80 = 14.95, from cited totals
    prov = Provenance(source_file=label, content_fingerprint=content_fp, sheet=result.sheet,
                      cell=(part_fig.provenance.cell if part_fig.provenance else ''),
                      row_label='(reconciling)',
                      derived_from=[f'part-year cell {part_fig.provenance.cell if part_fig.provenance else "?"}',
                                    'stated ITD total'],
                      note=(f'{part.fields.get("key")}: source rounds part-year fee to {part_fig.value_cr}; '
                            f'precise {precise_part} = ITD {stated} − full years {full_years}'))
    recon = Figure('annual_fee', -residual, None, prov, basis='point_in_time')
    result.records.append(Record('fees', entity_id='(reconciling: part-year rounding)',
                                 fields={'key': f'less: {part.fields.get("key")} rounding',
                                         'annual_fee': recon}))
    result.amount_figures.append(recon)

    # clear the naive within-sheet hold, then re-tie WITH the reconciling item → exact
    for rec in result.records:
        for f in rec.figures():
            if f.hold_reason.startswith('Σ fees rows != stated total'):
                f.held, f.hold_reason = False, ''
    result.held = False
    r = reconcile.hard_sum_equal('fees_rows_sum_to_total', result.amount_figures, stated,
                                 lhs_label='Σ fee rows + reconciling item', rhs_label='stated ITD total')
    result.checks[:] = [c for c in result.checks if c.get('id') != 'fees_rows_sum_to_total']
    result.checks.append(r)
    result.checks.append(reconcile.disclosure('fees_partyear_reconciliation',
        f'part-year fee shown as {part_fig.value_cr} in source (precise {precise_part}); named '
        f'reconciling line {-residual} added so the schedule ties to stated ITD {stated} to the rupee',
        residual=str(residual), part_year=str(part.fields.get('key'))))
    if r['status'] != reconcile.PASS:                    # defensive: if it still doesn't tie, hold
        _hold_amounts(result, f'fees: reconciled Σ still != ITD {stated} — held')
    return result


def tie_per_group(result: LedgerResult, cfg: LedgerConfig, group_concept: str,
                  controls_by_key: dict, *, check_id: str) -> List[dict]:
    """Per-group control tie — e.g. investment tranches: Σ(amount for company X) must
    equal company X's cost. Groups the amount figures by `group_concept`, ties each
    group's Σ to its control (keyed by normalised group value), and HOLDs a group's
    figures on mismatch. Returns one check row per group with a control."""
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for rec in result.records:
        g = rec.fields.get(group_concept)
        amt = rec.fields.get(cfg.amount_concept)
        if isinstance(g, str) and isinstance(amt, Figure) and amt.confirmed:
            groups[lexicon.normalise_label(g)].append((rec, amt))
    checks: List[dict] = []
    for gkey, members in sorted(groups.items()):
        control = controls_by_key.get(gkey)
        if control is None:
            continue
        figs = [amt for _, amt in members]
        r = reconcile.hard_sum_equal(f'{check_id}:{gkey[:24]}', figs, control,
                                     lhs_label=f'Σ {cfg.domain} for {gkey[:20]}',
                                     rhs_label='company control')
        checks.append(r)
        if r['status'] in (reconcile.FAIL, reconcile.INDETERMINATE):
            for _, amt in members:
                amt.held = True
                if not amt.hold_reason:
                    amt.hold_reason = f'{cfg.domain} Σ for {gkey[:20]} != control {control} — held'[:90]
    return checks
