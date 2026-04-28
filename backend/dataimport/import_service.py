"""
FundImportService — orchestrates the import of a single fund Excel file.

Uses Gemini column mapping for semantic field resolution, then imports
data into all Django models using the mapped fields.

Falls back to the existing positional import (from import_fund_excel.py)
if Gemini mapping is unavailable or low-confidence.
"""

import logging
import os
from datetime import date
from decimal import Decimal, InvalidOperation

import openpyxl
from django.db import transaction
from django.utils.text import slugify

from accounts.models import Organization, User, FundAccess
from funds.models import FundCategory, Entity, Fund, Scheme
from lp.models import (BankAccount, Investor, Commitment, CapitalCall,
                        CapitalCallLineItem, Distribution, DistributionLineItem)
from investments.models import (PortfolioCompany, Investment, InvestmentTranche,
                                 Valuation, KPIDefinition, PortfolioKPI,
                                 ExitEvent, BoardMeeting)
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
# Utility helpers (same as import_fund_excel.py)
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
}


def _d(val, default=None):
    if val is None or val == '' or val == 'None':
        return default
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


def _is_section_header(val):
    if not val:
        return False
    s = str(val).strip()
    for header in _SECTION_HEADERS:
        if header in s.upper():
            return True
    return s.isupper() and len(s) > 15 and ' ' in s


def find_section_rows(ws, section_name):
    for r in range(1, ws.max_row + 1):
        val = ws.cell(r, 1).value
        if val and section_name in str(val):
            return r
    return None


def read_table(ws, start_row=1, max_rows=None):
    """Read rows from a worksheet starting at a header row."""
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


# ---------------------------------------------------------------------------
# Mapped row reader — uses Gemini column mapping
# ---------------------------------------------------------------------------

def _build_header_index(ws, header_row):
    """Build a {header_text: column_index} dict from a worksheet row."""
    index = {}
    for c in range(1, ws.max_column + 1):
        val = ws.cell(header_row, c).value
        if val:
            index[str(val).strip()] = c
    return index


def _read_mapped_value(ws, row, header_index, section_mappings, canonical_field):
    """
    Read a cell value using the Gemini column mapping.

    Looks up the canonical_field in section_mappings to find the
    corresponding Excel column header, then uses header_index to
    find the column number.

    Returns the cell value, or None if the field isn't mapped.
    """
    if not section_mappings:
        return None

    mappings = section_mappings.get('mappings', [])
    for m in mappings:
        if m.get('canonical_field') == canonical_field:
            excel_col = m.get('excel_column', '')
            col_idx = m.get('column_index')

            # Try by column_index first (most reliable)
            if col_idx:
                return ws.cell(row, col_idx).value

            # Fall back to header lookup
            if excel_col in header_index:
                return ws.cell(row, header_index[excel_col]).value

            return None

    return None


# ---------------------------------------------------------------------------
# FundImportService
# ---------------------------------------------------------------------------

