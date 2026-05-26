"""
FundImportService — orchestrates the import of a single fund Excel file.

Uses Gemini AI exclusively for all sheet classification and column mapping.
No hardcoded keywords, sheet names, or fallback scanning.

Pipeline:
  1. Gemini Pass 1: classify each sheet → domain_map (1:many, 19 domains)
  2. Gemini Pass 2: map column headers → canonical field names
  3. Each _import_* method uses _dm_sheets(domain_map, domain) to find its sheets
  4. Row reading via read_all_sections_from_sheet() + _find_col() fuzzy matching
"""

import calendar
import logging
import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation

import openpyxl
from django.db import transaction
from django.db.models import Q
from django.utils.text import slugify

from accounts.models import Organization, User, FundAccess
from funds.models import FundCategory, Entity, Fund, Scheme
from lp.models import (BankAccount, Investor, Commitment, CapitalCall,
                        CapitalCallLineItem, Distribution, DistributionLineItem,
                        LPCapitalAccount)
from investments.models import (PortfolioCompany, Investment, InvestmentTranche,
                                 Valuation, KPIDefinition, PortfolioKPI,
                                 CompanyFinancials, ExitEvent, BoardMeeting)
from accounting.models import (ChartOfAccounts, NAVRecord, CarriedInterest,
                                FundLedger, ManagementFeeSchedule)
from portfolio.models import PortfolioSnapshot, PortfolioNode

try:
    from compliance.models import (SEBIReport, AMLDueDiligence,
                                    ComplianceCalendar, ComplianceTestReport,
                                    CTRChecklistItem, PPMAmendment,
                                    SEBICircular, CircularAction)
    HAS_COMPLIANCE = True
except ImportError:
    HAS_COMPLIANCE = False

from .gemini_column_mapper import map_workbook_columns

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

CATEGORY_MAP = {
    'CAT_I_VCF': ('Category I AIF', 'Venture Capital Fund', False),
    'CAT_II': ('Category II AIF', 'Private Equity Fund', False),
    'CAT_III_LVF': ('Category III AIF', 'Long-Short Equity Fund', True),
}




def _d(val, default=None):
    if val is None or val == '' or val == 'None':
        return default
    # Excel date serial corruption: openpyxl reads numeric cells with date-format
    # as datetime/date/time objects instead of their plain numeric value.
    # Recover the original Excel serial number so that amounts like "800 Cr"
    # stored in a date-formatted cell are not silently dropped as None.
    from datetime import datetime as _dt, date as _date_cls, time as _time_cls
    if isinstance(val, _dt):
        # datetime → Excel serial (days from 1899-12-31) + fractional day
        _epoch = _date_cls(1899, 12, 31)
        _days = (val.date() - _epoch).days
        _frac = (val.hour * 3600 + val.minute * 60 + val.second
                 + val.microsecond / 1_000_000) / 86400
        return Decimal(str(round(_days + _frac, 6)))
    if isinstance(val, _date_cls):
        _epoch = _date_cls(1899, 12, 31)
        return Decimal(str((val - _epoch).days))
    if isinstance(val, _time_cls):
        # time values are fractions stored as HH:MM:SS (e.g. 0.897 → 21:31:06)
        _frac = (val.hour * 3600 + val.minute * 60 + val.second
                 + val.microsecond / 1_000_000) / 86400
        return Decimal(str(round(_frac, 6)))
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError):
        return default


def _date(val):
    if val is None:
        return None
    if hasattr(val, 'date'):
        return val.date()
    if isinstance(val, date):
        return val
    # Try string parsing
    if isinstance(val, str):
        val = val.strip()
        for fmt in (
            '%Y-%m-%d',       # 2022-01-18
            '%d-%m-%Y',       # 18-01-2022
            '%d/%m/%Y',       # 18/01/2022
            '%m/%d/%Y',       # 01/18/2022
            '%d-%b-%Y',       # 18-Jan-2022  ← DD-MMM-YYYY (most Excel files)
            '%d %b %Y',       # 18 Jan 2022
            '%d/%b/%Y',       # 18/Jan/2022
            '%b %d, %Y',      # Jan 18, 2022
            '%d-%B-%Y',       # 18-January-2022
            '%d %B %Y',       # 18 January 2022
            '%d.%m.%Y',       # 18.01.2022
            '%Y/%m/%d',       # 2022/01/18
            '%d-%b-%y',       # 31-Mar-25  ← 2-digit year, common in Indian AIF files
            '%d/%m/%y',       # 31/03/25
            '%d.%m.%y',       # 31.03.25
            '%d %b %y',       # 31 Mar 25
        ):
            try:
                from datetime import datetime
                return datetime.strptime(val, fmt).date()
            except ValueError:
                continue
    return None


def _str(val, default=''):
    if val is None:
        return default
    return str(val).strip()


def _bool(val):
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in ('yes', 'true', '1')


# Sheet names that are NEVER data sheets — they are summaries/covers/indices.
# No data import function should read companies, investments, or any
# transactional data from these sheets. Source of truth is always the
# dedicated data sheets (Portfolio Investments, Investors & LPs, etc.).
def _is_cover_or_summary_sheet(sheet_name, ws=None):
    """Return True if this sheet name indicates a cover/summary page.

    Uses layout-based detection (no hardcoded keywords):
    - Very few non-empty rows → likely a cover page
    - No row with 4+ non-empty cells (no tabular data) → likely a cover page

    Cover sheets may have fund statistics (company count, total FV, etc.)
    that look like real data but are just display aggregates — often
    computed by hand and prone to errors. We always derive statistics
    from the actual data sheets instead.
    """
    if ws is None:
        return False
    max_row = ws.max_row or 0
    max_col = ws.max_column or 0
    if max_row < 3:
        return True
    non_empty_rows = 0
    has_tabular_row = False
    scan_limit = min(max_row + 1, 30)
    for r in range(1, scan_limit):
        cell_count = sum(
            1 for c in range(1, min(max_col + 1, 20))
            if ws.cell(r, c).value is not None
        )
        if cell_count >= 1:
            non_empty_rows += 1
        if cell_count >= 4:
            has_tabular_row = True
            break
    if non_empty_rows < 5 and not has_tabular_row:
        return True
    return False


def _is_section_header(val):
    """Return True if val looks like a section header — layout-only detection.

    A section header is a string that is:
      - Predominantly uppercase (≥70% of alpha chars)
      - Longer than 3 characters
      - Contains at least one space (multi-word title)
    No hardcoded keywords — purely format-based.
    """
    if not val:
        return False
    s = str(val).strip()
    if len(s) <= 3 or ' ' not in s:
        return False
    alpha_chars = [ch for ch in s if ch.isalpha()]
    if not alpha_chars:
        return False
    upper_ratio = sum(1 for ch in alpha_chars if ch.isupper()) / len(alpha_chars)
    return upper_ratio >= 0.70


def _is_header_row(val):
    """Check if a cell looks like a header row marker (e.g., '#' or 'S.No')."""
    if not val:
        return False
    s = str(val).strip()
    return s in ('#', 'S.No', 'Sr', 'Sr.', 'SNo', 'S.No.')


# Prefixes that identify non-data rows masquerading as data:
# subtotal lines, grand-total lines, repeated header rows, separator labels.
def _is_junk_row(name):
    """Return True if *name* is obviously junk based on format alone.

    Format-based checks (universal, language-independent):
      - Empty / None / whitespace-only
      - Dash/placeholder characters: —, –, -, #, *
      - Purely numeric: serial numbers, counts, aggregated values

    For semantic junk detection (subtotals, totals, headers, notes),
    use FundImportService._classify_junk_names() which classifies via Gemini.
    """
    if not name:
        return True
    n = str(name).strip()
    if not n or n in ('—', '–', '-', '#', '*'):
        return True
    # Purely numeric (serial number, count, or aggregated value in name col)
    try:
        float(n.replace(',', '').replace(' ', ''))
        return True
    except ValueError:
        pass
    return False


def find_section_rows(ws, section_name):
    for r in range(1, ws.max_row + 1):
        val = ws.cell(r, 1).value
        if val and section_name in str(val):
            return r
    return None


def read_table(ws, start_row=1, max_rows=None):
    """Read rows from a worksheet starting at a header row.
    Returns list of dicts keyed by header names."""
    header_row = start_row
    for r in range(start_row, min(ws.max_row + 1, start_row + 10)):
        val = ws.cell(r, 1).value
        if val and not _is_section_header(val):
            header_row = r
            break
        elif val and _is_section_header(val) and r == start_row:
            continue

    headers = []
    for c in range(1, ws.max_column + 1):
        h = ws.cell(header_row, c).value
        if h:
            headers.append((c, str(h).strip()))

    if not headers:
        return []

    rows = []
    for r in range(header_row + 1, ws.max_row + 1):
        if max_rows and len(rows) >= max_rows:
            break
        row_data = {}
        all_empty = True
        for col, name in headers:
            val = ws.cell(r, col).value
            if val is not None:
                all_empty = False
            row_data[name] = val
        if all_empty:
            break
        first_val = ws.cell(r, 1).value
        if _is_section_header(first_val):
            break
        rows.append(row_data)

    return rows


def _is_section_title_row(ws, r, max_col):
    """Return (True, title_text) if row r looks like a section title.

    100% format-agnostic layout detection — no hardcoded keywords.
    A section title row is identified by:
      - 1-2 non-empty cells in the row (not a data row with many columns)
      - First cell text is predominantly uppercase (≥70% of alpha chars)
      - Text length > 3 characters

    Also handles pipe-delimited multi-cell titles (3+ cells) where the first
    cell is predominantly uppercase (≥70%) — these are section headers with
    metadata columns appended (e.g. "PORTFOLIO INVESTMENTS | Fund Name | Date").
    """
    first_cell = ws.cell(r, 1).value
    if first_cell is None:
        return False, ''
    first_str = str(first_cell).strip()
    if not first_str or len(first_str) <= 3:
        return False, ''

    # Compute uppercase ratio of first cell
    alpha_chars = [ch for ch in first_str if ch.isalpha()]
    if not alpha_chars:
        return False, ''
    upper_ratio = sum(1 for ch in alpha_chars if ch.isupper()) / len(alpha_chars)
    if upper_ratio < 0.70:
        return False, ''

    # Count non-empty cells in the row
    cell_count = sum(1 for c in range(1, max_col + 1)
                     if ws.cell(r, c).value is not None)

    # Classic section header: 1-2 cells, predominantly uppercase
    if cell_count <= 2:
        return True, first_str

    # Pipe-delimited multi-cell title: first cell is uppercase title,
    # remaining cells are metadata. Only if first cell has a space
    # (multi-word title, not a single-word data value).
    if cell_count >= 3 and ' ' in first_str:
        return True, first_str

    return False, ''


def read_table_from_sheet(ws, skip_title_rows=1, alias_map=None):
    """Read a full sheet as a table, skipping initial title/section-header rows.

    Finds the header row (first row with multiple non-empty cells that looks
    like column headers), then reads all data rows below it.

    Tolerates up to 2 consecutive blank rows inside the data (some Excel files
    have blank separator rows between company groups within the same table).

    alias_map: optional {excel_col_header: canonical_field_name} from Gemini.
    When supplied, each row dict is enriched with the canonical field name as an
    additional key (only if that key is not already present). This lets _find_col()
    match by the canonical name regardless of the original Excel column wording.

    Returns (headers_dict, rows) where headers_dict maps header_text → col_index,
    and rows is a list of dicts keyed by header text.
    """
    max_col = ws.max_column or 0
    header_row = None
    for r in range(1, min(ws.max_row + 1, 20)):
        cells = []
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v is not None:
                cells.append((c, str(v).strip()))
        # A header row has multiple cells and doesn't look like a title
        if len(cells) >= 3:
            first_val = cells[0][1] if cells else ''
            # Skip rows that are section/title rows
            is_title, _ = _is_section_title_row(ws, r, max_col)
            if is_title:
                continue
            if not (_is_section_header(first_val) and len(cells) < 5):
                header_row = r
                break

    if not header_row:
        return {}, []

    headers = {}
    for c in range(1, max_col + 1):
        h = ws.cell(header_row, c).value
        if h:
            headers[str(h).strip()] = c

    rows = []
    trailing_blanks = 0  # tracks consecutive blank rows at current tail
    for r in range(header_row + 1, ws.max_row + 1):
        row_data = {}
        all_empty = True
        for name, col in headers.items():
            val = ws.cell(r, col).value
            if val is not None:
                all_empty = False
            row_data[name] = val

        if all_empty:
            # Never terminate on blank rows — Excel files routinely have
            # 3-10+ blank separator rows between company groups within the
            # same table.  Stopping on blanks silently drops companies.
            # We only truly stop at a recognized section-title row or at
            # the sheet boundary.
            trailing_blanks += 1
            continue

        trailing_blanks = 0

        # Stop if we hit a new section header inside the data area
        is_title, _ = _is_section_title_row(ws, r, max_col)
        if is_title:
            break

        # Gemini alias enrichment: add canonical field names as additional
        # keys so that _find_col(row, 'total_invested') hits Pass-1 exact match
        # regardless of how the Excel column was actually labelled.
        if alias_map:
            for excel_col, canonical in alias_map.items():
                if excel_col in row_data and canonical not in row_data:
                    row_data[canonical] = row_data[excel_col]

        rows.append(row_data)

    return headers, rows


def read_all_sections_from_sheet(ws, alias_map=None):
    """Read ALL sections from a multi-section sheet.

    Many fund Excel files have sheets with multiple sections separated by
    section headers (all-caps text like "PORTFOLIO COMPANIES", "INVESTMENTS",
    "INVESTMENT TRANCHES") and empty rows.

    Also handles pipe-delimited multi-cell section titles such as:
        "PORTFOLIO INVESTMENTS — FULL REGISTER | Fund Name | DD-MMM-YYYY"
    where the first cell contains the domain keyword and remaining cells are
    contextual metadata.

    Tolerates up to 2 consecutive blank rows within a data section (some Excel
    files have blank rows separating company groups in the same table).

    alias_map: optional {excel_col: canonical_field} from Gemini. Applied to
    each row so that _find_col(row, 'canonical_name') always hits Pass-1 exact
    match regardless of the original Excel column wording.

    Returns: dict mapping section_name → (headers_dict, rows)
    where section_name is the all-caps header text (e.g., 'INVESTMENTS')
    and the default first section (if no header) is keyed as '__default__'.
    """
    sections = {}
    max_row = ws.max_row or 0
    max_col = ws.max_column or 0

    def _read_data_rows(header_row, headers):
        """Read data rows starting after header_row. Returns (rows, next_r).

        Termination policy (in priority order):
          1. A recognised section-title row  → stop, next_r = that row
          2. End of sheet                    → stop naturally
          3. Blank rows                      → SKIP, never stop

        Rationale: fund Excel files regularly use 3-10+ consecutive blank
        rows as visual separators between company sub-groups (e.g., between
        sector groups within the same PORTFOLIO INVESTMENTS section).
        Stopping on blanks silently truncates the company list.  The only
        reliable terminator is an ALL-CAPS section header that signals a
        genuinely different domain.
        """
        rows = []
        next_r = max_row + 1
        for data_r in range(header_row + 1, max_row + 1):
            row_data = {}
            all_empty = True
            for name, col in headers.items():
                val = ws.cell(data_r, col).value
                if val is not None:
                    all_empty = False
                row_data[name] = val

            if all_empty:
                continue  # skip blank row — never terminate on blanks

            # Stop if we encounter a new section header
            is_title, _ = _is_section_title_row(ws, data_r, max_col)
            if is_title:
                next_r = data_r
                break

            # Gemini alias enrichment: add canonical field names as keys
            if alias_map:
                for excel_col, canonical in alias_map.items():
                    if excel_col in row_data and canonical not in row_data:
                        row_data[canonical] = row_data[excel_col]

            rows.append(row_data)
        return rows, next_r

    r = 1
    while r <= max_row:
        # Count non-empty cells in this row
        cell_count = sum(1 for c in range(1, max_col + 1)
                         if ws.cell(r, c).value is not None)

        # Skip completely empty rows
        if cell_count == 0:
            r += 1
            continue

        # Detect section header (single/double cell or multi-cell pipe title)
        is_section, section_name = _is_section_title_row(ws, r, max_col)

        if is_section:
            # Next row should be the header row for this section
            r += 1
            # Find the header row (first row with 2+ non-empty cells)
            header_row = None
            for scan_r in range(r, min(r + 6, max_row + 1)):
                scan_count = sum(1 for c in range(1, max_col + 1)
                                 if ws.cell(scan_r, c).value is not None)
                # Skip blank rows between section title and header
                if scan_count == 0:
                    continue
                # Skip a note/subtitle row that looks like another title
                is_sub, _ = _is_section_title_row(ws, scan_r, max_col)
                if is_sub and scan_count <= 2:
                    continue
                if scan_count >= 2:
                    header_row = scan_r
                    break

            if not header_row:
                r += 1
                continue

            # Read headers
            headers = {}
            for c in range(1, max_col + 1):
                h = ws.cell(header_row, c).value
                if h:
                    headers[str(h).strip()] = c

            rows, r = _read_data_rows(header_row, headers)

            # Normalize section name for lookup
            norm_name = section_name.upper().strip()
            # Remove parenthetical details like "(Main Scheme — Call #1)"
            if '(' in norm_name:
                norm_name = norm_name[:norm_name.index('(')].strip()
            # Remove pipe-delimited metadata suffix if present
            if '|' in norm_name:
                norm_name = norm_name[:norm_name.index('|')].strip()
            # Remove em-dash suffixes like "— FULL REGISTER"
            for sep in [' — ', ' - ', '—', ' –']:
                if sep in norm_name:
                    norm_name = norm_name[:norm_name.index(sep)].strip()
                    break

            sections[norm_name] = (headers, rows)
            continue

        # Not a section header — this might be a direct header row
        # (sheet starts directly with data, no section header)
        if cell_count >= 3:
            headers = {}
            for c in range(1, max_col + 1):
                h = ws.cell(r, c).value
                if h:
                    headers[str(h).strip()] = c

            rows, r = _read_data_rows(r, headers)
            sections['__default__'] = (headers, rows)
            continue

        r += 1

    return sections


def _dm_sheets(domain_map, domain_key):
    """Return list of sheet names for a domain. Works with 1:many domain_map."""
    val = domain_map.get(domain_key, [])
    if isinstance(val, list):
        return val
    return [val] if val else []


def _dm_first(domain_map, domain_key):
    """Return the first (primary) sheet name for a domain, or None."""
    sheets = _dm_sheets(domain_map, domain_key)
    return sheets[0] if sheets else None


def _get_section_rows(ws, domain_map, domain_key, section_subdomains=None,
                      section_map=None):
    """Smart row reader: tries dedicated domain sheet first, then falls back
    to reading a specific section from a multi-section sheet using Gemini
    sub-domain classification.

    Returns (headers_dict, rows) like read_table_from_sheet.
    """
    sheet_name = _dm_first(domain_map, domain_key)
    if not sheet_name or sheet_name not in (ws.parent.sheetnames if hasattr(ws, 'parent') else []):
        return {}, []

    target_ws = ws.parent[sheet_name] if hasattr(ws, 'parent') else ws

    # First try reading as a flat table
    headers, rows = read_table_from_sheet(target_ws)

    if rows:
        return headers, rows

    # If no rows from flat table, try reading sections and matching by
    # Gemini sub-domain classification
    if section_subdomains and section_map:
        sections = read_all_sections_from_sheet(target_ws)
        sheet_secs = section_map.get(sheet_name, {})
        for sec_name, (sec_headers, sec_rows) in sections.items():
            mapped_subdomain = sheet_secs.get(sec_name, 'unknown')
            if mapped_subdomain in section_subdomains and sec_rows:
                return sec_headers, sec_rows

    return headers, rows


# ---------------------------------------------------------------------------
# Fuzzy header matching — finds a column by trying multiple possible names
# ---------------------------------------------------------------------------

def _find_col(row, *candidates):
    """Find the first matching header value in a row dict.

    Matching priority (most specific → least specific).
    We iterate ALL candidates at each pass before falling to the next pass —
    this ensures a more-specific candidate (e.g. 'Company Name') is found
    before a less-specific one (e.g. 'Name') even if 'Name' would match earlier
    in a looser pass.

    Pass 1   — Exact case-sensitive:       "Company Name" == "Company Name"
    Pass 1.5 — Normalised (space↔underscore, case-insensitive):
               "Company Name" matches "company_name" from Gemini alias map
    Pass 2   — Exact case-insensitive:     "company name" == "Company Name"
    Pass 3   — Key ends with candidate:    "Company Name" ends with "Name"
    Pass 4   — Candidate ends with key:    "HQ City" key, candidate "City"
    Pass 5   — Loose substring (guarded)
    """
    # Pass 1 — exact case-sensitive
    for c in candidates:
        if c in row:
            return row[c]

    # Build lowercase lookup once
    row_lower = {k.lower(): v for k, v in row.items()}

    # Pass 1.5 — normalised: treat spaces, underscores, and hyphens as equivalent.
    # This bridges English candidates ("Company Name") with Gemini canonical
    # field names ("company_name") added by the alias map.
    row_norm = {k.replace(' ', '_').replace('-', '_'): v for k, v in row_lower.items()}
    for c in candidates:
        cn = c.lower().replace(' ', '_').replace('-', '_')
        if cn in row_norm:
            return row_norm[cn]

    # Pass 2 — exact case-insensitive
    for c in candidates:
        cl = c.lower()
        if cl in row_lower:
            return row_lower[cl]

    # Pass 3 — the column header ends with the candidate phrase (word-boundary)
    for c in candidates:
        cl = c.lower()
        for key_l, val in row_lower.items():
            if key_l == cl:
                return val
            if key_l.endswith(' ' + cl) or key_l.endswith('-' + cl):
                return val

    # Pass 4 — the candidate ends with the column header
    for c in candidates:
        cl = c.lower()
        for key_l, val in row_lower.items():
            if cl.endswith(' ' + key_l) or cl.endswith('-' + key_l):
                return val

    # Pass 5 — loose substring, guarded
    for c in candidates:
        cl = c.lower()
        if len(c.split()) == 1 and len(c) < 8:
            continue
        for key_l, val in row_lower.items():
            if cl in key_l or key_l in cl:
                return val

    return None


def _find_col_str(row, *candidates, default=''):
    val = _find_col(row, *candidates)
    return _str(val, default)


def _find_col_decimal(row, *candidates, default=None):
    val = _find_col(row, *candidates)
    return _d(val, default)


def _normalize_col_key(name):
    """
    Normalize an Excel column header for robust, format-agnostic fuzzy matching.

    Fund managers format column headers in wildly different ways across Excel files:
      'Budget(₹Cr)'        — no space before unit
      'Revenue (INR Mn)'   — space before unit
      'GrossProfit'        — CamelCase without spaces
      'Gross Profit(Cr)'   — CamelCase + unit
      'EBITDA%'            — ratio marker attached
      'Budget [INR Cr]'    — square-bracket unit notation

    This function normalises all such variations to a clean label so that
    _find_col() and _pl_map_to_line_item() can match reliably regardless of
    the Excel file's formatting conventions.

    Transformations applied (in order):
      1. Strip unit/currency annotations enclosed in () or []:
         'Budget(₹Cr)' → 'Budget', 'Revenue (INR Mn)' → 'Revenue'
         Works with any currency symbol (₹ $ € £ ¥) and any unit
         (Cr, Crore, Lakh, Mn, Million, Bn, Billion, INR, USD, EUR, GBP,
          K, Thousands, Rs, Rupees, Lk, Lakhs).
      2. Split CamelCase into words:
         'GrossProfit' → 'Gross Profit', 'NetWorth' → 'Net Worth'
         Only inserts a space where a lowercase letter is immediately followed
         by an uppercase letter — safe for acronyms like 'EBITDA', 'PAT'.
    """
    if not name:
        return ''
    s = str(name).strip()
    # Step 1 — strip unit/currency suffix in parentheses or square brackets
    s = re.sub(
        r'[\s]*[\(\[]\s*[₹$€£¥]?\s*'
        r'(?:cr|crore|lakh|lakhs|lk|mn|million|bn|billion|'
        r'inr|usd|eur|gbp|k|thousands|rs|rupees|₹)'
        r'[^)\]]*[\)\]]',
        '',
        s,
        flags=re.IGNORECASE,
    ).strip()
    # Step 2 — insert space at lowercase→uppercase boundary (CamelCase split)
    s = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', s)
    return s


def _norm_row(row):
    """
    Return a copy of a row dict with all keys passed through _normalize_col_key.

    Preserves None keys as empty string (they are always skipped in matching).
    Duplicate normalised keys are last-writer-wins — in practice this only
    occurs when a sheet has two columns that both normalise to the same label
    (e.g., 'EBITDA(Cr)' and 'EBITDA(INR Cr)'), which should not happen in
    well-formed Excel files.
    """
    return {_normalize_col_key(k) if k is not None else '': v for k, v in row.items()}


def _find_col_date(row, *candidates):
    val = _find_col(row, *candidates)
    return _date(val)


def _find_col_bool(row, *candidates):
    val = _find_col(row, *candidates)
    return _bool(val)


# ---------------------------------------------------------------------------
# Domain-to-sheet resolver
# ---------------------------------------------------------------------------

def _build_domain_sheet_map(classifications, wb):
    """Build a 1:many mapping from canonical domain name to sheet name list.

    Uses Gemini's sheet classification (Pass 1) exclusively — no keyword
    fallback. Multiple sheets can share the same domain (e.g., 4 financial
    sheets all classified as financials_pl_bva).

    CRITICAL RULE: Cover/summary sheets (Cover, Summary, Index, Dashboard,
    etc.) are NEVER mapped to transactional data domains like
    portfolio_investments, investors_aml, capital_calls, etc.
    They may only be mapped to fund_scheme_master for basic fund identity
    fields. All statistics on cover sheets (company count, total FV, etc.)
    are display aggregates — the source of truth is always the actual
    data sheets.
    """
    # Domains that MUST NEVER be served by a cover/summary sheet.
    # These domains read transactional/company rows from their sheets.
    _DATA_ONLY_DOMAINS = {
        'portfolio_hierarchy', 'portfolio_investments', 'organization_users',
        'investors_aml', 'commitments', 'capital_calls', 'valuations_kpis',
        'nav_accounting', 'exits_distributions', 'compliance',
    }

    from collections import defaultdict
    domain_map = defaultdict(list)  # domain → [sheet_name, ...]

    # From Gemini classification — allow MULTIPLE sheets per domain.
    # Gemini assigns each sheet a primary domain; multiple sheets CAN share
    # the same domain (e.g., P&L, BS, CF, BvA all → financials_pl_bva).
    for cls in classifications:
        sheet_name = cls.get('sheet_name', '')
        domains = cls.get('domains', [])
        for domain in domains:
            if domain and domain != 'unknown':
                ws_check = wb[sheet_name] if sheet_name in wb.sheetnames else None
                if domain in _DATA_ONLY_DOMAINS and _is_cover_or_summary_sheet(sheet_name, ws_check):
                    logger.warning(
                        f'Gemini classified cover/summary sheet "{sheet_name}" as '
                        f'data domain "{domain}" — overriding (layout detection: '
                        f'no tabular data) to prevent metadata rows being imported '
                        f'as company/investment records.'
                    )
                    continue
                if sheet_name not in domain_map[domain]:
                    domain_map[domain].append(sheet_name)

    # Convert to regular dict for downstream consumption
    domain_map = dict(domain_map)

    # Log Gemini domain_map
    total_sheets = sum(len(v) for v in domain_map.values())
    logger.info(f'[GEMINI] domain_map: {len(domain_map)} domains, {total_sheets} sheet assignments')
    for d, sheets in sorted(domain_map.items()):
        logger.info(f'  {d} → {sheets}')

    return domain_map


# ---------------------------------------------------------------------------
# Financial aggregation helpers (roll-up from companies → sectors → funds)
# ---------------------------------------------------------------------------

def _build_summary_from_pl(monthly_pl, budget_vs_actual=None):
    """Build a summary dict from a company's monthly_pl entries.

    Uses the latest period's values as the summary, plus computes YTD totals.
    This is what the portfolio dashboard reads: financials.summary.revenue, etc.
    """
    if not monthly_pl:
        return {}

    # Latest period values
    latest = monthly_pl[-1] if monthly_pl else {}
    summary = {
        'revenue': latest.get('revenue', 0),
        'cogs': latest.get('cogs', 0),
        'gross_profit': latest.get('gross_profit', 0),
        'opex': latest.get('opex', 0),
        'ebitda': latest.get('ebitda', 0),
        'gp_pct': latest.get('gp_pct'),
        'ebitda_pct': latest.get('ebitda_pct'),
        'period': latest.get('period', 'Latest'),
    }

    # YTD totals (sum all periods)
    for field in ('revenue', 'cogs', 'gross_profit', 'opex', 'ebitda'):
        ytd_key = f'ytd_{field}'
        vals = [p.get(field, 0) for p in monthly_pl if isinstance(p.get(field), (int, float))]
        if vals:
            summary[ytd_key] = round(sum(vals), 2)

    # Budget info from budget_vs_actual
    if budget_vs_actual:
        for entry in budget_vs_actual:
            li = (entry.get('line_item') or '').lower()
            if 'revenue' in li:
                summary['budget_revenue'] = entry.get('budget', 0)
                summary['ytd_budget_revenue'] = entry.get('budget', 0)
            elif 'ebitda' in li:
                summary['budget_ebitda'] = entry.get('budget', 0)
                summary['ytd_budget_ebitda'] = entry.get('budget', 0)

    return summary


def _aggregate_financials(children_financials):
    """Aggregate financial data from a list of child node financials dicts.

    Produces a parent-level financials dict with:
    - summary: summed revenue, cogs, gross_profit, opex, ebitda + derived pcts
    - monthly_pl: merged and summed by period
    - budget_vs_actual: merged by line_item
    """
    # --- Aggregate summary ---
    fields_to_sum = [
        'revenue', 'cogs', 'gross_profit', 'opex', 'ebitda',
        'ytd_revenue', 'ytd_cogs', 'ytd_gross_profit', 'ytd_opex', 'ytd_ebitda',
        'budget_revenue', 'budget_ebitda',
        'ytd_budget_revenue', 'ytd_budget_ebitda',
    ]
    summary = {}
    for field in fields_to_sum:
        vals = [
            (cf.get('summary') or {}).get(field)
            for cf in children_financials
        ]
        vals = [v for v in vals if isinstance(v, (int, float))]
        if vals:
            summary[field] = round(sum(vals), 2)

    rev = summary.get('revenue')
    if rev:
        gp = summary.get('gross_profit')
        ebitda = summary.get('ebitda')
        if gp is not None:
            summary['gp_pct'] = round(gp / rev * 100, 2)
        if ebitda is not None:
            summary['ebitda_pct'] = round(ebitda / rev * 100, 2)
    summary['period'] = 'Latest'

    # Budget variance percentages
    ytd_rev = summary.get('ytd_revenue')
    bud_rev = summary.get('ytd_budget_revenue')
    if ytd_rev and bud_rev:
        summary['bva_revenue_pct'] = round((ytd_rev - bud_rev) / bud_rev * 100, 2)

    # --- Aggregate monthly_pl ---
    by_period = {}
    for cf in children_financials:
        for pt in cf.get('monthly_pl', []) or []:
            period = pt.get('period')
            if not period:
                continue
            agg = by_period.setdefault(period, {
                'period': period, 'revenue': 0, 'cogs': 0,
                'gross_profit': 0, 'opex': 0, 'ebitda': 0,
            })
            for f in ('revenue', 'cogs', 'gross_profit', 'opex', 'ebitda'):
                v = pt.get(f)
                if isinstance(v, (int, float)):
                    agg[f] += v
    monthly_pl = []
    for period in sorted(by_period.keys()):
        agg = by_period[period]
        rev = agg['revenue']
        if rev:
            agg['gp_pct'] = round(agg['gross_profit'] / rev * 100, 2)
            agg['ebitda_pct'] = round(agg['ebitda'] / rev * 100, 2)
        for f in ('revenue', 'cogs', 'gross_profit', 'opex', 'ebitda'):
            agg[f] = round(agg[f], 2)
        monthly_pl.append(agg)

    # --- Aggregate budget_vs_actual ---
    bva_by_li = {}
    for cf in children_financials:
        for entry in cf.get('budget_vs_actual', []) or []:
            li = entry.get('line_item')
            if not li:
                continue
            agg = bva_by_li.setdefault(li, {
                'line_item': li, 'period': 'YTD', 'budget': 0, 'actual': 0,
            })
            for f in ('budget', 'actual'):
                v = entry.get(f)
                if isinstance(v, (int, float)):
                    agg[f] += v
    budget_vs_actual = []
    for agg in bva_by_li.values():
        agg['variance'] = round(agg['actual'] - agg['budget'], 2)
        agg['variance_pct'] = round(
            agg['variance'] / agg['budget'] * 100, 2
        ) if agg['budget'] else None
        for f in ('budget', 'actual'):
            agg[f] = round(agg[f], 2)
        budget_vs_actual.append(agg)

    return {
        'summary': summary,
        'monthly_pl': monthly_pl,
        'budget_vs_actual': budget_vs_actual,
    }


