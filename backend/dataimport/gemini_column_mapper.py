"""
Gemini-powered two-pass column mapper for fund Excel files.

Pass 1 — Sheet Classification: identifies which domain each sheet belongs to.
Pass 2 — Column Mapping: maps Excel column headers to canonical field names.

Follows the proven two-pass pattern from gemini_mis_parser.py:
  temperature=0, response_mime_type="application/json", confidence scoring.
"""

import json
import logging
import re
import os
import time
from typing import Optional

import openpyxl
import google.generativeai as genai
from django.conf import settings

from .canonical_schema import SHEET_DOMAINS, DOMAIN_FIELDS

logger = logging.getLogger(__name__)

_configured = False

# Retry settings for Gemini API calls
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2  # seconds; exponential: 2, 4, 8


def _ensure_configured():
    global _configured
    if not _configured:
        api_key = getattr(settings, 'GEMINI_API_KEY', '') or os.environ.get('GEMINI_API_KEY', '')
        if not api_key:
            raise ValueError('GEMINI_API_KEY not set in .env / Django settings')
        genai.configure(api_key=api_key)
        _configured = True


def _get_model():
    """Get a configured Gemini model with deterministic output."""
    _ensure_configured()
    model_name = getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash')
    return genai.GenerativeModel(
        model_name=model_name,
        generation_config={
            'temperature': 0,
            'response_mime_type': 'application/json',
        },
    )


def _call_gemini(prompt, context_label=''):
    """Call Gemini with retry + exponential backoff.

    Retries on transient errors (rate limits, network, server errors).
    Raises on permanent errors (bad API key, invalid model, etc.).
    Returns parsed JSON dict.
    """
    model = _get_model()
    last_error = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            # NO HARDCODED TIMEOUT — production policy is "let Gemini take
            # the time it needs to be accurate". The previous 60s ceiling
            # caused NAV_CALC and other dense-prompt sheets to fail when
            # Gemini was actually working correctly, just slowly. The Google
            # SDK's internal default (~10 min) is the only safety net, and it
            # only fires on a true network-level failure.
            response = model.generate_content(prompt)

            # Check for empty/blocked responses
            if not response.text:
                if response.prompt_feedback and response.prompt_feedback.block_reason:
                    raise ValueError(
                        f'Gemini blocked prompt ({context_label}): '
                        f'{response.prompt_feedback.block_reason}'
                    )
                raise ValueError(f'Gemini returned empty response ({context_label})')

            result = _parse_json_response(response.text)
            if attempt > 1:
                logger.info(
                    f'Gemini {context_label} succeeded on attempt {attempt}'
                )
            return result

        except (json.JSONDecodeError, ValueError) as e:
            # Non-retryable: bad response format or blocked prompt
            logger.error(f'Gemini {context_label} non-retryable error: {e}')
            raise

        except Exception as e:
            last_error = e
            err_name = type(e).__name__
            err_str = str(e)

            # Classify error for retry decision
            is_rate_limit = '429' in err_str or 'quota' in err_str.lower()
            is_server_error = any(
                code in err_str for code in ('500', '502', '503', '504')
            )
            is_transient = is_rate_limit or is_server_error or 'timeout' in err_str.lower()

            if not is_transient or attempt == _MAX_RETRIES:
                logger.error(
                    f'Gemini {context_label} failed after {attempt} attempt(s): '
                    f'{err_name}: {err_str}'
                )
                raise

            wait = _RETRY_BACKOFF_BASE ** attempt
            if is_rate_limit:
                wait = max(wait, 10)  # rate limits need longer backoff
            logger.warning(
                f'Gemini {context_label} attempt {attempt} failed ({err_name}), '
                f'retrying in {wait}s...'
            )
            time.sleep(wait)

    raise last_error


def _parse_json_response(text):
    """Parse JSON from Gemini response, handling markdown fences."""
    text = text.strip()
    # Strip markdown code fences
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the response
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            return json.loads(match.group())
        raise


def _build_cross_sheet_value_cache(filepath):
    """
    Load workbook twice (data_only and formula) to resolve cross-sheet cell references.

    Many fund Excel files use formulas like ='Portfolio Companies'!B10 or =Sheet2!C5
    to pull values from other sheets. openpyxl with data_only=True reads cached
    formula results; when the cache is empty (None), we parse the formula string
    and fetch the value from the referenced sheet instead.

    Returns a dict: {(sheet_name, row, col): resolved_value}
    where row and col are 1-based integers.
    """
    # Regex for single-cell cross-sheet reference: ='Sheet Name'!A1 or =Sheet!B2
    XREF_RE = re.compile(
        r"^=\s*'?([^'!\r\n]+?)'?\s*!\s*([A-Z]+)(\d+)\s*$", re.IGNORECASE
    )

    cache = {}
    try:
        # Load with data_only first (gets cached formula values)
        wb_data = openpyxl.load_workbook(filepath, data_only=True)
        # Load without data_only to get formula strings for cells with no cache
        wb_formula = openpyxl.load_workbook(filepath, data_only=False)
    except Exception as e:
        logger.warning(f'Cross-sheet cache build failed: {e}')
        return cache

    try:
        from openpyxl.utils import column_index_from_string

        for sname in wb_data.sheetnames:
            ws_data = wb_data[sname]
            ws_formula = wb_formula[sname] if sname in wb_formula.sheetnames else None

            for row in ws_data.iter_rows():
                for cell in row:
                    val = cell.value
                    if val is not None:
                        cache[(sname, cell.row, cell.column)] = val
                        continue

                    # Cell has no cached value — check for cross-sheet formula
                    if ws_formula is None:
                        continue
                    formula_cell = ws_formula.cell(row=cell.row, column=cell.column)
                    formula = formula_cell.value
                    if not formula or not isinstance(formula, str):
                        continue
                    formula = formula.strip()
                    if not formula.startswith('='):
                        continue

                    m = XREF_RE.match(formula)
                    if not m:
                        continue

                    ref_sheet = m.group(1).strip()
                    ref_col = column_index_from_string(m.group(2))
                    ref_row = int(m.group(3))

                    if ref_sheet not in wb_data.sheetnames:
                        continue

                    # Read from the referenced sheet's data-only version
                    ref_ws = wb_data[ref_sheet]
                    ref_val = ref_ws.cell(row=ref_row, column=ref_col).value
                    if ref_val is not None:
                        cache[(sname, cell.row, cell.column)] = ref_val

    except Exception as e:
        logger.warning(f'Cross-sheet resolution error: {e}')
    finally:
        try:
            wb_data.close()
            wb_formula.close()
        except Exception:
            pass

    return cache


# ---------------------------------------------------------------------------
# Pass 3: Semantic Value Interpretation functions
# These replace ALL hardcoded keyword dictionaries in import_service.py.
# ---------------------------------------------------------------------------

_classification_cache = {}


def clear_classification_cache():
    """Clear the module-level classification cache.

    Call at the start of each import to prevent stale classifications
    from a previous import session.
    """
    _classification_cache.clear()


def classify_labels(labels, category_key, canonical_options, context=''):
    """Classify a batch of text labels into canonical categories via Gemini.

    Works for ANY language — German, Japanese, French, Hindi, Arabic, etc.
    Uses the canonical_options descriptions for semantic matching.

    Args:
        labels: list of unique text labels to classify
        category_key: string key for caching (e.g., 'pl_line_items')
        canonical_options: dict {canonical_key: description}
        context: optional domain context string for the prompt

    Returns:
        dict {original_label: canonical_key_or_None}
    """
    if not labels:
        return {}

    unique_labels = list(set(l for l in labels if l and str(l).strip()))
    if not unique_labels:
        return {}

    cache_key = ('classify', category_key, frozenset(unique_labels))
    if cache_key in _classification_cache:
        return _classification_cache[cache_key]

    # canonical_options may now use either the legacy string shape
    # ({key: 'description'}) or the new dict shape
    # ({key: {'description': '...', 'value_type': '...', 'requires_variant': ...}}).
    # Extract just the description here — the structural metadata is
    # consumed by Pass 3.5 disambiguation later, not by classify_labels.
    def _desc(v):
        if isinstance(v, dict):
            return v.get('description', '') or str(v)
        return str(v)

    options_text = '\n'.join(
        f'  "{k}": {_desc(v)}' for k, v in canonical_options.items()
    )
    labels_text = '\n'.join(f'  - "{l}"' for l in unique_labels)
    context_line = f'\nDOMAIN CONTEXT: {context}\n' if context else ''

    prompt = SHARED_MISSION_PREAMBLE + f"""You are a financial data classifier for an Alternative Investment Fund (AIF) Excel file.
You have 20+ years of experience in fund accounting, LP/GP economics, and financial reporting.

TASK: Classify each label below into exactly one canonical category.
{context_line}
CANONICAL CATEGORIES (pick the BEST match for each label):
{options_text}

LABELS TO CLASSIFY:
{labels_text}

RULES:
1. Match SEMANTICALLY, not syntactically. Handle ANY language — German, Japanese, French, Hindi, Arabic, Indonesian, etc.
2. Ignore units/suffixes in brackets: (Cr), (Lakhs), ($M), (₹), (Mn), (Rs), (in '000s)
3. Ignore trailing %, #, or special characters when matching
4. If a label clearly does not match ANY category, return null for it
5. Be case-insensitive. "EBITDA" = "ebitda" = "Ebitda"
6. Handle abbreviations: "Rev" = Revenue, "D&A" = Depreciation & Amortisation, "GP" = Gross Profit
7. Handle partial matches: "Emp Cost" = Employee Cost, "Mktg" = Marketing

Return a JSON object mapping each label to its canonical key (or null):
{{"label_text": "canonical_key", "other_label": null, ...}}"""

    try:
        result = _call_gemini(prompt, context_label=f'Pass3-classify-{category_key}')

        valid_keys = set(canonical_options.keys())
        normalized = {}
        for label in unique_labels:
            key = result.get(label)
            if key and key in valid_keys:
                normalized[label] = key
            else:
                normalized[label] = None

        _classification_cache[cache_key] = normalized
        logger.info(
            f'[GEMINI Pass3] classify_labels({category_key}): '
            f'{len(unique_labels)} labels → '
            f'{sum(1 for v in normalized.values() if v)} classified'
        )
        return normalized

    except Exception as e:
        logger.warning(f'Gemini classify_labels failed for {category_key}: {e}')
        return {l: None for l in unique_labels}


def classify_enum_values(values, enum_key, enum_options, context=''):
    """Classify text values into a closed set of enum choices via Gemini.

    Unlike classify_labels, this ALWAYS returns a valid enum value (never null).
    Every value MUST map to the closest matching option.

    Args:
        values: list of unique text values to classify
        enum_key: string key for caching (e.g., 'exit_type')
        enum_options: dict {enum_value: description}
        context: optional domain context

    Returns:
        dict {original_value: enum_key}
    """
    if not values:
        return {}

    unique_values = list(set(v for v in values if v and str(v).strip()))
    if not unique_values:
        return {}

    cache_key = ('enum', enum_key, frozenset(unique_values))
    if cache_key in _classification_cache:
        return _classification_cache[cache_key]

    options_text = '\n'.join(
        f'  "{k}": {v}' for k, v in enum_options.items()
    )
    values_text = '\n'.join(f'  - "{v}"' for v in unique_values)
    context_line = f'\nDOMAIN CONTEXT: {context}\n' if context else ''

    prompt = SHARED_MISSION_PREAMBLE + f"""You are a financial data classifier for an Alternative Investment Fund (AIF) Excel file.

TASK: Classify each value below into exactly one of the allowed enum options.
{context_line}
ALLOWED OPTIONS (you MUST pick one for each value — never return null):
{options_text}

VALUES TO CLASSIFY:
{values_text}

RULES:
1. Every value MUST map to one of the listed options. Never return null.
2. If unsure, pick the CLOSEST matching option.
3. Match SEMANTICALLY across ANY language — German, Japanese, French, Hindi, etc.
4. Be case-insensitive.
5. Handle abbreviations and partial matches.

Return a JSON object mapping each value to its enum key:
{{"value_text": "enum_key", ...}}"""

    try:
        result = _call_gemini(prompt, context_label=f'Pass3-enum-{enum_key}')

        valid_keys = set(enum_options.keys())
        default_key = list(enum_options.keys())[0]
        normalized = {}
        for value in unique_values:
            key = result.get(value)
            if key and key in valid_keys:
                normalized[value] = key
            else:
                normalized[value] = default_key

        _classification_cache[cache_key] = normalized
        logger.info(
            f'[GEMINI Pass3] classify_enum({enum_key}): '
            f'{len(unique_values)} values classified'
        )
        return normalized

    except Exception as e:
        logger.warning(f'Gemini classify_enum failed for {enum_key}: {e}')
        default_key = list(enum_options.keys())[0]
        return {v: default_key for v in unique_values}


def extract_structured_metadata(label_value_pairs, field_definitions, context=''):
    """Extract structured metadata from label-value pairs via Gemini.

    Replaces LIFECYCLE_PATTERNS and fund metadata extraction.
    Works for ANY language.

    Args:
        label_value_pairs: list of (label_str, value_str) tuples
        field_definitions: dict {field_name: {desc: str, type: str}}
        context: optional domain context

    Returns:
        dict {field_name: raw_value_string}
    """
    if not label_value_pairs:
        return {}

    # Accept both dict and list-of-tuples
    if isinstance(label_value_pairs, dict):
        label_value_pairs = list(label_value_pairs.items())

    filtered = [(l, v) for l, v in label_value_pairs
                 if l and str(l).strip() and v is not None and str(v).strip()]
    if not filtered:
        return {}

    cache_key = ('metadata', context, frozenset((l, str(v)) for l, v in filtered))
    if cache_key in _classification_cache:
        return _classification_cache[cache_key]

    fields_text = '\n'.join(
        f'  "{k}": {fd["desc"]} (type: {fd["type"]})'
        for k, fd in field_definitions.items()
    )
    # NO 100-pair cap: send every label-value pair to Gemini. Truncating to
    # 100 silently dropped pairs from large metadata sheets (and the dropped
    # tail often contained the field we needed).
    pairs_text = '\n'.join(
        f'  "{l}" → "{v}"' for l, v in filtered
    )
    context_line = f'\nDOMAIN CONTEXT: {context}\n' if context else ''

    prompt = SHARED_MISSION_PREAMBLE + f"""You are a financial data extractor for an Alternative Investment Fund (AIF) Excel file.
You have deep knowledge of fund structures, SEBI regulations, and fund accounting.

TASK: Extract structured metadata from these key-value pairs found in Excel cells.
{context_line}
TARGET FIELDS (extract into these canonical fields):
{fields_text}

LABEL-VALUE PAIRS FROM EXCEL:
{pairs_text}

RULES:
1. Match labels SEMANTICALLY — handle ANY language (German, Japanese, French, Hindi, etc.)
2. Return the raw value text as-is from the Excel cell — do NOT convert units or parse dates
3. For enum-type fields (carry_type, fee_basis, structure_type), return the canonical value:
   - carry_type: "european" or "american"
   - fee_basis: "committed", "called", or "nav"
   - structure_type: "trust", "llp", or "company"
4. For bool-type fields (is_gift_city), return "true" or "false"
5. Skip pairs that do not match any target field
6. If multiple pairs match the same field, use the most specific/detailed one

Return a JSON object with only the matched fields:
{{"field_name": "raw_value_from_excel", ...}}"""

    try:
        result = _call_gemini(prompt, context_label=f'Pass3-metadata-{context}')

        valid_fields = set(field_definitions.keys())
        cleaned = {k: str(v) for k, v in result.items()
                   if k in valid_fields and v is not None and str(v).strip()}

        _classification_cache[cache_key] = cleaned
        logger.info(
            f'[GEMINI Pass3] extract_metadata({context}): '
            f'{len(filtered)} pairs → {len(cleaned)} fields extracted'
        )
        return cleaned

    except Exception as e:
        logger.warning(f'Gemini metadata extraction failed ({context}): {e}')
        return {}


def detect_currency_and_unit(headers, sample_values=None, sheet_name=''):
    """Detect currency and numeric unit from sheet context via Gemini.

    Args:
        headers: list of column header strings
        sample_values: optional list of sample numeric value strings
        sheet_name: sheet name for context

    Returns:
        dict {currency: 'INR', unit_multiplier: 10000000, unit_label: 'Cr'}
    """
    default_result = {'currency': 'INR', 'unit_multiplier': 10000000, 'unit_label': 'Cr'}

    if not headers:
        return default_result

    headers_text = ', '.join(f'"{h}"' for h in headers if h)
    samples_text = ', '.join(str(v) for v in (sample_values or [])[:20])

    cache_key = ('currency', sheet_name, frozenset(str(h) for h in headers if h))
    if cache_key in _classification_cache:
        return _classification_cache[cache_key]

    prompt = SHARED_MISSION_PREAMBLE + f"""Detect the currency and numeric unit used in this financial spreadsheet sheet.

Sheet name: "{sheet_name}"
Column headers: {headers_text}
Sample values: {samples_text or 'N/A'}

Detect:
1. Currency: INR, USD, EUR, GBP, JPY, SGD, AED, CHF, etc.
2. Numeric unit (what multiplier the numbers represent):
   - "Cr" or "Crore" or "Crores" → multiplier 10000000
   - "Lakhs" or "Lacs" or "Lac" → multiplier 100000
   - "Mn" or "Million" or "M" → multiplier 1000000
   - "Bn" or "Billion" or "B" → multiplier 1000000000
   - "K" or "Thousands" or "'000s" → multiplier 1000
   - No unit indicator → multiplier 1

Look for clues in:
- Column headers containing: "(Cr)", "(₹Cr)", "($M)", "(Rs. Lakhs)", "(in '000s)", "(Mn)"
- Currency symbols: ₹, $, €, £, ¥
- Sheet name patterns
- If no currency clue found, default to INR
- If no unit clue found, look at sample value magnitudes to infer

Return JSON: {{"currency": "INR", "unit_multiplier": 10000000, "unit_label": "Cr"}}"""

    try:
        result = _call_gemini(prompt, context_label=f'Pass3-currency-{sheet_name}')

        output = {
            'currency': result.get('currency', 'INR'),
            'unit_multiplier': int(result.get('unit_multiplier', 10000000)),
            'unit_label': result.get('unit_label', 'Cr'),
        }
        _classification_cache[cache_key] = output
        logger.info(
            f'[GEMINI Pass3] currency({sheet_name}): '
            f'{output["currency"]} in {output["unit_label"]}'
        )
        return output

    except Exception as e:
        logger.warning(f'Gemini currency detection failed for {sheet_name}: {e}')
        return default_result


