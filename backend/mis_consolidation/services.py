"""
MIS Consolidation Services:
1. MISAggregator — cross-company P&L rollup for a fund/scheme
2. AnomalyDetector — statistical variance alerts from BvA records
3. BvAImporter — bulk-upsert BvA from parsed MIS data dict
"""
import statistics
from decimal import Decimal
from typing import List, Optional

from django.db.models import Sum, Avg, StdDev, Count
from django.utils import timezone

from .models import BudgetVsActual, ConsolidatedMIS, MISAnomalyAlert


class MISAggregator:
    """
    Aggregates BvA records across all portfolio companies in a fund/scheme
    and writes ConsolidatedMIS records.
    Called as a Celery task after each import.
    """

    def __init__(self, fund, scheme=None, period_year=None, period_month=None):
        self.fund = fund
        self.scheme = scheme
        self.period_year = period_year
        self.period_month = period_month

    def run(self):
        """Compute and persist consolidated MIS for the fund."""
        from investments.models import PortfolioCompany

        # PortfolioCompany has no direct 'fund' field — scope via investments chain
        companies = PortfolioCompany.objects.filter(
            investments__scheme__fund=self.fund,
        ).distinct()
        if self.scheme:
            companies = companies.filter(
                investments__scheme=self.scheme,
            ).distinct()

        bva_qs = BudgetVsActual.objects.filter(
            portfolio_company__in=companies,
            fund=self.fund,
        )
        if self.period_year:
            bva_qs = bva_qs.filter(period_year=self.period_year)
        if self.period_month:
            bva_qs = bva_qs.filter(period_month=self.period_month)

        # Aggregate by line_item, period_year, period_month
        agg = bva_qs.values('line_item', 'period_year', 'period_month', 'period_quarter').annotate(
            total_actual=Sum('actual_inr'),
            total_budget=Sum('budget_inr'),
            company_count=Count('portfolio_company', distinct=True),
        )

        results = []
        for row in agg:
            total_actual = row['total_actual'] or Decimal('0')
            total_budget = row['total_budget'] or Decimal('0')
            variance = total_actual - total_budget
            variance_pct = None
            if total_budget and total_budget != 0:
                variance_pct = (variance / abs(total_budget)) * 100

            obj, _ = ConsolidatedMIS.objects.update_or_create(
                fund=self.fund,
                scheme=self.scheme,
                period_year=row['period_year'],
                period_month=row['period_month'],
                period_quarter=row['period_quarter'] or '',
                line_item=row['line_item'],
                defaults={
                    'organization': self.fund.organization,
                    'total_actual_inr': total_actual,
                    'total_budget_inr': total_budget,
                    'company_count': row['company_count'],
                    'total_variance_inr': variance,
                    'total_variance_pct': variance_pct,
                },
            )
            results.append(obj)
        return results

    @classmethod
    def get_6month_rollup(cls, fund, line_items=None, scheme=None):
        """
        Return last 6 months of consolidated P&L for chart rendering.
        Returns: list of {year, month, line_item, actual, budget, variance}
        """
        from datetime import date
        from dateutil.relativedelta import relativedelta

        today = date.today()
        six_months_ago = today - relativedelta(months=6)

        qs = ConsolidatedMIS.objects.filter(
            fund=fund,
            period_year__gte=six_months_ago.year,
        ).filter(
            period_month__isnull=False,
        )
        if scheme:
            qs = qs.filter(scheme=scheme)
        if line_items:
            qs = qs.filter(line_item__in=line_items)

        return list(qs.values(
            'period_year', 'period_month', 'line_item',
            'total_actual_inr', 'total_budget_inr', 'total_variance_inr', 'total_variance_pct',
        ).order_by('period_year', 'period_month'))


