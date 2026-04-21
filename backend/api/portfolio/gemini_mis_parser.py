"""
gemini_mis_parser.py
====================
Sends the raw workbook extraction to Gemini and asks it to return a
fully-structured Financials JSON that matches our canonical schema.

Architecture
------------
  1. mis_extractor.extract_summary_for_gemini(filepath) → raw_data dict
  2. Build a detailed system prompt explaining the target schema exactly
  3. Call Gemini with the raw_data as user message
  4. Gemini returns a JSON block with all fields populated
  5. We validate + normalise the response into a clean Financials dict
  6. On any parse error, retry once with an error-correction prompt

The key insight: we do NOT ask Gemini to guess — we give it the exact
target schema with field descriptions and ask it to fill every field
it can find. Fields it cannot find are left null, never fabricated.

Two-pass strategy for accuracy:
  Pass 1 (Structure Discovery): Ask Gemini to identify which sheets
    contain P&L, Cash Flow, Budget, Segments and the scale/currency.
  Pass 2 (Data Extraction): Ask Gemini to extract the actual numbers
    from those specific sheets into the canonical schema.

This two-pass approach prevents hallucination — Gemini first tells us
WHERE the data is before it tries to extract values.
"""

from __future__ import annotations

import json
import logging
import re
import os
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema description sent to Gemini (ground truth for output format)
# ---------------------------------------------------------------------------

