"""
NAV Engine — computes Net Asset Value for a scheme as of a given date.

Formula:
  Total NAV = investments_at_fair_value + cash_and_equivalents + receivables
              - management_fee_payable - other_liabilities

  NAV per unit = Total NAV / total_units_outstanding

Called after every import, valuation approval, and quarterly cycle.
"""

from decimal import Decimal
from datetime import date
from typing import Optional

from django.db import transaction
from django.utils import timezone


def compute_nav(scheme, as_of_date: Optional[date] = None):
    """
    Compute NAV for a scheme as of a given date and persist a NAVRecord.

    Uses the latest approved Valuation for each Investment to get
    fair_value_of_holding, and aggregates ledger entries for cash positions.

    Args:
        scheme: funds.Scheme instance
        as_of_date: Date of NAV calculation; defaults to today

    Returns:
        NAVRecord instance (created or updated)
    """
    from investments.models import Valuation
    from accounting.models import NAVRecord, FundLedger
    from investors.models import LPCommitment

    if as_of_date is None:
        as_of_date = timezone.now().date()

    # -- 1. Fair value of all active investments --
    # Use the latest approved Valuation on or before as_of_date per investment
    investments_fv = Decimal('0')
    active_investments = scheme.investments.filter(
        status__in=['active', 'partially_exited'],
    ).prefetch_related('valuations')

    for inv in active_investments:
        latest_val = (
            inv.valuations
            .filter(status='approved', valuation_date__lte=as_of_date)
            .order_by('-valuation_date')
            .first()
        )
        if latest_val:
            fv = latest_val.fair_value_of_holding or latest_val.fair_value or Decimal('0')
            investments_fv += fv

    # -- 2. Cash & equivalents from ledger --
    # Sum of all non-reversed ledger entries that hit Cash accounts (code 1000)
    ledger_entries = FundLedger.objects.filter(
        scheme=scheme,
        is_reversed=False,
        entry_date__lte=as_of_date,
    ).select_related('debit_account', 'credit_account')

    cash_balance = Decimal('0')
    mgmt_fee_payable = Decimal('0')
    other_liabilities = Decimal('0')

    for entry in ledger_entries:
        amt = entry.amount or Decimal('0')
        debit_code = entry.debit_account.account_code if entry.debit_account else ''
        credit_code = entry.credit_account.account_code if entry.credit_account else ''

        # Cash account (1000-1099)
        if debit_code.startswith('100'):
            cash_balance += amt
        if credit_code.startswith('100'):
            cash_balance -= amt

        # Management fee payable (2100-2199)
        if debit_code.startswith('210'):
            mgmt_fee_payable -= amt
        if credit_code.startswith('210'):
            mgmt_fee_payable += amt

        # Other liabilities (2000-2099 excl. mgmt fee)
        if credit_code.startswith('200'):
            other_liabilities += amt
        if debit_code.startswith('200'):
            other_liabilities -= amt

    cash_balance = max(cash_balance, Decimal('0'))

    # -- 3. Total NAV --
    total_nav = (
        investments_fv
        + cash_balance
        - abs(mgmt_fee_payable)
        - abs(other_liabilities)
    )
    total_nav = max(total_nav, Decimal('0'))

    # -- 4. Total units outstanding --
    # Sum of Commitment.units_allocated (units issued on drawdown) from lp app
    total_units = Decimal('0')
    try:
        from django.db.models import Sum
        total_units = (
            scheme.commitments
            .filter(status__in=['active', 'fully_called'])
            .aggregate(total=Sum('units_allocated'))['total']
        ) or Decimal('0')
    except Exception:
        total_units = Decimal('0')

    if total_units <= 0:
        nav_per_unit = Decimal('0')
    else:
        nav_per_unit = total_nav / total_units

    # -- 5. Persist NAVRecord --
    with transaction.atomic():
        nav_record, created = NAVRecord.objects.update_or_create(
            scheme=scheme,
            nav_date=as_of_date,
            defaults={
                'total_nav': total_nav,
                'total_units_outstanding': total_units,
                'nav_per_unit': nav_per_unit,
                'investments_at_fair_value': investments_fv,
                'cash_and_equivalents': cash_balance,
                'management_fee_payable': abs(mgmt_fee_payable),
                'other_liabilities': abs(other_liabilities),
            },
        )

    return nav_record
