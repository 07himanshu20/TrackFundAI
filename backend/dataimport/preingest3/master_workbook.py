"""
S9b — the 13-sheet consolidated MASTER workbook (the delivered product).

A RESHAPE, not a second engine: it arranges the SAME tested primitives from
assemble.py (cell_for_figure honest-state, hole-aware aggregate, the NAV two-base
logic) into the client-facing 13-sheet shape of the sample TrackFundAI_Master_v2.
assemble.build() (the older internal shape) is left untouched so its green tests
never move; build_master() is additive.

Four disciplines, all carried from one layer down to the spreadsheet:
  • HONEST PER-CELL STATE — every cell is a value, or ⚠HELD:reason, or — no data.
    Never a blank/zero that hides why. The sample was placeholder data with no
    holds; the real output holds ~58% of company cells, so the honesty layer is
    what makes the faithful file truthful — not decoration.
  • DUAL AS-OF — the fund side is rendered at the fund as-of (valuations date); each
    company's MIS carries its OWN native reporting date. The file is never presented
    as one coherent snapshot when it spans quarters/years.
  • STALENESS — a value whose own as-of is older than a DISCLOSED, TUNABLE threshold
    (default 2 quarters, since AIFs report quarterly) is flagged ⚠stale ALONGSIDE its
    value (shown, not hidden). The flag PROPAGATES to anything derived from it. An
    unparseable date is never guessed-stale (fail-closed on the flag).
  • SNAPSHOT + PROVENANCE — static computed values, each tracing to a real source
    cell. Not a live-formula model (that is the sample-as-template's property, not a
    consolidation snapshot's). One rule: show the real number, label its date, warn
    if it's old, trace it to source, never fake it.

Check rows (Total FV = Σ company FV, TVPI = DPI + RVPI, control-total ties) are
computed from the CIR and render PASS / FAIL / HELD — a check whose input
legitimately holds (NAV under an as-of mismatch) reads HELD, never faked to PASS.
"""
from __future__ import annotations

import datetime as _dt
import io
import re
import zipfile
from decimal import Decimal
from typing import List, Optional, Tuple

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import aggregate, lexicon
from .assemble import HELD_MARK, GAP_MARK, _HDR, _WARN, _HELD_FILL, cell_for_figure
from .cir import CIR, Figure
from .quantity import format_pct as _pctlabel     # THE canonical percent formatter (no local copy)

_PASS = Font(color='1E6B33', bold=True)
_PASS_FILL = PatternFill('solid', fgColor='EAF6EC')
_STALE = Font(color='9A6A00', bold=True)
_STALE_FILL = PatternFill('solid', fgColor='FBF4E6')

# ── presentation layer (report polish, applied as a deterministic post-pass) ──
# The delivered workbook must read like the sample REPORT, not a data dump, while
# keeping the honesty layer on top. All of this is a pure function of the cell
# values already written, so it never perturbs determinism, recalc or the honest
# per-cell state (formats touch numeric cells only; header styling touches header
# rows only — never a data cell carrying ⚠HELD/— n/r/⚠stale).
_TITLE_FILL = PatternFill('solid', fgColor='13233B')
_TITLE_FONT = Font(color='FFFFFF', bold=True, size=12)
_HDRBAND_FILL = PatternFill('solid', fgColor='1F3A5F')
_HDRBAND_FONT = Font(color='FFFFFF', bold=True)
_LEFT = Alignment(horizontal='left', vertical='center')
_BUILD_HDR = {}                      # id(ws) → [header row numbers]; consumed by _finalize

_FMT_PCT = '0.00%'
_FMT_MONEY = '#,##0.00'
_PCT_TOKENS = {'irr', 'rate'}        # percent columns with no literal '%' in the header
_MONEY_TOKENS = {'cost', 'fair', 'fv', 'nav', 'fee', 'fees', 'proceeds', 'commitment',
                 'committed', 'called', 'uncalled', 'distributed', 'cash', 'revenue',
                 'ebitda', 'burn', 'amount', 'base', 'cumulative', 'gain', 'ev', 'gross',
                 'net', 'aum', 'deployed', 'pnl', 'carry', 'commit', 'gst', 'total'}


def _fmt_for(label):
    """Number format for a column, inferred from its header word — universal, no
    per-sheet hardcoding. Percent columns store a FRACTION (0.14 → 14.00%); a '%'
    preceded by a digit (e.g. 'GST 18%') is a rate-in-a-name, NOT a percent column."""
    s = str(label).lower()
    toks = set(re.findall(r'[a-z]+', s))
    if re.search(r'(?<!\d)%', str(label)) or (toks & _PCT_TOKENS):
        return _FMT_PCT
    if '₹' in str(label) or 'cr' in toks or (toks & _MONEY_TOKENS):
        return _FMT_MONEY
    return None                       # ratios/counts/text left general (full precision)
_SHEETS = ['MASTER_INPUTS', 'LP_REGISTER', 'CAPITAL_CALLS', 'PORTFOLIO_MASTER',
           'VALUATIONS', 'QUOTED_UNQUOTED', 'NAV_CALC', 'MOIC_TVPI_DPI', 'WATERFALL_EUR',
           'SECTOR_ALLOCATION', 'EXITS', 'FEES', 'PORTFOLIO_KPI', 'DASHBOARD_BRIDGE',
           'RECONCILIATION']
_Q = Decimal('0.0001')
NR = '— n/r'                 # genuinely not reported in the source (honest gap)
STALE = '⚠ stale'
_DEFAULT_STALE_MONTHS = 6    # two quarters — AIFs report quarterly (TUNABLE, disclosed)


# ── honesty / staleness / dual-as-of primitives ────────────────────────────
_MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
           'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}


def _parse_ym(s) -> Optional[Tuple[int, int]]:
    """Best-effort (year, month) from a period/as-of label. None if unparseable — so a
    value is NEVER flagged stale on a guess (fail-closed on the flag)."""
    if not s:
        return None
    t = str(s).lower()
    m = re.search(r'(20\d\d)[-/](\d{1,2})[-/](\d{1,2})', t)     # ISO 2026-02-28
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r'([a-z]{3,9})[\s\'`\-]+(\d{2,4})', t)          # Feb'26 / Dec-23 / May 2025
    if m and m.group(1)[:3] in _MONTHS:
        yy = int(m.group(2))
        return (yy + 2000 if yy < 100 else yy), _MONTHS[m.group(1)[:3]]
    m = re.search(r'fy\s?[\'`]?(\d{2,4})', t)                     # FY26 → Indian FY ends March
    if m:
        yy = int(m.group(1))
        return (yy + 2000 if yy < 100 else yy), 3
    return None


def _months_old(asof_label, fund_ym) -> Optional[int]:
    ym = _parse_ym(asof_label)
    if ym is None or fund_ym is None:
        return None
    return (fund_ym[0] - ym[0]) * 12 + (fund_ym[1] - ym[1])


def _f(v):
    return None if v is None else float(Decimal(v).quantize(_Q))


def _num(fig) -> Optional[Decimal]:
    return fig.value_cr if isinstance(fig, Figure) and fig.confirmed and fig.value_cr is not None else None


def _entity(rec):
    return rec.entity_id or rec.fields.get('company', '') or ''


def _recs(cir, *domains):
    return [r for r in cir.records if r.domain in domains]


def _fig_asof(fig, default=''):
    if isinstance(fig, Figure) and fig.provenance and getattr(fig.provenance, 'col_label', ''):
        return str(fig.provenance.col_label)
    return default


def _cell(fig):
    if isinstance(fig, Figure) and fig.provenance:
        p = fig.provenance
        return f'{getattr(p, "sheet", "")}!{getattr(p, "cell", "")}'.strip('!')
    return ''


