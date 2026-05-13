from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsGPUser
from accounts.fund_access_helpers import get_accessible_fund_ids
from .models import ListedSecurityMapping, MarketPriceFeed, FXRateFeed


@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def security_list(request):
    """List / create listed security mappings for the org."""
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        qs = ListedSecurityMapping.objects.filter(
            portfolio_company__organization=org,
            is_active=True,
        ).select_related('portfolio_company').order_by('ticker_symbol')

        data = [
            {
                'id': str(s.id),
                'portfolio_company_id': str(s.portfolio_company_id),
                'portfolio_company_name': s.portfolio_company.name,
                'exchange': s.exchange,
                'ticker_symbol': s.ticker_symbol,
                'isin': s.isin,
                'shares_held': float(s.shares_held),
                'currency': s.currency,
                'is_primary_listing': s.is_primary_listing,
            }
            for s in qs
        ]
        return Response(data)

    # POST: create a new security mapping
    from investments.models import PortfolioCompany
    data = request.data
    try:
        company = PortfolioCompany.objects.get(
            pk=data['portfolio_company_id'], organization=org
        )
    except (PortfolioCompany.DoesNotExist, KeyError):
        return Response({'detail': 'Portfolio company not found.'}, status=404)

    sec = ListedSecurityMapping.objects.create(
        portfolio_company=company,
        exchange=data.get('exchange', 'nse'),
        ticker_symbol=data.get('ticker_symbol', ''),
        isin=data.get('isin', ''),
        shares_held=data.get('shares_held', 0),
        currency=data.get('currency', 'INR'),
        is_primary_listing=data.get('is_primary_listing', True),
    )
    return Response({'id': str(sec.id), 'ticker_symbol': sec.ticker_symbol}, status=201)


@api_view(['GET'])
@permission_classes([IsGPUser])
def price_history(request, security_id):
    """Get price history for a listed security."""
    org = request.organization
    try:
        sec = ListedSecurityMapping.objects.get(
            pk=security_id, portfolio_company__organization=org
        )
    except ListedSecurityMapping.DoesNotExist:
        return Response({'detail': 'Security not found.'}, status=404)

    qs = MarketPriceFeed.objects.filter(security=sec).order_by('-price_date')[:90]
    data = [
        {
            'price_date': str(p.price_date),
            'close_price': float(p.close_price),
            'fair_value_of_holding': float(p.fair_value_of_holding or 0),
            'source': p.source,
        }
        for p in qs
    ]
    return Response(data)


@api_view(['GET'])
@permission_classes([IsGPUser])
def fx_rates(request):
    """Get latest FX rates."""
    from django.db.models import Max
    from django.utils import timezone

    latest_rates = (
        FXRateFeed.objects
        .values('base_currency', 'quote_currency')
        .annotate(latest_date=Max('rate_date'))
        .order_by('base_currency')
    )

    result = []
    for lr in latest_rates:
        rate = FXRateFeed.objects.get(
            base_currency=lr['base_currency'],
            quote_currency=lr['quote_currency'],
            rate_date=lr['latest_date'],
        )
        result.append({
            'pair': f"{lr['base_currency']}/{lr['quote_currency']}",
            'rate': float(rate.rate),
            'date': str(rate.rate_date),
            'source': rate.source,
        })

    return Response(result)


@api_view(['POST'])
@permission_classes([IsGPUser])
def trigger_price_fetch(request):
    """Manually trigger market data fetch."""
    from .tasks import fetch_daily_prices, fetch_fx_rates
    fetch_daily_prices.delay()
    fetch_fx_rates.delay()
    return Response({'detail': 'Market data fetch triggered.'})
