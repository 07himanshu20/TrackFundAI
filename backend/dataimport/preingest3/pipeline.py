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
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
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
from . import currency_ledger
from . import nav
from . import waterfall
from . import sebi_compliance
from . import golden_store
from . import llm
from . import units
from . import portfolio_correspondence
from . import quoted_unquoted
from .alias_ledger import AliasLedger
from .cir import CIR, Record, Figure, Provenance
from . import extract as _extract
from .extract import extract_company
from .identity import compute_identity, extraction_cache_key, clear_parse_cache
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


class PreingestConservationError(RuntimeError):
    """Raised (fail-closed) when the file-conservation invariant is violated — an input file produced
    no terminal record, or produced more than one. A missing file is a HARD ERROR, never a silently
    smaller output: the run refuses to finish rather than drop a file the user uploaded."""


# HELD reason codes — a small CLOSED enum so a hold is auditable, not a free-text black hole. Every
# non-attributed terminal record carries one; the two-number measurement tallies cold holds by these.
HELD_PARSE_ERROR = 'parse_error'                  # could not be read/extracted (corrupt / format / timeout)
HELD_UNRESOLVED_IDENTITY = 'unresolved_identity'  # opened+read but not confidently matched to a company
HELD_AMBIGUOUS_MATCH = 'ambiguous_match'          # matched >1 company (collision) → held, never guess
HELD_NO_CONTENT = 'no_extractable_content'        # opened but no time-series statement / usable content
HELD_MISROUTED = 'misrouted_schedule'             # looked like a schedule but too many companies (misroute)


@dataclass
class FileReport:
    label: str
    role: str                       # 'fund' | 'mis' | 'unrecognised' | 'error'
    status: str                     # 'ok' | 'attributed' | 'held' | 'read_error'
    entity_id: Optional[str] = None
    reason: str = ''
    content_fp: str = ''
    reason_code: str = ''           # closed-enum HELD_* category when not attributed (auditable hold)
    path: str = ''                  # the INPUT file path — per-upload instance identity for conservation


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
    currency_report: Optional[dict] = None                        # U6 uncovered-currency report (empty when all-INR)


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


