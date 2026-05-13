"""
Excel Export Engine — TrackFundAI v5
Exports all major report types to XLSX using xlsxwriter.

Supported report types:
  - mis_report          : MIS (Management Information System) data for portfolio companies
  - valuation_report    : IPEV-based valuation summary per fund
  - lp_statement        : LP (investor) capital account statement
  - compliance_report   : Compliance calendar + RAG status
  - bva_report          : Budget vs Actual variance report
  - portfolio_summary   : Portfolio company summary sheet
  - tds_report          : TDS withholding + Form 26Q data

Usage:
    exporter = ExcelExporter(organization, fund=fund_obj)
    workbook_bytes = exporter.export('mis_report', filters={'scheme_id': ..., 'period': ...})
    # Returns bytes — attach to HttpResponse with content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
"""

import io
import logging
from datetime import date
from decimal import Decimal

logger = logging.getLogger(__name__)


# ─── Palette (matches McKinsey/TrackFundAI brand) ────────────────────────────
DARK_BLUE  = '#003366'
MID_BLUE   = '#0066CC'
LIGHT_BLUE = '#E8F0FB'
ACCENT_RED = '#CC3333'
LIGHT_GRAY = '#F5F5F5'
WHITE      = '#FFFFFF'
DARK_GRAY  = '#333333'


