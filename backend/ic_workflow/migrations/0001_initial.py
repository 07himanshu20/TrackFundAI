"""
Migration: IC Workflow — DealPipeline, ICPresentation, ICVote, ICDecision.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('accounts', '0005_user_v5_rbac_mfa_lockout'),
        ('funds', '0001_initial'),
        ('documents', '0001_initial'),
        ('investments', '0004_valuation_ipev_fields_kpidefinition_sector'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='DealPipeline',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('company_name', models.CharField(max_length=255)),
                ('sector', models.CharField(blank=True, max_length=100)),
                ('sub_sector', models.CharField(blank=True, max_length=100)),
                ('geography', models.CharField(default='India', max_length=100)),
                ('stage', models.CharField(
                    max_length=20, default='sourced',
                    choices=[
                        ('sourced', 'Sourced'), ('initial_screen', 'Initial Screen'),
                        ('deep_dive', 'Deep Dive'), ('term_sheet', 'Term Sheet'),
                        ('ic_presentation', 'IC Presentation'), ('approved', 'IC Approved'),
                        ('rejected', 'IC Rejected'), ('closed', 'Deal Closed'),
                        ('passed', 'Passed / No Action'),
                    ],
                )),
                ('proposed_investment_inr', models.DecimalField(decimal_places=2, max_digits=18, null=True, blank=True)),
                ('equity_stake_pct', models.DecimalField(decimal_places=3, max_digits=6, null=True, blank=True)),
                ('pre_money_valuation_inr', models.DecimalField(decimal_places=2, max_digits=18, null=True, blank=True)),
                ('sourced_date', models.DateField(null=True, blank=True)),
                ('source_channel', models.CharField(
                    max_length=30, default='inbound',
                    choices=[
                        ('network', 'Network Referral'), ('accelerator', 'Accelerator'),
                        ('inbound', 'Inbound'), ('scout', 'Scout'),
                        ('co_investor', 'Co-investor'), ('other', 'Other'),
                    ],
                )),
                ('executive_summary', models.TextField(blank=True)),
                ('rejection_reason', models.TextField(blank=True)),
                ('pass_reason', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='deal_pipeline',
                    to='accounts.organization',
                )),
                ('fund', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='deal_pipeline',
                    to='funds.fund',
                )),
                ('sourced_by', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='sourced_deals',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('linked_portfolio_company', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='deal_pipeline',
                    to='investments.portfoliocompany',
                )),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.AddIndex(
            model_name='dealpipeline',
            index=models.Index(fields=['organization', 'stage'], name='ic_deal_org_stage_idx'),
        ),

        migrations.CreateModel(
            name='ICPresentation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('presentation_date', models.DateField()),
                ('investment_thesis', models.TextField(blank=True)),
                ('key_risks', models.TextField(blank=True)),
                ('mitigants', models.TextField(blank=True)),
                ('recommended_valuation_inr', models.DecimalField(decimal_places=2, max_digits=18, null=True, blank=True)),
                ('quorum_required', models.PositiveIntegerField(default=3)),
                ('outcome', models.CharField(
                    max_length=15, default='pending',
                    choices=[
                        ('pending', 'Pending Vote'), ('approved', 'Approved'),
                        ('rejected', 'Rejected'), ('deferred', 'Deferred for More Info'),
                    ],
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('deal', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='presentations',
                    to='ic_workflow.dealpipeline',
                )),
                ('presenter', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='ic_presentations',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('memo_document', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='ic_presentations',
                    to='documents.document',
                )),
                ('deck_document', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='ic_decks',
                    to='documents.document',
                )),
            ],
            options={'ordering': ['-presentation_date']},
        ),

        migrations.CreateModel(
            name='ICVote',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('vote', models.CharField(
                    max_length=10,
                    choices=[('approve', 'Approve'), ('reject', 'Reject'),
                             ('abstain', 'Abstain'), ('defer', 'Defer')],
                )),
                ('comment', models.TextField(blank=True)),
                ('conditions', models.TextField(blank=True)),
                ('voted_at', models.DateTimeField(auto_now_add=True)),
                ('presentation', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='votes',
                    to='ic_workflow.icpresentation',
                )),
                ('voter', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='ic_votes',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['-voted_at'], 'unique_together': {('presentation', 'voter')}},
        ),

        migrations.CreateModel(
            name='ICDecision',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('decision', models.CharField(
                    max_length=15,
                    choices=[('approved', 'Approved'), ('rejected', 'Rejected'), ('deferred', 'Deferred')],
                )),
                ('decision_date', models.DateField()),
                ('approved_investment_inr', models.DecimalField(decimal_places=2, max_digits=18, null=True, blank=True)),
                ('approved_equity_stake_pct', models.DecimalField(decimal_places=3, max_digits=6, null=True, blank=True)),
                ('conditions', models.TextField(blank=True)),
                ('capital_call_triggered', models.BooleanField(default=False)),
                ('capital_call_date', models.DateField(null=True, blank=True)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('presentation', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='decision',
                    to='ic_workflow.icpresentation',
                )),
                ('decided_by', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='ic_decisions_made',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['-decision_date']},
        ),
    ]