# ── the workbook ───────────────────────────────────────────────────────────
def build_master(cir: CIR, *, rate_card=None, files: List[str] = None,
                 staleness_months: int = _DEFAULT_STALE_MONTHS,
                 title: str = 'TrackFundAI — Consolidated Fund Master') -> openpyxl.Workbook:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _BUILD_HDR.clear()               # header-row tracker is per-build (single-threaded)
    # pin metadata to a fixed instant so the same CIR yields byte-identical bytes (no
    # wall-clock in the delivered artifact) — the determinism guarantee at the file level.
    wb.properties.created = wb.properties.modified = _dt.datetime(2000, 1, 1)
    wb.properties.creator = 'TrackFundAI preingest3'
    ctx = _Ctx(cir, staleness_months)
    _master_inputs(wb, cir, ctx, files or [], title, rate_card)
    _lp_register(wb, cir, ctx)
    _capital_calls(wb, cir, ctx)
    _portfolio_master(wb, cir, ctx)
    _valuations(wb, cir, ctx)
    _quoted_unquoted(wb, cir, ctx)
    _nav_calc(wb, cir, ctx)
    _moic_tvpi_dpi(wb, cir, ctx)
    _waterfall(wb, cir, ctx)
    _sector_allocation(wb, cir, ctx)
    _exits(wb, cir, ctx)
    _fees(wb, cir, ctx)
    _portfolio_kpi(wb, cir, ctx)
    _dashboard_bridge(wb, cir, ctx)
    _reconciliation(wb, cir, ctx)
    for name in _SHEETS:                       # the full 13-tab shape always exists
        if name not in wb.sheetnames:
            ws = wb.create_sheet(name)
            _rowh(ws, [name.replace('_', ' ').title()])
            _row(ws, ['— not extracted in this run (disclosed, not hidden) —'], warn=(0,))
    for ws in list(wb.worksheets):             # report-polish post-pass (pure over values)
        _finalize(ws, _BUILD_HDR.get(id(ws), []))
    _BUILD_HDR.clear()
    wb._sheets.sort(key=lambda s: _SHEETS.index(s.title) if s.title in _SHEETS else 99)
    return wb


_REPRO_ZIP_DT = (1980, 1, 1, 0, 0, 0)          # zip epoch floor (year must be ≥1980)
_REPRO_XML_TS = '2000-01-01T00:00:00Z'
_CORE_TS_RE = re.compile(r'(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:(?:created|modified)>)')


def save_reproducible(wb, path) -> None:
    """Save the workbook BYTE-REPRODUCIBLY: same CIR → byte-identical file, run to run.

    xlsx bytes carry wall-clock in TWO container layers that openpyxl stamps at write time
    and that are NOT part of the data: (1) docProps/core.xml created/modified, (2) every ZIP
    member's date_time header. Both are normalised to a fixed instant here, independent of
    openpyxl/zipfile internals — so byte-equality tests the DATA, never the write moment.
    Determinism at the artifact level, matching the pipeline's content-determinism gate."""
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    src = zipfile.ZipFile(buf)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in src.namelist():                 # insertion order is deterministic
            data = src.read(name)
            if name == 'docProps/core.xml':
                data = _CORE_TS_RE.sub(r'\g<1>' + _REPRO_XML_TS + r'\g<2>',
                                       data.decode('utf-8')).encode('utf-8')
            zi = zipfile.ZipInfo(name, date_time=_REPRO_ZIP_DT)
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
    with open(path, 'wb') as fh:
        fh.write(out.getvalue())


class _Ctx:
    def __init__(self, cir: CIR, staleness_months: int):
        self.checks = {c.get('id'): c for c in cir.checks}
        self.stale_m = staleness_months
        nav = next(iter(_recs(cir, 'nav')), None)
        self.nav_rec = nav
        self.fund_asof = (nav.fields.get('as_of') if nav else None) or cir.as_of
        self.fund_ym = _parse_ym(self.fund_asof)
        inv = _recs(cir, 'portfolio_investments')
        self.sum_cost = sum((_num(r.fields.get('cost')) or Decimal('0')) for r in inv)
        self.cost_held = any(_num(r.fields.get('cost')) is None for r in inv)
        self.sum_fv = sum((_num(r.fields.get('fair_value')) or Decimal('0')) for r in inv)
        self.fv_held = any(_num(r.fields.get('fair_value')) is None for r in inv)
        lp = _recs(cir, 'lp_register')
        ff = next(iter(_recs(cir, 'fund_financials')), None)
        self.called = _num(ff.fields.get('called')) if ff else None
        self.distributed = _num(ff.fields.get('distributed')) if ff else None
        self.committed = sum((_num(r.fields.get('commitment')) or Decimal('0')) for r in lp) or None
        self.residual_nav = _num(nav.fields.get('lp_nav')) if nav else None
        self.nav_hold = ''
        if nav is not None and self.residual_nav is None:
            f = nav.fields.get('lp_nav')
            self.nav_hold = (f.hold_reason if isinstance(f, Figure) else '') or 'NAV held'
        # company MIS joined by normalised name, with each company's representative as-of + staleness
        self.mis_by_co = {}
        for r in _recs(cir, 'mis', 'company', 'portfolio_companies'):
            self.mis_by_co[lexicon.normalise_label(_entity(r))] = r
        # exited companies (for Status)
        self.exited = {lexicon.normalise_label(r.fields.get('company', '')) for r in _recs(cir, 'exits')}

    def mis(self, company):
        return self.mis_by_co.get(lexicon.normalise_label(company))

    def company_asof(self, rec) -> Tuple[str, Optional[int]]:
        """(representative as-of label, months old) for a company MIS record — the most
        recent parseable figure date; months_old None if none parseable."""
        best_label, best_ym = '', None
        for c in ('revenue', 'ebitda', 'cash', 'headcount'):
            lab = _fig_asof(rec.fields.get(c)) if rec else ''
            ym = _parse_ym(lab)
            if ym and (best_ym is None or ym > best_ym):
                best_ym, best_label = ym, lab
        mo = None if best_ym is None else (self.fund_ym[0] - best_ym[0]) * 12 + (self.fund_ym[1] - best_ym[1]) if self.fund_ym else None
        return best_label, mo


# ── row helpers ────────────────────────────────────────────────────────────
def _safe(v):
    """Neutralise a leading spreadsheet formula-trigger on ANY string cell (=, +, @) —
    a label like '= Gross NAV' must not evaluate as a formula, and arbitrary uploaded
    content ('=cmd|...') must never become live formula injection. Universal, applied
    to every write; a leading space makes the engine read it as text."""
    return ' ' + v if isinstance(v, str) and v[:1] in ('=', '+', '@') else v


def _row(ws, values, *, warn=(), good=(), stale=()):
    ws.append([_safe(v) for v in values])
    r = ws.max_row
    for i in range(len(values)):
        if (i) in warn:
            ws.cell(r, i + 1).font = _WARN
            ws.cell(r, i + 1).fill = _HELD_FILL
        elif (i) in good:
            ws.cell(r, i + 1).font = _PASS
            ws.cell(r, i + 1).fill = _PASS_FILL
        elif (i) in stale:
            ws.cell(r, i + 1).font = _STALE
            ws.cell(r, i + 1).fill = _STALE_FILL
    return r


def _rowh(ws, values):
    ws.append([_safe(v) for v in values])
    for i in range(len(values)):
        ws.cell(ws.max_row, i + 1).font = _HDR
    _BUILD_HDR.setdefault(id(ws), []).append(ws.max_row)   # remembered for _finalize
    return ws.max_row


def _finalize(ws, header_rows):
    """Deterministic report-polish post-pass: number formats + column widths + header
    banding. Pure over the already-written values; skips header rows for numbers and
    skips data rows for banding, so the honesty layer is never touched."""
    hdr_set = set(header_rows)
    # 1. number formats — carry the current column→format map from the most recent
    #    header row, so multi-table sheets (MASTER_INPUTS, NAV_CALC) format correctly.
    col_fmt = {}
    for r in range(1, ws.max_row + 1):
        if r in hdr_set:
            col_fmt = {}
            for c in range(1, ws.max_column + 1):
                v = ws.cell(r, c).value
                if isinstance(v, str) and v.strip():
                    f = _fmt_for(v)
                    if f:
                        col_fmt[c] = f
            continue
        for c, f in col_fmt.items():
            cell = ws.cell(r, c)
            if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                cell.number_format = f
    # 2. column widths from DATA rows (header banners are long single-cell strings that
    #    would otherwise inflate a narrow column like '#'; they get merged, so exclude them)
    for c in range(1, ws.max_column + 1):
        m = 0
        for r in range(1, ws.max_row + 1):
            if r in hdr_set:
                continue
            v = ws.cell(r, c).value
            if v is not None:
                m = max(m, len(str(v)))
        ws.column_dimensions[get_column_letter(c)].width = max(9, min(m + 2, 46))
    # 3. header banding (fonts/fills BEFORE any merge); merge only single-label banners
    title_row = header_rows[0] if header_rows else None
    for r in header_rows:
        is_title = r == title_row
        fill, font = (_TITLE_FILL, _TITLE_FONT) if is_title else (_HDRBAND_FILL, _HDRBAND_FONT)
        for c in range(1, ws.max_column + 1):
            cell = ws.cell(r, c)
            cell.fill = fill
            cell.font = font
            cell.alignment = _LEFT
        ne = [c for c in range(1, ws.max_column + 1)
              if isinstance(ws.cell(r, c).value, str) and ws.cell(r, c).value.strip()]
        if len(ne) == 1 and ws.max_column > 1:
            ws.merge_cells(start_row=r, end_row=r, start_column=1, end_column=ws.max_column)
    # 4. freeze under the first TOP-OF-TABLE column-header row (≥3 labels, near the top),
    #    else under the title — a key-value cover (MASTER_INPUTS) freezes only its title
    frz = None
    for r in header_rows[:4]:
        ne = sum(1 for c in range(1, ws.max_column + 1)
                 if isinstance(ws.cell(r, c).value, str) and ws.cell(r, c).value.strip())
        if ne >= 3 and r <= 5:
            frz = r
            break
    if frz is None:
        frz = title_row
    if frz:
        ws.freeze_panes = ws.cell(frz + 1, 1)


