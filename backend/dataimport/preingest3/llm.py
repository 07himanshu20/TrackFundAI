"""
The single structured-output gateway for every preingest3 model call (S3
classify, S4 locate, U4 bounded adjudication). Three jobs:

  1. STRUCTURED JSON — model calls return JSON; we parse defensively (fenced
     blocks, leading prose) so a stray token never crashes a run.
  2. PER-CALL CHECKPOINT (the deferred call-layer fix item #4) — every result is
     frozen to disk under a DETERMINISTIC key (call kind + content inputs +
     prompt hash + contract version). A re-run, a resumed worker, or a retried
     file re-serves completed calls instantly and only the un-checkpointed call
     re-executes. "Every external call … checkpoints its result on completion."
  3. HARDENED TRANSPORT — delegates to api.gemini_service.generate_content, which
     now carries a real socket read timeout, streaming inter-chunk idle abort,
     and jittered retry. Streaming is on by default for the heavy locator calls.

The model is a POINTER, never a source of numbers — this gateway is used only to
classify and to locate; it is never asked to compute, and callers must treat its
output as coordinates/labels, not values (build rules #1–#3).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from . import rate_governor
from .contract import contract_signature

logger = logging.getLogger(__name__)

# ── model provider (dependency injection — keeps this library Django-free) ────
# preingest3 is a standalone extraction library; it must not hard-import the
# Django `api` app at module load. The model transport is injected instead. A
# host (the Django adapter) calls set_model_provider() once; if none is injected
# we LAZILY fall back to `api.gemini_service` so existing in-app callers keep
# working with zero wiring. In a truly standalone process with no Django, the
# resolver returns None → call_json yields a typed ERROR (holds the residue),
# never a silent blank. The model is a POINTER, never a source of numbers.
_MODEL_PROVIDER = None


def set_model_provider(fn) -> None:
    """Inject the structured-output transport. `fn(prompt, *, response_mime_type,
    temperature, read_timeout_s, stream, model) -> resp` where resp has `.text`."""
    global _MODEL_PROVIDER
    _MODEL_PROVIDER = fn


def _resolve_provider():
    if _MODEL_PROVIDER is not None:
        return _MODEL_PROVIDER
    try:                                    # lazy, optional Django fallback
        from api import gemini_service
        return gemini_service.generate_content
    except Exception:                       # noqa: BLE001 — no Django / no model here
        return None

# Model for the whole pre-ingestion layer. Overridable by env without code edits
# (the exact Vertex model id should be confirmed in the project's model garden;
# if this id is not enabled there, calls return a typed ERROR — never void data).
PREINGEST_MODEL = os.environ.get('PREINGEST3_MODEL', 'gemini-2.5-flash')


def _thinking_kwargs(model: str) -> dict:
    """Locate is a POINTING task, not a reasoning task, so we run the model with thinking OFF/MINIMAL:
    measured 15.2s -> 4.9s per call on 2.5-flash (3.1x), coverage-identical (the deterministic layer
    re-reads + re-verifies every located row regardless, so less reasoning can only ever cost a HOLD,
    never a wrong number). Model-family aware + forward-compatible for the gemini-3.6-flash migration:
      gemini-2.x -> thinking_budget=0        (numeric budget)
      gemini-3.x -> thinking_level='minimal' (3.x REMOVED thinking_budget -> sending it 400s)
    Rollback valve: PREINGEST3_THINKING=default restores the model's own default reasoning.
    Passed as plain kwargs; the Vertex adapter (api.gemini_service) builds the SDK ThinkingConfig, so
    this library stays free of provider SDK types."""
    if os.environ.get('PREINGEST3_THINKING', 'lean') == 'default':
        return {}
    m = (model or '').lower()
    if m.startswith('gemini-3'):
        return {'thinking_level': 'minimal'}
    if m.startswith('gemini-2.'):
        return {'thinking_budget': 0}
    return {}


def escalation_thinking_kwargs(model: str) -> dict:
    """Tier-2 (adaptive) thinking: turn reasoning ON for a re-locate of a concept Tier-1 left
    UNDETERMINED (not confirmed-absent). Bounded to the hard concept, so the fast default stands on
    the easy hits. gemini-2.x → a positive thinking_budget; gemini-3.x → thinking_level='low'
    (re-tune minimal-vs-low on 3.6 when reachable). Values env-overridable for tuning on the messy
    corpus. Correctness unchanged: more reasoning only improves POINTING; the deterministic verify
    still guards every row, so this can only recover a hold, never emit a wrong number."""
    m = (model or '').lower()
    if m.startswith('gemini-3'):
        return {'thinking_level': os.environ.get('PREINGEST3_ESCALATE_LEVEL', 'low')}
    if m.startswith('gemini-2.'):
        return {'thinking_budget': int(os.environ.get('PREINGEST3_ESCALATE_BUDGET', '2048'))}
    return {}

# ── the three, type-distinct outcomes of every model call ────────────────
# The single most important rule after the timeout work: a failed call is NOT a
# value. It can NEVER collapse into 'absent'/empty and become data.
OK = 'ok'            # the model ran and returned parseable structured output
ABSENT = 'absent'    # the model ran and validly said "nothing here" (a real answer)
ERROR = 'error'      # 429 / timeout / 5xx / dropped / GARBLED reply — not data


@dataclass
class CallResult:
    kind: str
    data: Optional[object] = None
    reason: str = ''

    @property
    def is_ok(self) -> bool:
        return self.kind == OK

    @property
    def is_error(self) -> bool:
        return self.kind == ERROR


class ModelConfigError(RuntimeError):
    """A model-call outcome that is a CONFIGURATION / AUTH fault — NOT a data answer.

    404 (unknown model id), 401 / 403 (auth), 400 (malformed request), or a missing
    key / project. Such a fault will fail IDENTICALLY for every file in the run, so
    the only honest response is to HALT loudly — never swallow it into a per-file
    hold. This is the exact failure class that hid a wrong model id for weeks by
    wearing the costume of a legitimate 'held for review'. It must scream, not hide.
    """


# HTTP statuses that mean the request/config is wrong → same failure for every file
# → halt loudly. 429 is deliberately EXCLUDED (rate-limit → backoff, not a config bug).
_FATAL_STATUS = frozenset({400, 401, 403, 404})


def _status_of(exc) -> Optional[int]:
    """The HTTP status of a model-transport exception, if it carries one. google.genai
    APIError exposes `.code`; older/other shapes use `.status_code`."""
    code = getattr(exc, 'code', None)
    if not isinstance(code, int):
        code = getattr(exc, 'status_code', None)
    return code if isinstance(code, int) else None


def _classify_exc(exc) -> tuple:
    """Map a raised transport exception to one of three boundary outcomes:
      'fatal'      → config/auth (404/401/403/400) or missing creds → halt the run
      'rate_limit' → 429 → back off (honour Retry-After) and retry
      'transient'  → 5xx / timeout / connection drop → hold-and-retry, never halt
    Returns (kind, status_code_or_None)."""
    code = _status_of(exc)
    if code == 429:
        return 'rate_limit', code
    if code in _FATAL_STATUS:
        return 'fatal', code
    # get_client() raises ValueError when GOOGLE_API_KEY / GOOGLE_CLOUD_PROJECT is
    # absent — that is a deployment/config fault, identical for every file → fatal.
    if isinstance(exc, ValueError):
        return 'fatal', code
    return 'transient', code


def _retry_after_s(exc) -> Optional[float]:
    """Honour a server-provided Retry-After header on a 429, if present."""
    resp = getattr(exc, 'response', None)
    headers = getattr(resp, 'headers', None)
    if not headers:
        return None
    try:
        val = headers.get('Retry-After') or headers.get('retry-after')
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


# Rate-limit (429) retry budget lives HERE because the transport treats 429 as a
# non-retryable ClientError. Kept small — a 429 that survives a few backoffs is a
# real quota wall, and the file should hold (ERROR) rather than block the run.
_RL_ATTEMPTS = int(os.environ.get('PREINGEST3_RL_ATTEMPTS', '4'))
_RL_BACKOFF_BASE_S = float(os.environ.get('PREINGEST3_RL_BACKOFF_S', '2.0'))
_RL_BACKOFF_CAP_S = float(os.environ.get('PREINGEST3_RL_BACKOFF_CAP_S', '30'))


def _rl_sleep(attempt: int, retry_after: Optional[float]) -> None:  # patchable in tests
    if retry_after is not None:
        time.sleep(min(retry_after, _RL_BACKOFF_CAP_S))
        return
    time.sleep(min(_RL_BACKOFF_CAP_S, _RL_BACKOFF_BASE_S * (2 ** (attempt - 1))))


# ── per-run model-call metrics ───────────────────────────────────────────
# A run-scoped accumulator so every run can report exactly how the boundary
# behaved ("N calls, M ok, K held-on-error, R retries, Ls latency"). Stored in a
# ContextVar so concurrent runs in separate worker threads keep isolated counts
# (each thread gets its own context) — no cross-run bleed, no global lock.
@dataclass
class CallMetrics:
    calls: int = 0
    ok: int = 0
    error: int = 0
    fatal: int = 0
    retries: int = 0
    latency_s: float = 0.0
    by_status: dict = field(default_factory=dict)

    def _bump_status(self, code):
        k = str(code) if code is not None else 'none'
        self.by_status[k] = self.by_status.get(k, 0) + 1

    def summary(self) -> dict:
        return {
            'calls': self.calls, 'ok': self.ok, 'error': self.error,
            'fatal': self.fatal, 'retries': self.retries,
            'latency_s': round(self.latency_s, 3), 'by_status': dict(self.by_status),
        }


_CURRENT_METRICS: contextvars.ContextVar = contextvars.ContextVar(
    'preingest3_call_metrics', default=None)


def new_metrics() -> CallMetrics:
    """Start a fresh per-run metrics accumulator for THIS thread's run."""
    m = CallMetrics()
    _CURRENT_METRICS.set(m)
    return m


