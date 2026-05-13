"""
Fee Engine — computes management fees for a scheme for a given period.

Supports three fee basis types (from Scheme.management_fee_basis):
  - 'committed': fee on committed capital (stable, predictable)
  - 'called':    fee on called/drawn capital (invested capital)
  - 'nav':       fee on scheme NAV (mark-to-market basis)

GST at 18% is applied on top of the management fee (Indian AIF standard).
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import date

from django.db import transaction


GST_RATE = Decimal('0.18')


def _days_in_period(period_start: date, period_end: date) -> int:
    return (period_end - period_start).days + 1


def _annual_fee_to_period(annual_rate: Decimal, period_start: date, period_end: date) -> Decimal:
    """Pro-rate annual fee rate to the period length (actual/365 basis)."""
    days = _days_in_period(period_start, period_end)
    return annual_rate * Decimal(days) / Decimal('365')


def compute_management_fee(scheme, period_start: date, period_end: date):
    """
    Compute and persist the management fee for a scheme for a period.

    Args:
        scheme: funds.Scheme instance
        period_start: First day of billing period
        period_end:   Last day of billing period

    Returns:
        ManagementFeeSchedule instance (created or updated)
    """
    from accounting.models import ManagementFeeSchedule, NAVRecord, FundLedger

    annual_rate = Decimal(str(scheme.management_fee_pct or 0)) / 100
    fee_basis = scheme.management_fee_basis or 'committed'

    # -- 1. Determine the fee basis amount --
    if fee_basis == 'committed':
        # Total committed capital = sum of LP Commitments for this scheme (lp app)
        try:
            from django.db.models import Sum
            committed = (
                scheme.commitments
                .filter(status__in=['active', 'fully_called', 'partially_called'])
                .aggregate(total=Sum('commitment_amount'))['total']
            ) or Decimal('0')
        except Exception:
            committed = scheme.scheme_size or Decimal('0')
        fee_basis_amount = Decimal(str(committed))

    elif fee_basis == 'called':
        # Total called capital = sum of all capital call ledger entries
        called_entries = FundLedger.objects.filter(
            scheme=scheme,
            reference_type='capital_call',
            entry_date__lte=period_end,
            is_reversed=False,
        )
        fee_basis_amount = sum(
            (e.amount or Decimal('0')) for e in called_entries
        )

    else:  # nav
        # Latest NAV on or before period_end
        latest_nav = (
            NAVRecord.objects.filter(
                scheme=scheme,
                nav_date__lte=period_end,
            )
            .order_by('-nav_date')
            .first()
        )
        fee_basis_amount = Decimal(str(latest_nav.total_nav)) if latest_nav else Decimal('0')

    # -- 2. Pro-rate the annual fee to the period --
    period_rate = _annual_fee_to_period(annual_rate, period_start, period_end)
    fee_amount = (fee_basis_amount * period_rate).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP
    )

    # -- 3. GST --
    gst_amount = (fee_amount * GST_RATE).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP
    )
    total_fee_with_gst = fee_amount + gst_amount

    # -- 4. Persist --
    with transaction.atomic():
        fee_schedule, _ = ManagementFeeSchedule.objects.update_or_create(
            scheme=scheme,
            period_start=period_start,
            period_end=period_end,
            defaults={
                'fee_basis_amount': fee_basis_amount,
                'fee_rate': Decimal(str(scheme.management_fee_pct or 0)),
                'fee_amount': fee_amount,
                'gst_amount': gst_amount,
                'total_fee_with_gst': total_fee_with_gst,
                'fee_status': 'calculated',
            },
        )

    return fee_schedule
