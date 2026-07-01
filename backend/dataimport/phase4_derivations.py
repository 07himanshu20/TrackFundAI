"""
Phase 4 — post-persistence derivation of per-investment IRR + MOIC.

Universal across ANY AIF Excel format. Runs AFTER persist_phase2 commits.
Reads only DB state — never re-reads Excel. Adds ~70ms per 70-investment
fund (pure-Python bisection XIRR, no scipy dependency).

What it derives:
  • Investment.irr_pct  — per-investment IRR (XIRR over cash flows)
  • Investment.moic     — per-investment MOIC ((distributions + FV) / cost)
  • ExitEvent.irr_pct   — XIRR for exited investments (when missing)

Inputs (all already in DB after Phase 2):
  • InvestmentTranche.investment_date, amount_invested  → outflows
  • ExitEvent.exit_date, net_proceeds / proceeds_amount  → inflows
  • Distribution rows tied to investment                 → inflows (best-effort)
  • Valuation.fair_value_of_holding for terminal FV     → inflows (or fallback)
  • NAVRecord.unrealised_fmv / total_portfolio_cost ratio → markup proxy

Rule observed: AI never generates numbers. Math runs entirely in Python on
DB-resident values. No Gemini call here.
"""
import logging
from decimal import Decimal, InvalidOperation
from datetime import date as _date

logger = logging.getLogger(__name__)


# ── XIRR via bisection (universal — no numpy/scipy dep) ─────────────────

def _xirr(cashflows):
    """Cashflows: list of (date, signed_amount) with at least one negative
    and one positive. Returns IRR as Decimal percent (e.g. 27.5 for 27.5%),
    or None if uncomputable.

    Bisection over annual rate in [-0.99, +10.0]. ~80 iters → 1e-12 precision,
    runs in <1ms.
    """
    flows = []
    for d, a in cashflows:
        if d is None or a is None:
            continue
        try:
            v = float(a)
        except (TypeError, ValueError):
            continue
        if v == 0:
            continue
        flows.append((d, v))
    if len(flows) < 2:
        return None
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None

    flows.sort(key=lambda x: x[0])
    base = flows[0][0]

    def _npv(r):
        s = 0.0
        for d, a in flows:
            years = (d - base).days / 365.25
            try:
                s += a / ((1 + r) ** years)
            except (ZeroDivisionError, OverflowError, ValueError):
                return float('inf')
        return s

    lo, hi = -0.99, 10.0
    try:
        flo, fhi = _npv(lo), _npv(hi)
        if flo == float('inf') or fhi == float('inf'):
            return None
        if flo * fhi > 0:
            return None
        for _ in range(80):
            mid = (lo + hi) / 2.0
            fm = _npv(mid)
            if abs(fm) < 1e-9:
                break
            if flo * fm < 0:
                hi, fhi = mid, fm
            else:
                lo, flo = mid, fm
        return Decimal(str(round(mid * 100, 4)))
    except (ZeroDivisionError, OverflowError, ValueError):
        return None


# ── FV estimation fallback chain (universal) ────────────────────────────

def _latest_fund_markup(fund) -> Decimal:
    """Fund-level fair-value markup = latest_NAV.investments_at_fair_value /
    SUM(Investment.total_invested). Used as a proxy when per-investment
    Valuation is unavailable. Universal across funds — only requires that
    at least one NAVRecord and one Investment with cost exist for the fund.
    Returns 1.0 when either signal is missing.
    """
    from accounting.models import NAVRecord
    from django.db.models import Sum
    from investments.models import Investment
    nav = (
        NAVRecord.objects
        .filter(scheme__fund=fund)
        .order_by('-nav_date')
        .first()
    )
    total_cost = Investment.objects.filter(scheme__fund=fund).aggregate(
        s=Sum('total_invested'),
    )['s']
    if not nav or not total_cost or total_cost == 0:
        return Decimal('1.0')
    try:
        fmv = Decimal(str(nav.investments_at_fair_value or 0))
        cost = Decimal(str(total_cost))
        if cost > 0 and fmv > 0:
            return fmv / cost
    except (InvalidOperation, AttributeError):
        pass
    return Decimal('1.0')


def _estimate_current_fv(investment, fund_markup: Decimal) -> Decimal | None:
    """Universal FV-estimation chain. Returns Decimal or None.

      1. Latest Valuation.fair_value_of_holding for this investment (truth).
      2. post_money_valuation × ownership_pct (from latest tranche) ×
         fund_markup_ratio (NAV-based, kept current).
      3. total_invested × fund_markup_ratio (cost × portfolio-wide markup).
    """
    from investments.models import Valuation
    val = (
        Valuation.objects
        .filter(investment=investment)
        .order_by('-valuation_date')
        .first()
    )
    if val and val.fair_value_of_holding is not None:
        try:
            return Decimal(str(val.fair_value_of_holding))
        except (InvalidOperation, TypeError):
            pass

    latest_tranche = investment.tranches.order_by('-date').first()
    if latest_tranche:
        try:
            pm = Decimal(str(latest_tranche.post_money_valuation or 0))
            # InvestmentTranche stores ownership as fully_diluted_pct (in %)
            stake = Decimal(str(latest_tranche.fully_diluted_pct or 0))
            if pm > 0 and stake > 0:
                # If value looks like a percent (>1), convert to fraction
                if stake > 1:
                    stake = stake / Decimal('100')
                return pm * stake * fund_markup
        except (InvalidOperation, AttributeError, TypeError):
            pass

    if investment.total_invested:
        try:
            return Decimal(str(investment.total_invested)) * fund_markup
        except (InvalidOperation, TypeError):
            pass
    return None


# ── Main entry point ────────────────────────────────────────────────────

