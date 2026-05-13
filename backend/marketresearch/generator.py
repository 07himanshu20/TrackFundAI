"""
Market Study Generator — Bain/McKinsey/BCG style 11-section report using Gemini.
v5: Market Explorer — AI-generated market study.
"""
import time
from django.conf import settings

SECTION_PROMPTS = [
    ('section_1_executive_summary', 'Executive Summary', """
Write a 2-3 paragraph executive summary covering:
- The market opportunity and why it's compelling for Indian AIF investors
- Key headline market size (TAM/SAM) and growth trajectory
- Top 2-3 investment themes
- Risk/return profile vs alternatives
Be direct and data-driven. Use professional PE/VC tone."""),

    ('section_2_market_size', 'Market Size & Dynamics', """
Write a detailed market sizing analysis covering:
- TAM, SAM, SOM with methodology
- Historical growth (3-5 years) and forecast (2024-2030)
- Key demand drivers and catalysts
- Geographic breakdown within the market
- Revenue pool distribution across value chain
Include specific numbers. Cite plausible market data sources."""),

    ('section_3_competitive_landscape', 'Competitive Landscape', """
Analyze the competitive landscape:
- Market structure (fragmented/consolidated/oligopoly)
- Top 5-10 players with estimated market share
- Competitive dynamics: pricing, differentiation, switching costs
- Recent entrants and their impact
- Barriers to entry
- White spaces and underserved segments"""),

    ('section_4_porters_five_forces', "Porter's Five Forces Analysis", """
Conduct a rigorous Porter's Five Forces analysis:
1. Threat of New Entrants (HIGH/MEDIUM/LOW): [analysis]
2. Bargaining Power of Suppliers (HIGH/MEDIUM/LOW): [analysis]
3. Bargaining Power of Buyers (HIGH/MEDIUM/LOW): [analysis]
4. Threat of Substitutes (HIGH/MEDIUM/LOW): [analysis]
5. Competitive Rivalry (HIGH/MEDIUM/LOW): [analysis]
Overall Attractiveness Score: X/5

Be analytical and specific to this market."""),

    ('section_5_regulatory_environment', 'Regulatory Environment', """
Detail the regulatory landscape:
- Key regulators (SEBI, RBI, DPIIT, sector-specific)
- Current regulations impacting investments and operations
- Recent regulatory changes and their impact (last 2 years)
- Upcoming regulatory risks
- Licensing requirements for investments
- FEMA/FDI considerations for AIF investors
Focus on India regulatory context primarily."""),

    ('section_6_technology_trends', 'Technology & Innovation Trends', """
Analyze technology and innovation dynamics:
- Key technology enablers disrupting this market
- AI/ML/automation impact on the sector
- Emerging technologies that will reshape the industry (3-5 year horizon)
- Tech adoption curve — where is the market today
- IP landscape and patent activity
- Build vs buy vs partner strategies"""),

    ('section_7_customer_insights', 'Consumer / Customer Insights', """
Provide deep customer analysis:
- Primary customer segments with size and growth
- Customer pain points and unmet needs
- Purchase decision journey and key buying criteria
- Price sensitivity analysis
- Customer acquisition and retention dynamics
- Net Promoter Score / satisfaction benchmarks if available
- Emerging customer behavior shifts post-COVID"""),

    ('section_8_financial_benchmarks', 'Financial Benchmarks', """
Provide financial benchmarking for this sector:
- Revenue multiples (EV/Revenue) for listed/private comps
- EBITDA multiples range
- Gross margin benchmarks by sub-segment
- Unit economics benchmarks (CAC, LTV, churn for relevant metrics)
- Recent funding rounds and valuations (last 18 months)
- Exit multiples from recent M&A transactions
- Expected IRR range for PE/VC investments in this space"""),

    ('section_9_deal_activity', 'Deal Activity & M&A Landscape', """
Analyze recent deal activity:
- PE/VC investments in the sector (last 2 years, by stage)
- Notable M&A transactions and strategic rationale
- IPO pipeline in this sector
- Secondary market activity
- Cross-border deal flows (inbound/outbound India)
- Active investors and their thesis
- Co-investment opportunities and club deals
- Pipeline of potential targets for Indian AIFs"""),

    ('section_10_esg', 'ESG & Sustainability Considerations', """
Assess ESG factors for this market:
- Environmental impact and carbon footprint
- Social value creation (jobs, financial inclusion, healthcare access)
- Governance standards in the sector
- ESG risks that could impair investment returns
- Regulatory ESG requirements (SEBI Business Responsibility Report)
- ESG opportunities — premium for compliant companies
- SDG alignment (UN Sustainable Development Goals)
- BRSR (Business Responsibility and Sustainability Reporting) implications"""),

    ('section_11_recommendations', 'Investment Recommendations', """
Provide actionable investment recommendations:
1. INVEST / WATCH / AVOID recommendation with rationale
2. Optimal fund type and investment stage for this market
3. Target company profile (revenue range, growth rate, margin profile)
4. Key investment criteria and screening filters
5. Valuation framework: which multiples to use and why
6. Portfolio construction: how many bets, concentration vs diversification
7. Exit strategy: timeline, preferred routes (IPO/strategic/secondary)
8. Key risks and mitigation strategies
9. Due diligence checklist (5 critical items)
10. Competitive positioning vs other GPs in this space"""),
]


