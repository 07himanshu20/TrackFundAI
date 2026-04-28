"""
Investments views — Phase 2 of TrackFundAI.

Covers all 18 pending Phase 2 endpoints:
  - Investments & Tranches (6)
  - Valuations (4) with approval workflow
  - Founder Portal / KPIs (5)
  - Exit Scenarios (2)
  - Board Pack Generation (1)
"""

import io
import json
from datetime import date

from django.db.models import Count, Max, Subquery, OuterRef
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.audit import log_audit
from accounts.fund_access_helpers import (
    user_has_fund_access, get_accessible_fund_ids, filter_by_fund_access,
)
from accounts.permissions import IsGPUser, IsGPAdmin
from funds.models import Scheme
from notifications.helpers import notify_user, notify_org_admins

from .models import (
    PortfolioCompany, Investment, InvestmentTranche, Valuation,
    KPIDefinition, PortfolioKPI, ExitEvent, BoardMeeting,
)
from .serializers import (
    PortfolioCompanySerializer, PortfolioCompanyListSerializer,
    InvestmentListSerializer, InvestmentDetailSerializer, InvestmentCreateSerializer,
    InvestmentTrancheSerializer,
    ValuationSerializer, ValuationCreateSerializer,
    KPIDefinitionSerializer, PortfolioKPISerializer, KPISubmitSerializer,
    ExitEventSerializer, ExitEventCreateSerializer,
    BoardMeetingSerializer,
)


