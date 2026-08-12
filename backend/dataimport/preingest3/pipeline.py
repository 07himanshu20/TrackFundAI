"""
The ONE Django-free orchestrator for a preingest3 run.

`run(files, ...)` takes N uploaded workbooks (fund schedules + company MIS), routes
each, builds the fund anchors, resolves every MIS file to a portfolio company, and
returns the CIR (the stable contract every downstream reader binds to) plus a
per-file report and a review queue of unattributed files.

Design constraints this module honours (so it drops into a multi-tenant, multi-
worker app cleanly):

  • Django-free — imports only the preingest3 library + stdlib. The model transport
    (llm.set_model_provider) and the alias store are INJECTED by the host; nothing
    here reaches into `api`, models, or settings.
  • Org-scoped — the alias store is client-scoped. Entity resolution for org A can
    never read org B's confirmed aliases. The host passes an org-keyed store.
  • Entity resolution is LEDGER-FIRST → safe CLOSED-SET reverse match → HELD. A
    human confirmation in the store is authoritative; the closed-set matcher never
    guesses across companies; anything else holds for review (the write-back loop).
  • Incremental — extraction is deterministic and keyed by the PROVABLY-COMPLETE input
    set (content_fp + as_of + rate_card + anchor + domicile + use_model + net logic
    version — identity.extraction_cache_key), so a caller may pass `reuse` from a prior
    run's `RunResult.extraction` to re-attribute + re-assemble WITHOUT re-extracting an
    unchanged file, while any change to a value-affecting input forces a recompute (never
    a stale-config serve — the bug content_fp-alone would cause at the U6 boundary).
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Tuple

from . import formulas
from . import ledger
from . import lexicon
from . import fund_anchor
from . import fund_extract
from . import fund_terms
from . import nav
from . import llm
from .alias_ledger import AliasLedger
from .cir import CIR, Record, Figure, Provenance
from . import extract as _extract
from .extract import extract_company
from .identity import compute_identity, extraction_cache_key
from .namematch import tokens as _name_tokens
from .profiler import profile_file, clear_profile_cache, _cell_type
from .ratecard import RateCard, default_inr_card

logger = logging.getLogger(__name__)

# concepts carried on a fund-schedule portfolio record (point-in-time balances)
_FUND_FIGS = ('cost', 'fair_value')
# A single investment schedule listing this many companies is not a portfolio — it
# is a misrouted company MIS / ledger / matrix. Generous (real AIF portfolios are
# tens of companies); the point is only to catch a 3000-row explosion, hold it, and
# disclose it — never emit phantoms. The last-line safety net under routing.
_MAX_SCHEDULE_COMPANIES = 200


@dataclass
class FileReport:
    label: str
    role: str                       # 'fund' | 'mis' | 'unrecognised' | 'error'
    status: str                     # 'ok' | 'attributed' | 'held' | 'read_error'
    entity_id: Optional[str] = None
    reason: str = ''
    content_fp: str = ''


@dataclass
class ReviewFile:
    """An MIS file that could not be safely attributed → the alias review queue.
    The reviewer picks one candidate; the host writes it back to the org's alias
    store (provenance='human'); the next run's ledger lookup resolves it."""
    label: str
    content_fp: str
    identifiers: List[str]
    candidates: List[dict]          # [{'id': company_key, 'name': display}]
    reason: str


@dataclass
class RunResult:
    cir: CIR
    files: List[FileReport] = field(default_factory=list)
    review_queue: List[ReviewFile] = field(default_factory=list)
    extraction: Dict[str, Record] = field(default_factory=dict)   # content_fp → mis Record (for incremental re-run)
    model_metrics: Optional[dict] = None                          # per-run boundary metrics (calls/ok/error/…)
    model_health: Optional[dict] = None                           # health-check result when require_model=True


def _hints(prof, k: int = 10) -> List[str]:
    """A few short text cells from the top of the first sheets — content evidence
    for entity resolution, blind to any single header spelling."""
    out: List[str] = []
    for s in prof['sheets'][:6]:
        rows = prof['grid'][s.sheet]
        for r in range(min(4, len(rows))):
            for v in rows[r]:
                if _cell_type(v) == 'text' and 4 < len(str(v).strip()) < 60:
                    out.append(str(v).strip())
    return out[:k]


