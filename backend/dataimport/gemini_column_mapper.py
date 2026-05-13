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
from typing import Optional

import openpyxl
import google.generativeai as genai
from django.conf import settings

from .canonical_schema import SHEET_DOMAINS, DOMAIN_FIELDS

logger = logging.getLogger(__name__)

_configured = False


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


def _extract_sheet_previews(filepath):
    """
    Read an Excel file and extract sheet names + first 5 rows of each sheet.

    Uses data_only=True to get cached formula values, then resolves any cells
    that have cross-sheet formula references (e.g. ='Portfolio'!B10) so that
    Gemini sees the actual values rather than blanks.

    Returns {sheet_name: [[row1_values], [row2_values], ...]}
    """
    # Build cross-sheet value cache first (resolves =SheetX!CellRef formulas)
    xsheet_cache = _build_cross_sheet_value_cache(filepath)

    wb = openpyxl.load_workbook(filepath, data_only=True, read_only=True)
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

PASS1_PROMPT = """You are an AI engineer with 20+ years of experience in automating the finances of companies. You hold 20+ years of experience working with Python, and specialization in extraction, displaying and calculating data and accessing it from Excel/CSV/PDF sheets of multiple formats. You hold 15+ years of hands-on experience in software debugging and creating production-ready softwares and dashboards. You have robust knowledge of a CFO/CA to perform calculations on finance data.

Given the sheet names and first few rows of an AIF (Alternative Investment Fund) Excel workbook, classify each sheet into exactly one data domain.

Available domains and their descriptions:
{domains}

For each sheet, examine:
1. The sheet name itself
2. The header row(s) — look for section headers like "FUND MASTER DATA", "INVESTORS", "CAPITAL CALLS", etc.
3. The data content in sample rows

A single sheet may contain MULTIPLE sections (e.g., "Organization & Users" sheet has both organization master and user list). In that case, classify by the PRIMARY domain or list multiple domains.

IMPORTANT: Some sheets contain multiple sections separated by section headers (all-caps text like "FUND MASTER DATA", "SCHEMES", "PORTFOLIO COMPANIES"). Identify these multi-section sheets.

CROSS-SHEET LINKING — CRITICAL UNDERSTANDING:
Excel workbooks used by fund managers frequently contain cross-sheet cell references. A cell in one sheet may reference data from another sheet using formulas like:
  - =Sheet2!B5  (simple reference)
  - ='Portfolio Companies'!C10  (sheet name with spaces)
  - =VLOOKUP(A2,'Fund Data'!A:D,2,0)  (lookup from another sheet)

When you see cells showing empty values or '#REF!' or formula text, the ACTUAL value may exist in another sheet. The system has already resolved cross-sheet references before sending you this preview, so values shown reflect the true data. If you encounter empty cells in what appears to be a data area, assume those cells may be linked and classified accordingly.

CRITICAL RULES — NEVER VIOLATE:

1. COVER/SUMMARY SHEETS ARE NEVER DATA SHEETS.
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
   ALWAYS the dedicated data sheets (e.g., "Portfolio Investments" sheet
   for company/investment data).

2. DERIVE COUNTS FROM DATA SHEETS, NOT COVER SHEETS.
   If a Cover sheet says "Portfolio Companies: 13" but the "Portfolio
   Investments" sheet has 110 rows — the correct count is 110.
   Always trust the data sheet row count over any aggregate shown on
   a cover or summary page.

3. A sheet that has a two-column key-value layout (col A = label, col B = value)
   where labels are things like "Fund Name", "Short Code", "Vintage Year",
   "Management Fee", "Hurdle Rate", "Carried Interest", "Domicile" etc.
   is a METADATA sheet, not a data/transaction sheet.

4. FINANCIAL STATEMENT SHEETS (P&L, Budget vs Actual, Balance Sheet):
   Sheets with names like "Monthly P&L", "P&L", "Profit Loss", "Income Statement",
   "Budget vs Actual", "BvA", "Financial Statements", "Company Financials",
   "Balance Sheet", "Cash Flow" belong to the "financials_pl_bva" domain.
   These sheets contain company-level financial data (Revenue, EBITDA, PAT etc.)
   for portfolio companies — either one row per company or time-series pivot format.

Sheet data:
{sheet_data}

Respond with a JSON object:
{{
  "sheets": [
    {{
      "sheet_name": "exact sheet name",
      "domains": ["primary_domain", "secondary_domain_if_any"],
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

    model = _get_model()
    response = model.generate_content(prompt)
    result = _parse_json_response(response.text)

    if progress_cb:
        progress_cb(12, 'Sheet classification complete')

    return result.get('sheets', []), sheet_names


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


def map_columns_for_sheet(filepath, sheet_name, domains, sections, progress_cb=None):
    """
    Pass 2: For a classified sheet, map its columns to canonical fields.

    Uses the cross-sheet value cache so that formula-linked cells (e.g.
    ='Portfolio'!B10) are resolved to their actual values before sending
    to Gemini — preventing blank cells from confusing the AI column mapper.

    Returns: dict with section-level column mappings
    """
    # Build cross-sheet cache for this file (resolves =SheetX!CellRef formulas)
    xsheet_cache = _build_cross_sheet_value_cache(filepath)

    wb = openpyxl.load_workbook(filepath, data_only=True, read_only=True)
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

    model = _get_model()
    response = model.generate_content(prompt)
    result = _parse_json_response(response.text)

    return result


# ---------------------------------------------------------------------------
# Main entry point: full two-pass mapping
# ---------------------------------------------------------------------------

def map_workbook_columns(filepath, progress_cb=None):
    """
    Full two-pass Gemini column mapping for a fund Excel file.

    Args:
        filepath: Path to the .xlsx file
        progress_cb: Optional callable(pct: int, message: str)

    Returns:
        {
            'sheet_classifications': [...],
            'column_mappings': {sheet_name: mapping_result},
            'overall_confidence': float,
            'sheet_names': [...]
        }
    """
    # Pass 1: Classify sheets
    classifications, sheet_names = classify_sheets(filepath, progress_cb)

    if progress_cb:
        progress_cb(15, 'Mapping columns with AI...')

    # Pass 2: Map columns for each classified sheet
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
                filepath, sheet_name, domains, sections, progress_cb
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

    avg_confidence = total_confidence / mapped_count if mapped_count > 0 else 0.0

    if progress_cb:
        progress_cb(25, 'Column mapping complete')

    return {
        'sheet_classifications': classifications,
        'column_mappings': column_mappings,
        'overall_confidence': round(avg_confidence, 2),
        'sheet_names': sheet_names,
    }