def detect_sheet_layout(filepath, sheet_name, sample_top_rows=None, sample_bottom_rows=None):
    """Pass 2.5 — Detect the table layout(s) inside ONE sheet via Gemini.

    Replaces the brittle Python heuristic (`_is_section_title_row` +
    `_read_data_rows`) that fails on sheets whose first rows are
    banner/disclaimer/formula-legend text rather than headers.

    The call is per-sheet (NOT per-row) — one Gemini round-trip returns the
    full layout map for the sheet:
      - which rows are pre-header noise (banner / disclaimer / sub-title)
      - which row is the REAL header row (column names live here)
      - where the actual data rows start and end
      - whether the sheet contains multiple stacked sub-tables, each with its
        own header + data range
      - which columns are formula-derived in the Excel layout (e.g. the NAV
        sheet declares "Col I TotalNAV = C+E+F-D") and how to compute them
        from sibling columns when the cell value is empty

    Returns a strict JSON dict on success:
    {
      "sub_tables": [
        {
          "section_name": str,                # "PORTFOLIO INVESTMENTS" or "" if no banner
          "skip_rows_above": [int, ...],      # 0-indexed rows to ignore (banner, disclaimer)
          "header_row": int,                  # 0-indexed row whose cells name each column
          "data_start": int,                  # 0-indexed first real data row
          "data_end": int,                    # 0-indexed last real data row (inclusive)
          "derived_columns": [
            {
              "column_name": str,             # header text of the derived column
              "formula_components": [         # ordered terms: sum of (sign * source_column)
                {"sign": "+" | "-", "source_column": str},
                ...
              ],
              "source": "disclaimer_row" | "standard_formula"
            }
          ]
        }
      ]
    }

    Raises on:
      * Network / Gemini API failures (propagated from _call_gemini)
      * Malformed JSON
      * Layout validation failures (header_row >= data_start, indices out of
        bounds, etc.) — caller MUST treat as failure and may fall back to the
        deterministic heuristic.
    """
    import openpyxl

    cache_key = ('layout', filepath, sheet_name)
    if cache_key in _classification_cache:
        return _classification_cache[cache_key]

    try:
        wb = openpyxl.load_workbook(filepath, data_only=True, read_only=False)
        if sheet_name not in wb.sheetnames:
            raise ValueError(f'Sheet "{sheet_name}" not found in {filepath}')
        ws = wb[sheet_name]
        max_row = ws.max_row or 0
        max_col = ws.max_column or 0
    except Exception as e:
        raise ValueError(f'Could not open sheet "{sheet_name}": {e}')

    if max_row == 0 or max_col == 0:
        empty = {'sub_tables': []}
        _classification_cache[cache_key] = empty
        wb.close()
        return empty

    # FULL-COVERAGE: send EVERY row of the sheet to Gemini. No sampling.
    #
    # Earlier this function sampled only first-25 + last-5 rows. That broke
    # multi-section sheets (e.g. a 140-row Compliance Tracker with a
    # per-company grid in rows 3-131 and a fund-level filings sub-table in
    # rows 132-139): Gemini saw rows 0-24 + 135-139 and reported only ONE
    # sub-table truncated at row 24, missing 80 % of the data and the
    # entire second sub-table.
    #
    # Sending every row eliminates the entire class of "Gemini couldn't
    # see the middle of the sheet" failures and is universal — applies to
    # every domain and every sheet shape, with zero heuristics. The
    # `sample_top_rows`/`sample_bottom_rows` parameters are kept on the
    # signature for backwards compatibility but are now unused.
    sample_idxs = list(range(0, max_row))

    def _cell_preview(v):
        if v is None:
            return ''
        s = str(v).strip()
        return s[:80] if len(s) > 80 else s

    rows_for_prompt = []
    cap_col = min(max_col, 20)
    for ridx_0 in sample_idxs:
        cells = [_cell_preview(ws.cell(ridx_0 + 1, c).value) for c in range(1, cap_col + 1)]
        rows_for_prompt.append((ridx_0, cells))

    wb.close()

    # Build the prompt
    rows_text = '\n'.join(
        f'[Row {ridx}]: ' + ' | '.join(f'"{c}"' if c else '<empty>' for c in cells)
        for ridx, cells in rows_for_prompt
    )

    prompt = SHARED_MISSION_PREAMBLE + f"""You are a precise table-layout detector for an Indian AIF (Alternative Investment Fund) Excel sheet.

A data importer will use your output to read this sheet ROW-BY-ROW. Wrong row indices = wrong data imported. Be exact.

SHEET NAME: "{sheet_name}"
SHEET DIMENSIONS: {max_row} rows × {max_col} columns
SHOWING ALL {len(rows_for_prompt)} rows of the sheet with 0-BASED row indices and first {cap_col} columns. You see the FULL sheet — there is no sampling, no truncation. Identify EVERY sub-table that exists; do not under-report:

{rows_text}

TASK: Identify the table layout. Common patterns in fund files:
  - Row 0 may be a BANNER  (sheet title in caps + fund name) e.g. "PORTFOLIO INVESTMENTS | Multiples IV"
  - Row 1 may be a DISCLAIMER or FORMULA LEGEND e.g. "Blue = Input | Black = Formula" or "Col I TotalNAV = C+E+F-D"
  - The next row is the REAL HEADER (short noun-phrase column names like "Company Name", "Cost", "Date")
  - Below the header are data rows
  - Multiple stacked sub-tables may exist, each with its own banner+header

RETURN STRICT JSON exactly matching this schema:
{{
  "sub_tables": [
    {{
      "section_name": "string — banner text if any, else empty string",
      "skip_rows_above": [list of 0-indexed rows to ignore — banner, disclaimer, blank lines above the header],
      "header_row": "integer — 0-indexed row whose cells are the column NAMES",
      "data_start": "integer — 0-indexed first real data row (must be > header_row)",
      "data_end":   "integer — 0-indexed last real data row (must be <= {max_row - 1})",
      "derived_columns": [
        {{
          "column_name": "exact header text of a column whose value is a formula",
          "formula_components": [
            {{"sign": "+", "source_column": "exact header text of source column"}},
            {{"sign": "-", "source_column": "exact header text of source column"}}
          ],
          "source": "disclaimer_row OR standard_formula"
        }}
      ]
    }}
  ]
}}

CRITICAL RULES — violations cause data corruption:
1. All row indices are 0-BASED. Row 0 is the first row of the sheet.
2. header_row MUST point to a row whose cells are SHORT NOUN-PHRASE column names (≤ 5 words each, no sentences, no formulas, no all-caps banner text).
3. header_row MUST be strictly < data_start. data_start MUST be strictly <= data_end. data_end MUST be < {max_row} (the sheet size).
4. skip_rows_above lists rows BETWEEN the start of this sub-table and the header_row that should be ignored. Do NOT include the header_row itself.
5. If the sheet has NO tabular data (e.g. a Cover/Summary/Index sheet), return {{"sub_tables": []}}.
6. If the sheet has ONE table starting at row 0 with no banner, return a single sub_table with skip_rows_above=[] and header_row=0.
7. If the sheet has MULTIPLE stacked sub-tables (separated by banner/blank rows), return ONE entry per sub-table in top-to-bottom order. data_end of sub_table N MUST be < skip_rows_above[0] (or header_row) of sub_table N+1.
8. data_end MUST exclude any "Total", "Grand Total", "Sub-total", or summary footer rows — only real data rows count.
9. derived_columns: include ONLY columns that are explicitly declared as formulas in the disclaimer/legend rows (source="disclaimer_row") OR are standard SEBI AIF accounting identities that the importer should compute when the cell is blank (source="standard_formula"). Examples:
     - NAV sheet: "Total NAV" = Total Investments + Unrealized Gains + Realized Gains − Mgmt Fee − Fund Expenses
     - Valuations sheet: "MOIC" = FV Holding / Cost
   Use the EXACT column header text in source_column — the importer will look up these column names from the header_row.
10. NEVER invent rows or columns that are not in the sample shown. NEVER guess. If unsure, omit derived_columns entirely.

Respond with ONLY the JSON object."""

    result = _call_gemini(prompt, context_label=f'Pass2.5-layout-{sheet_name}')

    # Validate the response structure
    if not isinstance(result, dict) or 'sub_tables' not in result:
        raise ValueError(f'Gemini layout response missing "sub_tables" for {sheet_name}')
    sub_tables = result.get('sub_tables') or []
    if not isinstance(sub_tables, list):
        raise ValueError(f'Gemini layout "sub_tables" is not a list for {sheet_name}')

    validated = []
    last_end = -1
    for idx, st in enumerate(sub_tables):
        if not isinstance(st, dict):
            continue
        try:
            header_row = int(st.get('header_row'))
            data_start = int(st.get('data_start'))
            data_end   = int(st.get('data_end'))
        except (TypeError, ValueError):
            logger.warning(
                f'Layout {sheet_name} sub_table {idx} has non-integer indices; skipping'
            )
            continue

        # Strict validation: indices must be within bounds and ordered.
        if not (0 <= header_row < data_start <= data_end < max_row):
            logger.warning(
                f'Layout {sheet_name} sub_table {idx} indices out of order/bounds '
                f'(header={header_row}, start={data_start}, end={data_end}, sheet_rows={max_row}); skipping'
            )
            continue

        # Stacked sub-tables must not overlap
        if header_row <= last_end:
            logger.warning(
                f'Layout {sheet_name} sub_table {idx} overlaps previous (header={header_row} <= prev_end={last_end}); skipping'
            )
            continue
        last_end = data_end

        skip_above = st.get('skip_rows_above') or []
        if not isinstance(skip_above, list):
            skip_above = []
        skip_above = [int(x) for x in skip_above if isinstance(x, (int, float)) and 0 <= int(x) < header_row]

        derived = []
        for dc in (st.get('derived_columns') or []):
            if not isinstance(dc, dict):
                continue
            col_name = (dc.get('column_name') or '').strip()
            comps = dc.get('formula_components') or []
            if not col_name or not isinstance(comps, list) or not comps:
                continue
            clean_comps = []
            for comp in comps:
                if not isinstance(comp, dict):
                    continue
                sign = comp.get('sign', '+')
                src  = (comp.get('source_column') or '').strip()
                if sign not in ('+', '-') or not src:
                    continue
                clean_comps.append({'sign': sign, 'source_column': src})
            if clean_comps:
                derived.append({
                    'column_name': col_name,
                    'formula_components': clean_comps,
                    'source': dc.get('source', 'standard_formula'),
                })

        validated.append({
            'section_name':    (st.get('section_name') or '').strip(),
            'skip_rows_above': skip_above,
            'header_row':      header_row,
            'data_start':      data_start,
            'data_end':        data_end,
            'derived_columns': derived,
        })

    final = {'sub_tables': validated}
    _classification_cache[cache_key] = final
    logger.info(
        f'Pass2.5 layout for "{sheet_name}": {len(validated)} sub-table(s); '
        + ', '.join(
            f'header={st["header_row"]} data={st["data_start"]}-{st["data_end"]}'
            + (f' derived={len(st["derived_columns"])}' if st['derived_columns'] else '')
            for st in validated
        )
    )
    return final


def classify_subtable_purpose(headers, sample_rows, allowed_purposes, context=''):
    """Semantically classify what a sub-table inside a sheet actually IS.

    Used by domain-specific importers (e.g. _import_compliance) as a
    defense-in-depth check AFTER Pass 1 (sheet → domain) and Pass 2.5
    (sheet → sub-tables). If Pass 1 mis-classifies a sheet — for example,
    a workbook's "VALIDATION" sheet getting tagged as 'compliance' because
    Gemini matched on the word "compliance test report" — this call lets
    the importer cleanly reject sub-tables that don't actually carry the
    expected kind of data, AND route legitimate sub-tables to the correct
    write path without any keyword matching at all.

    headers:           list[str] — header-row column labels for the sub-table
    sample_rows:       list[list[str]] — up to ~5 representative data rows
                       (one inner list per row, one string per cell)
    allowed_purposes:  dict[str, str] — {purpose_key: description}
                       PLUS an implicit 'other' bucket that means "doesn't
                       match any of the allowed purposes". Callers should
                       skip sub-tables that return 'other'.
    context:           str — short free-form context for the prompt
                       (e.g. "compliance domain sheet 'Compliance Tracker'")

    Returns: one of the purpose_keys OR 'other' (never None).
    """
    if not allowed_purposes:
        return 'other'

    # Build a representation of the input — NO row or cell-text cap.
    header_line = ' | '.join(str(h) for h in headers if h)
    sample_text = '\n'.join(
        '  Row ' + str(i + 1) + ': ' + ' | '.join(
            (str(c) if c is not None else '') for c in row
        )
        for i, row in enumerate(sample_rows or [])
    ) or '  (no sample rows)'

    purpose_lines = '\n'.join(
        f'  - {key}: {desc}'
        for key, desc in allowed_purposes.items()
    )
    purpose_lines += '\n  - other: the sub-table is none of the above (e.g. instructions, legend, file-integrity validation rules, sample data, metadata, examples — anything that should NOT be written to the target tables)'

    cache_key = (
        'subtable_purpose',
        tuple(allowed_purposes.keys()),
        tuple(headers or []),
        tuple(tuple(r) for r in (sample_rows or [])),
    )
    if cache_key in _classification_cache:
        return _classification_cache[cache_key]

    prompt = SHARED_MISSION_PREAMBLE + f"""You are classifying the purpose of a sub-table found inside an Indian AIF Excel sheet.

CONTEXT: {context or '(no extra context)'}

SUB-TABLE HEADERS:
  {header_line}

SUB-TABLE SAMPLE ROWS (first few real data rows):
{sample_text}

POSSIBLE PURPOSES:
{purpose_lines}

TASK: choose exactly ONE purpose key from the list above. Judge by the SEMANTIC content of the headers and rows together — what kind of information does this sub-table actually carry? Do NOT match on individual keywords. A sub-table whose rows describe file-integrity checks, formula validations, sample/test data, or instructional content is 'other' even if a header word happens to overlap one of the allowed purposes.

RETURN STRICT JSON: {{"purpose": "<one_of_the_keys_or_other>"}}"""

    try:
        result = _call_gemini(prompt, context_label='subtable-purpose')
        purpose = (result or {}).get('purpose') or 'other'
    except Exception as e:
        logger.warning(f'Sub-table purpose classification failed: {e}')
        purpose = 'other'

    if purpose not in allowed_purposes and purpose != 'other':
        purpose = 'other'

    _classification_cache[cache_key] = purpose
    return purpose


def _extract_sheet_previews(filepath):
    """
    Read an Excel file and extract sheet names + first 5 rows of each sheet.

    Uses data_only=True to get cached formula values, then resolves any cells
    that have cross-sheet formula references (e.g. ='Portfolio'!B10) so that
    Gemini sees the actual values rather than blanks.

    IMPORTANT: Do NOT use read_only=True here. In read_only mode, openpyxl
    returns EmptyCell objects for empty cells — these lack .row and .column
    attributes, causing AttributeError crashes when we look up the xsheet_cache.

    No row-scan cap: production policy is "Gemini sees everything the sheet
    has to offer". Previously this read only the first 6 rows per sheet which
    silently dropped sheets whose meaningful data starts further down (e.g.
    NAV_CALC's monthly history block at row 31). We now stream the entire
    sheet; the per-row payload is small (string cells) so memory stays bounded.

    Returns {sheet_name: [[row1_values], [row2_values], ...]}
    """
    # Build cross-sheet value cache first (resolves =SheetX!CellRef formulas)
    xsheet_cache = _build_cross_sheet_value_cache(filepath)

    wb = openpyxl.load_workbook(filepath, data_only=True)
    previews = {}
    sheet_names = wb.sheetnames

    for sheet_name in sheet_names:
        ws = wb[sheet_name]
        rows = []
        # NO row cap — iterate every populated row.
        for i, row in enumerate(ws.iter_rows()):
            row_vals = []
            for cell in row:
                # Prefer cached cross-sheet resolved value; fall back to cell value
                val = xsheet_cache.get((sheet_name, cell.row, cell.column), cell.value)
                row_vals.append(str(val) if val is not None else '')
            rows.append(row_vals)
            if i >= 5:
                break
        if rows:
            previews[sheet_name] = rows

    wb.close()
    return sheet_names, previews


# ---------------------------------------------------------------------------
# Pass 1: Sheet Classification
# ---------------------------------------------------------------------------

SHARED_MISSION_PREAMBLE = """================================================================================
MISSION (READ FIRST — applies to every prompt in this pipeline)
================================================================================
Basically we have the terms and fields on the frontend dashboard which we need
to either EXTRACT from the Excel data sheet using semantic analysis (as we are
currently doing), or we need to CALCULATE them from other values that ARE
present in the Excel.

Every value displayed on the dashboard MUST be one of:
  (a) DIRECTLY EXTRACTED from a labelled cell in the Excel (Pass 1 / 1.5 / 2 / 3
      identify the value and stage it for the database), OR
  (b) DERIVED by a formula you choose at Pass 4 from extracted inputs.

CRUCIALITY: A missing value on the dashboard is a FAILURE. Indian PE / VC / LP
clients consume this data to make investment, regulatory, and audit decisions.
"Field shows —" because you skipped a row, mis-classified a section, or
truncated input is unacceptable. Take whatever time and tokens you need to be
thorough. There is NO row scan limit, NO time limit, NO column limit, and NO
sample-count limit on you. Scan the whole sheet. Read every cell. Consider
every section. If a label and a value can be matched semantically, match them.
If a value can be computed from available inputs, compute it.

Do NOT match by keyword. Match by MEANING. Section names, column headers, row
labels, sub-table order, sheet layout, and currency notation all vary file to
file. Semantic understanding is your job; the user has explicitly forbidden any
keyword-driven shortcut.
================================================================================
"""


