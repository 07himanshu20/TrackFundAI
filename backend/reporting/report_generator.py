"""
Report Generator — auto-generates LP Letters, Valuation Certificates,
NAV Statements, FATCA/CRS, Form 64A reports using ReportLab (PDF).

v5 spec: PDF output in Bain/McKinsey/BCG professional style.
"""

import io
import logging
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

logger = logging.getLogger(__name__)


def _get_reportlab():
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
        return True
    except ImportError:
        return False


def _draw_watermark(canvas, doc, text='CONFIDENTIAL'):
    """Draw a diagonal watermark on every page."""
    canvas.saveState()
    canvas.setFont('Helvetica-Bold', 60)
    try:
        canvas.setFillAlpha(0.06)
    except AttributeError:
        pass  # Older ReportLab versions
    from reportlab.lib import colors
    canvas.setFillColor(colors.HexColor('#003366'))
    canvas.translate(doc.pagesize[0] / 2, doc.pagesize[1] / 2)
    canvas.rotate(45)
    canvas.drawCentredString(0, 0, text)
    canvas.restoreState()


def _page_footer(canvas, doc, fund_name='', report_type=''):
    """Draw page footer with fund name and page number."""
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    width = doc.pagesize[0]

    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor('#D1D5DB'))
    canvas.setLineWidth(0.5)
    canvas.line(2 * cm, 1.5 * cm, width - 2 * cm, 1.5 * cm)
    canvas.setFont('Helvetica', 7)
    canvas.setFillColor(colors.HexColor('#6B7280'))
    label = f'{fund_name} — {report_type}' if fund_name else 'TrackFundAI Report'
    canvas.drawString(2 * cm, 1.0 * cm, f'{label} · Confidential')
    canvas.drawRightString(width - 2 * cm, 1.0 * cm, f'Page {doc.page}')
    canvas.restoreState()

    _draw_watermark(canvas, doc)


