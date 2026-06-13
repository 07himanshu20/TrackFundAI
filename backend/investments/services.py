from decimal import Decimal
from datetime import date

from django.db import transaction
from django.db.models import Sum, Min

from .models import Investment, InvestmentTranche, Valuation


def reconcile_investment_from_tranches(investment_id, save=True):
    """Recompute Investment aggregate fields from its child Tranches and
    Valuations. Universal — works for 1 tranche or N. Idempotent.

    Aggregation rules:
      total_invested = SUM(tranche.amount)
      investment_date = MIN(tranche.date)
      stage          = round_name of the LATEST tranche (chronological)
      ownership_pct  = LATEST tranche's ownership_pct (proxy for current stake)
      current_value  = fair_value from the LATEST approved Valuation
    """
    try:
        inv = Investment.objects.get(pk=investment_id)
    except Investment.DoesNotExist:
        return None

    tranches = list(InvestmentTranche.objects.filter(investment=inv))
    if not tranches:
        return inv

    update_fields = []

    amount_sum = sum((t.amount for t in tranches if t.amount is not None),
                     start=Decimal('0'))
    if amount_sum > 0:
        inv.total_invested = amount_sum
        update_fields.append('total_invested')

    dates = [t.date for t in tranches if t.date]
    if dates:
        inv.investment_date = min(dates)
        update_fields.append('investment_date')

    dated_tranches = [t for t in tranches if t.date]
    if dated_tranches:
        latest = max(dated_tranches, key=lambda t: t.date)
        if latest.round_name:
            inv.stage = latest.round_name
            update_fields.append('stage')
        if latest.ownership_pct is not None:
            inv.ownership_pct = latest.ownership_pct
            update_fields.append('ownership_pct')
        if latest.fully_diluted_pct is not None:
            inv.percentage_stake_fully_diluted = latest.fully_diluted_pct
            update_fields.append('percentage_stake_fully_diluted')

    latest_val = (Valuation.objects
                  .filter(investment=inv, status='approved')
                  .order_by('-valuation_date').first())
    if latest_val and latest_val.fair_value is not None:
        if hasattr(inv, 'current_value'):
            inv.current_value = latest_val.fair_value
            update_fields.append('current_value')

    if save and update_fields:
        inv.save(update_fields=update_fields)
    return inv


def reconcile_all_investments(scheme=None, organization=None):
    """Bulk reconcile every Investment matching the filter. Returns count."""
    qs = Investment.objects.all()
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    if organization is not None:
        qs = qs.filter(scheme__fund__organization=organization)

    n = 0
    with transaction.atomic():
        for inv_id in qs.values_list('id', flat=True):
            reconcile_investment_from_tranches(inv_id)
            n += 1
    return n


def upsert_tranche_from_row(investment, *, natural_key, amount, tranche_date,
                            round_name='', instrument_type='',
                            ownership_pct=None, fully_diluted_pct=None,
                            shares=None, price_per_share=None):
    """Create or update one InvestmentTranche from a source-file row.

    natural_key is the file's stable row identifier (Co.ID when present,
    else a deterministic fingerprint built by the caller). It is what
    makes re-imports idempotent: the same source row lands on the same
    tranche each time, regardless of row order or whether other rows
    were added in between.
    """
    if not natural_key:
        natural_key = ''  # tolerate; fall back to tranche_number ordering

    defaults = {
        'date': tranche_date or date.today(),
        'round_name': round_name or '',
        'instrument_type': instrument_type or '',
    }
    if amount is not None:
        defaults['amount'] = amount
    if ownership_pct is not None:
        defaults['ownership_pct'] = ownership_pct
    if fully_diluted_pct is not None:
        defaults['fully_diluted_pct'] = fully_diluted_pct
    if shares is not None:
        defaults['shares_acquired'] = shares
    if price_per_share is not None:
        defaults['price_per_share'] = price_per_share

    if natural_key:
        existing = InvestmentTranche.objects.filter(
            investment=investment, natural_key=natural_key,
        ).first()
        if existing:
            for k, v in defaults.items():
                setattr(existing, k, v)
            existing.save()
            return existing

    next_number = (InvestmentTranche.objects
                   .filter(investment=investment)
                   .aggregate(m=Min('tranche_number'))['m'] or 0) + 1
    next_number = max(
        next_number,
        (InvestmentTranche.objects.filter(investment=investment).count() + 1),
    )
    if 'amount' not in defaults:
        defaults['amount'] = Decimal('0')
    return InvestmentTranche.objects.create(
        investment=investment,
        tranche_number=next_number,
        natural_key=natural_key,
        **defaults,
    )


def upsert_valuation_from_row(investment, *, valuation_date, methodology,
                              fair_value, source_tranche_key='',
                              cost_basis=None, unrealized=None,
                              fair_value_of_holding=None):
    """Create or update one Valuation tied to a specific tranche's row."""
    defaults = {
        'fair_value': fair_value if fair_value is not None else Decimal('0'),
        'methodology': methodology or 'cost',
        'status': 'approved',
        'source_tranche_key': source_tranche_key or '',
    }
    if cost_basis is not None:
        defaults['cost_basis'] = cost_basis
    if unrealized is not None:
        defaults['unrealized_gain_loss'] = unrealized
    if fair_value_of_holding is not None:
        defaults['fair_value_of_holding'] = fair_value_of_holding

    if source_tranche_key:
        existing = Valuation.objects.filter(
            investment=investment,
            source_tranche_key=source_tranche_key,
        ).first()
        if existing:
            for k, v in defaults.items():
                setattr(existing, k, v)
            existing.save()
            return existing

    val, _ = Valuation.objects.update_or_create(
        investment=investment,
        valuation_date=valuation_date or date.today(),
        methodology=defaults['methodology'],
        defaults=defaults,
    )
    return val
