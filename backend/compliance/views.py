from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.audit import log_audit
from accounts.fund_access_helpers import get_accessible_fund_ids, user_has_fund_access
from accounts.permissions import IsGPUser
from .models import (
    SEBIReport, AMLDueDiligence, ComplianceTestReport,
    CTRChecklistItem, EquityThresholdAlert, ComplianceCalendar,
    PPMAmendment, SEBICircular, CircularAction,
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

@api_view(['GET'])
@permission_classes([IsGPUser])
def threshold_alert_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)
    qs = EquityThresholdAlert.objects.filter(
        investment__scheme__fund__organization=org,
        investment__scheme__fund__id__in=fund_ids,
    ).select_related('investment')
    unresolved = request.query_params.get('unresolved')
    if unresolved is not None and unresolved.lower() == 'true':
        qs = qs.filter(resolved=False)
    return Response(EquityThresholdAlertSerializer(qs, many=True).data)


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
