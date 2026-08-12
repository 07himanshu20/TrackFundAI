"""
S9 — Assembly. Builds the workbook from the CIR (a pure function: same CIR →
same workbook). The single most important rule here protects the guarantee #1
was built for, at the spreadsheet layer:

  A TOTAL WITH ANY HELD/GAP INPUT IS NEVER A LIVE =SUM.
  Excel/LibreOffice SUM treats a blank — and a text "HELD" — cell as 0, so a
  naive =SUM over a hole recalcs to a clean, wrong number, re-introducing the
  exact bug #1 fixed one layer down. Therefore code computes every total
  hole-aware (aggregate.py); a complete total may be written as a value OR a
  live SUM over a fully-confirmed contiguous range; an incomplete total is
  written as the literal text "INCOMPLETE" plus a disclosure. There is no path
  by which a hole becomes a zero in a total.

Other S9 rules:
  • Old-stable functions only (SUM, SUMIF, INDEX/MATCH, IFERROR) — nothing
    volatile/post-2007 — so the LibreOffice recalc gate computes what the client's
    Excel reads. Cross-sheet refs quote sheet names with spaces.
  • Figure-granular holds — a company with 4 of 5 metrics resolved appears with
    its four and a visible flag on the fifth; it never vanishes.
  • Held figures, gaps, and investments-with-no-MIS render as VISIBLE disclosures.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

import openpyxl
from openpyxl.styles import Font, PatternFill

from . import aggregate
from .cir import CIR, Figure
from .fund_terms import FundTerm

INCOMPLETE = 'INCOMPLETE'
HELD_MARK = '⚠ HELD'
GAP_MARK = '— (no data)'

_HDR = Font(bold=True)
_WARN = Font(color='8C2F2F', bold=True)
_HELD_FILL = PatternFill('solid', fgColor='FBF1F1')


def _write_row(ws, values, *, header=False, warn_cols=()):
    ws.append(values)
    r = ws.max_row
    for i, _v in enumerate(values, start=1):
        if header:
            ws.cell(r, i).font = _HDR
        elif (i - 1) in warn_cols:
            ws.cell(r, i).font = _WARN
            ws.cell(r, i).fill = _HELD_FILL


def cell_for_figure(fig: Optional[Figure]):
    """How a single figure renders — a confirmed value, or a VISIBLE held/gap
    marker (never a blank that a SUM would silently zero)."""
    if fig is None:
        return GAP_MARK, True
    if fig.gap:
        return GAP_MARK, True
    if fig.held:
        return f'{HELD_MARK}: {fig.hold_reason[:40]}' if fig.hold_reason else HELD_MARK, True
    return float(fig.value_cr) if fig.value_cr is not None else GAP_MARK, fig.value_cr is None


def write_total(ws, label: str, figures: List[Figure], *, policy='annualized'):
    """Write a hole-aware total row. Complete → the value; any held/gap input →
    the literal INCOMPLETE (never a formula, never a zeroed hole). Returns the
    AggResult so the caller can disclose incompleteness."""
    agg = aggregate.aggregate(figures, policy=policy)
    if agg.complete and agg.value is not None:
        _write_row(ws, [label, float(agg.value.quantize(Decimal('0.0001')))])
    else:
        _write_row(ws, [label, INCOMPLETE], warn_cols=(1,))
    return agg


# ── the workbook ─────────────────────────────────────────────────────────
def build(cir: CIR, *, rate_card=None, reconciliation: List[dict] = None,
          audit: List[dict] = None, missing_investments: List[dict] = None,
          title: str = 'TFAI — Fund Master Workbook (preingest3)') -> openpyxl.Workbook:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    figs_by_concept = _group_by_concept(cir)

    _cover(wb, title, cir, figs_by_concept)
    _portfolio_kpi(wb, cir)
    _fund_summary(wb, cir)                 # NEW sheet, fund-less CIR → no-op (MIS byte-identical)
    _lp_register(wb, cir)                  # NEW sheet, fund-less CIR → no-op
    _fund_terms(wb, cir)                   # NEW sheet, fund-less CIR → no-op
    _provenance(wb, cir)
    _disclosures(wb, cir, missing_investments or [])
    _reconciliation(wb, reconciliation or [])
    if rate_card is not None:
        _rate_card(wb, rate_card)
    _audit(wb, audit or [])
    return wb


def _group_by_concept(cir: CIR) -> Dict[str, List[Figure]]:
    out: Dict[str, List[Figure]] = {}
    for rec in cir.records:
        for f in rec.figures():
            out.setdefault(f.concept, []).append(f)
    return out


def _cover(wb, title, cir: CIR, figs_by_concept):
    ws = wb.create_sheet('Cover')
    _write_row(ws, [title], header=True)
    _write_row(ws, ['As of', cir.as_of, 'Rate Card', cir.rate_card_id])
    _write_row(ws, [])
    _write_row(ws, ['Metric', 'Value (₹Cr) / State'], header=True)
    # Every headline total is hole-aware — a held company can't zero it.
    for concept, label in [('revenue', 'Portfolio Revenue (annualized)'),
                           ('ebitda', 'Portfolio EBITDA (annualized)'),
                           ('cost', 'Total Deployed Cost'),
                           ('fair_value', 'Total Fair Value')]:
        agg = write_total(ws, label, figs_by_concept.get(concept, []))
        if not (agg.complete and agg.value is not None):
            # an INCOMPLETE total must NAME why, so the headline is never a
            # confident-looking silently-undercounted number. Two causes: a
            # held/gap input (not confirmed), and a confirmed FLOW whose period
            # is unknown (un-annualisable — the mixed-basis guard, named too).
            missing, unknown_period = [], []
            for rec in cir.records:
                fig = rec.fields.get(concept)
                if not isinstance(fig, Figure):
                    continue
                who = rec.entity_id or rec.fields.get('company', '')
                if not fig.confirmed:
                    missing.append(who)
                elif not aggregate._is_stock(fig) and not fig.months:
                    unknown_period.append(who)
            parts = []
            if missing:
                parts.append('held/gap: ' + ', '.join(m for m in missing if m))
            if unknown_period:
                parts.append('unknown-period flow: ' + ', '.join(m for m in unknown_period if m))
            if parts:
                ws.cell(ws.max_row, 3).value = ('excludes — ' + ' ; '.join(parts))[:200]
    n_companies = sum(1 for r in cir.records if r.domain in ('portfolio_companies', 'company', 'mis'))
    _write_row(ws, ['# Portfolio Companies', n_companies])
    _write_row(ws, ['Held / escalated figures', sum(1 for fs in figs_by_concept.values()
                                                     for f in fs if f.held)])


def _annualized_comparable(fig):
    """The cross-company COMPARABLE for a FLOW = its 12-month run-rate, so a 1-month
    and a full-year figure can be read side by side. A held/gap flow mirrors its
    native marker; a flow with unknown period is un-annualisable → an explicit
    'n/a — period unknown' (never a raw number masquerading as annualised). Stocks
    are NOT passed here — a balance is not annualised (nature-aware, U5)."""
    if not isinstance(fig, Figure) or fig.gap:
        return GAP_MARK
    if fig.held:
        return HELD_MARK                                 # escalated — mirror the native cell
    if fig.value_cr is None:
        return GAP_MARK
    m = fig.months
    if not m:
        return 'n/a — period unknown'                    # un-annualisable — never a raw number
    if 0 < m < 12:
        ann = round(float(fig.value_cr) * 12.0 / m, 4)
        # a very short stub ×12 is a weak projection — flag it so a 1-month figure
        # never reads as a solid annual run-rate in the comparable column.
        return f'{ann} (stub {m}m×12)' if m < aggregate._SHORT_STUB_MONTHS else ann
    return round(float(fig.value_cr), 4)                 # months >= 12 — already annual


def _portfolio_kpi(wb, cir: CIR):
    ws = wb.create_sheet('Portfolio_KPI')
    cols = ['Company', 'Revenue', 'EBITDA', 'Cash', 'Head-count', 'Basis',
            'Revenue (ann.₹Cr)', 'EBITDA (ann.₹Cr)', 'Notes']
    _write_row(ws, cols, header=True)
    concepts = ['revenue', 'ebitda', 'cash', 'headcount']   # native, as-reported
    for rec in cir.records:
        if rec.domain not in ('portfolio_companies', 'company', 'mis'):
            continue
        row = [rec.entity_id or rec.fields.get('company', '')]
        warn = []
        for i, c in enumerate(concepts, start=1):
            fig = rec.fields.get(c)
            val, held = cell_for_figure(fig if isinstance(fig, Figure) else None)
            row.append(val)
            if held:
                warn.append(i)
        basis = next((f.basis for f in rec.figures() if f.basis), '')
        row.append(basis)                                    # col 5 = Basis
        # nature-aware comparable: FLOWS get a 12-month run-rate; the stock metrics
        # (Cash / Head-count) are period-end balances and get no comparable column.
        rev_fig, eb_fig = rec.fields.get('revenue'), rec.fields.get('ebitda')
        row += [_annualized_comparable(rev_fig), _annualized_comparable(eb_fig)]   # cols 6,7
        color = list(warn)
        if isinstance(rev_fig, Figure) and rev_fig.held:
            color.append(6)
        if isinstance(eb_fig, Figure) and eb_fig.held:
            color.append(7)
        row.append(f'{len(warn)} held' if warn else '')      # col 8 = Notes
        _write_row(ws, row, warn_cols=tuple(color))


# ── Fund sections (Phase D) — NEW sheets, never modifications ──────────────
# Scoped to what extraction produces: a fund SUMMARY (capital-account flows +
# corpus, with the LP↔capital tie-out) and the per-LP REGISTER table. NAV /
# waterfall / terms are not in the CIR yet, so they get no sheet (building output
# for absent concepts is the speculative-build trap). Both functions EARLY-RETURN
# on a fund-less CIR, so an MIS-only workbook is byte-identical to before.
#
# concept → (label, source domain, tie-out check ids that must PASS for a live total)
_FUND_SUMMARY_ROWS = [
    ('called', 'Capital Called (cumulative)', 'fund_financials',
     ('lp_called_ties_to_capital_account',)),
    ('distributed', 'Distributions (cumulative)', 'fund_financials',
     ('lp_distributed_ties_to_capital_account',)),
    ('commitment', 'Total Commitment / Corpus', 'lp_register',
     ('commitments_sum', 'lp_total_row_multiset')),
]


def _tieout_text(tie_results: List[Optional[dict]]) -> str:
    """The cross-check line for a fund total — each reconciliation's values + verdict."""
    parts = []
    for r in tie_results:
        if r is None:
            continue
        ok = r.get('status') == 'pass'
        vals = ''
        if r.get('lhs') not in (None, ''):
            vals = f" {r.get('lhs')}{'=' if ok else '≠'}{r.get('rhs')}"
        parts.append(f"{r.get('id', '?')}{vals} {'✓' if ok else '✗ HELD'}")
    return '; '.join(parts)[:150] if parts else 'no cross-check available'