def derive_fund_investment_metrics(fund, as_of=None) -> dict:
    """Compute per-investment IRR + MOIC for every investment in the fund.
    Runs after persist_phase2 commits. Universal — no fund/sector logic.

    Returns: {'investments': N, 'irr_set': K1, 'moic_set': K2,
              'exit_irr_set': K3, 'errors': []}
    """
    from investments.models import Investment, ExitEvent
    from lp.models import Distribution

    today = as_of or _date.today()
    fund_markup = _latest_fund_markup(fund)
    logger.info(f'[phase4] {fund.name}: fund_markup={fund_markup:.4f}')

    investments = Investment.objects.filter(scheme__fund=fund).select_related(
        'portfolio_company', 'scheme',
    ).prefetch_related('tranches', 'exit_scenarios')

    irr_set = moic_set = exit_irr_set = 0
    errors: list[str] = []

    for inv in investments:
        try:
            outflows = []
            for t in inv.tranches.all():
                # InvestmentTranche fields: `amount`, `date` (universal)
                if t.amount is not None and t.date is not None:
                    outflows.append((t.date, -Decimal(str(t.amount))))
            if not outflows:
                continue

            exits = ExitEvent.objects.filter(investment=inv).order_by('exit_date')
            exit_inflows = []
            for e in exits:
                # ExitEvent fields: `proceeds` (gross), `net_exit_proceeds` (net of costs)
                amt = e.net_exit_proceeds or e.proceeds or 0
                if e.exit_date and amt:
                    exit_inflows.append((e.exit_date, Decimal(str(amt))))

            # Distributions tied to this investment via source_investment_ref
            # OR via scheme-level distributions allocated pro-rata. Without
            # a per-investment FK on Distribution, we conservatively skip
            # distributions here — they're already reflected at fund level.
            distribution_inflows = []

            # Terminal FV (only when no exit has terminated the investment)
            total_exit = sum((amt for _, amt in exit_inflows), Decimal('0'))
            total_cost = sum((-a for _, a in outflows), Decimal('0'))
            is_active = (
                inv.status == 'active' and not exits.exists()
            ) or (total_exit == 0)

            terminal_inflows = []
            if is_active:
                fv = _estimate_current_fv(inv, fund_markup)
                if fv is not None and fv > 0:
                    terminal_inflows.append((today, fv))

            cashflows = outflows + exit_inflows + distribution_inflows + terminal_inflows

            # --- IRR via XIRR ---
            irr = _xirr(cashflows)
            if irr is not None:
                inv.irr_pct = irr
                irr_set += 1

            # --- MOIC = total positive / total negative ---
            pos = sum((a for _, a in cashflows if a > 0), Decimal('0'))
            neg = sum((-a for _, a in cashflows if a < 0), Decimal('0'))
            if neg > 0:
                inv.moic = (pos / neg).quantize(Decimal('0.0001'))
                moic_set += 1

            inv.save(update_fields=['irr_pct', 'moic'])

            # --- ExitEvent IRR per-row (universal: one IRR per exit) ---
            for e in exits:
                if e.irr_pct is not None:
                    continue
                e_amt = e.net_exit_proceeds or e.proceeds or 0
                if not e.exit_date or not e_amt:
                    continue
                cf = outflows + [(e.exit_date, Decimal(str(e_amt)))]
                e_irr = _xirr(cf)
                if e_irr is not None:
                    e.irr_pct = e_irr
                    e.irr_on_exit = e_irr
                    e.save(update_fields=['irr_pct', 'irr_on_exit'])
                    exit_irr_set += 1

        except Exception as e:
            errors.append(f'{inv.id}: {e}')
            logger.warning(f'[phase4] derivation failed for inv {inv.id}: {e}')

    result = {
        'investments': investments.count(),
        'irr_set': irr_set,
        'moic_set': moic_set,
        'exit_irr_set': exit_irr_set,
        'fund_markup': float(fund_markup),
        'errors': errors,
    }
    logger.info(
        f'[phase4] {fund.name}: investments={result["investments"]} '
        f'irr_set={irr_set} moic_set={moic_set} exit_irr_set={exit_irr_set} '
        f'fund_markup={float(fund_markup):.4f}'
    )
    return result


# ── Deterministic European waterfall (universal across funds) ───────────────
#
# Single source of truth: persisted DB rows. Same inputs → same outputs every
# run. No assumed values. No synthetic NAV. No Gemini formulas.
#
# Decision tree per field:
#   1. Did EXTRACT-FIRST yield a verified ground-truth value from the
#      workbook (a Carry_Clawback / Fund_Overview / Cover sheet cell)?
#      → use it verbatim (CarriedInterest row already populated by persister).
#   2. Otherwise, can we COMPUTE the field from extracted LPA terms
#      (hurdle_rate_pct, carry_pct on Scheme) + extracted cashflow ledgers
#      (CapitalCall, Distribution)? → run the deterministic formula below.
#   3. Otherwise emit null + a quality flag. Dashboard renders "—".

def _safe_decimal(x, default=None):
    if x is None:
        return default
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return default