# ---------------------------------------------------------------------------
# FundImportService
# ---------------------------------------------------------------------------

class FundImportService:
    """
    Orchestrates the import of a single fund Excel file.

    Uses Gemini AI to semantically classify sheets and map columns,
    then imports data into Django models using header-based reading.
    No hardcoded sheet names or column positions.
    """

    def __init__(self, organization, user):
        self.org = organization
        self.user = user
        self.errors = []
        self.counts = {}
        self._imported_fund = None
        self._gemini_sheet_aliases = {}  # {sheet_name: {excel_col: canonical_field}}
        self._pl_line_item_map = {}     # {label: canonical_pl_category} — populated per sheet
        self._junk_name_cache = {}      # {name: row_type} — populated per section
        self._filepath = None           # set by _do_import; used by Pass 2.5 layout calls
        self._sheet_layouts = {}        # {sheet_name: layout_dict} — cached Pass 2.5 results
        self._layout_failed_sheets = set()  # sheets where Gemini layout call failed → fallback

    # ------------------------------------------------------------------
    # Pass 2.5 — Per-sheet Gemini layout detection (PRIMARY row reader)
    #
    # Replaces the brittle Python heuristic that previously decided "where
    # does the table start / where does it end / which row is the header /
    # which rows are banner-disclaimer noise."  The Python heuristic
    # (`_is_section_title_row` + `_read_data_rows`) survives ONLY as a
    # safety-net fallback for the rare case where Gemini itself is
    # unreachable.  In normal operation Gemini is the source of truth.
    # ------------------------------------------------------------------

    def _get_sheet_layout(self, sheet_name):
        """Return the cached Pass 2.5 layout dict for a sheet, calling
        Gemini once on the first access.  Returns None ONLY if the Gemini
        call raised an exception — caller then falls back to the heuristic
        reader.  A successful "this sheet has no tables" response is a
        valid layout (sub_tables=[]) and is cached as such, not None.
        """
        if sheet_name in self._sheet_layouts:
            return self._sheet_layouts[sheet_name]
        if sheet_name in self._layout_failed_sheets:
            return None
        if not self._filepath:
            return None
        try:
            from .gemini_column_mapper import detect_sheet_layout
            layout = detect_sheet_layout(self._filepath, sheet_name)
            self._sheet_layouts[sheet_name] = layout
            return layout
        except Exception as e:
            logger.warning(
                f'Pass 2.5 layout detection failed for "{sheet_name}" '
                f'({type(e).__name__}: {e}); falling back to heuristic reader'
            )
            self._layout_failed_sheets.add(sheet_name)
            return None

    def _read_sheet_via_layout(self, ws, alias_map=None):
        """Read a sheet using the Gemini layout map (Pass 2.5) — PRIMARY path.

        Returns a dict of {section_name: (headers_dict, rows)} with the same
        contract as read_all_sections_from_sheet() so call sites stay identical.

        Behaviour:
          * Gemini layout succeeded → read rows mechanically per the map.
          * Gemini layout failed    → fall back to read_all_sections_from_sheet
                                       (the deterministic Python heuristic).
          * Gemini layout says "no tables" → return {} (empty result, NOT a
                                              fallback — Gemini explicitly said
                                              the sheet has no data, e.g. Cover).
        """
        sheet_name = ws.title
        layout = self._get_sheet_layout(sheet_name)

        if layout is None:
            # Gemini truly failed (network / API error) — fall back to
            # heuristic. This is the ONLY path that uses the old reader.
            return read_all_sections_from_sheet(ws, alias_map=alias_map)

        sub_tables = layout.get('sub_tables', [])
        if not sub_tables:
            # Gemini explicitly said: no tables in this sheet
            return {}

        sections = {}
        max_col = ws.max_column or 0
        max_row = ws.max_row or 0

        for idx, st in enumerate(sub_tables):
            header_row_0 = st['header_row']
            data_start_0 = st['data_start']
            data_end_0   = st['data_end']

            # Convert 0-indexed → openpyxl 1-indexed
            header_row = header_row_0 + 1
            data_start = data_start_0 + 1
            data_end   = data_end_0 + 1

            # Defensive re-validation (Gemini already validated, but the
            # sheet may have changed dimensions between Gemini's sample
            # and this read — unlikely, but free insurance)
            if not (1 <= header_row < data_start <= data_end <= max_row):
                logger.warning(
                    f'Layout sub-table {idx} for "{sheet_name}" has invalid '
                    f'bounds (header={header_row}, start={data_start}, '
                    f'end={data_end}, sheet_rows={max_row}); skipping'
                )
                continue

            # Read header row
            headers = {}
            for c in range(1, max_col + 1):
                h = ws.cell(header_row, c).value
                if h is not None:
                    headers[str(h).strip()] = c
            if not headers:
                continue

            # Read data rows (data_start ... data_end inclusive)
            rows = []
            for r in range(data_start, data_end + 1):
                row_data = {}
                all_empty = True
                for name, col in headers.items():
                    v = ws.cell(r, col).value
                    if v is not None:
                        all_empty = False
                    row_data[name] = v
                if all_empty:
                    continue

                # Gemini Pass-2 alias enrichment
                if alias_map:
                    for excel_col, canonical in alias_map.items():
                        if excel_col in row_data and canonical not in row_data:
                            row_data[canonical] = row_data[excel_col]

                rows.append(row_data)

            # Normalize section name (compat with existing call sites that
            # look up sections by ALL-CAPS keys)
            section_name = (st.get('section_name') or '').upper().strip()
            if '|' in section_name:
                section_name = section_name[:section_name.index('|')].strip()
            for sep in [' — ', ' - ', '—', ' –']:
                if sep in section_name:
                    section_name = section_name[:section_name.index(sep)].strip()
                    break

            # If this is the only/first sub-table and has no banner,
            # use __default__ so the existing call sites (which look for
            # __default__ before iterating named sections) keep working.
            if not section_name:
                if idx == 0 and len(sub_tables) == 1:
                    section_name = '__default__'
                else:
                    section_name = f'__section_{idx}__'

            sections[section_name] = (headers, rows)

        return sections

    def _derived_column_for(self, sheet_name, target_header_aliases):
        """Look up the Gemini-discovered formula for a derived column.

        target_header_aliases: list of header-text patterns to match against
        the layout's derived_columns (case-insensitive substring match — same
        spirit as _find_col).

        Returns the matched derived_column dict, or None.
        """
        layout = self._sheet_layouts.get(sheet_name)
        if not layout:
            return None
        aliases_lc = [a.lower() for a in target_header_aliases if a]
        for st in layout.get('sub_tables', []):
            for dc in st.get('derived_columns', []):
                col_lc = (dc.get('column_name') or '').lower()
                if not col_lc:
                    continue
                for a in aliases_lc:
                    if a in col_lc or col_lc in a:
                        return dc
        return None

    def _evaluate_derived_formula(self, derived_col, row):
        """Compute a derived column's value from its formula_components.

        derived_col: {'column_name', 'formula_components': [{sign, source_column}]}
        row: the row dict (header_text → cell_value)

        Returns Decimal or None if any source column is missing/non-numeric.
        Universal — no hardcoded formula, no SEBI assumption.  The formula
        was discovered by Gemini either from an explicit disclaimer row in
        the Excel ("Col I = C+E+F-D") or from standard accounting identities.
        """
        from decimal import Decimal, InvalidOperation
        total = Decimal('0')
        for comp in derived_col.get('formula_components', []):
            src_name = comp.get('source_column', '')
            sign = comp.get('sign', '+')
            # Case-insensitive lookup in row dict — handles whitespace/case drift
            val_raw = None
            for k, v in row.items():
                if k and str(k).strip().lower() == src_name.strip().lower():
                    val_raw = v
                    break
            if val_raw is None or val_raw == '':
                return None  # missing source → can't compute
            try:
                val = Decimal(str(val_raw))
            except (InvalidOperation, TypeError, ValueError):
                return None
            total = total + val if sign == '+' else total - val
        return total

    # ------------------------------------------------------------------
    # Pass 3 helpers: Gemini-powered value classification
    # ------------------------------------------------------------------

    def _classify_labels(self, labels, category_key, context=''):
        """Classify text labels into canonical categories via Gemini.

        Returns {label: canonical_key_or_None}.
        """
        from .gemini_column_mapper import classify_labels
        from .canonical_schema import CANONICAL_VALUE_CATEGORIES
        options = CANONICAL_VALUE_CATEGORIES.get(category_key, {})
        if not options:
            logger.warning(f'Unknown category_key: {category_key}')
            return {}
        return classify_labels(list(labels), category_key, options, context)

    def _classify_enum(self, values, enum_key, context=''):
        """Classify text values into a closed enum set via Gemini.

        Returns {value: enum_key}. Never returns None — always picks closest match.
        """
        from .gemini_column_mapper import classify_enum_values
        from .canonical_schema import CANONICAL_ENUM_TYPES
        options = CANONICAL_ENUM_TYPES.get(enum_key, {})
        if not options:
            logger.warning(f'Unknown enum_key: {enum_key}')
            return {}
        return classify_enum_values(list(values), enum_key, options, context)

    def _classify_junk_names(self, names):
        """Classify a batch of names as real entities vs junk rows via Gemini.

        Returns a set of names that are junk (subtotals, totals, headers, serials).
        Real entity names are NOT in the returned set.
        """
        unique_names = list(set(n for n in names if n and str(n).strip()))
        if not unique_names:
            return set()

        row_type_map = self._classify_labels(unique_names, 'row_type',
                                              context='Entity names vs junk rows')
        junk_types = {'subtotal', 'total', 'header', 'serial', 'note'}
        junk_set = set()
        for name, rtype in row_type_map.items():
            if rtype in junk_types:
                junk_set.add(name)
            self._junk_name_cache[name] = rtype
        return junk_set

    def _get_alias(self, ws) -> dict:
        """Return Gemini-built alias map for this worksheet (empty dict if none)."""
        return self._gemini_sheet_aliases.get(getattr(ws, 'title', ''), {})

    def _get_section_subdomain(self, sheet_name, sec_name):
        """Return the Gemini-classified sub-domain for a section.

        Looks up the section_map built by Gemini Pass 1.5.
        The lookup normalizes the section name to uppercase and tries:
          1. Exact match on the raw section name
          2. Exact match on uppercase-stripped version
          3. Substring match (section_map key is contained in sec_name or vice versa)

        Returns the sub-domain string (e.g. 'portfolio_companies') or 'unknown'.
        """
        sheet_secs = self._section_map.get(sheet_name, {})
        if not sheet_secs:
            return 'unknown'

        # Try exact match first
        if sec_name in sheet_secs:
            return sheet_secs[sec_name]

        # Try uppercase-normalized match
        sec_upper = sec_name.upper().strip()
        for mapped_name, subdomain in sheet_secs.items():
            if mapped_name.upper().strip() == sec_upper:
                return subdomain

        # Try substring containment (handles truncation/suffix differences)
        for mapped_name, subdomain in sheet_secs.items():
            mu = mapped_name.upper().strip()
            if mu in sec_upper or sec_upper in mu:
                return subdomain

        return 'unknown'

    def import_file(self, import_file_record, progress_cb=None):
        """
        Main entry point. Processes a single ImportFile record.
        """
        filepath = import_file_record.file.path

        def _progress(pct, msg):
            if progress_cb:
                progress_cb(pct, msg)

        # Clear Pass 3 classification cache for fresh import
        from .gemini_column_mapper import clear_classification_cache
        clear_classification_cache()

        # Step 1: Gemini column mapping (two-pass)
        _progress(5, 'Reading workbook...')

        mapping_result = None
        try:
            mapping_result = map_workbook_columns(filepath, _progress)
            import_file_record.column_mapping = mapping_result.get('column_mappings', {})
            import_file_record.gemini_confidence = mapping_result.get('overall_confidence', 0.0)
            import_file_record.sheet_names = mapping_result.get('sheet_names', [])
            import_file_record.status = 'importing'
            import_file_record.save(update_fields=[
                'column_mapping', 'gemini_confidence', 'sheet_names', 'status',
            ])
            n_classified = len(mapping_result.get('sheet_classifications', []))
            n_mapped = len(mapping_result.get('column_mappings', {}))
            logger.info(
                f'Gemini mapping complete: {n_classified} sheets classified, '
                f'{n_mapped} sheets column-mapped, '
                f'confidence={mapping_result.get("overall_confidence", 0):.2f}'
            )
        except Exception as e:
            import traceback
            logger.error(
                f'Gemini mapping FAILED for "{import_file_record.original_filename}": '
                f'{type(e).__name__}: {e}\n{traceback.format_exc()}'
            )
            self.errors.append({
                'section': 'gemini_mapping',
                'error': f'{type(e).__name__}: {e}',
            })
            mapping_result = None
            import_file_record.status = 'importing'
            import_file_record.save(update_fields=['status'])

        # Step 2: Import data
        _progress(25, 'Starting data import...')

        classifications = []
        column_mappings = {}
        section_map = {}
        if mapping_result:
            classifications = mapping_result.get('sheet_classifications', [])
            column_mappings = mapping_result.get('column_mappings', {})
            section_map = mapping_result.get('section_map', {})

        result = self._do_import(filepath, classifications, _progress,
                                 column_mappings, section_map)

        # Save fund reference back to ImportFile for cascading delete support
        if self._imported_fund:
            import_file_record.fund = self._imported_fund
            import_file_record.fund_name = self._imported_fund.name
            import_file_record.save(update_fields=['fund', 'fund_name'])

        return result

    @transaction.atomic
    def _do_import(self, filepath, classifications, progress_cb,
                   column_mappings=None, section_map=None):
        """
        Run the actual import using Gemini's sheet classification.

        Builds a domain→sheet_name map, then reads each sheet by headers
        (no hardcoded column positions or sheet names).

        column_mappings: {sheet_name: {sections: [{mappings: [{excel_column, canonical_field, confidence}]}]}}
        Built by Gemini Pass-2. We flatten it into self._gemini_sheet_aliases so
        read_table_from_sheet / read_all_sections_from_sheet can enrich every row
        with canonical field names regardless of how the Excel column was labelled.

        section_map: {sheet_name: {section_name: sub_domain}}
        Built by Gemini Pass-1.5. Maps detected section titles to canonical
        sub-domains (e.g. 'portfolio_companies', 'investments', etc.) for
        routing sections within multi-section sheets.
        """
        wb = openpyxl.load_workbook(filepath, data_only=True)
        org = self.org

        # Store filepath so Pass 2.5 layout detection can re-open the workbook
        # per-sheet. Reset layout cache for this import (fresh classification
        # per file — no cross-import contamination).
        self._filepath = filepath
        self._sheet_layouts = {}
        self._layout_failed_sheets = set()

        # Store Gemini Pass 1.5 section classification for routing
        self._section_map = section_map or {}

        # Build domain→sheet map from Gemini classification
        domain_map = _build_domain_sheet_map(classifications, wb)
        logger.info(f'Domain→sheet map: {domain_map}')

        # Build per-sheet Gemini alias map: {sheet_name: {excel_col: canonical_field}}
        # Only keep high-confidence mappings (≥0.70).
        self._gemini_sheet_aliases = {}
        for sheet_name, mapping_data in (column_mappings or {}).items():
            aliases = {}
            for section in mapping_data.get('sections', []):
                for m in section.get('mappings', []):
                    excel_col = m.get('excel_column', '')
                    canonical = m.get('canonical_field', '')
                    confidence = m.get('confidence', 0.0)
                    if excel_col and canonical and confidence >= 0.70:
                        aliases[excel_col] = canonical
            if aliases:
                self._gemini_sheet_aliases[sheet_name] = aliases
                logger.info(
                    f'Gemini aliases for "{sheet_name}": {list(aliases.items())[:8]}'
                )

        # Ensure fund categories
        for code, (name, sub_cat, leverage) in CATEGORY_MAP.items():
            FundCategory.objects.get_or_create(
                sebi_category_code=code,
                defaults={
                    'name': name,
                    'sub_category': sub_cat,
                    'leverage_permitted': leverage,
                },
            )

        # --- Extract fund name from Cover sheet or filename ---
        progress_cb(28, 'Reading fund information...')
        fund_name = self._extract_fund_name(wb, domain_map, filepath)
        if not fund_name:
            wb.close()
            raise ValueError('Could not determine fund name from workbook')

        # --- Create Fund & Scheme ---
        progress_cb(32, f'Creating fund: {fund_name}...')
        fund, schemes = self._import_fund_and_schemes(wb, org, domain_map, fund_name)
        self._imported_fund = fund

        # Grant fund access to the uploading user
        FundAccess.objects.get_or_create(
            user=self.user, fund=fund,
            defaults={'access_level': 'admin'},
        )

        # --- Extract fund metadata from Cover sheet ---
        progress_cb(34, 'Extracting fund metadata...')
        try:
            self._extract_fund_metadata(wb, fund, schemes, domain_map)
        except Exception as e:
            logger.warning(f'Fund metadata extraction error: {e}')
            self.errors.append({'section': 'fund_metadata', 'error': str(e)})

        # --- Extract scheme lifecycle from QAR Part B / compliance sheets ---
        progress_cb(35, 'Extracting scheme lifecycle parameters...')
        try:
            self._import_scheme_lifecycle(wb, fund, schemes, domain_map)
        except Exception as e:
            logger.warning(f'Scheme lifecycle extraction error: {e}')
            self.errors.append({'section': 'scheme_lifecycle', 'error': str(e)})

        # --- Import key entities & link to fund ---
        progress_cb(35, 'Importing key entities...')
        try:
            self._import_entities(wb, org, fund, domain_map)
        except Exception as e:
            logger.warning(f'Entity import error: {e}')
            self.errors.append({'section': 'entities', 'error': str(e)})

        # --- Import investors ---
        progress_cb(37, 'Importing investors...')
        investors = {}
        try:
            investors = self._import_investors(wb, org, domain_map)
        except Exception as e:
            logger.warning(f'Investors import error: {e}')
            self.errors.append({'section': 'investors', 'error': str(e)})

        # --- Import commitments from investor data ---
        progress_cb(40, 'Importing commitments...')
        commitments = {}
        try:
            commitments = self._import_commitments(wb, org, investors, schemes, domain_map)
        except Exception as e:
            logger.warning(f'Commitments import error: {e}')
            self.errors.append({'section': 'commitments', 'error': str(e)})

        # --- Import capital calls from investor drawdowns ---
        progress_cb(43, 'Importing capital calls...')
        try:
            self._import_capital_calls(wb, schemes, commitments, domain_map)
        except Exception as e:
            logger.warning(f'Capital calls import error: {e}')
            self.errors.append({'section': 'capital_calls', 'error': str(e)})

        # --- Import portfolio companies & investments ---
        progress_cb(47, 'Importing portfolio companies & investments...')
        companies = {}
        investments = {}
        try:
            companies, investments = self._import_portfolio(
                wb, org, schemes, domain_map, progress_cb)
        except Exception as e:
            logger.warning(f'Portfolio import error: {e}')
            self.errors.append({'section': 'portfolio', 'error': str(e)})

        # --- Import investment tranches ---
        progress_cb(52, 'Importing investment tranches...')
        try:
            self._import_tranches(wb, investments, domain_map)
        except Exception as e:
            logger.warning(f'Tranches import error: {e}')
            self.errors.append({'section': 'tranches', 'error': str(e)})

        # --- Import valuations ---
        progress_cb(56, 'Importing valuations...')
        try:
            self._import_valuations(wb, investments, domain_map)
        except Exception as e:
            logger.warning(f'Valuations import error: {e}')
            self.errors.append({'section': 'valuations', 'error': str(e)})

        # --- Import KPIs ---
        progress_cb(60, 'Importing KPIs...')
        try:
            self._import_kpis(wb, org, investments, companies, domain_map)
        except Exception as e:
            logger.warning(f'KPIs import error: {e}')
            self.errors.append({'section': 'kpis', 'error': str(e)})

        # --- Import company financials (burn & runway) + SaaS KPIs ---
        progress_cb(63, 'Importing company financials & burn rates...')
        try:
            self._import_company_financials(wb, org, investments, companies, domain_map)
        except Exception as e:
            logger.warning(f'Company financials import error: {e}')
            self.errors.append({'section': 'company_financials', 'error': str(e)})

        # --- Import MIS financials (P&L, BvA → BudgetVsActual + ConsolidatedMIS) ---
        progress_cb(63, 'Importing MIS financials (P&L & Budget vs Actual)...')
        try:
            self._import_mis_financials(wb, org, fund, investments, companies, domain_map)
        except Exception as e:
            logger.warning(f'MIS financials import error: {e}')
            self.errors.append({'section': 'mis_financials', 'error': str(e)})

        # --- Classify quoted vs unquoted companies ---
        progress_cb(64, 'Classifying quoted & unquoted companies...')
        try:
            self._import_quoted_unquoted(wb, org, investments, companies, domain_map)
        except Exception as e:
            logger.warning(f'Quoted/Unquoted import error: {e}')
            self.errors.append({'section': 'quoted_unquoted', 'error': str(e)})

        # --- Import NAV records ---
        progress_cb(65, 'Importing NAV & accounting...')
        try:
            self._import_nav(wb, schemes, domain_map)
        except Exception as e:
            logger.warning(f'NAV import error: {e}')
            self.errors.append({'section': 'nav', 'error': str(e)})

        # --- Import exits & distributions ---
        progress_cb(70, 'Importing exits & distributions...')
        try:
            self._import_exits_and_distributions(wb, investments, schemes, domain_map)
        except Exception as e:
            logger.warning(f'Exits/distributions import error: {e}')
            self.errors.append({'section': 'exits_distributions', 'error': str(e)})

        # --- Import distributions to LPs ---
        progress_cb(74, 'Importing LP distributions...')
        try:
            self._import_distributions(wb, schemes, commitments, investments, domain_map)
        except Exception as e:
            logger.warning(f'Distributions import error: {e}')
            self.errors.append({'section': 'distributions', 'error': str(e)})

        # --- Seed Chart of Accounts & create ledger entries ---
        progress_cb(78, 'Setting up fund accounting...')
        try:
            self._setup_fund_accounting(org, fund, schemes, investments)
        except Exception as e:
            logger.warning(f'Fund accounting setup error: {e}')
            self.errors.append({'section': 'fund_accounting', 'error': str(e)})

        # --- Import management fee schedule ---
        progress_cb(82, 'Importing management fees...')
        try:
            self._import_management_fees(wb, schemes, domain_map)
        except Exception as e:
            logger.warning(f'Management fees import error: {e}')
            self.errors.append({'section': 'management_fees', 'error': str(e)})

        # --- Compute carried interest ---
        progress_cb(85, 'Computing carried interest...')
        try:
            self._compute_carried_interest(schemes, wb=wb, domain_map=domain_map)
        except Exception as e:
            logger.warning(f'Carried interest error: {e}')
            self.errors.append({'section': 'carried_interest', 'error': str(e)})

        # --- Generate LP Capital Accounts from imported data ---
        progress_cb(87, 'Generating LP capital accounts...')
        try:
            self._generate_lp_capital_accounts(fund, schemes, commitments)
        except Exception as e:
            logger.warning(f'LP capital accounts error: {e}')
            self.errors.append({'section': 'lp_capital_accounts', 'error': str(e)})

        # --- Create income/expense ledger entries from NAV data ---
        progress_cb(89, 'Generating income & expense ledger entries...')
        try:
            self._generate_income_expense_ledger(org, fund, schemes)
        except Exception as e:
            logger.warning(f'Income/expense ledger error: {e}')
            self.errors.append({'section': 'income_expense_ledger', 'error': str(e)})

        # --- Build portfolio hierarchy ---
        progress_cb(92, 'Building portfolio hierarchy...')
        try:
            self._build_hierarchy(wb, org, fund, schemes, companies,
                                  investments, domain_map, filepath)
        except Exception as e:
            logger.warning(f'Portfolio hierarchy error: {e}')
            self.errors.append({'section': 'hierarchy', 'error': str(e)})

        # --- Seed IC Workflow deal pipeline from investments ---
        progress_cb(95, 'Seeding IC deal pipeline...')
        try:
            self._seed_ic_pipeline(org, fund, investments)
        except Exception as e:
            logger.warning(f'IC pipeline seeding error: {e}')
            self.errors.append({'section': 'ic_pipeline', 'error': str(e)})

        wb.close()

        # Collect counts — always from DB rows, never from Cover sheet aggregates
        self.counts = self._collect_counts(org, fund)

        # Post-import sanity check: catch metadata-as-data pollution early.
        # If we only imported a tiny number of companies (≤20) for a fund,
        # check whether any of those "company names" look like metadata labels
        # (e.g., "Short Code", "Fund Corpus", "Hurdle Rate"). If so, log a
        # critical warning so operators can investigate immediately.
        self._validate_imported_companies(fund)

        progress_cb(100, 'Import complete')

        return {
            'counts': self.counts,
            'errors': self.errors,
            'fund_name': fund.name,
        }

    # ------------------------------------------------------------------
    # Extract fund name
    # ------------------------------------------------------------------

    def _extract_fund_identity(self, wb, domain_map):
        """Extract fund identity metadata from fund_scheme_master sheet via Gemini.

        Returns dict with keys: fund_name, sebi_registration_number, category,
        structure_type, fund_pan, fund_gstin, is_gift_city (all optional).
        """
        sheet_name = _dm_first(domain_map, 'fund_scheme_master')
        if not sheet_name or sheet_name not in wb.sheetnames:
            return {}
        ws = wb[sheet_name]
        # Collect all label-value pairs from the sheet
        kv_pairs = {}
        scan_limit = min(ws.max_row + 1, 100)
        for r in range(1, scan_limit):
            label = _str(ws.cell(r, 1).value).strip()
            val = ws.cell(r, 2).value
            if label and val is not None:
                kv_pairs[label] = _str(val).strip()
        if not kv_pairs:
            return {}
        from .gemini_column_mapper import extract_structured_metadata
        from .canonical_schema import CANONICAL_METADATA_FIELDS
        field_defs = CANONICAL_METADATA_FIELDS.get('fund_identity', {})
        return extract_structured_metadata(kv_pairs, field_defs,
                                           context='Fund identity metadata from AIF fund master sheet')

    def _extract_fund_name(self, wb, domain_map, filepath):
        """Extract fund name from Gemini-classified fund_scheme_master sheet or filename."""
        identity = self._extract_fund_identity(wb, domain_map)
        if identity.get('fund_name'):
            return identity['fund_name']
        # Fallback: extract from filename
        basename = os.path.basename(filepath)
        name = re.sub(r'^Mock_\d+_', '', basename)
        name = re.sub(r'\.xlsx?$', '', name, flags=re.IGNORECASE)
        name = name.replace('_', ' ')
        return name if name else None

    # ------------------------------------------------------------------
    # Fund & Schemes
    # ------------------------------------------------------------------

    def _import_fund_and_schemes(self, wb, org, domain_map, fund_name):
        """Create fund and schemes from available data.

        Handles:
        - Fund & Scheme Master sheet with FUND MASTER DATA + SCHEMES sections
        - Cover sheet with basic fund info
        - Auto-creates default scheme if no explicit scheme data found
        """
        # Use Gemini-extracted fund identity metadata
        identity = self._extract_fund_identity(wb, domain_map)

        # Determine AIF category from Gemini extraction or CATEGORY_MAP code
        cat_code = 'CAT_II'  # default
        cat_raw = identity.get('category', '')
        if cat_raw:
            if cat_raw in CATEGORY_MAP:
                cat_code = cat_raw
            else:
                cl = str(cat_raw).lower().strip()
                if re.search(r'\biii\b|cat.?iii', cl):
                    cat_code = 'CAT_III_LVF'
                elif re.search(r'\bii\b|cat.?ii', cl):
                    cat_code = 'CAT_II'
                elif re.search(r'\bi\b|cat.?i', cl):
                    cat_code = 'CAT_I_VCF'

        fund_category = FundCategory.objects.filter(
            sebi_category_code=cat_code).first()

        sebi_reg = identity.get('sebi_registration_number', '')
        fund_pan = identity.get('fund_pan', '')[:10] if identity.get('fund_pan') else ''
        fund_gstin = identity.get('fund_gstin', '')
        gift_city_raw = identity.get('is_gift_city', '')
        gift_city = str(gift_city_raw).lower() in ('yes', 'true', '1', 'y') if gift_city_raw else False

        # Structure type: Gemini extracts as enum
        structure = 'trust'
        struct_raw = identity.get('structure_type', '')
        if struct_raw:
            struct_result = self._classify_enum([struct_raw], 'structure_type',
                                                context='Legal structure of AIF fund')
            structure = struct_result.get(struct_raw, 'trust')

        fund_defaults = {}
        if fund_category:
            fund_defaults['fund_category'] = fund_category
        if structure:
            fund_defaults['structure_type'] = structure
        fund_defaults['base_currency'] = 'INR'
        if sebi_reg:
            fund_defaults['sebi_registration_number'] = sebi_reg
        if fund_pan:
            fund_defaults['pan'] = fund_pan
        if fund_gstin:
            fund_defaults['gstin'] = fund_gstin
        if gift_city:
            fund_defaults['is_gift_city'] = gift_city

        fund, created = Fund.objects.update_or_create(
            organization=org,
            name=fund_name,
            defaults=fund_defaults,
        )
        logger.info(f'{"Created" if created else "Updated"} Fund: {fund.name}')

        schemes = {}

        # Check for explicit scheme data in SCHEMES section
        sheet_name = _dm_first(domain_map, 'fund_scheme_master')
        if sheet_name and sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            schemes_start = find_section_rows(ws, 'SCHEMES')
            if schemes_start:
                # Read the header row right after the SCHEMES section header
                header_row = None
                for scan_r in range(schemes_start + 1, min(schemes_start + 5, ws.max_row + 1)):
                    cell_count = sum(1 for c in range(1, ws.max_column + 1)
                                     if ws.cell(scan_r, c).value is not None)
                    if cell_count >= 3:
                        header_row = scan_r
                        break

                if header_row:
                    # Read as a proper table
                    headers = {}
                    for c in range(1, ws.max_column + 1):
                        h = ws.cell(header_row, c).value
                        if h:
                            headers[str(h).strip()] = c

                    for r in range(header_row + 1, ws.max_row + 1):
                        row_data = {}
                        all_empty = True
                        for name, col in headers.items():
                            val = ws.cell(r, col).value
                            if val is not None:
                                all_empty = False
                            row_data[name] = val
                        if all_empty:
                            break
                        fc = ws.cell(r, 1).value
                        if fc and _is_section_header(_str(fc)):
                            break

                        sn = _find_col_str(
                            row_data, 'Scheme Name', 'Name', 'Scheme')
                        if not sn:
                            continue

                        vintage = _find_col_decimal(
                            row_data, 'Vintage Year', 'Vintage')
                        first_close = _find_col_date(
                            row_data, 'First Close', 'First Close Date')
                        final_close = _find_col_date(
                            row_data, 'Final Close', 'Final Close Date')
                        scheme_size = _find_col_decimal(
                            row_data, 'Scheme Size (Cr)', 'Scheme Size',
                            'Fund Size', 'Size')
                        tenure = _find_col_decimal(
                            row_data, 'Tenure (Years)', 'Tenure', 'Term')
                        hurdle = _find_col_decimal(
                            row_data, 'Hurdle Rate %', 'Hurdle', 'Hurdle Rate')
                        carry = _find_col_decimal(
                            row_data, 'Carry %', 'Carry', 'Carried Interest')
                        carry_type_raw = _find_col_str(
                            row_data, 'Carry Type', 'Waterfall Type',
                            default='european')
                        fee_basis_raw = _find_col_str(
                            row_data, 'Mgmt Fee Basis', 'Fee Basis',
                            default='committed')
                        fee_pct = _find_col_decimal(
                            row_data, 'Mgmt Fee %', 'Management Fee %',
                            'Fee %')
                        sponsor_pct = _find_col_decimal(
                            row_data, 'Sponsor Commitment %', 'Sponsor %')
                        status_raw = _find_col_str(
                            row_data, 'Status', 'Scheme Status',
                            default='Investing')
                        scheme_status_result = self._classify_enum(
                            [status_raw], 'scheme_status',
                            context='Scheme/fund lifecycle status')
                        scheme_status = scheme_status_result.get(
                            status_raw, 'investing')

                        carry_type_result = self._classify_enum(
                            [carry_type_raw], 'carry_type',
                            context='Waterfall/carry type') if carry_type_raw else {}
                        carry_type = carry_type_result.get(carry_type_raw, 'european')

                        fee_basis_result = self._classify_enum(
                            [fee_basis_raw], 'fee_basis',
                            context='Management fee calculation basis') if fee_basis_raw else {}
                        fee_basis = fee_basis_result.get(fee_basis_raw, 'committed')

                        scheme_defaults = {'is_active': True}
                        if vintage:
                            scheme_defaults['vintage_year'] = int(vintage)
                        if first_close:
                            scheme_defaults['first_close_date'] = first_close
                        if final_close:
                            scheme_defaults['final_close_date'] = final_close
                        if scheme_size:
                            scheme_defaults['scheme_size'] = scheme_size
                        if tenure:
                            scheme_defaults['tenure_years'] = int(tenure)
                        if hurdle:
                            scheme_defaults['hurdle_rate_pct'] = hurdle
                        if carry:
                            scheme_defaults['carry_pct'] = carry
                        if carry_type:
                            scheme_defaults['carry_type'] = carry_type
                        if fee_pct:
                            scheme_defaults['management_fee_pct'] = fee_pct
                        if fee_basis:
                            scheme_defaults['management_fee_basis'] = fee_basis
                        if sponsor_pct:
                            scheme_defaults['sponsor_commitment_pct'] = sponsor_pct
                        if scheme_status:
                            scheme_defaults['scheme_status'] = scheme_status

                        s, _ = Scheme.objects.update_or_create(
                            fund=fund, name=sn,
                            defaults=scheme_defaults,
                        )
                        schemes[sn] = s
                else:
                    # Fallback: simple row-by-row reading
                    for r in range(schemes_start + 1, ws.max_row + 1):
                        sn = ws.cell(r, 1).value
                        if not sn or _str(sn) == 'Scheme Name':
                            continue
                        if _is_section_header(sn):
                            break
                        sn = _str(sn)
                        s, _ = Scheme.objects.update_or_create(
                            fund=fund, name=sn,
                            defaults={
                                'scheme_status': 'investing',
                                'is_active': True,
                            },
                        )
                        schemes[sn] = s

        # If no schemes found, create a default one
        if not schemes:
            scheme_name = f'{fund_name} - Scheme I'
            scheme, _ = Scheme.objects.update_or_create(
                fund=fund,
                name=scheme_name,
                defaults={
                    'vintage_year': date.today().year,
                    'scheme_status': 'investing',
                    'is_active': True,
                },
            )
            schemes[scheme_name] = scheme

        return fund, schemes

    # ------------------------------------------------------------------
    # Fund metadata extraction from Cover sheet
    # ------------------------------------------------------------------

    def _extract_fund_metadata(self, wb, fund, schemes, domain_map=None):
        """Extract rich fund metadata from ALL candidate sheets.

        Scans all relevant sheets (cover, fund_scheme_master, summary, overview)
        and merges their key-value pairs. Detailed/dedicated sheets are scanned
        FIRST so their values take priority; Sheet 1 / summary sheets are
        scanned LAST as fallback. This ensures we never stop at Sheet 1 if
        more detailed data exists in sub-sheets.
        """
        # Use Gemini-classified fund_scheme_master sheets
        candidate_sheets = _dm_sheets(domain_map, 'fund_scheme_master') if domain_map else []
        # Filter to sheets that actually exist
        candidate_sheets = [s for s in candidate_sheets if s in wb.sheetnames]

        if not candidate_sheets:
            return

        # Build a MERGED key-value map from ALL candidate sheets (raw labels preserved).
        kv = {}  # {raw_label: raw_value}
        for sheet_name in candidate_sheets:
            ws = wb[sheet_name]
            for r in range(1, min(ws.max_row + 1, 200)):
                for label_col, val_col in [(1, 2), (2, 3), (6, 7)]:
                    label = _str(ws.cell(r, label_col).value).strip().rstrip(':')
                    val = ws.cell(r, val_col).value
                    if label and val is not None and label not in kv:
                        kv[label] = val
            logger.info(f'  Fund metadata: scanned sheet "{sheet_name}" ({len(kv)} kv pairs total)')

        if not kv:
            return

        # Use Gemini to extract fund identity fields
        from .gemini_column_mapper import extract_structured_metadata
        from .canonical_schema import CANONICAL_METADATA_FIELDS

        kv_str = {k: _str(v).strip() for k, v in kv.items() if v is not None}

        # Extract fund identity
        fund_fields = CANONICAL_METADATA_FIELDS.get('fund_identity', {})
        fund_identity = extract_structured_metadata(
            kv_str, fund_fields,
            context='Fund identity metadata from cover/master sheet')

        # Extract scheme lifecycle
        scheme_fields = CANONICAL_METADATA_FIELDS.get('scheme_lifecycle', {})
        scheme_lifecycle = extract_structured_metadata(
            kv_str, scheme_fields,
            context='Scheme lifecycle parameters from cover/master sheet')

        # Update Fund fields
        update_fields = []

        reg_no = fund_identity.get('sebi_registration_number')
        if reg_no and not fund.sebi_registration_number:
            fund.sebi_registration_number = str(reg_no)
            update_fields.append('sebi_registration_number')

        corpus_raw = scheme_lifecycle.get('scheme_size')
        if corpus_raw and not fund.corpus_target:
            corpus_str = str(corpus_raw)
            corpus_num = re.sub(r'[₹,\s]', '', corpus_str)
            corpus_num = re.sub(r'[Cc]r\.?$', '', corpus_num)
            corpus_num = re.sub(r'^[Rr][Ss]\.?\s*', '', corpus_num)
            corpus_d = _d(corpus_num)
            if corpus_d:
                fund.corpus_target = corpus_d
                update_fields.append('corpus_target')

        vintage_raw = scheme_lifecycle.get('vintage_year')
        if vintage_raw:
            vintage_str = str(vintage_raw)
            if not fund.inception_date:
                vintage_date = _date(vintage_str)
                if vintage_date:
                    fund.inception_date = vintage_date
                    update_fields.append('inception_date')
                elif vintage_str.isdigit() and len(vintage_str) == 4:
                    fund.inception_date = date(int(vintage_str), 1, 1)
                    update_fields.append('inception_date')

        pan_raw = fund_identity.get('fund_pan')
        if pan_raw and not fund.pan:
            fund.pan = str(pan_raw)[:10]
            update_fields.append('pan')

        if update_fields:
            fund.save(update_fields=update_fields)
            logger.info(f'  Fund metadata updated: {update_fields}')

        # Update Scheme fields from Cover data — apply to ALL schemes
        for default_scheme in schemes.values():
            scheme_updates = []

            if vintage_raw:
                v_str = str(vintage_raw)
                if v_str.isdigit() and len(v_str) == 4:
                    default_scheme.vintage_year = int(v_str)
                    scheme_updates.append('vintage_year')

            hurdle_raw = scheme_lifecycle.get('hurdle_rate_pct')
            if hurdle_raw:
                h_str = str(hurdle_raw).replace('%', '').strip()
                h_val = _d(h_str)
                if h_val and not default_scheme.hurdle_rate_pct:
                    default_scheme.hurdle_rate_pct = h_val
                    scheme_updates.append('hurdle_rate_pct')

            carry_raw = scheme_lifecycle.get('carry_pct')
            if carry_raw:
                c_str = str(carry_raw).replace('%', '').strip()
                c_val = _d(c_str)
                if c_val and not default_scheme.carry_pct:
                    default_scheme.carry_pct = c_val
                    scheme_updates.append('carry_pct')

            fee_raw = scheme_lifecycle.get('management_fee_pct')
            if fee_raw:
                f_str = str(fee_raw).replace('%', '').strip()
                f_val = _d(f_str)
                if f_val and not default_scheme.management_fee_pct:
                    default_scheme.management_fee_pct = f_val
                    scheme_updates.append('management_fee_pct')

            fee_basis_raw = scheme_lifecycle.get('management_fee_basis')
            if fee_basis_raw:
                fb_result = self._classify_enum([str(fee_basis_raw)], 'fee_basis',
                                                context='Management fee basis')
                fb = fb_result.get(str(fee_basis_raw))
                if fb and not default_scheme.management_fee_basis:
                    default_scheme.management_fee_basis = fb
                    scheme_updates.append('management_fee_basis')

            tenure_raw = scheme_lifecycle.get('tenure_years')
            if tenure_raw and not default_scheme.tenure_years:
                t_str = re.sub(r'[^\d]', '', str(tenure_raw))
                if t_str and t_str.isdigit():
                    default_scheme.tenure_years = int(t_str)
                    scheme_updates.append('tenure_years')

            fc_raw = scheme_lifecycle.get('first_close_date')
            if fc_raw and not default_scheme.first_close_date:
                d_val = _date(str(fc_raw))
                if d_val:
                    default_scheme.first_close_date = d_val
                    scheme_updates.append('first_close_date')

            lc_raw = scheme_lifecycle.get('final_close_date')
            if lc_raw and not default_scheme.final_close_date:
                d_val = _date(str(lc_raw))
                if d_val:
                    default_scheme.final_close_date = d_val
                    scheme_updates.append('final_close_date')

            corpus_raw2 = scheme_lifecycle.get('scheme_size') or corpus_raw
            if corpus_raw2 and not default_scheme.scheme_size:
                c_str2 = str(corpus_raw2)
                c_num = re.sub(r'[₹,\s]', '', c_str2)
                c_num = re.sub(r'[Cc]r\.?$', '', c_num)
                c_num = re.sub(r'^[Rr][Ss]\.?\s*', '', c_num)
                c_val2 = _d(c_num)
                if c_val2:
                    default_scheme.scheme_size = c_val2
                    scheme_updates.append('scheme_size')

            sponsor_raw = scheme_lifecycle.get('sponsor_commitment_pct')
            if sponsor_raw and not default_scheme.sponsor_commitment_pct:
                s_str = str(sponsor_raw).replace('%', '').strip()
                s_val = _d(s_str)
                if s_val:
                    default_scheme.sponsor_commitment_pct = s_val
                    scheme_updates.append('sponsor_commitment_pct')

            if scheme_updates:
                default_scheme.save(update_fields=scheme_updates)
                logger.info(f'  Scheme "{default_scheme.name}" metadata updated: {scheme_updates}')

    # ------------------------------------------------------------------
    # Scheme Lifecycle Parameters (QAR Part B, compliance sheets, etc.)
    # ------------------------------------------------------------------

    def _import_scheme_lifecycle(self, wb, fund, schemes, domain_map):
        """Extract scheme lifecycle parameters from QAR Part B / compliance sheets.

        Many fund Excel files have a sheet like "QAR Part B" or "Scheme Lifecycle
        Parameters" that contains key-value pairs for: tenure, close dates, corpus,
        hurdle rate, carry, management fees, waterfall type, etc.

        This method scans ALL candidate sheets for key-value pairs matching scheme
        lifecycle fields. Detailed sub-sheets (QAR, lifecycle, compliance) are
        scanned FIRST so their values take priority over Sheet 1 summary data.
        Sheet 1 (fund_scheme_master) is scanned LAST as a fallback for any fields
        not found in dedicated sheets.
        """
        # Use Gemini-classified sheets: compliance first (more detailed), then fund_scheme_master
        candidate_sheets = []
        for domain in ('compliance', 'fund_scheme_master'):
            for s in _dm_sheets(domain_map, domain):
                if s in wb.sheetnames and s not in candidate_sheets:
                    candidate_sheets.append(s)

        if not candidate_sheets:
            return

        # Scan each candidate sheet for label-value pairs
        all_pairs = {}  # label_str → val_str (first occurrence wins)

        for sheet_name in candidate_sheets:
            ws = wb[sheet_name]
            for r in range(1, min(ws.max_row + 1, 200)):
                for label_col, val_col in [(1, 2), (2, 3), (1, 3)]:
                    label_raw = ws.cell(r, label_col).value
                    val_raw = ws.cell(r, val_col).value
                    if not label_raw or val_raw is None:
                        continue
                    label = _str(label_raw).strip().rstrip(':')
                    val_str = _str(val_raw).strip()
                    if not val_str or val_str.lower() in ('none', 'n/a', '-', ''):
                        continue
                    if label and label not in all_pairs:
                        all_pairs[label] = val_str

        if not all_pairs:
            return

        # Use Gemini to semantically match labels → scheme lifecycle fields
        from .gemini_column_mapper import extract_structured_metadata
        from .canonical_schema import CANONICAL_METADATA_FIELDS
        field_defs = CANONICAL_METADATA_FIELDS.get('scheme_lifecycle', {})
        extracted = extract_structured_metadata(all_pairs, field_defs,
                                                context='Scheme lifecycle parameters from QAR / compliance sheet')

        # Parse raw values into typed Python values
        collected = {}
        for field_name, raw_val in extracted.items():
            if field_name in collected:
                continue
            ftype = field_defs.get(field_name, {}).get('type', 'str')
            parsed = None
            val_str = str(raw_val).strip()
            if ftype == 'int':
                nums = re.sub(r'[^\d]', '', val_str)
                if nums and nums.isdigit():
                    parsed = int(nums)
            elif ftype == 'decimal':
                clean = re.sub(r'[₹,\s]', '', val_str)
                clean = re.sub(r'\s*[Cc]r\.?$', '', clean)
                clean = re.sub(r'^[Rr][Ss]\.?\s*', '', clean)
                parsed = _d(clean)
            elif ftype == 'pct':
                clean = val_str.replace('%', '').strip()
                clean = re.split(r'\s+(?:per|p\.?a|above|on)', clean, maxsplit=1)[0].strip()
                parsed = _d(clean)
            elif ftype == 'date':
                parsed = _date(val_str)
            elif ftype == 'enum':
                # Gemini already returned canonical enum value
                parsed = raw_val if raw_val else None
            else:
                parsed = val_str if val_str else None

            if parsed is not None:
                collected[field_name] = parsed
                logger.info(f'  Lifecycle: {field_name} = {parsed}')

        if not collected:
            return

        # Apply collected values to ALL schemes (lifecycle params are fund-level)
        for scheme in schemes.values():
            update_fields = []
            for field_name, value in collected.items():
                current = getattr(scheme, field_name, None)
                if not current:  # Only fill if currently empty/None
                    setattr(scheme, field_name, value)
                    update_fields.append(field_name)
            if update_fields:
                scheme.save(update_fields=update_fields)
                logger.info(f'  Scheme "{scheme.name}" lifecycle updated: {update_fields}')

    # ------------------------------------------------------------------
    # Investors
    # ------------------------------------------------------------------

    def _import_investors(self, wb, org, domain_map):
        """Import investors from the Investors/LP sheet."""
        sheet_name = _dm_first(domain_map, 'investors_aml')
        if not sheet_name or sheet_name not in wb.sheetnames:
            return {}

        ws = wb[sheet_name]
        _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
        investors = {}

        # Pass 3: Collect all unique investor types and classify via Gemini
        raw_inv_types = set()
        for row in rows:
            t = _find_col_str(row, 'Investor Type', 'LP Type', 'Type', 'Category')
            if t:
                raw_inv_types.add(t.strip())
        inv_type_map = self._classify_labels(raw_inv_types, 'investor_types',
                                              context='Investor/LP type classification')

        for row in rows:
            inv_name = _find_col_str(
                row, 'Investor Name', 'LP Name', 'Name', 'Investor')
            if not inv_name:
                continue

            inv_type_raw = _find_col_str(
                row, 'Investor Type', 'LP Type', 'Type', 'Category')
            inv_type = inv_type_map.get(inv_type_raw, 'other') if inv_type_raw else 'other'

            country = _find_col_str(row, 'Country', 'Domicile', default='India')
            commitment_amt = _find_col_decimal(
                row, 'Commitment(Cr)', 'Commitment', 'Committed Amount',
                'Commitment Amount', 'Total Commitment')
            pct_fund = _find_col_decimal(
                row, '% Fund', 'Fund %', 'Allocation %', 'Share %')
            drawdown = _find_col_decimal(
                row, 'Drawdown(Cr)', 'Drawdown', 'Called Amount',
                'Amount Called', 'Drawn')
            distributions = _find_col_decimal(
                row, 'Distributions', 'Distribution', 'Returned',
                'Amount Returned')
            status = _find_col_str(row, 'Status', 'LP Status', default='Active')

            investor, created = Investor.objects.get_or_create(
                organization=org,
                investor_name=inv_name,
                defaults={
                    'investor_type': inv_type,
                    'country': country,
                    'kyc_status': 'completed',
                },
            )
            investors[inv_name] = investor
            if created:
                logger.info(f'  Created Investor: {inv_name} ({inv_type})')

        logger.info(f'  Investors imported: {len(investors)}')
        return investors

    # ------------------------------------------------------------------
    # Commitments from investor data
    # ------------------------------------------------------------------

    def _import_commitments(self, wb, org, investors, schemes, domain_map):
        """Create Commitment records.

        Handles two formats:
        1. Dedicated 'commitments' sheet with columns: Investor Name, Scheme Name,
           Commitment Amount, Close Type, etc. (Format B / structured)
        2. Commitment columns embedded in investors_aml sheet:
           Commitment(Cr), Drawdown(Cr), etc. (Format A / flat)
        """
        default_scheme = list(schemes.values())[0] if schemes else None
        if not default_scheme:
            return {}

        commitments = {}

        # --- Strategy 1: Try dedicated Commitments sheet ---
        commit_sheet = _dm_first(domain_map, 'commitments')
        if commit_sheet and commit_sheet in wb.sheetnames:
            ws = wb[commit_sheet]
            _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
            if rows:
                for row in rows:
                    inv_name = _find_col_str(
                        row, 'Investor Name', 'LP Name', 'Name', 'Investor')
                    if not inv_name or inv_name not in investors:
                        continue

                    investor = investors[inv_name]
                    commitment_amt = _find_col_decimal(
                        row, 'Commitment Amount (Cr)', 'Commitment Amount',
                        'Commitment(Cr)', 'Commitment', 'Committed Amount',
                        'Total Commitment')
                    if not commitment_amt or commitment_amt <= 0:
                        continue

                    # Determine target scheme
                    scheme_name_raw = _find_col_str(
                        row, 'Scheme Name', 'Scheme', 'Fund Scheme')
                    target_scheme = default_scheme
                    if scheme_name_raw:
                        target_scheme = schemes.get(scheme_name_raw, default_scheme)

                    commit_date = _find_col_date(
                        row, 'Commitment Date', 'Date', 'Close Date')
                    close_type_raw = _find_col_str(
                        row, 'close_type', default='first_close')
                    close_type_result = self._classify_enum(
                        [close_type_raw], 'close_type',
                        context='Fund close type for investor commitment')
                    close_type = close_type_result.get(close_type_raw, 'first_close')

                    units = _find_col_decimal(
                        row, 'Units Allocated', 'Units', 'Allotted Units')
                    side_letter = _find_col_bool(
                        row, 'Side Letter', 'Side Letter Exists')

                    commitment, created = Commitment.objects.get_or_create(
                        investor=investor,
                        scheme=target_scheme,
                        commitment_amount=commitment_amt,
                        defaults={
                            'commitment_date': commit_date or date.today(),
                            'close_type': close_type,
                            'commitment_status': 'active',
                            'side_letter_exists': side_letter,
                            'units_allocated': units,
                        },
                    )
                    # Key by investor + scheme for later lookup
                    key = f'{inv_name}|{target_scheme.name}'
                    commitments[key] = commitment
                    # Also keep simple investor-name key for backward compat
                    if inv_name not in commitments:
                        commitments[inv_name] = commitment

                if commitments:
                    logger.info(f'  Commitments (dedicated sheet): {len(commitments)}')
                    return commitments

        # --- Strategy 2: Extract from investors_aml sheet (flat format) ---
        sheet_name = _dm_first(domain_map, 'investors_aml')
        if not sheet_name or sheet_name not in wb.sheetnames:
            return {}

        ws = wb[sheet_name]
        _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))

        for row in rows:
            inv_name = _find_col_str(
                row, 'Investor Name', 'LP Name', 'Name', 'Investor')
            if not inv_name or inv_name not in investors:
                continue

            investor = investors[inv_name]
            commitment_amt = _find_col_decimal(
                row, 'Commitment(Cr)', 'Commitment', 'Committed Amount',
                'Commitment Amount', 'Total Commitment')
            if not commitment_amt or commitment_amt <= 0:
                continue

            pct_fund = _find_col_decimal(
                row, '% Fund', 'Fund %', 'Allocation %', 'Share %')
            side_letter = _find_col_bool(
                row, 'Side Letter', 'Side Letter Exists')

            commitment, created = Commitment.objects.get_or_create(
                investor=investor,
                scheme=default_scheme,
                defaults={
                    'commitment_amount': commitment_amt,
                    'commitment_date': date.today(),
                    'close_type': 'first_close',
                    'commitment_status': 'active',
                    'side_letter_exists': side_letter,
                },
            )
            commitments[inv_name] = commitment
            if created and pct_fund:
                commitment.units_allocated = pct_fund * 100
                commitment.save(update_fields=['units_allocated'])

        logger.info(f'  Commitments: {len(commitments)}')
        return commitments

    # ------------------------------------------------------------------
    # Capital calls from investor drawdowns
    # ------------------------------------------------------------------

    def _import_capital_calls(self, wb, schemes, commitments, domain_map):
        """Create CapitalCall + CapitalCallLineItem records.

        Handles two formats:
        1. Dedicated 'capital_calls' sheet with explicit call records and
           separate CAPITAL CALL LINE ITEMS sections (Format B / structured)
        2. Drawdown amounts embedded in investors_aml sheet (Format A / flat)
        """
        default_scheme = list(schemes.values())[0] if schemes else None
        if not default_scheme:
            return

        # --- Strategy 1: Dedicated Capital Calls sheet ---
        # This runs even when commitments are empty — fund-level call data
        # (total amounts, dates, purposes) does not require LP records.
        cc_sheet = _dm_first(domain_map, 'capital_calls')
        if cc_sheet and cc_sheet in wb.sheetnames:
            ws = wb[cc_sheet]
            sections = self._read_sheet_via_layout(ws, alias_map=self._get_alias(ws))

            # Find the main capital calls section (not line items)
            cc_rows = []
            for sec_name, (sec_headers, sec_rows) in sections.items():
                subdomain = self._get_section_subdomain(cc_sheet, sec_name)
                if subdomain == 'capital_call_line_items':
                    continue  # Skip line item sections for now
                if subdomain == 'capital_call_headers' and sec_rows:
                    cc_rows = sec_rows
                    break
                # Fallback for __default__ or unclassified sections
                if sec_rows and not cc_rows:
                    cc_rows = sec_rows

            if cc_rows:
                call_count = 0
                for row in cc_rows:
                    scheme_name_raw = _find_col_str(
                        row, 'Scheme Name', 'Scheme', 'Fund Scheme')
                    target_scheme = default_scheme
                    if scheme_name_raw:
                        target_scheme = schemes.get(scheme_name_raw, default_scheme)

                    # Row serial / call ref — use as unique call_number.
                    # "Call#" (no space) is a common header variant; also plain "#".
                    call_num_raw = _find_col_decimal(
                        row, 'Call#', '#', 'S.No', 'Sr No', 'Serial',
                        'Call #', 'Call Number', 'Call No',
                        'Capital Call Number', 'call_number')
                    call_ref = _find_col_str(
                        row, 'Call Ref', 'Call Reference', 'Ref No', 'Reference')
                    call_num = int(call_num_raw) if call_num_raw else None
                    # Fall back: hash call_ref to a stable int
                    if not call_num and call_ref:
                        call_num = abs(hash(call_ref)) % 1000000

                    call_date = _find_col_date(
                        row, 'Call Date*', 'Call Date', 'Date', 'Call Issue Date')
                    due_date = _find_col_date(
                        row, 'Payment Due', 'Payment Due Date', 'Due Date',
                        'Payment Deadline')
                    call_pct = _find_col_decimal(
                        row, 'Corpus%', 'Corpus %', '% of Commit',
                        'Call %', 'Call Percentage', 'Drawdown %',
                        'call_percentage')
                    total_amt = _find_col_decimal(
                        row, 'Amount (Cr)', 'Amount(Cr)', 'Amount(₹Cr)',
                        'Amount(₹ Cr)', 'Amount (₹Cr)',
                        'Amount(INR Cr)', 'Amount (INR Cr)',
                        'Total Call Amount (Cr)', 'Total Call Amount',
                        'Call Amount', 'Total Amount', 'Actual Received',
                        'total_call_amount')
                    # LP name + portfolio purpose → store in purpose field
                    lp_name = _find_col_str(
                        row, 'LP Name', 'Investor Name', 'Investor', 'LP')
                    portfolio_purpose = _find_col_str(
                        row, 'Portfolio Co. / Purpose', 'Portfolio Co.',
                        'Portfolio Company', 'Purpose', 'Description', 'Notes')
                    if lp_name and portfolio_purpose:
                        purpose = f'{lp_name} — {portfolio_purpose}'
                    elif lp_name:
                        purpose = lp_name
                    else:
                        purpose = portfolio_purpose or ''

                    status_raw = _find_col_str(
                        row, 'Status', 'Call Status', 'LP Notified?', default='Paid')
                    call_status_result = self._classify_enum(
                        [status_raw], 'capital_call_status',
                        context='Capital call funding status')
                    status = call_status_result.get(status_raw, 'paid')

                    if not total_amt or total_amt <= 0:
                        continue
                    # Skip summary/total rows — call_num is only None when
                    # the first column has text, not a number
                    if not call_num:
                        if _is_junk_row(purpose):
                            continue
                        call_num = call_count + 1

                    CapitalCall.objects.update_or_create(
                        scheme=target_scheme,
                        call_number=call_num,
                        defaults={
                            'call_date': call_date or date.today(),
                            'payment_due_date': due_date or call_date or date.today(),
                            'call_percentage': call_pct or Decimal('0'),
                            'total_call_amount': total_amt,
                            'purpose': purpose,
                            'call_status': status,
                            'created_by': self.user,
                        },
                    )
                    call_count += 1

                # Now import line items from CAPITAL CALL LINE ITEMS sections
                line_count = 0
                for sec_name, (sec_headers, sec_rows) in sections.items():
                    subdomain = self._get_section_subdomain(cc_sheet, sec_name)
                    if subdomain != 'capital_call_line_items':
                        continue

                    # Try to figure out which call this belongs to
                    # Section name might be "CAPITAL CALL LINE ITEMS (Main Scheme — Call #1)"
                    parent_call = None
                    for call_obj in CapitalCall.objects.filter(
                            scheme__fund=default_scheme.fund):
                        call_ref = f'Call #{call_obj.call_number}'
                        if call_ref in sec_name:
                            parent_call = call_obj
                            break

                    if not parent_call:
                        # Default to first call for this scheme
                        parent_call = CapitalCall.objects.filter(
                            scheme__fund=default_scheme.fund
                        ).order_by('call_number').first()

                    if not parent_call:
                        continue

                    for row in sec_rows:
                        inv_name = _find_col_str(
                            row, 'Investor Name', 'LP Name', 'Name',
                            'Investor')
                        if not inv_name:
                            continue

                        # Find the commitment for this investor+scheme
                        commitment = None
                        key = f'{inv_name}|{parent_call.scheme.name}'
                        commitment = commitments.get(key)
                        if not commitment:
                            commitment = commitments.get(inv_name)
                        if not commitment:
                            continue

                        called_amt = _find_col_decimal(
                            row, 'Called Amount (Cr)', 'Called Amount',
                            'Amount Called', 'Call Amount')
                        cum_pct = _find_col_decimal(
                            row, 'Cumulative Called %', 'Cumulative %',
                            'Called %')
                        pay_status_raw = _find_col_str(
                            row, 'Payment Status', 'Status', default='Paid')
                        received = _find_col_decimal(
                            row, 'Amount Received (Cr)', 'Amount Received',
                            'Received')
                        pay_date = _find_col_date(
                            row, 'Payment Date', 'Received Date')

                        if not called_amt or called_amt <= 0:
                            continue

                        CapitalCallLineItem.objects.get_or_create(
                            capital_call=parent_call,
                            commitment=commitment,
                            defaults={
                                'called_amount': called_amt,
                                'cumulative_called_pct': cum_pct,
                                'payment_status': self._classify_enum(
                                    [pay_status_raw], 'payment_status',
                                    context='Capital call payment status').get(pay_status_raw, 'pending'),
                                'amount_received': received or called_amt,
                                'payment_date': pay_date or parent_call.call_date,
                            },
                        )
                        line_count += 1

                logger.info(f'  Capital calls (dedicated sheet): {call_count} calls, {line_count} line items')
                return

        # --- Strategy 2: Extract from investors_aml sheet (flat format) ---
        sheet_name = _dm_first(domain_map, 'investors_aml')
        if not sheet_name or sheet_name not in wb.sheetnames:
            return

        ws = wb[sheet_name]
        _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))

        total_drawn = Decimal('0')
        lp_drawdowns = []
        for row in rows:
            inv_name = _find_col_str(
                row, 'Investor Name', 'LP Name', 'Name', 'Investor')
            if not inv_name or inv_name not in commitments:
                continue

            drawn = _find_col_decimal(
                row, 'Drawdown(Cr)', 'Drawdown', 'Called Amount',
                'Amount Called', 'Drawn', 'Capital Called')
            drawn_pct = _find_col_decimal(
                row, 'Drawn%', 'Drawn Pct', 'Called %', 'Call %')
            if not drawn or drawn <= 0:
                continue

            total_drawn += drawn
            lp_drawdowns.append((inv_name, commitments[inv_name], drawn, drawn_pct))

        if not lp_drawdowns:
            return

        avg_drawn_pct = Decimal('0')
        for _, commitment, _, drawn_pct in lp_drawdowns:
            if drawn_pct:
                pct = drawn_pct * 100 if drawn_pct <= 1 else drawn_pct
                avg_drawn_pct += pct
        if lp_drawdowns:
            avg_drawn_pct = avg_drawn_pct / len(lp_drawdowns)

        call, created = CapitalCall.objects.get_or_create(
            scheme=default_scheme,
            call_number=1,
            defaults={
                'call_date': date.today(),
                'payment_due_date': date.today(),
                'call_percentage': avg_drawn_pct or Decimal('80'),
                'total_call_amount': total_drawn,
                'purpose': 'Investment deployment and fund expenses',
                'call_status': 'paid',
                'created_by': self.user,
            },
        )

        if not created:
            return

        line_count = 0
        for inv_name, commitment, drawn_amt, drawn_pct in lp_drawdowns:
            cumulative_pct = drawn_pct
            if cumulative_pct and cumulative_pct <= 1:
                cumulative_pct = cumulative_pct * 100

            CapitalCallLineItem.objects.get_or_create(
                capital_call=call,
                commitment=commitment,
                defaults={
                    'called_amount': drawn_amt,
                    'cumulative_called_pct': cumulative_pct,
                    'payment_status': 'paid',
                    'amount_received': drawn_amt,
                    'payment_date': date.today(),
                },
            )
            line_count += 1

        logger.info(f'  Capital calls: 1 call, {line_count} line items')

    # ------------------------------------------------------------------
    # Portfolio companies & investments
    # ------------------------------------------------------------------

    def _import_portfolio(self, wb, org, schemes, domain_map, progress_cb=None):
        """Import portfolio companies and investments.

        Handles two formats:
        1. Multi-section sheet: PORTFOLIO COMPANIES section (master data) +
           INVESTMENTS section (financial data) on the same sheet (Format B)
        2. Flat table: One row per company with both master and investment
           data combined (Format A)
        """
        def _cb(pct, msg):
            if progress_cb:
                progress_cb(pct, msg)
        sheet_name = _dm_first(domain_map, 'portfolio_investments')
        if not sheet_name or sheet_name not in wb.sheetnames:
            return {}, {}

        ws = wb[sheet_name]
        default_scheme = list(schemes.values())[0] if schemes else None
        if not default_scheme:
            return {}, {}

        companies = {}
        investments = {}

        # Try reading as multi-section sheet first
        sections = self._read_sheet_via_layout(ws, alias_map=self._get_alias(ws))

        # Look for separate PORTFOLIO COMPANIES and INVESTMENTS sections.
        #
        # Section name taxonomy:
        #   "PORTFOLIO COMPANIES"      → master data only (company names/sectors)
        #   "INVESTMENTS"              → financial investment data only
        #   "PORTFOLIO INVESTMENTS"    → combined (both company + investment per row)
        #   "__default__"              → entire sheet is one flat table
        company_rows = None
        investment_rows = None
        combined_rows = None   # "PORTFOLIO INVESTMENTS" — company+investment merged

        for sec_name, (sec_headers, sec_rows) in sections.items():
            subdomain = self._get_section_subdomain(sheet_name, sec_name)
            if subdomain == 'investment_tranches':
                continue  # Handled by _import_tranches
            elif subdomain == 'temporary_investments':
                continue  # Liquid MFs, overnight funds — skip
            elif subdomain == 'portfolio_companies':
                company_rows = sec_rows
            elif subdomain == 'investments':
                # Gemini classifies combined company+investment tables as
                # 'investments' — check if rows also have company identity
                # columns to decide if this is combined or pure investment.
                # Check if rows have company identity columns via alias map
                _sec_norm = {k.lower().replace(' ', '_').replace('-', '_')
                             for k in sec_headers}
                if sec_rows and ('company_name' in _sec_norm
                                or 'company_name' in set(
                                    self._get_alias(ws).get(k, '')
                                    for k in sec_headers)):
                    combined_rows = sec_rows
                else:
                    investment_rows = sec_rows
            elif sec_name == '__default__' and not company_rows and not combined_rows:
                combined_rows = sec_rows

        # Promote combined_rows to company_rows if no separate sections found
        if combined_rows and not company_rows:
            company_rows = combined_rows
            combined_rows = None

        # If we have separate company + investment rows, it's Format B
        if company_rows and investment_rows:
            # Import company master data
            for row in company_rows:
                name = _find_col_str(
                    row, 'Company Name', 'Company', 'Name', 'Portfolio Company')
                if _is_junk_row(name):
                    continue  # skip subtotal/total/header rows

                sector = _find_col_str(row, 'Sector', 'Industry', 'Vertical')
                sub_sector = _find_col_str(
                    row, 'Sub-Sector', 'Sub Sector', 'Subsector', 'Segment')
                city = _find_col_str(
                    row, 'City', 'Headquarters', 'HQ', 'HQ City',
                    'headquarters_city')
                country = _find_col_str(
                    row, 'Country', 'HQ Country', default='India')
                website = _find_col_str(row, 'Website', 'URL')
                founders = _find_col_str(row, 'Founders', 'Founder')

                # Use update_or_create so that re-importing always reflects the
                # current Excel data. Only overwrite a field if the Excel provides
                # a non-empty value — prevents a sparse re-import file from
                # blanking out good data that was previously imported.
                update_fields = {}
                if sector:
                    update_fields['sector'] = sector
                if sub_sector:
                    update_fields['sub_sector'] = sub_sector
                if city:
                    update_fields['headquarters_city'] = city
                if country:
                    update_fields['headquarters_country'] = country

                company, _ = PortfolioCompany.objects.update_or_create(
                    organization=org,
                    name=name,
                    defaults=update_fields,
                )
                companies[name] = company

            # Import investment data from the INVESTMENTS section
            for row in investment_rows:
                name = _find_col_str(
                    row, 'Company Name', 'Company', 'Name', 'Portfolio Company')
                if _is_junk_row(name):
                    continue  # skip subtotal/total/header rows

                # Ensure company exists. If the PORTFOLIO COMPANIES section
                # already loaded it, use that; otherwise create a minimal record.
                # update_or_create ensures re-imports don't leave stale data.
                company = companies.get(name)
                if not company:
                    company, _ = PortfolioCompany.objects.update_or_create(
                        organization=org, name=name,
                        defaults={'headquarters_country': 'India'},
                    )
                    companies[name] = company

                # Determine target scheme
                scheme_name_raw = _find_col_str(
                    row, 'Scheme', 'Scheme Name', 'Fund Scheme')
                target_scheme = default_scheme
                if scheme_name_raw:
                    target_scheme = schemes.get(scheme_name_raw, default_scheme)

                instrument_raw = _find_col_str(
                    row, 'instrument_type', default='Equity')
                instrument_result = self._classify_enum(
                    [instrument_raw], 'instrument_type',
                    context='Investment instrument or security type')
                instrument = instrument_result.get(instrument_raw, 'equity')

                stage = _find_col_str(row, 'Round', 'Stage', 'Funding Round', 'Round Name')
                irr_raw = _find_col_decimal(
                    row, 'IRR%(Gross)', 'IRR%', 'Gross IRR', 'IRR', 'irr_pct')
                hold_pct = _find_col_decimal(
                    row, 'Ownership %', 'Hold%', 'Holding %', 'Ownership',
                    'ownership_pct')
                fd_pct = _find_col_decimal(
                    row, 'Fully Diluted %', 'FD%', 'FD', 'Diluted %')
                invested = _find_col_decimal(
                    row, 'Total Invested (Cr)', 'Total Invested',
                    'Cost (Cr)', 'Cost(Cr)', 'Cost(₹Cr)',
                    'Cost', 'Invested', 'Investment Amount', 'Amount',
                    'total_invested')
                inv_date = _find_col_date(
                    row, 'Investment Date', 'Inv.Date', 'Date',
                    'investment_date')
                status_raw = _find_col_str(
                    row, 'Status', 'Investment Status', default='Active')
                board_seat = _find_col_bool(
                    row, 'Board Seat', 'Board', 'Has Board Seat')
                is_lead = _find_col_bool(
                    row, 'Lead Investor', 'Is Lead', 'Lead')

                status_result = self._classify_enum(
                    [status_raw], 'investment_status',
                    context='Investment/portfolio company status')
                status = status_result.get(status_raw, 'active')

                inv_defaults = {
                    'portfolio_company': company,
                    'currency': 'INR',
                    'status': status,
                    'sector': company.sector or '',
                    'board_seat': board_seat,
                    'is_lead_investor': is_lead or False,
                }
                if stage:
                    inv_defaults['stage'] = stage
                if irr_raw is not None:
                    irr_val = irr_raw * 100 if abs(irr_raw) <= 2 else irr_raw
                    inv_defaults['irr_pct'] = round(irr_val, 2)
                if hold_pct is not None:
                    inv_defaults['ownership_pct'] = hold_pct
                if fd_pct is not None:
                    inv_defaults['percentage_stake_fully_diluted'] = fd_pct
                if invested is not None:
                    inv_defaults['total_invested'] = abs(invested)
                if inv_date:
                    inv_defaults['investment_date'] = inv_date

                inv, created = Investment.objects.update_or_create(
                    scheme=target_scheme,
                    company_name=name,
                    instrument_type=instrument,
                    defaults=inv_defaults,
                )
                key = f'{name}|{target_scheme.name}|{instrument}'
                investments[key] = inv

            logger.info(f'  Portfolio (structured): {len(companies)} companies, '
                         f'{len(investments)} investments')
            return companies, investments

        # --- Format A: Flat combined table (one row = company + investment) ---
        # Use already-parsed rows from section reader when available;
        # only fall back to read_table_from_sheet if the section reader
        # returned nothing (e.g. unrecognized layout).
        if company_rows:
            rows = company_rows
        else:
            _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))

        total_rows = len(rows)
        row_idx = 0
        for row in rows:
            row_idx += 1
            # Emit a progress tick every 10 companies so the browser sees
            # the bar advancing during large imports (47% → 52% range)
            if row_idx % 10 == 0 and total_rows > 0:
                frac = row_idx / total_rows
                pct = int(47 + frac * 5)   # interpolate 47→52
                _cb(pct, f'Importing company {row_idx} of {total_rows}...')
            name = _find_col_str(
                row, 'Company Name', 'Company', 'Name', 'Portfolio Company')
            if _is_junk_row(name):
                continue  # skip subtotal/total/header rows

            sector = _find_col_str(row, 'Sector', 'Industry', 'Vertical')
            sub_sector = _find_col_str(
                row, 'Sub-Sector', 'Sub Sector', 'Subsector')
            city = _find_col_str(
                row, 'City', 'Headquarters', 'HQ', 'headquarters_city')
            country = _find_col_str(
                row, 'Country', 'HQ Country', default='India')
            listing_raw = _find_col_str(
                row, 'Listed', 'Listing Status', 'Quoted', 'Listed/Unlisted',
                'Quoted/Unquoted', 'Public/Private', 'is_quoted')
            listing_exchange = _find_col_str(
                row, 'Exchange', 'Listed On', 'Stock Exchange', 'listing_exchange')

            # update_or_create — always sync master data fields from Excel.
            # Only overwrite with non-empty values to protect existing good data.
            pc_update = {}
            if sector:
                pc_update['sector'] = sector
            if sub_sector:
                pc_update['sub_sector'] = sub_sector
            if city:
                pc_update['headquarters_city'] = city
            if country:
                pc_update['headquarters_country'] = country
            if listing_raw:
                qs = self._classify_enum([listing_raw], 'quoted_status',
                                          context='Listing status of portfolio company')
                pc_update['is_quoted'] = (qs.get(listing_raw) == 'quoted')
            if listing_exchange:
                pc_update['listing_exchange'] = listing_exchange.upper()

            company, _ = PortfolioCompany.objects.update_or_create(
                organization=org,
                name=name,
                defaults=pc_update,
            )
            companies[name] = company

            round_name = _find_col_str(
                row, 'Round', 'Funding Round', 'Stage', 'round_name')
            irr_raw_a = _find_col_decimal(
                row, 'IRR%(Gross)', 'IRR%', 'Gross IRR', 'IRR', 'irr_pct',
                'Net IRR', 'IRR (Gross)')
            invested = _find_col_decimal(
                row, 'Cost (Cr)', 'Cost(Cr)', 'Cost(₹Cr)',
                'Cost', 'Invested', 'Total Invested',
                'Investment Amount', 'Amount', 'total_invested')
            hold_pct = _find_col_decimal(
                row, 'Hold%', 'Holding %', 'Ownership', 'Ownership %',
                'ownership_pct')
            fd_pct = _find_col_decimal(
                row, 'FD%', 'Fully Diluted %', 'FD', 'Diluted %')
            inv_date = _find_col_date(
                row, 'Inv.Date', 'Investment Date', 'Date', 'investment_date')
            status_raw = _find_col_str(row, 'Status', 'Investment Status',
                                       default='Active')
            board_seat = _find_col_bool(row, 'Board', 'Board Seat',
                                         'Has Board Seat')

            status_result = self._classify_enum(
                [status_raw], 'investment_status',
                context='Investment/portfolio company status')
            status = status_result.get(status_raw, 'active')

            inv_a_defaults = {
                'portfolio_company': company,
                'instrument_type': 'equity',
                'currency': 'INR',
                'status': status,
                'board_seat': board_seat,
                'is_lead_investor': False,
            }
            if sector:
                inv_a_defaults['sector'] = sector
            if round_name:
                inv_a_defaults['stage'] = round_name
            if irr_raw_a is not None:
                irr_val_a = irr_raw_a * 100 if abs(irr_raw_a) <= 2 else irr_raw_a
                inv_a_defaults['irr_pct'] = round(irr_val_a, 2)
            if hold_pct is not None:
                inv_a_defaults['ownership_pct'] = hold_pct
            if fd_pct is not None:
                inv_a_defaults['percentage_stake_fully_diluted'] = fd_pct
            if invested is not None:
                inv_a_defaults['total_invested'] = abs(invested)
            if inv_date:
                inv_a_defaults['investment_date'] = inv_date

            inv, created = Investment.objects.update_or_create(
                scheme=default_scheme,
                company_name=name,
                defaults=inv_a_defaults,
            )
            key = f'{name}|{default_scheme.name}|equity'
            investments[key] = inv

            # If the flat table also carries FV (Cr) / Val. Date columns,
            # create a Valuation record now. This covers files that have no
            # dedicated Valuations sheet — the Portfolio Investments row is the
            # single source of truth for both cost and fair value.
            fv_raw = _find_col_decimal(
                row, 'FV (Cr)', 'FV(Cr)', 'FV(₹Cr)',
                'Fair Value (Cr)', 'Fair Value', 'Current Value (Cr)',
                'Current Value', 'Market Value (Cr)', 'fair_value')
            val_date_raw = _find_col_date(
                row, 'Val. Date', 'Val.Date', 'Valuation Date',
                'valuation_date')
            unrealized_raw = _find_col_decimal(
                row, 'Unrealised (Cr)', 'Unrealized (Cr)',
                'Unrealized Gain/Loss (Cr)', 'Unrealized Gain',
                'Unrealised', 'unrealized_gain_loss')
            if fv_raw is not None:
                Valuation.objects.update_or_create(
                    investment=inv,
                    valuation_date=val_date_raw or date.today(),
                    methodology='cost',
                    defaults={
                        'fair_value': fv_raw,
                        'cost_basis': invested,
                        'unrealized_gain_loss': unrealized_raw,
                        'status': 'approved',
                    },
                )

        logger.info(f'  Portfolio: {len(companies)} companies, '
                     f'{len(investments)} investments')
        return companies, investments

    # ------------------------------------------------------------------
    # Investment Tranches
    # ------------------------------------------------------------------

    def _import_tranches(self, wb, investments, domain_map):
        """Create InvestmentTranche records.

        Handles two formats:
        1. Dedicated INVESTMENT TRANCHES section within a multi-section sheet
           with explicit tranche #, amount, date, shares, PPS etc. (Format B)
        2. Flat table where each company row doubles as a single tranche (Format A)
        """
        sheet_name = _dm_first(domain_map, 'portfolio_investments')
        if not sheet_name or sheet_name not in wb.sheetnames:
            return

        ws = wb[sheet_name]

        # Try multi-section approach first
        sections = self._read_sheet_via_layout(ws, alias_map=self._get_alias(ws))
        tranche_rows = None
        for sec_name, (sec_headers, sec_rows) in sections.items():
            subdomain = self._get_section_subdomain(sheet_name, sec_name)
            if subdomain == 'investment_tranches':
                tranche_rows = sec_rows
                break

        count = 0

        if tranche_rows:
            # Format B: Dedicated tranche section
            for row in tranche_rows:
                name = _find_col_str(
                    row, 'Company Name', 'Company', 'Name', 'Portfolio Company')
                if _is_junk_row(name):
                    continue

                inv = None
                for key, i in investments.items():
                    if key.startswith(f'{name}|'):
                        inv = i
                        break
                if not inv:
                    continue

                tranche_num = _find_col_decimal(
                    row, 'Tranche #', 'Tranche Number', 'Tranche No', '#')
                tranche_num = int(tranche_num) if tranche_num else None

                invested = _find_col_decimal(
                    row, 'Amount (Cr)', 'Amount', 'Cost(₹Cr)', 'Cost',
                    'Invested', 'Total Invested', 'Investment Amount')
                inv_date = _find_col_date(
                    row, 'Date', 'Inv.Date', 'Investment Date',
                    'Tranche Date')
                round_name = _find_col_str(
                    row, 'Round Name', 'Round', 'Funding Round', 'Stage')
                shares = _find_col_decimal(
                    row, 'Shares Acquired', 'Shares', 'No. of Shares',
                    'Units')
                pps = _find_col_decimal(
                    row, 'Price/Share (INR)', 'Price/Share',
                    'Price Per Share', 'PPS')
                pre_money = _find_col_decimal(
                    row, 'Pre-Money Val (Cr)', 'Pre-Money',
                    'Pre Money Valuation')
                post_money = _find_col_decimal(
                    row, 'Post-Money Val (Cr)', 'Post-Money',
                    'Post Money Valuation')

                if not invested or invested <= 0:
                    continue

                if not tranche_num:
                    existing_count = InvestmentTranche.objects.filter(
                        investment=inv).count()
                    tranche_num = existing_count + 1

                InvestmentTranche.objects.get_or_create(
                    investment=inv,
                    tranche_number=tranche_num,
                    defaults={
                        'amount': abs(invested),
                        'date': inv_date or date.today(),
                        'shares_acquired': shares,
                        'price_per_share': pps,
                        'pre_money_valuation': pre_money,
                        'post_money_valuation': post_money,
                        'round_name': round_name or '',
                    },
                )
                count += 1

            logger.info(f'  Tranches (dedicated section): {count}')
            return

        # Format A: Create one tranche per investment from flat portfolio data
        _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))

        for row in rows:
            name = _find_col_str(
                row, 'Company Name', 'Company', 'Name', 'Portfolio Company')
            if _is_junk_row(name):
                continue

            inv = None
            for key, i in investments.items():
                if key.startswith(f'{name}|'):
                    inv = i
                    break
            if not inv:
                continue

            invested = _find_col_decimal(
                row, 'Cost(₹Cr)', 'Cost', 'Invested', 'Total Invested',
                'Investment Amount', 'Amount')
            inv_date = _find_col_date(
                row, 'Inv.Date', 'Investment Date', 'Date')
            round_name = _find_col_str(
                row, 'Round', 'Funding Round', 'Stage')
            shares = _find_col_decimal(
                row, 'Shares', 'Shares Acquired', 'No. of Shares')
            pps = _find_col_decimal(
                row, 'Price/Share', 'Price Per Share', 'PPS')
            pre_money = _find_col_decimal(
                row, 'Pre-Money', 'Pre Money Valuation', 'Pre-Money Val')
            post_money = _find_col_decimal(
                row, 'Post-Money', 'Post Money Valuation', 'Post-Money Val')

            if not invested or invested <= 0:
                continue

            existing_count = InvestmentTranche.objects.filter(
                investment=inv).count()

            InvestmentTranche.objects.get_or_create(
                investment=inv,
                tranche_number=existing_count + 1,
                defaults={
                    'amount': abs(invested),
                    'date': inv_date or date.today(),
                    'shares_acquired': shares,
                    'price_per_share': pps,
                    'pre_money_valuation': pre_money,
                    'post_money_valuation': post_money,
                    'round_name': round_name or '',
                },
            )
            count += 1

        logger.info(f'  Tranches: {count}')

    # ------------------------------------------------------------------
    # Valuations
    # ------------------------------------------------------------------

    def _import_valuations(self, wb, investments, domain_map):
        """Import valuations from Gemini-classified valuations_kpis sheets."""
        sheets = _dm_sheets(domain_map, 'valuations_kpis')
        if not sheets:
            return
        # Process all valuation sheets — column detection determines if data matches
        sheet_name = sheets[0]
        if sheet_name not in wb.sheetnames:
            return

        ws = wb[sheet_name]
        _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))

        count = 0
        for row in rows:
            name = _find_col_str(
                row, 'Company Name', 'Company', 'Name', 'Portfolio Company')
            if _is_junk_row(name):
                continue

            inv = None
            for key, i in investments.items():
                if key.startswith(f'{name}|'):
                    inv = i
                    break
            if not inv:
                continue

            val_date = _find_col_date(
                row, 'Val. Date', 'Val.Date', 'Valuation Date',
                'Date', 'valuation_date')
            methodology_raw = _find_col_str(
                row, 'Methodology', 'Method', 'Valuation Method',
                'Val Method', 'IPEV Technique', 'Technique',
                'Val. Method', 'Valuation Basis')

            val_method_result = self._classify_enum(
                [methodology_raw], 'valuation_methodology',
                context='Valuation methodology for portfolio company')
            methodology = val_method_result.get(methodology_raw, 'cost')

            ev = _find_col_decimal(
                row, 'EV(₹Cr)', 'Enterprise Value (Cr)',
                'Enterprise Value', 'EV', 'enterprise_value')
            equity_val = _find_col_decimal(
                row, 'FV (Cr)', 'FV(Cr)', 'FV(₹Cr)',
                'Equity Val', 'Equity Value', 'Fair Value (Cr)',
                'Fair Value', 'Current Value (Cr)', 'Current Value',
                'Market Value (Cr)', 'fair_value')
            fv_holding = _find_col_decimal(
                row, 'FV Holding', 'FV of Holding (Cr)',
                'FV of Holding', 'Fair Value Holding',
                'fair_value_of_holding')
            cost_basis = _find_col_decimal(
                row, 'Cost Basis (Cr)', 'Cost Basis',
                'Cost (Cr)', 'Cost(Cr)', 'Cost(₹Cr)',
                'Cost', 'cost_basis')
            unrealized = _find_col_decimal(
                row, 'Unrealized Gain/Loss (Cr)', 'Unrealised (Cr)',
                'Unrealised', 'Unreal G/L',
                'Unrealized', 'Unrealized Gain',
                'unrealized_gain_loss')
            moic = _find_col_decimal(row, 'MOIC', 'Multiple', 'moic')

            # Derive IPEV Level from valuation methodology.
            # All private equity unquoted assets are Level 3 by IPEV standard.
            # Level 2 applies only when observable market prices exist (listed comparables).
            _IPEV_LEVEL_MAP = {
                'dcf': 3, 'comparables': 3, 'recent_transaction': 3,
                'net_assets': 3, 'cost': 3, 'option_pricing': 3,
            }
            ipev_level = _IPEV_LEVEL_MAP.get(methodology, 3)
            # IPEV Technique column may explicitly specify the fair value level
            _ipev_technique_raw = _find_col_str(
                row, 'ipev_level', 'ipev_technique')
            if _ipev_technique_raw:
                _ipev_result = self._classify_enum(
                    [_ipev_technique_raw], 'ipev_level',
                    context='IPEV fair value hierarchy level')
                _ipev_mapped = _ipev_result.get(_ipev_technique_raw, str(ipev_level))
                try:
                    ipev_level = int(_ipev_mapped)
                except (ValueError, TypeError):
                    pass

            # Compute MOIC from FV / Cost if not directly in the sheet.
            if moic is None and equity_val:
                _cost = cost_basis or (inv.total_invested if inv else None)
                if _cost and _cost > 0:
                    try:
                        moic = round(equity_val / _cost, 2)
                    except Exception:
                        pass

            _val_defaults = {
                'fair_value': equity_val if equity_val is not None else Decimal('0'),
                'fair_value_of_holding': fv_holding,
                'enterprise_value': ev,
                'cost_basis': cost_basis,
                'unrealized_gain_loss': unrealized,
                'multiple': moic,
                'ipev_level': ipev_level,
                'status': 'approved',
            }
            Valuation.objects.update_or_create(
                investment=inv,
                valuation_date=val_date or date.today(),
                methodology=methodology,
                defaults=_val_defaults,
            )
            count += 1

        logger.info(f'  Valuations: {count}')

    # ------------------------------------------------------------------
    # KPIs
    # ------------------------------------------------------------------

    def _import_kpis(self, wb, org, investments, companies, domain_map):
        """Import KPIs from Gemini-classified valuations_kpis sheets."""
        sheets = _dm_sheets(domain_map, 'valuations_kpis')
        if not sheets:
            return
        # Use all KPI-classified sheets; start with the first one
        sheet_name = sheets[0]
        if sheet_name not in wb.sheetnames:
            return

        ws = wb[sheet_name]
        headers_dict, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))

        if not rows:
            return

        # Detect format: pivot (period columns like Oct-24) vs vertical
        period_cols = []
        period_pattern = re.compile(
            r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/]\d{2,4}$',
            re.IGNORECASE)
        for h in headers_dict.keys():
            if period_pattern.match(h.strip()):
                period_cols.append(h)

        count = 0
        if period_cols:
            # Pivot format: each row has a company + KPI name + values
            # across period columns
            for row in rows:
                name = _find_col_str(
                    row, 'Company Name', 'Company', 'Name')
                kpi_name = _find_col_str(
                    row, 'KPI Name', 'KPI', 'Metric', 'Indicator')
                if not name or not kpi_name:
                    continue

                inv = None
                for key, i in investments.items():
                    if key.startswith(f'{name}|'):
                        inv = i
                        break
                if not inv:
                    continue

                company = companies.get(name)
                kpi_slug = slugify(kpi_name)
                kpi_def, _ = KPIDefinition.objects.get_or_create(
                    organization=org,
                    slug=kpi_slug,
                    defaults={
                        'name': kpi_name,
                        'format': 'number',
                        'frequency': 'monthly',
                    },
                )

                for pcol in period_cols:
                    val = _d(row.get(pcol))
                    if val is None:
                        continue
                    # Parse period to date (e.g., "Oct-24" → 2024-10-01)
                    period_date = self._parse_period(pcol)
                    if not period_date:
                        continue

                    PortfolioKPI.objects.get_or_create(
                        investment=inv,
                        kpi_definition=kpi_def,
                        period=period_date,
                        defaults={
                            'portfolio_company': company,
                            'value': val,
                            'source': 'excel_upload',
                            'status': 'approved',
                        },
                    )
                    count += 1
        else:
            # Check if this is a vertical format (KPI Name column exists) or flat snapshot
            # Detect via Gemini Pass 2 alias map: any column mapped to kpi_name
            _alias = self._get_alias(ws)
            has_kpi_name_col = (
                'kpi_name' in {str(h or '').lower().strip() for h in headers_dict.keys()}
                or 'kpi_name' in set(_alias.values())
            )

            if has_kpi_name_col:
                # Vertical format: one KPI per row with period column
                for row in rows:
                    name = _find_col_str(
                        row, 'Company Name', 'Company', 'Name')
                    kpi_name = _find_col_str(
                        row, 'KPI Name', 'KPI', 'Metric')
                    if not name or not kpi_name:
                        continue

                    inv = None
                    for key, i in investments.items():
                        if key.startswith(f'{name}|'):
                            inv = i
                            break
                    if not inv:
                        continue

                    company = companies.get(name)
                    kpi_slug = slugify(kpi_name)
                    kpi_def, _ = KPIDefinition.objects.get_or_create(
                        organization=org,
                        slug=kpi_slug,
                        defaults={
                            'name': kpi_name,
                            'format': 'number',
                            'frequency': 'monthly',
                        },
                    )
                    period = _find_col_date(row, 'Period', 'Date', 'Month')
                    value = _find_col_decimal(row, 'Value', 'KPI Value')
                    if period and value is not None:
                        PortfolioKPI.objects.get_or_create(
                            investment=inv,
                            kpi_definition=kpi_def,
                            period=period,
                            defaults={
                                'portfolio_company': company,
                                'value': value,
                                'source': 'excel_upload',
                                'status': 'approved',
                            },
                        )
                        count += 1
            else:
                # Flat snapshot format: multi-section sheet where each column is a KPI.
                # E.g. Portfolio KPIs sheet with sector groups (Consumer, Financial Svcs…),
                # each group having its own header row and company data rows.
                count += self._import_kpis_flat_snapshot(ws, org, investments, companies)

        logger.info(f'  KPIs: {count}')

    # KPI canonical key → display name + format
    _KPI_DISPLAY = {
        'gmv': ('GMV', 'currency'), 'revenue': ('Revenue', 'currency'),
        'gross_margin_pct': ('Gross Margin %', 'percent'), 'ebitda': ('EBITDA', 'currency'),
        'ebitda_pct': ('EBITDA %', 'percent'), 'orders': ('Orders', 'number'),
        'aov': ('AOV', 'currency'), 'returns_pct': ('Returns %', 'percent'),
        'cac': ('CAC', 'currency'), 'repeat_pct': ('Repeat %', 'percent'),
        'cost_to_income': ('Cost to Income', 'ratio'), 'nim_pct': ('NIM %', 'percent'),
        'gnpa_pct': ('GNPA %', 'percent'), 'nnpa_pct': ('NNPA %', 'percent'),
        'roe_pct': ('ROE %', 'percent'), 'aum': ('AUM', 'currency'),
        'car_pct': ('CAR %', 'percent'), 'd_ebitda': ('D/EBITDA', 'ratio'),
        'capacity_pct': ('Capacity %', 'percent'), 'export_pct': ('Export %', 'percent'),
        'headcount': ('Headcount', 'number'), 'bed_occupancy': ('Bed Occupancy', 'percent'),
        'arpob': ('ARPOB', 'currency'), 'cap_rate_pct': ('Cap Rate %', 'percent'),
        'cost': ('Cost', 'currency'), 'fv': ('FV', 'currency'),
        'moic': ('MOIC', 'ratio'), 'mrr': ('MRR', 'currency'),
        'arr': ('ARR', 'currency'), 'churn_pct': ('Churn %', 'percent'),
        'nrr_pct': ('NRR %', 'percent'), 'ltv': ('LTV', 'currency'),
        'ltv_cac': ('LTV/CAC', 'ratio'), 'burn_rate': ('Burn Rate', 'currency'),
        'runway': ('Runway', 'number'), 'pat': ('PAT', 'currency'),
    }

    def _semantic_kpi_column_map(self, raw_headers):
        """Use Gemini Pass 3 to semantically map raw KPI column headers to canonical names.

        Returns: dict mapping raw_header → {canonical_name, slug, format}
        """
        if not raw_headers:
            return {}

        # Classify all headers via Gemini in a single call
        kpi_map = self._classify_labels(raw_headers, 'kpi_types',
                                         context='KPI column headers from portfolio sheet')

        result = {}
        for raw in raw_headers:
            canonical_key = kpi_map.get(raw)
            if canonical_key and canonical_key in self._KPI_DISPLAY:
                display_name, fmt = self._KPI_DISPLAY[canonical_key]
                result[raw] = {
                    'canonical_name': display_name,
                    'slug': slugify(display_name),
                    'format': fmt,
                }
            else:
                result[raw] = {
                    'canonical_name': raw,
                    'slug': slugify(raw),
                    'format': 'percent' if '%' in raw else 'number',
                }

        return result

    def _import_kpis_flat_snapshot(self, ws, org, investments, companies):
        """
        Handle Portfolio KPIs sheets in multi-section flat format.

        Each section has its own header row (first cell = 'Company'), followed by
        company data rows.  Each non-identity column is treated as a KPI metric.

        This handles sector-grouped KPI sheets where Consumer & Retail has CAC/GMV
        columns, Financial Svcs has AUM/NIM% columns, etc.  Reads ALL sections so
        every company gets its sector-appropriate KPIs imported.

        Uses Gemini-powered semantic matching: column names like
        "GMV (Cr)", "GMV in Crore", "Gross Merch Value" all map to the same
        canonical KPI definition via fuzzy/semantic matching.
        """
        # Skip identity/metadata columns — use canonical field names + format tokens.
        # Single-character tokens (#, id) and canonical names are language-independent.
        SKIP_COL_LOWER = {
            'company_name', 'name', '#', 'id', 'sr', 'sl', 'no',
            'sector', 'sub_sector', 'status', 'stage', 'headquarters_city',
            'instrument_type', 'round_name', 'scheme_name',
        }
        snapshot_date = date.today()
        count = 0
        current_headers = []  # [(col_idx, header_name), ...]

        max_col = ws.max_column or 1
        for row_idx in range(1, ws.max_row + 1):
            row_vals = [ws.cell(row_idx, c).value for c in range(1, max_col + 1)]

            # Blank row → reset section headers
            if not any(v is not None for v in row_vals):
                current_headers = []
                continue

            # Section title row: only first cell filled (sector group label)
            non_empty = sum(1 for v in row_vals if v is not None)
            if non_empty == 1:
                continue

            first_cell = str(row_vals[0] or '').strip()

            # Detect header row: first cell maps to 'company_name' canonical field
            _fc_norm = first_cell.lower().replace(' ', '_')
            if _fc_norm in ('company', 'company_name', 'name'):
                current_headers = []
                raw_kpi_headers = []
                for col_idx, val in enumerate(row_vals, 1):
                    if val is None:
                        continue
                    header = str(val).strip()
                    header_norm = _normalize_col_key(header)
                    _hn = header_norm.lower().replace(' ', '_')
                    _hl = header.lower().replace(' ', '_')
                    if _hn in SKIP_COL_LOWER or _hl in SKIP_COL_LOWER:
                        continue
                    if not header_norm:
                        continue
                    raw_kpi_headers.append((col_idx, header_norm))

                # Semantic mapping via Gemini + local aliases
                if raw_kpi_headers:
                    raw_names = [h[1] for h in raw_kpi_headers]
                    sem_map = self._semantic_kpi_column_map(raw_names)
                    for col_idx, header_name in raw_kpi_headers:
                        mapped = sem_map.get(header_name, {})
                        canonical = mapped.get('canonical_name', header_name)
                        kpi_slug = mapped.get('slug', slugify(header_name))
                        kpi_format = mapped.get('format', 'percent' if '%' in header_name else 'number')
                        if not kpi_slug:
                            continue
                        current_headers.append((col_idx, canonical, kpi_slug, kpi_format))
                continue

            # Data row — needs active section headers
            if not current_headers:
                continue

            company_name = _str(first_cell)
            if not company_name or _is_junk_row(company_name):
                continue

            # Resolve investment
            inv = None
            for key, i in investments.items():
                if key.startswith(f'{company_name}|'):
                    inv = i
                    break
            if not inv:
                co_obj = PortfolioCompany.objects.filter(
                    organization=org, name__iexact=company_name
                ).first()
                if co_obj:
                    inv = Investment.objects.filter(portfolio_company=co_obj).first()
            if not inv:
                continue

            company_obj = companies.get(company_name)

            for col_idx, header_name, kpi_slug, kpi_format in current_headers:
                if col_idx > len(row_vals):
                    continue
                cell_val = row_vals[col_idx - 1]
                if cell_val is None:
                    continue
                dec_val = _d(cell_val)
                if dec_val is None:
                    continue

                kpi_def, _ = KPIDefinition.objects.update_or_create(
                    organization=org,
                    slug=kpi_slug,
                    defaults={
                        'name': header_name,
                        'format': kpi_format,
                        'frequency': 'monthly',
                        'sector_template': 'generic',
                    },
                )
                PortfolioKPI.objects.update_or_create(
                    investment=inv,
                    kpi_definition=kpi_def,
                    period=snapshot_date,
                    defaults={
                        'portfolio_company': company_obj,
                        'value': dec_val,
                        'source': 'excel_upload',
                        'status': 'approved',
                    },
                )
                count += 1

        return count

    def _parse_quarter_period(self, quarter_str):
        """Parse 'Q1 FY25' → (start_date, end_date) using Indian FY (Apr–Mar).

        Q1 FY25 = Apr 2024 – Jun 2024
        Q2 FY25 = Jul 2024 – Sep 2024
        Q3 FY25 = Oct 2024 – Dec 2024
        Q4 FY25 = Jan 2025 – Mar 2025

        Returns (start_date, end_date) or (None, None) if unparseable.
        """
        if not quarter_str:
            return None, None
        match = re.match(r'Q([1-4])\s*FY\s*(\d{2,4})', quarter_str.strip(), re.IGNORECASE)
        if not match:
            return None, None
        q = int(match.group(1))
        fy = int(match.group(2))
        if fy < 100:
            fy += 2000
        # Indian FY starts in April of (fy-1) and ends in March of fy
        start_month, start_year = {
            1: (4, fy - 1), 2: (7, fy - 1), 3: (10, fy - 1), 4: (1, fy),
        }[q]
        end_month, end_year = {
            1: (6, fy - 1), 2: (9, fy - 1), 3: (12, fy - 1), 4: (3, fy),
        }[q]
        start = date(start_year, start_month, 1)
        last_day = calendar.monthrange(end_year, end_month)[1]
        end = date(end_year, end_month, last_day)
        return start, end

    def _parse_period(self, period_str):
        """Parse period strings like 'Oct-24', 'Nov-2024', 'Mar-25' to date."""
        period_str = period_str.strip()
        month_map = {
            'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
            'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
        }
        match = re.match(
            r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/](\d{2,4})',
            period_str, re.IGNORECASE)
        if match:
            month = month_map[match.group(1).lower()]
            year = int(match.group(2))
            if year < 100:
                year += 2000
            return date(year, month, 1)
        return None

    # ------------------------------------------------------------------
    # Company Financials (Burn & Runway)
    # ------------------------------------------------------------------

    def _import_company_financials(self, wb, org, investments, companies, domain_map):
        """Import burn rate, cash balance, runway, and SaaS metrics from Excel.

        Uses Gemini-classified domain_map to find the relevant sheet(s).
        Checks 'burn_runway' domain first, then 'valuations_kpis' as fallback
        (some funds embed burn data alongside KPIs).
        """
        # Get sheets from Gemini classification
        burn_sheets = _dm_sheets(domain_map, 'burn_runway')
        if not burn_sheets:
            # Fallback: valuations_kpis often contains embedded burn columns
            burn_sheets = _dm_sheets(domain_map, 'valuations_kpis')
        if not burn_sheets:
            return

        burn_sheet = burn_sheets[0]
        if burn_sheet not in wb.sheetnames:
            return

        ws = wb[burn_sheet]
        headers_dict, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
        if not rows:
            return

        # Detect period columns (e.g., "Oct-24", "Nov-24")
        period_pattern = re.compile(
            r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/]\d{2,4}$',
            re.IGNORECASE)
        period_cols = [h for h in headers_dict.keys()
                       if period_pattern.match(h.strip())]

        count = 0

        if period_cols:
            # Pivot format: rows have Company + Metric, columns are periods
            # Group rows by company, accumulate Gross Burn / Net Burn / Cash per period
            from collections import defaultdict
            company_period_data = defaultdict(lambda: defaultdict(dict))

            # Pre-classify all unique metric labels via Gemini Pass 3
            _metric_labels = set()
            for row in rows:
                metric = _find_col_str(row, 'kpi_name', 'metric')
                if metric:
                    _metric_labels.add(metric)
            _metric_map = self._classify_labels(list(_metric_labels), 'burn_runway_metrics',
                                                 context='Burn rate and runway metrics for portfolio companies')

            for row in rows:
                name = _find_col_str(row, 'company_name')
                metric = _find_col_str(row, 'kpi_name', 'metric')
                if not name or not metric:
                    continue
                if _is_junk_row(name):
                    continue

                field = _metric_map.get(metric)
                if not field:
                    continue

                for pcol in period_cols:
                    val = _d(row.get(pcol))
                    if val is None:
                        continue
                    period_date = self._parse_period(pcol)
                    if not period_date:
                        continue
                    company_period_data[name][period_date][field] = val

            for name, period_map in company_period_data.items():
                inv = None
                for key, i in investments.items():
                    if key.startswith(f'{name}|'):
                        inv = i
                        break
                if not inv:
                    continue
                company = companies.get(name)

                for period_date, fields in period_map.items():
                    gross = fields.get('gross_burn')
                    net = fields.get('net_burn')
                    cash = fields.get('cash_balance')
                    runway = fields.get('runway_months')

                    # Compute runway if not provided but burn + cash are known
                    if runway is None and cash is not None and net and net > 0:
                        from decimal import Decimal
                        runway = round(cash / net, 1)

                    defaults = {'portfolio_company': company}
                    if gross is not None:
                        defaults['gross_burn'] = abs(gross)
                    if net is not None:
                        defaults['net_burn'] = abs(net)
                    if cash is not None:
                        defaults['cash_balance'] = abs(cash)
                    if runway is not None:
                        defaults['runway_months'] = runway

                    CompanyFinancials.objects.update_or_create(
                        investment=inv,
                        period=period_date,
                        defaults=defaults,
                    )
                    count += 1

        else:
            # Flat format: one row per company (snapshot) or (company, period)
            # Some sheets (e.g. "SaaS Metrics & Burn") have no period column —
            # they represent a current snapshot.  Use today as the period.
            snapshot_date = date.today()

            # Guard: only extract SaaS KPIs from sheets with SaaS-specific headers.
            # Check via Gemini Pass 2 alias map: if any canonical field is a SaaS KPI.
            _saas_canonical = {'mrr', 'arr', 'nrr_pct', 'churn_pct', 'cac', 'ltv', 'ltv_cac'}
            _alias_map = self._get_alias(ws)
            _has_real_saas_cols = bool(_saas_canonical & set(_alias_map.values()))

            # SaaS KPI definitions — canonical field name only.
            # Gemini Pass 2 maps Excel column headers (in ANY language) to
            # canonical field names (arr, mrr, nrr, etc.) which are added as
            # keys in the row dict by read_table_from_sheet(). No hardcoded
            # English alias lists needed.
            SAAS_KPI_DEFS = [
                ('arr',       'ARR',            'currency', 'saas', 'arr'),
                ('mrr',       'MRR',            'currency', 'saas', 'mrr'),
                ('nrr',       'NRR',            'percent',  'saas', 'nrr'),
                ('churn-rate', 'Churn Rate',    'percent',  'saas', 'churn_rate'),
                ('cac',       'CAC',            'currency', 'saas', 'cac'),
                ('ltv',       'LTV',            'currency', 'saas', 'ltv'),
                ('ltv-cac',   'LTV:CAC Ratio',  'ratio',   'saas', 'ltv_cac_ratio'),
            ]
            kpi_def_cache = {}
            for slug, name_label, fmt, tmpl, _ in SAAS_KPI_DEFS:
                kd, _ = KPIDefinition.objects.get_or_create(
                    organization=org, slug=slug,
                    defaults={'name': name_label, 'format': fmt,
                              'frequency': 'monthly', 'sector_template': tmpl},
                )
                kpi_def_cache[slug] = kd

            for row in rows:
                name = _find_col_str(row, 'company_name')
                if not name or _is_junk_row(name):
                    continue

                inv = None
                for key, i in investments.items():
                    if key.startswith(f'{name}|'):
                        inv = i
                        break
                if not inv:
                    continue

                company = companies.get(name)
                period_date = _find_col_date(row, 'period', 'kpi_period')
                # Snapshot sheets have no period column — fall back to today
                if not period_date:
                    period_date = snapshot_date

                # Gemini Pass 2 adds canonical field names as row keys —
                # no hardcoded English candidates needed.
                gross = _find_col_decimal(row, 'gross_burn')
                net = _find_col_decimal(row, 'net_burn')
                cash = _find_col_decimal(row, 'cash_balance')
                runway = _find_col_decimal(row, 'runway_months')

                if runway is None and cash is not None and net and net > 0:
                    from decimal import Decimal
                    runway = round(cash / net, 1)

                defaults = {'portfolio_company': company}
                if gross is not None:
                    defaults['gross_burn'] = abs(gross)
                if net is not None:
                    defaults['net_burn'] = abs(net)
                if cash is not None:
                    defaults['cash_balance'] = abs(cash)
                if runway is not None:
                    defaults['runway_months'] = runway

                if len(defaults) > 1:  # at least one real field besides portfolio_company
                    CompanyFinancials.objects.update_or_create(
                        investment=inv,
                        period=period_date,
                        defaults=defaults,
                    )
                    count += 1

                # Extract SaaS KPIs from the same row — ONLY if the sheet has
                # actual SaaS-specific columns (detected via Gemini Pass 2).
                if _has_real_saas_cols:
                    for slug, _label, _fmt, _tmpl, canonical_field in SAAS_KPI_DEFS:
                        val = _find_col_decimal(row, canonical_field)
                        if val is None:
                            continue
                        kd = kpi_def_cache[slug]
                        PortfolioKPI.objects.update_or_create(
                            investment=inv,
                            kpi_definition=kd,
                            period=period_date,
                            defaults={
                                'portfolio_company': company,
                                'value': val,
                                'source': 'excel_upload',
                                'status': 'approved',
                            },
                        )

        logger.info(f'  Company financials: {count}')

    # ------------------------------------------------------------------
    # MIS Financials — P&L and Budget vs Actual for BudgetVsActual model
    # ------------------------------------------------------------------

    def _pl_pre_classify(self, labels):
        """Pre-classify a batch of P&L line item labels via Gemini.

        Populates self._pl_line_item_map so subsequent _pl_map_to_line_item() calls
        are instant cache lookups. Call once per sheet before the row processing loop.
        """
        new_labels = [l for l in labels if l and str(l).strip()
                      and str(l).strip().lower() not in self._pl_line_item_map]
        if not new_labels:
            return
        result = self._classify_labels(new_labels, 'pl_line_items',
                                        context='P&L / financial statement line item labels')
        for label, canonical in result.items():
            self._pl_line_item_map[label.lower().strip()] = canonical

    def _pl_map_to_line_item(self, text):
        """Map a column header or row label text to a canonical P&L line_item key.

        Uses self._pl_line_item_map (populated by _pl_pre_classify or on-demand Gemini).
        """
        if not text:
            return None
        t = str(text).lower().strip()
        if t in self._pl_line_item_map:
            return self._pl_line_item_map[t]
        # On-demand classify for labels not in the pre-classified batch
        result = self._classify_labels([text], 'pl_line_items',
                                        context='P&L / financial statement line item label')
        canonical = result.get(text)
        self._pl_line_item_map[t] = canonical
        return canonical

    @staticmethod
    def _pl_parse_year_month(period_str):
        """
        Parse period string → (year: int, month: int|None).

        Handles: Apr-24, Apr-2024, April 2024, 2024-04, Q1-FY25, FY2025, 2025, etc.
        Returns (None, None) if unparseable.
        """
        if not period_str:
            return None, None
        s = str(period_str).strip()

        MONTH_MAP = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
                     'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}

        # Apr-24, Apr-2024
        m = re.match(r'^([a-zA-Z]{3})[-/](\d{2,4})$', s)
        if m:
            mon = MONTH_MAP.get(m.group(1).lower())
            yr = int(m.group(2))
            if yr < 100:
                yr += 2000
            return yr, mon

        # April 2024, April-2024
        m = re.match(r'^([a-zA-Z]+)[\s\-](\d{4})$', s)
        if m:
            mon = MONTH_MAP.get(m.group(1).lower()[:3])
            yr = int(m.group(2))
            return yr, mon

        # 2024-04, 2024/04
        m = re.match(r'^(\d{4})[-/](\d{1,2})$', s)
        if m:
            return int(m.group(1)), int(m.group(2))

        # Q1-FY25, Q2 FY2025, Q3FY25
        m = re.match(r'^Q([1-4])[\s\-]?FY(\d{2,4})$', s, re.IGNORECASE)
        if m:
            q = int(m.group(1))
            yr = int(m.group(2))
            if yr < 100:
                yr += 2000
            # India FY: Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar
            mon = {1: 4, 2: 7, 3: 10, 4: 1}.get(q)
            return yr, mon

        # FY2025 or FY25 (annual, no month)
        m = re.match(r'^FY(\d{2,4})$', s, re.IGNORECASE)
        if m:
            yr = int(m.group(1))
            if yr < 100:
                yr += 2000
            return yr, None

        # Plain year: 2025
        m = re.match(r'^(\d{4})$', s)
        if m:
            return int(m.group(1)), None

        return None, None

    def _import_mis_financials(self, wb, org, fund, investments, companies, domain_map):
        """
        Import P&L and Budget vs Actual data for the MIS Consolidation module.

        Uses Gemini-classified domain_map to find financial sheets. All sheets
        classified as 'financials_pl_bva' or 'fund_pl_bs' are processed.
        Each sheet is tried as both P&L and BvA — the processing functions
        use column detection to determine the actual data type.
        """
        from mis_consolidation.services import BvAImporter, MISAggregator, AnomalyDetector

        # Get all sheets Gemini classified as financial statements
        fin_sheets = _dm_sheets(domain_map, 'financials_pl_bva')
        fund_fin_sheets = _dm_sheets(domain_map, 'fund_pl_bs')
        all_fin_sheets = fin_sheets + [s for s in fund_fin_sheets if s not in fin_sheets]

        # Pre-compute period labels from a NAV/accounting sheet if available.
        inferred_periods = self._infer_period_labels_from_wb(wb, domain_map)

        count = 0
        for sn in all_fin_sheets:
            if sn not in wb.sheetnames:
                continue
            # Try both P&L and BvA processing on every financial sheet.
            # Each processor uses column detection to determine if the sheet
            # matches its expected structure. Returns 0 if no match.
            try:
                count += self._process_pl_sheet(wb[sn], companies, fund,
                                                inferred_periods=inferred_periods)
            except Exception as e:
                logger.warning(f'P&L processing of sheet {sn!r} error: {e}')
            try:
                count += self._process_bva_sheet(wb[sn], companies, fund)
            except Exception as e:
                logger.warning(f'BvA processing of sheet {sn!r} error: {e}')

        if count > 0:
            try:
                MISAggregator(fund=fund).run()
            except Exception as e:
                logger.warning(f'MISAggregator error: {e}')
            for company in companies.values():
                try:
                    AnomalyDetector(company).run_all()
                except Exception:
                    pass

        logger.info(f'  MIS financials: {count} records')

    def _infer_period_labels_from_wb(self, wb, domain_map=None):
        """
        Scan Gemini-classified sheets for explicit month-period column headers
        (e.g. 'Oct-24', 'Nov-24', …) and return an ordered list of those labels.

        Used to annotate fund-level P&L sheets that carry value columns but no headers.
        Uses only Gemini-classified sheets: nav_accounting, financials_pl_bva,
        fund_pl_bs, nav_calculation.
        """
        _period_re = re.compile(
            r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/]\d{2,4}$',
            re.IGNORECASE)
        # Use only Gemini-classified sheets — no all-sheets fallback
        candidate_sheets = []
        if domain_map:
            for domain in ('nav_accounting', 'financials_pl_bva', 'fund_pl_bs', 'nav_calculation'):
                for s in _dm_sheets(domain_map, domain):
                    if s in wb.sheetnames and s not in candidate_sheets:
                        candidate_sheets.append(s)

        for sn in candidate_sheets:
            ws = wb[sn]
            for row in ws.iter_rows(min_row=1, max_row=5, values_only=True):
                labels = []
                for cell in row:
                    if cell and isinstance(cell, str) and _period_re.match(cell.strip()):
                        labels.append(cell.strip())
                if len(labels) >= 3:
                    return labels   # e.g. ['Oct-24','Nov-24','Dec-24','Jan-25','Feb-25','Mar-25']
        return []

    def _process_pl_sheet(self, ws, companies, fund=None, inferred_periods=None):
        """
        Process a P&L / Balance Sheet / Cash Flow sheet and write BudgetVsActual records.

        Detects layouts automatically:
        A) Horizontal: rows = (company, period), each col is a P&L line item.
           Sub-case A2: cols carry budget/actual qualifier ("Revenue Budget", "EBITDA Actual").
        B) Vertical pivot: first col = line item label, remaining cols = time periods
           with company identified by a preceding single-cell section header row.
           Sub-case B2: period cols carry qualifier ("Budget Apr-24", "Apr-24 Actual").
        C) Fund-level transposed: first col = line item, remaining cols = unlabelled period
           values. inferred_periods provides the period labels for those columns.
        """
        from mis_consolidation.models import BudgetVsActual, ConsolidatedMIS

        # Period regex — uses standard financial month abbreviations (universal)
        PERIOD_RE = re.compile(
            r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/]\d{2,4}$'
            r'|^Q[1-4][\s\-]?FY\d{2,4}$'
            r'|^\d{4}[-/]\d{1,2}$'
            r'|^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s]\d{4}$',
            re.IGNORECASE,
        )
        # Inner period pattern (without anchors) for extracting period from qualified headers
        _INNER_PERIOD_RE = re.compile(
            r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/\s]\d{2,4}'
            r'|Q[1-4][\s\-]?FY\d{2,4}|\d{4}[-/]\d{1,2})',
            re.IGNORECASE,
        )

        headers_dict, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
        if not rows:
            return 0

        period_cols = [h for h in headers_dict.keys()
                       if h and PERIOD_RE.match(str(h).strip())]

        # Detect qualified period columns ("Budget Apr-24", "Apr-24 Actual")
        # via Gemini: classify non-period column headers as budget/actual/variance
        _candidate_qualified = []
        for h in headers_dict.keys():
            if not h or h in period_cols:
                continue
            h_str = str(h).strip()
            m = _INNER_PERIOD_RE.search(h_str)
            if m:
                _candidate_qualified.append(h)

        budget_period_cols = {}   # header_text → (yr, mo)
        actual_period_cols = {}   # header_text → (yr, mo)
        if _candidate_qualified:
            _qual_map = self._classify_enum(
                _candidate_qualified, 'column_qualifier',
                context='Column headers from a P&L / Budget vs Actual financial sheet. '
                        'Classify each as budget (planned), actual (realized), or variance.')
            for h in _candidate_qualified:
                h_str = str(h).strip()
                m = _INNER_PERIOD_RE.search(h_str)
                if not m:
                    continue
                period_part = m.group(1).strip()
                yr_q, mo_q = self._pl_parse_year_month(period_part)
                if yr_q:
                    qual = _qual_map.get(h, 'actual')
                    if qual == 'budget':
                        budget_period_cols[h] = (yr_q, mo_q)
                    elif qual != 'variance':
                        actual_period_cols[h] = (yr_q, mo_q)

        all_period_like_cols = period_cols or list(budget_period_cols) or list(actual_period_cols)
        count = 0

        if all_period_like_cols:
            # -----------------------------------------------------------------
            # Layout B / B2: vertical pivot — rows are line items, cols are periods.
            # Company name comes from a preceding single-cell section header row.
            # -----------------------------------------------------------------
            current_company = None

            for row in rows:
                # Normalise keys once per row so all _find_col_* calls work
                # regardless of unit-suffix or CamelCase formatting.
                nr = _norm_row(row)
                non_empty_vals = [v for v in row.values() if v and str(v).strip()]

                # Single non-empty cell in row → possible company section header
                if len(non_empty_vals) == 1:
                    candidate = str(non_empty_vals[0]).strip()
                    if not _is_junk_row(candidate):
                        matched = (companies.get(candidate)
                                   or next((v for k, v in companies.items()
                                            if k.lower() == candidate.lower()), None))
                        if matched:
                            current_company = matched
                    continue

                # Try horizontal layout: does this row have a Company column?
                name = _find_col_str(nr, 'Company Name', 'Company', 'Name', 'Entity',
                                     'Investee', 'Portfolio Company')
                if name and not _is_junk_row(name):
                    co = (companies.get(name)
                          or next((v for k, v in companies.items()
                                   if k.lower() == name.lower()), None))
                    if co:
                        for pcol in period_cols:
                            val = _d(row.get(pcol))
                            if val is None:
                                continue
                            yr, mo = self._pl_parse_year_month(pcol)
                            if not yr:
                                continue
                            li_key = self._pl_map_to_line_item(pcol)
                            if not li_key:
                                continue
                            BudgetVsActual.objects.update_or_create(
                                portfolio_company=co,
                                fund=fund,
                                period_year=yr, period_month=mo,
                                period_quarter='', line_item=li_key,
                                defaults={'period_type': 'monthly' if mo else 'annual',
                                          'actual_inr': abs(val)},
                            )
                            count += 1
                        continue

                # Vertical pivot row: first column is the line item label.
                # Use normalised row for the label lookup so 'LineItem' → 'Line Item' works.
                label = _find_col_str(nr, 'Particulars', 'Line Item', 'Description',
                                      'Account', 'Item', 'Category')
                if not label:
                    label = non_empty_vals[0] if non_empty_vals else None
                # Also normalise the label itself before mapping (e.g. 'GrossProfit' → 'Gross Profit')
                label = _normalize_col_key(label) if label else None
                li_key = self._pl_map_to_line_item(label) if label else None
                if not li_key or not current_company:
                    continue

                # Plain period cols → actual
                for pcol in period_cols:
                    val = _d(row.get(pcol))
                    if val is None:
                        continue
                    yr, mo = self._pl_parse_year_month(pcol)
                    if not yr:
                        continue
                    BudgetVsActual.objects.update_or_create(
                        portfolio_company=current_company,
                        fund=fund,
                        period_year=yr, period_month=mo,
                        period_quarter='', line_item=li_key,
                        defaults={'period_type': 'monthly' if mo else 'annual',
                                  'actual_inr': abs(val)},
                    )
                    count += 1

                # "Budget Apr-24" qualified cols → budget_inr
                for pcol, (yr, mo) in budget_period_cols.items():
                    val = _d(row.get(pcol))
                    if val is None:
                        continue
                    BudgetVsActual.objects.update_or_create(
                        portfolio_company=current_company,
                        fund=fund,
                        period_year=yr, period_month=mo,
                        period_quarter='', line_item=li_key,
                        defaults={'period_type': 'monthly' if mo else 'annual',
                                  'budget_inr': abs(val)},
                    )
                    count += 1

                # "Apr-24 Actual" qualified cols → actual_inr
                for pcol, (yr, mo) in actual_period_cols.items():
                    val = _d(row.get(pcol))
                    if val is None:
                        continue
                    BudgetVsActual.objects.update_or_create(
                        portfolio_company=current_company,
                        fund=fund,
                        period_year=yr, period_month=mo,
                        period_quarter='', line_item=li_key,
                        defaults={'period_type': 'monthly' if mo else 'annual',
                                  'actual_inr': abs(val)},
                    )
                    count += 1

        else:
            # -----------------------------------------------------------------
            # Layout A / A2: horizontal — rows = companies, cols = P&L line items.
            # Handles both pure line-item columns ("Revenue") and
            # budget/actual-qualified columns ("Revenue Budget", "Budgeted EBITDA").
            # Period may be a column or absent (snapshot = today).
            # -----------------------------------------------------------------
            today = date.today()
            snapshot_yr, snapshot_mo = today.year, today.month

            for row in rows:
                # Normalise keys: 'Revenue(Cr)' → 'Revenue', 'GrossProfit(Cr)' → 'Gross Profit'
                nr = _norm_row(row)

                name = _find_col_str(nr, 'Company Name', 'Company', 'Name',
                                     'Entity', 'Investee', 'Portfolio Company')
                if not name or _is_junk_row(name):
                    continue

                co = (companies.get(name)
                      or next((v for k, v in companies.items()
                               if k.lower() == name.lower()), None))
                if not co:
                    continue

                period_str = _find_col_str(nr, 'Period', 'Month', 'Date',
                                           'Reporting Period', 'Reporting Month',
                                           'Quarter', 'FY', 'Financial Year')
                yr, mo = (self._pl_parse_year_month(period_str)
                          if period_str else (snapshot_yr, snapshot_mo))
                if not yr:
                    yr, mo = snapshot_yr, snapshot_mo

                # Pre-classify column headers as budget/actual/variance via Gemini
                _col_names = [str(c).strip() for c in nr.keys() if c and str(c).strip()]
                _col_qual_map = self._classify_enum(
                    _col_names, 'column_qualifier',
                    context='Column headers from a P&L / financial statement. '
                            'Classify each as budget, actual, or variance.')

                for col_name, col_val in nr.items():
                    if not col_name:
                        continue
                    # Skip variance columns (detected by Gemini)
                    col_qualifier = _col_qual_map.get(str(col_name).strip(), 'actual')
                    if col_qualifier == 'variance':
                        continue

                    is_budget_col = (col_qualifier == 'budget')

                    li_key = self._pl_map_to_line_item(col_name)
                    if not li_key:
                        continue

                    val = _d(col_val)
                    if val is None:
                        continue

                    upd = {'period_type': 'monthly' if mo else 'annual'}
                    if is_budget_col:
                        upd['budget_inr'] = abs(val)
                    else:
                        upd['actual_inr'] = abs(val)

                    BudgetVsActual.objects.update_or_create(
                        portfolio_company=co,
                        fund=fund,
                        period_year=yr, period_month=mo,
                        period_quarter='', line_item=li_key,
                        defaults=upd,
                    )
                    count += 1

        if count == 0 and inferred_periods and fund:
            # ----------------------------------------------------------------
            # Layout C: Fund-level transposed P&L with no period headers.
            # The sheet has line items in column A and numeric values in columns B+.
            # We map column positions to period labels from inferred_periods.
            # Writes to ConsolidatedMIS (not BudgetVsActual — no company breakdown).
            # ----------------------------------------------------------------
            count += self._process_fund_level_pl(
                ws, fund, inferred_periods, len(companies))

        return count

    def _process_fund_level_pl(self, ws, fund, period_labels, company_count=0):
        """
        Import a fund-level transposed P&L sheet into ConsolidatedMIS.

        Only runs for P&L sheets — skips Balance Sheet and Cash Flow sheets
        which have non-P&L line items that would pollute the fund P&L.

        Sheet format:
          Col A: line item label  (Revenue, EBITDA, ...)
          Col B..N: numeric values for successive periods (no header labels)

        period_labels: ordered list of period strings inferred from another sheet,
        e.g. ['Oct-24','Nov-24','Dec-24','Jan-25','Feb-25','Mar-25'].
        We map col B → period_labels[0], col C → period_labels[1], etc.
        The last column is often a total/YTD — skip it if len(cols) > len(labels).
        """
        from mis_consolidation.models import ConsolidatedMIS
        from django.utils import timezone as _tz

        # Skip Balance Sheet and Cash Flow sheets — their line items (assets, liabilities,
        # cash inflows) are not P&L items and would corrupt the fund P&L view.
        # Uses Gemini Pass 1.5 section sub-domains: P&L sheets have only 'unknown'
        # sub-domains, while BS/CF sheets have structured sub-domains (nav_records,
        # distributions, investments, capital_call_headers).
        _ws_title = getattr(ws, 'title', '')
        _sheet_sections = self._section_map.get(_ws_title, {})
        _sub_domains = set(_sheet_sections.values())
        _non_pl_indicators = {'nav_records', 'distributions', 'investments',
                              'capital_call_headers'}
        if _sub_domains & _non_pl_indicators:
            return 0

        if not period_labels:
            return 0

        # Find the first data row: skip title rows and section headers
        # We scan row-by-row; a data row has a non-junk string in col A and ≥1 numeric cols
        data_rows = []   # list of (line_item_label, [val_col_B, val_col_C, ...])
        for row in ws.iter_rows(min_row=1, values_only=True):
            if not row or not row[0]:
                continue
            label = str(row[0]).strip()
            if _is_junk_row(label):
                continue
            li_key = self._pl_map_to_line_item(label)
            if not li_key:
                continue
            # Collect numeric values from columns 1 onward (0-indexed)
            vals = []
            for cell in row[1:]:
                try:
                    vals.append(Decimal(str(cell)) if cell is not None else None)
                except Exception:
                    vals.append(None)
            if not any(v is not None for v in vals):
                continue
            data_rows.append((li_key, vals))

        if not data_rows:
            return 0

        default_scheme = Scheme.objects.filter(fund=fund).first()

        count = 0
        for li_key, vals in data_rows:
            for i, period_str in enumerate(period_labels):
                if i >= len(vals):
                    break
                val = vals[i]
                if val is None:
                    continue
                yr, mo = self._pl_parse_year_month(period_str)
                if not yr:
                    continue
                pq = ''
                if mo:
                    pq = ('Q1' if mo in (4, 5, 6) else
                          'Q2' if mo in (7, 8, 9) else
                          'Q3' if mo in (10, 11, 12) else 'Q4')
                ConsolidatedMIS.objects.update_or_create(
                    organization=self.org,
                    fund=fund,
                    scheme=default_scheme,
                    period_year=yr,
                    period_month=mo,
                    period_quarter=pq,
                    line_item=li_key,
                    defaults={
                        'period_type': 'monthly',
                        'total_actual_inr': abs(val),
                        'company_count': company_count,
                        'computed_at': _tz.now(),
                    },
                )
                count += 1

        logger.info(f'  Fund-level P&L → ConsolidatedMIS: {count} records')
        return count

    def _process_bva_sheet(self, ws, companies, fund=None):
        """
        Process a Budget vs Actual sheet and write BudgetVsActual or ConsolidatedMIS records.

        Handles two formats:
        A) Company-level: Company | Line Item | Budget | Actual | (Period)
           Each row is one company × line_item → written to BudgetVsActual.
        B) Fund-level: Metric | Q1 Bdgt | Q1 Actual | Q2 Bdgt | Q2 Actual | FY Budget | FY Actual
           Rows are fund-level metrics, columns are period×qualifier pairs.
           Written directly to ConsolidatedMIS (no per-company breakdown available).
        """
        from mis_consolidation.models import BudgetVsActual, ConsolidatedMIS

        headers_dict, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
        if not rows:
            return 0

        today = date.today()
        count = 0

        # Scan first few rows (before the header row) for FY period information.
        # Many fund BvA sheets have a title like "BUDGET VS ACTUAL — FY 2024-25 YTD".
        # If found, use that as the default period for rows with no explicit Period column,
        # instead of falling back to today's date.
        _sheet_default_yr = None
        _sheet_default_mo = None
        _FY_RE = re.compile(r'FY\s*\d{2,4}\s*[-–]\s*(\d{2,4})', re.IGNORECASE)
        _YEAR_RE = re.compile(r'\b(20\d{2})\b')
        for _r in range(1, min(ws.max_row + 1, 6)):
            for _c in range(1, min(ws.max_column + 1, 4)):
                _cell_val = ws.cell(_r, _c).value
                if not _cell_val or not isinstance(_cell_val, str):
                    continue
                _fy_m = _FY_RE.search(_cell_val)
                if _fy_m:
                    _end = int(_fy_m.group(1))
                    _sheet_default_yr = _end + 2000 if _end < 100 else _end
                    _sheet_default_mo = None   # annual record — no specific month
                    break
                _yr_m = _YEAR_RE.search(_cell_val)
                if _yr_m and not _sheet_default_yr:
                    _sheet_default_yr = int(_yr_m.group(1))
            if _sheet_default_yr:
                break

        for row in rows:
            # Normalise column keys: strips unit annotations ('Budget(₹Cr)' → 'Budget',
            # 'Actual (INR Mn)' → 'Actual') and splits CamelCase ('GrossProfit' → 'Gross Profit').
            # This makes _find_col_* robust to any Excel formatting convention.
            nr = _norm_row(row)

            name = _find_col_str(nr, 'Company Name', 'Company', 'Name',
                                 'Entity', 'Investee', 'Portfolio Company')
            if not name or _is_junk_row(name):
                continue

            co = (companies.get(name)
                  or next((v for k, v in companies.items()
                           if k.lower() == name.lower()), None))
            if not co:
                continue

            line_item_str = _find_col_str(nr, 'Line Item', 'Item', 'Particulars',
                                          'Account', 'Description', 'Category',
                                          'P&L Item', 'Financial Item', 'Metric')
            li_key = self._pl_map_to_line_item(line_item_str) if line_item_str else None
            if not li_key:
                continue

            # Gemini Pass 2 adds canonical field names as row keys
            budget = _find_col_decimal(nr, 'budget', 'budget_amount')
            actual = _find_col_decimal(nr, 'actual', 'actual_amount')

            if budget is None and actual is None:
                continue

            period_str = _find_col_str(nr, 'Period', 'Month', 'Date',
                                       'Reporting Period', 'Quarter', 'FY',
                                       'Financial Year', 'Reporting Month')
            yr, mo = (self._pl_parse_year_month(period_str)
                      if period_str
                      else (_sheet_default_yr or today.year, _sheet_default_mo))
            if not yr:
                yr, mo = _sheet_default_yr or today.year, _sheet_default_mo

            # India FY quarter mapping
            pq = ''
            if mo:
                pq = 'Q1' if mo in (4, 5, 6) else (
                     'Q2' if mo in (7, 8, 9) else (
                     'Q3' if mo in (10, 11, 12) else 'Q4'))

            defaults = {'period_type': 'monthly' if mo else 'annual'}
            if budget is not None:
                defaults['budget_inr'] = budget
            if actual is not None:
                defaults['actual_inr'] = abs(actual)

            BudgetVsActual.objects.update_or_create(
                portfolio_company=co,
                fund=fund,
                period_year=yr,
                period_month=mo,
                period_quarter=pq,
                line_item=li_key,
                defaults=defaults,
            )
            count += 1

        if count == 0:
            # ----------------------------------------------------------------
            # Fund-level BvA fallback:
            # No company column found. Try to detect the format:
            #   Metric | Q1 Bdgt | Q1 Actual | Q2 Bdgt | Q2 Actual | FY Budget | FY Actual
            # If detected, write directly to ConsolidatedMIS.
            # ----------------------------------------------------------------
            count += self._process_fund_level_bva(ws, headers_dict, rows, fund,
                                                   _sheet_default_yr,
                                                   company_count=len(companies))

        return count

    def _process_fund_level_bva(self, ws, headers_dict, rows, fund, default_yr=None,
                                company_count=0):
        """
        Import fund-level Budget vs Actual into ConsolidatedMIS.

        Detects columns like 'Q1 Bdgt', 'Q1 Actual', 'Q2 Bdgt', 'FY Budget', 'FY Actual'
        and maps each row's Metric value to a canonical line_item.
        Works universally: 'AOP', 'Plan', 'Target' are treated as budget qualifiers;
        'Actuals', 'Reported', 'YTD Actual' are treated as actual qualifiers.
        """
        if not fund:
            return 0
        from mis_consolidation.models import ConsolidatedMIS

        # Regex for quarter/FY detection (universal financial tokens)
        _Q_RE = re.compile(r'^(Q[1-4])\s*[-\s]?\s*(.+)', re.IGNORECASE)
        _FY_Q_RE = re.compile(r'^(FY|annual|full.?year)\s*(.+)', re.IGNORECASE)

        # Collect candidate column headers for budget/actual classification
        _candidate_cols = [str(h).strip() for h in headers_dict if h and str(h).strip()]

        # Classify ALL column headers via Gemini (budget/actual/variance)
        _col_qual_map = self._classify_enum(
            _candidate_cols, 'column_qualifier',
            context='Column headers from a fund Budget vs Actual sheet. '
                    'Classify each as budget (planned), actual (realized), or variance.')

        # Map column headers → (period_quarter, period_type, is_budget)
        period_budget_cols = {}
        for h in headers_dict:
            if not h:
                continue
            h_s = str(h).strip()
            qual = _col_qual_map.get(h_s, 'actual')
            if qual == 'variance':
                continue
            is_bud = (qual == 'budget')

            m = _Q_RE.match(h_s)
            if m:
                qtr = m.group(1).upper()
                period_budget_cols[h] = (qtr, 'quarterly', is_bud)
                continue
            m2 = _FY_Q_RE.match(h_s)
            if m2:
                period_budget_cols[h] = ('FY', 'annual', is_bud)
                continue
            # Non-period columns classified as budget or actual → annual
            if qual in ('budget', 'actual'):
                period_budget_cols[h] = ('FY', 'annual', is_bud)

        if not period_budget_cols:
            return 0

        yr = default_yr
        if not yr:
            # Extract year from the sheet title row
            for _r in range(1, min(ws.max_row + 1, 4)):
                _v = ws.cell(_r, 1).value
                if isinstance(_v, str):
                    _ym = re.search(r'\b(20\d{2})\b', _v)
                    if _ym:
                        yr = int(_ym.group(1))
                        break
        if not yr:
            from datetime import date as _date
            yr = _date.today().year

        # Find the default scheme for this fund
        default_scheme = Scheme.objects.filter(fund=fund).first()

        # Fund-level performance metrics (Net IRR, TVPI, Portfolio FV) found in
        # BvA sheets as special metric rows — not P&L line items.  We detect them
        # via Gemini and store as ConsolidatedMIS records with special line_item keys.

        # Pre-classify all metric labels for fund-level metric detection
        all_metric_labels = set()
        for row in rows:
            nr = _norm_row(row)
            m = _find_col_str(nr, 'Metric', 'Line Item', 'Particulars',
                              'Item', 'Category', 'Description')
            if m and not _is_junk_row(m):
                all_metric_labels.add(m)
        fund_metric_map = self._classify_labels(all_metric_labels, 'fund_metrics',
                                                 context='Fund-level performance metrics (IRR, TVPI, FV)')

        count = 0
        from django.utils import timezone as _tz
        for row in rows:
            nr = _norm_row(row)
            metric = _find_col_str(nr, 'Metric', 'Line Item', 'Particulars',
                                   'Item', 'Category', 'Description')
            if not metric or _is_junk_row(metric):
                continue

            # Check if this is a special fund-level performance metric row
            special_li_key = fund_metric_map.get(metric)

            if special_li_key:
                # Extract FY Actual value (the most authoritative single number)
                fy_actual = None
                fy_budget = None
                for col_h, (qtr, ptype, is_bud) in period_budget_cols.items():
                    if ptype == 'annual':
                        val = _d(row.get(col_h))
                        if val is None:
                            continue
                        if is_bud:
                            fy_budget = val
                        else:
                            fy_actual = val
                if fy_actual is None:
                    # Fallback: any actual column
                    for col_h, (qtr, ptype, is_bud) in period_budget_cols.items():
                        if not is_bud:
                            val = _d(row.get(col_h))
                            if val is not None:
                                fy_actual = val
                                break

                if fy_actual is not None:
                    # For IRR/TVPI: convert fraction → percentage if looks like fraction
                    stored_val = fy_actual
                    if special_li_key == 'net_irr' and abs(float(fy_actual)) <= 2:
                        stored_val = fy_actual * 100  # 0.212 → 21.2
                    elif special_li_key == 'tvpi' and abs(float(fy_actual)) <= 5:
                        stored_val = fy_actual  # keep as ratio (e.g. 1.567x)

                    upd_fm = {
                        'period_type': 'annual',
                        'company_count': company_count,
                        'total_actual_inr': stored_val,
                        'computed_at': _tz.now(),
                    }
                    if fy_budget is not None:
                        bud_stored = fy_budget
                        if special_li_key == 'net_irr' and abs(float(fy_budget)) <= 2:
                            bud_stored = fy_budget * 100
                        upd_fm['total_budget_inr'] = bud_stored
                    ConsolidatedMIS.objects.update_or_create(
                        organization=self.org,
                        fund=fund,
                        scheme=default_scheme,
                        period_year=yr,
                        period_month=None,
                        period_quarter='FY',
                        line_item=special_li_key,
                        defaults=upd_fm,
                    )
                    count += 1
                continue  # Don't fall through to standard P&L processing

            li_key = self._pl_map_to_line_item(metric)
            if not li_key:
                continue

            # Accumulate budget and actual per (quarter, period_type) pair
            period_values = {}   # (quarter, period_type) → {'budget': x, 'actual': x}
            for col_h, (qtr, ptype, is_bud) in period_budget_cols.items():
                val = _d(row.get(col_h))
                if val is None:
                    continue
                key = (qtr, ptype)
                if key not in period_values:
                    period_values[key] = {}
                if is_bud:
                    period_values[key]['budget'] = val
                else:
                    period_values[key]['actual'] = val

            for (qtr, ptype), vals in period_values.items():
                bud = vals.get('budget')
                act = vals.get('actual')
                if bud is None and act is None:
                    continue
                variance = None
                variance_pct = None
                if bud is not None and act is not None and bud != 0:
                    variance = act - bud
                    variance_pct = float(variance / bud * 100)
                upd = {'period_type': ptype, 'company_count': company_count}
                if bud is not None:
                    upd['total_budget_inr'] = abs(bud)
                if act is not None:
                    upd['total_actual_inr'] = abs(act)
                if variance is not None:
                    upd['total_variance_inr'] = variance
                if variance_pct is not None:
                    upd['total_variance_pct'] = variance_pct
                upd['computed_at'] = _tz.now()
                ConsolidatedMIS.objects.update_or_create(
                    organization=self.org,
                    fund=fund,
                    scheme=default_scheme,
                    period_year=yr,
                    period_month=None,
                    period_quarter=qtr,
                    line_item=li_key,
                    defaults=upd,
                )
                count += 1

        logger.info(f'  Fund-level BvA → ConsolidatedMIS: {count} records')
        return count

    # ------------------------------------------------------------------
    # Quoted & Unquoted classification
    # ------------------------------------------------------------------

    def _import_quoted_unquoted(self, wb, org, investments, companies, domain_map):
        """Read the Quoted & Unquoted Shares sheet and update PortfolioCompany.is_quoted.

        Uses Gemini-classified 'quoted_unquoted' domain from domain_map.
        Falls back to 'valuations_kpis' since IPEV level data sometimes
        lives alongside valuations.
        """
        sheets = _dm_sheets(domain_map, 'quoted_unquoted')
        if not sheets:
            sheets = _dm_sheets(domain_map, 'valuations_kpis')
        if not sheets:
            return

        target_sheet = sheets[0]
        if target_sheet not in wb.sheetnames:
            return

        ws = wb[target_sheet]
        _headers, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
        if not rows:
            return

        count = 0
        for row in rows:
            name = _find_col_str(row, 'Company Name', 'Company', 'Name',
                                  'Portfolio Company', 'Investee', 'Entity Name')
            if not name or _is_junk_row(name):
                continue

            share_type = _find_col_str(
                row,
                'Share Type', 'Share Class', 'Instrument Type',
                'Equity Type', 'Security Type', 'Instrument', 'Type of Share',
                'Nature of Instrument', 'Class of Shares', 'Type',
            )
            ipev_raw = _find_col_str(
                row,
                'IPEV Level', 'IPEV Classification', 'IPEV', 'Level',
                'Valuation Level', 'Fair Value Hierarchy', 'Level (IPEV)',
                'Measurement Level', 'FV Level', 'Fair Value Level',
            )
            exchange = _find_col_str(
                row,
                'Exchange', 'Listed On', 'Stock Exchange', 'Listing Exchange',
                'Market', 'Primary Exchange', 'Exchange Name',
                'listing_exchange',
            )

            # Determine quoted status via Gemini classification
            classify_input = share_type or ipev_raw or ''
            if classify_input.strip():
                qs_result = self._classify_enum(
                    [classify_input], 'quoted_status',
                    context='Share type / instrument type classification: quoted vs unquoted')
                is_quoted = (qs_result.get(classify_input) == 'quoted')
            else:
                is_quoted = False

            pc = companies.get(name)
            if pc:
                update = {'is_quoted': is_quoted}
                if exchange:
                    update['listing_exchange'] = exchange.upper()
                elif is_quoted and not pc.listing_exchange:
                    # Mark as 'BSE/NSE' generically for Indian listed companies
                    update['listing_exchange'] = 'LISTED'
                PortfolioCompany.objects.filter(pk=pc.pk).update(**update)
                # Refresh the cached object so views see updated values
                pc.is_quoted = is_quoted
                if 'listing_exchange' in update:
                    pc.listing_exchange = update['listing_exchange']
                count += 1

        logger.info(f'  Quoted/Unquoted classification: {count} companies updated')

    # ------------------------------------------------------------------
    # NAV records
    # ------------------------------------------------------------------

    def _import_nav(self, wb, schemes, domain_map):
        """Import NAV records from Gemini-classified NAV sheets.

        Handles three formats:
        - Format A: Flat table with one row per period (Period | NAV | Units | ...)
        - Format B: Multi-section sheet with NAV RECORDS header
        - Format C: Transposed table where rows = metrics, columns = periods
          (Component | Oct-24 | Nov-24 | Dec-24 | ...)

        Uses Gemini domain_map to find nav_accounting sheets (time-series NAV
        data). Key-value computation sheets (nav_calculation) are consumed
        separately in _enrich_nav_records_post_import().
        """
        default_scheme = list(schemes.values())[0] if schemes else None
        if not default_scheme:
            return

        # Use Gemini-classified nav_accounting sheets only
        candidate_sheets = [s for s in _dm_sheets(domain_map, 'nav_accounting')
                            if s in wb.sheetnames]
        if not candidate_sheets:
            logger.info('  No Gemini-classified nav_accounting sheets found')
            return

        # Regex for period-column headers (e.g. "Oct-24", "Nov-24")
        _period_re = re.compile(
            r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/]\d{2,4}$',
            re.IGNORECASE)

        # Try each candidate sheet until we find one that yields NAV records
        for sheet_name in candidate_sheets:
            ws = wb[sheet_name]
            nav_count = self._try_import_nav_from_sheet(
                ws, sheet_name, default_scheme, schemes, wb, _period_re,
                domain_map=domain_map)
            if nav_count > 0:
                logger.info(f'  NAV imported from sheet: "{sheet_name}" ({nav_count} records)')
                return

        logger.info('  No NAV records found in any sheet')

    def _try_import_nav_from_sheet(self, ws, sheet_name, default_scheme,
                                    schemes, wb, _period_re, domain_map=None):
        """Try to import NAV records from a single worksheet.

        Returns the number of NAV records created (0 if the sheet format
        is not suitable for NAV time-series data).
        """
        headers_dict, table_rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
        period_cols = [h for h in headers_dict.keys() if _period_re.match(h.strip())]

        if period_cols and table_rows:
            # ── Format C: Transposed (Component | Oct-24 | Nov-24 | …) ──────────
            # Classify all component labels via Gemini in one batch call
            all_comp_labels = set()
            for row in table_rows:
                comp = _find_col_str(row, 'Component', 'Line Item', 'Item',
                                     'Metric', 'Particulars', 'P&L Line',
                                     'Cash Flow Item', 'Parameter')
                if comp:
                    all_comp_labels.add(comp)
            nav_comp_map = self._classify_labels(all_comp_labels, 'nav_components',
                                                  context='NAV statement components')

            period_data = {p: {} for p in period_cols}
            has_nav_row = False
            for row in table_rows:
                comp = _find_col_str(row, 'Component', 'Line Item', 'Item',
                                     'Metric', 'Particulars', 'P&L Line',
                                     'Cash Flow Item', 'Parameter')
                if not comp:
                    continue
                nav_type = nav_comp_map.get(comp)
                if not nav_type:
                    continue
                for pcol in period_cols:
                    val = _d(row.get(pcol))
                    if val is None:
                        continue
                    if nav_type == 'total_nav':
                        period_data[pcol]['total_nav'] = val
                        has_nav_row = True
                    elif nav_type == 'unrealized_gains':
                        period_data[pcol]['unrealized'] = val
                    elif nav_type == 'realized_gains':
                        period_data[pcol]['realized'] = val
                    elif nav_type == 'mgmt_fee':
                        period_data[pcol]['mgmt_fee'] = abs(val)
                    elif nav_type == 'carry_provision':
                        period_data[pcol]['carry'] = val
                    elif nav_type == 'investment_income':
                        period_data[pcol]['income'] = val

            # Only proceed if we found at least one row that looks like NAV data
            if not has_nav_row:
                return 0

            import calendar as _cal
            count = 0
            for pcol in period_cols:
                # Skip aggregation columns (H2 Total, FY Total, Grand Total, etc.)
                if _is_junk_row(pcol):
                    continue
                pd = period_data.get(pcol, {})
                nav_date = self._parse_period(pcol)
                if not nav_date:
                    continue
                total_nav = pd.get('total_nav')
                if not total_nav:
                    continue
                # Last day of the month
                last_day = _cal.monthrange(nav_date.year, nav_date.month)[1]
                nav_date = nav_date.replace(day=last_day)
                update_fields = {
                    'total_nav': total_nav,
                    'nav_per_unit': Decimal('0'),
                    'total_units_outstanding': Decimal('0'),
                }
                if pd.get('unrealized'): update_fields['unrealized_gains'] = pd['unrealized']
                if pd.get('realized'):   update_fields['realized_gains'] = pd['realized']
                if pd.get('mgmt_fee'):   update_fields['management_fee_payable'] = pd['mgmt_fee']
                NAVRecord.objects.update_or_create(
                    scheme=default_scheme,
                    nav_date=nav_date,
                    defaults=update_fields,
                )
                count += 1

            if count > 0:
                # Enrich with NAV/Unit and realized gains from other sheets
                self._enrich_nav_records_post_import(wb, default_scheme, domain_map)
            return count

        # ── Format B: Multi-section ──────────────────────────────────────────
        sections = self._read_sheet_via_layout(ws, alias_map=self._get_alias(ws))
        nav_rows = None
        for sec_name, (sec_headers, sec_rows) in sections.items():
            subdomain = self._get_section_subdomain(sheet_name, sec_name)
            if subdomain == 'nav_records' or sec_name == '__default__':
                nav_rows = sec_rows
                break

        if not nav_rows:
            nav_rows = table_rows  # already-read flat table

        if not nav_rows:
            return 0

        # ── Format A: Flat table (Period | NAV | Units | …) ──────────────────
        count = 0
        for row in nav_rows:
            scheme_name_raw = _find_col_str(
                row, 'Scheme', 'Scheme Name', 'Fund Scheme')
            target_scheme = default_scheme
            if scheme_name_raw:
                target_scheme = schemes.get(scheme_name_raw, default_scheme)

            period_raw = _find_col(
                row, 'Period', 'NAV Date', 'Date', 'Month', 'nav_date')
            nav_date = None
            if period_raw:
                nav_date = _date(period_raw)
                if not nav_date and isinstance(period_raw, str):
                    nav_date = self._parse_period(period_raw)
            if not nav_date:
                continue

            total_nav = _find_col_decimal(
                row, 'Total NAV (Cr)', 'Total NAV', 'NAV',
                'Net Asset Value', 'total_nav')

            # ── Fix B: NAV formula-derived fallback (no hardcoded SEBI formula) ──
            # Many fund Excel files define "Total NAV" as a calculated cell
            # (e.g. NAV sheet header row 1 says "Col I TotalNAV = C+E+F-D")
            # but the workbook is saved without cached values → the cell
            # comes through as None/0 here.  Pass 2.5 Gemini layout call
            # identifies these derived columns and returns the formula in
            # canonical form (sum of signed source-column references).  We
            # evaluate it from the OTHER cells in this same row.  Universal:
            # works for any Excel where Gemini sees the formula in a
            # disclaimer row OR recognises a standard SEBI AIF identity.
            if (not total_nav or total_nav == 0):
                derived = self._derived_column_for(
                    sheet_name,
                    ['Total NAV', 'Total NAV(₹Cr)', 'Total NAV (Cr)', 'NAV',
                     'Net Asset Value', 'total_nav'],
                )
                if derived:
                    computed = self._evaluate_derived_formula(derived, row)
                    if computed is not None:
                        total_nav = computed
                        logger.info(
                            f'NAV row computed from Gemini-derived formula: '
                            f'{[c["sign"]+c["source_column"] for c in derived["formula_components"]]} '
                            f'= {computed} (sheet="{sheet_name}")'
                        )

            nav_per_unit = _find_col_decimal(
                row, 'NAV/Unit (INR)', 'NAV/Unit(₹)', 'NAV/Unit',
                'NAV Per Unit', 'nav_per_unit')
            inv_fv = _find_col_decimal(
                row, 'Investments at FV (Cr)', 'Investments at FV',
                'Total Investments', 'investments_at_fair_value')
            mgmt_fee = _find_col_decimal(
                row, 'Mgmt Fee(₹Cr)', 'Mgmt Fee', 'Mgmt Fees',
                'Management Fee(₹Cr)', 'Management Fee', 'Fees',
                'management_fee_payable')
            fund_expenses = _find_col_decimal(
                row, 'Fund Expenses(₹Cr)', 'Fund Expenses', 'Expenses')
            unrealized = _find_col_decimal(
                row, 'Unrealized Gains(₹Cr)', 'Unrealized Gains',
                'Unrealized G/L', 'Unrealised Gains', 'Mark-to-Market Gain',
                'MTM Gain', 'unrealized_gains')
            realized = _find_col_decimal(
                row, 'Realized Gains(₹Cr)', 'Realized Gains',
                'Realized G/L', 'Realised Gains', 'realized_gains')
            total_units = _find_col_decimal(
                row, 'Total Units', 'Units Outstanding', 'Units',
                'total_units_outstanding')
            cash = _find_col_decimal(
                row, 'Cash (Cr)', 'Cash', 'Cash & Equivalents',
                'Cash Balance', 'Bank Balance', 'cash_and_equivalents')
            receivables = _find_col_decimal(
                row, 'Receivables (Cr)', 'Receivables', 'Accounts Receivable')
            other_liab = _find_col_decimal(
                row, 'Other Liabilities (Cr)', 'Other Liabilities',
                'Liabilities', 'Payables', 'other_liabilities')

            units = total_units or Decimal('0')
            if not units and total_nav and nav_per_unit and nav_per_unit > 0:
                units = (total_nav / nav_per_unit).quantize(Decimal('0.000001'))
            if not other_liab and fund_expenses:
                other_liab = fund_expenses

            update_fields = {
                'total_nav': total_nav or Decimal('0'),
                'nav_per_unit': nav_per_unit or Decimal('0'),
                'total_units_outstanding': units,
            }
            if inv_fv:      update_fields['investments_at_fair_value'] = inv_fv
            if mgmt_fee:    update_fields['management_fee_payable'] = mgmt_fee
            if cash:        update_fields['cash_and_equivalents'] = cash
            if receivables: update_fields['receivables'] = receivables
            if other_liab:  update_fields['other_liabilities'] = other_liab
            if unrealized:  update_fields['unrealized_gains'] = unrealized
            if realized:    update_fields['realized_gains'] = realized

            NAVRecord.objects.update_or_create(
                scheme=target_scheme,
                nav_date=nav_date,
                defaults=update_fields,
            )
            count += 1

        return count

    def _enrich_nav_records_post_import(self, wb, scheme, domain_map=None):
        """
        Enrich NAV records after transposed-format import.

        1. NAV/Unit: use Gemini-classified nav_calculation sheets (fallback to
           nav_accounting) to find "Closing NAV/Unit" and apply to the most
           recent NAV record.

        2. Realized Gains: sum ExitEvent.realized_gain_loss for this fund and apply
           the total to the most recent NAV record.
        """
        from django.db.models import Sum

        # ── 1. NAV/Unit from Gemini-classified nav_calculation sheets ────────
        closing_nav_per_unit = None
        opening_nav_per_unit = None
        total_units = None

        # Use Gemini domain_map: nav_calculation first, then nav_accounting
        calc_sheets = _dm_sheets(domain_map, 'nav_calculation') if domain_map else []
        if not calc_sheets:
            calc_sheets = _dm_sheets(domain_map, 'nav_accounting') if domain_map else []
        calc_sheets = [s for s in calc_sheets if s in wb.sheetnames]

        for sn in calc_sheets:
            calc_ws = wb[sn]
            max_r = min(calc_ws.max_row + 1, 80)
            # Collect label-value pairs and classify via Gemini
            nav_kv = {}
            nav_vals = {}
            for rr in range(1, max_r):
                label = calc_ws.cell(rr, 1).value
                if not label:
                    continue
                val = calc_ws.cell(rr, 2).value
                dval = _d(val)
                if dval is None:
                    continue
                label_str = str(label).strip()
                nav_kv[label_str] = label_str
                nav_vals[label_str] = dval
            if nav_kv:
                nav_label_map = self._classify_labels(
                    list(nav_kv.keys()), 'nav_components',
                    context='NAV calculation sheet labels')
                for label_str, nav_type in nav_label_map.items():
                    dval = nav_vals.get(label_str)
                    if dval is None:
                        continue
                    if nav_type == 'closing_nav_per_unit':
                        closing_nav_per_unit = dval
                    elif nav_type == 'opening_nav_per_unit':
                        opening_nav_per_unit = dval
                    elif nav_type == 'total_units':
                        total_units = dval
            if closing_nav_per_unit is not None:
                break  # Found it — no need to scan more sheets

        latest_nav = NAVRecord.objects.filter(scheme=scheme).order_by('-nav_date').first()
        if latest_nav and closing_nav_per_unit and closing_nav_per_unit > 0:
            # nav_per_unit stored in Rs (raw value from Excel)
            units = total_units
            if not units and latest_nav.total_nav and latest_nav.total_nav > 0:
                # Estimate units: total_nav is in Cr → convert to Rs then divide
                total_nav_rs = latest_nav.total_nav * Decimal('10000000')
                try:
                    units = (total_nav_rs / closing_nav_per_unit).quantize(Decimal('0.000001'))
                except Exception:
                    pass
            latest_nav.nav_per_unit = closing_nav_per_unit
            if units:
                latest_nav.total_units_outstanding = units
            latest_nav.save(update_fields=['nav_per_unit', 'total_units_outstanding'])
            logger.info(f'  NAV/Unit set: {closing_nav_per_unit} Rs for {latest_nav.nav_date}')

            # Propagate units to earlier NAV records so nav_per_unit can be derived
            if units and units > 0 and opening_nav_per_unit:
                earlier_navs = NAVRecord.objects.filter(
                    scheme=scheme
                ).exclude(id=latest_nav.id).order_by('nav_date')
                for nav_rec in earlier_navs:
                    if nav_rec.nav_per_unit == 0 and nav_rec.total_nav and nav_rec.total_nav > 0:
                        # Interpolate between opening and closing NAV/Unit linearly
                        # (simple approximation; actual value not in sheet)
                        total_nav_rs = nav_rec.total_nav * Decimal('10000000')
                        try:
                            est_units = (total_nav_rs / closing_nav_per_unit).quantize(Decimal('0.000001'))
                            est_nav_per_unit = (total_nav_rs / units).quantize(Decimal('0.000001'))
                            nav_rec.nav_per_unit = est_nav_per_unit
                            nav_rec.total_units_outstanding = est_units
                            nav_rec.save(update_fields=['nav_per_unit', 'total_units_outstanding'])
                        except Exception:
                            pass

        # ── 2. Realized Gains from ExitEvent records ─────────────────────────
        fund = scheme.fund if scheme else None
        if fund:
            realized_total = ExitEvent.objects.filter(
                investment__scheme__fund=fund,
                realized_gain_loss__isnull=False,
            ).aggregate(total=Sum('realized_gain_loss'))['total']

            if realized_total and latest_nav and latest_nav.realized_gains == 0:
                latest_nav.realized_gains = realized_total
                latest_nav.save(update_fields=['realized_gains'])
                logger.info(f'  Realized Gains set: {realized_total} Cr on {latest_nav.nav_date}')

    # ------------------------------------------------------------------
    # Exits & distributions
    # ------------------------------------------------------------------

    def _import_exits_and_distributions(self, wb, investments, schemes, domain_map):
        """Import exit events and fund-level distributions.

        Uses Gemini-classified exits_distributions sheets from domain_map.
        Validates each sheet by checking for exit-relevant columns
        (Exit Date, Exit Type, Proceeds, MOIC) before processing.
        """
        # Use all Gemini-classified exits sheets
        candidate_sheets = [s for s in _dm_sheets(domain_map, 'exits_distributions')
                            if s in wb.sheetnames]

        if not candidate_sheets:
            return

        for sheet_name in candidate_sheets:
            ws = wb[sheet_name]
            exit_count = self._try_import_exits_from_sheet(
                ws, sheet_name, investments, schemes)
            if exit_count > 0:
                logger.info(f'  Exits imported from sheet "{sheet_name}": {exit_count}')
                return

    def _try_import_exits_from_sheet(self, ws, sheet_name, investments, schemes):
        """Try to import exits from a single sheet. Returns exit count (0 if unsuitable)."""
        sections = self._read_sheet_via_layout(ws, alias_map=self._get_alias(ws))
        exit_rows = None
        dist_rows = None

        for sec_name, (sec_headers, sec_rows) in sections.items():
            subdomain = self._get_section_subdomain(sheet_name, sec_name)
            if subdomain == 'exit_events':
                if exit_rows is None:
                    exit_rows = sec_rows
            elif subdomain == 'distributions':
                dist_rows = sec_rows

        # Structured sections found — validate and process
        if exit_rows is not None:
            if not self._rows_have_exit_columns(exit_rows):
                return 0
            exit_count = self._process_exit_rows(exit_rows, investments)
            if dist_rows:
                default_scheme = list(schemes.values())[0] if schemes else None
                if default_scheme:
                    self._process_distribution_rows(dist_rows, schemes,
                                                     investments)
            return exit_count

        # Flat table fallback
        _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
        if not rows:
            if '__default__' in sections:
                _, rows = sections['__default__']

        if rows and self._rows_have_exit_columns(rows):
            return self._process_exit_rows(rows, investments)
        return 0

    @staticmethod
    def _rows_have_exit_columns(rows):
        """Check if rows contain exit-relevant columns (Exit Date, Exit Type, Proceeds, MOIC).

        Returns True if at least 2 of these column families are present —
        a sheet with only 'Type' or only 'Date' is too ambiguous.
        """
        if not rows:
            return False
        sample = rows[0]
        keys_lower = {k.lower() for k in sample.keys()}

        # Detect exit data using Gemini Pass 2 canonical field names
        # which are added as row keys regardless of language
        exit_canonical = {'exit_date', 'exit_type', 'exit_route',
                          'exit_proceeds', 'realized_value', 'moic'}
        return len(exit_canonical & keys_lower) >= 2

    def _process_exit_rows(self, rows, investments):
        """Process exit event rows from either format.

        Exited companies often do NOT appear in the Portfolio Investments sheet
        (they have already exited). We resolve the Investment via a 3-step lookup:
        1. In-memory investments dict (fast path, current portfolio companies)
        2. DB lookup by company name + org (case-insensitive)
        3. Create a skeleton PortfolioCompany + Investment so the ExitEvent can be saved
        """
        exit_count = 0
        default_scheme = list(investments.values())[0].scheme if investments else None
        for row in rows:
            name = _find_col_str(
                row, 'Company Name', 'Company', 'Name', 'Portfolio Company')
            if _is_junk_row(name):
                continue

            # Pre-extract cost from THIS row — needed for both step 3 and the
            # ExitEvent itself, and must be read before we might skip the row.
            cost = _find_col_decimal(
                row, 'Cost(Cr)', 'Cost(₹Cr)', 'Cost (Cr)',
                'Cost Basis', 'Invested', 'cost_basis')

            # Validate that the row has at least SOME exit-relevant data.
            # Without this check, investor/LP rows or key-value rows from
            # mis-mapped sheets would create phantom PortfolioCompany records.
            exit_date_check = _find_col_date(
                row, 'Exit Date', 'Date', 'Realization Date')
            exit_type_check = _find_col_str(
                row, 'Exit Route', 'Exit Type', 'Route', 'Exit Method',
                'exit_type')
            proceeds_check = _find_col_decimal(
                row, 'Proceeds(Cr)', 'Proceeds (Cr)', 'Realized(₹Cr)',
                'Realized', 'Proceeds', 'Exit Proceeds', 'Realization',
                'Gross Proceeds (Cr)', 'Net Proceeds (Cr)', 'proceeds')
            moic_check = _find_col_decimal(row, 'MOIC', 'Multiple', 'moic')
            if not exit_date_check and not exit_type_check and not proceeds_check and not moic_check:
                continue

            # Step 1: look up in the in-memory investments dict (current portfolio)
            inv = None
            for key, i in investments.items():
                if key.startswith(f'{name}|'):
                    inv = i
                    break

            if not inv:
                # Step 2: look up in DB by company name (case-insensitive)
                co = PortfolioCompany.objects.filter(
                    organization=self.org, name__iexact=name).first()
                if co:
                    inv = Investment.objects.filter(
                        portfolio_company=co).first()

            if not inv and default_scheme:
                # Step 3: create skeleton PortfolioCompany + Investment for this exit.
                # Exited companies are NOT in Portfolio Investments — they need a
                # skeleton record so the ExitEvent FK can be satisfied.
                # total_invested MUST never be None (NOT NULL constraint).
                sector = _find_col_str(row, 'Sector', 'Industry', 'Segment', default='Other')
                co, _ = PortfolioCompany.objects.update_or_create(
                    organization=self.org, name=name,
                    defaults={'sector': sector, 'is_active': False}
                        if sector and sector != 'Other'
                        else {'is_active': False},
                )
                inv, _ = Investment.objects.update_or_create(
                    portfolio_company=co,
                    scheme=default_scheme,
                    defaults={
                        'company_name': name,
                        'investment_date': exit_date_check,
                        'total_invested': abs(cost) if cost else Decimal('0'),
                        'instrument_type': 'equity',
                        'status': 'exited',
                    },
                )

            if not inv:
                logger.warning(f'  Exit row skipped — could not resolve company: {name!r}')
                continue

            # Reuse pre-validated values from row-level check above
            exit_date = exit_date_check
            exit_route = exit_type_check or ''
            realized = proceeds_check
            moic = moic_check
            irr_raw = _find_col_decimal(
                row, 'Gross IRR', 'IRR', 'IRR%', 'Gross IRR%', 'irr_pct')
            # IRR stored as decimal fraction (0.355 = 35.5%) → convert to %
            irr = (irr_raw * 100) if (irr_raw is not None and irr_raw < 2) else irr_raw
            net_irr_raw = _find_col_decimal(
                row, 'Net IRR', 'Net IRR%', 'Net Return', 'net_irr_pct')
            net_irr = (net_irr_raw * 100) if (net_irr_raw is not None and net_irr_raw < 2) else net_irr_raw
            is_actual_raw = _find_col_str(row, 'Is Actual', default='Yes')

            # Classify exit type via Gemini
            exit_type_result = self._classify_enum(
                [exit_route], 'exit_type',
                context='Exit route / exit type for portfolio investment')
            exit_type = exit_type_result.get(exit_route, 'secondary_sale')

            is_actual = is_actual_raw.lower() in ('yes', 'true', '1', 'y')

            gain_loss = None
            if realized and cost:
                gain_loss = realized - cost

            ExitEvent.objects.update_or_create(
                investment=inv,
                exit_type=exit_type,
                defaults={
                    'is_actual': is_actual,
                    'exit_date': exit_date,
                    'proceeds': realized or Decimal('0'),
                    'net_exit_proceeds': realized,
                    'realized_gain_loss': gain_loss,
                    'moic': moic,
                    'irr_pct': irr,
                    'irr_on_exit': net_irr,
                },
            )
            exit_count += 1
        return exit_count

    def _process_distribution_rows(self, rows, schemes, investments):
        """Process fund-level distribution rows from structured format.

        These are distributions to LPs from the Exits & Distributions sheet,
        separate from the investor-level distributions.
        """
        dist_count = 0
        default_scheme = list(schemes.values())[0] if schemes else None
        if not default_scheme:
            return

        for row in rows:
            scheme_name_raw = _find_col_str(
                row, 'Scheme', 'Scheme Name', 'Fund Scheme')
            target_scheme = default_scheme
            if scheme_name_raw:
                target_scheme = schemes.get(scheme_name_raw, default_scheme)

            dist_num = _find_col_decimal(
                row, 'Dist#', 'Dist #', 'Distribution #',
                'Distribution Number', '#', 'distribution_number')
            dist_num = int(dist_num) if dist_num else None

            dist_date = _find_col_date(
                row, 'Distribution Date', 'Date', 'Payment Date')
            # "Quarter" column (e.g. "Q1 FY25") is common in AIF distribution schedules
            if not dist_date:
                quarter_raw = _find_col_str(row, 'Quarter', 'Period')
                if quarter_raw:
                    _, dist_date = self._parse_quarter_period(quarter_raw)
            dist_type_raw = _find_col_str(
                row, 'Type', 'Distribution Type', default='return_of_capital')
            gross_amt = _find_col_decimal(
                row, 'Amount (Cr)', 'Amount(Cr)', 'Gross Amount (Cr)',
                'Gross Amount', 'Total Amount', 'total_gross_amount')
            tds_amt = _find_col_decimal(
                row, 'TDS Amount (Cr)', 'TDS Amount', 'TDS', 'Tax Deducted')
            net_amt = _find_col_decimal(
                row, 'Net Amount (Cr)', 'Net Amount', 'Net Payout')

            if not gross_amt or gross_amt <= 0:
                continue

            if not dist_num:
                existing = Distribution.objects.filter(
                    scheme=target_scheme).count()
                dist_num = existing + 1

            dist_type_result = self._classify_enum(
                [dist_type_raw], 'distribution_type',
                context='Distribution type for LP payout')
            dist_type = dist_type_result.get(dist_type_raw, 'return_of_capital')

            Distribution.objects.get_or_create(
                scheme=target_scheme,
                distribution_number=dist_num,
                defaults={
                    'distribution_date': dist_date or date.today(),
                    'distribution_type': dist_type,
                    'total_gross_amount': gross_amt,
                    'total_tds_amount': tds_amt or Decimal('0'),
                    'total_net_amount': net_amt or gross_amt,
                    'distribution_status': 'distributed',
                    'created_by': self.user,
                },
            )
            dist_count += 1

        if dist_count:
            logger.info(f'  Distributions (structured): {dist_count}')

    # ------------------------------------------------------------------
    # Distributions to LPs
    # ------------------------------------------------------------------

    def _import_distributions(self, wb, schemes, commitments, investments, domain_map):
        """Create Distribution + DistributionLineItem records from investor data.

        Reads distribution amounts from the investors_aml sheet (flat format
        where each LP row has a Distributions column). For structured formats,
        fund-level distributions are handled by _process_distribution_rows.
        """
        if not commitments:
            return

        default_scheme = list(schemes.values())[0] if schemes else None
        if not default_scheme:
            return

        # Try investors_aml sheet for flat-format LP distribution data
        sheet_name = _dm_first(domain_map, 'investors_aml')
        if not sheet_name or sheet_name not in wb.sheetnames:
            return

        ws = wb[sheet_name]
        _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))

        # Check if this sheet actually has distribution columns
        # (Format B investor sheets don't have distribution amounts)
        # Detect distribution columns via Gemini Pass 2 canonical field names
        has_dist_col = False
        if rows:
            sample = rows[0]
            _keys_norm = {k.lower().replace(' ', '_').replace('-', '_')
                          for k in sample.keys()}
            dist_fields = {'gross_amount', 'net_amount', 'tds_amount',
                           'distribution_amount', 'distribution_type'}
            has_dist_col = bool(dist_fields & _keys_norm)
        if not has_dist_col:
            return

        # Collect LP distribution data
        lp_distributions = []
        total_gross = Decimal('0')
        for row in rows:
            inv_name = _find_col_str(
                row, 'Investor Name', 'LP Name', 'Name', 'Investor')
            if not inv_name or inv_name not in commitments:
                continue

            dist_amt = _find_col_decimal(
                row, 'Distributions', 'Distribution', 'Returned',
                'Amount Returned', 'Total Distribution')
            if not dist_amt or dist_amt <= 0:
                continue

            total_gross += dist_amt
            lp_distributions.append((inv_name, commitments[inv_name], dist_amt))

        if not lp_distributions:
            return

        # Create a consolidated distribution record
        dist, created = Distribution.objects.get_or_create(
            scheme=default_scheme,
            distribution_number=1,
            defaults={
                'distribution_date': date.today(),
                'distribution_type': 'return_of_capital',
                'total_gross_amount': total_gross,
                'total_tds_amount': Decimal('0'),
                'total_net_amount': total_gross,
                'distribution_status': 'distributed',
                'created_by': self.user,
            },
        )

        if not created:
            return  # Already exists

        # Create line items per LP
        line_count = 0
        for inv_name, commitment, dist_amt in lp_distributions:
            DistributionLineItem.objects.get_or_create(
                distribution=dist,
                commitment=commitment,
                defaults={
                    'gross_amount': dist_amt,
                    'tds_rate': Decimal('0'),
                    'tds_amount': Decimal('0'),
                    'net_amount': dist_amt,
                },
            )
            line_count += 1

        logger.info(f'  Distributions: 1 distribution, {line_count} line items')

    # ------------------------------------------------------------------
    # Key Entities — Import & Link to Fund
    # ------------------------------------------------------------------

    # Map entity_type values from Excel to the Fund model FK field names
    _ENTITY_FK_MAP = {
        'manager': 'manager_entity',
        'trustee': 'trustee_entity',
        'sponsor': 'sponsor_entity',
        'custodian': 'custodian_entity',
        'statutory_auditor': 'auditor_entity',
    }

    def _normalize_entity_type(self, raw_type):
        """Normalize entity type string to a valid model choice via Gemini classification."""
        if not raw_type:
            return None
        raw = raw_type.strip()
        if not raw:
            return None
        # Direct match against Django model choices
        raw_lower = raw.lower().replace('-', '_')
        valid_types = [c[0] for c in Entity.ENTITY_TYPE_CHOICES]
        if raw_lower in valid_types:
            return raw_lower
        # Gemini classification
        result = self._classify_enum([raw], 'entity_type',
                                      context='Entity/service provider type for AIF fund')
        return result.get(raw)

    def _import_entities(self, wb, org, fund, domain_map):
        """Import key entities from Excel and link them to the fund.

        Dynamically finds entity data in:
        - Dedicated 'organization_users' domain sheet with KEY ENTITIES section
        - Fund & Scheme Master sheet with entity references
        - Any sheet containing entity-related sections

        Uses header-based reading with _find_col for format-agnostic extraction.
        """
        count = 0
        entity_map = {}  # entity_type -> Entity object

        # Strategy 1: Read from Gemini-classified organization_users domain
        sheet_name = _dm_first(domain_map, 'organization_users')

        if sheet_name and sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sections = self._read_sheet_via_layout(ws, alias_map=self._get_alias(ws))

            # Find entity section via Gemini sub-domain
            entity_rows = None
            for sec_name, (sec_headers, sec_rows) in sections.items():
                subdomain = self._get_section_subdomain(sheet_name, sec_name)
                if subdomain == 'entities':
                    entity_rows = sec_rows
                    break

            if entity_rows:
                for row in entity_rows:
                    raw_type = _find_col(
                        row, 'Entity Type', 'Type', 'Role', 'Entity Role',
                        'Category', 'Entity Category')
                    entity_name = _find_col(
                        row, 'Entity Name', 'Name', 'Legal Name',
                        'Organization Name', 'Firm Name', 'Company Name')
                    if not raw_type or not entity_name:
                        continue

                    etype = self._normalize_entity_type(str(raw_type))
                    if not etype:
                        logger.warning(f'  Entity: unknown type "{raw_type}" for "{entity_name}"')
                        continue

                    pan = _find_col(row, 'PAN', 'PAN Number', 'Tax ID') or ''
                    gstin = _find_col(row, 'GSTIN', 'GST', 'GST Number', 'GST ID') or ''
                    sebi_reg = _find_col(
                        row, 'SEBI Registration', 'SEBI Reg', 'SEBI No',
                        'Registration Number', 'Reg No', 'License') or ''
                    contact = _find_col(
                        row, 'Contact Person', 'Contact', 'Contact Name',
                        'Representative', 'Person') or ''
                    email = _find_col(
                        row, 'Email', 'Email Address', 'Email ID',
                        'Contact Email', 'E-mail') or ''
                    phone = _find_col(
                        row, 'Phone', 'Phone Number', 'Mobile',
                        'Contact Phone', 'Tel') or ''
                    address = _find_col(
                        row, 'Address', 'Office Address', 'Location',
                        'Registered Address') or ''
                    city = _find_col(row, 'City', 'Location') or ''
                    state = _find_col(row, 'State', 'Province') or ''
                    country = _find_col(row, 'Country', 'Nation') or 'India'

                    entity, _ = Entity.objects.get_or_create(
                        organization=org,
                        entity_type=etype,
                        entity_name=_str(entity_name),
                        defaults={
                            'pan': _str(pan),
                            'gstin': _str(gstin),
                            'sebi_registration': _str(sebi_reg),
                            'contact_person': _str(contact),
                            'email': _str(email),
                            'phone': _str(phone),
                            'address': _str(address),
                            'city': _str(city),
                            'state': _str(state),
                            'country': _str(country),
                        },
                    )
                    entity_map[etype] = entity
                    count += 1

        # Strategy 2: Read entity references from Fund & Scheme Master sheet
        if not entity_map:
            fsm_sheet = _dm_first(domain_map, 'fund_scheme_master')
            if fsm_sheet and fsm_sheet in wb.sheetnames:
                ws = wb[fsm_sheet]
                # Collect all label-value pairs first
                label_value_pairs = {}
                for r in range(1, ws.max_row + 1):
                    label = _str(ws.cell(r, 1).value).strip()
                    value = _str(ws.cell(r, 2).value).strip()
                    if label and value and len(value) > 2:
                        label_value_pairs[label] = value
                # Classify labels as entity types via Gemini (returns None for non-entity labels)
                if label_value_pairs:
                    from .canonical_schema import CANONICAL_ENUM_TYPES
                    from .gemini_column_mapper import classify_labels
                    entity_opts = CANONICAL_ENUM_TYPES.get('entity_type', {})
                    label_types = classify_labels(
                        list(label_value_pairs.keys()), 'entity_type_labels',
                        entity_opts,
                        context='Labels from fund master sheet — classify ONLY if label refers to an entity/service provider role, otherwise null')
                    for label, etype in label_types.items():
                        if etype and etype not in entity_map:
                            entity, _ = Entity.objects.get_or_create(
                                organization=org,
                                entity_type=etype,
                                entity_name=label_value_pairs[label],
                            )
                            entity_map[etype] = entity
                            count += 1

        # Link entities to fund
        if entity_map:
            update_fields = []
            for etype, fk_field in self._ENTITY_FK_MAP.items():
                if etype in entity_map:
                    setattr(fund, fk_field, entity_map[etype])
                    update_fields.append(fk_field)
            if update_fields:
                fund.save(update_fields=update_fields)

        logger.info(f'  Entities: {count} created/found, '
                     f'{len([k for k in entity_map if k in self._ENTITY_FK_MAP])} linked to fund')

    # ------------------------------------------------------------------
    # LP Capital Accounts — Auto-generate from imported data
    # ------------------------------------------------------------------

    def _generate_lp_capital_accounts(self, fund, schemes, commitments):
        """Auto-generate LP Capital Account snapshots from imported data.

        For each commitment, computes:
        - committed_capital from the Commitment record
        - called_capital from CapitalCallLineItems
        - distributed_capital from DistributionLineItems
        - unrealized_value pro-rata from NAV data
        - Performance metrics: TVPI, DPI, RVPI, MOIC

        Creates one snapshot per commitment as of the latest NAV date or today.
        """
        from django.db.models import Sum

        if not commitments:
            return

        count = 0
        for commit_key, commitment in commitments.items():
            scheme = commitment.scheme

            # Called capital: sum of all capital call line items for this commitment
            called = CapitalCallLineItem.objects.filter(
                commitment=commitment
            ).aggregate(total=Sum('called_amount'))['total'] or Decimal('0')

            # If no line items, try summing from capital calls on the scheme
            # pro-rata by commitment amount
            if called == 0:
                total_scheme_called = CapitalCall.objects.filter(
                    scheme=scheme
                ).aggregate(total=Sum('total_call_amount'))['total'] or Decimal('0')
                total_scheme_committed = Commitment.objects.filter(
                    scheme=scheme
                ).aggregate(total=Sum('commitment_amount'))['total'] or Decimal('0')
                if total_scheme_committed > 0 and total_scheme_called > 0:
                    ratio = commitment.commitment_amount / total_scheme_committed
                    called = (total_scheme_called * ratio).quantize(Decimal('0.01'))

            committed = commitment.commitment_amount or Decimal('0')
            uncalled = max(committed - called, Decimal('0'))

            # Distributed capital: sum of all distribution line items for this commitment
            distributed = DistributionLineItem.objects.filter(
                commitment=commitment
            ).aggregate(total=Sum('gross_amount'))['total'] or Decimal('0')

            # Unrealized value: LP's pro-rata share of latest NAV
            latest_nav = NAVRecord.objects.filter(
                scheme=scheme
            ).order_by('-nav_date').first()

            unrealized = Decimal('0')
            as_of = date.today()
            if latest_nav:
                as_of = latest_nav.nav_date
                total_nav = latest_nav.total_nav or Decimal('0')
                total_scheme_committed = Commitment.objects.filter(
                    scheme=scheme
                ).aggregate(total=Sum('commitment_amount'))['total'] or Decimal('0')
                if total_scheme_committed > 0:
                    lp_share = committed / total_scheme_committed
                    unrealized = (total_nav * lp_share).quantize(Decimal('0.01'))

            total_value = distributed + unrealized

            # Performance metrics
            tvpi = dpi = rvpi = moic = None
            if called > 0:
                tvpi = (total_value / called).quantize(Decimal('0.0001'))
                dpi = (distributed / called).quantize(Decimal('0.0001'))
                rvpi = (unrealized / called).quantize(Decimal('0.0001'))
                moic = tvpi  # Same as TVPI at aggregate level

            # Units held: from commitment if available, or pro-rata from NAV
            units_held = commitment.units_allocated
            if not units_held and latest_nav and latest_nav.total_units_outstanding:
                total_scheme_committed = Commitment.objects.filter(
                    scheme=scheme
                ).aggregate(total=Sum('commitment_amount'))['total'] or Decimal('0')
                if total_scheme_committed > 0:
                    lp_share = committed / total_scheme_committed
                    units_held = (latest_nav.total_units_outstanding * lp_share).quantize(
                        Decimal('0.000001'))

            # Management fee charged: LP's pro-rata share
            total_fees = ManagementFeeSchedule.objects.filter(
                scheme=scheme
            ).aggregate(total=Sum('fee_amount'))['total'] or Decimal('0')
            mgmt_fee_charged = Decimal('0')
            if total_fees > 0:
                total_scheme_committed = Commitment.objects.filter(
                    scheme=scheme
                ).aggregate(total=Sum('commitment_amount'))['total'] or Decimal('0')
                if total_scheme_committed > 0:
                    lp_share = committed / total_scheme_committed
                    mgmt_fee_charged = (total_fees * lp_share).quantize(Decimal('0.01'))

            LPCapitalAccount.objects.update_or_create(
                commitment=commitment,
                as_of_date=as_of,
                defaults={
                    'committed_capital': committed,
                    'called_capital': called,
                    'uncalled_capital': uncalled,
                    'distributed_capital': distributed,
                    'unrealized_value': unrealized,
                    'total_value': total_value,
                    'tvpi': tvpi,
                    'dpi': dpi,
                    'rvpi': rvpi,
                    'moic': moic,
                    'units_held': units_held,
                    'management_fee_charged': mgmt_fee_charged,
                    'carried_interest_charged': Decimal('0'),
                },
            )
            count += 1

        logger.info(f'  LP Capital Accounts: {count} snapshots generated')

    # ------------------------------------------------------------------
    # Income/Expense Ledger — Generate from NAV + Fee data
    # ------------------------------------------------------------------

    def _generate_income_expense_ledger(self, org, fund, schemes):
        """Generate income & expense ledger entries from NAV and fee data.

        This creates the journal entries that power the Income Statement
        and Cash Flow Statement in Fund Accounting.

        For each NAV period, creates entries for:
        - Unrealized gains/losses (from NAV movements)
        - Realized gains (from exits)
        - Management fees (from fee schedule)
        - Fund expenses (from NAV data)

        Uses existing COA accounts seeded by _setup_fund_accounting.
        """
        from django.db.models import Sum

        # Get COA map for this organization
        coa_map = {}
        for acct in ChartOfAccounts.objects.filter(organization=org):
            coa_map[acct.account_code] = acct

        if not coa_map:
            logger.info('  Income/expense ledger: no COA accounts found, skipping')
            return

        total_entries = 0

        for scheme_key, scheme in schemes.items():
            # Find the highest existing JE number for this scheme
            last_je = FundLedger.objects.filter(
                scheme=scheme
            ).order_by('-journal_entry_number').first()
            if last_je:
                # Extract number from JE-XXXX format
                try:
                    je_num = int(last_je.journal_entry_number.replace('JE-', '')) + 1
                except (ValueError, AttributeError):
                    je_num = 1000
            else:
                je_num = 1

            # --- NAV-based income entries (unrealized gains, realized gains) ---
            nav_records = NAVRecord.objects.filter(
                scheme=scheme
            ).order_by('nav_date')

            prev_investments_fv = None
            for nav in nav_records:
                nav_date = nav.nav_date

                # Unrealized gains: change in investment FV between periods
                current_fv = nav.investments_at_fair_value or Decimal('0')
                if prev_investments_fv is not None and current_fv > 0:
                    unrealized_change = current_fv - prev_investments_fv
                    if unrealized_change != 0 and '4100' in coa_map and '1200' in coa_map:
                        if unrealized_change > 0:
                            # Gain: debit Investments at FV, credit Unrealized Gains
                            FundLedger.objects.get_or_create(
                                scheme=scheme,
                                journal_entry_number=f'JE-{je_num:04d}',
                                defaults={
                                    'entry_date': nav_date,
                                    'description': f'Unrealized gain on investments — {nav_date.strftime("%b %Y")}',
                                    'debit_account': coa_map['1200'],
                                    'credit_account': coa_map['4100'],
                                    'amount': abs(unrealized_change),
                                    'reference_type': 'valuation_adjustment',
                                    'posted_by': self.user,
                                },
                            )
                        else:
                            # Loss: debit Unrealized Gains (reverse), credit Investments at FV
                            FundLedger.objects.get_or_create(
                                scheme=scheme,
                                journal_entry_number=f'JE-{je_num:04d}',
                                defaults={
                                    'entry_date': nav_date,
                                    'description': f'Unrealized loss on investments — {nav_date.strftime("%b %Y")}',
                                    'debit_account': coa_map['4100'],
                                    'credit_account': coa_map['1200'],
                                    'amount': abs(unrealized_change),
                                    'reference_type': 'valuation_adjustment',
                                    'posted_by': self.user,
                                },
                            )
                        je_num += 1
                        total_entries += 1

                prev_investments_fv = current_fv

                # Management fee expense entry from NAV data
                mgmt_fee = nav.management_fee_payable or Decimal('0')
                if mgmt_fee > 0 and '5000' in coa_map and '2000' in coa_map:
                    FundLedger.objects.get_or_create(
                        scheme=scheme,
                        journal_entry_number=f'JE-{je_num:04d}',
                        defaults={
                            'entry_date': nav_date,
                            'description': f'Management fee — {nav_date.strftime("%b %Y")}',
                            'debit_account': coa_map['5000'],
                            'credit_account': coa_map['2000'],
                            'amount': mgmt_fee,
                            'reference_type': 'management_fee',
                            'posted_by': self.user,
                        },
                    )
                    je_num += 1
                    total_entries += 1

                # Other liabilities / fund expenses
                other_liab = nav.other_liabilities or Decimal('0')
                if other_liab > 0 and '5100' in coa_map and '2200' in coa_map:
                    FundLedger.objects.get_or_create(
                        scheme=scheme,
                        journal_entry_number=f'JE-{je_num:04d}',
                        defaults={
                            'entry_date': nav_date,
                            'description': f'Fund expenses — {nav_date.strftime("%b %Y")}',
                            'debit_account': coa_map['5100'],
                            'credit_account': coa_map['2200'],
                            'amount': other_liab,
                            'reference_type': 'expense',
                            'posted_by': self.user,
                        },
                    )
                    je_num += 1
                    total_entries += 1

                # Cash & bank balance tracking
                cash = nav.cash_and_equivalents or Decimal('0')
                if cash > 0 and '1000' in coa_map and '1300' in coa_map:
                    receivables = nav.receivables or Decimal('0')
                    if receivables > 0:
                        FundLedger.objects.get_or_create(
                            scheme=scheme,
                            journal_entry_number=f'JE-{je_num:04d}',
                            defaults={
                                'entry_date': nav_date,
                                'description': f'Receivables — {nav_date.strftime("%b %Y")}',
                                'debit_account': coa_map['1300'],
                                'credit_account': coa_map['4200'],
                                'amount': receivables,
                                'reference_type': 'other',
                                'posted_by': self.user,
                            },
                        )
                        je_num += 1
                        total_entries += 1

            # --- Exit-based realized gains ---
            exits = ExitEvent.objects.filter(
                investment__scheme=scheme,
                is_actual=True,
            )
            for exit_event in exits:
                proceeds = exit_event.proceeds or Decimal('0')
                # Use realized_gain_loss if available, else compute from investment cost
                gain = exit_event.realized_gain_loss or Decimal('0')
                if gain == 0 and proceeds > 0 and exit_event.investment:
                    cost = exit_event.investment.total_invested or Decimal('0')
                    gain = proceeds - cost
                if gain != 0 and '4000' in coa_map and '1000' in coa_map:
                    exit_date = exit_event.exit_date or date.today()
                    company = exit_event.investment.company_name if exit_event.investment else 'Unknown'
                    if gain > 0:
                        FundLedger.objects.get_or_create(
                            scheme=scheme,
                            journal_entry_number=f'JE-{je_num:04d}',
                            defaults={
                                'entry_date': exit_date,
                                'description': f'Realized gain — exit from {company}',
                                'debit_account': coa_map['1000'],
                                'credit_account': coa_map['4000'],
                                'amount': abs(gain),
                                'reference_type': 'other',
                                'posted_by': self.user,
                            },
                        )
                    else:
                        FundLedger.objects.get_or_create(
                            scheme=scheme,
                            journal_entry_number=f'JE-{je_num:04d}',
                            defaults={
                                'entry_date': exit_date,
                                'description': f'Realized loss — exit from {company}',
                                'debit_account': coa_map['4000'],
                                'credit_account': coa_map['1000'],
                                'amount': abs(gain),
                                'reference_type': 'other',
                                'posted_by': self.user,
                            },
                        )
                    je_num += 1
                    total_entries += 1

            # --- Management fee entries from fee schedule ---
            fees = ManagementFeeSchedule.objects.filter(scheme=scheme)
            for fee in fees:
                if fee.fee_amount and fee.fee_amount > 0 and '5000' in coa_map and '1000' in coa_map:
                    FundLedger.objects.get_or_create(
                        scheme=scheme,
                        journal_entry_number=f'JE-{je_num:04d}',
                        defaults={
                            'entry_date': fee.period_end,
                            'description': f'Management fee payment — {fee.period_start.strftime("%b %Y")}',
                            'debit_account': coa_map['2000'],
                            'credit_account': coa_map['1000'],
                            'amount': fee.fee_amount,
                            'reference_type': 'management_fee',
                            'posted_by': self.user,
                        },
                    )
                    je_num += 1
                    total_entries += 1

                # GST on management fee
                if fee.gst_amount and fee.gst_amount > 0 and '5100' in coa_map and '1000' in coa_map:
                    FundLedger.objects.get_or_create(
                        scheme=scheme,
                        journal_entry_number=f'JE-{je_num:04d}',
                        defaults={
                            'entry_date': fee.period_end,
                            'description': f'GST on management fee — {fee.period_start.strftime("%b %Y")}',
                            'debit_account': coa_map['5100'],
                            'credit_account': coa_map['1000'],
                            'amount': fee.gst_amount,
                            'reference_type': 'management_fee',
                            'posted_by': self.user,
                        },
                    )
                    je_num += 1
                    total_entries += 1

            # --- Carried interest entry ---
            carry_records = CarriedInterest.objects.filter(scheme=scheme)
            for carry in carry_records:
                if carry.carry_amount_gross and carry.carry_amount_gross > 0:
                    if '5200' in coa_map and '2100' in coa_map:
                        FundLedger.objects.get_or_create(
                            scheme=scheme,
                            journal_entry_number=f'JE-{je_num:04d}',
                            defaults={
                                'entry_date': carry.calculation_date,
                                'description': f'Carried interest accrual — {carry.calculation_date}',
                                'debit_account': coa_map['5200'],
                                'credit_account': coa_map['2100'],
                                'amount': carry.carry_amount_gross,
                                'reference_type': 'carried_interest',
                                'posted_by': self.user,
                            },
                        )
                        je_num += 1
                        total_entries += 1

        logger.info(f'  Income/expense ledger: {total_entries} additional entries created')

    # ------------------------------------------------------------------
    # Chart of Accounts & Fund Ledger
    # ------------------------------------------------------------------

    # Standard fund accounting chart of accounts
    _COA_SEED = [
        ('1000', 'Cash & Bank', 'asset'),
        ('1100', 'Investments at Cost', 'asset'),
        ('1200', 'Investments at Fair Value', 'asset'),
        ('1300', 'Receivables', 'asset'),
        ('2000', 'Management Fee Payable', 'liability'),
        ('2100', 'Carried Interest Payable', 'liability'),
        ('2200', 'Other Liabilities', 'liability'),
        ('2300', 'Distributions Payable', 'liability'),
        ('3000', 'LP Capital', 'equity'),
        ('3100', 'Retained Earnings', 'equity'),
        ('4000', 'Realized Gains', 'income'),
        ('4100', 'Unrealized Gains', 'income'),
        ('4200', 'Interest Income', 'income'),
        ('4300', 'Dividend Income', 'income'),
        ('5000', 'Management Fees', 'expense'),
        ('5100', 'Fund Expenses', 'expense'),
        ('5200', 'Carried Interest', 'expense'),
    ]

    def _setup_fund_accounting(self, org, fund, schemes, investments):
        """Seed Chart of Accounts and create initial ledger entries."""
        # Seed COA
        coa_map = {}
        for code, name, acct_type in self._COA_SEED:
            acct, _ = ChartOfAccounts.objects.get_or_create(
                organization=org,
                account_code=code,
                defaults={
                    'account_name': name,
                    'account_type': acct_type,
                    'is_active': True,
                },
            )
            coa_map[code] = acct

        if not schemes:
            logger.info(f'  COA: {len(coa_map)} accounts seeded')
            return

        total_je = 0
        # Create ledger entries for ALL schemes (not just the first one)
        for scheme_key, scheme in schemes.items():
            je_num = FundLedger.objects.filter(scheme=scheme).count() + 1

            # Create ledger entries for capital calls (cash in → LP capital)
            capital_calls = CapitalCall.objects.filter(scheme=scheme)
            for call in capital_calls:
                FundLedger.objects.get_or_create(
                    scheme=scheme,
                    journal_entry_number=f'JE-{je_num:04d}',
                    defaults={
                        'entry_date': call.call_date,
                        'description': f'Capital Call #{call.call_number}',
                        'debit_account': coa_map['1000'],   # Cash
                        'credit_account': coa_map['3000'],   # LP Capital
                        'amount': call.total_call_amount,
                        'reference_type': 'capital_call',
                        'reference_id': call.id,
                        'posted_by': self.user,
                    },
                )
                je_num += 1

            # Create ledger entries for investments (cash out → investments at cost)
            for key, inv in investments.items():
                if inv.scheme_id == scheme.id and inv.total_invested and inv.total_invested > 0:
                    FundLedger.objects.get_or_create(
                        scheme=scheme,
                        journal_entry_number=f'JE-{je_num:04d}',
                        defaults={
                            'entry_date': inv.investment_date or date.today(),
                            'description': f'Investment in {inv.company_name}',
                            'debit_account': coa_map['1100'],   # Investments at Cost
                            'credit_account': coa_map['1000'],   # Cash
                            'amount': inv.total_invested,
                            'reference_type': 'investment',
                            'reference_id': inv.id,
                            'posted_by': self.user,
                        },
                    )
                    je_num += 1

            # Create ledger entries for distributions (distributions payable → cash out)
            distributions = Distribution.objects.filter(scheme=scheme)
            for dist in distributions:
                FundLedger.objects.get_or_create(
                    scheme=scheme,
                    journal_entry_number=f'JE-{je_num:04d}',
                    defaults={
                        'entry_date': dist.distribution_date,
                        'description': f'Distribution #{dist.distribution_number}',
                        'debit_account': coa_map['3000'],   # LP Capital (return)
                        'credit_account': coa_map['1000'],   # Cash
                        'amount': dist.total_gross_amount,
                        'reference_type': 'distribution',
                        'reference_id': dist.id,
                        'posted_by': self.user,
                    },
                )
                je_num += 1

            total_je += je_num - 1

        logger.info(f'  Fund accounting: {len(coa_map)} COA accounts, '
                     f'{total_je} journal entries across {len(schemes)} schemes')

    # ------------------------------------------------------------------
    # Management Fee Schedule
    # ------------------------------------------------------------------

    def _import_management_fees(self, wb, schemes, domain_map):
        """Create ManagementFeeSchedule records.

        Handles two formats:
        - Format A: Flat table (Period | Mgmt Fee | ...) from nav_accounting sheet
        - Format B: Quarterly transposed table (FEES_REGISTER / FEES sheet) with
          columns like "Q1 FY25 | Q2 FY25 | Q3 FY25 | Q4 FY25".
          This is the dominant format in fund management Excel files.
        """
        default_scheme = list(schemes.values())[0] if schemes else None
        if not default_scheme:
            return

        fee_rate = default_scheme.management_fee_pct or Decimal('2.00')

        # ── Format B: FEES_REGISTER / dedicated fees sheet ────────────────────
        # Use Gemini-classified 'fees_register' domain, fallback to 'nav_accounting'
        fees_sheets = _dm_sheets(domain_map, 'fees_register')
        if not fees_sheets:
            fees_sheets = _dm_sheets(domain_map, 'nav_accounting')
        fees_sheet = fees_sheets[0] if fees_sheets else None

        if fees_sheet and fees_sheet in wb.sheetnames:
            ws_f = wb[fees_sheet]
            _quarter_re = re.compile(r'^Q[1-4]\s*FY\s*\d{2,4}$', re.IGNORECASE)
            headers_dict, fee_rows = read_table_from_sheet(
                ws_f, alias_map=self._get_alias(ws_f))
            quarter_cols = [h for h in headers_dict.keys()
                            if _quarter_re.match(h.strip())]

            if quarter_cols and fee_rows:
                # Classify fee component labels via Gemini
                fee_comp_labels = set()
                for row in fee_rows:
                    c = _find_col_str(row, 'Component', 'Expense Category', 'Item')
                    if c:
                        fee_comp_labels.add(c)
                fee_comp_map = self._classify_labels(
                    fee_comp_labels, 'fee_components',
                    context='Fee schedule components: management fee vs GST')

                mgmt_fee_vals = {}
                gst_vals = {}
                for row in fee_rows:
                    comp = _find_col_str(row, 'Component', 'Expense Category', 'Item')
                    fee_type = fee_comp_map.get(comp) if comp else None
                    if not fee_type:
                        continue
                    for qcol in quarter_cols:
                        val = _d(row.get(qcol))
                        if val is None:
                            continue
                        if fee_type == 'management_fee' and val > 0:
                            mgmt_fee_vals[qcol] = val
                        elif fee_type == 'gst_on_management_fee':
                            gst_vals[qcol] = abs(val)

                count = 0
                for qcol in quarter_cols:
                    fee_amt = mgmt_fee_vals.get(qcol)
                    if not fee_amt or fee_amt <= 0:
                        continue
                    gst = gst_vals.get(qcol,
                                       (fee_amt * Decimal('0.18')).quantize(Decimal('0.01')))
                    period_start, period_end = self._parse_quarter_period(qcol)
                    if not period_start:
                        continue
                    ManagementFeeSchedule.objects.get_or_create(
                        scheme=default_scheme,
                        period_start=period_start,
                        period_end=period_end,
                        defaults={
                            'fee_basis_amount': Decimal('0'),
                            'fee_rate': fee_rate,
                            'fee_amount': fee_amt,
                            'gst_amount': gst,
                            'total_fee_with_gst': fee_amt + gst,
                            'fee_status': 'paid',
                        },
                    )
                    count += 1
                logger.info(f'  Management fees (fees register): {count} quarters')
                return  # Done — skip the NAV-sheet path below

        # ── Format A: Flat table from nav_accounting sheet ────────────────────
        sheet_name = _dm_first(domain_map, 'nav_accounting')
        if not sheet_name or sheet_name not in wb.sheetnames:
            return

        ws = wb[sheet_name]
        sections = self._read_sheet_via_layout(ws, alias_map=self._get_alias(ws))
        rows = None
        for sec_name, (sec_headers, sec_rows) in sections.items():
            subdomain = self._get_section_subdomain(sheet_name, sec_name)
            if subdomain == 'nav_records' or sec_name == '__default__':
                rows = sec_rows
                break
        if not rows:
            _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))

        count = 0
        for row in rows:
            period_raw = _find_col(
                row, 'Period', 'NAV Date', 'Date', 'Month')
            if not period_raw:
                continue
            period_date = _date(period_raw)
            if not period_date and isinstance(period_raw, str):
                period_date = self._parse_period(period_raw)
            if not period_date:
                continue

            fee_amount = _find_col_decimal(
                row, 'Mgmt Fees', 'Management Fee', 'Fees',
                'Management Fee Amount')
            if not fee_amount or fee_amount <= 0:
                continue

            total_inv = _find_col_decimal(
                row, 'Total Investments', 'Investments at FV',
                'Total NAV', 'Fee Basis')
            fee_basis = total_inv or Decimal('0')

            # Period is monthly: period_start = 1st of month, period_end = last of month
            period_start = period_date.replace(day=1)
            last_day = calendar.monthrange(period_date.year, period_date.month)[1]
            period_end = period_date.replace(day=last_day)

            # Compute GST (18% in India)
            gst = (fee_amount * Decimal('0.18')).quantize(Decimal('0.01'))

            ManagementFeeSchedule.objects.get_or_create(
                scheme=default_scheme,
                period_start=period_start,
                period_end=period_end,
                defaults={
                    'fee_basis_amount': fee_basis,
                    'fee_rate': fee_rate,
                    'fee_amount': fee_amount,
                    'gst_amount': gst,
                    'total_fee_with_gst': fee_amount + gst,
                    'fee_status': 'paid',
                },
            )
            count += 1

        logger.info(f'  Management fees: {count} periods')

    # ------------------------------------------------------------------
    # Carried Interest Computation
    # ------------------------------------------------------------------

    def _compute_carried_interest(self, schemes, wb=None, domain_map=None):
        """Compute carried interest from exits and scheme waterfall config.

        Two-pass approach:
        1. Compute from DB records (exits, distributions, capital calls)
        2. If the computed carry is 0 but the Excel has explicit carry values
           in a waterfall/carry sheet, use the Excel values as authoritative
        """
        if not schemes:
            return

        from django.db.models import Sum

        for scheme_key, scheme in schemes.items():
            carry_pct = scheme.carry_pct or Decimal('20')
            hurdle_rate = scheme.hurdle_rate_pct or Decimal('8')

            total_called = CapitalCall.objects.filter(
                scheme=scheme
            ).aggregate(total=Sum('total_call_amount'))['total'] or Decimal('0')

            total_distributions = Distribution.objects.filter(
                scheme=scheme
            ).aggregate(total=Sum('total_gross_amount'))['total'] or Decimal('0')

            total_exit_proceeds = ExitEvent.objects.filter(
                investment__scheme=scheme, is_actual=True
            ).aggregate(total=Sum('proceeds'))['total'] or Decimal('0')

            if total_called <= 0:
                continue

            preferred_return = (total_called * hurdle_rate / 100).quantize(Decimal('0.01'))
            total_value = total_distributions + total_exit_proceeds
            carry_base = max(total_value - total_called - preferred_return, Decimal('0'))
            carry_gross = (carry_base * carry_pct / 100).quantize(Decimal('0.01'))

            # If DB-computed carry is 0, try to extract explicit carry values
            # from the workbook (WATERFALL / carry / NAV sheets).
            # These sheets often have key-value rows like:
            #   "Carried Interest Provision" → 104.2 (Cr)
            #   "Preferred Return" → 273.6 (Cr)
            excel_carry_gross = None
            excel_preferred_return = None
            if wb and carry_gross == 0:
                excel_carry_gross, excel_preferred_return = \
                    self._extract_carry_from_workbook(wb, domain_map)
                if excel_carry_gross and excel_carry_gross > 0:
                    carry_gross = excel_carry_gross
                    carry_base = carry_gross  # approximate
                if excel_preferred_return and excel_preferred_return > 0:
                    preferred_return = excel_preferred_return

            CarriedInterest.objects.update_or_create(
                scheme=scheme,
                calculation_date=date.today(),
                defaults={
                    'total_distributions': total_distributions,
                    'total_called_capital': total_called,
                    'preferred_return_amount': preferred_return,
                    'carry_base': carry_base,
                    'carry_amount_gross': carry_gross,
                    'carry_amount_net': carry_gross,
                    'gp_clawback_provision': Decimal('0'),
                    'calculation_status': 'indicative',
                },
            )

            logger.info(f'  Carried interest ({scheme.name}): called={total_called}, '
                         f'pref_return={preferred_return}, carry={carry_gross}')

    def _extract_carry_from_workbook(self, wb, domain_map=None):
        """Extract carry/preferred-return amounts from Gemini-classified sheets.

        Uses waterfall_carry domain first, then nav_calculation, then nav_accounting.
        Searches key-value formatted sheets for rows that semantically match
        carry-related labels.

        Returns (carry_gross, preferred_return) as Decimals or (None, None).
        """
        carry_gross = None
        preferred_return = None

        # Use Gemini domain_map: waterfall_carry → nav_calculation → nav_accounting
        candidate_sheets = []
        if domain_map:
            for domain in ('waterfall_carry', 'nav_calculation', 'nav_accounting'):
                for s in _dm_sheets(domain_map, domain):
                    if s in wb.sheetnames and s not in candidate_sheets:
                        candidate_sheets.append(s)

        if not candidate_sheets:
            return None, None

        for sn in candidate_sheets:
            ws = wb[sn]
            max_r = min(ws.max_row + 1, 80)
            # Collect all label-value pairs
            carry_labels = {}
            carry_vals = {}
            for rr in range(1, max_r):
                for label_col, val_col in [(1, 2), (2, 3)]:
                    label = ws.cell(rr, label_col).value
                    if not label:
                        continue
                    raw_val = ws.cell(rr, val_col).value
                    val = _d(raw_val)
                    if val is None or val <= 0:
                        continue
                    label_str = str(label).strip()
                    carry_labels[label_str] = label_str
                    carry_vals[label_str] = val

            if carry_labels:
                wf_map = self._classify_labels(
                    list(carry_labels.keys()), 'waterfall_components',
                    context='Waterfall/carry components: carried interest vs preferred return')
                for label_str, wf_type in wf_map.items():
                    val = carry_vals.get(label_str)
                    if val is None:
                        continue
                    if wf_type == 'carry_gross' and carry_gross is None:
                        carry_gross = val
                    elif wf_type == 'preferred_return' and preferred_return is None:
                        preferred_return = val

            if carry_gross is not None and preferred_return is not None:
                break

        return carry_gross, preferred_return

    # ------------------------------------------------------------------
    # Portfolio hierarchy builder
    # ------------------------------------------------------------------

    def _build_hierarchy(self, wb, org, fund, schemes, companies,
                         investments, domain_map, filepath):
        """Build PortfolioNode hierarchy from Gemini-classified portfolio_hierarchy sheet."""
        sheet_name = _dm_first(domain_map, 'portfolio_hierarchy')
        if not sheet_name or sheet_name not in wb.sheetnames:
            return

        ws = wb[sheet_name]
        _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
        if not rows:
            return

        # Get or create snapshot
        snapshot = PortfolioSnapshot.objects.filter(
            organization=org, is_active=True, source='excel_parse',
        ).first()
        if not snapshot:
            PortfolioSnapshot.objects.filter(
                organization=org, is_active=True, source='excel_parse',
            ).update(is_active=False)
            snapshot = PortfolioSnapshot.objects.create(
                organization=org,
                schema_version='2.0',
                base_currency='INR',
                source='excel_parse',
                is_active=True,
            )

        fund_slug = slugify(fund.name)
        fund_node_id = f'fund_{fund_slug}'

        # Delete old nodes for this fund
        PortfolioNode.objects.filter(
            snapshot=snapshot, node_id__startswith=f'fund_{fund_slug}').delete()

        # Create fund-level node (financials will be aggregated later)
        fund_node, _ = PortfolioNode.objects.get_or_create(
            snapshot=snapshot,
            node_id=fund_node_id,
            defaults={
                'name': fund.name,
                'level': 'fund',
                'parent_node_id': None,
                'financials': {},
            },
        )

        sector_nodes = {}   # sector_name -> PortfolioNode
        sector_companies = {}  # sector_name -> list of company financials dicts
        all_company_financials = []  # all company financials for fund-level agg
        node_count = 1

        for row in rows:
            name = _find_col_str(
                row, 'Company Name', 'Company', 'Name')
            if _is_junk_row(name):
                continue
            sector = _find_col_str(row, 'Sector', 'Industry')
            if not sector:
                sector = 'Other'
            city = _find_col_str(row, 'City', 'HQ City', 'Headquarters City', 'headquarters_city')
            stage = _find_col_str(row, 'Stage', 'Round', 'Funding Round')
            sub_sector = _find_col_str(row, 'Sub-Sector', 'Sub Sector', 'Subsector', 'Segment')

            pc_updates = {}
            if city:
                pc_updates['headquarters_city'] = city
            if sub_sector:
                pc_updates['sub_sector'] = sub_sector
            if pc_updates:
                PortfolioCompany.objects.filter(organization=org, name=name).update(**pc_updates)

            if stage:
                Investment.objects.filter(
                    scheme__fund__organization=org,
                    company_name=name,
                    stage='',
                ).update(stage=stage)

            # Create sector node if needed
            sector_slug = slugify(sector)
            sector_id = f'{fund_node_id}::sector_{sector_slug}'
            if sector not in sector_nodes:
                sector_node, _ = PortfolioNode.objects.get_or_create(
                    snapshot=snapshot,
                    node_id=sector_id,
                    defaults={
                        'name': sector,
                        'level': 'sector',
                        'parent_node_id': fund_node_id,
                        'parent': fund_node,
                        'financials': {},
                    },
                )
                sector_nodes[sector] = sector_node
                sector_companies[sector] = []
                node_count += 1

            # Create company node
            company_slug = slugify(name)
            company_id = f'{sector_id}::company_{company_slug}'
            invested = _find_col_decimal(
                row, 'Cost(₹Cr)', 'Cost', 'Invested', 'Total Invested')
            fair_value = _find_col_decimal(
                row, 'FV(₹Cr)', 'Fair Value', 'FV', 'Current Value')
            hold_pct = _find_col_decimal(row, 'Hold%', 'Holding %', 'Ownership')
            node_status = _find_col_str(row, 'Status', default='Active')

            # Collect MIS data from other sheets
            mis_data = self._collect_mis_data(wb, name, domain_map)

            # Build company summary from latest monthly_pl entry
            company_fin = {
                'invested': float(invested) if invested else 0,
                'fair_value': float(fair_value) if fair_value else 0,
                'holding_pct': float(hold_pct) if hold_pct else 0,
                'status': node_status,
                **mis_data,
            }
            company_fin['summary'] = _build_summary_from_pl(
                company_fin.get('monthly_pl', []),
                company_fin.get('budget_vs_actual', []),
            )

            PortfolioNode.objects.get_or_create(
                snapshot=snapshot,
                node_id=company_id,
                defaults={
                    'name': name,
                    'level': 'company',
                    'parent_node_id': sector_id,
                    'parent': sector_nodes[sector],
                    'financials': company_fin,
                },
            )
            sector_companies[sector].append(company_fin)
            all_company_financials.append(company_fin)
            node_count += 1

        # Aggregate financials upward: sector nodes
        for sector_name, sector_node in sector_nodes.items():
            children_fin = sector_companies.get(sector_name, [])
            sector_fin = _aggregate_financials(children_fin)
            sector_node.financials = sector_fin
            sector_node.save(update_fields=['financials'])

        # Aggregate financials upward: fund node
        fund_fin = _aggregate_financials(all_company_financials)
        fund_node.financials = fund_fin
        fund_node.save(update_fields=['financials'])

        # Invalidate portfolio service cache
        try:
            from api.portfolio import service as portfolio_service
            portfolio_service.reload(org.id)
        except Exception as e:
            logger.warning(f'Portfolio cache reload failed: {e}')

        logger.info(f'  Hierarchy: {node_count} nodes')

    def _collect_mis_data(self, wb, company_name, domain_map):
        """Collect Monthly P&L and Budget vs Actual data for a company."""
        data = {}

        # Monthly P&L — use Gemini-classified financials sheets
        fin_sheets = _dm_sheets(domain_map, 'financials_pl_bva') if domain_map else []
        for sn in fin_sheets:
            if sn in wb.sheetnames:
                ws = wb[sn]
                _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
                pl_entries = []
                for row in rows:
                    name = _find_col_str(
                        row, 'Company Name', 'Company', 'Name')
                    if name != company_name:
                        continue
                    period = _find_col_str(row, 'Period', 'Month')
                    revenue = _find_col_decimal(
                        row, 'Revenue(₹Cr)', 'Revenue')
                    ebitda = _find_col_decimal(
                        row, 'EBITDA(₹Cr)', 'EBITDA')
                    gp = _find_col_decimal(
                        row, 'Gross Profit(₹Cr)', 'Gross Profit')
                    gp_pct = _find_col_decimal(row, 'GP%', 'GP Margin')
                    ebitda_pct = _find_col_decimal(
                        row, 'EBITDA%', 'EBITDA Margin')
                    opex = _find_col_decimal(row, 'OpEx(₹Cr)', 'OpEx')
                    cogs = _find_col_decimal(row, 'COGS(₹Cr)', 'COGS')

                    # Convert percentage stored as decimal
                    if gp_pct and abs(float(gp_pct)) <= 1:
                        gp_pct = round(float(gp_pct) * 100, 1)
                    if ebitda_pct and abs(float(ebitda_pct)) <= 1:
                        ebitda_pct = round(float(ebitda_pct) * 100, 1)

                    pl_entries.append({
                        'period': period,
                        'revenue': float(revenue) if revenue else 0,
                        'cogs': float(cogs) if cogs else 0,
                        'gross_profit': float(gp) if gp else 0,
                        'gp_pct': float(gp_pct) if gp_pct else None,
                        'opex': float(opex) if opex else 0,
                        'ebitda': float(ebitda) if ebitda else 0,
                        'ebitda_pct': float(ebitda_pct) if ebitda_pct else None,
                    })
                if pl_entries:
                    data['monthly_pl'] = pl_entries
                break

        # Budget vs Actual — same Gemini-classified financials sheets
        for sn in fin_sheets:
            if sn in wb.sheetnames:
                ws = wb[sn]
                _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
                bva_entries = []
                for row in rows:
                    name = _find_col_str(
                        row, 'Company Name', 'Company', 'Name')
                    if name != company_name:
                        continue
                    line_item = _find_col_str(row, 'Line Item', 'Item')
                    actual = _find_col_decimal(
                        row, 'Actual YTD', 'Actual', 'Actuals')
                    budget = _find_col_decimal(
                        row, 'Budget YTD', 'Budget', 'Budgeted')
                    bva_entries.append({
                        'line_item': line_item,
                        'actual': float(actual) if actual else 0,
                        'budget': float(budget) if budget else 0,
                    })
                if bva_entries:
                    data['budget_vs_actual'] = bva_entries
                break

        return data

    # ------------------------------------------------------------------
    # Post-import validation
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # IC Pipeline seeding
    # ------------------------------------------------------------------

    _STATUS_TO_PIPELINE_STAGE = {
        'active':           'approved',
        'partially_exited': 'approved',
        'fully_exited':     'closed',
        'written_off':      'rejected',
    }

    def _seed_ic_pipeline(self, org, fund, investments):
        """
        Idempotent: upsert one DealPipeline record per Investment so the
        IC Workflow dashboard widget shows real data immediately after import.
        Keyed on (organization, fund, company_name) — safe to re-import.
        """
        from ic_workflow.models import DealPipeline
        for inv in investments.values():
            co    = inv.portfolio_company
            stage = self._STATUS_TO_PIPELINE_STAGE.get(inv.status, 'approved')
            DealPipeline.objects.update_or_create(
                organization=org,
                fund=fund,
                company_name=co.name,
                defaults={
                    'sector':                   co.sector or inv.sector or '',
                    'stage':                    stage,
                    'proposed_investment_inr':  inv.total_invested,
                    'linked_portfolio_company': co,
                    'source_channel':           'other',
                    'sourced_date':             inv.investment_date,
                },
            )

    def _validate_imported_companies(self, fund):
        """Detect and warn when metadata field names were imported as company names.

        Uses Gemini Pass 3 row_type classification to identify names that look
        like metadata labels rather than real company names. Works for any
        language — no hardcoded keyword list.
        """
        from investments.models import Investment
        invs = Investment.objects.filter(scheme__fund=fund).values_list('company_name', flat=True)
        names = [n for n in invs if n and n.strip()]
        if not names:
            return

        # Classify all company names — real entities vs metadata labels
        name_map = self._classify_labels(
            names, 'row_type',
            context='These are company names from a fund portfolio. '
                    'Identify any that look like metadata field labels '
                    '(e.g., fund configuration fields, sheet headers) '
                    'rather than real company/entity names.')
        suspect = [n for n in names if name_map.get(n) in ('header', 'note', 'subtotal', 'total', 'serial')]

        if suspect:
            msg = (
                f'IMPORT INTEGRITY ERROR for fund "{fund.name}": '
                f'{len(suspect)} investments have company names that look like '
                f'fund metadata labels, not real companies: {suspect}. '
                f'This usually means a Cover or Summary sheet was incorrectly '
                f'used as a data source. Delete these investments and reimport '
                f'from the correct data sheet.'
            )
            logger.critical(msg)
            self.errors.append({'section': 'validation', 'error': msg})

        # Also warn if total investment count looks suspiciously low
        total_inv = len(invs)
        if 0 < total_inv <= 15:
            logger.warning(
                f'Fund "{fund.name}" imported only {total_inv} investments. '
                f'If the fund is expected to have more portfolio companies, '
                f'check that the correct data sheet was used (not a Cover/Summary page).'
            )

    # ------------------------------------------------------------------
    # Collect counts
    # ------------------------------------------------------------------

    def _collect_counts(self, org, fund):
        """Collect record counts for the result summary — all counts scoped to this fund."""
        counts = {
            'funds': 1,
            'schemes': Scheme.objects.filter(fund=fund).count(),
            # Fund-scoped: only LPs who have a commitment under this fund
            'investors': Investor.objects.filter(
                commitments__scheme__fund=fund).distinct().count(),
            'commitments': Commitment.objects.filter(scheme__fund=fund).count(),
            'capital_calls': CapitalCall.objects.filter(scheme__fund=fund).count(),
            'capital_call_line_items': CapitalCallLineItem.objects.filter(
                capital_call__scheme__fund=fund).count(),
            # Fund-scoped: only companies that have an investment under this fund
            'portfolio_companies': PortfolioCompany.objects.filter(
                investments__scheme__fund=fund).distinct().count(),
            'investments': Investment.objects.filter(scheme__fund=fund).count(),
            'tranches': InvestmentTranche.objects.filter(
                investment__scheme__fund=fund).count(),
            'valuations': Valuation.objects.filter(
                investment__scheme__fund=fund).count(),
            'nav_records': NAVRecord.objects.filter(scheme__fund=fund).count(),
            'exit_events': ExitEvent.objects.filter(
                investment__scheme__fund=fund).count(),
            'distributions': Distribution.objects.filter(scheme__fund=fund).count(),
            'distribution_line_items': DistributionLineItem.objects.filter(
                distribution__scheme__fund=fund).count(),
            # Fund-scoped: distinct accounts actually referenced in this fund's ledger
            'chart_of_accounts': ChartOfAccounts.objects.filter(
                Q(debit_entries__scheme__fund=fund) |
                Q(credit_entries__scheme__fund=fund)
            ).distinct().count(),
            'ledger_entries': FundLedger.objects.filter(
                scheme__fund=fund).count(),
            'management_fees': ManagementFeeSchedule.objects.filter(
                scheme__fund=fund).count(),
            'carried_interest': CarriedInterest.objects.filter(
                scheme__fund=fund).count(),
            # Fund-scoped: entities directly linked to this fund record
            'entities': Entity.objects.filter(
                Q(managed_funds=fund) | Q(trustee_funds=fund) |
                Q(sponsored_funds=fund) | Q(custodian_funds=fund) |
                Q(audited_funds=fund)
            ).distinct().count(),
            'lp_capital_accounts': LPCapitalAccount.objects.filter(
                commitment__scheme__fund=fund).count(),
            'hierarchy_nodes': PortfolioNode.objects.filter(
                snapshot__organization=org,
                node_id__startswith=f'fund_{slugify(fund.name)}'
            ).count(),
        }

        if HAS_COMPLIANCE:
            counts['sebi_reports'] = SEBIReport.objects.filter(fund=fund).count()
            counts['compliance_calendar'] = ComplianceCalendar.objects.filter(
                fund=fund).count()

        return counts


# ---------------------------------------------------------------------------
# Helpers for management command output suppression (kept for legacy fallback)
# ---------------------------------------------------------------------------

class _NullOutput:
    """Swallows all write calls."""
    def write(self, *args, **kwargs):
        pass

    def flush(self):
        pass


class _NullStyle:
    """Mimics Django management command style object."""
    def SUCCESS(self, text):
        return text

    def ERROR(self, text):
        return text

    def WARNING(self, text):
        return text
