from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='navrecord',
            name='unrealized_gains',
            field=models.DecimalField(
                decimal_places=2, default=0, max_digits=18,
                help_text='Unrealized gains from portfolio revaluation',
            ),
        ),
        migrations.AddField(
            model_name='navrecord',
            name='realized_gains',
            field=models.DecimalField(
                decimal_places=2, default=0, max_digits=18,
                help_text='Realized gains from exits/distributions',
            ),
        ),
    ]
