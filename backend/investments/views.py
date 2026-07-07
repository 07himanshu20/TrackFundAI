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
from django.db.models.functions import Coalesce
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
from config.cache_utils import cached_api_view, invalidate_fund_cache
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
        # Annotate with tranche count and latest valuation.
        # Prefer fair_value_of_holding (the fund's share) over fair_value
        # (whole-company equity) — see Rule 22 in the Phase 2 extractor.
        latest_val = Valuation.objects.filter(
            investment=OuterRef('pk'), status='approved',
        ).order_by('-valuation_date').annotate(
            holding_or_equity=Coalesce('fair_value_of_holding', 'fair_value')
        ).values('holding_or_equity')[:1]

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
                ).order_by('-valuation_date').annotate(
                    holding_or_equity=Coalesce('fair_value_of_holding', 'fair_value')
                ).values('holding_or_equity')[:1]
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
# PORTFOLIO FUND-LEVEL ANALYTICS (Burn, Exits, KPIs, SaaS, Quoted)
# ═══════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def portfolio_burn_runway(request):
    """
    GET /api/portfolio/burn-runway/?fund=<id>
    Returns latest burn/cash/runway per company for the selected fund.

    Primary source: CompanyFinancials (if populated from a dedicated burn sheet).
    Fallback: derives burn metrics from BudgetVsActual (P&L import) when no
    CompanyFinancials records exist:
      gross_burn  = latest total_opex (total operating expenses per month)
      net_burn    = max(0, -EBITDA) i.e. only when EBITDA is negative (losing cash)
      cash_balance = latest cash_and_equivalents from Balance Sheet import
      runway_months = cash_balance / gross_burn (if gross_burn > 0)
    """
    from decimal import Decimal
    from django.db.models import Max
    from .models import CompanyFinancials

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_id = request.query_params.get('fund')
    qs = CompanyFinancials.objects.filter(
        investment__scheme__fund__organization=org,
    ).select_related('investment', 'portfolio_company')
    if fund_id:
        qs = qs.filter(investment__scheme__fund__id=fund_id)

    # Get latest period per investment
    latest_per_inv = {}
    for cf in qs.order_by('investment_id', '-period'):
        inv_id = str(cf.investment_id)
        if inv_id not in latest_per_inv:
            latest_per_inv[inv_id] = cf

    companies = []
    total_cash = Decimal('0')
    total_gross = Decimal('0')
    total_net = Decimal('0')
    total_runway = Decimal('0')
    runway_count = 0

    if latest_per_inv:
        for cf in latest_per_inv.values():
            row = {
                'company_name': cf.investment.company_name,
                'period': str(cf.period),
                'gross_burn': float(cf.gross_burn) if cf.gross_burn is not None else None,
                'net_burn': float(cf.net_burn) if cf.net_burn is not None else None,
                'cash_balance': float(cf.cash_balance) if cf.cash_balance is not None else None,
                'runway_months': float(cf.runway_months) if cf.runway_months is not None else None,
            }
            companies.append(row)
            if cf.gross_burn:
                total_gross += cf.gross_burn
            if cf.net_burn:
                total_net += cf.net_burn
            if cf.cash_balance:
                total_cash += cf.cash_balance
            if cf.runway_months:
                total_runway += cf.runway_months
                runway_count += 1
    else:
        # Fallback: derive from BudgetVsActual (P&L + Balance Sheet imports)
        try:
            from mis_consolidation.models import BudgetVsActual
            from django.db.models import Q

            bva_filter = Q(portfolio_company__organization=org)
            if fund_id:
                bva_filter &= Q(fund_id=fund_id)

            # For each company, get the latest monthly P&L records
            bva_qs = BudgetVsActual.objects.filter(
                bva_filter,
                period_month__isnull=False,
                line_item__in=['total_opex', 'ebitda', 'cash_and_equivalents'],
            ).select_related('portfolio_company')

            # Group by company → {line_item: (year, month, value)}
            from collections import defaultdict
            co_data = defaultdict(dict)
            for rec in bva_qs.order_by('portfolio_company_id', '-period_year', '-period_month'):
                co_name = rec.portfolio_company.name
                li = rec.line_item
                # Only keep the latest period per line_item per company
                if li not in co_data[co_name]:
                    co_data[co_name][li] = {
                        'value': rec.actual_inr,
                        'period_year': rec.period_year,
                        'period_month': rec.period_month,
                    }

            for co_name, metrics in co_data.items():
                opex_info   = metrics.get('total_opex')
                ebitda_info = metrics.get('ebitda')
                cash_info   = metrics.get('cash_and_equivalents')

                gross_burn  = float(opex_info['value']) if opex_info and opex_info['value'] else None
                ebitda_val  = float(ebitda_info['value']) if ebitda_info and ebitda_info['value'] else None
                # Net burn = monthly cash loss; only meaningful when EBITDA < 0
                net_burn    = (-ebitda_val) if (ebitda_val is not None and ebitda_val < 0) else None
                cash        = float(cash_info['value']) if cash_info and cash_info['value'] else None

                # Runway: cash / monthly gross burn
                runway = None
                if cash and gross_burn and gross_burn > 0:
                    runway = cash / gross_burn

                # Latest period label
                ref = opex_info or ebitda_info or cash_info
                period_label = ''
                if ref:
                    mo = ref['period_month']
                    yr = ref['period_year']
                    mon_names = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                                 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
                    period_label = f'{mon_names[mo] if 1 <= mo <= 12 else mo}-{yr}'

                companies.append({
                    'company_name': co_name,
                    'period': period_label,
                    'gross_burn': gross_burn,
                    'net_burn': net_burn,
                    'cash_balance': cash,
                    'runway_months': runway,
                })

                if gross_burn:
                    total_gross += Decimal(str(gross_burn))
                if net_burn:
                    total_net += Decimal(str(net_burn))
                if cash:
                    total_cash += Decimal(str(cash))
                if runway:
                    total_runway += Decimal(str(runway))
                    runway_count += 1
        except Exception:
            pass

    n = len(companies)
    return Response({
        'companies': sorted(companies, key=lambda x: x.get('gross_burn') or 0, reverse=True),
        'avg_gross_burn': float(total_gross / n) if n else None,
        'avg_net_burn': float(total_net / n) if n else None,
        'total_cash': float(total_cash),
        'avg_runway': float(total_runway / runway_count) if runway_count else None,
    })


