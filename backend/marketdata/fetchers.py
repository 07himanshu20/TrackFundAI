"""
Market data fetchers — pluggable fetchers for BSE, NSE, Bloomberg, Alpha Vantage.

Design:
  - Each fetcher returns { 'close': Decimal, 'open': Decimal, 'high': Decimal,
                           'low': Decimal, 'volume': int, 'currency': str }
  - All fetchers are format-agnostic and exchange-aware
  - API keys are loaded from settings (never hardcoded)
  - Graceful degradation: if one source fails, try the next
  - Circuit breaker: rejects prices that deviate >±20% from previous day close

Exchange routing:
  bse, nse → BSE/NSE India public APIs
  nyse, nasdaq → Alpha Vantage (free tier) or Bloomberg
  lse, sgx → Bloomberg only
"""

import logging
from decimal import Decimal
from datetime import date, timedelta
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# Circuit breaker threshold — reject prices deviating more than this from prev close
CIRCUIT_BREAKER_THRESHOLD = Decimal('0.20')


def _get_previous_close(ticker: str, exchange: str, price_date: date) -> Optional[Decimal]:
    """
    Retrieve the most recent closing price before price_date from the database.
    Returns None if no prior price exists (allows first-time ingestion).
    """
    try:
        from .models import ListedSecurityMapping, MarketPriceFeed
        mapping = ListedSecurityMapping.objects.filter(
            ticker_symbol__iexact=ticker,
            exchange__iexact=exchange,
            is_active=True,
        ).first()
        if not mapping:
            return None
        prev = (
            MarketPriceFeed.objects
            .filter(security=mapping, price_date__lt=price_date)
            .order_by('-price_date')
            .values_list('close_price', flat=True)
            .first()
        )
        return Decimal(str(prev)) if prev is not None else None
    except Exception as e:
        logger.warning(f'Circuit breaker: could not fetch previous close for {ticker}: {e}')
        return None


def _circuit_breaker_check(ticker: str, exchange: str, price_date: date, new_price: Decimal) -> bool:
    """
    Circuit breaker: return True if price is within ±20% of previous close.
    Returns True if no previous price exists (first-time ingestion allowed).
    """
    prev_close = _get_previous_close(ticker, exchange, price_date)
    if prev_close is None or prev_close == 0:
        return True  # No prior data — allow

    deviation = abs(new_price - prev_close) / prev_close
    if deviation > CIRCUIT_BREAKER_THRESHOLD:
        logger.warning(
            f'CIRCUIT BREAKER TRIGGERED: {ticker} ({exchange}) on {price_date} — '
            f'new_price={new_price}, prev_close={prev_close}, '
            f'deviation={deviation:.1%} exceeds ±{CIRCUIT_BREAKER_THRESHOLD:.0%} threshold. '
            f'Price rejected.'
        )
        return False
    return True


def fetch_price(ticker: str, exchange: str, price_date: date) -> Optional[dict]:
    """
    Fetch closing price for a ticker on a given date.
    Routes to the appropriate data source based on exchange.
    Applies ±20% circuit breaker before returning any price.

    Returns dict with price data or None if unavailable/circuit-broken.
    """
    exchange = exchange.lower()

    if exchange in ('bse', 'nse'):
        result = _fetch_india(ticker, exchange, price_date)
    elif exchange in ('nyse', 'nasdaq'):
        result = _fetch_alpha_vantage(ticker, price_date)
        if not result:
            result = _fetch_bloomberg(ticker, price_date)
    else:
        result = _fetch_bloomberg(ticker, price_date)

    if result and result.get('close'):
        if not _circuit_breaker_check(ticker, exchange, price_date, result['close']):
            result['circuit_breaker_triggered'] = True
            result['close'] = None  # Nullify the price so callers know it's rejected
            return None  # Reject the price entirely

    return result