# Fund-only vocabulary a portfolio-company MIS never carries. ≥2 DISTINCT of these mark a
# FUND-LEVEL file (fund P&L / capital-account / LPA terms) that must NOT be mined as a
# company MIS. Deliberately excludes weak/ambiguous tokens (a bare 'nav', 'management fee')
# that a company could mention; the ≥2-distinct threshold is the false-positive guard.
_FUND_LEVEL_MARKERS = (
    'capital call', 'capital called', 'called capital', 'drawdown', 'commitment',
    'committed capital', 'uncalled', 'unfunded', 'undrawn', 'limited partner', 'lp name',
    'carried interest', 'hurdle', 'catch-up', 'catch up', 'clawback', 'gp commitment',
    'sponsor commitment', 'net asset value', 'distribution to lp', 'distributions to lp',
)


def _statement_is_fund_level(rows, label_col, min_distinct: int = 2) -> bool:
    """INTERIM fail-closed guard (advisor 2026-07-26): a fund-level statement NAMES fund
    concepts as its own LINE ITEMS (capital calls / distributions to LPs / carry). Scoped to
    the SELECTED statement's label column — NOT the whole workbook — because a company MIS in a
    big SAP export may mention a fund word incidentally in scattered GL detail (CPM: 2 fund
    words workbook-wide but only 1 in its statement labels → correctly stays MIS; Fund_Accounts:
    3 in its statement labels → fund-level). ≥`min_distinct` distinct markers ⇒ route 'unknown'
    (held), never mined as a company MIS. Concept-driven, not filename-based; the seed of the
    Phase-D `fund_financials` class + fund extractor."""
    found = set()
    for row in rows:
        if label_col is not None and label_col < len(row) and isinstance(row[label_col], str):
            low = row[label_col].lower()
            for m in _FUND_LEVEL_MARKERS:
                if m in low:
                    found.add(m)
                    if len(found) >= min_distinct:
                        return True
    return False


def _route_structural(prof) -> str:
    """3-way structural router (quota-free), MIS-FIRST. A company MIS is DEFINED by
    a TIME-SERIES financial statement carrying ≥2 MIS concepts (revenue / ebitda /
    cash / headcount across period columns) — that test comes first, so a monthly
    report is never mistaken for a fund schedule and mined into thousands of phantom
    companies. Only a file WITHOUT such a statement can be a fund file, and only if
    it carries a genuine (ledger-excluded) investment schedule of ≥2 companies.
    Anything else is 'unknown' → held & disclosed, never guessed.

    FUND-LEVEL GUARD: a file whose statement looks MIS-shaped (≥2 MIS concepts) but which
    speaks fund-only vocabulary (capital calls / commitments / carry / LPs) is a fund P&L,
    NOT a portfolio company — held as 'unknown' until Phase-D fund extraction exists, so a
    fund figure can never ride into MIS coverage mislabeled as a company number."""
    best = _extract._best_sheet(prof, _extract.MIS_CONCEPTS)
    if best is not None and best[0] >= 2 and not _statement_is_fund_level(best[3], best[5]):
        return 'mis'                                # ≥2 MIS concepts on a time axis, not fund-level
    for s in prof['sheets']:
        recs = fund_anchor._schedule_rows_from_sheet(prof['grid'][s.sheet])
        if recs and len(recs) >= 2:
            return 'fund'
    if _is_fund_financials(prof):
        return 'fund_financials'
    return 'unknown'


def _is_fund_financials(prof, min_distinct: int = 2) -> bool:
    """A fund-LEVEL workbook (capital account / fund accounts / terms / LP register):
    it NAMES fund concepts as its own content but is neither a company MIS (checked
    first) nor an investment schedule (checked second). ≥`min_distinct` distinct fund
    markers ⇒ route to the Phase-D fund extractor instead of holding as 'unknown'.
    Reuses the same marker set as `_statement_is_fund_level` — one fund vocabulary."""
    found = set()
    for s in prof['sheets']:
        for row in prof['grid'][s.sheet]:
            for v in row:
                if isinstance(v, str):
                    low = v.lower()
                    for m in _FUND_LEVEL_MARKERS:
                        if m in low:
                            found.add(m)
                            if len(found) >= min_distinct:
                                return True
    return False