@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def portfolio_exits_summary(request):
    """
    GET /api/portfolio/exits/?fund=<id>
    Returns all exit events for the selected fund with summary metrics.
    """
    from decimal import Decimal

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_id = request.query_params.get('fund')
    qs = ExitEvent.objects.filter(
        investment__scheme__fund__organization=org,
    ).select_related('investment', 'investment__portfolio_company',
                     'investment__scheme')
    if fund_id:
        qs = qs.filter(investment__scheme__fund__id=fund_id)

    # Build investment cost map for DPI calc
    inv_ids = qs.values_list('investment_id', flat=True).distinct()
    cost_map = {
        str(inv.id): inv.total_invested
        for inv in Investment.objects.filter(id__in=inv_ids)
    }

    exits = []
    total_proceeds = Decimal('0')
    total_cost = Decimal('0')
    moic_sum = Decimal('0')
    irr_sum = Decimal('0')
    net_irr_sum = Decimal('0')
    moic_count = irr_count = net_irr_count = 0

    for e in qs.order_by('-exit_date'):
        cost = cost_map.get(str(e.investment_id), Decimal('0'))
        exits.append({
            'company_name': e.investment.company_name,
            'sector': e.investment.portfolio_company.sector if e.investment.portfolio_company else '',
            'exit_type': e.exit_type,
            'exit_type_display': e.get_exit_type_display(),
            'is_actual': e.is_actual,
            'exit_date': str(e.exit_date) if e.exit_date else None,
            'cost': float(cost),
            'proceeds': float(e.proceeds) if e.proceeds is not None else None,
            'moic': float(e.moic) if e.moic is not None else None,
            'irr_pct': float(e.irr_pct) if e.irr_pct is not None else None,
            'net_irr_pct': float(e.irr_on_exit) if e.irr_on_exit is not None else None,
            'gain_loss_nature': e.gain_loss_nature,
            'buyer_name': e.buyer_name,
        })
        if e.proceeds:
            total_proceeds += e.proceeds
        if cost:
            total_cost += cost
        if e.moic:
            moic_sum += e.moic
            moic_count += 1
        if e.irr_pct:
            irr_sum += e.irr_pct
            irr_count += 1
        if e.irr_on_exit:
            net_irr_sum += e.irr_on_exit
            net_irr_count += 1

    # Only compute DPI from actual exits
    actual_proceeds = sum(
        (e['proceeds'] or 0) for e in exits if e.get('is_actual')
    )

    return Response({
        'exits': exits,
        'summary': {
            'total_exits': len(exits),
            'total_proceeds': float(total_proceeds),
            'avg_moic': float(moic_sum / moic_count) if moic_count else None,
            'avg_irr': float(irr_sum / irr_count) if irr_count else None,
            'avg_net_irr': float(net_irr_sum / net_irr_count) if net_irr_count else None,
            'dpi': float(actual_proceeds / float(total_cost)) if total_cost else None,
        },
    })