PASS1_PROMPT = SHARED_MISSION_PREAMBLE + """You are an AI engineer with 20+ years of experience in automating the finances of companies, specializing in Alternative Investment Funds (AIFs), Private Equity, and Venture Capital fund operations. You hold 25+ years of experience as a CA/CFO with deep knowledge of fund accounting, LP/GP economics, capital calls, distributions, carried interest, NAV calculation, and SEBI regulatory compliance for Indian AIFs.

You MUST use this financial domain expertise to correctly classify each sheet. The difference between an LP (investor) and a portfolio company (investee) is fundamental — confusing them would be like confusing a bank's depositors with its loan customers.

Given the sheet names and first few rows of an AIF Excel workbook, classify each sheet into its PRIMARY data domain.

Available domains and their descriptions:
{domains}

For each sheet, examine:
1. The sheet name itself
2. The header row(s) — look for section headers like "FUND MASTER DATA", "INVESTORS", "CAPITAL CALLS", etc.
3. The data content in sample rows
4. The NATURE of entities described (are they investors/LPs or portfolio companies/investees?)

IMPORTANT: Some sheets contain multiple sections separated by section headers (all-caps text like "FUND MASTER DATA", "SCHEMES", "PORTFOLIO COMPANIES"). Identify these multi-section sheets.

CROSS-SHEET LINKING — CRITICAL UNDERSTANDING:
Excel workbooks used by fund managers frequently contain cross-sheet cell references. A cell in one sheet may reference data from another sheet using formulas like:
  - =Sheet2!B5  (simple reference)
  - ='Portfolio Companies'!C10  (sheet name with spaces)
  - =VLOOKUP(A2,'Fund Data'!A:D,2,0)  (lookup from another sheet)

When you see cells showing empty values or '#REF!' or formula text, the ACTUAL value may exist in another sheet. The system has already resolved cross-sheet references before sending you this preview, so values shown reflect the true data. If you encounter empty cells in what appears to be a data area, assume those cells may be linked and classified accordingly.

CRITICAL RULES — NEVER VIOLATE:

1. ONE PRIMARY DOMAIN PER SHEET.
   Each sheet must be classified with EXACTLY ONE primary domain. Do NOT assign
   multiple domains just because a sheet contains a column with a keyword that
   APPEARS related to another domain.

   A column name is an ATTRIBUTE of the entities on that sheet — it does NOT
   change the sheet's domain. For example:
   - An Investors sheet with a "Distributions" column → still investors_aml
     (Distributions here = money RETURNED TO the LP)
   - A Portfolio sheet with a "Sector" column → still portfolio_investments
     (Sector here = the investee company's industry)
   - A Capital Calls sheet with an "Investor Name" column → still capital_calls
     (Investor Name here = which LP is being called)

   Only assign a second domain if the sheet genuinely contains TWO SEPARATE
   data tables (e.g., "Organization & Users" has both org master data AND a
   separate user list table below it).

2. FUNDAMENTAL DISTINCTION: LPs (INVESTORS) vs PORTFOLIO COMPANIES (INVESTEES).
   This is the most critical distinction in fund management:

   LPs / INVESTORS (→ investors_aml domain):
   - These are entities who GIVE money TO the fund
   - Names are typically: sovereign wealth funds (Temasek, GIC, Mubadala, ADIA),
     pension funds (CPPIB, OTPP, CalPERS), DFIs (IFC, CDC, NABARD, SIDBI, EDB),
     insurance companies, family offices, corporates, HNIs
   - Columns: Commitment, Drawdown, Drawdown%, Distributions, Carry Provision,
     Demat, PAN, KYC Status, Bank Details, Investor Type
   - A "Distributions" column on this sheet = money PAID BACK to the LP
   - This sheet is ALWAYS investors_aml, NEVER exits_distributions

   PORTFOLIO COMPANIES / INVESTEES (→ portfolio_investments or exits_distributions):
   - These are companies the fund INVESTS money INTO
   - Names are typically: private companies (e.g., "XYZ Pvt Ltd", "ABC Inc")
   - For exits: columns include Exit Date, Exit Type/Route (IPO, M&A, Secondary,
     Buyback), Cost, Proceeds, MOIC, IRR
   - For active portfolio: columns include Investment Date, Cost, Fair Value,
     Ownership %, Sector, Stage

   NEVER classify an LP/Investor sheet as exits_distributions, even if it has
   a "Distributions" column. The word "distribution" has DIFFERENT meanings:
   - On an Investors sheet: distribution = money returned to LP (an LP attribute)
   - On an Exits sheet: distribution = fund-level payout schedule after exits

3. COVER/SUMMARY SHEETS ARE NEVER DATA SHEETS.
   Sheets named "Cover", "Summary", "Index", "Contents", "Dashboard", "Overview",
   "Front Page", "Title", "Home", "Intro", "README" etc. are display pages.
   They contain KEY-VALUE metadata pairs (e.g., "Fund Name: ABC Fund",
   "Portfolio Companies: 110", "Total FV: ₹1,234 Cr") that are COMPUTED
   AGGREGATES — not raw transactional records.

   These sheets MUST ONLY be classified as "fund_scheme_master" (for basic
   fund identity) or "unknown". NEVER classify them as:
   - portfolio_investments (even if they show a company count)
   - investors_aml (even if they show an LP count)
   - capital_calls, nav_accounting, compliance, or any other data domain

   The numbers on cover sheets are often inaccurate, out of date, or
   filled in by hand. The source of truth for all counts and values is
   ALWAYS the dedicated data sheets.

4. DERIVE COUNTS FROM DATA SHEETS, NOT COVER SHEETS.
   If a Cover sheet says "Portfolio Companies: 13" but the "Portfolio
   Investments" sheet has 110 rows — the correct count is 110.
   Always trust the data sheet row count over any aggregate shown on
   a cover or summary page.

5. A sheet that has a two-column key-value layout (col A = label, col B = value)
   where labels are things like "Fund Name", "Short Code", "Vintage Year",
   "Management Fee", "Hurdle Rate", "Carried Interest", "Domicile" etc.
   is a METADATA sheet, not a data/transaction sheet.

6. FINANCIAL STATEMENT SHEETS (P&L, Budget vs Actual, Balance Sheet):
   Sheets with names like "Monthly P&L", "P&L", "Profit Loss", "Income Statement",
   "Budget vs Actual", "BvA", "Financial Statements", "Company Financials",
   "Balance Sheet", "Cash Flow" belong to the "financials_pl_bva" domain.
   These sheets contain company-level financial data (Revenue, EBITDA, PAT etc.)
   for portfolio companies — either one row per company or time-series pivot format.

7. TEMPORARY / TREASURY INVESTMENTS ARE NOT PORTFOLIO COMPANIES.
   Sections titled "Temporary Investments", "Treasury Investments", "Cash
   Instruments", "Liquid Fund Holdings" contain liquid mutual funds, overnight
   funds, money market instruments, etc. These are cash management tools, NOT
   portfolio company investments. They belong to nav_accounting (as cash
   equivalents) or fund_scheme_master, NEVER to portfolio_investments.

8. EXITS SHEET VALIDATION:
   A sheet classified as exits_distributions MUST have columns indicating actual
   exit events: Exit Date, Exit Type/Route/Method, Proceeds/Realization, MOIC.
   If a sheet has investor names with commitment/drawdown/distribution columns
   but NO exit-specific columns (Exit Date, Exit Type, Proceeds, MOIC), it is
   investors_aml — NOT exits_distributions.

9. GRANULAR DOMAIN CLASSIFICATION:
   Use the MOST SPECIFIC domain available. Do NOT lump everything into broad domains:

   - "FEES_REGISTER", "Fee Schedule", "Management Fees" → fees_register
     (NOT nav_accounting — fees_register is the dedicated domain for fee data)
   - "Quoted & Unquoted Shares", "IPEV Levels", "Share Classification",
     "Listed vs Unlisted" → quoted_unquoted
     (NOT valuations_kpis — quoted_unquoted is the dedicated domain)
   - "SaaS Metrics & Burn", "Burn Rate", "Cash & Runway", "Portfolio Financials",
     "Operating Metrics" → burn_runway
     (NOT valuations_kpis — burn_runway is the dedicated domain for burn/SaaS data)
   - "FUND_PL", "FUND_BS", "Fund P&L", "Fund Balance Sheet" → fund_pl_bs
     (These are fund-entity-level statements, NOT company-level financials_pl_bva)
   - "LP Capital Accounts", "Capital Account Statements" → lp_capital_accounts
     (NOT investors_aml — lp_capital_accounts is the dedicated domain)
   - "NAV Calculation", "NAV Calc", "NAV Computation", "NAV Build Up",
     "NAV Working", "Closing NAV" → nav_calculation
     (This is the single-period computational worksheet that shows how the NAV
     figure is derived — Opening NAV, adjustments, fees, Closing NAV, NAV/Unit.
     It is a KEY-VALUE or line-item format, NOT a time-series table.
     DIFFERENT from nav_accounting which stores period-wise NAV time-series.
     If a sheet has "NAV" in its name AND contains labels like "Closing NAV/Unit",
     "Opening NAV", "Units Outstanding", "Fair Value Adjustment", "Management Fee"
     in column A with single values in column B — it is nav_calculation.)
   - "Waterfall", "Carry", "Carried Interest", "Carried Interest Waterfall",
     "Distribution Waterfall", "GP Economics", "Performance Fee",
     "GP/LP Split", "Carry Calculation" → waterfall_carry
     (This sheet shows the GP/LP economics: preferred return / hurdle amount,
     catch-up, carried interest provision, GP carry amount, LP share.
     It typically has key-value label-pairs like "Total Capital Called",
     "Preferred Return", "Carried Interest Provision", "GP Share", "LP Share".
     DIFFERENT from exits_distributions which tracks individual company exits.
     DIFFERENT from nav_accounting which tracks periodic NAV values.)

10. MULTIPLE SHEETS CAN SHARE THE SAME DOMAIN.
    If the workbook has 4 financial statement sheets (P&L, BS, CF, BvA), classify
    ALL of them as financials_pl_bva. If there are 2 NAV sheets, classify BOTH as
    nav_accounting. Do NOT force different domains just because sheets are separate.

11. NAV CALCULATION vs NAV ACCOUNTING — CRITICAL DISTINCTION.
    These are two DIFFERENT sheet types that both relate to NAV:

    nav_accounting (TIME-SERIES):
    - Contains MULTIPLE NAV records across periods (one row per month/quarter)
    - Columns: Period, NAV Date, Total NAV, Units, NAV/Unit
    - Used for tracking NAV history over time
    - Example sheet names: "NAV & Accounting", "NAV Records", "Monthly NAV"

    nav_calculation (SINGLE-PERIOD COMPUTATION):
    - Contains the NAV BUILD-UP for ONE period — how the NAV was calculated
    - Key-value format: label in col A, value in col B
    - Labels include: Opening NAV, Investments at Cost, Fair Value Adjustment,
      Unrealised Gains, Management Fees, Operating Expenses, Closing NAV,
      Total Units Outstanding, Closing NAV per Unit, Opening NAV per Unit
    - Example sheet names: "NAV Calculation", "NAV Calc", "NAV Computation"

    If unsure: if the sheet has MANY rows of period-NAV data → nav_accounting.
    If the sheet has a computation breakdown → nav_calculation.

12. WATERFALL / CARRY vs OTHER DOMAINS — AVOID CONFUSION.
    waterfall_carry sheets contain GP/LP economic splits and carry calculations.
    They are NOT:
    - exits_distributions (which tracks individual company exit events with
      Exit Date, Exit Type, Proceeds, MOIC columns)
    - nav_accounting (which tracks periodic NAV time-series)
    - investors_aml (which lists LP investor master records)

    A waterfall sheet typically has labels like: "Total Capital Called",
    "Preferred Return Amount", "Carry Provision", "Carried Interest",
    "GP Share", "LP Share", "Clawback". These are FUND-LEVEL economics,
    not individual company exits or LP records.

Sheet data:
{sheet_data}

Respond with a JSON object:
{{
  "sheets": [
    {{
      "sheet_name": "exact sheet name",
      "domains": ["primary_domain_only"],
      "sections": ["SECTION HEADER 1", "SECTION HEADER 2"],
      "confidence": 0.95
    }}
  ]
}}

Only use domain names from this list: {domain_list}
If a sheet doesn't match any domain, use "unknown".
"""


def classify_sheets(filepath, progress_cb=None):
    """
    Pass 1: Send sheet previews to Gemini and get domain classification.

    Returns: list of {sheet_name, domains, sections, confidence}
    """
    if progress_cb:
        progress_cb(5, 'Reading workbook structure...')

    sheet_names, previews = _extract_sheet_previews(filepath)

    if progress_cb:
        progress_cb(8, 'Classifying sheets with AI...')

    # Build the prompt
    domains_desc = '\n'.join(f'  - {k}: {v}' for k, v in SHEET_DOMAINS.items())
    domain_list = ', '.join(SHEET_DOMAINS.keys())

    sheet_data_parts = []
    for name, rows in previews.items():
        sheet_data_parts.append(f'\n--- Sheet: "{name}" ---')
        for i, row in enumerate(rows):
            # Filter out empty values for cleaner output
            non_empty = [v for v in row if v]
            if non_empty:
                sheet_data_parts.append(f'  Row {i+1}: {non_empty}')

    prompt = PASS1_PROMPT.format(
        domains=domains_desc,
        domain_list=domain_list,
        sheet_data='\n'.join(sheet_data_parts),
    )

    result = _call_gemini(prompt, context_label='Pass1-classify')

    classifications = result.get('sheets', [])
    logger.info(
        f'Gemini Pass 1: classified {len(classifications)} sheets '
        f'from {len(sheet_names)} total'
    )
    for cls in classifications:
        logger.info(
            f'  Sheet "{cls.get("sheet_name")}" → '
            f'{cls.get("domains")} (conf={cls.get("confidence", 0):.2f})'
        )

    if progress_cb:
        progress_cb(12, 'Sheet classification complete')

    return classifications, sheet_names


# ---------------------------------------------------------------------------
# Pass 1.5: Section Classification within Multi-Section Sheets
# ---------------------------------------------------------------------------

PASS1_5_PROMPT = SHARED_MISSION_PREAMBLE + """You are an AI engineer with 20+ years of experience in Alternative Investment Funds (AIFs), Private Equity, and Venture Capital fund operations across multiple countries. You hold deep expertise in fund accounting, LP/GP economics, capital calls, distributions, carried interest, NAV calculation, and regulatory compliance (SEBI for India, SEC for US, FCA for UK, MAS for Singapore, CSSF for Luxembourg).

You are classifying SECTIONS found within multi-section Excel sheets from a fund data workbook. Each sheet has already been classified to a primary data domain. Now you must classify each section within those sheets to a specific sub-domain.

CRITICAL CONTEXT: Fund Excel files from different managers, countries, and formats use WILDLY DIFFERENT names for the same data concept. Your job is to understand the SEMANTIC MEANING regardless of the exact text. Examples:

PORTFOLIO / COMPANY sections:
  "PORTFOLIO COMPANIES", "INVESTEE COMPANIES", "COMPANIES", "COMPANY MASTER",
  "FUND HOLDINGS", "COMPANY REGISTER", "INVESTEE DETAILS" → portfolio_companies

INVESTMENT sections:
  "INVESTMENTS", "INVESTMENT DETAILS", "INVESTMENT REGISTER", "DEPLOYED CAPITAL",
  "PORTFOLIO INVESTMENTS", "FUND DEPLOYMENT", "INVESTMENT BOOK" → investments

TRANCHE sections:
  "INVESTMENT TRANCHES", "TRANCHES", "FUNDING ROUNDS", "DRAWDOWN TRANCHES",
  "ROUND DETAILS", "TRANCHE REGISTER", "DEAL HISTORY" → investment_tranches

TEMPORARY / TREASURY sections (MUST be identified — these get SKIPPED):
  "TEMPORARY INVESTMENTS", "TREASURY INVESTMENTS", "LIQUID INVESTMENTS",
  "CASH INSTRUMENTS", "MONEY MARKET", "OVERNIGHT FUNDS", "LIQUID FUND HOLDINGS",
  "SHORT TERM INVESTMENTS" → temporary_investments

CAPITAL CALL sections:
  "CAPITAL CALLS", "DRAWDOWNS", "CALL SCHEDULE", "CAPITAL DRAWDOWNS" → capital_call_headers
  "CAPITAL CALL LINE ITEMS", "LP DRAWDOWNS", "INVESTOR DRAWDOWNS" → capital_call_line_items

EXIT sections:
  "EXIT EVENTS", "EXITS", "REALIZATIONS", "DIVESTMENTS", "PORTFOLIO EXITS" → exit_events

DISTRIBUTION sections:
  "DISTRIBUTIONS", "DISTRIBUTION SCHEDULE", "LP DISTRIBUTIONS", "PAYOUTS" → distributions

NAV sections — distinguish by row STRUCTURE, not by section name alone:
  "NAV RECORDS", "NAV HISTORY", "MONTHLY NAV", "QUARTERLY NAV" → nav_records
    (sample rows MUST start with a date column — these are time-series)
  "FUND NAV (CURRENT PERIOD)", "NAV BREAKDOWN", "NAV COMPONENTS",
    "NAV CALCULATION (CURRENT)", "NAV BUILD-UP" → nav_breakdown
    (sample rows are key-value: "Total Fair Value of Portfolio | 1165",
     "Cash & Equivalents | 285", etc. — NO date column. Final row sums to Total NAV.)
  "NAV PER UNIT", "UNIT NAV", "NAV/UNIT" → nav_per_unit
    (sample rows are key-value: "Total Fund NAV | 1862", "Total Units Issued | 152000",
     "NAV Per Unit | 1.22", etc.)

FUND PERFORMANCE sections (NEW):
  "FUND PERFORMANCE", "FUND-LEVEL MULTIPLES", "MOIC TVPI DPI",
    "PERFORMANCE METRICS", "FUND KPIS" → fund_performance_breakdown
    (key-value rows: "Total Invested Capital | 1520", "Gross MOIC | 0.88",
     "Net IRR | 0.1612", etc.)

WATERFALL sections (NEW):
  "WATERFALL", "CARRY COMPUTATION", "EUROPEAN WATERFALL",
    "AMERICAN WATERFALL" → waterfall_breakdown
    (key-value rows describing waterfall parameters)

SCHEME sections:
  "SCHEMES", "SCHEME DETAILS", "FUND SCHEMES", "SUB-FUND DETAILS" → schemes

FUND MASTER sections:
  "FUND MASTER DATA", "FUND DETAILS", "FUND INFORMATION" → fund_master

ENTITY sections:
  "KEY ENTITIES", "ENTITIES", "SERVICE PROVIDERS", "FUND ENTITIES" → entities

VALUATION sections:
  "VALUATIONS", "PORTFOLIO VALUATIONS", "FAIR VALUE ASSESSMENT" → valuations

Available sub-domains and their descriptions:
{subdomains}

HOW TO CLASSIFY — USE SECTION NAME, COLUMN HEADERS, AND SAMPLE DATA ROWS:

1. **First, inspect the SAMPLE DATA ROWS.** Row structure is the most reliable
   signal because section names and column headers can be ambiguous, but the
   shape of the data is unambiguous. Specifically:

   a. If the FIRST data column carries a DATE in every sample row
      (e.g. "2024-01-31", "Jan-24", "31/03/2025") AND subsequent columns
      carry numeric amounts → this is a TIME-SERIES section. Pick
      `nav_records`, `capital_call_headers`, `distributions`, etc.
      according to what the amounts represent.

   b. If the FIRST column carries a LABEL (e.g. "Total Fair Value of
      Portfolio", "Cash & Equivalents", "Mgmt Fee Payable") and the
      SECOND column carries a single amount → this is a KEY-VALUE
      BREAKDOWN section. Pick `nav_breakdown`, `nav_per_unit`,
      `fund_performance_breakdown`, `waterfall_breakdown`, or
      `fund_master` according to what the labels describe.

   c. If rows carry COMPANY NAMES + sector/stage/city → `portfolio_companies`
      or `investments` (the latter if financial columns are also present).

   d. If rows carry LP NAMES + commitment/called amounts → `entities` (LP
      register) or `capital_call_line_items`.

2. Use the column headers to confirm what the values represent.

3. Use the section name as a tiebreaker only — it is the weakest signal.

4. If sample rows are not provided (empty section), fall back to section
   name + column headers + parent domain.

CRITICAL: a NAV-related section is NOT automatically `nav_records`. It is
`nav_records` ONLY when the rows are time-indexed (column 1 is a date).
Otherwise it is `nav_breakdown` (component decomposition) or `nav_per_unit`
(per-unit value table). Mis-classifying a key-value table as `nav_records`
causes the importer to drop the data silently.

CRITICAL RULES:
1. "__default__" means the sheet has NO section headers (entire sheet is one flat table).
   Classify based on columns + parent domain:
   - parent=portfolio_investments + columns have Cost/FV → investments
   - parent=capital_calls → capital_call_headers
   - parent=nav_accounting → nav_records (only if rows are time-indexed)
   - parent=nav_calculation + rows time-indexed → nav_records
   - parent=nav_calculation + rows key-value → nav_breakdown
   - parent=exits_distributions → exit_events
   - parent=organization_users + columns have Entity Type → entities
   - parent=fund_scheme_master → fund_master

2. TEMPORARY INVESTMENTS are critical to detect — if missed, liquid mutual funds
   get imported as portfolio companies (phantom records). Always check for keywords
   like "temporary", "treasury", "liquid", "overnight", "money market".

3. A section that appears to be a COMBINED company+investment table (has BOTH
   company identity columns AND investment financial columns) → classify as "investments"

4. If truly unrecognizable, classify as "unknown" — never guess

Section data:
{section_data}

Respond with a JSON object:
{{
  "classifications": [
    {{
      "sheet_name": "exact sheet name",
      "sections": [
        {{
          "section_name": "EXACT SECTION HEADER TEXT",
          "sub_domain": "one of the sub-domain keys",
          "confidence": 0.95
        }}
      ]
    }}
  ]
}}

Only use sub-domain names from this list: {subdomain_list}
"""


def classify_sections(classifications, sheet_section_data, progress_cb=None):
    """
    Pass 1.5: Classify all section headers in a single batched Gemini call.

    Args:
        classifications: Pass 1 results (list of sheet classification dicts)
        sheet_section_data: dict mapping sheet_name to list of dicts:
            [{name: str, columns: list[str]}, ...]
            where 'name' is the section title text and 'columns' are the
            column headers found in that section.
        progress_cb: Optional progress callback

    Returns:
        {sheet_name: {section_name: sub_domain}}
    """
    from .canonical_schema import SECTION_SUBDOMAINS

    if progress_cb:
        progress_cb(13, 'Classifying sections with AI...')

    # Build sheet → primary domain lookup from Pass 1
    sheet_domain_lookup = {}
    for cls in classifications:
        sname = cls.get('sheet_name', '')
        domains = cls.get('domains', [])
        if domains and domains[0] != 'unknown':
            sheet_domain_lookup[sname] = domains[0]

    # Filter to sheets with sections to classify
    sections_to_classify = {
        sname: secs for sname, secs in sheet_section_data.items()
        if secs and sname in sheet_domain_lookup
    }

    if not sections_to_classify:
        logger.info('Gemini Pass 1.5: no multi-section sheets to classify')
        return {}

    # Build prompt input
    subdomains_desc = '\n'.join(
        f'  - {k}: {v}' for k, v in SECTION_SUBDOMAINS.items()
    )
    subdomain_list = ', '.join(SECTION_SUBDOMAINS.keys())

    section_data_parts = []
    for sname, secs in sections_to_classify.items():
        parent_domain = sheet_domain_lookup.get(sname, 'unknown')
        section_data_parts.append(
            f'\n--- Sheet: "{sname}" (parent domain: {parent_domain}) ---'
        )
        for sec in secs:
            cols = sec.get('columns', []) or []
            cols_str = ', '.join(cols) if cols else '(no columns detected)'
            section_data_parts.append(
                f'  Section: "{sec["name"]}"\n    Columns ({len(cols)}): {cols_str}'
            )
            # Include sample data rows so Gemini can SEE the structure:
            # rows starting with a date → time-series; rows with labels in
            # column 1 + amounts in column 2 → key-value breakdown; etc.
            samples = sec.get('sample_rows', []) or []
            if samples:
                section_data_parts.append(
                    f'    Sample data rows ({len(samples)}):'
                )
                for i, row in enumerate(samples, 1):
                    # Trim each cell to ~60 chars to keep prompt readable
                    rendered = [
                        (str(c)[:60] + ('…' if len(str(c)) > 60 else ''))
                        for c in row
                    ]
                    section_data_parts.append(f'      row {i}: {rendered}')
            else:
                section_data_parts.append('    Sample data rows: (none)')

    prompt = PASS1_5_PROMPT.format(
        subdomains=subdomains_desc,
        subdomain_list=subdomain_list,
        section_data='\n'.join(section_data_parts),
    )

    result = _call_gemini(prompt, context_label='Pass1.5-sections')

    # Parse result into {sheet_name: {section_name: sub_domain}}
    section_map = {}
    for sheet_cls in result.get('classifications', []):
        sname = sheet_cls.get('sheet_name', '')
        sheet_secs = {}
        for sec in sheet_cls.get('sections', []):
            sec_name = sec.get('section_name', '')
            sub_domain = sec.get('sub_domain', 'unknown')
            confidence = sec.get('confidence', 0.0)
            if sec_name and sub_domain in SECTION_SUBDOMAINS:
                sheet_secs[sec_name] = sub_domain
            else:
                sheet_secs[sec_name] = 'unknown'
            logger.info(
                f'  Section "{sec_name}" in "{sname}" → '
                f'{sub_domain} (conf={confidence:.2f})'
            )
        if sheet_secs:
            section_map[sname] = sheet_secs

    if progress_cb:
        progress_cb(14, 'Section classification complete')

    total_sections = sum(len(v) for v in section_map.values())
    logger.info(
        f'Gemini Pass 1.5: classified {total_sections} sections '
        f'across {len(section_map)} sheets'
    )

    return section_map


