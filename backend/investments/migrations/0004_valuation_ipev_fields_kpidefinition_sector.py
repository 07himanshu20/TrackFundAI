"""
Migration: Add IPEV Level 1/2/3 fields to Valuation model.
Add sector_template and is_system_kpi to KPIDefinition.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('investments', '0003_alter_exitevent_proceeds_and_more'),
    ]

    operations = [
        # IPEV level field
        migrations.AddField(
            model_name='valuation',
            name='ipev_level',
            field=models.PositiveSmallIntegerField(
                blank=True, null=True,
                choices=[(1, 'Level 1 — Quoted (Exchange Listed, CMP × shares)'),
                         (2, 'Level 2 — Observable Inputs (Peer Multiples, IBBI certified)'),
                         (3, 'Level 3 — Unobservable Inputs (DCF / Last Round, Board + IBBI certified)')],
                help_text='IPEV Level 1/2/3 classification',
            ),
        ),
        # Pre-IPO track
        migrations.AddField(
            model_name='valuation',
            name='is_pre_ipo',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='valuation',
            name='drhp_value',
            field=models.DecimalField(blank=True, null=True, max_digits=18, decimal_places=2),
        ),
        # DLOM
        migrations.AddField(
            model_name='valuation',
            name='dlom_pct',
            field=models.DecimalField(blank=True, null=True, max_digits=5, decimal_places=2),
        ),
        # Level 2/3 specific
        migrations.AddField(
            model_name='valuation',
            name='peer_multiples_used',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='valuation',
            name='dcf_terminal_growth_rate',
            field=models.DecimalField(blank=True, null=True, max_digits=5, decimal_places=2),
        ),
        migrations.AddField(
            model_name='valuation',
            name='last_round_premium_discount_pct',
            field=models.DecimalField(blank=True, null=True, max_digits=5, decimal_places=2),
        ),
        # Corporate action
        migrations.AddField(
            model_name='valuation',
            name='corporate_action_type',
            field=models.CharField(
                max_length=15, default='none',
                choices=[('none', 'None'), ('stock_split', 'Stock Split'),
                         ('bonus', 'Bonus Issue'), ('rights', 'Rights Issue'),
                         ('dividend', 'Dividend'), ('buyback', 'Buyback')],
            ),
        ),
        migrations.AddField(
            model_name='valuation',
            name='corporate_action_ratio',
            field=models.DecimalField(blank=True, null=True, max_digits=8, decimal_places=4),
        ),
        # KPIDefinition sector_template + is_system_kpi
        migrations.AddField(
            model_name='kpidefinition',
            name='sector_template',
            field=models.CharField(
                max_length=15, default='generic',
                choices=[('generic', 'Generic'), ('saas', 'SaaS'),
                         ('healthcare', 'Healthcare'), ('manufacturing', 'Manufacturing'),
                         ('nbfc', 'NBFC / Fintech'), ('consumer', 'Consumer'),
                         ('realestate', 'Real Estate')],
            ),
        ),
        migrations.AddField(
            model_name='kpidefinition',
            name='is_system_kpi',
            field=models.BooleanField(default=False),
        ),
    ]
