"""
Market Explorer — market opportunity database + AI-generated market studies.
v5: Completely new module — 142 seed opportunities, 6-filter system, 11-section report.

Bain/McKinsey/BCG style analysis:
1. Executive Summary
2. Market Size & Dynamics (TAM/SAM/SOM)
3. Competitive Landscape
4. Porter's Five Forces
5. Regulatory Environment
6. Technology & Innovation Trends
7. Consumer / Customer Insights
8. Financial Benchmarks
9. Deal Activity & M&A
10. ESG / Sustainability Considerations
11. Investment Recommendations
"""
import uuid
from django.conf import settings
from django.db import models


class MarketOpportunity(models.Model):
    """
    A seeded market opportunity record (one of 142 seeds + user additions).
    Represents a market vertical that a GP can explore for investments.
    """
    # 6-Dimension Filter System
    SECTOR_CHOICES = [
        ('technology', 'Technology'), ('saas', 'SaaS / Cloud'),
        ('fintech', 'Fintech'), ('healthtech', 'Healthtech'),
        ('edtech', 'EdTech'), ('agritech', 'AgriTech'),
        ('cleantech', 'CleanTech / ESG'), ('logistics', 'Logistics / Supply Chain'),
        ('consumer', 'Consumer / D2C'), ('manufacturing', 'Manufacturing'),
        ('healthcare', 'Healthcare / Pharma'), ('nbfc', 'NBFC / Financial Services'),
        ('real_estate', 'Real Estate'), ('media', 'Media / Content'),
        ('aerospace', 'Aerospace / Defence'), ('ev', 'EV / Mobility'),
        ('infrastructure', 'Infrastructure'), ('ecommerce', 'E-commerce'),
        ('gaming', 'Gaming / Metaverse'), ('biotech', 'Biotech / Life Sciences'),
        ('cybersecurity', 'Cybersecurity'), ('ai_ml', 'AI / ML'),
        ('other', 'Other'),
    ]
    COUNTRY_CHOICES = [
        ('india', 'India'), ('usa', 'USA'), ('uk', 'UK'), ('singapore', 'Singapore'),
        ('uae', 'UAE'), ('germany', 'Germany'), ('france', 'France'), ('japan', 'Japan'),
        ('china', 'China'), ('brazil', 'Brazil'), ('indonesia', 'Indonesia'),
        ('nigeria', 'Nigeria'), ('kenya', 'Kenya'), ('israel', 'Israel'),
        ('australia', 'Australia'), ('canada', 'Canada'), ('global', 'Global'),
    ]
    CONTINENT_CHOICES = [
        ('asia', 'Asia'), ('north_america', 'North America'), ('europe', 'Europe'),
        ('africa', 'Africa'), ('latin_america', 'Latin America'), ('oceania', 'Oceania'),
        ('global', 'Global'),
    ]
    STAGE_CHOICES = [
        ('seed', 'Seed'), ('pre_series_a', 'Pre-Series A'), ('series_a', 'Series A'),
        ('series_b', 'Series B'), ('series_c', 'Series C+'), ('growth', 'Growth'),
        ('late_stage', 'Late Stage / Pre-IPO'), ('buyout', 'Buyout'),
        ('all_stages', 'All Stages'),
    ]
    FIN_CATEGORY_CHOICES = [
        ('high_growth', 'High Growth (>30% CAGR)'), ('steady_growth', 'Steady Growth (10-30% CAGR)'),
        ('value_play', 'Value Play'), ('turnaround', 'Turnaround'),
        ('distressed', 'Distressed / Special Situations'), ('infrastructure', 'Infrastructure / Yield'),
    ]
    FUND_TYPE_CHOICES = [
        ('aif_cat1', 'AIF Category I'), ('aif_cat2', 'AIF Category II'),
        ('aif_cat3', 'AIF Category III'), ('vcc', 'Variable Capital Company (Singapore)'),
        ('lp_gp', 'LP/GP Structure (US/UK)'), ('hedge_fund', 'Hedge Fund'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, help_text='Market opportunity name')
    slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField(help_text='1-2 paragraph overview')

    # 6-Dimension Filter
    sector = models.CharField(max_length=20, choices=SECTOR_CHOICES)
    country = models.CharField(max_length=20, choices=COUNTRY_CHOICES, default='india')
    continent = models.CharField(max_length=20, choices=CONTINENT_CHOICES, default='asia')
    investment_stage = models.CharField(max_length=20, choices=STAGE_CHOICES, default='series_a')
    financial_category = models.CharField(max_length=20, choices=FIN_CATEGORY_CHOICES, default='high_growth')
    fund_type = models.CharField(max_length=20, choices=FUND_TYPE_CHOICES, default='aif_cat2')

    # Market size data
    tam_usd_bn = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Total Addressable Market in USD Billion',
    )
    sam_usd_bn = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Serviceable Addressable Market in USD Billion',
    )
    cagr_pct = models.DecimalField(
        max_digits=5, decimal_places=1, null=True, blank=True,
        help_text='Market CAGR %',
    )
    cagr_period = models.CharField(max_length=20, blank=True, help_text='e.g. 2024-2030')

    # Key stats
    key_players = models.TextField(blank=True, help_text='Top 5-10 players comma-separated')
    investment_thesis = models.TextField(blank=True, help_text='1 para investment thesis')
    key_risks = models.TextField(blank=True)
    regulatory_notes = models.TextField(blank=True)
    esg_score = models.CharField(
        max_length=10, blank=True,
        choices=[('A', 'A — Excellent'), ('B', 'B — Good'), ('C', 'C — Neutral'),
                 ('D', 'D — Concerns'), ('', 'Not Rated')],
    )

    # Metadata
    is_active = models.BooleanField(default=True)
    is_seeded = models.BooleanField(default=True, help_text='True for system-seeded records')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sector', 'name']
        indexes = [
            models.Index(fields=['sector', 'country'], name='mktres_sector_country_idx'),
            models.Index(fields=['continent', 'investment_stage'], name='mktres_continent_stage_idx'),
        ]

    def __str__(self):
        return f'{self.name} ({self.get_sector_display()}, {self.get_country_display()})'