SCHEMA_DESCRIPTION = """
You must output a JSON object that exactly matches this TypeScript type:

interface Financials {
  // Top-level snapshot for the most recent / current reporting period
  summary: {
    period: string,               // e.g. "May 2025", "YTD Jun 2025", "FY2025"
    revenue: number | null,       // Total revenue (current period, native currency)
    cogs: number | null,          // Cost of goods sold (positive number)
    gross_profit: number | null,  // Revenue - COGS
    gp_pct: number | null,        // Gross profit % of revenue (0-100 scale)
    opex: number | null,          // Total operating expenses (positive number)
    ebitda: number | null,        // EBITDA (can be negative)
    ebitda_pct: number | null,    // EBITDA % of revenue (-100 to 100 scale)
    ytd_revenue: number | null,   // Year-to-date revenue
    ytd_gross_profit: number | null,
    ytd_ebitda: number | null,
    budget_revenue: number | null,        // Budgeted/AOP revenue for current period
    budget_ebitda: number | null,         // Budgeted/AOP EBITDA for current period
    ytd_budget_revenue: number | null,    // YTD budget/AOP revenue
    ytd_budget_ebitda: number | null      // YTD budget/AOP EBITDA
  },

  // Monthly P&L time series — one entry per month, oldest first
  // Include ALL months you can find (going back as far as available, ideally 2016+)
  monthly_pl: Array<{
    period: string,         // "YYYY-MM" format strictly, e.g. "2025-05"
    revenue: number | null,
    cogs: number | null,
    gross_profit: number | null,
    gp_pct: number | null,
    opex: number | null,
    ebitda: number | null,
    ebitda_pct: number | null
  }>,

  // Cash flow — one entry per period available
  cash_flow: Array<{
    period: string,           // "YYYY-MM" or "YYYY-QN" if quarterly
    opening_cash: number | null,
    operating_cf: number | null,
    investing_cf: number | null,
    financing_cf: number | null,
    net_cash_flow: number | null,
    closing_cash: number | null
  }>,

  // Working capital — one entry per period
  working_capital: Array<{
    period: string,
    dso: number | null,   // Days Sales Outstanding
    dio: number | null,   // Days Inventory Outstanding
    dpo: number | null,   // Days Payable Outstanding
    nwc: number | null,   // Net Working Capital (monetary)
    ccc: number | null    // Cash Conversion Cycle = DSO + DIO - DPO
  }>,

  // Budget vs Actual comparison — one entry per line item
  budget_vs_actual: Array<{
    period: string,         // The period this covers e.g. "YTD May 2025"
    line_item: string,      // "Revenue", "COGS", "Gross Profit", "OPEX", "EBITDA"
    budget: number | null,
    actual: number | null,
    variance: number | null,      // actual - budget
    variance_pct: number | null   // (variance / budget) * 100
  }>,

  // Sales breakdown by business segment/division/product line
  sales_by_segment: Array<{
    label: string,
    revenue: number | null,
    gross_margin: number | null,
    gm_pct: number | null
  }>,

  // Sales breakdown by geography/country
  sales_by_geo: Array<{
    label: string,
    revenue: number | null,
    gross_margin: number | null,
    gm_pct: number | null
  }>,

  // Cost structure as percentages of revenue (current period)
  cost_structure: {
    cogs_pct: number | null,
    gp_pct: number | null,
    opex_pct: number | null,
    ebitda_pct: number | null
  },

  // Self-assessed confidence of this extraction, per section (0.0 - 1.0).
  // Return LOW (<0.7) if you had to guess, the sheet structure was ambiguous,
  // data was partially missing, or columns were unclear. Return HIGH (>=0.9)
  // only when the data was unambiguous and complete.
  confidence: {
    summary: number,
    monthly_pl: number,
    cash_flow: number,
    working_capital: number,
    budget_vs_actual: number,
    sales_by_segment: number,
    sales_by_geo: number,
    overall: number,        // weighted average of the above
    notes: string           // ≤200 chars, explain any low-confidence reasons
  }
}

CRITICAL RULES:
1. ALL monetary values must be in the NATIVE CURRENCY of the file — do NOT convert to USD.
   The currency will be recorded separately.
2. ALL percentage fields must be on the 0-100 scale (not 0-1).
   If the Excel stores GP% as 0.42 (fraction), convert to 42.0.
3. DO NOT scale or multiply monetary values yourself.
   Return the RAW CELL VALUES exactly as they appear in the data.
   Scaling (thousands/millions/lakhs) is handled by the post-processing pipeline.
   e.g. if cell value is 1234.5 and sheet says "In MYR '000", return 1234.5 (not 1234500).
4. monthly_pl periods must be "YYYY-MM" format strictly.
   Convert "May 2025" → "2025-05", "Jun-25" → "2025-06", datetime(2025,5,1) → "2025-05"
5. If a value is genuinely not present in the file, output null. NEVER fabricate numbers.
6. For cash flow: operating_cf is the cash from operations (net), NOT just EBITDA.
7. COGS and OPEX should always be positive numbers even if stored as negative in Excel.
8. Go back as far as the data allows for monthly_pl (ideally 2016 if sheets exist).
9. ACTUALS vs BUDGET distinction — VERY IMPORTANT:
   - For summary.revenue, summary.ebitda etc: use ACTUAL values (what really happened), NOT budget/forecast/AOP values.
   - For budget_vs_actual: populate BOTH actual AND budget columns from the comparison sheet.
   - For monthly_pl: use ACTUAL reported values only, NOT forecast or budget months.
     If a sheet contains both historical actuals and future forecast months, include ONLY
     months up to and including the latest_period identified in Pass 1. Skip future months.
   - Budget/AOP/Forecast columns should only appear in budget_vs_actual rows, NOT in summary.
10. SEGMENT revenue: extract from the segment sales sheet. If segments show zero revenue
    but the overall company has revenue, look harder — the segment data is likely in a
    separate section of the same sheet or a different sheet from pl_sheets.
    The sum of all segment revenues should approximately equal total company revenue.
11. For budget_vs_actual, actual and budget MUST be DIFFERENT numbers.
    If they appear identical, you are reading the same column twice — recheck the column mapping.
"""


# ---------------------------------------------------------------------------
# Pass 1 prompt: structure discovery
# ---------------------------------------------------------------------------

