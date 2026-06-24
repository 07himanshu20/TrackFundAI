"""
Risk Scoring Engine — 10-signal composite risk score for portfolio companies.

Phase 1: Rule-based scoring (deterministic, no ML training required)
Phase 2: XGBoost ensemble (requires historical labeled data)

Signal weights (v5 spec):
  Revenue vs Plan:        15%
  EBITDA Margin Trend:    15%
  Cash Burn & Runway:     15%
  Working Capital:        10%
  Debt Service Coverage:  10%
  Customer Concentration: 10%
  Management Changes:      5%
  Market Conditions:       5%
  Peer Comparisons:       10%
  Compliance Status:       5%

Each signal scored 0-10 (0 = lowest risk, 10 = highest risk).
Composite = weighted sum (maps to 0-100).
"""

import logging
from decimal import Decimal
from datetime import date, timedelta
from typing import Optional

from django.utils import timezone

logger = logging.getLogger(__name__)

# Signal weights (must sum to 1.0)
SIGNAL_WEIGHTS = {
    'revenue_vs_plan':          0.15,
    'ebitda_margin_trend':      0.15,
    'cash_burn_runway':         0.15,
    'working_capital':          0.10,
    'debt_service':             0.10,
    'customer_concentration':   0.10,
    'mgmt_changes':             0.05,
    'market_conditions':        0.05,
    'peer_comparisons':         0.10,
    'compliance_status':        0.05,
}


def compute_risk_score(portfolio_company, as_of_date: Optional[date] = None):
    """
    Compute and persist a risk score for a portfolio company.

    Args:
        portfolio_company: investments.PortfolioCompany instance
        as_of_date: Scoring date; defaults to today

    Returns:
        CompanyRiskScore instance
    """
    from riskscore.models import CompanyRiskScore

    if as_of_date is None:
        as_of_date = timezone.now().date()

    signals = {}
    flags = []

    # -- Compute each signal --
    signals['revenue_vs_plan']        = _score_revenue_vs_plan(portfolio_company, as_of_date, flags)
    signals['ebitda_margin_trend']    = _score_ebitda_margin_trend(portfolio_company, as_of_date, flags)
    signals['cash_burn_runway']       = _score_cash_burn_runway(portfolio_company, as_of_date, flags)
    signals['working_capital']        = _score_working_capital(portfolio_company, as_of_date, flags)
    signals['debt_service']           = _score_debt_service(portfolio_company, as_of_date, flags)
    signals['customer_concentration'] = _score_customer_concentration(portfolio_company, as_of_date, flags)
    signals['mgmt_changes']           = _score_mgmt_changes(portfolio_company, as_of_date, flags)
    signals['market_conditions']      = _score_market_conditions(portfolio_company, as_of_date, flags)
    signals['peer_comparisons']       = _score_peer_comparisons(portfolio_company, as_of_date, flags)
    signals['compliance_status']      = _score_compliance_status(portfolio_company, as_of_date, flags)

    # -- Composite score (weighted sum, 0-100) --
    composite = sum(
        signals[key] * weight * 10
        for key, weight in SIGNAL_WEIGHTS.items()
    )
    composite = round(min(max(composite, 0), 100), 2)

    # -- Tier --
    if composite <= 33:
        tier = 'low'
    elif composite <= 66:
        tier = 'medium'
    else:
        tier = 'high'

    # -- Previous score for trend --
    prev = (
        CompanyRiskScore.objects.filter(
            portfolio_company=portfolio_company,
            score_date__lt=as_of_date,
        ).order_by('-score_date').first()
    )

    # -- AI commentary (Gemini) --
    ai_commentary = _generate_commentary(portfolio_company, composite, tier, flags, signals)

    # -- Persist --
    from django.db import transaction
    with transaction.atomic():
        score_record, _ = CompanyRiskScore.objects.update_or_create(
            portfolio_company=portfolio_company,
            score_date=as_of_date,
            defaults={
                'risk_score': Decimal(str(composite)),
                'risk_tier': tier,
                'method': 'rule_based',
                'signal_revenue_vs_plan':        Decimal(str(signals['revenue_vs_plan'])),
                'signal_ebitda_margin_trend':     Decimal(str(signals['ebitda_margin_trend'])),
                'signal_cash_burn_runway':        Decimal(str(signals['cash_burn_runway'])),
                'signal_working_capital':         Decimal(str(signals['working_capital'])),
                'signal_debt_service':            Decimal(str(signals['debt_service'])),
                'signal_customer_concentration':  Decimal(str(signals['customer_concentration'])),
                'signal_mgmt_changes':            Decimal(str(signals['mgmt_changes'])),
                'signal_market_conditions':       Decimal(str(signals['market_conditions'])),
                'signal_peer_comparisons':        Decimal(str(signals['peer_comparisons'])),
                'signal_compliance_status':       Decimal(str(signals['compliance_status'])),
                'flags': flags,
                'ai_commentary': ai_commentary,
                'previous_score': Decimal(str(prev.risk_score)) if prev else None,
            },
        )

    return score_record


