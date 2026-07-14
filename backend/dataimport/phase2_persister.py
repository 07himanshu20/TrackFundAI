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


def _total_fair_value_metric(*, active_fv, realised) -> Optional[Decimal]:
    """Universal 'Total Fair Value' = Active FV + Realised Proceeds from Exits.

    Matches the AIF industry-standard formula and Excel Cover 'Total FV
    Unrealised (B8) + Total Realised Proceeds (B9)'. Both inputs are
    optional individually — if either is present, the sum is meaningful
    (the missing side is treated as 0). Returns None only when BOTH inputs
    are absent, so the FundMetric loop skips writing the row for funds with
    no valuations AND no exits (e.g. a brand-new fund pre-drawdown).

    Universal: works on any fund regardless of workbook layout — the two
    inputs are already normalised by the aggregator (holding basis for
    Active FV, ExitEvent-derived for Realised).
    """
    a = _d(active_fv)
    r = _d(realised)
    if a is None and r is None:
        return None
    return (a or Decimal('0')) + (r or Decimal('0'))


def _derive_carry_net(agg: dict, wf: dict):
    """Universal fallback: carry_amount_net = carry_amount_gross − gp_clawback.

    Used when no explicit "Net Carry" was extracted (Sequoia case: only
    "Carry Escrow Balance" was published). Returns None if either input is
    missing, so `_first_present` cleanly falls through to the None-tail.
    Deterministic; never fires when an explicit net-carry candidate exists.
    """
    _wf = wf or {}
    _gross = _first_present(
        (agg or {}).get('carry_amount_gross'),
        _wf.get('carry_amount_gross'),
    )
    _clawback = _first_present(
        (agg or {}).get('gp_clawback_provision'),
        _wf.get('clawback_provision'),
        _wf.get('gp_clawback_provision'),
    )
    _cg, _cb = _d(_gross), _d(_clawback)
    if _cg is None or _cb is None:
        return None
    net = _cg - _cb
    return net if net >= 0 else Decimal('0')


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
    # Fix U6a — pull waterfall_kv early so _persist_scheme can read LPA-declared
    # holdback / hurdle / carry from it when Fund_Overview does not carry them.
    wf_for_scheme = data.get('waterfall') or {}

    fund_name = _str(fm.get('fund_name'), 255)
    if not fund_name:
        stem = (getattr(import_file, 'original_filename', '') or '').rsplit('.', 1)[0]
        fund_name = _str(stem, 255) or 'Unnamed Fund'
    scheme_name = _str(fm.get('scheme_name'), 255) or fund_name

    counts = {}

    with transaction.atomic():
        # ---- Fund ----
        _p(81, 'Persistence: Fund + Scheme…')
        fund = _persist_fund(organization, fund_name, fm, user)
        scheme = _persist_scheme(fund, scheme_name, fm, wf_for_scheme)
        import_file.fund = fund
        import_file.fund_name = fund_name
        import_file.save(update_fields=['fund', 'fund_name'])

        # ---- Investors + Commitments ----
        _p(83, 'Persistence: Investors + commitments…')
        counts['investors'] = _persist_investors(organization, data.get('investors') or [])
        counts['commitments'] = _persist_commitments(
            organization, scheme,
            data.get('commitments') or data.get('investors') or []
        )

        # ---- Capital Calls ----
        _p(85, 'Persistence: Capital calls…')
        counts['capital_calls'] = _persist_capital_calls(scheme, data.get('capital_calls') or [], user)

        # ---- Portfolio Companies + Investments + Tranches ----
        _p(87, 'Persistence: Portfolio companies + investments + tranches…')
        co_count, inv_count, tr_count = _persist_portfolio(
            organization, scheme, data.get('portfolio_investments') or [],
            data.get('valuations') or [], user,
        )
        counts['portfolio_companies'] = co_count
        counts['investments'] = inv_count
        counts['tranches'] = tr_count

        # ---- Valuations ----
        _p(89, 'Persistence: Valuations…')
        counts['valuations'] = _persist_valuations(scheme, data.get('valuations') or [])

        # ---- Quoted/Unquoted ----
        _p(90, 'Persistence: Quoted/Unquoted listing status…')
        counts['quoted_updates'] = _persist_quoted(organization, data.get('quoted_unquoted') or [])

        # ---- Exits + Distributions ----
        _p(91, 'Persistence: Exits + distributions…')
        counts['exits'] = _persist_exits(scheme, data.get('exits') or [], user)
        counts['distributions'] = _persist_distributions(scheme, data.get('distributions') or [], user)

        # ---- NAV records (FULL monthly walk) ----
        _p(93, 'Persistence: NAV walk…')
        counts['nav_records'] = _persist_nav_records(scheme, data.get('nav_records') or [])

        # ---- Compliance (SEBI + Calendar) ─────────────────────────────
        # Universal: persist any compliance_records[] entries Phase 3 emitted.
        try:
            sebi_n, cal_n = _persist_compliance(
                organization, fund, scheme, data.get('compliance_records') or []
            )
            counts['sebi_reports'] = sebi_n
            counts['compliance_calendar'] = cal_n
        except Exception as e:
            logger.warning(f'[compliance] persistence failed (non-fatal): {e}')

        # ---- Auto-Valuation for Investments missing FV ────────────────
        # Universal: when source workbook publishes valuations for only some
        # investments, derive FV for the rest using cost × scheme markup.
        # Synthetic rows are flagged via methodology so the audit drawer can
        # tell them apart from source-reported valuations.
        try:
            counts['auto_valuations'] = _auto_create_valuations(scheme)
        except Exception as e:
            logger.warning(f'[auto_valuation] failed (non-fatal): {e}')

        # ---- Sector multi-source backfill ─────────────────────────────
        # Universal: scan every Phase 3 block + PortfolioCompany for sector.
        try:
            counts['sectors_backfilled'] = _backfill_investment_sector_multi(scheme, data)
        except Exception as e:
            logger.warning(f'[sector_backfill] failed (non-fatal): {e}')

        # ---- LP per-investor cumulative rollups (added 2026-06-30) ──────
        # Commitment.cumulative_called / cumulative_distributed are NULL on
        # almost every fund-admin Excel (the master sheet only shows raw
        # commitment, not running drawdown totals).
        #
        # Two-step fill:
        #   (a) PRIMARY: sum per-LP LineItem rows (most accurate, used
        #       whenever the source workbook actually publishes per-LP
        #       breakdowns per capital call / distribution).
        #   (b) FALLBACK: pro-rate the SCHEME total by each LP's commitment
        #       share. Universal across any European whole-fund AIF since
        #       all LPs are called pro-rata under the standard LPA.
        try:
            from lp.models import (Commitment, CapitalCall, CapitalCallLineItem,
                                   Distribution, DistributionLineItem)
            from django.db.models import Sum

            scheme_total_committed = (Commitment.objects.filter(scheme=scheme)
                                      .aggregate(s=Sum('commitment_amount'))['s']) or Decimal('0')
            scheme_total_called = (CapitalCall.objects.filter(scheme=scheme)
                                   .aggregate(s=Sum('total_call_amount'))['s']) or Decimal('0')
            # Sum NET first, fall back to gross for Distribution.
            scheme_total_dist_net = (Distribution.objects.filter(scheme=scheme)
                                     .aggregate(s=Sum('total_net_amount'))['s']) or Decimal('0')
            scheme_total_dist_gross = (Distribution.objects.filter(scheme=scheme)
                                       .aggregate(s=Sum('total_gross_amount'))['s']) or Decimal('0')
            scheme_total_dist = scheme_total_dist_net if scheme_total_dist_net > 0 else scheme_total_dist_gross

            rolled_a = rolled_b = 0
            for c in Commitment.objects.filter(scheme=scheme):
                line_called = (CapitalCallLineItem.objects.filter(commitment=c)
                               .aggregate(s=Sum('called_amount'))['s']) or Decimal('0')
                line_dist = (DistributionLineItem.objects.filter(commitment=c)
                             .aggregate(s=Sum('net_amount'))['s']) or Decimal('0')

                # Priority ladder for cumulative_called / cumulative_distributed:
                #   1. Extractor-populated value from the LP register (e.g. the
                #      "Drawdown(₹Cr)" column) — trust it; the fund manager
                #      published this figure explicitly. Do NOT overwrite.
                #   2. Sum of per-LP LineItem rows (CapitalCallLineItem /
                #      DistributionLineItem) when the workbook publishes them.
                #   3. Pro-rata fallback based on commitment share.
                #
                # Fix (2026-07-10): Priority 3 used to overwrite Priority 1 —
                # if the LP register said Temasek drew 202 Cr but the total
                # CapitalCall sheet only had 125 Cr, the pro-rata overwrote
                # 202 with commitment_share × 125 (= 8.20 Cr) and destroyed
                # the extracted signal. Now Priority 1 is treated as final.
                existing_called = c.cumulative_called
                existing_dist   = c.cumulative_distributed
                is_extracted_called = existing_called is not None and existing_called > 0
                is_extracted_dist   = existing_dist   is not None and existing_dist   > 0

                new_called = None if is_extracted_called else (line_called if line_called > 0 else None)
                new_dist   = None if is_extracted_dist   else (line_dist   if line_dist   > 0 else None)

                # Pro-rate fallback when LineItems missing AND we have a
                # commitment share to apply. Only fires when Priority 1 was
                # also empty (extracted value absent) — never overrides.
                if (new_called is None or new_dist is None) and \
                   c.commitment_amount and scheme_total_committed > 0:
                    share = c.commitment_amount / scheme_total_committed
                    if new_called is None and not is_extracted_called and scheme_total_called > 0:
                        new_called = (scheme_total_called * share).quantize(Decimal('0.01'))
                    if new_dist is None and not is_extracted_dist and scheme_total_dist > 0:
                        new_dist = (scheme_total_dist * share).quantize(Decimal('0.01'))

                changed = False
                if new_called is not None and (c.cumulative_called or Decimal('0')) != new_called:
                    c.cumulative_called = new_called
                    changed = True
                if new_dist is not None and (c.cumulative_distributed or Decimal('0')) != new_dist:
                    c.cumulative_distributed = new_dist
                    changed = True
                if changed:
                    c.save(update_fields=['cumulative_called', 'cumulative_distributed'])
                    if line_called > 0 or line_dist > 0:
                        rolled_a += 1
                    else:
                        rolled_b += 1
            logger.info(
                f'[lp_rollup] backfilled {rolled_a} from LineItems + '
                f'{rolled_b} via pro-rate fallback (of {Commitment.objects.filter(scheme=scheme).count()} commitments)'
            )
        except Exception as e:
            logger.warning(f'[lp_rollup] failed (non-fatal): {e}')

        # ─── Phase 4: Reconciler + Python waterfall (fast, deterministic) ──
        # Order of resolution per metric (universal across all funds):
        #   1. Trusted extraction (workbook_aggregates with label-whitelist OK)
        #   2. Python waterfall on persisted atomic ledger + LPA terms
        #   3. None  (only when neither yields a number)
        #
        # Gemini Code Execution was removed from the primary path on
        # 2026-06-30 because it took 600s+ for one Bharatcrest call. It is
        # kept on disk (phase4_gemini_compute.py) for an optional async
        # validator post-import. Primary path is now Python-only, <5s.
        _p(95, 'Phase 4: Reconciler (extracted-wins) + Python waterfall…')
        aggregates: dict = {}
        phase4_audit: dict = {}

        try:
            from .phase4_derivations import compute_all_fund_aggregates as _py_waterfall
            from .phase4_reconciler  import reconcile

            # Python waterfall computes every fund-level + per-investment
            # metric from atomic DB rows. Sub-second for 50 companies,
            # ~2-3s for 200 companies. Deterministic by construction.
            py_result = _py_waterfall(fund, scheme, data) or {}

            # Reconciler picks per metric: trusted-extraction > python > None
            recon = reconcile(data, {'flat': py_result, 'metrics': {}})
            aggregates = dict(recon['flat'] or {})

            # Fill any keys the reconciler left None from the Python result
            # (reconciler only knows about its METRIC_CONTRACT; legacy keys
            # like step_1_return_of_capital still come from py_result.)
            for k, v in py_result.items():
                if aggregates.get(k) is None and v is not None:
                    aggregates[k] = v

            phase4_audit = {
                'extracted_count':   recon['extracted_count'],
                'computed_count':    recon['computed_count'],
                'unavailable_count': recon['unavailable_count'],
                'provenance':        recon['provenance'],
            }
            logger.info(
                f'[phase4.new] OK — extracted={phase4_audit["extracted_count"]}, '
                f'python_computed={phase4_audit["computed_count"]}, '
                f'unavailable={phase4_audit["unavailable_count"]}'
            )
            if py_result.get('reasons'):
                for k, why in py_result['reasons'].items():
                    logger.info(f'[phase4.py] {k}: {why}')
        except Exception as e:
            logger.exception(f'[phase4.new] failed: {e}')
            phase4_audit['error'] = str(e)

        if phase4_audit and hasattr(import_file, 'metadata'):
            try:
                meta = dict(import_file.metadata or {})
                meta['phase4_audit'] = phase4_audit
                import_file.metadata = meta
                import_file.save(update_fields=['metadata'])
            except Exception as e:
                logger.warning(f'[phase4.audit] could not stash audit: {e}')

        _p(96, 'Persistence: Carry + fund metrics…')
        _persist_carried_interest(scheme, aggregates, data.get('waterfall') or {}, fp)
        counts['fund_metrics'] = _persist_fund_metrics(
            organization, scheme, fp, data.get('waterfall') or {},
            data.get('valuations') or [], import_file,
            reconciliation=data.get('__reconciliation__') or None,
            fm=fm,
            aggregates=aggregates,
        )

        # ---- Per-company periodic KPIs ----
        # Universal: fan out KPIs from BOTH dedicated KPI sheets and from
        # monthly P&L / BS / CF rows AND the burn_runway / SaaS-metrics
        # snapshot so the company-matrix dashboard sees EVERY metric value
        # Gemini extracted, regardless of which sheet it came from.
        # burn_runway ships MRR/ARR/NRR/Churn/CAC/LTV/runway_months as a
        # single-period snapshot per company — the persister already has a
        # fund-context period fallback for period-less rows.
        _p(97, 'Persistence: Per-company periodic KPIs + monthly financials…')
        combined_kpi_source = (
            (data.get('portfolio_kpis_periodic') or [])
            + (data.get('monthly_pl_rows') or [])
            + (data.get('monthly_bs_rows') or [])
            + (data.get('monthly_cf_rows') or [])
            + (data.get('burn_runway') or [])
        )
        counts['portfolio_kpis'] = _persist_portfolio_kpis(
            organization, scheme, combined_kpi_source,
        )

        # ---- Budget vs Actual ----
        _p(98, 'Persistence: Budget vs Actual…')
        counts['budget_vs_actual'] = _persist_budget_vs_actual(
            organization, fund, data.get('budget_vs_actual') or []
        )

    # ---- Phase 4: per-investment IRR + MOIC derivation (post-commit) ----
    # Universal across funds/sectors. Pure Python math, no Gemini call.
    # Runs OUTSIDE the transaction so derivation failures never roll back
    # the persisted base records. ~70ms for a 70-investment fund.
    try:
        _p(99, 'Phase 4: Per-investment IRR + MOIC derivation…')
        from .phase4_derivations import derive_fund_investment_metrics
        deriv = derive_fund_investment_metrics(fund)
        counts['phase4_irr_set'] = deriv.get('irr_set', 0)
        counts['phase4_moic_set'] = deriv.get('moic_set', 0)
        counts['phase4_exit_irr_set'] = deriv.get('exit_irr_set', 0)
    except Exception as e:
        logger.warning(f'Phase 4 derivation failed (non-fatal): {e}')

    # NOTE: Phase 4 waterfall computation now happens INSIDE persist_phase2
    # at progress 95-96% via compute_all_fund_aggregates(). This block is
    # intentionally removed — both CarriedInterest and FundMetric are
    # written from the same aggregator output so they cannot diverge.

    summary = (
        f'F:{counts.get("portfolio_companies",0)} I:{counts.get("investments",0)} '
        f'T:{counts.get("tranches",0)} V:{counts.get("valuations",0)} '
        f'LP:{counts.get("investors",0)} C:{counts.get("commitments",0)} '
        f'CC:{counts.get("capital_calls",0)} D:{counts.get("distributions",0)} '
        f'E:{counts.get("exits",0)} NAV:{counts.get("nav_records",0)} '
        f'KPI:{counts.get("portfolio_kpis",0)} '
        f'IRR4:{counts.get("phase4_irr_set",0)} MOIC4:{counts.get("phase4_moic_set",0)}'
    )

    # Universal cache invalidation at the data-write boundary.
    # Previously only the upload-API view invalidated cache; script/management/
    # CLI triggered imports left stale cached API responses for up to 10 min.
    # Now every import (regardless of how it was triggered) flushes the
    # affected fund's cache so the dashboard sees the new data immediately.
    try:
        from config.cache_utils import invalidate_fund_cache
        invalidate_fund_cache(organization.id, fund.id)
        logger.info(f'[cache] invalidated fund={fund.id} org={organization.id}')
    except Exception as e:
        logger.warning(f'[cache] invalidation failed (non-fatal): {e}')

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