# ---------------------------------------------------------------------------
# Pass 2: Column Mapping per Sheet
# ---------------------------------------------------------------------------

PASS2_PROMPT = SHARED_MISSION_PREAMBLE + """You are an AI engineer with 20+ years of experience in automating the finances of companies. You hold 20+ years of experience working with Python, and specialization in extraction, displaying and calculating data and accessing it from Excel/CSV/PDF sheets of multiple formats. You hold 15+ years of hands-on experience in software debugging and creating production-ready softwares and dashboards. You have robust knowledge of a CFO/CA to perform calculations on finance data.

You are mapping Excel columns to canonical fund management database fields.

This sheet belongs to the domain: {domain}
Domain description: {domain_desc}

The sheet has these sections (identified by all-caps headers in the data):
{sections}

Excel data (first rows including headers):
{sheet_data}

Canonical fields for this domain (field_name: description):
{canonical_fields}

CROSS-SHEET LINKING — IMPORTANT:
This Excel workbook may use cross-sheet cell references. The system has already resolved
cross-sheet formula references (e.g. ='Portfolio'!B10, =Sheet2!C5) so you see the actual
resolved values in the preview above. However:
- Some columns that appear blank may still contain formula-linked data in data rows
- A column header like "Revenue" may pull data from a linked worksheet
- Time-series columns (Apr-24, May-24, Q1 FY25) often reference formula-computed values from other sheets
- When you see a column with only one or two sample values and the rest blank, assume the remaining
  rows contain formula-linked data — still map those columns to canonical fields

FINANCIAL STATEMENT LAYOUT VARIANTS — CRITICAL FOR financials_pl_bva DOMAIN:
Financial P&L sheets can appear in two layouts:
1. HORIZONTAL (rows = companies, columns = P&L line items):
   | Company | Period | Revenue | COGS | EBITDA | PAT |
   | CompA   | Apr-24 | 100     | 50   | 30     | 20  |

2. VERTICAL / PIVOT (rows = line items, columns = time periods):
   | Particulars  | Apr-24 | May-24 | Jun-24 |
   | Revenue      | 100    | 120    | 130    |
   | COGS         | 50     | 60     | 65     |
   | EBITDA       | 30     | 40     | 45     |
   In this layout: map the label column to "line_item" and each period column to "period"

3. BUDGET vs ACTUAL (rows = companies × line items, columns = Budget | Actual):
   | Company | Line Item | Budget | Actual | Variance |
   | CompA   | Revenue   | 100    | 95     | -5       |

Identify which layout applies and map accordingly.

GLOBAL SEMANTIC EQUIVALENCE — CRITICAL:
Fund managers worldwide use wildly different column names and currency notations for
the SAME underlying data field. You MUST recognize all of them as semantically identical:

CURRENCY UNIT VARIATIONS (all mean the same underlying amount):
  "Cost(Cr)"  =  "Cost(Lakhs)"  =  "Cost in Crore"  =  "Cost (₹Cr)"  =  "Cost (INR Mn)"
  =  "Cost(₹)"  =  "Cost (000s)"  =  "Cost [Cr]"  =  "Investment Cost (Crore)"
  — The unit suffix NEVER changes the semantic meaning of the column; strip it and map to cost_basis.

  "Revenue(₹Cr)"  =  "Revenue (Lakhs)"  =  "Revenue in Crore"  =  "Net Sales (Cr)"
  =  "Operating Revenue (₹)"  =  "Revenue [INR Mn]"  =  "Top Line (Cr)"  → revenue

INVESTMENT COST / BASIS:
  "Cost(Cr)"  "Cost(₹Cr)"  "Cost in Crore"  "Cost(Lakhs)"  "Invested(Cr)"  "Total Invested"
  "Investment Amount"  "Amount Invested"  "Capital Deployed"  "Amount(Cr)"  "Inv. Amount"
  → cost_basis / total_invested

FAIR VALUE / CURRENT VALUE:
  "FV(Cr)"  "FV(₹Cr)"  "FV Holding"  "Fair Value (Cr)"  "Current Value"  "Market Value(Cr)"
  "NAV(Cr)"  "Equity Val"  "Holding Value"  "Portfolio Value"  → fair_value

MOIC / MULTIPLE:
  "MOIC"  "MoIC"  "Multiple"  "Money Multiple"  "Return Multiple"  "Investment Multiple"
  "Return on Investment"  "2.5x"  → moic

IRR VARIANTS:
  "Gross IRR"  "IRR%"  "IRR (Gross)"  "Gross Return %"  "IRR%p.a."  "XIRR"  → irr_pct (gross)
  "Net IRR"  "IRR (Net)"  "Net Return"  "LP IRR"  → net_irr_pct

PERIOD / DATE NOTATION:
  "Apr-24"  "Apr-2024"  "April 2024"  "04/2024"  "2024-04"  → monthly period (Apr 2024)
  "Q1 FY25"  "Q1FY2025"  "Q1-FY25"  "1QFY25"  → quarterly period (Apr-Jun FY25)
  "FY2025"  "FY25"  "2024-25"  → annual period (FY2025)

HOLDING % / OWNERSHIP:
  "Hold%"  "Holding %"  "Ownership %"  "% Stake"  "FD%"  "Equity Stake"
  "% Shareholding"  "Investment %"  → ownership_pct

BUDGET / PLAN:
  "Budget"  "Budget YTD"  "AOP"  "Annual Operating Plan"  "Plan"  "Target"
  "Budgeted"  "Forecast"  "Budget Amount"  → budget

ACTUAL / ACHIEVED:
  "Actual"  "Actual YTD"  "YTD Actual"  "Actuals"  "Achieved"  "Reported"
  "Actual Amount"  → actual

LINE ITEM (row label in pivot layouts):
  "Particulars"  "Line Item"  "Description"  "Account"  "P&L Item"  "Category"  → line_item

For EACH section in the sheet, map the Excel column headers to canonical field names.
Consider semantic meaning, not just exact text match. For example:
  - "LP Name" or "Investor" → investor_name
  - "Committed Amount" or "Commitment (Cr)" or "Commitment(₹Cr)" → commitment_amount
  - "SEBI Reg No" or "Registration Number" → sebi_registration_number
  - "Net Sales" or "Top Line" or "Operating Revenue" or "Revenue(₹Cr)" → revenue
  - "Profit After Tax" or "Net Profit" or "Bottom Line" or "PAT(₹Cr)" → pat
  - "Shareholders Funds" or "Total Equity" or "Net Worth (Cr)" → net_worth
  - "AOP" or "Plan" or "Target" or "Budget YTD" → budget
  - "YTD Actual" or "Actuals" or "Achieved" or "Actual YTD" → actual
  - "Realized(₹Cr)" or "Exit Proceeds(Cr)" or "Gross Proceeds" → proceeds
  - "D&A(₹Cr)" or "Depreciation & Amortisation" → depreciation
  - "Op Ex(₹Cr)" or "Total Opex" or "Operating Expenses" → total_opex
  - "Gross Profit(₹Cr)" or "GP" or "Contribution Margin" → gross_profit

Output JSON:
{{
  "sections": [
    {{
      "section_name": "SECTION HEADER or sheet_name if no sections",
      "header_row": 1,
      "data_start_row": 2,
      "layout": "horizontal OR vertical_pivot OR budget_vs_actual",
      "mappings": [
        {{
          "excel_column": "exact Excel header text",
          "column_index": 1,
          "canonical_field": "canonical_field_name",
          "confidence": 0.95,
          "is_period_column": false,
          "cross_sheet_linked": false
        }}
      ],
      "unmapped_columns": ["column that has no canonical match"],
      "missing_fields": ["canonical fields not found in Excel"]
    }}
  ],
  "overall_confidence": 0.90
}}

Rules:
- column_index is 1-based (first column = 1)
- Only map columns you are confident about (>0.6 confidence)
- Leave unmapped_columns for columns that don't match any canonical field
- List missing_fields for canonical fields that should exist but weren't found
- If a sheet has multiple sections (separated by all-caps headers), map each section separately
- Be thorough — map every column you can identify
- Set is_period_column=true for time-period columns like "Apr-24", "Q1 FY25", "2024-04"
- Set cross_sheet_linked=true for columns where values appear to be pulled from another sheet
"""


def map_columns_for_sheet(filepath, sheet_name, domains, sections,
                          progress_cb=None, xsheet_cache=None, wb=None,
                          sections_data=None):
    """
    Pass 2: For a classified sheet, map its columns to canonical fields.

    Uses the cross-sheet value cache so that formula-linked cells (e.g.
    ='Portfolio'!B10) are resolved to their actual values before sending
    to Gemini — preventing blank cells from confusing the AI column mapper.

    Args:
        xsheet_cache: Pre-built cross-sheet cache (optional; built if None)
        wb: Pre-opened workbook (optional; opened if None)
        sections_data: list of dicts from `_detect_sections_lightweight`,
            each with {name, columns, sample_rows}. When provided, Pass 2's
            prompt is built from these per-section snapshots (so EVERY
            sub-table's columns are visible to Gemini, not just the first
            sheet-wide 20-row window). This is the production path for
            sheets with multiple sub-tables (NAV_CALC, EXITS,
            MOIC_TVPI_DPI, etc.). When None, falls back to the legacy
            first-20-rows scan.

    Returns: dict with section-level column mappings
    """
    # Use pre-built cache or build one (for backwards compat / standalone calls)
    if xsheet_cache is None:
        xsheet_cache = _build_cross_sheet_value_cache(filepath)

    # Use pre-opened workbook or open one
    close_wb = False
    if wb is None:
        wb = openpyxl.load_workbook(filepath, data_only=True)
        close_wb = True

    ws = wb[sheet_name]

    # Use primary domain
    primary_domain = domains[0] if domains else 'unknown'
    if primary_domain == 'unknown' or primary_domain not in DOMAIN_FIELDS:
        if close_wb:
            wb.close()
        return {'sections': [], 'overall_confidence': 0.0}

    # Build canonical fields description (shared across all sub-table calls)
    fields = DOMAIN_FIELDS[primary_domain]
    fields_desc = '\n'.join(f'  - {k}: {v}' for k, v in fields.items())

    # PRODUCTION PATH: one Gemini call PER SUB-TABLE.
    # Previously every sub-table of a sheet was packed into one giant prompt,
    # which made Gemini's confidence collapse on sheets with many sub-tables
    # or many sample rows (PORTFOLIO_MASTER with 50 rows, LP_REGISTER, etc.)
    # — every alias would come back with low confidence and the importer
    # would silently filter them out via its 0.70 threshold, leaving 0
    # records written to the LP / CapitalCall / ExitEvent tables. The fix
    # is structural: each sub-table has its own column structure, so each
    # gets its own focused Gemini call with just that sub-table's header +
    # sample rows. No more confidence collapse from over-stuffed prompts.
    if sections_data:
        merged_sections = []
        total_conf = 0.0
        n_sub = 0
        for sec in sections_data:
            sec_name = sec.get('name', '__default__')
            cols = sec.get('columns', []) or []
            samples = sec.get('sample_rows', []) or []
            if not cols and not samples:
                continue

            # Build a focused single-sub-table preview
            sub_parts = [f'\n--- Section: "{sec_name}" ---']
            if cols:
                sub_parts.append(f'  Header columns ({len(cols)}): {cols}')
            else:
                sub_parts.append('  Header columns: (none detected)')
            if samples:
                sub_parts.append(f'  Sample data rows ({len(samples)}):')
                for i, row in enumerate(samples, 1):
                    rendered = [
                        (str(c) if c is not None else '') for c in row
                    ]
                    sub_parts.append(f'    row {i}: {rendered}')

            sub_prompt = PASS2_PROMPT.format(
                domain=primary_domain,
                domain_desc=SHEET_DOMAINS.get(primary_domain, ''),
                sections=sec_name,
                sheet_data='\n'.join(sub_parts),
                canonical_fields=fields_desc,
            )
            try:
                sub_result = _call_gemini(
                    sub_prompt,
                    context_label=f'Pass2-map({sheet_name}/{sec_name}:{primary_domain})',
                )
            except Exception as e:
                logger.warning(
                    f'Pass 2 per-section call failed for '
                    f'"{sheet_name}/{sec_name}": {e}'
                )
                continue

            # sub_result is shaped {sections: [{section_name, mappings, ...}], overall_confidence}
            # Merge each section block into our running list.
            for s in sub_result.get('sections', []):
                merged_sections.append(s)
                c = s.get('confidence') or s.get('overall_confidence') or 0.0
                if isinstance(c, (int, float)):
                    total_conf += float(c)
                    n_sub += 1
            sub_overall = sub_result.get('overall_confidence')
            if isinstance(sub_overall, (int, float)) and n_sub == 0:
                # Use the per-call overall confidence as a fallback
                total_conf += float(sub_overall)
                n_sub += 1

        if close_wb:
            wb.close()

        overall = (total_conf / n_sub) if n_sub else 0.0
        return {
            'sections': merged_sections,
            'overall_confidence': round(overall, 4),
        }

    # LEGACY FALLBACK PATH — no sections_data supplied. Stream the entire
    # sheet into one prompt. (No row cap — previously 20.)
    sheet_data_parts = []
    rows = []
    for row in ws.iter_rows():
        row_vals = []
        for cell in row:
            val = xsheet_cache.get((sheet_name, cell.row, cell.column), cell.value)
            row_vals.append(str(val) if val is not None else '')
        rows.append(row_vals)
    for i, row in enumerate(rows):
        non_empty = [v for v in row if v]
        if non_empty:
            sheet_data_parts.append(f'  Row {i+1}: {non_empty}')

    if close_wb:
        wb.close()

    if not sheet_data_parts:
        return {'sections': [], 'overall_confidence': 0.0}

    sections_str = ', '.join(sections) if sections else 'No explicit sections — treat entire sheet as one section'

    prompt = PASS2_PROMPT.format(
        domain=primary_domain,
        domain_desc=SHEET_DOMAINS.get(primary_domain, ''),
        sections=sections_str,
        sheet_data='\n'.join(sheet_data_parts),
        canonical_fields=fields_desc,
    )

    result = _call_gemini(
        prompt, context_label=f'Pass2-map({sheet_name}:{primary_domain})'
    )

    return result


# ---------------------------------------------------------------------------
# Main entry point: full two-pass mapping
# ---------------------------------------------------------------------------

def _detect_sections_lightweight(ws):
    """Detect section boundaries in a worksheet using layout-only heuristics.

    Returns a list of dicts:
        [{name: str, columns: [str, ...], sample_rows: [[cell, ...], ...]}]

    where:
      - 'name' is the section title text (or '__default__' for the first
        flat-table region with no section header)
      - 'columns' are the column headers found immediately after the title
      - 'sample_rows' are up to 5 data rows from the section, used by Pass 1.5
        to distinguish key-value layouts from time-series layouts. With actual
        row data, Gemini can see whether the first column carries dates
        (time-series), labels (key-value), or company names (entity list).

    Detection is 100% format-agnostic — no keyword matching. A section title
    row is identified by:
      - 1-2 non-empty cells in the row
      - First cell text is predominantly uppercase (≥70% of alpha chars)
      - Text length > 3 characters

    No column cap (was 15) — full headers ship to Gemini so the section's
    real shape is visible.
    """
    max_row = ws.max_row or 0
    max_col = ws.max_column or 0
    sections = []
    seen_section = False

    def _get_header_row_index(start_r):
        """Find the first row at/after start_r with ≥3 non-empty cells."""
        for scan_r in range(start_r, min(start_r + 8, max_row + 1)):
            count = sum(
                1 for c in range(1, max_col + 1)
                if ws.cell(scan_r, c).value is not None
            )
            if count >= 3:
                return scan_r
        return None

    def _read_row(r):
        return [
            ws.cell(r, c).value for c in range(1, max_col + 1)
        ]

    def _collect_columns_and_samples(header_r, end_r):
        """Return (columns, sample_rows). Columns come from header_r; sample
        rows are up to 5 data rows from header_r+1 to end_r (stops on blank
        rows or section boundaries)."""
        cols = [
            str(v).strip() for v in _read_row(header_r) if v is not None
        ]
        samples = []
        blanks_in_a_row = 0
        for r in range(header_r + 1, end_r + 1):
            row_vals = _read_row(r)
            nonnull = [v for v in row_vals if v is not None]
            if not nonnull:
                blanks_in_a_row += 1
                if blanks_in_a_row >= 3:
                    break
                continue
            blanks_in_a_row = 0
            # Stringify each cell so dates / Decimals serialise cleanly in
            # the prompt. None → '' to preserve column alignment.
            samples.append([
                ('' if v is None else
                 (v.isoformat() if hasattr(v, 'isoformat') else str(v)))
                for v in row_vals
            ])
            # NO sample-row cap. Every populated row in the section gets
            # sent to Gemini so it can reason over the complete structure
            # (a 36-row time-series shouldn't be truncated to 5).
        return cols, samples

    # First pass: locate every section-title row, recording (title_row, title_text)
    section_title_rows = []
    r = 1
    while r <= max_row:
        cell_vals = []
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v is not None:
                cell_vals.append(str(v).strip())

        if cell_vals:
            first_str = cell_vals[0]
            if len(cell_vals) <= 2 and len(first_str) > 3:
                alpha_chars = [ch for ch in first_str if ch.isalpha()]
                upper_ratio = (
                    sum(1 for ch in alpha_chars if ch.isupper()) / len(alpha_chars)
                    if alpha_chars else 0.0
                )
                if upper_ratio >= 0.70:
                    section_title_rows.append((r, first_str))
        r += 1

    # PRODUCTION FIX: always snapshot the pre-first-title area as a
    # __default__ sub-table. Previously this only ran when ZERO titles
    # were found, which meant sheets like PORTFOLIO_MASTER (main 50-company
    # table at rows 5-54 + small "SUMMARY STATISTICS" title block at row 56)
    # had their main data table SKIPPED — Pass 2 saw only the summary
    # block. Same regression on LP_REGISTER / CAPITAL_CALLS / EXITS, where
    # the main data table sits ABOVE a small validation/check sub-table.
    #
    # The pre-title area is the main data table whenever it contains a
    # header-shaped row (≥3 non-empty cells). We snapshot that header +
    # its sample rows independently of any title-block sub-tables that
    # follow.
    pre_title_end = (
        section_title_rows[0][0] - 1 if section_title_rows else max_row
    )
    if pre_title_end >= 1:
        for r in range(1, pre_title_end + 1):
            count = sum(
                1 for c in range(1, max_col + 1)
                if ws.cell(r, c).value is not None
            )
            if count >= 3:
                cols, samples = _collect_columns_and_samples(r, pre_title_end)
                if cols or samples:
                    sections.append({
                        'name': '__default__',
                        'columns': cols,
                        'sample_rows': samples,
                    })
                    seen_section = True
                break

    # Then add each title-block sub-table.
    if section_title_rows:
        for idx, (title_r, title_text) in enumerate(section_title_rows):
            end_r = (section_title_rows[idx + 1][0] - 1
                     if idx + 1 < len(section_title_rows) else max_row)
            header_r = _get_header_row_index(title_r + 1)
            if header_r is None:
                sections.append({
                    'name': title_text,
                    'columns': [],
                    'sample_rows': [],
                })
                continue
            cols, samples = _collect_columns_and_samples(header_r, end_r)
            sections.append({
                'name': title_text,
                'columns': cols,
                'sample_rows': samples,
            })
            seen_section = True

    return sections


