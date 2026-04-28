# Hand-written migration -- FundOS India alignment for Fund Administration module
# Split into careful ordering for SQLite compatibility with existing data

from django.db import migrations, models
import django.db.models.deletion
import uuid


def populate_entity_organization(apps, schema_editor):
    """Copy organization from fund to entity for existing rows using raw SQL."""
    schema_editor.execute(
        "UPDATE funds_entity SET organization_id = "
        "(SELECT organization_id FROM funds_fund WHERE funds_fund.id = funds_entity.fund_id) "
        "WHERE fund_id IS NOT NULL"
    )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_initial'),
        ('funds', '0001_initial'),
    ]

    operations = [
        # -- FundCategory (new model) --
        migrations.CreateModel(
            name='FundCategory',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('sebi_category_code', models.CharField(help_text='SEBI category code', max_length=20, unique=True)),
                ('name', models.CharField(help_text='Category I AIF / Category II AIF / Category III AIF', max_length=100)),
                ('sub_category', models.CharField(blank=True, help_text='Venture Capital Fund, Angel Fund, PE Fund, Hedge Fund, etc.', max_length=100)),
                ('leverage_permitted', models.BooleanField(default=False, help_text='Category III only')),
                ('description', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['sebi_category_code'],
                'verbose_name_plural': 'fund categories',
            },
        ),

        # -- Fund: rename status -> fund_status --
        migrations.RenameField(
            model_name='fund',
            old_name='status',
            new_name='fund_status',
        ),

        # -- Fund: remove old CharField category --
        migrations.RemoveField(
            model_name='fund',
            name='category',
        ),

        # -- Fund: add fund_category FK --
        migrations.AddField(
            model_name='fund',
            name='fund_category',
            field=models.ForeignKey(
                blank=True, null=True,
                help_text='SEBI AIF category (Category I/II/III + sub-category)',
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='funds',
                to='funds.fundcategory',
            ),
        ),

        # -- Fund: add entity FK fields --
        migrations.AddField(
            model_name='fund',
            name='manager_entity',
            field=models.ForeignKey(
                blank=True, null=True,
                help_text='Investment Manager entity',
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='managed_funds',
                to='funds.entity',
            ),
        ),
        migrations.AddField(
            model_name='fund',
            name='trustee_entity',
            field=models.ForeignKey(
                blank=True, null=True,
                help_text='Trustee entity',
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='trustee_funds',
                to='funds.entity',
            ),
        ),
        migrations.AddField(
            model_name='fund',
            name='sponsor_entity',
            field=models.ForeignKey(
                blank=True, null=True,
                help_text='Sponsor entity',
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='sponsored_funds',
                to='funds.entity',
            ),
        ),
        migrations.AddField(
            model_name='fund',
            name='custodian_entity',
            field=models.ForeignKey(
                blank=True, null=True,
                help_text='Custodian entity',
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='custodian_funds',
                to='funds.entity',
            ),
        ),
        migrations.AddField(
            model_name='fund',
            name='auditor_entity',
            field=models.ForeignKey(
                blank=True, null=True,
                help_text='Statutory Auditor entity',
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='audited_funds',
                to='funds.entity',
            ),
        ),

        # -- Fund: add PAN / GSTIN --
        migrations.AddField(
            model_name='fund',
            name='pan',
            field=models.CharField(blank=True, help_text='PAN of the fund (trust/company/LLP)', max_length=10),
        ),
        migrations.AddField(
            model_name='fund',
            name='gstin',
            field=models.CharField(blank=True, help_text='GSTIN of the fund', max_length=15),
        ),

        # -- Entity: FIRST drop old unique_together (fund, role, name) --
        # Required before any field changes on SQLite
        migrations.AlterUniqueTogether(
            name='entity',
            unique_together=set(),
        ),

        # -- Entity: rename role -> entity_type --
        migrations.RenameField(
            model_name='entity',
            old_name='role',
            new_name='entity_type',
        ),

        # -- Entity: rename name -> entity_name --
        migrations.RenameField(
            model_name='entity',
            old_name='name',
            new_name='entity_name',
        ),

        # -- Entity: add organization FK as NULLABLE first --
        migrations.AddField(
            model_name='entity',
            name='organization',
            field=models.ForeignKey(
                help_text='Owning organization',
                on_delete=django.db.models.deletion.CASCADE,
                related_name='entities',
                to='accounts.organization',
                null=True,
            ),
        ),

        # -- Entity: data migration to populate organization from fund --
        migrations.RunPython(
            populate_entity_organization,
            reverse_code=migrations.RunPython.noop,
        ),

        # -- Entity: NOW remove fund FK (org is populated) --
        migrations.RemoveField(
            model_name='entity',
            name='fund',
        ),

        # -- Entity: make organization NOT NULL --
        migrations.AlterField(
            model_name='entity',
            name='organization',
            field=models.ForeignKey(
                help_text='Owning organization',
                on_delete=django.db.models.deletion.CASCADE,
                related_name='entities',
                to='accounts.organization',
            ),
        ),

        # -- Entity: add new fields --
        migrations.AddField(
            model_name='entity',
            name='pan',
            field=models.CharField(blank=True, help_text='PAN', max_length=10),
        ),
        migrations.AddField(
            model_name='entity',
            name='gstin',
            field=models.CharField(blank=True, help_text='GSTIN', max_length=15),
        ),
        migrations.AddField(
            model_name='entity',
            name='city',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='entity',
            name='state',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='entity',
            name='country',
            field=models.CharField(default='India', max_length=100),
        ),

        # -- Entity: update choices for entity_type (add valuer) --
        migrations.AlterField(
            model_name='entity',
            name='entity_type',
            field=models.CharField(
                choices=[
                    ('manager', 'Investment Manager'),
                    ('trustee', 'Trustee'),
                    ('sponsor', 'Sponsor'),
                    ('custodian', 'Custodian'),
                    ('statutory_auditor', 'Statutory Auditor'),
                    ('legal_counsel', 'Legal Counsel'),
                    ('registrar', 'Registrar & Transfer Agent'),
                    ('valuer', 'Registered Valuer'),
                ],
                max_length=20,
            ),
        ),

        # -- Entity: set new unique_together and ordering --
        migrations.AlterUniqueTogether(
            name='entity',
            unique_together={('organization', 'entity_type', 'entity_name')},
        ),
        migrations.AlterModelOptions(
            name='entity',
            options={
                'ordering': ['entity_type', 'entity_name'],
                'verbose_name_plural': 'entities',
            },
        ),

        # -- Scheme: add new fields --
        migrations.AddField(
            model_name='scheme',
            name='dissolution_date',
            field=models.DateField(blank=True, help_text='Actual or expected dissolution date', null=True),
        ),
        migrations.AddField(
            model_name='scheme',
            name='tenure_years',
            field=models.PositiveIntegerField(blank=True, help_text='Scheme tenure in years', null=True),
        ),
        migrations.AddField(
            model_name='scheme',
            name='sponsor_commitment_pct',
            field=models.DecimalField(
                blank=True, decimal_places=2,
                help_text='Sponsor commitment as pct of scheme size',
                max_digits=5, null=True,
            ),
        ),
        migrations.AddField(
            model_name='scheme',
            name='scheme_status',
            field=models.CharField(
                choices=[
                    ('fundraising', 'Fundraising'),
                    ('investing', 'Investing'),
                    ('harvesting', 'Harvesting'),
                    ('dissolved', 'Dissolved'),
                ],
                default='fundraising',
                max_length=15,
            ),
        ),

        # -- Scheme: update scheme_size help_text --
        migrations.AlterField(
            model_name='scheme',
            name='scheme_size',
            field=models.DecimalField(
                blank=True, decimal_places=2,
                help_text='Target scheme size in fund base currency',
                max_digits=18, null=True,
            ),
        ),

        # -- Scheme: update hurdle_rate_pct help_text --
        migrations.AlterField(
            model_name='scheme',
            name='hurdle_rate_pct',
            field=models.DecimalField(
                blank=True, decimal_places=2,
                help_text='Hurdle rate percentage (e.g., 8.00 = 8% preferred return)',
                max_digits=5, null=True,
            ),
        ),
    ]
