"""
Phase 2 thin DB persister.

Takes the extracted JSON from single_call_extractor and writes it into the
Django models. Uses update_or_create with natural keys that include enough
discriminators to prevent the bugs we saw in legacy (e.g. INV001+INV002 of
same company collapsing into one Investment row).

Surface: persist_phase2(data, import_file, organization, user, progress_cb)
        → result dict (counts, scheme_id, fund_id)
"""

import logging
import re
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from django.db import transaction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

def _first_present(*values):
    """Return the first value that is not None. Unlike `a or b`, this does
    NOT treat 0 / '' / False as missing — only None counts as absent.
    Required for waterfall metrics where 0 is a real, valid value (no carry
    earned) that must be persisted, not silently dropped."""
    for v in values:
        if v is not None:
            return v
    return None


def _d(v) -> Optional[Decimal]:
    if v is None or v == '':
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _date(v) -> Optional[date]:
    if v is None or v == '':
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    for fmt in ('%Y-%m-%d', '%d-%b-%Y', '%d/%m/%Y', '%Y/%m/%d', '%d-%m-%Y', '%b-%y', '%d-%b-%y'):
        try:
            return datetime.strptime(str(v)[:10] if fmt != '%b-%y' and fmt != '%d-%b-%y' else str(v), fmt).date()
        except ValueError:
            continue
    return None


# Universal period-label parser. Handles common fund-data period notations
# without any keyword/file-specific hardcoding. Returns first day of period.
#
# Recognised patterns (in order):
#   2025-04-01 / 2025/04/01           → ISO date
#   FY 2024-25 / FY2024-25 / FY24-25  → Indian FY start (April 1, first year)
#   Q1 FY25 / Q2 FY 2024-25           → Indian fiscal quarter start
#   Q1 2025 / Q2-2024 / Q3 24         → calendar quarter start (Jan/Apr/Jul/Oct)
#   Mar-25 / Apr 2024 / March 2025    → first day of month
#   2025                              → April 1 of that year (FY-start convention)
#
# Returns None on no match — caller picks a fallback.
import calendar as _calendar
_MONTH_TO_NUM = {m.lower(): i for i, m in enumerate(_calendar.month_name) if m}
_MONTH_TO_NUM.update({m.lower(): i for i, m in enumerate(_calendar.month_abbr) if m})


def _period_to_date(v) -> Optional[date]:
    if v is None or v == '':
        return None
    # If already a date / datetime / pandas Timestamp, normalize via _date()
    direct = _date(v)
    if direct:
        return direct

    s = str(v).strip().lower()
    if not s:
        return None
    import re as _re

    # FY YYYY-YY → April 1 of starting year (Indian fiscal year convention)
    m = _re.search(r'^fy\s*(\d{2,4})(?:[-\s]+\d{2,4})?$', s)
    if m:
        yr = int(m.group(1))
        if yr < 100:
            yr += 2000
        return date(yr, 4, 1)

    # Q[1-4] FY YYYY-YY → Indian fiscal quarter start
    m = _re.search(r'^q([1-4])\s*fy\s*(\d{2,4})(?:[-\s]+\d{2,4})?$', s)
    if m:
        q = int(m.group(1))
        yr = int(m.group(2))
        if yr < 100:
            yr += 2000
        # Indian FY: Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar
        if q in (1, 2, 3):
            return date(yr, [None, 4, 7, 10][q], 1)
        else:  # Q4
            return date(yr + 1, 1, 1)

    # Q[1-4] YYYY → calendar quarter start
    m = _re.search(r'^q([1-4])[\s\-/]+(\d{2,4})$', s)
    if m:
        q = int(m.group(1))
        yr = int(m.group(2))
        if yr < 100:
            yr += 2000
        return date(yr, [None, 1, 4, 7, 10][q], 1)

    # Mon-YY / Mon-YYYY / MonthName YYYY
    m = _re.search(r'^([a-z]+)[\s\-/](\d{2,4})$', s)
    if m:
        mon_str, yr = m.group(1), int(m.group(2))
        if yr < 100:
            yr += 2000
        mn = _MONTH_TO_NUM.get(mon_str) or _MONTH_TO_NUM.get(mon_str[:3])
        if mn:
            return date(yr, mn, 1)

    # YYYY-MM
    m = _re.search(r'^(\d{4})[\-/](\d{1,2})$', s)
    if m:
        yr, mn = int(m.group(1)), int(m.group(2))
        if 1 <= mn <= 12:
            return date(yr, mn, 1)

    # Bare year
    m = _re.search(r'^(19|20)\d{2}$', s)
    if m:
        return date(int(s), 4, 1)  # default to Apr-1 (FY start convention)

    return None


def _int(v) -> Optional[int]:
    if v is None or v == '':
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _bool(v) -> Optional[bool]:
    if v is None or v == '':
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ('true', 'yes', 'y', '1', 'listed', 'quoted'):
        return True
    if s in ('false', 'no', 'n', '0', 'unlisted', 'unquoted'):
        return False
    return None


def _str(v, maxlen: int = 255) -> str:
    if v is None:
        return ''
    s = str(v).strip()
    return s[:maxlen]


def _ipev_to_int(v):
    # IPEV uses digits 1/2/3 (or Roman I/II/III) in every language — extract
    # whichever is present. Returns None for anything that isn't a valid level.
    if v is None or v == '' or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        return n if n in (1, 2, 3) else None
    try:
        s = str(v).strip()
    except Exception:
        return None
    if not s:
        return None
    m = re.search(r'(?<!\d)([123])(?!\d)', s)
    if m:
        return int(m.group(1))
    s_up = s.upper()
    if re.search(r'\bIII\b', s_up): return 3
    if re.search(r'\bII\b', s_up):  return 2
    if re.search(r'\bI\b', s_up):   return 1
    return None


def _enum(v, choices: dict, default: str = '') -> str:
    """Map a free-text value to a model enum using simple substring matching.
    `choices` is {raw_substring: canonical_value}.
    """
    if v is None:
        return default
    s = str(v).strip().lower()
    for key, canonical in choices.items():
        if key in s:
            return canonical
    return default


def _set_if(d: dict, key: str, value):
    """Only put `value` in `d[key]` if value is non-empty/non-null.
    This preserves existing DB values when re-importing a sparser file.
    """
    if value not in (None, '', 0):
        d[key] = value
    elif value == 0 and isinstance(value, (int, float, Decimal)):
        # zero is a legitimate numeric value
        d[key] = value


def _apply_model_defaults(model_class, fields: dict) -> dict:
    """Universal NOT-NULL safety net.

    For EVERY NOT-NULL field on `model_class` that the source data doesn't
    provide, fill in a type-appropriate neutral default so the row persists
    without an IntegrityError. Future-proof: new NOT-NULL fields added to
    models later are handled automatically — zero per-field maintenance.

    Rules:
      • Skips fields that are nullable, auto-generated, PKs, or have a model
        default — Django handles those.
      • Skips FK / M2M relations — those are passed in at the call site.
      • Skips fields already populated in `fields`.
      • Otherwise applies a neutral default based on Django internal type:
            Decimal/Number → 0
            CharField with choices → first choice
            CharField / TextField → ''
            Date → today's date
            DateTime → now (UTC)
            Boolean → False
            JSON → {} or [] matching the model default factory
    """
    from django.utils import timezone as _tz
    # Collect every field that is part of a uniqueness constraint
    # (unique_together OR unique=True). NEVER default these — they are
    # lookup keys; defaulting them would corrupt rows via update_or_create.
    unique_constraint_fields: set[str] = set()
    for ut in getattr(model_class._meta, 'unique_together', ()) or ():
        if isinstance(ut, (list, tuple)):
            unique_constraint_fields.update(ut)
    for constraint in getattr(model_class._meta, 'constraints', []) or []:
        for f in getattr(constraint, 'fields', []) or []:
            unique_constraint_fields.add(f)
    for f in model_class._meta.get_fields():
        if getattr(f, 'unique', False) and not getattr(f, 'primary_key', False):
            unique_constraint_fields.add(f.name)

    for field in model_class._meta.get_fields():
        # 1. skip non-concrete fields (reverse relations, generic relations)
        if not getattr(field, 'concrete', False):
            continue
        if getattr(field, 'auto_created', False) and not field.is_relation:
            continue
        if getattr(field, 'primary_key', False):
            continue
        # 2. skip FK/M2M — handled at the call site
        if field.is_relation:
            continue
        # 3. nullable or has a default → Django handles it
        if getattr(field, 'null', True):
            continue
        if field.has_default():
            continue
        # 4. already populated by caller
        name = field.name
        if name in fields and fields[name] not in (None, ''):
            continue
        # 5. UNIVERSAL SAFETY: never default a field that participates in a
        # uniqueness constraint — those must come from the caller as
        # lookup keys; defaulting them corrupts rows on update_or_create.
        if name in unique_constraint_fields:
            continue

        internal = field.get_internal_type()
        if internal in ('DecimalField',):
            fields[name] = Decimal('0')
        elif internal in ('IntegerField', 'FloatField', 'PositiveIntegerField',
                          'BigIntegerField', 'SmallIntegerField',
                          'PositiveSmallIntegerField'):
            fields[name] = 0
        elif internal == 'CharField':
            if getattr(field, 'choices', None):
                # Pick the first valid choice (typically the most neutral)
                fields[name] = field.choices[0][0]
            else:
                fields[name] = ''
        elif internal == 'TextField':
            fields[name] = ''
        elif internal == 'DateField':
            fields[name] = date.today()
        elif internal == 'DateTimeField':
            fields[name] = _tz.now()
        elif internal == 'BooleanField':
            fields[name] = False
        elif internal == 'JSONField':
            # Respect the field's default factory if it's dict vs list
            try:
                default_factory = field.default
                fields[name] = default_factory() if callable(default_factory) else (default_factory or {})
            except Exception:
                fields[name] = {}
        elif internal == 'EmailField':
            fields[name] = ''
        elif internal in ('URLField', 'SlugField'):
            fields[name] = ''
        else:
            # Unknown type — best-effort empty string; if the field rejects
            # it the caller will see a clear error
            fields[name] = ''
    return fields