def map_workbook_columns(filepath, progress_cb=None):
    """
    Full three-pass Gemini column mapping for a fund Excel file.

    Pass 1:   Sheet classification → domain map
    Pass 1.5: Section classification → sub-domain map (within multi-section sheets)
    Pass 2:   Column mapping → canonical field names

    Args:
        filepath: Path to the .xlsx file
        progress_cb: Optional callable(pct: int, message: str)

    Returns:
        {
            'sheet_classifications': [...],
            'column_mappings': {sheet_name: mapping_result},
            'section_map': {sheet_name: {section_name: sub_domain}},
            'overall_confidence': float,
            'sheet_names': [...]
        }
    """
    # Pass 1: Classify sheets
    classifications, sheet_names = classify_sheets(filepath, progress_cb)

    if not classifications:
        logger.warning(
            f'Gemini Pass 1 returned 0 classifications for {len(sheet_names)} sheets'
        )

    # Pass 1.5: Classify sections within multi-section sheets
    section_map = {}
    try:
        wb = openpyxl.load_workbook(filepath, data_only=True)
        sheet_section_data = {}
        for cls in classifications:
            sname = cls.get('sheet_name', '')
            domains = cls.get('domains', [])
            if not domains or domains == ['unknown']:
                continue
            if sname not in wb.sheetnames:
                continue
            ws = wb[sname]
            detected = _detect_sections_lightweight(ws)
            if detected:
                sheet_section_data[sname] = detected

        wb.close()

        if sheet_section_data:
            section_map = classify_sections(
                classifications, sheet_section_data, progress_cb
            )
            logger.info(f'Gemini Pass 1.5 section_map: {section_map}')
    except Exception as e:
        logger.warning(f'Gemini Pass 1.5 section classification failed: {e}')

    if progress_cb:
        progress_cb(15, 'Mapping columns with AI...')

    # Pass 2: Map columns for each classified sheet
    # Build cross-sheet cache and open workbook ONCE (not per-sheet)
    xsheet_cache = _build_cross_sheet_value_cache(filepath)
    wb_pass2 = openpyxl.load_workbook(filepath, data_only=True)

    column_mappings = {}
    total_confidence = 0.0
    mapped_count = 0

    # PRODUCTION: parallel Pass 2 using ThreadPoolExecutor.
    # Architecture (per the user-approved design):
    #   - Layer 1: each Pass2-map call already has _call_gemini's internal
    #     retry (3 attempts, exponential backoff) for in-call transient errors.
    #   - Layer 2: at the BATCH level, we run sheets in parallel with bounded
    #     concurrency. If a sheet fails after all Layer-1 retries, it's
    #     collected and re-issued in a NEW parallel batch after a jittered
    #     30s sleep. Up to 3 outer rounds. Sheets that fail all 3 outer
    #     rounds are reported as Pass 2 failures and the audit catches them.
    #
    # Concurrency = 6 in flight. Token consumption is identical to sequential
    # — same prompts, same responses — only wall-clock changes (faster).
    # On paid Tier 1 (1000 RPM) our peak load is <2% of quota, so 429s
    # should be rare; if they occur, Layer 2 handles them gracefully.
    import concurrent.futures as _futures
    import random as _random
    import time as _time

    MAX_WORKERS = 6
    OUTER_RETRIES = 3
    OUTER_BACKOFF_BASE = 30  # seconds

    # Build the list of sheets to map
    pending = []
    for i, sheet_cls in enumerate(classifications):
        sheet_name = sheet_cls.get('sheet_name', '')
        domains = sheet_cls.get('domains', [])
        sections = sheet_cls.get('sections', [])
        cls_confidence = sheet_cls.get('confidence', 0.0)
        if not domains or domains == ['unknown']:
            continue
        pending.append({
            'sheet_name': sheet_name,
            'domains': domains,
            'sections': sections,
            'cls_confidence': cls_confidence,
        })

    def _map_one(spec):
        sname = spec['sheet_name']
        try:
            mapping = map_columns_for_sheet(
                filepath, sname, spec['domains'], spec['sections'], progress_cb,
                xsheet_cache=xsheet_cache, wb=wb_pass2,
                sections_data=sheet_section_data.get(sname),
            )
            return ('ok', spec, mapping)
        except Exception as e:
            return ('error', spec, e)

    failed_specs = list(pending)
    for outer_attempt in range(1, OUTER_RETRIES + 1):
        if not failed_specs:
            break
        round_specs = failed_specs
        failed_specs = []
        n_total = len(round_specs)
        logger.info(
            f'Pass 2 parallel round {outer_attempt}/{OUTER_RETRIES}: '
            f'dispatching {n_total} sheet(s) with max_workers={MAX_WORKERS}'
        )
        if progress_cb:
            base_pct = 15 + (outer_attempt - 1) * 3
            progress_cb(base_pct,
                        f'Pass 2 parallel round {outer_attempt}: mapping '
                        f'{n_total} sheets...')

        completed_in_round = 0
        with _futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_map_one, s): s for s in round_specs}
            for fut in _futures.as_completed(futures):
                status, spec, payload = fut.result()
                sname = spec['sheet_name']
                completed_in_round += 1
                if status == 'ok':
                    mapping = payload
                    column_mappings[sname] = {
                        'domains': spec['domains'],
                        'sections_from_classification': spec['sections'],
                        **mapping,
                    }
                    overall_conf = mapping.get('overall_confidence',
                                               spec['cls_confidence'])
                    total_confidence += overall_conf
                    mapped_count += 1
                else:
                    err = payload
                    logger.warning(
                        f'Pass 2 sheet "{sname}" failed in round '
                        f'{outer_attempt}: {type(err).__name__}: {err}'
                    )
                    failed_specs.append(spec)

                if progress_cb and completed_in_round % 3 == 0:
                    progress_cb(
                        15 + (outer_attempt - 1) * 3,
                        f'Pass 2 round {outer_attempt}: '
                        f'{completed_in_round}/{n_total} sheets mapped'
                    )

        if failed_specs and outer_attempt < OUTER_RETRIES:
            jitter = _random.uniform(0, 10)
            sleep_s = OUTER_BACKOFF_BASE + jitter
            logger.info(
                f'Pass 2: {len(failed_specs)} sheet(s) failed in round '
                f'{outer_attempt}, sleeping {sleep_s:.1f}s before re-issuing '
                f'them in parallel'
            )
            _time.sleep(sleep_s)

    # Sheets that exhausted all outer retries
    for spec in failed_specs:
        sname = spec['sheet_name']
        column_mappings[sname] = {
            'domains': spec['domains'],
            'error': (f'Pass 2 failed after {OUTER_RETRIES} outer parallel '
                      f'rounds (sheet remained un-mappable across all '
                      f'Layer-1 + Layer-2 retries).'),
            'overall_confidence': 0.0,
        }
        logger.error(
            f'Pass 2 sheet "{sname}" exhausted {OUTER_RETRIES} outer retry '
            f'rounds — audit will flag any downstream empty tables.'
        )

    try:
        wb_pass2.close()
    except Exception:
        pass

    avg_confidence = total_confidence / mapped_count if mapped_count > 0 else 0.0

    if progress_cb:
        progress_cb(25, 'Column mapping complete')

    return {
        'sheet_classifications': classifications,
        'column_mappings': column_mappings,
        'section_map': section_map,
        'overall_confidence': round(avg_confidence, 2),
        'sheet_names': sheet_names,
    }


# ---------------------------------------------------------------------------
# Pass 4: Derive missing fund-level metrics
# ---------------------------------------------------------------------------

def derive_metric_via_gemini(metric_key, metric_meta, available_inputs,
                             scheme_context=''):
    """Ask Gemini for RANKED CANDIDATE FORMULAS to derive a missing fund-level
    metric. CRITICAL CONTRACT: Gemini's only job is to propose formulas and
    declare their required input variables. Gemini DOES NOT supply numeric
    values — Python evaluates each candidate using ONLY the catalogue
    values in `available_inputs`. This eliminates the hallucination
    vector where Gemini invents fake variables + fake values and Python
    trusts them blindly.

    The caller (MetricDerivationService._derive_one) will:
      1. Iterate candidates in rank order (most preferred first).
      2. For each, AST-validate that every variable referenced exists in
         the actual `available_inputs` catalogue (reject if hallucinated).
      3. Build the eval context EXCLUSIVELY from
         `available_inputs[<key>]['value']` — never from any Gemini-supplied
         numeric value.
      4. Use the first candidate that validates AND evaluates to a real
         number.

    Args:
        metric_key: e.g. 'net_irr', 'moic', 'tvpi'
        metric_meta: dict from DERIVABLE_FUND_METRICS — {label, unit, description}
        available_inputs: dict {input_key: {value, unit, description, available}}
                          where value is a number/str/list, available is bool
        scheme_context: human-readable scheme + fund summary string

    Returns:
        dict shaped:
        {
            'candidate_formulas': [
                {
                    'rank':              int,    # 1 = highest priority
                    'formula_expression': str,   # arithmetic OR "XIRR(cashflow_series)"
                    'inputs_required':   [input_key, ...],  # must be subset of available_inputs
                    'applies_when':      str,    # human description of when this fits
                    'confidence':        float 0-1,
                },
                ...
            ],
            'reasoning':           str,
        }
        On failure returns {'candidate_formulas': [], 'reasoning': '...'}.
    """
    # Build the "available inputs" section: for each input show description,
    # whether it's available, and the current value (truncated for series).
    lines = []
    for key, meta in available_inputs.items():
        avail = meta.get('available', False)
        val = meta.get('value')
        unit = meta.get('unit', '')
        desc = meta.get('description', '')

        # Render value. For lists (e.g. cashflow_series) show EVERY entry
        # so Gemini can reason over the complete dataset — truncating misled
        # earlier Gemini calls into refusing valid derivations.
        if val is None:
            val_repr = 'NULL'
        elif isinstance(val, list):
            val_repr = f'list of {len(val)} entries → {val}'
        else:
            val_repr = str(val)

        marker = '[AVAILABLE]' if avail else '[MISSING]'
        lines.append(f'  - {key} {marker} ({unit}): {desc}')
        lines.append(f'      current_value = {val_repr}')

    inputs_block = '\n'.join(lines)

    prompt = SHARED_MISSION_PREAMBLE + f"""You are a CFO/CA with 20+ years of experience in Alternative Investment
Fund (AIF) accounting and Private Equity / Venture Capital fund performance
metrics. A fund-management dashboard needs a value for the metric below, and
the imported Excel did NOT contain a direct value. You must derive it from
available sub-inputs.

METRIC TO DERIVE
================
key:         {metric_key}
label:       {metric_meta.get('label', metric_key)}
unit:        {metric_meta.get('unit', '')}
description: {metric_meta.get('description', '')}

SCHEME CONTEXT
==============
{scheme_context or '(no additional context)'}

AVAILABLE INPUTS (from the database)
====================================
{inputs_block}

LPA TERMS (Limited Partner Agreement economics)
================================================
Several inputs above are prefixed with "lpa_" — these are the fund's economic
terms extracted from the Limited Partner Agreement / Private Placement
Memorandum (annual management fee %, fee basis, hurdle rate %, carried
interest %, waterfall type, sponsor commitment %, tenure). When a metric
requires NET-of-fee or NET-of-carry treatment, or when a metric is defined on
a fee/hurdle-adjusted base (e.g. Net IRR, NAV after fee accrual, preferred
return), you MUST incorporate these LPA terms into the chosen formula. For
example: if a fund charges 2% mgmt fee on committed capital, the annual fee
drag is total_committed_capital × 0.02 × years_since_inception. If a hurdle
of 8% applies, the preferred return is total_called_capital × (1.08^years - 1).
Use the lpa_* inputs ANYWHERE they are relevant — do not silently drop them.

YOUR TASK — PROPOSE RANKED CANDIDATE FORMULAS
=============================================
Step 1. Enumerate ALL canonical/textbook formulas to compute this metric. Be
        exhaustive — list every standard PE/VC, accounting, or financial-math
        formula you know for this metric, including LPA-driven variants
        (net-of-fee / net-of-carry / hurdle-adjusted).

Step 2. Rank them. Rank 1 = the formula that is BOTH the textbook
        industry-standard for this metric AND uses inputs that are AVAILABLE
        (non-null, non-zero) in the catalogue above. Lower ranks = fallback
        formulas that should be tried if rank-1's inputs turn out to be
        unusable, or alternative canonical formulations.

Step 3. For each candidate formula:
        - `formula_expression` must reference ONLY input keys that appear
          verbatim in the AVAILABLE INPUTS catalogue above. If you reference
          a variable that is NOT in the catalogue, Python WILL reject the
          formula and try the next candidate.
        - `inputs_required` must list every variable used in
          `formula_expression`, verbatim.
        - `applies_when` is a 1-sentence description of WHEN this formula
          is the right choice (e.g. "When a direct cashflow series is
          available", "When only summary aggregates are reported", etc.).
        - `confidence` is your confidence in this formula's correctness in
          [0, 1].
        - `inputs_disjoint_proof` is a 1-2 sentence proof that the inputs
          in `inputs_required` are mathematically DISJOINT — i.e. summing
          / combining them does not double-count any cash flow. READ the
          catalogue descriptions; if two inputs OVERLAP (e.g.
          total_distributions and total_realised_proceeds share exit
          proceeds that were distributed to LPs), state explicitly that
          you have AVOIDED that overlap in your formula. If you cannot
          prove disjointness, RANK THIS FORMULA LOWER. Hallucinated
          disjointness is the single biggest source of dashboard-number
          errors — be honest.

Step 4. FORMULA SYNTAX:
        - For IRR-class metrics: set `formula_expression` to exactly the
          string "XIRR(cashflow_series)". Python will look up
          cashflow_series in the catalogue and run brentq XIRR on it.
          (cashflow_series must appear as a catalogue key.) Sign
          convention: contributions NEGATIVE, distributions POSITIVE,
          terminal NAV POSITIVE.

        - For ratio/multiple metrics (MOIC/TVPI/DPI/RVPI): plain arithmetic
          using ONLY catalogue keys as variable names — e.g.
          "(total_distributions_to_lps + total_unrealised_fair_value) / total_called_capital".

        - For NAV/currency/waterfall components: plain arithmetic of catalogue
          keys with the allowed function set below.

Step 5. The Python safe evaluator supports: + - * / ** % () plus bare-name
        functions `max(...)`, `min(...)`, `abs(x)`. It does NOT support
        attribute access (e.g. `.days`), conditional expressions,
        comparisons, or any other function calls.

Step 6. If NO formula can be expressed using ONLY catalogue keys, return
        an EMPTY `candidate_formulas` list and explain in `reasoning`
        which input is missing.

CRITICAL CONSTRAINTS — VIOLATIONS WILL BE REJECTED MECHANICALLY
================================================================
- DO NOT invent variable names. Every variable in every formula MUST be a
  key in the AVAILABLE INPUTS catalogue. Python AST-validates every
  formula and discards any that reference unknown variables.
- DO NOT supply numeric values. Python reads values from the catalogue
  directly. Your formula text + `inputs_required` list is the ONLY
  thing Python uses; any numeric values you mention are for your own
  reasoning only.
- DO NOT pick a formula whose required inputs are MISSING from the
  catalogue (marker `[MISSING]` above).
- DO NOT pick a formula whose required inputs are zero where division
  by zero or meaningless multiplication would result. Rank such
  formulas BELOW formulas with all non-zero inputs.
- For percentages, the formula MUST produce a number in dashboard scale
  (e.g. 18.5 for 18.5%), NOT a fraction (0.185).
- For multiples, the formula MUST produce a number (e.g. 1.85 for 1.85x).
- For currency, the formula MUST produce a value in the SAME units as the
  catalogue inputs (₹ raw — do not divide by Cr or Lakhs).

RETURN STRICT JSON ONLY (no markdown fences, no commentary outside JSON):
{{
  "candidate_formulas": [
    {{
      "rank":              1,
      "formula_expression": "<arithmetic / XIRR formula referencing only catalogue keys>",
      "inputs_required":   ["<catalogue_key>", ...],
      "applies_when":      "<1-sentence description of when this formula fits>",
      "inputs_disjoint_proof": "<1-2 sentence proof of why the inputs do not double-count>",
      "confidence":        <float 0.0 - 1.0>
    }},
    {{
      "rank":              2,
      "formula_expression": "<...>",
      "inputs_required":   ["<...>", ...],
      "applies_when":      "<...>",
      "confidence":        <float 0.0 - 1.0>
    }}
  ],
  "reasoning": "<1-3 sentence summary of the ranking choice>"
}}
"""

    try:
        # _call_gemini already returns parsed JSON (it runs _parse_json_response
        # internally). Use the dict directly — do NOT parse again.
        result = _call_gemini(prompt, context_label=f'Pass4-derive-{metric_key}')

        if not isinstance(result, dict):
            return {
                'candidate_formulas': [],
                'reasoning': 'Gemini returned non-dict response',
            }

        # Accept both the new ranked-candidates shape and the legacy
        # single-formula shape for backward compatibility. Normalise into
        # the canonical list-of-candidates form.
        raw_candidates = result.get('candidate_formulas')
        if raw_candidates is None and 'formula_expression' in result:
            raw_candidates = [{
                'rank': 1,
                'formula_expression': result.get('formula_expression', ''),
                'inputs_required': (
                    list((result.get('inputs_used') or {}).keys())
                ),
                'applies_when': result.get('reasoning') or '',
                'confidence': float(result.get('confidence') or 0.0),
            }]
        if not isinstance(raw_candidates, list):
            raw_candidates = []

        cleaned = []
        for c in raw_candidates:
            if not isinstance(c, dict):
                continue
            formula = str(c.get('formula_expression') or '').strip()
            if not formula:
                continue
            cleaned.append({
                'rank': int(c.get('rank') or (len(cleaned) + 1)),
                'formula_expression': formula[:2000],
                'inputs_required': c.get('inputs_required') or [],
                'applies_when': str(c.get('applies_when') or '')[:500],
                'inputs_disjoint_proof': str(
                    c.get('inputs_disjoint_proof') or ''
                )[:800],
                'confidence': float(c.get('confidence') or 0.0),
            })
        cleaned.sort(key=lambda c: c['rank'])

        out = {
            'candidate_formulas': cleaned,
            'reasoning': str(result.get('reasoning') or '').strip()[:4000],
        }

        logger.info(
            f'[GEMINI Pass4] derive_metric({metric_key}): '
            f'{len(cleaned)} candidate formula(s) returned'
        )
        return out

    except Exception as e:
        # DO NOT silently swallow API errors here. The previous behaviour
        # (return confidence=0.0) made an API failure look identical to a
        # "Gemini correctly decided no formula applies" outcome — so the
        # caller couldn't retry, and the user saw 4 metrics return null
        # without anyone realising the API itself had failed. Re-raise so
        # the orchestrator (MetricDerivationService._derive_one) can apply
        # its own retry/backoff and surface a distinct "api_error" status.
        logger.error(
            f'Gemini derive_metric API call failed for {metric_key}: '
            f'{type(e).__name__}: {e}'
        )
        raise


# ---------------------------------------------------------------------------
# Pass 2.6 — Column Semantic Role Classifier
# ---------------------------------------------------------------------------
# For each horizontal tabular section detected by Pass 2.5, classify every
# numeric column into a SEMANTIC ROLE. This is the missing context Pass 3.5
# needs to pick the right cell when one row carries multiple metrics in
# different columns (waterfall step tables, P&L sheets, KPI grids, etc.).
#
# ZERO keyword matching. Gemini reads the headers + sample data and reasons
# about what each column REPRESENTS. The same prompt works for any tabular
# layout in any Excel file.

