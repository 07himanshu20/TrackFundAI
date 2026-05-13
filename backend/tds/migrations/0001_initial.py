"""
Migration: TDS — TDSWithholding, Form26QReturn.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid
from decimal import Decimal


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('accounts', '0005_user_v5_rbac_mfa_lockout'),
        ('funds', '0001_initial'),
        ('documents', '0001_initial'),
        ('lp', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TDSWithholding',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('deductee_name', models.CharField(max_length=255)),
                ('deductee_pan', models.CharField(blank=True, max_length=10)),
                ('deductee_tan', models.CharField(blank=True, max_length=10)),
                ('deductee_is_nri', models.BooleanField(default=False)),
                ('deductee_country', models.CharField(blank=True, default='India', max_length=100)),
                ('payment_date', models.DateField()),
                ('payment_nature', models.CharField(
                    max_length=30,
                    choices=[
                        ('distribution', 'Distribution of Profits — 194LBA'),
                        ('return_of_capital', 'Return of Capital — Not Taxable'),
                        ('interest', 'Interest — 194A'),
                        ('exit_proceeds_resident', 'Exit Proceeds — Resident — 194'),
                        ('exit_proceeds_nri', 'Exit Proceeds — NRI — 195'),
                        ('management_fee', 'Management Fee — 194J'),
                        ('other', 'Other Payment'),
                    ],
                )),
                ('gross_amount_inr', models.DecimalField(decimal_places=2, max_digits=18)),
                ('tds_rate_pct', models.DecimalField(decimal_places=3, max_digits=5)),
                ('surcharge_pct', models.DecimalField(decimal_places=3, default=Decimal('0.000'), max_digits=5)),
                ('cess_pct', models.DecimalField(decimal_places=3, default=Decimal('4.000'), max_digits=5)),
                ('base_tax_inr', models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ('surcharge_inr', models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ('cess_inr', models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ('total_tds_inr', models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ('net_payment_inr', models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ('challan_no', models.CharField(blank=True, max_length=50)),
                ('challan_date', models.DateField(null=True, blank=True)),
                ('bsr_code', models.CharField(blank=True, max_length=7)),
                ('deposited_to_govt', models.BooleanField(default=False)),
                ('deposit_date', models.DateField(null=True, blank=True)),
                ('quarter', models.CharField(blank=True, max_length=5)),
                ('financial_year', models.CharField(blank=True, max_length=7)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='tds_withholdings',
                    to='accounts.organization',
                )),
                ('fund', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='tds_withholdings',
                    to='funds.fund',
                )),
                ('investor', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='tds_withholdings',
                    to='lp.investor',
                )),
                ('created_by', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['-payment_date']},
        ),
        migrations.AddIndex(
            model_name='tdswithholding',
            index=models.Index(fields=['organization', 'financial_year', 'quarter'], name='tds_org_fy_q_idx'),
        ),

        migrations.CreateModel(
            name='Form26QReturn',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('financial_year', models.CharField(max_length=7)),
                ('quarter', models.CharField(
                    max_length=2,
                    choices=[('Q1', 'Q1 Apr-Jun'), ('Q2', 'Q2 Jul-Sep'),
                             ('Q3', 'Q3 Oct-Dec'), ('Q4', 'Q4 Jan-Mar')],
                )),
                ('due_date', models.DateField()),
                ('status', models.CharField(
                    max_length=10, default='draft',
                    choices=[
                        ('draft', 'Draft'), ('computed', 'Computed'),
                        ('filed', 'Filed with TRACES'), ('accepted', 'Accepted'),
                        ('rejected', 'Rejected — Resubmit'),
                    ],
                )),
                ('total_transactions', models.IntegerField(default=0)),
                ('total_gross_payment_inr', models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ('total_tds_deducted_inr', models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ('total_tds_deposited_inr', models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ('traces_ack_no', models.CharField(blank=True, max_length=50)),
                ('filed_date', models.DateField(null=True, blank=True)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='form26q_returns',
                    to='accounts.organization',
                )),
                ('filed_by', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('return_document', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='form26q_returns',
                    to='documents.document',
                )),
            ],
            options={'ordering': ['-financial_year', 'quarter'],
                     'unique_together': {('organization', 'financial_year', 'quarter')}},
        ),
    ]
