"""
gemini_service.py — Central Gemini helper for the entire TrackFundAI backend.

ALL Gemini calls in the codebase go through this module. The same google.genai
SDK supports two backends — switched via GOOGLE_GENAI_USE_VERTEXAI env var:

  False (default, dev) → AI Studio (api_key auth, has free tier)
  True   (prod)         → Vertex AI (gcloud ADC auth, paid per token)

The rest of the codebase doesn't care which backend is active — it just
calls generate_content() / create_chat() and gets a response object.
"""
import json
import logging
import os
import random
import re
import time
import typing

from django.conf import settings
from google import genai
from google.genai import types as genai_types
from google.genai import errors as genai_errors

import requests.exceptions as _req_exc

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# Call-layer hardening — the deadline lives on the CALL, not on a process kill
# ─────────────────────────────────────────────────────────────────────────
# Root cause of the "request sent, reply never comes, SDK timeout never fires"
# hangs: google-genai 0.5.0's transport is `requests`, and NO timeout was ever
# reaching it (the old code referenced types.HttpOptions — absent in 0.5.0 —
# inside a swallowed try/except, so `requests` got timeout=None → infinite recv
# on a half-open socket). The universal fix is a real socket-level READ timeout:
# `requests` honours a (connect, read) tuple where `read` fires when no bytes
# arrive between reads — exactly what unblocks a stalled connection. Because the
# SDK streams with stream=True on the streamed path, that read timeout doubles
# as an inter-chunk idle timeout. Every call is then bounded, retried with
# jittered backoff, and idempotent — the process-kill in the pipeline is demoted
# to a pure backstop that should now almost never fire.

# HttpOptions changed BOTH its module AND its `timeout` wire-format across SDK versions:
#   • requests-based SDKs (e.g. 0.5.x): `timeout` is SECONDS — a float, or a (connect, read) tuple.
#   • httpx-based SDKs (newer):         `timeout` is a MILLISECOND integer scalar.
# HttpOptions is importable from `_api_client` in BOTH generations, so the import LOCATION does NOT
# tell them apart — that was the ROOT-CAUSE bug: a newer SDK still exposed `_api_client.HttpOptions`,
# so the code assumed the seconds-tuple form and pydantic rejected the tuple ("timeout Input should be
# a valid integer ... input_value=(10.0, 240.0)") → every Gemini call died before it started. The ONLY
# ground truth is the field's OWN declared TYPE — ask the model what it accepts. This is correct on
# every SDK generation, past or future, with no version guessing.
try:
    from google.genai._api_client import HttpOptions as _HttpOptions
except Exception:  # pragma: no cover — SDK layout without _api_client
    from google.genai.types import HttpOptions as _HttpOptions


def _timeout_annotation():
    """The declared type of HttpOptions.timeout on the INSTALLED SDK (pydantic v2 `model_fields`,
    falling back to v1 `__fields__`), or None when the field cannot be introspected."""
    mf = getattr(_HttpOptions, 'model_fields', None)
    if isinstance(mf, dict) and 'timeout' in mf:
        return getattr(mf['timeout'], 'annotation', None)
    v1 = getattr(_HttpOptions, '__fields__', None)
    if isinstance(v1, dict) and 'timeout' in v1:
        return getattr(v1['timeout'], 'outer_type_', None)
    return None


def _annotation_allows(ann, target) -> bool:
    """Does typing annotation `ann` admit a value of `target` (int / float / tuple)? Recurses through
    Optional/Union; a Tuple[...] origin counts only for target `tuple`."""
    origin = typing.get_origin(ann)
    if origin is typing.Union:
        return any(_annotation_allows(a, target) for a in typing.get_args(ann))
    if origin is tuple:
        return target is tuple
    return ann is target