# Allowed roles — Gemini must pick one for each numeric column.
COLUMN_ROLE_OPTIONS = {
    'per_period_amount': (
        'The actual value FOR this row\'s period / step / step-row entity. '
        'Examples: "LP Share" / "GP Share" / "Total Step" in a waterfall step '
        'table; "Q1 Revenue" in a quarterly P&L; "Jan-25 Burn" in a monthly '
        'burn-rate sheet.'
    ),
    'cumulative_total': (
        'A running total that accumulates across rows. Examples: "Cumulative '
        'Distributed" / "Balance Remaining" in a waterfall; "YTD Revenue"; '
        '"Cumulative IRR".'
    ),
    'ratio_percent': (
        'A share or percentage of a total. Examples: "% of Portfolio"; '
        '"% Allocation"; "Equity %".'
    ),
    'identifier': (
        'Row identifier — sequence number, step number, SKU. Examples: '
        '"Step #", "S.No", "Order ID".'
    ),
    'metadata_text': (
        'Free-text annotation, formula text, or notes that happen to render '
        'numerically. Should NOT be used as a metric value.'
    ),
    'derived_indicator': (
        'A column whose value is mechanically derived from the other columns '
        'of the SAME row (e.g. row-level multiple, row-level percentage of '
        'totals). Sometimes useful, sometimes a duplicate of a per_period '
        'value.'
    ),
    'unknown': (
        'Cannot be confidently classified into any of the above roles given '
        'the header text and sample values.'
    ),
}


def classify_column_roles(section_title, column_headers, sample_data_rows):
    """Classify each column in a horizontal tabular section by its SEMANTIC
    ROLE so Pass 3.5 can route candidates correctly.

    Args:
        section_title: e.g. "WATERFALL STEPS — Step-by-Step Formula
            Computation" — the section's own title from Pass 1.5.
        column_headers: dict {col_idx (1-based): header_text}. Only numeric
            columns are strictly required, but text columns help Gemini
            understand the table's overall structure.
        sample_data_rows: list of up to 3 dicts, each {col_idx: cell_value},
            representing the first few data rows. Gives Gemini visibility
            of magnitudes and monotonicity (e.g. cumulative columns
            strictly increase down the table).

    Returns:
        dict {col_idx: role} where role ∈ COLUMN_ROLE_OPTIONS keys.
        Unmapped/unsure columns become 'unknown'. Empty dict on API failure.

    Raises on hard API errors (caller can retry).
    """
    if not column_headers:
        return {}

    role_block = '\n'.join(
        f'  - "{key}": {desc}' for key, desc in COLUMN_ROLE_OPTIONS.items()
    )
    headers_block = '\n'.join(
        f'  col {ci}: "{ht}"' for ci, ht in sorted(column_headers.items())
    )
    samples_block_lines = []
    for i, row in enumerate(sample_data_rows[:3]):
        row_str = ', '.join(
            f'col{ci}={row.get(ci, "")!r}' for ci in sorted(column_headers.keys())
        )
        samples_block_lines.append(f'  Row {i+1}: {row_str}')
    samples_block = '\n'.join(samples_block_lines) or '  (no sample rows available)'

    prompt = SHARED_MISSION_PREAMBLE + f"""You are a CFO/CA classifying the SEMANTIC ROLE of each column in a
tabular section of an Alternative Investment Fund (AIF) Excel workbook.
Downstream code uses the role labels you assign to decide WHICH column
is the canonical source for each metric the dashboard needs. Picking the
wrong column produces wrong dashboard numbers, so be careful and precise.

SECTION TITLE: {section_title or '(none)'}

COLUMN HEADERS
==============
{headers_block}

SAMPLE DATA ROWS (look at magnitudes + monotonicity)
====================================================
{samples_block}

ALLOWED ROLES (pick exactly one per column)
============================================
{role_block}

REASONING GUIDANCE
==================
1. Look at the header text AND the sample values together. A column named
   "Cumulative" whose values strictly increase down the table is
   `cumulative_total`. A column whose values can rise OR fall row-to-row
   is `per_period_amount`.
2. In a waterfall STEP table, expect: 1 `identifier` column (Step #),
   1 `metadata_text` column (Description), 2-3 `per_period_amount`
   columns (LP Share / GP Share / Total Step), 1-2 `cumulative_total`
   columns (Cumulative Distributed / Balance Remaining), and possibly a
   `metadata_text` column (Formula).
3. In a quarterly P&L, expect 4 `per_period_amount` columns (one per
   quarter) and possibly a 5th `cumulative_total` column (FY/YTD).
4. If you cannot confidently classify a column, return `unknown` for that
   column. Do NOT guess. Downstream code treats `unknown` columns as
   ineligible for canonical-metric extraction.

RETURN STRICT JSON ONLY (no markdown fences, no commentary outside JSON):
{{
  "<col_idx as integer>": "<role>",
  ...
}}

Where each role MUST be one of: {", ".join(repr(k) for k in COLUMN_ROLE_OPTIONS.keys())}.
"""

    try:
        result = _call_gemini(
            prompt, context_label=f'Pass2.6-column-roles-{section_title[:40]}'
        )
        if not isinstance(result, dict):
            return {}
        out = {}
        for k, v in result.items():
            try:
                ci = int(k)
            except (TypeError, ValueError):
                continue
            role = str(v or '').strip()
            if role not in COLUMN_ROLE_OPTIONS:
                role = 'unknown'
            out[ci] = role
        logger.info(
            f'[GEMINI Pass2.6] classify_column_roles({section_title[:40]!r}): '
            f'{len(out)}/{len(column_headers)} columns classified '
            f'(roles: {dict((c, out[c]) for c in sorted(out))})'
        )
        return out
    except Exception as e:
        logger.error(
            f'Gemini classify_column_roles failed for section '
            f'{section_title[:40]!r}: {type(e).__name__}: {e}'
        )
        raise


def classify_metric_variant(metric_key, metric_label, metric_description,
                            variant_options, candidates):
    """For canonical metrics that come in semantic variants (gross/net,
    pre-fee/post-fee, ...), tag each candidate cell with which variant it
    represents. Called BEFORE select_authoritative_source so the
    disambiguator can also filter by variant.

    Args:
        metric_key: canonical key, e.g. 'total_unrealised_fair_value'
        metric_label / metric_description: from the catalogue
        variant_options: list of allowed variant tags, e.g. ['gross', 'net']
        candidates: list of dicts with 'label', 'value', 'source_cell',
            'column_header' (per L2). The function adds 'variant' to each.

    Returns:
        List of candidate dicts (same order, same keys) with an added
        'variant' field per candidate. Variant is one of variant_options or
        the string 'unknown' when Gemini cannot tell.

    Raises on hard API errors.
    """
    if not candidates or not variant_options:
        return candidates

    lines = []
    for i, c in enumerate(candidates):
        col_h = c.get('column_header') or ''
        col_part = f'  column_header="{col_h}"' if col_h else ''
        lines.append(
            f'  [{i}] source={c.get("source_cell", "?")}  '
            f'label="{c.get("label", "")}"{col_part}  '
            f'value={c.get("value")}'
        )
    candidates_block = '\n'.join(lines)
    variants_block = ', '.join(repr(v) for v in variant_options) + ", 'unknown'"

    prompt = SHARED_MISSION_PREAMBLE + f"""You are classifying which VARIANT each candidate cell represents for
the canonical metric below. Variants exist because the same metric can
be reported with DIFFERENT semantic definitions in different parts of
the workbook (e.g. gross-of-fees vs net-of-fees; pre-DLOM vs
post-DLOM; pre-carry vs post-carry).

CANONICAL METRIC
================
key:         {metric_key}
label:       {metric_label}
description: {metric_description}

ALLOWED VARIANT TAGS (pick one per candidate)
=============================================
{variants_block}

CANDIDATE CELLS
===============
{candidates_block}

REASONING GUIDANCE
==================
- Read the row label, column header, sheet name, and the canonical
  metric description together. Decide which variant each candidate's
  cell represents.
- If a candidate's source row clearly mentions a discount (DLOM, DLOC,
  haircut), it is the NET variant.
- If a candidate's source row is a fund-level summary in a waterfall
  / cashflow sheet, it is usually GROSS.
- If you cannot confidently tag the variant, return 'unknown' — the
  candidate will be deprioritised.

RETURN STRICT JSON ONLY (no markdown fences, no commentary outside JSON):
{{
  "<candidate_index_as_integer>": "<variant_tag>",
  ...
}}
"""

    try:
        result = _call_gemini(
            prompt, context_label=f'Pass3.5-variant-{metric_key}'
        )
        if not isinstance(result, dict):
            return candidates
        for k, v in result.items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(candidates):
                variant = str(v or '').strip()
                if variant not in variant_options:
                    variant = 'unknown'
                candidates[idx]['variant'] = variant
        # Ensure every candidate has a variant field
        for c in candidates:
            c.setdefault('variant', 'unknown')
        logger.info(
            f'[GEMINI Pass3.5] classify_metric_variant({metric_key}): '
            f'tagged {len([c for c in candidates if c.get("variant") != "unknown"])}'
            f'/{len(candidates)} candidates'
        )
        return candidates
    except Exception as e:
        logger.error(
            f'Gemini classify_metric_variant failed for {metric_key}: '
            f'{type(e).__name__}: {e}'
        )
        raise


def validate_waterfall_identity(values, tolerance_pct=2.0):
    """After Pass 4 derives all waterfall components, ask Gemini whether
    the values are mutually consistent under the standard waterfall
    identity. This is a sanity check, not a heuristic — it uses the
    mathematical identity
        return_of_capital + preferred_return + gp_catchup + carry_base
        ≈ total_proceeds
    (the LHS is the sum of every step in a European waterfall; the RHS
    is the total cash pool. They MUST be equal up to rounding when the
    extraction + derivation pipeline is correct.)

    Args:
        values: dict {canonical_key: numeric_value}. Must contain
            'return_of_capital_amount', 'preferred_return_amount',
            'gp_catchup_amount', 'carry_base', and either
            'total_realised_proceeds' + 'total_unrealised_fair_value'
            OR a precomputed 'total_proceeds_available'. Missing inputs
            cause this function to return {'status': 'skipped_missing_inputs'}.
        tolerance_pct: pass if |LHS - RHS| / RHS * 100 ≤ tolerance_pct.

    Returns:
        dict {
            'status': 'pass' | 'fail' | 'skipped_missing_inputs',
            'lhs_sum': float,
            'rhs_total': float,
            'diff_pct': float,
            'reasoning': str,
        }
    """
    required = ['return_of_capital_amount', 'preferred_return_amount',
                'gp_catchup_amount', 'carry_base']
    if any(values.get(k) is None for k in required):
        return {
            'status': 'skipped_missing_inputs',
            'lhs_sum': None, 'rhs_total': None, 'diff_pct': None,
            'reasoning': (
                f'Cannot validate — missing inputs: '
                f'{[k for k in required if values.get(k) is None]}'
            ),
        }

    lhs = (
        float(values['return_of_capital_amount'])
        + float(values['preferred_return_amount'])
        + float(values['gp_catchup_amount'])
        + float(values['carry_base'])
    )
    rhs = None
    if values.get('total_proceeds_available') is not None:
        rhs = float(values['total_proceeds_available'])
    elif (values.get('total_realised_proceeds') is not None
          and values.get('total_unrealised_fair_value') is not None):
        rhs = (
            float(values['total_realised_proceeds'])
            + float(values['total_unrealised_fair_value'])
        )
    if rhs is None or rhs == 0:
        return {
            'status': 'skipped_missing_inputs',
            'lhs_sum': lhs, 'rhs_total': rhs, 'diff_pct': None,
            'reasoning': 'Cannot compute RHS (total proceeds) from inputs.',
        }

    diff_pct = abs(lhs - rhs) / abs(rhs) * 100.0
    status = 'pass' if diff_pct <= tolerance_pct else 'fail'
    reasoning = (
        f'Waterfall identity: return_of_capital + preferred_return + '
        f'gp_catchup + carry_base = {lhs:.4f}. '
        f'Total proceeds = {rhs:.4f}. '
        f'Diff = {abs(lhs - rhs):.4f} ({diff_pct:.2f}% of total proceeds). '
        f'Tolerance = {tolerance_pct:.2f}%. Status = {status}.'
    )
    logger.info(f'[Pass4 identity] {reasoning}')
    return {
        'status': status, 'lhs_sum': lhs, 'rhs_total': rhs,
        'diff_pct': diff_pct, 'reasoning': reasoning,
    }


# ---------------------------------------------------------------------------
# Pass 8 — Direct Waterfall Computation
# ---------------------------------------------------------------------------
# Replaces the layered Pass 3.5 + Pass 4 derivation pipeline for the four
# carry/clawback dashboard fields with ONE Gemini call that sees the
# complete waterfall sheet content and the fund's LPA terms. This matches
# the proven approach where Gemini, given direct access to the workbook,
# returns accurate values in one shot — without the intermediate
# extraction errors that broke the layered pipeline.
#
# ZERO formulas in code. Gemini reads the sheet, decides the formula, and
# reports the four values WITH source-cell citations and confidence.
# Python performs only basic sanity checks (net = gross - clawback, etc.)
# and stores the result with full provenance.

# The exact metric keys this pass writes — narrow scope to avoid
# disrupting other extraction logic.
WATERFALL_PASS8_METRIC_KEYS = (
    'carry_base',
    'carry_amount_gross',
    'gp_clawback_provision',
    'carry_amount_net',
)

# Supplementary metrics Pass 8 may ALSO report when the sheet exposes
# them; these are not strictly required, but capturing them improves
# the audit trail and lets the frontend waterfall bars render correctly.
WATERFALL_PASS8_SUPPLEMENTARY_KEYS = (
    'return_of_capital_amount',
    'preferred_return_amount',
    'gp_catchup_amount',
    'lp_total_return',
    'gp_total_distribution',
    'total_proceeds_available',
)


def _dump_sheet_as_text(ws, max_rows=200, max_cols=20):
    """Render a worksheet as a labelled cell grid for Gemini.

    Each non-empty cell becomes one line: "R<row> C<col>: <value>".
    This format is compact, deterministic, and lets Gemini quote source
    cells by their (row, col) coordinates in its response.
    """
    lines = []
    real_max_row = min(ws.max_row or 0, max_rows)
    real_max_col = min(ws.max_column or 0, max_cols)
    for r in range(1, real_max_row + 1):
        row_cells = []
        for c in range(1, real_max_col + 1):
            v = ws.cell(r, c).value
            if v is None:
                continue
            s = str(v).replace('\n', ' ').strip()
            if not s:
                continue
            row_cells.append(f'C{c}="{s[:200]}"')
        if row_cells:
            lines.append(f'  R{r}: ' + ' | '.join(row_cells))
    return '\n'.join(lines)


def compute_waterfall_metrics_directly(waterfall_sheets, lpa_terms,
                                       capital_flows, as_of_date):
    """Pass 8 — ONE Gemini call computes the four carry/clawback fields by
    reading the complete waterfall sheet(s) directly.

    Args:
        waterfall_sheets: dict {sheet_name: openpyxl Worksheet} — every
            sheet Pass 1 classified into the `waterfall_carry` domain.
        lpa_terms: dict of fund's LPA terms (hurdle_rate_pct, carry_pct,
            carry_type, management_fee_pct, management_fee_basis,
            tenure_years, sponsor_commitment_pct). Values may be None
            when not extracted yet.
        capital_flows: dict of cumulative capital-flow inputs visible
            to Gemini for cross-check (total_called_capital,
            total_committed_capital, total_distributions,
            total_realised_proceeds, total_unrealised_fair_value).
        as_of_date: date — the calculation date Gemini should treat as
            "today".

    Returns:
        dict {
            'metrics': {
                'carry_base': {
                    'value': float,
                    'source_cells': ['SHEET!R23', ...],
                    'formula_used': '<plain-text formula>',
                    'confidence': float 0-1,
                    'reasoning': '<1-3 sentences>',
                },
                'carry_amount_gross': {...},
                'gp_clawback_provision': {...},
                'carry_amount_net': {...},
                # Supplementary keys (optional, may be missing):
                'return_of_capital_amount': {...},
                'preferred_return_amount': {...},
                'gp_catchup_amount': {...},
                ...
            },
            'overall_reasoning': '<3-5 sentence summary>',
            'sheet_used': '<primary sheet name>',
        }

        Raises on hard API errors. Returns {'metrics': {}} when no
        waterfall sheet is available.
    """
    if not waterfall_sheets:
        logger.info('Pass 8: no waterfall_carry sheets found in workbook — skipping')
        return {'metrics': {}, 'overall_reasoning': 'No waterfall sheet present.',
                'sheet_used': None}

    # Build the sheet-content block. If multiple waterfall sheets exist,
    # dump each one with its name as a header.
    sheet_blocks = []
    for sname, ws in waterfall_sheets.items():
        block = _dump_sheet_as_text(ws)
        sheet_blocks.append(f'═══ SHEET: {sname} ═══\n{block}')
    sheets_text = '\n\n'.join(sheet_blocks)

    # LPA terms block
    lpa_lines = []
    for k in ('hurdle_rate_pct', 'carry_pct', 'carry_type',
              'management_fee_pct', 'management_fee_basis',
              'tenure_years', 'sponsor_commitment_pct'):
        v = lpa_terms.get(k) if isinstance(lpa_terms, dict) else None
        lpa_lines.append(f'  {k}: {v}')
    lpa_block = '\n'.join(lpa_lines)

    # Capital flow context (cross-check inputs)
    flow_lines = []
    for k in ('total_called_capital', 'total_committed_capital',
              'total_distributions', 'total_realised_proceeds',
              'total_unrealised_fair_value'):
        v = capital_flows.get(k) if isinstance(capital_flows, dict) else None
        flow_lines.append(f'  {k}: {v}')
    flows_block = '\n'.join(flow_lines)

    prompt = SHARED_MISSION_PREAMBLE + f"""You are a CFO/CA with 20+ years of experience computing carried
interest waterfalls for Indian AIFs (Alternative Investment Funds).
You have COMPLETE ACCESS to the workbook's waterfall sheet(s) below.
Read the sheet content, identify the relevant cells, and compute the
four carry & clawback dashboard fields with 100% ACCURACY.

DO NOT GUESS. DO NOT INVENT VALUES. Every output number MUST be
traceable to specific cells in the sheet content below, OR derived
arithmetically from those cell values using formulas you cite
explicitly.

WATERFALL SHEET CONTENT (every non-empty cell, format "R<row> C<col>=<value>")
==============================================================================
{sheets_text}

FUND'S LPA TERMS (from the Scheme model)
========================================
{lpa_block}

CAPITAL FLOW CONTEXT (cross-check inputs from DerivedMetric — use only
if the waterfall sheet itself doesn't already report the same number)
=====================================================================
{flows_block}

AS-OF DATE: {as_of_date}

REQUIRED OUTPUTS
================
For EACH of the four fields below, return:
  - value             — a number, in the SAME currency unit the sheet uses
                        (typically INR Cr; do NOT convert).
  - source_cells      — a list of cell references like ["WATERFALL_EUR!R23"]
                        or ["WATERFALL_EUR!R23C5"] you read to compute it.
                        If you derived the number arithmetically, list the
                        cells whose values feed the arithmetic.
  - formula_used      — plain text formula, e.g.
                        "Step 4 Total Step (R23 C5) = LP residual + GP residual = 345.96 + 86.49".
  - confidence        — float 0..1 (your confidence the value is correct).
  - reasoning         — 1-3 sentence justification.

THE FOUR REQUIRED FIELDS
========================
1. carry_base
   The eligible profit pool subject to the GP's carry-percentage split.
   In a European waterfall with a 4-step structure:
     carry_base = Step 4 Total Step (the residual pool after Steps 1-3).
   If the sheet has no explicit Step 4, derive as:
     carry_base = total_proceeds_available - return_of_capital
                  - preferred_return - gp_catchup.

2. carry_amount_gross
   Total GP carry across all waterfall steps BEFORE any clawback
   adjustment. In a European 4-step waterfall this equals:
     gp_catchup (Step 3 GP Share) + GP share of Step 4 residual.
   Cite the GP-share cells from each step.

3. gp_clawback_provision
   Excess/escrowed carry returned to LPs. KEY RULE: a clawback can only
   exist if carry has ACTUALLY BEEN PAID to the GP and later proven
   excessive. For a European (whole-fund) waterfall with the fund still
   active and realised proceeds below the LP-capital-return threshold,
   the clawback is ZERO. Only return a non-zero value if the sheet
   explicitly shows distributed carry that exceeds the GP entitlement
   at this as-of date.

4. carry_amount_net
   = carry_amount_gross - gp_clawback_provision.

ALSO REPORT (supplementary; include only when the sheet exposes them):
  return_of_capital_amount   — Step 1 LP Share
  preferred_return_amount    — Step 2 LP Share (per-step amount, NOT
                               cumulative running total)
  gp_catchup_amount          — Step 3 GP Share
  lp_total_return            — Sum of LP shares across all steps
  gp_total_distribution      — Sum of GP shares across all steps
                               (= carry_amount_gross when there is no
                               separate LP/GP catch-up bookkeeping)
  total_proceeds_available   — The single fund-level total proceeds cell
                               from the sheet's Inputs section

VALIDATION RULES TO SELF-CHECK BEFORE RESPONDING
=================================================
- The Step 1 LP Share value MUST equal the total LP committed capital
  (Return of Capital is the FIRST step in a European waterfall).
- Cumulative columns ("Cumulative Distributed", "Balance Remaining",
  etc.) are RUNNING TOTALS, not per-step amounts. NEVER read a per-step
  metric from a cumulative column.
- carry_amount_net MUST equal carry_amount_gross - gp_clawback_provision.
- The sum (return_of_capital + preferred_return + gp_catchup + carry_base)
  MUST equal total_proceeds_available to within rounding (≤ 2%).
- If a self-check fails, re-read the sheet content and correct your
  picks before responding.

RETURN STRICT JSON ONLY (no markdown fences, no prose outside JSON):
{{
  "metrics": {{
    "carry_base":              {{"value": <number>, "source_cells": [...], "formula_used": "...", "confidence": <float>, "reasoning": "..."}},
    "carry_amount_gross":      {{"value": <number>, "source_cells": [...], "formula_used": "...", "confidence": <float>, "reasoning": "..."}},
    "gp_clawback_provision":   {{"value": <number>, "source_cells": [...], "formula_used": "...", "confidence": <float>, "reasoning": "..."}},
    "carry_amount_net":        {{"value": <number>, "source_cells": [...], "formula_used": "...", "confidence": <float>, "reasoning": "..."}},
    "return_of_capital_amount": {{...}} | null,
    "preferred_return_amount":  {{...}} | null,
    "gp_catchup_amount":        {{...}} | null,
    "lp_total_return":          {{...}} | null,
    "gp_total_distribution":    {{...}} | null,
    "total_proceeds_available": {{...}} | null
  }},
  "overall_reasoning": "<3-5 sentence summary of waterfall mechanics applied>",
  "sheet_used": "<primary sheet name>"
}}
"""

    primary_sheet = next(iter(waterfall_sheets.keys()))
    try:
        result = _call_gemini(
            prompt, context_label=f'Pass8-waterfall-{primary_sheet}'
        )
        if not isinstance(result, dict):
            logger.warning('Pass 8 returned non-dict response')
            return {'metrics': {}, 'overall_reasoning': 'Non-dict response',
                    'sheet_used': primary_sheet}

        raw_metrics = result.get('metrics') or {}
        cleaned = {}
        for key in (WATERFALL_PASS8_METRIC_KEYS
                    + WATERFALL_PASS8_SUPPLEMENTARY_KEYS):
            entry = raw_metrics.get(key)
            if not isinstance(entry, dict):
                continue
            val = entry.get('value')
            try:
                val = float(val) if val is not None else None
            except (TypeError, ValueError):
                val = None
            if val is None:
                continue
            cleaned[key] = {
                'value': val,
                'source_cells': entry.get('source_cells') or [],
                'formula_used': str(entry.get('formula_used') or '')[:1000],
                'confidence': float(entry.get('confidence') or 0.0),
                'reasoning': str(entry.get('reasoning') or '')[:2000],
            }

        # Sanity-check identity: carry_amount_net ≈ gross - clawback.
        # If Gemini violated it, log loudly and prefer the identity-derived
        # value over Gemini's own carry_amount_net.
        g = cleaned.get('carry_amount_gross', {}).get('value')
        c = cleaned.get('gp_clawback_provision', {}).get('value')
        n = cleaned.get('carry_amount_net', {}).get('value')
        if g is not None and c is not None:
            implied_net = g - c
            if n is None or abs(n - implied_net) > max(1.0, abs(implied_net) * 0.01):
                logger.warning(
                    f'Pass 8 carry_amount_net inconsistent '
                    f'(Gemini said {n}, identity g-c={implied_net}). '
                    f'Replacing with identity-derived value.'
                )
                cleaned['carry_amount_net'] = {
                    'value': implied_net,
                    'source_cells': (
                        cleaned.get('carry_amount_gross', {}).get('source_cells', [])
                        + cleaned.get('gp_clawback_provision', {}).get('source_cells', [])
                    ),
                    'formula_used': 'carry_amount_gross - gp_clawback_provision (identity backfill)',
                    'confidence': min(
                        cleaned.get('carry_amount_gross', {}).get('confidence', 0.0),
                        cleaned.get('gp_clawback_provision', {}).get('confidence', 0.0),
                    ),
                    'reasoning': (
                        'Python re-derived net = gross - clawback after detecting '
                        'an inconsistency in Gemini-reported net.'
                    ),
                }

        out = {
            'metrics': cleaned,
            'overall_reasoning': str(result.get('overall_reasoning') or '')[:4000],
            'sheet_used': str(result.get('sheet_used') or primary_sheet),
        }
        logger.info(
            f'[GEMINI Pass8] compute_waterfall_metrics_directly: '
            f'returned {len(cleaned)} metric(s) for sheet {out["sheet_used"]}'
        )
        for k, v in cleaned.items():
            logger.info(
                f'  [Pass8] {k} = {v["value"]} '
                f'(conf={v["confidence"]:.2f}, '
                f'src={v["source_cells"]}, '
                f'formula="{v["formula_used"][:80]}")'
            )
        return out

    except Exception as e:
        logger.error(
            f'Pass 8 compute_waterfall_metrics_directly failed: '
            f'{type(e).__name__}: {e}'
        )
        raise