@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def portfolio_kpis_summary(request):
    """
    GET /api/portfolio/kpis/?fund=<id>
    Returns all KPI values for companies in the selected fund.
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_id = request.query_params.get('fund')
    qs = PortfolioKPI.objects.filter(
        investment__scheme__fund__organization=org,
    ).select_related('investment', 'kpi_definition')
    if fund_id:
        qs = qs.filter(investment__scheme__fund__id=fund_id)

    kpis = []
    for k in qs.order_by('investment__company_name', 'kpi_definition__name', '-period')[:500]:
        kpis.append({
            'company_name': k.investment.company_name,
            'kpi_name': k.kpi_definition.name,
            'kpi_slug': k.kpi_definition.slug,
            'format': k.kpi_definition.format,
            'sector_template': k.kpi_definition.sector_template,
            'period': str(k.period),
            'value': float(k.value),
        })

    return Response({'kpis': kpis, 'count': len(kpis)})


@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def portfolio_saas_metrics(request):
    """
    GET /api/portfolio/saas-metrics/?fund=<id>
    Returns SaaS-specific KPIs (MRR, ARR, NRR, Churn, CAC, LTV) per company.
    """
    # Slug variants accepted per canonical SaaS metric. Includes BOTH
    # hyphen and underscore forms because the KPI persister slugifies
    # multi-word field names with an underscore (`churn_rate`,
    # `ltv_cac_ratio`, `gross_margin_pct`) while some human-authored
    # KPIDefinition rows use hyphens (`churn-rate`, `ltv-cac`). Without
    # the underscore variants the persisted burn_runway data never
    # matches this whitelist and the dashboard stays blank.
    SAAS_SLUGS = {
        'mrr':           ('mrr', 'monthly-recurring-revenue', 'monthly-revenue'),
        'arr':           ('arr', 'annual-recurring-revenue', 'annual-revenue-run-rate'),
        'churn_rate':    ('churn-rate', 'churn_rate', 'churn', 'revenue-churn',
                          'customer-churn', 'monthly-churn'),
        'nrr':           ('nrr', 'net-revenue-retention', 'net-dollar-retention',
                          'ndr', 'net-retention'),
        'cac':           ('cac', 'customer-acquisition-cost', 'blended-cac'),
        'ltv':           ('ltv', 'clv', 'customer-ltv', 'lifetime-value',
                          'customer-lifetime-value'),
        'ltv_cac_ratio': ('ltv-cac', 'ltv_cac_ratio', 'ltv-cac-ratio',
                          'ltv-cac-multiple'),
    }
    all_slugs = [s for slugs in SAAS_SLUGS.values() for s in slugs]

    # Reverse map: any slug variant → canonical key (e.g. 'churn-rate' → 'churn_rate')
    # Without this, 'churn-rate' is stored under key 'churn-rate' but JS reads c.churn_rate
    # — they are different property names; values always showed as undefined (—).
    slug_to_canonical = {}
    for canonical, slugs in SAAS_SLUGS.items():
        for s in slugs:
            slug_to_canonical[s] = canonical

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_id = request.query_params.get('fund')
    qs = PortfolioKPI.objects.filter(
        investment__scheme__fund__organization=org,
        kpi_definition__slug__in=all_slugs,
    ).select_related('investment', 'kpi_definition')
    if fund_id:
        qs = qs.filter(investment__scheme__fund__id=fund_id)

    # Also match KPIs tagged with sector_template='saas' (covers non-standard slugs)
    saas_template_qs = PortfolioKPI.objects.filter(
        investment__scheme__fund__organization=org,
        kpi_definition__sector_template='saas',
    ).select_related('investment', 'kpi_definition')
    if fund_id:
        saas_template_qs = saas_template_qs.filter(investment__scheme__fund__id=fund_id)

    from itertools import chain
    all_kpis = list(chain(qs, saas_template_qs))

    # Base: ALL investments for this fund (so every company appears even without SaaS KPIs)
    fund_ids = get_accessible_fund_ids(request.user)
    all_inv_qs = Investment.objects.filter(
        scheme__fund__organization=org,
        scheme__fund__id__in=fund_ids,
    ).select_related('portfolio_company')
    if fund_id:
        all_inv_qs = all_inv_qs.filter(scheme__fund__id=fund_id)

    companies = {}
    for inv in all_inv_qs.order_by('company_name'):
        name = inv.company_name
        if not name or name in companies:
            continue
        sector = inv.sector or (inv.portfolio_company.sector if inv.portfolio_company else '') or ''
        companies[name] = {'company_name': name, 'sector': sector}

    # Overlay KPI data (latest period per investment+slug wins)
    seen = set()
    for k in sorted(all_kpis, key=lambda x: str(x.period), reverse=True):
        dedup_key = (str(k.investment_id), k.kpi_definition.slug)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        name = k.investment.company_name
        raw_slug = k.kpi_definition.slug
        canonical = slug_to_canonical.get(raw_slug, raw_slug)
        if name not in companies:
            sector = k.investment.sector or ''
            companies[name] = {'company_name': name, 'sector': sector}
        companies[name][canonical] = float(k.value)

    # Compute ltv_cac_ratio inline if individual LTV and CAC values are present
    for co in companies.values():
        if co.get('ltv_cac_ratio') is None and co.get('ltv') and co.get('cac') and co['cac'] != 0:
            co['ltv_cac_ratio'] = round(co['ltv'] / co['cac'], 4)

    return Response({
        'companies': list(companies.values()),
        'metrics_legend': {k: list(v) for k, v in SAAS_SLUGS.items()},
    })


@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def portfolio_quoted_unquoted(request):
    """
    GET /api/portfolio/quoted-unquoted/?fund=<id>
    Returns companies split by quoted (listed) vs unquoted (private).
    Also uses IPEV Level 1 valuations as a signal for quoted companies.
    """
    from django.db.models import Max

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_id = request.query_params.get('fund')
    fund_ids = get_accessible_fund_ids(request.user)
    qs = PortfolioCompany.objects.filter(
        organization=org,
        investments__scheme__fund__id__in=fund_ids,
    ).distinct()
    if fund_id:
        qs = qs.filter(investments__scheme__fund__id=fund_id).distinct()

    # Get latest valuation and investment data per company
    from django.db.models import Sum
    inv_qs = Investment.objects.filter(
        scheme__fund__organization=org,
    )
    if fund_id:
        inv_qs = inv_qs.filter(scheme__fund__id=fund_id)

    cost_map = {}
    fv_map = {}
    ipev_map = {}
    for inv in inv_qs.prefetch_related('valuations'):
        name = inv.company_name
        cost_map[name] = cost_map.get(name, 0) + float(inv.total_invested or 0)
        latest_val = inv.valuations.filter(status='approved').order_by('-valuation_date').first()
        if latest_val:
            fv_map[name] = fv_map.get(name, 0) + float(latest_val.fair_value or 0)
            if latest_val.ipev_level:
                ipev_map[name] = latest_val.ipev_level

    quoted = []
    unquoted = []
    for co in qs:
        # Quoted = is_quoted flag OR IPEV Level 1 valuation
        is_q = co.is_quoted or (ipev_map.get(co.name) == 1)
        row = {
            'name': co.name,
            'sector': co.sector,
            'exchange': co.listing_exchange or (
                'IPEV L1' if ipev_map.get(co.name) == 1 else ''),
            'cost': cost_map.get(co.name, 0),
            'fair_value': fv_map.get(co.name, 0),
            'ipev_level': ipev_map.get(co.name),
        }
        if is_q:
            quoted.append(row)
        else:
            unquoted.append(row)

    return Response({
        'quoted': quoted,
        'unquoted': unquoted,
        'summary': {
            'total': len(quoted) + len(unquoted),
            'quoted_count': len(quoted),
            'unquoted_count': len(unquoted),
            'quoted_cost': sum(r['cost'] for r in quoted),
            'unquoted_cost': sum(r['cost'] for r in unquoted),
        },
    })


# ═══════════════════════════════════════════════════════════════
# PORTFOLIO FUND-LEVEL: INVESTMENTS, VALUATIONS, KPI TRACKING,
# EXIT SCENARIOS, BOARD MEETINGS (5 new endpoints)
# ═══════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def portfolio_investments_list(request):
    """
    GET /api/portfolio/investments/?fund=<id>
    Returns one row per InvestmentTranche (i.e. one row per investment
    round / position in the file) so the dashboard reflects the file's
    'Portfolio Investments' sheet row-for-row.  Multi-round companies
    show one row per round; single-round companies show one row.

    Also returns aggregate counts: distinct companies and distinct
    investment positions.
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)
    fund_id = request.query_params.get('fund')

    inv_qs = Investment.objects.filter(
        scheme__fund__organization=org,
        scheme__fund__id__in=fund_ids,
    ).select_related('scheme', 'scheme__fund', 'portfolio_company')

    if fund_id:
        inv_qs = inv_qs.filter(scheme__fund__id=fund_id)

    inv_ids = list(inv_qs.values_list('id', flat=True))

    tranche_qs = (InvestmentTranche.objects
                  .filter(investment_id__in=inv_ids)
                  .select_related('investment',
                                  'investment__scheme',
                                  'investment__portfolio_company')
                  .order_by('investment__company_name',
                            'date', 'tranche_number'))

    latest_val_by_inv = {}
    for v in (Valuation.objects.filter(investment_id__in=inv_ids,
                                       status='approved')
              .order_by('investment_id', '-valuation_date')):
        if v.investment_id not in latest_val_by_inv:
            latest_val_by_inv[v.investment_id] = v

    rows = []
    for t in tranche_qs:
        inv = t.investment
        latest_val = latest_val_by_inv.get(inv.id)
        rows.append({
            'id':                       str(t.id),
            'investment_id':            str(inv.id),
            'company_name':             inv.company_name,
            'scheme_name':              inv.scheme.name,
            'sector':                   inv.sector or (inv.portfolio_company.sector
                                                        if inv.portfolio_company else ''),
            'stage':                    t.round_name or inv.stage or '',
            'tranche_number':           t.tranche_number,
            'natural_key':              t.natural_key or '',
            'instrument_type':          (t.instrument_type or inv.instrument_type),
            'instrument_type_display':  inv.get_instrument_type_display(),
            'status':                   inv.status,
            'status_display':           inv.get_status_display(),
            'total_invested':           float(t.amount) if t.amount is not None else 0,
            'ownership_pct':            float(t.ownership_pct) if t.ownership_pct is not None
                                          else (float(inv.ownership_pct) if inv.ownership_pct else None),
            'investment_date':          str(t.date) if t.date else None,
            'irr_pct':                  float(inv.irr_pct) if inv.irr_pct else None,
            'latest_valuation':         float(latest_val.fair_value_of_holding or latest_val.fair_value) if latest_val else None,
            'currency':                 inv.currency,
        })

    distinct_companies = len({(r['company_name'] or '').strip().lower()
                               for r in rows if r['company_name']})

    return Response({
        'investments':         rows,
        'count':               len(rows),
        'distinct_companies':  distinct_companies,
        'distinct_investments': len(rows),
    })