class FundImportService:
    """
    Orchestrates the import of a single fund Excel file.

    Uses Gemini AI to semantically map columns, then imports data
    into all 44 Django models.
    """

    STAGES = [
        (5, 'Reading workbook...'),
        (10, 'Classifying sheets with AI...'),
        (20, 'Mapping columns with AI...'),
        (25, 'Importing organization & users...'),
        (35, 'Importing fund master data...'),
        (45, 'Importing investors...'),
        (55, 'Importing commitments...'),
        (60, 'Importing capital calls...'),
        (70, 'Importing portfolio companies...'),
        (75, 'Importing valuations & KPIs...'),
        (80, 'Importing NAV records...'),
        (85, 'Importing exits & distributions...'),
        (90, 'Importing accounting records...'),
        (95, 'Importing compliance data...'),
        (98, 'Building portfolio hierarchy...'),
        (100, 'Complete'),
    ]

    def __init__(self, organization, user):
        self.org = organization
        self.user = user
        self.errors = []
        self.counts = {}

    def import_file(self, import_file_record, progress_cb=None):
        """
        Main entry point. Processes a single ImportFile record.

        Args:
            import_file_record: dataimport.models.ImportFile instance
            progress_cb: callable(pct: int, message: str) for progress updates

        Returns:
            dict with record counts per model type
        """
        filepath = import_file_record.file.path

        def _progress(pct, msg):
            if progress_cb:
                progress_cb(pct, msg)

        # Step 1: Gemini column mapping
        _progress(5, 'Reading workbook...')

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
            logger.warning(f'Gemini mapping failed, falling back to positional: {e}')
            mapping_result = None
            import_file_record.status = 'importing'
            import_file_record.save(update_fields=['status'])

        # Step 2: Import using the management command logic
        _progress(25, 'Starting data import...')

        column_mappings = mapping_result.get('column_mappings', {}) if mapping_result else {}

        result = self._do_import(filepath, column_mappings, _progress)

        return result

    @transaction.atomic
    def _do_import(self, filepath, column_mappings, progress_cb):
        """
        Run the actual import — wraps everything in a transaction.

        This delegates to the existing management command's import logic.
        We import the Command class and call its methods directly, injecting
        our organization context.
        """
        from funds.management.commands.import_fund_excel import Command as ImportCommand

        # Create a command instance but use our org
        cmd = ImportCommand()
        cmd.stdout = _NullOutput()  # suppress management command output
        cmd.stderr = _NullOutput()
        cmd.style = _NullStyle()

        wb = openpyxl.load_workbook(filepath, data_only=True)

        # Ensure org exists
        org = self.org

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

        progress_cb(28, 'Importing organization & users...')
        try:
            users = cmd._import_users(wb, org)
        except Exception as e:
            logger.warning(f'Users import error: {e}')
            users = []
            self.errors.append({'section': 'users', 'error': str(e)})

        progress_cb(32, 'Importing fund master data...')
        try:
            fund, schemes = cmd._import_fund_and_schemes(wb, org)
        except Exception as e:
            logger.error(f'Fund import failed: {e}')
            self.errors.append({'section': 'fund_master', 'error': str(e)})
            wb.close()
            raise  # Can't continue without a fund

        # Grant fund access to users from the Excel file
        for user in users:
            FundAccess.objects.get_or_create(
                user=user, fund=fund,
                defaults={'access_level': 'admin'},
            )

        # Grant fund access to the uploading user (critical for data isolation)
        FundAccess.objects.get_or_create(
            user=self.user, fund=fund,
            defaults={'access_level': 'admin'},
        )

        progress_cb(40, 'Importing investors...')
        try:
            investors = cmd._import_investors(wb, org)
        except Exception as e:
            logger.warning(f'Investors import error: {e}')
            investors = {}
            self.errors.append({'section': 'investors', 'error': str(e)})

        progress_cb(48, 'Importing commitments...')
        try:
            commitments = cmd._import_commitments(wb, org, schemes, investors)
        except Exception as e:
            logger.warning(f'Commitments import error: {e}')
            commitments = {}
            self.errors.append({'section': 'commitments', 'error': str(e)})

        progress_cb(55, 'Importing capital calls...')
        try:
            cmd._import_capital_calls(wb, schemes, commitments)
        except Exception as e:
            logger.warning(f'Capital calls import error: {e}')
            self.errors.append({'section': 'capital_calls', 'error': str(e)})

        progress_cb(62, 'Importing portfolio companies & investments...')
        try:
            companies, investments = cmd._import_portfolio(wb, org, schemes)
        except Exception as e:
            logger.warning(f'Portfolio import error: {e}')
            companies, investments = {}, {}
            self.errors.append({'section': 'portfolio', 'error': str(e)})

        progress_cb(68, 'Importing valuations & KPIs...')
        try:
            cmd._import_valuations(wb, investments)
        except Exception as e:
            logger.warning(f'Valuations import error: {e}')
            self.errors.append({'section': 'valuations', 'error': str(e)})

        try:
            cmd._import_kpis(wb, org, investments, companies)
        except Exception as e:
            logger.warning(f'KPIs import error: {e}')
            self.errors.append({'section': 'kpis', 'error': str(e)})

        progress_cb(75, 'Importing NAV records...')
        try:
            cmd._import_nav(wb, schemes)
        except Exception as e:
            logger.warning(f'NAV import error: {e}')
            self.errors.append({'section': 'nav', 'error': str(e)})

        progress_cb(80, 'Importing exits & distributions...')
        try:
            cmd._import_exits_and_distributions(wb, investments, schemes, commitments)
        except Exception as e:
            logger.warning(f'Exits/distributions import error: {e}')
            self.errors.append({'section': 'exits_distributions', 'error': str(e)})

        progress_cb(85, 'Importing accounting records...')
        try:
            cmd._import_accounting(wb, org, schemes)
        except Exception as e:
            logger.warning(f'Accounting import error: {e}')
            self.errors.append({'section': 'accounting', 'error': str(e)})

        progress_cb(90, 'Importing compliance data...')
        if HAS_COMPLIANCE:
            try:
                cmd._import_compliance(wb, fund, schemes)
            except Exception as e:
                logger.warning(f'Compliance import error: {e}')
                self.errors.append({'section': 'compliance', 'error': str(e)})

        progress_cb(93, 'Importing board meetings...')
        try:
            cmd._import_board_meetings(wb, investments)
        except Exception as e:
            logger.warning(f'Board meetings import error: {e}')
            self.errors.append({'section': 'board_meetings', 'error': str(e)})

        progress_cb(96, 'Building portfolio hierarchy...')
        try:
            # Build hierarchy for this single fund
            data_dir = os.path.dirname(filepath)
            cmd._build_portfolio_hierarchy(org, data_dir, [os.path.basename(filepath)])
            # Invalidate the portfolio service cache so new data is visible
            from api.portfolio import service as portfolio_service
            portfolio_service.reload(org.id)
        except Exception as e:
            logger.warning(f'Portfolio hierarchy error: {e}')
            self.errors.append({'section': 'hierarchy', 'error': str(e)})

        wb.close()

        # Collect counts
        self.counts = self._collect_counts(org, fund)
        progress_cb(100, 'Import complete')

        return {
            'counts': self.counts,
            'errors': self.errors,
            'fund_name': fund.name,
        }

    def _collect_counts(self, org, fund):
        """Collect record counts for the result summary."""
        counts = {
            'funds': 1,
            'schemes': Scheme.objects.filter(fund=fund).count(),
            'investors': Investor.objects.filter(organization=org).count(),
            'commitments': Commitment.objects.filter(scheme__fund=fund).count(),
            'capital_calls': CapitalCall.objects.filter(scheme__fund=fund).count(),
            'portfolio_companies': PortfolioCompany.objects.filter(organization=org).count(),
            'investments': Investment.objects.filter(scheme__fund=fund).count(),
            'tranches': InvestmentTranche.objects.filter(
                investment__scheme__fund=fund).count(),
            'valuations': Valuation.objects.filter(
                investment__scheme__fund=fund).count(),
            'nav_records': NAVRecord.objects.filter(scheme__fund=fund).count(),
            'exit_events': ExitEvent.objects.filter(
                investment__scheme__fund=fund).count(),
            'distributions': Distribution.objects.filter(scheme__fund=fund).count(),
        }

        if HAS_COMPLIANCE:
            counts['sebi_reports'] = SEBIReport.objects.filter(fund=fund).count()
            counts['compliance_calendar'] = ComplianceCalendar.objects.filter(
                fund=fund).count()

        return counts


# ---------------------------------------------------------------------------
# Helpers for management command output suppression
# ---------------------------------------------------------------------------

class _NullOutput:
    """Swallows all write calls — used to suppress management command output."""
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
