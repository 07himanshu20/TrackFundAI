from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('accounts', '0001_initial'),
        ('investments', '0004_valuation_ipev_fields_kpidefinition_sector'),
        ('dataimport', '0002_add_fund_tracking_to_importfile'),
    ]

    operations = [
        migrations.CreateModel(
            name='EmailMISSubmission',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('email_uid', models.CharField(max_length=100)),
                ('sender_email', models.EmailField()),
                ('sender_name', models.CharField(blank=True, max_length=255)),
                ('subject', models.CharField(blank=True, max_length=500)),
                ('received_at', models.DateTimeField()),
                ('attachment_filename', models.CharField(blank=True, max_length=500)),
                ('attachment_content_type', models.CharField(blank=True, max_length=100)),
                ('status', models.CharField(
                    max_length=12, default='received',
                    choices=[('received', 'Received'), ('parsing', 'Parsing'),
                             ('imported', 'Imported'), ('failed', 'Failed'),
                             ('duplicate', 'Duplicate'), ('ignored', 'Ignored — no attachment')],
                )),
                ('error_message', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='email_mis_submissions',
                    to='accounts.organization',
                )),
                ('portfolio_company', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='email_submissions',
                    to='investments.portfoliocompany',
                )),
                ('import_file', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='email_submission',
                    to='dataimport.importfile',
                )),
            ],
            options={'ordering': ['-received_at'],
                     'unique_together': {('organization', 'email_uid')}},
        ),
        migrations.CreateModel(
            name='MailboxPollLog',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('polled_at', models.DateTimeField(auto_now_add=True)),
                ('emails_found', models.IntegerField(default=0)),
                ('emails_new', models.IntegerField(default=0)),
                ('emails_processed', models.IntegerField(default=0)),
                ('error_message', models.TextField(blank=True)),
                ('success', models.BooleanField(default=True)),
                ('organization', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='mailbox_poll_logs',
                    to='accounts.organization',
                )),
            ],
            options={'ordering': ['-polled_at']},
        ),
    ]