def _timeout_shapes(ann, connect_s, read_s):
    """Ordered candidate `timeout` values for the given field annotation: the shape the SDK DECLARES
    first, the others after as a backstop for an unforeseen variant. A field that takes a tuple or a
    float is SECONDS (requests); an int-only field is MILLISECONDS (httpx). Pure → unit-testable per
    SDK generation, and it never feeds a milliseconds integer to a seconds field (the 240000-seconds
    trap): the ms shape is chosen first only when the field rejects both tuple and float."""
    tup = (float(connect_s), float(read_s))
    sec = float(read_s)
    ms = int(read_s * 1000)
    if _annotation_allows(ann, tuple):
        return [tup, sec, ms]
    if _annotation_allows(ann, float):
        return [sec, tup, ms]
    return [ms, tup, sec]


# Resolved ONCE at import (the installed SDK cannot change mid-process).
_TIMEOUT_ANNOTATION = _timeout_annotation()
_TIMEOUT_HAS_FIELD = _TIMEOUT_ANNOTATION is not None or (
    isinstance(getattr(_HttpOptions, 'model_fields', None), dict)
    and 'timeout' in _HttpOptions.model_fields)

_CONNECT_TIMEOUT_S = float(os.environ.get('GEMINI_CONNECT_TIMEOUT_S', '10'))
_READ_TIMEOUT_S = float(os.environ.get('GEMINI_READ_TIMEOUT_S', '60'))
_MAX_ATTEMPTS = int(os.environ.get('GEMINI_MAX_ATTEMPTS', '3'))
_BACKOFF_BASE_S = float(os.environ.get('GEMINI_BACKOFF_BASE_S', '1.5'))
_BACKOFF_CAP_S = float(os.environ.get('GEMINI_BACKOFF_CAP_S', '20'))

# Legacy knob — still honoured, converted to a read-timeout in seconds.
_DEFAULT_TIMEOUT_MS = int(os.environ.get('GEMINI_CALL_TIMEOUT_MS', '0'))

# Clients are cached by (backend, connect, read) so a caller that wants a longer
# read window for a heavy locator call gets its own client without a fresh auth
# handshake every time. 0.5.0 honours http_options ONLY at the client level
# (GenerateContentConfig has no http_options field), so the timeout must live
# here, not on the per-call config.
_client_cache = {}


def _build_http_options(connect_s, read_s):
    """Build HttpOptions carrying a read-timeout in the shape THIS SDK version DECLARES — decided from
    the field's own type, never from the import location. Tries the declared shape first, then the
    others as a backstop; if every shape is rejected (an unforeseen SDK), returns HttpOptions with no
    explicit timeout so a client can STILL be built (an untimed call is bounded by the pipeline's
    process-kill backstop — far better than crashing the import as the original bug did)."""
    if not _TIMEOUT_HAS_FIELD:
        return _HttpOptions()
    for shape in _timeout_shapes(_TIMEOUT_ANNOTATION, connect_s, read_s):
        try:
            return _HttpOptions(timeout=shape)
        except Exception:  # noqa: BLE001 — SDK/pydantic rejected this shape; try the next
            continue
    return _HttpOptions()


def _is_retryable(exc) -> bool:
    """Transient transport / server faults are retryable; 4xx client errors
    (bad request, auth, quota-of-shape) are not — retrying them just wastes
    time and money."""
    if isinstance(exc, (_req_exc.Timeout, _req_exc.ConnectionError,
                        _req_exc.ChunkedEncodingError)):
        return True
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.ClientError):
        return False
    code = getattr(exc, 'code', None) or getattr(exc, 'status_code', None)
    if isinstance(code, int) and 500 <= code < 600:
        return True
    # Unknown timeout-shaped errors by name (defensive across SDK versions).
    return 'timeout' in exc.__class__.__name__.lower()


def _backoff_sleep(attempt: int):
    delay = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2 ** (attempt - 1)))
    delay += random.uniform(0, delay * 0.5)  # full jitter on the top half
    time.sleep(delay)


# ─────────────────────────────────────────────────────────────────────────
# Client / model helpers — used by EVERY caller in the codebase
# ─────────────────────────────────────────────────────────────────────────