@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def portfolio_valuations_list(request):
    """
    GET /api/portfolio/valuations/?fund=<id>
    Returns all valuations for investments in the selected fund.
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)
    fund_id = request.query_params.get('fund')

    qs = Valuation.objects.filter(
        investment__scheme__fund__organization=org,
        investment__scheme__fund__id__in=fund_ids,
    ).select_related('investment', 'investment__scheme', 'submitted_by', 'approved_by')

    if fund_id:
        qs = qs.filter(investment__scheme__fund__id=fund_id)

    valuations = []
    for v in qs.order_by('-valuation_date'):
        valuations.append({
            'id': str(v.id),
            'company_name': v.investment.company_name,
            'scheme_name': v.investment.scheme.name,
            'valuation_date': str(v.valuation_date),
            'fair_value': float(v.fair_value) if v.fair_value else 0,
            'methodology': v.methodology,
            'methodology_display': v.get_methodology_display(),
            'ipev_level': v.ipev_level,
            'multiple': float(v.multiple) if v.multiple else None,
            'status': v.status,
            'submitted_by': v.submitted_by.username if v.submitted_by else None,
            'approved_by': v.approved_by.username if v.approved_by else None,
        })

    return Response({'valuations': valuations, 'count': len(valuations)})


@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def portfolio_kpi_tracking(request):
    """
    GET /api/portfolio/kpi-tracking/?fund=<id>&status=<status>
    Returns all KPI submissions for the selected fund with review status.
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)
    fund_id = request.query_params.get('fund')
    kpi_status = request.query_params.get('status')

    qs = PortfolioKPI.objects.filter(
        investment__scheme__fund__organization=org,
        investment__scheme__fund__id__in=fund_ids,
    ).select_related('investment', 'kpi_definition', 'submitted_by', 'reviewed_by')

    if fund_id:
        qs = qs.filter(investment__scheme__fund__id=fund_id)
    if kpi_status:
        qs = qs.filter(status=kpi_status)

    kpis = []
    for k in qs.order_by('-period', 'investment__company_name')[:500]:
        kpis.append({
            'id': str(k.id),
            'company_name': k.investment.company_name,
            'kpi_name': k.kpi_definition.name,
            'kpi_slug': k.kpi_definition.slug,
            'format': k.kpi_definition.format,
            'period': str(k.period),
            'value': float(k.value),
            'notes': k.notes or '',
            'status': k.status,
            'submitted_by': k.submitted_by.username if k.submitted_by else None,
            'submitted_at': k.submitted_at.isoformat() if k.submitted_at else None,
            'reviewed_by': k.reviewed_by.username if k.reviewed_by else None,
        })

    return Response({'kpis': kpis, 'count': len(kpis)})