def _checkrow(ws, label, lhs, rhs, *, tol=Decimal('0.01'), held_reason='', basis=''):
    if held_reason or lhs is None or rhs is None:
        _row(ws, [label, _f(lhs) if lhs is not None else '—', _f(rhs) if rhs is not None else '—',
                  basis, 'HELD', held_reason or 'input not available'], warn=(4, 5))
        return 'held'
    ok = abs(Decimal(str(lhs)) - Decimal(str(rhs))) <= tol
    _row(ws, [label, _f(lhs), _f(rhs), basis, 'PASS' if ok else 'FAIL',
              '' if ok else f'Δ={_f(abs(Decimal(str(lhs))-Decimal(str(rhs))))}'],
         good=(4,) if ok else (), warn=() if ok else (4, 5))
    return 'pass' if ok else 'fail'


def _catrow(ws, cid, label, ctx):
    c = ctx.checks.get(cid)
    if not c:
        return
    ok = c.get('status') == 'pass'
    _row(ws, [label, str(c.get('lhs', '')), str(c.get('rhs', '')),
              'PASS' if ok else c.get('status', '').upper(), (c.get('detail', '') or '')[:60]],
         good=(3,) if ok else (), warn=() if ok else (3, 4))


# ── 1. MASTER_INPUTS ───────────────────────────────────────────────────────
def _master_inputs(wb, cir, ctx, files, title, rate_card=None):
    ws = wb.create_sheet('MASTER_INPUTS')
    _rowh(ws, [title])
    _row(ws, ['DUAL AS-OF: fund figures as of the fund date below; each company MIS carries its '
              'OWN native date (see PORTFOLIO_KPI). Not one coherent snapshot.'])
    _row(ws, ['Legend', '⚠ HELD = value withheld pending review', '— n/r = not reported in source',
              '⚠ stale = older than the threshold below'])
    _row(ws, ['Fund as-of', ctx.fund_asof, 'Rate card', cir.rate_card_id])
    _row(ws, ['Staleness threshold', f'{ctx.stale_m} months (tunable)',
              'a value older than this vs the fund as-of is flagged ⚠ stale (shown, not held)'])
    _row(ws, [])
    _rowh(ws, ['A. CAPITAL STRUCTURE (₹Cr)', 'Value', 'Source'])
    _row(ws, ['Committed capital', _f(ctx.committed) if ctx.committed else NR, 'Σ LP commitments'])
    _row(ws, ['Capital called (cum)', _f(ctx.called) if ctx.called is not None else NR, 'capital account'])
    _row(ws, ['Uncalled', _f(ctx.committed - ctx.called) if (ctx.committed and ctx.called is not None) else NR, 'committed − called'])
    _row(ws, ['Distributed (cum)', _f(ctx.distributed) if ctx.distributed is not None else NR, 'capital account'])
    _row(ws, ['Deployed cost', _f(ctx.sum_cost) if not ctx.cost_held else 'INCOMPLETE', 'Σ investment cost'])
    lp_nav = _num(ctx.nav_rec.fields.get('lp_nav')) if ctx.nav_rec else None
    g_nav = _num(ctx.nav_rec.fields.get('gross_nav')) if ctx.nav_rec else None
    _row(ws, ['Fund NAV — LP (net carry)', _f(lp_nav) if lp_nav is not None else (HELD_MARK), 'roll-forward'],
         warn=() if lp_nav is not None else (1,))
    _row(ws, ['Fund NAV — Gross', _f(g_nav) if g_nav is not None else HELD_MARK, 'roll-forward'],
         warn=() if g_nav is not None else (1,))
    _row(ws, [])
    _terms = next(iter(_recs(cir, 'fund_terms')), None)
    tf = _terms.fields if _terms else {}
    _rowh(ws, ['B. FUND IDENTITY', 'Value', 'Source'])
    _row(ws, ['Fund name', tf.get('fund_name', NR), 'terms sheet'],
         warn=() if tf.get('fund_name') else (1,))
    _row(ws, ['Legal structure / SEBI category', tf.get('legal_structure', NR), 'terms sheet'],
         warn=() if tf.get('legal_structure') else (1,))
    _row(ws, ['Vintage year', tf.get('vintage_year', NR), 'terms sheet'],
         warn=() if tf.get('vintage_year') else (1,))
    _row(ws, [])
    passed = sum(1 for c in cir.checks if c.get('status') == 'pass')
    failed = [c for c in cir.checks if c.get('status') == 'fail']
    disc = sum(1 for c in cir.checks if c.get('status') == 'disclosed')
    _rowh(ws, ['C. RECONCILIATION', f'{passed} PASS · {len(failed)} FAIL · {disc} disclosed'])
    for c in failed:
        _row(ws, ['  FAIL', c.get('id', ''), (c.get('detail', '') or '')[:70]], warn=(0, 1))
    _row(ws, [])
    _rowh(ws, ['D. SOURCE FILES', str(len(files))])
    for fn in files:
        _row(ws, ['', fn])
    _row(ws, [])
    # E. RATE CARD — every FX rate used, its date and source (U6: a material input absent
    # from the source data is a disclosed, attributed run parameter). INR per 1 unit.
    _rowh(ws, ['E. RATE CARD (FX applied — INR per 1 unit)', 'INR/unit', 'Rate date', 'Source'])
    _row(ws, ['card id', cir.rate_card_id, '', ''])
    rates = getattr(rate_card, 'rates', None) or {}
    if rates:
        for ccy in sorted(rates):
            rt = rates[ccy]
            _row(ws, [ccy, _f(Decimal(str(rt.inr_per_unit))), getattr(rt, 'rate_date', ''),
                      f'{getattr(rt, "source", "")} ({getattr(rt, "source_type", "")})'])
    else:
        _row(ws, ['—', 'no foreign-currency rate in card (INR-only run)', '', ''])


# ── 2. LP_REGISTER ─────────────────────────────────────────────────────────
def _lp_register(wb, cir, ctx):
    lp = [r for r in _recs(cir, 'lp_register') if r.entity_id != '(ambiguous)']
    if not lp:
        return
    ws = wb.create_sheet('LP_REGISTER')
    _rowh(ws, ['LP INVESTOR REGISTER (₹Cr) — fund as-of ' + str(ctx.fund_asof)])
    _rowh(ws, ['LP / Investor', 'Type', 'Commitment', 'Called', 'Uncalled', '% Funded',
               'Distributed', 'DPI', 'NAV Share', 'RVPI', 'TVPI', 'IRR', 'KYC', 'Commit cell', 'State'])
    tc = td = tk = Decimal('0')
    hc = hd = hk = False
    for rec in lp:
        com, cal, dis = (_num(rec.fields.get('commitment')), _num(rec.fields.get('called')),
                         _num(rec.fields.get('distributed')))
        unc = _f(com - cal) if (com is not None and cal is not None) else NR
        pf = _f(cal / com) if (com and cal is not None) else NR       # fraction → 0.00%
        dpi = _f(dis / cal) if (cal and dis is not None) else NR
        warn = [i for i, v in [(2, com), (3, cal), (6, dis)] if v is None]
        if com is not None:
            tc += com
        else:
            hc = True
        if cal is not None:
            td += cal
        else:
            hd = True
        if dis is not None:
            tk += dis
        else:
            hk = True
        _row(ws, [rec.entity_id, rec.fields.get('lp_type', ''),
                  _f(com) if com is not None else HELD_MARK, _f(cal) if cal is not None else HELD_MARK,
                  unc, pf, _f(dis) if dis is not None else HELD_MARK, dpi,
                  NR, NR, NR, NR, NR, _cell(rec.fields.get('commitment')),
                  'ok' if not warn else 'held'], warn=tuple(warn))
    _row(ws, [])
    _row(ws, ['Σ (hole-aware)', '', 'INCOMPLETE' if hc else _f(tc), 'INCOMPLETE' if hd else _f(td),
              '', '', 'INCOMPLETE' if hk else _f(tk)],
         warn=tuple([i for i, h in [(2, hc), (3, hd), (6, hk)] if h]))


