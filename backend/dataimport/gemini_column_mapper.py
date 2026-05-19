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
            response = model.generate_content(
                prompt,
                request_options={'timeout': 60},
            )

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

    options_text = '\n'.join(
        f'  "{k}": {v}' for k, v in canonical_options.items()
    )
    labels_text = '\n'.join(f'  - "{l}"' for l in unique_labels)
    context_line = f'\nDOMAIN CONTEXT: {context}\n' if context else ''

    prompt = f"""You are a financial data classifier for an Alternative Investment Fund (AIF) Excel file.
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

    prompt = f"""You are a financial data classifier for an Alternative Investment Fund (AIF) Excel file.

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
    pairs_text = '\n'.join(
        f'  "{l}" → "{v}"' for l, v in filtered[:100]
    )
    context_line = f'\nDOMAIN CONTEXT: {context}\n' if context else ''

    prompt = f"""You are a financial data extractor for an Alternative Investment Fund (AIF) Excel file.
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

    prompt = f"""Detect the currency and numeric unit used in this financial spreadsheet sheet.

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


def _extract_sheet_previews(filepath):
    """
    Read an Excel file and extract sheet names + first 5 rows of each sheet.

    Uses data_only=True to get cached formula values, then resolves any cells
    that have cross-sheet formula references (e.g. ='Portfolio'!B10) so that
    Gemini sees the actual values rather than blanks.

    IMPORTANT: Do NOT use read_only=True here. In read_only mode, openpyxl
    returns EmptyCell objects for empty cells — these lack .row and .column
    attributes, causing AttributeError crashes when we look up the xsheet_cache.
    We only read max_row=6 per sheet, so memory/performance is not a concern.

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
        for i, row in enumerate(ws.iter_rows(max_row=6)):
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

PASS1_PROMPT = """You are an AI engineer with 20+ years of experience in automating the finances of companies, specializing in Alternative Investment Funds (AIFs), Private Equity, and Venture Capital fund operations. You hold 25+ years of experience as a CA/CFO with deep knowledge of fund accounting, LP/GP economics, capital calls, distributions, carried interest, NAV calculation, and SEBI regulatory compliance for Indian AIFs.

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

