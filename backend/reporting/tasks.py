"""
Reporting Celery tasks — calendar management, reminders, report generation.

Scheduled tasks:
  - update_reporting_calendar: Daily — seed/update calendar for current quarter
  - send_reporting_reminders: Daily — send T+3 and T+5 reminders for overdue MIS
  - generate_quarterly_reports: Quarterly (15th of following quarter)
"""

from celery import shared_task
from django.utils import timezone
import logging
import datetime

logger = logging.getLogger(__name__)


@shared_task(name='reporting.update_reporting_calendar')
def update_reporting_calendar():
    """
    Ensure all required ReportingCalendar entries exist for the current
    and upcoming quarter. Run daily.
    """
    from accounts.models import Organization
    from funds.models import Fund, Scheme
    from reporting.models import ReportingCalendar

    today = timezone.now().date()
    q_start, q_end, q_label = _current_quarter(today)
    deadline_15th_next = _quarter_end_plus_15(q_end)

    orgs = Organization.objects.filter(is_active=True)

    for org in orgs:
        funds = Fund.objects.filter(organization=org, fund_status='active')
        for fund in funds:
            # Monthly MIS obligation (this month)
            _ensure_calendar(org, fund, None, 'monthly_mis',
                             f"Monthly MIS — {today.strftime('%B %Y')}",
                             today.replace(day=1),
                             (today.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1),
                             _t5_working_days(today.replace(day=1)))

            # SEBI Monthly Report (7th of following month)
            next_month_7 = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=7)
            _ensure_calendar(org, fund, None, 'sebi_monthly',
                             f"SEBI Monthly — {today.strftime('%B %Y')}",
                             today.replace(day=1),
                             (today.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1),
                             next_month_7)

            for scheme in fund.schemes.filter(is_active=True):
                # Quarterly obligations
                _ensure_calendar(org, fund, scheme, 'quarterly_lp',
                                 f'Quarterly LP Letter — {q_label}',
                                 q_start, q_end, deadline_15th_next)
                _ensure_calendar(org, fund, scheme, 'valuation_cert',
                                 f'Valuation Certificate — {q_label}',
                                 q_start, q_end, deadline_15th_next)
                _ensure_calendar(org, fund, scheme, 'nav_statement',
                                 f'NAV Statement — {q_label}',
                                 q_start, q_end, deadline_15th_next)

            # Annual obligations (if we're in the right time of year)
            if today.month >= 4 and today.month <= 6:  # Annual accounts due 30-Jun
                year = today.year
                _ensure_calendar(org, fund, None, 'annual_accounts',
                                 f'Annual Accounts FY{year-1}-{str(year)[2:]}',
                                 datetime.date(year-1, 4, 1),
                                 datetime.date(year, 3, 31),
                                 datetime.date(year, 6, 30))
                _ensure_calendar(org, fund, None, 'fatca_crs',
                                 f'FATCA/CRS Report FY{year-1}-{str(year)[2:]}',
                                 datetime.date(year-1, 4, 1),
                                 datetime.date(year, 3, 31),
                                 datetime.date(year, 5, 31))
                _ensure_calendar(org, fund, None, 'form_64a',
                                 f'Form 64A / LP Tax FY{year-1}-{str(year)[2:]}',
                                 datetime.date(year-1, 4, 1),
                                 datetime.date(year, 3, 31),
                                 datetime.date(year, 6, 30))

    logger.info(f'Reporting calendar updated for {today}')


def _ensure_calendar(org, fund, scheme, report_type, period_label,
                     period_start, period_end, deadline):
    from reporting.models import ReportingCalendar
    obj, created = ReportingCalendar.objects.get_or_create(
        organization=org, fund=fund, scheme=scheme,
        report_type=report_type, period_start=period_start,
        defaults={
            'period_label': period_label,
            'period_end': period_end,
            'deadline': deadline,
            'status': 'upcoming',
        },
    )
    # Update status if past deadline
    if not created:
        today = timezone.now().date()
        if obj.status in ('upcoming', 'due') and today > deadline:
            obj.status = 'overdue'
            obj.save(update_fields=['status'])
        elif obj.status == 'upcoming' and today >= (deadline - datetime.timedelta(days=5)):
            obj.status = 'due'
            obj.save(update_fields=['status'])


@shared_task(name='reporting.send_reporting_reminders')
def send_reporting_reminders():
    """Send T+3 and T+5 reminders for overdue or due MIS obligations."""
    from reporting.models import ReportingCalendar, ReportingReminder

    today = timezone.now().date()
    overdue = ReportingCalendar.objects.filter(
        status__in=['due', 'overdue'],
    ).select_related('fund', 'scheme')

    for obligation in overdue:
        days_overdue = (today - obligation.deadline).days

        if days_overdue == 3:
            _send_reminder(obligation, 't3_reminder')
        elif days_overdue == 5:
            _send_reminder(obligation, 't5_escalation')