def _safe_save(model_class, lookup_kwargs: dict, defaults: dict, mode: str = 'update_or_create'):
    """Universal wrapper for update_or_create / get_or_create.

    Guarantees:
      1. NOT-NULL fields without callers' values get type-neutral defaults
         via `_apply_model_defaults`.
      2. Lookup keys ALWAYS win — any matching key is removed from defaults
         so the caller's lookup value is never overwritten by a helper default
         (this was the silent "investor_name set to ''" bug).
      3. Uniqueness-constraint fields are also auto-excluded by the helper.

    Returns (obj, created) — same shape as Django's update_or_create.
    """
    defaults = _apply_model_defaults(model_class, dict(defaults or {}))
    # Lookup keys always win over defaults
    for k in (lookup_kwargs or {}).keys():
        defaults.pop(k, None)
    mgr = model_class.objects
    if mode == 'get_or_create':
        return mgr.get_or_create(**lookup_kwargs, defaults=defaults)
    return mgr.update_or_create(**lookup_kwargs, defaults=defaults)


def audit_persister_models() -> dict:
    """One-shot pre-flight audit. Prints what defaults `_apply_model_defaults`
    would emit for each model the persister touches. Used to verify coverage
    before a live import. Returns {model_name: {field: default}}.
    """
    from funds.models import Fund, Scheme
    from lp.models import Investor, Commitment, CapitalCall, Distribution
    from investments.models import (
        PortfolioCompany, Investment, InvestmentTranche, Valuation,
        ExitEvent, PortfolioKPI, KPIDefinition,
    )
    from accounting.models import NAVRecord, CarriedInterest
    from dataimport.models import FundMetric
    out = {}
    for m in [Fund, Scheme, Investor, Commitment, CapitalCall, Distribution,
              PortfolioCompany, Investment, InvestmentTranche, Valuation,
              ExitEvent, PortfolioKPI, KPIDefinition, NAVRecord,
              CarriedInterest, FundMetric]:
        empty: dict = {}
        _apply_model_defaults(m, empty)
        if empty:
            out[m.__name__] = empty
    return out


# Enum maps (canonical key sets pulled from the Django models)
_INVESTOR_TYPE_MAP = {
    'sovereign': 'sovereign', 'pension': 'pension', 'insurance': 'insurance',
    'family office': 'family_office', 'hni': 'individual', 'uhni': 'individual',
    'individual': 'individual', 'fund of funds': 'fund_of_funds', 'fof': 'fund_of_funds',
    'dfi': 'company', 'multilateral': 'company', 'bilateral': 'company',
    'corporate': 'company', 'company': 'company', 'fpi': 'fpi', 'nri': 'nri',
    'huf': 'huf', 'trust': 'trust', 'llp': 'llp', 'amc': 'company',
    'gp': 'company', 'general partner': 'company', 'foreign pension': 'pension',
    'domestic pension': 'pension',
}

_INSTRUMENT_MAP = {
    'ccps': 'ccps', 'ccd': 'ccd', 'ncd': 'ncd', 'ocd': 'convertible_note',
    'equity': 'equity', 'ord': 'equity', 'pref': 'ccps', 'safe': 'safe',
    'term loan': 'term_loan', 'convertible': 'convertible_note',
}

_EXIT_TYPE_MAP = {
    'ipo': 'ipo', 'listing': 'ipo',
    'm&a': 'merger_acquisition', 'merger': 'merger_acquisition',
    'acquisition': 'merger_acquisition', 'strategic': 'merger_acquisition',
    'trade sale': 'merger_acquisition',
    'secondary': 'secondary_sale', 'secondaries': 'secondary_sale',
    'buyback': 'buyback', 'mbo': 'buyback',
    'write-off': 'write_off', 'write off': 'write_off', 'impairment': 'write_off',
}

_STATUS_MAP = {
    'active': 'active', 'fully_exited': 'fully_exited', 'fully exited': 'fully_exited',
    'partially_exited': 'partially_exited', 'partially exited': 'partially_exited',
    'written_off': 'written_off', 'written off': 'written_off', 'write-off': 'written_off',
}