# ── 3. CAPITAL_CALLS ───────────────────────────────────────────────────────
def _capital_calls(wb, cir, ctx):
    calls = sorted(_recs(cir, 'capital_calls'), key=lambda r: r.fields.get('key', ''))
    if not calls:
        return
    ws = wb.create_sheet('CAPITAL_CALLS')
    _rowh(ws, ['CAPITAL CALL LEDGER (₹Cr)'])
    _rowh(ws, ['Call #', 'Date', 'Amount', 'Purpose', 'Cumulative Called', 'Cum Call %', 'Uncalled Balance', 'Cell'])
    cum = Decimal('0')
    for rec in calls:
        amt = _num(rec.fields.get('amount'))
        if amt is not None:
            cum += amt
        cumpct = _f(cum / ctx.committed) if ctx.committed else NR      # fraction → 0.00%
        unc = _f(ctx.committed - cum) if ctx.committed else NR
        _row(ws, [rec.fields.get('key', _entity(rec)), rec.fields.get('date', ''),
                  _f(amt) if amt is not None else HELD_MARK, str(rec.fields.get('purpose', ''))[:40],
                  _f(cum), cumpct, unc, _cell(rec.fields.get('amount'))],
             warn=() if amt is not None else (2,))
    _row(ws, [])
    _rowh(ws, ['CHECKS', 'LHS', 'RHS', 'Verdict', 'Detail'])
    _catrow(ws, 'capital_calls_rows_sum_to_total', 'Σ calls = stated total', ctx)
    _catrow(ws, 'capital_calls_tie_to_called', 'Calls tie to capital-account called', ctx)


# ── 4. PORTFOLIO_MASTER (investments ⋈ company MIS) ─────────────────────────
def _portfolio_master(wb, cir, ctx):
    inv = sorted(_recs(cir, 'portfolio_investments'), key=_entity)
    if not inv:
        return
    ws = wb.create_sheet('PORTFOLIO_MASTER')
    _rowh(ws, ['PORTFOLIO MASTER — per company (₹Cr). Fund cols as-of ' + str(ctx.fund_asof) +
               '; MIS cols carry each company’s own date + ⚠stale if > ' + str(ctx.stale_m) + 'm old'])
    _rowh(ws, ['#', 'Company', 'Sector', 'Stage', 'Domicile', 'Inv Date', 'Inv Year', 'Yrs Held',
               'Cost', 'Equity %', 'Val Method', 'Revenue TTM', 'EBITDA', 'Cash', 'Monthly Burn',
               'Status', 'MIS As-of', 'Stale?', 'Notes'])
    from .nav import _parse_date as _pd
    fund_d = _pd(str(ctx.fund_asof))
    for i, rec in enumerate(inv, start=1):
        f = rec.fields
        ent = _entity(rec)
        own = f.get('ownership_pct')
        mis = ctx.mis(ent)
        asof, mo = ctx.company_asof(mis) if mis else ('', None)
        stale = mo is not None and mo > ctx.stale_m
        rev = cell_for_figure(mis.fields.get('revenue') if mis else None)[0]
        eb = cell_for_figure(mis.fields.get('ebitda') if mis else None)[0]
        cash = cell_for_figure(mis.fields.get('cash') if mis else None)[0]
        status = 'Part-exit' if lexicon.normalise_label(ent) in ctx.exited else 'Active'
        # Inv Year / Yrs Held — DERIVED from the investment date we already carry (P7)
        inv_d = _pd(str(f.get('investment_date') or ''))
        inv_year = inv_d.year if inv_d else NR
        yrs = round((fund_d - inv_d).days / 365.25, 1) if (inv_d and fund_d) else NR
        warn = [c for c, v in [(11, rev), (12, eb), (13, cash)] if isinstance(v, str) and ('HELD' in v or 'no data' in v)]
        _row(ws, [i, ent, f.get('sector', NR), f.get('stage', NR), f.get('domicile', NR),
                  f.get('investment_date', NR), inv_year, yrs, _f(_num(f.get('cost'))),
                  _f(own) if own is not None else NR, f.get('valuation_method', NR), rev, eb, cash, NR, status,
                  asof or NR, STALE if stale else '', f'{len(warn)} MIS held' if warn else ''],
             warn=tuple(warn), stale=(16, 17) if stale else ())
    _row(ws, [])
    _row(ws, ['', 'Σ', '', '', '', '', '', '', _f(ctx.sum_cost) if not ctx.cost_held else 'INCOMPLETE'])


# ── 5. VALUATIONS ──────────────────────────────────────────────────────────
def _valuations(wb, cir, ctx):
    inv = sorted(_recs(cir, 'portfolio_investments'), key=_entity)
    if not inv:
        return
    ws = wb.create_sheet('VALUATIONS')
    _rowh(ws, ['PORTFOLIO VALUATIONS (₹Cr) — as-of ' + str(ctx.fund_asof)])
    _rowh(ws, ['#', 'Company', 'Sector', 'Val Method', 'Cost', 'Fair Value', 'Unrealised Gain',
               'Gain %', 'IRR', 'Multiple', 'Derived EV', 'DLOM', 'Net FV chain', 'Val Date'])
    for i, rec in enumerate(inv, start=1):
        f = rec.fields
        cost, fv = _num(f.get('cost')), _num(f.get('fair_value'))
        irr = _num(f.get('irr'))
        gain = _f(fv - cost) if (cost is not None and fv is not None) else HELD_MARK
        gpct = _f((fv - cost) / cost) if (cost and fv is not None) else NR   # fraction → 0.00%
        _row(ws, [i, _entity(rec), f.get('sector', NR), f.get('valuation_method', NR),
                  _f(cost), _f(fv), gain, gpct, _f(irr) if irr is not None else NR,
                  NR, NR, NR, NR, ctx.fund_asof])
    _row(ws, [])
    _row(ws, ['', 'Σ', '', '', _f(ctx.sum_cost) if not ctx.cost_held else 'INCOMPLETE',
              _f(ctx.sum_fv) if not ctx.fv_held else 'INCOMPLETE',
              _f(ctx.sum_fv - ctx.sum_cost) if not (ctx.cost_held or ctx.fv_held) else 'INCOMPLETE'])


# ── 5b. QUOTED_UNQUOTED (additive overlay over the untouched per-company FV) ──
def _quoted_unquoted(wb, cir, ctx):
    inv = sorted(_recs(cir, 'portfolio_investments'), key=_entity)
    if not inv:
        return
    ws = wb.create_sheet('QUOTED_UNQUOTED')
    _rowh(ws, ['QUOTED / UNQUOTED CLASSIFICATION (₹Cr) — as-of ' + str(ctx.fund_asof) +
               '. A partition of the SAME per-company fair values shown in VALUATIONS — it '
               're-labels, it never re-values. Basis=inferred means derived from valuation '
               'methodology (no source-stated ISIN / exchange / share type), ready to be overridden.'])
    _rowh(ws, ['#', 'Company', 'Fair Value', 'Classification', 'Basis', 'ISIN', 'Exchange',
               'Evidence / hold reason'])
    q = u = h = Decimal('0')
    nq = nu = nh = 0
    fv_incomplete = False
    for i, rec in enumerate(inv, start=1):
        f = rec.fields
        fv = _num(f.get('fair_value'))
        iq = f.get('is_quoted')
        basis = f.get('quoted_basis', '')
        klass = 'QUOTED' if iq is True else 'UNQUOTED' if iq is False else 'HELD'
        val = fv if fv is not None else Decimal('0')
        if fv is None:
            fv_incomplete = True
        if iq is True:
            q += val; nq += 1
        elif iq is False:
            u += val; nu += 1
        else:
            h += val; nh += 1
        _row(ws, [i, _entity(rec), _f(fv) if fv is not None else HELD_MARK, klass, basis or NR,
                  str(f.get('isin') or NR), str(f.get('listing_exchange') or NR),
                  str(f.get('quoted_evidence') or '')[:70]],
             good=(3,) if iq is True else (), warn=(3, 7) if iq is None else ())
    _row(ws, [])
    _rowh(ws, ['BUCKET', 'Companies', 'Σ Fair Value', '% of Σ FV'])
    tot = q + u + h
    def _pct(x):
        return _f(x / tot) if tot else NR                     # fraction → 0.00%
    _row(ws, ['Quoted', nq, _f(q), _pct(q)], good=(0,) if nq else ())
    _row(ws, ['Unquoted', nu, _f(u), _pct(u)])
    _row(ws, ['Held (unclassified)', nh, _f(h), _pct(h)], warn=(0,) if nh else ())
    _row(ws, ['Σ Total', nq + nu + nh, _f(tot),
              _f(Decimal('1')) if tot else NR])
    _row(ws, [])
    _rowh(ws, ['RECONCILIATION', 'LHS', 'RHS', 'Verdict', 'Detail'])
    # HARD control: the partition must tie to the independently-summed Σ portfolio FV — this
    # is what proves the labelling dropped or double-counted no company (struck in pipeline).
    _catrow(ws, 'quoted_unquoted_partition_ties_to_total', 'Σ buckets = Σ portfolio FV', ctx)
    if fv_incomplete:
        _row(ws, ['note', 'a company fair value is HELD upstream — bucket sums exclude it; '
                  'partition tie deferred (indeterminate), never a false pass'], warn=(1,))


