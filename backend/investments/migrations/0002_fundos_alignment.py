# Hand-written migration — FundOS India alignment for Portfolio Monitoring module

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_initial'),
        ('investments', '0001_initial'),
    ]

    operations = [
        # ── PortfolioCompany (new model) ─────────────────────────────────
        migrations.CreateModel(
            name='PortfolioCompany',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('cin', models.CharField(blank=True, help_text='Corporate Identity Number (MCA India)', max_length=21)),
                ('pan', models.CharField(blank=True, help_text='PAN of the portfolio company', max_length=10)),
                ('sector', models.CharField(blank=True, max_length=100)),
                ('sub_sector', models.CharField(blank=True, max_length=100)),
                ('incorporation_date', models.DateField(blank=True, null=True)),
                ('headquarters_city', models.CharField(blank=True, max_length=100)),
                ('headquarters_country', models.CharField(default='India', max_length=100)),
                ('website', models.URLField(blank=True, max_length=500)),
                ('founder_names', models.JSONField(blank=True, default=list, help_text='List of founder names')),
                ('description', models.TextField(blank=True)),
                ('is_active', models.BooleanField(default=True)),
                ('portfolio_node_id', models.CharField(blank=True, help_text='Links to PortfolioNode.node_id in the dashboard hierarchy', max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='portfolio_companies', to='accounts.organization')),
            ],
            options={
                'ordering': ['name'],
                'verbose_name_plural': 'portfolio companies',
                'unique_together': {('organization', 'name')},
            },
        ),

        # ── Investment: add portfolio_company FK ─────────────────────────
        migrations.AddField(
            model_name='investment',
            name='portfolio_company',
            field=models.ForeignKey(
                blank=True, null=True,
                help_text='Link to master portfolio company record',
                on_delete=django.db.models.deletion.CASCADE,
                related_name='investments',
                to='investments.portfoliocompany',
            ),
        ),

        # ── Investment: add SEBI threshold fields ────────────────────────
        migrations.AddField(
            model_name='investment',
            name='percentage_stake_fully_diluted',
            field=models.DecimalField(blank=True, decimal_places=4, help_text='Ownership % on fully diluted basis', max_digits=8, null=True),
        ),
        migrations.AddField(
            model_name='investment',
            name='exceeds_10pct_threshold',
            field=models.BooleanField(db_index=True, default=False, help_text='SEBI: Auto-set when ownership >= 10% — requires custodian notification'),
        ),
        migrations.AddField(
            model_name='investment',
            name='threshold_breach_date',
            field=models.DateField(blank=True, help_text='SEBI: Date when 10% threshold was breached — T+30 = custodian notification deadline', null=True),
        ),

        # ── Investment: add governance / lifecycle fields ────────────────
        migrations.AddField(
            model_name='investment',
            name='is_lead_investor',
            field=models.BooleanField(default=False, help_text='Whether this fund is the lead investor in this round'),
        ),
        migrations.AddField(
            model_name='investment',
            name='write_off_date',
            field=models.DateField(blank=True, help_text='Date investment was written off (if applicable)', null=True),
        ),

        # ── Investment: expand instrument_type choices ───────────────────
        migrations.AlterField(
            model_name='investment',
            name='instrument_type',
            field=models.CharField(
                choices=[
                    ('equity', 'Equity'),
                    ('ccps', 'CCPS (Compulsorily Convertible Preference Shares)'),
                    ('ccd', 'CCD (Compulsorily Convertible Debentures)'),
                    ('ncd', 'NCD (Non-Convertible Debentures)'),
                    ('odi', 'ODI (Optionally Convertible Debentures)'),
                    ('safe', 'SAFE'),
                    ('convertible_note', 'Convertible Note'),
                    ('term_loan', 'Term Loan'),
                ],
                default='equity', max_length=20,
            ),
        ),

        # ── Valuation: add FundOS fields ─────────────────────────────────
        migrations.AddField(
            model_name='valuation',
            name='fair_value_of_holding',
            field=models.DecimalField(blank=True, decimal_places=2, help_text="FMV of fund's stake — drives NAV calculation", max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='valuation',
            name='enterprise_value',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Enterprise value of the portfolio company', max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='valuation',
            name='fvtpl_movement',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='SEBI: Ind AS 109 FVTPL (Fair Value Through Profit & Loss) movement', max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='valuation',
            name='valuer_name',
            field=models.CharField(blank=True, help_text='Name of the IBBI Registered Valuer', max_length=255),
        ),
        migrations.AddField(
            model_name='valuation',
            name='valuer_reg_number',
            field=models.CharField(blank=True, help_text='IBBI Registered Valuer registration number', max_length=50),
        ),

        # ── Valuation: expand methodology choices ────────────────────────
        migrations.AlterField(
            model_name='valuation',
            name='methodology',
            field=models.CharField(
                choices=[
                    ('dcf', 'Discounted Cash Flow'),
                    ('comparables', 'Market Comparables'),
                    ('recent_transaction', 'Recent Transaction'),
                    ('net_assets', 'Net Assets'),
                    ('cost', 'Cost (at cost)'),
                    ('option_pricing', 'Option Pricing Model'),
                ],
                max_length=20,
            ),
        ),

        # ── PortfolioKPI: add FundOS fields ──────────────────────────────
        migrations.AddField(
            model_name='portfoliokpi',
            name='portfolio_company',
            field=models.ForeignKey(
                blank=True, null=True,
                help_text='Direct link to portfolio company (denormalized for queries)',
                on_delete=django.db.models.deletion.CASCADE,
                related_name='kpis',
                to='investments.portfoliocompany',
            ),
        ),
        migrations.AddField(
            model_name='portfoliokpi',
            name='period_end_date',
            field=models.DateField(blank=True, help_text='Last day of the reporting period', null=True),
        ),
        migrations.AddField(
            model_name='portfoliokpi',
            name='source',
            field=models.CharField(
                choices=[
                    ('manual', 'Manual Entry'),
                    ('tally_import', 'Tally Import'),
                    ('api_integration', 'API Integration'),
                    ('excel_upload', 'Excel Upload'),
                ],
                default='manual',
                help_text='How this KPI value was captured',
                max_length=20,
            ),
        ),

        # ── PortfolioKPI: increase value precision ───────────────────────
        migrations.AlterField(
            model_name='portfoliokpi',
            name='value',
            field=models.DecimalField(decimal_places=4, help_text='High precision for ratios and large INR values', max_digits=22),
        ),

        # ── ExitEvent: add FundOS fields ─────────────────────────────────
        migrations.AddField(
            model_name='exitevent',
            name='gain_loss_nature',
            field=models.CharField(
                choices=[
                    ('ltcg', 'Long Term Capital Gain'),
                    ('stcg', 'Short Term Capital Gain'),
                    ('short_term_loss', 'Short Term Loss'),
                    ('long_term_loss', 'Long Term Loss'),
                    ('na', 'Not Applicable'),
                ],
                default='na',
                help_text='SEBI: Capital gains classification — LTCG/STCG determines TDS rate',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='exitevent',
            name='net_exit_proceeds',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Net proceeds after transaction costs', max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='exitevent',
            name='exit_multiple',
            field=models.DecimalField(blank=True, decimal_places=4, help_text='MoIC on this specific exit event', max_digits=8, null=True),
        ),
        migrations.AddField(
            model_name='exitevent',
            name='irr_on_exit',
            field=models.DecimalField(blank=True, decimal_places=4, help_text='IRR realised at exit', max_digits=8, null=True),
        ),
    ]
