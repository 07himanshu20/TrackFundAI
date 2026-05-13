"""
Migration: Fund Close — FundCloseEvent, ClawbackCalculation, SEBIDeregistration.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid
from decimal import Decimal


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('accounts', '0005_user_v5_rbac_mfa_lockout'),
        ('funds', '0001_initial'),
        ('documents', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='FundCloseEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(
                    max_length=20, default='initiated',
                    choices=[
                        ('initiated', 'Close Initiated'),
                        ('final_accounts', 'Final Accounts Being Prepared'),
                        ('carry_calc', 'Carry & Clawback Calculation'),
                        ('lp_distribution', 'LP Final Distribution'),
                        ('sebi_filing', 'SEBI Deregistration Filing'),
                        ('deregistered', 'SEBI Deregistered'),
                        ('closed', 'Fund Closed'),
                    ],
                )),
                ('initiation_date', models.DateField()),
                ('target_close_date', models.DateField(null=True, blank=True)),
                ('actual_close_date', models.DateField(null=True, blank=True)),
                ('final_nav_inr', models.DecimalField(decimal_places=2, max_digits=18, null=True, blank=True)),
                ('total_invested_inr', models.DecimalField(decimal_places=2, max_digits=18, null=True, blank=True)),
                ('total_realized_inr', models.DecimalField(decimal_places=2, max_digits=18, null=True, blank=True)),
                ('final_moic', models.DecimalField(decimal_places=3, max_digits=6, null=True, blank=True)),
                ('final_irr_pct', models.DecimalField(decimal_places=3, max_digits=6, null=True, blank=True)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('fund', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='close_events',
                    to='funds.fund',
                )),
                ('scheme', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='close_events',
                    to='funds.scheme',
                )),
                ('initiated_by', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['-initiation_date'], 'unique_together': {('fund', 'scheme')}},
        ),

        migrations.CreateModel(
            name='ClawbackCalculation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('calc_date', models.DateField()),
                ('total_committed_capital_inr', models.DecimalField(decimal_places=2, max_digits=18)),
                ('total_drawn_capital_inr', models.DecimalField(decimal_places=2, max_digits=18)),
                ('total_distributions_inr', models.DecimalField(decimal_places=2, max_digits=18)),
                ('hurdle_rate_pct', models.DecimalField(decimal_places=2, max_digits=5, default=Decimal('8.00'))),
                ('carry_rate_pct', models.DecimalField(decimal_places=2, max_digits=5, default=Decimal('20.00'))),
                ('return_of_capital_inr', models.DecimalField(decimal_places=2, max_digits=18, default=0)),
                ('preferred_return_inr', models.DecimalField(decimal_places=2, max_digits=18, default=0)),
                ('profit_above_hurdle_inr', models.DecimalField(decimal_places=2, max_digits=18, default=0)),
                ('gp_carry_owed_inr', models.DecimalField(decimal_places=2, max_digits=18, default=0)),
                ('gp_carry_paid_inr', models.DecimalField(decimal_places=2, max_digits=18, default=0)),
                ('clawback_amount_inr', models.DecimalField(decimal_places=2, max_digits=18, default=0)),
                ('clawback_direction', models.CharField(
                    max_length=10, default='none',
                    choices=[('gp_owes', 'GP Owes LPs'), ('none', 'No Clawback'), ('lp_owes', 'LP Topup')],
                )),
                ('settled', models.BooleanField(default=False)),
                ('settled_date', models.DateField(null=True, blank=True)),
                ('settlement_notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('close_event', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='clawback',
                    to='fundclose.fundcloseevent',
                )),
                ('calculated_by', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['-calc_date']},
        ),

        migrations.CreateModel(
            name='SEBIDeregistration',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(
                    max_length=20, default='not_started',
                    choices=[
                        ('not_started', 'Not Started'),
                        ('noc_obtained', 'NOC from LPs Obtained'),
                        ('sebi_application', 'Application Filed with SEBI'),
                        ('sebi_review', 'SEBI Under Review'),
                        ('approved', 'SEBI Approved Deregistration'),
                        ('completed', 'Deregistration Complete'),
                        ('rejected', 'SEBI Rejected — Resubmit'),
                    ],
                )),
                ('noc_date', models.DateField(null=True, blank=True)),
                ('application_date', models.DateField(null=True, blank=True)),
                ('sebi_acknowledgement_no', models.CharField(blank=True, max_length=50)),
                ('sebi_approval_date', models.DateField(null=True, blank=True)),
                ('sebi_certificate_surrender_date', models.DateField(null=True, blank=True)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('close_event', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='sebi_deregistration',
                    to='fundclose.fundcloseevent',
                )),
                ('final_accounts_document', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='sebi_deregistration_final_accounts',
                    to='documents.document',
                )),
                ('application_document', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='sebi_deregistration_applications',
                    to='documents.document',
                )),
                ('compliance_officer', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]