def _fund_summary(wb, cir: CIR):
    """Fund headline totals. Cumulative flows are shown AS cumulative (never
    annualised — same category error as annualising a stock). A total whose tie-out
    reconciliation HELD renders INCOMPLETE, exactly like a held input: a fund total
    that doesn't reconcile is not a number (guarantee #1 at the fund layer)."""
    if not any(r.domain in ('fund_financials', 'lp_register', 'nav') for r in cir.records):
        return
    ws = wb.create_sheet('Fund_Summary')
    _write_row(ws, ['TFAI — Fund Summary'], header=True)
    _write_row(ws, ['cumulative ₹Cr — capital-account flows are lifetime-to-date, NOT annualised'])
    _write_row(ws, [])
    _write_row(ws, ['Metric', 'Value (₹Cr)', 'Basis', 'Tie-out (LP ↔ capital account)', 'State'], header=True)
    checks = {c.get('id'): c for c in cir.checks}
    for concept, label, domain, tie_ids in _FUND_SUMMARY_ROWS:
        figs = [r.fields[concept] for r in cir.records
                if r.domain == domain and isinstance(r.fields.get(concept), Figure)]
        if not figs:
            continue                                    # concept not extracted this run — omit
        agg = aggregate.aggregate(figs, policy='as_reported')   # cumulative → NO annualisation
        tie_results = [checks.get(cid) for cid in tie_ids]
        present = [t for t in tie_results if t is not None]
        tie_failed = any(t.get('status') != 'pass' for t in present)   # a held/failed recon
        basis = next((f.basis for f in figs if f.basis), '')
        if agg.complete and agg.value is not None and not tie_failed:
            _write_row(ws, [label, float(agg.value.quantize(Decimal('0.0001'))), basis,
                            _tieout_text(tie_results), 'reconciled'])
        else:
            why = 'tie-out did not reconcile' if tie_failed else 'held input'
            _write_row(ws, [label, INCOMPLETE, basis, _tieout_text(tie_results), why], warn_cols=(1, 4))
    _nav_block(ws, cir, checks)