# ── Canonical slug → column mapping for KPI matrix ──────────
# IMPORTANT: column headers in the matrix that end with "%" (GROSS M%,
# EBITDA%, RETURNS%, REPEAT%) MUST only accept PERCENTAGE slugs. Raw
# amount slugs (e.g. plain 'ebitda' for Crore-denominated EBITDA) must
# NOT be routed into the % columns, otherwise the dashboard renders a
# raw 6.10 Cr as "6.10%". The percentage versions are produced by the
# Pass 6 percentage-derivation step (e.g. ebitda-pct = ebitda / revenue * 100)
# and stored under the *-pct / *-margin slugs.
_KPI_COL_SLUGS = {
    'gmv':      ['gmv', 'gmv-rs-cr', 'gmvcr', 'gmv_cr'],
    'revenue':  ['rev', 'revenue', 'revenue-rs-cr', 'revenuecr', 'revenue_cr',
                 'total_revenue', 'net_sales'],
    'gross_m':  ['gross-margin-pct', 'gross-margin', 'gross-m',
                 'gross-margin-percent', 'gross_margin_pct', 'gross_margin',
                 'gross_m', 'gross_profit_pct'],
    'ebitda':   ['ebitda-pct', 'ebitda-margin', 'ebitda-margin-pct',
                 'ebitda-margin-percent', 'ebitda_margin_pct',
                 'ebitda_margin', 'ebitda_pct'],
    'orders':   ['orders', 'order-book', 'order-book-rs-cr', 'order_count',
                 'total_orders', 'transactions'],
    'aov':      ['aov', 'average_order_value', 'avg_order_value'],
    'returns':  ['returns-pct', 'returns', 'returns-percent', 'returns_pct',
                 'return_rate_pct', 'rto_pct'],
    'cac':      ['cac', 'customer_acquisition_cost', 'blended_cac'],
    'repeat':   ['repeat-pct', 'repeat', 'repeat-percent', 'repeat_pct',
                 'repeat_customer_rate', 'retention_pct'],
}
# Universal Fix B (2026-07-06) — Persister writes KPIDefinition.slug in
# underscore-normalised form (e.g. `gross_margin_pct`, `ebitda_margin_pct`);
# some older code paths and reference data write hyphenated forms
# (`gross-margin-pct`). Build a slug→column index that normalises BOTH
# separators so either dialect resolves to the same column. Universal —
# every fund benefits, no per-slug hand-maintenance beyond the mapping above.
def _canon_kpi_slug(s: str) -> str:
    return (s or '').strip().lower().replace('_', '-')