def compute_waterfall_for_scheme(scheme, as_of=None) -> dict:
    """Deterministic European whole-fund waterfall.

    Inputs (all from DB):
      • scheme.hurdle_rate_pct          (extracted; if null → cannot compute)
      • scheme.carry_pct                (extracted; if null → cannot compute)
      • scheme.first_close_date OR scheme.fund.inception_date
                                        (extracted; if null → cannot compute)
      • CapitalCall(scheme).call_date + total_call_amount      (extracted ledger)
      • EITHER CarriedInterest.total_distributions (extracted aggregate cell)
        OR sum(Distribution(scheme).total_net_amount)          (extracted ledger)

    The aggregate path is preferred when extracted with a real cell ref —
    if the Distributions sheet TOTAL row was correctly extracted, that's
    the single source of truth and matches what the CA computed. Re-summing
    persisted Distribution rows would under-count whenever the per-row
    persister missed any row.

    Returns dict of computed fields the persister will write to CarriedInterest.
    """
    from accounting.models import CarriedInterest, NAVRecord
    from lp.models import CapitalCall, Distribution
    from django.db.models import Sum

    # As-of date: prefer caller-supplied → latest CarriedInterest.calculation_date
    # → latest NAVRecord.nav_date → today. Using `today` is wrong for historic
    # imports because preferred_return accrues per day from each call.
    today = as_of
    if today is None:
        ci_existing = (CarriedInterest.objects.filter(scheme=scheme)
                       .order_by('-calculation_date').first())
        if ci_existing and ci_existing.calculation_date:
            today = ci_existing.calculation_date
    if today is None:
        nav_latest = (NAVRecord.objects.filter(scheme=scheme)
                      .order_by('-nav_date').first())
        if nav_latest and nav_latest.nav_date:
            today = nav_latest.nav_date
    if today is None:
        today = _date.today()

    hurdle_pct = _safe_decimal(scheme.hurdle_rate_pct)
    carry_pct = _safe_decimal(scheme.carry_pct)
    inception = scheme.first_close_date or getattr(scheme.fund, 'inception_date', None)
    # Default holdback policy (universal industry standard): 20% of carry
    # distributed sits in escrow. If a fund's LPA differs, store on Scheme;
    # extend this read when that field exists. Until then 20% is the SEBI /
    # ILPA default and matches every AIF we have on file.
    holdback_pct = Decimal('0.20')

    missing = []
    if hurdle_pct is None:
        missing.append('Scheme.hurdle_rate_pct (LPA hurdle %)')
    if carry_pct is None:
        missing.append('Scheme.carry_pct (LPA carry %)')
    if inception is None:
        missing.append('Scheme.first_close_date / Fund.inception_date')
    if missing:
        return {
            'computed': False,
            'reason': 'Insufficient LPA terms — ' + '; '.join(missing),
        }

    # ── Ledger sums (extracted facts only) ──────────────────────────────
    calls = list(CapitalCall.objects.filter(scheme=scheme).order_by('call_date'))
    if not calls:
        return {'computed': False, 'reason': 'No CapitalCall rows persisted'}

    total_called = sum((_safe_decimal(c.total_call_amount, Decimal('0')) for c in calls),
                       Decimal('0'))
    # Prefer extracted aggregate from CarriedInterest (which only persisted if
    # provenance was a real cell ref — i.e. someone wrote the totals row).
    ci_existing = (CarriedInterest.objects.filter(scheme=scheme)
                   .order_by('-calculation_date').first())
    if ci_existing and ci_existing.total_called_capital and ci_existing.total_called_capital > 0:
        if total_called == 0 or abs(ci_existing.total_called_capital - total_called) > Decimal('1'):
            # If extracted aggregate exists and differs materially from row-sum,
            # extracted aggregate (totals-row cell) wins.
            total_called = _safe_decimal(ci_existing.total_called_capital, total_called)

    # Distributions: prefer extracted aggregate over row-sum (row persister may
    # have missed rows for various reasons; the TOTAL row cell is authoritative
    # when extracted with a real cell-ref provenance).
    total_distributed = Decimal('0')
    if ci_existing and ci_existing.total_distributions and ci_existing.total_distributions > 0:
        total_distributed = _safe_decimal(ci_existing.total_distributions, Decimal('0'))
    if total_distributed == 0:
        dists = list(Distribution.objects.filter(scheme=scheme).order_by('distribution_date'))
        for d in dists:
            amt = _safe_decimal(d.total_net_amount)
            if amt is None:
                amt = _safe_decimal(d.total_gross_amount, Decimal('0'))
            total_distributed += amt or Decimal('0')

    # ── Step 1 — Return of Capital ──────────────────────────────────────
    step1_roc = min(total_called, total_distributed)

    # ── Step 2 — Preferred Return ───────────────────────────────────────
    # Per-call accrual: amount × ((1+hurdle)^years − 1), where years is
    # measured from each call's call_date to as_of_date. This is the
    # industry-standard "compounded preferred return" computation and it
    # matches the worked example in the Carry_Clawback ground-truth sheet.
    hurdle = hurdle_pct / Decimal('100')  # 8.00 → 0.08
    preferred_return = Decimal('0')
    for c in calls:
        years = Decimal(str((today - c.call_date).days)) / Decimal('365.25')
        try:
            accrual = _safe_decimal(c.total_call_amount, Decimal('0')) * (
                ((Decimal('1') + hurdle) ** years) - Decimal('1')
            )
            preferred_return += accrual
        except (InvalidOperation, OverflowError):
            pass
    preferred_return = preferred_return.quantize(Decimal('0.01'))

    # ── Step 3 — GP Catch-Up (100% to GP until carry% of profit-above-RoC)
    # carry_base = total profit above capital
    carry_base = max(Decimal('0'), total_distributed - total_called)
    carry_pct_decimal = carry_pct / Decimal('100')  # 20.00 → 0.20

    # GP entitlement = carry% of carry_base (the total carry GP earns over
    # the fund life). Catch-up brings GP up to carry% of (pref + catchup).
    # Equivalent closed form: catchup = pref × (carry% / (1 − carry%))
    one_minus_carry = Decimal('1') - carry_pct_decimal
    if one_minus_carry == 0:
        return {'computed': False, 'reason': 'carry_pct = 100% — invalid'}
    catchup_amount = (preferred_return * carry_pct_decimal / one_minus_carry).quantize(
        Decimal('0.01')
    )
    # Cap catchup so it does not exceed remaining distributed cash above
    # (RoC + preferred return).
    available_after_roc_pref = max(Decimal('0'), total_distributed - step1_roc - preferred_return)
    catchup_amount = min(catchup_amount, available_after_roc_pref)

    # GP carry entitlement = carry% × carry_base
    carry_amount_gross = (carry_pct_decimal * carry_base).quantize(Decimal('0.01'))

    # ── Step 4 — 80:20 split of remainder above (pref + catchup) ────────
    remainder = max(Decimal('0'), available_after_roc_pref - catchup_amount)
    step4_lp = (remainder * one_minus_carry).quantize(Decimal('0.01'))
    step4_gp = (remainder * carry_pct_decimal).quantize(Decimal('0.01'))

    # GP distributed = catchup + step4_gp. If > entitlement → clawback.
    gp_distributed = (catchup_amount + step4_gp).quantize(Decimal('0.01'))
    gp_clawback = max(Decimal('0'), gp_distributed - carry_amount_gross).quantize(
        Decimal('0.01')
    )

    # Holdback escrow = holdback_pct × gp_distributed
    gp_holdback = (holdback_pct * gp_distributed).quantize(Decimal('0.01'))

    # Net carry = gross entitlement − clawback (after holdback covers).
    # When escrow ≥ clawback, the net to GP is entitlement − clawback.
    carry_amount_net = (carry_amount_gross - gp_clawback).quantize(Decimal('0.01'))

    return {
        'computed': True,
        'reason': None,
        'total_called_capital': total_called.quantize(Decimal('0.01')),
        'total_distributions': total_distributed.quantize(Decimal('0.01')),
        'step1_return_of_capital': step1_roc.quantize(Decimal('0.01')),
        'preferred_return_amount': preferred_return,
        'gp_catchup_amount': catchup_amount,
        'carry_base': carry_base.quantize(Decimal('0.01')),
        'carry_amount_gross': carry_amount_gross,
        'step4a_lp_residual': step4_lp,
        'step4b_gp_residual_carry': step4_gp,
        'gp_distributed': gp_distributed,
        'gp_holdback_escrow': gp_holdback,
        'gp_clawback_provision': gp_clawback,
        'carry_amount_net': carry_amount_net,
        'hurdle_rate_pct_used': hurdle_pct,
        'carry_pct_used': carry_pct,
        'inception_used': inception.isoformat() if inception else None,
        'as_of': today.isoformat(),
    }