class ExcelExporter:
    """
    Main export class. Instantiate with org + optional fund, call .export(report_type, filters).
    """

    def __init__(self, organization, fund=None):
        self.org  = organization
        self.fund = fund
        self.today = date.today()

    def export(self, report_type: str, filters: dict = None) -> bytes:
        """
        Dispatch to the correct builder and return raw XLSX bytes.
        """
        filters = filters or {}
        dispatch = {
            'mis_report':        self._build_mis_report,
            'valuation_report':  self._build_valuation_report,
            'lp_statement':      self._build_lp_statement,
            'compliance_report': self._build_compliance_report,
            'bva_report':        self._build_bva_report,
            'portfolio_summary': self._build_portfolio_summary,
            'tds_report':        self._build_tds_report,
        }
        builder = dispatch.get(report_type)
        if not builder:
            raise ValueError(f'Unknown report type: {report_type}')
        return builder(filters)

    # ─── Common workbook helpers ───────────────────────────────────────────

    def _new_workbook(self, title: str):
        """Create in-memory xlsxwriter workbook with TrackFundAI brand formats."""
        try:
            import xlsxwriter
        except ImportError:
            raise RuntimeError('xlsxwriter is required — install it: pip install xlsxwriter')

        buf = io.BytesIO()
        wb  = xlsxwriter.Workbook(buf, {'in_memory': True})

        # Define reusable formats
        wb.fmt = {}
        wb.fmt['title'] = wb.add_format({
            'bold': True, 'font_size': 14, 'font_color': WHITE,
            'bg_color': DARK_BLUE, 'align': 'center', 'valign': 'vcenter',
            'border': 0,
        })
        wb.fmt['header'] = wb.add_format({
            'bold': True, 'font_size': 10, 'font_color': WHITE,
            'bg_color': MID_BLUE, 'align': 'center', 'valign': 'vcenter',
            'border': 1, 'border_color': '#0044AA',
        })
        wb.fmt['subheader'] = wb.add_format({
            'bold': True, 'font_size': 10, 'font_color': DARK_BLUE,
            'bg_color': LIGHT_BLUE, 'border': 1, 'border_color': '#AAAACC',
        })
        wb.fmt['cell'] = wb.add_format({
            'font_size': 9, 'font_color': DARK_GRAY,
            'border': 1, 'border_color': '#DDDDDD',
        })
        wb.fmt['cell_gray'] = wb.add_format({
            'font_size': 9, 'font_color': DARK_GRAY,
            'bg_color': LIGHT_GRAY, 'border': 1, 'border_color': '#DDDDDD',
        })
        wb.fmt['number'] = wb.add_format({
            'font_size': 9, 'num_format': '#,##0.00',
            'border': 1, 'border_color': '#DDDDDD', 'align': 'right',
        })
        wb.fmt['currency'] = wb.add_format({
            'font_size': 9, 'num_format': '₹#,##0.00',
            'border': 1, 'border_color': '#DDDDDD', 'align': 'right',
        })
        wb.fmt['pct'] = wb.add_format({
            'font_size': 9, 'num_format': '0.00%',
            'border': 1, 'border_color': '#DDDDDD', 'align': 'right',
        })
        wb.fmt['date'] = wb.add_format({
            'font_size': 9, 'num_format': 'DD-MMM-YYYY',
            'border': 1, 'border_color': '#DDDDDD',
        })
        wb.fmt['bold_cell'] = wb.add_format({
            'bold': True, 'font_size': 9, 'font_color': DARK_BLUE,
            'border': 1, 'border_color': '#DDDDDD',
        })
        wb.fmt['total'] = wb.add_format({
            'bold': True, 'font_size': 9, 'font_color': WHITE,
            'bg_color': DARK_BLUE, 'num_format': '#,##0.00',
            'border': 1, 'border_color': DARK_BLUE, 'align': 'right',
        })
        wb.fmt['total_label'] = wb.add_format({
            'bold': True, 'font_size': 9, 'font_color': WHITE,
            'bg_color': DARK_BLUE, 'border': 1, 'border_color': DARK_BLUE,
        })
        wb.fmt['green'] = wb.add_format({
            'font_size': 9, 'bg_color': '#D4EDDA', 'font_color': '#155724',
            'border': 1, 'border_color': '#C3E6CB',
        })
        wb.fmt['amber'] = wb.add_format({
            'font_size': 9, 'bg_color': '#FFF3CD', 'font_color': '#856404',
            'border': 1, 'border_color': '#FFEEBA',
        })
        wb.fmt['red_fmt'] = wb.add_format({
            'font_size': 9, 'bg_color': '#F8D7DA', 'font_color': '#721C24',
            'border': 1, 'border_color': '#F5C6CB',
        })
        wb.fmt['meta'] = wb.add_format({
            'font_size': 8, 'font_color': '#666666', 'italic': True,
        })

        wb._buf = buf
        return wb

    def _write_cover_row(self, ws, wb, title: str, subtitle: str, col_count: int):
        ws.merge_range(0, 0, 0, col_count - 1, title, wb.fmt['title'])
        ws.set_row(0, 30)
        ws.merge_range(1, 0, 1, col_count - 1, subtitle, wb.fmt['subheader'])
        ws.merge_range(2, 0, 2, col_count - 1,
                       f'TrackFundAI  |  Generated: {self.today.strftime("%d %B %Y")}  |  CONFIDENTIAL',
                       wb.fmt['meta'])
        return 3  # next row

    def _finalize(self, wb) -> bytes:
        wb.close()
        return wb._buf.getvalue()

    def _rag_fmt(self, wb, rag: str):
        return {
            'green': wb.fmt['green'],
            'amber': wb.fmt['amber'],
            'red':   wb.fmt['red_fmt'],
        }.get(rag, wb.fmt['cell'])

    # ─── MIS Report ───────────────────────────────────────────────────────

    def _build_mis_report(self, filters: dict) -> bytes:
        from mis_consolidation.models import BudgetVsActual
        wb = self._new_workbook('MIS Report')
        ws = wb.add_worksheet('MIS Data')
        ws.set_column('A:A', 30)
        ws.set_column('B:B', 20)
        ws.set_column('C:F', 16)
        ws.set_column('G:H', 12)

        headers = ['Portfolio Company', 'Line Item', 'Period', 'Budget (₹)', 'Actual (₹)', 'Variance (₹)', 'Variance %', 'Favorable?']
        row = self._write_cover_row(ws, wb, 'MIS Report — Budget vs Actual', f'Organization: {self.org.name}', len(headers))

        for col, h in enumerate(headers):
            ws.write(row, col, h, wb.fmt['header'])
        row += 1

        qs = BudgetVsActual.objects.filter(organization=self.org).select_related('portfolio_company')
        if filters.get('scheme_id'):
            qs = qs.filter(portfolio_company__investments__scheme_id=filters['scheme_id'])
        if filters.get('period'):
            qs = qs.filter(period=filters['period'])
        if filters.get('company_id'):
            qs = qs.filter(portfolio_company_id=filters['company_id'])

        fmt_toggle = [wb.fmt['cell'], wb.fmt['cell_gray']]
        for i, bva in enumerate(qs.order_by('portfolio_company__name', 'line_item', 'period')):
            fmt = fmt_toggle[i % 2]
            num_fmt = wb.fmt['currency']
            ws.write(row, 0, bva.portfolio_company.name, fmt)
            ws.write(row, 1, bva.get_line_item_display(), fmt)
            ws.write(row, 2, str(bva.period), fmt)
            ws.write(row, 3, float(bva.budget_amount_inr),  num_fmt)
            ws.write(row, 4, float(bva.actual_amount_inr),  num_fmt)
            ws.write(row, 5, float(bva.variance_inr),       num_fmt)
            ws.write(row, 6, float(bva.variance_pct or 0) / 100, wb.fmt['pct'])
            fav_fmt = wb.fmt['green'] if bva.is_favorable else wb.fmt['red_fmt']
            ws.write(row, 7, 'Yes' if bva.is_favorable else 'No', fav_fmt)
            row += 1

        ws.autofilter(3, 0, row - 1, len(headers) - 1)
        return self._finalize(wb)

    # ─── Valuation Report ─────────────────────────────────────────────────

    def _build_valuation_report(self, filters: dict) -> bytes:
        from investments.models import Investment, Valuation
        wb = self._new_workbook('Valuation Report')
        ws = wb.add_worksheet('Valuation')
        ws.set_column('A:A', 28)
        ws.set_column('B:D', 18)
        ws.set_column('E:K', 14)

        headers = ['Portfolio Company', 'Fund / Scheme', 'Valuation Date',
                   'IPEV Level', 'Cost (₹ Cr)', 'Fair Value (₹ Cr)',
                   'MOIC', 'IRR %', 'Unrealised G/L (₹ Cr)', 'Methodology']
        row = self._write_cover_row(ws, wb, 'Portfolio Valuation Report (IPEV)', f'Organization: {self.org.name}', len(headers))

        for col, h in enumerate(headers):
            ws.write(row, col, h, wb.fmt['header'])
        row += 1

        fund_filter = self.fund
        investments = Investment.objects.filter(
            scheme__fund__organization=self.org
        ).select_related('portfolio_company', 'scheme__fund')
        if fund_filter:
            investments = investments.filter(scheme__fund=fund_filter)
        if filters.get('fund_id'):
            investments = investments.filter(scheme__fund_id=filters['fund_id'])

        total_cost = total_fv = Decimal('0')
        for i, inv in enumerate(investments.order_by('scheme__fund__name', 'portfolio_company__name')):
            latest_val = inv.valuations.order_by('-valuation_date').first()
            fmt = wb.fmt['cell'] if i % 2 == 0 else wb.fmt['cell_gray']

            cost_cr = float((inv.investment_amount_inr or 0) / Decimal('10000000'))
            fv_cr   = float((latest_val.fair_value_of_holding or 0) / Decimal('10000000')) if latest_val else 0
            moic    = float(latest_val.moic or 0) if latest_val else 0
            irr_pct = float(latest_val.irr_pct or 0) if latest_val else 0
            gl_cr   = fv_cr - cost_cr

            ws.write(row, 0, inv.portfolio_company.name, fmt)
            ws.write(row, 1, f'{inv.scheme.fund.name} / {inv.scheme.name}', fmt)
            ws.write(row, 2, str(latest_val.valuation_date) if latest_val else 'N/A', fmt)
            ws.write(row, 3, latest_val.get_ipev_level_display() if latest_val else 'N/A', fmt)
            ws.write(row, 4, cost_cr, wb.fmt['number'])
            ws.write(row, 5, fv_cr,   wb.fmt['number'])
            ws.write(row, 6, moic,    wb.fmt['number'])
            ws.write(row, 7, irr_pct / 100, wb.fmt['pct'])
            gl_fmt = wb.fmt['green'] if gl_cr >= 0 else wb.fmt['red_fmt']
            ws.write(row, 8, gl_cr, gl_fmt)
            ws.write(row, 9, latest_val.get_methodology_display() if latest_val else 'N/A', fmt)

            total_cost += Decimal(str(cost_cr))
            total_fv   += Decimal(str(fv_cr))
            row += 1

        # Totals row
        ws.write(row, 0, 'TOTAL', wb.fmt['total_label'])
        ws.write(row, 4, float(total_cost), wb.fmt['total'])
        ws.write(row, 5, float(total_fv),   wb.fmt['total'])
        gl_total = float(total_fv - total_cost)
        ws.write(row, 8, gl_total, wb.fmt['total'])

        ws.autofilter(3, 0, row - 1, len(headers) - 1)
        return self._finalize(wb)

    # ─── LP Statement ─────────────────────────────────────────────────────

    def _build_lp_statement(self, filters: dict) -> bytes:
        from lp.models import Investor, FundCommitment, CapitalCall, Distribution
        wb = self._new_workbook('LP Capital Account Statement')
        ws = wb.add_worksheet('LP Statement')
        ws.set_column('A:A', 30)
        ws.set_column('B:H', 18)

        investor_id = filters.get('investor_id')
        fund_id = filters.get('fund_id') or (str(self.fund.id) if self.fund else None)

        headers = ['Investor', 'Fund / Scheme', 'Commitment (₹)', 'Called (%)',
                   'Called Amount (₹)', 'Distributions (₹)', 'Net IRR %', 'MOIC']
        row = self._write_cover_row(ws, wb, 'LP Capital Account Statement', f'Organization: {self.org.name}', len(headers))
        for col, h in enumerate(headers):
            ws.write(row, col, h, wb.fmt['header'])
        row += 1

        commitments = FundCommitment.objects.filter(
            investor__organization=self.org
        ).select_related('investor', 'scheme__fund')
        if investor_id:
            commitments = commitments.filter(investor_id=investor_id)
        if fund_id:
            commitments = commitments.filter(scheme__fund_id=fund_id)

        for i, fc in enumerate(commitments.order_by('investor__investor_name')):
            fmt = wb.fmt['cell'] if i % 2 == 0 else wb.fmt['cell_gray']
            called_calls = CapitalCall.objects.filter(scheme=fc.scheme, investor_commitments=fc)
            total_called = sum(
                c.capital_call_schedules.filter(
                    commitment=fc
                ).values_list('amount_called_inr', flat=True)
                for c in called_calls
            ) if hasattr(fc, 'pk') else 0

            distributions = Distribution.objects.filter(
                scheme=fc.scheme
            ).aggregate_investor_total(fc) if hasattr(Distribution, 'aggregate_investor_total') else Decimal('0')

            commitment = float(fc.commitment_amount_inr or 0)
            called_pct = (total_called / commitment * 100) if commitment else 0

            ws.write(row, 0, fc.investor.investor_name, fmt)
            ws.write(row, 1, f'{fc.scheme.fund.name} / {fc.scheme.name}', fmt)
            ws.write(row, 2, commitment, wb.fmt['currency'])
            ws.write(row, 3, called_pct / 100, wb.fmt['pct'])
            ws.write(row, 4, float(total_called) if not isinstance(total_called, (int, float)) else total_called, wb.fmt['currency'])
            ws.write(row, 5, 0, wb.fmt['currency'])  # distributions placeholder
            ws.write(row, 6, float(fc.net_irr_pct or 0) / 100 if hasattr(fc, 'net_irr_pct') else 0, wb.fmt['pct'])
            ws.write(row, 7, float(fc.moic or 0) if hasattr(fc, 'moic') else 0, wb.fmt['number'])
            row += 1

        ws.autofilter(3, 0, row - 1, len(headers) - 1)
        return self._finalize(wb)

    # ─── Compliance Report ────────────────────────────────────────────────

    def _build_compliance_report(self, filters: dict) -> bytes:
        from compliance.models import ComplianceCalendar, EquityThresholdAlert, SEBIReport
        wb = self._new_workbook('Compliance Report')

        # Sheet 1: Compliance Calendar
        ws1 = wb.add_worksheet('Compliance Calendar')
        ws1.set_column('A:A', 30)
        ws1.set_column('B:G', 16)
        headers1 = ['Title', 'Type', 'Due Date', 'Status', 'Assigned To', 'Fund', 'Notes']
        row = self._write_cover_row(ws1, wb, 'Compliance Calendar', f'Organization: {self.org.name}', len(headers1))
        for col, h in enumerate(headers1):
            ws1.write(row, col, h, wb.fmt['header'])
        row += 1

        cal_qs = ComplianceCalendar.objects.filter(organization=self.org).select_related('fund', 'assigned_to')
        for i, ev in enumerate(cal_qs.order_by('due_date')):
            fmt = wb.fmt['cell'] if i % 2 == 0 else wb.fmt['cell_gray']
            status_fmt = {
                'completed': wb.fmt['green'],
                'overdue':   wb.fmt['red_fmt'],
                'in_progress': wb.fmt['amber'],
            }.get(ev.status, fmt)
            ws1.write(row, 0, ev.title, fmt)
            ws1.write(row, 1, ev.get_compliance_type_display(), fmt)
            ws1.write(row, 2, str(ev.due_date), wb.fmt['date'])
            ws1.write(row, 3, ev.get_status_display(), status_fmt)
            ws1.write(row, 4, ev.assigned_to.get_full_name() if ev.assigned_to else '', fmt)
            ws1.write(row, 5, ev.fund.name if ev.fund else 'All Funds', fmt)
            ws1.write(row, 6, ev.notes[:80] if ev.notes else '', fmt)
            row += 1

        # Sheet 2: Equity Threshold Alerts
        ws2 = wb.add_worksheet('Equity Alerts')
        ws2.set_column('A:H', 18)
        headers2 = ['Company', 'Stake %', 'Breach Date', 'Severity', 'Custodian Deadline', 'Notified?', 'Escalated?', 'Resolved?']
        row2 = self._write_cover_row(ws2, wb, 'Equity Threshold Alerts (SEBI 10% Rule)', f'Organization: {self.org.name}', len(headers2))
        for col, h in enumerate(headers2):
            ws2.write(row2, col, h, wb.fmt['header'])
        row2 += 1

        alerts = EquityThresholdAlert.objects.filter(
            investment__scheme__fund__organization=self.org
        ).select_related('investment')
        if filters.get('unresolved_only'):
            alerts = alerts.filter(resolved=False)

        for i, alert in enumerate(alerts.order_by('-breach_date')):
            sev_fmt = {
                'urgent': wb.fmt['red_fmt'],
                'high':   wb.fmt['amber'],
                'medium': wb.fmt['green'],
            }.get(alert.severity, wb.fmt['cell'])
            fmt = wb.fmt['cell'] if i % 2 == 0 else wb.fmt['cell_gray']
            ws2.write(row2, 0, alert.investment.company_name, fmt)
            ws2.write(row2, 1, float(alert.stake_percentage), wb.fmt['number'])
            ws2.write(row2, 2, str(alert.breach_date), wb.fmt['date'])
            ws2.write(row2, 3, alert.severity.upper(), sev_fmt)
            ws2.write(row2, 4, str(alert.custodian_notification_deadline), wb.fmt['date'])
            ws2.write(row2, 5, 'Yes' if alert.custodian_notified else 'No',
                      wb.fmt['green'] if alert.custodian_notified else wb.fmt['red_fmt'])
            ws2.write(row2, 6, 'Yes' if alert.is_escalated else 'No', fmt)
            ws2.write(row2, 7, 'Yes' if alert.resolved else 'No',
                      wb.fmt['green'] if alert.resolved else wb.fmt['amber'])
            row2 += 1

        return self._finalize(wb)

    # ─── BvA Report ───────────────────────────────────────────────────────

    def _build_bva_report(self, filters: dict) -> bytes:
        from mis_consolidation.models import BudgetVsActual, MISAnomalyAlert
        wb = self._new_workbook('Budget vs Actual Report')

        ws = wb.add_worksheet('BvA Variance')
        ws.set_column('A:A', 30)
        ws.set_column('B:I', 16)
        headers = ['Company', 'Line Item', 'Period', 'Budget (₹)', 'Actual (₹)',
                   'Variance (₹)', 'Variance %', 'Favorable?', 'Alert Level']
        row = self._write_cover_row(ws, wb, 'Budget vs Actual Variance Report', f'Organization: {self.org.name}', len(headers))
        for col, h in enumerate(headers):
            ws.write(row, col, h, wb.fmt['header'])
        row += 1

        qs = BudgetVsActual.objects.filter(organization=self.org).select_related('portfolio_company')
        if filters.get('period'):
            qs = qs.filter(period=filters['period'])
        if filters.get('company_id'):
            qs = qs.filter(portfolio_company_id=filters['company_id'])
        if filters.get('unfavorable_only'):
            qs = qs.filter(is_favorable=False)

        for i, bva in enumerate(qs.order_by('portfolio_company__name', 'period', 'line_item')):
            fmt = wb.fmt['cell'] if i % 2 == 0 else wb.fmt['cell_gray']
            var_pct = float(bva.variance_pct or 0)
            # Determine alert level
            alert_level = ''
            alert_fmt   = fmt
            abs_var_pct = abs(var_pct)
            if abs_var_pct > 50 and not bva.is_favorable:
                alert_level = 'CRITICAL'
                alert_fmt   = wb.fmt['red_fmt']
            elif abs_var_pct > 25 and not bva.is_favorable:
                alert_level = 'HIGH'
                alert_fmt   = wb.fmt['amber']
            elif abs_var_pct > 10 and not bva.is_favorable:
                alert_level = 'MEDIUM'

            ws.write(row, 0, bva.portfolio_company.name,    fmt)
            ws.write(row, 1, bva.get_line_item_display(),   fmt)
            ws.write(row, 2, str(bva.period),               fmt)
            ws.write(row, 3, float(bva.budget_amount_inr),  wb.fmt['currency'])
            ws.write(row, 4, float(bva.actual_amount_inr),  wb.fmt['currency'])
            ws.write(row, 5, float(bva.variance_inr),       wb.fmt['currency'])
            ws.write(row, 6, var_pct / 100,                 wb.fmt['pct'])
            ws.write(row, 7, 'Yes' if bva.is_favorable else 'No',
                     wb.fmt['green'] if bva.is_favorable else wb.fmt['red_fmt'])
            ws.write(row, 8, alert_level, alert_fmt)
            row += 1

        ws.autofilter(3, 0, row - 1, len(headers) - 1)

        # Sheet 2: Anomalies
        ws2 = wb.add_worksheet('Anomalies')
        ws2.set_column('A:G', 18)
        headers2 = ['Company', 'Anomaly Type', 'Severity', 'Period', 'Message', 'Resolved?', 'Detected At']
        row2 = self._write_cover_row(ws2, wb, 'MIS Anomaly Alerts', '', len(headers2))
        for col, h in enumerate(headers2):
            ws2.write(row2, col, h, wb.fmt['header'])
        row2 += 1

        anomalies = MISAnomalyAlert.objects.filter(
            organization=self.org
        ).select_related('portfolio_company').order_by('-created_at')
        for i, a in enumerate(anomalies):
            sev_fmt = {
                'critical': wb.fmt['red_fmt'],
                'high':     wb.fmt['amber'],
                'medium':   wb.fmt['cell'],
            }.get(a.severity, wb.fmt['cell'])
            fmt = wb.fmt['cell'] if i % 2 == 0 else wb.fmt['cell_gray']
            ws2.write(row2, 0, a.portfolio_company.name, fmt)
            ws2.write(row2, 1, a.get_anomaly_type_display(), fmt)
            ws2.write(row2, 2, a.severity.upper(), sev_fmt)
            ws2.write(row2, 3, str(a.period) if a.period else '', fmt)
            ws2.write(row2, 4, a.message[:100] if a.message else '', fmt)
            ws2.write(row2, 5, 'Yes' if a.resolved else 'No',
                     wb.fmt['green'] if a.resolved else wb.fmt['amber'])
            ws2.write(row2, 6, str(a.created_at)[:19], fmt)
            row2 += 1

        return self._finalize(wb)

    # ─── Portfolio Summary ────────────────────────────────────────────────

    def _build_portfolio_summary(self, filters: dict) -> bytes:
        from investments.models import PortfolioCompany, Investment
        wb = self._new_workbook('Portfolio Summary')
        ws = wb.add_worksheet('Portfolio')
        ws.set_column('A:A', 30)
        ws.set_column('B:K', 16)
        headers = ['Company', 'Sector', 'Stage', 'Investment Date', 'Cost (₹ Cr)',
                   'Fair Value (₹ Cr)', 'MOIC', 'IRR %', 'Fund', 'Status']
        row = self._write_cover_row(ws, wb, 'Portfolio Company Summary', f'Organization: {self.org.name}', len(headers))
        for col, h in enumerate(headers):
            ws.write(row, col, h, wb.fmt['header'])
        row += 1

        companies = PortfolioCompany.objects.filter(organization=self.org, is_active=True)
        if filters.get('fund_id'):
            companies = companies.filter(investments__scheme__fund_id=filters['fund_id']).distinct()

        for i, co in enumerate(companies.order_by('sector', 'name')):
            fmt = wb.fmt['cell'] if i % 2 == 0 else wb.fmt['cell_gray']
            inv = co.investments.select_related('scheme__fund').order_by('-investment_date').first()
            latest_val = co.investments.filter(
                valuations__isnull=False
            ).prefetch_related('valuations').first()
            lv = latest_val.valuations.order_by('-valuation_date').first() if latest_val else None

            cost_cr = float((inv.investment_amount_inr or 0) / Decimal('10000000')) if inv else 0
            fv_cr   = float((lv.fair_value_of_holding or 0) / Decimal('10000000')) if lv else cost_cr
            moic    = float(lv.moic or 0) if lv else 0
            irr_pct = float(lv.irr_pct or 0) if lv else 0

            ws.write(row, 0, co.name, fmt)
            ws.write(row, 1, co.sector, fmt)
            ws.write(row, 2, inv.get_investment_stage_display() if inv else '', fmt)
            ws.write(row, 3, str(inv.investment_date) if inv else '', wb.fmt['date'])
            ws.write(row, 4, cost_cr, wb.fmt['number'])
            ws.write(row, 5, fv_cr,   wb.fmt['number'])
            ws.write(row, 6, moic,    wb.fmt['number'])
            ws.write(row, 7, irr_pct / 100, wb.fmt['pct'])
            ws.write(row, 8, inv.scheme.fund.name if inv else '', fmt)
            ws.write(row, 9, co.get_company_status_display() if hasattr(co, 'get_company_status_display') else co.company_status, fmt)
            row += 1

        ws.autofilter(3, 0, row - 1, len(headers) - 1)
        return self._finalize(wb)

    # ─── TDS Report ───────────────────────────────────────────────────────

    def _build_tds_report(self, filters: dict) -> bytes:
        from tds.models import TDSWithholding, Form26QReturn
        wb = self._new_workbook('TDS Report')

        ws = wb.add_worksheet('TDS Withholding')
        ws.set_column('A:A', 25)
        ws.set_column('B:K', 16)
        headers = ['Investor / Payee', 'Payment Nature', 'Payment Date', 'Quarter', 'FY',
                   'Gross (₹)', 'TDS Rate %', 'Base Tax (₹)', 'Surcharge (₹)',
                   'Cess (₹)', 'Total TDS (₹)', 'Net Payment (₹)']
        row = self._write_cover_row(ws, wb, 'TDS Withholding Statement', f'Organization: {self.org.name}', len(headers))
        for col, h in enumerate(headers):
            ws.write(row, col, h, wb.fmt['header'])
        row += 1

        qs = TDSWithholding.objects.filter(organization=self.org).select_related('investor')
        if filters.get('financial_year'):
            qs = qs.filter(financial_year=filters['financial_year'])
        if filters.get('quarter'):
            qs = qs.filter(quarter=filters['quarter'])

        total_gross = total_tds = Decimal('0')
        for i, tds in enumerate(qs.order_by('payment_date')):
            fmt = wb.fmt['cell'] if i % 2 == 0 else wb.fmt['cell_gray']
            investor_name = tds.investor.investor_name if tds.investor else tds.payee_name
            ws.write(row, 0, investor_name, fmt)
            ws.write(row, 1, tds.get_payment_nature_display(), fmt)
            ws.write(row, 2, str(tds.payment_date), wb.fmt['date'])
            ws.write(row, 3, tds.quarter, fmt)
            ws.write(row, 4, tds.financial_year, fmt)
            ws.write(row, 5, float(tds.gross_amount_inr), wb.fmt['currency'])
            ws.write(row, 6, float(tds.tds_rate_pct) / 100, wb.fmt['pct'])
            ws.write(row, 7, float(tds.base_tax), wb.fmt['currency'])
            ws.write(row, 8, float(tds.surcharge_amount), wb.fmt['currency'])
            ws.write(row, 9, float(tds.cess_amount), wb.fmt['currency'])
            ws.write(row, 10, float(tds.total_tds), wb.fmt['currency'])
            ws.write(row, 11, float(tds.net_payment), wb.fmt['currency'])
            total_gross += tds.gross_amount_inr
            total_tds   += tds.total_tds
            row += 1

        ws.write(row, 0, 'TOTAL', wb.fmt['total_label'])
        ws.write(row, 5, float(total_gross), wb.fmt['total'])
        ws.write(row, 10, float(total_tds), wb.fmt['total'])

        ws.autofilter(3, 0, row - 1, len(headers) - 1)
        return self._finalize(wb)
