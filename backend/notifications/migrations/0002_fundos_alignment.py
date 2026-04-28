# Hand-written migration — FundOS India alignment for Notifications module

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0001_initial'),
    ]

    operations = [
        # ── Notification: add delivery tracking fields ───────────────────
        migrations.AddField(
            model_name='notification',
            name='sent_via_email',
            field=models.BooleanField(default=False, help_text='Email delivery tracking'),
        ),
        migrations.AddField(
            model_name='notification',
            name='sent_via_whatsapp',
            field=models.BooleanField(default=False, help_text="WhatsApp delivery — India's primary messaging channel"),
        ),
        migrations.AddField(
            model_name='notification',
            name='sent_via_sms',
            field=models.BooleanField(default=False, help_text='SMS delivery tracking'),
        ),

        # ── Notification: expand category choices ────────────────────────
        migrations.AlterField(
            model_name='notification',
            name='category',
            field=models.CharField(
                choices=[
                    ('fund', 'Fund Update'),
                    ('document', 'Document'),
                    ('capital_call', 'Capital Call'),
                    ('distribution', 'Distribution'),
                    ('compliance', 'Compliance'),
                    ('kpi', 'KPI Submission'),
                    ('nav_update', 'NAV Update'),
                    ('system', 'System'),
                ],
                default='system', max_length=20,
            ),
        ),
    ]
