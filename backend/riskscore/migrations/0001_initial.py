from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('investments', '0004_valuation_ipev_fields_kpidefinition_sector'),
    ]

    operations = [
        migrations.CreateModel(
            name='CompanyRiskScore',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('score_date', models.DateField(help_text='Date of risk score computation')),
                ('risk_score', models.DecimalField(
                    max_digits=5, decimal_places=2,
                    help_text='Composite risk score 0-100 (higher = more risk)',
                )),
                ('risk_tier', models.CharField(
                    max_length=8,
                    choices=[
                        ('low', 'LOW (0-33)'),
                        ('medium', 'MEDIUM (34-66)'),
                        ('high', 'HIGH (67-100)'),
                    ],
                )),
                ('method', models.CharField(
                    max_length=12, default='rule_based',
                    choices=[
                        ('rule_based', 'Rule-Based (Phase 1)'),
                        ('xgboost', 'XGBoost Ensemble (Phase 2)'),
                    ],
                )),
                # 10 signal scores (0-10 each)
                ('signal_revenue_vs_plan', models.DecimalField(max_digits=4, decimal_places=2, default=0)),
                ('signal_ebitda_margin_trend', models.DecimalField(max_digits=4, decimal_places=2, default=0)),
                ('signal_cash_burn_runway', models.DecimalField(max_digits=4, decimal_places=2, default=0)),
                ('signal_working_capital', models.DecimalField(max_digits=4, decimal_places=2, default=0)),
                ('signal_debt_service', models.DecimalField(max_digits=4, decimal_places=2, default=0)),
                ('signal_customer_concentration', models.DecimalField(max_digits=4, decimal_places=2, default=0)),
                ('signal_mgmt_changes', models.DecimalField(max_digits=4, decimal_places=2, default=0)),
                ('signal_market_conditions', models.DecimalField(max_digits=4, decimal_places=2, default=0)),
                ('signal_peer_comparisons', models.DecimalField(max_digits=4, decimal_places=2, default=0)),
                ('signal_compliance_status', models.DecimalField(max_digits=4, decimal_places=2, default=0)),
                ('flags', models.JSONField(
                    default=list, blank=True,
                    help_text='List of risk flags e.g. ["Cash runway < 6 months"]',
                )),
                ('ai_commentary', models.TextField(
                    blank=True,
                    help_text='Gemini-generated natural language risk summary',
                )),
                ('previous_score', models.DecimalField(
                    max_digits=5, decimal_places=2, null=True, blank=True,
                    help_text='Previous risk score (for trend display)',
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('portfolio_company', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='risk_scores',
                    to='investments.portfoliocompany',
                )),
            ],
            options={'ordering': ['portfolio_company', '-score_date']},
        ),
        migrations.AlterUniqueTogether(
            name='companyriskscore',
            unique_together={('portfolio_company', 'score_date')},
        ),
        migrations.AddIndex(
            model_name='companyriskscore',
            index=models.Index(fields=['risk_tier', 'score_date'], name='riskscore_tier_date_idx'),
        ),
    ]