def _route_file(prof, *, label=None, path=None, content_fp=None, use_model=False) -> str:
    """Structural route, refined by the model ONLY for the genuinely ambiguous tail.
    The structural router is authoritative for anything it recognises; the classifier
    is consulted solely to rescue a file it returns 'unknown' — and even then only to
    PROMOTE a real company MIS the code missed. Fail-closed: a transport error, a
    non-MIS classification, or no provider all keep the safe 'unknown' hold. Confidence
    never gates this (build rule #4) — only the discrete file_class does."""
    structural = _route_structural(prof)
    if not use_model or structural != 'unknown' or llm._resolve_provider() is None:
        return structural
    try:
        from . import classifier
        fp = content_fp or (compute_identity(label, path).content_fp if (label and path) else '')
        cls = classifier.classify(label or '', prof['sheets'], fp)
    except Exception:  # noqa: BLE001 — a classifier hiccup must never fail-open
        return structural
    if cls.get('error') or cls.get('file_class') != classifier.MIS:
        return structural                          # keep the safe hold unless it's clearly a company MIS
    return 'mis'


def _resolve_mis(alias_ids: List[str], file_text: str, anchors: Dict, store) -> Tuple[Optional[str], str]:
    """HUMAN-ALIAS → CLOSED-SET → HELD. Returns (entity_key or None, reason).

    The durable alias store is written ONLY by human confirmation at the review gate
    (keyed on the file's unique identity). There is NO auto-learn: the closed-set
    reverse matcher is deterministic and recomputes the same answer every run, so
    caching it buys nothing and can only poison resolution (a shared token binding
    one company's numbers onto another — the U4 class). Resolution binds only on a
    DISTINCTIVE identifier; anything non-unique HOLDS, never guesses."""
    hit = store.lookup(alias_ids)                     # human confirmations only
    if hit is not None and hit in anchors:
        return hit, 'human-confirmed alias, re-validated in this run'
    if hit is not None:
        return None, f'stale alias → {hit!r} absent from this run — held'
    key = fund_anchor.resolve_closed_set(file_text, anchors)
    if key is not None:
        return key, 'closed-set distinctive-token unique match'
    return None, 'no unique company match — held for review'