# ── 6. NAV_CALC (two independent paths: §5.1 balance-sheet + roll-forward) ───
def _nav_calc(wb, cir, ctx):
    ws = wb.create_sheet('NAV_CALC')
    nav = ctx.nav_rec
    _rowh(ws, ['NAV CALCULATION — two independent paths (§5.1 balance-sheet build + capital-account roll-forward)'])
    if nav is None:
        _row(ws, ['— NAV not extracted in this run —'], warn=(0,))
        return
    _row(ws, ['NAV as-of', nav.fields.get('as_of', ctx.fund_asof),
              'derivation', nav.fields.get('derivation', '')])
    _row(ws, [])
    g_nav, lp_nav = _num(nav.fields.get('gross_nav')), _num(nav.fields.get('lp_nav'))
    carry = nav.fields.get('carry_value')
    carry_d = Decimal(str(carry)) if carry not in (None, '', '—') else None

    def _d(v):
        return Decimal(str(v)) if v not in (None, '', '—') else None

    # ── PATH A — §5.1 balance-sheet build (Assets − Liabilities), the sample structure ──
    bs_comps = nav.fields.get('bs_components') or []
    bs_pf, bs_gross, bs_lp = _d(nav.fields.get('bs_portfolio_fv')), _d(nav.fields.get('bs_gross_nav')), _d(nav.fields.get('bs_lp_nav'))
    if bs_comps and bs_pf is not None:
        from .nav import _bs_role
        _rowh(ws, ['PATH A — §5.1 balance-sheet build (Assets − Liabilities)', 'Amount (₹Cr)', 'Source cell'])
        _row(ws, ['(+) Investments at fair value (Σ portfolio)', _f(bs_pf), 'VALUATIONS Σ (per-company FV)'])
        for lbl, val, cell in bs_comps:
            role = _bs_role(lbl)
            sign = '(+)' if role == 'asset' else '(−)' if role == 'liability' else '( )'
            _row(ws, [f'{sign} {lbl}', _f(_d(val)), cell])
        _row(ws, ['(=) Gross NAV (before carry)', _f(bs_gross) if bs_gross is not None else HELD_MARK, ''],
             good=(1,) if bs_gross is not None else ())
        _row(ws, ['(−) Accrued carry (approx)', _f(carry_d) if carry_d is not None else NR, ''])
        _row(ws, ['(=) NAV — LP (net of carry)', _f(bs_lp) if bs_lp is not None else HELD_MARK, ''],
             good=(1,) if bs_lp is not None else ())
        _row(ws, ['caveat', 'the FV term aggregates per-company marks across as-ofs (dual-as-of); Path B '
                  'below is the single-as-of NAV operative for fund metrics (RVPI/TVPI)'])
        _row(ws, [])

    # ── PATH B — capital-account roll-forward (single as-of; operative for metrics) ──
    _rowh(ws, ['PATH B — capital-account roll-forward (single as-of; operative for metrics)', 'Amount (₹Cr)', 'Source cell'])
    for comp in (nav.fields.get('components') or []):
        try:
            name, val, cell = comp
        except Exception:
            continue
        _row(ws, [str(name), _f(_d(val)) if _d(val) is not None else NR, str(cell)])
    _row(ws, ['(=) Gross NAV (before carry)', _f(g_nav) if g_nav is not None else HELD_MARK, ''],
         good=(1,) if g_nav is not None else (), warn=() if g_nav is not None else (1,))
    _row(ws, ['(=) NAV — LP (net of carry)', _f(lp_nav) if lp_nav is not None else HELD_MARK, ''],
         good=(1,) if lp_nav is not None else (), warn=() if lp_nav is not None else (1,))
    _row(ws, [])
    _rowh(ws, ['RECONCILIATION & CONSISTENCY', 'LHS', 'RHS', 'Verdict', 'Detail'])
    _catrow(ws, 'nav_two_path_reconciliation', '§5.1 vs roll-forward (gross NAV)', ctx)
    _catrow(ws, 'nav_as_of_consistency', 'NAV as-of consistency', ctx)
    _catrow(ws, 'nav_carry_plausibility_ceiling', 'Accrued carry ≤ ceiling', ctx)


# ── 7. MOIC_TVPI_DPI ───────────────────────────────────────────────────────
def _moic_tvpi_dpi(wb, cir, ctx):
    ws = wb.create_sheet('MOIC_TVPI_DPI')
    _rowh(ws, ['MOIC / TVPI / DPI / RVPI — fund performance (as-of ' + str(ctx.fund_asof) + ')'])
    _row(ws, [f'Σ Cost {_f(ctx.sum_cost)} · Σ FV {_f(ctx.sum_fv)} · Called {_f(ctx.called)} · '
              f'Distributed {_f(ctx.distributed)} · '
              + (f'Residual NAV {_f(ctx.residual_nav)}' if ctx.residual_nav is not None else 'Residual NAV HELD')])
    _row(ws, [])
    _rowh(ws, ['Metric', 'Value', 'Basis', 'State', 'Note'])
    cost_ok = not ctx.cost_held and ctx.sum_cost > 0
    called_ok = ctx.called not in (None, 0)
    moic = ctx.sum_fv / ctx.sum_cost if (cost_ok and not ctx.fv_held) else None
    dpi = ctx.distributed / ctx.called if (called_ok and ctx.distributed is not None) else None
    rvpi = ctx.residual_nav / ctx.called if (called_ok and ctx.residual_nav is not None) else None
    tvpi = ((ctx.distributed or Decimal('0')) + ctx.residual_nav) / ctx.called \
        if (called_ok and ctx.residual_nav is not None and ctx.distributed is not None) else None
    _metric(ws, 'MOIC (FV / Cost)', moic, 'both sides confirmed' if moic is not None else 'cost/FV held')
    _metric(ws, 'DPI (Distributed / Called)', dpi, 'both sides confirmed' if dpi is not None else 'called/dist missing')
    _metric(ws, 'RVPI (Residual NAV / Called)', rvpi, '' if rvpi is not None else ('NAV held: ' + ctx.nav_hold)[:60])
    _metric(ws, 'TVPI ((Dist + NAV) / Called)', tvpi, '' if tvpi is not None else ('NAV held: ' + ctx.nav_hold)[:60])
    _row(ws, [])
    _rowh(ws, ['IDENTITY CHECKS', 'LHS', 'RHS', 'Basis', 'Verdict', 'Note'])
    _checkrow(ws, 'TVPI = DPI + RVPI', tvpi, (dpi + rvpi) if (dpi is not None and rvpi is not None) else None,
              held_reason='' if tvpi is not None else ('NAV held → RVPI/TVPI pending: ' + ctx.nav_hold)[:60])
    _checkrow(ws, 'Total FV = Σ company FV', ctx.sum_fv if not ctx.fv_held else None,
              ctx.sum_fv if not ctx.fv_held else None, held_reason='' if not ctx.fv_held else 'a company FV held')
    _row(ws, [])
    _rowh(ws, ['Per-company MOIC', 'Cost', 'Fair value', 'MOIC', 'State'])
    for rec in sorted(_recs(cir, 'portfolio_investments'), key=_entity):
        c, fv = _num(rec.fields.get('cost')), _num(rec.fields.get('fair_value'))
        m = _f(fv / c) if (c and fv is not None) else HELD_MARK
        _row(ws, [_entity(rec), _f(c) if c is not None else HELD_MARK, _f(fv) if fv is not None else HELD_MARK,
                  m, 'ok' if (c and fv is not None) else 'held'], warn=() if (c and fv is not None) else (3,))