_SLUG_TO_COL = {}
for _col, _slugs in _KPI_COL_SLUGS.items():
    for _s in _slugs:
        _SLUG_TO_COL[_canon_kpi_slug(_s)] = _col


@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def portfolio_kpi_matrix(request):
    """
    GET /api/portfolio/kpi-matrix/?fund=<id>
    Returns one row per company with pivoted KPI columns:
    GMV, REV, Gross M%, EBITDA, Orders, AOV, Returns, CAC, Repeat%, Cost, FV.
    Cost comes from Investment.total_invested; FV from latest Valuation.
    """
    from collections import defaultdict

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)
    fund_id = request.query_params.get('fund')

    inv_filter = dict(
        scheme__fund__organization=org,
        scheme__fund__id__in=fund_ids,
    )
    if fund_id:
        inv_filter['scheme__fund__id'] = fund_id

    investments = Investment.objects.filter(**inv_filter).select_related(
        'portfolio_company', 'scheme__fund',
    )

    # Build company → investment ids and cost
    company_data = {}  # company_name → {inv_ids, cost, sector, ...}
    for inv in investments:
        name = inv.company_name
        if name not in company_data:
            company_data[name] = {
                'inv_ids': [],
                'cost': 0,
                'sector': inv.sector or '',
            }
        company_data[name]['inv_ids'].append(inv.id)
        if inv.total_invested:
            company_data[name]['cost'] += float(inv.total_invested)

    if not company_data:
        return Response({'companies': [], 'count': 0})

    # Get latest FV per investment from Valuation
    all_inv_ids = []
    for cd in company_data.values():
        all_inv_ids.extend(cd['inv_ids'])

    latest_val_qs = Valuation.objects.filter(
        investment_id__in=all_inv_ids,
    ).order_by('investment_id', '-valuation_date')

    inv_fv = {}  # investment_id → latest fair_value
    for v in latest_val_qs:
        if v.investment_id not in inv_fv and v.fair_value:
            inv_fv[v.investment_id] = float(v.fair_value)

    for name, cd in company_data.items():
        cd['fv'] = sum(inv_fv.get(iid, 0) for iid in cd['inv_ids'])

    # Get KPI data — latest value per company per canonical column. Pull the
    # full KPI set for these investments in one query then dispatch by
    # canon-slug (underscores collapsed to hyphens, lowercased) so both
    # persister-side and reference-side dialects resolve.
    kpi_qs = PortfolioKPI.objects.filter(
        investment_id__in=all_inv_ids,
    ).select_related('investment', 'kpi_definition').order_by('-period')

    company_kpis = defaultdict(dict)  # company_name → {col: value}
    _canon_col_slugs = {col: [_canon_kpi_slug(cs) for cs in slugs]
                        for col, slugs in _KPI_COL_SLUGS.items()}
    for k in kpi_qs:
        canon = _canon_kpi_slug(k.kpi_definition.slug)
        name = k.investment.company_name
        # Fast exact-canon match first — covers 99% of persister-written rows.
        col = _SLUG_TO_COL.get(canon)
        if col and col not in company_kpis[name]:
            company_kpis[name][col] = float(k.value)
            continue
        # Substring fallback for hand-authored slugs like 'arr-rs-cr' or
        # 'ebitda-margin-inr' where the canonical token sits inside a
        # decorated variant. Longest-token-first isn't necessary here because
        # each column's slug list already lists specific variants first.
        for col, cslugs in _canon_col_slugs.items():
            if col in company_kpis.get(name, {}):
                continue
            for cs in cslugs:
                if cs in canon or canon in cs:
                    company_kpis[name][col] = float(k.value)
                    break

    # Normalize percent columns: values < 1 are fractions → multiply by 100
    PCT_COLS = {'gross_m', 'ebitda', 'returns', 'repeat'}
    for name, kvals in company_kpis.items():
        for pc in PCT_COLS:
            v = kvals.get(pc)
            if v is not None and abs(v) < 1:
                kvals[pc] = round(v * 100, 2)

    # Assemble response
    cols = ['gmv', 'revenue', 'gross_m', 'ebitda', 'orders', 'aov', 'returns', 'cac', 'repeat']
    companies = []
    for name in sorted(company_data.keys()):
        cd = company_data[name]
        row = {
            'company_name': name,
            'sector': cd['sector'],
            'cost': cd['cost'] if cd['cost'] else None,
            'fv': cd['fv'] if cd.get('fv') else None,
        }
        kvals = company_kpis.get(name, {})
        for c in cols:
            row[c] = kvals.get(c)
        companies.append(row)

    return Response({'companies': companies, 'count': len(companies)})


