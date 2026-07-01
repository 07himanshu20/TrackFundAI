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

                new_called = line_called if line_called > 0 else None
                new_dist   = line_dist   if line_dist   > 0 else None

                # Pro-rate fallback when LineItems missing AND we have a
                # commitment share to apply.
                if (new_called is None or new_dist is None) and \
                   c.commitment_amount and scheme_total_committed > 0:
                    share = c.commitment_amount / scheme_total_committed
                    if new_called is None and scheme_total_called > 0:
                        new_called = (scheme_total_called * share).quantize(Decimal('0.01'))
                    if new_dist is None and scheme_total_dist > 0:
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

        _p(96, 'Phase 2: Carry + fund metrics…')
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
    _set_if(defaults, 'gp_holdback_pct', _d(_first_present(
        fm.get('gp_holdback_pct'),
        fm.get('escrow_holdback_pct'),
        fm.get('clawback_holdback_pct'),
        fm.get('holdback_pct'),
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
        # Universal scale-normalisation: Excel stores percentages as
        # decimal fractions (0.3329 = 33.29%). Gemini + Python read the raw
        # fraction. All downstream code (Phase 4 XIRR, dashboard display,
        # weighted-avg aggregation) treats irr_pct as PERCENT scale (e.g.,
        # 33.29). Detect fraction-shape values (|v| < 5) and rescale.
        # A real fund IRR is essentially never in the [-5%, +5%] range and
        # then formatted as 5 not 0.05 — the choice is deterministic.
        if gemini_irr is not None and abs(gemini_irr) < Decimal('5'):
            gemini_irr = gemini_irr * Decimal('100')
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
    # FV precedence: prefer `fair_value` (what the Cover/Summary sheet
    # reports and what Excel-generating fund managers show as "Total FV"),
    # fall back to `fair_value_of_holding`. For workbooks that populate only
    # one column, the persister mirrors it into both — so both funds
    # (equity-reporting like Multiples and holding-only like AI_Trivesta)
    # yield the same authoritative number here.
    latest_per_inv = Valuation.objects.filter(
        investment=OuterRef('pk'),
    ).exclude(
        methodology='derived_from_cost_x_scheme_markup',
    ).order_by('-valuation_date').annotate(
        holding_or_equity=Coalesce('fair_value', 'fair_value_of_holding'),
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
        # Performance ratios — show ATOMIC inputs (source-only, matches the
        # actual computation the dashboard tile reads). Previously these used
        # the extracted (often inflated) fund_nav, which made the audit drawer
        # show different inputs than what the tile value was computed from.
        if metric_key == 'tvpi':
            return ('(Total Distributions + Atomic FV) / Called Capital',
                    f'(₹{T(_atomic_dist_total)} Cr + ₹{T(_atomic_fv)} Cr) / ₹{T(_atomic_call_total)} Cr')
        if metric_key == 'dpi':
            return ('Total Distributions / Called Capital',
                    f'₹{T(_atomic_dist_total)} Cr / ₹{T(_atomic_call_total)} Cr')
        if metric_key == 'rvpi':
            return ('Atomic FV (sum of source Valuations) / Called Capital',
                    f'₹{T(_atomic_fv)} Cr / ₹{T(_atomic_call_total)} Cr')
        if metric_key == 'moic':
            return ('(Total Distributions + Atomic FV) / Total Invested Capital',
                    f'(₹{T(_atomic_dist_total)} Cr + ₹{T(_atomic_fv)} Cr) / ₹{T(_atomic_invested)} Cr')
        if metric_key == 'net_irr':
            # Method-aware formula display. Net IRR is computed via a 3-tier
            # priority ladder in phase4; the description shown here reflects
            # which tier actually produced the current value + a small note
            # of the fallback order so the user understands the choice.
            _method = agg.get('net_irr_method')
            _ladder_note = (
                'Priority order: 1) Fund-level XIRR on CapitalCall + Distribution '
                '(ILPA Net IRR)  →  2) Cost-weighted average of per-investment IRR '
                '(Gross)  →  3) Fund-level XIRR on Investment cashflows → LP terminal '
                '(Gross approx).'
            )
            if _method == 'capitalcall_distribution_xirr':
                return (
                    'Priority 1 (chosen): Fund-level XIRR on CapitalCall + Distribution. ' + _ladder_note,
                    f'XIRR over {{ {_atomic_call_count} calls totalling −₹{T(_atomic_call_total)} Cr, '
                    f'{_atomic_dist_count} distributions totalling +₹{T(_atomic_dist_total)} Cr, '
                    f'terminal Atomic FV +₹{T(_atomic_fv)} Cr at as_of_date }}'
                )
            if _method == 'cost_weighted_per_investment_irr':
                _cw_num = agg.get('total_invested_capital') or Decimal('0')
                return (
                    'Priority 2 (chosen): Cost-weighted average of per-investment IRR (Gross). ' + _ladder_note,
                    f'SUM(Investment.total_invested × Investment.irr_pct) / SUM(Investment.total_invested), '
                    f'across {_cw_num} Cr of workbook-reported IRRs'
                )
            if _method == 'investment_cashflow_xirr':
                return (
                    'Priority 3 (chosen): Fund-level XIRR on Investment cashflows → LP terminal (Gross approx). ' + _ladder_note,
                    f'XIRR over {{ Investment cost outflows totalling '
                    f'−₹{T(agg.get("total_invested_capital"))} Cr, '
                    f'{_atomic_dist_count} distributions totalling +₹{T(_atomic_dist_total)} Cr, '
                    f'terminal Atomic FV +₹{T(_atomic_fv)} Cr at as_of_date }}'
                )
            # No tier produced a value — surface why in the description
            return (
                'Net IRR unavailable: no priority tier produced a plausible value. ' + _ladder_note,
                'No valid cashflow series could be constructed from CapitalCall, '
                'per-investment IRR, or Investment cost + terminal FV.'
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
    # We surface that 0 explicitly when (and only when) the fund is in ROC
    # phase so the cards render "₹0 Cr" rather than blank.
    _fund_in_roc_phase = (lp_distributions_value or Decimal('0')) == Decimal('0')
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
        # produced the value. Values: 'capitalcall_distribution_xirr',
        # 'cost_weighted_per_investment_irr', 'investment_cashflow_xirr'.
        'net_irr_method':           agg.get('net_irr_method'),
        # Totals — aggregator-derived
        'committed_capital':        agg.get('total_committed_capital'),
        'called_capital':           agg.get('total_capital_called'),
        'uncalled_capital':         agg.get('total_uncalled_capital'),
        'invested_cost':            agg.get('total_invested_capital'),
        'realized_proceeds':        agg.get('total_realised_proceeds'),
        'lp_distributions':         agg.get('total_distributions'),
        # Universal FV tile: prefer real-source aggregate → then any
        # DB Valuation FV sum (includes synthetic) → then the fund's NAV FMV.
        # This guarantees the dashboard tile shows a number for every fund,
        # even those whose workbook has no dedicated Valuations sheet — the
        # NAV walk's Unrealised FMV column is authoritative in that case.
        # Prefer the portfolio-equity FV (matches Cover Total Fair Value on
        # workbooks with FV Holding vs Equity Val distinction). Falls back
        # to LP-holding FV, then live-DB FV sum, then extracted NAV. Universal.
        'active_fair_value':        (agg.get('total_portfolio_fv')
                                     or agg.get('total_unrealised_fv_holding')
                                     or active_fv
                                     or agg.get('fund_nav_latest')),
        'fund_nav':                 agg.get('fund_nav_latest'),
        # Waterfall — aggregator-derived (extracted-first, else Python)
        # Waterfall metrics with Phase-3 wf-block fallback (Gemini-extracted
        # values take precedence over formula-computed 0 when atomic ledger
        # lacks per-event GP carry data). Added 2026-06-30.
        'carry_amount_gross':       _zero_if_roc(_first_present(agg.get('carry_amount_gross'),  (wf or {}).get('carry_amount_gross'))),
        'carry_amount_net':         _zero_if_roc(_first_present(agg.get('carry_amount_net'),    (wf or {}).get('net_carry'), (wf or {}).get('carry_amount_net'))),
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
        'capitalcall_distribution_xirr': 'Priority 1: Fund-level XIRR on CapitalCall + Distribution',
        'cost_weighted_per_investment_irr': 'Priority 2: Cost-weighted average of per-investment IRR',
        'investment_cashflow_xirr': 'Priority 3: Fund-level XIRR on Investment cashflows → LP terminal',
    }
    for key, raw_value in metric_map.items():
        # net_irr_method is metadata (routes through the metric_map so the
        # frontend can read it via FundMetric.inputs_used), not a metric.
        # Skip its own FundMetric row.
        if key == 'net_irr_method':
            continue
        val = _d(raw_value)
        if val is None:
            continue
        prov = _provenance_for(key, raw_value)
        # For net_irr: attach the priority-tier method used, and a human label
        # so the frontend can print "Method used: Priority X — <label>".
        if key == 'net_irr':
            _method_code = agg.get('net_irr_method')
            if _method_code:
                prov['net_irr_method'] = _method_code
                prov['net_irr_method_label'] = _NET_IRR_METHOD_LABELS.get(
                    _method_code, _method_code
                )
                prov['net_irr_priority_ladder'] = [
                    'Priority 1: Fund-level XIRR on CapitalCall + Distribution (ILPA Net IRR)',
                    'Priority 2: Cost-weighted average of per-investment IRR (Gross)',
                    'Priority 3: Fund-level XIRR on Investment cashflows → LP terminal (Gross approx)',
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
    if any(isinstance(r, dict) and not r.get('period') for r in rows):
        latest_nav = (NAVRecord.objects.filter(scheme__fund=fund)
                      .order_by('-nav_date').first())
        if latest_nav and latest_nav.nav_date:
            y, m = latest_nav.nav_date.year, latest_nav.nav_date.month
            fy_end = y + 1 if m > 3 else y   # Indian FY ends 31 March
            fallback_period = {'period_year': fy_end, 'period_type': 'annual'}
            logger.info(
                f'[phase2.bva] period fallback: no period column in sheet — '
                f'using FY {fy_end - 1}-{str(fy_end)[-2:]} (from latest NAV '
                f'{latest_nav.nav_date.isoformat()})'
            )

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
        co = PortfolioCompany.objects.filter(organization=organization, name=co_name).first()
        if not co:
            continue
        inv = Investment.objects.filter(scheme=scheme, portfolio_company=co).first()
        if inv is None:
            # PortfolioKPI.investment is a required FK. Skip rows where no
            # Investment exists for this (scheme, company) — happens when the
            # KPI sheet covers a company whose investment row was not
            # extracted (rare) or doesn't exist in source. Universal.
            continue

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
            ebitda = _rev - _cogs - _rd - _mktg - _ga
            row.setdefault('ebitda', ebitda)
            row.setdefault('ebitda_margin_pct', (ebitda / _rev) * Decimal('100'))

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
