"""
Stage 4 — Normalization (CODE, zero tokens).

Deterministic transforms over frozen records: FX conversion from a VERSIONED
config table (never the model's memory), scale-unit normalization to ₹ Crore,
and retention of the native value alongside the normalized value for audit.

Fund workbooks author figures already in ₹ Crore (their banners say "₹ Cr"), so
those pass through unchanged. Company-MIS operating metrics are in local currency
at the sheet's scale (raw / thousands / lakhs / millions), so they are converted:
    native_absolute = value × scale_factor
    inr             = native_absolute × FX[currency→INR]
    crore           = inr ÷ 1e7
"""

# Versioned FX table (illustrative reference rates → INR). Productionization
# replaces this with a dated rates service; the point is it is CONFIG, not model.
FX_VERSION = 'fx-2026-07'
FX_TO_INR = {
    'INR': 1.0, '₹': 1.0, 'RS': 1.0,
    'USD': 83.0, '$': 83.0, 'US$': 83.0,
    'EUR': 90.0, '€': 90.0,
    'GBP': 105.0, '£': 105.0,
    'SGD': 62.0, 'AED': 22.6, 'JPY': 0.55,
    'MYR': 18.0, 'RM': 18.0,
}
_SCALE_TO_ABS = {
    'raw': 1.0, 'rupees': 1.0, 'thousands': 1e3, 'lakhs': 1e5,
    'millions': 1e6, 'crores': 1e7, 'billions': 1e9,
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_crore(value, currency, scale):
    """Convert a native company-MIS figure to ₹ Crore. Returns None if unknown."""
    n = _num(value)
    if n is None:
        return None
    absf = _SCALE_TO_ABS.get((scale or 'raw').lower())
    if absf is None:
        return None
    fx = FX_TO_INR.get(str(currency or 'INR').upper())
    if fx is None:
        return None
    return n * absf * fx / 1e7


def normalize_company_row(row: dict) -> dict:
    """Add ₹Cr fields to a Portfolio row; keep native values under *_native."""
    currency = row.get('currency') or 'INR'
    scale = row.get('unit') or 'raw'
    for field in ('revenue_ttm', 'ebitda_ttm', 'cash_balance'):
        native = row.get(field)
        row[f'{field}_native'] = native
        row[field] = to_crore(native, currency, scale)
    return row