class MarketStudy(models.Model):
    """
    AI-generated market study report (11 sections, Bain/McKinsey/BCG style).
    One study per opportunity per organization (organizations can have custom studies).
    """
    STATUS_CHOICES = [
        ('generating', 'Generating...'),
        ('complete', 'Complete'),
        ('failed', 'Generation Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    opportunity = models.ForeignKey(
        MarketOpportunity, on_delete=models.CASCADE, related_name='studies',
    )
    organization = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='market_studies',
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='generating')
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='market_studies',
    )

    # 11 Sections (stored as JSON for flexibility)
    section_1_executive_summary = models.TextField(blank=True)
    section_2_market_size = models.TextField(blank=True)
    section_3_competitive_landscape = models.TextField(blank=True)
    section_4_porters_five_forces = models.TextField(blank=True)
    section_5_regulatory_environment = models.TextField(blank=True)
    section_6_technology_trends = models.TextField(blank=True)
    section_7_customer_insights = models.TextField(blank=True)
    section_8_financial_benchmarks = models.TextField(blank=True)
    section_9_deal_activity = models.TextField(blank=True)
    section_10_esg = models.TextField(blank=True)
    section_11_recommendations = models.TextField(blank=True)

    # Metadata
    word_count = models.IntegerField(default=0)
    generation_time_seconds = models.FloatField(null=True, blank=True)
    pdf_document = models.ForeignKey(
        'documents.Document', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='market_studies',
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('opportunity', 'organization')

    def __str__(self):
        return f'Study: {self.opportunity.name} for {self.organization.name}'

    def all_sections(self):
        """Returns all 11 sections as an ordered list of (title, content) tuples."""
        return [
            ('Executive Summary', self.section_1_executive_summary),
            ('Market Size & Dynamics', self.section_2_market_size),
            ('Competitive Landscape', self.section_3_competitive_landscape),
            ("Porter's Five Forces", self.section_4_porters_five_forces),
            ('Regulatory Environment', self.section_5_regulatory_environment),
            ('Technology & Innovation Trends', self.section_6_technology_trends),
            ('Consumer / Customer Insights', self.section_7_customer_insights),
            ('Financial Benchmarks', self.section_8_financial_benchmarks),
            ('Deal Activity & M&A', self.section_9_deal_activity),
            ('ESG / Sustainability', self.section_10_esg),
            ('Investment Recommendations', self.section_11_recommendations),
        ]


class FilterPreset(models.Model):
    """Saved filter combination for quick access."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='market_filter_presets',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='market_filter_presets',
    )
    name = models.CharField(max_length=100)
    filters = models.JSONField(default=dict, help_text='Saved filter values as JSON')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.user.username})'
