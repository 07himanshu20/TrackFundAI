from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsGPUser
from accounts.fund_access_helpers import get_accessible_fund_ids
from .models import CompanyRiskScore
from .scoring_engine import compute_risk_score


@api_view(['GET'])
@permission_classes([IsGPUser])
def risk_score_list(request):
    """
    List latest risk scores for all portfolio companies accessible to the user.
    Optional filters: ?tier=high&fund_id=<uuid>
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)

    # Get all accessible portfolio companies
    from investments.models import PortfolioCompany
    companies = PortfolioCompany.objects.filter(
        organization=org,
        investments__scheme__fund__id__in=fund_ids,
        is_active=True,
    ).distinct()

    tier_filter = request.query_params.get('tier')
    fund_filter = request.query_params.get('fund_id')

    from investments.models import Investment

    # Get latest risk score per company
    result = []
    for company in companies:
        latest = (
            CompanyRiskScore.objects.filter(portfolio_company=company)
            .order_by('-score_date')
            .first()
        )
        if latest:
            if tier_filter and latest.risk_tier != tier_filter:
                continue
            trend = None
            if latest.previous_score is not None:
                diff = float(latest.risk_score) - float(latest.previous_score)
                trend = 'up' if diff > 1 else ('down' if diff < -1 else 'stable')

            # Pull stage and IRR from the most recent investment
            latest_inv = (
                Investment.objects.filter(portfolio_company=company)
                .order_by('-investment_date')
                .first()
            )
            stage = latest_inv.stage if latest_inv else None
            irr_pct = float(latest_inv.irr_pct) if latest_inv and latest_inv.irr_pct is not None else None

            result.append({
                'company_id': str(company.id),
                'company_name': company.name,
                'sector': company.sector,
                'stage': stage,
                'irr_pct': irr_pct,
                'risk_score': float(latest.risk_score),
                'risk_tier': latest.risk_tier,
                'score_date': str(latest.score_date),
                'trend': trend,
                'flags': latest.flags,
                'ai_commentary': latest.ai_commentary,
                'signals': {
                    'revenue_vs_plan':       float(latest.signal_revenue_vs_plan),
                    'ebitda_margin_trend':   float(latest.signal_ebitda_margin_trend),
                    'cash_burn_runway':      float(latest.signal_cash_burn_runway),
                    'working_capital':       float(latest.signal_working_capital),
                    'debt_service':          float(latest.signal_debt_service),
                    'customer_concentration':float(latest.signal_customer_concentration),
                    'mgmt_changes':          float(latest.signal_mgmt_changes),
                    'market_conditions':     float(latest.signal_market_conditions),
                    'peer_comparisons':      float(latest.signal_peer_comparisons),
                    'compliance_status':     float(latest.signal_compliance_status),
                },
            })

    # Sort by risk score descending (highest risk first)
    result.sort(key=lambda x: x['risk_score'], reverse=True)
    return Response(result)


@api_view(['POST'])
@permission_classes([IsGPUser])
def compute_score(request, company_id):
    """Manually trigger risk score computation for a specific company."""
    from investments.models import PortfolioCompany
    import datetime

    org = request.organization
    try:
        company = PortfolioCompany.objects.get(pk=company_id, organization=org)
    except PortfolioCompany.DoesNotExist:
        return Response({'detail': 'Company not found.'}, status=404)

    as_of_date = None
    raw = request.data.get('as_of_date')
    if raw:
        try:
            as_of_date = datetime.date.fromisoformat(raw)
        except ValueError:
            return Response({'detail': 'Invalid date format.'}, status=400)

    score = compute_risk_score(company, as_of_date)

    return Response({
        'company_id': str(company.id),
        'company_name': company.name,
        'risk_score': float(score.risk_score),
        'risk_tier': score.risk_tier,
        'score_date': str(score.score_date),
        'flags': score.flags,
        'ai_commentary': score.ai_commentary,
    })


@api_view(['GET'])
@permission_classes([IsGPUser])
def fund_risk_summary(request):
    """
    Fund-level composite risk summary: count by tier + weighted avg score.
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)

    from investments.models import PortfolioCompany
    from django.db.models import Avg, Count

    companies = PortfolioCompany.objects.filter(
        organization=org,
        investments__scheme__fund__id__in=fund_ids,
        is_active=True,
    ).distinct()

    tiers = {'low': 0, 'medium': 0, 'high': 0}
    scores = []

    for company in companies:
        latest = (
            CompanyRiskScore.objects.filter(portfolio_company=company)
            .order_by('-score_date')
            .first()
        )
        if latest:
            tiers[latest.risk_tier] = tiers.get(latest.risk_tier, 0) + 1
            scores.append(float(latest.risk_score))

    avg_score = round(sum(scores) / len(scores), 2) if scores else 0

    return Response({
        'portfolio_count': len(companies),
        'scored_count': len(scores),
        'average_risk_score': avg_score,
        'tier_breakdown': tiers,
        'fund_risk_tier': (
            'high' if tiers['high'] > tiers['low'] else
            ('medium' if tiers['medium'] >= tiers['low'] else 'low')
        ),
    })


@api_view(['POST'])
@permission_classes([IsGPUser])
def compute_all_scores(request):
    """
    POST /api/risk-scores/compute-all/
    Bulk-computes risk scores for all portfolio companies accessible to the user.
    Caps at 50 companies per call to avoid timeouts.
    Returns list of computed scores.
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fund_ids = get_accessible_fund_ids(request.user)

    from investments.models import PortfolioCompany, Investment
    companies = PortfolioCompany.objects.filter(
        organization=org,
        is_active=True,
    ).distinct()

    if fund_ids:
        companies = companies.filter(
            investments__scheme__fund__id__in=fund_ids
        ).distinct()

    companies = list(companies[:50])

    computed = []
    errors = []
    for company in companies:
        try:
            score = compute_risk_score(company)
            computed.append({
                'company_id':   str(company.id),
                'company_name': company.name,
                'risk_score':   float(score.risk_score),
                'risk_tier':    score.risk_tier,
            })
        except Exception as e:
            errors.append({'company_id': str(company.id), 'error': str(e)})

    return Response({
        'computed': len(computed),
        'errors':   len(errors),
        'results':  computed,
    })
