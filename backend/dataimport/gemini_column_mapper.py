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


def _extract_sheet_previews(filepath):
    """
    Read an Excel file and extract sheet names + first 5 rows of each sheet.
    Returns {sheet_name: [[row1_values], [row2_values], ...]}
    """
    wb = openpyxl.load_workbook(filepath, data_only=True, read_only=True)
    previews = {}
    sheet_names = wb.sheetnames

    for sheet_name in sheet_names:
        ws = wb[sheet_name]
        rows = []
        for i, row in enumerate(ws.iter_rows(max_row=6, values_only=True)):
            # Convert all values to strings for JSON serialization
            rows.append([str(v) if v is not None else '' for v in row])
            if i >= 5:
                break
        if rows:
            previews[sheet_name] = rows

    wb.close()
    return sheet_names, previews


# ---------------------------------------------------------------------------
# Pass 1: Sheet Classification
# ---------------------------------------------------------------------------

PASS1_PROMPT = """You are an expert fund management data analyst. Given the sheet names and first few rows of an AIF (Alternative Investment Fund) Excel workbook, classify each sheet into exactly one data domain.

Available domains and their descriptions:
{domains}

For each sheet, examine:
1. The sheet name itself
2. The header row(s) — look for section headers like "FUND MASTER DATA", "INVESTORS", "CAPITAL CALLS", etc.
3. The data content in sample rows

A single sheet may contain MULTIPLE sections (e.g., "Organization & Users" sheet has both organization master and user list). In that case, classify by the PRIMARY domain or list multiple domains.

IMPORTANT: Some sheets contain multiple sections separated by section headers (all-caps text like "FUND MASTER DATA", "SCHEMES", "PORTFOLIO COMPANIES"). Identify these multi-section sheets.

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

PASS2_PROMPT = """You are mapping Excel columns to canonical fund management database fields.

This sheet belongs to the domain: {domain}
Domain description: {domain_desc}

The sheet has these sections (identified by all-caps headers in the data):
{sections}

Excel data (first rows including headers):
{sheet_data}

Canonical fields for this domain (field_name: description):
{canonical_fields}

For EACH section in the sheet, map the Excel column headers to canonical field names.
Consider semantic meaning, not just exact text match. For example:
  - "LP Name" or "Investor" → investor_name
  - "Committed Amount" or "Commitment (Cr)" → commitment_amount
  - "SEBI Reg No" or "Registration Number" → sebi_registration_number
  - "P&L" or "Profit and Loss" or "Profit Analysis" → same semantic concept

Output JSON:
{{
  "sections": [
    {{
      "section_name": "SECTION HEADER or sheet_name if no sections",
      "header_row": 1,
      "data_start_row": 2,
      "mappings": [
        {{
          "excel_column": "exact Excel header text",
          "column_index": 1,
          "canonical_field": "canonical_field_name",
          "confidence": 0.95
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
"""


def map_columns_for_sheet(filepath, sheet_name, domains, sections, progress_cb=None):
    """
    Pass 2: For a classified sheet, map its columns to canonical fields.

    Returns: dict with section-level column mappings
    """
    wb = openpyxl.load_workbook(filepath, data_only=True, read_only=True)
    ws = wb[sheet_name]

    # Read more rows for mapping (up to 20 for context)
    rows = []
    for i, row in enumerate(ws.iter_rows(max_row=20, values_only=True)):
        rows.append([str(v) if v is not None else '' for v in row])
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