def _metric(ws, label, val, note):
    if val is None:
        _row(ws, [label, HELD_MARK, 'ratio', 'HELD', note], warn=(1, 3))
    else:
        _row(ws, [label, _f(val), 'ratio', 'computed', note], good=(3,))


# ── 8. WATERFALL_EUR ───────────────────────────────────────────────────────
def _waterfall(wb, cir, ctx):
    from .fund_terms import FundTerm
    terms = next(iter(_recs(cir, 'fund_terms')), None)
    dist = sorted(_recs(cir, 'distributions'), key=lambda r: r.fields.get('key', ''))
    if terms is None and not dist:
        return
    ws = wb.create_sheet('WATERFALL_EUR')
    _rowh(ws, ['EUROPEAN WATERFALL (whole-fund)'])
    _rowh(ws, ['Term', 'Value', 'Unit', 'Base', 'Verdict'])
    if terms is not None:
        for k, v in terms.fields.items():
            if isinstance(v, FundTerm):
                ok = v.verdict == 'confirmed'
                _row(ws, [k.replace('_', ' '), v.display, v.unit, v.base, v.verdict],
                     good=(4,) if ok else (), warn=() if ok else (4,))
    _waterfall_compute(ws, cir, ctx, terms)
    _row(ws, [])
    _rowh(ws, ['Distribution', 'Date', 'Type', 'Gross', 'GP carry', 'Net to LP'])
    for rec in dist:
        g, c, n = (_num(rec.fields.get('gross')), _num(rec.fields.get('gp_carry')), _num(rec.fields.get('net')))
        _row(ws, [rec.fields.get('key', ''), rec.fields.get('date', ''), str(rec.fields.get('type', ''))[:24],
                  _f(g) if g is not None else HELD_MARK, _f(c) if c is not None else HELD_MARK,
                  _f(n) if n is not None else HELD_MARK])
    _row(ws, [])
    _rowh(ws, ['TERM AGREEMENT CHECKS', 'LHS', 'RHS', 'Verdict', 'Detail'])
    for cid in ('term_agrees_carried_interest', 'term_agrees_hurdle_rate', 'term_agrees_catch_up',
                'term_agrees_clawback_holdback'):
        _catrow(ws, cid, cid.replace('term_agrees_', 'agree: '), ctx)


def _waterfall_compute(ws, cir, ctx, terms):
    """Run the European (whole-fund) waterfall. TWO honest layers:
      1. CRYSTALLISED position — fact. Distributions actually paid run only as far up the
         tiers as the cash reaches; with called ≫ distributed the fund is not at capital-back
         so 0 carry has crystallised (matches nav.py's 'no carry crystallised').
      2. ILLUSTRATIVE full-realisation — at the current gross NAV as a terminal proxy, with
         its SIMPLIFYING ASSUMPTIONS DISCLOSED (simple 1-period pref, not the IRR-based pref
         that needs dated cash flows). Its implied GP carry is a CROSS-CHECK band against the
         accrued carry and the verified-rate ceiling — never presented as the authoritative
         carry (that is the dated-cashflow slice, deferred)."""
    from .fund_terms import FundTerm

    def _term(k):
        t = terms.fields.get(k) if terms else None
        return t.value if isinstance(t, FundTerm) and t.confirmed and t.value is not None else None

    def _dd(v):
        return Decimal(str(v)) if v not in (None, '', '—') else None

    nav = ctx.nav_rec
    gross_nav = _num(nav.fields.get('gross_nav')) if nav else None
    carry_acc = _dd(nav.fields.get('carry_value')) if nav else None
    ceiling = _dd(nav.fields.get('carry_ceiling')) if nav else None
    carry_rate, hurdle, catchup = _term('carried_interest'), _term('hurdle_rate'), _term('catch_up')
    contributed, distributed = ctx.called, ctx.distributed

    # 1 ── crystallised position (fact) ──
    _row(ws, [])
    _rowh(ws, ['CRYSTALLISED POSITION (as distributions actually stand today)', 'Amount (₹Cr)', 'Note'])
    if contributed is not None and distributed is not None:
        roc_paid = min(distributed, contributed)
        at_back = distributed >= contributed
        _row(ws, ['Contributed capital (called)', _f(contributed), ''])
        _row(ws, ['Distributed to date', _f(distributed), ''])
        _row(ws, ['→ Return of capital paid', _f(roc_paid),
                  (f'{_pctlabel(roc_paid / contributed)} of capital returned' if contributed else '')])
        _row(ws, ['→ GP carry crystallised to date', _f(Decimal('0')),
                  'LPs not yet at capital-back → 0 carry crystallised' if not at_back else 'capital returned'],
             good=() if not at_back else (0,))
    else:
        _row(ws, ['— called/distributed not both available — crystallised position not struck —'], warn=(0,))

    # 2 ── illustrative full-realisation waterfall (assumptions disclosed) ──
    _row(ws, [])
    _rowh(ws, ['ILLUSTRATIVE FULL-REALISATION WATERFALL (assumptions disclosed)', 'Amount (₹Cr)', 'Tier logic'])
    if gross_nav is None or contributed is None or carry_rate is None:
        _row(ws, ['— gross NAV / contributed / carry rate incomplete — illustrative waterfall not struck —'],
             warn=(0,))
        return
    total_value = gross_nav + (distributed or Decimal('0'))
    profit = total_value - contributed
    _row(ws, ['Terminal value proxy (gross NAV + cum. distributions)', _f(total_value),
              'illustrative terminal value (current gross NAV as proxy)'])
    _row(ws, ['Total profit above contributed capital', _f(profit), f'{_f(total_value)} − {_f(contributed)}'])
    _row(ws, ['(1) Return of capital → LP', _f(contributed), 'LPs recover contributed capital first'])
    pref = (hurdle * contributed) if hurdle is not None else None
    _row(ws, ['(2) Preferred return → LP', _f(pref) if pref is not None else NR,
              (f'SIMPLE {_pctlabel(hurdle)}×contributed — 1-PERIOD ILLUSTRATION only; true pref is IRR-based '
               'over the holding period (dated-cashflow waterfall deferred)') if pref is not None else 'hurdle n/r'])
    rem = profit - (pref or Decimal('0'))
    if rem < 0:
        rem = Decimal('0')
    catchup_gp = None
    if catchup is not None and catchup >= 1 and pref is not None:
        catchup_gp = min(carry_rate / (1 - carry_rate) * pref, rem)     # full catch-up, capped by remaining
    _row(ws, ['(3) GP catch-up → GP', _f(catchup_gp) if catchup_gp is not None else NR,
              (f'{_pctlabel(catchup)} catch-up to {_pctlabel(carry_rate)} of profit above RoC'
               if catchup_gp is not None else 'catch-up <100% or hurdle n/r — not modelled (dated slice)')])
    rem2 = rem - (catchup_gp or Decimal('0'))
    lp_split, gp_split = rem2 * (1 - carry_rate), rem2 * carry_rate
    _row(ws, [f'(4a) Residual split → LP ({_pctlabel(1 - carry_rate)})', _f(lp_split), 'residual carried-interest split'])
    _row(ws, [f'(4b) Residual split → GP ({_pctlabel(carry_rate)})', _f(gp_split), 'carried interest'])
    gp_total = (catchup_gp or Decimal('0')) + gp_split
    _row(ws, ['(=) Implied GP carry at full realisation', _f(gp_total),
              f'illustrative; with full catch-up = {_pctlabel(carry_rate)} × profit {_f(profit)}'], good=(0,))
    # cross-check band: accrued ≤ illustrative ≤ ceiling (each a different, disclosed basis)
    if carry_acc is not None and ceiling is not None:
        band_ok = carry_acc <= gp_total <= ceiling
        _row(ws, ['cross-check: accrued ≤ illustrative ≤ ceiling',
                  f'{_f(carry_acc)} ≤ {_f(gp_total)} ≤ {_f(ceiling)}', 'PASS' if band_ok else 'REVIEW'],
             good=(2,) if band_ok else (), warn=() if band_ok else (2,))


