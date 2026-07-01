"""
Value coercers — turn raw cell values into the correct Python type.
Universal: no fund-specific behavior, no sheet-specific rules.
"""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


_NULL_TEXTS = {'-', '--', 'n/a', 'na', 'nil', 'tbd', 'none', '—', '–'}


def to_decimal(v: Any) -> Optional[Decimal]:
    if v is None or v == '':
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:
            return None
        return Decimal(str(v))
    if isinstance(v, Decimal):
        return v
    s = str(v).strip()
    if not s or s.lower() in _NULL_TEXTS:
        return None
    # Universal decimal parse — strip units/prefixes BEFORE whitespace collapse
    # so "Rs 3,800 Cr" → "3800" cleanly (not "Rs3800Cr" which is unparseable).
    # 1. Descriptive prefix like "Target: ", "Est. ", "Approx: "
    s = re.sub(r'^\s*(target|estimate[d]?|approx|est|approx\.?|max|min)\s*[:\-]\s*',
               '', s, flags=re.I).strip()
    # 2. Currency prefix: "Rs ", "Rs.", "INR ", "₹ "
    s = re.sub(r'^(?:rs\.?|inr|usd|eur|gbp|₹|\$|£|€)\s*', '', s, flags=re.I).strip()
    # 3. Unit suffix: "Cr", "Crore", "Lakh", "x", "% p.a. ..." — use lookbehind
    #    that allows whitespace OR a word boundary (handles "3800Cr" and "3800 Cr")
    s = re.sub(r'\s*(cr|crore|crores|lakh|lakhs|mn|million|bn|billion|x)\b'
               r'.*$', '', s, flags=re.I).strip()
    # 4. Whitespace and thousands-separator commas
    s = re.sub(r'[₹\$£€,\s]', '', s)
    if s.endswith('%'):
        s = s[:-1]
    m = re.match(r'^\(([^)]+)\)$', s)  # (100) → -100
    if m:
        s = '-' + m.group(1)
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


_DATE_FORMATS = (
    '%Y-%m-%d', '%Y/%m/%d', '%d-%b-%Y', '%d %b %Y', '%d-%B-%Y', '%d %B %Y',
    '%d/%m/%Y', '%d-%m-%Y', '%d-%b-%y', '%b-%y', '%b %y', '%b %Y', '%B %Y',
    '%Y-%m-%d %H:%M:%S', '%d-%b-%Y %H:%M:%S',
)


def to_date(v: Any) -> Optional[date]:
    if v is None or v == '':
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s or s.lower() in _NULL_TEXTS:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def to_str(v: Any, maxlen: int = 1024) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in _NULL_TEXTS:
        return None
    return s[:maxlen]


# ── Canonical field type registry (used by _coerce) ──────────────────────────
_DATE_FIELDS = {
    'investment_date', 'tranche_date', 'exit_date', 'valuation_date', 'nav_date',
    'call_date', 'payment_due_date', 'distribution_date', 'commitment_date',
    'first_close_date', 'final_close_date', 'inception_date', 'incorporation_date',
    'due_date', 'filed_date', 'completed_date', 'calculation_date', 'realisation_date',
    'as_of_date', 'realized_date', 'end_date',
    'ppm_filing_date', 'value_date', 'review_date',
    # NOTE: 'period', 'period_start', 'period_end' intentionally EXCLUDED —
    # they need to stay as strings so the persister's _period_to_date() can
    # handle FY / quarter / month-year notations. Coercing to date here
    # drops rows whose period label is "FY 2022-23" (has no strptime fmt).
}
_INT_FIELDS = {
    'tranche_number', 'vintage_year', 'tenure_years', 'units_allocated', 'units_held',
    'total_units_outstanding', 'lp_count', 'ipev_level', 'shares_acquired',
    'no_of_shares', 'no_of_units', 'portfolio_companies', 'headcount',
    'total_employees', 'year_founded',
}
_BOOL_FIELDS = {
    'is_quoted', 'is_lead_investor', 'board_seat', 'is_favorable', 'is_gift_city',
    'side_letter_exists', 'beneficial_owner_identified',
    'is_land_border_country_investor', 'exceeds_50pct_threshold', 'str_filed',
    'kyc_completed', 'has_board_seat',
}
_STRING_FIELDS = {
    'company_name', 'sector', 'sub_sector', 'fund_name', 'scheme_name', 'investor_name',
    'investor_type', 'purpose', 'stage', 'round_name', 'instrument_type', 'methodology',
    'city', 'country', 'listing_exchange', 'exit_type', 'call_status',
    'distribution_type', 'distribution_status', 'commitment_status', 'company_cin',
    'company_pan', 'pan', 'isin', 'currency', 'buyer_name', 'valuer_name',
    'investment_status', 'headquarters_city', 'headquarters_country', 'notes',
    'co_investors', 'founder_names', 'financial_year', 'source_of_distribution',
    'source', 'close_type', 'gain_loss_nature', 'description', 'business_description',
    'kpi_name', 'line_item', 'call_purpose', 'ownership_pct_after',
    'promoters', 'domicile', 'regulation', 'custodian_name', 'trustee_name',
    'manager_name', 'auditor_name', 'legal_counsel', 'rta_name', 'sponsor_name',
    'board_nominee_name', 'key_promoters', 'current_stage', 'stage_at_first_investment',
    'exit_route', 'lp_id', 'company_id',
}


def coerce_by_canonical(canon: str, raw: Any) -> Any:
    if raw is None or raw == '':
        return None
    key = canon.lower()
    if key in _DATE_FIELDS or key.endswith('_date') or key.endswith('_dt'):
        return to_date(raw)
    if key in _INT_FIELDS or key.endswith('_count') or key.endswith('_num'):
        d = to_decimal(raw)
        return int(d) if d is not None else None
    if key in _BOOL_FIELDS or key.startswith('is_'):
        s = to_str(raw)
        if s is None:
            return None
        s = s.lower().strip()
        if s in ('true', 'yes', 'y', '1', 'listed', 'quoted', 'active'):
            return True
        if s in ('false', 'no', 'n', '0', 'unlisted', 'unquoted', 'inactive'):
            return False
        return None
    if key in _STRING_FIELDS or key.endswith('_id') or key.endswith('_name'):
        return to_str(raw)
    d = to_decimal(raw)
    if d is not None:
        return d
    return to_str(raw)


def extract_pct(v: Any) -> Optional[Decimal]:
    """Extract a percentage number from 'X% p.a. ...' or bare 20."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    s = str(v)
    m = re.search(r'(\d+(?:\.\d+)?)\s*%', s)
    if m:
        return Decimal(m.group(1))
    m = re.search(r'^(\d+(?:\.\d+)?)', s.strip())
    if m:
        return Decimal(m.group(1))
    return None
