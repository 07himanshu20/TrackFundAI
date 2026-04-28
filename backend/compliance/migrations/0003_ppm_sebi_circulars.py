"""
Migration 0003 — Compliance: Add PPMAmendment, SEBICircular, CircularAction.

Maps to FundOS: ppm_amendments, sebi_circulars, circular_actions tables.
"""

import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('compliance', '0002_initial'),
        ('funds', '0002_fundos_alignment'),
        ('accounts', '0003_fundos_alignment'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [

        # ── PPMAmendment ─────────────────────────────────────────────────
        migrations.CreateModel(
            name='PPMAmendment',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('amendment_number', models.PositiveIntegerField(help_text='Sequential amendment number (Amendment 1, 2, 3...)')),
                ('amendment_type', models.CharField(
                    choices=[
                        ('investment_strategy', 'Investment Strategy Change'),
                        ('fee_structure', 'Fee Structure Change'),
                        ('key_personnel', 'Key Personnel Change'),
                        ('scheme_tenure', 'Scheme Tenure Change'),
                        ('corpus_limit', 'Target Corpus Change'),
                        ('investment_restrictions', 'Investment Restrictions Change'),
                        ('distribution_policy', 'Distribution Policy Change'),
                        ('other', 'Other Material Change'),
                    ],
                    max_length=30,
                )),
                ('title', models.CharField(max_length=255)),
                ('description', models.TextField()),
                ('board_approval_date', models.DateField(blank=True, null=True)),
                ('trustee_approval_date', models.DateField(blank=True, null=True)),
                ('sebi_filing_date', models.DateField(blank=True, null=True)),
                ('investor_notification_date', models.DateField(blank=True, null=True)),
                ('effective_date', models.DateField(blank=True, null=True)),
                ('investor_exit_window_days', models.PositiveIntegerField(default=30)),
                ('investor_exit_window_expiry', models.DateField(blank=True, null=True)),
                ('approval_status', models.CharField(
                    choices=[
                        ('draft', 'Draft'),
                        ('under_review', 'Under Review'),
                        ('trustee_approved', 'Trustee Approved'),
                        ('sebi_filed', 'Filed with SEBI'),
                        ('investor_notified', 'Investors Notified'),
                        ('effective', 'Effective'),
                    ],
                    default='draft',
                    max_length=20,
                )),
                ('sebi_acknowledgement_number', models.CharField(blank=True, max_length=50)),
                ('document_url', models.URLField(blank=True, max_length=500)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('fund', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='ppm_amendments',
                    to='funds.fund',
                )),
                ('scheme', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='ppm_amendments',
                    to='funds.scheme',
                )),
                ('prepared_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['fund', '-amendment_number'],
            },
        ),
        migrations.AddConstraint(
            model_name='ppmamendment',
            constraint=models.UniqueConstraint(
                fields=['fund', 'amendment_number'],
                name='unique_fund_amendment_number',
            ),
        ),

        # ── SEBICircular ──────────────────────────────────────────────────
        migrations.CreateModel(
            name='SEBICircular',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('circular_number', models.CharField(max_length=100)),
                ('circular_date', models.DateField()),
                ('title', models.CharField(max_length=500)),
                ('summary', models.TextField(blank=True)),
                ('applicability', models.CharField(
                    choices=[
                        ('all_aif', 'All AIFs'),
                        ('cat_i', 'Category I AIF Only'),
                        ('cat_ii', 'Category II AIF Only'),
                        ('cat_iii', 'Category III AIF Only'),
                        ('cat_i_ii', 'Category I & II AIF'),
                        ('gift_city', 'GIFT City / IFSC AIF'),
                        ('specific', 'Specific Funds (see notes)'),
                    ],
                    default='all_aif',
                    max_length=20,
                )),
                ('impact_level', models.CharField(
                    choices=[
                        ('low', 'Low — Informational Only'),
                        ('medium', 'Medium — Process Change Required'),
                        ('high', 'High — Immediate Action Required'),
                        ('critical', 'Critical — Regulatory Deadline'),
                    ],
                    default='medium',
                    max_length=10,
                )),
                ('compliance_deadline', models.DateField(blank=True, null=True)),
                ('sebi_url', models.URLField(blank=True, max_length=500)),
                ('full_text', models.TextField(blank=True)),
                ('ai_parsed', models.BooleanField(default=False)),
                ('ai_parsed_at', models.DateTimeField(blank=True, null=True)),
                ('is_superseded', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='sebi_circulars',
                    to='accounts.organization',
                )),
                ('superseded_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='supersedes',
                    to='compliance.sebicircular',
                )),
            ],
            options={
                'ordering': ['-circular_date'],
            },
        ),
        migrations.AddIndex(
            model_name='sebicircular',
            index=models.Index(fields=['-circular_date'], name='compliance_circular_date_idx'),
        ),
        migrations.AddIndex(
            model_name='sebicircular',
            index=models.Index(fields=['impact_level'], name='compliance_impact_level_idx'),
        ),

        # ── CircularAction ─────────────────────────────────────────────────
        migrations.CreateModel(
            name='CircularAction',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('action_title', models.CharField(max_length=255)),
                ('action_description', models.TextField()),
                ('priority', models.CharField(
                    choices=[
                        ('low', 'Low'),
                        ('medium', 'Medium'),
                        ('high', 'High'),
                        ('critical', 'Critical'),
                    ],
                    default='medium',
                    max_length=10,
                )),
                ('due_date', models.DateField(blank=True, null=True)),
                ('status', models.CharField(
                    choices=[
                        ('pending', 'Pending'),
                        ('in_progress', 'In Progress'),
                        ('completed', 'Completed'),
                        ('not_applicable', 'Not Applicable'),
                        ('deferred', 'Deferred (with justification)'),
                    ],
                    default='pending',
                    max_length=15,
                )),
                ('completion_date', models.DateField(blank=True, null=True)),
                ('completion_notes', models.TextField(blank=True)),
                ('deferred_reason', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('circular', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='actions',
                    to='compliance.sebicircular',
                )),
                ('fund', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='circular_actions',
                    to='funds.fund',
                )),
                ('assigned_to', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='circular_actions',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['circular', 'priority', 'due_date'],
            },
        ),
        migrations.AddIndex(
            model_name='circularaction',
            index=models.Index(fields=['status'], name='compliance_action_status_idx'),
        ),
        migrations.AddIndex(
            model_name='circularaction',
            index=models.Index(fields=['due_date'], name='compliance_action_due_idx'),
        ),
    ]