def generate_market_study(study_pk: str):
    """
    Celery task function: generate all 11 sections of a market study.
    Designed to be called as: generate_market_study.delay(str(study.pk))
    """
    from .models import MarketStudy
    try:
        study = MarketStudy.objects.get(pk=study_pk)
    except MarketStudy.DoesNotExist:
        return

    opp = study.opportunity
    start_time = time.time()

    context = f"""Market: {opp.name}
Sector: {opp.get_sector_display()}
Geography: {opp.get_country_display()} / {opp.get_continent_display()}
Investment Stage Focus: {opp.get_investment_stage_display()}
TAM: USD {opp.tam_usd_bn}B
CAGR: {opp.cagr_pct}% ({opp.cagr_period})
Key Players: {opp.key_players}
Investment Thesis: {opp.investment_thesis}
Fund Type: {opp.get_fund_type_display()}"""

    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel(settings.GEMINI_MODEL)

        total_words = 0
        for field_name, section_title, section_prompt in SECTION_PROMPTS:
            prompt = f"""You are a senior partner at a top-tier management consulting firm (Bain/McKinsey/BCG).
You are writing a market research report for an Indian Alternative Investment Fund (AIF) GP.

Market Context:
{context}

Section to write: {section_title}

Instructions:
{section_prompt}

Write in professional consulting prose. Use data, frameworks, and specific examples.
Length: 400-600 words for this section."""

            try:
                response = model.generate_content(prompt)
                section_text = response.text.strip()
            except Exception as e:
                section_text = f'[Generation error for {section_title}: {str(e)}]'

            setattr(study, field_name, section_text)
            total_words += len(section_text.split())
            study.save(update_fields=[field_name])

        study.status = 'complete'
        study.word_count = total_words
        study.generation_time_seconds = time.time() - start_time
        study.save(update_fields=['status', 'word_count', 'generation_time_seconds'])

    except Exception as e:
        study.status = 'failed'
        study.error_message = str(e)
        study.save(update_fields=['status', 'error_message'])


def generate_pdf_report(study_pk: str) -> bytes:
    """Generate a Bain/McKinsey style PDF report using ReportLab."""
    from .models import MarketStudy
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, PageBreak
    from reportlab.platypus import Table, TableStyle
    import io

    study = MarketStudy.objects.get(pk=study_pk)
    opp = study.opportunity
    buf = io.BytesIO()

    # McKinsey-inspired color palette
    DARK_BLUE = colors.HexColor('#003366')
    MID_BLUE = colors.HexColor('#0066CC')
    LIGHT_BLUE = colors.HexColor('#E8F0FB')
    DARK_GRAY = colors.HexColor('#333333')
    LIGHT_GRAY = colors.HexColor('#F5F5F5')
    ACCENT = colors.HexColor('#CC3333')  # McKinsey red

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2.5 * cm, rightMargin=2.5 * cm,
        topMargin=3 * cm, bottomMargin=2.5 * cm,
        title=f'Market Study: {opp.name}',
        author='TrackFundAI',
    )

    styles = getSampleStyleSheet()
    story = []

    # Title styles
    title_style = ParagraphStyle('Title', fontSize=24, textColor=DARK_BLUE,
                                  spaceAfter=6, leading=28, fontName='Helvetica-Bold')
    subtitle_style = ParagraphStyle('Subtitle', fontSize=12, textColor=DARK_GRAY,
                                     spaceAfter=4, leading=16)
    h1_style = ParagraphStyle('H1', fontSize=16, textColor=DARK_BLUE,
                               spaceAfter=8, spaceBefore=16, leading=20, fontName='Helvetica-Bold')
    body_style = ParagraphStyle('Body', fontSize=10, textColor=DARK_GRAY,
                                 spaceAfter=6, leading=15, fontName='Helvetica')
    meta_style = ParagraphStyle('Meta', fontSize=9, textColor=colors.HexColor('#666666'),
                                 spaceAfter=4)

    # Cover page
    story.append(Spacer(1, 1 * cm))
    story.append(HRFlowable(width='100%', thickness=3, color=DARK_BLUE))
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(opp.name, title_style))
    story.append(Paragraph('Market Research & Investment Analysis', subtitle_style))
    story.append(Spacer(1, 0.3 * cm))
    story.append(HRFlowable(width='100%', thickness=1, color=MID_BLUE))
    story.append(Spacer(1, 0.5 * cm))

    # Market metadata table
    meta_data = [
        ['Sector', opp.get_sector_display(), 'Country', opp.get_country_display()],
        ['Stage Focus', opp.get_investment_stage_display(), 'Fund Type', opp.get_fund_type_display()],
        ['TAM', f'USD {opp.tam_usd_bn}B' if opp.tam_usd_bn else 'N/A',
         'CAGR', f'{opp.cagr_pct}% {opp.cagr_period}' if opp.cagr_pct else 'N/A'],
        ['Financial Category', opp.get_financial_category_display(), 'ESG', opp.get_esg_score_display() if opp.esg_score else 'N/A'],
    ]
    meta_table = Table(meta_data, colWidths=[4 * cm, 6 * cm, 4 * cm, 6 * cm])
    meta_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), LIGHT_BLUE),
        ('BACKGROUND', (2, 0), (2, -1), LIGHT_BLUE),
        ('TEXTCOLOR', (0, 0), (-1, -1), DARK_GRAY),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ROWBACKGROUNDS', (0, 0), (-1, -1), [LIGHT_GRAY, colors.white]),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
        ('INNERGRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#CCCCCC')),
        ('PADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(
        f'Prepared by TrackFundAI — {study.created_at.strftime("%B %Y")} — CONFIDENTIAL',
        meta_style,
    ))

    # 11 Sections
    for idx, (section_title, content) in enumerate(study.all_sections(), 1):
        story.append(PageBreak())
        story.append(HRFlowable(width='100%', thickness=2, color=MID_BLUE))
        story.append(Spacer(1, 0.3 * cm))
        story.append(Paragraph(f'{idx}. {section_title}', h1_style))
        story.append(HRFlowable(width='100%', thickness=0.5, color=LIGHT_BLUE))
        story.append(Spacer(1, 0.3 * cm))

        if content:
            for para in content.split('\n\n'):
                para = para.strip()
                if para:
                    story.append(Paragraph(para.replace('\n', '<br/>'), body_style))
                    story.append(Spacer(1, 0.2 * cm))
        else:
            story.append(Paragraph('<i>[Section being generated...]</i>', meta_style))

    doc.build(story)
    return buf.getvalue()