def current_metrics() -> Optional[CallMetrics]:
    return _CURRENT_METRICS.get()


def _m() -> Optional[CallMetrics]:
    return _CURRENT_METRICS.get()


# ── model boundary health-check ──────────────────────────────────────────
_HEALTH_PROMPT = 'Reply with exactly this JSON and nothing else: {"ok": true}'


@dataclass
class HealthResult:
    ok: bool
    model: str
    latency_s: float
    detail: str = ''


def health_check(*, model: str = None, read_timeout_s: float = 20.0) -> HealthResult:
    """One trivial LIVE model call to prove the boundary is healthy BEFORE any file
    is processed. A 404 / auth / bad-config raises ModelConfigError (halt loud — so a
    wrong model id or missing credential can never again hide as a per-file hold). A
    transient fault returns ok=False with a reason (caller may proceed degraded).
    Bypasses the checkpoint cache — a health-check must exercise the live transport."""
    provider = _resolve_provider()
    if provider is None:
        raise ModelConfigError(
            'no model provider injected and no Django fallback — cannot health-check '
            'the model boundary; refusing to run the model path blind')
    mdl = model or PREINGEST_MODEL
    t0 = time.monotonic()
    try:
        resp = provider(_HEALTH_PROMPT, response_mime_type='application/json',
                        temperature=0.0, read_timeout_s=read_timeout_s,
                        stream=False, model=mdl)
    except Exception as e:  # noqa: BLE001
        kind, code = _classify_exc(e)
        if kind == 'fatal':
            raise ModelConfigError(
                f'model health-check FAILED for {mdl!r}: HTTP {code or "config-fault"} '
                f'({e.__class__.__name__}: {str(e)[:200]}). This is a configuration/auth '
                f'fault (model id or credentials), not a data hold — fix it before '
                f'processing any file; refusing to run against a broken model boundary.'
            ) from e
        return HealthResult(False, mdl, time.monotonic() - t0,
                            detail=f'{kind}: {e.__class__.__name__}: {str(e)[:160]}')
    dt = time.monotonic() - t0
    data = _extract_json(getattr(resp, 'text', '') or '')
    if data is None:
        return HealthResult(False, mdl, dt, detail='live reply not parseable as JSON')
    return HealthResult(True, mdl, dt, detail='ok')