_DIST_TYPE_MAP = {
    'return_of_capital': 'return_of_capital', 'return of capital': 'return_of_capital',
    'capital': 'return_of_capital',
    'stcg': 'stcg', 'ltcg': 'ltcg', 'interest': 'interest',
    'dividend': 'dividend', 'carry': 'carry',
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def persist_phase2(data: dict, import_file, organization, user,
                   progress_cb: Optional[Callable] = None) -> dict:
    def _p(pct, msg):
        if progress_cb:
            try:
                progress_cb(pct, msg)
            except Exception:
                pass

    from funds.models import Fund, Scheme

    fm = data.get('fund_master') or {}
    fp = data.get('fund_performance') or {}

    fund_name = _str(fm.get('fund_name'), 255)
    if not fund_name:
        stem = (getattr(import_file, 'original_filename', '') or '').rsplit('.', 1)[0]
        fund_name = _str(stem, 255) or 'Unnamed Fund'
    scheme_name = _str(fm.get('scheme_name'), 255) or fund_name

    counts = {}

    with transaction.atomic():
        # ---- Fund ----
        _p(81, 'Phase 2: Fund + Scheme…')
        fund = _persist_fund(organization, fund_name, fm, user)
        scheme = _persist_scheme(fund, scheme_name, fm)
        import_file.fund = fund
        import_file.fund_name = fund_name
        import_file.save(update_fields=['fund', 'fund_name'])

        # ---- Investors + Commitments ----
        _p(83, 'Phase 2: Investors + commitments…')
        counts['investors'] = _persist_investors(organization, data.get('investors') or [])
        counts['commitments'] = _persist_commitments(
            organization, scheme,
            data.get('commitments') or data.get('investors') or []
        )

        # ---- Capital Calls ----
        _p(85, 'Phase 2: Capital calls…')
        counts['capital_calls'] = _persist_capital_calls(scheme, data.get('capital_calls') or [], user)

        # ---- Portfolio Companies + Investments + Tranches ----
        _p(87, 'Phase 2: Portfolio companies + investments + tranches…')
        co_count, inv_count, tr_count = _persist_portfolio(
            organization, scheme, data.get('portfolio_investments') or [],
            data.get('valuations') or [], user,
        )
        counts['portfolio_companies'] = co_count
        counts['investments'] = inv_count
        counts['tranches'] = tr_count

        # ---- Valuations ----
        _p(89, 'Phase 2: Valuations…')
        counts['valuations'] = _persist_valuations(scheme, data.get('valuations') or [])

        # ---- Quoted/Unquoted ----
        _p(90, 'Phase 2: Quoted/Unquoted listing status…')
        counts['quoted_updates'] = _persist_quoted(organization, data.get('quoted_unquoted') or [])

        # ---- Exits + Distributions ----
        _p(91, 'Phase 2: Exits + distributions…')
        counts['exits'] = _persist_exits(scheme, data.get('exits') or [], user)
        counts['distributions'] = _persist_distributions(scheme, data.get('distributions') or [], user)

        # ---- NAV records (FULL monthly walk) ----
        _p(93, 'Phase 2: NAV walk…')
        counts['nav_records'] = _persist_nav_records(scheme, data.get('nav_records') or [])

        # ---- CarriedInterest + FundMetrics ----
        _p(95, 'Phase 2: Carry + fund metrics…')
        _persist_carried_interest(scheme, data.get('waterfall') or {}, fp)
        counts['fund_metrics'] = _persist_fund_metrics(
            organization, scheme, fp, data.get('waterfall') or {},
            data.get('valuations') or [], import_file,
            reconciliation=data.get('__reconciliation__') or None,
            fm=fm,
        )

        # ---- Per-company periodic KPIs ----
        # Universal: fan out KPIs from BOTH dedicated KPI sheets and from
        # monthly P&L / BS / CF rows so the company-matrix dashboard sees
        # EVERY metric value Gemini extracted, regardless of which sheet
        # it came from. Previously only portfolio_kpis_periodic was read.
        _p(97, 'Phase 2: Per-company periodic KPIs + monthly financials…')
        combined_kpi_source = (
            (data.get('portfolio_kpis_periodic') or [])
            + (data.get('monthly_pl_rows') or [])
            + (data.get('monthly_bs_rows') or [])
            + (data.get('monthly_cf_rows') or [])
        )
        counts['portfolio_kpis'] = _persist_portfolio_kpis(
            organization, scheme, combined_kpi_source,
        )

        # ---- Budget vs Actual ----
        _p(98, 'Phase 2: Budget vs Actual…')
        counts['budget_vs_actual'] = _persist_budget_vs_actual(
            organization, fund, data.get('budget_vs_actual') or []
        )

    summary = (
        f'F:{counts.get("portfolio_companies",0)} I:{counts.get("investments",0)} '
        f'T:{counts.get("tranches",0)} V:{counts.get("valuations",0)} '
        f'LP:{counts.get("investors",0)} C:{counts.get("commitments",0)} '
        f'CC:{counts.get("capital_calls",0)} D:{counts.get("distributions",0)} '
        f'E:{counts.get("exits",0)} NAV:{counts.get("nav_records",0)} '
        f'KPI:{counts.get("portfolio_kpis",0)}'
    )
    return {
        'counts': counts,
        'summary': summary,
        'fund_id': str(fund.id),
        'scheme_id': str(scheme.id),
    }


# ---------------------------------------------------------------------------
# Persisters per section
# ---------------------------------------------------------------------------

def _persist_fund(organization, fund_name: str, fm: dict, user):
    from funds.models import Fund, FundCategory

    cat_code_raw = _str(fm.get('sebi_category_code'), 16).upper()
    # Accept 'CAT_II', 'CAT II', 'AIF CAT II', 'CATEGORY II', etc.
    cat_code = None
    for token in ('CAT_III_LVF', 'CAT_III', 'CAT_II', 'CAT_I_VCF', 'CAT_I'):
        if token in cat_code_raw.replace(' ', '_'):
            cat_code = token
            break
    cat_obj = None
    if cat_code:
        cat_obj = FundCategory.objects.filter(sebi_category_code=cat_code).first()

    defaults = {}
    _set_if(defaults, 'sebi_registration_number', _str(fm.get('sebi_registration_number'), 50))
    if cat_obj:
        defaults['fund_category'] = cat_obj
    _set_if(defaults, 'structure_type', _str(fm.get('structure_type'), 10).lower() or 'trust')
    _set_if(defaults, 'pan', _str(fm.get('fund_pan'), 10))
    _set_if(defaults, 'gstin', _str(fm.get('fund_gstin'), 15))
    _set_if(defaults, 'inception_date', _date(fm.get('inception_date')))
    _set_if(defaults, 'corpus_target', _d(fm.get('corpus_target') or fm.get('scheme_size')))
    _set_if(defaults, 'base_currency', _str(fm.get('base_currency'), 3) or 'INR')
    _set_if(defaults, 'is_gift_city', _bool(fm.get('is_gift_city')))
    _set_if(defaults, 'fund_status', _str(fm.get('fund_status'), 15).lower() or 'active')
    if user and not Fund.objects.filter(organization=organization, name=fund_name).exists():
        defaults['created_by'] = user

    fund, _created = _safe_save(Fund,
        lookup_kwargs={'organization': organization, 'name': fund_name},
        defaults=defaults,
    )
    return fund


def _persist_scheme(fund, scheme_name: str, fm: dict):
    from funds.models import Scheme

    defaults = {}
    _set_if(defaults, 'vintage_year', _int(fm.get('vintage_year')))
    _set_if(defaults, 'first_close_date', _date(fm.get('first_close_date')))
    _set_if(defaults, 'final_close_date', _date(fm.get('final_close_date')))
    _set_if(defaults, 'scheme_size', _d(fm.get('scheme_size') or fm.get('corpus_target')))
    _set_if(defaults, 'tenure_years', _int(fm.get('tenure_years')))
    _set_if(defaults, 'hurdle_rate_pct', _d(fm.get('hurdle_rate_pct')))
    _set_if(defaults, 'carry_pct', _d(fm.get('carry_pct')))
    _set_if(defaults, 'carry_type', _str(fm.get('carry_type'), 10).lower() or 'european')
    _set_if(defaults, 'management_fee_basis', _str(fm.get('management_fee_basis'), 16).lower() or 'committed')
    _set_if(defaults, 'management_fee_pct', _d(fm.get('management_fee_pct')))
    _set_if(defaults, 'sponsor_commitment_pct', _d(fm.get('sponsor_commitment_pct')))
    _set_if(defaults, 'scheme_status', _str(fm.get('scheme_status'), 16).lower() or 'investing')

    scheme, _ = _safe_save(Scheme,
        lookup_kwargs={'fund': fund, 'name': scheme_name},
        defaults=defaults,
    )
    return scheme


def _persist_investors(organization, investors: list) -> int:
    from lp.models import Investor
    count = 0
    for inv in investors:
        if not isinstance(inv, dict):
            continue
        name = _str(inv.get('investor_name') or inv.get('name'), 255)
        if not name or name.lower() in ('totals', 'total'):
            continue
        itype = _enum(inv.get('investor_type'), _INVESTOR_TYPE_MAP, default='individual')
        defaults = {}
        defaults['investor_type'] = itype
        _set_if(defaults, 'contact_person', _str(inv.get('contact_person'), 255))
        _set_if(defaults, 'email', _str(inv.get('email'), 254))
        _set_if(defaults, 'phone', _str(inv.get('phone'), 20))
        _set_if(defaults, 'address', _str(inv.get('address'), 500))
        _set_if(defaults, 'city', _str(inv.get('city'), 100))
        _set_if(defaults, 'state', _str(inv.get('state'), 100))
        _set_if(defaults, 'country', _str(inv.get('country'), 100) or 'India')
        _set_if(defaults, 'pan', _str(inv.get('pan'), 10))
        _set_if(defaults, 'kyc_status',
                _enum(inv.get('kyc_status'),
                      {'completed': 'completed', 'verified': 'completed', 'pending': 'pending'},
                      default='pending'))

        _safe_save(Investor,
            lookup_kwargs={'organization': organization, 'investor_name': name},
            defaults=defaults,
        )
        count += 1
    return count


def _persist_commitments(organization, scheme, rows: list) -> int:
    from lp.models import Investor, Commitment
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _str(row.get('investor_name') or row.get('name'), 255)
        if not name or name.lower() in ('totals', 'total'):
            continue
        amt = _d(row.get('commitment_amount') or row.get('commitment') or row.get('commitment_cr'))
        if not amt or amt <= 0:
            continue
        investor = Investor.objects.filter(
            organization=organization, investor_name=name
        ).first()
        if not investor:
            continue

        defaults = {'commitment_amount': amt}
        _set_if(defaults, 'commitment_date', _date(row.get('commitment_date')))
        ct = _str(row.get('close_type'), 32).lower()
        if ct:
            if 'first' in ct or 'initial' in ct:
                defaults['close_type'] = 'first_close'
            elif 'final' in ct:
                defaults['close_type'] = 'final_close'
            elif 'subsequent' in ct or 'subseq' in ct:
                defaults['close_type'] = 'subsequent_close'
        _set_if(defaults, 'units_allocated', _d(row.get('units_allocated')))
        cs = _str(row.get('commitment_status') or row.get('status'), 16).lower()
        if cs in ('active', 'defaulted', 'transferred', 'cancelled'):
            defaults['commitment_status'] = cs

        _safe_save(Commitment,
            lookup_kwargs={'investor': investor, 'scheme': scheme},
            defaults=defaults,
        )
        count += 1
    return count


def _persist_capital_calls(scheme, rows: list, user) -> int:
    from lp.models import CapitalCall
    count = 0
    # Sort by call_date so auto-numbered calls (when Gemini omits call_number)
    # are numbered chronologically — Call #1 is the earliest.
    sorted_rows = sorted(
        [r for r in rows if isinstance(r, dict)],
        key=lambda r: _date(r.get('call_date')) or date.max,
    )
    for idx, row in enumerate(sorted_rows, start=1):
        # Universal call_number resolution:
        #   1. Use explicit call_number / call_ref digit if Gemini emitted one
        #   2. Otherwise auto-number from chronological row index (1..N)
        # Last run Gemini omitted call_number entirely → every row was skipped
        # (CC:0). With this fallback every call persists regardless.
        cn_raw = row.get('call_number') or row.get('call_ref')
        cn = _str(cn_raw, 16) if cn_raw is not None else ''
        m = re.search(r'\d+', cn) if cn else None
        call_number = int(m.group()) if m else idx

        # NOT-NULL fields on CapitalCall: call_date, payment_due_date,
        # call_percentage, total_call_amount. Provide zero/today defaults
        # so a row with missing percentage (LLM omitted) still persists.
        call_date = _date(row.get('call_date')) or date.today()
        pay_due   = _date(row.get('payment_due_date')) or call_date
        pct       = _d(row.get('call_percentage')) or Decimal('0')
        amount    = _d(row.get('total_call_amount') or row.get('amount')) or Decimal('0')

        defaults = {
            'call_date': call_date,
            'payment_due_date': pay_due,
            'call_percentage': pct,
            'total_call_amount': amount,
        }
        _set_if(defaults, 'purpose', _str(row.get('purpose'), 500))
        cs = _str(row.get('call_status') or row.get('status'), 16).lower()
        if cs in ('draft', 'approved', 'sent', 'paid', 'defaulted', 'funded'):
            defaults['call_status'] = 'paid' if cs == 'funded' else cs
        if user:
            defaults.setdefault('created_by', user)

        _safe_save(CapitalCall,
            lookup_kwargs={'scheme': scheme, 'call_number': call_number},
            defaults=defaults,
        )
        count += 1
    return count


def _resolve_investment_date(raw_date, scheme) -> Optional[date]:
    """Universal fallback chain for an investment's effective date.

    Order (preferred → least preferred):
      1. The raw date from the JSON row (most accurate)
      2. Scheme.first_close_date  — fund started deploying capital here
      3. Scheme.final_close_date  — fundraising closed
      4. date(Scheme.vintage_year, 7, 1)  — mid-year approximation
      5. None  — caller decides what to do

    NEVER falls back to date.today(). For an investment that was made in
    2021 and has 2025 valuations on the books, stamping today (2026) would
    make IRR mathematically broken (terminal value before investment date).
    """
    d = _date(raw_date)
    if d:
        return d
    fc = getattr(scheme, 'first_close_date', None)
    if fc:
        return fc
    fl = getattr(scheme, 'final_close_date', None)
    if fl:
        return fl
    vy = getattr(scheme, 'vintage_year', None)
    if vy:
        try:
            return date(int(vy), 7, 1)
        except (TypeError, ValueError):
            pass
    return None


def _compute_investment_irr(tranche_cashflows: list, terminal_value, as_of_date) -> Optional[Decimal]:
    """Per-investment XIRR fallback for Rule 21.

    tranche_cashflows: list of (date, amount) where amount is the money the
                       fund put IN (positive value).
    terminal_value:    the latest fair_value_of_holding for the investment.
    as_of_date:        valuation date for the terminal value.

    Returns IRR as a percentage (e.g. Decimal('18.5')) or None if uncomputable.
    Uses a simple bisection over annualised rate.
    """
    if not tranche_cashflows or terminal_value is None or as_of_date is None:
        return None
    try:
        terminal_value = float(terminal_value)
    except (TypeError, ValueError):
        return None
    if terminal_value <= 0:
        return None
    flows = []
    for d, amt in tranche_cashflows:
        if d is None or amt is None:
            continue
        try:
            flows.append((d, -float(amt)))  # cash out of fund = negative
        except (TypeError, ValueError):
            continue
    if not flows:
        return None
    flows.append((as_of_date, terminal_value))  # terminal inflow = positive
    flows.sort(key=lambda x: x[0])
    base = flows[0][0]

    def npv(rate: float) -> float:
        total = 0.0
        for d, a in flows:
            years = (d - base).days / 365.25
            total += a / ((1 + rate) ** years)
        return total

    lo, hi = -0.99, 10.0
    try:
        if npv(lo) * npv(hi) > 0:
            return None
        for _ in range(80):
            mid = (lo + hi) / 2.0
            v = npv(mid)
            if abs(v) < 1e-6:
                break
            if npv(lo) * v < 0:
                hi = mid
            else:
                lo = mid
        return Decimal(str(round(mid * 100, 4)))
    except (ZeroDivisionError, OverflowError, ValueError):
        return None


def _persist_portfolio(organization, scheme, rows: list, valuation_rows: list, user) -> tuple[int, int, int]:
    """Each row from portfolio_investments becomes:
       - ONE PortfolioCompany (deduped by org+name)
       - ONE Investment per (scheme, company, stage, investment_date) — fixes B1
       - ONE InvestmentTranche per Investment row (natural_key built from above)

    Rule 21 fallback: if Gemini omits per-investment `irr_pct`, compute it
    from the tranche cashflows + the latest fair_value_of_holding.
    """
    from investments.models import PortfolioCompany, Investment, InvestmentTranche

    # Index latest FV holding + valuation_date per company for the IRR fallback.
    latest_fv_by_company: dict[str, tuple[date, Decimal]] = {}
    for vr in (valuation_rows or []):
        if not isinstance(vr, dict):
            continue
        cname = _str(vr.get('company_name'), 255)
        vdate = _date(vr.get('valuation_date'))
        fvh = _d(vr.get('fair_value_of_holding') or vr.get('fv_holding'))
        if not cname or not vdate or fvh is None or fvh <= 0:
            continue
        prev = latest_fv_by_company.get(cname)
        if prev is None or vdate > prev[0]:
            latest_fv_by_company[cname] = (vdate, fvh)

    # First pass: companies (unique by name)
    company_map = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _str(row.get('company_name'), 255)
        if not name or name.lower() in ('totals', 'total'):
            continue
        if name not in company_map:
            defaults = {}
            _set_if(defaults, 'cin', _str(row.get('company_cin'), 21))
            _set_if(defaults, 'pan', _str(row.get('company_pan'), 10))
            _set_if(defaults, 'sector', _str(row.get('sector'), 100))
            _set_if(defaults, 'sub_sector', _str(row.get('sub_sector'), 100))
            _set_if(defaults, 'incorporation_date', _date(row.get('incorporation_date')))
            _set_if(defaults, 'headquarters_city', _str(row.get('headquarters_city') or row.get('city'), 100))
            _set_if(defaults, 'headquarters_country',
                    _str(row.get('headquarters_country') or row.get('country'), 100) or 'India')
            _set_if(defaults, 'website', _str(row.get('website'), 500))
            fnames = row.get('founder_names')
            if fnames:
                if isinstance(fnames, str):
                    fnames = [s.strip() for s in re.split(r'[,;]', fnames) if s.strip()]
                if isinstance(fnames, list):
                    defaults['founder_names'] = fnames
            cinv = row.get('co_investors') or row.get('co_investor') or row.get('syndicate')
            if cinv:
                if isinstance(cinv, str):
                    cinv = [s.strip() for s in re.split(r'[,;|/]', cinv) if s.strip()]
                if isinstance(cinv, list):
                    defaults['co_investors'] = [str(x).strip() for x in cinv if str(x).strip()]
            iq = _bool(row.get('is_quoted'))
            if iq is not None:
                defaults['is_quoted'] = iq
            _set_if(defaults, 'listing_exchange', _str(row.get('listing_exchange'), 16))

            co, _ = _safe_save(PortfolioCompany,
                lookup_kwargs={'organization': organization, 'name': name},
                defaults=defaults,
            )
            company_map[name] = co

    # ── Investment + Tranche persistence ──
    # DB unique constraints:
    #   Investment       = (scheme, company_name, instrument_type, stage)
    #   InvestmentTranche = (investment, tranche_number)
    #
    # `stage` (round name) is part of the natural key so that Series A and
    # Series B of the same company persist as DISTINCT Investments — each
    # with its own cost basis, FMV, MOIC, IRR. Source rows are grouped by
    # (company, instrument, stage_or_fallback).
    #
    # Universal fallback chain for the stage discriminator (every workbook
    # has at least one of these for every row):
    #   row.stage  →  row.round_name  →  str(row.investment_date)  →  'initial'
    #
    # Rows that share the same fallback key represent multiple tranches of
    # one round (e.g. Series A drawn down in two cheques on different dates
    # under the same stage label) — they collapse into one Investment with
    # multiple InvestmentTranche rows.

    def _stage_key(row: dict) -> str:
        """Universal stage discriminator. Never returns empty."""
        s = _str(row.get('stage') or row.get('round_name'), 100).strip()
        if s:
            return s
        d = _date(row.get('investment_date') or row.get('tranche_date'))
        if d:
            return d.isoformat()
        return 'initial'

    # 1. Group source rows by (company_name, instrument_type, stage_key)
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _str(row.get('company_name'), 255)
        if not name or name not in company_map:
            continue
        amount = _d(row.get('total_invested') or row.get('tranche_amount') or row.get('amount'))
        if not amount:
            continue  # no amount → not a real investment row
        instrument = _enum(row.get('instrument_type'), _INSTRUMENT_MAP, default='equity')
        stage = _stage_key(row)
        groups.setdefault((name, instrument, stage), []).append(row)

    inv_count = 0
    tr_count = 0
    for (name, instrument, stage), group_rows in groups.items():
        co = company_map[name]

        # Sort by investment_date so tranche_number 1 = earliest round
        def _row_date(r):
            return _date(r.get('investment_date') or r.get('tranche_date')) or date.max
        group_rows.sort(key=_row_date)

        # Aggregate group-level fields
        total_amount = sum(
            (_d(r.get('total_invested') or r.get('tranche_amount') or r.get('amount')) or Decimal('0'))
            for r in group_rows
        )
        first_row = group_rows[0]
        latest_row = group_rows[-1]
        # Latest ownership %, lead, board come from the most recent round
        # (it best reflects current ownership state)
        inv_defaults = {
            'company_name': name,
            'instrument_type': instrument,
            'stage': stage,  # part of the natural key — always non-empty
            'total_invested': total_amount,
            'investment_date': (
                _resolve_investment_date(first_row.get('investment_date'), scheme)
                or date.today()
            ),
            'currency': _str(latest_row.get('currency'), 3) or 'INR',
        }
        _set_if(inv_defaults, 'ownership_pct', _d(latest_row.get('ownership_pct')))
        _set_if(inv_defaults, 'percentage_stake_fully_diluted',
                _d(latest_row.get('fd_pct') or latest_row.get('percentage_stake_fully_diluted')))
        _set_if(inv_defaults, 'sector', _str(latest_row.get('sector') or first_row.get('sector'), 100))
        gemini_irr = _d(latest_row.get('irr_pct'))
        if gemini_irr is None:
            # Rule 21 fallback — compute IRR from tranche cashflows + latest FV.
            # Universal: if a row lacks an explicit investment_date, fall back to
            # scheme.first_close_date (NOT date.today() — that flips chronology).
            tcf = []
            for r in group_rows:
                td = _resolve_investment_date(
                    r.get('investment_date') or r.get('tranche_date'), scheme,
                )
                tamt = _d(r.get('total_invested') or r.get('tranche_amount') or r.get('amount'))
                if td and tamt:
                    tcf.append((td, tamt))
            term = latest_fv_by_company.get(name)
            if tcf and term:
                gemini_irr = _compute_investment_irr(tcf, term[1], term[0])
        # Use update_or_create on irr_pct directly so a legitimate computed 0
        # (or negative) IRR overwrites stale data. _set_if skips 0 — wrong here.
        if gemini_irr is not None:
            inv_defaults['irr_pct'] = gemini_irr
        _set_if(inv_defaults, 'board_seat', _bool(latest_row.get('board_seat')))
        _set_if(inv_defaults, 'is_lead_investor', _bool(latest_row.get('is_lead_investor')))
        status = _enum(latest_row.get('investment_status') or latest_row.get('status'),
                       _STATUS_MAP, default='active')
        inv_defaults['status'] = status
        if 'portfolio_company' not in inv_defaults:
            inv_defaults['portfolio_company'] = co
        if user:
            inv_defaults.setdefault('created_by', user)

        inv, created = _safe_save(Investment,
            lookup_kwargs={'scheme': scheme, 'company_name': name,
                           'instrument_type': instrument, 'stage': stage},
            defaults=inv_defaults,
        )
        inv_count += 1

        # Write one Tranche per source row, numbered sequentially (1..N)
        for idx, row in enumerate(group_rows, start=1):
            t_amount = _d(row.get('total_invested') or row.get('tranche_amount') or row.get('amount')) or Decimal('0')
            t_date = (
                _resolve_investment_date(
                    row.get('investment_date') or row.get('tranche_date'), scheme,
                )
                or date.today()
            )
            tnum = _int(row.get('tranche_number')) or idx
            round_name = _str(row.get('round_name') or row.get('stage'), 64)
            nat_key = f'{co.name}::{instrument}::T{tnum}'[:128]
            tdefs = {
                'amount': t_amount,
                'date': t_date,
                'natural_key': nat_key,
            }
            _set_if(tdefs, 'shares_acquired', _d(row.get('shares_acquired')))
            _set_if(tdefs, 'price_per_share', _d(row.get('price_per_share')))
            _set_if(tdefs, 'pre_money_valuation', _d(row.get('pre_money_valuation')))
            _set_if(tdefs, 'post_money_valuation', _d(row.get('post_money_valuation')))
            _set_if(tdefs, 'round_name', round_name)
            _set_if(tdefs, 'instrument_type', instrument)
            _set_if(tdefs, 'ownership_pct', _d(row.get('ownership_pct')))
            _set_if(tdefs, 'fully_diluted_pct', _d(row.get('fd_pct') or row.get('fully_diluted_pct')))

            _safe_save(InvestmentTranche,
                lookup_kwargs={'investment': inv, 'tranche_number': tnum},
                defaults=tdefs,
            )
            tr_count += 1

    return len(company_map), inv_count, tr_count


def _persist_valuations(scheme, rows: list) -> int:
    """Persist per-investment valuations.

    Rule 26: each row represents ONE investment of a company (e.g. INV001
    Series A vs INV002 Series B Follow-on are two distinct investments for
    the same company). To map a valuation row to the correct Investment we
    use a multi-key match:

      1. If Gemini emitted `investment_ref` (e.g. "INV001"), match by stage /
         round_name first (often the workbook's id maps cleanly).
      2. Match by (company, cost_basis) — `cost_basis` is unique per
         investment for a given company.
      3. Match by (company, instrument_type) if cost_basis is missing.
      4. Last resort: pick the earliest Investment for the company.

    Without this disambiguation the persister collapses N investments into
    1 row and the dashboard loses (N−1) × fair_value_of_holding worth of FV.
    """
    from investments.models import Investment, Valuation
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        co_name = _str(row.get('company_name'), 255)
        if not co_name:
            continue
        vdate = _date(row.get('valuation_date'))
        if not vdate:
            continue

        # Multi-key Investment lookup (Rule 26 — disambiguate multi-investment companies)
        candidates = Investment.objects.filter(
            scheme=scheme, portfolio_company__name=co_name
        ).order_by('investment_date')
        if not candidates.exists():
            continue

        inv = None
        row_cost = _d(row.get('cost_basis') or row.get('cost'))
        # 1. Match by cost_basis (most discriminating)
        if row_cost is not None:
            for c in candidates:
                if c.total_invested is not None and abs(c.total_invested - row_cost) < Decimal('0.5'):
                    inv = c
                    break
        # 2. Match by instrument_type if Gemini provided one
        if inv is None:
            row_instr = _enum(row.get('instrument_type'), _INSTRUMENT_MAP, default=None) if row.get('instrument_type') else None
            if row_instr:
                inv = candidates.filter(instrument_type=row_instr).first()
        # 3. Fall back to round_name / stage match
        if inv is None:
            row_round = _str(row.get('round_name') or row.get('stage'), 64)
            if row_round:
                inv = candidates.filter(stage__iexact=row_round).first()
        # 4. Last resort: pick the candidate that doesn't yet have a Valuation
        #    for this date (so we distribute multiple rows across multiple Investments).
        if inv is None:
            for c in candidates:
                if not Valuation.objects.filter(investment=c, valuation_date=vdate).exists():
                    inv = c
                    break
        # 5. Truly last resort: earliest Investment
        if inv is None:
            inv = candidates.first()
        if not inv:
            continue

        # CRITICAL: prefer fair_value_of_holding (fund's share). Fall back to fair_value.
        # Maps to the bug B4/B5 fix.
        fv_holding = _d(row.get('fair_value_of_holding') or row.get('fv_holding'))
        fv_equity = _d(row.get('fair_value') or row.get('equity_val'))

        # NOT-NULL fields on Valuation: methodology (str), fair_value (Decimal).
        # If only fv_holding was provided, mirror it into fair_value so the
        # dashboard's existing `latest_valuation` annotation (which reads
        # fair_value) shows the fund's share rather than 0.
        # status='approved' — the GP uploaded this workbook, so by definition
        # these valuations are approved (the dashboard's latest_valuation
        # subquery filters status='approved'; without this they would not
        # show on the Companies table).
        defaults = {
            'methodology': _str(row.get('methodology'), 32) or 'unknown',
            'fair_value': fv_equity or fv_holding or Decimal('0'),
            'status': 'approved',
        }
        _set_if(defaults, 'fair_value_of_holding', fv_holding or fv_equity)
        _set_if(defaults, 'enterprise_value', _d(row.get('enterprise_value')))
        _set_if(defaults, 'cost_basis', _d(row.get('cost_basis') or row.get('cost')))
        _set_if(defaults, 'multiple', _d(row.get('multiple') or row.get('moic')))
        _set_if(defaults, 'discount_rate', _d(row.get('discount_rate')))
        _set_if(defaults, 'valuer_name', _str(row.get('valuer_name'), 255))
        _set_if(defaults, 'valuer_reg_number', _str(row.get('valuer_reg_number'), 64))
        _set_if(defaults, 'ipev_level', _ipev_to_int(row.get('ipev_level')))
        _set_if(defaults, 'assumptions', _str(row.get('assumptions') or row.get('key_assumptions'), 1000))

        # DB unique constraint = (investment, valuation_date, methodology).
        methodology = defaults.pop('methodology', None) or 'unknown'
        _safe_save(Valuation,
            lookup_kwargs={'investment': inv, 'valuation_date': vdate, 'methodology': methodology},
            defaults=defaults,
        )
        count += 1
    return count


def _persist_quoted(organization, rows: list) -> int:
    from investments.models import PortfolioCompany
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _str(row.get('company_name'), 255)
        if not name:
            continue
        co = PortfolioCompany.objects.filter(organization=organization, name=name).first()
        if not co:
            continue
        share_type = _str(row.get('share_type'), 32).lower()
        is_quoted = None
        if share_type:
            if 'listed' in share_type or 'quoted' in share_type:
                if 'unlisted' in share_type or 'unquoted' in share_type:
                    is_quoted = False
                else:
                    is_quoted = True
        iq_explicit = _bool(row.get('is_quoted'))
        if iq_explicit is not None:
            is_quoted = iq_explicit
        if is_quoted is not None:
            co.is_quoted = is_quoted
        ex = _str(row.get('listing_exchange'), 16)
        if ex:
            co.listing_exchange = ex
        co.save(update_fields=['is_quoted', 'listing_exchange'])
        count += 1
    return count


def _persist_exits(scheme, rows: list, user) -> int:
    from investments.models import Investment, ExitEvent
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        co_name = _str(row.get('company_name'), 255)
        if not co_name:
            continue
        edate = _date(row.get('exit_date'))
        if not edate:
            continue
        inv = Investment.objects.filter(
            scheme=scheme, portfolio_company__name=co_name
        ).order_by('-investment_date').first()
        if not inv:
            continue
        exit_type = _enum(row.get('exit_type') or row.get('exit_route'),
                          _EXIT_TYPE_MAP, default='secondary_sale')
        defaults = {
            'exit_type': exit_type,
            'is_actual': True,
        }
        _set_if(defaults, 'exit_valuation', _d(row.get('exit_valuation')))
        _set_if(defaults, 'proceeds', _d(row.get('proceeds') or row.get('realised') or row.get('gross_proceeds')))
        _set_if(defaults, 'net_exit_proceeds', _d(row.get('net_exit_proceeds')))
        _set_if(defaults, 'realized_gain_loss', _d(row.get('realized_gain_loss')))
        _set_if(defaults, 'moic', _d(row.get('moic') or row.get('gross_moic')))
        _set_if(defaults, 'irr_pct', _d(row.get('irr_pct') or row.get('gross_irr')))
        _set_if(defaults, 'buyer_name', _str(row.get('buyer_name'), 255))
        if user:
            defaults.setdefault('created_by', user)

        _safe_save(ExitEvent,
            lookup_kwargs={'investment': inv, 'exit_date': edate},
            defaults=defaults,
        )
        count += 1
    return count


def _persist_distributions(scheme, rows: list, user) -> int:
    from lp.models import Distribution
    count = 0
    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        dn_raw = row.get('distribution_number') or row.get('dist_id')
        m = re.search(r'\d+', _str(dn_raw))
        dnum = int(m.group()) if m else idx
        ddate = _date(row.get('distribution_date'))
        # Use _first_present (NOT `or`) — Python's `or` treats 0.0 as falsy
        # and falls through, dropping a legitimate ₹0 distribution.
        gross = _d(_first_present(row.get('total_gross_amount'), row.get('gross_amount')))
        net   = _d(_first_present(row.get('total_net_amount'), row.get('net_distribution')))
        # Accept a distribution if EITHER gross or net is present (most workbooks
        # publish only one — absent TDS the two are equal). Per Rule 29.
        # Decimal('0') is falsy in Python — guard explicitly against None so a
        # legitimate ₹0 distribution (ROC-phase fund) is not silently skipped.
        if _first_present(gross, net) is None or ddate is None:
            continue
        # If only net is given, mirror it into gross so DPI / waterfall queries
        # that read total_gross_amount don't underreport.
        if gross is None and net is not None:
            gross = net
        dt = _enum(row.get('distribution_type'), _DIST_TYPE_MAP, default='return_of_capital')

        defaults = {
            'distribution_date': ddate,
            'distribution_type': dt,
            'total_gross_amount': gross,
        }
        _set_if(defaults, 'total_tds_amount', _d(row.get('total_tds_amount')))
        _set_if(defaults, 'total_net_amount', net)
        ds = _str(row.get('distribution_status') or row.get('status'), 16).lower()
        if ds in ('draft', 'approved', 'distributed'):
            defaults['distribution_status'] = ds
        if user:
            defaults.setdefault('created_by', user)

        _safe_save(Distribution,
            lookup_kwargs={'scheme': scheme, 'distribution_number': dnum},
            defaults=defaults,
        )
        count += 1
    return count


def _persist_nav_records(scheme, rows: list) -> int:
    from accounting.models import NAVRecord
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        nd = _date(row.get('nav_date') or row.get('date') or row.get('period_end'))
        if not nd:
            continue
        # NOT-NULL on NAVRecord: total_nav, total_units_outstanding, nav_per_unit.
        # Compute nav_per_unit from total_nav/units when missing; default zeros
        # so a row still persists even on sparse periods.
        total_nav = _d(row.get('total_nav') or row.get('net_nav') or row.get('closing_nav')) or Decimal('0')
        units     = _d(row.get('total_units_outstanding') or row.get('units_outstanding') or row.get('units_os')) or Decimal('0')
        npu       = _d(row.get('nav_per_unit'))
        if npu is None:
            npu = (total_nav / units) if units and units != 0 else Decimal('0')
        defaults = {
            'total_nav': total_nav,
            'total_units_outstanding': units,
            'nav_per_unit': npu,
        }
        _set_if(defaults, 'investments_at_fair_value', _d(row.get('investments_at_fair_value') or row.get('total_investments')))
        _set_if(defaults, 'cash_and_equivalents', _d(row.get('cash_and_equivalents') or row.get('fund_cash')))
        _set_if(defaults, 'management_fee_payable', _d(row.get('management_fee_payable') or row.get('mgmt_fee')))
        _set_if(defaults, 'unrealized_gains', _d(row.get('unrealized_gains') or row.get('unrealised_gains')))
        _set_if(defaults, 'realized_gains', _d(row.get('realized_gains') or row.get('realised_gains')))

        _safe_save(NAVRecord,
            lookup_kwargs={'scheme': scheme, 'nav_date': nd},
            defaults=defaults,
        )
        count += 1
    return count


def _persist_carried_interest(scheme, wf: dict, fp: dict):
    from accounting.models import CarriedInterest

    cdate = _date(fp.get('as_of_date')) or date.today()
    defaults = {}
    # Use _first_present (not `or`) so a literal 0 from Gemini persists.
    # For ROC-phase funds, gross/net/clawback are legitimately 0 and must show
    # as ₹0 on the dashboard, not "—".
    _set_if(defaults, 'total_distributions', _d(_first_present(wf.get('total_distributions'), fp.get('total_distributions'))))
    _set_if(defaults, 'total_called_capital', _d(_first_present(wf.get('total_capital_called'), fp.get('total_called_capital'))))
    _set_if(defaults, 'preferred_return_amount', _d(_first_present(wf.get('preferred_return_amount'), wf.get('step_2_preferred_return'))))
    _set_if(defaults, 'carry_base', _d(_first_present(wf.get('carry_base'), wf.get('available_after_roc_and_pref'))))
    _set_if(defaults, 'carry_amount_gross', _d(_first_present(wf.get('carry_amount_gross'), fp.get('carry_amount_gross'))))
    _set_if(defaults, 'carry_amount_net', _d(_first_present(wf.get('net_carry'), wf.get('carry_amount_net'), fp.get('carry_amount_net'))))
    _set_if(defaults, 'gp_clawback_provision', _d(_first_present(wf.get('clawback_provision'), fp.get('gp_clawback_provision'))))
    status = _str(wf.get('carry_status'), 16).lower()
    if status in ('indicative', 'crystallised', 'paid'):
        defaults['calculation_status'] = status

    _safe_save(CarriedInterest,
        lookup_kwargs={'scheme': scheme, 'calculation_date': cdate},
        defaults=defaults,
    )


def _persist_fund_metrics(organization, scheme, fp: dict, wf: dict,
                          valuation_rows: list, import_file,
                          reconciliation: dict | None = None,
                          fm: dict | None = None) -> int:
    """Write canonical fund metrics into FundMetric model.

    Three universal fixes baked in:

    (a) `active_fair_value` is computed from the ACTUAL PERSISTED DB rows
        (sum of latest Valuation per Investment, preferring fair_value_of_holding
        over fair_value). This guarantees the dashboard tile and the chatbot's
        per-row query return the same number — single source of truth.

    (b) Source resolution uses `_first_present(...)` (NOT `... or ...`). A
        literal 0 from Gemini is a real, valid value (e.g. no carry earned
        yet in ROC phase) and MUST persist as ₹0 on the dashboard, not "—".

    (c) Per-metric provenance is copied from Gemini's `waterfall.provenance`
        and `fund_performance.provenance` sub-objects. Without this, every
        metric tile shows the same useless "phase2_single_call" string in
        the provenance panel.
    """
    from dataimport.models import FundMetric
    from investments.models import Valuation, Investment
    from django.db.models import Subquery, OuterRef
    from django.db.models.functions import Coalesce

    # ── (a) Authoritative FV total: sum of latest persisted Valuation per
    #        Investment, preferring fair_value_of_holding. With Rule 26 fixed
    #        (cost_basis discriminator), every distinct investment has its own
    #        Valuation row, so this DB sum equals Gemini's per-row sum and
    #        matches what the per-company dashboard tiles + chatbot display.
    latest_per_inv = Valuation.objects.filter(
        investment=OuterRef('pk'),
    ).order_by('-valuation_date').annotate(
        holding_or_equity=Coalesce('fair_value_of_holding', 'fair_value'),
    ).values('holding_or_equity')[:1]
    inv_qs = Investment.objects.filter(scheme=scheme).annotate(
        latest_fv=Subquery(latest_per_inv),
    )
    db_fv_sum = Decimal('0')
    contributing_companies = []
    for inv in inv_qs:
        if inv.latest_fv is not None:
            db_fv_sum += inv.latest_fv
            contributing_companies.append((inv.company_name, inv.latest_fv))

    # Compute the Gemini per-row sum from the INPUT list (pre-persistence).
    gemini_row_sum = Decimal('0')
    for vr in (valuation_rows or []):
        if isinstance(vr, dict):
            fvh = _d(vr.get('fair_value_of_holding') or vr.get('fv_holding'))
            if fvh is not None:
                gemini_row_sum += fvh
    gemini_fv_total = _d(fp.get('total_unrealised_fv_holding'))

    # Precedence: DB sum (single source of truth) — but if DB sum is short of
    # Gemini's input-row sum (some Valuation rows didn't persist, e.g. Investment
    # lookup failed), fall back to Gemini's input sum since that's what the
    # workbook actually contained.
    active_fv = db_fv_sum if db_fv_sum > 0 else (gemini_row_sum or gemini_fv_total or Decimal('0'))
    if db_fv_sum > 0 and gemini_row_sum > 0 and abs(db_fv_sum - gemini_row_sum) > Decimal('0.5'):
        logger.warning(
            f'Phase 2: DB sum(Valuation.fair_value_of_holding)={db_fv_sum} differs '
            f'from Gemini input-row sum={gemini_row_sum}. Some valuation rows '
            f'failed to persist — likely Investment lookup mismatch (see Rule 26).'
        )

    # ── (a2) Authoritative DPI numerator: sum of CAPITAL distributions only.
    #        ILPA-aligned DPI excludes interim income (interest, dividends)
    #        and GP carry payouts. Capital types = return_of_capital + STCG + LTCG.
    #        Falls back to Gemini's total_distributions if no distribution rows
    #        persisted (e.g. an early-stage fund pre-first-distribution).
    from lp.models import Distribution
    CAPITAL_DIST_TYPES = ('return_of_capital', 'stcg', 'ltcg')
    capital_dist_qs = Distribution.objects.filter(
        scheme=scheme, distribution_type__in=CAPITAL_DIST_TYPES,
    )
    db_capital_distributions = Decimal('0')
    for d in capital_dist_qs:
        amt = d.total_net_amount if d.total_net_amount is not None else d.total_gross_amount
        if amt is not None:
            db_capital_distributions += amt
    gemini_total_dist = _d(_first_present(fp.get('total_distributions'), wf.get('total_distributions')))
    lp_distributions_value = (
        db_capital_distributions if db_capital_distributions > 0
        else (gemini_total_dist or Decimal('0'))
    )

    # ── (c) Build a provenance lookup keyed by FundMetric.metric_key.
    #        Each Gemini block has a `provenance` sub-object mapping its
    #        local field names to their source cells / formulas. We copy
    #        the matching entry so the panel can show real provenance.
    wf_prov = (wf.get('provenance') or {}) if isinstance(wf, dict) else {}
    fp_prov = (fp.get('provenance') or {}) if isinstance(fp, dict) else {}
    # Map FundMetric.metric_key → list of (source_block, gemini_key) candidates
    PROV_SOURCES = {
        'moic':                     [('fp', 'moic_portfolio'), ('fp', 'moic')],
        'tvpi':                     [('fp', 'tvpi')],
        'dpi':                      [('fp', 'dpi')],
        'rvpi':                     [('fp', 'rvpi')],
        'net_irr':                  [('fp', 'net_irr_computed'), ('fp', 'net_irr_stated')],
        'committed_capital':        [('fp', 'total_committed_capital')],
        'called_capital':           [('fp', 'total_called_capital'), ('wf', 'total_capital_called')],
        'uncalled_capital':         [('fp', 'total_uncalled_capital')],
        'invested_cost':            [('fp', 'total_invested_capital')],
        'realized_proceeds':        [('fp', 'total_realised_proceeds')],
        'lp_distributions':         [('fp', 'total_distributions'), ('wf', 'total_distributions')],
        'active_fair_value':        [('fp', 'total_unrealised_fv_holding')],
        'fund_nav':                 [('fp', 'fund_nav_latest')],
        'carry_amount_gross':       [('wf', 'carry_amount_gross'), ('fp', 'carry_amount_gross')],
        'carry_amount_net':         [('wf', 'net_carry'), ('wf', 'carry_amount_net'), ('fp', 'carry_amount_net')],
        'gp_clawback_provision':    [('wf', 'clawback_provision'), ('fp', 'gp_clawback_provision')],
        'gp_catchup_amount':        [('wf', 'step_3_catchup_amount'), ('wf', 'gp_catchup_amount')],
        'preferred_return_amount':  [('wf', 'step_2_preferred_return'), ('wf', 'preferred_return_amount')],
        'return_of_capital_amount': [('wf', 'step_1_return_of_capital')],
        'carry_base':               [('wf', 'carry_base'), ('wf', 'available_after_roc_and_pref')],
        'lp_total_return':          [('wf', 'lp_share'), ('wf', 'step_4a_lp_residual')],
        'gp_total_distribution':    [('wf', 'gp_share')],
        'accrued_management_fees':  [('fp', 'accrued_management_fees')],
    }

    # ── Canonical formula synthesis (Rule fix: every derived metric MUST
    #    carry a formula and substituted-value expression in provenance).
    #    When Gemini doesn't provide provenance text for a derived metric,
    #    we synthesize one from the canonical European-waterfall formulas
    #    using values we already have in this scope.
    def _fmt_num(v):
        if v is None:
            return '?'
        try:
            return f'{float(v):.2f}'.rstrip('0').rstrip('.')
        except (ValueError, TypeError):
            return str(v)

    _wf_total_dist   = _d(_first_present(wf.get('total_distributions'), fp.get('total_distributions')))
    _wf_called       = _d(_first_present(wf.get('total_capital_called'), fp.get('total_called_capital')))
    _wf_pref         = _d(_first_present(wf.get('step_2_preferred_return'), wf.get('preferred_return_amount')))
    _wf_nav          = _d(fp.get('fund_nav_latest'))
    _wf_carry_gross  = _d(_first_present(wf.get('carry_amount_gross'), fp.get('carry_amount_gross')))
    _wf_clawback     = _d(_first_present(wf.get('clawback_provision'), fp.get('gp_clawback_provision')))
    _wf_catchup      = _d(wf.get('step_3_catchup_amount'))
    _wf_step4b       = _d(wf.get('step_4b_gp_residual_carry'))
    _wf_carry_pct    = _d(wf.get('carry_percentage'))
    _wf_hurdle       = _d(wf.get('hurdle_rate'))
    _wf_years        = _d(wf.get('step_2_years_compounded'))

    def _canonical_formula(metric_key):
        """Return (formula_template, substituted_formula) for derived metrics.
        Used as a fallback when Gemini doesn't emit per-key provenance."""
        # Each entry: (description, template, substituted with real numbers)
        T = _fmt_num
        if metric_key == 'carry_base':
            return ('Total Value − Called − Preferred Return',
                    f'(₹{T(_wf_total_dist)} Cr + ₹{T(_wf_nav)} Cr) − ₹{T(_wf_called)} Cr − ₹{T(_wf_pref)} Cr')
        if metric_key == 'carry_amount_gross':
            return ('GP Catch-up + GP share of residual after catch-up',
                    f'₹{T(_wf_catchup)} Cr + ₹{T(_wf_step4b)} Cr')
        if metric_key == 'carry_amount_net':
            return ('Gross Carry − Clawback Provision',
                    f'₹{T(_wf_carry_gross)} Cr − ₹{T(_wf_clawback)} Cr')
        if metric_key == 'gp_clawback_provision':
            return ('20% of Gross Carry (default LPA escrow)',
                    f'₹{T(_wf_carry_gross)} Cr × 20%')
        if metric_key == 'preferred_return_amount':
            return ('LP Called × ((1 + hurdle)^years − 1)',
                    f'₹{T(_wf_called)} Cr × ((1 + {T(_wf_hurdle)}%)^{T(_wf_years)} − 1)')
        if metric_key == 'gp_catchup_amount':
            return ('Preferred Return × (carry% / (1 − carry%))',
                    f'₹{T(_wf_pref)} Cr × ({T(_wf_carry_pct)}% / (100% − {T(_wf_carry_pct)}%))')
        if metric_key == 'return_of_capital_amount':
            return ('LP Called Capital (returned 100% in Step 1)',
                    f'₹{T(_wf_called)} Cr')
        if metric_key == 'tvpi':
            return ('(Distributions + NAV) / Called',
                    f'(₹{T(_wf_total_dist)} Cr + ₹{T(_wf_nav)} Cr) / ₹{T(_wf_called)} Cr')
        if metric_key == 'dpi':
            return ('Distributions / Called',
                    f'₹{T(_wf_total_dist)} Cr / ₹{T(_wf_called)} Cr')
        if metric_key == 'rvpi':
            return ('NAV / Called',
                    f'₹{T(_wf_nav)} Cr / ₹{T(_wf_called)} Cr')
        if metric_key == 'moic':
            return ('(Distributions + NAV) / Total Invested  (or  sum(FMV holding) / sum(cost))',
                    f'(₹{T(_wf_total_dist)} Cr + ₹{T(_wf_nav)} Cr) / ₹{T(_wf_called)} Cr')
        if metric_key == 'net_irr':
            return ('XIRR over LP cashflows: calls (out) + distributions (in) + ending NAV (in)',
                    'Python XIRR solver over fund_performance.net_irr_cashflows')
        return (None, None)

    def _provenance_for(metric_key: str, raw_value):
        """Build the inputs_used dict for the provenance panel."""
        prov_text = None
        prov_key = None
        prov_block = None
        for block, gkey in PROV_SOURCES.get(metric_key, []):
            src = wf_prov if block == 'wf' else fp_prov
            if gkey in src and src[gkey]:
                prov_text = str(src[gkey])
                prov_key = gkey
                prov_block = 'waterfall' if block == 'wf' else 'fund_performance'
                break
        # Classify source: cell reference (Sheet:Rxx:Cyy) vs computed formula
        is_cell = prov_text and ':' in prov_text and not prov_text.startswith('computed')
        is_formula = prov_text and prov_text.startswith('computed')
        out = {
            'gemini_key': prov_key,
            'gemini_block': prov_block,
            'gemini_value': str(raw_value) if raw_value is not None else None,
        }
        if is_cell:
            # Cell ref like "Fund_Overview:R47:C2" — split into sheet + coords
            parts = prov_text.split(':', 1)
            out['source_sheet'] = parts[0]
            out['source_cells'] = parts[1] if len(parts) > 1 else ''
            out['provenance_kind'] = 'extracted'
        elif is_formula:
            # "computed: 550 × ((1.08)^4.083 − 1)" — strip prefix
            out['formula_expression'] = prov_text[len('computed:'):].strip() if prov_text.startswith('computed:') else prov_text
            out['provenance_kind'] = 'computed_by_gemini'
        elif prov_text:
            out['note'] = prov_text
            out['provenance_kind'] = 'gemini_provided'
        else:
            # FALLBACK: synthesize from canonical formulas. Means Gemini didn't
            # provide a per-key provenance string, but we know the standard
            # formula and have all the inputs already in scope.
            tpl, subs = _canonical_formula(metric_key)
            if tpl and subs:
                out['canonical_formula_template'] = tpl
                out['formula_expression'] = f'{tpl}  =  {subs}  =  {_fmt_num(raw_value)}'
                out['provenance_kind'] = 'computed_from_canonical_formula'
            else:
                out['provenance_kind'] = 'no_provenance'
        return out

    # ── (b1) ROC-phase carry defaults ─────────────────────────────────
    # When the fund hasn't generated carry yet (no LP distributions, no carry
    # extraction in waterfall JSON), the persister previously left these keys
    # as None — the dashboard then rendered them as "—". Per Rule: a metric
    # whose value is mathematically zero MUST show as ₹0, not as missing.
    # We surface that 0 explicitly when (and only when) the fund is in ROC
    # phase so the cards render "₹0 Cr" rather than blank.
    _fund_in_roc_phase = (lp_distributions_value or Decimal('0')) == Decimal('0')
    _ZERO = Decimal('0')

    def _zero_if_roc(raw):
        if raw is not None:
            return raw
        return _ZERO if _fund_in_roc_phase else None

    # ── (b) Resolution with _first_present so 0 persists ───────────────
    metric_map = {
        'moic':                     _first_present(fp.get('moic_portfolio'), fp.get('moic')),
        'tvpi':                     fp.get('tvpi'),
        'dpi':                      fp.get('dpi'),
        'rvpi':                     fp.get('rvpi'),
        'net_irr':                  _first_present(fp.get('net_irr_computed'), fp.get('net_irr_stated')),
        'committed_capital':        fp.get('total_committed_capital'),
        'called_capital':           _first_present(fp.get('total_called_capital'), wf.get('total_capital_called')),
        'uncalled_capital':         fp.get('total_uncalled_capital'),
        'invested_cost':            fp.get('total_invested_capital'),
        'realized_proceeds':        fp.get('total_realised_proceeds'),
        'lp_distributions':         lp_distributions_value,
        'active_fair_value':        active_fv,
        'fund_nav':                 fp.get('fund_nav_latest'),
        'carry_amount_gross':       _zero_if_roc(_first_present(wf.get('carry_amount_gross'), fp.get('carry_amount_gross'))),
        'carry_amount_net':         _zero_if_roc(_first_present(wf.get('net_carry'), wf.get('carry_amount_net'), fp.get('carry_amount_net'))),
        'gp_clawback_provision':    _zero_if_roc(_first_present(wf.get('clawback_provision'), fp.get('gp_clawback_provision'))),
        'gp_catchup_amount':        _zero_if_roc(_first_present(wf.get('step_3_catchup_amount'), wf.get('gp_catchup_amount'))),
        'preferred_return_amount':  _first_present(wf.get('step_2_preferred_return'), wf.get('preferred_return_amount')),
        'return_of_capital_amount': wf.get('step_1_return_of_capital'),
        'carry_base':               _zero_if_roc(_first_present(wf.get('carry_base'), wf.get('available_after_roc_and_pref'))),
        'lp_total_return':          _first_present(wf.get('lp_share'), wf.get('step_4a_lp_residual')),
        'gp_total_distribution':    wf.get('gp_share'),
        'accrued_management_fees':  fp.get('accrued_management_fees'),
        # Scheme terms — dashboard reads these via FundMetric (not Scheme model)
        # to show "X% Hurdle · Y% Carry · Z% Mgmt Fee" in the Waterfall header.
        'hurdle_rate':              (fm or {}).get('hurdle_rate_pct'),
        'carry_pct':                (fm or {}).get('carry_pct'),
        'mgmt_fee_pct':             (fm or {}).get('management_fee_pct'),
        'sponsor_commitment_pct':   (fm or {}).get('sponsor_commitment_pct'),
    }

    count = 0
    for key, raw_value in metric_map.items():
        val = _d(raw_value)
        if val is None:
            continue
        prov = _provenance_for(key, raw_value)
        # For active_fair_value, supplement with the live DB breakdown so
        # the panel can show "= co1 + co2 + ..." with real numbers.
        if key == 'active_fair_value' and contributing_companies:
            prov['contributing_companies'] = [
                {'company': c, 'fair_value_of_holding': str(v)}
                for c, v in contributing_companies
            ]
            prov['provenance_kind'] = 'computed_from_db'
            prov['formula_expression'] = (
                'sum(latest Valuation.fair_value_of_holding per Investment) = '
                + ' + '.join(f'{v}' for _, v in contributing_companies)
                + f' = {active_fv}'
            )
        # For lp_distributions: when we derived it from DB capital-only rows,
        # override Gemini's row-level provenance so the panel shows the real
        # filter rather than a stale total_distributions cell reference.
        if key == 'lp_distributions' and db_capital_distributions > 0:
            prov['provenance_kind'] = 'computed_from_db'
            prov['formula_expression'] = (
                'sum(Distribution.net_amount WHERE distribution_type IN '
                "('return_of_capital','stcg','ltcg')) "
                f'= {db_capital_distributions} (ILPA-aligned: capital only, '
                'excludes interim dividends/interest/carry)'
            )
        # Phase 3: inject priority_rule_applied + alternatives + disagreements
        # from the reconciler so the side panel can render the "Priority Rule
        # Applied" section. metric_key == reconciliation field_id by design.
        if reconciliation and isinstance(reconciliation, dict):
            recon = reconciliation.get(key)
            if isinstance(recon, dict):
                prov['priority_rule_applied'] = recon.get('priority_rule_applied')
                prov['priority_rule_reason'] = recon.get('reason')
                prov['principles_meaning'] = recon.get('principles_meaning')
                prov['picked_source'] = recon.get('picked_source')
                if recon.get('skipped_higher_priority_sources'):
                    prov['skipped_higher_priority_sources'] = recon['skipped_higher_priority_sources']
                if recon.get('alternatives_within_tolerance'):
                    prov['alternatives'] = recon['alternatives_within_tolerance']
                if recon.get('disagreements_outside_tolerance'):
                    prov['disagreements'] = recon['disagreements_outside_tolerance']
                if recon.get('quality_flag'):
                    prov['quality_flag'] = recon['quality_flag']

        # `source` field: 'extracted' for cell-refs, otherwise 'computed'.
        # Dashboard provenance panel uses this to pick the pill colour.
        source_label = 'extracted' if prov.get('provenance_kind') == 'extracted' else 'computed'
        _safe_save(FundMetric,
            lookup_kwargs={'organization': organization, 'scheme': scheme, 'metric_key': key},
            defaults={
                'value': val,
                'source': source_label,
                'source_import_file': import_file,
                'inputs_used': prov,
            },
        )
        count += 1
    return count


def _normalize_label(s: str) -> str:
    """Canonicalize a free-text label for substring matching.

    Treats ' & ' and ' and ' as equivalent, collapses whitespace, strips
    punctuation noise (parens, slashes). Universal across formats — does
    not assume any specific source language.
    """
    if s is None:
        return ''
    s = str(s).strip().lower()
    # ' and ' ↔ ' & ' equivalence is the single most common label collision
    s = re.sub(r'\band\b', '&', s)
    # Strip parenthetical units like '(Cr)', '(₹)', '(Lakhs)' — they're noise
    s = re.sub(r'\([^)]*\)', ' ', s)
    # Collapse all separators (commas, slashes, dashes, multi-space) to a single space
    s = re.sub(r'[,/–—\-]+', ' ', s)
    # Collapse spaces around '&' so 'r & d', 'r&d', and 'r and d' all become 'r&d'
    s = re.sub(r'\s*&\s*', '&', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _build_pl_line_item_alias_map() -> dict:
    """Build alias→canonical_key map from canonical_schema (source of truth).

    Avoids the hardcoded English-keyword anti-pattern called out in the
    [[feedback-no-hardcoding]] memory. The canonical schema's descriptions
    of the form "Alias 1 / Alias 2 / Alias 3 — explanation" are split on
    '/' to extract aliases, then normalised via `_normalize_label`.

    When the codebase migrates from local matching to
    Gemini's `classify_labels('pl_line_items')` (see canonical_schema.py
    plan), this function becomes a thin local fallback — every alias still
    routes to the same canonical key, so no model-layer change is needed.
    """
    from .canonical_schema import CANONICAL_VALUE_CATEGORIES
    cat = CANONICAL_VALUE_CATEGORIES.get('pl_line_items', {})
    alias_map = {}
    for canonical_key, description in cat.items():
        # Description shape: "Alias1 / Alias2 / Alias3 — long explanation"
        head = description.split('—', 1)[0].strip() if '—' in description else description.strip()
        for alias in head.split('/'):
            a = _normalize_label(alias)
            if a:
                alias_map[a] = canonical_key
        # The canonical key itself (snake_case) is always a valid label
        alias_map[_normalize_label(canonical_key.replace('_', ' '))] = canonical_key
        alias_map[_normalize_label(canonical_key)] = canonical_key
    return alias_map


_BVA_LINE_ITEM_MAP = _build_pl_line_item_alias_map()


def _bva_line_item_key(li_text):
    """Map free-text line item label to BudgetVsActual.LINE_ITEM_CHOICES key.

    Universal across any AIF Excel: aliases drawn from canonical_schema.
    Both input and aliases pass through `_normalize_label` so '&' ↔ 'and',
    unit suffixes like '(Cr)', and punctuation noise don't block matches.
    Returns None if no canonical alias matches.
    """
    if not li_text:
        return None
    s = _normalize_label(li_text)
    if not s:
        return None
    if s in _BVA_LINE_ITEM_MAP:
        return _BVA_LINE_ITEM_MAP[s]
    # Longest-match-first: ensures multi-word aliases win over single-word
    # prefixes (e.g. 'net working capital' beats 'working capital' beats 'capital').
    # Also prevents 'tax' inside 'earnings before interest tax depreciation' from
    # winning over the (longer) 'earnings before interest tax depreciation amortisation'.
    for k in sorted(_BVA_LINE_ITEM_MAP.keys(), key=len, reverse=True):
        if k in s:
            return _BVA_LINE_ITEM_MAP[k]
    return None


_BVA_FY_RE = re.compile(r'FY\s*(\d{2,4})\s*[-/]\s*(\d{2,4})', re.IGNORECASE)
_BVA_QUARTER_RE = re.compile(r'(Q[1-4])\s*[-\s]?(?:FY)?\s*(\d{2,4})', re.IGNORECASE)
_BVA_MON_RE = re.compile(
    r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[-\s]+(\d{2,4})',
    re.IGNORECASE,
)
_BVA_MON_NUM = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
                'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}


def _bva_parse_period(p):
    """Parse period label → (period_year, period_type, period_month, period_quarter).
    Universal across formats: 'FY 2024-25', 'Q1 FY25', 'Mar-25', '31-Mar-2025'."""
    if not p:
        return None
    s = str(p).strip()
    m = _BVA_FY_RE.search(s)
    if m:
        end = int(m.group(2))
        if end < 100:
            end = 2000 + end
        return {'period_year': end, 'period_type': 'annual'}
    m = _BVA_QUARTER_RE.search(s)
    if m:
        y = int(m.group(2))
        if y < 100:
            y = 2000 + y
        return {'period_year': y, 'period_type': 'quarterly',
                'period_quarter': m.group(1).upper()}
    m = _BVA_MON_RE.search(s)
    if m:
        mon_name = m.group(1).lower()
        y = int(m.group(2))
        if y < 100:
            y = 2000 + y
        return {'period_year': y, 'period_type': 'monthly',
                'period_month': _BVA_MON_NUM[mon_name]}
    # Last-resort: try ISO date '2025-03-31' / '31/03/2025'
    d = _date(s)
    if d:
        return {'period_year': d.year, 'period_type': 'monthly',
                'period_month': d.month}
    return None


def _persist_budget_vs_actual(organization, fund, rows: list) -> int:
    """Persist Budget vs Actual rows into the BudgetVsActual model.

    Universal across formats: handles 'FY 2024-25' / 'Q1 FY25' / 'Mar-25' period
    labels and any line-item synonym in _BVA_LINE_ITEM_MAP. Skips rows with no
    matching PortfolioCompany or unrecognised line_item — both are logged at
    debug level so they don't crash the run.
    """
    from mis_consolidation.models import BudgetVsActual
    from investments.models import PortfolioCompany
    if not fund:
        return 0
    count = 0
    skipped_no_co = 0
    skipped_no_line = 0
    skipped_no_period = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        co_name = _str(row.get('company_name'), 255)
        line_item_raw = row.get('line_item')
        if not co_name or not line_item_raw:
            continue
        co = PortfolioCompany.objects.filter(organization=organization, name=co_name).first()
        if not co:
            skipped_no_co += 1
            continue
        line_item_key = _bva_line_item_key(line_item_raw)
        if not line_item_key:
            skipped_no_line += 1
            continue
        period_dict = _bva_parse_period(row.get('period'))
        if not period_dict:
            skipped_no_period += 1
            continue
        budget = _d(row.get('budget'))
        actual = _d(row.get('actual'))
        variance = _d(row.get('variance'))
        # JSON stores variance_pct as a fraction (0.12 = 12%); model stores percent (12.0).
        var_pct_raw = _d(row.get('variance_pct'))
        var_pct = (var_pct_raw * Decimal('100')) if var_pct_raw is not None else None
        defaults = {
            'budget_inr': budget,
            'actual_inr': actual,
            'variance_inr': variance,
            'variance_pct': var_pct,
        }
        is_fav = row.get('is_favorable')
        if is_fav is not None:
            defaults['is_favorable'] = bool(is_fav)
        lookup = {
            'portfolio_company': co,
            'fund': fund,
            'period_year': period_dict['period_year'],
            'period_type': period_dict['period_type'],
            'line_item': line_item_key,
            'period_month': period_dict.get('period_month'),
            'period_quarter': period_dict.get('period_quarter', ''),
        }
        BudgetVsActual.objects.update_or_create(**lookup, defaults=defaults)
        count += 1
    if skipped_no_co or skipped_no_line or skipped_no_period:
        logger.info(
            f'[phase2.bva] persisted={count}  skipped: '
            f'no_company={skipped_no_co}  no_line_item={skipped_no_line}  '
            f'no_period={skipped_no_period}'
        )
    return count


def _persist_portfolio_kpis(organization, scheme, rows: list) -> int:
    """Persist per-company periodic KPIs into PortfolioKPI rows.

    PortfolioKPI uses kpi_definition (FK) + period as natural key. For Phase 2
    MVP we store using a lookup-by-name on KPIDefinition, creating definitions
    on demand. Skips rows with no value.
    """
    from investments.models import PortfolioCompany, Investment, PortfolioKPI

    # Lazy import — KPIDefinition might be in a separate module
    try:
        from investments.models import KPIDefinition
    except ImportError:
        logger.warning('Phase 2: KPIDefinition not importable — skipping periodic KPI persistence')
        return 0

    # Fields to project from each row
    kpi_fields = [
        'revenue', 'cogs', 'gross_profit', 'gross_margin_pct',
        'ebitda', 'ebitda_margin_pct', 'pat', 'headcount',
        'gmv', 'orders', 'aov', 'returns_pct', 'repeat_pct',
        'mrr', 'arr', 'nrr', 'churn_rate', 'cac', 'ltv', 'ltv_cac_ratio',
        'burn_rate', 'runway_months', 'nim_pct', 'gnpa_pct', 'nnpa_pct',
        'roe_pct', 'capacity_utilization', 'export_pct', 'bed_occupancy',
        'arpob', 'cap_rate_pct', 'aum_value', 'cost_to_income', 'debt_to_ebitda',
    ]

    # Cache KPIDefinition lookups (model uses `slug` + `name`)
    def_cache: dict[str, Any] = {}
    def _get_def(key: str):
        if key in def_cache:
            return def_cache[key]
        kdef, _ = _safe_save(KPIDefinition,
            lookup_kwargs={'organization': organization, 'slug': key},
            defaults={'name': key.replace('_', ' ').title()},
            mode='get_or_create',
        )
        def_cache[key] = kdef
        return kdef

    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        co_name = _str(row.get('company_name'), 255)
        period_label = _str(row.get('period'), 32)
        if not co_name or not period_label:
            continue
        # PortfolioKPI.period is a DateField; convert label → date universally
        period_date = _period_to_date(period_label) or date.today()
        co = PortfolioCompany.objects.filter(organization=organization, name=co_name).first()
        if not co:
            continue
        inv = Investment.objects.filter(scheme=scheme, portfolio_company=co).first()

        for fname in kpi_fields:
            val = _d(row.get(fname))
            if val is None:
                continue
            kdef = _get_def(fname)
            _safe_save(PortfolioKPI,
                lookup_kwargs={'investment': inv, 'portfolio_company': co,
                               'kpi_definition': kdef, 'period': period_date},
                defaults={
                    'value': val,
                    'source': 'phase2_single_call',
                    'notes': f'period_label={period_label}',
                },
            )
            count += 1
    return count