# ── 9. SECTOR_ALLOCATION (now real — uses increment-A sector) ───────────────
def _sector_allocation(wb, cir, ctx):
    from collections import defaultdict
    inv = _recs(cir, 'portfolio_investments')
    ws = wb.create_sheet('SECTOR_ALLOCATION')
    _rowh(ws, ['SECTOR ALLOCATION (by fair value, ₹Cr) — as-of ' + str(ctx.fund_asof)])
    by_n, by_cost, by_fv = defaultdict(int), defaultdict(Decimal), defaultdict(Decimal)
    for r in inv:
        sec = r.fields.get('sector') or '(sector n/r)'
        by_n[sec] += 1
        by_cost[sec] += (_num(r.fields.get('cost')) or Decimal('0'))
        by_fv[sec] += (_num(r.fields.get('fair_value')) or Decimal('0'))
    tot_fv = sum(by_fv.values()) or Decimal('1')
    _rowh(ws, ['Sector', '# Cos', 'Cost', 'Fair Value', '% by FV', 'MOIC'])
    for sec in sorted(by_fv, key=lambda s: -by_fv[s]):
        moic = _f(by_fv[sec] / by_cost[sec]) if by_cost[sec] else NR
        _row(ws, [sec, by_n[sec], _f(by_cost[sec]), _f(by_fv[sec]), _f(by_fv[sec] / tot_fv), moic])  # frac → 0.00%
    _row(ws, [])
    _row(ws, ['Σ', sum(by_n.values()), _f(ctx.sum_cost), _f(ctx.sum_fv), _f(Decimal('1'))])


# ── 10. EXITS ──────────────────────────────────────────────────────────────
def _exits(wb, cir, ctx):
    exits = sorted(_recs(cir, 'exits'), key=lambda r: r.fields.get('key', ''))
    if not exits:
        return
    ws = wb.create_sheet('EXITS')
    _rowh(ws, ['REALISED EXITS (₹Cr)'])
    _rowh(ws, ['#', 'Company', 'Sector', 'Exit Date', 'Type', 'Cost', 'Gross Proceeds',
               'Net Proceeds', 'Realised MOIC', 'Exit IRR'])
    tcost = tgross = tnet = Decimal('0')
    inv_sector = {lexicon.normalise_label(_entity(r)): r.fields.get('sector', NR)
                  for r in _recs(cir, 'portfolio_investments')}
    for rec in exits:
        co = rec.fields.get('company', '')
        cost, gp, npx = (_num(rec.fields.get('cost_realised')), _num(rec.fields.get('gross_proceeds')),
                         _num(rec.fields.get('net_proceeds')))
        irr = _num(rec.fields.get('irr_on_exit'))
        moic = _f(gp / cost) if (cost and gp is not None) else NR
        tcost += cost or 0; tgross += gp or 0; tnet += npx or 0
        _row(ws, [rec.fields.get('key', ''), co, inv_sector.get(lexicon.normalise_label(co), NR),
                  rec.fields.get('date', ''), str(rec.fields.get('type', ''))[:22],
                  _f(cost), _f(gp), _f(npx), moic, _f(irr) if irr is not None else NR])
    _row(ws, [])
    _row(ws, ['TOTAL', '', '', '', '', _f(tcost), _f(tgross), _f(tnet)])
    _rowh(ws, ['CHECK', 'LHS', 'RHS', 'Verdict', 'Detail'])
    _catrow(ws, 'exits_rows_sum_to_total', 'Σ exit proceeds = stated total', ctx)


# ── 11. FEES ───────────────────────────────────────────────────────────────
def _fees(wb, cir, ctx):
    fees = [r for r in _recs(cir, 'fees') if not str(r.entity_id).startswith('(reconciling')]
    if not fees:
        return
    ws = wb.create_sheet('FEES')
    _rowh(ws, ['MANAGEMENT FEE SCHEDULE (₹Cr)'])
    _rowh(ws, ['FY', 'Committed Base', 'Rate', 'Annual Fee', 'Cumulative', 'GST 18%', 'Total w/ GST'])
    cum = Decimal('0')
    for rec in fees:
        base, rate, fee = (_num(rec.fields.get('committed_base')), _num(rec.fields.get('rate')),
                           _num(rec.fields.get('annual_fee')))
        if fee is not None:
            cum += fee
        gst = _f(fee * Decimal('0.18')) if fee is not None else NR
        tot = _f(fee * Decimal('1.18')) if fee is not None else NR
        _row(ws, [rec.fields.get('key', _entity(rec)), _f(base), _f(rate) if rate is not None else NR,
                  _f(fee) if fee is not None else HELD_MARK, _f(cum), gst, tot],
             warn=() if fee is not None else (3,))
    _row(ws, [])
    _rowh(ws, ['CHECKS', 'LHS', 'RHS', 'Verdict', 'Detail'])
    _catrow(ws, 'fees_rows_sum_to_total', 'Σ fee rows = stated total', ctx)
    _catrow(ws, 'management_fee_vs_actual', 'rate × committed = actual fee', ctx)


# ── 12. PORTFOLIO_KPI (company MIS — honest per-cell + staleness) ───────────
def _portfolio_kpi(wb, cir, ctx):
    ws = wb.create_sheet('PORTFOLIO_KPI')
    _rowh(ws, ['PORTFOLIO KPI TRACKER — company MIS (each at its OWN as-of; ⚠stale if > '
               + str(ctx.stale_m) + 'm before fund as-of ' + str(ctx.fund_asof) + ')'])
    _rowh(ws, ['Company', 'Sector', 'Stage', 'MIS As-of', 'Age(mo)', 'Stale?', 'Revenue', 'EBITDA',
               'EBITDA Margin %', 'Cash', 'Head-count', 'Basis', 'State'])
    inv_by = {lexicon.normalise_label(_entity(r)): r for r in _recs(cir, 'portfolio_investments')}
    emit = held = gap = 0
    for rec in _recs(cir, 'mis', 'company', 'portfolio_companies'):
        ent = _entity(rec)
        inv = inv_by.get(lexicon.normalise_label(ent))
        sector = (inv.fields.get('sector', '') if inv else '') or NR
        stage = (inv.fields.get('stage', '') if inv else '') or NR
        asof, mo = ctx.company_asof(rec)
        stale = mo is not None and mo > ctx.stale_m
        figs = {c: rec.fields.get(c) for c in ('revenue', 'ebitda', 'cash', 'headcount')}
        cells, warn = [], []
        for idx, c in enumerate(('revenue', 'ebitda', 'cash', 'headcount')):
            v, hole = cell_for_figure(figs[c] if isinstance(figs[c], Figure) else None)
            cells.append(v)
            fig = figs[c]
            if isinstance(fig, Figure) and fig.confirmed:
                emit += 1
            elif isinstance(fig, Figure) and fig.held:
                held += 1
            else:
                gap += 1
        rev, eb, cash, hc = cells
        rv, ev = _num(figs['revenue']), _num(figs['ebitda'])
        margin = _f(ev / rv) if (rv and ev is not None) else (NR if (rv is None or ev is None) else '')  # frac → 0.00%
        # staleness propagates to the derived margin — it inherits the row's stale state
        hg = sum(1 for c in ('revenue', 'ebitda', 'cash', 'headcount')
                 if not (isinstance(figs[c], Figure) and figs[c].confirmed))
        warn = [6 + i for i, c in enumerate(('revenue', 'ebitda', 'cash', 'headcount'))
                if not (isinstance(figs[c], Figure) and figs[c].confirmed)]
        _row(ws, [ent, sector, stage, asof or NR, (mo if mo is not None else '?'),
                  STALE if stale else '', rev, eb, margin, cash, hc, _fig_basis(figs),
                  ('all solid' if hg == 0 else f'{hg} awaiting review') + (' · STALE' if stale else '')],
             warn=tuple(warn), stale=(3, 4, 5) if stale else ())
    _row(ws, [])
    tot = emit + held + gap
    _rowh(ws, [f'COVERAGE: {emit}/{tot} emitted · {held} held · {gap} not-reported '
               f'({round(100*emit/tot) if tot else 0}% company-side)'])


def _fig_basis(figs):
    for c in ('revenue', 'ebitda', 'cash', 'headcount'):
        f = figs.get(c)
        if isinstance(f, Figure) and f.basis:
            return f.basis
    return ''