_CACHE_DIR = os.path.join(
    os.environ.get('PREINGEST2_CACHE_ROOT')  # reuse the configured media root if set
    or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'media'),
    'preingest3_calls',
)


def _key(kind: str, inputs: str, prompt: str) -> str:
    blob = f'{kind}\x1f{contract_signature()}\x1f{inputs}\x1f{prompt}'
    return hashlib.sha256(blob.encode('utf-8', 'replace')).hexdigest()


def _cache_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, key[:2], key + '.json')


def _cache_get(key: str):
    p = _cache_path(key)
    if os.path.exists(p):
        try:
            with open(p, 'r') as fh:
                return json.load(fh)
        except Exception:
            return None
    return None


def _cache_put(key: str, value):
    p = _cache_path(key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(value, fh)
    os.replace(tmp, p)   # atomic write — a half-written checkpoint never poisons the cache


def _extract_json(text: str):
    """Best-effort JSON out of a model reply — handles ```json fences and
    leading/trailing prose. Returns None on genuine failure."""
    if not text:
        return None
    s = text.strip()
    if s.startswith('```'):
        s = s.split('```', 2)[1]
        if s.lstrip().lower().startswith('json'):
            s = s.lstrip()[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # fall back to the first balanced object/array
    for opener, closer in (('{', '}'), ('[', ']')):
        i, j = s.find(opener), s.rfind(closer)
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    return None


def call_json(kind: str, inputs_signature: str, prompt: str, *,
              read_timeout_s: float = 90.0, stream: bool = True,
              temperature: float = 0.0, use_cache: bool = True,
              thinking: Optional[dict] = None) -> CallResult:
    """Run one structured model call and return a TYPED outcome. The boundary is
    now three-valued at the transport level, and the classes are NOT interchangeable:

      • CONFIG / AUTH fault (404 / 401 / 403 / 400 / missing creds) → raises
        ModelConfigError. This HALTS the run — it will fail identically for every
        file, so it must scream, never collapse into a silent per-file hold. This is
        the swallowed-404 the whole project was blind to for weeks.
      • 429 rate-limit → backs off (honouring Retry-After) and retries a small budget;
        if the quota wall persists → CallResult(ERROR) so the file HOLDS, not crashes.
      • 5xx / timeout / dropped / GARBLED reply → CallResult(ERROR). The transport
        already retried transient faults; a surviving one means 'hold this file and
        retry the run', NEVER an extracted blank.
      • genuine parseable answer → CallResult(OK, data).

    ERROR is never returned as empty/None that a caller could read as 'no data'; only
    a real model answer is OK. ModelConfigError is never caught here — it propagates."""
    key = _key(kind, inputs_signature, prompt)
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            data = hit.get('data') if isinstance(hit, dict) and '_pi3' in hit else hit
            return CallResult(OK, data)

    provider = _resolve_provider()
    if provider is None:                     # no transport wired (standalone, no Django)
        return CallResult(ERROR, reason='no model provider injected — holding residue')

    for attempt in range(1, _RL_ATTEMPTS + 1):
        # Proactive rate GOVERNOR (default off → instant True → byte-identical). Bounds the aggregate
        # issue rate under the Vertex quota across all threads; fail-closed to a HOLD if the wait budget
        # is exhausted — never a flood, never a wrong number. Reactive 429 backoff below is the backstop.
        if not rate_governor.acquire(rate_governor.est_tokens(prompt)):
            return CallResult(ERROR, reason='rate-governor: issue-rate budget exhausted — held; re-run')
        m = _m()
        if m is not None:
            m.calls += 1
        t0 = time.monotonic()
        try:
            _tk = thinking if thinking is not None else _thinking_kwargs(PREINGEST_MODEL)
            _kw = dict(response_mime_type='application/json', read_timeout_s=read_timeout_s,
                       stream=stream, model=PREINGEST_MODEL, **_tk)
            if not PREINGEST_MODEL.lower().startswith('gemini-3'):
                _kw['temperature'] = temperature   # 3.x ignores temperature (400 on future gens)
            resp = provider(prompt, **_kw)
        except Exception as e:  # noqa: BLE001
            dt = time.monotonic() - t0
            cls, code = _classify_exc(e)
            if m is not None:
                m.latency_s += dt
                m._bump_status(code)
            if cls == 'fatal':
                if m is not None:
                    m.fatal += 1
                logger.error('[preingest3.llm] %s: FATAL config/auth fault (HTTP %s, %s) '
                             '— halting run', kind, code, e.__class__.__name__)
                raise ModelConfigError(
                    f'{kind}: model call returned HTTP {code or "config-fault"} '
                    f'({e.__class__.__name__}: {str(e)[:200]}). Configuration/auth fault '
                    f'(model id / credentials), not a data hold — halting the run.') from e
            if cls == 'rate_limit' and attempt < _RL_ATTEMPTS:
                if m is not None:
                    m.retries += 1
                # Log the FULL 429 body — it names the exact quota METRIC exceeded (e.g.
                # generate_content_requests_per_minute_per_project_per_region). That is the ground truth
                # for sizing the rate governor; a rebranded console label is not.
                logger.warning('[preingest3.llm] %s: 429 rate-limit (attempt %d/%d) — backing off; '
                               'quota detail: %s', kind, attempt, _RL_ATTEMPTS, str(e)[:400])
                _rl_sleep(attempt, _retry_after_s(e))
                continue
            if m is not None:
                m.error += 1
            logger.warning('[preingest3.llm] %s: call FAILED (%s HTTP %s) — ERROR state, not empty',
                           kind, e.__class__.__name__, code)
            return CallResult(ERROR, reason=f'{e.__class__.__name__} (HTTP {code}): {str(e)[:140]}')

        dt = time.monotonic() - t0
        if m is not None:
            m.latency_s += dt
        data = _extract_json(getattr(resp, 'text', '') or '')
        if data is None:
            # a reply that arrived but can't be parsed is a FAILURE, not "absent"
            if m is not None:
                m.error += 1
            logger.warning('[preingest3.llm] %s: unparseable reply — ERROR state, not empty', kind)
            return CallResult(ERROR, reason='garbled/unparseable model reply')
        if m is not None:
            m.ok += 1
        if use_cache:
            _cache_put(key, {'_pi3': 1, 'kind': kind, 'data': data})
        return CallResult(OK, data)

    # 429 retry budget exhausted → hold the file (real quota wall), never crash the run
    m = _m()
    if m is not None:
        m.error += 1
    logger.warning('[preingest3.llm] %s: 429 rate-limit — retries exhausted, holding', kind)
    return CallResult(ERROR, reason='rate-limited (429) — retries exhausted; holding file')


def cache_stats() -> dict:
    n = 0
    for root, _dirs, files in os.walk(_CACHE_DIR):
        n += sum(1 for f in files if f.endswith('.json'))
    return {'calls_cached': n, 'dir': _CACHE_DIR}
