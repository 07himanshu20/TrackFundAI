"""
Compliance Escalation Service — v5
Handles:
  1. Auto-escalation chain: GP Partner → CFO/Fund Accountant → Compliance Officer
  2. Combined fund compliance score computation (0-100, weighted)
  3. Push notifications on new escalations
"""
from decimal import Decimal
from datetime import date, timedelta
import logging

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Escalation Chain
# Level 1 = GP Partner (gp_admin)
# Level 2 = CFO / Fund Accountant (fund_accountant)
# Level 3 = Compliance Officer (compliance_officer)
# ──────────────────────────────────────────────────────────────
ESCALATION_CHAIN = [
    (1, 'gp_admin'),
    (2, 'fund_accountant'),
    (3, 'compliance_officer'),
]


class ComplianceEscalationService:
    """
    Creates EscalationLog records and sends notifications up the chain.
    Usage:
        svc = ComplianceEscalationService(organization)
        svc.escalate_equity_breach(alert)
        svc.escalate_sebi_deadline(sebi_report)
        svc.escalate_circular_action(action)
        svc.run_all(fund)   # full scan for any new breaches
    """

    def __init__(self, organization):
        self.org = organization

    def escalate_equity_breach(self, alert, triggered_by=None):
        """
        Escalate an EquityThresholdAlert through the chain.
        URGENT → all 3 levels immediately.
        HIGH   → Level 1, then L2 if unresolved after 3 days.
        MEDIUM → Level 1 only.
        """
        from .models import EscalationLog
        if alert.is_escalated:
            return  # Already escalated

        levels_to_notify = []
        if alert.severity == 'urgent':
            levels_to_notify = ESCALATION_CHAIN  # All 3 immediately
        elif alert.severity == 'high':
            levels_to_notify = ESCALATION_CHAIN[:2]  # L1 + L2
        else:
            levels_to_notify = ESCALATION_CHAIN[:1]  # L1 only

        for level, role in levels_to_notify:
            msg = (
                f'SEBI Equity Threshold Breach [{alert.severity.upper()}]: '
                f'{alert.investment.company_name} stake is {alert.stake_percentage}% '
                f'(breached on {alert.breach_date}). '
                f'Custodian notification deadline: {alert.custodian_notification_deadline}.'
            )
            log = EscalationLog.objects.create(
                organization=self.org,
                escalation_type='equity_threshold_breach',
                level=level,
                escalated_to_role=role,
                message=msg,
                equity_alert=alert,
                escalated_by=triggered_by,
            )
            self._send_notification(log)

        alert.is_escalated = True
        alert.save(update_fields=['is_escalated'])

    def escalate_sebi_deadline(self, sebi_report, triggered_by=None):
        """Escalate an overdue or near-due SEBI report filing."""
        from .models import EscalationLog
        today = date.today()
        days_overdue = (today - sebi_report.due_date).days
        level = 1
        role = 'gp_admin'
        if days_overdue > 7:
            level, role = 2, 'fund_accountant'
        if days_overdue > 14:
            level, role = 3, 'compliance_officer'

        msg = (
            f'SEBI {sebi_report.get_report_type_display()} for {sebi_report.fund.name} '
            f'was due {sebi_report.due_date} ({days_overdue} days overdue). '
            f'Current status: {sebi_report.get_filing_status_display()}. '
            f'Immediate action required.'
        )
        log = EscalationLog.objects.create(
            organization=self.org,
            escalation_type='sebi_deadline_breach',
            level=level,
            escalated_to_role=role,
            message=msg,
            sebi_report=sebi_report,
            escalated_by=triggered_by,
        )
        self._send_notification(log)

    def escalate_circular_action(self, action, triggered_by=None):
        """Escalate an overdue circular action item."""
        from .models import EscalationLog
        today = date.today()
        if not action.due_date or action.due_date >= today:
            return

        days_overdue = (today - action.due_date).days
        level, role = 1, 'gp_admin'
        if days_overdue > 7:
            level, role = 2, 'fund_accountant'
        if days_overdue > 21:
            level, role = 3, 'compliance_officer'

        msg = (
            f'SEBI Circular Action overdue: "{action.action_title}" '
            f'from circular {action.circular.circular_number} '
            f'was due {action.due_date} ({days_overdue} days overdue). '
            f'Priority: {action.priority.upper()}.'
        )
        log = EscalationLog.objects.create(
            organization=self.org,
            escalation_type='circular_action_overdue',
            level=level,
            escalated_to_role=role,
            message=msg,
            circular_action=action,
            escalated_by=triggered_by,
        )
        self._send_notification(log)

    def run_all(self, fund):
        """
        Full compliance scan for a fund — auto-escalate any open breaches.
        Call from a Celery periodic task (daily).
        """
        from .models import EquityThresholdAlert, SEBIReport, CircularAction
        today = date.today()

        # 1. Equity threshold breaches not yet escalated
        for alert in EquityThresholdAlert.objects.filter(
            investment__fund=fund,
            threshold_breached=True,
            resolved=False,
            is_escalated=False,
        ):
            self.escalate_equity_breach(alert)

        # 2. SEBI reports overdue (not filed, due date passed)
        for report in SEBIReport.objects.filter(
            fund=fund,
            filing_status__in=['not_started', 'data_collection', 'in_review'],
            due_date__lt=today,
        ):
            # Only escalate if not already escalated
            if not report.escalations.filter(resolved=False).exists():
                self.escalate_sebi_deadline(report)

        # 3. Overdue circular actions for this fund
        for action in CircularAction.objects.filter(
            fund=fund,
            status__in=['pending', 'in_progress'],
            due_date__lt=today,
        ):
            if not action.escalations.filter(resolved=False).exists():
                self.escalate_circular_action(action)

    def _send_notification(self, escalation_log):
        """
        Push a Notification record to users with the escalated_to_role.
        Falls back silently if Notification model is not available.
        """
        try:
            from accounts.models import User, Notification
            recipients = User.objects.filter(
                organization=self.org,
                role=escalation_log.escalated_to_role,
                is_active=True,
            )
            for user in recipients:
                Notification.objects.create(
                    user=user,
                    title=f'Compliance Escalation — {escalation_log.get_escalation_type_display()}',
                    message=escalation_log.message,
                    notification_type='compliance_breach',
                    severity=_esc_to_severity(escalation_log.level),
                )
        except Exception as e:
            logger.warning(f'Escalation notification failed: {e}')