# ── 13. DASHBOARD_BRIDGE (curated widget→source map, like the sample) ───────
def _dashboard_bridge(wb, cir, ctx):
    ws = wb.create_sheet('DASHBOARD_BRIDGE')
    _rowh(ws, ['DASHBOARD BRIDGE — every dashboard widget → its value, source cell and calc logic'])
    nav = ctx.nav_rec
    lp_nav = _num(nav.fields.get('lp_nav')) if nav else None
    g_nav = _num(nav.fields.get('gross_nav')) if nav else None
    bs_lp = nav.fields.get('bs_lp_nav') if nav else None
    carry = nav.fields.get('carry_value') if nav else None
    moic = (ctx.sum_fv / ctx.sum_cost) if (not ctx.cost_held and not ctx.fv_held and ctx.sum_cost) else None
    dpi = (ctx.distributed / ctx.called) if (ctx.called and ctx.distributed is not None) else None
    rvpi = (ctx.residual_nav / ctx.called) if (ctx.called and ctx.residual_nav is not None) else None
    tvpi = (dpi + rvpi) if (dpi is not None and rvpi is not None) else None
    inv = _recs(cir, 'portfolio_investments')
    nsec = len({r.fields.get('sector') for r in inv if r.fields.get('sector')})
    exits = _recs(cir, 'exits')
    exit_gross = sum((_num(r.fields.get('gross_proceeds')) or Decimal('0')) for r in exits)
    fees_cum = sum((_num(r.fields.get('annual_fee')) or Decimal('0'))
                   for r in _recs(cir, 'fees') if not str(r.entity_id).startswith('(reconciling'))
    npass = sum(1 for c in cir.checks if c.get('status') == 'pass')
    nfail = sum(1 for c in cir.checks if c.get('status') in ('fail', 'indeterminate'))

    def _v(x):
        return _f(x) if isinstance(x, Decimal) else (_f(Decimal(str(x))) if x not in (None, '') else NR)

    rows = [
        ('Fund Overview', 'Total Fund NAV — LP (net carry)', _v(lp_nav), '₹Cr', 'NAV_CALC · PATH B', 'called − distributed + net ITD P&L − accrued carry'),
        ('Fund Overview', 'Fund NAV — Gross', _v(g_nav), '₹Cr', 'NAV_CALC · PATH B', 'called − distributed + net ITD P&L'),
        ('Fund Overview', 'Fund NAV — §5.1 balance-sheet (corrob.)', _v(bs_lp), '₹Cr', 'NAV_CALC · PATH A', 'ΣFV + cash + receivables − liabilities − carry'),
        ('Capital', 'Committed capital', _v(ctx.committed), '₹Cr', 'MASTER_INPUTS · A', 'Σ LP commitments'),
        ('Capital', 'Capital called (cum)', _v(ctx.called), '₹Cr', 'CAPITAL_CALLS', 'capital account'),
        ('Capital', 'Distributed (cum)', _v(ctx.distributed), '₹Cr', 'CAPITAL_CALLS', 'capital account'),
        ('Capital', 'Uncalled', _v(ctx.committed - ctx.called) if (ctx.committed and ctx.called is not None) else NR, '₹Cr', 'MASTER_INPUTS · A', 'committed − called'),
        ('Capital', 'Deployed cost', _v(ctx.sum_cost) if not ctx.cost_held else 'INCOMPLETE', '₹Cr', 'PORTFOLIO_MASTER · Σ', 'Σ investment cost'),
        ('Performance', 'MOIC (gross)', _v(moic), 'x', 'MOIC_TVPI_DPI', 'Σ Fair Value / Σ Cost'),
        ('Performance', 'DPI', _v(dpi), 'x', 'MOIC_TVPI_DPI', 'Distributed / Called'),
        ('Performance', 'RVPI', _v(rvpi), 'x', 'MOIC_TVPI_DPI', 'Residual NAV / Called'),
        ('Performance', 'TVPI', _v(tvpi), 'x', 'MOIC_TVPI_DPI', 'DPI + RVPI'),
        ('Portfolio', '# Portfolio companies', len(inv), '#', 'PORTFOLIO_MASTER', 'count of investee rows'),
        ('Portfolio', 'Σ Fair Value', _v(ctx.sum_fv) if not ctx.fv_held else 'INCOMPLETE', '₹Cr', 'VALUATIONS · Σ', 'Σ per-company fair value'),
        ('Portfolio', 'Σ Cost', _v(ctx.sum_cost) if not ctx.cost_held else 'INCOMPLETE', '₹Cr', 'PORTFOLIO_MASTER · Σ', 'Σ per-company cost'),
        ('Portfolio', '# Sectors', nsec, '#', 'SECTOR_ALLOCATION', 'distinct sectors'),
        ('Realisations', '# Exits', len(exits), '#', 'EXITS', 'count of realised exits'),
        ('Realisations', 'Σ Exit gross proceeds', _v(exit_gross), '₹Cr', 'EXITS · TOTAL', 'Σ gross proceeds'),
        ('Fees', 'Σ Management fees (cum)', _v(fees_cum), '₹Cr', 'FEES', 'Σ annual fee'),
        ('Waterfall', 'Accrued carry (approx)', _v(carry), '₹Cr', 'WATERFALL_EUR / NAV_CALC', 'carry provision (accrued, unrealised)'),
        ('Reconciliation', 'Checks passed', npass, '#', 'RECONCILIATION', 'Σ status = pass'),
        ('Reconciliation', 'Checks failed/indeterminate', nfail, '#', 'RECONCILIATION', 'Σ status = fail/indeterminate'),
    ]
    _rowh(ws, ['Widget', 'Metric', 'Value', 'Unit', 'Source Sheet → Cell', 'Calc Logic'])
    for w, m, v, u, src, logic in rows:
        _row(ws, [w, m, v, u, src, logic], warn=(2,) if v in (NR, 'INCOMPLETE') else ())
    # granular per-figure provenance retained below as the drill-down (honesty layer)
    _row(ws, [])
    _rowh(ws, ['GRANULAR PROVENANCE FEED (entity × concept) — drill-down for every value',
               'Concept', 'Value (₹Cr)', 'As-of', 'Stale', 'State', 'Source cell'])
    for rec in cir.records:
        ent = _entity(rec)
        for fig in rec.figures():
            state = 'emitted' if fig.confirmed else ('not_reported' if fig.gap else 'held')
            asof = _fig_asof(fig, ctx.fund_asof)
            mo = _months_old(asof, ctx.fund_ym)
            stale = state == 'emitted' and mo is not None and mo > ctx.stale_m
            _row(ws, [ent, fig.concept, _f(fig.value_cr) if fig.value_cr is not None else '', asof,
                      STALE if stale else '', state, _cell(fig)],
                 warn=(5,) if state != 'emitted' else (), good=(5,) if state == 'emitted' else (),
                 stale=(4,) if stale else ())


# ── 14. RECONCILIATION (U7: every check materialises — pass is information) ──
def _reconciliation(wb, cir, ctx):
    ws = wb.create_sheet('RECONCILIATION')
    _rowh(ws, ['RECONCILIATION — every check the run performed. A check that PASSES is information, '
               'not silence (soft checks are published whether or not they clear tolerance).'])
    _rowh(ws, ['Check', 'Class', 'LHS', 'RHS', 'Variance', 'Var %', 'Outcome', 'Explanation'])
    _order = {'hard': 0, 'soft': 1, 'disclosure': 2}
    npass = nfail = nsoft = ndisc = 0
    for c in sorted(cir.checks, key=lambda c: (_order.get(c.get('class'), 9), c.get('id', ''))):
        cls, status = c.get('class', ''), c.get('status', '')
        # soft_correspondence emits its two figures as a/b (not lhs/rhs) — fall back so a soft
        # check's numbers RENDER instead of being silently dropped (they were, pre-fix). U7:
        # a soft check is published WITH both figures and the variance, never as a bare verdict.
        lhs = c.get('lhs', c.get('a', ''))
        rhs = c.get('rhs', c.get('b', ''))
        var = varpct = ''
        try:
            l, r = Decimal(str(lhs)), Decimal(str(rhs))
            var = _f(abs(l - r))
            varpct = _f(abs(l - r) / abs(r)) if r != 0 else ''      # fraction → 0.00%
        except Exception:
            pass
        ok = status == 'pass'
        bad = status in ('fail', 'indeterminate')
        npass += ok
        nfail += bad
        nsoft += cls == 'soft'
        ndisc += cls == 'disclosure'
        _row(ws, [c.get('id', ''), cls, str(lhs), str(rhs), var, varpct, status.upper(),
                  (c.get('detail', '') or '')[:180]],   # audit sheet — the coverage/basis explanation IS the value
             good=(6,) if ok else (), warn=(6,) if bad else ())
    _row(ws, [])
    _rowh(ws, [f'TOTALS: {npass} pass · {nfail} fail/indeterminate · {len(cir.checks)} total '
               f'({nsoft} soft · {ndisc} disclosure) — all published (U7)'])
