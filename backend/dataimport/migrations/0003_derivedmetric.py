import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0006_alter_user_account_locked_until_and_more'),
        ('funds', '0004_alter_entity_gstin_alter_entity_organization_and_more'),
        ('dataimport', '0002_add_fund_tracking_to_importfile'),
    ]

    operations = [
        migrations.CreateModel(
            name='DerivedMetric',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('metric_key', models.CharField(help_text='Canonical metric key (e.g. net_irr, moic, tvpi, dpi, nav, rvpi)', max_length=64)),
                ('value', models.DecimalField(blank=True, decimal_places=6, help_text='Computed value; null if Gemini could not pick a viable formula', max_digits=20, null=True)),
                ('formula_expression', models.TextField(blank=True, help_text='Human-readable formula chosen by Gemini')),
                ('inputs_used', models.JSONField(blank=True, default=dict, help_text='Map of input_name -> {value, source} used in the formula')),
                ('confidence', models.FloatField(blank=True, help_text='Gemini confidence 0.0-1.0', null=True)),
                ('gemini_reasoning', models.TextField(blank=True, help_text='Why this formula was chosen over alternates')),
                ('candidate_formulas', models.JSONField(blank=True, default=list, help_text='All formulas Gemini considered: [{formula, inputs_required, available, reason_rejected}]')),
                ('derived_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='derived_metrics', to='accounts.organization')),
                ('scheme', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='derived_metrics', to='funds.scheme')),
                ('source_import_file', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='derived_metrics', to='dataimport.importfile')),
            ],
            options={
                'indexes': [
                    models.Index(fields=['scheme', 'metric_key'], name='dataimport__scheme__idx'),
                    models.Index(fields=['organization', 'metric_key'], name='dataimport__org__met_idx'),
                ],
                'unique_together': {('scheme', 'metric_key')},
            },
        ),
    ]
