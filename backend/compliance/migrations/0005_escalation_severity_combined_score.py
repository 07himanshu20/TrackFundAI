"""
Migration: Compliance v5 — EscalationLog model, severity on EquityThresholdAlert,
FundComplianceScore (combined fund-level score).
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('compliance', '0004_compliance_2_0_portfolio_obligations'),
        ('funds', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. Add severity to EquityThresholdAlert
        migrations.AddField(
            model_name='equitythresholdalert',
            name='severity',
            field=models.CharField(
                max_length=8,
                choices=[
                    ('urgent', 'URGENT — Breach >25%, immediate action required'),
                    ('high',   'HIGH — Breach >10%, action within 7 days'),
                    ('medium', 'MEDIUM — Breach approaching, monitor closely'),
                ],
                default='high',
                help_text='Auto-classified by stake_percentage at breach time',
            ),
        ),

        # 2. Add is_escalated to EquityThresholdAlert
        migrations.AddField(
            model_name='equitythresholdalert',
            name='is_escalated',
            field=models.BooleanField(
                default=False,
                help_text='Whether this breach has been escalated up the chain',
            ),
        ),

        # 3. EscalationLog — tracks each step in the GP→CFO→ComplianceOfficer chain
        migrations.CreateModel(
            name='EscalationLog',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('escalation_type', models.CharField(
                    max_length=30,
                    choices=[
                        ('equity_threshold_breach', 'Equity Threshold Breach'),
                        ('sebi_deadline_breach',    'SEBI Deadline Breach'),
                        ('ctr_overdue',             'CTR Overdue'),
                        ('aml_high_risk',           'AML High Risk Investor'),
                        ('fema_overdue',            'FEMA Filing Overdue'),
                        ('portfolio_non_compliant', 'Portfolio Company Non-Compliant'),
                        ('circular_action_overdue', 'Circular Action Overdue'),
                    ],
                    default='equity_threshold_breach',
                )),
                ('level', models.PositiveSmallIntegerField(
                    help_text='1=GP Partner, 2=CFO/Fund Accountant, 3=Compliance Officer',
                )),
                ('escalated_to_role', models.CharField(
                    max_length=30,
                    choices=[
                        ('gp_admin',           'GP Partner'),
                        ('fund_accountant',    'CFO / Fund Accountant'),
                        ('compliance_officer', 'Compliance Officer'),
                        ('platform_admin',     'Platform Admin'),
                    ],
                )),
                ('message', models.TextField(
                    help_text='Escalation message sent to the escalated-to role',
                )),
                ('resolved', models.BooleanField(default=False)),
                ('resolved_at', models.DateTimeField(null=True, blank=True)),
                ('resolution_notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),

                # Links to trigger objects (at most one will be non-null)
                ('equity_alert', models.ForeignKey(
                    to='compliance.EquityThresholdAlert',
                    on_delete=django.db.models.deletion.SET_NULL,
                    null=True, blank=True,
                    related_name='escalations',
                )),
                ('sebi_report', models.ForeignKey(
                    to='compliance.SEBIReport',
                    on_delete=django.db.models.deletion.SET_NULL,
                    null=True, blank=True,
                    related_name='escalations',
                )),
                ('circular_action', models.ForeignKey(
                    to='compliance.CircularAction',
                    on_delete=django.db.models.deletion.SET_NULL,
                    null=True, blank=True,
                    related_name='escalations',
                )),
                ('organization', models.ForeignKey(
                    to='accounts.Organization',
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='escalation_logs',
                )),
                ('escalated_by', models.ForeignKey(
                    to=settings.AUTH_USER_MODEL,
                    on_delete=django.db.models.deletion.SET_NULL,
                    null=True, blank=True,
                    related_name='escalations_triggered',
                )),
            ],
            options={'ordering': ['-created_at']},
        ),

        # 4. FundComplianceScore — combined fund-level 0-100 composite score
        migrations.CreateModel(
            name='FundComplianceScore',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('score_date', models.DateField()),
                # Sub-scores (each 0-100, weighted into combined)
                ('sebi_filing_score',       models.DecimalField(max_digits=5, decimal_places=2, default=100)),
                ('aml_score',               models.DecimalField(max_digits=5, decimal_places=2, default=100)),
                ('equity_threshold_score',  models.DecimalField(max_digits=5, decimal_places=2, default=100)),
                ('portfolio_company_score', models.DecimalField(max_digits=5, decimal_places=2, default=100)),
                ('circular_action_score',   models.DecimalField(max_digits=5, decimal_places=2, default=100)),
                # Combined (weighted average)
                ('combined_score', models.DecimalField(
                    max_digits=5, decimal_places=2, default=100,
                    help_text='Weighted composite: SEBI 30%, AML 20%, Equity 20%, Portfolio 20%, Circulars 10%',
                )),
                ('score_detail', models.JSONField(
                    default=dict,
                    help_text='Breakdown JSON — counts, reasons for deductions',
                )),
                ('computed_at', models.DateTimeField(auto_now_add=True)),
                ('fund', models.ForeignKey(
                    to='funds.Fund',
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='compliance_scores',
                )),
            ],
            options={
                'ordering': ['-score_date'],
                'unique_together': {('fund', 'score_date')},
            },
        ),

        # 5. Index on EscalationLog
        migrations.AddIndex(
            model_name='escalationlog',
            index=models.Index(
                fields=['organization', '-created_at'],
                name='esclog_org_created_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='escalationlog',
            index=models.Index(
                fields=['resolved', 'escalation_type'],
                name='esclog_resolved_type_idx',
            ),
        ),
    ]
