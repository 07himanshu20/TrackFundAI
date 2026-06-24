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
import re

from django.conf import settings
from google import genai
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

# Module-level singleton client — instantiating Client per call is slow
# and creates redundant auth handshakes. One process-wide client is fine.
_client = None


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


def get_client():
    """Return the singleton google.genai.Client for the active backend.

    Branches on GOOGLE_GENAI_USE_VERTEXAI:
      True  → Vertex AI via Application Default Credentials
      False → AI Studio via GOOGLE_API_KEY
    """
    global _client
    if _client is not None:
        return _client

    if _is_vertex_mode():
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
        _client = genai.Client(vertexai=True, project=project, location=location)
        logger.info(
            f'Gemini client (Vertex AI) initialised — project={project} location={location}'
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
        _client = genai.Client(api_key=api_key)
        # Never log the key — even partially. Just confirm the backend is up.
        logger.info('Gemini client (AI Studio) initialised — api_key auth')

    return _client


def get_model_name(default: str = 'gemini-2.5-flash') -> str:
    """Resolve the Gemini model name from settings / env."""
    return (
        getattr(settings, 'GEMINI_MODEL', None)
        or os.environ.get('GEMINI_MODEL')
        or default
    )


_DEFAULT_TIMEOUT_MS = int(os.environ.get('GEMINI_CALL_TIMEOUT_MS', '300000'))


def generate_content(
    prompt,
    *,
    model: str = None,
    system_instruction: str = None,
    response_mime_type: str = None,
    response_schema=None,
    temperature: float = None,
    timeout_ms: int = None,
    **extra_config,
):
    """One-shot Gemini call. Returns the raw response object.

    `timeout_ms` caps the HTTP call so a hung Vertex backend can't stall the
    caller indefinitely. Defaults to GEMINI_CALL_TIMEOUT_MS (300_000ms = 5min).
    `response_schema` forces Gemini to emit JSON matching the given schema
    when combined with response_mime_type='application/json'.
    """
    client = get_client()
    model_name = model or get_model_name()

    config_kwargs = dict(extra_config)
    if system_instruction is not None:
        config_kwargs['system_instruction'] = system_instruction
    if response_mime_type is not None:
        config_kwargs['response_mime_type'] = response_mime_type
    if response_schema is not None:
        config_kwargs['response_schema'] = response_schema
    if temperature is not None:
        config_kwargs['temperature'] = temperature

    effective_timeout_ms = timeout_ms if timeout_ms is not None else _DEFAULT_TIMEOUT_MS
    if effective_timeout_ms and effective_timeout_ms > 0:
        try:
            config_kwargs['http_options'] = genai_types.HttpOptions(
                timeout=effective_timeout_ms,
            )
        except Exception:
            # Older SDK versions may not expose HttpOptions on this path —
            # fall through; the global client timeout (if any) still applies.
            pass

    config = genai_types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

    return client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=config,
    )


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