def _esc_to_severity(level: int) -> str:
    return {1: 'medium', 2: 'high', 3: 'critical'}.get(level, 'medium')


# ──────────────────────────────────────────────────────────────
# Combined Fund Compliance Score
# ──────────────────────────────────────────────────────────────

SCORE_WEIGHTS = {
    'sebi_filing_score':       Decimal('0.30'),
    'aml_score':               Decimal('0.20'),
    'equity_threshold_score':  Decimal('0.20'),
    'portfolio_company_score': Decimal('0.20'),
    'circular_action_score':   Decimal('0.10'),
}


class FundComplianceScorer:
    """
    Computes a combined 0-100 compliance score for a Fund.
    Uses weighted average of 5 sub-scores.

    Usage:
        scorer = FundComplianceScorer(fund, organization)
        score_obj = scorer.compute_and_save()
    """

    def __init__(self, fund, organization):
        self.fund = fund
        self.org = organization
        self.today = date.today()

    def compute_and_save(self):
        from .models import FundComplianceScore
        scores, detail = self._compute_all()
        combined = sum(
            scores[k] * SCORE_WEIGHTS[k] for k in SCORE_WEIGHTS
        ).quantize(Decimal('0.01'))

        obj, _ = FundComplianceScore.objects.update_or_create(
            fund=self.fund,
            score_date=self.today,
            defaults={
                'sebi_filing_score':       scores['sebi_filing_score'],
                'aml_score':               scores['aml_score'],
                'equity_threshold_score':  scores['equity_threshold_score'],
                'portfolio_company_score': scores['portfolio_company_score'],
                'circular_action_score':   scores['circular_action_score'],
                'combined_score':          combined,
                'score_detail':            detail,
            },
        )
        return obj

    def _compute_all(self):
        scores = {}
        detail = {}

        scores['sebi_filing_score'], detail['sebi'] = self._sebi_score()
        scores['aml_score'],         detail['aml']  = self._aml_score()
        scores['equity_threshold_score'], detail['equity'] = self._equity_score()
        scores['portfolio_company_score'], detail['portfolio'] = self._portfolio_score()
        scores['circular_action_score'], detail['circulars'] = self._circular_score()

        return scores, detail

    def _sebi_score(self):
        from .models import SEBIReport
        reports = SEBIReport.objects.filter(fund=self.fund)
        total = reports.count()
        if total == 0:
            return Decimal('100'), {'total': 0, 'note': 'No SEBI reports'}

        overdue = reports.filter(
            filing_status__in=['not_started', 'data_collection'],
            due_date__lt=self.today,
        ).count()
        score = max(Decimal('0'), Decimal('100') - (overdue / total * 100)).quantize(Decimal('0.01'))
        return score, {'total': total, 'overdue': overdue}

    def _aml_score(self):
        from .models import AMLDueDiligence
        from lp.models import Investor
        investors = Investor.objects.filter(fund_commitments__fund=self.fund).distinct()
        total = investors.count()
        if total == 0:
            return Decimal('100'), {'total': 0, 'note': 'No investors'}

        high_risk = AMLDueDiligence.objects.filter(
            investor__in=investors,
            risk_rating__in=['high', 'very_high'],
        ).count()
        unassessed = investors.filter(aml_records__isnull=True).count()
        deduction = ((high_risk * 10) + (unassessed * 5))
        score = max(Decimal('0'), Decimal('100') - deduction).quantize(Decimal('0.01'))
        return score, {'total': total, 'high_risk': high_risk, 'unassessed': unassessed}

    def _equity_score(self):
        from .models import EquityThresholdAlert
        from investments.models import Investment
        investments = Investment.objects.filter(fund=self.fund)
        alerts = EquityThresholdAlert.objects.filter(
            investment__in=investments,
            threshold_breached=True,
            resolved=False,
        )
        total_alerts = alerts.count()
        urgent = alerts.filter(severity='urgent').count()
        high = alerts.filter(severity='high').count()
        deduction = (urgent * 30) + (high * 15) + ((total_alerts - urgent - high) * 5)
        score = max(Decimal('0'), Decimal('100') - deduction).quantize(Decimal('0.01'))
        return score, {'open_alerts': total_alerts, 'urgent': urgent, 'high': high}

    def _portfolio_score(self):
        from .models import PortfolioComplianceScore
        from investments.models import PortfolioCompany
        companies = PortfolioCompany.objects.filter(
            investments__fund=self.fund,
        ).distinct()
        scores = PortfolioComplianceScore.objects.filter(
            portfolio_company__in=companies,
        ).order_by('portfolio_company', '-score_date').distinct('portfolio_company')
        if not scores.exists():
            return Decimal('100'), {'note': 'No portfolio company scores'}

        avg = sum(s.compliance_score for s in scores) / scores.count()
        return avg.quantize(Decimal('0.01')), {'companies_scored': scores.count()}

    def _circular_score(self):
        from .models import CircularAction
        actions = CircularAction.objects.filter(fund=self.fund)
        total = actions.exclude(status='not_applicable').count()
        if total == 0:
            return Decimal('100'), {'total': 0}

        overdue = actions.filter(
            status__in=['pending', 'in_progress'],
            due_date__lt=self.today,
        ).count()
        score = max(Decimal('0'), Decimal('100') - (overdue / total * 100)).quantize(Decimal('0.01'))
        return score, {'total': total, 'overdue': overdue}
