# Hand-written migration — FundOS India alignment for Documents module

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('documents', '0001_initial'),
    ]

    operations = [
        # ── Document: add checksum_sha256 ────────────────────────────────
        migrations.AddField(
            model_name='document',
            name='checksum_sha256',
            field=models.CharField(blank=True, help_text='SHA-256 checksum for file integrity verification — detect tampering', max_length=64),
        ),

        # ── Document: add watermark_lp_access ────────────────────────────
        migrations.AddField(
            model_name='document',
            name='watermark_lp_access',
            field=models.BooleanField(default=False, help_text='Watermark with LP name when accessed — for LP-visible documents'),
        ),

        # ── Document: expand category choices ────────────────────────────
        migrations.AlterField(
            model_name='document',
            name='category',
            field=models.CharField(
                choices=[
                    ('ppm', 'Private Placement Memorandum'),
                    ('subscription', 'Subscription Agreement'),
                    ('contribution', 'Contribution Agreement'),
                    ('capital_call', 'Capital Call Notice'),
                    ('distribution', 'Distribution Notice'),
                    ('valuation', 'Valuation Report'),
                    ('audit', 'Audit Report'),
                    ('compliance', 'Compliance Report'),
                    ('financial', 'Financial Statement'),
                    ('legal', 'Legal Document'),
                    ('board', 'Board Resolution / Minutes'),
                    ('kyc', 'KYC Document'),
                    ('nav_statement', 'NAV Statement'),
                    ('sebi_filing', 'SEBI Filing'),
                    ('ctr', 'Compliance Test Report'),
                    ('tax', 'Tax Document'),
                    ('other', 'Other'),
                ],
                default='other', max_length=20,
            ),
        ),
    ]
