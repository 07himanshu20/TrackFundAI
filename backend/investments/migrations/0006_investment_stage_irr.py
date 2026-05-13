from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('investments', '0005_alter_kpidefinition_is_system_kpi_and_more'),
    ]
    operations = [
        migrations.AddField(
            model_name='investment',
            name='stage',
            field=models.CharField(blank=True, help_text='Funding stage / round name (e.g. Seed, Series A, Series B, Bridge)', max_length=100),
        ),
        migrations.AddField(
            model_name='investment',
            name='irr_pct',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Gross IRR % for this investment (e.g. 45.92 means 45.92%)', max_digits=8, null=True),
        ),
    ]