def _persist_scheme(fund, scheme_name: str, fm: dict, wf: dict | None = None):
    """Persist a Scheme. `wf` is the waterfall_kv dict — used for LPA fields
    (holdback, hurdle, carry) that some funds publish on their dedicated
    waterfall / terms sheet rather than on Fund_Overview. Default {} for
    callers who don't have a waterfall context.
    """
    from funds.models import Scheme

    wf = wf or {}
    defaults = {}
    _set_if(defaults, 'vintage_year', _int(fm.get('vintage_year')))
    _set_if(defaults, 'first_close_date', _date(fm.get('first_close_date')))
    _set_if(defaults, 'final_close_date', _date(fm.get('final_close_date')))
    _set_if(defaults, 'scheme_size', _d(fm.get('scheme_size') or fm.get('corpus_target')))
    _set_if(defaults, 'tenure_years', _int(fm.get('tenure_years')))
    # Fix U4 lives at extraction time (normalize_percentage_value in unified_builder).
    # By the time values reach here they're already in percent form (8.00, 20.00);
    # _d() just parses them numerically. If Fix U4 ever misses a fraction, the
    # scheme just stores 0.08 and downstream waterfall math produces near-zero —
    # loud and easy to spot on the dashboard.
    _set_if(defaults, 'hurdle_rate_pct', _d(_first_present(
        fm.get('hurdle_rate_pct'),
        wf.get('hurdle_rate'),
        wf.get('hurdle_rate_pct'),
    )))
    _set_if(defaults, 'carry_pct', _d(_first_present(
        fm.get('carry_pct'),
        wf.get('carry_percentage'),
        wf.get('carry_pct'),
    )))
    _set_if(defaults, 'carry_type', _str(fm.get('carry_type'), 10).lower() or 'european')
    # Fix U6a — GP holdback lives on the waterfall / clawback sheet in most
    # AIF workbooks (AI_Trivesta's Waterfall_Inputs, TrackFundAI Master's
    # MASTER_INPUTS Clawback Reserve %, Bharatcrest's Carry_Clawback). Read
    # from BOTH fund_master and waterfall_kv so we capture whichever tab
    # the fund manager chose. `_first_present` returns the first non-null.
    _set_if(defaults, 'gp_holdback_pct', _d(_first_present(
        fm.get('gp_holdback_pct'),
        fm.get('escrow_holdback_pct'),
        fm.get('clawback_holdback_pct'),
        fm.get('holdback_pct'),
        fm.get('clawback_reserve_pct'),
        wf.get('gp_holdback_pct'),
        wf.get('clawback_holdback'),
        wf.get('escrow_holdback_pct'),
        wf.get('clawback_reserve_pct'),
    )))
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

        # Per-LP cumulative drawdown + distributions (Bug 3+4 fix).
        # The Investors/LP-Master sheet in most fund-admin Excels publishes
        # these totals per LP. Many aliases handled — `_first_present`
        # preserves a legitimate 0.
        _set_if(defaults, 'cumulative_called', _d(_first_present(
            row.get('cumulative_called'),
            row.get('drawdown_amount'),
            row.get('drawdown'),
            row.get('drawn_amount'),
            row.get('called_to_date'),
            row.get('contributed_capital'),
        )))
        _set_if(defaults, 'cumulative_distributed', _d(_first_present(
            row.get('cumulative_distributed'),
            row.get('distributions_amount'),
            row.get('distributions_to_date'),
            row.get('distributions_received'),
            row.get('total_distributions'),
        )))

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
    # ── Universal layout detection: fund-level ledger vs LP-split matrix ──
    # Group input rows by (call_number OR call_date). If every group has
    # exactly 1 row → workbook is a "one-row-per-call" ledger, and each
    # row's `called_amount` IS the fund-level total for that call
    # (Multiples / Edelweiss template). If any group has >1 row → workbook
    # is a "call × LP matrix" where `called_amount` is per-LP and rescuing
    # it would double-count (TrackFundAI Master template). This detection
    # runs ONCE and is universal — no per-file hardcoding.
    def _group_key(r):
        cn_raw = r.get('call_number') or r.get('call_ref')
        cn = _str(cn_raw, 32) if cn_raw is not None else ''
        m = re.search(r'\d+', cn) if cn else None
        if m:
            return f'CN:{m.group()}'
        d = _date(r.get('call_date'))
        return f'D:{d.isoformat()}' if d else 'NONE'
    _group_counts: dict[str, int] = {}
    for _r in sorted_rows:
        _k = _group_key(_r)
        _group_counts[_k] = _group_counts.get(_k, 0) + 1
    _is_fund_level_ledger = (
        len(_group_counts) > 0
        and all(v == 1 for v in _group_counts.values())
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

        # Universal amount rescue: Gemini's column mapping is non-deterministic
        # across imports of the same-template workbook (Mock_14 got amount
        # right; Mock_28 with identical template got 0). Search fund-level
        # amount aliases first, then conditionally rescue `called_amount`.
        _amt_candidates = (
            row.get('total_call_amount'),
            row.get('amount'),
            row.get('actual_received'),
        )
        raw_amt = None
        for _c in _amt_candidates:
            _dc = _d(_c)
            if _dc is not None and _dc != 0:
                raw_amt = _dc
                break
        # Conditional rescue: `called_amount` semantics depend on layout.
        #   • Fund-level ledger (1 row per call, e.g. Multiples / Edelweiss):
        #     called_amount IS the fund-level total → safe to rescue.
        #   • LP-split matrix (N rows per call, e.g. TrackFundAI Master):
        #     called_amount is per-LP → summing it duplicates via the last
        #     -resort per-LP sum below.
        # The pre-loop `_is_fund_level_ledger` flag makes this universal.
        if (raw_amt is None or raw_amt == 0) and _is_fund_level_ledger:
            _dc = _d(row.get('called_amount'))
            if _dc is not None and _dc != 0:
                raw_amt = _dc
        # Fallback: any non-None value (permits genuine 0-Cr planned calls
        # where the workbook explicitly wrote 0 into the amount cell).
        if raw_amt is None:
            raw_amt = _d(_first_present(*_amt_candidates))
        # Last-resort rescue: sum per-LP columns (LP001, LP002, …) — this IS
        # mathematically valid (fund-level total = sum of per-LP allocations
        # for the same call). Distinct from the per-LP field rescue above
        # (which would double-count a single LP's total commitment).
        if raw_amt is None or raw_amt == 0:
            lp_sum = Decimal('0')
            for _k, _v in row.items():
                if isinstance(_k, str) and re.match(r'^lp\d+(_cr)?$', _k, re.I):
                    _d_val = _d(_v)
                    if _d_val is not None:
                        lp_sum += _d_val
            if lp_sum > 0:
                raw_amt = lp_sum

        # Fix 1b — defensive phantom-row guard. XIRR is mathematically defined
        # as Σ cashflow_i / (1+r)^((date_i - date_0)/365); a row without
        # date_i is undefined for XIRR. Persisting date=None as date.today()
        # gives XIRR a fake input; dropping it gives XIRR only real dated
        # inputs. Universal — no fund/sheet hardcoding. Fix A in
        # unified_builder.py routes LP-shape rows upstream; this is the
        # persister-layer backstop for any pattern that slips through.
        raw_date = _date(row.get('call_date'))
        if raw_date is None:
            continue

        # NOT-NULL fields on CapitalCall: call_date, payment_due_date,
        # call_percentage, total_call_amount. Provide zero/today defaults
        # so a row with missing percentage (LLM omitted) still persists.
        call_date = raw_date or date.today()
        pay_due   = _date(row.get('payment_due_date')) or call_date
        pct       = _d(row.get('call_percentage')) or Decimal('0')
        amount    = raw_amt if raw_amt is not None else Decimal('0')

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

    # ── Universal idempotency: drop stale Investments before re-persisting.
    # Without this, every re-import accumulates extra Investment rows when
    # the (instrument_type, stage_key) natural key differs by even one
    # character between runs (e.g. CCPS vs ccps, "Series A" vs date string).
    # Bharatcrest grew from 69 → 79 across 5 imports for exactly this reason.
    # Cascade removes child Tranches / Valuations / Exits / KPIs — they will
    # be re-created in the same Phase 2 pass from the current JSON.
    # Fixed 2026-06-30.
    stale_count = Investment.objects.filter(scheme=scheme).count()
    if stale_count > 0:
        Investment.objects.filter(scheme=scheme).delete()
        logger.info(
            f'[stale_cleanup] dropped {stale_count} stale Investment row(s) '
            f'(plus cascading Tranches/Valuations/Exits/KPIs) before re-import'
        )

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
        gemini_irr_source = 'gemini' if gemini_irr is not None else None
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
                if gemini_irr is not None:
                    gemini_irr_source = 'computed'
        # Universal scale-normalisation applies ONLY to Gemini-extracted values,
        # never to Python-computed XIRR (which already returns percent scale).
        # Excel stores percentages as decimal fractions (0.3329 = 33.29%);
        # Gemini reads the raw fraction and we must rescale to percent.
        # A real fund IRR is essentially never in the [-5%, +5%] range and
        # then formatted as 5 not 0.05 — the choice is deterministic.
        # WITHOUT this source guard, a computed IRR of -2.9% was being
        # multiplied by 100 → -290%, producing impossible values on the
        # dashboard (e.g. CloudBase India -304.6%, CyberShield -429.7%).
        if (
            gemini_irr is not None
            and gemini_irr_source == 'gemini'
            and abs(gemini_irr) < Decimal('5')
        ):
            gemini_irr = gemini_irr * Decimal('100')
        # Universal per-investment IRR sanity clamp: mathematical floor is
        # -100% (can't lose more than you invested) and no realistic
        # per-investment annualised IRR exceeds 1000%. Values outside this
        # window are extraction / compute artefacts — set to None so the
        # dashboard shows "—" instead of a garbage number.
        if gemini_irr is not None and not (
            Decimal('-99.99') <= gemini_irr <= Decimal('999.99')
        ):
            logger.warning(
                f'[irr_clamp] {name}: rejecting per-investment IRR '
                f'{gemini_irr}% (source={gemini_irr_source}) — outside '
                f'[-99.99, 999.99] window'
            )
            gemini_irr = None
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

        # Universal sector fallback: if row data didn't carry a sector but
        # the PortfolioCompany has one (extracted from a master / cover
        # sheet), copy it onto the Investment. Dashboard tile reads
        # Investment.sector directly. Fix added 2026-06-30 for Bharatcrest
        # where Investment.sector=0/69 even though PortfolioCompany.sector=80%.
        if not inv_defaults.get('sector') and co and co.sector:
            inv_defaults['sector'] = co.sector

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
    # Universal fund-context fallback for valuation_date: when a Valuations
    # sheet publishes rows without a per-row date column (True North's IPEV
    # sheet only puts the date in the sheet title), fall back to the scheme's
    # final_close_date / first_close_date. Deterministic + fund-scoped. Prior
    # behaviour dropped every dateless row silently — dashboard FV showed 0.
    _fund_ctx_val_date = (getattr(scheme, 'final_close_date', None)
                          or getattr(scheme, 'first_close_date', None))
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        co_name = _str(row.get('company_name'), 255)
        if not co_name:
            continue
        vdate = _date(row.get('valuation_date'))
        if not vdate:
            vdate = _fund_ctx_val_date
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
    from investments.models import Investment, PortfolioCompany, ExitEvent
    count = 0
    _autocreated = 0
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
            # ── Solution G — Exit-only company auto-create ───────────
            # Some workbooks list exits for companies that never appear
            # in the Portfolio Investments sheet (Sequoia's 7 exits are
            # in a distinct universe from its 17 active companies).
            # Without this rescue the ExitEvent, its proceeds, its IRR
            # and its downstream distributions are silently dropped.
            #
            # Universal safeguards:
            #   1. Only fires when we have a real exit_date AND either
            #      cost_basis or proceeds → this is a real historical
            #      exit worth capturing, not a placeholder line item.
            #   2. Creates minimal shell rows: a PortfolioCompany scoped
            #      to the scheme's organization (multi-tenancy honored)
            #      and an Investment with status='exited' so it doesn't
            #      inflate active-portfolio counts.
            #   3. Uses update_or_create so re-imports don't duplicate.
            #   4. If the row lacks BOTH cost and proceeds, skip — we
            #      have no anchor to reason from, so silently dropping
            #      is safer than fabricating a company.
            cost_hint = _d(row.get('cost') or row.get('cost_basis')
                           or row.get('cost_cr') or row.get('cost_of_investment'))
            proceeds_hint = _d(row.get('proceeds') or row.get('realised')
                               or row.get('gross_proceeds') or row.get('net_exit_proceeds'))
            if cost_hint is None and proceeds_hint is None:
                continue
            org = getattr(scheme, 'organization', None) or getattr(scheme.fund, 'organization', None)
            if org is None:
                continue
            sector = _str(row.get('sector') or row.get('sector_group'), 100)
            pc, _ = PortfolioCompany.objects.update_or_create(
                organization=org,
                name=co_name,
                defaults={
                    'sector': sector or 'Unknown',
                    'is_active': False,
                },
            )
            # Placeholder Investment. investment_date < exit_date is required
            # so tranche → exit ordering makes sense in IRR compute; fall
            # back to exit_date if we have no better signal.
            inv_date_hint = _date(row.get('investment_date')) or edate
            hold_years = _d(row.get('hold_years') or row.get('holding_period_years'))
            if hold_years and hold_years > 0:
                # Prefer inferred entry date from hold_years if we have it.
                try:
                    from datetime import timedelta as _td
                    inv_date_hint = edate - _td(days=int(float(hold_years) * 365))
                except (TypeError, ValueError):
                    pass
            inv, _ = Investment.objects.update_or_create(
                scheme=scheme,
                portfolio_company=pc,
                defaults={
                    'company_name': co_name,
                    'total_invested': cost_hint or Decimal('0'),
                    'investment_date': inv_date_hint,
                    'sector': sector or 'Unknown',
                    'status': 'exited',
                    'created_by': user,
                },
            )
            _autocreated += 1
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
    if _autocreated:
        logger.info(
            f'[persister] Solution G auto-created {_autocreated} '
            f'exit-only companies (scheme={scheme.name!r})'
        )
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
        # Solution A — Fall back to _period_to_date when the distribution
        # row only publishes a "Quarter" label (e.g. Sequoia's "Q1 FY25")
        # and no explicit date column. Universal — same _period_to_date
        # already used by valuations/KPI persisters for FY / quarter labels.
        ddate = (_date(row.get('distribution_date'))
                 or _period_to_date(row.get('distribution_date'))
                 or _period_to_date(row.get('period'))
                 or _period_to_date(row.get('quarter')))
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
        # GP Carry Component per row — universal across European whole-fund
        # AIFs that publish a carry-breakdown column. Used downstream by the
        # waterfall aggregator to compute clawback and net carry from the
        # actual amounts the GP has received, not formula-derived placeholders.
        gp_carry = _d(_first_present(
            row.get('gp_carry_amount'),
            row.get('gp_carry_component'),
            row.get('carried_interest_distribution'),
            row.get('carry_component'),
            row.get('gp_carry'),
            row.get('carry_to_gp'),
        ))
        _set_if(defaults, 'gp_carry_amount', gp_carry)
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


def _persist_compliance(organization, fund, scheme, rows: list) -> tuple[int, int]:
    """Persist compliance_records[] from Phase 3 into SEBIReport + ComplianceCalendar.

    Universal across all AIFs — uses heuristics that work for any
    fund-admin Excel that emits a compliance/regulatory tab.

    Row shape (variable across funds; we accept any subset):
      {fund_name, report_type, compliance_type, calendar_title,
       due_date, filing_status, calendar_status, filed_date,
       regulation_reference, calendar_notes,
       reporting_period_start, reporting_period_end}

    Routing:
      • If row has report_type in {qar, aar, quarterly, annual, sebi...} AND
        a reporting period end date → SEBIReport (formal SEBI filing).
      • Otherwise → ComplianceCalendar (any other deadline/event).

    Returns (sebi_count, calendar_count).
    """
    from compliance.models import SEBIReport, ComplianceCalendar
    sebi_count = cal_count = 0

    SEBI_REPORT_TYPE_MAP = {
        'qar': 'qar', 'quarterly activity report': 'qar', 'quarterly': 'qar',
        'aar': 'aar', 'annual activity report': 'aar', 'annual': 'aar',
    }
    FILING_STATUS_NORMALISE = {
        'filed': 'filed', 'submitted': 'filed', 'accepted': 'accepted',
        'rejected': 'rejected', 'in review': 'in_review', 'review': 'in_review',
        'data collection': 'data_collection', 'collection': 'data_collection',
        'not started': 'not_started', 'pending': 'not_started',
    }
    CAL_TYPE_MAP = {
        'regulatory filings': 'sebi_qar',
        'sebi': 'sebi_qar',
        'fema': 'other',
        'fema / rbi compliance': 'other',
        'rbi': 'other',
        'investment limits': 'other',
        'investment limits & concentration': 'other',
        'personnel & key man': 'other',
        'valuation compliance': 'other',
        'anti-money laundering': 'other',
        'aml': 'other',
        'gst': 'gst_filing',
        'tds': 'tds_filing',
        'custodian': 'custodian_report',
        'auditor': 'auditor_appointment',
        'board': 'board_meeting',
        'nav': 'nav_declaration',
        'depository': 'depository_reconciliation',
        'kyc': 'kyc_renewal',
    }
    CAL_STATUS_NORMALISE = {
        'upcoming': 'upcoming', 'in progress': 'in_progress',
        'completed': 'completed', 'overdue': 'overdue', 'filed': 'completed',
    }

    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        title = _str(row.get('calendar_title') or row.get('title')
                     or row.get('compliance_type') or row.get('report_type'), 255)
        if not title:
            continue
        due = _date(row.get('due_date') or row.get('next_due_date'))
        if not due:
            continue
        rt_raw = _str(row.get('report_type'), 64).lower()
        rt = next((v for k, v in SEBI_REPORT_TYPE_MAP.items() if k in rt_raw), None)
        rps = _date(row.get('reporting_period_start'))
        rpe = _date(row.get('reporting_period_end'))

        if rt and rpe:
            # SEBI filing path
            fs_raw = _str(row.get('filing_status'), 64).lower()
            fs = next((v for k, v in FILING_STATUS_NORMALISE.items() if k in fs_raw), 'not_started')
            defaults = {
                'report_type': rt,
                'reporting_period_start': rps or rpe.replace(month=1, day=1),
                'reporting_period_end': rpe,
                'due_date': due,
                'filing_status': fs,
                'filed_date': _date(row.get('filed_date')),
                'si_portal_reference_number': _str(row.get('regulation_reference')
                                                   or row.get('si_portal_reference_number'), 50) or '',
                'report_data': {
                    'compliance_type': row.get('compliance_type'),
                    'notes': row.get('calendar_notes'),
                },
            }
            try:
                _safe_save(SEBIReport,
                    lookup_kwargs={'fund': fund, 'report_type': rt,
                                   'reporting_period_end': rpe},
                    defaults=defaults,
                )
                sebi_count += 1
            except Exception as e:
                logger.warning(f'[compliance] SEBIReport save failed for {title}: {e}')
        else:
            # General compliance calendar path
            ct_raw = _str(row.get('compliance_type') or row.get('report_type'), 64).lower()
            ct = next((v for k, v in CAL_TYPE_MAP.items() if k in ct_raw), 'other')
            cs_raw = _str(row.get('calendar_status') or row.get('filing_status'), 32).lower()
            cs = next((v for k, v in CAL_STATUS_NORMALISE.items() if k in cs_raw), 'upcoming')
            defaults = {
                'compliance_type': ct,
                'title': title,
                'description': _str(row.get('calendar_notes')
                                    or row.get('regulation_reference') or '', 4096),
                'due_date': due,
                'status': cs,
            }
            try:
                _safe_save(ComplianceCalendar,
                    lookup_kwargs={'organization': organization, 'fund': fund,
                                   'title': title[:255], 'due_date': due},
                    defaults=defaults,
                )
                cal_count += 1
            except Exception as e:
                logger.warning(f'[compliance] ComplianceCalendar save failed for {title}: {e}')

    return sebi_count, cal_count


def _auto_create_valuations(scheme) -> int:
    """Create synthetic Valuation rows for Investments missing one.

    Universal across all AIFs — uses scheme-level fund_markup derived from
    Investments that DO have valuations. If a fund has zero Valuation rows
    at all (no markup derivable), returns 0 — dashboard will show no FV.

    Methodology = 'derived_from_cost_x_scheme_markup' so analysts can tell
    synthetic from source-reported valuations in the audit drawer.
    """
    from investments.models import Investment, Valuation
    from datetime import date as _date_cls
    from decimal import Decimal as _D

    # Compute scheme-level markup = sum(latest FV_holding) / sum(matching cost)
    invs = list(Investment.objects.filter(scheme=scheme))
    fv_sum = _D('0'); cost_sum = _D('0')
    have_val_ids = set()
    for inv in invs:
        latest = (Valuation.objects.filter(investment=inv)
                  .order_by('-valuation_date').first())
        if latest:
            fv = latest.fair_value_of_holding or latest.fair_value
            cost = latest.cost_basis or inv.total_invested
            if fv and cost and cost > 0:
                fv_sum += fv
                cost_sum += cost
                have_val_ids.add(inv.id)

    # Universal fallback path: when no per-investment Valuation rows exist
    # at all, the scheme markup is not derivable from valuations. Use the
    # LATEST NAVRecord's investments_at_fair_value ÷ total invested cost.
    # Works for any fund whose workbook has a NAV walk (Bharatcrest-style).
    # If neither valuations nor NAV FMV data are available, return 0.
    if cost_sum <= 0:
        from accounting.models import NAVRecord as _NAV
        latest_nav = (_NAV.objects.filter(scheme=scheme)
                      .exclude(investments_at_fair_value__isnull=True)
                      .order_by('-nav_date').first())
        if latest_nav and latest_nav.investments_at_fair_value:
            total_cost_across_invs = _D('0')
            for inv in invs:
                if inv.total_invested and inv.total_invested > 0:
                    total_cost_across_invs += inv.total_invested
            if total_cost_across_invs > 0:
                markup = latest_nav.investments_at_fair_value / total_cost_across_invs
                logger.info(f'[auto_valuation] no per-inv valuations — using NAV '
                            f'FMV fallback: fund_fmv={latest_nav.investments_at_fair_value} '
                            f'/ total_cost={total_cost_across_invs} = markup={markup:.4f}')
            else:
                return 0
        else:
            return 0
    else:
        markup = (fv_sum / cost_sum)

    today = _date_cls.today()
    created = 0
    for inv in invs:
        if inv.id in have_val_ids:
            continue
        cost = inv.total_invested
        if not cost or cost <= 0:
            continue
        synthetic_fv = (cost * markup).quantize(_D('0.01'))
        try:
            Valuation.objects.update_or_create(
                investment=inv,
                valuation_date=today,
                defaults={
                    'fair_value': synthetic_fv,
                    'fair_value_of_holding': synthetic_fv,
                    'cost_basis': cost,
                    'methodology': 'derived_from_cost_x_scheme_markup',
                    'unrealized_gain_loss': synthetic_fv - cost,
                    # Universal: mark as 'approved' so the dashboard's per-row
                    # FV column reads it (frontend filters status='approved').
                    # The methodology tag still excludes it from fund-level
                    # active_fair_value/MOIC/TVPI/RVPI/IRR aggregates.
                    'status': 'approved',
                },
            )
            created += 1
        except Exception as e:
            logger.warning(f'[auto_valuation] failed for {inv.company_name}: {e}')

    logger.info(f'[auto_valuation] created {created} synthetic Valuation row(s) '
                f'using scheme markup={markup:.4f}')
    return created


def _backfill_investment_sector_multi(scheme, unified_json: dict) -> int:
    """Universal sector backfill — Gemini frequently drops the sector field
    even though canonical_schema declares it. This function applies a two-tier
    fallback to populate Investment.sector + PortfolioCompany.sector for any
    fund whose workbook actually contains sector data anywhere.

    Tier 1 — Phase 3 JSON scan
      Walk every list block in unified_json (portfolio_investments,
      portfolio_companies, quoted_unquoted, portfolio_hierarchy) and
      collect a company_name → sector map.

    Tier 2 — Direct workbook read
      When Tier 1 yields no map for a company, open the source xlsx via
      workbook_cache and scan EVERY sheet for a column whose header
      contains 'sector' or 'industry' + a parallel company-name column.
      Match by company name. Universal across any AIF format.

    Returns total Investment rows updated.
    """
    from investments.models import Investment, PortfolioCompany

    # ── Tier 1: scan unified_json blocks
    sector_by_name: dict[str, str] = {}

    def _absorb(rows):
        if not isinstance(rows, list): return
        for r in rows:
            if not isinstance(r, dict): continue
            name = (r.get('company_name') or r.get('name') or '').strip()
            if not name: continue
            sec = (r.get('sector') or r.get('industry')
                   or r.get('sector_name') or '').strip()
            if sec and name not in sector_by_name:
                sector_by_name[name] = sec[:100]

    u = unified_json or {}
    _absorb(u.get('portfolio_investments'))
    _absorb(u.get('portfolio_companies'))
    _absorb(u.get('quoted_unquoted'))
    _absorb(u.get('portfolio_hierarchy'))

    # ── Tier 2: direct workbook read for anything Tier 1 missed
    filepath = u.get('__source_filepath__')
    if filepath:
        try:
            from .phase3_layers.workbook_cache import load_workbook
            cached = load_workbook(filepath)
            # Use exact-match exclusion sets (NOT substring) because
            # substrings break — e.g. 'pan' is a substring of 'company_name'
            # at positions 3-5 (com[pan]y_name), so substring-match wrongly
            # excludes the company column. Fixed 2026-06-30.
            COMPANY_HDR_TOKENS = ('company_name', 'company name',
                                  'portfolio_company', 'portfolio company',
                                  'investee_company', 'investee company',
                                  'investee', 'company')
            COMPANY_HDR_EXCLUDE_EXACT = {'cin', 'pan', 'co_id', 'inv_id',
                                          'company_cin', 'company_pan'}
            COMPANY_HDR_EXCLUDE_KEYWORDS = ('fund_name', 'scheme_name',
                                             'investor_name', 'sponsor_name',
                                             'lp_name', 'gp_name')
            SECTOR_HDR_TOKENS = ('sector', 'industry', 'vertical')
            for sname, sheet_data in (cached.get('data') or {}).items():
                rows = sheet_data.get('rows') or []
                if not rows: continue
                # Locate header row containing BOTH a sector-like and a
                # company-like column (scan up to first 8 rows).
                for hi in range(min(8, len(rows))):
                    hrow = rows[hi]
                    sec_col = None; co_col = None
                    for ci, cell in enumerate(hrow):
                        if not isinstance(cell, str): continue
                        cl = cell.strip().lower()
                        if not cl: continue
                        if sec_col is None and any(t in cl for t in SECTOR_HDR_TOKENS) \
                                and 'sub' not in cl:
                            sec_col = ci
                        if co_col is None and any(t in cl for t in COMPANY_HDR_TOKENS) \
                                and cl not in COMPANY_HDR_EXCLUDE_EXACT \
                                and not any(k in cl for k in COMPANY_HDR_EXCLUDE_KEYWORDS):
                            co_col = ci
                    if sec_col is None or co_col is None:
                        continue
                    # Extract rows below the header
                    for di in range(hi + 1, len(rows)):
                        drow = rows[di]
                        if sec_col >= len(drow) or co_col >= len(drow):
                            continue
                        cname = drow[co_col]
                        sec   = drow[sec_col]
                        if not isinstance(cname, str) or not isinstance(sec, str):
                            continue
                        cname = cname.strip(); sec = sec.strip()
                        if cname and sec and cname not in sector_by_name:
                            sector_by_name[cname] = sec[:100]
                    break  # found a header in this sheet; move to next sheet
        except Exception as e:
            logger.warning(f'[sector_backfill] direct workbook read failed: {e}')

    # ── Apply to Investment + PortfolioCompany
    updated_inv = 0
    updated_co = 0
    for inv in Investment.objects.filter(scheme=scheme):
        if inv.sector:
            continue
        sec = sector_by_name.get(inv.company_name)
        if not sec:
            co = inv.portfolio_company
            if co and co.sector:
                sec = co.sector
        if sec:
            inv.sector = sec
            inv.save(update_fields=['sector'])
            updated_inv += 1

    co_ids = set(i.portfolio_company_id for i in Investment.objects.filter(scheme=scheme))
    for co in PortfolioCompany.objects.filter(id__in=co_ids):
        if co.sector:
            continue
        sec = sector_by_name.get(co.name)
        if sec:
            co.sector = sec
            co.save(update_fields=['sector'])
            updated_co += 1

    logger.info(
        f'[sector_backfill] map={len(sector_by_name)} entries; '
        f'updated {updated_inv} Investment + {updated_co} PortfolioCompany'
    )
    return updated_inv


def _compute_nav_fallback(row: dict) -> Optional[Decimal]:
    """Universal NAV computer for when source's total_nav is None/missing.

    Many fund Excels store Total NAV as a formula (=C+E+F-D etc.). When the
    workbook is saved without calculated values, openpyxl returns None for
    those cells — Gemini sees blank, JSON has no total_nav. This helper
    re-derives total_nav from its components, in priority order.

    Two strategies (returns first non-None):
      1. NAV per Unit × Units Outstanding — most precise when both present
      2. ASSETS − LIABILITIES with universal AIF accounting components

    Universal across funds — uses every standard component alias and works
    even when only a subset of inputs is present.
    """
    # Strategy 1: NAV per unit × units (works for any unit-based fund)
    npu = _d(row.get('nav_per_unit'))
    units = _d(row.get('total_units_outstanding')
               or row.get('units_outstanding') or row.get('units_os'))
    if npu is not None and units is not None and units > 0 and npu > 0:
        return npu * units

    # Strategy 2: standard AIF accounting — assets minus liabilities
    iaf    = _d(row.get('investments_at_fair_value')
                or row.get('total_investments')) or Decimal('0')
    cash   = _d(row.get('cash_and_equivalents')
                or row.get('fund_cash')) or Decimal('0')
    unreal = _d(row.get('unrealized_gains')
                or row.get('unrealised_gains')) or Decimal('0')
    real   = _d(row.get('realized_gains')
                or row.get('realised_gains')) or Decimal('0')
    mgmt   = _d(row.get('management_fee_payable')
                or row.get('mgmt_fee')) or Decimal('0')
    exp    = _d(row.get('fund_expenses')
                or row.get('accrued_expenses')) or Decimal('0')

    assets = iaf + cash + unreal + real
    liabilities = mgmt + exp
    computed = assets - liabilities
    return computed if computed > 0 else None


def _persist_nav_records(scheme, rows: list) -> int:
    from accounting.models import NAVRecord
    # Universal fund-context fallback for NAV rows that arrived without a
    # nav_date (e.g. the synthetic single-row NAV built from a KV-only
    # NAV Calculation sheet — True North Healthcare Fund VI's NAV sheet is
    # entirely key-value with no per-period date column). Falls back to the
    # scheme's final_close_date / first_close_date deterministically.
    _fund_ctx_nav_date = (getattr(scheme, 'final_close_date', None)
                          or getattr(scheme, 'first_close_date', None))
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Universal NAV date: accept the canonical `nav_date`, or any of the
        # common publication labels: `quarter` (Mar-20), `period` (Q1 FY22),
        # `period_end`, `financial_year` (FY 2019-20), plain `date`.
        # Fall back through _period_to_date() so Indian FY strings resolve.
        raw = (row.get('nav_date') or row.get('date') or row.get('period_end')
               or row.get('period') or row.get('quarter')
               or row.get('financial_year') or row.get('fy'))
        nd = _date(raw) or _period_to_date(raw)
        if not nd:
            nd = _fund_ctx_nav_date
        if not nd:
            continue
        # NOT-NULL on NAVRecord: total_nav, total_units_outstanding, nav_per_unit.
        # Use _first_present so a literal 0 from Gemini is preserved (vs. `or`
        # falling through). Fall back to computed components when source has
        # neither a direct total_nav nor any standard alias.
        total_nav = _d(_first_present(
            row.get('total_nav'),
            row.get('net_nav'),
            row.get('closing_nav'),
        ))
        if total_nav is None:
            total_nav = _compute_nav_fallback(row)
        if total_nav is None:
            total_nav = Decimal('0')
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


_PROVENANCE_SYNTHETIC_MARKERS = (
    'assumed', 'computed', 'synthetic', 'not_found_in_workbook',
    'not found in workbook', 'estimate', 'derived from', 'fabricated',
)


def _provenance_is_extracted(prov_value) -> bool:
    """True iff this provenance string looks like a real cell reference
    ('Sheet!A1' or 'Sheet!A1:B3' or 'Sheet R12'). Returns False for any
    formula (starts with '='), any '(assumed …)' tag, any 'computed …'
    marker, or anything that doesn't include a cell-like token.

    Universal — applies the same rule to every fund/sheet/field. Strict by
    design: when in doubt, treat as non-extracted so Phase 4 can run a
    deterministic Python computation instead of trusting Gemini's math.
    """
    if prov_value is None:
        return False
    s = str(prov_value).strip().lower()
    if not s:
        return False
    if s.startswith('='):
        return False                          # formula
    for marker in _PROVENANCE_SYNTHETIC_MARKERS:
        if marker in s:
            return False                      # explicit synthetic
    # A real cell-ref looks like "sheet!a1" — contains '!' followed by a
    # letter+digit. Or "sheet r12" / "row 12 col B".
    import re as _re
    if _re.search(r'![a-z]+\d+', s):
        return True
    if _re.search(r'\br\d+\b', s):             # "R12"
        return True
    if _re.search(r'\brow\s*\d+', s):
        return True
    return False


def _extracted_only(wf: dict, value_key: str, *prov_keys):
    """Return wf[value_key] ONLY if its provenance entry looks like a real
    cell reference. Otherwise return None — Phase 4 will compute it.

    Provenance can be keyed under the value's own name or any of the
    additional `prov_keys` (used when extraction wrote the value under a
    different name than the provenance entry — e.g. 'step_2_preferred_return'
    value alongside 'preferred_return_amount' provenance).
    """
    val = wf.get(value_key)
    if val is None or val == '':
        return None
    prov_block = wf.get('provenance') or {}
    if not isinstance(prov_block, dict):
        return None
    candidate_keys = (value_key,) + prov_keys
    for k in candidate_keys:
        if k in prov_block and _provenance_is_extracted(prov_block.get(k)):
            return val
    return None


def _persist_carried_interest(scheme, aggregates: dict, wf: dict, fp: dict):
    """Persist Carried Interest from the UNIVERSAL aggregator's output.

    `aggregates` is the dict returned by compute_all_fund_aggregates() —
    the single deterministic source consumed by both this writer and
    _persist_fund_metrics. Same DB → same numbers, every run.

    `wf` / `fp` are kept only for the calculation_status flag and as a
    last-resort cell-extracted fallback for fields the aggregator could
    not produce (e.g. when LPA terms missing and Gemini extracted a value
    directly from a Carry_Clawback cell that the aggregator's override
    detection missed).
    """
    from accounting.models import CarriedInterest

    cdate = (aggregates or {}).get('as_of_date') if aggregates else None
    cdate = cdate or _date(fp.get('as_of_date')) or date.today()
    defaults = {}

    # Universal fallback ladder: aggregates (Phase 4) → waterfall block
    # (Gemini's structured Phase 3 extraction) → None.
    # Added 2026-06-30 because Gemini already extracts `clawback_provision`,
    # `gp_holdback_escrow` and other waterfall step values into the wf
    # block — these are valid CA-extracted numbers we shouldn't discard
    # just because the Phase 4 reconciler doesn't track those exact keys.
    _WF_FALLBACK_MAP = {
        'total_distributions':     ('total_distributions',),
        'total_capital_called':    ('total_capital_called',),
        'preferred_return_amount': ('preferred_return_amount', 'step_2_preferred_return'),
        'carry_base':              ('carry_base',),
        'carry_amount_gross':      ('carry_amount_gross',),
        'carry_amount_net':        ('net_carry', 'carry_amount_net'),
        'gp_clawback_provision':   ('clawback_provision', 'gp_clawback_provision'),
    }

    def _take(field, agg_key):
        v = (aggregates or {}).get(agg_key)
        if v is None:
            for wf_key in _WF_FALLBACK_MAP.get(agg_key, ()):
                wf_v = (wf or {}).get(wf_key)
                if wf_v is not None and wf_v != '':
                    v = wf_v
                    break
        if v is not None:
            _set_if(defaults, field, _d(v))

    _take('total_distributions',     'total_distributions')
    _take('total_called_capital',    'total_capital_called')
    _take('preferred_return_amount', 'preferred_return_amount')
    _take('carry_base',              'carry_base')
    _take('carry_amount_gross',      'carry_amount_gross')
    _take('carry_amount_net',        'carry_amount_net')
    _take('gp_clawback_provision',   'gp_clawback_provision')

    # Fix 2 (2026-07-06) — Universal carry_amount_net derivation.
    # If no explicit "Net Carry" cell survived extraction OR reconciliation
    # (e.g. Sequoia only publishes "Carry Escrow Balance" — correctly
    # mapped to gp_clawback_provision by Fix D — with no separate net-carry
    # number), derive net = gross − clawback. Only fires when the
    # authoritative field is still empty AND both inputs are populated;
    # extracted values always win.
    if defaults.get('carry_amount_net') is None:
        _derived_net = _derive_carry_net(aggregates or {}, wf)
        if _derived_net is not None:
            defaults['carry_amount_net'] = _d(_derived_net)

    status = _str((wf or {}).get('carry_status'), 16).lower()
    if status in ('indicative', 'crystallised', 'paid'):
        defaults['calculation_status'] = status

    _safe_save(CarriedInterest,
        lookup_kwargs={'scheme': scheme, 'calculation_date': cdate},
        defaults=defaults,
    )


def _persist_fund_metrics(organization, scheme, fp: dict, wf: dict,
                          valuation_rows: list, import_file,
                          reconciliation: dict | None = None,
                          fm: dict | None = None,
                          aggregates: dict | None = None) -> int:
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

    # ── STALE-ROW CLEANUP (universal) ─────────────────────────────────
    # Every import re-derives every aggregate from scratch via the Phase 4
    # aggregator. If a metric is no longer derivable (e.g. Commitment table
    # empty this run → committed_capital = None), the persister loop below
    # silently skips it — which would leave a wrong value from a PREVIOUS
    # import sitting on the dashboard. Wipe the slate so only the current
    # import's values survive. Universal across any fund / re-import.
    stale_deleted, _ = FundMetric.objects.filter(
        organization=organization, scheme=scheme,
    ).delete()
    if stale_deleted:
        logger.info(
            f'[persist_fund_metrics] cleared {stale_deleted} stale FundMetric '
            f'row(s) for {scheme.name} before writing this import\'s values'
        )

    # ── (a) Authoritative FV total: sum of latest persisted Valuation per
    #        Investment, preferring fair_value_of_holding. With Rule 26 fixed
    #        (cost_basis discriminator), every distinct investment has its own
    #        Valuation row, so this DB sum equals Gemini's per-row sum and
    #        matches what the per-company dashboard tiles + chatbot display.
    # Active fair value = sum of latest SOURCE Valuation per Investment.
    # Synthetic Valuations (methodology='derived_from_cost_x_scheme_markup')
    # are excluded so the dashboard's active_fair_value / MOIC / TVPI / RVPI
    # tiles reflect only real source-reported valuations. The per-investment
    # FV column still shows synthetic estimates (read directly from Valuation).
    # Universal — synthetic rows are tagged at creation time.
    # FV precedence (2026-07-11 flip): prefer `fair_value_of_holding` (the
    # FUND'S stake in the company — what an LP would actually get) and fall
    # back to `fair_value` (the company's total equity value) only when the
    # workbook doesn't publish a per-holding column. Universal:
    #   • Single-FV-column workbooks mirror both fields at persist time, so
    #     preferring either column yields the identical number.
    #   • Multi-column workbooks (distinct Equity Val vs FV Holding) now use
    #     the LP-stake column — matches Excel Cover 'Total FV Unrealised'
    #     (SUM VALUATIONS!P) and the industry-standard AIF convention.
    latest_per_inv = Valuation.objects.filter(
        investment=OuterRef('pk'),
    ).exclude(
        methodology='derived_from_cost_x_scheme_markup',
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
    #        Falls back through three sources: Distribution events → Gemini
    #        fund-perf totals → sum of per-LP cumulative_distributed (Bug 3 fix).
    from lp.models import Distribution, Commitment
    from django.db.models import Sum
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
    # Per-LP cumulative distributions (Bug 3 fix) — set by _persist_commitments
    # from the Investors/LP-Master sheet.
    lp_cumulative_dist_sum = Commitment.objects.filter(
        scheme=scheme,
    ).aggregate(s=Sum('cumulative_distributed'))['s'] or Decimal('0')
    lp_distributions_value = (
        db_capital_distributions if db_capital_distributions > 0
        else (gemini_total_dist if gemini_total_dist not in (None, Decimal('0'))
              else lp_cumulative_dist_sum)
    )

    # ── (a3) Authoritative Called Capital (Bug 4 fix) ───────────────────
    # Source priority (universal — works across any AIF Excel):
    #   1. Gemini fund_performance.total_called_capital  (explicit headline)
    #   2. Sum of CapitalCall events                    (per-event rollup)
    #   3. Sum of per-LP cumulative_called               (Investors sheet)
    #   4. waterfall.total_capital_called                (cross-check from wf block)
    # Many fund-admin Excels publish per-LP drawdowns on the Investors
    # sheet only, with a sparse Capital Calls sheet — the LP sum is the
    # truer number in that case. We prefer larger sources to avoid
    # under-reporting Called Capital when explicit events are incomplete.
    from lp.models import CapitalCall
    db_capital_calls_sum = CapitalCall.objects.filter(
        scheme=scheme,
    ).aggregate(s=Sum('total_call_amount'))['s'] or Decimal('0')
    lp_cumulative_called_sum = Commitment.objects.filter(
        scheme=scheme,
    ).aggregate(s=Sum('cumulative_called'))['s'] or Decimal('0')
    gemini_called = _d(_first_present(
        fp.get('total_called_capital'),
        wf.get('total_capital_called'),
    ))
    # Pick the largest non-None source. This catches the Tata-style case
    # where Capital Calls sheet has 1 row of ₹44 Cr but Investors sheet
    # has per-LP drawdowns summing to ₹500+ Cr — without distorting funds
    # where the explicit Capital Calls sheet IS complete.
    called_candidates = [v for v in (
        gemini_called, db_capital_calls_sum, lp_cumulative_called_sum,
    ) if v is not None]
    called_capital_value = max(called_candidates) if called_candidates else None

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

    # Fix (2026-07-10) — Provenance values now read from `aggregates` FIRST.
    # Aggregates is the authoritative post-reconciler dict that CarriedInterest
    # / FundMetric were actually written from. Reading from raw `wf` / `fp`
    # first (as previous code did) produced "?" in the substituted formula
    # for every fund whose Fund_Overview did not publish a matching aggregate
    # cell — even though the number was computed correctly downstream.
    # Universal: aggregates always has these keys (computed_from_db or
    # extracted_verified); fall back to wf/fp only for legacy paths.
    _agg_prov = aggregates or {}
    _wf_total_dist   = _d(_first_present(
        _agg_prov.get('total_distributions'),
        wf.get('total_distributions'), fp.get('total_distributions')))
    _wf_called       = _d(_first_present(
        _agg_prov.get('total_capital_called'),
        wf.get('total_capital_called'), fp.get('total_called_capital')))
    _wf_pref         = _d(_first_present(
        _agg_prov.get('preferred_return_amount'),
        wf.get('step_2_preferred_return'), wf.get('preferred_return_amount')))
    # Fix (2026-07-10) — carry_base uses RESIDUAL NAV (sum of per-investment
    # fair_value_of_holding), not accounting NAV. `total_unrealised_fv_holding`
    # is the authoritative residual computed by compute_all_fund_aggregates.
    _wf_residual_nav = _d(_first_present(
        _agg_prov.get('total_unrealised_fv_holding'),
        _agg_prov.get('active_fair_value'),
        fp.get('fund_nav_latest')))
    _wf_nav          = _d(_first_present(
        _agg_prov.get('fund_nav_latest'),
        fp.get('fund_nav_latest')))
    _wf_carry_gross  = _d(_first_present(
        _agg_prov.get('carry_amount_gross'),
        wf.get('carry_amount_gross'), fp.get('carry_amount_gross')))
    _wf_clawback     = _d(_first_present(
        _agg_prov.get('gp_clawback_provision'),
        wf.get('clawback_provision'), fp.get('gp_clawback_provision')))
    _wf_catchup      = _d(_first_present(
        _agg_prov.get('gp_catchup_amount'),
        wf.get('step_3_catchup_amount')))
    _wf_step4b       = _d(wf.get('step_4b_gp_residual_carry'))
    _wf_carry_pct    = _d(_first_present(
        wf.get('carry_percentage'),
        getattr(scheme, 'carry_pct', None)))
    _wf_hurdle       = _d(_first_present(
        wf.get('hurdle_rate'),
        getattr(scheme, 'hurdle_rate_pct', None)))
    _wf_years        = _d(wf.get('step_2_years_compounded'))
    # Actual holdback rate used in the computation — from Scheme, not
    # hardcoded. Feeds the clawback / gp_holdback provenance strings so
    # the user sees the LPA-declared value.
    _wf_holdback_pct = _d(getattr(scheme, 'gp_holdback_pct', None))

    # Fix (2026-07-10) — For carry-component amounts whose semantic is
    # "0 = no carry activity yet" (fund open, GP hasn't been paid),
    # substitute Decimal('0') when the aggregator returned None. Matches
    # the CarriedInterest model's NOT NULL DEFAULT 0 for these fields, so
    # the provenance panel shows "₹0 Cr" instead of "₹? Cr" — which
    # matches what the dashboard tile shows.
    #
    # Universal: applies to gp_clawback_provision (open funds show 0),
    # gp_catchup_amount (no distributions yet → 0 catchup), and
    # gp_carry_holdback_amount (nothing paid → nothing held back).
    #
    # NOTE: percentages (hurdle_rate, carry_pct, holdback_pct) are NOT
    # defaulted to 0 — an unknown rate is genuinely unknown, and showing
    # "0%" would be misleading. Percentages stay as None → "?" so the
    # user knows the LPA field is missing.
    if _wf_clawback is None:
        _wf_clawback = Decimal('0')
    if _wf_catchup is None:
        _wf_catchup = Decimal('0')

    # Additional carry components read from aggregates for the Net Carry
    # formula. These are the SAME three inputs the aggregator uses to
    # compute carry_amount_net internally (phase4_derivations line 1439/1443
    # for gp_data_captured funds, 1425/1428 for formula-derived funds).
    # Universal — all three default to 0 for open funds with no GP payout.
    _wf_gp_distributed  = _d(_agg_prov.get('gp_carry_distributed'))
    _wf_gp_holdback_amt = _d(_agg_prov.get('gp_holdback_escrow'))
    if _wf_gp_distributed is None:
        _wf_gp_distributed = Decimal('0')
    if _wf_gp_holdback_amt is None:
        _wf_gp_holdback_amt = Decimal('0')

    # ── Atomic per-event breakdown (used in provenance to show real numbers
    # instead of generic "XIRR solver over ..." placeholder).
    # Universal across funds — pulls from the same DB rows the math uses.
    from lp.models import CapitalCall as _CC, Distribution as _D2
    _cc_qs = _CC.objects.filter(scheme=scheme).order_by('call_date')
    _d_qs  = _D2.objects.filter(scheme=scheme).order_by('distribution_date')
    _atomic_call_total = sum((c.total_call_amount or Decimal('0')) for c in _cc_qs)
    _atomic_dist_total = sum(((d.total_net_amount if d.total_net_amount is not None else d.total_gross_amount)
                              or Decimal('0')) for d in _d_qs)
    _atomic_call_count = _cc_qs.count()
    _atomic_dist_count = _d_qs.count()
    # Terminal FV used by IRR — prefer atomic source FV (matches what
    # phase4 IRR computation uses; excludes synthetic auto-valuations).
    # Use a locally-resolved agg ref because `agg = aggregates or {}` is
    # assigned later in this function.
    _agg_local = aggregates or {}
    _atomic_fv = _d(_agg_local.get('total_unrealised_fv_holding')) or active_fv
    # Total invested (cost basis denominator for MOIC)
    _atomic_invested = _d(_agg_local.get('total_invested_capital'))
    # Realised proceeds (numerator addend for Gross MOIC) — matches Excel B9
    # 'Total Realised Proceeds' and ExitEvent.proceeds. Universal.
    _atomic_realised = _d(_agg_local.get('total_realised_proceeds')) or Decimal('0')

    def _canonical_formula(metric_key):
        """Return (formula_template, substituted_formula) for derived metrics.
        Used as a fallback when Gemini doesn't emit per-key provenance."""
        # Each entry: (description, template, substituted with real numbers)
        T = _fmt_num
        if metric_key == 'carry_base':
            # Fix U5 provenance: match the actual formula in
            # phase4_derivations._compute_european_waterfall — carry_base is
            # "total profit above capital" = (realised + residual NAV) − called.
            # NO preferred_return subtraction here — that happens later in the
            # catch-up / step-4 split, not in the base itself. Matches the
            # Bharatcrest Carry_Clawback ground truth cell "Total Profit above
            # Capital = 1430.60".
            return ('(Distributions + Residual NAV) − Called Capital',
                    f'(₹{T(_wf_total_dist)} Cr + ₹{T(_wf_residual_nav)} Cr) − ₹{T(_wf_called)} Cr')
        if metric_key == 'carry_amount_gross':
            # Two equivalent forms — the one that shows real numbers first:
            #   (a) 20% × carry_base                    ← always populated
            #   (b) GP Catch-up + GP share of residual  ← only when the
            #                                             per-step decomposition
            #                                             was published
            if _wf_carry_pct is not None and _wf_total_dist is not None \
                    and _wf_residual_nav is not None and _wf_called is not None:
                _base_num = (_wf_total_dist + _wf_residual_nav - _wf_called)
                if _base_num < 0:
                    _base_num = Decimal('0')
                return (f'{T(_wf_carry_pct)}% × Carry Base',
                        f'{T(_wf_carry_pct)}% × ₹{T(_base_num)} Cr')
            return ('GP Catch-up + GP share of residual after catch-up',
                    f'₹{T(_wf_catchup)} Cr + ₹{T(_wf_step4b)} Cr')
        if metric_key == 'carry_amount_net':
            # Match the actual aggregator computation (phase4_derivations
            # line 1443 for gp_data_captured funds; line 1428 else):
            #     net = distributed_to_GP − holdback − clawback
            # For open funds with no GP payout yet, all three inputs are 0,
            # so the formula honestly shows "0 − 0 − 0 = 0" instead of the
            # arithmetically-inconsistent "gross − 0 = 0" the old formula
            # produced. For Bharatcrest-style closed distributions, the
            # three real values show (296.12 − 59.22 − 10 = 226.90).
            return ('Distributed to GP − Holdback Escrow − Clawback Provision',
                    f'₹{T(_wf_gp_distributed)} Cr − ₹{T(_wf_gp_holdback_amt)} Cr − ₹{T(_wf_clawback)} Cr')
        if metric_key == 'gp_clawback_provision':
            # Fix U6 provenance: no hardcoded 20%. Use the LPA-declared
            # Scheme.gp_holdback_pct when present; otherwise show that the
            # LPA is silent (no invented number).
            if _wf_holdback_pct is not None:
                return (f'{T(_wf_holdback_pct)}% of Gross Carry (LPA escrow rate)',
                        f'₹{T(_wf_carry_gross)} Cr × {T(_wf_holdback_pct)}%')
            return ('Clawback provision (LPA holdback rate not published)',
                    f'—')
        if metric_key == 'preferred_return_amount':
            return ('LP Called × ((1 + hurdle)^years − 1)',
                    f'₹{T(_wf_called)} Cr × ((1 + {T(_wf_hurdle)}%)^{T(_wf_years)} − 1)')
        if metric_key == 'gp_catchup_amount':
            return ('Preferred Return × (carry% / (1 − carry%))',
                    f'₹{T(_wf_pref)} Cr × ({T(_wf_carry_pct)}% / (100% − {T(_wf_carry_pct)}%))')
        if metric_key == 'return_of_capital_amount':
            return ('LP Called Capital (returned 100% in Step 1)',
                    f'₹{T(_wf_called)} Cr')
        # Performance ratios — show ATOMIC inputs (source-only, matches the
        # actual computation the dashboard tile reads). Previously these used
        # the extracted (often inflated) fund_nav, which made the audit drawer
        # show different inputs than what the tile value was computed from.
        if metric_key == 'tvpi':
            # Show ONLY the formula for the P-tier that actually fired.
            # The priority-ladder section elsewhere on the panel lists all
            # four tiers with a "Used" badge on the chosen one, so the user
            # already sees the alternatives — this section is strictly the
            # arithmetic that produced the displayed value.
            _tvpi_code = agg.get('tvpi_method')
            _tvpi_dist_val = _d(agg.get('total_distributions')) or Decimal('0')
            _tvpi_res_val  = _d(_first_present(
                agg.get('total_unrealised_fv_holding'),
                agg.get('active_fair_value'))) or Decimal('0')
            _tvpi_fund_nav = _d(agg.get('fund_nav_latest')) or Decimal('0')
            _tvpi_called   = _d(agg.get('total_capital_called')) or Decimal('0')
            if _tvpi_code == 'p1_computed_with_distributions':
                return ('P1: (Total Distributions + Residual NAV) / Total Called Capital',
                        f'(₹{T(_tvpi_dist_val)} Cr + ₹{T(_tvpi_res_val)} Cr) / ₹{T(_tvpi_called)} Cr')
            if _tvpi_code == 'p2_computed_fund_nav_over_called':
                return ('P2: Fund NAV / Total Called Capital',
                        f'₹{T(_tvpi_fund_nav)} Cr / ₹{T(_tvpi_called)} Cr')
            if _tvpi_code == 'p3_extracted_cell':
                return ('P3: Extracted directly from a workbook cell labelled "TVPI" / "Net MOIC"',
                        'value read directly from workbook — no arithmetic')
            # p4_insufficient_data or None — value is blank, no arithmetic to show
            return ('P4: Insufficient data — no formula produced a value',
                    'see the Missing inputs table for the specific inputs absent')
        if metric_key == 'dpi':
            return ('Total Distributions / Called Capital',
                    f'₹{T(_atomic_dist_total)} Cr / ₹{T(_atomic_call_total)} Cr')
        if metric_key == 'rvpi':
            return ('Atomic FV (sum of source Valuations) / Called Capital',
                    f'₹{T(_atomic_fv)} Cr / ₹{T(_atomic_call_total)} Cr')
        if metric_key == 'moic':
            # Gross MOIC (universal AIF industry-standard, matches Excel row 15
            # `(B8 + B9) / B7`): numerator uses Realised Proceeds — full exit
            # cashback — NOT LP Distributions. The aggregator switched to this
            # formula on 2026-07-11; the provenance panel must reflect it.
            # 2026-07-11: renamed "Atomic FV" -> "Active Fair Value" and
            # "Total Invested Capital" -> "Total Cost" so the formula reads
            # the same as the KPI tiles + AIF industry convention.
            return ('(Realised Proceeds + Active Fair Value) / Total Cost',
                    f'(₹{T(_atomic_realised)} Cr + ₹{T(_atomic_fv)} Cr) / ₹{T(_atomic_invested)} Cr')
        if metric_key == 'net_irr':
            # Method-aware formula display. Net IRR is computed via a 3-tier
            # priority ladder in phase4; the description shown here reflects
            # which tier actually produced the current value + a small note
            # of the fallback order so the user understands the choice.
            _method = agg.get('net_irr_method')
            _ladder_note = (
                'Ladder (Option B, 2026-07-07): '
                '1) Priority 1 XIRR on real dated CapitalCall + Distribution + terminal NAV  '
                '→  2) Extracted directly from a workbook cell (net_irr_stated)  '
                '→  3) Insufficient-data blank with itemised reason.'
            )
            if _method == 'priority1_xirr':
                return (
                    'Priority 1 (chosen): XIRR on real dated cashflows + terminal NAV. ' + _ladder_note,
                    f'XIRR over {{ {_atomic_call_count} calls totalling −₹{T(_atomic_call_total)} Cr, '
                    f'{_atomic_dist_count} distributions totalling +₹{T(_atomic_dist_total)} Cr, '
                    f'terminal Atomic FV +₹{T(_atomic_fv)} Cr at as_of_date }}'
                )
            if _method == 'extracted_cell':
                return (
                    'Priority 2 (chosen): Extracted directly from the workbook cell (net_irr_stated). ' + _ladder_note,
                    'The fund file publishes an explicit Net IRR value in a cell '
                    '(verified provenance). No XIRR computation performed — the '
                    'workbook author\'s stated Net IRR is used as-is.'
                )
            _reason = (agg.get('reasons') or {}).get('net_irr') if isinstance(agg.get('reasons'), dict) else None
            return (
                'Net IRR unavailable: ' + (_reason or 'insufficient data.') + ' ' + _ladder_note,
                _reason or (
                    'Neither Priority 1 (XIRR on real dated cashflows + terminal '
                    'NAV) nor Priority 2 (extracted workbook cell) produced a '
                    'value. See the "reasons" block for the itemised list of '
                    'missing inputs.'
                )
            )
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
    # We surface that 0 explicitly when (and only when) the fund is in the
    # pre-carry phase so the cards render "₹0 Cr" rather than blank.
    #
    # Universal ROC-phase detection: In European whole-fund waterfalls,
    # GP earns ZERO carry until 100% of called capital is returned to LPs.
    # A fund with partial distributions (interim dividends, partial exits)
    # is still in the pre-carry phase if `total_distributions < called`.
    # Detecting on "no distributions at all" was too strict — it caused
    # clawback / gross carry / net carry to show "—" for funds that had
    # any interim distribution even though carry was mathematically 0.
    # Universal across every European-waterfall fund.
    _lp_dist = lp_distributions_value or Decimal('0')
    _called  = called_capital_value or Decimal('0')
    _fund_in_roc_phase = (
        _lp_dist == Decimal('0')           # no distributions at all
        or _called > _lp_dist              # capital not yet fully returned
    )
    _ZERO = Decimal('0')

    def _zero_if_roc(raw):
        if raw is not None:
            return raw
        return _ZERO if _fund_in_roc_phase else None

    # ── (a4) Derived consistency — uncalled_capital and dpi must stay in
    #        sync with the called_capital we just recomputed. Gemini's
    #        original total_uncalled_capital was calculated against its own
    #        (often understated) called number; recompute here so the trio
    #        committed/called/uncalled is always coherent on the dashboard.
    committed_val = _d(fp.get('total_committed_capital'))
    uncalled_capital_value = None
    if committed_val is not None and called_capital_value is not None:
        uncalled_capital_value = max(Decimal('0'), committed_val - called_capital_value)
    else:
        uncalled_capital_value = _d(fp.get('total_uncalled_capital'))

    # DPI = LP distributions / Called Capital. Recompute when we have
    # better called/distributions data than Gemini's headline.
    dpi_value = None
    if called_capital_value and called_capital_value > 0:
        dpi_value = (lp_distributions_value or Decimal('0')) / called_capital_value
    else:
        dpi_value = _d(fp.get('dpi'))

    # ── (b) UNIVERSAL SOURCE OF TRUTH — aggregator output ──────────────
    # Every aggregate below comes from compute_all_fund_aggregates() (Phase
    # 4 deterministic aggregator). Same DB rows + same LPA terms = same
    # numbers on every re-import. Gemini's stochastic computed values are
    # already filtered out at the aggregator level (only cell-ref-extracted
    # values pass through as overrides). Pre-aggregator wf/fp fallbacks
    # remain only for the small set of fields the aggregator doesn't yet
    # produce (LPA terms, accrued mgmt fees, step_1_return_of_capital).
    agg = aggregates or {}
    metric_map = {
        # Performance ratios — aggregator-derived from atomic ledgers
        'moic':                     agg.get('moic'),
        'tvpi':                     agg.get('tvpi'),
        'dpi':                      agg.get('dpi'),
        'rvpi':                     agg.get('rvpi'),
        'net_irr':                  agg.get('net_irr'),
        # Universal Net-IRR method tag — passes the tier used (Priority 1/2/3)
        # into FundMetric.notes so the frontend can display which computation
        # produced the value. Values (Option B, 2026-07-07):
        #   'priority1_xirr'    — XIRR on real dated cashflows + terminal NAV
        #   'extracted_cell'    — value published in the workbook
        #   'insufficient_data' — no computable value; see reasons['net_irr']
        'net_irr_method':           agg.get('net_irr_method'),
        # Totals — aggregator-derived
        'committed_capital':        agg.get('total_committed_capital'),
        'called_capital':           agg.get('total_capital_called'),
        'uncalled_capital':         agg.get('total_uncalled_capital'),
        'invested_cost':            agg.get('total_invested_capital'),
        'realized_proceeds':        agg.get('total_realised_proceeds'),
        'lp_distributions':         agg.get('total_distributions'),
        # Universal FV tile (2026-07-11 semantic clarification):
        #
        #   active_fair_value  =  fund's stake in still-held positions only
        #                         (mark-to-market of unrealised portfolio,
        #                          on LP-holding basis — matches Excel B8
        #                          "Total FV Unrealised" / SUM VALUATIONS!P).
        #
        #   total_fair_value   =  active_fair_value + realised_proceeds
        #                         (industry-standard "Total Fair Value" for
        #                          AIFs — matches Excel Cover B8 + B9).
        #
        # Fallback ladder for active_fair_value: prefer LP-holding basis
        # (total_unrealised_fv_holding) — fall back to equity-basis
        # (total_portfolio_fv) only when the workbook has no per-holding
        # column, then to live-DB FV sum, then to extracted NAV. Universal.
        'active_fair_value':        (agg.get('total_unrealised_fv_holding')
                                     or agg.get('total_portfolio_fv')
                                     or active_fv
                                     or agg.get('fund_nav_latest')),
        'total_fair_value':         _total_fair_value_metric(
                                        active_fv=(agg.get('total_unrealised_fv_holding')
                                                   or agg.get('total_portfolio_fv')
                                                   or active_fv
                                                   or agg.get('fund_nav_latest')),
                                        realised=agg.get('total_realised_proceeds'),
                                    ),
        'fund_nav':                 agg.get('fund_nav_latest'),
        # Waterfall — aggregator-derived (extracted-first, else Python)
        # Waterfall metrics with Phase-3 wf-block fallback (Gemini-extracted
        # values take precedence over formula-computed 0 when atomic ledger
        # lacks per-event GP carry data). Added 2026-06-30.
        'carry_amount_gross':       _zero_if_roc(_first_present(agg.get('carry_amount_gross'),  (wf or {}).get('carry_amount_gross'))),
        # Net Carry uses the AIF-standard formula (Gross × (1 − Clawback %))
        # via the aggregator's P1/P2/P3 ladder. NO _zero_if_roc wrapping —
        # if the aggregator emits None with carry_net_method='p3_insufficient_
        # data', we want that None to flow through so the persister writes a
        # blank FundMetric row with the itemised missing-inputs table (rather
        # than a misleading ₹0). If the aggregator gives a real value we use
        # that; fallback to extracted-cell candidates from Gemini's wf block.
        'carry_amount_net':         _first_present(
            agg.get('carry_amount_net'),
            (wf or {}).get('net_carry'),
            (wf or {}).get('carry_amount_net'),
        ),
        'gp_clawback_provision':    _zero_if_roc(_first_present(agg.get('gp_clawback_provision'), (wf or {}).get('clawback_provision'), (wf or {}).get('gp_clawback_provision'))),
        'gp_catchup_amount':        _zero_if_roc(_first_present(agg.get('gp_catchup_amount'),   (wf or {}).get('step_3_catchup_amount'), (wf or {}).get('gp_catchup_amount'))),
        'preferred_return_amount':  _first_present(agg.get('preferred_return_amount'),          (wf or {}).get('step_2_preferred_return'), (wf or {}).get('preferred_return_amount')),
        'return_of_capital_amount': (wf or {}).get('step_1_return_of_capital'),
        'carry_base':               _zero_if_roc(agg.get('carry_base')),
        'lp_total_return':          _first_present((wf or {}).get('lp_share'), (wf or {}).get('step_4a_lp_residual')),
        'gp_total_distribution':    (wf or {}).get('gp_share'),
        'accrued_management_fees':  (fp or {}).get('accrued_management_fees'),
        # Scheme terms — dashboard reads these via FundMetric (not Scheme model)
        # to show "X% Hurdle · Y% Carry · Z% Mgmt Fee" in the Waterfall header.
        'hurdle_rate':              (fm or {}).get('hurdle_rate_pct'),
        'carry_pct':                (fm or {}).get('carry_pct'),
        'mgmt_fee_pct':             (fm or {}).get('management_fee_pct'),
        'sponsor_commitment_pct':   (fm or {}).get('sponsor_commitment_pct'),
    }

    count = 0
    _NET_IRR_METHOD_LABELS = {
        'priority1_xirr': 'Calculated — XIRR on real dated cashflows (capital calls + distributions + terminal NAV)',
        'extracted_cell': 'Extracted — Net IRR read directly from a labelled workbook cell',
        'insufficient_data': 'Insufficient data — cannot compute or extract; see reasons below',
    }
    _CARRY_METHOD_LABELS = {
        'p1_extracted_cell':    'P1 — Extracted from workbook (a Carry_Clawback / Fund_Overview / WATERFALL_EUR cell)',
        'p2_computed_formula':  'P2 — Computed via universal formula: carry_pct × max(0, (Realised + Residual NAV) − Called). European Whole-Fund + 100% GP Catch-Up (ILPA standard).',
        'p3_insufficient_data': 'P3 — Insufficient data. Cannot compute AND no workbook cell available. See "Missing inputs" table.',
    }
    _CARRY_PRIORITY_LADDER = [
        'P1 — Extract directly from a published workbook cell (Carry_Clawback / Fund_Overview / WATERFALL_EUR). Wins whenever the CA has written a "GP Total Carry" / "Carry Provision" value.',
        'P2 — Compute via the universal formula. Waterfall variant is fixed to European Whole-Fund with 100% GP Catch-Up. Formula: Carry = carry_pct × max(0, (Realised Proceeds + Residual NAV) − Called Capital).',
        'P3 — Blank. Neither extract nor compute produced a value; the "Missing inputs" table lists exactly which inputs are absent.',
    ]
    _CARRY_NET_METHOD_LABELS = {
        'p1_extracted_cell':    'P1 — Extracted from workbook (a "Net Carry" / "Carry Payable" / "Performance Fee Payable" cell)',
        'p2_computed_formula':  'P2 — Computed via AIF-standard formula: Net = Gross Carry × (1 − Clawback Reserve %). Universal across every European whole-fund AIF.',
        'p3_insufficient_data': 'P3 — Insufficient data. Either Gross Carry or Clawback Reserve % is unavailable. See "Missing inputs" table.',
    }
    _CARRY_NET_PRIORITY_LADDER = [
        'P1 — Extract directly from a published workbook cell (MASTER_INPUTS "Performance Fee / Carry Payable" / Fund_Overview "Net Carry" / any labelled net-carry balance).',
        'P2 — Compute via the AIF-standard formula. Net Carry = Gross Carry × (1 − Clawback Reserve %). Clawback Reserve % is a mandatory LPA disclosure (typically 20–30%).',
        'P3 — Blank. Either Gross Carry could not be derived, or Clawback Reserve % is not published on Scheme.gp_holdback_pct.',
    ]
    _TVPI_METHOD_LABELS = {
        'p1_computed_with_distributions':   'P1 — Computed via Formula 1 (fund has distributions): Net MOIC = (Total Distributions + Residual NAV) / Total Called Capital.',
        'p2_computed_fund_nav_over_called': 'P2 — Computed via Formula 2 (pre-distribution phase): Net MOIC = Fund NAV / Total Called Capital.',
        'p3_extracted_cell':                'P3 — Extracted directly from a workbook cell labelled "TVPI" / "Net MOIC".',
        'p4_insufficient_data':             'P4 — Insufficient data. Neither formula could be computed AND no extracted cell available. See "Missing inputs" table.',
    }
    _TVPI_PRIORITY_LADDER = [
        'P1 — Formula 1 (used when the fund has distributed to LPs): Net MOIC = (Total Distributions + Residual NAV) / Total Called Capital. LP-perspective TVPI, matches ILPA standard.',
        'P2 — Formula 2 (fallback for pre-distribution funds): Net MOIC = Fund NAV / Total Called Capital. Uses accounting NAV as a proxy when no cash has flowed out yet.',
        'P3 — Extract directly from a published workbook cell labelled "TVPI" / "Net MOIC" (CA-provided headline value).',
        'P4 — Blank. None of Formula 1, Formula 2, or a published cell could produce a value. See the "Missing inputs" table for the specific inputs absent.',
    ]
    # Metrics for which a P-blank row still gets persisted (so the frontend
    # can render the sliding panel and explain what's missing). For every
    # other metric, val is None → skip.
    _P3_BLANK_KEYS = {'carry_base', 'carry_amount_gross', 'carry_amount_net', 'tvpi'}
    for key, raw_value in metric_map.items():
        # net_irr_method / carry_method are metadata routed through metric_map
        # so the frontend can read them via FundMetric.inputs_used. Skip their
        # own FundMetric rows.
        if key in ('net_irr_method',):
            continue
        val = _d(raw_value)
        _carry_method_code     = agg.get('carry_method')
        _carry_net_method_code = agg.get('carry_net_method')
        _tvpi_method_code      = agg.get('tvpi_method')
        # Allow P-blank rows through for carry/tvpi keys so the sidebar panel
        # can render "insufficient data" with the itemised missing inputs
        # table. For every other metric, val=None still skips.
        _is_p3_gross_key = (key in ('carry_base', 'carry_amount_gross')
                            and _carry_method_code == 'p3_insufficient_data')
        _is_p3_net_key   = (key == 'carry_amount_net'
                            and _carry_net_method_code == 'p3_insufficient_data')
        _is_p4_tvpi_key  = (key == 'tvpi'
                            and _tvpi_method_code == 'p4_insufficient_data')
        if val is None and not (_is_p3_gross_key or _is_p3_net_key or _is_p4_tvpi_key):
            continue
        prov = _provenance_for(key, raw_value)
        # For net_irr: attach the priority-tier method used, a human label,
        # and the itemised reasons dict emitted by compute_all_fund_aggregates.
        # The frontend renders every reason key under the "Method used" panel
        # so the user sees exactly what inputs were present, what formula
        # ran, and (in Case 1) both the calculated + extracted values when
        # they disagree.
        if key == 'net_irr':
            _method_code = agg.get('net_irr_method')
            if _method_code:
                prov['net_irr_method'] = _method_code
                prov['net_irr_method_label'] = _NET_IRR_METHOD_LABELS.get(
                    _method_code, _method_code
                )
                prov['net_irr_priority_ladder'] = [
                    'Case 1 — Both calculated & extracted present → prefer calculated; show both if they disagree',
                    'Case 2 — Only calculated → use calculated',
                    'Case 3 — Only extracted → use extracted (Priority 1 inputs incomplete)',
                    'Case 4 — Neither → blank with itemised reason',
                ]
                _reasons_dict = (agg or {}).get('reasons') or {}
                for _rk in (
                    'net_irr_source', 'net_irr_stated_alt', 'net_irr_terminal',
                ):
                    _rv = _reasons_dict.get(_rk)
                    if _rv:
                        prov[_rk] = _rv
        # ── Universal Fund NAV provenance ─────────────────────────────
        # Attach the P1/P2 tier code, formula, both values (computed and
        # extracted), the source cell for the extracted value, and the full
        # 7-component input table. The frontend sidebar reads inputs_used
        # to render the professional NAV panel with a component table plus
        # an "also extracted from workbook" row when the two disagree.
        if key == 'fund_nav':
            _nav_prov = (agg or {}).get('fund_nav_provenance') or {}
            if _nav_prov:
                prov['fund_nav_method']        = _nav_prov.get('method')
                prov['fund_nav_method_label']  = _nav_prov.get('method_label')
                prov['fund_nav_formula']       = _nav_prov.get('formula')
                prov['fund_nav_priority_ladder'] = [
                    'P1 — Compute via the universal AIF NAV formula. All 7 balance-sheet components (Realised, Unrealised, Cash, Receivables, Mgmt Fee, Carry, Other Liabilities) must be present. When available, the computed value ALWAYS wins over the extracted cell.',
                    'P2 — Fall back to the extracted "TOTAL FUND NAV" cell from the workbook when any component is missing.',
                ]
                prov['fund_nav_computed_value']  = _nav_prov.get('computed_value')
                prov['fund_nav_extracted_value'] = _nav_prov.get('extracted_value')
                prov['fund_nav_extracted_source'] = _nav_prov.get('extracted_source') or {}
                prov['fund_nav_components']     = _nav_prov.get('components') or {}
                prov['fund_nav_missing_inputs'] = _nav_prov.get('missing_inputs') or []
                _c = prov['fund_nav_components']
                _rs = _c.get('receivables_split') or {}
                _os = _c.get('other_liab_split') or {}
                # Build a professional flat table the sidebar can render
                # verbatim. Each row = one formula input + numeric value +
                # role (positive/negative in the formula) + human origin.
                _rb = _c.get('realised_basis') or 'realised_gains'
                _realised_label = (
                    'Realised Gains on Exits'
                    if _rb == 'realised_gains'
                    else 'Realised Value (Exit Proceeds — cost basis fallback)'
                )
                _realised_origin = (
                    'sum(ExitEvent.realized_gain_loss) — profit portion of exits '
                    '(proceeds − cost). Only the gain counts because the cost basis '
                    'is already recognised via Called Capital (AIF audited-NAV convention).'
                    if _rb == 'realised_gains'
                    else 'sum(ExitEvent.proceeds) — gross cash returned from exits '
                         '(used as a fallback; the workbook did not publish per-exit gain data).'
                )
                prov['fund_nav_inputs_breakdown'] = [
                    {'label': _realised_label,
                     'value_rs_cr': _c.get('realised'),
                     'role': 'Positive (+)',
                     'origin': _realised_origin},
                    {'label': 'Unrealised Value (Residual NAV)',
                     'value_rs_cr': _c.get('unrealised'),
                     'role': 'Positive (+)',
                     'origin': 'sum(latest Valuation.fair_value_of_holding per Investment) — unrealised portfolio at fair value'},
                    {'label': 'Undistributed Cash',
                     'value_rs_cr': _c.get('cash'),
                     'role': 'Positive (+)',
                     'origin': 'Extracted from workbook — Cash & Cash Equivalents cell (fund bank balance)'},
                    {'label': 'Receivables (Interest/Dividend + Other)',
                     'value_rs_cr': _c.get('receivables'),
                     'role': 'Positive (+)',
                     'origin': ('Extracted from workbook — Interest/Dividend Receivable ('
                                f'{_rs.get("interest_dividend")}) + Other Receivables ('
                                f'{_rs.get("other")})')},
                    {'label': 'Management Fee Payable',
                     'value_rs_cr': _c.get('mgmt_fee_payable'),
                     'role': 'Negative (−)',
                     'origin': 'Extracted from workbook — Management Fee Payable (accrued) cell'},
                    {'label': 'Performance Fee / Carry Payable',
                     'value_rs_cr': _c.get('carry_payable'),
                     'role': 'Negative (−)',
                     'origin': 'Extracted from workbook — Performance Fee / Carry Payable (accrued) cell'},
                    {'label': 'Other Liabilities (Fund Exp + Tax + Borrowings)',
                     'value_rs_cr': _c.get('other_liabilities'),
                     'role': 'Negative (−)',
                     'origin': ('Extracted from workbook — Fund Expenses Payable ('
                                f'{_os.get("fund_expenses")}) + Tax/Other ('
                                f'{_os.get("tax")}) + Borrowings ('
                                f'{_os.get("borrowings")})')},
                ]

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
        # For carry_base + carry_amount_gross + carry_amount_net: attach the
        # priority tier used (P1 extract / P2 formula / P3 blank), the ladder
        # text, an INPUTS breakdown table (universal formula), and — when P3
        # — an itemised list of the specific inputs that were missing so the
        # sliding panel can explain why the tile is blank. Mirrors the net_irr
        # provenance pattern exactly.
        if key in _P3_BLANK_KEYS:
            # Route each carry/tvpi metric to its own tier tracker + labels
            # + ladder. Every one of these emits (method_code, method_label,
            # priority_ladder, missing_inputs) into `prov` so the frontend
            # sidebar can render a uniform P-tier badge + missing-inputs table.
            if key == 'carry_amount_net':
                _method_code = agg.get('carry_net_method')
                _method_labels = _CARRY_NET_METHOD_LABELS
                _method_ladder = _CARRY_NET_PRIORITY_LADDER
                _method_key    = 'carry_net_method'
                _label_key     = 'carry_net_method_label'
                _ladder_key    = 'carry_net_priority_ladder'
                _missing_key   = 'carry_net_missing_inputs'
                _missing_reason_key = 'carry_net_inputs'
                _blank_code    = 'p3_insufficient_data'
            elif key == 'tvpi':
                _method_code = agg.get('tvpi_method')
                _method_labels = _TVPI_METHOD_LABELS
                _method_ladder = _TVPI_PRIORITY_LADDER
                _method_key    = 'tvpi_method'
                _label_key     = 'tvpi_method_label'
                _ladder_key    = 'tvpi_priority_ladder'
                _missing_key   = 'tvpi_missing_inputs'
                _missing_reason_key = 'tvpi_inputs'
                _blank_code    = 'p4_insufficient_data'
            else:  # carry_base, carry_amount_gross
                _method_code = agg.get('carry_method')
                _method_labels = _CARRY_METHOD_LABELS
                _method_ladder = _CARRY_PRIORITY_LADDER
                _method_key    = 'carry_method'
                _label_key     = 'carry_method_label'
                _ladder_key    = 'carry_priority_ladder'
                _missing_key   = 'carry_missing_inputs'
                _missing_reason_key = 'carry_inputs'
                _blank_code    = 'p3_insufficient_data'
            if _method_code:
                prov[_method_key]  = _method_code
                prov[_label_key]   = _method_labels.get(_method_code, _method_code)
                prov[_ladder_key]  = _method_ladder
                # Universal formula inputs (real numbers from this scheme).
                # Residual NAV = unrealised portfolio FV = sum of latest
                # per-investment Valuation.fair_value_of_holding. This is the
                # LPA-standard definition of "Residual NAV" — do NOT confuse
                # with fund_nav_latest (accounting NAV), which additionally
                # includes cash + receivables − liabilities and is not the
                # right input to the carry base formula.
                _cp   = _d(_first_present(_wf_carry_pct, agg.get('carry_pct'))) or Decimal('0')
                _real = _d(agg.get('total_realised_proceeds')) or Decimal('0')
                _res  = _d(_first_present(
                    agg.get('total_unrealised_fv_holding'),
                    agg.get('active_fair_value'))) or Decimal('0')
                _origin_res = "sum(latest Valuation.fair_value_of_holding per Investment) — unrealised portfolio FV. LPA-standard 'Residual NAV' = still-held portfolio at fair value; excludes fund-level cash + receivables + fee accruals (those live inside fund_nav_latest, which is a different metric)."
                _cap  = _d(agg.get('total_capital_called')) or Decimal('0')
                _tv   = _real + _res
                _base = _tv - _cap
                if _base < 0:
                    _base = Decimal('0')
                _origin_real = "sum(ExitEvent.proceeds) — cash returned from exited investments"
                _origin_cap  = "sum(CapitalCall.total_call_amount) — contributed capital to date"
                _origin_cpct = "funds_scheme.carry_pct — LPA-declared GP carry rate (typically 20%)"
                if key == 'carry_base':
                    prov['inputs_breakdown'] = [
                        {'label': 'Realised Exit Proceeds',
                         'value_rs_cr': str(_real),
                         'role': 'Total Value component',
                         'origin': _origin_real},
                        {'label': 'Residual NAV (unrealised portfolio FV)',
                         'value_rs_cr': str(_res),
                         'role': 'Total Value component',
                         'origin': _origin_res},
                        {'label': 'Total Capital Called',
                         'value_rs_cr': str(_cap),
                         'role': 'Subtracted (LP capital returned first)',
                         'origin': _origin_cap},
                        {'label': 'Formula',
                         'value_rs_cr': '',
                         'role': 'Universal',
                         'origin': 'Carry Base = max(0, (Residual NAV + Realised Exit Proceeds) − Total Capital Called)'},
                    ]
                elif key == 'carry_amount_gross':
                    prov['inputs_breakdown'] = [
                        {'label': 'GP Carry %',
                         'value_rs_cr': (f'{_cp}%' if _cp else ''),
                         'role': 'Multiplier',
                         'origin': _origin_cpct},
                        {'label': 'Carry Base',
                         'value_rs_cr': str(_base),
                         'role': 'Base (multiplicand)',
                         'origin': 'max(0, (Realised + Residual NAV) − Called Capital)'},
                        {'label': 'Formula',
                         'value_rs_cr': '',
                         'role': 'Universal',
                         'origin': 'Carry = GP Carry % × Carry Base  (European Whole-Fund + 100% GP Catch-Up identity)'},
                    ]
                elif key == 'carry_amount_net':
                    _gross_carry_val = _d(agg.get('carry_amount_gross'))
                    _clawback_pct    = _d(getattr(scheme, 'gp_holdback_pct', None))
                    _net_computed    = None
                    if _gross_carry_val is not None and _clawback_pct is not None:
                        _net_computed = (_gross_carry_val
                                         * (Decimal('1') - _clawback_pct / Decimal('100'))).quantize(Decimal('0.01'))
                    prov['inputs_breakdown'] = [
                        {'label': 'Gross Carry',
                         'value_rs_cr': (str(_gross_carry_val) if _gross_carry_val is not None else ''),
                         'role': 'Base (multiplicand)',
                         'origin': 'derived: carry_amount_gross — see the Gross Carry panel for its own provenance (P1 extract / P2 formula)'},
                        {'label': 'Clawback Reserve %',
                         'value_rs_cr': (f'{_clawback_pct}%' if _clawback_pct is not None else ''),
                         'role': 'Discount (LP escrow-back portion)',
                         'origin': "funds_scheme.gp_holdback_pct — LPA-declared holdback. Sourced from workbook cells labelled 'Clawback Reserve %' / 'Holdback %' / 'Escrow %' on the MASTER_INPUTS / Fund_Overview / Carry_Clawback sheets."},
                        {'label': 'Net Carry (computed)',
                         'value_rs_cr': (str(_net_computed) if _net_computed is not None else ''),
                         'role': 'Result',
                         'origin': 'Gross Carry × (1 − Clawback Reserve %/100)'},
                        {'label': 'Formula',
                         'value_rs_cr': '',
                         'role': 'AIF-standard, universal',
                         'origin': 'Net Carry = Gross Carry × (1 − Clawback Reserve %). Indicative: what net carry the GP would take home after the LPA-mandated clawback reserve is set aside.'},
                    ]
                else:  # tvpi (a.k.a. Net MOIC)
                    _tvpi_dist_val    = _d(agg.get('total_distributions'))
                    _tvpi_res_val     = _d(_first_present(
                        agg.get('total_unrealised_fv_holding'),
                        agg.get('active_fair_value'))) or Decimal('0')
                    _tvpi_fund_nav    = _d(agg.get('fund_nav_latest'))
                    _tvpi_called_val  = _d(agg.get('total_capital_called'))
                    # Show P1 result if it fired, else P2 result if it fired
                    _p1_result = None
                    if (_tvpi_dist_val is not None and _tvpi_dist_val > 0
                            and _tvpi_called_val is not None and _tvpi_called_val > 0):
                        _p1_result = ((_tvpi_dist_val + _tvpi_res_val) / _tvpi_called_val).quantize(Decimal('0.0001'))
                    _p2_result = None
                    if (_tvpi_fund_nav is not None and _tvpi_fund_nav > 0
                            and _tvpi_called_val is not None and _tvpi_called_val > 0):
                        _p2_result = (_tvpi_fund_nav / _tvpi_called_val).quantize(Decimal('0.0001'))
                    prov['inputs_breakdown'] = [
                        {'label': 'Total Distributions',
                         'value_rs_cr': (str(_tvpi_dist_val) if _tvpi_dist_val is not None else '—'),
                         'role': 'P1 numerator component (Formula 1 only)',
                         'origin': 'sum(Distribution.total_net_amount) — cash returned to LPs. If null/zero, P1 formula skips and we fall to P2.'},
                        {'label': 'Residual NAV (unrealised portfolio FV)',
                         'value_rs_cr': str(_tvpi_res_val),
                         'role': 'P1 numerator component',
                         'origin': "sum(latest Valuation.fair_value_of_holding per Investment) — LPA-standard 'Residual NAV' = still-held portfolio at fair value."},
                        {'label': 'Fund NAV (accounting NAV)',
                         'value_rs_cr': (str(_tvpi_fund_nav) if _tvpi_fund_nav is not None else '—'),
                         'role': 'P2 numerator (Formula 2 only)',
                         'origin': "Extracted 'TOTAL FUND NAV' cell from NAV_CALC / Fund_Overview. Used as a proxy for total value when the fund hasn't distributed yet."},
                        {'label': 'Total Called Capital',
                         'value_rs_cr': (str(_tvpi_called_val) if _tvpi_called_val is not None else '—'),
                         'role': 'Denominator (both formulas)',
                         'origin': 'sum(CapitalCall.total_call_amount) — LP paid-in capital. Same denominator in P1 and P2.'},
                        {'label': 'Formula 1 (P1)',
                         'value_rs_cr': (f'{_p1_result}x' if _p1_result is not None else '— not computable'),
                         'role': 'Result if P1 fires',
                         'origin': 'Net MOIC = (Total Distributions + Residual NAV) / Total Called Capital. Applies when the fund has distributed to LPs.'},
                        {'label': 'Formula 2 (P2)',
                         'value_rs_cr': (f'{_p2_result}x' if _p2_result is not None else '— not computable'),
                         'role': 'Result if P2 fires',
                         'origin': "Net MOIC = Fund NAV / Total Called Capital. Fallback for pre-distribution funds (uses accounting NAV as proxy for LP value)."},
                    ]
                # P-blank tier — explicit missing inputs table. Sidebar panel
                # iterates this list and shows one row per input we do NOT
                # have. Users see EXACTLY which cell to add to unblock.
                if _method_code == _blank_code:
                    prov[_missing_key] = (
                        (agg.get('reasons') or {}).get(_missing_reason_key) or []
                    )
                    # Force a specific provenance_kind so the frontend routes
                    # to the "insufficient data" panel layout.
                    prov['provenance_kind'] = 'insufficient_data'
                    _formula_txt_by_key = {
                        'carry_base':          'Carry Base = max(0, (Residual NAV + Realised) − Called)',
                        'carry_amount_gross':  'Gross Carry = carry_pct × Carry Base',
                        'carry_amount_net':    'Net Carry = Gross Carry × (1 − Clawback Reserve %)',
                        'tvpi':                'Net MOIC = (P1) (Distributions + Residual NAV) / Called  OR  (P2) Fund NAV / Called',
                    }
                    prov['formula_expression'] = (
                        _formula_txt_by_key.get(key,
                        'Carry = carry_pct × max(0, (Realised + Residual NAV) − Called)') + '  '
                        '=  ?  (see missing inputs table)'
                    )
        # For moic: attach an INPUTS table so the panel can render each
        # component of the formula with its origin. Universal — the values
        # come from the same agg dict the formula string used.
        if key == 'moic':
            _mfv = _d(agg.get('total_unrealised_fv_holding')
                      or agg.get('total_portfolio_fv')
                      or active_fv) or Decimal('0')
            _mreal = _d(agg.get('total_realised_proceeds')) or Decimal('0')
            _mcost = _d(agg.get('total_invested_capital')) or Decimal('0')
            prov['inputs_breakdown'] = [
                {'label': 'Realised Proceeds',
                 'value_rs_cr': str(_mreal),
                 'role': 'Numerator addend',
                 'origin': "sum(ExitEvent.proceeds where is_actual=True) — exit cashback to the fund"},
                {'label': 'Active Fair Value',
                 'value_rs_cr': str(_mfv),
                 'role': 'Numerator addend',
                 'origin': "sum(latest Valuation.fair_value_of_holding per Investment) — fund's stake in still-held positions"},
                {'label': 'Total Cost',
                 'value_rs_cr': str(_mcost),
                 'role': 'Denominator',
                 'origin': 'sum(Investment.total_invested) — cost basis of every deployment'},
            ]
        # For active_fair_value: attach an EXPLAINER block on top of the
        # per-company breakdown so the panel can render "what this is, how
        # we get it, where the numbers come from" as tabular rows. Universal.
        if key == 'active_fair_value':
            prov['inputs_breakdown'] = [
                {'label': 'What it is',
                 'value_rs_cr': '',
                 'role': 'Definition',
                 'origin': "The fund's stake in every portfolio company it still owns, marked to today's fair value. Excludes cash already returned from exits."},
                {'label': 'How it is derived',
                 'value_rs_cr': '',
                 'role': 'Formula',
                 'origin': 'Σ latest_per_investment(Valuation.fair_value_of_holding) with fair_value fallback for workbooks that only publish one FV column.'},
                {'label': 'Where the numbers come from',
                 'value_rs_cr': '',
                 'role': 'Source',
                 'origin': "Excel VALUATIONS sheet → Gemini row extractor → investments_valuation table → aggregator picks the latest valuation per investment and sums the column."},
            ]
        # For total_fair_value: universal formula
        #   Total Fair Value = Active Fair Value + Realised Proceeds from Exits
        # The renderer's `computed_from_canonical_formula` branch splits the
        # formula_expression on the literal '  =  ' (three spaces either side)
        # into (template, substituted, result) and renders three tidy rows
        # under "How we got it". The tabular breakdown is carried by
        # `contributing_companies` (repurposed as the component list).
        if key == 'total_fair_value':
            _active = _d(agg.get('total_unrealised_fv_holding')
                         or agg.get('total_portfolio_fv')
                         or active_fv) or Decimal('0')
            _realised = _d(agg.get('total_realised_proceeds')) or Decimal('0')
            _total = _active + _realised
            prov['provenance_kind'] = 'computed_from_canonical_formula'
            prov['formula_expression'] = (
                f'Active Fair Value + Realised Proceeds from Exits'
                f'  =  '
                f'Rs.{_active:,.2f} Cr + Rs.{_realised:,.2f} Cr'
                f'  =  '
                f'Rs.{_total:,.2f} Cr'
            )
            prov['contributing_companies'] = [
                {'company': 'Active Fair Value  (fund stake in still-held positions)',
                 'fair_value_of_holding': str(_active)},
                {'company': 'Realised Proceeds from Exits',
                 'fair_value_of_holding': str(_realised)},
            ]
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
    # Fix 1 (2026-07-06) — fund-level metrics (net_irr / tvpi / portfolio_fv)
    # are valid BvA line items (present in LINE_ITEM_CHOICES) but they live
    # in the `fund_metrics` category of the canonical schema, not
    # `pl_line_items`. Merge both so BvA sheets that publish "Net IRR" /
    # "TVPI" / "Portfolio FV" alongside "Portfolio Revenue" / "EBITDA"
    # all resolve. Universal — every fund whose BvA sheet mixes fund and
    # portfolio metrics benefits; per-company BvA sheets are unaffected
    # because their line items match on the pl_line_items branch.
    merged: dict[str, str] = {}
    for cat_key in ('pl_line_items', 'fund_metrics'):
        for canonical_key, description in (
            CANONICAL_VALUE_CATEGORIES.get(cat_key) or {}
        ).items():
            merged.setdefault(canonical_key, description)
    alias_map = {}
    for canonical_key, description in merged.items():
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

    Fund-context period fallback (universal): when the BvA sheet ships as a
    single-period snapshot with NO period column (e.g. Multiples IV format:
    Company / Line Item / Actual / Budget only), rows arrive with period=None.
    Rather than dropping them all, we fall back to the fund's latest-reported
    period — inferred from the newest NAVRecord's FY. This is deterministic
    from DB state (no calendar guess) and applies to every fund whose BvA
    sheet skips the period column. Universal.
    """
    from mis_consolidation.models import BudgetVsActual
    from investments.models import PortfolioCompany
    from accounting.models import NAVRecord
    if not fund:
        return 0

    fallback_period = None
    # Fix 1 (2026-07-06) — trigger fallback when ANY row's period is missing
    # OR unparseable (e.g. Sequoia BvA has period='994' — Gemini misassigned
    # the "Total" numeric column to the period slot). Old rule (`not
    # r.get('period')`) missed those cases and dropped every row for
    # `no_period`. Universal — files whose periods parse cleanly still see
    # zero effect (fallback is never consulted).
    _needs_fallback = any(
        isinstance(r, dict) and _bva_parse_period(r.get('period')) is None
        for r in rows
    )
    if _needs_fallback:
        latest_nav = (NAVRecord.objects.filter(scheme__fund=fund)
                      .order_by('-nav_date').first())
        if latest_nav and latest_nav.nav_date:
            y, m = latest_nav.nav_date.year, latest_nav.nav_date.month
            fy_end = y + 1 if m > 3 else y   # Indian FY ends 31 March
            fallback_period = {'period_year': fy_end, 'period_type': 'annual'}
            logger.info(
                f'[phase2.bva] period fallback: unparseable / missing period — '
                f'using FY {fy_end - 1}-{str(fy_end)[-2:]} (from latest NAV '
                f'{latest_nav.nav_date.isoformat()})'
            )

    count = 0
    skipped_no_co = 0
    skipped_no_line = 0
    skipped_no_period = 0
    # Fix 1 (2026-07-06) — Fund-level BVA rows.
    # Some workbooks (Sequoia MIS "Budget vs Actual" sheet) publish BvA at
    # the fund aggregate level: line_item="Portfolio Revenue" / "EBITDA" /
    # "Portfolio FV" / "Net IRR" / "TVPI" with budget + actual + variance
    # but NO per-company scoping. Attach the fund-aggregate sentinel
    # PortfolioCompany so these rows persist through the existing lookup
    # chain unchanged. Sentinel is created lazily via all_objects (custom
    # manager hides it from every user-facing query).
    #
    # Universal: the same sentinel is reused by the KPI persister for
    # fund-level Monthly P&L rows. Per-company BvA rows keep the existing
    # path and never touch the sentinel.
    from dataimport.phase6_extractor.unified_builder import (
        FUND_AGGREGATE_SENTINEL,
    )
    _sentinel_co = None
    def _get_sentinel_co():
        nonlocal _sentinel_co
        if _sentinel_co is not None:
            return _sentinel_co
        _sentinel_co = PortfolioCompany.all_objects.filter(
            organization=organization, name=FUND_AGGREGATE_SENTINEL,
        ).first()
        if _sentinel_co is None:
            _sentinel_co, _ = PortfolioCompany.all_objects.update_or_create(
                organization=organization,
                name=FUND_AGGREGATE_SENTINEL,
                defaults={
                    'is_active': False,
                    'is_aggregate': True,
                    'sector': '__fund_aggregate__',
                    'description': (
                        'Sentinel row for fund-level aggregate KPIs '
                        '(Monthly P&L, Budget vs Actual). Not a real '
                        'portfolio company — excluded from every '
                        'user-facing query by the default manager.'
                    ),
                },
            )
        return _sentinel_co

    for row in rows:
        if not isinstance(row, dict):
            continue
        co_name = _str(row.get('company_name'), 255)
        line_item_raw = row.get('line_item')
        if not line_item_raw:
            continue
        if not co_name:
            # Fund-level row — route to the sentinel PortfolioCompany.
            co = _get_sentinel_co()
        else:
            co = PortfolioCompany.objects.filter(
                organization=organization, name=co_name,
            ).first()
        if not co:
            skipped_no_co += 1
            continue
        line_item_key = _bva_line_item_key(line_item_raw)
        if not line_item_key:
            skipped_no_line += 1
            continue
        period_dict = _bva_parse_period(row.get('period')) or fallback_period
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

    Fund-context period fallback (universal): SaaS Metrics / KPI-snapshot
    sheets often ship without a period column — every row carries current-
    period values. To keep those rows, we fall back to the fund's latest
    NAVRecord date. Deterministic from DB state, not calendar-based, so it
    applies uniformly across every fund that ships period-less KPI sheets.
    """
    from investments.models import PortfolioCompany, Investment, PortfolioKPI
    from accounting.models import NAVRecord

    # Lazy import — KPIDefinition might be in a separate module
    try:
        from investments.models import KPIDefinition
    except ImportError:
        logger.warning('Phase 2: KPIDefinition not importable — skipping periodic KPI persistence')
        return 0

    fallback_period_date = None
    if scheme:
        latest_nav = (NAVRecord.objects.filter(scheme=scheme)
                      .order_by('-nav_date').first())
        if latest_nav and latest_nav.nav_date:
            fallback_period_date = latest_nav.nav_date
        # Universal second-tier fallback: some funds ship KPI sheets without
        # any period column AND without a NAV walk (or with a NAV walk that
        # extracted zero rows because of a multi-section layout mismatch).
        # Use scheme.final_close_date or first_close_date as a deterministic,
        # fund-scoped date so KPI rows still persist. Otherwise the SaaS/KPI
        # dashboard stays blank for the entire fund.
        if fallback_period_date is None:
            fallback_period_date = (getattr(scheme, 'final_close_date', None)
                                    or getattr(scheme, 'first_close_date', None))

    # Fields to project from each row
    kpi_fields = [
        'revenue', 'cogs', 'gross_profit', 'gross_margin_pct',
        'ebitda', 'ebitda_margin_pct', 'pat', 'headcount',
        'gmv', 'orders', 'aov', 'returns_pct', 'repeat_pct',
        'mrr', 'arr', 'nrr', 'churn_rate', 'cac', 'ltv', 'ltv_cac_ratio',
        'customers', 'new_customers',
        'burn_rate', 'gross_burn', 'net_burn', 'cash_balance',
        'runway_months', 'nim_pct', 'gnpa_pct', 'nnpa_pct',
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
        # Universal period source: `period` is the canonical field, but sheet
        # authors often publish just `financial_year` / `fy` / `year` when a
        # KPI is annual, or `period_end` / `period_start` for BS-style rows.
        period_label = _str(
            row.get('period')
            or row.get('financial_year')
            or row.get('fy')
            or row.get('year')
            or row.get('period_end')
            or row.get('period_start'),
            32,
        )
        if not co_name:
            continue
        # PortfolioKPI.period is a DateField; convert label → date universally.
        # Universal fund-context fallback: rows without any period label (e.g.
        # SaaS Metrics single-snapshot sheets) inherit the fund's latest-NAV
        # date. Skipping them means the SaaS tab stays blank; using today()
        # would be non-deterministic. Latest NAV is deterministic + fund-scoped.
        period_date = None
        if period_label:
            period_date = _period_to_date(period_label)
        if period_date is None:
            period_date = fallback_period_date
        if period_date is None:
            continue
        # Fix C — Fund-level P&L sentinel handling.
        # unified_builder pivots fund-level Monthly P&L rows into KPI shape
        # tagged with a sentinel company_name. Auto-create the sentinel
        # PortfolioCompany + Investment shell (is_aggregate=True hides them
        # from every user-facing query via the custom manager) so the KPI
        # derivation ladder below runs unchanged and every fund-level P&L
        # row lands in PortfolioKPI. Universal — fires only for the
        # sentinel name; every other row goes through the regular path.
        from dataimport.phase6_extractor.unified_builder import (
            FUND_AGGREGATE_SENTINEL,
        )
        if co_name == FUND_AGGREGATE_SENTINEL:
            co = PortfolioCompany.all_objects.filter(
                organization=organization, name=co_name,
            ).first()
            if co is None:
                co, _ = PortfolioCompany.all_objects.update_or_create(
                    organization=organization,
                    name=co_name,
                    defaults={
                        'is_active': False,
                        'is_aggregate': True,
                        'sector': '__fund_aggregate__',
                        'description': (
                            'Sentinel row for fund-level aggregate KPIs '
                            '(Monthly P&L, Budget vs Actual). Not a real '
                            'portfolio company — excluded from every '
                            'user-facing query by the default manager.'
                        ),
                    },
                )
        else:
            co = PortfolioCompany.objects.filter(
                organization=organization, name=co_name,
            ).first()
        if not co:
            continue
        # For the sentinel case, use `all_objects` so the newly-added
        # Investment default manager (which hides is_aggregate=True rows)
        # doesn't hide our own shell from ourselves on re-imports.
        _inv_qs = Investment.all_objects if co.is_aggregate else Investment.objects
        inv = _inv_qs.filter(scheme=scheme, portfolio_company=co).first()
        if inv is None:
            if co.is_aggregate:
                # Sentinel shell Investment — anchors PortfolioKPI FK without
                # touching real Investment analytics. Zero commit / zero cost;
                # investment_date pinned to fund's earliest anchor so ordering
                # (tranche → exit) never treats it as a real deployment.
                inv_date_hint = (
                    getattr(scheme, 'first_close_date', None)
                    or getattr(scheme, 'final_close_date', None)
                )
                if inv_date_hint is None and fallback_period_date is not None:
                    inv_date_hint = fallback_period_date
                inv, _ = Investment.all_objects.update_or_create(
                    scheme=scheme,
                    portfolio_company=co,
                    defaults={
                        'company_name': co_name,
                        'total_invested': Decimal('0'),
                        'investment_date': inv_date_hint,
                        'sector': '__fund_aggregate__',
                        'status': 'active',
                    },
                )
            else:
                # PortfolioKPI.investment is a required FK. Skip rows where no
                # Investment exists for this (scheme, company) — happens when
                # the KPI sheet covers a company whose investment row was not
                # extracted (rare) or doesn't exist in source. Universal.
                continue

        # ── Universal kpi_name/kpi_value discriminator routing ──────────
        # Some workbooks compress multiple SaaS metrics into ONE value
        # column plus a `Metric_Type` discriminator column (e.g.
        # "ARR / GMV / AUM (INR Cr)" + "Metric_Type (ARR/GMV/AUM)"). The
        # extractor emits {kpi_name: 'ARR', kpi_value: 35} — routing this
        # into the specific canonical field lets the SaaS Metrics dashboard
        # populate. Only fires when a dedicated column wasn't already set.
        # Universal across any workbook that ships this compressed pattern.
        _kpi_name_raw = row.get('kpi_name')
        _kpi_value = row.get('kpi_value')
        if _kpi_name_raw and _kpi_value is not None:
            _kpi_slug = str(_kpi_name_raw).strip().lower().replace(' ', '_').replace('-', '_')
            _KPI_DISCRIMINATOR_MAP = {
                'arr': 'arr', 'mrr': 'mrr', 'nrr': 'nrr',
                'gmv': 'gmv', 'aum': 'aum_value',
                'churn': 'churn_rate', 'churn_rate': 'churn_rate',
                'cac': 'cac', 'ltv': 'ltv', 'ltv_cac': 'ltv_cac_ratio',
                'burn_rate': 'burn_rate', 'gross_burn': 'gross_burn',
                'net_burn': 'net_burn', 'runway': 'runway_months',
            }
            _target = _KPI_DISCRIMINATOR_MAP.get(_kpi_slug)
            if _target:
                row.setdefault(_target, _kpi_value)

        # ── Universal per-row derivations ──────────────────────────────
        # Fund workbooks publish RAW P&L line items (Revenue, COGS, R&D,
        # S&M, G&A, D&A) plus SaaS metrics (MRR, ARR, Churn, Customers,
        # New Customers). The dashboard shows DERIVED ratios (Gross Margin
        # %, EBITDA %, CAC, LTV, LTV/CAC) that don't appear as columns.
        # Compute them here so the KPI matrix has values wherever the
        # inputs exist. Universal across every fund's P&L sheet — only
        # sets a field when it is currently missing.
        # Universal input aliases — every fund's P&L sheet uses slightly
        # different column names for the same concept. _first_present
        # preserves a literal 0 (unlike Python's `or`).
        _rev  = _d(_first_present(
            row.get('revenue'), row.get('total_revenue'),
            row.get('net_sales'), row.get('turnover'), row.get('top_line')))
        _cogs = _d(_first_present(
            row.get('cogs'), row.get('cost_of_revenue'),
            row.get('cost_of_sales'), row.get('direct_cost')))
        _rd   = _d(_first_present(
            row.get('rd_cost'), row.get('rd_expense'),
            row.get('research_and_development'))) or Decimal('0')
        _mktg = _d(_first_present(
            row.get('marketing_cost'), row.get('sales_and_marketing'),
            row.get('sales_marketing'), row.get('sm_cost'))) or Decimal('0')
        _ga   = _d(_first_present(
            row.get('g_and_a'), row.get('ga_expense'),
            row.get('general_and_admin'), row.get('sga'))) or Decimal('0')
        _dep  = _d(_first_present(
            row.get('depreciation'), row.get('depreciation_and_amortisation'),
            row.get('d_and_a'))) or Decimal('0')
        _nc   = _d(_first_present(
            row.get('new_customers'), row.get('newly_acquired_customers'),
            row.get('new_signups'), row.get('new_subscribers'),
            row.get('new_users'), row.get('newly_added_customers')))
        _cust = _d(_first_present(
            row.get('customers'), row.get('total_customers'),
            row.get('active_customers'), row.get('subscribers'),
            row.get('users_count'), row.get('paying_customers'),
            row.get('clients'), row.get('end_users')))
        _arr  = _d(row.get('arr'))
        _churn = _d(row.get('churn_rate'))

        if _rev and _rev > 0 and _cogs is not None:
            gp = _rev - _cogs
            row.setdefault('gross_profit', gp)
            row.setdefault('gross_margin_pct', (gp / _rev) * Decimal('100'))
            # EBITDA = Revenue - COGS - OpEx (R&D + S&M + G&A). Excludes D&A.
            computed_ebitda = _rev - _cogs - _rd - _mktg - _ga
            row.setdefault('ebitda', computed_ebitda)
            # EBITDA margin — use the row's EBITDA (explicit from the sheet
            # if present, else the computed value). Universal correctness:
            # fund-level P&L sheets often publish EBITDA directly without an
            # OpEx breakdown, in which case computed_ebitda collapses to
            # gross_profit (rev-cogs-0-0-0) and would report the wrong
            # margin. `setdefault` above preserved the explicit value; read
            # it back here so the margin matches whatever ebitda we actually
            # persist.
            _row_ebitda = _d(row.get('ebitda'))
            if _row_ebitda is not None:
                row.setdefault('ebitda_margin_pct',
                               (_row_ebitda / _rev) * Decimal('100'))

        # CAC = S&M spend / new customers acquired. Convert Cr → INR
        # (₹1 Cr = ₹1,00,00,000) so the tile shows a real per-customer cost.
        if _mktg and _mktg > 0 and _nc and _nc > 0:
            row.setdefault('cac',
                (_mktg * Decimal('10000000')) / _nc)

        # LTV ≈ ARPU × Gross Margin / Churn (SaaS textbook formula).
        # ARPU = ARR / customers, all in Cr; convert final to INR.
        if (_arr and _arr > 0 and _cust and _cust > 0
                and _churn and _churn > 0
                and _rev and _rev > 0 and _cogs is not None):
            arpu_cr = _arr / _cust
            gm = (_rev - _cogs) / _rev
            row.setdefault('ltv',
                (arpu_cr * gm / _churn) * Decimal('10000000'))

        # LTV/CAC — universally computed once both are in the row.
        _ltv = _d(row.get('ltv'))
        _cac = _d(row.get('cac'))
        if _ltv and _cac and _cac > 0:
            row.setdefault('ltv_cac_ratio', _ltv / _cac)

        # ── Fix E — Universal SaaS + Commerce KPI back-fills ────────────
        # Every fund's KPI / SaaS Metrics sheet publishes a slightly
        # different subset. The dashboard renders the same tile grid for
        # every fund, so blanks look like broken extraction even when the
        # underlying value is trivially derivable from what IS present.
        # Each block below fires only when its inputs exist AND the target
        # field is currently missing (`setdefault`), so a value published
        # directly by the sheet always wins over a derived one.
        # Universal — no fund-specific mapping.

        # MRR ↔ ARR bidirectional (SaaS textbook: ARR = MRR × 12).
        _mrr = _d(row.get('mrr'))
        _arr_now = _d(row.get('arr'))
        if _mrr and _arr_now is None:
            row.setdefault('arr', _mrr * Decimal('12'))
        elif _arr_now and _mrr is None:
            row.setdefault('mrr', _arr_now / Decimal('12'))

        # AOV / GMV / Orders triangle — any two derives the third.
        _gmv    = _d(row.get('gmv'))
        _orders = _d(row.get('orders'))
        _aov    = _d(row.get('aov'))
        if _gmv and _orders and _orders > 0 and _aov is None:
            # AOV: rupees per order. GMV usually in Cr; convert to ₹.
            row.setdefault('aov', (_gmv * Decimal('10000000')) / _orders)
        elif _gmv and _aov and _aov > 0 and _orders is None:
            row.setdefault('orders', (_gmv * Decimal('10000000')) / _aov)
        elif _orders and _aov and _orders > 0 and _aov > 0 and _gmv is None:
            # Reverse: infer GMV in Cr from orders × AOV(₹).
            row.setdefault('gmv', (_orders * _aov) / Decimal('10000000'))

        # Churn from customer flow when explicit churn rate absent.
        _churn_cust = _d(_first_present(
            row.get('churned_customers'), row.get('lost_customers'),
            row.get('churn_count')))
        if (_churn_cust is not None and _cust and _cust > 0
                and row.get('churn_rate') in (None, '')):
            row.setdefault('churn_rate', (_churn_cust / _cust) * Decimal('100'))

        # Revenue back-fill from GMV when the sheet only publishes GMV
        # (marketplaces / commerce funds). Uses take-rate if published;
        # otherwise skipped (we never fabricate a rate).
        _take_rate_pct = _d(_first_present(
            row.get('take_rate_pct'), row.get('take_rate'),
            row.get('commission_pct')))
        if (row.get('revenue') in (None, '')
                and _gmv and _gmv > 0
                and _take_rate_pct and _take_rate_pct > 0):
            row.setdefault('revenue', _gmv * _take_rate_pct / Decimal('100'))

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