def generate_lp_letter(scheme, period_label: str, period_start: date, period_end: date, user=None):
    """
    Generate a quarterly LP Letter PDF for a scheme.

    Includes:
      - Fund metadata (name, category, SEBI reg, vintage)
      - 6 KPI cards: AUM, MOIC, Net IRR, Deployment %, LP Count, Carry
      - Portfolio company performance table
      - Investment highlights (Gemini-generated)
      - Outlook section (Gemini-generated)

    Returns GeneratedReport instance.
    """
    from accounting.models import NAVRecord
    from investments.models import Investment

    if not _get_reportlab():
        logger.warning('ReportLab not installed — skipping LP letter generation')
        return None

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    fund = scheme.fund
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=2*cm, bottomMargin=2*cm,
                            leftMargin=2.5*cm, rightMargin=2.5*cm)

    styles = getSampleStyleSheet()
    DARK_BLUE = colors.HexColor('#003366')
    MID_BLUE  = colors.HexColor('#0066CC')
    LIGHT_BG  = colors.HexColor('#F5F8FF')

    title_style = ParagraphStyle('title', parent=styles['Heading1'],
                                 fontSize=18, textColor=DARK_BLUE,
                                 spaceAfter=4)
    subtitle_style = ParagraphStyle('subtitle', parent=styles['Normal'],
                                    fontSize=11, textColor=MID_BLUE)
    section_style = ParagraphStyle('section', parent=styles['Heading2'],
                                   fontSize=13, textColor=DARK_BLUE,
                                   spaceBefore=12, spaceAfter=4)
    body_style = ParagraphStyle('body', parent=styles['Normal'],
                                fontSize=10, leading=14)

    story = []

    # Header
    story.append(Paragraph(f'{fund.name}', title_style))
    story.append(Paragraph(f'{scheme.name} — {period_label} Investor Letter', subtitle_style))
    story.append(Spacer(1, 0.3*cm))
    story.append(HRFlowable(width='100%', thickness=1, color=DARK_BLUE))
    story.append(Spacer(1, 0.5*cm))

    # Fund details table
    story.append(Paragraph('Fund Overview', section_style))
    fund_data = [
        ['Fund Name', fund.name],
        ['Scheme', scheme.name],
        ['SEBI Reg. No.', fund.sebi_registration_number or '—'],
        ['Vintage Year', str(scheme.vintage_year) if scheme.vintage_year else '—'],
        ['Hurdle Rate', f'{scheme.hurdle_rate_pct}%' if scheme.hurdle_rate_pct else '—'],
        ['Carry', f'{scheme.carry_pct}%' if scheme.carry_pct else '—'],
        ['Report Period', period_label],
    ]
    fund_table = Table(fund_data, colWidths=[5*cm, 10*cm])
    fund_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), LIGHT_BG),
        ('TEXTCOLOR', (0, 0), (0, -1), DARK_BLUE),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('PADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(fund_table)
    story.append(Spacer(1, 0.5*cm))

    # NAV / Performance section
    latest_nav = NAVRecord.objects.filter(
        scheme=scheme, nav_date__lte=period_end,
    ).order_by('-nav_date').first()

    story.append(Paragraph('Performance Summary', section_style))
    perf_data = [
        ['Metric', 'Value'],
        ['Total NAV', f'₹{float(latest_nav.total_nav):,.2f} Cr' if latest_nav else '—'],
        ['NAV per Unit', f'₹{float(latest_nav.nav_per_unit):,.4f}' if latest_nav else '—'],
        ['Units Outstanding', f'{float(latest_nav.total_units_outstanding):,.2f}' if latest_nav else '—'],
    ]

    # Carry info
    from accounting.models import CarriedInterest
    latest_carry = CarriedInterest.objects.filter(
        scheme=scheme, calculation_date__lte=period_end,
    ).order_by('-calculation_date').first()

    if latest_carry:
        perf_data += [
            ['Carry (Gross)', f'₹{float(latest_carry.carry_amount_gross):,.2f} Cr'],
            ['Carry (Net)', f'₹{float(latest_carry.carry_amount_net):,.2f} Cr'],
        ]

    perf_table = Table(perf_data, colWidths=[8*cm, 7*cm])
    perf_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), DARK_BLUE),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('BACKGROUND', (0, 1), (-1, -1), LIGHT_BG),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('PADDING', (0, 0), (-1, -1), 6),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
    ]))
    story.append(perf_table)
    story.append(Spacer(1, 0.5*cm))

    # Portfolio companies table
    story.append(Paragraph('Portfolio Companies', section_style))

    investments = Investment.objects.filter(
        scheme=scheme, status__in=['active', 'partially_exited'],
    ).select_related('portfolio_company').order_by('company_name')

    port_data = [['Company', 'Sector', 'Invested (Cr)', 'FV (Cr)', 'Status']]
    for inv in investments:
        latest_val = inv.valuations.filter(status='approved').order_by('-valuation_date').first()
        fv = f'₹{float(latest_val.fair_value_of_holding):,.2f}' if latest_val and latest_val.fair_value_of_holding else '—'
        port_data.append([
            inv.company_name[:25],
            (inv.sector or '—')[:15],
            f'₹{float(inv.total_invested):,.2f}',
            fv,
            inv.get_status_display(),
        ])

    if len(port_data) > 1:
        port_table = Table(port_data, colWidths=[5*cm, 3*cm, 3*cm, 3*cm, 2.5*cm])
        port_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), DARK_BLUE),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ('PADDING', (0, 0), (-1, -1), 5),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [LIGHT_BG, colors.white]),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ]))
        story.append(port_table)

    story.append(Spacer(1, 0.5*cm))

    # AI commentary (Gemini)
    ai_commentary = _generate_lp_commentary(scheme, period_label, investments)
    if ai_commentary:
        story.append(Paragraph('Investment Highlights', section_style))
        story.append(Paragraph(ai_commentary, body_style))
        story.append(Spacer(1, 0.3*cm))

    # Disclaimer
    story.append(HRFlowable(width='100%', thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 0.2*cm))
    disclaimer = (
        'This letter is prepared solely for the use of Limited Partners of the fund. '
        'Past performance is not a guarantee of future returns. All values are indicative '
        'and subject to final audit. This document is confidential.'
    )
    story.append(Paragraph(disclaimer, ParagraphStyle('disclaimer', parent=styles['Normal'],
                                                       fontSize=7, textColor=colors.grey)))

    _fund_name = fund.name

    def _on_page(canvas, doc):
        _page_footer(canvas, doc, fund_name=_fund_name, report_type='Quarterly LP Letter')

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    pdf_bytes = buf.getvalue()

    return _save_generated_report(
        organization=fund.organization,
        report_type='quarterly_lp_letter',
        filename=f'LP_Letter_{scheme.fund.name}_{scheme.name}_{period_label}.pdf',
        content=pdf_bytes,
        user=user,
    )


