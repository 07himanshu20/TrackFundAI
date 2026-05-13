"""
Migration: Add Compliance 2.0 models — PortfolioCompanyCompliance,
PortfolioComplianceScore, FEMACompliance.
"""

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('compliance', '0003_ppm_sebi_circulars'),
        ('investments', '0004_valuation_ipev_fields_kpidefinition_sector'),
        ('documents', '0001_initial'),
    ]

    operations = [
        # PortfolioCompanyCompliance
        migrations.CreateModel(
            name='PortfolioCompanyCompliance',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('obligation_type', models.CharField(
                    max_length=25,
                    choices=[
                        ('roc_annual_return', 'ROC/MCA Annual Return'),
                        ('gst_gstr3b', 'GST GSTR-3B Monthly'),
                        ('labour_pf_esi', 'Labour Laws — PF/ESI'),
                        ('labour_factories_act', 'Labour Laws — Factories Act'),
                        ('epf_monthly', 'EPF Monthly Deposit'),
                        ('board_meeting', 'Board Meeting Compliance'),
                        ('statutory_audit', 'Statutory Audit'),
                        ('income_tax_tds', 'Income Tax — TDS'),
                        ('income_tax_advance', 'Income Tax — Advance Tax'),
                        ('rera', 'RERA (Real Estate)'),
                        ('sector_specific', 'Sector-Specific Obligation'),
                        ('other', 'Other'),
                    ],
                )),
                ('obligation_name', models.CharField(max_length=200)),
                ('period_start', models.DateField(blank=True, null=True)),
                ('period_end', models.DateField(blank=True, null=True)),
                ('deadline', models.DateField()),
                ('status', models.CharField(
                    max_length=15, default='due',
                    choices=[('compliant', 'Compliant'), ('due', 'Due Soon'),
                             ('overdue', 'Overdue'), ('filed', 'Filed'),
                             ('not_applicable', 'N/A')],
                )),
                ('rag_status', models.CharField(
                    max_length=6, default='amber',
                    choices=[('green', 'Green — Compliant'), ('amber', 'Amber — Due/Minor Issues'),
                             ('red', 'Red — Overdue/Non-Compliant'), ('grey', 'Grey — N/A')],
                )),
                ('filed_at', models.DateField(blank=True, null=True)),
                ('challan_no', models.CharField(blank=True, max_length=100)),
                ('reference_no', models.CharField(blank=True, max_length=100)),
                ('penalty_amount', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('portfolio_company', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='compliance_obligations',
                    to='investments.portfoliocompany',
                )),
                ('document', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='compliance_obligations',
                    to='documents.document',
                )),
            ],
            options={'ordering': ['portfolio_company', 'deadline']},
        ),
        migrations.AddIndex(
            model_name='portfoliocompanycompliance',
            index=models.Index(fields=['portfolio_company', 'rag_status'],
                               name='compl_pc_rag_idx'),
        ),
        migrations.AddIndex(
            model_name='portfoliocompanycompliance',
            index=models.Index(fields=['deadline', 'status'],
                               name='compl_deadline_status_idx'),
        ),

        # PortfolioComplianceScore
        migrations.CreateModel(
            name='PortfolioComplianceScore',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('score_date', models.DateField()),
                ('compliance_score', models.DecimalField(decimal_places=2, max_digits=5)),
                ('total_obligations', models.IntegerField(default=0)),
                ('compliant_count', models.IntegerField(default=0)),
                ('overdue_count', models.IntegerField(default=0)),
                ('amber_count', models.IntegerField(default=0)),
                ('computed_at', models.DateTimeField(auto_now_add=True)),
                ('portfolio_company', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='compliance_scores',
                    to='investments.portfoliocompany',
                )),
            ],
            options={'ordering': ['-score_date'],
                     'unique_together': {('portfolio_company', 'score_date')}},
        ),

        # FEMACompliance
        migrations.CreateModel(
            name='FEMACompliance',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('form_type', models.CharField(
                    max_length=10,
                    choices=[('fc_gpr', 'FC-GPR'), ('apr', 'APR'),
                             ('fc_trs', 'FC-TRS'), ('llp_i', 'LLP-I'), ('llp_ii', 'LLP-II')],
                )),
                ('filing_date', models.DateField(blank=True, null=True)),
                ('due_date', models.DateField(blank=True, null=True)),
                ('status', models.CharField(
                    max_length=10, default='pending',
                    choices=[('pending', 'Pending'), ('filed', 'Filed'),
                             ('accepted', 'Accepted'), ('rejected', 'Rejected')],
                )),
                ('rbi_arn', models.CharField(blank=True, max_length=50)),
                ('amount_usd', models.DecimalField(blank=True, null=True, decimal_places=2, max_digits=14)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('investment', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='fema_compliance',
                    to='investments.investment',
                )),
            ],
            options={'ordering': ['-due_date']},
        ),
    ]