@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def portfolio_exit_scenarios_list(request):
    """
    GET /api/portfolio/exit-scenarios/?fund=<id>
    Returns all exit scenarios and actual exits for the selected fund.
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)
    fund_id = request.query_params.get('fund')

    qs = ExitEvent.objects.filter(
        investment__scheme__fund__organization=org,
        investment__scheme__fund__id__in=fund_ids,
    ).select_related('investment', 'investment__scheme')

    if fund_id:
        qs = qs.filter(investment__scheme__fund__id=fund_id)

    scenarios = []
    for e in qs.order_by('investment__company_name', '-exit_date'):
        scenarios.append({
            'id': str(e.id),
            'company_name': e.investment.company_name,
            'scheme_name': e.investment.scheme.name,
            'exit_type': e.exit_type,
            'exit_type_display': e.get_exit_type_display(),
            'is_actual': e.is_actual,
            'exit_date': str(e.exit_date) if e.exit_date else None,
            'proceeds': float(e.proceeds) if e.proceeds else None,
            'moic': float(e.moic) if e.moic else None,
            'irr_pct': float(e.irr_pct) if e.irr_pct else None,
            'gain_loss_nature': e.gain_loss_nature or '',
            'buyer_name': e.buyer_name or '',
            'assumptions': e.assumptions or '',
        })

    return Response({'scenarios': scenarios, 'count': len(scenarios)})


@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def portfolio_board_meetings_list(request):
    """
    GET /api/portfolio/board-meetings/?fund=<id>
    Returns all board meetings for investments in the selected fund.
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)
    fund_id = request.query_params.get('fund')

    qs = BoardMeeting.objects.filter(
        investment__scheme__fund__organization=org,
        investment__scheme__fund__id__in=fund_ids,
    ).select_related('investment', 'investment__scheme')

    if fund_id:
        qs = qs.filter(investment__scheme__fund__id=fund_id)

    meetings = []
    for m in qs.order_by('-meeting_date'):
        meetings.append({
            'id': str(m.id),
            'company_name': m.investment.company_name,
            'scheme_name': m.investment.scheme.name,
            'meeting_date': str(m.meeting_date),
            'agenda': m.agenda or '',
            'attendees': m.attendees or [],
            'resolutions': m.resolutions or [],
            'minutes': m.minutes or '',
            'next_meeting_date': str(m.next_meeting_date) if m.next_meeting_date else None,
        })

    return Response({'meetings': meetings, 'count': len(meetings)})