# ── Signal computation functions ──────────────────────────────────────────────

def _get_recent_kpis(company, slug: str, n_periods: int = 3):
    """Fetch the n most recent KPI values for a company and KPI slug."""
    from investments.models import PortfolioKPI, KPIDefinition
    return (
        PortfolioKPI.objects.filter(
            portfolio_company=company,
            kpi_definition__slug=slug,
            status='approved',
        )
        .order_by('-period')
        .values_list('value', flat=True)[:n_periods]
    )


def _score_revenue_vs_plan(company, as_of_date, flags):
    """Revenue actual vs budget. Sources: PortfolioKPI (revenue, revenue_plan)."""
    try:
        actual_vals = list(_get_recent_kpis(company, 'revenue', 3))
        plan_vals   = list(_get_recent_kpis(company, 'revenue-plan', 3))
        if not actual_vals or not plan_vals:
            return 5.0  # No data → medium risk

        latest_actual = float(actual_vals[0])
        latest_plan   = float(plan_vals[0])

        if latest_plan <= 0:
            return 5.0

        variance_pct = (latest_actual - latest_plan) / latest_plan * 100

        if variance_pct >= 0:
            return 0.0  # At or above plan
        elif variance_pct >= -10:
            return 2.0
        elif variance_pct >= -20:
            score = 4.0
            flags.append(f'{company.name}: Revenue {variance_pct:.1f}% below plan')
            return score
        elif variance_pct >= -30:
            flags.append(f'{company.name}: Revenue {variance_pct:.1f}% below plan — WARNING')
            return 6.0
        else:
            flags.append(f'{company.name}: Revenue {variance_pct:.1f}% below plan — CRITICAL')
            return 9.0
    except Exception:
        return 5.0


def _score_ebitda_margin_trend(company, as_of_date, flags):
    """EBITDA margin trend over last 3 periods."""
    try:
        margins = list(_get_recent_kpis(company, 'ebitda-margin', 3))
        if len(margins) < 2:
            return 5.0

        latest = float(margins[0])
        prev   = float(margins[1])

        if latest < 0:
            flags.append(f'{company.name}: Negative EBITDA margin ({latest:.1f}%)')
            return 8.0
        elif latest < 5:
            return 6.0
        elif latest < 10:
            # Check trend
            if latest < prev:
                flags.append(f'{company.name}: EBITDA margin declining ({prev:.1f}%→{latest:.1f}%)')
                return 5.0
            return 3.0
        else:
            return max(0.0, 2.0 - (latest - 10) * 0.1)
    except Exception:
        return 5.0