def run(files: List[Tuple[str, str]], *, as_of: str, org: str = 'default',
        rate_card: RateCard = None, alias_store=None,
        reuse: Dict[str, Record] = None,
        model_provider: Callable = None, require_model: bool = False,
        progress: Optional[Callable] = None) -> RunResult:
    """files = [(label, absolute_path)]. Returns a RunResult with the CIR.
    `progress(pct:int, msg:str)` is called at stage boundaries for async workers.

    `require_model=True` runs a LIVE health-check at the door before touching any
    file: a 404 / auth / bad-config fault raises ModelConfigError and halts the run
    loudly (a broken model boundary can never again hide as a per-file hold). Every
    run — model-driven or purely deterministic — reports its boundary behaviour in
    RunResult.model_metrics so 'the model ran / didn't run' is measured, not assumed."""
    def _p(pct, msg):
        if progress:
            progress(pct, msg)
    if model_provider is not None:
        llm.set_model_provider(model_provider)
    metrics = llm.new_metrics()               # per-run, thread-isolated boundary counters
    health = None
    if require_model:                         # fail loud NOW, before any file work
        _p(2, 'Health-checking model boundary')
        health = llm.health_check()           # raises ModelConfigError on 404/auth/bad-config
        if not health.ok:
            logger.warning('[preingest3] model health-check degraded: %s', health.detail)
    rate_card = rate_card or default_inr_card(as_of)
    store = alias_store if alias_store is not None else AliasLedger(org=org)
    reuse = reuse or {}
    clear_profile_cache()   # reuse each file's grid within THIS run; never across runs
    _p(5, 'Reading & routing files')

    # ── Route every file (fund schedule vs company MIS vs unknown) fail-closed ──
    fund_paths: List[str] = []
    mis: List[Tuple[str, str, object]] = []   # (label, path, profile)
    fund_fin: List[Tuple[str, str, object]] = []   # (label, path, profile) — Phase-D fund files
    schedule_files: List[Tuple[str, str, object]] = []   # (label, path, profile) — 'fund' schedule
    unknown: List[str] = []
    reports: List[FileReport] = []
    for label, path in files:
        prof = profile_file(label, path)
        if prof.get('error'):
            reports.append(FileReport(label, 'error', 'read_error', reason=prof['error']))
            continue
        role = _route_file(prof, label=label, path=path, use_model=require_model)
        if role == 'fund':
            n = len(fund_anchor.build_fund_anchors([path]))     # safety net: a real
            if n > _MAX_SCHEDULE_COMPANIES:                      # schedule is bounded
                unknown.append((label, f'{n} candidate companies from one file — '
                                       f'not a real investment schedule (likely a misrouted MIS/ledger)'))
                reports.append(FileReport(label, 'unknown', 'held',
                                          reason=f'{n} candidate companies — not a real schedule; held'))
            else:
                fund_paths.append(path)
                schedule_files.append((label, path, prof))   # also a ledger source (e.g. Exits)
                reports.append(FileReport(label, 'fund', 'ok'))
        elif role == 'mis':
            mis.append((label, path, prof))
        elif role == 'fund_financials':
            fund_fin.append((label, path, prof))
        else:
            unknown.append((label, 'no time-series statement or investment schedule found'))
            reports.append(FileReport(label, 'unknown', 'held',
                                      reason='no time-series statement or investment schedule found'))

    _p(25, 'Building fund anchors')
    anchors = fund_anchor.build_fund_anchors(fund_paths) if fund_paths else {}
    candidates = [{'id': k, 'name': ca.company} for k, ca in sorted(anchors.items())]

    cir = CIR(as_of=as_of, rate_card_id=rate_card.card_id)
    for label, reason in unknown:                  # recognised-as-neither → disclosed hold, not junk
        cir.disclose('unrecognised_file', f'{label}: {reason}', entity=label)

    # ── Fund anchors → portfolio_investments records (cost / fair value) ──
    for key, ca in sorted(anchors.items()):
        pv = Provenance(source_file='fund_schedule', content_fingerprint='',
                        sheet='Portfolio', cell='', row_label=ca.company)
        fields = {'company': ca.company}
        # descriptive attributes (plain strings, NOT Figures) — enrich the portfolio
        # master; deliberately outside the Figure-based money/coverage/determinism rulers.
        for _attr in ('sector', 'stage', 'investment_date', 'instrument', 'valuation_method', 'domicile'):
            _v = getattr(ca, _attr, None)
            if _v:
                fields[_attr] = _v
        if ca.ownership_frac is not None:      # equity % (plain float, not a money Figure)
            fields['ownership_pct'] = ca.ownership_frac
        if ca.cost_cr is not None:
            fields['cost'] = Figure('cost', ca.cost_cr, None, pv, basis='point_in_time')
        if ca.fair_value_cr is not None:
            # fund's-stake FV (holding basis, §8.3) — the basis MOIC/TVPI consume
            fields['fair_value'] = Figure('fair_value', ca.fair_value_cr, None, pv,
                                          basis='point_in_time', value_basis=formulas.FV_HOLDING)
        if ca.irr_gross is not None:
            # stated IRR%(Gross), preserved per §2.7 (XIRR recomputation suppressed);
            # gross-tagged so it can never bleed into the fund Net IRR field. Still
            # run through §6.3 sanity bounds — the document's OWN guard (not a
            # recompute): an out-of-range stated IRR is held with a logged reason.
            _irr_ok = formulas.in_sanity_irr(ca.irr_gross)
            fields['irr'] = Figure('irr', ca.irr_gross, None, pv,
                                   basis='point_in_time', value_basis=formulas.BASIS_GROSS_XIRR,
                                   held=not _irr_ok,
                                   hold_reason='' if _irr_ok else
                                   f'stated IRR {ca.irr_gross} outside §6.3 sanity bounds '
                                   f'[-99.99%, 500%] — rejected with logged reason')
        cir.add(Record('portfolio_investments', entity_id=ca.company, fields=fields))

    # ── Phase-D fund-financials files → deterministic fund extractor ──
    if fund_fin:
        _p(40, f'Extracting {len(fund_fin)} fund file(s)')
    lp_registers: List = []                        # LPRegister per file — reconciled after the loop
    term_records: List = []                        # per-file fund_terms records — reconciled after
    nav_inputs: List = []                           # per-file NAV signals — roll-forward struck after
    base_phase_hint = None                          # first stated mgmt-fee base/phase across the files
    fee_actual = None                               # (value, provenance, candidates) for the annual actual fee
    last_fp = ''
    for label, path, prof in fund_fin:
        fp = compute_identity(label, path).content_fp
        last_fp = fp
        rec = fund_extract.extract_fund_financials(label, path, prof, rate_card=rate_card, content_fp=fp)
        reg = fund_extract.extract_lp_register(label, path, prof, rate_card=rate_card, content_fp=fp)
        terms = fund_terms.extract_fund_terms(label, path, prof, content_fp=fp)
        ni = nav.extract_nav_inputs(label, path, prof, as_of=as_of, content_fp=fp)
        if ni is not None:
            nav_inputs.append(ni)
        produced = False
        if rec is not None:                        # capital-account flows (MVP slice)
            cir.add(rec)
            produced = True
        if reg is not None:                        # per-LP register rows
            for r in reg.records:
                cir.add(r)
            lp_registers.append(reg)
            produced = True
        if terms is not None:                      # LPA economic terms
            term_records.append(terms)
            produced = True
        if ni is not None:                         # NAV roll-forward signals (P&L and/or dated flows)
            produced = True
        if base_phase_hint is None:                # a fee base/phase stated anywhere in the run
            bp = fund_terms.fee_base_phase(label, path, prof, content_fp=fp)
            if bp[0] != 'UNSPECIFIED' or bp[1] != 'UNSPECIFIED':
                base_phase_hint = bp
        if fee_actual is None:                     # the ANNUAL ACTUAL mgmt fee (period-selected)
            fa = fund_terms.resolve_actual_annual_fee(label, prof, content_fp=fp)
            if fa[0] is not None:
                fee_actual = fa
        reports.append(FileReport(label, 'fund_financials', 'ok', content_fp=fp))
        if not produced:                           # recognised fund file, no slice concepts yet
            cir.disclose('fund_no_flows', f'{label}: fund file recognised; no capital-account '
                         'flows, LP register, terms or NAV signals found', entity=label)

    # ── cross-file fund reconciliation: LP register ↔ capital account. Runs AFTER all
    # fund files are extracted, because the register and the capital account live in
    # DIFFERENT files. Each hard identity holds the affected column on mismatch, so a
    # per-LP breakdown that doesn't tie to the fund total is never shipped. ──
    if lp_registers:
        capital = next((r for r in cir.records if r.domain == 'fund_financials'), None)
        for reg in lp_registers:
            for chk in fund_extract.reconcile_lp_register(reg, capital=capital):
                cir.checks.append(chk)
                if chk.get('class') == 'hard' and chk.get('status') in ('fail', 'indeterminate'):
                    cir.disclose('lp_reconciliation',
                                 f'{reg.source}: {chk["id"]} — {chk.get("detail", "")}', entity=reg.source)

    # ── cross-file terms reconcile: multi-source agreement + base/phase enrichment.
    # Merges the per-file terms records into ONE canonical fund_terms record; a term
    # that disagrees across sources is held (never silently pick one). ──
    canonical_terms = None
    committed_base = next((reg.corpus_cr for reg in lp_registers if reg.corpus_cr is not None), None)
    if term_records:
        canonical_terms, term_checks = fund_terms.reconcile_fund_terms(
            term_records, base_phase_hint=base_phase_hint, fee_actual=fee_actual, committed_base=committed_base)
        if canonical_terms is not None:
            cir.add(canonical_terms)
        for chk in term_checks:
            cir.checks.append(chk)
            if chk.get('class') == 'hard' and chk.get('status') in ('fail', 'indeterminate'):
                cir.disclose('term_reconciliation', f'{chk["id"]} — {chk.get("detail", "")}', entity='fund')

    # ── NAV roll-forward (cross-file): the P&L lives in the accounts file, called/distributed
    # in the capital account, the carry rate in the terms. Struck AFTER those exist, with the
    # as-of-consistency and carry cross-check guards, and emitted as two labeled bases. ──
    if nav_inputs:
        capital = next((r for r in cir.records if r.domain == 'fund_financials'), None)
        carry_rate = carry_rate_prov = None
        if canonical_terms is not None:
            ct = canonical_terms.fields.get('carried_interest')
            if isinstance(ct, fund_terms.FundTerm) and ct.confirmed and ct.value is not None:
                carry_rate, carry_rate_prov = ct.value, ct.provenance
        # Σ per-company FV — the §5.1 balance-sheet path's largest term (cross-file; the
        # Total-FV=Σ check ties it). Confirmed FVs only; None if any is held (no partial FV).
        _fv_figs = [r.fields.get('fair_value') for r in cir.records if r.domain == 'portfolio_investments']
        _portfolio_fv = (sum(f.value_cr for f in _fv_figs if getattr(f, 'confirmed', False))
                         if _fv_figs and all(getattr(f, 'confirmed', False) for f in _fv_figs) else None)
        nav_rec, nav_checks, nav_disc = nav.reconcile_nav(
            nav_inputs, capital=capital, carry_rate=carry_rate, carry_rate_prov=carry_rate_prov,
            as_of=as_of, fee_actual=fee_actual, committed_base=committed_base, source_fp=last_fp,
            portfolio_fv=_portfolio_fv)
        if nav_rec is not None:
            cir.add(nav_rec)
        for chk in nav_checks:
            cir.checks.append(chk)
            if chk.get('class') == 'hard' and chk.get('status') in ('fail', 'indeterminate'):
                cir.disclose('nav_reconciliation', f'{chk["id"]} — {chk.get("detail", "")}', entity='fund')
        for d in nav_disc:
            cir.disclose(d.get('kind', 'nav'), d.get('detail', ''), entity=d.get('entity', 'fund'))

    # ── row-level LEDGERS via the ONE universal reader, CLASSIFIER-FIRST. Struck AFTER
    # the totals they tie to exist. For every fund-domain sheet we classify to at most
    # ONE domain by distinguishing markers; a sheet matching TWO domains (the look-alike
    # trap: calls vs distributions, tranches vs valuations) is HELD, never guessed; a
    # second sheet for the same domain is HELD too. A confidently-classified sheet is
    # extracted and its rows emitted ONLY if Σ(amount) ties to a control total we trust
    # (the sheet's own total AND an independent total where one exists). Every sheet's
    # verdict is disclosed as a classification row → visible in the coverage map. ──
    _fin = next((r for r in cir.records if r.domain == 'fund_financials'), None)
    _called = _fin.fields.get('called') if _fin else None
    _distributed = _fin.fields.get('distributed') if _fin else None
    _cost_by_co = {lexicon.normalise_label(r.entity_id): r.fields['cost'].value_cr
                   for r in cir.records if r.domain == 'portfolio_investments'
                   and isinstance(r.fields.get('cost'), Figure) and r.fields['cost'].confirmed}
    _ledger_cfg = {cfg.domain: cfg for cfg in ledger.LEDGERS}
    # Each ledger domain's trusted EXTERNAL control (same-order ₹Cr), passed to the frame
    # resolver so a block whose sheet omits its unit label still resolves from the known-Cr
    # magnitude it must sit near — instead of being held for a missing label. NOT the
    # block's own stated total (same native unit, can't fix scale); the exact Σ tie still
    # verifies scale to the rupee downstream, so this recovers coverage without weakening.
    _total_cost = sum(_cost_by_co.values(), Decimal('0')) if _cost_by_co else None
    _anchors: Dict[str, Decimal] = {}
    if isinstance(_called, Figure) and _called.confirmed:
        _anchors['capital_calls'] = _called.value_cr
    if isinstance(_distributed, Figure) and _distributed.confirmed:
        _anchors['distributions'] = _distributed.value_cr
    if _total_cost:
        _anchors['investment_tranches'] = _total_cost
        _anchors['exits'] = _total_cost
    # 1. SEGMENT every fund sheet into per-domain blocks. A sheet may hold ONE domain or
    #    STACK several (calls above, distributions below); segment_sheet bounds each block
    #    to [header, next-header) so a top block never bleeds into the one below, and a
    #    genuinely ambiguous single header is HELD (disclosed), never guessed.
    _domain_blocks: Dict[str, list] = {}
    for label, path, prof in fund_fin + schedule_files:
        fp = compute_identity(label, path).content_fp
        for s in prof['sheets']:
            blocks, disclosures = ledger.segment_sheet(prof['grid'][s.sheet], s.sheet, ledger.LEDGERS,
                                                       label=label, content_fp=fp, rate_card=rate_card,
                                                       anchors=_anchors)
            for d in disclosures:                  # one classification row per block → visible in coverage map
                cir.disclose('sheet_classification', f'{label}: {d.get("detail", "")}', entity=s.sheet)
            for b in blocks:
                _domain_blocks.setdefault(b.domain, []).append(b)

    # 2. per domain: MERGE its blocks (not first-wins) and tie the COMBINED Σ to the
    #    trusted control (the capital-account called/distributed; per-company cost). A
    #    ledger emits its rows ONLY if it ties; otherwise the rows are held for review.
    for dom, blocks in _domain_blocks.items():
        cfg = _ledger_cfg[dom]
        combined = ledger.combine_blocks(dom, blocks)
        if dom == 'capital_calls' and isinstance(_called, Figure) and _called.confirmed:
            xr = ledger.tie_to_control(combined, _called.value_cr, control_label='capital-account called',
                                       check_id='capital_calls_tie_to_called')
            if xr is not None:
                combined.checks.append(xr)
        elif dom == 'distributions' and isinstance(_distributed, Figure) and _distributed.confirmed:
            xr = ledger.tie_to_control(combined, _distributed.value_cr,
                                       control_label='capital-account distributed',
                                       check_id='distributions_tie_to_distributed')
            if xr is not None:
                combined.checks.append(xr)
        elif dom == 'investment_tranches' and _cost_by_co:
            combined.checks.extend(ledger.tie_per_group(combined, cfg, 'company', _cost_by_co,
                                                        check_id='tranches_tie_to_company_cost'))
        for rec in combined.records:
            cir.add(rec)
        for chk in combined.checks:
            cir.checks.append(chk)
            if chk.get('class') == 'hard' and chk.get('status') in ('fail', 'indeterminate'):
                cir.disclose('ledger_reconciliation',
                             f'{chk["id"]} — {chk.get("detail", "")}', entity=dom)
        cir.disclose('sheet_classification',
                     f'{dom}: {len(combined.records)} rows across {len(blocks)} block(s) '
                     f'[{combined.sheet}], held={combined.held}', entity=dom)

    # ── MIS files → resolve (pass 1) ──
    _p(45, f'Resolving {len(mis)} company files')
    review: List[ReviewFile] = []
    extraction: Dict[str, Record] = {}
    resolved = []   # (label, path, prof, fp, entity_key, reason)
    for label, path, prof in mis:
        fp = compute_identity(label, path).content_fp
        # Alias-store keys are the DISTINCTIVE FILENAME ONLY. Content hints help the
        # closed-set MATCH find the company name, but must NEVER be alias keys — a
        # generic hint ('Monthly MIS', a month name) shared by two files would
        # cross-attribute one file's numbers onto another company (the U4 bug).
        alias_ids = [label]
        file_text = label + ' ' + ' '.join(_hints(prof))
        entity_key, reason = _resolve_mis(alias_ids, file_text, anchors, store)
        resolved.append((label, path, prof, fp, entity_key, reason))

    # ── BIJECTION HARD-GUARD (U4): each company may be claimed by at most ONE file.
    # If two files resolve to the SAME company this run, that is a resolution error
    # ('Hubbler ×3') — HOLD every claimant and flag, never emit scrambled records.
    # The highest-stakes invariant (right numbers under the right name) now has a
    # structural check, not just careful code. ──
    claim_counts = Counter(ek for *_r, ek, _rz in resolved if ek is not None)
    collided = {ek for ek, n in claim_counts.items() if n > 1}

    # ── emit (pass 2) ──
    _p(60, f'Extracting {len(mis)} company files')
    for label, path, prof, fp, entity_key, reason in resolved:
        if entity_key is None:
            reports.append(FileReport(label, 'mis', 'held', reason=reason, content_fp=fp))
            review.append(ReviewFile(label, fp, [label], candidates, reason))
            cir.disclose('held_file', f'{label}: {reason}', entity=label)
            continue
        ca = anchors.get(entity_key)
        if entity_key in collided:                # collision → hold, never emit
            others = [l for l, *_x, ek, _y in resolved if ek == entity_key and l != label]
            why = (f'resolution collision — {ca.company!r} also claimed by '
                   f'{", ".join(others)}; held to avoid mixing companies')
            reports.append(FileReport(label, 'mis', 'held', entity_id=ca.company,
                                      reason=why, content_fp=fp))
            review.append(ReviewFile(label, fp, [label], candidates, why))
            cir.disclose('resolution_collision', f'{label}: {why}', entity=ca.company)
            continue
        # Inc-5: the reuse cache is keyed on the PROVABLY-COMPLETE input set, not content_fp
        # alone. content_fp-only would serve a file's prior-config record under a new rate
        # card / as_of / anchor / domicile (a silent stale number that goes live at the U6
        # multi-currency boundary). The composite key preserves the alias-confirmation fast
        # path (same file + same config → hit → re-attribute) while forcing a recompute the
        # instant any value-affecting input changes. Reporting still uses content_fp (`fp`).
        ck = extraction_cache_key(fp, as_of=as_of, rate_card_id=rate_card.card_id,
                                  anchor_cr=ca.anchor_cr, domicile=ca.domicile,
                                  use_model=require_model)
        rec = reuse.get(ck)                        # incremental: reuse prior extraction
        if rec is None:
            rec = extract_company(ca.company, path, rate_card=rate_card,
                                  entity=ca.company, domicile=ca.domicile,
                                  anchor_cr=ca.anchor_cr, use_model=require_model)
        else:                                     # same file+config, new attribution only
            rec = Record(rec.domain, entity_id=ca.company, fields=dict(rec.fields))
            rec.fields['company'] = ca.company
        extraction[ck] = rec
        if '_note' in rec.fields:                 # resolved, but no usable statement
            reports.append(FileReport(label, 'mis', 'held', entity_id=ca.company,
                                      reason=str(rec.fields['_note']), content_fp=fp))
            cir.disclose('held_file', f'{label}: {rec.fields["_note"]}', entity=ca.company)
            continue
        cir.add(rec)
        reports.append(FileReport(label, 'mis', 'attributed', entity_id=ca.company,
                                  reason=reason, content_fp=fp))

    # reverse coverage — investments with no MIS this run (disclosed gaps)
    attributed = {r.entity_id for r in reports if r.status == 'attributed'}
    for key, ca in sorted(anchors.items()):
        if ca.company not in attributed:
            cir.disclose('investment_without_mis',
                         f'{ca.company!r} had no attributed MIS file this run', entity=ca.company)

    _p(90, 'Assembling consolidated record')
    logger.info('[preingest3] run complete — model boundary: %s', metrics.summary())
    return RunResult(cir=cir, files=reports, review_queue=review, extraction=extraction,
                     model_metrics=metrics.summary(),
                     model_health=(health.__dict__ if health is not None else None))