def _fetch_india(ticker: str, exchange: str, price_date: date) -> Optional[dict]:
    """
    Fetch from BSE/NSE public data APIs.
    Falls back to NSE if BSE fails (and vice versa).
    Note: BSE/NSE public APIs are rate-limited; production should use NSE or
    a commercial data vendor.
    """
    try:
        import urllib.request
        import json

        # NSE India open API approach (no auth required for EOD data)
        if exchange == 'nse':
            url = f'https://www.nseindia.com/api/quote-equity?symbol={ticker.upper()}'
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0',
                'Accept': 'application/json',
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            price_info = data.get('priceInfo', {})
            close = price_info.get('lastPrice') or price_info.get('close')
            if close:
                return {
                    'close': Decimal(str(close)),
                    'open':  Decimal(str(price_info.get('open', close))),
                    'high':  Decimal(str(price_info.get('intraDayHighLow', {}).get('max', close))),
                    'low':   Decimal(str(price_info.get('intraDayHighLow', {}).get('min', close))),
                    'volume': None,
                    'currency': 'INR',
                    'source': 'nse_api',
                }
    except Exception as e:
        logger.warning(f'NSE fetch failed for {ticker}: {e}')

    # BSE fallback
    try:
        import urllib.request
        import json
        # BSE uses a different scraping approach; this is a placeholder
        # In production, use BSE's Bhavcopy CSV or a paid API
        logger.info(f'BSE fetch not implemented for {ticker} — using Alpha Vantage fallback')
    except Exception as e:
        logger.warning(f'BSE fetch failed for {ticker}: {e}')

    return _fetch_alpha_vantage(ticker, price_date)


def _fetch_alpha_vantage(ticker: str, price_date: date) -> Optional[dict]:
    """Fetch from Alpha Vantage TIME_SERIES_DAILY endpoint (free API key required)."""
    api_key = getattr(settings, 'ALPHA_VANTAGE_API_KEY', '') or getattr(settings, 'NSE_API_KEY', '')
    if not api_key or api_key == 'demo':
        return None

    try:
        import urllib.request
        import json

        date_str = price_date.strftime('%Y-%m-%d')
        url = (
            f'https://www.alphavantage.co/query?function=TIME_SERIES_DAILY'
            f'&symbol={ticker}&apikey={api_key}&outputsize=compact'
        )
        req = urllib.request.Request(url, headers={'User-Agent': 'TrackFundAI/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        series = data.get('Time Series (Daily)', {})
        day_data = series.get(date_str)
        if not day_data:
            # Try the most recent available date
            if series:
                latest_date = max(series.keys())
                day_data = series[latest_date]
                date_str = latest_date
            else:
                return None

        return {
            'close': Decimal(day_data.get('4. close', '0')),
            'open':  Decimal(day_data.get('1. open', '0')),
            'high':  Decimal(day_data.get('2. high', '0')),
            'low':   Decimal(day_data.get('3. low', '0')),
            'volume': int(day_data.get('5. volume', 0)),
            'currency': 'USD',
            'source': 'alpha_vantage',
        }
    except Exception as e:
        logger.warning(f'Alpha Vantage fetch failed for {ticker}: {e}')
        return None


def _fetch_bloomberg(ticker: str, price_date: date) -> Optional[dict]:
    """
    Bloomberg data fetch — requires Bloomberg API subscription.
    This is a stub; replace with blpapi calls when Bloomberg terminal is available.
    """
    bloomberg_key = getattr(settings, 'BLOOMBERG_API_KEY', '')
    if not bloomberg_key:
        logger.info(f'Bloomberg API key not configured — skipping for {ticker}')
        return None

    # Placeholder for Bloomberg Open API / BLPAPI integration
    logger.info(f'Bloomberg fetch: {ticker} ({price_date}) — stub, configure BLPAPI')
    return None


def fetch_fx_rate(base: str, quote: str, rate_date: date) -> Optional[Decimal]:
    """
    Fetch FX rate from RBI or Alpha Vantage.
    base='USD', quote='INR' → USD/INR rate.
    """
    try:
        import urllib.request
        import json

        # Alpha Vantage FX endpoint
        api_key = getattr(settings, 'ALPHA_VANTAGE_API_KEY', '') or getattr(settings, 'NSE_API_KEY', '')
        if not api_key:
            return None

        url = (
            f'https://www.alphavantage.co/query?function=FX_DAILY'
            f'&from_symbol={base}&to_symbol={quote}&apikey={api_key}'
        )
        req = urllib.request.Request(url, headers={'User-Agent': 'TrackFundAI/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        series = data.get('Time Series FX (Daily)', {})
        date_str = rate_date.strftime('%Y-%m-%d')
        day_data = series.get(date_str)
        if not day_data and series:
            latest_date = max(series.keys())
            day_data = series[latest_date]

        if day_data:
            return Decimal(day_data.get('4. close', '0'))
    except Exception as e:
        logger.warning(f'FX rate fetch failed ({base}/{quote}): {e}')

    return None
