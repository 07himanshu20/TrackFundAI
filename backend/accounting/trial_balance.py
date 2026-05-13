"""
Trial Balance Generator — produces a trial balance from FundLedger entries.

A trial balance lists all accounts with their debit and credit balances
as of a given date. The sum of all debit balances must equal the sum of
all credit balances (double-entry accounting validation).

Output structure:
  {
    'as_of_date': '2025-03-31',
    'scheme_id': '...',
    'scheme_name': '...',
    'accounts': [
      {
        'account_code': '1000',
        'account_name': 'Cash and Bank',
        'account_type': 'asset',
        'debit_total': 5000000.00,
        'credit_total': 2000000.00,
        'net_balance': 3000000.00,
        'normal_balance': 'debit',
      },
      ...
    ],
    'total_debits': 10000000.00,
    'total_credits': 10000000.00,
    'is_balanced': True,
  }
"""

from collections import defaultdict
from decimal import Decimal
from datetime import date
from typing import Optional

from django.utils import timezone


def generate_trial_balance(scheme, as_of_date: Optional[date] = None) -> dict:
    """
    Generate a trial balance for a scheme as of a given date.

    Args:
        scheme: funds.Scheme instance
        as_of_date: Balance date; defaults to today

    Returns:
        dict with trial balance structure (described above)
    """
    from accounting.models import FundLedger

    if as_of_date is None:
        as_of_date = timezone.now().date()

    entries = FundLedger.objects.filter(
        scheme=scheme,
        entry_date__lte=as_of_date,
        is_reversed=False,
    ).select_related('debit_account', 'credit_account')

    # Accumulate debit/credit totals per account
    account_debits = defaultdict(Decimal)
    account_credits = defaultdict(Decimal)
    account_meta = {}

    for entry in entries:
        amt = entry.amount or Decimal('0')

        if entry.debit_account:
            acct = entry.debit_account
            account_debits[acct.id] += amt
            account_meta[acct.id] = {
                'account_code': acct.account_code,
                'account_name': acct.account_name,
                'account_type': acct.account_type,
            }

        if entry.credit_account:
            acct = entry.credit_account
            account_credits[acct.id] += amt
            account_meta[acct.id] = {
                'account_code': acct.account_code,
                'account_name': acct.account_name,
                'account_type': acct.account_type,
            }

    # Build account rows
    all_account_ids = set(account_debits.keys()) | set(account_credits.keys())
    rows = []

    for acct_id in all_account_ids:
        meta = account_meta[acct_id]
        debit_total = account_debits.get(acct_id, Decimal('0'))
        credit_total = account_credits.get(acct_id, Decimal('0'))
        net_balance = debit_total - credit_total

        # Normal balance convention
        acct_type = meta['account_type']
        normal_balance = 'debit' if acct_type in ('asset', 'expense') else 'credit'

        rows.append({
            'account_code': meta['account_code'],
            'account_name': meta['account_name'],
            'account_type': acct_type,
            'debit_total': float(debit_total),
            'credit_total': float(credit_total),
            'net_balance': float(abs(net_balance)),
            'normal_balance': normal_balance,
            'net_balance_side': 'debit' if net_balance >= 0 else 'credit',
        })

    # Sort by account code
    rows.sort(key=lambda x: x['account_code'])

    total_debits = sum(r['debit_total'] for r in rows)
    total_credits = sum(r['credit_total'] for r in rows)
    is_balanced = abs(total_debits - total_credits) < 0.01

    return {
        'as_of_date': as_of_date.isoformat(),
        'scheme_id': str(scheme.id),
        'scheme_name': str(scheme),
        'accounts': rows,
        'total_debits': round(total_debits, 2),
        'total_credits': round(total_credits, 2),
        'is_balanced': is_balanced,
        'variance': round(abs(total_debits - total_credits), 2),
    }