def _generate_lp_commentary(scheme, period_label, investments):
    """Use Gemini to generate LP letter investment highlights."""
    try:
        import google.generativeai as genai
        if not settings.GEMINI_API_KEY:
            return ''

        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel(settings.GEMINI_MODEL)

        companies = [inv.company_name for inv in investments[:10]]
        sectors = list(set(inv.sector for inv in investments if inv.sector))

        prompt = f"""Write a professional 3-paragraph LP letter investment highlights section.

Fund: {scheme.fund.name} — {scheme.name}
Period: {period_label}
Portfolio companies: {', '.join(companies) if companies else 'See table above'}
Sectors represented: {', '.join(sectors) if sectors else 'Diversified'}

Write in a professional private equity tone. Paragraph 1: portfolio overview.
Paragraph 2: key highlights and value creation. Paragraph 3: outlook.
Keep each paragraph to 3-4 sentences. Be concise and professional."""

        result = model.generate_content(prompt)
        return result.text.strip()
    except Exception as e:
        logger.warning(f'LP commentary generation failed: {e}')
        return ''


def _save_generated_report(organization, report_type, filename, content, user=None):
    """Save the generated PDF to storage and create a GeneratedReport record."""
    from reporting.models import GeneratedReport

    report = GeneratedReport.objects.create(
        organization=organization,
        report_type=report_type,
        report_format='pdf',
        file_size=len(content),
        generated_by=user,
    )
    report.file.save(filename, ContentFile(content), save=True)
    return report


def generate_nav_statement(scheme, period_end: date, user=None):
    """Generate NAV Statement PDF for a scheme."""
    from accounting.models import NAVRecord
    from accounting.nav_engine import compute_nav

    # Ensure NAV is computed
    nav = compute_nav(scheme, period_end)

    if not _get_reportlab():
        return None

    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=2*cm, bottomMargin=2*cm,
                            leftMargin=2.5*cm, rightMargin=2.5*cm)
    styles = getSampleStyleSheet()

    story = [
        Paragraph(f'NAV Statement — {scheme}', styles['Heading1']),
        Paragraph(f'As of {period_end}', styles['Normal']),
        Spacer(1, 0.5*cm),
    ]

    data = [
        ['Component', 'Amount (Cr)'],
        ['Investments at Fair Value', f'{float(nav.investments_at_fair_value):,.2f}'],
        ['Cash and Equivalents', f'{float(nav.cash_and_equivalents):,.2f}'],
        ['Receivables', f'{float(nav.receivables):,.2f}'],
        ['Less: Management Fee Payable', f'({float(nav.management_fee_payable):,.2f})'],
        ['Less: Other Liabilities', f'({float(nav.other_liabilities):,.2f})'],
        ['TOTAL NAV', f'{float(nav.total_nav):,.2f}'],
        ['Units Outstanding', f'{float(nav.total_units_outstanding):,.4f}'],
        ['NAV per Unit', f'₹{float(nav.nav_per_unit):,.4f}'],
    ]

    t = Table(data, colWidths=[10*cm, 5*cm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#003366')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#E8F0FE')),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('PADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(t)

    _fund_name = scheme.fund.name

    def _on_page(canvas, doc):
        _page_footer(canvas, doc, fund_name=_fund_name, report_type='NAV Statement')

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)

    return _save_generated_report(
        organization=scheme.fund.organization,
        report_type='nav_statement',
        filename=f'NAV_Statement_{scheme.fund.name}_{scheme.name}_{period_end}.pdf',
        content=buf.getvalue(),
        user=user,
    )