def _nav_block(ws, cir: CIR, checks: dict):
    """NAV as TWO labeled bases (the design ruling) — LP NAV (net of accrued carry) primary,
    Gross NAV (before carry) secondary. Not a fuzzy band and not a silently-picked point: the
    span between them is the one bounded soft input (the 'approx' carry). NAV is single-source
    (roll-forward; investments-at-FV never stated) — a PERMANENT label, shown in every row's
    state, so a reader never mistakes a well-founded-but-uncorroborated figure for a tied one.
    A held base (missing hard component or as-of cutoffs disagreeing) renders INCOMPLETE."""
    nav_rec = next((r for r in cir.records if r.domain == 'nav'), None)
    if nav_rec is None:
        return
    as_of = nav_rec.fields.get('as_of', '')
    leg = checks.get('fee_base_vs_nav')
    carry_ok = nav_rec.fields.get('carry_corroborated')
    carry_note = ('carry plausible (≤ceiling)' if carry_ok is True else
                  'carry EXCEEDS ceiling' if carry_ok is False else 'carry ceiling n/a')
    _write_row(ws, [])
    _write_row(ws, [f'NAV (as on {as_of}) — roll-forward, single-source (position uncorroborated); '
                    f'two bases bound the carry fork'], header=True)
    rows = (('lp_nav', 'NAV — LP (net of accrued carry) [primary]'),
            ('gross_nav', 'NAV — Gross (before carry) [secondary]'))
    for concept, label in rows:
        fig = nav_rec.fields.get(concept)
        if not isinstance(fig, Figure):
            continue
        basis = f'point_in_time @ {as_of}'
        tie = (leg.get('detail', '')[:60] if leg else '') or carry_note
        if fig.confirmed:
            _write_row(ws, [label, float(fig.value_cr.quantize(Decimal('0.0001'))), basis,
                            tie, f'derived — single-source ({carry_note})'])
        else:
            _write_row(ws, [label, INCOMPLETE, basis, tie,
                            fig.hold_reason or 'held input'], warn_cols=(1, 4))


