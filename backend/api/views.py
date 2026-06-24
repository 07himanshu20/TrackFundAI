"""
views.py
All REST API endpoints for the Analisa Resources MBR dashboard.
"""
import io
import os
import logging

from django.conf import settings
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from api import data_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _require_data():
    data = data_store.get_data()
    if data is None:
        return None, Response(
            {"error": "No MIS file loaded. Please upload an Excel file."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return data, None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([AllowAny])
def summary(request):
    """GET /api/summary/ — top-level KPI snapshot."""
    data, err = _require_data()
    if err:
        return err

    s = data.get("summary", {})
    cf = data.get("cash_flow", [])
    monthly = data.get("monthly_pl", [])
    meta = data_store.get_meta()

    # Latest closing cash from cash flow
    latest_cash = None
    if cf:
        last_cf = cf[-1]
        latest_cash = last_cf.get("closing_cash")

    # YTD Revenue: compare same months in 2025 vs 2024
    months_2025 = {m["month_num"] for m in monthly if m["year"] == 2025}
    ytd_2025 = sum(m["revenue"] for m in monthly if m["year"] == 2025)
    ytd_2024 = sum(m["revenue"] for m in monthly if m["year"] == 2024 and m["month_num"] in months_2025)
    yoy_growth = round((ytd_2025 - ytd_2024) / ytd_2024 * 100, 2) if ytd_2024 else None

    return Response({
        "company": data.get("company"),
        "currency": data.get("currency"),
        "report_month": data.get("report_month"),
        "loaded_at": meta.get("loaded_at"),
        "summary_pl": s,
        "latest_closing_cash": latest_cash,
        "ytd_revenue_2025": round(ytd_2025, 2),
        "ytd_revenue_2024": round(ytd_2024, 2),
        "yoy_revenue_growth_pct": yoy_growth,
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def monthly_pl(request):
    """GET /api/monthly-pl/?year=2024,2025 — monthly P&L trend."""
    data, err = _require_data()
    if err:
        return err

    years_param = request.query_params.get("year", "")
    if years_param:
        try:
            filter_years = [int(y.strip()) for y in years_param.split(",")]
        except ValueError:
            filter_years = []
    else:
        filter_years = []

    monthly = data.get("monthly_pl", [])
    if filter_years:
        monthly = [m for m in monthly if m["year"] in filter_years]

    return Response({"monthly_pl": monthly})


@api_view(["GET"])
@permission_classes([AllowAny])
def cash_flow(request):
    """GET /api/cash-flow/ — monthly cash flow statement."""
    data, err = _require_data()
    if err:
        return err
    return Response({"cash_flow": data.get("cash_flow", [])})


@api_view(["GET"])
@permission_classes([AllowAny])
def working_capital(request):
    """GET /api/working-capital/ — DSO/DIO/DPO/NWC metrics."""
    data, err = _require_data()
    if err:
        return err
    return Response({"working_capital": data.get("working_capital", {})})


@api_view(["GET"])
@permission_classes([AllowAny])
def sales_segments(request):
    """GET /api/sales-segments/ — revenue by business segment."""
    data, err = _require_data()
    if err:
        return err
    return Response({"sales_segments": data.get("sales_segments", {})})


@api_view(["GET"])
@permission_classes([AllowAny])
def full_data(request):
    """GET /api/full-data/ — complete parsed dataset (for AI context)."""
    data, err = _require_data()
    if err:
        return err
    return Response(data)


@api_view(["POST"])
@parser_classes([JSONParser])
@permission_classes([AllowAny])
def chat(request):
    """
    POST /api/chat/
    Body: { "message": str, "history": [...] }
    Calls Gemini with full portfolio context. Returns AI reply + highlights.
    """
    from api import gemini_service

    message = request.data.get("message", "").strip()
    history = request.data.get("history", [])

    if not message:
        return Response({"error": "message is required"}, status=status.HTTP_400_BAD_REQUEST)

    data = data_store.get_data()
    portfolio_context = data if data else {"note": "No MIS data loaded yet."}

    try:
        result = gemini_service.chat(message, history, portfolio_context)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as e:
        logger.exception("Chat endpoint error")
        return Response({"error": "AI service unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    return Response(result)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
@permission_classes([AllowAny])
def upload_mis(request):
    """
    POST /api/upload-mis/
    Accepts multipart Excel file upload, parses it, refreshes the in-memory store.
    """
    if "file" not in request.FILES:
        return Response({"error": "No file provided. Use field name 'file'."}, status=status.HTTP_400_BAD_REQUEST)

    uploaded_file = request.FILES["file"]
    filename = uploaded_file.name

    # Accept only .xlsx / .xls
    if not (filename.endswith(".xlsx") or filename.endswith(".xls")):
        return Response({"error": "Only .xlsx and .xls files are accepted."}, status=status.HTTP_400_BAD_REQUEST)

    # Save to media directory
    media_root = getattr(settings, "MEDIA_ROOT", "/tmp")
    os.makedirs(media_root, exist_ok=True)
    save_path = os.path.join(media_root, filename)

    with open(save_path, "wb") as f:
        for chunk in uploaded_file.chunks():
            f.write(chunk)

    # Parse and cache
    try:
        parsed_data = data_store.load_file(save_path)
    except Exception as e:
        logger.exception("Upload parse error")
        return Response({"error": f"Failed to parse file: {e}"}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    parse_report = parsed_data.get("parse_report", {})
    return Response({
        "message": "File uploaded and parsed successfully.",
        "company": parsed_data.get("company"),
        "report_month": parsed_data.get("report_month"),
        "monthly_periods": len(parsed_data.get("monthly_pl", [])),
        "cash_flow_periods": len(parsed_data.get("cash_flow", [])),
        "parse_report": parse_report,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([AllowAny])
def status_check(request):
    """GET /api/status/ — health check + data load status."""
    meta = data_store.get_meta()
    data = data_store.get_data()
    return Response({
        "status": "ok",
        "data_loaded": meta["has_data"],
        "loaded_at": meta["loaded_at"],
        "filepath": os.path.basename(meta["filepath"]) if meta["filepath"] else None,
        "report_month": data.get("report_month") if data else None,
    })


# ---------------------------------------------------------------------------
# AI Insights — Portfolio Risk Heatmap + Full Gemini Analysis
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ai_insights(request):
    """
    GET /api/ai-insights/
    Returns:
      - heatmap: [{company, sector, risk_score, risk_tier, moic, irr_pct}] for visual heatmap
      - full_analysis: Gemini markdown analysis of the entire portfolio
      - sector_summary: [{sector, avg_moic, avg_irr, company_count, avg_risk}]
    """
    import json
    import re
    from django.conf import settings
    from accounts.fund_access_helpers import get_accessible_fund_ids
    from investments.models import PortfolioCompany, Investment, Valuation
    from riskscore.models import CompanyRiskScore
    from collections import defaultdict
    from api.gemini_service import generate_content

    org = getattr(request, 'organization', None)
    if not org:
        try:
            from accounts.models import FundAccess
            fa = FundAccess.objects.filter(user=request.user, revoked_at__isnull=True).select_related('fund__organization').first()
            if fa and fa.fund:
                org = fa.fund.organization
        except Exception:
            pass
    if not org:
        try:
            from accounts.models import FundAccess
            fa = FundAccess.objects.filter(user=request.user).select_related('fund__organization').first()
            if fa and fa.fund:
                org = fa.fund.organization
        except Exception:
            pass
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)
    companies_qs = PortfolioCompany.objects.filter(organization=org, is_active=True)
    if fund_ids:
        companies_qs = companies_qs.filter(
            investments__scheme__fund__id__in=fund_ids
        ).distinct()

    # Build heatmap data
    heatmap = []
    sector_data = defaultdict(lambda: {'moics': [], 'irrs': [], 'risks': [], 'count': 0})

    # No row cap — render every active company in the scoped fund. The
    # previous [:60] cap was hiding portfolios larger than 60, which
    # silently truncated both the heatmap AND the sector summary
    # (sectors that only appeared in companies > 60 were missing).
    for co in companies_qs:
        inv = Investment.objects.filter(portfolio_company=co).order_by('-investment_date').first()
        val = Valuation.objects.filter(investment__portfolio_company=co).order_by('-valuation_date').first()
        risk = CompanyRiskScore.objects.filter(portfolio_company=co).order_by('-score_date').first()

        moic = None
        if inv and inv.total_invested and val and val.fair_value:
            try:
                moic = round(float(val.fair_value) / float(inv.total_invested), 2)
            except Exception:
                pass

        irr_pct = float(inv.irr_pct) if inv and inv.irr_pct is not None else None
        risk_score = float(risk.risk_score) if risk else 50.0
        risk_tier = risk.risk_tier if risk else 'medium'
        sector = co.sector or 'Other'

        entry = {
            'company_id': str(co.id),
            'company_name': co.name,
            'sector': sector,
            'stage': inv.stage if inv else '—',
            'moic': moic,
            'irr_pct': irr_pct,
            'risk_score': risk_score,
            'risk_tier': risk_tier,
        }
        heatmap.append(entry)

        sd = sector_data[sector]
        sd['count'] += 1
        if moic is not None: sd['moics'].append(moic)
        if irr_pct is not None: sd['irrs'].append(irr_pct)
        sd['risks'].append(risk_score)

    # Sector summary
    sector_summary = []
    for sector, sd in sector_data.items():
        sector_summary.append({
            'sector': sector,
            'company_count': sd['count'],
            'avg_moic': round(sum(sd['moics']) / len(sd['moics']), 2) if sd['moics'] else None,
            'avg_irr': round(sum(sd['irrs']) / len(sd['irrs']), 1) if sd['irrs'] else None,
            'avg_risk': round(sum(sd['risks']) / len(sd['risks']), 0) if sd['risks'] else None,
        })
    sector_summary.sort(key=lambda x: x['avg_moic'] or 0, reverse=True)

    # Full Gemini (Vertex AI) portfolio analysis
    full_analysis = ''
    try:
        if heatmap:
            portfolio_summary = json.dumps({
                'total_companies': len(heatmap),
                'sector_summary': sector_summary,
                'top_performers': sorted(
                    [h for h in heatmap if h['moic'] is not None],
                    key=lambda x: x['moic'], reverse=True
                )[:5],
                'high_risk': [h for h in heatmap if h['risk_tier'] == 'high'][:5],
            }, default=str)

            prompt = f"""You are a senior CA/CFO and fund analyst for an Indian AIF with 20+ years experience.
Write a comprehensive portfolio analysis (400-500 words) in professional markdown format.

Portfolio data:
{portfolio_summary}

Structure your analysis with these sections:
## Portfolio Overview
## Sector Analysis
## Top Performers
## Risk Assessment
## Key Recommendations

Be specific about numbers, sector trends, and actionable recommendations.
Use Indian financial context (₹ Crore, SEBI, AIF categories, Indian market dynamics)."""

            response = generate_content(prompt)
            full_analysis = response.text.strip()
    except Exception as e:
        logger.error('AI Insights Gemini error: %s', e)
        full_analysis = _build_rule_based_analysis(heatmap, sector_summary)

    return Response({
        'heatmap': heatmap,
        'sector_summary': sector_summary,
        'full_analysis': full_analysis,
        'total_companies': len(heatmap),
    })


def _build_rule_based_analysis(heatmap, sector_summary):
    """Fallback text analysis when Gemini is unavailable."""
    total = len(heatmap)
    high_risk = [h for h in heatmap if h['risk_tier'] == 'high']
    performers = [h for h in heatmap if h.get('moic') and h['moic'] >= 2.0]
    moics = [h['moic'] for h in heatmap if h['moic'] is not None]
    avg_moic = round(sum(moics) / len(moics), 2) if moics else 0

    top_sector = max(sector_summary, key=lambda s: s['avg_moic'] or 0) if sector_summary else {}

    return f"""## Portfolio Overview
Portfolio comprises **{total} active companies** with an average MOIC of **{avg_moic}x**.
{len(performers)} companies are outperforming (MOIC ≥ 2.0x) and {len(high_risk)} are flagged as high-risk.

## Sector Analysis
Top performing sector: **{top_sector.get('sector', 'N/A')}**
(avg MOIC: {top_sector.get('avg_moic', '—')}x, {top_sector.get('company_count', 0)} companies).

## Risk Assessment
{len(high_risk)} high-risk companies require immediate attention.
Run individual risk score computation for detailed signal breakdown.

## Key Recommendations
- Compute risk scores for all companies to enable full AI analysis
- Review high-risk companies for portfolio action (write-down, bridge, exit)
- Import KPI data (revenue, EBITDA, cash) to improve signal accuracy"""


# ---------------------------------------------------------------------------
# AI Predictions — Gemini-powered exit probability + revenue forecasting
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ai_predictions(request):
    """
    GET /api/ai-predictions/
    Uses Gemini to analyse all portfolio company data and return:
      - exit_probabilities per company (12-month horizon)
      - revenue_forecast (6-month aggregate)
      - portfolio_insights (risk score, outperformers, momentum)
    """
    import json
    import re
    from django.conf import settings
    from accounts.fund_access_helpers import get_accessible_fund_ids
    from investments.models import PortfolioCompany, Investment, Valuation
    from riskscore.models import CompanyRiskScore
    from api.gemini_service import generate_content, get_model_name

    org = getattr(request, 'organization', None)
    if not org:
        # Fallback: resolve org via FundAccess (revoked_at__isnull=True for active access)
        try:
            from accounts.models import FundAccess
            fa = FundAccess.objects.filter(user=request.user, revoked_at__isnull=True).select_related('fund__organization').first()
            if fa and fa.fund:
                org = fa.fund.organization
        except Exception:
            pass
    if not org:
        # Last resort: any fund this user has ever had access to
        try:
            from accounts.models import FundAccess
            fa = FundAccess.objects.filter(user=request.user).select_related('fund__organization').first()
            if fa and fa.fund:
                org = fa.fund.organization
        except Exception:
            pass

    if not org:
        return Response({'detail': 'No organization found for user.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)

    # Collect portfolio company data — include all org companies even without investments
    companies_qs = PortfolioCompany.objects.filter(organization=org, is_active=True)
    if fund_ids:
        companies_qs = companies_qs.filter(
            investments__scheme__fund__id__in=fund_ids
        ).distinct()
    if not companies_qs.exists():
        # Fallback: all active companies in org regardless of fund access
        companies_qs = PortfolioCompany.objects.filter(organization=org, is_active=True)

    company_data = []
    for co in companies_qs[:50]:  # cap at 50 to keep prompt manageable
        inv = Investment.objects.filter(portfolio_company=co).order_by('-investment_date').first()
        val = Valuation.objects.filter(investment__portfolio_company=co).order_by('-valuation_date').first()
        risk = CompanyRiskScore.objects.filter(portfolio_company=co).order_by('-score_date').first()

        moic = None
        if inv and inv.total_invested and val and val.fair_value:
            try:
                moic = round(float(val.fair_value) / float(inv.total_invested), 2)
            except Exception:
                pass

        company_data.append({
            'id': str(co.id),
            'name': co.name,
            'sector': co.sector or 'Unknown',
            'stage': inv.stage if inv else 'Unknown',
            'moic': moic,
            'irr_pct': float(inv.irr_pct) if inv and inv.irr_pct is not None else None,
            'cost_inr_cr': float(inv.total_invested) if inv and inv.total_invested else None,
            'fv_inr_cr': float(val.fair_value) if val and val.fair_value else None,
            'risk_score': float(risk.risk_score) if risk else None,
            'risk_tier': risk.risk_tier if risk else None,
        })

    if not company_data:
        return Response({
            'exit_probabilities': [],
            'revenue_forecast': {'months': [], 'values': [], 'growth_cagr_pct': 0},
            'portfolio_insights': {'avg_risk_score': 0, 'outperformers_count': 0, 'underperformers_count': 0,
                                   'sector_alpha_tech_pct': 0, 'portfolio_momentum': 'Unknown', 'rev_growth_cagr': 0},
            'peer_benchmarking': [],
        })

    # Build Gemini prompt
    companies_json = json.dumps(company_data, default=str)
    prompt = f"""You are a senior fund analyst for Indian AIFs with 20+ years of PE/VC experience.
Analyze this portfolio and return predictions as EXACT JSON only (no markdown, no code fences, no explanation).

Portfolio companies:
{companies_json}

Return this EXACT JSON structure:
{{
  "exit_probabilities": [
    {{
      "company_id": "<id>",
      "company_name": "<name>",
      "stage": "<stage>",
      "moic": <float or null>,
      "exit_prob_12m": <integer 0-100>,
      "expected_exit_type": "<IPO|Strategic Sale|Secondary|Write-off>",
      "reasoning": "<1 sentence>"
    }}
  ],
  "revenue_forecast": {{
    "months": ["Nov-25","Dec-25","Jan-26","Feb-26","Mar-26","Jun-26"],
    "values": [<6 floats in ₹Cr consolidated portfolio>],
    "growth_cagr_pct": <float>,
    "confidence": "<high|medium|low>",
    "methodology": "<brief note>"
  }},
  "portfolio_insights": {{
    "avg_risk_score": <integer 1-100>,
    "outperformers_count": <integer — IRR > 25%>,
    "underperformers_count": <integer — IRR < 5% or risk_tier high>,
    "rev_growth_cagr": <float — 6-month annualised revenue CAGR %>,
    "sector_alpha_tech_pct": <float — tech sector alpha vs benchmark %>,
    "portfolio_momentum": "<Strong ↑|Moderate ↑|Stable|Weak ↓|Declining ↓>"
  }},
  "peer_benchmarking": [
    {{
      "company_name": "<name>",
      "sector": "<sector>",
      "moic": <float>,
      "irr_pct": <float>,
      "benchmark_moic": <float — typical for stage/sector>,
      "benchmark_irr": <float>,
      "outperforming": <true|false>
    }}
  ]
}}

Rules:
- MOIC > 3.0x + Pre-IPO/Series D+: exit_prob_12m 40-65
- MOIC 2.0-3.0x + Series C: exit_prob_12m 20-40
- MOIC < 1.0x or no data: exit_prob_12m 5-20
- IRR > 25%: outperformer; IRR < 5% or risk_tier=high: underperformer
- FinTech/SaaS exit faster; Healthcare/CleanTech slower
- Revenue forecast should show a realistic growth curve based on MOIC trends
- peer_benchmarking: include top 8 companies by MOIC
- Return valid JSON only"""

    try:
        response = generate_content(prompt)
        raw = response.text.strip()

        # Strip markdown code fences if Gemini wraps in ```json ... ```
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'```\s*$', '', raw, flags=re.MULTILINE).strip()

        predictions = json.loads(raw)
    except Exception as e:
        logger.error('Gemini predictions error: %s', e)
        # Fallback: derive basic values from data without AI
        predictions = _fallback_predictions(company_data)

    return Response(predictions)


def _fallback_predictions(company_data):
    """Rule-based fallback when Gemini is unavailable."""
    import datetime
    exit_probs = []
    outperformers = 0
    underperformers = 0

    for co in company_data:
        moic = co.get('moic') or 1.0
        irr = co.get('irr_pct') or 0
        stage = (co.get('stage') or '').lower()
        risk_tier = co.get('risk_tier') or 'medium'

        if moic >= 3.0 and ('pre-ipo' in stage or 'series d' in stage or 'buyout' in stage):
            prob = 55
            exit_type = 'IPO'
        elif moic >= 2.0:
            prob = 30
            exit_type = 'Strategic Sale'
        elif moic < 1.0 or risk_tier == 'high':
            prob = 10
            exit_type = 'Write-off'
        else:
            prob = 20
            exit_type = 'Secondary'

        if irr and irr > 25:
            outperformers += 1
        if irr is not None and (irr < 5 or risk_tier == 'high'):
            underperformers += 1

        exit_probs.append({
            'company_id': co['id'],
            'company_name': co['name'],
            'stage': co.get('stage', '—'),
            'moic': moic,
            'exit_prob_12m': prob,
            'expected_exit_type': exit_type,
            'reasoning': 'Rule-based estimate (AI unavailable)',
        })

    # Simple revenue forecast: flat growth
    today = datetime.date.today()
    months = []
    values = []
    base = sum(co.get('fv_inr_cr') or 0 for co in company_data) * 0.15
    for i in range(6):
        m = today.replace(day=1)
        import calendar
        days = calendar.monthrange(m.year, m.month)[1]
        future = today.replace(day=1)
        # advance i months
        month_num = (today.month + i - 1) % 12 + 1
        year_num = today.year + (today.month + i - 1) // 12
        months.append(f"{datetime.date(year_num, month_num, 1).strftime('%b-%y')}")
        values.append(round(base * (1 + 0.015 * i), 1))

    risk_scores = [co['risk_score'] for co in company_data if co.get('risk_score')]
    avg_risk = round(sum(risk_scores) / len(risk_scores)) if risk_scores else 68

    peer = [
        {
            'company_name': co['name'],
            'sector': co.get('sector', '—'),
            'moic': co.get('moic') or 1.0,
            'irr_pct': co.get('irr_pct') or 0,
            'benchmark_moic': 2.0,
            'benchmark_irr': 18.0,
            'outperforming': (co.get('moic') or 0) > 2.0,
        }
        for co in sorted(company_data, key=lambda x: x.get('moic') or 0, reverse=True)[:8]
    ]

    return {
        'exit_probabilities': exit_probs,
        'revenue_forecast': {
            'months': months,
            'values': values,
            'growth_cagr_pct': 15.0,
            'confidence': 'low',
            'methodology': 'Rule-based fallback',
        },
        'portfolio_insights': {
            'avg_risk_score': avg_risk,
            'outperformers_count': outperformers,
            'underperformers_count': underperformers,
            'rev_growth_cagr': 15.0,
            'sector_alpha_tech_pct': 3.2,
            'portfolio_momentum': 'Moderate ↑',
        },
        'peer_benchmarking': peer,
    }


# ---------------------------------------------------------------------------
# Report Generation — Fund-Level and Company-Level MIS reports
# ---------------------------------------------------------------------------

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def generate_mis_report(request):
    """
    POST /api/generate-report/
    Body: { "report_type": "monthly_nav" | "quarterly_lp" | "valuation_cert" |
                           "capital_account" | "annual_fund" | "waterfall_carry" |
                           "pl_mis" | "balance_sheet" | "cash_flow" | "bva" |
                           "saas_kpi" | "sector_kpi",
            "fund_id": "<uuid>",
            "scheme_id": "<uuid>" (optional) }
    Returns: { "report_type": ..., "title": ..., "content": [...rows], "generated_at": ... }
    """
    import json
    import re
    import datetime
    from django.conf import settings
    from api.gemini_service import generate_content

    org = getattr(request, 'organization', None)
    if not org and request.user and request.user.is_authenticated:
        from accounts.models import Organization
        org = Organization.objects.filter(users=request.user).first()

    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    report_type = request.data.get('report_type', '')
    fund_id = request.data.get('fund_id')

    REPORT_META = {
        'monthly_nav':     'Monthly NAV Report',
        'quarterly_lp':    'Quarterly LP Letter',
        'valuation_cert':  'Valuation Certification Report',
        'capital_account': 'Capital Account Statement',
        'annual_fund':     'Annual Fund Report',
        'waterfall_carry': 'Waterfall & Carry Schedule',
        'pl_mis':          'P&L (Monthly MIS)',
        'balance_sheet':   'Balance Sheet Snapshot',
        'cash_flow':       'Cash Flow Statement',
        'bva':             'Budget vs Actual',
        'saas_kpi':        'SaaS KPI Report (Tech)',
        'sector_kpi':      'Sector KPI Dashboard',
    }

    title = REPORT_META.get(report_type, 'Report')

    # Collect relevant data for the report
    context_data = _collect_report_data(org, fund_id, report_type)

    prompt = f"""You are a CA/CFO with 20+ years of Indian AIF fund reporting experience.
Generate a "{title}" report for this fund in EXACT JSON only (no markdown, no code fences).

Fund data:
{json.dumps(context_data, default=str)}

Return JSON:
{{
  "title": "{title}",
  "period": "<e.g. Apr 2025 – Mar 2026>",
  "summary": "<2-3 sentence executive summary>",
  "sections": [
    {{
      "heading": "<section heading>",
      "rows": [
        {{"label": "<metric>", "value": "<formatted value>", "note": "<optional note>"}}
      ]
    }}
  ],
  "highlights": ["<bullet 1>", "<bullet 2>", "<bullet 3>"],
  "risk_flags": ["<risk 1 if any>"]
}}"""

    try:
        response = generate_content(prompt)
        raw = response.text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'```\s*$', '', raw, flags=re.MULTILINE).strip()
        report_content = json.loads(raw)
    except Exception as e:
        logger.error('Report generation error: %s', e)
        report_content = {
            'title': title,
            'period': f"FY {datetime.date.today().year}",
            'summary': f'{title} could not be generated — check AI configuration.',
            'sections': [],
            'highlights': [],
            'risk_flags': [],
        }

    # Generate watermarked PDF from the report content
    pdf_url = None
    report_id = None
    try:
        pdf_bytes = _generate_report_pdf(report_content, title, fund_id, org)
        if pdf_bytes:
            from reporting.models import GeneratedReport
            from django.core.files.base import ContentFile
            report_obj = GeneratedReport.objects.create(
                organization=org,
                report_type=report_type,
                report_format='pdf',
                file_size=len(pdf_bytes),
                generated_by=request.user,
            )
            fname = f'{title.replace(" ", "_")}_{datetime.date.today()}.pdf'
            report_obj.file.save(fname, ContentFile(pdf_bytes), save=True)
            pdf_url = report_obj.file.url
            report_id = str(report_obj.id)
    except Exception as e:
        logger.warning('PDF generation for %s failed: %s', report_type, e)

    return Response({
        'report_type': report_type,
        'generated_at': datetime.datetime.now().isoformat(),
        'pdf_url': pdf_url,
        'report_id': report_id,
        **report_content,
    })


def _generate_report_pdf(report_content, title, fund_id, org):
    """Generate a watermarked PDF from Gemini-generated JSON report content."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
        from reporting.report_generator import _draw_watermark, _page_footer
    except ImportError:
        return None

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=2.2 * cm, bottomMargin=2.2 * cm,
                            leftMargin=2 * cm, rightMargin=2 * cm)

    styles = getSampleStyleSheet()
    DARK_BLUE = colors.HexColor('#003366')
    MID_BLUE = colors.HexColor('#0066CC')
    LIGHT_BG = colors.HexColor('#F5F8FF')
    GREY = colors.HexColor('#4B5563')

    title_style = ParagraphStyle('title', parent=styles['Heading1'],
                                  fontSize=20, textColor=DARK_BLUE, spaceAfter=6,
                                  alignment=TA_CENTER)
    section_style = ParagraphStyle('section', parent=styles['Heading2'],
                                    fontSize=13, textColor=DARK_BLUE,
                                    spaceBefore=14, spaceAfter=6)
    body_style = ParagraphStyle('body', parent=styles['Normal'],
                                 fontSize=9, leading=13, alignment=TA_JUSTIFY)
    small_style = ParagraphStyle('small', parent=styles['Normal'],
                                  fontSize=8, textColor=GREY)

    story = []

    # Title
    story.append(Spacer(1, 1.5 * cm))
    story.append(Paragraph(title, title_style))

    period = report_content.get('period', '')
    if period:
        story.append(Paragraph(period, ParagraphStyle(
            'period', parent=styles['Normal'], fontSize=11,
            textColor=MID_BLUE, alignment=TA_CENTER)))
    story.append(Spacer(1, 0.3 * cm))
    story.append(HRFlowable(width='80%', thickness=1, color=DARK_BLUE, hAlign='CENTER'))
    story.append(Spacer(1, 0.5 * cm))

    # Summary
    summary = report_content.get('summary', '')
    if summary:
        story.append(Paragraph('Executive Summary', section_style))
        story.append(Paragraph(summary, body_style))
        story.append(Spacer(1, 0.3 * cm))

    # Risk flags
    risk_flags = report_content.get('risk_flags', [])
    if risk_flags:
        story.append(Paragraph('Risk Flags', section_style))
        for flag in risk_flags:
            story.append(Paragraph(f'  {flag}', ParagraphStyle(
                'flag', parent=body_style, textColor=colors.HexColor('#DC2626'))))
        story.append(Spacer(1, 0.3 * cm))

    # Sections
    for sec in report_content.get('sections', []):
        heading = sec.get('heading', 'Section')
        story.append(Paragraph(heading, section_style))
        rows_data = sec.get('rows', [])
        if rows_data:
            table_data = [['Metric', 'Value', 'Note']]
            for row in rows_data:
                table_data.append([
                    row.get('label', ''),
                    row.get('value', ''),
                    row.get('note', ''),
                ])
            t = Table(table_data, colWidths=[5.5 * cm, 5.5 * cm, 5.5 * cm])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), DARK_BLUE),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#D1D5DB')),
                ('PADDING', (0, 0), (-1, -1), 5),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
            ]))
            story.append(t)
        story.append(Spacer(1, 0.3 * cm))

    # Highlights
    highlights = report_content.get('highlights', [])
    if highlights:
        story.append(Paragraph('Key Highlights', section_style))
        for h in highlights:
            story.append(Paragraph(f'  {h}', body_style))
        story.append(Spacer(1, 0.3 * cm))

    # Disclaimer
    story.append(Spacer(1, 1 * cm))
    story.append(HRFlowable(width='100%', thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph(
        'This report is generated by TrackFundAI and is classified as CONFIDENTIAL. '
        'All values are indicative and subject to final audit. '
        'For authorised recipients only.',
        small_style
    ))

    fund_name = ''
    if fund_id:
        from funds.models import Fund
        try:
            fund_name = Fund.objects.get(pk=fund_id).name
        except Exception:
            pass

    def _on_page(canvas, doc):
        _page_footer(canvas, doc, fund_name=fund_name, report_type=title)

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return buf.getvalue()


def _collect_report_data(org, fund_id, report_type):
    """Collect relevant DB data for a given report type."""
    data = {'fund_id': str(fund_id) if fund_id else None}

    try:
        from investments.models import PortfolioCompany, Investment, Valuation
        from accounting.models import NAVRecord, CarriedInterest, ManagementFeeSchedule
        from lp.models import Investor, CapitalCall, Distribution
        from mis_consolidation.models import BudgetVsActual, ConsolidatedMIS

        cos = PortfolioCompany.objects.filter(organization=org, is_active=True)
        data['company_count'] = cos.count()
        data['companies'] = list(cos.values('name', 'sector')[:20])

        if report_type in ('monthly_nav', 'annual_fund', 'waterfall_carry'):
            navs = NAVRecord.objects.filter(
                scheme__fund__organization=org
            ).order_by('-nav_date')[:12]
            data['nav_records'] = [
                {
                    'date': str(n.nav_date),
                    'total_nav': float(n.total_nav),
                    'nav_per_unit': float(n.nav_per_unit),
                    'total_units': float(n.total_units_outstanding),
                    'mgmt_fee': float(n.management_fee_payable),
                }
                for n in navs
            ]

        if report_type == 'waterfall_carry':
            carries = CarriedInterest.objects.filter(
                scheme__fund__organization=org
            ).order_by('-calculation_date')[:6]
            data['carried_interest'] = [
                {
                    'date': str(c.calculation_date),
                    'carry_base': float(c.carry_base),
                    'carry_gross': float(c.carry_amount_gross),
                    'carry_net': float(c.carry_amount_net),
                    'clawback': float(c.gp_clawback_provision),
                }
                for c in carries
            ]

        if report_type in ('quarterly_lp', 'capital_account'):
            calls = CapitalCall.objects.filter(scheme__fund__organization=org)[:20]
            data['capital_calls'] = [
                {
                    'call_number': c.call_number,
                    'date': str(c.call_date) if c.call_date else None,
                    'amount': float(c.total_amount_inr),
                    'status': c.status,
                }
                for c in calls
            ]
            dists = Distribution.objects.filter(scheme__fund__organization=org)[:20]
            data['distributions'] = [
                {
                    'date': str(d.distribution_date) if d.distribution_date else None,
                    'amount': float(d.amount_inr),
                    'type': d.distribution_type,
                }
                for d in dists
            ]

        if report_type in ('pl_mis', 'bva', 'balance_sheet', 'cash_flow'):
            bva = BudgetVsActual.objects.filter(
                portfolio_company__organization=org
            ).order_by('-period_year', '-period_month')[:40]
            data['bva_records'] = [
                {
                    'company': b.portfolio_company.name,
                    'line_item': b.line_item,
                    'budget': float(b.budget_inr) if b.budget_inr else None,
                    'actual': float(b.actual_inr) if b.actual_inr else None,
                    'variance_pct': float(b.variance_pct) if b.variance_pct else None,
                    'period': f"{b.period_year}-{b.period_month or 'Q'}",
                }
                for b in bva
            ]

        if report_type in ('valuation_cert', 'annual_fund'):
            vals = Valuation.objects.filter(
                investment__portfolio_company__organization=org
            ).order_by('-valuation_date')[:20]
            data['valuations'] = [
                {
                    'company': v.investment.portfolio_company.name,
                    'date': str(v.valuation_date),
                    'fair_value': float(v.fair_value),
                    'method': v.methodology,
                }
                for v in vals
            ]

    except Exception as e:
        logger.error('Report data collection error: %s', e)

    return data


# ---------------------------------------------------------------------------
# Comprehensive MIS Report (PDF + on-screen)
# ---------------------------------------------------------------------------
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def generate_comprehensive_mis_report(request):
    """
    POST /api/generate-comprehensive-mis/
    Body: { "fund_id": "<uuid>" }
    Returns: JSON with on-screen report data + PDF download URL.
    """
    from funds.models import Fund
    from reporting.comprehensive_mis_report import generate_comprehensive_mis
    from reporting.models import GeneratedReport
    from django.core.files.base import ContentFile
    import datetime

    org = getattr(request, 'organization', None)
    if not org and request.user and request.user.is_authenticated:
        from accounts.models import Organization
        org = Organization.objects.filter(users=request.user).first()
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_id = request.data.get('fund_id')
    if not fund_id:
        return Response({'detail': 'fund_id required.'}, status=400)

    try:
        fund = Fund.objects.get(pk=fund_id, organization=org)
    except Fund.DoesNotExist:
        return Response({'detail': 'Fund not found.'}, status=404)

    pdf_bytes, report_data = generate_comprehensive_mis(fund, user=request.user)

    if not pdf_bytes:
        return Response({
            'detail': 'Report generation failed. Check server logs.',
            **report_data,
        }, status=500)

    # Save PDF to storage
    report = GeneratedReport.objects.create(
        organization=org,
        report_type='comprehensive_mis',
        report_format='pdf',
        file_size=len(pdf_bytes),
        generated_by=request.user,
    )
    filename = f'Comprehensive_MIS_{fund.name}_{datetime.date.today()}.pdf'
    report.file.save(filename, ContentFile(pdf_bytes), save=True)

    return Response({
        'report_type': 'comprehensive_mis',
        'title': f'Comprehensive MIS Report — {fund.name}',
        'generated_at': report_data.get('generated_at', datetime.datetime.now().isoformat()),
        'total_pages': report_data.get('total_pages', 0),
        'sections': report_data.get('sections', []),
        'pdf_url': report.file.url if report.file else None,
        'report_id': str(report.id),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_report(request, report_id):
    """
    GET /api/download-report/<uuid>/
    Serve a generated report PDF for download.
    """
    from reporting.models import GeneratedReport
    from django.http import FileResponse

    org = getattr(request, 'organization', None)
    if not org and request.user and request.user.is_authenticated:
        from accounts.models import Organization
        org = Organization.objects.filter(users=request.user).first()

    try:
        report = GeneratedReport.objects.get(pk=report_id, organization=org)
    except GeneratedReport.DoesNotExist:
        return Response({'detail': 'Report not found.'}, status=404)

    if not report.file:
        return Response({'detail': 'Report file not available.'}, status=404)

    return FileResponse(
        report.file.open('rb'),
        content_type='application/pdf',
        as_attachment=True,
        filename=report.file.name.split('/')[-1],
    )
