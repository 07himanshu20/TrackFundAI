"""
Post-import recalculation tasks.

After every successful Excel import, triggers:
  1. NAV computation for all affected schemes
  2. Carry waterfall computation for all affected schemes
  3. Management fee recomputation for the current quarter
  4. Risk score recomputation for all affected portfolio companies

This ensures that KPI cards and dashboards always reflect the latest data
immediately after an import, without requiring a manual refresh.
"""

from celery import shared_task
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)


@shared_task(name='dataimport.post_import_recalculate', bind=True, max_retries=2)
def post_import_recalculate(self, import_file_id: str):
    """
    Triggered after a successful import. Recomputes NAV, carry, fees, and risk scores
    for all schemes and companies touched by the import.

    Args:
        import_file_id: UUID of the ImportFile that was just processed
    """
    from dataimport.models import ImportFile
    from accounting.nav_engine import compute_nav
    from accounting.carry_engine import compute_carry
    from accounting.fee_engine import compute_management_fee

    try:
        import_file = ImportFile.objects.select_related('job', 'fund').get(pk=import_file_id)
    except ImportFile.DoesNotExist:
        logger.error(f'ImportFile {import_file_id} not found for post-import recalculation')
        return

    fund = import_file.fund
    if not fund:
        logger.info(f'ImportFile {import_file_id} has no linked fund — skipping recalc')
        return

    today = timezone.now().date()

    # Compute for all schemes in the fund
    for scheme in fund.schemes.filter(is_active=True):
        try:
            compute_nav(scheme, today)
            logger.info(f'NAV recomputed for scheme {scheme}')
        except Exception as e:
            logger.error(f'NAV recomputation failed for scheme {scheme}: {e}')

        try:
            compute_carry(scheme, today)
            logger.info(f'Carry recomputed for scheme {scheme}')
        except Exception as e:
            logger.error(f'Carry recomputation failed for scheme {scheme}: {e}')

        try:
            # Current quarter
            q_start, q_end = _current_quarter_dates(today)
            compute_management_fee(scheme, q_start, q_end)
            logger.info(f'Fee recomputed for scheme {scheme}')
        except Exception as e:
            logger.error(f'Fee recomputation failed for scheme {scheme}: {e}')

    # Trigger risk score recomputation for portfolio companies
    try:
        _recompute_risk_scores(fund, today)
    except Exception as e:
        logger.error(f'Risk score recomputation failed for fund {fund}: {e}')

    logger.info(f'Post-import recalculation complete for import {import_file_id}')


def _current_quarter_dates(today):
    """Returns (quarter_start, quarter_end) for the given date (Indian FY basis)."""
    import datetime

    m = today.month
    y = today.year

    # Indian FY quarters: Apr-Jun, Jul-Sep, Oct-Dec, Jan-Mar
    if 4 <= m <= 6:
        return datetime.date(y, 4, 1), datetime.date(y, 6, 30)
    elif 7 <= m <= 9:
        return datetime.date(y, 7, 1), datetime.date(y, 9, 30)
    elif 10 <= m <= 12:
        return datetime.date(y, 10, 1), datetime.date(y, 12, 31)
    else:  # Jan-Mar
        return datetime.date(y, 1, 1), datetime.date(y, 3, 31)


def _recompute_risk_scores(fund, as_of_date):
    """Trigger risk score recomputation for all portfolio companies in the fund."""
    try:
        from riskscore.scoring_engine import compute_risk_score
        for scheme in fund.schemes.filter(is_active=True):
            for inv in scheme.investments.filter(status='active').select_related('portfolio_company'):
                if inv.portfolio_company:
                    try:
                        compute_risk_score(inv.portfolio_company, as_of_date)
                    except Exception as e:
                        logger.warning(f'Risk score failed for {inv.portfolio_company}: {e}')
    except ImportError:
        logger.info('riskscore app not available — skipping risk score recomputation')
