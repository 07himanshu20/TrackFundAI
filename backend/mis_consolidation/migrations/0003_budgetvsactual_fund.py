import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('funds', '0001_initial'),
        ('mis_consolidation', '0002_alter_budgetvsactual_actual_inr_and_more'),
    ]

    operations = [
        # 1. Add fund FK (nullable so existing rows keep working)
        migrations.AddField(
            model_name='budgetvsactual',
            name='fund',
            field=models.ForeignKey(
                blank=True, null=True,
                help_text='Fund this BvA record belongs to — prevents cross-fund contamination',
                on_delete=django.db.models.deletion.CASCADE,
                related_name='bva_records',
                to='funds.fund',
            ),
        ),
        # 2. Drop old unique_together (portfolio_company, period_year, …)
        migrations.AlterUniqueTogether(
            name='budgetvsactual',
            unique_together=set(),
        ),
        # 3. Add new unique_together that includes fund
        migrations.AlterUniqueTogether(
            name='budgetvsactual',
            unique_together={
                ('portfolio_company', 'fund', 'period_year', 'period_month', 'period_quarter', 'line_item')
            },
        ),
    ]
