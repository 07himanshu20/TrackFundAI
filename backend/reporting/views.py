import datetime

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status

from accounts.permissions import IsGPUser
from accounts.fund_access_helpers import get_accessible_fund_ids
from .models import ReportingCalendar, GeneratedReport


@api_view(['GET'])
@permission_classes([IsGPUser])
def calendar_list(request):
    """
    List reporting calendar obligations for the user's accessible funds.
    Optional filters: ?status=overdue&report_type=quarterly_lp&fund_id=<uuid>
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)
    qs = ReportingCalendar.objects.filter(
        organization=org,
        fund__id__in=fund_ids,
    ).select_related('fund', 'scheme').order_by('deadline')

    status_filter = request.query_params.get('status')
    if status_filter:
        qs = qs.filter(status=status_filter)

    report_type_filter = request.query_params.get('report_type')
    if report_type_filter:
        qs = qs.filter(report_type=report_type_filter)

    fund_filter = request.query_params.get('fund_id')
    if fund_filter:
        qs = qs.filter(fund__id=fund_filter)

    today = datetime.date.today()
    data = [
        {
            'id': str(ob.id),
            'report_type': ob.report_type,
            'report_type_display': ob.get_report_type_display(),
            'fund_name': ob.fund.name if ob.fund else '—',
            'scheme_name': ob.scheme.name if ob.scheme else '—',
            'period_label': ob.period_label,
            'period_start': str(ob.period_start),
            'period_end': str(ob.period_end),
            'deadline': str(ob.deadline),
            'days_remaining': (ob.deadline - today).days,
            'status': ob.status,
            'status_display': ob.get_status_display(),
            'report_generated_at': ob.report_generated_at.isoformat() if ob.report_generated_at else None,
            'submitted_at': ob.submitted_at.isoformat() if ob.submitted_at else None,
        }
        for ob in qs
    ]
    return Response(data)


@api_view(['GET'])
@permission_classes([IsGPUser])
def calendar_detail(request, obligation_id):
    org = request.organization
    try:
        ob = ReportingCalendar.objects.select_related('fund', 'scheme').get(
            pk=obligation_id, organization=org
        )
    except ReportingCalendar.DoesNotExist:
        return Response({'detail': 'Obligation not found.'}, status=404)

    return Response({
        'id': str(ob.id),
        'report_type': ob.report_type,
        'report_type_display': ob.get_report_type_display(),
        'fund_name': ob.fund.name if ob.fund else '—',
        'period_label': ob.period_label,
        'deadline': str(ob.deadline),
        'status': ob.status,
        'notes': ob.notes,
    })


@api_view(['POST'])
@permission_classes([IsGPUser])
def mark_submitted(request, obligation_id):
    """Mark a reporting obligation as submitted."""
    from django.utils import timezone
    org = request.organization
    try:
        ob = ReportingCalendar.objects.get(pk=obligation_id, organization=org)
    except ReportingCalendar.DoesNotExist:
        return Response({'detail': 'Obligation not found.'}, status=404)

    ob.status = 'submitted'
    ob.submitted_at = timezone.now()
    ob.submitted_by = request.user
    ob.notes = request.data.get('notes', ob.notes)
    ob.save(update_fields=['status', 'submitted_at', 'submitted_by', 'notes'])
    return Response({'detail': 'Marked as submitted.'})


@api_view(['POST'])
@permission_classes([IsGPUser])
def generate_report(request, obligation_id):
    """Trigger on-demand report generation for a reporting obligation."""
    org = request.organization
    try:
        ob = ReportingCalendar.objects.select_related('fund', 'scheme').get(
            pk=obligation_id, organization=org
        )
    except ReportingCalendar.DoesNotExist:
        return Response({'detail': 'Obligation not found.'}, status=404)

    from reporting.report_generator import generate_lp_letter, generate_nav_statement

    if ob.report_type == 'quarterly_lp' and ob.scheme:
        report = generate_lp_letter(
            ob.scheme, ob.period_label, ob.period_start, ob.period_end, request.user
        )
        if report:
            from django.utils import timezone
            ob.report_generated_at = timezone.now()
            ob.save(update_fields=['report_generated_at'])
            return Response({'detail': 'LP Letter generated.', 'report_id': str(report.id)})

    elif ob.report_type == 'nav_statement' and ob.scheme:
        report = generate_nav_statement(ob.scheme, ob.period_end, request.user)
        if report:
            from django.utils import timezone
            ob.report_generated_at = timezone.now()
            ob.save(update_fields=['report_generated_at'])
            return Response({'detail': 'NAV Statement generated.', 'report_id': str(report.id)})

    return Response({'detail': f'Report type {ob.report_type} not yet implemented for on-demand generation.'}, status=400)


@api_view(['GET'])
@permission_classes([IsGPUser])
def generated_reports_list(request):
    """List generated reports for the org."""
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    qs = GeneratedReport.objects.filter(organization=org).order_by('-generated_at')[:50]
    data = [
        {
            'id': str(r.id),
            'report_type': r.report_type,
            'report_format': r.report_format,
            'file_url': r.file.url if r.file else None,
            'file_size': r.file_size,
            'generated_at': r.generated_at.isoformat(),
        }
        for r in qs
    ]
    return Response(data)


@api_view(['POST'])
@permission_classes([IsGPUser])
def trigger_calendar_update(request):
    """Manually trigger the reporting calendar update task."""
    from reporting.tasks import update_reporting_calendar
    update_reporting_calendar.delay()
    return Response({'detail': 'Calendar update triggered.'})


# ─── Excel Export (v5) ────────────────────────────────────────────────────────

REPORT_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
REPORT_FILENAMES = {
    'mis_report':        'mis_report',
    'valuation_report':  'valuation_report',
    'lp_statement':      'lp_statement',
    'compliance_report': 'compliance_report',
    'bva_report':        'bva_variance',
    'portfolio_summary': 'portfolio_summary',
    'tds_report':        'tds_report',
}


@api_view(['GET'])
@permission_classes([IsGPUser])
def excel_export(request, report_type):
    """
    Download an XLSX export for any major report type.

    URL: GET /api/reporting/export/<report_type>/

    Supported types: mis_report, valuation_report, lp_statement,
                     compliance_report, bva_report, portfolio_summary, tds_report

    Query params passed as filters:
        ?fund_id=<uuid>     — filter by fund
        ?period=2024-Q2     — filter by period (BvA/MIS)
        ?company_id=<uuid>  — filter by portfolio company
        ?financial_year=FY2024-25  — for TDS
        ?quarter=Q1         — for TDS
        ?unresolved_only=true  — compliance alerts
        ?unfavorable_only=true — BvA unfavorable variances only
    """
    from django.http import HttpResponse
    from .excel_exporter import ExcelExporter
    from funds.models import Fund

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if report_type not in REPORT_FILENAMES:
        return Response({
            'detail': f'Unknown report type: {report_type}. Valid types: {list(REPORT_FILENAMES.keys())}',
        }, status=400)

    # Resolve optional fund filter
    fund = None
    fund_id = request.query_params.get('fund_id')
    if fund_id:
        try:
            fund = Fund.objects.get(pk=fund_id, organization=org)
        except Fund.DoesNotExist:
            return Response({'detail': 'Fund not found.'}, status=404)

    filters = {k: v for k, v in request.query_params.items() if k != 'fund_id'}

    try:
        exporter = ExcelExporter(org, fund=fund)
        xlsx_bytes = exporter.export(report_type, filters)
    except RuntimeError as e:
        return Response({'detail': str(e)}, status=500)
    except Exception as e:
        return Response({'detail': f'Export failed: {str(e)}'}, status=500)

    import datetime
    today_str = datetime.date.today().strftime('%Y%m%d')
    filename = f'{REPORT_FILENAMES[report_type]}_{today_str}.xlsx'

    resp = HttpResponse(xlsx_bytes, content_type=REPORT_CONTENT_TYPE)
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp
