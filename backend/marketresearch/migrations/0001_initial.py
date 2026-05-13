"""
Migration: Market Explorer — MarketOpportunity, MarketStudy, FilterPreset.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('accounts', '0005_user_v5_rbac_mfa_lockout'),
        ('documents', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MarketOpportunity',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=200)),
                ('slug', models.SlugField(max_length=200, unique=True)),
                ('description', models.TextField()),
                ('sector', models.CharField(max_length=20, choices=[
                    ('technology', 'Technology'), ('saas', 'SaaS / Cloud'), ('fintech', 'Fintech'),
                    ('healthtech', 'Healthtech'), ('edtech', 'EdTech'), ('agritech', 'AgriTech'),
                    ('cleantech', 'CleanTech / ESG'), ('logistics', 'Logistics / Supply Chain'),
                    ('consumer', 'Consumer / D2C'), ('manufacturing', 'Manufacturing'),
                    ('healthcare', 'Healthcare / Pharma'), ('nbfc', 'NBFC / Financial Services'),
                    ('real_estate', 'Real Estate'), ('media', 'Media / Content'),
                    ('aerospace', 'Aerospace / Defence'), ('ev', 'EV / Mobility'),
                    ('infrastructure', 'Infrastructure'), ('ecommerce', 'E-commerce'),
                    ('gaming', 'Gaming / Metaverse'), ('biotech', 'Biotech / Life Sciences'),
                    ('cybersecurity', 'Cybersecurity'), ('ai_ml', 'AI / ML'), ('other', 'Other'),
                ])),
                ('country', models.CharField(max_length=20, default='india', choices=[
                    ('india', 'India'), ('usa', 'USA'), ('uk', 'UK'), ('singapore', 'Singapore'),
                    ('uae', 'UAE'), ('germany', 'Germany'), ('france', 'France'), ('japan', 'Japan'),
                    ('china', 'China'), ('brazil', 'Brazil'), ('indonesia', 'Indonesia'),
                    ('nigeria', 'Nigeria'), ('kenya', 'Kenya'), ('israel', 'Israel'),
                    ('australia', 'Australia'), ('canada', 'Canada'), ('global', 'Global'),
                ])),
                ('continent', models.CharField(max_length=20, default='asia', choices=[
                    ('asia', 'Asia'), ('north_america', 'North America'), ('europe', 'Europe'),
                    ('africa', 'Africa'), ('latin_america', 'Latin America'), ('oceania', 'Oceania'),
                    ('global', 'Global'),
                ])),
                ('investment_stage', models.CharField(max_length=20, default='series_a', choices=[
                    ('seed', 'Seed'), ('pre_series_a', 'Pre-Series A'), ('series_a', 'Series A'),
                    ('series_b', 'Series B'), ('series_c', 'Series C+'), ('growth', 'Growth'),
                    ('late_stage', 'Late Stage / Pre-IPO'), ('buyout', 'Buyout'), ('all_stages', 'All Stages'),
                ])),
                ('financial_category', models.CharField(max_length=20, default='high_growth', choices=[
                    ('high_growth', 'High Growth (>30% CAGR)'), ('steady_growth', 'Steady Growth (10-30% CAGR)'),
                    ('value_play', 'Value Play'), ('turnaround', 'Turnaround'),
                    ('distressed', 'Distressed / Special Situations'), ('infrastructure', 'Infrastructure / Yield'),
                ])),
                ('fund_type', models.CharField(max_length=20, default='aif_cat2', choices=[
                    ('aif_cat1', 'AIF Category I'), ('aif_cat2', 'AIF Category II'),
                    ('aif_cat3', 'AIF Category III'), ('vcc', 'Variable Capital Company (Singapore)'),
                    ('lp_gp', 'LP/GP Structure (US/UK)'), ('hedge_fund', 'Hedge Fund'),
                ])),
                ('tam_usd_bn', models.DecimalField(decimal_places=2, max_digits=10, null=True, blank=True)),
                ('sam_usd_bn', models.DecimalField(decimal_places=2, max_digits=10, null=True, blank=True)),
                ('cagr_pct', models.DecimalField(decimal_places=1, max_digits=5, null=True, blank=True)),
                ('cagr_period', models.CharField(blank=True, max_length=20)),
                ('key_players', models.TextField(blank=True)),
                ('investment_thesis', models.TextField(blank=True)),
                ('key_risks', models.TextField(blank=True)),
                ('regulatory_notes', models.TextField(blank=True)),
                ('esg_score', models.CharField(blank=True, max_length=10,
                    choices=[('A', 'A — Excellent'), ('B', 'B — Good'), ('C', 'C — Neutral'),
                             ('D', 'D — Concerns'), ('', 'Not Rated')])),
                ('is_active', models.BooleanField(default=True)),
                ('is_seeded', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'ordering': ['sector', 'name']},
        ),
        migrations.AddIndex(
            model_name='marketopportunity',
            index=models.Index(fields=['sector', 'country'], name='mktres_sector_country_idx'),
        ),
        migrations.AddIndex(
            model_name='marketopportunity',
            index=models.Index(fields=['continent', 'investment_stage'], name='mktres_continent_stage_idx'),
        ),

        migrations.CreateModel(
            name='MarketStudy',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(max_length=12, default='generating',
                    choices=[('generating', 'Generating...'), ('complete', 'Complete'), ('failed', 'Generation Failed')])),
                ('section_1_executive_summary', models.TextField(blank=True)),
                ('section_2_market_size', models.TextField(blank=True)),
                ('section_3_competitive_landscape', models.TextField(blank=True)),
                ('section_4_porters_five_forces', models.TextField(blank=True)),
                ('section_5_regulatory_environment', models.TextField(blank=True)),
                ('section_6_technology_trends', models.TextField(blank=True)),
                ('section_7_customer_insights', models.TextField(blank=True)),
                ('section_8_financial_benchmarks', models.TextField(blank=True)),
                ('section_9_deal_activity', models.TextField(blank=True)),
                ('section_10_esg', models.TextField(blank=True)),
                ('section_11_recommendations', models.TextField(blank=True)),
                ('word_count', models.IntegerField(default=0)),
                ('generation_time_seconds', models.FloatField(null=True, blank=True)),
                ('error_message', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('opportunity', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='studies',
                    to='marketresearch.marketopportunity',
                )),
                ('organization', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='market_studies',
                    to='accounts.organization',
                )),
                ('generated_by', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='market_studies',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('pdf_document', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='market_studies',
                    to='documents.document',
                )),
            ],
            options={'ordering': ['-created_at'], 'unique_together': {('opportunity', 'organization')}},
        ),

        migrations.CreateModel(
            name='FilterPreset',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=100)),
                ('filters', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('organization', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='market_filter_presets',
                    to='accounts.organization',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='market_filter_presets',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['name']},
        ),
    ]
