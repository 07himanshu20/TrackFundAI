"""
Management command: seed_sector_kpis
Seeds KPIDefinition system KPIs for all organizations (or a specific one).
v5 sector KPI library: SaaS, Healthcare, Manufacturing, NBFC, Generic.

Usage:
  python manage.py seed_sector_kpis                    # seed for ALL orgs
  python manage.py seed_sector_kpis --org=<slug>       # seed for specific org
  python manage.py seed_sector_kpis --clear            # clear system KPIs first
"""
from django.core.management.base import BaseCommand
from django.utils.text import slugify
from investments.models import KPIDefinition
from accounts.models import Organization

# (name, display_name, sector, format, frequency, unit_hint, sort_order, description)
KPI_LIBRARY = [
    # ── SaaS ──────────────────────────────────────
    ('ARR', 'Annual Recurring Revenue', 'saas', 'currency', 'monthly', 1,
     'Total annualized revenue from active subscriptions. Formula: MRR × 12'),
    ('MRR', 'Monthly Recurring Revenue', 'saas', 'currency', 'monthly', 2,
     'Predictable monthly subscription revenue. Benchmark: MoM growth > 5%'),
    ('NRR', 'Net Revenue Retention %', 'saas', 'percent', 'monthly', 3,
     '(Opening MRR + Expansion − Churn − Contraction) / Opening MRR × 100. >120% = best-in-class'),
    ('Churn_Rate', 'Monthly Churn Rate %', 'saas', 'percent', 'monthly', 4,
     'Customers/revenue lost per month. <2% monthly = world-class SaaS'),
    ('CAC', 'Customer Acquisition Cost', 'saas', 'currency', 'quarterly', 5,
     '(Sales + Marketing Spend) / New Customers. LTV/CAC > 3× = healthy'),
    ('LTV', 'Customer Lifetime Value', 'saas', 'currency', 'quarterly', 6,
     'ARPU / Monthly Churn Rate. LTV > 3× CAC target'),
    ('LTV_CAC_Ratio', 'LTV / CAC Ratio', 'saas', 'ratio', 'quarterly', 7,
     '>3× healthy; >5× exceptional. Formula: LTV / CAC'),
    ('CAC_Payback', 'CAC Payback Period (months)', 'saas', 'number', 'quarterly', 8,
     'CAC / (ARPU × Gross Margin%). <12 months = world class'),
    ('Gross_Margin_SaaS', 'Gross Margin %', 'saas', 'percent', 'monthly', 9,
     '(Revenue − COGS) / Revenue × 100. >70% = SaaS benchmark'),

    # ── Healthcare ──────────────────────────────────
    ('ARPOB', 'Avg Revenue Per Occupied Bed (₹/day)', 'healthcare', 'currency', 'monthly', 1,
     'Revenue / Occupied Bed Days. Tier-1 hospitals: ₹15,000+/day target'),
    ('Bed_Occupancy', 'Bed Occupancy Rate %', 'healthcare', 'percent', 'monthly', 2,
     'Occupied Bed Days / Available Bed Days × 100. >75% = efficient'),
    ('ALOS', 'Avg Length of Stay (days)', 'healthcare', 'number', 'monthly', 3,
     'Total Inpatient Days / Total Discharges. 3-5 days for general hospitals'),
    ('OPD_Footfall', 'OPD Footfall (patients/month)', 'healthcare', 'number', 'monthly', 4,
     'Total OPD visits. YoY growth >15% = strong demand'),
    ('EBITDA_Per_Bed', 'EBITDA Per Bed (₹L/year)', 'healthcare', 'currency', 'annual', 5,
     'Annual EBITDA / Total Beds. ₹15-25L/bed/year = multi-specialty target'),
    ('Surgical_Volume', 'Monthly Surgical Volume', 'healthcare', 'number', 'monthly', 6,
     'Total surgical procedures. YoY growth >10%'),

    # ── Manufacturing ────────────────────────────────
    ('Capacity_Utilisation', 'Capacity Utilisation %', 'manufacturing', 'percent', 'monthly', 1,
     'Actual Output / Installed Capacity × 100. >80% = efficient'),
    ('Order_Book', 'Order Book Value (₹ Cr)', 'manufacturing', 'currency', 'monthly', 2,
     'Total unexecuted confirmed orders. >3× annual revenue = strong visibility'),
    ('Inventory_Turnover', 'Inventory Turnover Ratio', 'manufacturing', 'ratio', 'quarterly', 3,
     'COGS / Average Inventory. >6× for most manufacturing'),
    ('ROCE', 'Return on Capital Employed %', 'manufacturing', 'percent', 'annual', 4,
     'EBIT / (Total Assets − Current Liabilities) × 100. >15% target'),
    ('Gross_Margin_Mfg', 'Gross Margin % (Manufacturing)', 'manufacturing', 'percent', 'monthly', 5,
     '(Revenue − COGS) / Revenue × 100. >30% specialty; >20% commodity'),
    ('Debtor_Days', 'Debtor Days (DSO)', 'manufacturing', 'number', 'monthly', 6,
     'Receivables / Revenue × 365. <45 days = healthy'),

    # ── NBFC ─────────────────────────────────────────
    ('AUM', 'Assets Under Management (₹ Cr)', 'nbfc', 'currency', 'monthly', 1,
     'Gross Loan Outstanding. YoY growth >20% = healthy'),
    ('NIM', 'Net Interest Margin %', 'nbfc', 'percent', 'quarterly', 2,
     '(Interest Income − Interest Expense) / Avg Earning Assets × 100. >4% target'),
    ('Gross_NPA', 'Gross NPA %', 'nbfc', 'percent', 'quarterly', 3,
     'Gross NPA / Gross Advances × 100. <2% = strong; <5% = acceptable'),
    ('Net_NPA', 'Net NPA %', 'nbfc', 'percent', 'quarterly', 4,
     '(Gross NPA − Provisions) / Net Advances × 100. <1% = strong'),
    ('PCR', 'Provision Coverage Ratio %', 'nbfc', 'percent', 'quarterly', 5,
     'Cumulative Provisions / Gross NPA × 100. >70% = well-provisioned (RBI guideline)'),
    ('CAR', 'Capital Adequacy Ratio %', 'nbfc', 'percent', 'quarterly', 6,
     '(Tier 1 + Tier 2 Capital) / RWA × 100. >15% (RBI minimum for NBFCs)'),
    ('ROA_NBFC', 'Return on Assets %', 'nbfc', 'percent', 'annual', 7,
     'PAT / Avg Total Assets × 100. >2% = healthy NBFC'),
    ('ROE_NBFC', 'Return on Equity %', 'nbfc', 'percent', 'annual', 8,
     'PAT / Avg Net Worth × 100. >15% = strong'),

    # ── Generic ──────────────────────────────────────
    ('Revenue_Growth_YoY', 'Revenue Growth YoY %', 'generic', 'percent', 'annual', 1,
     '(Current Revenue − Prior Revenue) / Prior Revenue × 100. >20% = strong'),
    ('EBITDA_Margin', 'EBITDA Margin %', 'generic', 'percent', 'monthly', 2,
     'EBITDA / Revenue × 100. >20% = healthy for most sectors'),
    ('Debt_Equity', 'Debt / Equity Ratio', 'generic', 'ratio', 'quarterly', 3,
     'Total Debt / Net Worth. <1× conservative; <2× acceptable; >3× high leverage'),
    ('Current_Ratio', 'Current Ratio', 'generic', 'ratio', 'quarterly', 4,
     'Current Assets / Current Liabilities. >1.5× healthy; <1× = liquidity stress'),
    ('Cash_Runway', 'Cash Runway (months)', 'generic', 'number', 'monthly', 5,
     'Cash Balance / Monthly Burn. >18 months = comfortable; <6 months = critical'),
    ('Headcount', 'Total Headcount', 'generic', 'number', 'monthly', 6,
     'Full-time employees. Track growth rate and revenue per employee'),
    ('Revenue_Per_Employee', 'Revenue Per Employee (₹L)', 'generic', 'currency', 'annual', 7,
     'Annual Revenue / Total Headcount. Productivity metric'),
]

