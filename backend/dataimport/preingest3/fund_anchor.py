"""
Fund-file ANCHOR — the whole-company magnitude that resolves each company MIS
statement's monetary frame (units.resolve_monetary_frame). Offline, deterministic.

The fund's investment schedule holds, per portfolio company, the fund's COST
(possibly split across tranche rows), its OWNERSHIP %, its FAIR VALUE, and its
DOMICILE. From those the anchor is built (units.whole_company_anchor): cost ÷
ownership as an implied ENTRY valuation — an order-of-magnitude ruler for the
company's operating figures, NOT a value to match. The domicile it also yields
feeds the currency geography-gate.

Universality (no hardcoded sheet/column names, per CLAUDE.md):
  • every column is found by lexicon fuzzy match on the sheet's header row
    (company / cost / ownership_pct / fair_value / domicile) — never a fixed
    header spelling or cell address;
  • a schedule sheet is ANY sheet exposing a company axis plus ≥1 of those
    attributes, so a tranche sheet, a portfolio sheet and a valuation sheet are
    all read and merged;
  • within a sheet a company's rows are SUMMED (tranche split → total cost);
    ACROSS sheets the per-sheet sums are MAX-ed, so a total restated in a second
    sheet (valuation 'cost for ref') never double-counts the tranche total.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from . import concept_nets
from . import lexicon
from . import quoted_unquoted
from .concept_identity import ConceptKey
from .namematch import similarity
from .profiler import _cell_type, profile_file
from .quantity import to_decimal
from .units import expected_currency, whole_company_anchor

# a schedule sheet must expose the company axis + at least one of these.
# 'irr' is the stated per-company IRR%(Gross) column (Valuation working) — a company
# performance figure carried on the same schedule rows; company IRR is GROSS by
# convention (§8.3: fees/carry are fund-level), so it is tagged gross downstream and
# tied to a tranche-computed XIRR before it is trusted (transcription-vs-truth).
_ATTRS = ('cost', 'ownership_pct', 'fair_value', 'domicile', 'irr')
# descriptive (text) attributes — extracted ADDITIVELY on the header that _ATTRS
# already selected, so they never change WHICH sheet/header/company rows are chosen
# (zero blast radius on the money anchor). Values are plain strings.
_DESC_ATTRS = ('sector', 'stage', 'investment_date', 'instrument', 'valuation_method')
# listing signals (ISIN / exchange / share type) for the quoted-unquoted overlay — located
# on the ALREADY-CHOSEN header via the isolated quoted_unquoted.locate_listing_signals (its
# own richer synonym set + ISIN checksum gate), so like _DESC_ATTRS they are pure text
# enrichment: never gating which sheet/header/rows are chosen, never touching the money
# anchor. Absent in every file we have today (a no-op); the capability is READY the day a
# real listed-holdings fund arrives. The column-LOCATION is spec-tested-until-field-tested.
_LISTING_ATTRS = ('isin', 'listing_exchange', 'share_type')
_MATCH_FLOOR = 0.5      # min name similarity to attach an MIS file to an anchor
# a schedule's total/subtotal row is NOT a portfolio company — reading it as one
# double-counts every headline (Total Deployed Cost 448 → 896). Universal junk
# labels, not fund-specific (CLAUDE.md _is_junk_row rule).
_JUNK_NAMES = {'total', 'grand total', 'subtotal', 'sub total', 'sub-total', 'sum',
               'portfolio total', 'grand-total', 'totals'}


def _is_junk_company(name: str) -> bool:
    n = ' '.join(str(name or '').strip().lower().split())
    return (not n) or n in _JUNK_NAMES or n.startswith('total ') or n.startswith('grand total')


@dataclass
class CompanyAnchor:
    company: str
    domicile: Optional[str] = None
    domicile_conflict: bool = False            # schedules disagreed on domicile in a way that implies
                                               # DIFFERENT currencies → domicile withheld (None) so U6
                                               # fail-closes the currency, and the pipeline discloses it
    cost_cr: Optional[Decimal] = None
    ownership_frac: Optional[float] = None
    fair_value_cr: Optional[Decimal] = None
    irr_gross: Optional[Decimal] = None        # stated IRR%(Gross) per company, as a fraction
    anchor_cr: Optional[Decimal] = None
    # descriptive attributes (text) — enrich the portfolio master; never influence
    # the money anchor or the schedule-header SELECTION (added additively).
    sector: Optional[str] = None
    stage: Optional[str] = None
    investment_date: Optional[str] = None
    instrument: Optional[str] = None
    valuation_method: Optional[str] = None
    # listing signals — populated only when a source carries them (none do today)
    isin: Optional[str] = None
    listing_exchange: Optional[str] = None
    share_type: Optional[str] = None
    sources: List[str] = field(default_factory=list)


def _find_col(rows, header_row: int, concept: str) -> Optional[int]:
    """The column in `header_row` whose text denotes `concept` (lexicon whole-word
    match). Exact-strength match wins over a mere containment match."""
    contained = None
    for c, v in enumerate(rows[header_row]):
        if _cell_type(v) != 'text':
            continue
        strength = lexicon.match_strength(v, concept)
        if strength == 'exact':
            return c
        if strength == 'contains' and contained is None:
            contained = c
    return contained


def _ownership_frac(v) -> Optional[float]:
    """A stake as a fraction in (0, 1]: 0.18 stays 0.18; 18 or '18%' → 0.18; 100 → 1.0. A value that
    CANNOT be a valid ownership fraction — ≤0, or >100% after normalisation — is REJECTED as None.
    UNIVERSAL domain invariant: no entity owns >100% of another, on any file or format, so a normalised
    ownership >1 is always a misparse (e.g. an 'FV of holding' column mis-read as ownership → 148%), never
    a real stake. Rejecting it keeps the whole-company anchor (cost ÷ ownership) sane, and — because the
    merge then never captures the bogus value — removes the order-dependence it caused."""
    d = to_decimal(str(v).replace('%', '') if isinstance(v, str) else v)
    if d is None or d <= 0:
        return None
    f = float(d)
    frac = f / 100.0 if f > 1 else f
    return frac if 0 < frac <= 1 else None


def _ledger_row_indices(rows) -> set:
    """Row indices that belong to a LEDGER region (a long, homogeneous, dated +
    categorical transaction listing — an LP register, a capital-call log, a SAP
    GL feeder). These are NOT portfolio companies and must never be mined as such;
    skipping them is what stops the row-explosion (CPM→6803, CSS→3299 phantoms).
    Uses the same statement-vs-ledger discriminator the model path relies on, so
    the rule is one shared definition, not a second heuristic."""
    from . import statements
    idx = set()
    for st in statements.segment_regions('', rows):
        if st.kind == statements.LEDGER:
            idx.update(range(st.start_row, st.end_row + 1))
    return idx


def _schedule_rows_from_sheet(rows) -> Optional[List[dict]]:
    """If this sheet is an investment schedule, return one dict per company row
    with whatever attributes it carries; else None. LEDGER regions are excluded —
    a portfolio investment schedule is a SHORT, heterogeneous statement, never a
    thousand-row transaction listing."""
    # the header row is the one that names a company column plus ≥1 attribute
    best_hr, best_cols = None, None
    for hr in range(min(30, len(rows))):
        comp_c = _find_col(rows, hr, 'company')
        if comp_c is None:
            continue
        cols = {a: _find_col(rows, hr, a) for a in _ATTRS}
        present = {a: c for a, c in cols.items() if c is not None}
        if present and (best_cols is None or len(present) > len(best_cols) - 1):
            best_hr, best_cols = hr, {'company': comp_c, **present}
    if best_hr is None:
        return None
    # descriptive columns are located on the ALREADY-CHOSEN header (they play no part
    # in selecting it) — enrichment only, never a change to the money extraction.
    for a in _DESC_ATTRS:
        dc = _find_col(rows, best_hr, a)
        if dc is not None:
            best_cols[a] = dc
    # listing signals via the isolated locator (own synonym set) — same chosen header,
    # same enrichment-only contract; empty for every file we have today.
    for a, dc in quoted_unquoted.locate_listing_signals(rows[best_hr]).items():
        best_cols[a] = dc
    ledger_rows = _ledger_row_indices(rows)
    out = []
    for r in range(best_hr + 1, len(rows)):
        if r in ledger_rows:                # transaction/register row, not a company
            continue
        cc = best_cols['company']
        if cc >= len(rows[r]) or _cell_type(rows[r][cc]) != 'text':
            continue
        name = str(rows[r][cc]).strip()
        if _is_junk_company(name):          # skip 'Total' / subtotal rows — not companies
            continue
        rec = {'company': name}
        for a in _ATTRS:
            c = best_cols.get(a)
            if c is None or c >= len(rows[r]):
                continue
            v = rows[r][c]
            if a == 'ownership_pct':
                rec[a] = _ownership_frac(v)
            elif a == 'domicile':
                rec[a] = str(v).strip() if _cell_type(v) == 'text' else None
            else:  # cost / fair_value → numeric
                rec[a] = to_decimal(v)
        for a in _DESC_ATTRS + _LISTING_ATTRS:   # text enrichment (never gates a row)
            c = best_cols.get(a)
            if c is None or c >= len(rows[r]):
                continue
            v = rows[r][c]
            if v is None or v == '':
                continue
            if a == 'investment_date':
                rec[a] = str(v).strip()     # a date or a text date — keep as displayed
            elif _cell_type(v) == 'text':
                rec[a] = str(v).strip()     # sector / stage / instrument / listing signals are text
        # A portfolio investment row MUST carry a monetary/ownership attribute
        # (cost, fair value, or ownership). A bare name with no such value is a
        # metric label ('Revenue'), a GL line, or a heading — NOT an investment.
        # This is the root guard that makes row-explosion impossible even if a
        # company MIS is ever misrouted into the anchor builder.
        if (rec.get('cost') is not None or rec.get('fair_value') is not None
                or rec.get('ownership_pct') is not None):
            out.append(rec)
    return out or None


def _resolve_ownership(cands: List[tuple]) -> Optional[float]:
    """cands = [(value, from_cost_sheet)]. Agree → use. Disagree → prefer a value stated on a
    schedule that also carries COST (the deployment/Investments schedule is authoritative for the
    fund's stake), else the deterministic minimum. Order-INDEPENDENT (no first-seen).

    DETECTION (do the stated stakes agree?) is routed through the one mechanism (concept_nets Net 1) so
    it can't drift from the general net; the prefer-cost-sheet RESOLUTION on a disagreement stays the
    declared behaviour (spec §0.1). Byte-identical to the prior len(set)==1 check (tol_abs=0 → exact).

    3a note: the ConceptKey stays base-AUTHORITATIVE ('ownership_pct'), NOT decompose(label) — these net
    sites receive bare numbers grouped by a concept already resolved upstream, so no per-value label reaches
    here for decompose to read a qualifier from; decompose-at-resolution's danger (gross/net) lives at
    fund_terms, not here. TRIGGER to revisit: a real cross-sheet qualifier divergence at this merge."""
    if not cands:
        return None
    vals = sorted({v for v, _ in cands})
    verdict = concept_nets.net1_collision(
        [concept_nets.Observation(ConceptKey('ownership_pct'), value=Decimal(str(v))) for v in vals],
        tol_abs=Decimal('0'))
    if verdict.outcome != concept_nets.COLLIDE:          # one stated stake (or all equal) → use it
        return vals[0]
    from_cost = sorted({v for v, c in cands if c})       # declared resolution: prefer the cost-bearing schedule
    return (from_cost or vals)[0]


def _resolve_domicile(doms: List[str]) -> tuple:
    """Return (domicile, conflict). Agree, or all imply the SAME currency → deterministic pick.
    Disagree in a way that implies DIFFERENT currencies (including a known-vs-unmappable split) →
    (None, True): domicile is withheld so U6 fail-closes the currency (no geo evidence) and the
    pipeline discloses the conflict. A pure spelling variant that maps to one currency is NOT a
    conflict (no over-hold — same principle as coexisting-concept non-conflicts)."""
    distinct = sorted({d for d in doms if d})
    if not distinct:
        return None, False
    ccys = {expected_currency(d) for d in distinct}
    if len(ccys) > 1:                      # currencies genuinely differ → currency ambiguous → hold
        return None, True
    return distinct[0], False              # one currency (or all unmappable) → deterministic, no conflict


def _resolve_numeric_collision(base: str, vals: List[Decimal]) -> Optional[Decimal]:
    """Merge multiple per-sheet numeric values for a company anchor attribute (cost / fair_value). DETECTION
    is routed through the one mechanism (concept_nets Net 1) so the net SEES every fund_anchor merge and
    fires COLLIDE on a real cross-sheet disagreement (measured: 3 on the fund corpus) — not silently
    swallowed. RESOLUTION stays the DECLARED per-base policy (spec §0.1 detection-universal / resolution-
    declared): cost/FV declare 'max' (a total restated in a second sheet — 'cost for ref' on the valuation
    schedule — never double-counts the tranche sum). Byte-identical: max(vals) is the value on both the
    agree and collide paths (equal values → max == the agreed value). Fail-closed (like require_basis): a
    collision whose declared resolution is not 'max' HOLDS by raising — never a guessed aggregation.

    3a note: the ConceptKey stays base-AUTHORITATIVE (the passed `base`, which IS the resolution-policy
    key), NOT decompose(base) — re-deriving it via decompose would risk the policy lookup drifting to None
    (→ a spurious hold_both raise on a real cost/FV collision). No per-value qualifier label reaches this
    merge, so decompose adds nothing here; its danger lives at fund_terms. Grounded no-op, base-safe."""
    if not vals:
        return None
    verdict = concept_nets.net1_collision(
        [concept_nets.Observation(ConceptKey(base), value=v) for v in vals], tol_abs=Decimal('0'))
    if verdict.outcome == concept_nets.COLLIDE and verdict.resolution != 'max':
        raise ValueError(f"fund_anchor: '{base}' cross-sheet collision resolution is "
                         f"{verdict.resolution!r}, not the declared 'max' — refusing to guess")
    return max(vals)


def build_fund_anchors(paths: List[str]) -> Dict[str, CompanyAnchor]:
    """Read every fund file's schedule sheets → {normalised company → CompanyAnchor}.

    COLLECT-then-RESOLVE: every attribute is gathered across all sheets with provenance, then merged
    by an order-INDEPENDENT rule — never silent first-wins (which locks in whichever sheet happened to
    be read first). cost/fair_value = MAX of per-sheet sums; ownership/domicile/irr resolved
    deterministically (see the _resolve_* helpers); descriptive text = agreed value or the
    deterministic minimum. So build_fund_anchors(files) == build_fund_anchors(reversed(files))."""
    acc: Dict[str, dict] = {}
    for path in paths:
        prof = profile_file(path.split('/')[-1], path)
        for s in prof['sheets']:
            recs = _schedule_rows_from_sheet(prof['grid'][s.sheet])
            if not recs:
                continue
            # ── per-sheet reduction: sum cost across tranche rows; first row per company for the
            #    single-valued attrs (within-sheet row order is stable, not upload order) ──
            sheet_cost: Dict[str, Decimal] = {}
            sheet_one: Dict[str, dict] = {}
            for rec in recs:
                key = lexicon.normalise_label(rec['company'])
                if not key:
                    continue
                if rec.get('cost') is not None:
                    sheet_cost[key] = sheet_cost.get(key, Decimal('0')) + rec['cost']
                o = sheet_one.setdefault(key, {'company': rec['company']})
                for a in ('ownership_pct', 'fair_value', 'domicile', 'irr') + _DESC_ATTRS + _LISTING_ATTRS:
                    if rec.get(a) is not None and o.get(a) is None:
                        o[a] = rec[a]
            # ── push this sheet's per-company results into cross-sheet candidate lists ──
            for key in set(list(sheet_cost) + list(sheet_one)):
                o = sheet_one.get(key, {})
                a = acc.setdefault(key, {'company': o.get('company', key), 'cost': [], 'fair_value': [],
                                         'own': [], 'dom': [], 'irr': [],
                                         'desc': {d: [] for d in _DESC_ATTRS + _LISTING_ATTRS}, 'sheets': set()})
                has_cost = key in sheet_cost
                has_fv = o.get('fair_value') is not None
                if has_cost:
                    a['cost'].append(sheet_cost[key])
                if has_fv:
                    a['fair_value'].append(o['fair_value'])
                if o.get('ownership_pct') is not None:
                    a['own'].append((o['ownership_pct'], has_cost))
                if o.get('domicile'):
                    a['dom'].append(o['domicile'])
                # IRR: the stated GROSS IRR is the one on the MARK/valuation schedule (it carries the
                # company's fair value). A realisation schedule's IRR is a DIFFERENT figure (realised
                # IRR-on-exit) already retained by the exits pipeline — never merge it into irr_gross.
                if o.get('irr') is not None and has_fv:
                    a['irr'].append(o['irr'])
                for d in _DESC_ATTRS + _LISTING_ATTRS:
                    if o.get(d) is not None:
                        a['desc'][d].append(o[d])
                a['sheets'].add(s.sheet)
    merged: Dict[str, CompanyAnchor] = {}
    for key, a in acc.items():
        ca = CompanyAnchor(company=a['company'])
        ca.cost_cr = _resolve_numeric_collision('cost', a['cost'])
        ca.fair_value_cr = _resolve_numeric_collision('fair_value', a['fair_value'])
        ca.ownership_frac = _resolve_ownership(a['own'])
        ca.domicile, ca.domicile_conflict = _resolve_domicile(a['dom'])
        ca.irr_gross = sorted(set(a['irr']))[0] if a['irr'] else None   # deterministic if marks disagree
        for d in _DESC_ATTRS + _LISTING_ATTRS:
            vals = sorted({v for v in a['desc'][d]})
            if vals:
                setattr(ca, d, vals[0])                                  # agreed value, or deterministic min
        ca.sources = sorted(a['sheets'])
        ca.anchor_cr = whole_company_anchor(cost_cr=ca.cost_cr, ownership_frac=ca.ownership_frac,
                                            fair_value_cr=ca.fair_value_cr)
        merged[key] = ca
    return merged


def match_anchor(entity_label: str, anchors: Dict[str, CompanyAnchor]) -> Optional[CompanyAnchor]:
    """Best-name-similarity anchor for an MIS file's entity label (fuzzy, robust to
    'Hubbler (Hubler)' vs 'Hubler'), above a floor — else None (never a wrong one)."""
    best, best_sc = None, 0.0
    for ca in anchors.values():
        sc = similarity(entity_label, ca.company)
        if sc > best_sc:
            best, best_sc = ca, sc
    return best if best_sc >= _MATCH_FLOOR else None


def distinctive_tokens(anchors: Dict[str, CompanyAnchor]) -> Dict[str, set]:
    """company_key -> the set of its name tokens that identify EXACTLY ONE company
    in the closed set (and are ≥3 chars, so a stub can't match). Shared/generic
    tokens ('digital', 'resources') are dropped — only a token unique to one
    company can bind a file to it."""
    from .namematch import tokens as name_tokens
    per, counts = {}, {}
    for key, ca in anchors.items():
        toks = {t for t in name_tokens(ca.company) if len(t) >= 3}
        per[key] = toks
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
    return {key: {t for t in toks if counts[t] == 1} for key, toks in per.items()}


def resolve_closed_set(file_text: str, anchors: Dict[str, CompanyAnchor]) -> Optional[str]:
    """CLOSED-SET REVERSE match — the fail-safe entity resolver. Instead of pulling
    a noisy identifier out of the file and matching outward (which mis-attributes),
    test each KNOWN company's distinctive tokens against the file's text (filename +
    content). Return the company key ONLY on a UNIQUE hit; zero or MULTIPLE distinct
    companies matched → None (HOLD). Because each name is tested independently, one
    file's numbers can NEVER collapse onto another company; a file that mentions two
    portfolio entities (group consolidation) holds rather than guessing."""
    from .namematch import tokens as name_tokens
    ftoks = set(name_tokens(file_text))
    dist = distinctive_tokens(anchors)
    hits = [key for key, toks in dist.items() if toks & ftoks]
    return hits[0] if len(hits) == 1 else None
