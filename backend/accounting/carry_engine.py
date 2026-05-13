"""
Carry Engine — computes carried interest waterfall for a scheme.

Waterfall (European / Whole Fund):
  Step 1: Return of contributed capital to LPs
  Step 2: Preferred return (hurdle) to LPs = contributed capital × hurdle_rate
  Step 3: GP catch-up (if defined) — GP gets 100% until GP has received carry_pct
           of (LP preferred return + catch-up)
  Step 4: Remaining profits split carry_pct to GP, (100 - carry_pct)% to LPs

For American (deal-by-deal) carry, each investment is evaluated independently.
"""

from decimal import Decimal
from datetime import date
from typing import Optional

from django.db import transaction
from django.utils import timezone


def _compute_irr(cash_flows: list) -> Optional[float]:
    """
    Simple IRR approximation using Newton-Raphson.
    cash_flows: list of (date, amount) tuples.
    Negative = outflow (invested), Positive = inflow (returned).
    Returns IRR as a float (0.15 = 15%) or None if not convergeable.
    """
    if len(cash_flows) < 2:
        return None
    try:
        # Convert to days-based fractional years from first cash flow
        t0 = cash_flows[0][0]
        tflows = [
            ((cf_date - t0).days / 365.0, float(amount))
            for cf_date, amount in cash_flows
        ]

        def npv(r):
            return sum(amt / ((1 + r) ** t) for t, amt in tflows)

        def dnpv(r):
            return sum(-t * amt / ((1 + r) ** (t + 1)) for t, amt in tflows)

        r = 0.15  # initial guess
        for _ in range(100):
            npv_val = npv(r)
            dnpv_val = dnpv(r)
            if abs(dnpv_val) < 1e-12:
                break
            r_new = r - npv_val / dnpv_val
            if abs(r_new - r) < 1e-8:
                r = r_new
                break
            r = r_new
            if r < -0.999:
                return None
        return round(r * 100, 4) if -100 < r * 100 < 10000 else None
    except Exception:
        return None


def compute_carry(scheme, as_of_date: Optional[date] = None):
    """
    Compute carried interest for a scheme as of a given date.

    Args:
        scheme: funds.Scheme instance
        as_of_date: Calculation date; defaults to today

    Returns:
        CarriedInterest instance (created or updated)
    """
    from accounting.models import CarriedInterest, FundLedger
    from investments.models import Investment, ExitEvent

    if as_of_date is None:
        as_of_date = timezone.now().date()

    hurdle_rate = Decimal(str(scheme.hurdle_rate_pct or 0)) / 100
    carry_pct = Decimal(str(scheme.carry_pct or 20)) / 100

    # -- 1. Total capital called (contributed) --
    total_called = Decimal('0')
    capital_call_entries = FundLedger.objects.filter(
        scheme=scheme,
        reference_type='capital_call',
        entry_date__lte=as_of_date,
        is_reversed=False,
    )
    for entry in capital_call_entries:
        total_called += entry.amount or Decimal('0')

    # -- 2. Total distributions paid to LPs --
    total_distributions = Decimal('0')
    distribution_entries = FundLedger.objects.filter(
        scheme=scheme,
        reference_type='distribution',
        entry_date__lte=as_of_date,
        is_reversed=False,
    )
    for entry in distribution_entries:
        total_distributions += entry.amount or Decimal('0')

    # -- 3. Current fair value of remaining portfolio --
    from investments.models import Valuation
    current_fv = Decimal('0')
    active_investments = scheme.investments.filter(
        status__in=['active', 'partially_exited'],
    )
    for inv in active_investments:
        latest_val = (
            inv.valuations
            .filter(status='approved', valuation_date__lte=as_of_date)
            .order_by('-valuation_date')
            .first()
        )
        if latest_val:
            fv = latest_val.fair_value_of_holding or latest_val.fair_value or Decimal('0')
            current_fv += fv

    # -- 4. Total fund value = distributions + remaining FV --
    total_fund_value = total_distributions + current_fv

    # -- 5. European waterfall --
    # Step 1: Return of capital
    after_return_of_capital = max(total_fund_value - total_called, Decimal('0'))

    # Step 2: Preferred return (simple interest proxy — actual funds use daily/compound)
    preferred_return_amount = total_called * hurdle_rate
    after_preferred = max(after_return_of_capital - preferred_return_amount, Decimal('0'))

    # Step 3 & 4: Carry calculation
    # Carry base = total profit above hurdle
    carry_base = max(after_preferred, Decimal('0'))
    carry_amount_gross = carry_base * carry_pct

    # Clawback: GP can't get carry if LPs haven't been fully returned
    # If total_fund_value < total_called + preferred_return, no carry yet
    lp_minimum_required = total_called + preferred_return_amount
    if total_fund_value <= lp_minimum_required:
        carry_amount_gross = Decimal('0')
        carry_base = Decimal('0')

    # -- 6. Clawback provision --
    # Previously paid carry that exceeds what GP is actually entitled to
    # (simplified: computed on actual distributions paid as carry)
    carry_actually_paid = Decimal('0')
    carry_paid_entries = FundLedger.objects.filter(
        scheme=scheme,
        reference_type='carried_interest',
        entry_date__lte=as_of_date,
        is_reversed=False,
    )
    for entry in carry_paid_entries:
        carry_actually_paid += entry.amount or Decimal('0')

    gp_clawback_provision = max(carry_actually_paid - carry_amount_gross, Decimal('0'))
    carry_amount_net = carry_amount_gross - gp_clawback_provision

    # -- 7. IRR computation for notes --
    # Build cash flows: calls = outflow, distributions = inflow
    cash_flows = []
    for entry in FundLedger.objects.filter(
        scheme=scheme,
        reference_type__in=['capital_call', 'distribution'],
        entry_date__lte=as_of_date,
        is_reversed=False,
    ).order_by('entry_date'):
        amt = entry.amount or Decimal('0')
        if entry.reference_type == 'capital_call':
            cash_flows.append((entry.entry_date, -amt))
        else:
            cash_flows.append((entry.entry_date, amt))
    # Add current NAV as terminal value
    if current_fv > 0:
        cash_flows.append((as_of_date, current_fv))

    irr = _compute_irr(cash_flows) if cash_flows else None

    notes = (
        f'Hurdle: {float(hurdle_rate*100):.2f}% | '
        f'Carry %: {float(carry_pct*100):.2f}% | '
        f'Total Called: {float(total_called):,.2f} | '
        f'Total Distributions: {float(total_distributions):,.2f} | '
        f'Current FV: {float(current_fv):,.2f} | '
        f'Net IRR: {irr:.2f}%' if irr is not None else 'Net IRR: N/A'
    )

    # -- 8. Persist --
    with transaction.atomic():
        carry_record, _ = CarriedInterest.objects.update_or_create(
            scheme=scheme,
            calculation_date=as_of_date,
            defaults={
                'total_distributions': total_distributions,
                'total_called_capital': total_called,
                'preferred_return_amount': preferred_return_amount,
                'carry_base': carry_base,
                'carry_amount_gross': carry_amount_gross,
                'carry_amount_net': max(carry_amount_net, Decimal('0')),
                'gp_clawback_provision': gp_clawback_provision,
                'notes': notes,
            },
        )

    return carry_record