class AnomalyDetector:
    """
    Statistical anomaly detection on BvA records.
    Methods:
    - detect_budget_variance: flag records where variance% > threshold
    - detect_statistical_outlier: Z-score > 2 across 6-month window
    - detect_revenue_decline: MoM revenue decline > 20%
    """

    VARIANCE_THRESHOLDS = {
        'critical': Decimal('50'),
        'high': Decimal('25'),
        'medium': Decimal('10'),
    }

    def __init__(self, portfolio_company):
        self.company = portfolio_company

    def run_all(self):
        """Run all detectors and create MISAnomalyAlert records."""
        alerts = []
        alerts += self.detect_budget_variance()
        alerts += self.detect_statistical_outliers()
        alerts += self.detect_revenue_decline()
        return alerts

    def detect_budget_variance(self):
        """Flag BvA records where |variance_pct| exceeds threshold."""
        from django.db.models import Q
        records = BudgetVsActual.objects.filter(
            portfolio_company=self.company,
            variance_pct__isnull=False,
        ).exclude(budget_inr=0)

        alerts = []
        for rec in records:
            abs_var = abs(rec.variance_pct)
            severity = None
            if abs_var >= self.VARIANCE_THRESHOLDS['critical']:
                severity = 'critical'
            elif abs_var >= self.VARIANCE_THRESHOLDS['high']:
                severity = 'high'
            elif abs_var >= self.VARIANCE_THRESHOLDS['medium']:
                severity = 'medium'

            if severity and not MISAnomalyAlert.objects.filter(
                bva_record=rec, anomaly_type='budget_variance', resolved=False,
            ).exists():
                alert = MISAnomalyAlert.objects.create(
                    bva_record=rec,
                    portfolio_company=self.company,
                    anomaly_type='budget_variance',
                    severity=severity,
                    description=(
                        f'{rec.get_line_item_display()}: {rec.period_year}-{rec.period_month:02d} '
                        f'variance {rec.variance_pct:.1f}% '
                        f'(Budget: {rec.budget_inr}, Actual: {rec.actual_inr})'
                    ),
                )
                alerts.append(alert)
        return alerts

    def detect_statistical_outliers(self):
        """
        Z-score analysis: for each line item, compute mean & stdev over last 6 months,
        flag records > 2 SD from mean.
        """
        from collections import defaultdict
        alerts = []
        # Get last 6 months of actual values per line item
        records = list(BudgetVsActual.objects.filter(
            portfolio_company=self.company,
            actual_inr__isnull=False,
        ).order_by('-period_year', '-period_month')[:72])  # 12 line items × 6 months

        by_line = defaultdict(list)
        for r in records:
            by_line[r.line_item].append((r, float(r.actual_inr)))

        for line_item, pairs in by_line.items():
            if len(pairs) < 3:
                continue
            values = [v for _, v in pairs]
            mean = statistics.mean(values)
            stdev = statistics.stdev(values) if len(values) > 1 else 0
            if stdev == 0:
                continue

            for rec, val in pairs:
                z_score = abs(val - mean) / stdev
                if z_score > 2:
                    if not MISAnomalyAlert.objects.filter(
                        bva_record=rec, anomaly_type='statistical_outlier', resolved=False,
                    ).exists():
                        severity = 'critical' if z_score > 3 else 'high'
                        alert = MISAnomalyAlert.objects.create(
                            bva_record=rec,
                            portfolio_company=self.company,
                            anomaly_type='statistical_outlier',
                            severity=severity,
                            description=(
                                f'{rec.get_line_item_display()}: Z-score={z_score:.2f} '
                                f'(value={val:.0f}, mean={mean:.0f}, σ={stdev:.0f})'
                            ),
                        )
                        alerts.append(alert)
        return alerts

    def detect_revenue_decline(self):
        """Flag >20% MoM revenue decline."""
        revenue_records = list(BudgetVsActual.objects.filter(
            portfolio_company=self.company,
            line_item='revenue',
            actual_inr__isnull=False,
        ).order_by('period_year', 'period_month'))

        alerts = []
        for i in range(1, len(revenue_records)):
            prev = revenue_records[i - 1]
            curr = revenue_records[i]
            if prev.actual_inr and prev.actual_inr != 0:
                decline_pct = ((curr.actual_inr - prev.actual_inr) / abs(prev.actual_inr)) * 100
                if decline_pct <= Decimal('-20'):
                    if not MISAnomalyAlert.objects.filter(
                        bva_record=curr, anomaly_type='revenue_decline', resolved=False,
                    ).exists():
                        alert = MISAnomalyAlert.objects.create(
                            bva_record=curr,
                            portfolio_company=self.company,
                            anomaly_type='revenue_decline',
                            severity='high' if decline_pct <= Decimal('-30') else 'medium',
                            description=(
                                f'Revenue declined {abs(decline_pct):.1f}% MoM: '
                                f'{prev.period_year}-{prev.period_month:02d} '
                                f'({prev.actual_inr}) → {curr.period_year}-{curr.period_month:02d} '
                                f'({curr.actual_inr})'
                            ),
                        )
                        alerts.append(alert)
        return alerts

    @classmethod
    def run_all_companies(cls, organization):
        """Run anomaly detection across all portfolio companies in an org."""
        from investments.models import PortfolioCompany
        companies = PortfolioCompany.objects.filter(organization=organization)
        all_alerts = []
        for company in companies:
            detector = cls(company)
            all_alerts += detector.run_all()
        return all_alerts


class BvAImporter:
    """
    Bulk-upsert BvA records from a parsed MIS data dict.
    Called by the dataimport service after parsing MIS Excel.

    Expected data format:
    {
      'period_year': 2025, 'period_month': 3,
      'line_items': {'revenue': {'budget': 1000, 'actual': 950}, ...}
    }
    """

    def __init__(self, portfolio_company):
        self.company = portfolio_company

    def upsert(self, data: dict) -> List[BudgetVsActual]:
        """Upsert BvA records from a parsed MIS dict."""
        period_year = data['period_year']
        period_month = data.get('period_month')
        period_quarter = data.get('period_quarter', '')
        period_type = data.get('period_type', 'monthly')
        line_items = data.get('line_items', {})

        records = []
        for line_item, values in line_items.items():
            if line_item not in dict(BudgetVsActual._meta.get_field('line_item').choices):
                continue  # Skip unknown line items
            budget = values.get('budget')
            actual = values.get('actual')

            obj, _ = BudgetVsActual.objects.update_or_create(
                portfolio_company=self.company,
                period_year=period_year,
                period_month=period_month,
                period_quarter=period_quarter,
                line_item=line_item,
                defaults={
                    'period_type': period_type,
                    'budget_inr': Decimal(str(budget)) if budget is not None else None,
                    'actual_inr': Decimal(str(actual)) if actual is not None else None,
                },
            )
            records.append(obj)

        # After upsert, run anomaly detection
        detector = AnomalyDetector(self.company)
        detector.run_all()

        return records
