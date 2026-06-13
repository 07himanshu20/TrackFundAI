from django.core.management.base import BaseCommand
from funds.models import Fund, Scheme

from investments.models import Investment, InvestmentTranche
from investments.services import (
    reconcile_investment_from_tranches, reconcile_all_investments,
)


class Command(BaseCommand):
    help = ('Recompute Investment aggregate fields (total_invested, '
            'investment_date, stage, current_value) from their child '
            'InvestmentTranche + Valuation rows.')

    def add_arguments(self, parser):
        parser.add_argument('--scheme', type=str, default=None,
                            help='Scheme UUID or name to limit reconciliation.')
        parser.add_argument('--fund', type=str, default=None,
                            help='Fund UUID or name to limit reconciliation.')

    def handle(self, *args, **opts):
        sch_filter = opts.get('scheme')
        fund_filter = opts.get('fund')

        scheme = None
        if sch_filter:
            try:
                scheme = Scheme.objects.get(pk=sch_filter)
            except (Scheme.DoesNotExist, ValueError, Exception):
                scheme = Scheme.objects.filter(name=sch_filter).first()
            if not scheme:
                self.stderr.write(f'Scheme not found: {sch_filter}')
                return
        elif fund_filter:
            fund = None
            try:
                fund = Fund.objects.get(pk=fund_filter)
            except (Fund.DoesNotExist, ValueError, Exception):
                fund = Fund.objects.filter(name=fund_filter).first()
            if not fund:
                self.stderr.write(f'Fund not found: {fund_filter}')
                return
            schemes = list(fund.schemes.all())
            total = 0
            for s in schemes:
                total += reconcile_all_investments(scheme=s)
            self.stdout.write(self.style.SUCCESS(
                f'Reconciled {total} investments across {len(schemes)} schemes'))
            return

        n = reconcile_all_investments(scheme=scheme)
        self.stdout.write(self.style.SUCCESS(f'Reconciled {n} investments'))