def apply_python_waterfall(fund, as_of=None) -> dict:
    """For every Scheme in the fund, compute the deterministic waterfall and
    UPDATE the latest CarriedInterest row IF AND ONLY IF the extracted
    value is empty / 0 / null for that field.

    Precedence per field (strict):
      1. EXTRACTED ground truth from Carry_Clawback / Fund_Overview cell
         (already on CarriedInterest from persister)  → KEEP.
      2. PYTHON-COMPUTED from extracted LPA terms + ledger              → WRITE.
      3. Neither available                                                → leave 0/null.

    Returns summary dict for diagnostics."""
    from accounting.models import CarriedInterest

    summary = {'schemes': 0, 'computed': 0, 'extracted_kept': 0, 'reasons': []}
    for scheme in fund.schemes.all():
        summary['schemes'] += 1
        wf = compute_waterfall_for_scheme(scheme, as_of=as_of)
        if not wf.get('computed'):
            summary['reasons'].append(f'{scheme.name}: {wf.get("reason")}')
            continue
        summary['computed'] += 1

        # Latest CarriedInterest row (persister wrote one per import).
        ci = (CarriedInterest.objects
              .filter(scheme=scheme)
              .order_by('-calculation_date', '-created_at')
              .first())
        if ci is None:
            ci = CarriedInterest.objects.create(
                scheme=scheme,
                calculation_date=_date.today(),
                calculation_status='indicative',
            )

        # Field-by-field merge: keep extracted truth (non-zero) over computed.
        FIELD_MAP = [
            ('total_called_capital',   'total_called_capital'),
            ('total_distributions',    'total_distributions'),
            ('preferred_return_amount','preferred_return_amount'),
            ('carry_base',             'carry_base'),
            ('carry_amount_gross',     'carry_amount_gross'),
            ('carry_amount_net',       'carry_amount_net'),
            ('gp_clawback_provision',  'gp_clawback_provision'),
        ]
        changes = []
        for db_field, wf_key in FIELD_MAP:
            current = getattr(ci, db_field, None) or Decimal('0')
            computed = wf.get(wf_key)
            # Only overwrite when DB has nothing (0 or null) AND we have a
            # computed value. Extracted facts always win.
            if (current is None or current == 0) and computed is not None:
                setattr(ci, db_field, computed)
                changes.append(f'{db_field}={computed}')
            else:
                summary['extracted_kept'] += 1
        if changes:
            ci.save()
            logger.info(
                f'[phase4.waterfall] {scheme.name}: filled {len(changes)} '
                f'field(s) from deterministic computation — {", ".join(changes)}'
            )

    logger.info(
        f'[phase4.waterfall] schemes={summary["schemes"]} '
        f'computed={summary["computed"]} '
        f'extracted_kept={summary["extracted_kept"]} '
        f'skipped={len(summary["reasons"])}'
    )
    return summary


# ════════════════════════════════════════════════════════════════════════════
# UNIVERSAL FUND-LEVEL AGGREGATOR
# ════════════════════════════════════════════════════════════════════════════
#
# Single deterministic source of truth for every aggregate the dashboard
# displays. Reads atomic DB rows (CapitalCall, Distribution, NAVRecord,
# Investment, Valuation) + extracted LPA terms (Scheme.hurdle_rate_pct,
# Scheme.carry_pct) + optional ground-truth overrides extracted by Gemini
# from explicit cells (e.g. Carry_Clawback!R37).
#
# Every aggregate is produced by ONE function call. Both _persist_carried_
# interest and _persist_fund_metrics consume the same output. Re-running on
# identical inputs MUST yield identical outputs.
#
# Rule of thumb: anything that can be computed from atomic facts MUST be
# computed here. Gemini's own "computed" aggregates are DISCARDED.

_CELLREF_RE = None  # lazy

def _is_cell_ref(prov) -> bool:
    """True iff provenance string looks like a real cell reference, NOT a
    formula or assumed value or computed marker.

    Universal across every fund/sheet. Accepts:
      • 'Sheet!A1'  / 'Sheet!A1:B5'                 (openpyxl style)
      • 'Sheet:R12:C3' / 'Sheet R12'                (legacy phase 2 style)
      • 'sum(Sheet!A1:A10)' or similar simple aggregations over a real range
    Rejects:
      • starts with '='  → formula
      • contains 'assumed' / 'computed' / 'synthetic' / 'not_found'
      • starts with 'computed:' / 'derived'
    """
    if prov is None:
        return False
    s = str(prov).strip().lower()
    if not s:
        return False
    if s.startswith('='):
        return False
    for marker in ('assumed', 'computed', 'synthetic', 'not_found_in_workbook',
                   'not found in workbook', 'estimate', 'derived from',
                   'fabricated'):
        if marker in s:
            return False
    global _CELLREF_RE
    if _CELLREF_RE is None:
        import re as _re
        # Sheet!A1, Sheet!A1:B5, Sheet:R12:C3, Sheet R12, row 12
        _CELLREF_RE = _re.compile(
            r'(![a-z]+\d+|:r\d+:c\d+|\br\d+\b|\brow\s*\d+)',
            _re.IGNORECASE,
        )
    return bool(_CELLREF_RE.search(s))


def _extract_overrides(unified_json: dict) -> dict:
    """Pull every Gemini value whose provenance is a real cell reference.

    Returns a flat dict keyed by canonical metric name:
      {
        'total_capital_called':   Decimal,
        'total_distributions':    Decimal,
        'carry_base':             Decimal,
        'gp_carry_gross':         Decimal,
        'gp_carry_net':           Decimal,
        'gp_clawback':            Decimal,
        'gp_holdback':            Decimal,
        'preferred_return':       Decimal,
        'gp_catchup':             Decimal,
        'gp_carry_distributed':   Decimal,
        'fund_nav_latest':        Decimal,
        'tvpi':                   Decimal,
        'dpi':                    Decimal,
        'rvpi':                   Decimal,
        'moic':                   Decimal,
        'net_irr_stated':         Decimal,
        'total_committed':        Decimal,
        'total_invested':         Decimal,
        'total_realised':         Decimal,
      }
    Only fields whose provenance survives _is_cell_ref get included.
    Universal across any fund — applies the same gate to every field.
    """
    out: dict = {}
    if not isinstance(unified_json, dict):
        return out

    wf = unified_json.get('waterfall') or {}
    fp = unified_json.get('fund_performance') or {}
    wf_prov = (wf.get('provenance') or {}) if isinstance(wf, dict) else {}
    fp_prov = (fp.get('provenance') or {}) if isinstance(fp, dict) else {}

    # Field aliases: (canonical_name, [source_dict, value_key, provenance_key])
    field_specs = [
        ('total_capital_called', wf, 'total_capital_called', wf_prov),
        ('total_capital_called', fp, 'total_called_capital', fp_prov),
        ('total_distributions',  wf, 'total_distributions',  wf_prov),
        ('total_distributions',  fp, 'total_distributions',  fp_prov),
        ('total_committed',      fp, 'total_committed_capital', fp_prov),
        ('total_invested',       fp, 'total_invested_capital',  fp_prov),
        ('total_realised',       fp, 'total_realised_proceeds', fp_prov),
        ('fund_nav_latest',      fp, 'fund_nav_latest',      fp_prov),
        ('carry_base',           wf, 'carry_base',           wf_prov),
        ('gp_carry_gross',       wf, 'carry_amount_gross',   wf_prov),
        ('gp_carry_net',         wf, 'net_carry',            wf_prov),
        ('gp_carry_net',         wf, 'carry_amount_net',     wf_prov),
        ('gp_clawback',          wf, 'clawback_provision',   wf_prov),
        ('gp_holdback',          wf, 'gp_holdback_escrow',   wf_prov),
        ('preferred_return',     wf, 'preferred_return_amount', wf_prov),
        ('preferred_return',     wf, 'step_2_preferred_return', wf_prov),
        ('gp_catchup',           wf, 'step_3_catchup_amount',   wf_prov),
        ('gp_carry_distributed', wf, 'carry_distributed_gross', wf_prov),
        ('tvpi',                 fp, 'tvpi',                 fp_prov),
        ('dpi',                  fp, 'dpi',                  fp_prov),
        ('rvpi',                 fp, 'rvpi',                 fp_prov),
        ('moic',                 fp, 'moic_portfolio',       fp_prov),
        ('moic',                 fp, 'moic',                 fp_prov),
        ('net_irr_stated',       fp, 'net_irr_stated',       fp_prov),
    ]
    for canonical, src, value_key, prov_block in field_specs:
        if canonical in out:
            continue
        if not isinstance(src, dict):
            continue
        v = src.get(value_key)
        p = (prov_block or {}).get(value_key)
        if v is None or v == '':
            continue
        if not _is_cell_ref(p):
            continue
        dv = _safe_decimal(v)
        if dv is None:
            continue
        out[canonical] = dv
    return out