PASS1_5_PROMPT = """You are an AI engineer with 20+ years of experience in Alternative Investment Funds (AIFs), Private Equity, and Venture Capital fund operations across multiple countries. You hold deep expertise in fund accounting, LP/GP economics, capital calls, distributions, carried interest, NAV calculation, and regulatory compliance (SEBI for India, SEC for US, FCA for UK, MAS for Singapore, CSSF for Luxembourg).

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

NAV sections:
  "NAV RECORDS", "NAV HISTORY", "NET ASSET VALUE", "MONTHLY NAV" → nav_records

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

HOW TO CLASSIFY — USE BOTH SECTION NAME AND COLUMN HEADERS:

1. First, look at the section name for semantic meaning
2. Then, look at the column headers to CONFIRM the classification:
   - Columns like Company Name, Sector, Stage, City → portfolio_companies
   - Columns like Instrument, Cost, Fair Value, Ownership% → investments
   - Columns like Tranche#, Amount, Date, Round, Price/Share → investment_tranches
   - Columns like Call#, Call Date, Call%, Total Amount → capital_call_headers
   - Columns like Investor Name, Called Amount, Payment Status → capital_call_line_items
   - Columns like Exit Type, Exit Date, Proceeds, MOIC → exit_events
   - Columns like Distribution#, Dist Date, Gross Amount, TDS → distributions
   - Columns like NAV Date, Total NAV, NAV/Unit, Units → nav_records
3. If the section name is ambiguous, let the COLUMN HEADERS decide
4. If column headers are not provided (empty), classify by section name + parent domain

CRITICAL RULES:
1. "__default__" means the sheet has NO section headers (entire sheet is one flat table).
   Classify based on columns + parent domain:
   - parent=portfolio_investments + columns have Cost/FV → investments
   - parent=capital_calls → capital_call_headers
   - parent=nav_accounting → nav_records
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
            cols_str = ', '.join(sec.get('columns', [])[:15]) or '(no columns detected)'
            section_data_parts.append(
                f'  Section: "{sec["name"]}"\n    Columns: {cols_str}'
            )

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

PASS2_PROMPT = """You are an AI engineer with 20+ years of experience in automating the finances of companies. You hold 20+ years of experience working with Python, and specialization in extraction, displaying and calculating data and accessing it from Excel/CSV/PDF sheets of multiple formats. You hold 15+ years of hands-on experience in software debugging and creating production-ready softwares and dashboards. You have robust knowledge of a CFO/CA to perform calculations on finance data.

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
                          progress_cb=None, xsheet_cache=None, wb=None):
    """
    Pass 2: For a classified sheet, map its columns to canonical fields.

    Uses the cross-sheet value cache so that formula-linked cells (e.g.
    ='Portfolio'!B10) are resolved to their actual values before sending
    to Gemini — preventing blank cells from confusing the AI column mapper.

    Args:
        xsheet_cache: Pre-built cross-sheet cache (optional; built if None)
        wb: Pre-opened workbook (optional; opened if None)

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

    # Read more rows for mapping (up to 20 for context), resolving cross-sheet refs
    rows = []
    for i, row in enumerate(ws.iter_rows(max_row=20)):
        row_vals = []
        for cell in row:
            val = xsheet_cache.get((sheet_name, cell.row, cell.column), cell.value)
            row_vals.append(str(val) if val is not None else '')
        rows.append(row_vals)
        if i >= 19:
            break

    if close_wb:
        wb.close()

    if not rows:
        return {'sections': [], 'overall_confidence': 0.0}

    # Use primary domain
    primary_domain = domains[0] if domains else 'unknown'
    if primary_domain == 'unknown' or primary_domain not in DOMAIN_FIELDS:
        return {'sections': [], 'overall_confidence': 0.0}

    # Build canonical fields description
    fields = DOMAIN_FIELDS[primary_domain]
    fields_desc = '\n'.join(f'  - {k}: {v}' for k, v in fields.items())

    # Build sheet data preview
    sheet_data_parts = []
    for i, row in enumerate(rows):
        non_empty = [v for v in row if v]
        if non_empty:
            sheet_data_parts.append(f'  Row {i+1}: {non_empty}')

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

    Returns a list of dicts: [{name: str, columns: [str, ...]}]
    where 'name' is the section title text (or '__default__' for the first
    flat-table region with no section header) and 'columns' are the column
    headers found immediately after that section title.

    Detection is 100% format-agnostic — no keywords. A section title row is
    identified by:
      - 1-2 non-empty cells in the row
      - First cell text is predominantly uppercase (≥70% of alpha chars)
      - Text length > 3 characters
    """
    max_row = ws.max_row or 0
    max_col = ws.max_column or 0
    sections = []
    seen_section = False

    def _get_columns_from_header_row(start_r):
        """Scan rows starting at start_r to find a header row (≥3 cells)."""
        for scan_r in range(start_r, min(start_r + 8, max_row + 1)):
            cells = []
            for c in range(1, max_col + 1):
                v = ws.cell(scan_r, c).value
                if v is not None:
                    cells.append(str(v).strip())
            if len(cells) >= 3:
                return cells[:15]  # cap at 15 columns for prompt size
        return []

    r = 1
    while r <= max_row:
        # Count non-empty cells
        cell_vals = []
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v is not None:
                cell_vals.append(str(v).strip())

        if not cell_vals:
            r += 1
            continue

        first_str = cell_vals[0]

        # Check if this row is a section title: 1-2 cells, mostly uppercase
        if len(cell_vals) <= 2 and len(first_str) > 3:
            alpha_chars = [ch for ch in first_str if ch.isalpha()]
            upper_ratio = (
                sum(1 for ch in alpha_chars if ch.isupper()) / len(alpha_chars)
                if alpha_chars else 0.0
            )
            if upper_ratio >= 0.70:
                # This is a section title row
                cols = _get_columns_from_header_row(r + 1)
                sections.append({'name': first_str.strip(), 'columns': cols})
                seen_section = True
                r += 1
                continue

        # If no section header seen yet and this row has ≥3 cells, it's a
        # flat header row → __default__ section
        if not seen_section and len(cell_vals) >= 3:
            sections.append({'name': '__default__', 'columns': cell_vals[:15]})
            seen_section = True
            # Skip past data rows to look for more sections
            r += 1
            continue

        r += 1

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

    for i, sheet_cls in enumerate(classifications):
        sheet_name = sheet_cls.get('sheet_name', '')
        domains = sheet_cls.get('domains', [])
        sections = sheet_cls.get('sections', [])
        cls_confidence = sheet_cls.get('confidence', 0.0)

        if not domains or domains == ['unknown']:
            continue

        if progress_cb:
            pct = 15 + int((i / max(len(classifications), 1)) * 10)
            progress_cb(pct, f'Mapping columns: {sheet_name}...')

        try:
            mapping = map_columns_for_sheet(
                filepath, sheet_name, domains, sections, progress_cb,
                xsheet_cache=xsheet_cache, wb=wb_pass2,
            )
            column_mappings[sheet_name] = {
                'domains': domains,
                'sections_from_classification': sections,
                **mapping,
            }
            overall_conf = mapping.get('overall_confidence', cls_confidence)
            total_confidence += overall_conf
            mapped_count += 1
        except Exception as e:
            logger.warning(f'Column mapping failed for sheet "{sheet_name}": {e}')
            column_mappings[sheet_name] = {
                'domains': domains,
                'error': str(e),
                'overall_confidence': 0.0,
            }

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
