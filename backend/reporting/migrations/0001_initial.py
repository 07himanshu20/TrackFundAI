from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('accounts', '0001_initial'),
        ('documents', '0001_initial'),
        ('funds', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ReportingCalendar',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('report_type', models.CharField(
                    max_length=20,
                    choices=[
                        ('monthly_mis', 'Monthly MIS (P&L + BS + CF)'),
                        ('sebi_monthly', 'SEBI AIF Monthly Report'),
                        ('quarterly_lp', 'Quarterly LP Letter'),
                        ('valuation_cert', 'Valuation Certificate (IPEV)'),
                        ('nav_statement', 'NAV Statement'),
                        ('annual_accounts', 'Annual Accounts'),
                        ('fatca_crs', 'FATCA/CRS Report'),
                        ('form_64a', 'Form 64A / LP Tax'),
                        ('sebi_qar', 'SEBI Quarterly Activity Report'),
                        ('sebi_aar', 'SEBI Annual Activity Report'),
                    ],
                )),
                ('period_label', models.CharField(
                    max_length=50,
                    help_text='Human-readable period e.g. "Q1 FY25 (Apr–Jun 2024)"',
                )),
                ('period_start', models.DateField()),
                ('period_end', models.DateField()),
                ('deadline', models.DateField(help_text='SLA deadline per v5 reporting calendar')),
                ('status', models.CharField(
                    max_length=10, default='upcoming',
                    choices=[
                        ('upcoming', 'Upcoming'),
                        ('due', 'Due'),
                        ('overdue', 'Overdue'),
                        ('submitted', 'Submitted'),
                        ('filed', 'Filed'),
                        ('waived', 'Waived'),
                    ],
                )),
                ('report_generated_at', models.DateTimeField(null=True, blank=True)),
                ('submitted_at', models.DateTimeField(null=True, blank=True)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='reporting_calendar',
                    to='accounts.organization',
                )),
                ('fund', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='reporting_obligations',
                    to='funds.fund',
                )),
                ('scheme', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='reporting_obligations',
                    to='funds.scheme',
                )),
                ('report_document', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='reporting_obligations',
                    to='documents.document',
                )),
                ('submitted_by', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['deadline', 'report_type']},
        ),
        migrations.AlterUniqueTogether(
            name='reportingcalendar',
            unique_together={('organization', 'fund', 'scheme', 'report_type', 'period_start')},
        ),
        migrations.CreateModel(
            name='ReportingReminder',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('reminder_type', models.CharField(
                    max_length=15,
                    choices=[
                        ('t3_reminder', 'T+3 First Reminder'),
                        ('t5_escalation', 'T+5 Escalation'),
                        ('manual', 'Manual Reminder'),
                    ],
                )),
                ('sent_at', models.DateTimeField(auto_now_add=True)),
                ('sent_to', models.EmailField(blank=True, help_text='Recipient email address')),
                ('subject', models.CharField(max_length=200, blank=True)),
                ('body', models.TextField(blank=True)),
                ('success', models.BooleanField(default=True)),
                ('error_message', models.TextField(blank=True)),
                ('obligation', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='reminders',
                    to='reporting.reportingcalendar',
                )),
            ],
            options={'ordering': ['-sent_at']},
        ),
        migrations.CreateModel(
            name='GeneratedReport',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('report_type', models.CharField(max_length=30)),
                ('report_format', models.CharField(
                    max_length=5, default='pdf',
                    choices=[('pdf', 'PDF'), ('excel', 'Excel')],
                )),
                ('file', models.FileField(upload_to='reports/%Y/%m/')),
                ('file_size', models.IntegerField(default=0)),
                ('generated_at', models.DateTimeField(auto_now_add=True)),
                ('obligation', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='generated_reports',
                    to='reporting.reportingcalendar',
                )),
                ('organization', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='generated_reports',
                    to='accounts.organization',
                )),
                ('generated_by', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['-generated_at']},
        ),
    ]
