"""
fx_rates.py
Foreign exchange rates for portfolio currency normalization.
All rates expressed as (units of foreign currency) per 1 USD.
Base currency: USD.

Spot rates as of 2026-04-13 (sources: ECB, Bloomberg, Federal Reserve H.10).
Override here to refresh.
"""

FX_RATES_PER_USD = {
    "USD": 1.00,
    "MYR": 3.97,    # ~3.965-3.98 from Bloomberg/Yahoo, 8-10 Apr 2026
    "EUR": 0.856,   # ECB ref / poundsterlinglive, 13 Apr 2026
    "INR": 93.30,   # Federal Reserve H.10, 13 Apr 2026
}

FX_AS_OF_DATE = "2026-04-13"


def to_usd(amount, currency):
    """Convert an amount from the given currency to USD."""
    if amount is None:
        return None
    rate = FX_RATES_PER_USD.get(currency.upper())
    if rate is None:
        raise ValueError(f"No FX rate configured for currency: {currency}")
    return amount / rate


def from_usd(amount, currency):
    """Convert a USD amount into the given currency."""
    if amount is None:
        return None
    rate = FX_RATES_PER_USD.get(currency.upper())
    if rate is None:
        raise ValueError(f"No FX rate configured for currency: {currency}")
    return amount * rate


def convert(amount, from_currency, to_currency):
    """Convert amount between two arbitrary currencies via USD."""
    if amount is None:
        return None
    if from_currency.upper() == to_currency.upper():
        return amount
    return from_usd(to_usd(amount, from_currency), to_currency)