# Map sector_template names to KPIDefinition.SECTOR_TEMPLATE_CHOICES keys
SECTOR_MAP = {
    'saas': 'saas',
    'healthcare': 'healthcare',
    'manufacturing': 'manufacturing',
    'nbfc': 'nbfc',
    'generic': 'generic',
}

FORMAT_MAP = {
    'currency': 'currency',
    'percent': 'percent',
    'ratio': 'ratio',
    'number': 'number',
}


class Command(BaseCommand):
    help = 'Seed KPIDefinition system KPIs (v5 library: SaaS, Healthcare, Manufacturing, NBFC, Generic)'

    def add_arguments(self, parser):
        parser.add_argument('--org', type=str, default=None,
                            help='Organization slug to seed (default: all organizations)')
        parser.add_argument('--clear', action='store_true',
                            help='Delete existing system KPIs before seeding')

    def handle(self, *args, **options):
        orgs = Organization.objects.filter(is_active=True)
        if options['org']:
            orgs = orgs.filter(slug=options['org'])
            if not orgs.exists():
                self.stdout.write(self.style.ERROR(f"Organization '{options['org']}' not found."))
                return

        if options['clear']:
            deleted = KPIDefinition.objects.filter(is_system_kpi=True).delete()
            self.stdout.write(self.style.WARNING(f'Cleared {deleted[0]} system KPIs.'))

        total_created = total_updated = 0
        for org in orgs:
            created, updated = self._seed_for_org(org)
            total_created += created
            total_updated += updated
            self.stdout.write(f'  {org.name}: {created} created, {updated} updated')

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. Total: {total_created} created, {total_updated} updated '
            f'across {orgs.count()} organization(s).'
        ))

    def _seed_for_org(self, org):
        created = updated = 0
        for idx, kpi in enumerate(KPI_LIBRARY):
            name, display_name, sector, fmt, freq, sort_order, description = kpi
            slug = slugify(name)

            obj, was_created = KPIDefinition.objects.update_or_create(
                organization=org,
                slug=slug,
                defaults={
                    'name': display_name,
                    'description': description,
                    'format': FORMAT_MAP.get(fmt, 'number'),
                    'frequency': freq,
                    'sector_template': SECTOR_MAP.get(sector, 'generic'),
                    'sort_order': sort_order + (idx * 100),  # Ensure global ordering
                    'is_system_kpi': True,
                    'is_required': False,
                    'is_active': True,
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1
        return created, updated