PASS1_PROMPT = """You are a financial data extraction expert. You have received the raw cell data from a company's MIS (Management Information System) Excel file.

Your task is to ANALYSE the structure and tell me:
1. Which sheet(s) contain the P&L (Income Statement)?
2. Which sheet(s) contain Cash Flow data?
3. Which sheet(s) contain Budget vs Actual comparison?
4. Which sheet(s) contain sales breakdown by segment/product?
5. Which sheet(s) contain geographic sales breakdown?
6. Which sheet(s) contain working capital / DSO / DIO / DPO data?
7. What is the native currency of the financial data?
8. What is the scale/unit of monetary values? (e.g. "full units", "thousands", "millions", "lakhs")
9. What is the most recent reporting period covered?
10. What is the earliest year of monthly data available?
11. For the P&L sheet: which row index contains Revenue? Which row index contains EBITDA?
12. For the P&L sheet: which column index is "Actual current period"? Which is "Budget"? Which is "YTD Actual"? Which is "YTD Budget"?

Respond ONLY with a JSON object in this exact shape (no markdown, no explanation):
{
  "pl_sheets": ["sheet name 1", ...],
  "cf_sheets": ["sheet name", ...],
  "bva_sheets": ["sheet name", ...],
  "segment_sheets": ["sheet name", ...],
  "geo_sheets": ["sheet name", ...],
  "wc_sheets": ["sheet name", ...],
  "currency": "MYR" | "USD" | "EUR" | "SGD" | "INR" | "IDR" | "THB" | "PHP" | "VND" | "GBP" | "AED" | "other",
  "scale": "full" | "thousands" | "millions" | "lakhs" | "crores",
  "scale_multiplier": 1,
  "latest_period": "May 2025",
  "earliest_year": 2024,
  "pl_revenue_row": null,
  "pl_ebitda_row": null,
  "pl_actual_col": null,
  "pl_budget_col": null,
  "pl_ytd_actual_col": null,
  "pl_ytd_budget_col": null
}

RAW WORKBOOK DATA:
"""

# ---------------------------------------------------------------------------
# Pass 2 prompt: full data extraction
# ---------------------------------------------------------------------------