# ═══════════════════════════════════════════════════════════════════════════
# Pass 9 — UNIFIED FUND METRICS COMPUTE
# ═══════════════════════════════════════════════════════════════════════════
#
# ONE Gemini call sees the raw content of every fund-level sheet, plus the
# LPA terms, plus the canonical definition of every dashboard metric, and
# returns ALL fund-level metric values with formulas + source cells +
# confidence + reasoning. This is the generalisation of Pass 8 (which
# only handled the 4 waterfall fields).
#
# Architectural premise (per the user's own demonstration):
#   "If a single 1-line prompt to Gemini against the raw workbook produces
#    100% accurate values, multi-layer extraction pipelines add no value —
#    they only introduce indirection that loses information."
#
# Pass 9 replaces the catalogue-of-variables → formula-derivation chain
# (Pass 4) for fund-level metrics. Pass 4 stays as a fallback only for
# metrics Pass 9 declines to return. Pass 8 is subsumed by Pass 9 (it
# remains callable for fast waterfall-only runs but is no longer wired
# into the import flow).
#
# What this fixes that the previous pipeline got wrong:
#   - Mock_14: Pass 4 used sum__navrecord__total_nav (summed 12 monthly
#       snapshots) → carry_base of ₹44,013 Cr. Pass 9 sees the NAV sheet
#       directly and reads the latest single row.
#   - Trivesta: Pass 4's catalogue exposed sum__investment__total_invested
#       and Investment.fair_value with rotten per-row values → MOIC 0.63x.
#       Pass 9 reads the MOIC_TVPI_DPI sheet's totals row directly.
#   - Trivesta Net IRR: Pass 4 declined; frontend fell back to cost-
#       weighted average of per-investment IRRs → −14.2% (wrong sign).
#       Pass 9 reads MASTER_INPUTS R91 "Net IRR = 0.1612" directly.

# Fund-level metric keys Pass 9 is responsible for (superset of Pass 8's
# waterfall keys plus all multiples / IRR / NAV / aggregate-flow keys).
PASS9_METRIC_KEYS = (
    # Multiples & ratios
    'moic', 'tvpi', 'dpi', 'rvpi', 'net_irr', 'gross_irr',
    # Aggregates / totals
    'nav', 'total_unrealised_fair_value', 'total_realised_proceeds',
    'total_distributions', 'total_called_capital',
    'total_committed_capital',
    # Waterfall (formerly Pass 8)
    'return_of_capital_amount', 'preferred_return_amount',
    'gp_catchup_amount', 'carry_base', 'carry_amount_gross',
    'gp_clawback_provision', 'carry_amount_net',
    'lp_total_return', 'gp_total_distribution',
    'total_proceeds_available',
)

# Pass 1 sheet-domains whose content Pass 9 needs to see. We deliberately
# EXCLUDE portfolio_companies (50-130 rows, irrelevant for fund-level
# aggregates), KPI trackers (per-company KPIs), investor_register (LP-
# level, not fund-level), and compliance/governance domains.
PASS9_RELEVANT_DOMAINS = (
    'waterfall_carry', 'nav_records', 'capital_calls', 'distributions',
    'exits', 'fund_pl', 'fund_balance_sheet', 'fund_cashflow',
    'scheme_lifecycle', 'commitment_summary', 'bva',
    # Some workbooks (TrackFundAI training file) place fund-level
    # summaries on sheets named "MOIC_TVPI_DPI", "MASTER_INPUTS",
    # "DASHBOARD_BRIDGE" — these may classify as unknown or scheme_terms.
    # We also include 'scheme_terms' and 'fund_overview' for those.
    'scheme_terms', 'fund_overview',
)


def _canonical_metric_definitions_block():
    """Return a structured plain-text block describing every Pass-9 metric
    with its canonical formula, unit, and read-from-sheet hints. Single
    source of truth for the prompt — keep it concise; Gemini sees it once
    per call, not per metric.
    """
    return """
moic              — Multiple on Invested Capital (gross). Formula:
                    (cumulative_distributions_to_LPs + residual_NAV)
                    / cumulative_paid_in_capital. Many workbooks
                    report it as a single cell labelled "Gross MOIC"
                    or "Blended Portfolio MOIC". Unit: multiple (x).

tvpi              — Total Value to Paid-In. Formula:
                    (cumulative_distributions_to_LPs + residual_fund_NAV)
                    / cumulative_LP_paid_in_capital. Unit: multiple (x).
                    NOTE: cost basis is NOT the denominator — paid-in
                    (called) capital is. Many sheets confuse these.

dpi               — Distributions to Paid-In = LP_distributions /
                    paid_in_capital. Unit: multiple (x).

rvpi              — Residual Value to Paid-In = residual_NAV /
                    paid_in_capital. Unit: multiple (x).
                    Self-check: tvpi ≈ dpi + rvpi.

net_irr           — Net IRR to LPs, annualised. Computed as XIRR over
                    LP cash flows (capital calls negative, distributions
                    positive, terminal NAV as positive synthetic
                    inflow at as-of date). Workbooks often pre-compute
                    this on a cash-flow table or in a fund-summary
                    section labelled "Net IRR". Unit: percent.

gross_irr         — Same but on gross cash flows (before mgmt fees).
                    Unit: percent.

nav               — Latest single-row total fund NAV (assets − liabilities).
                    NEVER sum NAV across monthly snapshots — pick the
                    most recent row. Unit: currency (INR Cr typically).

total_unrealised_fair_value — Sum of FV across active (un-exited)
                    portfolio investments at the AS-OF date. Read
                    from a totals row, not by summing per-row cells
                    if the sheet provides a totals row.

total_realised_proceeds — Cumulative exit proceeds across all exited
                    investments. Read from the exits sheet totals row.

total_distributions — Cumulative cash distributions paid to LPs across
                    the fund's life. Read from the LP register or
                    distributions sheet totals row.

total_called_capital — Cumulative LP capital drawn from commitments.
                    Read from the capital calls totals row or the
                    fund-master "Total Called" cell.

total_committed_capital — Sum of LP commitments at final close.
                    Read from the fund-master commitments cell.

return_of_capital_amount — Waterfall Step 1 LP Share (European).
                    In a European waterfall, this equals total LP
                    committed capital (or the called portion if the
                    sheet uses called as the basis).

preferred_return_amount — Waterfall Step 2 LP Share. The
                    PER-STEP amount, NOT a cumulative running total.

gp_catchup_amount — Waterfall Step 3 GP Share (in 100%-catchup
                    structures). Cite the per-step GP-share cell.

carry_base        — Profit pool subject to the final carry split
                    (Step 4 Total Step in a 4-step European waterfall).

carry_amount_gross — Total GP carry across all waterfall steps BEFORE
                    clawback. = gp_catchup + GP_share_of_Step_4.

gp_clawback_provision — Clawback escrow. ZERO when the fund has not
                    yet over-paid carry. For interim periods with no
                    realised carry distribution, return 0.

carry_amount_net  — = carry_amount_gross − gp_clawback_provision.

lp_total_return   — Sum of LP shares across all 4 waterfall steps.

gp_total_distribution — Sum of GP shares across all 4 waterfall steps.

total_proceeds_available — Total fund proceeds available for the
                    waterfall (sum of Distributions + Realised Proceeds
                    + Residual NAV in a European whole-fund model).
"""


def compute_fund_metrics_unified(filepath, sheet_classifications, lpa_terms,
                                 as_of_date):
    """Pass 9 — ONE Gemini call computes every fund-level dashboard metric.

    Args:
        filepath: path to the workbook on disk.
        sheet_classifications: Pass 1 output, dict of
            {sheet_name: {'primary_domain': str, ...}}.
        lpa_terms: dict with hurdle_rate_pct, carry_pct, carry_type,
            management_fee_pct, management_fee_basis, tenure_years,
            sponsor_commitment_pct, vintage_year. None values are OK.
        as_of_date: date — treated as "today" by Gemini.

    Returns:
        dict {
            'metrics': {
                'moic': {'value': float, 'source_cells': [...],
                         'formula_used': str, 'confidence': float,
                         'reasoning': str},
                'tvpi': {...},
                ...one entry per metric Gemini could compute...
            },
            'sheets_used': [str, ...],
            'overall_reasoning': str,
        }

        Returns {'metrics': {}, 'sheets_used': []} when no relevant
        sheets are present. Raises on hard API failure (caller decides
        whether to fall back to Pass 4).
    """
    import openpyxl

    # Pick relevant sheets via Pass 1 domain classification, with a
    # graceful fallback to "all non-portfolio_companies sheets" when
    # classifications are missing or empty (handles workbooks where
    # Pass 1 was skipped or returned blanks).
    relevant_sheets = []
    if sheet_classifications:
        for sn, cls in sheet_classifications.items():
            domain = (
                cls.get('primary_domain') if isinstance(cls, dict) else None
            )
            if domain in PASS9_RELEVANT_DOMAINS:
                relevant_sheets.append(sn)

    try:
        wb = openpyxl.load_workbook(filepath, data_only=True, read_only=False)
    except Exception as e:
        raise ValueError(f'Pass 9 could not open workbook {filepath}: {e}')

    # Fallback: when classification gave us nothing, include any sheet
    # whose name hints at fund-level content. Last-resort heuristic so a
    # missing Pass 1 result doesn't disable Pass 9 entirely.
    if not relevant_sheets:
        FUND_LEVEL_NAME_HINTS = (
            'waterfall', 'nav', 'master', 'fund', 'capital_call',
            'distribution', 'exit', 'p&l', 'pnl', 'bva', 'budget',
            'moic', 'tvpi', 'dpi', 'irr', 'summary', 'dashboard',
            'lifecycle', 'commitment',
        )
        for sn in wb.sheetnames:
            sn_low = sn.lower().replace(' ', '_')
            if any(h in sn_low for h in FUND_LEVEL_NAME_HINTS):
                relevant_sheets.append(sn)

    if not relevant_sheets:
        wb.close()
        logger.info('Pass 9: no fund-level sheets found — skipping')
        return {'metrics': {}, 'sheets_used': [],
                'overall_reasoning': 'No fund-level sheets present.'}

    # Token budget — soft cap per sheet so a 500-row workbook doesn't
    # blow the context. Most fund summary sheets are < 100 rows; the
    # cash-flow / waterfall sheets that matter most are < 50.
    PER_SHEET_ROW_CAP = 180
    PER_SHEET_COL_CAP = 14

    sheet_blocks = []
    for sn in relevant_sheets:
        try:
            ws = wb[sn]
        except KeyError:
            continue
        block = _dump_sheet_as_text(
            ws, max_rows=PER_SHEET_ROW_CAP, max_cols=PER_SHEET_COL_CAP,
        )
        if block.strip():
            sheet_blocks.append(f'═══ SHEET: {sn} ═══\n{block}')
    sheets_text = '\n\n'.join(sheet_blocks)
    try:
        wb.close()
    except Exception:
        pass

    # LPA terms — give Gemini the fund's actual scheme parameters so it
    # can reproduce the hurdle / carry / management fee arithmetic.
    lpa_lines = []
    for k in ('hurdle_rate_pct', 'carry_pct', 'carry_type',
              'management_fee_pct', 'management_fee_basis',
              'tenure_years', 'sponsor_commitment_pct', 'vintage_year'):
        v = lpa_terms.get(k) if isinstance(lpa_terms, dict) else None
        lpa_lines.append(f'  {k}: {v}')
    lpa_block = '\n'.join(lpa_lines)

    metric_defs = _canonical_metric_definitions_block()

    prompt = SHARED_MISSION_PREAMBLE + f"""You are a CFO / Chartered Accountant with 20+ years computing
performance metrics for Indian AIFs. The workbook for ONE fund is below.
Read EVERY sheet, identify the cells the workbook uses to report each
metric, and compute the dashboard values with 100% ACCURACY.

ABSOLUTE RULES
==============
1. EVERY value you return MUST be traceable to specific cells you cite
   ("SHEET!R<row>C<col>") OR derived arithmetically from cited cells.
   Never invent a number.
2. When the workbook ALREADY reports a metric (e.g. a cell labelled
   "Gross MOIC = 0.7534"), READ that cell. Do NOT recompute from raw
   underlying data — recomputation introduces drift.
3. For periodic-snapshot fields (NAV, Total Fund Value), use the LATEST
   single row, NEVER the sum across months.
4. For cumulative-flow fields (called capital, distributions, exit
   proceeds), use the cumulative total — the TOTALS row if the sheet
   has one.
5. For waterfall steps, read PER-STEP cells (Step 1 LP Share, Step 2 LP
   Share, etc.), not cumulative running totals.
6. If the workbook contains contradictory values for the same metric
   (e.g. one sheet says MOIC=0.75 and another says MOIC=2.0 "target"),
   PREFER the value labelled as actual / current / realised over any
   value labelled "target" / "benchmark" / "estimated" / "budgeted".
7. If a metric CANNOT be computed with high confidence from the sheets
   below, return that metric with value=null and a reason. NEVER guess.
8. carry_amount_net MUST equal carry_amount_gross − gp_clawback_provision.
9. tvpi ≈ dpi + rvpi (to within rounding). Flag if it doesn't.

WORKBOOK SHEETS (every non-empty cell, format R<row> C<col>="<value>")
=====================================================================
{sheets_text}

FUND LPA TERMS (from the Scheme model)
======================================
{lpa_block}

AS-OF DATE: {as_of_date}

CANONICAL METRIC DEFINITIONS
============================
{metric_defs}

REQUIRED OUTPUT
===============
Return STRICT JSON only (no markdown fences). For each metric you can
compute, include an entry. For metrics you CANNOT compute, EITHER omit
them OR return them with value=null + reason. Schema:

{{
  "metrics": {{
    "<metric_key>": {{
      "value": <number> | null,
      "source_cells": ["SHEET!R<row>C<col>", ...],
      "formula_used": "<plain-text formula>",
      "confidence": <float 0..1>,
      "reasoning": "<1-3 sentences>"
    }},
    ...
  }},
  "sheets_used": ["<sheet name>", ...],
  "overall_reasoning": "<3-5 sentences summarising how the workbook reports its fund metrics>"
}}

METRICS TO COMPUTE (return entries keyed by these exact strings):
  moic, tvpi, dpi, rvpi, net_irr, gross_irr,
  nav, total_unrealised_fair_value, total_realised_proceeds,
  total_distributions, total_called_capital, total_committed_capital,
  return_of_capital_amount, preferred_return_amount, gp_catchup_amount,
  carry_base, carry_amount_gross, gp_clawback_provision, carry_amount_net,
  lp_total_return, gp_total_distribution, total_proceeds_available.
"""

    primary_label = (
        relevant_sheets[0] if len(relevant_sheets) == 1
        else f'{relevant_sheets[0]}+{len(relevant_sheets)-1}more'
    )
    try:
        result = _call_gemini(
            prompt, context_label=f'Pass9-unified-{primary_label}'
        )
    except Exception as e:
        logger.error(
            f'Pass 9 compute_fund_metrics_unified failed: '
            f'{type(e).__name__}: {e}'
        )
        raise

    if not isinstance(result, dict):
        logger.warning('Pass 9 returned non-dict response')
        return {'metrics': {}, 'sheets_used': relevant_sheets,
                'overall_reasoning': 'Non-dict response.'}

    raw_metrics = result.get('metrics') or {}
    cleaned = {}
    for key in PASS9_METRIC_KEYS:
        entry = raw_metrics.get(key)
        if not isinstance(entry, dict):
            continue
        val = entry.get('value')
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        cleaned[key] = {
            'value': val,
            'source_cells': entry.get('source_cells') or [],
            'formula_used': str(entry.get('formula_used') or '')[:1000],
            'confidence': float(entry.get('confidence') or 0.0),
            'reasoning': str(entry.get('reasoning') or '')[:2000],
        }

    # Python-side identity guards. These are not heuristics — they are
    # arithmetic facts that hold for every fund regardless of workbook
    # format. We do NOT silently rewrite Gemini's value; we backfill ONLY
    # when Gemini reported BOTH sides of the identity and they disagree.

    # Identity 1: carry_amount_net == carry_amount_gross − gp_clawback_provision
    g = cleaned.get('carry_amount_gross', {}).get('value')
    c = cleaned.get('gp_clawback_provision', {}).get('value')
    n = cleaned.get('carry_amount_net', {}).get('value')
    if g is not None and c is not None:
        implied_net = g - c
        if n is None or abs(n - implied_net) > max(1.0, abs(implied_net) * 0.01):
            logger.warning(
                f'Pass 9 carry_amount_net inconsistent '
                f'(Gemini said {n}, identity g-c={implied_net}). '
                f'Replacing with identity-derived value.'
            )
            cleaned['carry_amount_net'] = {
                'value': implied_net,
                'source_cells': (
                    cleaned.get('carry_amount_gross', {}).get('source_cells', [])
                    + cleaned.get('gp_clawback_provision', {}).get('source_cells', [])
                ),
                'formula_used': (
                    'carry_amount_gross − gp_clawback_provision '
                    '(Python identity backfill)'
                ),
                'confidence': min(
                    cleaned.get('carry_amount_gross', {}).get('confidence', 0.0),
                    cleaned.get('gp_clawback_provision', {}).get('confidence', 0.0),
                ),
                'reasoning': (
                    'Python re-derived net = gross − clawback after detecting '
                    'an inconsistency in Gemini-reported net.'
                ),
            }

    # Identity 2: net carry ≥ 0
    nn = cleaned.get('carry_amount_net', {}).get('value')
    if nn is not None and nn < 0:
        cleaned['carry_amount_net']['value'] = 0.0
        cleaned['carry_amount_net']['formula_used'] = (
            'max(carry_amount_net, 0) — physical clamp; net carry cannot be negative.'
        )

    # Identity 3: gp_clawback_provision ≥ 0
    cc = cleaned.get('gp_clawback_provision', {}).get('value')
    if cc is not None and cc < 0:
        cleaned['gp_clawback_provision']['value'] = 0.0

    out = {
        'metrics': cleaned,
        'sheets_used': result.get('sheets_used') or relevant_sheets,
        'overall_reasoning': str(result.get('overall_reasoning') or '')[:4000],
    }
    logger.info(
        f'[GEMINI Pass9] compute_fund_metrics_unified: returned '
        f'{len(cleaned)} of {len(PASS9_METRIC_KEYS)} metrics across '
        f'{len(out["sheets_used"])} sheet(s).'
    )
    for k, v in cleaned.items():
        logger.info(
            f'  [Pass9] {k} = {v["value"]} '
            f'(conf={v["confidence"]:.2f}, '
            f'src={v["source_cells"][:3]}, '
            f'formula="{v["formula_used"][:80]}")'
        )
    return out


