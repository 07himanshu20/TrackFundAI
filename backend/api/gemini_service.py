"""
gemini_service.py
Wraps Google Gemini API. API key is read from Django settings (loaded from .env).
The key is NEVER returned to the frontend.
"""
import json
import logging
import re

import google.generativeai as genai
from django.conf import settings

logger = logging.getLogger(__name__)

_configured = False


def _ensure_configured():
    global _configured
    if not _configured:
        api_key = getattr(settings, "GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set in .env")
        genai.configure(api_key=api_key)
        _configured = True


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
    """
    Send a message to Gemini with portfolio context.
    history: list of {"role": "user"|"model", "parts": [str]} dicts.
    Returns {"reply": str, "highlight_metrics": [str]}.
    """
    _ensure_configured()
    model_name = getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash")

    system_with_data = SYSTEM_PROMPT.replace(
        "{portfolio_json}",
        json.dumps(portfolio_data, indent=2, default=str)
    )

    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=system_with_data,
    )

    # Build conversation history
    gemini_history = []
    for turn in (history or []):
        role = turn.get("role", "user")
        text = turn.get("content", "")
        gemini_history.append({"role": role, "parts": [text]})

    chat_session = model.start_chat(history=gemini_history)

    try:
        response = chat_session.send_message(message)
        reply_text = response.text
    except Exception as e:
        logger.error("Gemini API error: %s", e)
        return {"reply": f"AI service error: {str(e)}", "highlight_metrics": []}

    # Extract mentioned metrics/segments for frontend highlighting
    highlight_metrics = _extract_highlights(reply_text)

    return {
        "reply": reply_text,
        "highlight_metrics": highlight_metrics,
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