def _send_reminder(obligation, reminder_type):
    from reporting.models import ReportingReminder
    import django.core.mail as mail
    from django.conf import settings

    # Don't double-send
    if ReportingReminder.objects.filter(
        obligation=obligation, reminder_type=reminder_type
    ).exists():
        return

    fund_name = obligation.fund.name if obligation.fund else 'Fund'
    subject = f'[TrackFundAI] Reminder: {obligation.get_report_type_display()} overdue — {fund_name}'
    body = (
        f'This is an automated reminder.\n\n'
        f'The following report is {'overdue' if reminder_type == "t5_escalation" else "due"}:\n\n'
        f'Report: {obligation.get_report_type_display()}\n'
        f'Fund: {fund_name}\n'
        f'Period: {obligation.period_label}\n'
        f'Deadline: {obligation.deadline}\n\n'
        f'Please submit the report at the earliest.\n\n'
        f'TrackFundAI Compliance System'
    )

    # Get fund team email addresses
    recipients = []
    if obligation.fund:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        recipients = list(
            User.objects.filter(
                organization=obligation.organization,
                role__in=['admin', 'gp_partner', 'cfo'],
            ).values_list('email', flat=True)
        )

    success = True
    error_msg = ''
    if recipients:
        try:
            mail.send_mail(
                subject=subject,
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL if hasattr(settings, 'DEFAULT_FROM_EMAIL') else 'noreply@trackfundai.com',
                recipient_list=recipients,
                fail_silently=True,
            )
        except Exception as e:
            success = False
            error_msg = str(e)

    ReportingReminder.objects.create(
        obligation=obligation,
        reminder_type=reminder_type,
        sent_to=', '.join(recipients),
        subject=subject,
        body=body,
        success=success,
        error_message=error_msg,
    )


@shared_task(name='reporting.generate_quarterly_reports')
def generate_quarterly_reports():
    """
    Trigger quarterly report generation on the 15th of each quarter-end following month.
    Generates: LP Letter, NAV Statement, Valuation Certificate.
    """
    from accounts.models import Organization
    from reporting.models import ReportingCalendar
    from reporting.report_generator import generate_lp_letter, generate_nav_statement

    today = timezone.now().date()
    # Only run on the 15th of Jan, Apr, Jul, Oct (15th of month following quarter end)
    if today.day != 15 or today.month not in (1, 4, 7, 10):
        return

    obligations = ReportingCalendar.objects.filter(
        report_type__in=['quarterly_lp', 'nav_statement'],
        deadline=today,
        status__in=['upcoming', 'due'],
    ).select_related('fund', 'scheme')

    for obligation in obligations:
        if not obligation.scheme:
            continue
        try:
            if obligation.report_type == 'quarterly_lp':
                generate_lp_letter(
                    obligation.scheme,
                    obligation.period_label,
                    obligation.period_start,
                    obligation.period_end,
                )
                obligation.status = 'submitted'
                obligation.report_generated_at = timezone.now()
                obligation.save(update_fields=['status', 'report_generated_at'])

            elif obligation.report_type == 'nav_statement':
                generate_nav_statement(obligation.scheme, obligation.period_end)
                obligation.status = 'submitted'
                obligation.report_generated_at = timezone.now()
                obligation.save(update_fields=['status', 'report_generated_at'])

        except Exception as e:
            logger.error(f'Report generation failed for {obligation}: {e}')


def _current_quarter(today):
    """Returns (quarter_start, quarter_end, label) for Indian FY."""
    y = today.year
    m = today.month

    if 4 <= m <= 6:
        return datetime.date(y, 4, 1), datetime.date(y, 6, 30), f'Q1 FY{y}-{str(y+1)[2:]}'
    elif 7 <= m <= 9:
        return datetime.date(y, 7, 1), datetime.date(y, 9, 30), f'Q2 FY{y}-{str(y+1)[2:]}'
    elif 10 <= m <= 12:
        return datetime.date(y, 10, 1), datetime.date(y, 12, 31), f'Q3 FY{y}-{str(y+1)[2:]}'
    else:
        return datetime.date(y, 1, 1), datetime.date(y, 3, 31), f'Q4 FY{y-1}-{str(y)[2:]}'


def _quarter_end_plus_15(quarter_end):
    """Return the 15th of the month following the quarter end."""
    next_month = (quarter_end.replace(day=1) + datetime.timedelta(days=32)).replace(day=15)
    return next_month


def _t5_working_days(period_start):
    """Return T+5 working days from period start (rough estimate)."""
    d = period_start
    working_days = 0
    while working_days < 5:
        d = d + datetime.timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            working_days += 1
    return d
