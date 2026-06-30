from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.audit import log_audit
from accounts.fund_access_helpers import get_accessible_fund_ids, user_has_fund_access
from accounts.permissions import IsGPUser
from config.cache_utils import cached_api_view
from .models import (
    SEBIReport, AMLDueDiligence, ComplianceTestReport,
    CTRChecklistItem, EquityThresholdAlert, ComplianceCalendar,
    PPMAmendment, SEBICircular, CircularAction,
    EscalationLog, FundComplianceScore,
)
from .serializers import (
    SEBIReportListSerializer, SEBIReportDetailSerializer,
    AMLDueDiligenceSerializer,
    ComplianceTestReportListSerializer, ComplianceTestReportDetailSerializer,
    CTRChecklistItemSerializer,
    EquityThresholdAlertSerializer,
    ComplianceCalendarSerializer,
    PPMAmendmentListSerializer, PPMAmendmentDetailSerializer,
    SEBICircularListSerializer, SEBICircularDetailSerializer,
    CircularActionSerializer,
)


# -- SEBI Report CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def sebi_report_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        fund_ids = get_accessible_fund_ids(request.user)
        qs = SEBIReport.objects.filter(
            fund__organization=org,
            fund__id__in=fund_ids,
        ).select_related('fund', 'scheme')
        fund_id = request.query_params.get('fund')
        if fund_id:
            qs = qs.filter(fund_id=fund_id)
        report_type = request.query_params.get('report_type')
        if report_type:
            qs = qs.filter(report_type=report_type)
        filing_status = request.query_params.get('filing_status')
        if filing_status:
            qs = qs.filter(filing_status=filing_status)
        return Response(SEBIReportListSerializer(qs, many=True).data)

    ser = SEBIReportDetailSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    report = ser.save()
    log_audit(request, 'create', 'sebi_report', report.id, {
        'fund': str(report.fund_id), 'type': report.report_type,
        'period_end': str(report.reporting_period_end),
    })
    return Response(SEBIReportDetailSerializer(report).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT'])
@permission_classes([IsGPUser])
def sebi_report_detail(request, report_id):
    org = request.organization
    try:
        report = SEBIReport.objects.select_related('fund', 'scheme').get(
            pk=report_id, fund__organization=org,
        )
    except SEBIReport.DoesNotExist:
        return Response({'detail': 'SEBI report not found.'}, status=404)

    if not user_has_fund_access(request.user, report.fund):
        return Response({'detail': 'SEBI report not found.'}, status=404)

    if request.method == 'GET':
        return Response(SEBIReportDetailSerializer(report).data)

    ser = SEBIReportDetailSerializer(report, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    log_audit(request, 'update', 'sebi_report', report.id, {
        'fields': list(request.data.keys()),
    })
    return Response(SEBIReportDetailSerializer(report).data)


# -- AML Due Diligence CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def aml_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        qs = AMLDueDiligence.objects.filter(
            investor__organization=org,
        ).select_related('investor')
        risk_rating = request.query_params.get('risk_rating')
        if risk_rating:
            qs = qs.filter(risk_rating=risk_rating)
        land_border = request.query_params.get('land_border')
        if land_border is not None:
            qs = qs.filter(is_land_border_country_investor=land_border.lower() == 'true')
        return Response(AMLDueDiligenceSerializer(qs, many=True).data)

    ser = AMLDueDiligenceSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    aml = ser.save(assessed_by=request.user)
    log_audit(request, 'create', 'aml_due_diligence', aml.id, {
        'investor': str(aml.investor_id), 'risk': aml.risk_rating,
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT'])
@permission_classes([IsGPUser])
def aml_detail(request, aml_id):
    org = request.organization
    try:
        aml = AMLDueDiligence.objects.select_related('investor').get(
            pk=aml_id, investor__organization=org,
        )
    except AMLDueDiligence.DoesNotExist:
        return Response({'detail': 'AML record not found.'}, status=404)

    if request.method == 'GET':
        return Response(AMLDueDiligenceSerializer(aml).data)

    ser = AMLDueDiligenceSerializer(aml, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    log_audit(request, 'update', 'aml_due_diligence', aml.id, {
        'fields': list(request.data.keys()),
    })
    return Response(ser.data)


# -- Compliance Test Report CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def ctr_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        fund_ids = get_accessible_fund_ids(request.user)
        qs = ComplianceTestReport.objects.filter(
            scheme__fund__organization=org,
            scheme__fund__id__in=fund_ids,
        ).select_related('scheme')
        scheme_id = request.query_params.get('scheme')
        if scheme_id:
            qs = qs.filter(scheme_id=scheme_id)
        fy = request.query_params.get('financial_year')
        if fy:
            qs = qs.filter(financial_year=fy)
        return Response(ComplianceTestReportListSerializer(qs, many=True).data)

    ser = ComplianceTestReportDetailSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    ctr = ser.save(prepared_by=request.user)
    log_audit(request, 'create', 'compliance_test_report', ctr.id, {
        'scheme': str(ctr.scheme_id), 'fy': ctr.financial_year,
    })
    return Response(ComplianceTestReportDetailSerializer(ctr).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT'])
@permission_classes([IsGPUser])
def ctr_detail(request, ctr_id):
    org = request.organization
    try:
        ctr = ComplianceTestReport.objects.select_related('scheme__fund').prefetch_related(
            'checklist_items',
        ).get(pk=ctr_id, scheme__fund__organization=org)
    except ComplianceTestReport.DoesNotExist:
        return Response({'detail': 'CTR not found.'}, status=404)

    if not user_has_fund_access(request.user, ctr.scheme.fund):
        return Response({'detail': 'CTR not found.'}, status=404)

    if request.method == 'GET':
        return Response(ComplianceTestReportDetailSerializer(ctr).data)

    ser = ComplianceTestReportDetailSerializer(ctr, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    log_audit(request, 'update', 'compliance_test_report', ctr.id, {
        'fields': list(request.data.keys()),
    })
    return Response(ComplianceTestReportDetailSerializer(ctr).data)


# -- CTR Checklist Items --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def ctr_checklist_list(request, ctr_id):
    org = request.organization
    try:
        ctr = ComplianceTestReport.objects.select_related('scheme__fund').get(
            pk=ctr_id, scheme__fund__organization=org,
        )
    except ComplianceTestReport.DoesNotExist:
        return Response({'detail': 'CTR not found.'}, status=404)

    if not user_has_fund_access(request.user, ctr.scheme.fund):
        return Response({'detail': 'CTR not found.'}, status=404)

    if request.method == 'GET':
        items = ctr.checklist_items.all()
        return Response(CTRChecklistItemSerializer(items, many=True).data)

    data = request.data.copy()
    data['compliance_test_report'] = str(ctr.id)
    ser = CTRChecklistItemSerializer(data=data)
    ser.is_valid(raise_exception=True)
    item = ser.save()
    log_audit(request, 'create', 'ctr_checklist_item', item.id, {
        'ctr': str(ctr.id), 'check': item.check_number,
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['PUT'])
@permission_classes([IsGPUser])
def ctr_checklist_detail(request, item_id):
    org = request.organization
    try:
        item = CTRChecklistItem.objects.select_related(
            'compliance_test_report__scheme__fund',
        ).get(
            pk=item_id,
            compliance_test_report__scheme__fund__organization=org,
        )
    except CTRChecklistItem.DoesNotExist:
        return Response({'detail': 'Checklist item not found.'}, status=404)

    if not user_has_fund_access(request.user, item.compliance_test_report.scheme.fund):
        return Response({'detail': 'Checklist item not found.'}, status=404)

    ser = CTRChecklistItemSerializer(item, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    log_audit(request, 'update', 'ctr_checklist_item', item.id, {
        'fields': list(request.data.keys()),
    })
    return Response(ser.data)


# -- Equity Threshold Alert --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def threshold_alert_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)
    qs = EquityThresholdAlert.objects.filter(
        investment__scheme__fund__organization=org,
        investment__scheme__fund__id__in=fund_ids,
    ).select_related('investment')
    if request.method == 'GET':
        unresolved = request.query_params.get('unresolved')
        if unresolved is not None and unresolved.lower() == 'true':
            qs = qs.filter(resolved=False)
        return Response(EquityThresholdAlertSerializer(qs, many=True).data)

    # POST: manually create a threshold alert (e.g., when flagging a new investment)
    ser = EquityThresholdAlertSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    alert = ser.save()
    log_audit(request, 'create', 'equity_threshold_alert', alert.id, {
        'investment': str(alert.investment_id),
        'stake_pct': str(alert.stake_percentage),
        'breach_date': str(alert.breach_date),
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT'])
@permission_classes([IsGPUser])
def threshold_alert_detail(request, alert_id):
    org = request.organization
    try:
        alert = EquityThresholdAlert.objects.select_related(
            'investment__scheme__fund',
        ).get(
            pk=alert_id, investment__scheme__fund__organization=org,
        )
    except EquityThresholdAlert.DoesNotExist:
        return Response({'detail': 'Alert not found.'}, status=404)

    if not user_has_fund_access(request.user, alert.investment.scheme.fund):
        return Response({'detail': 'Alert not found.'}, status=404)

    if request.method == 'GET':
        return Response(EquityThresholdAlertSerializer(alert).data)

    ser = EquityThresholdAlertSerializer(alert, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    log_audit(request, 'update', 'equity_threshold_alert', alert.id, {
        'fields': list(request.data.keys()),
    })
    return Response(ser.data)


# -- Compliance Calendar CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def calendar_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        qs = ComplianceCalendar.objects.filter(organization=org)
        cal_status = request.query_params.get('status')
        if cal_status:
            qs = qs.filter(status=cal_status)
        compliance_type = request.query_params.get('compliance_type')
        if compliance_type:
            qs = qs.filter(compliance_type=compliance_type)
        return Response(ComplianceCalendarSerializer(qs, many=True).data)

    ser = ComplianceCalendarSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    event = ser.save(organization=org)
    log_audit(request, 'create', 'compliance_calendar', event.id, {
        'title': event.title, 'due': str(event.due_date),
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def calendar_detail(request, event_id):
    org = request.organization
    try:
        event = ComplianceCalendar.objects.get(pk=event_id, organization=org)
    except ComplianceCalendar.DoesNotExist:
        return Response({'detail': 'Calendar event not found.'}, status=404)

    if request.method == 'GET':
        return Response(ComplianceCalendarSerializer(event).data)

    if request.method == 'PUT':
        ser = ComplianceCalendarSerializer(event, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'compliance_calendar', event.id, {
            'fields': list(request.data.keys()),
        })
        return Response(ser.data)

    log_audit(request, 'delete', 'compliance_calendar', event.id)
    event.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# -- PPM Amendments --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def ppm_amendment_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        fund_ids = get_accessible_fund_ids(request.user)
        qs = PPMAmendment.objects.filter(
            fund__organization=org,
            fund__id__in=fund_ids,
        ).select_related('fund', 'scheme')
        fund_id = request.query_params.get('fund')
        if fund_id:
            qs = qs.filter(fund_id=fund_id)
        approval_status = request.query_params.get('approval_status')
        if approval_status:
            qs = qs.filter(approval_status=approval_status)
        return Response(PPMAmendmentListSerializer(qs, many=True).data)

    ser = PPMAmendmentDetailSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    amendment = ser.save(prepared_by=request.user)
    log_audit(request, 'create', 'ppm_amendment', amendment.id, {
        'fund': str(amendment.fund_id),
        'amendment_number': amendment.amendment_number,
        'title': amendment.title,
    })
    return Response(PPMAmendmentDetailSerializer(amendment).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def ppm_amendment_detail(request, amendment_id):
    org = request.organization
    try:
        amendment = PPMAmendment.objects.select_related('fund', 'scheme').get(
            pk=amendment_id, fund__organization=org,
        )
    except PPMAmendment.DoesNotExist:
        return Response({'detail': 'PPM amendment not found.'}, status=404)

    if not user_has_fund_access(request.user, amendment.fund):
        return Response({'detail': 'PPM amendment not found.'}, status=404)

    if request.method == 'GET':
        return Response(PPMAmendmentDetailSerializer(amendment).data)

    if request.method == 'PUT':
        ser = PPMAmendmentDetailSerializer(amendment, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'ppm_amendment', amendment.id, {
            'fields': list(request.data.keys()),
        })
        return Response(PPMAmendmentDetailSerializer(amendment).data)

    log_audit(request, 'delete', 'ppm_amendment', amendment.id, {
        'fund': str(amendment.fund_id), 'number': amendment.amendment_number,
    })
    amendment.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# -- SEBI Circulars --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def circular_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        qs = SEBICircular.objects.filter(organization=org).prefetch_related('actions')
        impact = request.query_params.get('impact_level')
        if impact:
            qs = qs.filter(impact_level=impact)
        is_superseded = request.query_params.get('is_superseded')
        if is_superseded is not None:
            qs = qs.filter(is_superseded=is_superseded.lower() == 'true')
        return Response(SEBICircularListSerializer(qs, many=True).data)

    ser = SEBICircularDetailSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    circular = ser.save(organization=org)
    log_audit(request, 'create', 'sebi_circular', circular.id, {
        'number': circular.circular_number, 'impact': circular.impact_level,
    })
    return Response(SEBICircularDetailSerializer(circular).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def circular_detail(request, circular_id):
    org = request.organization
    try:
        circular = SEBICircular.objects.prefetch_related('actions').get(
            pk=circular_id, organization=org,
        )
    except SEBICircular.DoesNotExist:
        return Response({'detail': 'SEBI circular not found.'}, status=404)

    if request.method == 'GET':
        return Response(SEBICircularDetailSerializer(circular).data)

    if request.method == 'PUT':
        ser = SEBICircularDetailSerializer(circular, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'sebi_circular', circular.id, {
            'fields': list(request.data.keys()),
        })
        return Response(SEBICircularDetailSerializer(circular).data)

    log_audit(request, 'delete', 'sebi_circular', circular.id, {
        'number': circular.circular_number,
    })
    circular.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# -- Circular Actions (nested under circular) --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def circular_action_list(request, circular_id):
    org = request.organization
    try:
        circular = SEBICircular.objects.get(pk=circular_id, organization=org)
    except SEBICircular.DoesNotExist:
        return Response({'detail': 'SEBI circular not found.'}, status=404)

    if request.method == 'GET':
        actions = circular.actions.select_related('fund', 'assigned_to').all()
        status_filter = request.query_params.get('status')
        if status_filter:
            actions = actions.filter(status=status_filter)
        return Response(CircularActionSerializer(actions, many=True).data)

    data = request.data.copy()
    data['circular'] = str(circular.id)
    ser = CircularActionSerializer(data=data)
    ser.is_valid(raise_exception=True)
    action = ser.save(circular=circular)
    log_audit(request, 'create', 'circular_action', action.id, {
        'circular': str(circular.id), 'title': action.action_title,
    })
    return Response(CircularActionSerializer(action).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT'])
@permission_classes([IsGPUser])
def circular_action_detail(request, action_id):
    org = request.organization
    try:
        action = CircularAction.objects.select_related(
            'circular', 'fund', 'assigned_to',
        ).get(pk=action_id, circular__organization=org)
    except CircularAction.DoesNotExist:
        return Response({'detail': 'Action not found.'}, status=404)

    if request.method == 'GET':
        return Response(CircularActionSerializer(action).data)

    ser = CircularActionSerializer(action, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    log_audit(request, 'update', 'circular_action', action.id, {
        'fields': list(request.data.keys()),
    })
    return Response(CircularActionSerializer(action).data)


# ═══════════════════════════════════════════════════════════════
# Compliance 2.0 — Portfolio Company-Level Obligations (v5)
# ═══════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsGPUser])
def portfolio_compliance_heatmap(request):
    """
    RAG heatmap: for each accessible portfolio company, returns
    obligation breakdown by RAG status + composite compliance score.

    Query params: ?fund_id=<uuid>
    """
    from .models import PortfolioCompanyCompliance, PortfolioComplianceScore
    from investments.models import PortfolioCompany

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)
    fund_filter = request.query_params.get('fund_id')

    companies = PortfolioCompany.objects.filter(
        organization=org,
        investments__scheme__fund__id__in=fund_ids,
        is_active=True,
    ).distinct()

    if fund_filter:
        companies = companies.filter(investments__scheme__fund__id=fund_filter)

    result = []
    for company in companies:
        obligations = PortfolioCompanyCompliance.objects.filter(portfolio_company=company)
        total = obligations.count()
        green  = obligations.filter(rag_status='green').count()
        amber  = obligations.filter(rag_status='amber').count()
        red    = obligations.filter(rag_status='red').count()
        overdue = obligations.filter(status='overdue').count()

        # Composite compliance score
        if total > 0:
            score = round((green / total) * 100, 1)
        else:
            score = None  # No data

        latest_score = PortfolioComplianceScore.objects.filter(
            portfolio_company=company
        ).order_by('-score_date').first()

        result.append({
            'company_id': str(company.id),
            'company_name': company.name,
            'sector': company.sector,
            'total_obligations': total,
            'green': green,
            'amber': amber,
            'red': red,
            'overdue_count': overdue,
            'compliance_score': float(latest_score.compliance_score) if latest_score else score,
            'rag': 'red' if red > 0 else ('amber' if amber > 0 else 'green'),
        })

    # Sort by risk: red first
    result.sort(key=lambda x: (
        0 if x['rag'] == 'red' else (1 if x['rag'] == 'amber' else 2),
        -x['overdue_count'],
    ))
    return Response(result)


@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def portfolio_obligation_list(request, company_id):
    """List or create compliance obligations for a portfolio company."""
    from .models import PortfolioCompanyCompliance
    from investments.models import PortfolioCompany

    org = request.organization
    try:
        company = PortfolioCompany.objects.get(pk=company_id, organization=org)
    except PortfolioCompany.DoesNotExist:
        return Response({'detail': 'Company not found.'}, status=404)

    if request.method == 'GET':
        qs = PortfolioCompanyCompliance.objects.filter(
            portfolio_company=company
        ).order_by('deadline')

        data = [
            {
                'id': str(ob.id),
                'obligation_type': ob.obligation_type,
                'obligation_name': ob.obligation_name,
                'deadline': str(ob.deadline),
                'status': ob.status,
                'rag_status': ob.rag_status,
                'filed_at': str(ob.filed_at) if ob.filed_at else None,
                'penalty_amount': float(ob.penalty_amount),
                'notes': ob.notes,
            }
            for ob in qs
        ]
        return Response(data)

    # POST: create obligation
    import datetime
    ob = PortfolioCompanyCompliance.objects.create(
        portfolio_company=company,
        obligation_type=request.data.get('obligation_type', 'other'),
        obligation_name=request.data.get('obligation_name', ''),
        deadline=request.data.get('deadline', datetime.date.today()),
        period_start=request.data.get('period_start'),
        period_end=request.data.get('period_end'),
        status=request.data.get('status', 'due'),
        notes=request.data.get('notes', ''),
    )
    log_audit(request, 'create', 'portfolio_obligation', ob.id, {
        'company': company.name, 'type': ob.obligation_type,
    })
    return Response({'id': str(ob.id)}, status=status.HTTP_201_CREATED)


@api_view(['PUT'])
@permission_classes([IsGPUser])
def portfolio_obligation_update(request, obligation_id):
    """Update a compliance obligation (e.g., mark as filed)."""
    from .models import PortfolioCompanyCompliance
    import datetime

    org = request.organization
    try:
        ob = PortfolioCompanyCompliance.objects.select_related(
            'portfolio_company'
        ).get(pk=obligation_id, portfolio_company__organization=org)
    except PortfolioCompanyCompliance.DoesNotExist:
        return Response({'detail': 'Obligation not found.'}, status=404)

    for field in ['status', 'filed_at', 'challan_no', 'reference_no', 'penalty_amount', 'notes']:
        if field in request.data:
            setattr(ob, field, request.data[field])
    ob.save()
    log_audit(request, 'update', 'portfolio_obligation', ob.id, {
        'fields': list(request.data.keys()),
    })
    return Response({'detail': 'Updated.', 'rag_status': ob.rag_status})


# ═══════════════════════════════════════════════════════════════
# Escalation Log + Combined Score (v5)
# ═══════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsGPUser])
def escalation_log_list(request):
    """
    List escalation log entries for this organization.
    Query params: ?resolved=false, ?escalation_type=equity_threshold_breach
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    qs = EscalationLog.objects.filter(organization=org).select_related(
        'equity_alert__investment', 'sebi_report__fund',
        'circular_action__circular', 'escalated_by',
    )
    if request.query_params.get('resolved') == 'false':
        qs = qs.filter(resolved=False)
    esc_type = request.query_params.get('escalation_type')
    if esc_type:
        qs = qs.filter(escalation_type=esc_type)

    data = [
        {
            'id': str(e.id),
            'escalation_type': e.escalation_type,
            'escalation_type_display': e.get_escalation_type_display(),
            'level': e.level,
            'escalated_to_role': e.escalated_to_role,
            'message': e.message,
            'resolved': e.resolved,
            'resolved_at': e.resolved_at,
            'created_at': e.created_at,
        }
        for e in qs[:100]
    ]
    return Response({'count': len(data), 'results': data})


@api_view(['POST'])
@permission_classes([IsGPUser])
def resolve_escalation(request, escalation_id):
    """Mark an escalation as resolved."""
    org = request.organization
    try:
        log = EscalationLog.objects.get(pk=escalation_id, organization=org)
    except EscalationLog.DoesNotExist:
        return Response({'detail': 'Escalation not found.'}, status=404)

    from django.utils import timezone
    log.resolved = True
    log.resolved_at = timezone.now()
    log.resolution_notes = request.data.get('resolution_notes', '')
    log.save(update_fields=['resolved', 'resolved_at', 'resolution_notes'])
    return Response({'detail': 'Escalation resolved.'})


@api_view(['POST'])
@permission_classes([IsGPUser])
def run_escalation_scan(request):
    """
    Trigger a full compliance scan for a fund — auto-escalates any new breaches.
    Body: { "fund_id": "<uuid>" }
    """
    from .escalation import ComplianceEscalationService
    from funds.models import Fund

    org = request.organization
    fund_id = request.data.get('fund_id')
    if not fund_id:
        return Response({'detail': 'fund_id required.'}, status=400)

    try:
        fund = Fund.objects.get(pk=fund_id, organization=org)
    except Fund.DoesNotExist:
        return Response({'detail': 'Fund not found.'}, status=404)

    svc = ComplianceEscalationService(org)
    svc.run_all(fund)
    return Response({'detail': 'Compliance scan complete.'})


@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def fund_compliance_score(request, fund_id):
    """
    GET: Latest combined compliance score for a fund.
    POST: Recompute and save the score now.
    """
    from .escalation import FundComplianceScorer
    from funds.models import Fund

    org = request.organization
    try:
        fund = Fund.objects.get(pk=fund_id, organization=org)
    except Fund.DoesNotExist:
        return Response({'detail': 'Fund not found.'}, status=404)

    if request.method == 'POST':
        scorer = FundComplianceScorer(fund, org)
        score = scorer.compute_and_save()
    else:
        score = FundComplianceScore.objects.filter(fund=fund).order_by('-score_date').first()
        if not score:
            return Response({'detail': 'No score computed yet. POST to compute.'}, status=404)

    return Response({
        'fund_id': str(fund.id),
        'fund_name': fund.name,
        'score_date': score.score_date,
        'combined_score': float(score.combined_score),
        'sub_scores': {
            'sebi_filings': float(score.sebi_filing_score),
            'aml': float(score.aml_score),
            'equity_threshold': float(score.equity_threshold_score),
            'portfolio_companies': float(score.portfolio_company_score),
            'circular_actions': float(score.circular_action_score),
        },
        'detail': score.score_detail,
        'computed_at': score.computed_at,
    })
