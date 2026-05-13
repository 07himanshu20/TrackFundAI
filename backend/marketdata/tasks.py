"""
Celery tasks for market data ingestion.

Scheduled daily (post-market close, ~4:00 PM IST) via django-celery-beat.
"""

from celery import shared_task
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)


@shared_task(name='marketdata.fetch_daily_prices')
def fetch_daily_prices():
    """
    Fetch daily closing prices for all active listed security mappings.
    Runs after market close (~4 PM IST). Delta detection: skip if price unchanged.
    Also triggers NAV recomputation for schemes holding listed securities.
    """
    from marketdata.models import ListedSecurityMapping, MarketPriceFeed
    from marketdata.fetchers import fetch_price

    today = timezone.now().date()
    securities = ListedSecurityMapping.objects.filter(is_active=True).select_related(
        'portfolio_company'
    )

    updated = 0
    errors = 0

    for sec in securities:
        try:
            # Delta detection: skip if we already have today's price
            if MarketPriceFeed.objects.filter(security=sec, price_date=today).exists():
                continue

            price_data = fetch_price(sec.ticker_symbol, sec.exchange, today)
            if not price_data:
                continue

            # Check previous close for delta
            prev = MarketPriceFeed.objects.filter(
                security=sec, price_date__lt=today
            ).order_by('-price_date').first()

            if prev and abs(float(prev.close_price - price_data['close'])) < 0.001:
                # No meaningful price change — still record for completeness
                pass

            MarketPriceFeed.objects.create(
                security=sec,
                price_date=today,
                close_price=price_data['close'],
                open_price=price_data.get('open'),
                high_price=price_data.get('high'),
                low_price=price_data.get('low'),
                volume=price_data.get('volume'),
                currency=price_data.get('currency', 'INR'),
                source=price_data.get('source', 'manual'),
            )
            updated += 1

            # Update the IPEV Level 1 Valuation for the corresponding investment
            _update_level1_valuation(sec, price_data['close'], today)

        except Exception as e:
            errors += 1
            logger.error(f'Price fetch error for {sec.ticker_symbol}: {e}')

    logger.info(f'Market data: {updated} prices updated, {errors} errors')
    return {'updated': updated, 'errors': errors, 'date': str(today)}


def _update_level1_valuation(security, close_price, price_date):
    """
    Auto-update IPEV Level 1 Valuation for a listed security holding.
    Creates a new Valuation record (or updates today's draft) with:
      fair_value_of_holding = close_price × shares_held
    """
    from investments.models import Investment, Valuation
    from decimal import Decimal

    # Find investments in this portfolio company that have a listed security mapping
    investments = Investment.objects.filter(
        portfolio_company=security.portfolio_company,
        status='active',
    )

    for inv in investments:
        shares = security.shares_held
        if not shares:
            continue

        fv = Decimal(str(close_price)) * shares

        # Update or create today's Level 1 valuation
        Valuation.objects.update_or_create(
            investment=inv,
            valuation_date=price_date,
            methodology='comparables',  # Level 1 uses market price
            defaults={
                'fair_value': fv,
                'fair_value_of_holding': fv,
                'ipev_level': 1,
                'status': 'approved',  # Auto-approved for listed securities
                'assumptions': f'IPEV Level 1: {security.ticker_symbol} @ {close_price} × {shares} shares',
            },
        )


@shared_task(name='marketdata.fetch_fx_rates')
def fetch_fx_rates():
    """Fetch daily FX rates for common currency pairs."""
    from marketdata.models import FXRateFeed
    from marketdata.fetchers import fetch_fx_rate

    today = timezone.now().date()
    pairs = [
        ('USD', 'INR'),
        ('EUR', 'INR'),
        ('GBP', 'INR'),
        ('SGD', 'INR'),
    ]

    for base, quote in pairs:
        try:
            if FXRateFeed.objects.filter(
                base_currency=base, quote_currency=quote, rate_date=today
            ).exists():
                continue

            rate = fetch_fx_rate(base, quote, today)
            if rate:
                FXRateFeed.objects.create(
                    base_currency=base,
                    quote_currency=quote,
                    rate_date=today,
                    rate=rate,
                    source='alpha_vantage',
                )
        except Exception as e:
            logger.error(f'FX rate fetch failed {base}/{quote}: {e}')