PASS2_PROMPT = """You are a financial data extraction expert. You have already identified the structure of this MIS Excel file. Now extract ALL financial data into the exact JSON schema below.

STRUCTURE ANALYSIS FROM PASS 1:
{structure_json}

TARGET SCHEMA:
{schema}

RAW WORKBOOK DATA:
{raw_data}

INSTRUCTIONS:
- Extract every number you can find. Be thorough — check all identified sheets.
- For monthly_pl, extract EVERY month available (going back as far as data exists, ideally to 2016).
  ONLY include months up to and including latest_period from Pass 1. Do NOT include future/forecast months.
- For percentages stored as fractions (0.42), convert to percentage (42.0).
- Return RAW CELL VALUES exactly as seen — do NOT multiply by scale factors. Scaling is post-processed.
- NEVER guess or interpolate. Only output values explicitly present in the data.
- Output ONLY the JSON object with no markdown fences, no explanation text, nothing else.
- The JSON must start with {{ and end with }}.
- For summary fields: use ACTUAL values, not budget/AOP. Budget goes only in budget_vs_actual.
- CRITICAL MTD vs YTD separation: `revenue`, `cogs`, `gross_profit`, `opex`, `ebitda`
  are MTD (current period only). `ytd_revenue`, `ytd_gross_profit`, `ytd_ebitda` are
  cumulative year-to-date. NEVER put the YTD value in the MTD field just because it's
  the biggest number. If the workbook has separate MTD and YTD sheets/columns (e.g.
  "Country(mtd)" and "Country (YTD)"), extract BOTH. If the workbook only has YTD,
  leave MTD fields null rather than copying YTD into them.
- Similarly for budget_vs_actual: emit BOTH a MTD row (period="<Month> <Year>") AND
  a YTD row (period="YTD <Month> <Year>") for each line item when both periods exist
  in the source. Do not collapse to YTD-only.
- For budget_vs_actual: actual and budget MUST be different numbers. If they look the same,
  you are reading the wrong column — the actual column and budget column are always different.
- For sales_by_segment: segment revenues should sum to approximately YTD revenue
  (use YTD figures per segment when both MTD and YTD exist in the source).
  Extract from the segment breakdown sheet identified in Pass 1.
- Populate the confidence object honestly. If you could not find monthly P&L
  month-by-month columns, confidence.monthly_pl should be 0.3 or lower.
  If you had to derive values, say so in confidence.notes.
"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_mis_with_gemini(
    raw_workbook_data: dict,
    company_hints: Optional[dict] = None,
) -> dict:
    """
    Run two-pass Gemini extraction on raw workbook data.

    Args:
        raw_workbook_data: Output of mis_extractor.extract_summary_for_gemini()
        company_hints: Optional dict with known facts to help Gemini:
            {
                "currency": "MYR",           # override if known
                "scale": "thousands",         # override if known
                "company_name": "Analisa Resources",
                "reporting_period": "May 2025"
            }

    Returns:
        Financials dict matching the canonical schema.
        All monetary values in native currency, full units.
    """
    import google.generativeai as genai
    from django.conf import settings

    # Ensure Gemini is configured
    api_key = getattr(settings, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set in .env / Django settings")
    genai.configure(api_key=api_key)

    model_name = getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash")
    model = genai.GenerativeModel(
        model_name=model_name,
        generation_config={
            "temperature": 0,       # zero temperature = deterministic, no hallucination
            "response_mime_type": "application/json",  # force JSON output
        },
    )

    raw_json_str = json.dumps(raw_workbook_data, ensure_ascii=False, separators=(",", ":"))

    # Inject company hints into the prompt if provided
    hints_str = ""
    if company_hints:
        hints_str = f"\n\nKNOWN FACTS ABOUT THIS COMPANY (use these to guide extraction):\n{json.dumps(company_hints, indent=2)}"

    # ── Pass 1: Structure discovery ─────────────────────────────────────
    logger.info("MIS Parser Pass 1: structure discovery...")
    pass1_message = PASS1_PROMPT + hints_str + "\n\n" + raw_json_str

    try:
        pass1_response = model.generate_content(pass1_message)
        structure = _parse_json_response(pass1_response.text, "Pass 1")
        logger.info("Pass 1 structure: %s", json.dumps(structure, indent=2))
    except Exception as e:
        logger.error("Pass 1 failed: %s", e)
        # Fall back to direct extraction without structure hints
        structure = {}

    # ── Filter workbook to only the relevant sheets before Pass 2 ───────
    # This dramatically reduces token count for Pass 2 and avoids rate limits
    relevant_names = set()
    for key in ("pl_sheets", "cf_sheets", "bva_sheets", "segment_sheets",
                "geo_sheets", "wc_sheets"):
        for name in structure.get(key, []):
            relevant_names.add(name)

    if relevant_names:
        filtered_sheets = [
            s for s in raw_workbook_data.get("sheets", [])
            if s["name"] in relevant_names
        ]
        # Always keep top-5 by finance_score even if not in structure
        top5 = sorted(
            raw_workbook_data.get("sheets", []),
            key=lambda x: x.get("finance_score", 0),
            reverse=True
        )[:5]
        for s in top5:
            if s not in filtered_sheets:
                filtered_sheets.append(s)
    else:
        filtered_sheets = raw_workbook_data.get("sheets", [])

    filtered_data = dict(raw_workbook_data)
    filtered_data["sheets"] = filtered_sheets
    filtered_json_str = json.dumps(filtered_data, ensure_ascii=False, separators=(",", ":"))
    logger.info(
        "Pass 2 input: %d sheets, %d chars (from %d sheets, %d chars)",
        len(filtered_sheets), len(filtered_json_str),
        len(raw_workbook_data.get("sheets", [])), len(raw_json_str),
    )

    # ── Retry wrapper for rate limit (429) ───────────────────────────────
    import time

    def _call_with_retry(prompt: str, max_retries: int = 3) -> str:
        for attempt in range(max_retries):
            try:
                response = model.generate_content(prompt)
                return response.text
            except Exception as exc:
                err_str = str(exc)
                is_rate_limit = "429" in err_str or "quota" in err_str.lower() or "rate" in err_str.lower()
                is_daily_limit = "PerDay" in err_str or "per_day" in err_str.lower()

                if is_daily_limit:
                    raise RuntimeError(
                        "Gemini daily quota exhausted. The free-tier allows 20 requests/day. "
                        "Please wait until tomorrow or upgrade to a paid API key."
                    ) from exc

                is_transient = (
                    "504" in err_str or "Deadline" in err_str
                    or "503" in err_str or "Unavailable" in err_str
                    or "500" in err_str
                )

                if is_rate_limit:
                    wait = 60
                    import re as _re
                    m = _re.search(r"retry in (\d+\.?\d*)\s*s", err_str)
                    if m:
                        wait = int(float(m.group(1))) + 5
                    logger.warning(
                        "Rate limit on attempt %d/%d. Waiting %ds before retry...",
                        attempt + 1, max_retries, wait
                    )
                    time.sleep(wait)
                elif is_transient and attempt < max_retries - 1:
                    wait = 30 * (attempt + 1)
                    logger.warning(
                        "Transient Gemini error on attempt %d/%d (%s). Waiting %ds...",
                        attempt + 1, max_retries, err_str[:80], wait,
                    )
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError("Gemini rate limit persisted after all retries")

    # ── Pass 2: Full data extraction ────────────────────────────────────
    logger.info("MIS Parser Pass 2: data extraction...")
    pass2_message = PASS2_PROMPT.format(
        structure_json=json.dumps(structure, indent=2),
        schema=SCHEMA_DESCRIPTION,
        raw_data=filtered_json_str + hints_str,
    )

    try:
        pass2_text = _call_with_retry(pass2_message)
        financials_raw = _parse_json_response(pass2_text, "Pass 2")
    except Exception as e:
        logger.error("Pass 2 failed: %s", e)
        raise RuntimeError(f"Gemini extraction failed: {e}") from e

    # ── Validate + normalise output ──────────────────────────────────────
    financials = _normalise_financials(financials_raw, structure, company_hints or {})

    logger.info(
        "MIS Parser complete. Revenue=%s, EBITDA=%s, monthly_pl_rows=%d, bva_rows=%d",
        financials.get("summary", {}).get("revenue"),
        financials.get("summary", {}).get("ebitda"),
        len(financials.get("monthly_pl", [])),
        len(financials.get("budget_vs_actual", [])),
    )

    return financials


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

def _parse_json_response(text: str, pass_name: str) -> dict:
    """
    Extract and parse the JSON from Gemini's response text.
    Handles:
      - Markdown fences
      - Truncated responses (JSON cut mid-stream due to output token limits)
      - Leading/trailing garbage text
    """
    text = text.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()

    # First: try clean parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Second: if truncated, try to repair by closing open structures
    repaired = _repair_truncated_json(text)
    if repaired:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

    # Third: try to extract the largest { } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        candidate = match.group()
        repaired2 = _repair_truncated_json(candidate)
        for attempt in [candidate, repaired2]:
            if attempt:
                try:
                    return json.loads(attempt)
                except json.JSONDecodeError:
                    pass

    logger.error("%s JSON parse error\nRaw text (first 500): %s", pass_name, text[:500])
    raise ValueError(f"{pass_name} JSON parse failed — response was truncated or malformed") from None


def _repair_truncated_json(text: str) -> str | None:
    """
    Attempt to repair a JSON string that was truncated mid-stream.
    Strategy: count open brackets/braces and close any that are unclosed.
    Works for the case where Gemini hits output token limit mid-array.
    """
    if not text:
        return None

    # Walk backwards to find a safe truncation point (last clean value terminator)
    clean_end = len(text)
    for i in range(len(text) - 1, max(len(text) - 300, 0), -1):
        c = text[i]
        if c in ('}', ']'):
            clean_end = i + 1
            break
        if c in ('"', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9'):
            # Verify not inside an incomplete string
            clean_end = i + 1
            break

    truncated = text[:clean_end]

    # Count open brackets/braces (simple non-string-aware scan for speed)
    depth_curly = 0
    depth_square = 0
    in_string = False
    escape_next = False
    for char in truncated:
        if escape_next:
            escape_next = False
            continue
        if char == '\\' and in_string:
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
        elif not in_string:
            if char == '{':
                depth_curly += 1
            elif char == '}':
                depth_curly -= 1
            elif char == '[':
                depth_square += 1
            elif char == ']':
                depth_square -= 1

    # Remove trailing comma before adding closers (invalid JSON)
    repaired = truncated.rstrip()
    if repaired and repaired[-1] == ',':
        repaired = repaired[:-1]

    # Close in correct order: close innermost (square) first
    closing = ']' * max(0, depth_square) + '}' * max(0, depth_curly)
    return repaired + closing if closing else repaired


# ---------------------------------------------------------------------------
# Normalisation: enforce schema types + scale + percentage conversions
# ---------------------------------------------------------------------------

def _normalise_financials(raw: dict, structure: dict, hints: dict) -> dict:
    """
    Enforce the canonical Financials schema on Gemini's raw output.
    - Coerce all monetary fields to float or None
    - Ensure percentages are on 0-100 scale
    - Ensure monthly_pl periods are YYYY-MM
    - Apply scale_multiplier if Gemini reported scale != 'full'
    """
    scale_mult = float(structure.get("scale_multiplier", 1) or 1)
    # If hints override scale, recalculate
    hint_scale = hints.get("scale")
    if hint_scale == "thousands":
        scale_mult = 1_000
    elif hint_scale == "millions":
        scale_mult = 1_000_000
    elif hint_scale == "lakhs":
        scale_mult = 100_000
    elif hint_scale == "crores":
        scale_mult = 10_000_000
    elif hint_scale == "full":
        scale_mult = 1

    def _money(v):
        if v is None:
            return None
        try:
            f = float(v)
            return round(f * scale_mult, 2) if scale_mult != 1 else round(f, 2)
        except (TypeError, ValueError):
            return None

    def _pct(v):
        """Ensure percentage is on 0-100 scale."""
        if v is None:
            return None
        try:
            f = float(v)
            # If Gemini returned a fraction (0-1 range), convert
            if -1.0 <= f <= 1.0 and f != 0:
                f = round(f * 100, 4)
            return round(f, 2)
        except (TypeError, ValueError):
            return None

    def _period_to_iso(p) -> Optional[str]:
        """Convert any period representation to YYYY-MM."""
        if p is None:
            return None
        if isinstance(p, dict) and "__dt" in p:
            # datetime object from extractor
            return p["__dt"][:7]  # first 7 chars = YYYY-MM
        p = str(p).strip()
        # Already YYYY-MM
        if re.match(r"^\d{4}-\d{2}$", p):
            return p
        # YYYY-MM-DD
        if re.match(r"^\d{4}-\d{2}-\d{2}", p):
            return p[:7]
        # "May 2025", "May-2025", "May-25"
        MONTH_MAP = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04",
            "may": "05", "jun": "06", "jul": "07", "aug": "08",
            "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }
        m = re.match(r"([a-zA-Z]{3})[^\d]*(\d{2,4})", p)
        if m:
            mon = MONTH_MAP.get(m.group(1).lower())
            yr = m.group(2)
            if len(yr) == 2:
                yr = "20" + yr if int(yr) < 50 else "19" + yr
            if mon:
                return f"{yr}-{mon}"
        # "Q1 2025" → use first month of quarter
        m2 = re.match(r"Q([1-4])\s*(\d{4})", p, re.IGNORECASE)
        if m2:
            q_start = {"1": "01", "2": "04", "3": "07", "4": "10"}
            return f"{m2.group(2)}-{q_start[m2.group(1)]}"
        return p  # return as-is if we can't parse

    # ── Summary ──────────────────────────────────────────────────────────
    raw_summary = raw.get("summary") or {}
    summary = {
        "period": raw_summary.get("period"),
        "revenue":           _money(raw_summary.get("revenue")),
        "cogs":              _money(raw_summary.get("cogs")),
        "gross_profit":      _money(raw_summary.get("gross_profit")),
        "gp_pct":            _pct(raw_summary.get("gp_pct")),
        "opex":              _money(raw_summary.get("opex")),
        "ebitda":            _money(raw_summary.get("ebitda")),
        "ebitda_pct":        _pct(raw_summary.get("ebitda_pct")),
        "ytd_revenue":       _money(raw_summary.get("ytd_revenue")),
        "ytd_gross_profit":  _money(raw_summary.get("ytd_gross_profit")),
        "ytd_ebitda":        _money(raw_summary.get("ytd_ebitda")),
        "budget_revenue":    _money(raw_summary.get("budget_revenue")),
        "budget_ebitda":     _money(raw_summary.get("budget_ebitda")),
        "ytd_budget_revenue": _money(raw_summary.get("ytd_budget_revenue")),
        "ytd_budget_ebitda":  _money(raw_summary.get("ytd_budget_ebitda")),
    }

    # Cost lines are always positive magnitudes — flip sign if Gemini kept Excel's negative.
    for cost_key in ("cogs", "opex"):
        v = summary.get(cost_key)
        if v is not None and v < 0:
            summary[cost_key] = abs(v)

    # Derive gross_profit / gp_pct if missing
    if summary["gross_profit"] is None and summary["revenue"] and summary["cogs"]:
        summary["gross_profit"] = round(summary["revenue"] - summary["cogs"], 2)
    if summary["gp_pct"] is None and summary["revenue"] and summary["gross_profit"]:
        summary["gp_pct"] = round(summary["gross_profit"] / summary["revenue"] * 100, 2)
    if summary["ebitda_pct"] is None and summary["revenue"] and summary["ebitda"] is not None:
        summary["ebitda_pct"] = round(summary["ebitda"] / summary["revenue"] * 100, 2)

    # ── Monthly P&L ──────────────────────────────────────────────────────
    monthly_pl = []
    for pt in (raw.get("monthly_pl") or []):
        iso = _period_to_iso(pt.get("period"))
        if not iso:
            continue
        rev = _money(pt.get("revenue"))
        cogs = _money(pt.get("cogs"))
        gp = _money(pt.get("gross_profit"))
        gp_pct = _pct(pt.get("gp_pct"))
        opex = _money(pt.get("opex"))
        ebitda = _money(pt.get("ebitda"))
        ebitda_pct = _pct(pt.get("ebitda_pct"))

        # Derive missing fields
        if gp is None and rev is not None and cogs is not None:
            gp = round(rev - cogs, 2)
        if gp_pct is None and rev and gp is not None:
            gp_pct = round(gp / rev * 100, 2)
        if ebitda_pct is None and rev and ebitda is not None:
            ebitda_pct = round(ebitda / rev * 100, 2)

        monthly_pl.append({
            "period": iso,
            "revenue": rev,
            "cogs": cogs,
            "gross_profit": gp,
            "gp_pct": gp_pct,
            "opex": opex,
            "ebitda": ebitda,
            "ebitda_pct": ebitda_pct,
        })

    # Sort by period and deduplicate (keep last seen for each period)
    seen_periods = {}
    for pt in monthly_pl:
        seen_periods[pt["period"]] = pt
    monthly_pl = sorted(seen_periods.values(), key=lambda x: x["period"])

    # ── Cash Flow ────────────────────────────────────────────────────────
    cash_flow = []
    for pt in (raw.get("cash_flow") or []):
        iso = _period_to_iso(pt.get("period"))
        cash_flow.append({
            "period": iso or str(pt.get("period", "")),
            "opening_cash":  _money(pt.get("opening_cash")),
            "operating_cf":  _money(pt.get("operating_cf")),
            "investing_cf":  _money(pt.get("investing_cf")),
            "financing_cf":  _money(pt.get("financing_cf")),
            "net_cash_flow": _money(pt.get("net_cash_flow")),
            "closing_cash":  _money(pt.get("closing_cash")),
        })

    # ── Working Capital ──────────────────────────────────────────────────
    working_capital = []
    for pt in (raw.get("working_capital") or []):
        iso = _period_to_iso(pt.get("period"))
        dso = pt.get("dso")
        dio = pt.get("dio")
        dpo = pt.get("dpo")
        nwc = pt.get("nwc")
        ccc = pt.get("ccc")
        # CCC = DSO + DIO - DPO
        if ccc is None and dso is not None and dio is not None and dpo is not None:
            try:
                ccc = round(float(dso) + float(dio) - float(dpo), 2)
            except (TypeError, ValueError):
                pass
        working_capital.append({
            "period": iso or str(pt.get("period", "")),
            "dso": _num(dso),
            "dio": _num(dio),
            "dpo": _num(dpo),
            "nwc": _money(nwc),
            "ccc": _num(ccc),
        })

    # ── Budget vs Actual ─────────────────────────────────────────────────
    budget_vs_actual = []
    for pt in (raw.get("budget_vs_actual") or []):
        actual = _money(pt.get("actual"))
        budget = _money(pt.get("budget"))
        line_item_str = str(pt.get("line_item", "")).strip().lower()
        # Cost rows (COGS/OPEX/expenses) should be reported as positive magnitudes.
        if any(k in line_item_str for k in ("cogs", "opex", "expense", "cost of")):
            if actual is not None and actual < 0:
                actual = abs(actual)
            if budget is not None and budget < 0:
                budget = abs(budget)
        variance = pt.get("variance")
        if variance is None and actual is not None and budget is not None:
            variance = round(actual - budget, 2)
        else:
            variance = _money(variance)
        var_pct_raw = pt.get("variance_pct")
        if var_pct_raw is None and variance is not None and budget:
            var_pct = round(variance / budget * 100, 2)
        elif var_pct_raw is not None:
            try:
                f = float(var_pct_raw)
                if -1.0 < f < 1.0 and f != 0 and budget and variance is not None:
                    computed = variance / budget * 100
                    if abs(abs(computed) - abs(f * 100)) < 0.5:
                        f = f * 100
                var_pct = round(f, 2)
            except (TypeError, ValueError):
                var_pct = None
        else:
            var_pct = None

        budget_vs_actual.append({
            "period": str(pt.get("period", "")),
            "line_item": str(pt.get("line_item", "")),
            "budget": budget,
            "actual": actual,
            "variance": variance,
            "variance_pct": var_pct,
        })

    # ── Sales by Segment ─────────────────────────────────────────────────
    sales_by_segment = []
    for pt in (raw.get("sales_by_segment") or []):
        rev = _money(pt.get("revenue"))
        gm = _money(pt.get("gross_margin"))
        gm_pct = _pct(pt.get("gm_pct"))
        if gm_pct is None and rev and gm:
            gm_pct = round(gm / rev * 100, 2)
        sales_by_segment.append({
            "label": str(pt.get("label", "")),
            "revenue": rev,
            "gross_margin": gm,
            "gm_pct": gm_pct,
        })

    # ── Sales by Geography ───────────────────────────────────────────────
    sales_by_geo = []
    for pt in (raw.get("sales_by_geo") or []):
        rev = _money(pt.get("revenue"))
        gm = _money(pt.get("gross_margin"))
        gm_pct = _pct(pt.get("gm_pct"))
        if gm_pct is None and rev and gm:
            gm_pct = round(gm / rev * 100, 2)
        sales_by_geo.append({
            "label": str(pt.get("label", "")),
            "revenue": rev,
            "gross_margin": gm,
            "gm_pct": gm_pct,
        })

    # ── Cost Structure ───────────────────────────────────────────────────
    raw_cs = raw.get("cost_structure") or {}
    cost_structure = {
        "cogs_pct":   _pct(raw_cs.get("cogs_pct")),
        "gp_pct":     _pct(raw_cs.get("gp_pct")),
        "opex_pct":   _pct(raw_cs.get("opex_pct")),
        "ebitda_pct": _pct(raw_cs.get("ebitda_pct")),
    }
    # Derive from summary if missing
    rev = summary.get("revenue")
    if rev:
        if cost_structure["cogs_pct"] is None and summary.get("cogs") is not None:
            cost_structure["cogs_pct"] = round(summary["cogs"] / rev * 100, 2)
        if cost_structure["gp_pct"] is None and summary.get("gross_profit") is not None:
            cost_structure["gp_pct"] = round(summary["gross_profit"] / rev * 100, 2)
        if cost_structure["opex_pct"] is None and summary.get("opex") is not None:
            cost_structure["opex_pct"] = round(summary["opex"] / rev * 100, 2)
        if cost_structure["ebitda_pct"] is None and summary.get("ebitda") is not None:
            cost_structure["ebitda_pct"] = round(summary["ebitda"] / rev * 100, 2)

    raw_conf = raw.get("confidence") or {}
    def _c(k, default=0.5):
        try:
            v = float(raw_conf.get(k, default))
            return max(0.0, min(1.0, round(v, 3)))
        except (TypeError, ValueError):
            return default
    conf_sections = {
        "summary":          _c("summary"),
        "monthly_pl":       _c("monthly_pl"),
        "cash_flow":        _c("cash_flow"),
        "working_capital":  _c("working_capital"),
        "budget_vs_actual": _c("budget_vs_actual"),
        "sales_by_segment": _c("sales_by_segment"),
        "sales_by_geo":     _c("sales_by_geo"),
    }
    overall = raw_conf.get("overall")
    try:
        overall = float(overall) if overall is not None else None
    except (TypeError, ValueError):
        overall = None
    if overall is None:
        overall = round(sum(conf_sections.values()) / len(conf_sections), 3)
    confidence = {
        **conf_sections,
        "overall": max(0.0, min(1.0, round(overall, 3))),
        "notes": str(raw_conf.get("notes", ""))[:500],
    }

    return {
        "summary": summary,
        "monthly_pl": monthly_pl,
        "cash_flow": cash_flow,
        "working_capital": working_capital,
        "budget_vs_actual": budget_vs_actual,
        "sales_by_segment": sales_by_segment,
        "sales_by_geo": sales_by_geo,
        "cost_structure": cost_structure,
        "confidence": confidence,
    }


def _num(v):
    """Safe float conversion, no scaling."""
    if v is None:
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None