# ═══════════════════════════════════════════════════════════════
# INVESTMENTS & TRANCHES (6 endpoints)
# ═══════════════════════════════════════════════════════════════

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def investment_list(request, scheme_id):
    """
    GET  /api/schemes/{id}/investments/  -> list investments under scheme
    POST /api/schemes/{id}/investments/  -> create investment
    """
    org = request.organization
    try:
        scheme = Scheme.objects.select_related('fund').get(
            pk=scheme_id, fund__organization=org,
        )
    except Scheme.DoesNotExist:
        return Response({'detail': 'Scheme not found.'}, status=404)

    if not user_has_fund_access(request.user, scheme.fund):
        return Response({'detail': 'Scheme not found.'}, status=404)

    if request.method == 'GET':
        # Annotate with tranche count and latest valuation fair_value
        latest_val = Valuation.objects.filter(
            investment=OuterRef('pk'), status='approved',
        ).order_by('-valuation_date').values('fair_value')[:1]

        investments = (
            scheme.investments
            .annotate(
                tranche_count=Count('tranches'),
                latest_valuation=Subquery(latest_val),
            )
        )
        return Response(InvestmentListSerializer(investments, many=True).data)

    # POST — create
    if not request.user.is_admin:
        return Response({'detail': 'Only admins can create investments.'}, status=403)

    ser = InvestmentCreateSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    inv = ser.save(scheme=scheme, created_by=request.user)

    log_audit(request, 'create', 'investment', inv.id, {
        'company': inv.company_name, 'scheme': str(scheme.id),
    })
    notify_org_admins(
        org, 'New Investment Recorded',
        f'{inv.company_name} added to {scheme.name} by {request.user.username}.',
        category='investment', resource_type='investment', resource_id=inv.id,
        created_by=request.user, exclude_user=request.user,
    )

    return Response(InvestmentDetailSerializer(inv).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT'])
@permission_classes([IsGPUser])
def investment_detail(request, investment_id):
    """
    GET  /api/investments/{id}/  -> investment detail with tranches
    PUT  /api/investments/{id}/  -> update investment
    """
    org = request.organization
    try:
        inv = Investment.objects.select_related('scheme__fund').get(
            pk=investment_id, scheme__fund__organization=org,
        )
    except Investment.DoesNotExist:
        return Response({'detail': 'Investment not found.'}, status=404)

    if not user_has_fund_access(request.user, inv.scheme.fund):
        return Response({'detail': 'Investment not found.'}, status=404)

    if request.method == 'GET':
        return Response(InvestmentDetailSerializer(inv).data)

    # PUT
    if not request.user.is_admin:
        return Response({'detail': 'Only admins can update investments.'}, status=403)

    ser = InvestmentCreateSerializer(inv, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    log_audit(request, 'update', 'investment', inv.id, {
        'company': inv.company_name, 'fields': list(request.data.keys()),
    })
    return Response(InvestmentDetailSerializer(inv).data)


@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def tranche_list(request, investment_id):
    """
    GET  /api/investments/{id}/tranches/  -> list tranches
    POST /api/investments/{id}/tranches/  -> add tranche
    """
    org = request.organization
    try:
        inv = Investment.objects.select_related('scheme__fund').get(
            pk=investment_id, scheme__fund__organization=org,
        )
    except Investment.DoesNotExist:
        return Response({'detail': 'Investment not found.'}, status=404)

    if not user_has_fund_access(request.user, inv.scheme.fund):
        return Response({'detail': 'Investment not found.'}, status=404)

    if request.method == 'GET':
        return Response(InvestmentTrancheSerializer(inv.tranches.all(), many=True).data)

    # POST — add tranche
    if not request.user.is_admin:
        return Response({'detail': 'Only admins can add tranches.'}, status=403)

    ser = InvestmentTrancheSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    tranche = ser.save(investment=inv)

    # Update total_invested on the investment
    from django.db.models import Sum
    total = inv.tranches.aggregate(t=Sum('amount'))['t'] or 0
    inv.total_invested = total
    inv.save(update_fields=['total_invested'])

    log_audit(request, 'create', 'tranche', tranche.id, {
        'company': inv.company_name, 'amount': str(tranche.amount),
        'tranche': tranche.tranche_number,
    })

    return Response(InvestmentTrancheSerializer(tranche).data, status=status.HTTP_201_CREATED)


# ═══════════════════════════════════════════════════════════════
# VALUATIONS (4 endpoints)
# ═══════════════════════════════════════════════════════════════

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def valuation_list(request, investment_id):
    """
    GET  /api/investments/{id}/valuations/  -> valuation history
    POST /api/investments/{id}/valuations/  -> submit valuation
    """
    org = request.organization
    try:
        inv = Investment.objects.select_related('scheme__fund').get(
            pk=investment_id, scheme__fund__organization=org,
        )
    except Investment.DoesNotExist:
        return Response({'detail': 'Investment not found.'}, status=404)

    if not user_has_fund_access(request.user, inv.scheme.fund):
        return Response({'detail': 'Investment not found.'}, status=404)

    if request.method == 'GET':
        return Response(ValuationSerializer(inv.valuations.all(), many=True).data)

    # POST — submit valuation
    ser = ValuationCreateSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    val = ser.save(investment=inv, submitted_by=request.user, status='submitted')

    log_audit(request, 'create', 'valuation', val.id, {
        'company': inv.company_name, 'fair_value': str(val.fair_value),
        'methodology': val.methodology,
    })

    return Response(ValuationSerializer(val).data, status=status.HTTP_201_CREATED)


@api_view(['PUT'])
@permission_classes([IsGPUser])
def valuation_update(request, valuation_id):
    """
    PUT  /api/valuations/{id}/  -> update valuation (only if draft/submitted)
    """
    org = request.organization
    try:
        val = Valuation.objects.select_related('investment__scheme__fund').get(
            pk=valuation_id, investment__scheme__fund__organization=org,
        )
    except Valuation.DoesNotExist:
        return Response({'detail': 'Valuation not found.'}, status=404)

    if not user_has_fund_access(request.user, val.investment.scheme.fund):
        return Response({'detail': 'Valuation not found.'}, status=404)

    if val.status == 'approved':
        return Response({'detail': 'Cannot edit an approved valuation.'}, status=400)

    ser = ValuationCreateSerializer(val, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()

    log_audit(request, 'update', 'valuation', val.id, {
        'company': val.investment.company_name,
        'fields': list(request.data.keys()),
    })
    return Response(ValuationSerializer(val).data)


@api_view(['POST'])
@permission_classes([IsGPAdmin])
def valuation_approve(request, valuation_id):
    """
    POST /api/valuations/{id}/approve/  -> approve or reject valuation
    Body: {"action": "approve"} or {"action": "reject", "reason": "..."}
    """
    org = request.organization
    try:
        val = Valuation.objects.select_related('investment__scheme__fund').get(
            pk=valuation_id, investment__scheme__fund__organization=org,
        )
    except Valuation.DoesNotExist:
        return Response({'detail': 'Valuation not found.'}, status=404)

    if not user_has_fund_access(request.user, val.investment.scheme.fund):
        return Response({'detail': 'Valuation not found.'}, status=404)

    if val.status not in ('submitted', 'draft'):
        return Response({'detail': f'Cannot approve a valuation with status "{val.status}".'}, status=400)

    action = request.data.get('action', 'approve')
    if action == 'approve':
        val.status = 'approved'
        val.approved_by = request.user
        val.approved_at = timezone.now()
        val.save(update_fields=['status', 'approved_by', 'approved_at'])
        log_audit(request, 'update', 'valuation', val.id, {
            'action': 'approved', 'company': val.investment.company_name,
        })
        if val.submitted_by:
            notify_user(
                val.submitted_by, 'Valuation Approved',
                f'Your {val.get_methodology_display()} valuation for {val.investment.company_name} '
                f'({val.valuation_date}) has been approved.',
                category='investment', resource_type='valuation', resource_id=val.id,
                created_by=request.user,
            )
    elif action == 'reject':
        val.status = 'rejected'
        val.save(update_fields=['status'])
        log_audit(request, 'update', 'valuation', val.id, {
            'action': 'rejected', 'company': val.investment.company_name,
            'reason': request.data.get('reason', ''),
        })
        if val.submitted_by:
            notify_user(
                val.submitted_by, 'Valuation Rejected',
                f'Your valuation for {val.investment.company_name} was rejected. '
                f'Reason: {request.data.get("reason", "No reason given")}',
                category='investment', resource_type='valuation', resource_id=val.id,
                created_by=request.user,
            )
    else:
        return Response({'detail': 'action must be "approve" or "reject".'}, status=400)

    return Response(ValuationSerializer(val).data)


# ═══════════════════════════════════════════════════════════════
# FOUNDER PORTAL / KPI (5 endpoints)
# ═══════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def founder_companies(request):
    """
    GET /api/founder/companies/  -> founder's companies
    Returns investments linked to companies the founder user manages.
    For founders: filtered by user's linked investments.
    For GP users: returns all investments in their org.
    """
    user = request.user
    org = request.organization

    fund_ids = get_accessible_fund_ids(user)

    if user.role == 'founder_user':
        # Founders see only investments where they are the point of contact
        investments = Investment.objects.filter(
            scheme__fund__organization=org,
            scheme__fund__id__in=fund_ids,
            created_by=user,
        ).select_related('scheme__fund')
    else:
        # GP users see investments in funds they have access to
        investments = Investment.objects.filter(
            scheme__fund__organization=org,
            scheme__fund__id__in=fund_ids,
        ).select_related('scheme__fund')

    return Response(InvestmentListSerializer(
        investments.annotate(
            tranche_count=Count('tranches'),
            latest_valuation=Subquery(
                Valuation.objects.filter(
                    investment=OuterRef('pk'), status='approved',
                ).order_by('-valuation_date').values('fair_value')[:1]
            ),
        ), many=True,
    ).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def founder_submit_kpi(request, investment_id):
    """
    POST /api/founder/companies/{id}/submit-kpi/  -> submit monthly KPIs
    Body: {"period": "2025-04-01", "values": [{"kpi_definition_id": "...", "value": 123, "notes": "..."}]}
    """
    org = request.organization
    try:
        inv = Investment.objects.select_related('scheme__fund').get(
            pk=investment_id, scheme__fund__organization=org,
        )
    except Investment.DoesNotExist:
        return Response({'detail': 'Investment not found.'}, status=404)

    if not user_has_fund_access(request.user, inv.scheme.fund):
        return Response({'detail': 'Investment not found.'}, status=404)

    ser = KPISubmitSerializer(data=request.data)
    ser.is_valid(raise_exception=True)

    period = ser.validated_data['period']
    values = ser.validated_data['values']
    now = timezone.now()

    created = []
    for item in values:
        kpi_def_id = item.get('kpi_definition_id')
        if not kpi_def_id:
            continue
        try:
            kpi_def = KPIDefinition.objects.get(pk=kpi_def_id, organization=org)
        except KPIDefinition.DoesNotExist:
            continue

        kpi, _ = PortfolioKPI.objects.update_or_create(
            investment=inv,
            kpi_definition=kpi_def,
            period=period,
            defaults={
                'value': item.get('value', 0),
                'notes': item.get('notes', ''),
                'status': 'submitted',
                'submitted_by': request.user,
                'submitted_at': now,
            },
        )
        created.append(kpi)

    log_audit(request, 'create', 'portfolio_kpi', str(inv.id), {
        'company': inv.company_name, 'period': str(period),
        'kpi_count': len(created),
    })

    # Notify GP admins about new KPI submission
    notify_org_admins(
        org, 'KPIs Submitted',
        f'{request.user.username} submitted {len(created)} KPIs for '
        f'{inv.company_name} ({period}).',
        category='investment', resource_type='portfolio_kpi',
        resource_id=inv.id, created_by=request.user,
    )

    return Response({
        'detail': f'{len(created)} KPIs submitted for {period}.',
        'kpis': PortfolioKPISerializer(created, many=True).data,
    }, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def founder_kpi_history(request, investment_id):
    """
    GET /api/founder/companies/{id}/kpi-history/  -> KPI history for a company
    """
    org = request.organization
    try:
        inv = Investment.objects.select_related('scheme__fund').get(
            pk=investment_id, scheme__fund__organization=org,
        )
    except Investment.DoesNotExist:
        return Response({'detail': 'Investment not found.'}, status=404)

    if not user_has_fund_access(request.user, inv.scheme.fund):
        return Response({'detail': 'Investment not found.'}, status=404)

    kpis = inv.kpis.select_related('kpi_definition').all()

    # Optional period filter
    period_from = request.query_params.get('from')
    period_to = request.query_params.get('to')
    if period_from:
        kpis = kpis.filter(period__gte=period_from)
    if period_to:
        kpis = kpis.filter(period__lte=period_to)

    return Response(PortfolioKPISerializer(kpis, many=True).data)


@api_view(['GET'])
@permission_classes([IsGPUser])
def investment_kpis(request, investment_id):
    """
    GET /api/investments/{id}/kpis/  -> KPI submissions (GP view)
    Same data as founder_kpi_history but requires GP role.
    """
    org = request.organization
    try:
        inv = Investment.objects.select_related('scheme__fund').get(
            pk=investment_id, scheme__fund__organization=org,
        )
    except Investment.DoesNotExist:
        return Response({'detail': 'Investment not found.'}, status=404)

    if not user_has_fund_access(request.user, inv.scheme.fund):
        return Response({'detail': 'Investment not found.'}, status=404)

    kpis = inv.kpis.select_related('kpi_definition').all()

    # Optional status filter
    kpi_status = request.query_params.get('status')
    if kpi_status:
        kpis = kpis.filter(status=kpi_status)

    return Response(PortfolioKPISerializer(kpis, many=True).data)


@api_view(['PUT'])
@permission_classes([IsGPAdmin])
def kpi_review(request, kpi_id):
    """
    PUT /api/kpis/{id}/review/  -> review/approve KPI
    Body: {"action": "approve"} or {"action": "reject", "reason": "..."}
    """
    org = request.organization
    try:
        kpi = PortfolioKPI.objects.select_related(
            'investment__scheme__fund', 'kpi_definition',
        ).get(
            pk=kpi_id, investment__scheme__fund__organization=org,
        )
    except PortfolioKPI.DoesNotExist:
        return Response({'detail': 'KPI not found.'}, status=404)

    if not user_has_fund_access(request.user, kpi.investment.scheme.fund):
        return Response({'detail': 'KPI not found.'}, status=404)

    action = request.data.get('action', 'approve')
    now = timezone.now()

    if action == 'approve':
        kpi.status = 'approved'
        kpi.reviewed_by = request.user
        kpi.reviewed_at = now
        kpi.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])
        log_audit(request, 'update', 'portfolio_kpi', kpi.id, {
            'action': 'approved', 'company': kpi.investment.company_name,
            'kpi': kpi.kpi_definition.name,
        })
    elif action == 'reject':
        kpi.status = 'rejected'
        kpi.reviewed_by = request.user
        kpi.reviewed_at = now
        kpi.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])
        log_audit(request, 'update', 'portfolio_kpi', kpi.id, {
            'action': 'rejected', 'company': kpi.investment.company_name,
            'kpi': kpi.kpi_definition.name,
            'reason': request.data.get('reason', ''),
        })
    else:
        return Response({'detail': 'action must be "approve" or "reject".'}, status=400)

    return Response(PortfolioKPISerializer(kpi).data)


# ═══════════════════════════════════════════════════════════════
# EXIT SCENARIOS (2 endpoints)
# ═══════════════════════════════════════════════════════════════

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def exit_scenario_list(request, investment_id):
    """
    GET  /api/investments/{id}/exit-scenarios/  -> list scenarios
    POST /api/investments/{id}/exit-scenarios/  -> model new scenario
    """
    org = request.organization
    try:
        inv = Investment.objects.select_related('scheme__fund').get(
            pk=investment_id, scheme__fund__organization=org,
        )
    except Investment.DoesNotExist:
        return Response({'detail': 'Investment not found.'}, status=404)

    if not user_has_fund_access(request.user, inv.scheme.fund):
        return Response({'detail': 'Investment not found.'}, status=404)

    if request.method == 'GET':
        return Response(ExitEventSerializer(inv.exit_scenarios.all(), many=True).data)

    # POST — model new scenario
    ser = ExitEventCreateSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    exit_ev = ser.save(investment=inv, created_by=request.user)

    # If it is an actual exit, update investment status
    if exit_ev.is_actual:
        inv.status = 'fully_exited'
        inv.save(update_fields=['status'])

    log_audit(request, 'create', 'exit_event', exit_ev.id, {
        'company': inv.company_name, 'exit_type': exit_ev.exit_type,
        'is_actual': exit_ev.is_actual,
    })

    return Response(ExitEventSerializer(exit_ev).data, status=status.HTTP_201_CREATED)


# ═══════════════════════════════════════════════════════════════
# BOARD MEETINGS (list + create per investment)
# ═══════════════════════════════════════════════════════════════

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def board_meeting_list(request, investment_id):
    """
    GET  /api/investments/{id}/board-meetings/  -> list board meetings
    POST /api/investments/{id}/board-meetings/  -> create board meeting
    """
    org = request.organization
    try:
        inv = Investment.objects.select_related('scheme__fund').get(
            pk=investment_id, scheme__fund__organization=org,
        )
    except Investment.DoesNotExist:
        return Response({'detail': 'Investment not found.'}, status=404)

    if not user_has_fund_access(request.user, inv.scheme.fund):
        return Response({'detail': 'Investment not found.'}, status=404)

    if request.method == 'GET':
        return Response(BoardMeetingSerializer(inv.board_meetings.all(), many=True).data)

    # POST — create
    ser = BoardMeetingSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    meeting = ser.save(investment=inv, created_by=request.user)

    log_audit(request, 'create', 'board_meeting', meeting.id, {
        'company': inv.company_name, 'date': str(meeting.meeting_date),
    })

    return Response(BoardMeetingSerializer(meeting).data, status=status.HTTP_201_CREATED)


# ═══════════════════════════════════════════════════════════════
# BOARD PACK GENERATION (1 endpoint)
# ═══════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([IsGPAdmin])
def board_pack_generate(request, scheme_id):
    """
    POST /api/schemes/{id}/board-pack/generate/  -> auto-generate board pack
    Generates a JSON summary of all investments under the scheme.
    In production this would render to PDF; for now returns structured data.
    """
    org = request.organization
    try:
        scheme = Scheme.objects.select_related('fund').get(
            pk=scheme_id, fund__organization=org,
        )
    except Scheme.DoesNotExist:
        return Response({'detail': 'Scheme not found.'}, status=404)

    if not user_has_fund_access(request.user, scheme.fund):
        return Response({'detail': 'Scheme not found.'}, status=404)

    investments = scheme.investments.prefetch_related(
        'tranches', 'valuations', 'exit_scenarios', 'board_meetings',
    ).all()

    pack_data = {
        'scheme': scheme.name,
        'fund': scheme.fund.name,
        'generated_at': timezone.now().isoformat(),
        'generated_by': request.user.username,
        'investments': [],
    }

    for inv in investments:
        latest_val = inv.valuations.filter(status='approved').order_by('-valuation_date').first()
        latest_board = inv.board_meetings.order_by('-meeting_date').first()

        inv_data = {
            'company': inv.company_name,
            'instrument': inv.get_instrument_type_display(),
            'status': inv.get_status_display(),
            'ownership_pct': str(inv.ownership_pct) if inv.ownership_pct else None,
            'total_invested': str(inv.total_invested),
            'currency': inv.currency,
            'tranches': inv.tranches.count(),
            'latest_valuation': {
                'date': str(latest_val.valuation_date) if latest_val else None,
                'fair_value': str(latest_val.fair_value) if latest_val else None,
                'methodology': latest_val.get_methodology_display() if latest_val else None,
                'multiple': str(latest_val.multiple) if latest_val and latest_val.multiple else None,
            },
            'exit_scenarios': [
                {
                    'type': e.get_exit_type_display(),
                    'is_actual': e.is_actual,
                    'proceeds': str(e.proceeds) if e.proceeds else None,
                    'moic': str(e.moic) if e.moic else None,
                }
                for e in inv.exit_scenarios.all()
            ],
            'last_board_meeting': {
                'date': str(latest_board.meeting_date) if latest_board else None,
                'resolutions': latest_board.resolutions if latest_board else [],
            },
        }
        pack_data['investments'].append(inv_data)

    log_audit(request, 'export', 'board_pack', str(scheme.id), {
        'scheme': scheme.name, 'investment_count': len(pack_data['investments']),
    })

    # Return as JSON (PDF rendering to be added in Phase 6)
    return Response(pack_data)


# ═══════════════════════════════════════════════════════════════
# PORTFOLIO COMPANY CRUD
# ═══════════════════════════════════════════════════════════════

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def portfolio_company_list(request):
    """List all portfolio companies for the org, or create one."""
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        fund_ids = get_accessible_fund_ids(request.user)
        # Only show companies that have investments in funds user can access
        qs = PortfolioCompany.objects.filter(
            organization=org,
            investments__scheme__fund__id__in=fund_ids,
        ).distinct()
        sector = request.query_params.get('sector')
        if sector:
            qs = qs.filter(sector__iexact=sector)
        active = request.query_params.get('active')
        if active is not None:
            qs = qs.filter(is_active=active.lower() == 'true')
        return Response(PortfolioCompanyListSerializer(qs, many=True).data)

    ser = PortfolioCompanySerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    company = ser.save(organization=org)
    log_audit(request, 'create', 'portfolio_company', company.id, {
        'name': company.name, 'sector': company.sector,
    })
    return Response(PortfolioCompanySerializer(company).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def portfolio_company_detail(request, company_id):
    """Get, update, or delete a portfolio company."""
    org = request.organization
    try:
        company = PortfolioCompany.objects.get(pk=company_id, organization=org)
    except PortfolioCompany.DoesNotExist:
        return Response({'detail': 'Portfolio company not found.'}, status=404)

    if request.method == 'GET':
        return Response(PortfolioCompanySerializer(company).data)

    if request.method == 'PUT':
        ser = PortfolioCompanySerializer(company, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'portfolio_company', company.id, {
            'name': company.name, 'fields': list(request.data.keys()),
        })
        return Response(PortfolioCompanySerializer(company).data)

    log_audit(request, 'delete', 'portfolio_company', company.id, {
        'name': company.name,
    })
    company.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ═══════════════════════════════════════════════════════════════
# KPI DEFINITION CRUD
# ═══════════════════════════════════════════════════════════════

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def kpi_definition_list(request):
    """List or create KPI definitions for the org."""
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        qs = KPIDefinition.objects.filter(organization=org)
        return Response(KPIDefinitionSerializer(qs, many=True).data)

    if not request.user.is_admin:
        return Response({'detail': 'Only admins can create KPI definitions.'}, status=403)

    ser = KPIDefinitionSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    kpi_def = ser.save(organization=org)
    log_audit(request, 'create', 'kpi_definition', kpi_def.id, {
        'name': kpi_def.name,
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def kpi_definition_detail(request, kpi_def_id):
    """Get, update, or delete a KPI definition."""
    org = request.organization
    try:
        kpi_def = KPIDefinition.objects.get(pk=kpi_def_id, organization=org)
    except KPIDefinition.DoesNotExist:
        return Response({'detail': 'KPI definition not found.'}, status=404)

    if request.method == 'GET':
        return Response(KPIDefinitionSerializer(kpi_def).data)

    if not request.user.is_admin:
        return Response({'detail': 'Only admins can modify KPI definitions.'}, status=403)

    if request.method == 'PUT':
        ser = KPIDefinitionSerializer(kpi_def, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'kpi_definition', kpi_def.id, {
            'name': kpi_def.name, 'fields': list(request.data.keys()),
        })
        return Response(ser.data)

    log_audit(request, 'delete', 'kpi_definition', kpi_def.id)
    kpi_def.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)