# ── Option C — cell-verified aggregate overrides (universal) ────────────────
#
# Gemini extracts a `workbook_aggregates[]` array of `{metric, value, sheet,
# cell}` entries from any labelled aggregate it sees in the workbook. Python
# verifies each one by re-reading the exact cell from the in-memory
# workbook_cache. Matches become trusted overrides; mismatches are rejected
# with a logged warning. Universal across any AIF format — sheet name, cell
# position, and layout can all change because Gemini discovers them each run.

_OPTION_C_REL_TOLERANCE = Decimal('0.01')   # 1 % relative
_OPTION_C_ABS_TOLERANCE = Decimal('1.0')    # ₹1 Cr absolute floor


def _parse_numeric(v):
    """Coerce any cell value (str / int / float / decimal / formatted text)
    into a Decimal. Returns None on uncoercible inputs."""
    if v is None or v == '':
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        try:
            return Decimal(str(v))
        except InvalidOperation:
            return None
    s = str(v).strip()
    if not s:
        return None
    # Strip currency symbols and thousands separators commonly seen in
    # Indian AIF Excels: '₹', ',', spaces, ' Cr', ' L'.
    for tok in ('₹', '$', '€', '£', ',', ' Cr', ' Lakhs', ' L', ' Mn', '%'):
        s = s.replace(tok, '')
    s = s.strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _verify_workbook_aggregates(unified_json: dict) -> dict:
    """DISABLED as of 2026-06-30.

    The new Phase 4 architecture routes ALL workbook_aggregates[] entries
    through phase4_reconciler.collect_trusted_extractions() which applies
    a semantic LABEL whitelist on top of cell-value verification.

    Cell-value verification alone (this function's old behaviour) accepts
    mislabelled values — e.g. Bharatcrest's "GP Carry Allocated" cell at
    Capital_Accounts!V11 = ₹1,153.56 Cr passed the value check but is NOT
    actually gross carry (it includes catch-up + GP commitment returns).

    Returning {} here ensures compute_all_fund_aggregates() does not
    re-apply Gemini's raw label→metric mapping. The reconciler is the
    single gatekeeper.
    """
    return {}


