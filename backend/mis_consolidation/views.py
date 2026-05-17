"""
MIS Consolidation API views — BvA CRUD, consolidated rollup, anomaly alerts.
"""
from rest_framework import status, serializers as drf_serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from config.cache_utils import cached_api_view
from .models import BudgetVsActual, ConsolidatedMIS, MISAnomalyAlert
from .services import MISAggregator, AnomalyDetector


class BvASerializer(drf_serializers.ModelSerializer):
    line_item_display = drf_serializers.CharField(source='get_line_item_display', read_only=True)
    company_name = drf_serializers.CharField(source='portfolio_company.name', read_only=True)

    class Meta:
        model = BudgetVsActual
        fields = '__all__'
        read_only_fields = ('id', 'variance_inr', 'variance_pct', 'is_favorable',
                            'created_at', 'updated_at')


class ConsolidatedMISSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = ConsolidatedMIS
        fields = '__all__'
        read_only_fields = ('id', 'computed_at')


class AnomalyAlertSerializer(drf_serializers.ModelSerializer):
    company_name = drf_serializers.CharField(source='portfolio_company.name', read_only=True)

    class Meta:
        model = MISAnomalyAlert
        fields = '__all__'
        read_only_fields = ('id', 'detected_at')


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
@cached_api_view(timeout=600)
def bva_list(request):
    org = request.organization
    if request.method == 'GET':
        company_id = request.query_params.get('company')
        fund_id    = request.query_params.get('fund')
        year       = request.query_params.get('year')
        month      = request.query_params.get('month')
        has_budget = request.query_params.get('has_budget')

        qs = BudgetVsActual.objects.filter(
            portfolio_company__organization=org
        ).select_related('portfolio_company')

        # Filter by fund FK directly — avoids cross-fund contamination from shared companies
        if fund_id:
            qs = qs.filter(fund_id=fund_id)

        if company_id:
            qs = qs.filter(portfolio_company_id=company_id)
        if year:
            qs = qs.filter(period_year=year)
        if month:
            qs = qs.filter(period_month=month)

        # Return only records that have budget set (for BvA tab) when requested
        if has_budget == '1':
            qs = qs.filter(budget_inr__isnull=False)

        # Order: annual records (period_month=None) first so BvA summary rows
        # appear before monthly P&L-only rows.  Within the same period type,
        # most-recent year first, then by company name.
        from django.db.models import F
        from django.db.models.functions import Coalesce
        qs = qs.order_by(
            F('period_month').asc(nulls_first=True),   # NULL (annual) before monthly
            '-period_year',
            'portfolio_company__name',
            'line_item',
        )

        return Response(BvASerializer(qs, many=True).data)

    ser = BvASerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    rec = ser.save()
    return Response(BvASerializer(rec).data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
@cached_api_view(timeout=600)
def consolidated_mis(request):
    """Get consolidated MIS for a fund, optionally filtered by period."""
    org = request.organization
    fund_id = request.query_params.get('fund')
    year = request.query_params.get('year')
    month = request.query_params.get('month')

    line_item = request.query_params.get('line_item')

    qs = ConsolidatedMIS.objects.filter(organization=org)
    if fund_id:
        qs = qs.filter(fund_id=fund_id)
    if year:
        qs = qs.filter(period_year=year)
    if month:
        qs = qs.filter(period_month=month)
    if line_item:
        qs = qs.filter(line_item=line_item)
    return Response(ConsolidatedMISSerializer(qs, many=True).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def run_consolidation(request):
    """
    Trigger MIS aggregation for a fund.
    Body: { fund_id, scheme_id (optional), period_year, period_month }
    """
    from funds.models import Fund, Scheme
    fund_id = request.data.get('fund_id')
    if not fund_id:
        return Response({'detail': 'fund_id required.'}, status=400)

    try:
        fund = Fund.objects.get(pk=fund_id, organization=request.organization)
    except Fund.DoesNotExist:
        return Response({'detail': 'Fund not found.'}, status=404)

    scheme = None
    if request.data.get('scheme_id'):
        scheme = Scheme.objects.get(pk=request.data['scheme_id'])

    aggregator = MISAggregator(
        fund=fund, scheme=scheme,
        period_year=request.data.get('period_year'),
        period_month=request.data.get('period_month'),
    )
    results = aggregator.run()
    return Response({'consolidated_records': len(results), 'status': 'success'})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
@cached_api_view(timeout=900)
def six_month_rollup(request):
    """6-month P&L rollup for sparkline charts."""
    from funds.models import Fund
    org = request.organization
    fund_id = request.query_params.get('fund')
    line_items = request.query_params.getlist('line_items') or ['revenue', 'ebitda', 'pat']

    if not fund_id:
        return Response({'detail': 'fund_id required.'}, status=400)
    try:
        fund = Fund.objects.get(pk=fund_id, organization=org)
    except Fund.DoesNotExist:
        return Response({'detail': 'Fund not found.'}, status=404)

    data = MISAggregator.get_6month_rollup(fund, line_items=line_items)
    return Response({'data': data})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
@cached_api_view(timeout=300)
def anomaly_alerts(request):
    """List active anomaly alerts for the organization."""
    org = request.organization
    qs = MISAnomalyAlert.objects.filter(
        portfolio_company__organization=org,
        resolved=False,
    )
    severity = request.query_params.get('severity')
    if severity:
        qs = qs.filter(severity=severity)
    company_id = request.query_params.get('company')
    if company_id:
        qs = qs.filter(portfolio_company_id=company_id)
    return Response(AnomalyAlertSerializer(qs[:50], many=True).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def resolve_anomaly(request, pk):
    """Mark an anomaly alert as resolved."""
    org = request.organization
    try:
        alert = MISAnomalyAlert.objects.get(pk=pk, portfolio_company__organization=org)
    except MISAnomalyAlert.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)

    from django.utils import timezone
    alert.resolved = True
    alert.resolved_at = timezone.now()
    alert.save(update_fields=['resolved', 'resolved_at'])
    return Response({'detail': 'Resolved.'})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
@cached_api_view(timeout=900)
def mis_submission_status(request):
    """
    Return per-company MIS submission completeness.
    Checks which companies have P&L, Balance Sheet, Cash Flow, and BvA records.
    Supports optional ?fund= filter to scope to a single fund's companies.
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    from investments.models import PortfolioCompany

    PL_ITEMS  = {'revenue', 'total_revenue', 'pat', 'ebitda', 'gross_profit'}
    BS_ITEMS  = {'total_assets', 'net_worth', 'total_debt', 'cash_and_equivalents'}
    CF_ITEMS  = {'cash_and_equivalents', 'finance_cost'}

    fund_id = request.query_params.get('fund')

    companies_qs = PortfolioCompany.objects.filter(
        organization=org, is_active=True
    ).order_by('name')

    # If fund filter is provided, only include companies that have investments
    # or BvA records under that fund
    if fund_id:
        from django.db.models import Q
        companies_qs = companies_qs.filter(
            Q(investments__scheme__fund_id=fund_id) |
            Q(bva_records__fund_id=fund_id)
        ).distinct()

    result = []
    for co in companies_qs:
        qs = BudgetVsActual.objects.filter(portfolio_company=co)
        if fund_id:
            qs = qs.filter(fund_id=fund_id)
        submitted_items = set(qs.values_list('line_item', flat=True).distinct())

        has_pl  = bool(submitted_items & PL_ITEMS)
        has_bs  = bool(submitted_items & BS_ITEMS)
        has_cf  = bool(submitted_items & CF_ITEMS)
        has_bva = qs.filter(budget_inr__isnull=False).exists()

        last_record = qs.order_by('-updated_at').first()
        last_updated = str(last_record.updated_at.date()) if last_record else None

        result.append({
            'company_id':   str(co.id),
            'company_name': co.name,
            'has_pl':       has_pl,
            'has_bs':       has_bs,
            'has_cf':       has_cf,
            'has_bva':      has_bva,
            'last_updated': last_updated,
            'status':       'active' if (has_pl and has_bs and has_cf and has_bva) else 'pending',
        })

    return Response(result)