def _lp_register(wb, cir: CIR):
    """The per-LP table — a new output shape (rows, not KPIs). Every LP figure
    carries its source cell (cite-evidence into the fund output, like Portfolio_KPI),
    and the hole-aware Σ row goes INCOMPLETE on any held figure (never a zeroed hole)."""
    lp = [r for r in cir.records if r.domain == 'lp_register' and r.entity_id != '(ambiguous)']
    if not lp:
        return
    ws = wb.create_sheet('LP_Register')
    _write_row(ws, ['LP / Investor', 'Type', 'Commitment (₹Cr)', 'Called cum (₹Cr)',
                    'Distributed cum (₹Cr)', 'Commit. cell', 'Called cell', 'Dist. cell', 'Notes'],
               header=True)
    concepts = ('commitment', 'called', 'distributed')
    for rec in lp:
        row = [rec.entity_id, rec.fields.get('lp_type', '')]
        warn, cells = [], []
        for i, c in enumerate(concepts, start=2):
            fig = rec.fields.get(c)
            val, is_hole = cell_for_figure(fig if isinstance(fig, Figure) else None)
            row.append(val)
            if isinstance(fig, Figure) and fig.held:
                warn.append(i)
            cells.append(f'{fig.provenance.sheet}!{fig.provenance.cell}' if isinstance(fig, Figure) else '')
        row += cells + [f'{len(warn)} held' if warn else '']
        _write_row(ws, row, warn_cols=tuple(warn))
    # hole-aware Σ (cumulative, NOT annualised) — INCOMPLETE if any LP figure held
    total, twarn = ['Σ (hole-aware)', ''], []
    for i, c in enumerate(concepts, start=2):
        agg = aggregate.aggregate([r.fields[c] for r in lp if isinstance(r.fields.get(c), Figure)],
                                  policy='as_reported')
        if agg.complete and agg.value is not None:
            total.append(float(agg.value.quantize(Decimal('0.0001'))))
        else:
            total.append(INCOMPLETE)
            twarn.append(i)
    _write_row(ws, [])
    _write_row(ws, total + ['', '', '', ''], warn_cols=tuple(twarn))


_FUND_TERM_ORDER = ['management_fee', 'carried_interest', 'hurdle_rate', 'catch_up',
                    'clawback_holdback', 'waterfall']
_FUND_TERM_QUALS = ('corroborated_by', 'base_source', 'catch_up_exists', 'waterfall_type')


