"""
Migration: v5 alignment for User model.
- Role choices updated to 6 roles (Super Admin, GP Partner, CFO, Analyst, Compliance Officer, LP)
- MFA TOTP secret field
- MFA SMS OTP fields
- login_attempts + lockout_until fields (3-attempt, 24h lockout)
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_auditlog_hash_chain'),
    ]

    operations = [
        # MFA TOTP secret
        migrations.AddField(
            model_name='user',
            name='mfa_totp_secret',
            field=models.CharField(
                blank=True, default='', max_length=64,
                help_text='Base32 TOTP secret (AES-256 encrypted at rest in prod)',
            ),
        ),
        # MFA SMS
        migrations.AddField(
            model_name='user',
            name='mfa_sms_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='user',
            name='mfa_sms_otp',
            field=models.CharField(blank=True, default='', max_length=10),
        ),
        migrations.AddField(
            model_name='user',
            name='mfa_sms_otp_expires',
            field=models.DateTimeField(null=True, blank=True),
        ),
        # Login lockout (v5: 3 attempts, 24h)
        migrations.AddField(
            model_name='user',
            name='login_attempts',
            field=models.PositiveIntegerField(
                default=0,
                help_text='Failed login count — resets on successful login',
            ),
        ),
        migrations.AddField(
            model_name='user',
            name='lockout_until',
            field=models.DateTimeField(
                null=True, blank=True,
                help_text='Account locked until this timestamp (24h after 3rd failed attempt)',
            ),
        ),
        # Update role choices to v5 6-role set
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.CharField(
                max_length=30,
                default='analyst',
                choices=[
                    ('platform_admin', 'Super Admin'),
                    ('gp_admin', 'GP Partner'),
                    ('fund_accountant', 'CFO'),
                    ('analyst', 'Investment Analyst'),
                    ('compliance_officer', 'Compliance Officer'),
                    ('lp_user', 'LP'),
                ],
            ),
        ),
    ]