# ---------------------------------------------------------------------------
# Pass 3.5 helper: source disambiguation when one canonical metric has many
# label-value candidates across the workbook
# ---------------------------------------------------------------------------

def select_authoritative_source(metric_key, metric_label, metric_description,
                                candidates):
    """Ask Gemini to pick the single most authoritative source for a metric.

    When Pass 3.5 finds multiple cells across the workbook that semantically
    match the same canonical metric (e.g. several rows labelled "MOIC" in
    different sheets with different values — one being a placeholder zero in
    a dashboard-bridge row, another being the real computed value in a
    summary table), this function asks Gemini to reason over the full set
    of candidates and pick the most authoritative one — or return null if
    none of them are plausible.

    No code-level heuristics filter the candidates (e.g. no "drop zeros"
    rule). Gemini decides based on sheet context, label phrasing, and the
    surrounding semantic story.

    Args:
        metric_key: canonical key, e.g. 'moic'
        metric_label: canonical label, e.g. 'MOIC'
        metric_description: canonical description from the catalogue
        candidates: list of dicts, each with keys:
            'label'         — the text in the label cell as it appears in Excel
            'value'         — the numeric value found beside the label
            'source_cell'   — 'SHEET_NAME!rowN' or 'SHEET_NAME!rowNcolM'
            'column_header' — (optional) text in the column header cell of
                              the same column in the same tabular section.
                              Empty string for free-form (non-tabular) rows.

    Returns:
        dict {
            'chosen_index':   int | None,   # 0-based index into candidates, or null
            'reasoning':      str,
            'confidence':     float,
        }
        On API failure, raises (caller can retry / fall back).
    """
    if not candidates:
        return {'chosen_index': None, 'reasoning': 'no candidates', 'confidence': 0.0}
    if len(candidates) == 1:
        return {'chosen_index': 0, 'reasoning': 'single candidate', 'confidence': 1.0}

    lines = []
    for i, c in enumerate(candidates):
        col_h = c.get('column_header') or ''
        col_part = f'  column_header="{col_h}"' if col_h else ''
        lines.append(
            f'  [{i}] source={c.get("source_cell", "?")}  '
            f'label="{c.get("label", "")}"{col_part}  '
            f'value={c.get("value")}'
        )
    candidates_block = '\n'.join(lines)

    prompt = SHARED_MISSION_PREAMBLE + f"""You are a CFO/CA with 20+ years of experience reading Indian AIF
(Alternative Investment Fund) Excel workbooks. The workbook below contains
MULTIPLE cells that all match the canonical fund-performance metric named
below. Your job is to pick the ONE cell whose value is the most
AUTHORITATIVE source for that metric — or return null if NONE of them are
plausible.

CANONICAL METRIC
================
key:         {metric_key}
label:       {metric_label}
description: {metric_description}

CANDIDATE CELLS (every cell across the workbook whose label semantically
matched this metric — for cells inside tabular sections, the column
header is also shown so you can pick the semantically correct column,
e.g. "GP Share" vs "LP Share" vs "Total" for a waterfall step row)
====================================================================
{candidates_block}

REASONING GUIDANCE
==================
- COLUMN_HEADER IS CRITICAL FOR TABULAR ROWS. When a row label like
  "GP Catch-Up" appears in a waterfall step table with columns
  ["LP Share","GP Share","Total Step","Cumulative","Balance Remaining"],
  the correct cell for canonical metric `gp_catchup_amount` is the
  "GP Share" column — NOT the LP Share (which would be 0 for a GP-only
  step). Read the column header alongside the label to pick the cell
  whose (label × column) semantics match the canonical metric.
- A dashboard-bridge / cross-reference row that simply quotes the value
  from another sheet is LESS authoritative than the originating
  computation row.
- A row inside a section explicitly titled "fund-level performance
  multiples" or equivalent is MORE authoritative than a row buried in a
  per-company table or a placeholder/template row.
- A value of zero, blank, or a value that contradicts the surrounding
  context is suspicious — but DO NOT mechanically reject zero values:
  use the label, column header, sheet name, and row position to judge
  whether the zero is a real reported value (e.g. LP Share of a
  GP-only catch-up step IS legitimately 0) or a placeholder.
- If two candidates are equally authoritative and agree on the value,
  pick the one whose sheet is the primary computation source (e.g.
  MOIC_TVPI_DPI for MOIC/TVPI/DPI; NAV_CALC for NAV; MASTER_INPUTS for
  fee terms; etc.).
- If you genuinely cannot tell which is authoritative AND the candidates
  disagree on value, return chosen_index = null so the system can
  derive the metric from first principles instead of trusting an
  ambiguous extracted value.

RETURN STRICT JSON ONLY (no markdown fences, no commentary outside JSON):
{{
  "chosen_index": <integer 0-based index into the candidate list, or null>,
  "reasoning":    "<1-3 sentence explanation of your choice>",
  "confidence":   <float 0.0 - 1.0>
}}
"""

    try:
        result = _call_gemini(
            prompt, context_label=f'Pass3.5-select-source-{metric_key}'
        )
        if not isinstance(result, dict):
            return {
                'chosen_index': None,
                'reasoning': 'Gemini returned non-dict response',
                'confidence': 0.0,
            }
        idx = result.get('chosen_index')
        if idx is not None:
            try:
                idx = int(idx)
                if idx < 0 or idx >= len(candidates):
                    idx = None
            except (TypeError, ValueError):
                idx = None
        out = {
            'chosen_index': idx,
            'reasoning': str(result.get('reasoning') or '').strip(),
            'confidence': float(result.get('confidence') or 0.0),
        }
        logger.info(
            f'[GEMINI Pass3.5] select_authoritative_source({metric_key}): '
            f'{len(candidates)} candidates -> chosen_index={out["chosen_index"]} '
            f'confidence={out["confidence"]}'
        )
        return out
    except Exception as e:
        logger.error(
            f'Gemini select_authoritative_source API call failed for '
            f'{metric_key}: {type(e).__name__}: {e}'
        )
        raise


# ---------------------------------------------------------------------------
# Pass 6: Per-row metric formula derivation
# ---------------------------------------------------------------------------

def derive_per_row_formulas(model_label, available_inputs, missing_fields,
                            sample_row_values):
    """Ask Gemini for RANKED candidate formulas to compute each missing per-row
    metric. Each row class (e.g. exited deals vs active deals) may have a
    different best formula; the evaluator tries candidates in rank order
    and the FIRST formula whose inputs are all present + non-null on a
    given row wins.

    This handles heterogeneous portfolios correctly: a precise
    row-class-specific formula (e.g. `exitevent__irr_pct` for exited
    deals) ranks above a more general fallback (e.g. CAGR
    `((valuation__fair_value_of_holding / total_invested) ** (1 /
    years_since_investment_date) - 1) * 100` for active deals).

    Args:
        model_label: e.g. 'investments.Investment'
        available_inputs: dict of {field_name: {description, unit, sample_value}}
            describing every field on a row that has a non-null value somewhere
            in the dataset. Gemini uses these as variable names in its formulas.
        missing_fields: list of {field_name: description} that need formulas.
        sample_row_values: list of 3 fully-populated sample rows (each a dict
            of field_name -> value) so Gemini can see the magnitude/relationship
            between fields.

    Returns:
        {field_name: {
            'candidate_formulas': [
                {
                  'rank': int,                # 1 = highest priority
                  'formula_expression': str,  # arithmetic over available_inputs
                  'inputs_required': [field_name, ...],
                  'applies_when': str,        # row-class description (human)
                  'confidence': float,
                },
                ...
            ],
            'reasoning': str,
        }}
        Empty dict if Gemini decides no formula is computable.

    Raises on API errors (caller handles outer retry).
    """
    if not missing_fields:
        return {}

    inputs_block = '\n'.join(
        f'  - {k}: {meta.get("description", "")} '
        f'(sample={meta.get("sample_value", "?")}, '
        f'unit={meta.get("unit", "auto")})'
        for k, meta in available_inputs.items()
    )

    missing_block = '\n'.join(
        f'  - {k}: {meta.get("description", k)} '
        f'(unit={meta.get("unit", "auto")})'
        for k, meta in missing_fields.items()
    )

    samples_block = '\n'.join(
        f'  Row {i+1}: {row}' for i, row in enumerate(sample_row_values[:3])
    )

    prompt = SHARED_MISSION_PREAMBLE + f"""You are a CFO/CA with 20+ years of experience in Alternative Investment
Fund accounting and Private Equity / Venture Capital metrics. The dashboard
shows ONE ROW PER ENTITY for this model — e.g. one row per portfolio company
investment. For SOME rows, certain fields are NULL because the Excel did not
have those columns. Your job is to provide a RANKED LIST of formulas that
derive each missing field from other AVAILABLE fields on the SAME row.

MODEL: {model_label}

AVAILABLE PER-ROW INPUTS (fields that are populated on at least some rows;
these are the variable names you should reference in your formulas):
{inputs_block}

MISSING PER-ROW FIELDS (provide formula candidates for each):
{missing_block}

SAMPLE ROWS (so you can see realistic magnitudes and relationships):
{samples_block}

==================================================================
CRITICAL — HETEROGENEOUS ROW CLASSES
==================================================================
Different rows of the same model often belong to DIFFERENT real-world
classes that expose DIFFERENT inputs:

  - Exited investments expose `exitevent__*` fields (e.g.
    `exitevent__irr_pct`, `exitevent__proceeds`) but active ones do NOT.
  - Active investments expose latest `valuation__*` fields but
    fully-exited ones may not have a current valuation.
  - Written-off rows have `write_off_date` set but `valuation__*` may be
    zero or null.

A SINGLE formula CANNOT serve all rows in a heterogeneous portfolio.
You MUST return a RANKED list of candidate formulas per target field,
ordered from MOST PRECISE / row-class-specific (rank 1) down to MOST
GENERAL fallback (rank N). The Python evaluator will try them in order
on each row and pick the FIRST candidate whose declared inputs are all
PRESENT and NON-NULL for that row.

Concrete example for `irr_pct` on `investments.Investment`:

  Rank 1 — Exited deals: use the directly-reported exit IRR
      formula:      "exitevent__irr_pct"
      inputs:       ["exitevent__irr_pct"]
      applies_when: "Investment has a related ExitEvent (exited deals)"

  Rank 2 — Active deals: use CAGR on FV vs cost
      formula:      "((valuation__fair_value_of_holding / total_invested) ** (1 / years_since_investment_date) - 1) * 100"
      inputs:       ["valuation__fair_value_of_holding", "total_invested", "years_since_investment_date"]
      applies_when: "Active investment with a current valuation and holding period > 0"

This same pattern applies to every field: enumerate row classes, give
each its most precise formula, RANK them. Never rely on a single
row-class-specific formula — at least include a general fallback when
one is mathematically possible from the available inputs.

==================================================================
RULES (READ CAREFULLY)
==================================================================
1. Match SEMANTICALLY. The field names above are the actual Django column
   names; do NOT match by keyword. Use the description + sample values to
   reason about WHAT each input represents and which formula is appropriate.

2. Formulas must reference ONLY field names from the AVAILABLE list.
   Do NOT invent variables. For each candidate, list `inputs_required`
   verbatim — every variable referenced in `formula_expression` MUST
   appear in `inputs_required`.

3. Rank candidates from 1 (highest priority) onward. Higher-priority
   formulas should be those that are:
     (a) directly reported in the data (e.g. a pre-computed field) over
         a derivation that has more rounding;
     (b) row-class-specific and use inputs that are PRESENT on the
         intended row class; followed by
     (c) a UNIVERSAL fallback that uses inputs likely present on most
         rows.

4. INPUT NAMING CONVENTIONS YOU MUST UNDERSTAND:
   - Direct field on the row: `field_name` (e.g. `total_invested`).
   - Field on a related row: `<relation_name>__<field_name>` (e.g.
     `valuation__fair_value` means the latest related Valuation's
     fair_value). Use these freely as scalar inputs.
   - Pre-computed years helper: `years_since_<date_field>` is the number of
     years between that date and today, ALREADY COMPUTED as a float.
     Use these directly — do NOT try to subtract dates yourself (the safe
     evaluator does not support date arithmetic; raw date fields are
     non-numeric to it).
   - Examples of helpers you may see and use:
       years_since_investment_date  → years held since investment_date
       years_since_valuation__valuation_date  → years since the latest valuation

5. For IRR-class metrics (annualised return %): the universal CAGR
   formula is:
     `((value_end / value_start) ** (1 / years) - 1) * 100`
   Identify value_end and value_start semantically from the inputs (e.g.
   value_end = `valuation__fair_value_of_holding` for active rows or
   `exitevent__proceeds` for exited rows; value_start = `total_invested`).
   Use the appropriate `years_since_<date>` helper for the time period.

6. For multiple/ratio metrics: use the standard form `numerator / denominator`.

7. For percentage metrics: ensure the result is in the same scale as the
   field expects (e.g. 18.5 for 18.5%, NOT 0.185).

8. If NO formula is derivable from the available inputs, OMIT that field
   from the output. Do NOT invent values.

9. The Python safe AST evaluator supports: + - * / ** % () and the
   bare-name functions `max(...)`, `min(...)`, `abs(x)` (any arity
   ≥ 1 for max/min). It does NOT support attribute access (e.g.
   `.days`), conditional expressions, comparisons, or any other
   function calls. Build formulas using ONLY arithmetic on the
   numeric inputs provided, plus the three allowed functions.

10. CONFIDENCE: assign each candidate a confidence in [0, 1] reflecting
    how trustworthy that formula is for its target row class.

RETURN STRICT JSON ONLY (no markdown fences, no commentary outside JSON):
{{
  "<field_name>": {{
    "candidate_formulas": [
      {{
        "rank": 1,
        "formula_expression": "<arithmetic formula referencing only available field names>",
        "inputs_required": ["<field_name>", ...],
        "applies_when": "<which row class this targets>",
        "confidence": <float 0.0-1.0>
      }},
      {{
        "rank": 2,
        "formula_expression": "<...>",
        "inputs_required": ["<...>"],
        "applies_when": "<...>",
        "confidence": <float 0.0-1.0>
      }}
    ],
    "reasoning": "<1-3 sentence summary of why these candidates in this order>"
  }},
  ...
}}
"""

    try:
        result = _call_gemini(prompt, context_label=f'Pass6-rowformulas-{model_label}')
        if not isinstance(result, dict):
            logger.warning(
                f'Pass 6 Gemini returned non-dict for {model_label}'
            )
            return {}
        out = {}
        for k, v in result.items():
            if not isinstance(v, dict):
                continue

            # Normalise into the candidate_formulas shape, accepting both
            # the new ranked format and the legacy single-formula format
            # for backward compatibility.
            raw_candidates = v.get('candidate_formulas')
            if raw_candidates is None and 'formula_expression' in v:
                # Legacy single-formula shape — wrap as length-1 list
                raw_candidates = [{
                    'rank': 1,
                    'formula_expression': v.get('formula_expression', ''),
                    'inputs_required': v.get('inputs_required') or [],
                    'applies_when': v.get('applies_when') or v.get('reasoning') or '',
                    'confidence': float(v.get('confidence') or 0.0),
                }]
            if not isinstance(raw_candidates, list) or not raw_candidates:
                continue

            cleaned = []
            for c in raw_candidates:
                if not isinstance(c, dict):
                    continue
                formula = (c.get('formula_expression') or '').strip()
                if not formula:
                    continue
                cleaned.append({
                    'rank': int(c.get('rank') or (len(cleaned) + 1)),
                    'formula_expression': formula[:2000],
                    'inputs_required': c.get('inputs_required') or [],
                    'applies_when': (c.get('applies_when') or '')[:500],
                    'confidence': float(c.get('confidence') or 0.0),
                })
            if not cleaned:
                continue
            cleaned.sort(key=lambda c: c['rank'])
            out[k] = {
                'candidate_formulas': cleaned,
                'reasoning': (v.get('reasoning') or '')[:2000],
            }

        logger.info(
            f'[GEMINI Pass6] derive_per_row_formulas({model_label}): '
            f'{len(out)}/{len(missing_fields)} fields received formula sets '
            f'(total candidates: {sum(len(o["candidate_formulas"]) for o in out.values())})'
        )
        return out
    except Exception as e:
        logger.error(
            f'Pass 6 Gemini call failed for {model_label}: '
            f'{type(e).__name__}: {e}'
        )
        raise