def _is_vertex_mode() -> bool:
    raw = (
        os.environ.get('GOOGLE_GENAI_USE_VERTEXAI')
        or getattr(settings, 'GOOGLE_GENAI_USE_VERTEXAI', 'False')
        or 'False'
    )
    return str(raw).lower() in ('true', '1', 'yes')


def get_client(connect_timeout_s: float = None, read_timeout_s: float = None):
    """Return a google.genai.Client for the active backend, carrying an explicit
    socket-level read timeout so no call can hang indefinitely.

    Clients are cached per (backend, connect, read) timeout profile — a heavy
    call that needs a longer read window gets its own client without repeating
    the auth handshake. Branches on GOOGLE_GENAI_USE_VERTEXAI:
      True  → Vertex AI via Application Default Credentials
      False → AI Studio via GOOGLE_API_KEY
    """
    connect_s = _CONNECT_TIMEOUT_S if connect_timeout_s is None else connect_timeout_s
    read_s = _READ_TIMEOUT_S if read_timeout_s is None else read_timeout_s
    vertex = _is_vertex_mode()
    key = (vertex, round(connect_s, 3), round(read_s, 3))
    cached = _client_cache.get(key)
    if cached is not None:
        return cached

    http_options = _build_http_options(connect_s, read_s)

    if vertex:
        project = (
            os.environ.get('GOOGLE_CLOUD_PROJECT')
            or getattr(settings, 'GOOGLE_CLOUD_PROJECT', '')
        )
        location = (
            os.environ.get('GOOGLE_CLOUD_LOCATION')
            or getattr(settings, 'GOOGLE_CLOUD_LOCATION', '')
            or 'us-central1'
        )
        if not project:
            raise ValueError(
                'GOOGLE_CLOUD_PROJECT not set — Vertex AI backend requires a GCP project. '
                'Either set GOOGLE_CLOUD_PROJECT, or flip GOOGLE_GENAI_USE_VERTEXAI=False '
                'to use AI Studio with an API key instead.'
            )
        client = genai.Client(vertexai=True, project=project, location=location,
                              http_options=http_options)
        logger.info(
            f'Gemini client (Vertex AI) initialised — project={project} location={location} '
            f'connect={connect_s}s read={read_s}s'
        )
    else:
        api_key = (
            os.environ.get('GOOGLE_API_KEY')
            or getattr(settings, 'GOOGLE_API_KEY', None)
        )
        if not api_key:
            raise ValueError(
                'GOOGLE_API_KEY not set — AI Studio backend requires an API key. '
                'Get one at https://aistudio.google.com/app/apikey, or flip '
                'GOOGLE_GENAI_USE_VERTEXAI=True to use Vertex AI with gcloud ADC.'
            )
        client = genai.Client(api_key=api_key, http_options=http_options)
        # Never log the key — even partially. Just confirm the backend is up.
        logger.info(f'Gemini client (AI Studio) initialised — api_key auth '
                    f'connect={connect_s}s read={read_s}s')

    _client_cache[key] = client
    return client


def get_model_name(default: str = 'gemini-2.5-flash') -> str:
    """Resolve the Gemini model name from settings / env."""
    return (
        getattr(settings, 'GEMINI_MODEL', None)
        or os.environ.get('GEMINI_MODEL')
        or default
    )


class _StreamedResponse:
    """Minimal response shim for the streamed path — exposes `.text` (and a
    best-effort `.parsed`) so callers that read `response.text` are unaffected."""
    __slots__ = ('text', 'parsed', 'candidates')

    def __init__(self, text, parsed=None, candidates=None):
        self.text = text
        self.parsed = parsed
        self.candidates = candidates


