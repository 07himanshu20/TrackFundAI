"""
Migration: Add SHA-256 hash chain fields to AuditLog.
v5 requirement: tamper-proof, append-only audit trail.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_fundos_alignment'),
    ]

    operations = [
        migrations.AddField(
            model_name='auditlog',
            name='prev_hash',
            field=models.CharField(
                blank=True, default='', max_length=64,
                help_text='SHA-256 hash of the previous AuditLog entry (genesis = empty string)',
            ),
        ),
        migrations.AddField(
            model_name='auditlog',
            name='record_hash',
            field=models.CharField(
                blank=True, default='', max_length=64,
                help_text='SHA-256(prev_hash + action + resource_type + resource_id + timestamp_iso)',
            ),
        ),
    ]