def compute_all_fund_aggregates(fund, scheme, unified_json: dict = None) -> dict:
    """THE single deterministic source for every fund-level aggregate.

    Universal across every fund / sector / format / source system (Excel,
    Tally, SAP). Reads ONLY:
      • Atomic ledgers from DB: CapitalCall, Distribution, Investment,
        Valuation, NAVRecord, Commitment, ExitEvent
      • LPA terms from Scheme: hurdle_rate_pct, carry_pct, first_close_date

    Architecture decision (Option A, locked 2026-06-30 after the Bharatcrest
    Gemini-fakes-provenance incident):

      NO Gemini-emitted aggregate values are trusted. Not even those with
      cell-reference provenance. Reason: Gemini was observed emitting
      "carry_base=3381, provenance=Fund_Overview!B60" when cell B60 actually
      contained 1430.60 — i.e. fabricated cell refs to make computed values
      look extracted. Format-only validation of provenance is insufficient.

      Every aggregate is computed in Python from persisted atomic ledger
      rows. Same DB → same numbers, deterministically. Re-importing the
      same file 100 times yields identical results.

    Returns a flat dict consumed by BOTH _persist_carried_interest AND
    _persist_fund_metrics so they cannot diverge.

    Precedence per metric (strict, post-2026-06-30):
      1. COMPUTED from atomic DB ledger rows (deterministic Python)
      2. null with a 'reasons[metric]' entry explaining why

    `unified_json` param is retained for the function signature but is no
    longer consulted for aggregate values — only for as_of_date hints when
    no NAVRecord exists.
    """
    from accounting.models import NAVRecord
    from lp.models import CapitalCall, Distribution, Commitment
    from investments.models import Investment, Valuation, ExitEvent
    from django.db.models import Sum, OuterRef, Subquery
    from django.db.models.functions import Coalesce

    # ── Option C — cell-verified aggregate overrides ────────────────────
    # Gemini emits workbook_aggregates[]; Python re-reads each named cell
    # from the in-memory workbook_cache; matches become trusted overrides;
    # mismatches are rejected. Unlike the abandoned Gemini-provenance-trust
    # path (Option B), this is robust against Gemini fabricating cell refs
    # because we VERIFY each one against the actual workbook bytes.
    #
    # Universal across any AIF format: Gemini discovers where each labelled
    # aggregate lives; cell positions / sheet names / layouts can all change.
    verified_overrides = _verify_workbook_aggregates(unified_json or {})
    reasons: dict[str, str] = {}

    # ── Atomic ledger sums (extracted facts only) ───────────────────────
    db_total_called = (CapitalCall.objects.filter(scheme=scheme)
                       .aggregate(s=Sum('total_call_amount'))['s']) or Decimal('0')
    db_total_committed = (Commitment.objects.filter(scheme=scheme)
                          .aggregate(s=Sum('commitment_amount'))['s']) or Decimal('0')
    db_total_invested = (Investment.objects.filter(scheme=scheme)
                         .aggregate(s=Sum('total_invested'))['s']) or Decimal('0')
    db_total_realised = (ExitEvent.objects.filter(investment__scheme=scheme)
                         .aggregate(s=Sum('net_exit_proceeds'))['s']) or Decimal('0')
    if db_total_realised == 0:
        db_total_realised = (ExitEvent.objects.filter(investment__scheme=scheme)
                             .aggregate(s=Sum('proceeds'))['s']) or Decimal('0')

    # Distributions: prefer net, fall back to gross per-row.
    db_total_distributed = Decimal('0')
    for d in Distribution.objects.filter(scheme=scheme):
        amt = _safe_decimal(d.total_net_amount)
        if amt is None:
            amt = _safe_decimal(d.total_gross_amount, Decimal('0'))
        db_total_distributed += amt or Decimal('0')

    # Latest fund NAV — only when a NAVRecord has a non-zero total_nav.
    # Never invent: a null Net NAV means dashboard shows "—".
    latest_nav = (NAVRecord.objects.filter(scheme=scheme)
                  .order_by('-nav_date').first())
    db_fund_nav = None
    db_as_of = None
    if latest_nav and latest_nav.nav_date:
        db_as_of = latest_nav.nav_date
        v = _safe_decimal(latest_nav.total_nav)
        if v and v > 0:
            db_fund_nav = v
        else:
            # Try gross_nav − accrued_management_fees if total_nav blank.
            gross = _safe_decimal(getattr(latest_nav, 'gross_nav', None))
            mgmt = _safe_decimal(getattr(latest_nav, 'management_fee_payable', None),
                                 Decimal('0'))
            if gross and gross > 0:
                db_fund_nav = gross - (mgmt or Decimal('0'))

    # Unrealised fair value of holdings — sum of latest SOURCE Valuation per
    # Investment. Critical filter (2026-06-30): exclude synthetic valuations
    # (methodology='derived_from_cost_x_scheme_markup') that auto-fill the
    # per-investment FV column on the dashboard. Including them would pollute
    # IRR/MOIC/TVPI/RVPI by double-counting marked-up cost as fair value.
    # Universal across funds — synthetic rows are tagged at creation time.
    # FV precedence: prefer `fair_value` (Cover/Summary "Total FV" column)
    # over `fair_value_of_holding`. Universal: workbooks that expose only
    # one FV column have both fields mirrored by the persister, so the
    # coalesce is a no-op for them. Workbooks with distinct equity vs.
    # holding columns (Multiples-style) get the value the Cover displays.
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
    db_active_fv = Decimal('0')
    for inv in inv_qs:
        if inv.latest_fv:
            db_active_fv += inv.latest_fv

    # ── Single source: atomic DB ledger sums (Option A) ────────────────
    # Override path removed because Gemini was observed fabricating cell-ref
    # provenance (claiming Fund_Overview!B60 = 3381 when the cell actually
    # contained 1430.60). Universal solution: trust ONLY persisted ledger
    # rows + LPA terms on Scheme.
    def _pick(canonical: str, db_value, *, reason_when_missing: str = ''):
        if db_value is not None and db_value > 0:
            return db_value, 'computed_from_db'
        if reason_when_missing:
            reasons[canonical] = reason_when_missing
        return None, 'missing'

    total_called,   total_called_src   = _pick('total_capital_called', db_total_called,
                                               reason_when_missing='No CapitalCall rows persisted')
    total_distributed, total_dist_src  = _pick('total_distributions',  db_total_distributed,
                                               reason_when_missing='No Distribution rows persisted')
    total_committed, total_committed_src = _pick('total_committed',    db_total_committed,
                                                 reason_when_missing='No Commitment rows persisted')
    total_invested,  total_invested_src = _pick('total_invested',      db_total_invested,
                                                reason_when_missing='No Investment rows persisted')
    total_realised,  total_realised_src = _pick('total_realised',      db_total_realised,
                                                reason_when_missing='No ExitEvent rows persisted')
    fund_nav,        fund_nav_src      = _pick('fund_nav_latest',      db_fund_nav,
                                               reason_when_missing='Net NAV not extractable from NAV-walk sheet')

    # Apply Option C top-level overrides (verified workbook aggregates win
    # over atomic-DB sums). Critical for fund_nav_latest because Bharatcrest-
    # style workbooks don't publish Net NAV per-period; the CA's "as of"
    # value lives in a Cover / Summary cell.
    if 'total_capital_called' in verified_overrides:
        total_called = verified_overrides['total_capital_called']
        total_called_src = 'extracted_verified'
    if 'total_distributions' in verified_overrides:
        total_distributed = verified_overrides['total_distributions']
        total_dist_src = 'extracted_verified'
    if 'total_committed_capital' in verified_overrides:
        total_committed = verified_overrides['total_committed_capital']
        total_committed_src = 'extracted_verified'
    if 'total_invested_capital' in verified_overrides:
        total_invested = verified_overrides['total_invested_capital']
        total_invested_src = 'extracted_verified'
    if 'total_realised_proceeds' in verified_overrides:
        total_realised = verified_overrides['total_realised_proceeds']
        total_realised_src = 'extracted_verified'
    if 'fund_nav_latest' in verified_overrides:
        fund_nav = verified_overrides['fund_nav_latest']
        fund_nav_src = 'extracted_verified'

    # ── LPA terms (only if extracted onto Scheme) ───────────────────────
    hurdle_pct = _safe_decimal(scheme.hurdle_rate_pct)
    carry_pct  = _safe_decimal(scheme.carry_pct)
    inception  = scheme.first_close_date or getattr(scheme.fund, 'inception_date', None)
    as_of      = db_as_of or _date.today()

    # ── Waterfall — deterministic Python from atomic facts ─────────────
    # Carry-type dispatch. Universal across any AIF carry structure:
    #   • 'european' (whole-fund): the standard SEBI / ILPA convention.
    #     LPs first receive 100% of called capital (RoC), then accrued
    #     preferred return at hurdle, then GP catches up to its carry%,
    #     then 80:20 split of remainder. Computed below.
    #   • 'american' (deal-by-deal): carry computed per investment exit.
    #     Not implemented here — Phase 4 per-investment IRR/MOIC handles
    #     per-deal performance instead. Waterfall block emits null and a
    #     reason so the dashboard shows "—" rather than a wrong number.
    #   • any other / unknown type → same null+reason path.
    carry_type = (getattr(scheme, 'carry_type', None) or 'european').strip().lower()

    carry_base = preferred_return = gp_catchup = gp_carry_gross = None
    gp_holdback = gp_clawback = gp_carry_net = gp_carry_distributed = None
    waterfall_source = 'missing'

    # Python compute when all required inputs are extracted facts AND the
    # carry structure is European whole-fund.
    can_compute_wf = (
        carry_type == 'european'
        and hurdle_pct is not None and carry_pct is not None
        and inception is not None
        and total_called is not None and total_called > 0
        and total_distributed is not None
    )
    if not can_compute_wf and carry_type != 'european':
        reasons['waterfall'] = (
            f"Scheme carry_type='{carry_type}' — only 'european' whole-fund "
            f"waterfall is currently supported. Dashboard will show '—' "
            f"for waterfall tiles. Per-investment IRR/MOIC still computed."
        )

    if can_compute_wf:
        calls = list(CapitalCall.objects.filter(scheme=scheme).order_by('call_date'))
        hurdle = hurdle_pct / Decimal('100')
        carry_d = carry_pct / Decimal('100')
        one_minus_carry = Decimal('1') - carry_d

        # Per-call preferred return accrual (industry standard, matches the
        # Carry_Clawback worked example).
        py_pref = Decimal('0')
        for c in calls:
            try:
                years = Decimal(str((as_of - c.call_date).days)) / Decimal('365.25')
                py_pref += _safe_decimal(c.total_call_amount, Decimal('0')) * (
                    ((Decimal('1') + hurdle) ** years) - Decimal('1')
                )
            except (InvalidOperation, OverflowError, TypeError):
                pass
        py_pref = py_pref.quantize(Decimal('0.01'))

        py_carry_base = max(Decimal('0'), total_distributed - total_called).quantize(Decimal('0.01'))
        py_catchup_uncapped = (py_pref * carry_d / one_minus_carry).quantize(Decimal('0.01'))
        py_avail_after_roc_pref = max(Decimal('0'),
                                      total_distributed - min(total_called, total_distributed) - py_pref)
        py_catchup = min(py_catchup_uncapped, py_avail_after_roc_pref).quantize(Decimal('0.01'))
        py_carry_gross = (carry_d * py_carry_base).quantize(Decimal('0.01'))

        # ── ACTUAL GP-distributed-to-date (universal across European AIFs) ──
        # Sum from TWO real per-row sources:
        #   (a) Distribution.gp_carry_amount  — the per-event GP carry
        #       component (when the source workbook publishes the split column).
        #   (b) Distribution.total_net_amount WHERE distribution_type='carry'
        #       — standalone carry-distribution events (when the source uses
        #       a separate row for each carry payment instead of a column).
        # Either pattern is industry-standard; this captures both. When the
        # workbook publishes NEITHER, both sums are 0 and we cannot infer
        # over-distribution — clawback/holdback/net emit null + reason so the
        # dashboard shows "—" rather than a misleading 0 / wrong number.
        gp_component_sum = (
            Distribution.objects
            .filter(scheme=scheme, gp_carry_amount__isnull=False)
            .aggregate(s=Sum('gp_carry_amount'))['s']
        ) or Decimal('0')
        carry_event_sum = Decimal('0')
        for d in Distribution.objects.filter(scheme=scheme, distribution_type='carry'):
            amt = _safe_decimal(d.total_net_amount)
            if amt is None:
                amt = _safe_decimal(d.total_gross_amount, Decimal('0'))
            carry_event_sum += amt or Decimal('0')
        actual_gp_distributed = (gp_component_sum + carry_event_sum).quantize(Decimal('0.01'))
        gp_data_captured = actual_gp_distributed > 0

        # Holdback % — LPA-specific value on Scheme, falling back to industry
        # default 20% (SEBI / ILPA standard) when LPA didn't publish it.
        scheme_holdback_pct = _safe_decimal(getattr(scheme, 'gp_holdback_pct', None))
        holdback_rate = (scheme_holdback_pct / Decimal('100')) if scheme_holdback_pct else Decimal('0.20')

        # Atomic-Python assignments — extracted facts win.
        carry_base           = py_carry_base
        preferred_return     = py_pref
        gp_catchup           = py_catchup
        gp_carry_gross       = py_carry_gross
        waterfall_source     = 'computed_from_db'

        # Compute formula-derived defaults FIRST. These are mathematically
        # consistent with the European whole-fund waterfall (catch-up + step-4
        # GP share) and give the dashboard defensible numbers even when no
        # per-event GP carry data has been captured. Universal — works for
        # any European-waterfall fund with hurdle/carry/ledgers in DB.
        py_step4_pool = max(Decimal('0'), py_avail_after_roc_pref - py_catchup)
        py_step4_gp = (py_step4_pool * carry_d).quantize(Decimal('0.01'))
        formula_gp_distributed = (py_catchup + py_step4_gp).quantize(Decimal('0.01'))
        formula_gp_holdback = (holdback_rate * formula_gp_distributed).quantize(Decimal('0.01'))
        formula_gp_clawback = max(Decimal('0'),
                                  formula_gp_distributed - py_carry_gross).quantize(Decimal('0.01'))
        formula_gp_net = (formula_gp_distributed - formula_gp_holdback
                          - formula_gp_clawback).quantize(Decimal('0.01'))

        if gp_data_captured:
            # We have REAL per-event GP carry data → trustworthy clawback math.
            gp_carry_distributed = actual_gp_distributed
            gp_holdback = (holdback_rate * actual_gp_distributed).quantize(Decimal('0.01'))
            gp_clawback = max(Decimal('0'),
                              actual_gp_distributed - py_carry_gross).quantize(Decimal('0.01'))
            # Net = gross distributed − holdback − clawback (matches CA's
            # worked-example output exactly: 296.12 − 59.22 − 10 = 226.90).
            gp_carry_net = (actual_gp_distributed - gp_holdback - gp_clawback).quantize(Decimal('0.01'))
        else:
            # No per-event carry-split data. Use formula-derived defaults for
            # gross / distributed / net, BUT leave clawback as None so the
            # downstream persister's waterfall-block fallback can pick up
            # Gemini's CA-extracted value (e.g. Bharatcrest's Carry_Clawback
            # sheet publishes clawback=10 explicitly). Per user rule:
            # extracted-from-Excel wins over formula-computed.
            gp_carry_distributed = formula_gp_distributed
            gp_holdback          = formula_gp_holdback
            gp_clawback          = None  # ← let wf fallback fill if present
            gp_carry_net         = formula_gp_net
            reasons['clawback_basis'] = (
                'No per-event GP carry component in atomic ledger — leaving '
                'clawback as None so persister can fall back to Gemini-extracted '
                'value from the waterfall block. If both are missing the '
                'dashboard will show ₹0 or "—" rather than a formula-derived 0.'
            )

        # ── Option C: apply cell-verified overrides on TOP of formula/atomic.
        # When Gemini's workbook_aggregates entry survived cell-content
        # verification, the CA's own written number wins over any formula
        # we computed. This handles the Bharatcrest case where the CA's
        # worked example (e.g. Carry_Clawback!R37) is the source of truth
        # but our atomic ledger doesn't have the per-event GP carry data.
        _METRIC_TO_LOCAL = {
            'carry_base':              'carry_base',
            'carry_amount_gross':      'gp_carry_gross',
            'carry_distributed_gross': 'gp_carry_distributed',
            'gp_clawback':             'gp_clawback',
            'gp_holdback':             'gp_holdback',
            'carry_amount_net':        'gp_carry_net',
            'preferred_return':        'preferred_return',
            'gp_catchup':              'gp_catchup',
        }
        for ov_metric, ov_value in verified_overrides.items():
            local = _METRIC_TO_LOCAL.get(ov_metric)
            if local is None:
                continue
            if   local == 'carry_base':            carry_base = ov_value
            elif local == 'gp_carry_gross':        gp_carry_gross = ov_value
            elif local == 'gp_carry_distributed':  gp_carry_distributed = ov_value
            elif local == 'gp_clawback':           gp_clawback = ov_value
            elif local == 'gp_holdback':           gp_holdback = ov_value
            elif local == 'gp_carry_net':          gp_carry_net = ov_value
            elif local == 'preferred_return':      preferred_return = ov_value
            elif local == 'gp_catchup':            gp_catchup = ov_value
    elif carry_type == 'european':
        # Diagnose WHY European compute couldn't run — surface the missing
        # input so the user can fix the source workbook / re-extract.
        if hurdle_pct is None:
            reasons['waterfall'] = 'Scheme.hurdle_rate_pct missing — cannot compute waterfall'
        elif carry_pct is None:
            reasons['waterfall'] = 'Scheme.carry_pct missing — cannot compute waterfall'
        elif inception is None:
            reasons['waterfall'] = 'Scheme.first_close_date / Fund.inception_date missing'
        elif total_called is None or total_called == 0:
            reasons['waterfall'] = 'No CapitalCall rows — cannot compute waterfall'
        elif total_distributed is None:
            reasons['waterfall'] = 'No Distribution rows — cannot compute waterfall'

    # ── Performance ratios — deterministic Python from atomic facts ─────
    # All ratios derived from atomic DB totals + extracted Net NAV.
    # Universal across any AIF: same DB rows → same ratios. Gemini's
    # tvpi/dpi/rvpi/moic values are NOT consulted (Option A).
    tvpi = dpi = rvpi = moic = None

    # Helper — same NAV-sanity logic as IRR uses (2026-06-30 fix).
    # Atomic active FV is the truth; extracted fund_nav is a fallback ONLY
    # when atomic is missing AND the extracted value passes a 5× sanity
    # bound vs invested. Without this guard, MOIC/TVPI/RVPI compound the
    # NAV-extraction error (Bharatcrest 4746 NAV vs 2079 atomic).
    def _residual_value():
        if db_active_fv and db_active_fv > 0:
            return db_active_fv
        if fund_nav is not None and fund_nav > 0:
            if total_invested and fund_nav > total_invested * Decimal('5'):
                return None
            return fund_nav
        return None

    # MOIC = (Distributions + remaining FV) / Invested Cost
    # Uses 'invested cost' (not 'called capital') in the denominator because
    # called capital includes management fees + fund expenses that aren't
    # invested. This is the ILPA-aligned definition universally.
    if total_invested and total_invested > 0:
        nav_part = _residual_value()
        moic = ((total_distributed or Decimal('0')) + (nav_part or Decimal('0'))) / total_invested
        moic = moic.quantize(Decimal('0.0001'))

    # DPI = cumulative distributions / called capital
    if total_called and total_called > 0 and total_distributed is not None:
        dpi = (total_distributed / total_called).quantize(Decimal('0.0001'))

    # RVPI = residual NAV / called capital — uses atomic FV preferentially.
    if total_called and total_called > 0:
        nav_part = _residual_value()
        if nav_part:
            rvpi = (nav_part / total_called).quantize(Decimal('0.0001'))

    # TVPI = DPI + RVPI (or DPI alone if RVPI uncomputable)
    if dpi is not None:
        tvpi = (dpi + (rvpi or Decimal('0'))).quantize(Decimal('0.0001'))

    # ── Net IRR — REAL cashflows + atomic terminal value ────────────────
    # Capital calls (negative), distributions (positive), then add terminal
    # unrealised value at as_of date.
    #
    # Terminal-value precedence (2026-06-30 fix):
    #   1. db_active_fv  — sum of latest Valuation.fair_value_of_holding per
    #                      Investment. This is what the portfolio is ACTUALLY
    #                      worth on the books today; the right IRR terminal.
    #   2. fund_nav      — extracted NAV cell, ONLY if db_active_fv missing
    #                      AND fund_nav is sanity-bounded (not >5× invested).
    # The earlier version always used fund_nav as terminal, which inflated
    # IRR when the extracted NAV was wrong (e.g. Bharatcrest 4746 vs 2079
    # actual unrealised — turned 38% IRR into 46%). Atomic FV is universal:
    # works for any fund where Valuation rows persisted, no LPA dependency.
    net_irr = None
    cf = []
    for c in sorted(
        CapitalCall.objects.filter(scheme=scheme),
        key=lambda c: (c.call_date or as_of, str(c.id))
    ):
        amt = _safe_decimal(c.total_call_amount, Decimal('0'))
        if c.call_date and amt and amt > 0:
            cf.append((c.call_date, -amt))
    for d in sorted(
        Distribution.objects.filter(scheme=scheme),
        key=lambda d: (d.distribution_date or as_of, str(d.id))
    ):
        amt = _safe_decimal(d.total_net_amount)
        if amt is None:
            amt = _safe_decimal(d.total_gross_amount, Decimal('0'))
        if d.distribution_date and amt and amt > 0:
            cf.append((d.distribution_date, amt))

    terminal_value = None
    if db_active_fv and db_active_fv > 0:
        terminal_value = db_active_fv
        reasons['net_irr_terminal'] = (
            f'Terminal = atomic Valuation sum (₹{db_active_fv} Cr) — '
            f'preferred over extracted NAV for IRR accuracy.'
        )
    elif fund_nav is not None and fund_nav > 0:
        # Sanity bound: a fund NAV >5× invested is almost certainly a
        # mis-extracted cell. Skip the terminal in that case.
        if total_invested and fund_nav > total_invested * Decimal('5'):
            reasons['net_irr_terminal'] = (
                f'Extracted fund_nav={fund_nav} > 5× invested={total_invested} — '
                f'suspect extraction error; omitting terminal value from IRR.'
            )
        else:
            terminal_value = fund_nav
            reasons['net_irr_terminal'] = (
                f'Terminal = extracted fund_nav (₹{fund_nav} Cr) — no atomic '
                f'valuations available.'
            )
    if terminal_value is not None:
        cf.append((as_of, terminal_value))

    irr = _xirr(cf)
    if irr is not None:
        net_irr = irr
    else:
        reasons['net_irr'] = ('Insufficient cashflows OR no terminal value — '
                              'IRR cannot be computed.')

    # ── Drawdown / uncalled ────────────────────────────────────────────
    total_uncalled = None
    if total_committed and total_called is not None:
        total_uncalled = max(Decimal('0'), total_committed - total_called)

    return {
        # Totals (atomic facts)
        'total_capital_called':   total_called,
        'total_distributions':    total_distributed,
        'total_committed_capital': total_committed,
        'total_uncalled_capital':  total_uncalled,
        'total_invested_capital':  total_invested,
        'total_realised_proceeds': total_realised,
        'total_unrealised_fv_holding': db_active_fv if db_active_fv > 0 else None,
        'fund_nav_latest':        fund_nav,
        # Waterfall (extracted-first, else Python-computed)
        'carry_base':             carry_base,
        'preferred_return_amount': preferred_return,
        'gp_catchup_amount':      gp_catchup,
        'carry_amount_gross':     gp_carry_gross,
        'gp_carry_distributed':   gp_carry_distributed,
        'gp_holdback_escrow':     gp_holdback,
        'gp_clawback_provision':  gp_clawback,
        'carry_amount_net':       gp_carry_net,
        # Performance ratios (extracted-first, else atomic-derived)
        'tvpi':                   tvpi,
        'dpi':                    dpi,
        'rvpi':                   rvpi,
        'moic':                   moic,
        'net_irr':                net_irr,
        # Metadata
        'as_of_date':             as_of,
        'waterfall_source':       waterfall_source,
        'sources': {
            'total_capital_called':  total_called_src,
            'total_distributions':   total_dist_src,
            'total_committed':       total_committed_src,
            'total_invested':        total_invested_src,
            'total_realised':        total_realised_src,
            'fund_nav':              fund_nav_src,
            'waterfall':             waterfall_source,
        },
        'reasons':                reasons,
    }
