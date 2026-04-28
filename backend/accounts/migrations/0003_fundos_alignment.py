# Hand-written migration — FundOS India alignment for Users & Access module

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_initial'),
        ('funds', '0002_fundos_alignment'),
    ]

    operations = [
        # ── User: add security / lockout fields ──────────────────────────
        migrations.AddField(
            model_name='user',
            name='failed_login_count',
            field=models.PositiveIntegerField(default=0, help_text='Account lockout after 5 failures'),
        ),
        migrations.AddField(
            model_name='user',
            name='account_locked_until',
            field=models.DateTimeField(blank=True, help_text='Account locked until this timestamp after too many failed logins', null=True),
        ),

        # ── FundAccess: add expires_at, revoked_at ───────────────────────
        migrations.AddField(
            model_name='fundaccess',
            name='expires_at',
            field=models.DateTimeField(blank=True, help_text='Time-bound access for auditors — NULL means permanent', null=True),
        ),
        migrations.AddField(
            model_name='fundaccess',
            name='revoked_at',
            field=models.DateTimeField(blank=True, help_text='Soft revocation — never delete access records for audit trail', null=True),
        ),

        # ── AuditLog: add old_values, new_values ────────────────────────
        migrations.AddField(
            model_name='auditlog',
            name='old_values',
            field=models.JSONField(blank=True, default=dict, help_text='Before state — SOC 2 / SEBI evidence'),
        ),
        migrations.AddField(
            model_name='auditlog',
            name='new_values',
            field=models.JSONField(blank=True, default=dict, help_text='After state — full change record'),
        ),

        # ── SchemeAccess (new model) ─────────────────────────────────────
        migrations.CreateModel(
            name='SchemeAccess',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('access_level', models.CharField(choices=[('read', 'Read Only'), ('write', 'Read + Write'), ('admin', 'Full Admin')], default='read', max_length=10)),
                ('granted_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='scheme_access', to=settings.AUTH_USER_MODEL)),
                ('scheme', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='user_access', to='funds.scheme')),
            ],
            options={
                'ordering': ['user', 'scheme'],
                'unique_together': {('user', 'scheme')},
            },
        ),
    ]