@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=900)
def portfolio_avg_holding(request):
    """
    GET /api/portfolio/avg-holding/?fund=<id>
    Returns avg holding period in years across all investments with a known
    investment_date.  Calculated on the backend using today's date as the
    reference so the frontend only ever renders the result.
    """
    from datetime import date as date_cls

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)
    fund_id = request.query_params.get('fund')

    qs = Investment.objects.filter(
        scheme__fund__organization=org,
        scheme__fund__id__in=fund_ids,
        investment_date__isnull=False,
    ).values_list('investment_date', flat=True)

    if fund_id:
        qs = qs.filter(scheme__fund__id=fund_id)

    dates = list(qs)
    if not dates:
        return Response({'avg_holding_years': None, 'investment_count': 0})

    today = date_cls.today()
    total_days = sum((today - d).days for d in dates)
    avg_years = round(total_days / len(dates) / 365.25, 1)

    return Response({
        'avg_holding_years': avg_years,
        'investment_count': len(dates),
    })


# ═══════════════════════════════════════════════════════════════
# PORTFOLIO COMPANY CRUD
# ═══════════════════════════════════════════════════════════════

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
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
        # Optional: filter to a specific fund or scheme
        fund_id = request.query_params.get('fund')
        if fund_id:
            qs = qs.filter(investments__scheme__fund__id=fund_id).distinct()
        scheme_id = request.query_params.get('scheme')
        if scheme_id:
            qs = qs.filter(investments__scheme__id=scheme_id).distinct()
        sector = request.query_params.get('sector')
        if sector:
            qs = qs.filter(sector__iexact=sector)
        active = request.query_params.get('active')
        if active is not None:
            qs = qs.filter(is_active=active.lower() == 'true')
        # Pass fund_id into the serializer context so per-company
        # IRR/MOIC aggregates are restricted to the active fund.
        ser_ctx = {'fund_id': fund_id} if fund_id else {}
        return Response(PortfolioCompanyListSerializer(
            qs, many=True, context=ser_ctx,
        ).data)

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


# ---------------------------------------------------------------------------
# Exit Signal Engine + Feature Engineering (v5 AI Analytics)
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=900)
def exit_signal_view(request, company_id):
    """
    AI-powered exit signal analysis for a portfolio company.
    Returns exit score, recommended timing, route, and Gemini rationale.
    """
    org = request.organization
    try:
        company = PortfolioCompany.objects.get(pk=company_id, fund__organization=org)
    except PortfolioCompany.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)

    from .feature_engineering import ExitSignalEngine
    engine = ExitSignalEngine(company)
    result = engine.analyze()
    return Response(result)


@api_view(['GET'])
@permission_classes([IsGPUser])
def company_features_view(request, company_id):
    """
    Return computed financial features (ratios, trends, Z-scores) for a company.
    Used by risk scoring and AI analytics.
    """
    org = request.organization
    try:
        company = PortfolioCompany.objects.get(pk=company_id, fund__organization=org)
    except PortfolioCompany.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)

    from .feature_engineering import FinancialFeatureExtractor, XGBoostRiskScorer
    features = FinancialFeatureExtractor(company).extract()
    risk = XGBoostRiskScorer(company).predict()
    return Response({'features': features, 'risk': risk})
