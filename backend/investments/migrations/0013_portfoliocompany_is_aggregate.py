from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('investments', '0012_investment_moic'),
    ]

    operations = [
        migrations.AddField(
            model_name='portfoliocompany',
            name='is_aggregate',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Sentinel row that stores fund-level aggregate metrics '
                    '(fund-wide P&L, KPI totals) rather than a real portfolio '
                    'company. Always excluded from company / investment listings.'
                ),
            ),
        ),
    ]