def _score_cash_burn_runway(company, as_of_date, flags):
    """Cash burn rate and runway in months."""
    try:
        cash_vals  = list(_get_recent_kpis(company, 'cash-balance', 2))
        burn_vals  = list(_get_recent_kpis(company, 'monthly-burn', 3))

        if not cash_vals or not burn_vals:
            # Try runway directly
            runway_vals = list(_get_recent_kpis(company, 'runway-months', 1))
            if runway_vals:
                runway = float(runway_vals[0])
                return _runway_score(runway, company, flags)
            return 5.0

        cash = float(cash_vals[0])
        avg_burn = sum(float(b) for b in burn_vals) / len(burn_vals)

        if avg_burn <= 0:
            return 0.0  # Cash-flow positive

        runway = cash / avg_burn
        return _runway_score(runway, company, flags)
    except Exception:
        return 5.0


def _runway_score(runway_months, company, flags):
    if runway_months >= 18:
        return 0.0
    elif runway_months >= 12:
        return 2.0
    elif runway_months >= 9:
        return 4.0
    elif runway_months >= 6:
        flags.append(f'{company.name}: Cash runway {runway_months:.0f} months — LOW')
        return 6.0
    elif runway_months >= 3:
        flags.append(f'{company.name}: Cash runway {runway_months:.0f} months — CRITICAL')
        return 8.5
    else:
        flags.append(f'{company.name}: Cash runway < 3 months — EMERGENCY')
        return 10.0


def _score_working_capital(company, as_of_date, flags):
    """Working capital ratio = current assets / current liabilities."""
    try:
        wc_vals = list(_get_recent_kpis(company, 'working-capital-ratio', 1))
        if wc_vals:
            ratio = float(wc_vals[0])
            if ratio >= 2.0:
                return 0.0
            elif ratio >= 1.5:
                return 2.0
            elif ratio >= 1.0:
                return 4.0
            elif ratio >= 0.75:
                flags.append(f'{company.name}: Working capital ratio {ratio:.2f} — LOW')
                return 7.0
            else:
                flags.append(f'{company.name}: Working capital ratio {ratio:.2f} — CRITICAL')
                return 9.5
        return 5.0
    except Exception:
        return 5.0


def _score_debt_service(company, as_of_date, flags):
    """Debt service coverage ratio (DSCR)."""
    try:
        dscr_vals = list(_get_recent_kpis(company, 'dscr', 1))
        if dscr_vals:
            dscr = float(dscr_vals[0])
            if dscr >= 2.0:
                return 0.0
            elif dscr >= 1.5:
                return 2.0
            elif dscr >= 1.25:
                return 4.0
            elif dscr >= 1.0:
                flags.append(f'{company.name}: DSCR {dscr:.2f} — near threshold')
                return 6.0
            else:
                flags.append(f'{company.name}: DSCR {dscr:.2f} — below 1x, debt distress risk')
                return 9.0
        return 3.0  # No debt data → lower risk (assume no debt)
    except Exception:
        return 5.0


def _score_customer_concentration(company, as_of_date, flags):
    """Customer concentration — % revenue from top customer."""
    try:
        conc_vals = list(_get_recent_kpis(company, 'top-customer-revenue-pct', 1))
        if conc_vals:
            pct = float(conc_vals[0])
            if pct >= 50:
                flags.append(f'{company.name}: Top customer = {pct:.0f}% of revenue — HIGH concentration')
                return 9.0
            elif pct >= 30:
                flags.append(f'{company.name}: Top customer = {pct:.0f}% of revenue')
                return 6.0
            elif pct >= 20:
                return 3.0
            else:
                return 1.0
        return 5.0
    except Exception:
        return 5.0


