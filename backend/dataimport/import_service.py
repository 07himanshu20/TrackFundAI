"""
FundImportService — orchestrates the import of a single fund Excel file.

Uses Gemini AI to semantically map columns, then imports data
into all Django models using header-based row reading.

Two strategies:
  1. Gemini-mapped: Gemini classifies sheets → domain map → read by headers
  2. Legacy fallback: delegates to import_fund_excel.py (hardcoded sheet names)

Strategy 1 is tried first. If Gemini mapping is unavailable, falls back to 2.
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

INVESTOR_TYPE_MAP = {
    'insurance': 'insurance', 'pension': 'pension', 'huf': 'huf',
    'trust': 'trust', 'individual': 'individual', 'fund_of_funds': 'fund_of_funds',
    'fpi': 'fpi', 'company': 'company', 'nri': 'nri', 'family_office': 'family_office',
    'endowment': 'endowment', 'llp': 'llp', 'sovereign': 'sovereign', 'bank': 'bank',
    # Extended mappings for varied Excel data
    'domestic pension': 'pension', 'bilateral dfi': 'company',
    'sovereign wealth fund': 'sovereign', 'corporate': 'company',
    'hnwi': 'individual', 'high net worth': 'individual',
    'family trust': 'trust', 'private trust': 'trust',
    'endowment fund': 'endowment', 'fund of funds': 'fund_of_funds',
    'foreign portfolio investor': 'fpi',
}

_SECTION_HEADERS = {
    'ORGANIZATION MASTER', 'KEY ENTITIES', 'GP USERS', 'FUND ACCESS MATRIX',
    'FUND MASTER DATA', 'SCHEMES', 'PORTFOLIO HIERARCHY',
    'CROSS-FUND SECTOR MAPPING', 'PORTFOLIO COMPANIES', 'INVESTMENTS',
    'INVESTMENT TRANCHES', 'CAPITAL CALLS', 'CAPITAL CALL LINE ITEMS',
    'NAV RECORDS', 'EXIT EVENTS', 'DISTRIBUTIONS', 'DISTRIBUTION LINE ITEMS',
    'CHART OF ACCOUNTS', 'DOUBLE-ENTRY', 'CARRIED INTEREST',
    'COMPLIANCE CALENDAR', 'SEBI REPORT FILINGS', 'AML DUE DILIGENCE',
    'COMPLIANCE TEST REPORT', 'SEBI CIRCULARS', 'PPM AMENDMENTS',
    'LIMITED PARTNERS', 'PORTFOLIO COMPANIES', 'PORTFOLIO VALUATIONS',
    'PORTFOLIO KPIs', 'NAV & FUND ACCOUNTING', 'EXITS, DISTRIBUTIONS',
    'BUDGET vs ACTUAL', 'MONTHLY P&L', 'MONTHLY BALANCE SHEET',
    'MONTHLY CASH FLOW',
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
_COVER_SHEET_NAMES = {
    'cover', 'summary', 'index', 'contents', 'table of contents',
    'toc', 'overview', 'dashboard', 'readme', 'read me', 'intro',
    'introduction', 'about', 'instructions', 'guide', 'help',
    'front page', 'front sheet', 'title', 'home',
}

def _is_cover_or_summary_sheet(sheet_name):
    """Return True if this sheet name indicates a cover/summary page.

    Cover sheets may have fund statistics (company count, total FV, etc.)
    that look like real data but are just display aggregates — often
    computed by hand and prone to errors. We always derive statistics
    from the actual data sheets instead.
    """
    sn = sheet_name.lower().strip()
    # Exact match or starts-with match
    if sn in _COVER_SHEET_NAMES:
        return True
    for kw in _COVER_SHEET_NAMES:
        if sn.startswith(kw):
            return True
    return False


def _is_section_header(val):
    if not val:
        return False
    s = str(val).strip()
    for header in _SECTION_HEADERS:
        if header in s.upper():
            return True
    return s.isupper() and len(s) > 15 and ' ' in s


def _is_header_row(val):
    """Check if a cell looks like a header row marker (e.g., '#' or 'S.No')."""
    if not val:
        return False
    s = str(val).strip()
    return s in ('#', 'S.No', 'Sr', 'Sr.', 'SNo', 'S.No.')


# Prefixes that identify non-data rows masquerading as data:
# subtotal lines, grand-total lines, repeated header rows, separator labels.
_JUNK_NAME_PREFIXES = (
    'subtotal', 'sub-total', 'sub total',
    'total', 'grand total', 'grand-total', 'grandtotal',
    'sum total', 'sum', 'average', 'avg', 'mean',
    's.no', 'sno', 'sr.', 'sr.no', 'srno', 'serial',
    '#', '—', '–',
    'note:', 'notes:', 'remark', 'footer',
)

# Word endings that conclusively identify a string as a company name and
# prevent false-positive junk detection on names like "Total Fitness Pvt Ltd"
# or "Summary Holdings Ltd".
_COMPANY_NAME_SUFFIXES = (
    ' ltd', ' ltd.', ' limited', ' pvt', ' pvt.',
    ' private limited', ' private', ' inc', ' inc.',
    ' corp', ' corp.', ' corporation', ' llp', ' lp',
    ' enterprises', ' holdings', ' group', ' solutions',
    ' technologies', ' tech', ' systems', ' services',
    ' ventures', ' capital', ' finance', ' fintech',
    ' industries', ' infratech', ' infra',
)


def _is_junk_row(name):
    """Return True if *name* looks like a subtotal, total, header, or serial-
    number row — NOT a real portfolio company name.

    Catches patterns like:
      - "Subtotal — Consumer & Retail"
      - "Grand Total"
      - "Total (25 companies)"
      - "S.No" / "#"  (repeated column header within data area)
      - "25"           (plain integer — row counter or summary value)
      - "—"            (dash placeholder)

    IMPORTANT: names ending with recognised company suffixes (e.g. "Pvt Ltd",
    "Limited", "Inc") are NEVER junk, regardless of what they start with.
    This prevents false positives on "Total Fitness Pvt Ltd" or
    "Summary Holdings Ltd".
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
    n_lower = n.lower()
    # A name that ends with a known company suffix is always a real company —
    # never flag it as junk even if its prefix matches a suspicious keyword.
    for suffix in _COMPANY_NAME_SUFFIXES:
        if n_lower.endswith(suffix):
            return False
    # Check against known junk prefixes (case-insensitive)
    for prefix in _JUNK_NAME_PREFIXES:
        if n_lower.startswith(prefix):
            return True
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

    Handles both:
    - Single-cell all-caps titles:  "PORTFOLIO COMPANIES"
    - Pipe-delimited multi-cell titles spread across columns:
        col1="PORTFOLIO INVESTMENTS — FULL REGISTER"  col2="Fund Name"  col3="DD-MMM-YYYY"
      In this case the first cell contains the meaningful domain keyword and
      the remaining cells are metadata/context values.
    """
    first_cell = ws.cell(r, 1).value
    if first_cell is None:
        return False, ''
    first_str = str(first_cell).strip()
    if not first_str:
        return False, ''

    # Check total non-empty cells in the row
    cell_vals = []
    for c in range(1, max_col + 1):
        v = ws.cell(r, c).value
        if v is not None:
            cell_vals.append(str(v).strip())

    if not cell_vals:
        return False, ''

    # Compute uppercase ratio once — used for both the 1-2 cell and 3+ cell checks
    alpha_chars = [ch for ch in first_str if ch.isalpha()]
    upper_ratio = (
        sum(1 for ch in alpha_chars if ch.isupper()) / len(alpha_chars)
        if alpha_chars else 0.0
    )

    # Classic single/double cell section header
    # Require predominantly-uppercase text (≥70%) to avoid mistaking a
    # mixed-case row header (e.g. "Capital Calls" as a sub-label) for a
    # true section separator.
    if len(cell_vals) <= 2 and upper_ratio >= 0.70:
        if _is_section_header(first_str):
            return True, first_str
        if first_str.isupper() and len(first_str) > 3:
            return True, first_str
        if any(kw in first_str.upper() for kw in [
            'PORTFOLIO COMPANIES', 'PORTFOLIO INVESTMENTS', 'INVESTMENTS',
            'INVESTMENT TRANCHES', 'CAPITAL CALLS', 'CAPITAL CALL LINE ITEMS',
            'EXIT EVENTS', 'DISTRIBUTIONS', 'NAV RECORDS', 'SCHEMES',
            'FUND MASTER', 'LIMITED PARTNERS', 'COMMITMENTS', 'VALUATIONS',
        ]):
            return True, first_str

    # Pipe-delimited multi-cell title: first cell contains a domain keyword
    # AND is predominantly uppercase (title-case guard prevents a company name
    # like "Investments Holdings Ltd" from being mistaken for a section header).
    # e.g. first_str = "PORTFOLIO INVESTMENTS — FULL REGISTER"
    if len(cell_vals) >= 3:
        first_upper = first_str.upper()
        if upper_ratio >= 0.70:
            domain_kws = [
                'PORTFOLIO COMPANIES', 'PORTFOLIO INVESTMENTS', 'INVESTMENTS',
                'INVESTMENT TRANCHES', 'CAPITAL CALLS', 'EXIT EVENTS',
                'DISTRIBUTIONS', 'NAV RECORDS', 'LIMITED PARTNERS',
                'FUND MASTER', 'COMMITMENTS', 'VALUATIONS', 'SCHEMES',
            ]
            for kw in domain_kws:
                if kw in first_upper:
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


def _get_section_rows(ws, domain_map, domain_key, section_keywords=None):
    """Smart row reader: tries dedicated domain sheet first, then falls back
    to reading a specific section from a multi-section sheet.

    Returns (headers_dict, rows) like read_table_from_sheet.
    """
    sheet_name = domain_map.get(domain_key)
    if not sheet_name or sheet_name not in (ws.parent.sheetnames if hasattr(ws, 'parent') else []):
        return {}, []

    target_ws = ws.parent[sheet_name] if hasattr(ws, 'parent') else ws

    # First try reading as a flat table
    headers, rows = read_table_from_sheet(target_ws)

    # If we got rows, check if the headers match what we expect
    # (i.e., they have the right columns for this domain)
    if rows:
        return headers, rows

    # If no rows, try reading sections
    if section_keywords:
        sections = read_all_sections_from_sheet(target_ws)
        for kw in section_keywords:
            for sec_name, (sec_headers, sec_rows) in sections.items():
                if kw.upper() in sec_name.upper():
                    if sec_rows:
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

    Pass 1 — Exact case-sensitive:        "Company Name" == "Company Name"
    Pass 2 — Exact case-insensitive:      "company name" == "Company Name"
    Pass 3 — Key ends with candidate:     "Company Name" ends with "Name"
              (covers typical label suffixes without matching mid-words)
    Pass 4 — Candidate ends with key:     "HQ City" key, candidate "City"
    Pass 5 — Loose substring (last resort, only for single-word candidates
              whose length ≥ 4 to avoid false positives on short tokens)
    """
    # Pass 1 — exact case-sensitive
    for c in candidates:
        if c in row:
            return row[c]

    # Build lowercase lookup once
    row_lower = {k.lower(): v for k, v in row.items()}

    # Pass 2 — exact case-insensitive
    for c in candidates:
        cl = c.lower()
        if cl in row_lower:
            return row_lower[cl]

    # Pass 3 — the column header ends with the candidate phrase (word-boundary)
    # e.g. key="Company Name", candidate="Name" → "company name".endswith(" name") ✓
    # e.g. key="Scheme Name", candidate="Company Name" → no match ✓
    for c in candidates:
        cl = c.lower()
        for key_l, val in row_lower.items():
            if key_l == cl:
                return val
            # Must be a word boundary: key ends with " <candidate>"
            if key_l.endswith(' ' + cl) or key_l.endswith('-' + cl):
                return val

    # Pass 4 — the candidate ends with the column header (candidate is more specific)
    # e.g. candidate="HQ City", key="City" → "hq city".endswith("city") but also
    # we only trigger this if key is a meaningful suffix of candidate
    for c in candidates:
        cl = c.lower()
        for key_l, val in row_lower.items():
            if cl.endswith(' ' + key_l) or cl.endswith('-' + key_l):
                return val

    # Pass 5 — loose substring, guarded: only for multi-word candidates or
    # single words with length ≥ 8 (avoids false matches on short tokens
    # like "Sector" (6 chars) matching "Investor Sector" or "Sector Group").
    # Multi-word candidates (e.g. "Company Name") are allowed through since
    # the word boundary check makes them precise enough.
    for c in candidates:
        cl = c.lower()
        if len(c.split()) == 1 and len(c) < 8:
            continue  # too short/ambiguous for loose matching
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
    """Build a mapping from canonical domain name to actual sheet name.

    Uses Gemini's sheet classification (Pass 1) to find which sheet
    corresponds to each domain. Falls back to keyword matching on
    sheet names if a domain wasn't classified.

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

    domain_map = {}  # domain → sheet_name

    # From Gemini classification — but sanitise: never let a cover sheet
    # be assigned to a data-only domain, no matter what Gemini says.
    for cls in classifications:
        sheet_name = cls.get('sheet_name', '')
        domains = cls.get('domains', [])
        for domain in domains:
            if domain and domain != 'unknown' and domain not in domain_map:
                if domain in _DATA_ONLY_DOMAINS and _is_cover_or_summary_sheet(sheet_name):
                    logger.warning(
                        f'Gemini classified cover/summary sheet "{sheet_name}" as '
                        f'data domain "{domain}" — overriding to prevent metadata '
                        f'rows being imported as company/investment records.'
                    )
                    continue
                domain_map[domain] = sheet_name

    # Fallback keyword matching for sheets not classified by Gemini.
    # Order matters: more specific domains first to avoid greedy matches.
    # Each entry: (domain, keywords, exclude_keywords)
    keyword_rules = [
        ('portfolio_hierarchy', ['hierarchy', 'structure'], []),
        ('portfolio_investments', ['investment', 'companies', 'portfolio compan'], ['hierarchy', 'kpi', 'p&l', 'budget', 'valuation']),
        ('fund_scheme_master', ['fund', 'scheme', 'master', 'cover'], ['investment', 'hierarchy', 'p&l', 'kpi']),
        ('organization_users', ['organization', 'users', 'entities'], []),
        ('investors_aml', ['investor', 'lp', 'limited partner', 'aml'], ['exit', 'distribution']),
        ('commitments', ['commitment'], []),
        ('capital_calls', ['capital call', 'drawdown'], []),
        ('valuations_kpis', ['valuation', 'kpi', 'metrics'], ['budget']),
        # 'NAV & Accounting' (time-series) must win over 'NAV Calculation' (static).
        # Two-step: first try sheets that are explicitly a history/accounting table,
        # then fall back to any nav/accounting sheet that isn't a calculation worksheet.
        ('nav_accounting', ['nav & accounting', 'nav and accounting', 'nav accounting', 'nav history', 'nav monthly'], ['chart']),
        ('nav_accounting', ['nav', 'accounting', 'ledger'], ['chart', 'calculation', 'calcu']),
        ('exits_distributions', ['exit', 'distribution', 'realized'], ['investor', 'lp']),
        ('compliance', ['compliance', 'sebi', 'regulatory'], []),
    ]

    taken_sheets = set(domain_map.values())

    for domain, keywords, excludes in keyword_rules:
        if domain in domain_map:
            continue
        for sheet_name in wb.sheetnames:
            # NEVER assign a cover/summary sheet to any data-only domain
            if domain in _DATA_ONLY_DOMAINS and _is_cover_or_summary_sheet(sheet_name):
                continue
            sn_lower = sheet_name.lower()
            if any(kw in sn_lower for kw in keywords):
                if excludes and any(ex in sn_lower for ex in excludes):
                    continue
                domain_map[domain] = sheet_name
                taken_sheets.add(sheet_name)
                break

    # Second pass: broader matching without exclusions (but still no cover sheets for data domains)
    for domain, keywords, _ in keyword_rules:
        if domain in domain_map:
            continue
        for sheet_name in wb.sheetnames:
            if domain in _DATA_ONLY_DOMAINS and _is_cover_or_summary_sheet(sheet_name):
                continue
            sn_lower = sheet_name.lower()
            if any(kw in sn_lower for kw in keywords):
                domain_map[domain] = sheet_name
                break

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

    def _get_alias(self, ws) -> dict:
        """Return Gemini-built alias map for this worksheet (empty dict if none)."""
        return self._gemini_sheet_aliases.get(getattr(ws, 'title', ''), {})

    def import_file(self, import_file_record, progress_cb=None):
        """
        Main entry point. Processes a single ImportFile record.
        """
        filepath = import_file_record.file.path

        def _progress(pct, msg):
            if progress_cb:
                progress_cb(pct, msg)

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
        except Exception as e:
            logger.warning(f'Gemini mapping failed: {e}')
            mapping_result = None
            import_file_record.status = 'importing'
            import_file_record.save(update_fields=['status'])

        # Step 2: Import data
        _progress(25, 'Starting data import...')

        classifications = []
        column_mappings = {}
        if mapping_result:
            classifications = mapping_result.get('sheet_classifications', [])
            column_mappings = mapping_result.get('column_mappings', {})

        result = self._do_import(filepath, classifications, _progress, column_mappings)

        # Save fund reference back to ImportFile for cascading delete support
        if self._imported_fund:
            import_file_record.fund = self._imported_fund
            import_file_record.fund_name = self._imported_fund.name
            import_file_record.save(update_fields=['fund', 'fund_name'])

        return result

    @transaction.atomic
    def _do_import(self, filepath, classifications, progress_cb, column_mappings=None):
        """
        Run the actual import using Gemini's sheet classification.

        Builds a domain→sheet_name map, then reads each sheet by headers
        (no hardcoded column positions or sheet names).

        column_mappings: {sheet_name: {sections: [{mappings: [{excel_column, canonical_field, confidence}]}]}}
        Built by Gemini Pass-2. We flatten it into self._gemini_sheet_aliases so
        read_table_from_sheet / read_all_sections_from_sheet can enrich every row
        with canonical field names regardless of how the Excel column was labelled.
        """
        wb = openpyxl.load_workbook(filepath, data_only=True)
        org = self.org

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
            self._extract_fund_metadata(wb, fund, schemes)
        except Exception as e:
            logger.warning(f'Fund metadata extraction error: {e}')
            self.errors.append({'section': 'fund_metadata', 'error': str(e)})

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
            self._compute_carried_interest(schemes)
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

    def _extract_fund_name(self, wb, domain_map, filepath):
        """Extract fund name from Cover sheet, first sheet, or filename."""
        # Try Cover sheet
        for name in wb.sheetnames:
            if 'cover' in name.lower():
                ws = wb[name]
                # Fund name is usually in one of the first 5 rows
                for r in range(1, 8):
                    for c in range(1, 5):
                        val = ws.cell(r, c).value
                        if val:
                            s = _str(val)
                            # Skip generic titles
                            if any(skip in s.lower() for skip in
                                   ['trackfundai', 'trivesta', 'powered by',
                                    'operating memorandum', 'fund data']):
                                continue
                            # A fund name is typically 3+ words or has common
                            # fund-name patterns
                            if (len(s) > 10 and
                                    any(kw in s.lower() for kw in
                                        ['fund', 'capital', 'ventures',
                                         'trust', 'partners', 'growth',
                                         'india', 'infra'])):
                                return s
                break

        # Try Fund & Scheme Master domain
        sheet_name = domain_map.get('fund_scheme_master')
        if sheet_name and sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            for r in range(1, 15):
                label = _str(ws.cell(r, 1).value).lower()
                if 'fund name' in label:
                    return _str(ws.cell(r, 2).value)

        # Fallback: extract from filename
        basename = os.path.basename(filepath)
        # Remove prefix like "Mock_06_" and extension
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
        cat_code = 'CAT_II'  # default

        # Try to detect category from the fund master sheet
        sheet_name = domain_map.get('fund_scheme_master')
        if sheet_name and sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            for r in range(1, 20):
                label = _str(ws.cell(r, 1).value).lower()
                val = _str(ws.cell(r, 2).value)
                if 'category code' in label or 'sebi category code' in label:
                    if val and val in CATEGORY_MAP:
                        cat_code = val
                        break
                elif 'category' in label:
                    if 'i' in val.lower() and 'iii' not in val.lower() and 'ii' not in val.lower():
                        cat_code = 'CAT_I_VCF'
                    elif 'iii' in val.lower():
                        cat_code = 'CAT_III_LVF'

        fund_category = FundCategory.objects.filter(
            sebi_category_code=cat_code).first()

        # Read SEBI reg, structure from fund master sheet
        sebi_reg = ''
        structure = 'trust'
        fund_pan = ''
        fund_gstin = ''
        gift_city = False

        if sheet_name and sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            for r in range(1, 20):
                label = _str(ws.cell(r, 1).value).lower()
                val = ws.cell(r, 2).value
                if not val:
                    continue
                val_str = _str(val)
                if any(kw in label for kw in ['sebi reg', 'registration']):
                    sebi_reg = val_str
                elif 'structure' in label:
                    structure = val_str.lower() if val_str.lower() in ('trust', 'company', 'llp') else 'trust'
                elif label == 'pan':
                    fund_pan = val_str[:10]
                elif label == 'gstin':
                    fund_gstin = val_str
                elif 'gift' in label:
                    gift_city = val_str.lower() in ('yes', 'true', '1', 'y')

        fund, created = Fund.objects.get_or_create(
            organization=org,
            name=fund_name,
            defaults={
                'fund_category': fund_category,
                'structure_type': structure,
                'base_currency': 'INR',
                'sebi_registration_number': sebi_reg,
                'pan': fund_pan,
                'gstin': fund_gstin,
                'is_gift_city': gift_city,
            },
        )
        logger.info(f'{"Created" if created else "Found"} Fund: {fund.name}')

        schemes = {}

        # Check for explicit scheme data in SCHEMES section
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
                        status_map = {
                            'investing': 'investing', 'fundraising': 'fundraising',
                            'harvesting': 'harvesting', 'closed': 'closed',
                            'winding up': 'winding_up',
                        }
                        scheme_status = status_map.get(
                            status_raw.lower(), 'investing')

                        carry_type = 'european'
                        if carry_type_raw.lower() in ('american', 'deal-by-deal',
                                                       'deal by deal'):
                            carry_type = 'american'

                        s, _ = Scheme.objects.get_or_create(
                            fund=fund, name=sn,
                            defaults={
                                'vintage_year': int(vintage) if vintage else date.today().year,
                                'first_close_date': first_close,
                                'final_close_date': final_close,
                                'scheme_size': scheme_size,
                                'tenure_years': int(tenure) if tenure else None,
                                'hurdle_rate_pct': hurdle,
                                'carry_pct': carry,
                                'carry_type': carry_type,
                                'management_fee_pct': fee_pct,
                                'sponsor_commitment_pct': sponsor_pct,
                                'scheme_status': scheme_status,
                                'is_active': True,
                            },
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
                        s, _ = Scheme.objects.get_or_create(
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
            scheme, _ = Scheme.objects.get_or_create(
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

    def _extract_fund_metadata(self, wb, fund, schemes):
        """Extract rich fund metadata from Cover sheet and update Fund + Scheme."""
        cover_ws = None
        for sn in wb.sheetnames:
            if 'cover' in sn.lower():
                cover_ws = wb[sn]
                break
        if not cover_ws:
            return

        # Build a key-value map from Cover sheet
        # Cover has pairs in columns B/C and F/G
        kv = {}
        for r in range(1, cover_ws.max_row + 1):
            for label_col, val_col in [(2, 3), (6, 7)]:
                label = _str(cover_ws.cell(r, label_col).value).lower()
                val = cover_ws.cell(r, val_col).value
                if label and val is not None:
                    kv[label] = val

        # Update Fund fields
        update_fields = []

        reg_no = kv.get('reg no.') or kv.get('sebi reg') or kv.get('registration')
        if reg_no and not fund.sebi_registration_number:
            fund.sebi_registration_number = _str(reg_no)
            update_fields.append('sebi_registration_number')

        corpus_raw = kv.get('corpus') or kv.get('fund size') or kv.get('target corpus')
        if corpus_raw and not fund.corpus_target:
            # Parse "₹ 3,500 Cr" or similar
            corpus_str = _str(corpus_raw)
            corpus_num = re.sub(r'[₹,\sCr]', '', corpus_str)
            corpus_d = _d(corpus_num)
            if corpus_d:
                fund.corpus_target = corpus_d
                update_fields.append('corpus_target')

        vintage_raw = kv.get('vintage') or kv.get('inception')
        if vintage_raw:
            vintage_str = _str(vintage_raw)
            if not fund.inception_date:
                vintage_date = _date(vintage_str)
                if vintage_date:
                    fund.inception_date = vintage_date
                    update_fields.append('inception_date')
                elif vintage_str.isdigit() and len(vintage_str) == 4:
                    fund.inception_date = date(int(vintage_str), 1, 1)
                    update_fields.append('inception_date')

        pan_raw = kv.get('pan')
        if pan_raw and not fund.pan:
            fund.pan = _str(pan_raw)[:10]
            update_fields.append('pan')

        if update_fields:
            fund.save(update_fields=update_fields)
            logger.info(f'  Fund metadata updated: {update_fields}')

        # Update Scheme fields from Cover data
        default_scheme = list(schemes.values())[0] if schemes else None
        if not default_scheme:
            return

        scheme_updates = []

        vintage_year = kv.get('vintage')
        if vintage_year:
            v_str = _str(vintage_year)
            if v_str.isdigit() and len(v_str) == 4:
                default_scheme.vintage_year = int(v_str)
                scheme_updates.append('vintage_year')

        hurdle_raw = kv.get('hurdle') or kv.get('hurdle rate')
        if hurdle_raw:
            h_str = _str(hurdle_raw).replace('%', '').strip()
            h_val = _d(h_str)
            if h_val and not default_scheme.hurdle_rate_pct:
                default_scheme.hurdle_rate_pct = h_val
                scheme_updates.append('hurdle_rate_pct')

        carry_raw = kv.get('carry') or kv.get('carried interest')
        if carry_raw:
            c_str = _str(carry_raw).replace('%', '').strip()
            c_val = _d(c_str)
            if c_val and not default_scheme.carry_pct:
                default_scheme.carry_pct = c_val
                scheme_updates.append('carry_pct')

        fee_raw = kv.get('mgmt fee') or kv.get('management fee')
        if fee_raw:
            f_str = _str(fee_raw).replace('%', '').strip()
            f_val = _d(f_str)
            if f_val and not default_scheme.management_fee_pct:
                default_scheme.management_fee_pct = f_val
                scheme_updates.append('management_fee_pct')

        corpus_raw2 = kv.get('corpus') or kv.get('fund size')
        if corpus_raw2 and not default_scheme.scheme_size:
            c_str2 = _str(corpus_raw2)
            c_num = re.sub(r'[₹,\sCr]', '', c_str2)
            c_val2 = _d(c_num)
            if c_val2:
                default_scheme.scheme_size = c_val2
                scheme_updates.append('scheme_size')

        if scheme_updates:
            default_scheme.save(update_fields=scheme_updates)
            logger.info(f'  Scheme metadata updated: {scheme_updates}')

    # ------------------------------------------------------------------
    # Investors
    # ------------------------------------------------------------------

    def _import_investors(self, wb, org, domain_map):
        """Import investors from the Investors/LP sheet."""
        sheet_name = domain_map.get('investors_aml')
        if not sheet_name or sheet_name not in wb.sheetnames:
            return {}

        ws = wb[sheet_name]
        _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
        investors = {}

        for row in rows:
            inv_name = _find_col_str(
                row, 'Investor Name', 'LP Name', 'Name', 'Investor')
            if not inv_name:
                continue

            inv_type_raw = _find_col_str(
                row, 'Investor Type', 'LP Type', 'Type', 'Category').lower()
            inv_type = INVESTOR_TYPE_MAP.get(inv_type_raw, 'other')

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
        commit_sheet = domain_map.get('commitments')
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
                        row, 'Close Type', 'Close', default='first_close')
                    close_map = {
                        'first close': 'first_close',
                        'subsequent close': 'subsequent_close',
                        'final close': 'final_close',
                    }
                    close_type = close_map.get(close_type_raw.lower(), 'first_close')

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
        sheet_name = domain_map.get('investors_aml')
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
        cc_sheet = domain_map.get('capital_calls')
        if cc_sheet and cc_sheet in wb.sheetnames:
            ws = wb[cc_sheet]
            sections = read_all_sections_from_sheet(ws, alias_map=self._get_alias(ws))

            # Find the main capital calls section
            cc_rows = []
            for sec_name, (sec_headers, sec_rows) in sections.items():
                if 'CAPITAL CALL LINE' in sec_name.upper():
                    continue  # Skip line item sections for now
                if sec_rows:
                    cc_rows = sec_rows
                    break

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
                    status_map = {
                        'paid': 'paid', 'funded': 'paid', 'yes': 'paid',
                        'pending': 'pending',
                        'partially paid': 'partially_paid',
                        'partial': 'partially_paid',
                        'overdue': 'overdue',
                    }
                    status = status_map.get(status_raw.lower(), 'paid')

                    if not total_amt or total_amt <= 0:
                        continue
                    # Skip summary/total rows (e.g. "TOTAL CALLED") —
                    # call_num is only None when the first column has text, not a number
                    if not call_num:
                        # If purpose looks like a total/summary label, skip row
                        if _is_junk_row(purpose) or (
                            purpose.strip().upper().startswith(('TOTAL', 'VALIDATION', 'SUM', 'CROSS'))
                        ):
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
                    if 'CAPITAL CALL LINE' not in sec_name.upper():
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
                                'payment_status': 'paid' if 'paid' in pay_status_raw.lower() else 'pending',
                                'amount_received': received or called_amt,
                                'payment_date': pay_date or parent_call.call_date,
                            },
                        )
                        line_count += 1

                logger.info(f'  Capital calls (dedicated sheet): {call_count} calls, {line_count} line items')
                return

        # --- Strategy 2: Extract from investors_aml sheet (flat format) ---
        sheet_name = domain_map.get('investors_aml')
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
        sheet_name = domain_map.get('portfolio_investments')
        if not sheet_name or sheet_name not in wb.sheetnames:
            return {}, {}

        ws = wb[sheet_name]
        default_scheme = list(schemes.values())[0] if schemes else None
        if not default_scheme:
            return {}, {}

        companies = {}
        investments = {}

        # Try reading as multi-section sheet first
        sections = read_all_sections_from_sheet(ws, alias_map=self._get_alias(ws))

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
            sec_upper = sec_name.upper()
            if 'INVESTMENT TRANCHE' in sec_upper:
                continue  # Handled by _import_tranches
            elif 'PORTFOLIO COMPAN' in sec_upper or sec_upper == 'COMPANIES':
                # Dedicated company master section
                company_rows = sec_rows
            elif 'PORTFOLIO INVESTMENT' in sec_upper:
                # Combined company+investment sheet — treat as flat table
                combined_rows = sec_rows
            elif 'INVESTMENT' in sec_upper:
                # Pure investment financial data section (no "PORTFOLIO" prefix)
                investment_rows = sec_rows
            elif sec_name == '__default__' and not company_rows and not combined_rows:
                # Sheet started directly with a header row — flat table
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
                    row, 'Instrument Type', 'Instrument', 'Security Type',
                    default='Equity')
                instrument_map = {
                    'equity': 'equity', 'safe': 'safe', 'ccps': 'ccps',
                    'convertible note': 'convertible_note',
                    'convertible': 'convertible_note',
                    'preference': 'preference_shares', 'ccd': 'ccd',
                    'debt': 'debt', 'warrant': 'warrant',
                }
                instrument = instrument_map.get(instrument_raw.lower(), 'equity')

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

                status_map = {
                    'active': 'active', 'partially exited': 'partially_exited',
                    'fully exited': 'fully_exited', 'written off': 'written_off',
                    'write-off': 'written_off', 'exited': 'fully_exited',
                }
                status = status_map.get(status_raw.lower(), 'active')

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
                pc_update['is_quoted'] = listing_raw.lower() in (
                    'listed', 'quoted', 'yes', 'true', '1', 'public')
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

            status_map = {
                'active': 'active', 'partially exited': 'partially_exited',
                'fully exited': 'fully_exited', 'written off': 'written_off',
                'write-off': 'written_off', 'exited': 'fully_exited',
            }
            status = status_map.get(status_raw.lower(), 'active')

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
        sheet_name = domain_map.get('portfolio_investments')
        if not sheet_name or sheet_name not in wb.sheetnames:
            return

        ws = wb[sheet_name]

        # Try multi-section approach first
        sections = read_all_sections_from_sheet(ws, alias_map=self._get_alias(ws))
        tranche_rows = None
        for sec_name, (sec_headers, sec_rows) in sections.items():
            if 'TRANCHE' in sec_name.upper():
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
        """Import valuations from the Valuations sheet.

        Handles:
        - Dedicated "Valuations" sheet (Format B)
        - valuations_kpis domain sheet (Format A, may share with KPIs)
        - Any sheet with 'valuation' in the name
        """
        sheet_name = None

        # Strategy 1: Look for a dedicated Valuations sheet
        for sn in wb.sheetnames:
            sn_lower = sn.lower()
            if 'valuation' in sn_lower and 'kpi' not in sn_lower:
                sheet_name = sn
                break

        # Strategy 2: Fall back to valuations_kpis domain
        if not sheet_name:
            sheet_name = domain_map.get('valuations_kpis')

        # Strategy 3: Any sheet with 'valuation' in the name
        if not sheet_name or sheet_name not in wb.sheetnames:
            for sn in wb.sheetnames:
                if 'valuation' in sn.lower():
                    sheet_name = sn
                    break

        if not sheet_name or sheet_name not in wb.sheetnames:
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

            method_map = {
                'dcf': 'dcf', 'comparables': 'comparables',
                'market comparables': 'comparables',
                'recent transaction': 'recent_transaction',
                'net assets': 'net_assets', 'cost': 'cost',
                'revenue multiple': 'comparables',
                'ebitda multiple': 'comparables',
                'p/e multiple': 'comparables',
                'ev/ebitda': 'comparables',
                'book value': 'net_assets',
                'option pricing model': 'option_pricing',
                'opm': 'option_pricing',
                'nav': 'net_assets',
            }
            methodology = method_map.get(methodology_raw.lower(), 'cost')

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
            # IPEV Technique column may explicitly say "Level 2" for market comparables
            _ipev_technique_raw = _find_col_str(
                row, 'IPEV Technique', 'Technique', 'IPEV Level', 'Level')
            if _ipev_technique_raw:
                _tl = _ipev_technique_raw.lower()
                if 'level 2' in _tl or 'level2' in _tl:
                    ipev_level = 2
                elif 'level 1' in _tl or 'level1' in _tl:
                    ipev_level = 1

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
        """Import KPIs from the Portfolio KPIs sheet.

        Handles both vertical format (one KPI per row) and horizontal/pivot
        format (KPI values across period columns like Oct-24, Nov-24...).
        """
        sheet_name = None
        for sn in wb.sheetnames:
            if 'kpi' in sn.lower():
                sheet_name = sn
                break
        if not sheet_name:
            sheet_name = domain_map.get('valuations_kpis')
        if not sheet_name or sheet_name not in wb.sheetnames:
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
            has_kpi_name_col = any(
                str(h or '').lower().strip() in ('kpi name', 'kpi', 'metric', 'indicator')
                for h in headers_dict.keys()
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

    def _import_kpis_flat_snapshot(self, ws, org, investments, companies):
        """
        Handle Portfolio KPIs sheets in multi-section flat format.

        Each section has its own header row (first cell = 'Company'), followed by
        company data rows.  Each non-identity column is treated as a KPI metric.

        This handles sector-grouped KPI sheets where Consumer & Retail has CAC/GMV
        columns, Financial Svcs has AUM/NIM% columns, etc.  Reads ALL sections so
        every company gets its sector-appropriate KPIs imported.
        """
        SKIP_COL_LOWER = {
            'company', 'company name', 'name', '#', 'id', 'sr', 'sl', 'no',
            'sector', 'industry', 'segment', 'status', 'stage', 'city',
            'cost', 'fv', 'moic',  # Skip computed/redundant columns
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

            # Detect header row: first cell is 'Company' or 'Company Name'
            if first_cell.lower() in ('company', 'company name', 'name'):
                current_headers = []
                for col_idx, val in enumerate(row_vals, 1):
                    if val is None:
                        continue
                    header = str(val).strip()
                    header_norm = _normalize_col_key(header)
                    if header_norm.lower() in SKIP_COL_LOWER or header.lower() in SKIP_COL_LOWER:
                        continue
                    kpi_slug = slugify(header_norm)
                    if not kpi_slug:
                        continue
                    current_headers.append((col_idx, header_norm, kpi_slug))
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

            for col_idx, header_name, kpi_slug in current_headers:
                if col_idx > len(row_vals):
                    continue
                cell_val = row_vals[col_idx - 1]
                if cell_val is None:
                    continue
                dec_val = _d(cell_val)
                if dec_val is None:
                    continue

                kpi_def, _ = KPIDefinition.objects.get_or_create(
                    organization=org,
                    slug=kpi_slug,
                    defaults={
                        'name': header_name,
                        'format': 'percent' if '%' in header_name else 'number',
                        'frequency': 'monthly',
                        'sector_template': 'saas',
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
        """Import burn rate, cash balance, and runway from Excel.

        Looks for sheets named:
        - Burn / Burn Rate / Burn & Runway / Portfolio Financials / Cash Position
        - Also checks the valuations_kpis sheet for embedded burn columns

        Supports two layouts:
        A) Pivot: company rows × period columns (Oct-24, Nov-24 ...)
           with Gross Burn, Net Burn, Cash rows detected by KPI Name column
        B) Flat: one row per (company, period) with burn/cash columns
        """
        # Find the burn/financials sheet — covers diverse naming conventions.
        # Priority: exact matches on known patterns first; fallback broad scan.
        burn_sheet = None
        burn_keywords = (
            'burn', 'cash position', 'portfolio financials', 'financials',
            'runway', 'cash flow summary', 'saas metrics', 'operating metrics',
            'cash & runway', 'company metrics', 'burn rate', 'cash runway',
        )
        # Skip cover/summary/nav/accounting-level sheets
        skip_keywords = ('cover', 'summary', 'index', 'overview', 'dashboard',
                         'nav', 'accounting', 'compliance', 'capital call',
                         'investor', 'lp ')
        for sn in wb.sheetnames:
            sl = sn.lower()
            if any(skip in sl for skip in skip_keywords):
                continue
            if any(kw in sl for kw in burn_keywords):
                burn_sheet = sn
                break

        if not burn_sheet:
            return  # No dedicated financials sheet; skip

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

            for row in rows:
                name = _find_col_str(row, 'Company Name', 'Company', 'Name')
                metric = _find_col_str(row, 'KPI Name', 'Metric', 'KPI',
                                       'Indicator', 'Financial Metric')
                if not name or not metric:
                    continue
                if _is_junk_row(name):
                    continue

                m = metric.lower()
                field = None
                if any(k in m for k in (
                    'gross burn', 'total burn', 'gross_burn', 'total outflow',
                    'total expenses', 'opex burn', 'total opex',
                    'total monthly expenses', 'cash outflow', 'operating expenses',
                )):
                    field = 'gross_burn'
                elif any(k in m for k in (
                    'net burn', 'net_burn', 'net outflow', 'net cash burn',
                    'net monthly burn', 'net burn rate', 'net cash outflow',
                    'operating cash flow', 'net operating cash',
                )):
                    field = 'net_burn'
                elif any(k in m for k in (
                    'cash in bank', 'cash balance', 'cash_balance',
                    'cash & equiv', 'available cash', 'bank balance',
                    'closing cash', 'closing balance', 'total cash',
                    'cash reserves', 'cash on hand',
                )) or (m.strip() == 'cash'):
                    field = 'cash_balance'
                elif any(k in m for k in (
                    'runway', 'months of runway', 'runway_months',
                    'cash runway', 'months runway', 'runway remaining',
                    'estimated runway', 'months left',
                )):
                    field = 'runway_months'
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

            # Guard: only extract SaaS KPIs (MRR, ARR, NRR, CAC…) from sheets
            # that actually have SaaS-specific column headers.  Generic financial
            # sheets with "Revenue" / "EBITDA" columns must NOT be treated as SaaS
            # sheets — "Revenue" falsely matches the "Monthly Revenue" MRR candidate
            # and "Net Revenue Retention" NRR candidate via Pass-5 loose matching.
            _SAAS_HEADER_SIGNALS = {
                'mrr', 'arr', 'nrr', 'cac', 'ltv', 'churn',
                'recurring', 'retention', 'acquisition',
            }
            _sheet_headers_lower = {str(h or '').lower() for h in headers_dict.keys()}
            _has_real_saas_cols = any(
                any(sig in h for sig in _SAAS_HEADER_SIGNALS)
                for h in _sheet_headers_lower
            )

            # SaaS KPI definitions — created once, reused per row.
            # Candidates are ordered: exact formats first (for Pass-1/2 match),
            # then common abbreviations, then full semantic names (Pass-3/4/5).
            # This covers diverse Excel layouts: columnar, pivot, mixed-currency,
            # INR Lakhs/Crore notation, USD, SaaS-specific terminology, etc.
            SAAS_KPI_DEFS = [
                ('arr', 'ARR', 'currency', 'saas', (
                    'ARR(Cr)*', 'ARR(Cr)', 'ARR (Cr)', 'ARR (₹Cr)', 'ARR($M)',
                    'ARR(M)', 'ARR (Mn)', 'ARR', 'Annual Recurring Revenue',
                    'Annual Run Rate', 'Annualized Revenue', 'Annual Revenue Run Rate',
                )),
                ('mrr', 'MRR', 'currency', 'saas', (
                    'MRR(Cr)*', 'MRR(Cr)', 'MRR (Cr)', 'MRR (₹Cr)', 'MRR($M)',
                    'MRR(M)', 'MRR (Mn)', 'MRR', 'Monthly Recurring Revenue',
                    'Monthly Revenue', 'Monthly Recurring Rev',
                )),
                ('nrr', 'NRR', 'percent', 'saas', (
                    'NRR%', 'NRR %', 'NRR', 'Net Revenue Retention',
                    'Net Revenue Retention Rate', 'Net Retention Rate',
                    'Net Dollar Retention', 'NDR', 'NDR%', 'NRR (Net Retention)',
                    'Net Retention', 'Revenue Retention',
                )),
                ('churn-rate', 'Churn Rate', 'percent', 'saas', (
                    'Churn%/mo', 'Churn%', 'Churn % /mo', 'Churn Rate (%)',
                    'Monthly Churn Rate', 'Monthly Churn', 'Churn Rate',
                    'Revenue Churn', 'Customer Churn', 'Churn (Monthly)',
                    'MoM Churn', 'Gross Churn', 'Net Churn',
                    'ChurnSale', 'Churn/Sale', 'Churn Sale', 'Churn % Sale',
                    'Churn%Sale', 'Monthly Churn%', 'Churn (%)','Churn(%)',
                )),
                ('cac', 'CAC', 'currency', 'saas', (
                    'CAC(Lakhs)', 'CAC (Lakhs)', 'CAC (₹L)', 'CAC($)', 'CAC (₹)',
                    'CAC', 'Customer Acquisition Cost', 'Blended CAC',
                    'Avg CAC', 'Cost of Acquisition', 'Customer Acq. Cost',
                    'CACLac', 'CAC Lac', 'CAC(Lac)', 'CAC(L)', 'CACLacs',
                    'CAC (Lac)', 'CAC(Lacs)', 'CAC (Lacs)', 'CAC (₹Lac)',
                )),
                ('ltv', 'LTV', 'currency', 'saas', (
                    'LTV', 'CLV', 'Customer LTV', 'Lifetime Value',
                    'Customer Lifetime Value', 'LTV (₹)', 'LTV(₹)',
                    'LTV(Lakhs)', 'LTV (Lakhs)', 'LTV(Cr)', 'LTV (Cr)',
                    'LTV(Lac)', 'LTV (Lac)', 'LTV($)', 'LTV (Rs)',
                    'Life Time Value', 'LifeTime Value', 'Customer Value',
                )),
                ('ltv-cac', 'LTV:CAC Ratio', 'ratio', 'saas', (
                    'LTV:CAC', 'LTV/CAC', 'LTV-CAC', 'LTV CAC',
                    'LTV:CAC Ratio', 'LTV/CAC Ratio', 'LTV to CAC',
                    'Lifetime Value to CAC', 'LTV CAC Multiple',
                )),
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
                name = _find_col_str(row, 'Company Name', 'Company', 'Name')
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
                period_date = _find_col_date(row, 'Period', 'Date', 'Month',
                                             'Reporting Month')
                # Snapshot sheets have no period column — fall back to today
                if not period_date:
                    period_date = snapshot_date

                gross = _find_col_decimal(
                    row,
                    'Gross Burn', 'Gross Burn(Cr/mo)', 'Gross Burn (Cr/mo)',
                    'Gross Burn Rate', 'Total Burn', 'Total Burn Rate',
                    'Monthly Expenses', 'Total Monthly Expenses',
                    'Cash Outflow', 'Total Cash Outflow', 'Opex Burn',
                    'Operating Expenses', 'Total Opex', 'gross_burn',
                )
                net = _find_col_decimal(
                    row,
                    'Net Burn', 'Net Burn(Cr/mo)', 'Net Burn (Cr/mo)',
                    'Net Cash Burn', 'Net Monthly Burn', 'Net Burn Rate',
                    'Net Outflow', 'Net Cash Outflow', 'Cash Burn (Net)',
                    'Net Operating Cash Flow', 'net_burn',
                )
                cash = _find_col_decimal(
                    row,
                    'Cash in Bank', 'Cash in Bank(Cr)', 'Cash Balance',
                    'Cash & Equivalents', 'Cash & Cash Equivalents',
                    'Cash on Hand', 'Available Cash', 'Bank Balance',
                    'Closing Cash', 'Closing Balance', 'Cash Reserves',
                    'Total Cash', 'cash_balance',
                )
                runway = _find_col_decimal(
                    row,
                    'Runway(mo)', 'Runway (Months)', 'Runway (mo)',
                    'Months of Runway', 'Cash Runway', 'Runway (Months Left)',
                    'Estimated Runway', 'Runway Remaining', 'Months Runway',
                    'runway_months',
                )

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

                # Extract SaaS KPIs (ARR, MRR, NRR, Churn, CAC, LTV:CAC) from
                # the same row — ONLY if the sheet has actual SaaS-specific column
                # headers.  Without this guard, generic financial columns like
                # "Revenue" falsely match "Monthly Revenue" (MRR candidate) and
                # "Net Revenue Retention" (NRR candidate) via Pass-5 substring match,
                # resulting in revenue figures being imported as MRR/ARR/NRR.
                if _has_real_saas_cols:
                    for slug, _label, _fmt, _tmpl, candidates in SAAS_KPI_DEFS:
                        val = _find_col_decimal(row, *candidates)
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

    # Map column header / row label text → BudgetVsActual.line_item key
    _PL_LINE_ITEM_KEYWORDS = {
        'revenue':              ('revenue', 'net sales', 'net revenue', 'operating revenue',
                                 'top line', 'sales revenue', 'total sales', 'gross revenue',
                                 'income from operations', 'operational revenue'),
        'other_income':         ('other income', 'non-operating income', 'non operating',
                                 'interest income', 'dividend income', 'misc income'),
        'total_revenue':        ('total revenue', 'total income', 'total topline', 'gross income',
                                 'total net revenue', 'net revenue total'),
        'cogs':                 ('cogs', 'cost of goods sold', 'cost of sales', 'cost of revenue',
                                 'direct cost', 'variable cost', 'material cost',
                                 'cost of production', 'cost of service', 'cost of goods'),
        'gross_profit':         ('gross profit', 'gross margin', 'contribution', 'gp',
                                 'revenue less cogs', 'revenue after cogs'),
        'employee_cost':        ('employee', 'payroll', 'salaries', 'staff cost', 'people cost',
                                 'compensation', 'manpower', 'hr cost', 'talent cost',
                                 'personnel cost', 'team cost', 'labour cost', 'labor cost',
                                 'wages', 'human resource'),
        'marketing_cost':       ('marketing', 'sales & marketing', 'advertising', 'promotion',
                                 'ads spend', 'brand cost', 's&m', 'sales and marketing',
                                 'customer acquisition cost', 'growth cost'),
        'rd_cost':              ('r&d', 'research', 'development cost', 'r & d', 'tech cost',
                                 'product cost', 'engineering cost', 'innovation cost',
                                 'research and development', 'rd expense'),
        'g_and_a':              ('g&a', 'general admin', 'overhead', 'corporate cost', 'g & a',
                                 'admin cost', 'administrative', 'general and admin',
                                 'corporate overhead', 'office cost'),
        'total_opex':           ('total opex', 'opex', 'op ex', 'total operating expense', 'total cost',
                                 'total expenditure', 'total expenses', 'operating expenses',
                                 'total operating cost', 'all opex', 'total overheads'),
        'ebitda':               ('ebitda', 'ebidta', 'ebit da', 'operating profit before dep',
                                 'earnings before interest tax dep', 'operating cash profit',
                                 'ebitda margin', 'ebidta margin'),
        'depreciation':         ('depreciation', 'amortisation', 'amortization', 'd&a', ' da ',
                                 'dep.', 'depr.', 'depreciation and amortization',
                                 'dep & amort', 'da charge'),
        'ebit':                 ('ebit', 'earnings before interest and tax', 'operating income',
                                 'operating profit', 'income from operations after dep'),
        'finance_cost':         ('finance cost', 'interest expense', 'interest cost',
                                 'borrowing cost', 'financial charges', 'interest paid',
                                 'finance charges', 'interest on loan', 'bank interest'),
        'pbt':                  ('pbt', 'profit before tax', 'earnings before tax',
                                 'pre-tax profit', 'profit before income tax', 'income before tax',
                                 'earnings before taxes', 'pre tax income'),
        'tax':                  ('income tax', 'tax expense', 'current tax', 'deferred tax',
                                 'tax provision', 'taxes', 'total tax', 'tax charge'),
        'pat':                  ('pat', 'profit after tax', 'net profit', 'net income',
                                 'net earnings', 'bottom line', 'profit for the year',
                                 'net loss', 'after tax profit', 'profit for period',
                                 'net profit after tax', 'profit attributable'),
        'total_assets':         ('total assets', 'balance sheet total', 'total asset',
                                 'gross assets', 'total resources'),
        'total_debt':           ('total debt', 'borrowings', 'total loans', 'bank borrowings',
                                 'long term debt', 'debt outstanding', 'total liabilities',
                                 'short term borrowing', 'debt'),
        'cash_and_equivalents': ('cash', 'bank balance', 'cash equivalents', 'liquid assets',
                                  'cash in bank', 'closing cash', 'cash and bank',
                                  'cash & equivalents', 'cash and cash equivalents',
                                  'cash reserves', 'available cash'),
        'net_worth':            ('net worth', 'shareholders equity', 'shareholders funds',
                                 'networth', 'total equity', 'owners equity',
                                 'capital and reserves', 'equity capital', 'book value'),
        'capex':               ('capex', 'capital expenditure', 'capital expense',
                                 'cap ex', 'capex additions', 'capital investment',
                                 'property plant equipment', 'ppe addition',
                                 'purchase of assets', 'fixed asset addition'),
        'working_capital':     ('working capital', 'net working capital', 'nwc',
                                 'current assets minus current liabilities',
                                 'trade working capital'),
        'net_working_capital': ('net wc', 'net working cap'),
        'dividend':            ('dividend', 'dividends paid', 'dividend paid',
                                 'equity dividend', 'dividend payout'),
        'other_cost':          ('other cost', 'other expense', 'miscellaneous expense',
                                 'other charges', 'other expenditure', 'misc cost',
                                 'sundry expense', 'other operating cost'),
    }

    @staticmethod
    def _pl_map_to_line_item(text):
        """Map a column header or row label text to a BudgetVsActual.line_item key."""
        if not text:
            return None
        t = str(text).lower().strip()
        for li_key, keywords in FundImportService._PL_LINE_ITEM_KEYWORDS.items():
            if any(kw in t for kw in keywords):
                return li_key
        return None

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

        Reads financial statement sheets (P&L, Balance Sheet, Cash Flow, BvA) and
        writes records to BudgetVsActual (one row per company × period × line_item).
        After import, triggers MISAggregator to populate ConsolidatedMIS and runs
        AnomalyDetector for each company.

        Handles three Excel layouts:
        A) Horizontal: rows = (company, period), cols = P&L line items
        B) Vertical pivot: rows = line items, cols = time periods (per-company section)
        C) Budget vs Actual explicit: rows = (company, line_item), cols = Budget | Actual
        """
        from mis_consolidation.services import BvAImporter, MISAggregator, AnomalyDetector

        PL_KEYWORDS = ('p&l', 'profit', 'income statement', 'pnl', 'p & l',
                       'financial statement', 'monthly financials', 'pl summary',
                       'portfolio p&l', 'mis report', 'revenue statement',
                       'profit loss', 'company financials report', 'financials report',
                       'monthly p', 'pl tab', 'pnl tab', 'p and l')
        BVA_KEYWORDS = ('budget vs actual', 'budget v actual', 'bva', 'b vs a',
                        'actual vs budget', 'budget actual', 'variance analysis',
                        'budget analysis', 'bv actual', 'budgeted vs actual')
        BS_KEYWORDS = ('balance sheet', 'bs statement', 'financial position',
                       'net worth statement', 'bs tab', 'balance sh')
        CF_KEYWORDS = ('cash flow', 'cash statement', 'cf statement', 'cash flow stmt')

        SKIP_KEYWORDS = ('cover', 'summary', 'index', 'overview', 'dashboard',
                         'nav record', 'capital call', 'investor', 'lp tab',
                         'compliance', 'valuation', 'burn rate', 'burn&', 'exits',
                         'distribution', 'kpi', 'saas metrics', 'portfolio companies',
                         'fund master', 'scheme master', 'capital accounts')

        pl_sheets = []
        bva_sheets = []

        for sn in wb.sheetnames:
            sl = sn.lower()
            if any(skip in sl for skip in SKIP_KEYWORDS):
                continue
            if any(kw in sl for kw in BVA_KEYWORDS):
                bva_sheets.append(sn)
            elif any(kw in sl for kw in PL_KEYWORDS + BS_KEYWORDS + CF_KEYWORDS):
                pl_sheets.append(sn)

        # Pre-compute period labels from a NAV/accounting sheet if available.
        # Fund P&L sheets sometimes have value columns with no period headers —
        # we infer the periods from NAV & Accounting which has the same column structure.
        inferred_periods = self._infer_period_labels_from_wb(wb)

        count = 0
        for sn in pl_sheets:
            try:
                count += self._process_pl_sheet(wb[sn], companies, fund,
                                                inferred_periods=inferred_periods)
            except Exception as e:
                logger.warning(f'P&L sheet {sn!r} import error: {e}')
            # Also run BvA extraction on P&L sheets — catches sheets that are P&L-named
            # but carry explicit Budget | Actual columns (Company | Line Item | Budget | Actual).
            # _process_bva_sheet safely returns 0 if it finds no valid structure.
            try:
                count += self._process_bva_sheet(wb[sn], companies, fund)
            except Exception as e:
                logger.warning(f'BvA scan of P&L sheet {sn!r} error: {e}')

        for sn in bva_sheets:
            try:
                count += self._process_bva_sheet(wb[sn], companies, fund)
            except Exception as e:
                logger.warning(f'BvA sheet {sn!r} import error: {e}')

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

    def _infer_period_labels_from_wb(self, wb):
        """
        Scan the workbook for a sheet that has explicit month-period column headers
        (e.g. 'Oct-24', 'Nov-24', …) and return an ordered list of those labels.

        Used to annotate fund-level P&L sheets that carry value columns but no headers.
        The NAV & Accounting sheet is the canonical source in most AIF fund reports.
        """
        _period_re = re.compile(
            r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/]\d{2,4}$',
            re.IGNORECASE)
        # Prefer sheets whose name suggests accounting/NAV history
        preferred = [sn for sn in wb.sheetnames
                     if 'nav' in sn.lower() and 'accounting' in sn.lower()]
        other = [sn for sn in wb.sheetnames if sn not in preferred]
        for sn in preferred + other:
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

        # Qualifiers that distinguish budget vs actual columns
        _BUD_QUAL = ('budget', 'aop', 'plan', 'target', 'budgeted', 'planned', 'forecast')
        _ACT_QUAL = ('actual', 'actuals', 'real', 'achieved', 'reported')

        PERIOD_RE = re.compile(
            r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/]\d{2,4}$'
            r'|^Q[1-4][\s\-]?FY\d{2,4}$'
            r'|^\d{4}[-/]\d{1,2}$'
            r'|^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s]\d{4}$',
            re.IGNORECASE,
        )
        # Matches "Budget Apr-24", "Apr-24 Actual", "AOP May 2024", etc.
        QUALIFIED_PERIOD_RE = re.compile(
            r'^(?:budget|aop|plan|target|actual|actuals|real|achieved|budgeted)[\s_\-]+'
            r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/\s]\d{2,4}'
            r'|Q[1-4][\s\-]?FY\d{2,4}|\d{4}[-/]\d{1,2})'
            r'|^((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/\s]\d{2,4}'
            r'|Q[1-4][\s\-]?FY\d{2,4}|\d{4}[-/]\d{1,2})'
            r'[\s_\-]+(?:budget|aop|plan|target|actual|actuals|real|achieved|budgeted)$',
            re.IGNORECASE,
        )

        headers_dict, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
        if not rows:
            return 0

        period_cols = [h for h in headers_dict.keys()
                       if h and PERIOD_RE.match(str(h).strip())]

        # Detect "Budget Apr-24" / "Apr-24 Actual" style period columns (Layout B2)
        budget_period_cols = {}   # header_text → (yr, mo)
        actual_period_cols = {}   # header_text → (yr, mo)
        for h in headers_dict.keys():
            if not h or h in period_cols:
                continue
            h_str = str(h).strip()
            m = QUALIFIED_PERIOD_RE.match(h_str)
            if m:
                period_part = (m.group(1) or m.group(2) or '').strip()
                yr_q, mo_q = self._pl_parse_year_month(period_part)
                if yr_q:
                    h_lower = h_str.lower()
                    if any(q in h_lower for q in _BUD_QUAL):
                        budget_period_cols[h] = (yr_q, mo_q)
                    else:
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

                for col_name, col_val in nr.items():
                    if not col_name:
                        continue
                    col_lower = str(col_name).lower().strip()

                    # Skip percentage/ratio columns — they carry a rate, not an amount.
                    # e.g. 'EBITDA%', 'GP%', 'Var%', 'Gross Margin %'
                    if col_lower.endswith('%') or col_lower.endswith('margin') or col_lower.endswith('ratio'):
                        continue

                    # Skip computed variance/difference columns
                    if any(v in col_lower for v in ('variance', 'var %', '% var', 'difference', ' diff')):
                        continue

                    # Detect budget/actual qualifier in the (already normalised) column name
                    is_budget_col = any(q in col_lower for q in _BUD_QUAL)
                    is_actual_col = any(q in col_lower for q in _ACT_QUAL)

                    # Strip qualifier to isolate the line-item name for mapping
                    li_key = None
                    if is_budget_col or is_actual_col:
                        stripped = col_lower
                        for q in _BUD_QUAL + _ACT_QUAL:
                            stripped = stripped.replace(q, '').strip(' _-()')
                        li_key = self._pl_map_to_line_item(stripped)
                    if not li_key:
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
        _ws_title = getattr(ws, 'title', '').lower()
        _NON_PL_TERMS = ('balance sheet', 'bs statement', 'financial position',
                         'net worth', 'cash flow', 'cash statement', 'cf statement',
                         'fund_bs', 'fund_cf', ' bs', ' cf')
        if any(t in _ws_title for t in _NON_PL_TERMS):
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
            # Skip percentage/margin/ratio rows — these are derived metrics,
            # not absolute currency values (e.g. "EBITDA Margin", "Gross Margin %")
            label_l = label.lower()
            if (label_l.endswith('%') or label_l.endswith('margin')
                    or label_l.endswith('ratio') or label_l.endswith('margin %')
                    or '% of' in label_l or 'per unit' in label_l):
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

            # After normalisation 'Budget(₹Cr)' → 'Budget', 'Actual(₹Cr)' → 'Actual', etc.
            # _find_col() now matches via Pass-2 exact case-insensitive regardless of
            # the original unit suffix or bracket style used by the fund manager.
            budget = _find_col_decimal(
                nr, 'Budget', 'Budget YTD', 'Annual Budget',
                'Plan', 'AOP', 'Annual Operating Plan', 'Budgeted', 'Target',
                'Budget Amount', 'Planned', 'Budget Value')
            actual = _find_col_decimal(
                nr, 'Actual', 'Actual YTD', 'YTD Actual',
                'Actuals', 'Real', 'Achieved', 'Actual Amount', 'Reported',
                'Actual Value')

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

        # Qualifier patterns (order matters — check budget first)
        _BUD_Q = ('bdgt', 'budget', 'aop', 'plan', 'target', 'planned', 'forecast')
        _ACT_Q = ('actual', 'actuals', 'real', 'achieved', 'reported')
        _Q_RE = re.compile(r'^(Q[1-4])\s*[-\s]?\s*(bdgt|budget|aop|plan|target|actual|actuals)',
                           re.IGNORECASE)
        _FY_Q_RE = re.compile(r'^(FY|annual|full.?year)\s*(budget|bdgt|aop|plan|actual|actuals)',
                              re.IGNORECASE)

        # Map column headers → (period_quarter, period_type, is_budget)
        period_budget_cols = {}   # col_header → (quarter, is_budget)
        for h in headers_dict:
            if not h:
                continue
            h_s = str(h).strip()
            h_l = h_s.lower()
            m = _Q_RE.match(h_s)
            if m:
                qtr = m.group(1).upper()
                qualifier = m.group(2).lower()
                is_bud = any(q in qualifier for q in _BUD_Q)
                period_budget_cols[h] = (qtr, 'quarterly', is_bud)
                continue
            m2 = _FY_Q_RE.match(h_s)
            if m2:
                qualifier = h_l
                is_bud = any(q in qualifier for q in _BUD_Q)
                period_budget_cols[h] = ('FY', 'annual', is_bud)
                continue
            # Plain "Budget" / "Actual" columns without period prefix
            if any(h_l == q for q in _BUD_Q):
                period_budget_cols[h] = ('FY', 'annual', True)
            elif any(h_l == q for q in _ACT_Q):
                period_budget_cols[h] = ('FY', 'annual', False)

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
        # by keyword and store as ConsolidatedMIS records with special line_item keys
        # ('net_irr', 'tvpi', 'portfolio_fv') so the API can expose them separately.
        # Values are stored in total_actual_inr (IRR: as percentage, e.g. 21.2 for 21.2%).
        _FUND_METRIC_KEYS = {
            'net_irr': ('net irr', 'irr net', 'net return', 'lp irr',
                        'net irr%', 'net internal rate', 'fund irr'),
            'tvpi':    ('tvpi', 'total value to paid', 'tv/pi', 'tv pi'),
            'portfolio_fv': ('portfolio fv', 'portfolio fair value', 'portfolio value',
                              'fund nav', 'total portfolio fv', 'total fv'),
        }

        count = 0
        from django.utils import timezone as _tz
        for row in rows:
            nr = _norm_row(row)
            metric = _find_col_str(nr, 'Metric', 'Line Item', 'Particulars',
                                   'Item', 'Category', 'Description')
            if not metric or _is_junk_row(metric):
                continue

            metric_l = metric.lower().strip()

            # Check if this is a special fund-level performance metric row
            special_li_key = None
            for li_key_candidate, patterns in _FUND_METRIC_KEYS.items():
                if any(p in metric_l for p in patterns):
                    special_li_key = li_key_candidate
                    break

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

        Signals for quoted (publicly listed):
        - Share Type contains 'Listed' (e.g. 'Equity (Listed)')
        - IPEV Level == 'Level 1' (mark-to-market, publicly observable price)

        Updates PortfolioCompany.is_quoted and listing_exchange (if inferable).
        """
        # Find the dedicated sheet — covers diverse naming conventions
        target_sheet = None
        keywords = (
            'quoted', 'unquoted', 'ipev', 'shareholding',
            'listed shares', 'share classification', 'share type',
            'equity classification', 'listing status',
        )
        for sn in wb.sheetnames:
            sl = sn.lower()
            # Skip cover/summary/index sheets
            if any(skip in sl for skip in ('cover', 'summary', 'index', 'overview', 'dashboard')):
                continue
            if any(kw in sl for kw in keywords):
                target_sheet = sn
                break

        if not target_sheet:
            # Fallback: scan all non-cover sheets for IPEV Level column presence
            for sn in wb.sheetnames:
                sl = sn.lower()
                if any(skip in sl for skip in ('cover', 'summary', 'index', 'overview', 'dashboard')):
                    continue
                ws_tmp = wb[sn]
                for r in range(1, min(ws_tmp.max_row + 1, 10)):
                    for c in range(1, min((ws_tmp.max_column or 0) + 1, 20)):
                        val = ws_tmp.cell(r, c).value
                        if val and 'ipev' in str(val).lower():
                            target_sheet = sn
                            break
                    if target_sheet:
                        break
                if target_sheet:
                    break

        if not target_sheet:
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

            # Determine quoted status.
            # Guard: 'listed' must NOT be preceded by 'un' so that
            # 'Equity (Unlisted)' is NOT treated as quoted.
            share_lower = share_type.lower()
            ipev_lower = ipev_raw.strip().lower()
            is_quoted = (
                '(listed)' in share_lower or       # "Equity (Listed)"
                share_lower == 'listed' or          # bare "Listed"
                share_lower.startswith('listed ') or
                (share_lower.endswith(' listed') and 'unlisted' not in share_lower) or
                'quoted' in share_lower or
                ipev_lower == 'level 1'             # exact IPEV Level 1
            )

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
        """Import NAV records from NAV/Accounting sheet.

        Handles three formats:
        - Format A: Flat table with one row per period (Period | NAV | Units | ...)
        - Format B: Multi-section sheet with NAV RECORDS header
        - Format C: Transposed table where rows = metrics, columns = periods
          (Component | Oct-24 | Nov-24 | Dec-24 | ...)
          Common in fund reporting Excel files.
        """
        # Check multiple possible sheet names.
        # Prefer sheets that combine nav + accounting (time-series history) over
        # pure-calculation sheets like "NAV Calculation" which hold a single static value.
        sheet_name = domain_map.get('nav_accounting')
        if not sheet_name:
            # First pass: require both 'nav' and 'accounting' in the name
            for sn in wb.sheetnames:
                sl = sn.lower()
                if 'nav' in sl and 'accounting' in sl:
                    sheet_name = sn
                    break
        if not sheet_name:
            # Second pass: any nav/accounting sheet that isn't a pure calculation table
            for sn in wb.sheetnames:
                sl = sn.lower()
                if ('nav' in sl or 'accounting' in sl) and 'calculat' not in sl:
                    sheet_name = sn
                    break
        if not sheet_name or sheet_name not in wb.sheetnames:
            return

        ws = wb[sheet_name]
        default_scheme = list(schemes.values())[0] if schemes else None
        if not default_scheme:
            return

        # Detect period-column headers (e.g. "Oct-24", "Nov-24")
        _period_re = re.compile(
            r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/]\d{2,4}$',
            re.IGNORECASE)
        headers_dict, table_rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
        period_cols = [h for h in headers_dict.keys() if _period_re.match(h.strip())]

        if period_cols and table_rows:
            # ── Format C: Transposed (Component | Oct-24 | Nov-24 | …) ──────────
            # Build {period_col: {metric_key: value}} by scanning component rows
            period_data = {p: {} for p in period_cols}
            for row in table_rows:
                comp = _find_col_str(row, 'Component', 'Line Item', 'Item', 'Metric')
                if not comp:
                    continue
                comp_l = comp.lower()
                for pcol in period_cols:
                    val = _d(row.get(pcol))
                    if val is None:
                        continue
                    if 'closing' in comp_l and 'nav' in comp_l:
                        period_data[pcol]['total_nav'] = val
                    elif 'unreali' in comp_l:
                        period_data[pcol]['unrealized'] = val
                    elif 'realised' in comp_l or 'realized' in comp_l:
                        period_data[pcol]['realized'] = val
                    elif 'management fee' in comp_l or 'mgmt fee' in comp_l:
                        period_data[pcol]['mgmt_fee'] = abs(val)
                    elif 'carry' in comp_l:
                        period_data[pcol]['carry'] = val
                    elif 'investment income' in comp_l:
                        period_data[pcol]['income'] = val

            count = 0
            for pcol in period_cols:
                if 'total' in pcol.lower():
                    continue  # Skip "H2 Total", "FY Total" etc.
                pd = period_data.get(pcol, {})
                nav_date = self._parse_period(pcol)
                if not nav_date:
                    continue
                total_nav = pd.get('total_nav')
                if not total_nav:
                    continue
                # Last day of the month
                import calendar as _cal
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
            logger.info(f'  NAV Records (transposed format): {count}')

            # After creating NAV records, enrich with NAV/Unit and realized gains.
            # NAV/Unit comes from the NAV Calculation sheet (closing value row).
            # Realized gains come from summing ExitEvent.realized_gain_loss for this fund.
            self._enrich_nav_records_post_import(wb, default_scheme)
            return

        # ── Format B: Multi-section ──────────────────────────────────────────
        sections = read_all_sections_from_sheet(ws, alias_map=self._get_alias(ws))
        nav_rows = None
        for sec_name, (sec_headers, sec_rows) in sections.items():
            if 'NAV' in sec_name.upper() or sec_name == '__default__':
                nav_rows = sec_rows
                break

        if not nav_rows:
            nav_rows = table_rows  # fallback to already-read flat table

        if not nav_rows:
            return

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

        logger.info(f'  NAV Records: {count}')

    def _enrich_nav_records_post_import(self, wb, scheme):
        """
        Enrich NAV records after transposed-format import.

        1. NAV/Unit: scan the NAV Calculation sheet for a "Closing NAV/Unit" row
           and apply the latest value to the most recent NAV record.  For earlier
           periods the per-unit is estimated from total_nav / units (if units known).

        2. Realized Gains: sum ExitEvent.realized_gain_loss for this fund and apply
           the total to the most recent NAV record.
        """
        from django.db.models import Sum

        # ── 1. NAV/Unit from NAV Calculation sheet ───────────────────────────
        closing_nav_per_unit = None
        opening_nav_per_unit = None
        total_units = None

        for sn in wb.sheetnames:
            sl = sn.lower()
            if 'nav' in sl and ('calc' in sl or 'calculation' in sl):
                calc_ws = wb[sn]
                max_r = min(calc_ws.max_row + 1, 80)
                for rr in range(1, max_r):
                    label = calc_ws.cell(rr, 1).value
                    if not label:
                        continue
                    label_l = str(label).lower()
                    val = calc_ws.cell(rr, 2).value
                    dval = _d(val)
                    if dval is None:
                        continue
                    if 'closing nav/unit' in label_l or ('closing' in label_l and 'nav' in label_l and 'unit' in label_l):
                        closing_nav_per_unit = dval
                    elif 'opening nav/unit' in label_l or ('opening' in label_l and 'nav' in label_l and 'unit' in label_l):
                        opening_nav_per_unit = dval
                    elif 'unit' in label_l and ('outstanding' in label_l or 'issued' in label_l):
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

        Handles two formats:
        1. Multi-section sheet: EXIT EVENTS section + DISTRIBUTIONS section
           on the same sheet (Format B)
        2. Flat table with one exit per row (Format A)
        """
        sheet_name = domain_map.get('exits_distributions')
        if not sheet_name:
            for sn in wb.sheetnames:
                if 'exit' in sn.lower() or 'distribution' in sn.lower():
                    sheet_name = sn
                    break
        if not sheet_name or sheet_name not in wb.sheetnames:
            return

        ws = wb[sheet_name]

        # Try multi-section approach
        sections = read_all_sections_from_sheet(ws, alias_map=self._get_alias(ws))
        exit_rows = None
        dist_rows = None

        for sec_name, (sec_headers, sec_rows) in sections.items():
            sec_upper = sec_name.upper()
            if 'EXIT' in sec_upper and 'DISTRIBUTION' not in sec_upper:
                exit_rows = sec_rows
            elif 'DISTRIBUTION' in sec_upper and 'EXIT' not in sec_upper:
                dist_rows = sec_rows

        # If we found separate sections, process them
        if exit_rows is not None:
            exit_count = self._process_exit_rows(exit_rows, investments)
            logger.info(f'  Exits (structured): {exit_count}')

            # Process distributions section if present
            if dist_rows:
                default_scheme = list(schemes.values())[0] if schemes else None
                if default_scheme:
                    self._process_distribution_rows(dist_rows, schemes,
                                                     investments)
            return

        # Flat table fallback — the whole sheet is one table
        _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))
        if not rows:
            # Also try __default__ section
            if '__default__' in sections:
                _, rows = sections['__default__']

        exit_count = self._process_exit_rows(rows, investments)
        logger.info(f'  Exits: {exit_count}')

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
                # Step 3: create skeleton PortfolioCompany + Investment for this exit
                sector = _find_col_str(row, 'Sector', 'Industry', 'Segment', default='Other')
                exit_date_tmp = _find_col_date(row, 'Exit Date', 'Date', 'Realization Date')
                cost_tmp = _find_col_decimal(
                    row, 'Cost(Cr)', 'Cost(₹Cr)', 'Cost (Cr)', 'Cost Basis', 'Invested')
                co, _ = PortfolioCompany.objects.update_or_create(
                    organization=self.org, name=name,
                    defaults={'sector': sector} if sector and sector != 'Other' else {},
                )
                inv, _ = Investment.objects.update_or_create(
                    portfolio_company=co,
                    scheme=default_scheme,
                    defaults={
                        'company_name': name,
                        'investment_date': exit_date_tmp,
                        'total_invested': abs(cost_tmp) if cost_tmp else None,
                        'instrument_type': 'equity',
                    },
                )

            if not inv:
                logger.warning(f'  Exit row skipped — could not resolve company: {name!r}')
                continue

            exit_date = _find_col_date(
                row, 'Exit Date', 'Date', 'Realization Date')
            exit_route = _find_col_str(
                row, 'Exit Route', 'Exit Type', 'Type', 'Route',
                'Exit Method', 'exit_type')
            cost = _find_col_decimal(
                row, 'Cost(Cr)', 'Cost(₹Cr)', 'Cost (Cr)',
                'Cost Basis', 'Invested', 'cost_basis')
            realized = _find_col_decimal(
                row, 'Proceeds(Cr)', 'Proceeds (Cr)', 'Realized(₹Cr)',
                'Realized', 'Proceeds', 'Exit Proceeds', 'Realization',
                'Gross Proceeds (Cr)', 'Gross Proceeds',
                'Net Proceeds (Cr)', 'Net Proceeds', 'proceeds')
            moic = _find_col_decimal(row, 'MOIC', 'Multiple', 'moic')
            irr_raw = _find_col_decimal(
                row, 'Gross IRR', 'IRR', 'IRR%', 'Gross IRR%', 'irr_pct')
            # IRR stored as decimal fraction (0.355 = 35.5%) → convert to %
            irr = (irr_raw * 100) if (irr_raw is not None and irr_raw < 2) else irr_raw
            net_irr_raw = _find_col_decimal(
                row, 'Net IRR', 'Net IRR%', 'Net Return', 'net_irr_pct')
            net_irr = (net_irr_raw * 100) if (net_irr_raw is not None and net_irr_raw < 2) else net_irr_raw
            is_actual_raw = _find_col_str(row, 'Is Actual', default='Yes')

            # Extended exit type map — covers IPO variants (IPO – BSE, IPO – NSE)
            # and management buyout (MBO/Mgmt Buyout)
            exit_route_l = exit_route.lower().strip()
            if exit_route_l.startswith('ipo'):
                exit_type = 'ipo'
            elif exit_route_l in ('trade sale', 'merger & acquisition', 'm&a', 'acquisition'):
                exit_type = 'merger_acquisition'
            elif 'buyout' in exit_route_l or exit_route_l == 'buyback':
                exit_type = 'buyback'
            elif 'secondary' in exit_route_l or exit_route_l == 'secondaries':
                exit_type = 'secondary_sale'
            elif 'write' in exit_route_l:
                exit_type = 'write_off'
            else:
                exit_type = 'secondary_sale'

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

            type_map = {
                'stcg': 'return_of_capital', 'ltcg': 'return_of_capital',
                'return of capital': 'return_of_capital',
                'capital + income': 'return_of_capital',
                'capital and income': 'return_of_capital',
                'income distribution': 'income_distribution',
                'profit': 'profit_distribution',
            }
            dist_type_key = dist_type_raw.lower().strip()
            dist_type = next((v for k, v in type_map.items() if k in dist_type_key),
                             'return_of_capital')

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
        sheet_name = domain_map.get('investors_aml')
        if not sheet_name or sheet_name not in wb.sheetnames:
            return

        ws = wb[sheet_name]
        _, rows = read_table_from_sheet(ws, alias_map=self._get_alias(ws))

        # Check if this sheet actually has distribution columns
        # (Format B investor sheets don't have distribution amounts)
        has_dist_col = False
        if rows:
            sample = rows[0]
            for key in sample.keys():
                kl = key.lower()
                if any(kw in kl for kw in ['distribution', 'returned', 'payout']):
                    has_dist_col = True
                    break
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

    # Fuzzy entity type matching: various ways users might name entity types
    _ENTITY_TYPE_ALIASES = {
        'manager': ['manager', 'investment manager', 'fund manager', 'im',
                     'asset manager', 'management company'],
        'trustee': ['trustee', 'trust company', 'trustee company'],
        'sponsor': ['sponsor', 'gp', 'general partner', 'promoter'],
        'custodian': ['custodian', 'custody', 'depository participant',
                      'dp', 'fund custodian'],
        'statutory_auditor': ['statutory_auditor', 'auditor', 'statutory auditor',
                              'audit firm', 'chartered accountant', 'ca firm'],
        'legal_counsel': ['legal_counsel', 'legal counsel', 'legal advisor',
                          'law firm', 'legal', 'advocate'],
        'registrar': ['registrar', 'rta', 'registrar & transfer agent',
                      'registrar and transfer agent', 'transfer agent'],
        'valuer': ['valuer', 'registered valuer', 'valuation firm',
                   'independent valuer', 'valuator'],
    }

    def _normalize_entity_type(self, raw_type):
        """Normalize entity type string to a valid model choice using fuzzy matching."""
        if not raw_type:
            return None
        raw = raw_type.strip().lower().replace('-', '_')
        # Direct match
        valid_types = [c[0] for c in Entity.ENTITY_TYPE_CHOICES]
        if raw in valid_types:
            return raw
        # Fuzzy match via aliases
        for etype, aliases in self._ENTITY_TYPE_ALIASES.items():
            for alias in aliases:
                if alias in raw or raw in alias:
                    return etype
        return None

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

        # Strategy 1: Read from organization_users domain (Format B)
        sheet_name = domain_map.get('organization_users')
        if not sheet_name:
            # Keyword fallback: look for sheets with 'organization' or 'users' or 'entity'
            for sn in wb.sheetnames:
                low = sn.lower()
                if any(kw in low for kw in ['organization', 'entities', 'entity master']):
                    sheet_name = sn
                    break

        if sheet_name and sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sections = read_all_sections_from_sheet(ws, alias_map=self._get_alias(ws))

            # Find entity section
            entity_rows = None
            for sec_name, (sec_headers, sec_rows) in sections.items():
                if any(kw in sec_name.upper() for kw in ['ENTITY', 'ENTITIES']):
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
            fsm_sheet = domain_map.get('fund_scheme_master')
            if not fsm_sheet:
                for sn in wb.sheetnames:
                    if 'fund' in sn.lower() and ('scheme' in sn.lower() or 'master' in sn.lower()):
                        fsm_sheet = sn
                        break

            if fsm_sheet and fsm_sheet in wb.sheetnames:
                ws = wb[fsm_sheet]
                # Scan for entity references as key-value pairs
                for r in range(1, ws.max_row + 1):
                    label = _str(ws.cell(r, 1).value).strip()
                    value = _str(ws.cell(r, 2).value).strip()
                    if not label or not value:
                        continue
                    label_lower = label.lower()
                    # Check if this row is an entity reference
                    for etype, aliases in self._ENTITY_TYPE_ALIASES.items():
                        if any(alias in label_lower for alias in aliases):
                            if etype not in entity_map and len(value) > 2:
                                entity, _ = Entity.objects.get_or_create(
                                    organization=org,
                                    entity_type=etype,
                                    entity_name=value,
                                )
                                entity_map[etype] = entity
                                count += 1
                            break

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
        # Look for a fees sheet first (more structured, more accurate than NAV)
        fees_sheet = None
        for sn in wb.sheetnames:
            sl = sn.lower()
            if 'fee' in sl and 'register' in sl:
                fees_sheet = sn
                break
        if not fees_sheet:
            for sn in wb.sheetnames:
                sl = sn.lower()
                if sl.startswith('fee') or sl == 'fees' or 'fee schedule' in sl:
                    fees_sheet = sn
                    break

        if fees_sheet and fees_sheet in wb.sheetnames:
            ws_f = wb[fees_sheet]
            _quarter_re = re.compile(r'^Q[1-4]\s*FY\s*\d{2,4}$', re.IGNORECASE)
            headers_dict, fee_rows = read_table_from_sheet(
                ws_f, alias_map=self._get_alias(ws_f))
            quarter_cols = [h for h in headers_dict.keys()
                            if _quarter_re.match(h.strip())]

            if quarter_cols and fee_rows:
                # Find the Management Fee row and GST row
                mgmt_fee_vals = {}
                gst_vals = {}
                for row in fee_rows:
                    comp = _find_col_str(row, 'Component', 'Expense Category', 'Item')
                    comp_l = comp.lower()
                    for qcol in quarter_cols:
                        val = _d(row.get(qcol))
                        if val is None:
                            continue
                        if 'management fee' in comp_l and 'gst' not in comp_l:
                            if val > 0:
                                mgmt_fee_vals[qcol] = val
                        elif 'gst' in comp_l and 'management' in comp_l:
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
        sheet_name = domain_map.get('nav_accounting')
        if not sheet_name:
            for sn in wb.sheetnames:
                if 'nav' in sn.lower() or 'accounting' in sn.lower():
                    sheet_name = sn
                    break
        if not sheet_name or sheet_name not in wb.sheetnames:
            return

        ws = wb[sheet_name]
        sections = read_all_sections_from_sheet(ws, alias_map=self._get_alias(ws))
        rows = None
        for sec_name, (sec_headers, sec_rows) in sections.items():
            if 'NAV' in sec_name.upper() or sec_name == '__default__':
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

    def _compute_carried_interest(self, schemes):
        """Compute carried interest from exits and scheme waterfall config."""
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

            CarriedInterest.objects.get_or_create(
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

    # ------------------------------------------------------------------
    # Portfolio hierarchy builder
    # ------------------------------------------------------------------

    def _build_hierarchy(self, wb, org, fund, schemes, companies,
                         investments, domain_map, filepath):
        """Build PortfolioNode hierarchy from Portfolio Hierarchy sheet."""
        sheet_name = domain_map.get('portfolio_hierarchy')
        if not sheet_name:
            for sn in wb.sheetnames:
                if 'hierarchy' in sn.lower():
                    sheet_name = sn
                    break
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

        # Monthly P&L
        for sn in wb.sheetnames:
            if 'p&l' in sn.lower() or 'profit' in sn.lower():
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

        # Budget vs Actual
        for sn in wb.sheetnames:
            if 'budget' in sn.lower():
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

    # Words that appear in fund metadata fields but NEVER in real company names.
    _METADATA_LABEL_WORDS = {
        'short code', 'type', 'sebi category', 'vintage year', 'reporting currency',
        'fund corpus', 'stage focus', 'management fee', 'hurdle rate',
        'carried interest', 'domicile', 'date format', 'currency display',
        'fund name', 'corpus', 'inception', 'carry', 'hurdle',
        'vintage', 'aum', 'drawdown', 'deployment',
    }

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
        for inv in investments:
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

        This catches the class of bug where a Cover/summary sheet row like
        "Short Code | SCIV-VIII" gets imported as a PortfolioCompany named
        "Short Code". If any investment company_name exactly matches a known
        metadata label we log a CRITICAL warning and append to self.errors.
        """
        from investments.models import Investment
        invs = Investment.objects.filter(scheme__fund=fund).values_list('company_name', flat=True)
        suspect = []
        for name in invs:
            if name and name.lower().strip() in self._METADATA_LABEL_WORDS:
                suspect.append(name)

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