def _canonical_order(files: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """U3.5 canonical processing order: sort the uploaded (label, path) pairs by CONTENT fingerprint,
    tie-broken by label. Upload order and filenames never influence the output. Applied before routing
    and anchor-building so the fund-anchor merge, record append order, and every downstream step see ONE
    stable order regardless of how the files arrived. NB this determinises ORDER; order-independence of
    VALUES is a separate guarantee owned by the extractors/merges (a determinism lock over a value that
    is itself order-dependent would only make a wrong answer stable — see the ownership-anchor fix)."""
    return sorted(files, key=lambda lp: (compute_identity(lp[0], lp[1]).content_fp, lp[0]))


def _extract_company_worker(task):
    """Pure per-file extraction unit for the L1 pool. Runs `extract_company` under a LOCAL currency
    ledger so its verdicts are CAPTURED and RETURNED — never written to the shared module global —
    and the parent merges them in canonical order (byte-identical to a sequential run). Module-level
    with picklable args/result so it runs under a ProcessPoolExecutor (spawn) as well as serially."""
    ck, company, path, domicile, anchor_cr, label, rate_card, use_model, base_currency = task
    local = currency_ledger.CurrencyLedger()
    prev = currency_ledger.active()
    currency_ledger.set_active(local)
    currency_ledger.context(entity=company, source_file=label)
    try:
        rec = extract_company(company, path, rate_card=rate_card, entity=company,
                              domicile=domicile, base_currency=base_currency,
                              anchor_cr=anchor_cr, use_model=use_model)
    except Exception as e:  # noqa: BLE001 — CONSERVATION: a per-file failure (corrupt / password / unknown
        # format / timeout / OOM-on-that-file) MUST become a HELD record, never crash the run or let a
        # swallowed worker exception silently lose the file. The emit loop turns this _note into a held
        # report with reason_code=parse_error, so `in == attributed + held` still balances.
        rec = Record('mis', entity_id=company,
                     fields={'company': company, '_note': f'parse_error: {type(e).__name__}: {e}'[:160]})
    finally:
        currency_ledger.set_active(prev)          # restore (matters only for the serial/in-process path)
        clear_parse_cache()                        # L2: release THIS file's grid so a reused pool worker
        clear_profile_cache()                      # never hoards every grid it touches across tasks
    return ck, rec, local.observations()


def _merge_metrics(dst, s) -> None:
    """Fold a worker thread's own CallMetrics summary into the parent run's metrics (thread metrics
    are ContextVar-isolated, so each worker counts its own calls and the parent sums them). Pure
    addition/count-merge → order-independent, so the merged model_metrics is deterministic."""
    if dst is None or not s:
        return
    dst.calls += s['calls']; dst.ok += s['ok']; dst.error += s['error']
    dst.fatal += s['fatal']; dst.retries += s['retries']; dst.latency_s += s['latency_s']
    for k, v in s['by_status'].items():
        dst.by_status[k] = dst.by_status.get(k, 0) + v


def _threaded_worker(task):
    """Thread variant of _extract_company_worker: gives THIS worker thread its own metrics
    accumulator (llm metrics is a thread-local ContextVar, so the parent's would be invisible here)
    and returns its summary for the parent to merge. Currency isolation + error→held come for free
    from _extract_company_worker (its local ledger is a per-thread ContextVar; a per-file exception
    is already caught into a held record)."""
    m = llm.new_metrics()
    ck, rec, obs = _extract_company_worker(task)
    return ck, rec, obs, m.summary()


def _run_extractions(tasks, *, rate_card, use_model, max_workers, base_currency=None):
    """Map `extract_company` over the cache-miss tasks. Returns {ck: (rec, currency_observations)}.

    Parallelism is picked to match the work's nature:
      • model OFF, max_workers>1 → PROCESS pool (the 90% is GIL-bound pure-Python; isolated memory
        makes 'no shared mutable state' structural).
      • model ON,  max_workers>1 → THREAD pool. The dominant cost is I/O — serial network locator
        calls (measured: 80 calls ≈ 19 min, one at a time). Threads overlap that wait (the GIL is
        released during the socket read), taking wall-time from ~N×latency toward ~N/workers×latency,
        while currency stays per-thread isolated (ContextVar ledger) and each file's failure is
        already a held record. Bounded to max_workers = the Vertex requests/min budget.
      • otherwise → serial (default max_workers=1) → byte-identical to the pre-parallel path.

    `base_currency` (the fund's user-confirmed base) is run-uniform — folded into each task tuple
    like rate_card/use_model — so every worker resolves currency with the same fund base."""
    if not tasks:
        return {}
    full = [(ck, co, pth, dom, acr, lbl, rate_card, use_model, base_currency)
            for (ck, co, pth, dom, acr, lbl) in tasks]
    if max_workers and max_workers > 1 and use_model:
        parent_m = llm.current_metrics()
        with ThreadPoolExecutor(max_workers=min(max_workers, len(full))) as ex:
            results = list(ex.map(_threaded_worker, full))     # ex.map preserves input (canonical) order
        for _ck, _rec, _obs, _sum in results:
            _merge_metrics(parent_m, _sum)
        return {ck: (rec, obs) for ck, rec, obs, _sum in results}
    if max_workers and max_workers > 1 and not use_model:
        with ProcessPoolExecutor(max_workers=min(max_workers, len(full))) as ex:
            results = list(ex.map(_extract_company_worker, full))
    else:
        results = [_extract_company_worker(t) for t in full]
    return {ck: (rec, obs) for ck, rec, obs in results}


@dataclass
class _Scan:
    """L2 front-end fact for ONE file, returned in canonical (content_fp) order. An MIS file carries only
    its resolution `hints` and NO grid — the grid is released inside the worker — so at 100 files the parent
    holds zero MIS grids. A fund-domain file (few) carries the re-parsed `prof` the fund pipeline needs.
    Routing is byte-identical to the serial path; this dataclass only changes WHERE the parse happens."""
    label: str
    path: str
    content_fp: str
    role: str
    hints: Optional[list] = None
    prof: Optional[dict] = None
    reason: str = ''


def _scan_one(label: str, path: str, *, use_model: bool) -> _Scan:
    """Profile + identity + structural route for ONE file, in-process (the serial/reference front end).
    Fund-domain keeps its `prof` (the parent needs the grid); MIS keeps only hints and drops the grid."""
    prof = profile_file(label, path)
    cfp = compute_identity(label, path).content_fp
    if prof.get('error'):
        return _Scan(label, path, cfp, 'error', reason=prof['error'])
    role = _route_file(prof, label=label, path=path, content_fp=cfp, use_model=use_model)
    if role == 'mis':
        return _Scan(label, path, cfp, 'mis', hints=_hints(prof))     # drop the grid — parent never holds it
    return _Scan(label, path, cfp, role, prof=prof)                   # fund-domain: parent needs the grid


def _scan_worker(task):
    """Parallel front-end unit (ProcessPoolExecutor, spawn). Route ONE file and return a PICKLABLE,
    GRID-FREE fact — the parsed grid never crosses the process boundary (for a fund-domain file the parent
    re-parses the few it needs; for MIS only the hints return). The worker releases its grid before
    returning so a reused pool worker never hoards every grid it touches (the per-worker memory wall).
    Model-off only: routing here is purely structural (the classifier tail stays in the serial path)."""
    label, path = task
    try:
        prof = profile_file(label, path)
        cfp = compute_identity(label, path).content_fp
        if prof.get('error'):
            return (label, path, cfp, 'error', None, prof['error'])
        role = _route_file(prof, label=label, path=path, content_fp=cfp, use_model=False)
        hints = _hints(prof) if role == 'mis' else None
        return (label, path, cfp, role, hints, '')
    finally:
        clear_parse_cache()
        clear_profile_cache()


def _route_all(files: List[Tuple[str, str]], *, use_model: bool, max_workers: int) -> List[_Scan]:
    """Front end: profile + identity + route every file, returned in canonical (content_fp) order — the
    single replacement for the old `_canonical_order` + serial routing loop. Serial (mw=1 or model) is the
    historical path verbatim (the byte-identical reference). Parallel (mw>1, model-off) fans the parse
    across a process pool and re-parses only the FEW fund-domain files in the parent, so the parent never
    holds an MIS grid and the read stops being the serial floor. Routing decisions are identical either
    way (same _route_file over the same prof); only WHERE the parse runs changes."""
    if max_workers and max_workers > 1 and not use_model and len(files) > 1:
        with ProcessPoolExecutor(max_workers=min(max_workers, len(files))) as ex:
            raw = list(ex.map(_scan_worker, list(files)))
        scans: List[_Scan] = []
        for label, path, cfp, role, hints, reason in raw:
            if role in ('mis', 'error'):
                scans.append(_Scan(label, path, cfp, role, hints=hints, reason=reason))
            else:                                    # fund-domain → parent re-parses the grid it needs (few)
                scans.append(_Scan(label, path, cfp, role, prof=profile_file(label, path)))
    else:
        scans = [_scan_one(label, path, use_model=use_model) for label, path in files]
    return sorted(scans, key=lambda s: (s.content_fp, s.label))


def _assert_conservation(files: List[Tuple[str, str]], reports: List[FileReport]) -> None:
    """Fail-closed file-conservation check: every input file INSTANCE must map to EXACTLY ONE terminal
    report. Keyed on the input PATH (per-upload instance identity), NOT the label — a label-multiset
    balances falsely when two DISTINCT files share a label and one is double-reported while the other is
    dropped (that residual belongs to a second guard); keying on path makes 'no uploaded file is ever
    dropped' airtight on its own. Raises PreingestConservationError (naming the missing/duplicated paths)
    rather than let a run finish having silently dropped a file the user uploaded. Factored out so the
    invariant can be red-before/green-after tested directly."""
    _in = Counter(pth for _lbl, pth in files)
    _out = Counter(r.path for r in reports)
    if _in != _out:
        _missing = _in - _out
        _dup = _out - _in
        raise PreingestConservationError(
            f'file conservation violated: in={sum(_in.values())} != out={sum(_out.values())}; '
            f'missing={dict(_missing)}; duplicated={dict(_dup)}')


def run(files: List[Tuple[str, str]], *, as_of: str, org: str = 'default',
        rate_card: RateCard = None, alias_store=None,
        reuse: Dict[str, Record] = None,
        store_dir: Optional[str] = None,
        base_currency: Optional[str] = None,
        model_provider: Callable = None, require_model: bool = False,
        max_workers: int = 1,
        progress: Optional[Callable] = None) -> RunResult:
    """files = [(label, absolute_path)]. Returns a RunResult with the CIR.
    `progress(pct:int, msg:str)` is called at stage boundaries for async workers.

    `require_model=True` runs a LIVE health-check at the door before touching any
    file: a 404 / auth / bad-config fault raises ModelConfigError and halts the run
    loudly (a broken model boundary can never again hide as a per-file hold). Every
    run — model-driven or purely deterministic — reports its boundary behaviour in
    RunResult.model_metrics so 'the model ran / didn't run' is measured, not assumed.

    `base_currency` is the fund's user-confirmed base reporting currency (fund-level
    setting, e.g. 'INR' for an Indian AIF). It is a THIRD positive-evidence source at
    resolve_currency, WEAKER than a file token or entity domicile: a statement carrying
    its OWN foreign token (or a foreign domicile) still HOLDS under it — the fund base
    can never override file evidence. Default None → the currency path is byte-identical
    (no-evidence still holds). Every figure resolved via the fund base is TAGGED
    (currency_user_confirmed) so (1) its provenance discloses it rests on the fund
    setting, not file evidence, and (2) RunResult.currency_report['base_currency_applied']
    surfaces those sites for per-batch review — a new foreign entrant with no marker can
    never be silently swept into the base currency unseen."""
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
    # Per-TENANT namespace for the durable golden store: the entry key is org-blind (content_fp + config
    # + logic), so one client's cached answer can NEVER be served to another even on one shared base dir —
    # the load-bearing multi-tenant isolation. None when store_dir is unset (store inactive → unchanged).
    _store_dir = golden_store.org_dir(store_dir, org)
    reuse = reuse or {}
    clear_profile_cache()   # reuse each file's grid within THIS run; never across runs
    clear_parse_cache()     # parse-once: reset the unified identity+grid parse cache per run
    _ccy_ledger = currency_ledger.CurrencyLedger()   # U6 Phase 2: capture every currency verdict this run
    currency_ledger.set_active(_ccy_ledger)          # (overwrites any leak from a prior aborted run)
    _p(5, 'Reading & routing files')
    # ── L2 front end: profile + identity + route every file, in canonical (content_fp) order. Parallel &
    # grid-free for the MIS bulk when max_workers>1 (model-off); byte-identical routing to the serial path.
    # The parent holds NO MIS grid — only the few fund-domain profs the fund pipeline needs. ──
    scans = _route_all(files, use_model=require_model, max_workers=max_workers)

    fund_paths: List[str] = []
    mis_facts: List[Tuple[str, str, str, list]] = []     # (label, path, content_fp, hints) — NO grid held
    fund_fin: List[Tuple[str, str, object]] = []         # (label, path, profile) — Phase-D fund files
    schedule_files: List[Tuple[str, str, object]] = []   # (label, path, profile) — 'fund' schedule
    unknown: List[str] = []
    reports: List[FileReport] = []
    for sc in scans:
        if sc.role == 'error':                       # opened-attempt failed to parse → HELD(parse_error)
            reports.append(FileReport(sc.label, 'error', 'read_error', reason=sc.reason,
                                      reason_code=HELD_PARSE_ERROR, path=sc.path))
        elif sc.role == 'fund':
            n = len(fund_anchor.build_fund_anchors([sc.path]))   # safety net: a real
            if n > _MAX_SCHEDULE_COMPANIES:                       # schedule is bounded
                unknown.append((sc.label, f'{n} candidate companies from one file — '
                                          f'not a real investment schedule (likely a misrouted MIS/ledger)'))
                reports.append(FileReport(sc.label, 'unknown', 'held',
                                          reason=f'{n} candidate companies — not a real schedule; held',
                                          reason_code=HELD_MISROUTED, path=sc.path))
            else:
                fund_paths.append(sc.path)
                schedule_files.append((sc.label, sc.path, sc.prof))   # also a ledger source (e.g. Exits)
                reports.append(FileReport(sc.label, 'fund', 'ok', path=sc.path))
        elif sc.role == 'mis':
            mis_facts.append((sc.label, sc.path, sc.content_fp, sc.hints))
        elif sc.role == 'fund_financials':
            fund_fin.append((sc.label, sc.path, sc.prof))
        else:
            unknown.append((sc.label, 'no time-series statement or investment schedule found'))
            reports.append(FileReport(sc.label, 'unknown', 'held',
                                      reason='no time-series statement or investment schedule found',
                                      reason_code=HELD_NO_CONTENT, path=sc.path))

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
        for _attr in ('sector', 'stage', 'investment_date', 'instrument', 'valuation_method', 'domicile',
                      'isin', 'listing_exchange', 'share_type'):
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

    # ── Q2: quoted / unquoted classification — an ADDITIVE overlay over the untouched
    # per-company FV. Writes plain-scalar verdict fields on each record and appends ONE
    # HARD partition-tie check; it NEVER rebinds the fair_value/cost/irr Figures, so the
    # NAV / TVPI / MOIC surfaces are neutral by construction. Classification is
    # positive-evidence + fail-closed (quoted needs a hard anchor; a private method infers
    # unquoted; ambiguity/contradiction HOLD). INFERRED and HELD verdicts are disclosed so
    # a derived label is never read as an extracted fact. ──
    _pi_recs = [r for r in cir.records if r.domain == 'portfolio_investments']
    if _pi_recs:
        _qu_items = []
        for r in _pi_recs:
            _cls = quoted_unquoted.classify(quoted_unquoted.signals_from_fields(r.fields))
            r.fields['is_quoted'] = _cls.is_quoted
            r.fields['quoted_basis'] = _cls.basis
            r.fields['quoted_evidence'] = _cls.evidence or _cls.reason
            _fvf = r.fields.get('fair_value')
            _fvv = _fvf.value_cr if (isinstance(_fvf, Figure) and _fvf.confirmed) else None
            _co = r.fields.get('company') or (r.entity_id or '')
            _qu_items.append((_co, _fvv, _cls))
            if _cls.basis == quoted_unquoted.INFERRED:
                cir.disclose('quoted_unquoted_inferred',
                             f'{_co}: classified {_cls.classification} — {_cls.evidence}; inferred from '
                             'valuation methodology (no source-stated listing data)', entity=_co)
            elif _cls.is_quoted is None:
                cir.disclose('quoted_unquoted_held', f'{_co}: quoted/unquoted HELD — {_cls.reason}',
                             entity=_co)
        _qu_part = quoted_unquoted.partition(_qu_items)
        _qu_fv = [r.fields.get('fair_value') for r in _pi_recs]
        _qu_total = (sum(f.value_cr for f in _qu_fv if getattr(f, 'confirmed', False))
                     if _qu_fv and all(getattr(f, 'confirmed', False) for f in _qu_fv) else None)
        cir.checks.append(quoted_unquoted.tie_check(_qu_part, _qu_total))

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
        currency_ledger.context(entity='(fund)', source_file=label)
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
        reports.append(FileReport(label, 'fund_financials', 'ok', content_fp=fp, path=path))
        if not produced:                           # recognised fund file, no slice concepts yet
            cir.disclose('fund_no_flows', f'{label}: fund file recognised; no capital-account '
                         'flows, LP register, terms or NAV signals found', entity=label)

    # ── REFERENCE-ONLY aggregate comparators (the fund's OWN top-down portfolio revenue/EBITDA
    # budget+actual, and stated realised-gross) — scanned across ALL fund-domain files, financials
    # AND schedules, because these lines live on EITHER (budget-vs-act on the accounts file, the
    # realised-gross line on the valuations/exits schedule). Filed under cir.comparators — never a
    # Record/Figure, so they can never reach the actuals emit surface; the reconciliation stage turns
    # them into soft-check rows. (Absent → the soft stage discloses, never fabricates.) ──
    for label, path, prof in fund_fin + schedule_files:
        fp = compute_identity(label, path).content_fp
        for _comp in fund_terms.extract_reference_comparators(label, prof, content_fp=fp):
            cir.add_comparator(_comp)
        _rg = fund_terms.extract_realised_gross(label, prof, content_fp=fp)
        if _rg is not None:
            cir.add_comparator(_rg)

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
        currency_ledger.context(entity='(fund ledger)', source_file=label)
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

    # ── ACCRUED-CARRY WATERFALL reconciliation (finance build 1): run the European whole-fund
    # waterfall at current fair value under EVERY plausible UNSTATED convention (hurdle basis ×
    # compounding × hard/soft) and reconcile the INDEPENDENTLY-computed accrued carry against the
    # fund's reported provision. Struck AFTER the dated capital-call ledger exists (the preferred
    # return accrues on the real drawdown dates) and after terms / NAV / committed-base resolve.
    # CHECK, NEVER FIT: the scenario table is computed with no knowledge of the reported figure; the
    # reported figure only partitions it. Emits SOFT rows only — a soft "approx" carry provision must
    # never BLOCK the workbook; an underdetermined or discrepant figure is HELD + fully disclosed. ──
    if canonical_terms is not None and nav_inputs:
        _call_recs = [r for r in cir.records if r.domain == 'capital_calls']
        _dist_recs_wf = [r for r in cir.records if r.domain == 'distributions']
        _wf_inputs, _rep_carry, _rep_cell = waterfall.build_waterfall_inputs(
            canonical_terms=canonical_terms, nav_inputs=nav_inputs, capital=_fin,
            committed_base=committed_base, call_records=_call_recs, dist_records=_dist_recs_wf,
            as_of=as_of)
        wf_checks, wf_disc, wf_verdict = waterfall.reconcile_waterfall(
            _wf_inputs, reported_carry=_rep_carry, reported_carry_cell=_rep_cell, source_fp=last_fp)
        for chk in wf_checks:
            cir.checks.append(chk)
            if chk.get('class') == 'hard' and chk.get('status') in ('fail', 'indeterminate'):
                cir.disclose('waterfall_reconciliation',
                             f'{chk["id"]} — {chk.get("detail", "")}', entity='fund')
        for d in wf_disc:
            cir.disclose(d.get('kind', 'waterfall'), d.get('detail', ''), entity=d.get('entity', 'fund'))

        # ── CLAWBACK reconciliation (finance build 2): rides the waterfall entitlement. GP clawback =
        # max(0, carry RECEIVED − carry ENTITLED). Received = Σ gp_carry from the distributions ledger;
        # entitlement = the waterfall verdict (point when inferred, range when underdetermined). Fail-
        # closed: an incomplete received history HOLDS, never asserts 0. When received is 0 (this fund),
        # clawback is provably 0 even though the entitlement is held — no false liability. ──
        _dist_recs = [r for r in cir.records if r.domain == 'distributions']
        _cr_received, _cr_complete = waterfall.carry_received_from_distributions(_dist_recs)
        _holdback = waterfall._term_value(canonical_terms, 'clawback_holdback')
        cb_checks, cb_disc = waterfall.reconcile_clawback(
            carry_received=_cr_received, received_complete=_cr_complete,
            verdict=wf_verdict, holdback_rate=_holdback)
        for chk in cb_checks:
            cir.checks.append(chk)
        for d in cb_disc:
            cir.disclose(d.get('kind', 'clawback'), d.get('detail', ''), entity='fund')

    # ── MIS files → resolve (pass 1) ──
    _p(45, f'Resolving {len(mis_facts)} company files')
    review: List[ReviewFile] = []
    extraction: Dict[str, Record] = {}
    resolved = []   # (label, path, fp, entity_key, reason)
    for label, path, fp, hints in mis_facts:
        # Alias-store keys are the DISTINCTIVE FILENAME ONLY. Content hints help the
        # closed-set MATCH find the company name, but must NEVER be alias keys — a
        # generic hint ('Monthly MIS', a month name) shared by two files would
        # cross-attribute one file's numbers onto another company (the U4 bug).
        # `fp`/`hints` were computed once in the front-end scan (grid released there) — the
        # parent never re-opens an MIS file, so no MIS grid is held here (L2 memory contract).
        alias_ids = [label]
        file_text = label + ' ' + ' '.join(hints)
        entity_key, reason = _resolve_mis(alias_ids, file_text, anchors, store)
        resolved.append((label, path, fp, entity_key, reason))

    # ── BIJECTION HARD-GUARD (U4): each company may be claimed by at most ONE file.
    # If two files resolve to the SAME company this run, that is a resolution error
    # ('Hubbler ×3') — HOLD every claimant and flag, never emit scrambled records.
    # The highest-stakes invariant (right numbers under the right name) now has a
    # structural check, not just careful code. ──
    claim_counts = Counter(ek for *_r, ek, _rz in resolved if ek is not None)
    collided = {ek for ek, n in claim_counts.items() if n > 1}

    # ── L1 PRE-SCAN: dispatch the slow, pure per-file extraction (extract_company, the ~90% of a
    # large fund's cost) across a process pool. Only cache-MISSES are dispatched (reuse/golden are
    # cheap parent-side lookups); each unique ck is computed once. The emit loop below then consumes
    # the precomputed results VERBATIM — same CIR/report/disclosure order — so max_workers=1 (default)
    # is byte-identical to the pre-parallel path and max_workers>1 must prove byte-identical to it. ──
    _seen_ck, _tasks = set(), []
    for _lbl, _pth, _fp, _ek, _rz in resolved:
        if _ek is None or _ek in collided:
            continue
        _ca = anchors.get(_ek)
        _ck = extraction_cache_key(_fp, as_of=as_of, rate_card_id=rate_card.card_id,
                                   anchor_cr=_ca.anchor_cr, domicile=_ca.domicile,
                                   use_model=require_model, base_currency=base_currency)
        if _ck in _seen_ck or reuse.get(_ck) is not None:
            continue
        if _store_dir and golden_store.get(_store_dir, _ck) is not None:
            continue
        _seen_ck.add(_ck)
        _tasks.append((_ck, _ca.company, _pth, _ca.domicile, _ca.anchor_cr, _lbl))
    _precomputed = _run_extractions(_tasks, rate_card=rate_card,
                                    use_model=require_model, max_workers=max_workers,
                                    base_currency=base_currency)

    # ── emit (pass 2) ──
    _p(60, f'Extracting {len(mis_facts)} company files')
    for label, path, fp, entity_key, reason in resolved:
        if entity_key is None:
            reports.append(FileReport(label, 'mis', 'held', reason=reason, content_fp=fp,
                                      reason_code=HELD_UNRESOLVED_IDENTITY, path=path))
            review.append(ReviewFile(label, fp, [label], candidates, reason))
            cir.disclose('held_file', f'{label}: {reason}', entity=label)
            continue
        ca = anchors.get(entity_key)
        if entity_key in collided:                # collision → hold, never emit
            others = [l for l, *_x, ek, _y in resolved if ek == entity_key and l != label]
            why = (f'resolution collision — {ca.company!r} also claimed by '
                   f'{", ".join(others)}; held to avoid mixing companies')
            reports.append(FileReport(label, 'mis', 'held', entity_id=ca.company,
                                      reason=why, content_fp=fp, reason_code=HELD_AMBIGUOUS_MATCH, path=path))
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
                                  use_model=require_model, base_currency=base_currency)
        currency_ledger.context(entity=ca.company, source_file=label)
        # Phase 2.4: the reuse cache is the in-run/in-memory layer; the durable golden store (opt-in
        # via store_dir, namespaced per-tenant as _store_dir) is the cross-PROCESS layer. Both are keyed
        # by the SAME provably-complete ck, so a changed value (→ new content_fp → new ck) is a guaranteed
        # miss in BOTH; a logic change (→ new net_logic_version → new ck) transparently rebuilds both. The
        # ck is org-blind, so cross-tenant isolation is carried by _store_dir's per-org path, never the key.
        # store_dir=None → _store_dir is None → default, behaviour identical to before this phase.
        rec = reuse.get(ck)                        # incremental: reuse prior extraction (this session)
        from_store = False
        if rec is None and _store_dir:
            rec = golden_store.get(_store_dir, ck)  # durable: prior process's extraction, same ck (this org)
            from_store = rec is not None
        if rec is None:
            # computed by the L1 pre-scan (parallel when max_workers>1); the worker ran it under a
            # local currency ledger and returned its verdicts — merge them HERE, at this file's emit
            # point, so the parent ledger's sequence matches a sequential run exactly.
            rec, _obs = _precomputed[ck]
            _ccy_ledger.merge_observations(_obs)
            # Carry the currency verdicts WITH the record so a future CACHE HIT can replay them. Currency
            # detection/conversion is deterministic and model-free, so it must be reconstructed on EVERY
            # run — otherwise a repeat run of already-seen files serves the held figures but loses the
            # 'these need a rate' signal, the uncovered report comes back empty, and the rate-card prompt
            # silently never appears (the exact repeat-run bug this closes).
            rec.currency_obs = list(_obs)
        else:                                     # CACHE HIT — same file+config, new attribution only.
            # Replay the served file's currency verdicts into the run ledger (re-stamped to the current
            # company) so the uncovered-currency report — and thus the rate-card prompt — is IDENTICAL to
            # a fresh extraction. A pre-fix entry carries none → replays nothing, but its logic_version
            # has changed, so it is already a guaranteed miss that recomputes and stores them.
            _hit_obs = currency_ledger.restamp(getattr(rec, 'currency_obs', None),
                                               entity=ca.company, source_file=label)
            rec = Record(rec.domain, entity_id=ca.company, fields=dict(rec.fields))
            rec.fields['company'] = ca.company
            rec.currency_obs = _hit_obs
            _ccy_ledger.merge_observations(_hit_obs)
        extraction[ck] = rec
        if _store_dir and not from_store:         # persist fresh computes (never re-write a disk hit)
            golden_store.put(_store_dir, ck, rec)
        if '_note' in rec.fields:                 # resolved, but no usable statement (or a per-file parse failure)
            _note = str(rec.fields['_note'])
            _code = HELD_PARSE_ERROR if _note.startswith('parse_error') else HELD_NO_CONTENT
            reports.append(FileReport(label, 'mis', 'held', entity_id=ca.company,
                                      reason=_note, content_fp=fp, reason_code=_code, path=path))
            cir.disclose('held_file', f'{label}: {rec.fields["_note"]}', entity=ca.company)
            continue
        cir.add(rec)
        reports.append(FileReport(label, 'mis', 'attributed', entity_id=ca.company,
                                  reason=reason, content_fp=fp, path=path))

    # ── FILE-CONSERVATION INVARIANT (fail-closed): every input file → EXACTLY ONE terminal report
    # (attributed or held(reason)). Runs in production on every run; a per-file parse failure already
    # became HELD in the worker, so even a corrupt/unreadable file is accounted for here, never dropped. ──
    _assert_conservation(files, reports)

    # reverse coverage — investments with no MIS this run (disclosed gaps)
    attributed = {r.entity_id for r in reports if r.status == 'attributed'}
    for key, ca in sorted(anchors.items()):
        if ca.company not in attributed:
            cir.disclose('investment_without_mis',
                         f'{ca.company!r} had no attributed MIS file this run', entity=ca.company)

    # ── portfolio-vs-fund soft correspondence (U7 flagship): bottom-up Σ of the company MIS
    # revenue/EBITDA vs the fund's OWN top-down aggregate (a reference-only comparator). Struck
    # HERE because it needs BOTH the company records and the fund comparators. COVERAGE-aware
    # first (a held company contributes 0 — the row is a completeness meter, not a bare variance),
    # period-aware second (company flows annualised to the fund's stated basis). Soft → never blocks. ──
    for chk in portfolio_correspondence.build(cir):
        cir.checks.append(chk)

    # ── SEBI COMPLIANCE-RECONCILIATION (finance build 3): declarative, category-aware, fail-closed.
    # Struck HERE because it needs BOTH the fund records (LP register, terms, corpus) and the company
    # records (per-investee cost → concentration numerator). Each rule's INDEPENDENT value is computed
    # from the CIR and tied to the compliance-sheet remark located by SEBI ref (parenthetical-preserving,
    # so 10(b)/(c)/(d)/(f) never collide). Verdicts ride the spine as SOFT rows (PASS/FLAG/HELD/ABSENT).
    # Concentration stays HELD until investable_funds (corpus − est. expenditure, Reg 2(1)(p)) is supplied
    # — never FV-basis, never back-solved from the reported figure (P3/P4). ──
    _sebi_grids = [prof['grid'] for _l, _pth, prof in fund_fin]
    if _sebi_grids:
        _sebi_prim = sebi_compliance.build_primitives(
            lp_registers=lp_registers, cir_records=cir.records, grids=_sebi_grids, investable_funds_cr=None)
        sebi_checks, sebi_disc = sebi_compliance.reconcile_compliance(_sebi_prim, _sebi_grids)
        cal_checks, cal_disc = sebi_compliance.check_calendar(
            [r for r in cir.records if r.domain == 'sebi_calendar'])
        for chk in sebi_checks + cal_checks:
            cir.checks.append(chk)
        for d in sebi_disc + cal_disc:
            cir.disclose(d.get('kind', 'sebi'), d.get('detail', ''), entity='fund')

    _p(90, 'Assembling consolidated record')
    logger.info('[preingest3] run complete — model boundary: %s', metrics.summary())
    # U6 Phase 2: build the uncovered-currency report from every verdict observed this run,
    # then release the ledger so it can never bleed into another run. A reused-cache entity is not
    # re-extracted, but its currency verdicts were CARRIED with the cached record and REPLAYED into the
    # ledger at its emit point above (currency is deterministic/model-free), so the report — and the
    # rate-card prompt — is identical whether a file was freshly extracted or served from the cache.
    # A company whose schedules disagreed on domicile in a way that implies DIFFERENT currencies has
    # had its domicile withheld (fund_anchor._resolve_domicile → None), so U6 already fail-closes its
    # currency for lack of geo evidence; disclose WHY so the fail-closed hold is explained, not silent.
    for ca in anchors.values():
        if getattr(ca, 'domicile_conflict', False):
            cir.disclose('domicile_conflict',
                         f'{ca.company}: schedules disagree on domicile implying different currencies — '
                         f'domicile withheld and currency held (fail-closed), resolve to confirm',
                         entity=ca.company)
    _foreign_dom = {ca.company: units.expected_currency(getattr(ca, 'domicile', None))
                    for ca in anchors.values()
                    if units.expected_currency(getattr(ca, 'domicile', None)) not in (None, 'INR')}
    # domicile STATED but non-India and unmapped → cannot be named; disclosed so it never hides
    _unmapped_dom = {ca.company: getattr(ca, 'domicile', None) for ca in anchors.values()
                     if getattr(ca, 'domicile', None) and units.expected_currency(ca.domicile) is None}
    _currency_report = _ccy_ledger.uncovered_report(rate_card, foreign_domiciles=_foreign_dom,
                                                     unmapped_foreign_domiciles=_unmapped_dom)
    currency_ledger.set_active(None)
    return RunResult(cir=cir, files=reports, review_queue=review, extraction=extraction,
                     model_metrics=metrics.summary(),
                     model_health=(health.__dict__ if health is not None else None),
                     currency_report=_currency_report)