def _fund_terms(wb, cir: CIR):
    """The LPA economic terms as BUNDLES — value + unit + base + phase + source cell +
    verdict (build rule #8: a term is never a bare number). An UNSPECIFIED base/phase is
    shown as UNSPECIFIED — a disclosed gap, never a silent default. A held term (unit
    unconfirmable, out of band, multi-source disagreement) is flagged, not shipped as fact."""
    recs = [r for r in cir.records if r.domain == 'fund_terms']
    if not recs:
        return
    ws = wb.create_sheet('Fund_Terms')
    _write_row(ws, ['TFAI — LPA Economic Terms'], header=True)
    _write_row(ws, ['a term is value + unit + base + phase; an UNSPECIFIED qualifier is disclosed, never assumed'])
    _write_row(ws, [])
    _write_row(ws, ['Term', 'Value', 'Unit', 'Base', 'Phase', 'Source cell', 'Verdict', 'Note'], header=True)
    for rec in recs:
        terms = {k: v for k, v in rec.fields.items() if isinstance(v, FundTerm)}
        for concept in _FUND_TERM_ORDER:
            t = terms.get(concept)
            if t is None:
                continue
            note = t.hold_reason or '; '.join(f'{k}={t.qualifiers[k]}' for k in _FUND_TERM_QUALS
                                              if k in t.qualifiers)
            warn = (1, 6) if t.verdict != 'confirmed' else ()          # flag value + verdict on a hold
            _write_row(ws, [concept.replace('_', ' '), t.display, t.unit, t.base, t.phase,
                            f'{t.provenance.sheet}!{t.provenance.cell}', t.verdict, note[:80]],
                       warn_cols=warn)


def _provenance(wb, cir: CIR):
    """Every figure traces to source→sheet→cell with its basis and state — the
    explainability bar: no emitted number is a mystery, no hold is a catch-all."""
    ws = wb.create_sheet('_Provenance')
    _write_row(ws, ['Entity', 'Concept', 'Value (₹Cr)', 'State', 'Basis', 'Months',
                    'Source file', 'Sheet', 'Cell', 'Reason'], header=True)
    for rec in cir.records:
        ent = rec.entity_id or rec.fields.get('company', '')
        for f in rec.figures():
            state = 'emitted' if f.confirmed else ('gap' if f.gap else 'held')
            p = f.provenance
            _write_row(ws, [ent, f.concept, '' if f.value_cr is None else float(f.value_cr),
                            state, f.basis or '', f.months if f.months is not None else '',
                            p.source_file, p.sheet, p.cell, (f.hold_reason or '')[:80]],
                       warn_cols=(3,) if state != 'emitted' else ())


def _disclosures(wb, cir: CIR, missing: List[dict]):
    ws = wb.create_sheet('_Disclosures')
    _write_row(ws, ['Kind', 'Entity / Concept', 'Detail'], header=True)
    for rec in cir.records:
        for f in rec.figures():
            if f.held:
                _write_row(ws, ['held', f'{rec.entity_id or ""}:{f.concept}', f.hold_reason], warn_cols=(0,))
            elif f.gap:
                _write_row(ws, ['gap', f'{rec.entity_id or ""}:{f.concept}', 'absent in source'])
    for d in cir.disclosures:
        _write_row(ws, [d.get('kind', 'disclosure'), d.get('entity', ''), d.get('detail', '')])
    for m in missing:
        _write_row(ws, [m.get('kind', 'investment_without_mis'), m.get('entity_id', ''), m.get('detail', '')], warn_cols=(0,))
    for h in cir.unresolved:
        _write_row(ws, ['held_file', h.get('file', ''), h.get('reason', '')], warn_cols=(0,))


def _reconciliation(wb, results: List[dict]):
    ws = wb.create_sheet('_Reconciliation')
    _write_row(ws, ['Check', 'Class', 'Status', 'LHS', 'RHS', 'Variance', 'Detail'], header=True)
    for r in results:
        warn = (2,) if r.get('status') in ('fail', 'indeterminate', 'variance') else ()
        _write_row(ws, [r.get('id', ''), r.get('class', ''), r.get('status', ''),
                        r.get('lhs', r.get('a', '')), r.get('rhs', r.get('b', '')),
                        r.get('variance', ''), r.get('detail', '')], warn_cols=warn)


def _rate_card(wb, rate_card):
    ws = wb.create_sheet('_RateCard')
    _write_row(ws, ['Currency', 'INR per unit', 'Rate date', 'Source', 'Type'], header=True)
    for row in rate_card.disclosure_rows():
        _write_row(ws, [row['currency'], row['inr_per_unit'], row['rate_date'], row['source'], row['source_type']])


def _audit(wb, audit: List[dict]):
    ws = wb.create_sheet('_Audit')
    _write_row(ws, ['File', 'Class', 'Entity', 'Status', 'Notes'], header=True)
    for a in audit:
        _write_row(ws, [a.get('file', ''), a.get('file_class', ''), a.get('entity', ''),
                        a.get('status', ''), a.get('notes', '')])
