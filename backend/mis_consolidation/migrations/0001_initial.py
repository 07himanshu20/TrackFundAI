"""
Migration: MIS Consolidation — BudgetVsActual, ConsolidatedMIS, MISAnomalyAlert.
"""
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('accounts', '0005_user_v5_rbac_mfa_lockout'),
        ('funds', '0001_initial'),
        ('investments', '0004_valuation_ipev_fields_kpidefinition_sector'),
    ]

    operations = [
        migrations.CreateModel(
            name='BudgetVsActual',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('period_year', models.PositiveIntegerField()),
                ('period_month', models.PositiveIntegerField(null=True, blank=True)),
                ('period_quarter', models.CharField(
                    blank=True, max_length=2,
                    choices=[('Q1', 'Q1'), ('Q2', 'Q2'), ('Q3', 'Q3'), ('Q4', 'Q4')],
                )),
                ('period_type', models.CharField(
                    max_length=10, default='monthly',
                    choices=[('monthly', 'Monthly'), ('quarterly', 'Quarterly'), ('annual', 'Annual')],
                )),
                ('line_item', models.CharField(max_length=30, choices=[
                    ('revenue', 'Revenue / Net Sales'), ('other_income', 'Other Income'),
                    ('total_revenue', 'Total Revenue'), ('cogs', 'Cost of Goods Sold (COGS)'),
                    ('gross_profit', 'Gross Profit'), ('employee_cost', 'Employee / Payroll Cost'),
                    ('marketing_cost', 'Marketing & Sales Cost'), ('rd_cost', 'R&D Cost'),
                    ('g_and_a', 'G&A / Overheads'), ('total_opex', 'Total Operating Expenses'),
                    ('ebitda', 'EBITDA'), ('depreciation', 'Depreciation & Amortisation'),
                    ('ebit', 'EBIT'), ('finance_cost', 'Finance Cost / Interest'),
                    ('pbt', 'Profit Before Tax (PBT)'), ('tax', 'Tax (Current + Deferred)'),
                    ('pat', 'Profit After Tax (PAT)'), ('total_assets', 'Total Assets'),
                    ('total_debt', 'Total Debt'), ('cash_and_equivalents', 'Cash & Equivalents'),
                    ('net_worth', 'Net Worth / Equity'),
                ])),
                ('budget_inr', models.DecimalField(decimal_places=2, max_digits=18, null=True, blank=True)),
                ('actual_inr', models.DecimalField(decimal_places=2, max_digits=18, null=True, blank=True)),
                ('variance_inr', models.DecimalField(decimal_places=2, max_digits=18, null=True, blank=True)),
                ('variance_pct', models.DecimalField(decimal_places=3, max_digits=8, null=True, blank=True)),
                ('is_favorable', models.BooleanField(null=True, blank=True)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('portfolio_company', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='bva_records',
                    to='investments.portfoliocompany',
                )),
            ],
            options={'ordering': ['-period_year', '-period_month', 'portfolio_company'],
                     'unique_together': {('portfolio_company', 'period_year', 'period_month', 'period_quarter', 'line_item')}},
        ),
        migrations.AddIndex(
            model_name='budgetvsactual',
            index=models.Index(fields=['portfolio_company', 'period_year', 'period_month'], name='bva_company_period_idx'),
        ),

        migrations.CreateModel(
            name='ConsolidatedMIS',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('period_year', models.PositiveIntegerField()),
                ('period_month', models.PositiveIntegerField(null=True, blank=True)),
                ('period_quarter', models.CharField(blank=True, max_length=2)),
                ('period_type', models.CharField(
                    max_length=10, default='monthly',
                    choices=[('monthly', 'Monthly'), ('quarterly', 'Quarterly'), ('annual', 'Annual')],
                )),
                ('line_item', models.CharField(max_length=30)),
                ('total_actual_inr', models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ('total_budget_inr', models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ('company_count', models.IntegerField(default=0)),
                ('total_variance_inr', models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ('total_variance_pct', models.DecimalField(decimal_places=3, max_digits=8, null=True, blank=True)),
                ('computed_at', models.DateTimeField(auto_now_add=True)),
                ('organization', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='consolidated_mis',
                    to='accounts.organization',
                )),
                ('fund', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='consolidated_mis',
                    to='funds.fund',
                )),
                ('scheme', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='consolidated_mis',
                    to='funds.scheme',
                )),
            ],
            options={'ordering': ['-period_year', '-period_month', 'line_item'],
                     'unique_together': {('fund', 'scheme', 'period_year', 'period_month', 'period_quarter', 'line_item')}},
        ),

        migrations.CreateModel(
            name='MISAnomalyAlert',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('anomaly_type', models.CharField(
                    max_length=25,
                    choices=[
                        ('budget_variance', 'Budget Variance Exceeded Threshold'),
                        ('revenue_decline', 'Revenue Decline >20% MoM'),
                        ('cash_burn', 'Cash Burn Acceleration'),
                        ('ebitda_compression', 'EBITDA Margin Compression'),
                        ('statistical_outlier', 'Statistical Outlier (Z-score >2)'),
                    ],
                )),
                ('severity', models.CharField(
                    max_length=10,
                    choices=[('critical', 'Critical (>50% variance)'), ('high', 'High (25-50% variance)'),
                             ('medium', 'Medium (10-25% variance)'), ('low', 'Low (<10% variance)')],
                )),
                ('description', models.TextField()),
                ('detected_at', models.DateTimeField(auto_now_add=True)),
                ('resolved', models.BooleanField(default=False)),
                ('resolved_at', models.DateTimeField(null=True, blank=True)),
                ('notification_sent', models.BooleanField(default=False)),
                ('bva_record', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='anomaly_alerts',
                    to='mis_consolidation.budgetvsactual',
                )),
                ('portfolio_company', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='mis_anomalies',
                    to='investments.portfoliocompany',
                )),
            ],
            options={'ordering': ['-detected_at']},
        ),
    ]