def _score_mgmt_changes(company, as_of_date, flags):
    """Management team changes in last 12 months (based on BoardMeeting notes)."""
    try:
        from investments.models import BoardMeeting
        # Count board meetings with management change keywords in last 12m
        cutoff = as_of_date - timedelta(days=365)
        meetings = BoardMeeting.objects.filter(
            investment__portfolio_company=company,
            meeting_date__gte=cutoff,
        ).values_list('resolutions', flat=True)

        change_count = 0
        for resolutions in meetings:
            if isinstance(resolutions, list):
                text = ' '.join(str(r) for r in resolutions).lower()
                if any(kw in text for kw in ['ceo', 'cfo', 'cto', 'resignation', 'appointment', 'director change']):
                    change_count += 1

        if change_count == 0:
            return 0.0
        elif change_count == 1:
            return 2.0
        elif change_count == 2:
            flags.append(f'{company.name}: {change_count} management changes in 12 months')
            return 5.0
        else:
            flags.append(f'{company.name}: {change_count}+ management changes — HIGH turnover')
            return 8.0
    except Exception:
        return 3.0


def _score_market_conditions(company, as_of_date, flags):
    """Market conditions — sector sentiment (Gemini-estimated or static default)."""
    # In Phase 1, use a static sector-based score
    # Phase 2 will use real-time Gemini sector sentiment
    sector = (company.sector or '').lower()

    HIGH_RISK_SECTORS   = ['crypto', 'nft', 'edtech', 'gaming']
    MEDIUM_RISK_SECTORS = ['retail', 'travel', 'hospitality', 'real estate']
    LOW_RISK_SECTORS    = ['healthcare', 'pharma', 'saas', 'fintech', 'deeptech']

    for s in HIGH_RISK_SECTORS:
        if s in sector:
            return 7.0

    for s in MEDIUM_RISK_SECTORS:
        if s in sector:
            return 4.0

    for s in LOW_RISK_SECTORS:
        if s in sector:
            return 1.0

    return 3.0  # Default: moderate


def _score_peer_comparisons(company, as_of_date, flags):
    """Peer comparison — valuation vs peers (based on IPEV Level 2 multiples)."""
    try:
        from investments.models import Valuation
        latest_val = Valuation.objects.filter(
            investment__portfolio_company=company,
            ipev_level=2,
        ).order_by('-valuation_date').first()

        if not latest_val or not latest_val.peer_multiples_used:
            return 5.0

        # Check if company's multiple is in a reasonable range
        # (a rough proxy for whether it's overvalued/undervalued vs peers)
        return 3.0  # Neutral if peer data exists
    except Exception:
        return 5.0


def _score_compliance_status(company, as_of_date, flags):
    """Compliance score based on portfolio company compliance obligations."""
    try:
        # Check if there are overdue compliance obligations (Phase 7 Compliance 2.0)
        try:
            from compliance.models import PortfolioCompanyCompliance
            overdue = PortfolioCompanyCompliance.objects.filter(
                portfolio_company=company,
                status='overdue',
            ).count()

            if overdue == 0:
                return 0.0
            elif overdue <= 2:
                flags.append(f'{company.name}: {overdue} overdue compliance obligation(s)')
                return 4.0
            else:
                flags.append(f'{company.name}: {overdue} overdue compliance obligations — HIGH RISK')
                return 8.0
        except Exception:
            return 2.0  # Compliance 2.0 not yet built — default low
    except Exception:
        return 2.0


def _generate_commentary(company, score, tier, flags, signals):
    """Generate AI commentary using Gemini (Vertex AI)."""
    try:
        from api.gemini_service import generate_content

        flags_text = '\n'.join(f'- {f}' for f in flags) if flags else '- No critical flags'
        signal_summary = ', '.join(
            f'{k.replace("_"," ")}: {v:.1f}/10'
            for k, v in signals.items()
        )

        prompt = f"""As a private equity risk analyst, write a 2-3 sentence risk summary for this portfolio company.

Company: {company.name}
Sector: {company.sector or 'Unknown'}
Risk Score: {score:.1f}/100 — {tier.upper()} RISK
Signal Scores: {signal_summary}
Key Flags:
{flags_text}

Write a concise, professional risk commentary. Focus on the most important risk drivers.
Do not repeat the score. Be specific and actionable."""

        result = generate_content(prompt)
        return result.text.strip()
    except Exception as e:
        logger.warning(f'Gemini commentary generation failed: {e}')
        return ''