def _generate_streamed(client, model_name, prompt, config, read_s):
    """Stream the response, enforcing an inter-chunk idle deadline in Python on
    top of the transport read timeout. If no new chunk arrives for `read_s`
    seconds the call is aborted (raised as a Timeout so the retry loop catches
    it) — the targeted cure for 'the reply never comes back'."""
    stream = client.models.generate_content_stream(
        model=model_name, contents=prompt, config=config,
    )
    parts = []
    last = time.monotonic()
    last_chunk = None
    for chunk in stream:
        now = time.monotonic()
        if now - last > read_s:
            raise _req_exc.ReadTimeout(
                f'inter-chunk idle > {read_s}s — aborting stalled stream')
        last = now
        last_chunk = chunk
        piece = getattr(chunk, 'text', None)
        if piece:
            parts.append(piece)
    parsed = getattr(last_chunk, 'parsed', None) if last_chunk is not None else None
    cands = getattr(last_chunk, 'candidates', None) if last_chunk is not None else None
    return _StreamedResponse(''.join(parts), parsed=parsed, candidates=cands)


def generate_content(
    prompt,
    *,
    model: str = None,
    system_instruction: str = None,
    response_mime_type: str = None,
    response_schema=None,
    temperature: float = None,
    timeout_ms: int = None,
    connect_timeout_s: float = None,
    read_timeout_s: float = None,
    max_attempts: int = None,
    stream: bool = False,
    **extra_config,
):
    """One-shot Gemini call with a bounded deadline, idempotent retry, and an
    optional streamed inter-chunk idle timeout. Returns the response object
    (or a `.text`-compatible shim when `stream=True`).

    The deadline is a real socket read timeout carried by the client (see
    get_client). `read_timeout_s` (or legacy `timeout_ms`) sets the max seconds
    with no bytes/chunks before the call aborts and retries. Retries use
    jittered exponential backoff and fire only on transient transport/5xx
    faults — never on 4xx client errors.
    """
    read_s = read_timeout_s
    if read_s is None and timeout_ms:
        read_s = timeout_ms / 1000.0
    if read_s is None:
        read_s = (_DEFAULT_TIMEOUT_MS / 1000.0) if _DEFAULT_TIMEOUT_MS > 0 else _READ_TIMEOUT_S
    connect_s = _CONNECT_TIMEOUT_S if connect_timeout_s is None else connect_timeout_s

    client = get_client(connect_timeout_s=connect_s, read_timeout_s=read_s)
    model_name = model or get_model_name()

    config_kwargs = dict(extra_config)
    # Thinking control (latency/cost lever): callers pass plain kwargs; build the SDK ThinkingConfig here
    # so provider-agnostic callers never import genai types. thinking_level (3.x enum: minimal/low/...)
    # takes precedence; thinking_budget (2.x numeric) otherwise. Never both -> the model 400s.
    _tl = config_kwargs.pop('thinking_level', None)
    _tb = config_kwargs.pop('thinking_budget', None)
    if _tl is not None:
        lvl = _tl if not isinstance(_tl, str) else genai_types.ThinkingLevel[_tl.upper()]
        config_kwargs['thinking_config'] = genai_types.ThinkingConfig(thinking_level=lvl)
    elif _tb is not None:
        config_kwargs['thinking_config'] = genai_types.ThinkingConfig(thinking_budget=_tb)
    if system_instruction is not None:
        config_kwargs['system_instruction'] = system_instruction
    if response_mime_type is not None:
        config_kwargs['response_mime_type'] = response_mime_type
    if response_schema is not None:
        config_kwargs['response_schema'] = response_schema
    if temperature is not None:
        config_kwargs['temperature'] = temperature

    config = genai_types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

    attempts = max_attempts or _MAX_ATTEMPTS
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            if stream:
                return _generate_streamed(client, model_name, prompt, config, read_s)
            return client.models.generate_content(
                model=model_name, contents=prompt, config=config,
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < attempts and _is_retryable(exc):
                logger.warning(
                    '[gemini] %s on attempt %d/%d (read=%ss) — backing off',
                    exc.__class__.__name__, attempt, attempts, read_s,
                )
                _backoff_sleep(attempt)
                continue
            raise
    raise last_exc


def create_chat(
    *,
    model: str = None,
    system_instruction: str = None,
    history: list = None,
    temperature: float = None,
    response_mime_type: str = None,
    **extra_config,
):
    """Create a multi-turn chat session against Vertex AI.

    Returns a chat object exposing `.send_message(text) -> response`.
    Mirrors the new google.genai SDK chat API.

    `history` accepts the legacy list-of-dict shape (`[{'role': 'user',
    'parts': [text]}]`) — we normalise it to the new SDK's Content objects.
    """
    client = get_client()
    model_name = model or get_model_name()

    config_kwargs = dict(extra_config)
    # Thinking control (latency/cost lever): callers pass plain kwargs; build the SDK ThinkingConfig here
    # so provider-agnostic callers never import genai types. thinking_level (3.x enum: minimal/low/...)
    # takes precedence; thinking_budget (2.x numeric) otherwise. Never both -> the model 400s.
    _tl = config_kwargs.pop('thinking_level', None)
    _tb = config_kwargs.pop('thinking_budget', None)
    if _tl is not None:
        lvl = _tl if not isinstance(_tl, str) else genai_types.ThinkingLevel[_tl.upper()]
        config_kwargs['thinking_config'] = genai_types.ThinkingConfig(thinking_level=lvl)
    elif _tb is not None:
        config_kwargs['thinking_config'] = genai_types.ThinkingConfig(thinking_budget=_tb)
    if system_instruction is not None:
        config_kwargs['system_instruction'] = system_instruction
    if response_mime_type is not None:
        config_kwargs['response_mime_type'] = response_mime_type
    if temperature is not None:
        config_kwargs['temperature'] = temperature
    config = genai_types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

    sdk_history = []
    for turn in (history or []):
        role = turn.get('role', 'user') if isinstance(turn, dict) else 'user'
        parts = turn.get('parts') if isinstance(turn, dict) else None
        text = ''
        if isinstance(parts, list) and parts:
            text = parts[0] if isinstance(parts[0], str) else str(parts[0])
        elif isinstance(turn, dict):
            text = turn.get('content') or turn.get('text') or ''
        else:
            text = str(turn)
        sdk_history.append(
            genai_types.Content(role=role, parts=[genai_types.Part(text=text)])
        )

    return client.chats.create(
        model=model_name,
        config=config,
        history=sdk_history or None,
    )


# ─────────────────────────────────────────────────────────────────────────
# Backwards-compatible wrappers — keep the existing call sites working
# ─────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are an expert CFO-level financial analyst AI for Analisa Resources (M) Sdn. Bhd.,
a Malaysian life-science equipment distribution company.

You have full access to the company's Monthly Business Review (MBR) financial data:
- Summary P&L (revenue, COGS, gross profit, GP%, OPEX, EBITDA, normalised EBITDA)
- Monthly P&L trend (Jan 2024 – May 2025) — values in full MYR
- Cash flow statements — values in MYR '000 (thousands)
- Working capital metrics: DSO, DIO, DPO, NWC, CCC
- Sales breakdown by business segment: HID, LabFriend, Project/NGS, Sci.Lab, Sci.Lab-Qiagen, Service

All monetary figures are in MYR (Malaysian Ringgit).

PORTFOLIO DATA (current snapshot):
{portfolio_json}

═══════════════════════════════════════════════════════════════════
INLINE CHART DIRECTIVES — IMPORTANT
═══════════════════════════════════════════════════════════════════
Whenever a pictorial representation would materially help the user
understand your answer, embed ONE OR MORE chart directives inside
your reply using the fenced code-block syntax below. The frontend
will parse these blocks and render interactive Chart.js charts
inline in the chat bubble.

Syntax (copy the exact fence label `chart`):

```chart
{
  "type": "bar" | "line" | "doughnut" | "pie",
  "title": "Short chart title",
  "labels": ["Label1", "Label2", ...],
  "datasets": [
    { "label": "Series A", "data": [123, 456, ...] },
    { "label": "Series B", "data": [111, 222, ...] }
  ],
  "yFormat": "MYR" | "MYR_K" | "percent" | "days" | "number",
  "notes": "One-sentence caption shown below the chart (optional)"
}
```

Rules for chart directives:
- Emit a chart ONLY when it clarifies the answer (YoY comparisons,
  trends over months, segment splits, ratios over time, etc.).
  Do NOT emit a chart for single-number answers.
- Use `"type": "bar"` for comparisons and values with outliers.
- Use `"type": "line"` for smooth percentage/ratio trends like GP%, DSO.
- Use `"type": "doughnut"` for part-of-whole splits (segment mix).
- Numbers inside `data` arrays must be raw numbers (no commas, no
  currency symbols, no k/M suffixes) — the frontend formats them.
- `yFormat: "MYR"` for full ringgit values (P&L).
- `yFormat: "MYR_K"` for cash-flow values (which are stored in MYR '000).
- Keep `labels` ≤ 15 entries — truncate to the most recent / most
  relevant if you have more.
- You may emit up to 2 chart directives per answer. More than that
  clutters the chat.
- Always write the text explanation BEFORE the chart block, so the
  reader has context when the chart appears.

Example answer shape:

  **YoY Revenue Growth:** MYR 3.69M (2025 YTD) vs MYR 2.69M (2024 YTD)
  — a **+37.3% increase**.

  ```chart
  {"type":"bar","title":"YTD Revenue: 2024 vs 2025","labels":["YTD 2024","YTD 2025"],"datasets":[{"label":"Revenue (MYR)","data":[2691093,3694112]}],"yFormat":"MYR"}
  ```

═══════════════════════════════════════════════════════════════════

Guidelines:
- Be concise and analytical. Lead with numbers.
- Format currency as MYR X,XXX,XXX or MYR X.Xk/M as appropriate.
- When referencing a dashboard chart section, mention it (e.g. "See the Revenue Trend chart above").
- If you identify a specific metric or trend that warrants attention, state it clearly.
- Respond in structured markdown (bold key figures, use bullet lists for comparisons).
- If asked about something not in the data (e.g. ARR/MRR, intercompany balances),
  say so clearly rather than fabricating — explain WHY the data doesn't support that metric
  (e.g. "This is a capital-equipment distributor, not a subscription business, so ARR/MRR
   aren't meaningful KPIs — the closest analogue is recurring service revenue from the
   Service segment").
"""


def chat(message: str, history: list, portfolio_data: dict) -> dict:
    """Multi-turn chat against Gemini-on-Vertex with portfolio context.

    history: list of {"role": "user"|"model", "parts": [str]} dicts.
    Returns {"reply": str, "highlight_metrics": [str]}.
    """
    system_with_data = SYSTEM_PROMPT.replace(
        "{portfolio_json}",
        json.dumps(portfolio_data, indent=2, default=str),
    )

    chat_session = create_chat(
        system_instruction=system_with_data,
        history=history,
    )

    try:
        response = chat_session.send_message(message)
        reply_text = response.text
    except Exception as e:
        logger.error("Vertex AI chat error: %s", e)
        return {"reply": f"AI service error: {str(e)}", "highlight_metrics": []}

    return {
        "reply": reply_text,
        "highlight_metrics": _extract_highlights(reply_text),
    }


def _extract_highlights(text: str) -> list[str]:
    """Extract financial metric and segment names from the reply for UI highlighting."""
    KNOWN_TERMS = [
        "Revenue", "EBITDA", "Gross Profit", "GP%", "OPEX", "COGS",
        "DSO", "DIO", "DPO", "NWC", "CCC", "Cash Flow",
        "HID", "LabFriend", "Project/NGS", "Sci.Lab", "Qiagen", "Service",
        "normalized EBITDA", "Normalised EBITDA", "Net Cash",
    ]
    found = []
    for term in KNOWN_TERMS:
        if re.search(re.escape(term), text, re.IGNORECASE):
            found.append(term)
    return found
