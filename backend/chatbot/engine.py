"""
NL Chatbot Engine — v5 AI Analytics.

Pipeline:
  User Query
    → Guardrails (reject off-topic, enforce finance-only scope)
    → Intent Classifier (Gemini)
    → Context Injector (org/fund/company context)
    → SQL Query Builder (Gemini → safe parameterized query)
    → Data Fetcher (Django ORM execution)
    → Response Renderer (Gemini → natural language + optional chart)
    → Fallback Handler (if intent unclear or no data)

Security: All generated SQL is validated against an allowlist of tables.
No DDL (CREATE/DROP/ALTER), no DELETE, no UPDATE — read-only SELECT only.
Rate limiting: 30 queries per minute per user.
"""
import json
import logging
import re
import time
from collections import defaultdict
from typing import Dict, Any, List, Optional, Tuple

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowlist — only these tables can appear in generated SQL
# ---------------------------------------------------------------------------

ALLOWED_TABLES = {
    # Investments
    'investments_portfoliocompany', 'investments_investment',
    'investments_investmenttranche', 'investments_valuation',
    'investments_portfoliokpi', 'investments_kpidefinition',
    'investments_companyfinancials', 'investments_exitevent',
    'investments_boardmeeting',
    # Funds
    'funds_fund', 'funds_scheme', 'funds_fundcategory', 'funds_entity',
    # LP
    'lp_investor', 'lp_commitment', 'lp_capitalcall',
    'lp_capitalcalllineitem', 'lp_distribution', 'lp_distributionlineitem',
    'lp_lpcapitalaccount', 'lp_bankaccount',
    # Accounting
    'accounting_navrecord', 'accounting_carriedinterest',
    'accounting_fundledger', 'accounting_managementfeeschedule',
    'accounting_chartofaccounts',
    # Compliance
    'compliance_sebireport', 'compliance_amlduediligence',
    'compliance_compliancecalendar', 'compliance_equitythresholdalert',
    'compliance_portfoliocompanycompliance', 'compliance_portfoliocompliancescore',
    'compliance_fundcompliancescore', 'compliance_sebicircular',
    'compliance_circularaction', 'compliance_escalationlog',
    'compliance_femacompliance',
    # MIS
    'mis_consolidation_budgetvsactual', 'mis_consolidation_consolidatedmis',
    'mis_consolidation_misanomalyalert',
    # IC Workflow
    'ic_workflow_dealpipeline', 'ic_workflow_icpresentation',
    'ic_workflow_icvote', 'ic_workflow_icdecision',
    # Accounts & Portfolio
    'accounts_organization', 'accounts_auditlog', 'accounts_fundaccess',
    'portfolio_portfoliosnapshot', 'portfolio_portfolionode',
}

BLOCKED_KEYWORDS = {
    'drop', 'delete', 'truncate', 'alter', 'create', 'insert', 'update',
    'grant', 'revoke', 'exec', 'execute', '--', ';--', 'xp_', 'pg_',
}

# ---------------------------------------------------------------------------
# Rate limiter — 30 queries/min per user (in-memory, resets on restart)
# ---------------------------------------------------------------------------

_rate_buckets: Dict[str, list] = defaultdict(list)
RATE_LIMIT = 30
RATE_WINDOW = 60  # seconds


def _check_rate_limit(user_id: str) -> bool:
    now = time.time()
    bucket = _rate_buckets[user_id]
    # Prune old entries
    _rate_buckets[user_id] = [t for t in bucket if now - t < RATE_WINDOW]
    if len(_rate_buckets[user_id]) >= RATE_LIMIT:
        return False
    _rate_buckets[user_id].append(now)
    return True


# ---------------------------------------------------------------------------
# Database Schema (embedded so Gemini knows exact table/column names)
# ---------------------------------------------------------------------------

DB_SCHEMA = """
-- Portfolio Companies
investments_portfoliocompany: id(UUID PK), organization_id(FK→accounts_organization), name(Char), sector(Char), sub_sector(Char), cin(Char), pan(Char), incorporation_date(Date), headquarters_city(Char), is_active(Bool), is_quoted(Bool), listing_exchange(Char), created_at(DateTime)

-- Investments (linked via scheme→fund)
investments_investment: id(UUID PK), scheme_id(FK→funds_scheme), portfolio_company_id(FK→investments_portfoliocompany), company_name(Char), instrument_type(Char), ownership_pct(Decimal), percentage_stake_fully_diluted(Decimal), total_invested(Decimal), investment_date(Date), currency(Char), status(Char choices: active/partially_exited/fully_exited/written_off), sector(Char)

-- Tranches
investments_investmenttranche: id(UUID PK), investment_id(FK), tranche_number(Int), amount(Decimal), date(Date), shares_acquired(Decimal), price_per_share(Decimal), pre_money_valuation(Decimal), post_money_valuation(Decimal), round_name(Char)

-- Valuations
investments_valuation: id(UUID PK), investment_id(FK), valuation_date(Date), methodology(Char), fair_value(Decimal), fair_value_of_holding(Decimal), enterprise_value(Decimal), cost_basis(Decimal), unrealized_gain_loss(Decimal), multiple(Decimal), ipev_level(Int)

-- KPI Definitions & Values
investments_kpidefinition: id(UUID PK), organization_id(FK), name(Char), slug(Slug), format(Char), frequency(Char), is_system_kpi(Bool)
investments_portfoliokpi: id(UUID PK), investment_id(FK), portfolio_company_id(FK), kpi_definition_id(FK), period(Date), value(Decimal), status(Char)

-- Company Financials (burn/runway)
investments_companyfinancials: id(UUID PK), investment_id(FK), portfolio_company_id(FK), period(Date), gross_burn(Decimal), net_burn(Decimal), cash_balance(Decimal), runway_months(Decimal)

-- Exit Events
investments_exitevent: id(UUID PK), investment_id(FK), exit_type(Char), is_actual(Bool), exit_date(Date), exit_valuation(Decimal), proceeds(Decimal), net_exit_proceeds(Decimal), realized_gain_loss(Decimal), moic(Decimal), irr_pct(Decimal), buyer_name(Char)

-- Funds & Schemes
funds_fund: id(UUID PK), organization_id(FK), name(Char), sebi_registration_number(Char), fund_category_id(FK→funds_fundcategory), inception_date(Date), corpus_target(Decimal)
funds_scheme: id(UUID PK), fund_id(FK→funds_fund), name(Char), vintage_year(Int), scheme_size(Decimal), tenure_years(Int), hurdle_rate_pct(Decimal), carry_pct(Decimal), management_fee_pct(Decimal)
funds_fundcategory: id(UUID PK), sebi_category_code(Char), name(Char), sub_category(Char)

-- LP / Investors
lp_investor: id(UUID PK), organization_id(FK), investor_name(Char), investor_type(Char), email(Email), city(Char), country(Char), pan(Char), kyc_status(Char choices: pending/verified/expired/rejected)
lp_commitment: id(UUID PK), investor_id(FK), scheme_id(FK), commitment_amount(Decimal), commitment_date(Date), units_allocated(Decimal), commitment_status(Char)
lp_capitalcall: id(UUID PK), scheme_id(FK), call_number(Int), call_date(Date), payment_due_date(Date), call_percentage(Decimal), total_call_amount(Decimal), call_status(Char)
lp_distribution: id(UUID PK), scheme_id(FK), distribution_number(Int), distribution_date(Date), distribution_type(Char), total_gross_amount(Decimal), total_tds_amount(Decimal), total_net_amount(Decimal), distribution_status(Char)
lp_lpcapitalaccount: id(UUID PK), commitment_id(FK→lp_commitment), as_of_date(Date), committed_capital(Decimal), called_capital(Decimal), uncalled_capital(Decimal), distributed_capital(Decimal), unrealized_value(Decimal), total_value(Decimal), irr(Decimal), tvpi(Decimal), dpi(Decimal), rvpi(Decimal), moic(Decimal)

-- Accounting
accounting_navrecord: id(UUID PK), scheme_id(FK), nav_date(Date), total_nav(Decimal), total_units_outstanding(Decimal), nav_per_unit(Decimal), investments_at_fair_value(Decimal), cash_and_equivalents(Decimal), unrealized_gains(Decimal), realized_gains(Decimal)
accounting_carriedinterest: id(UUID PK), scheme_id(FK), calculation_date(Date), total_distributions(Decimal), total_called_capital(Decimal), carry_amount_gross(Decimal), carry_amount_net(Decimal)
accounting_fundledger: id(UUID PK), scheme_id(FK), entry_date(Date), description(Char), amount(Decimal), reference_type(Char)
accounting_managementfeeschedule: id(UUID PK), scheme_id(FK), period_start(Date), period_end(Date), fee_basis_amount(Decimal), fee_rate(Decimal), fee_amount(Decimal), fee_status(Char)

-- Compliance
compliance_sebireport: id(UUID PK), fund_id(FK), scheme_id(FK), report_type(Char), due_date(Date), filing_status(Char choices: pending/filed/overdue), filed_date(Date)
compliance_compliancecalendar: id(UUID PK), organization_id(FK), fund_id(FK), title(Char), due_date(Date), status(Char choices: pending/completed/overdue), completed_date(Date)
compliance_equitythresholdalert: id(UUID PK), investment_id(FK), threshold_breached(Bool), breach_date(Date), stake_percentage(Decimal), severity(Char), resolved(Bool)
compliance_fundcompliancescore: id(UUID PK), fund_id(FK), score_date(Date), combined_score(Decimal)

-- MIS / Budget vs Actual
mis_consolidation_budgetvsactual: id(UUID PK), portfolio_company_id(FK), fund_id(FK), period_year(Int), period_month(Int), line_item(Char choices: revenue/ebitda/pat/cogs/employee_cost/etc), budget_inr(Decimal), actual_inr(Decimal), variance_inr(Decimal), variance_pct(Decimal), is_favorable(Bool)
mis_consolidation_consolidatedmis: id(UUID PK), organization_id(FK), fund_id(FK), period_year(Int), line_item(Char), total_actual_inr(Decimal), total_budget_inr(Decimal), company_count(Int)

-- IC Workflow / Deal Pipeline
ic_workflow_dealpipeline: id(UUID PK), organization_id(FK), fund_id(FK), company_name(Char), sector(Char), stage(Char choices: sourced/initial_screen/deep_dive/term_sheet/ic_presentation/approved/rejected/closed/passed), proposed_investment_inr(Decimal), sourced_date(Date)

-- Key relationships:
-- investments_investment.scheme_id → funds_scheme.id → funds_scheme.fund_id → funds_fund.id → funds_fund.organization_id → accounts_organization.id
-- investments_investment.portfolio_company_id → investments_portfoliocompany.id
-- To get companies for a fund: JOIN investments_investment ON scheme_id → funds_scheme WHERE fund_id = X
-- All amounts in Cr (Crores INR) unless noted otherwise. BvA amounts in Lakhs INR.
"""


# ---------------------------------------------------------------------------
# Guardrails — reject off-topic queries
# ---------------------------------------------------------------------------

OFF_TOPIC_PATTERNS = [
    r'\b(where\s+is|capital\s+of|president\s+of|prime\s+minister)\b',
    r'\b(recipe|weather|sports?\s+score|movie|song|joke|poem)\b',
    r'\b(who\s+is\s+(?!the\s+(?:fund|portfolio|investment|lp|gp)))',
    r'\b(taj\s+mahal|eiffel|statue\s+of\s+liberty)\b',
    r'\b(write\s+(?:me\s+)?(?:a\s+)?(?:code|program|script|essay|story))\b',
    r'\b(translate|define\s+the\s+word|spell)\b',
]

GUARDRAIL_RESPONSE = (
    "I'm TrackFundAI's portfolio intelligence assistant. I can only help with questions about "
    "your uploaded fund data, portfolio companies, investments, NAV, compliance, LP information, "
    "financial metrics, and market research related to your portfolio.\n\n"
    "Try asking:\n"
    "• \"How many portfolio companies do we have?\"\n"
    "• \"What's the total NAV across all schemes?\"\n"
    "• \"Show me overdue compliance filings\"\n"
    "• \"Which companies have the highest MOIC?\""
)


def _is_off_topic(query: str) -> bool:
    q = query.lower().strip()
    for pat in OFF_TOPIC_PATTERNS:
        if re.search(pat, q, re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# Intent Classifier
# ---------------------------------------------------------------------------

INTENT_SCHEMA = {
    'portfolio_summary': 'User wants overview of portfolio companies, counts, sectors, total invested, valuations',
    'fund_performance': 'User wants IRR, MOIC, TVPI, DPI, NAV, returns, unit value data for a fund or scheme',
    'company_financials': 'User wants P&L, revenue, EBITDA, PAT, cash, burn, runway for a specific company',
    'compliance_status': 'User wants compliance status, overdue filings, SEBI reports, equity threshold alerts',
    'lp_information': 'User wants LP/investor commitments, distributions, capital calls, capital accounts',
    'risk_analysis': 'User wants risk scores, anomaly alerts, budget variance alerts, watch list companies',
    'kpi_analysis': 'User wants KPI trends, sector benchmarks, specific operational metrics',
    'exit_analysis': 'User wants exit scenarios, exit events, MOIC analysis, exit recommendations',
    'deal_pipeline': 'User wants IC pipeline status, deal stages, sourcing data',
    'valuation_analysis': 'User wants portfolio valuations, fair value, unrealized gains, multiples, methodology',
    'accounting_query': 'User wants NAV records, management fees, carried interest, fund ledger, chart of accounts',
    'market_research': 'User wants sector comparisons, industry benchmarks, market analysis related to portfolio',
    'general_query': 'General question about the platform data, fund structure, or finance concepts',
    'out_of_scope': 'Question completely unrelated to finance, portfolio management, or fund data',
}


def classify_intent(query: str, organization_name: str) -> Dict[str, str]:
    """Use Gemini to classify user intent."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel(getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash'))

        intents_desc = '\n'.join(f'- {k}: {v}' for k, v in INTENT_SCHEMA.items())
        prompt = f"""You are an intent classifier for TrackFundAI, a portfolio management platform for Indian AIFs (Alternative Investment Funds) operated by {organization_name}.

Available intents:
{intents_desc}

IMPORTANT RULES:
1. If the query is about general finance concepts (MOIC, IRR, NAV definitions, market trends, valuation methods) — classify as "general_query", NOT "out_of_scope".
2. If the query asks about data that could be in the portfolio DB (companies, funds, investors, compliance, etc.) — classify with the most specific intent.
3. Only classify as "out_of_scope" if the query has ZERO relation to finance, investing, or portfolio management.
4. Extract company/fund names mentioned in the query as the "entity" field.

User query: "{query}"

Respond with JSON only (no markdown fences):
{{"intent": "<intent_key>", "entity": "<company/fund name if mentioned or null>", "time_filter": "<e.g. last 3 months, FY2025 or null>", "confidence": 0.0-1.0}}"""

        response = model.generate_content(prompt)
        text = response.text.strip()
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        return json.loads(text)
    except Exception as e:
        logger.warning(f'Intent classification error: {e}')
        return {'intent': 'general_query', 'entity': None, 'time_filter': None, 'confidence': 0.3}


# ---------------------------------------------------------------------------
# Context Injector
# ---------------------------------------------------------------------------

def _uuid_for_sql(val):
    """Return UUID string in the format used by the DB (no hyphens for SQLite)."""
    s = str(val)
    if hasattr(settings, 'DATABASES'):
        engine = settings.DATABASES.get('default', {}).get('ENGINE', '')
        if 'sqlite' in engine:
            return s.replace('-', '')
    return s


def build_context(organization, fund=None, company=None, intent_result=None) -> Dict[str, Any]:
    ctx = {
        'organization_id': _uuid_for_sql(organization.pk),
        'organization_name': organization.name,
        'fund_id': _uuid_for_sql(fund.pk) if fund else None,
        'fund_name': fund.name if fund else None,
        'company_id': _uuid_for_sql(company.pk) if company else None,
        'company_name': company.name if company else None,
    }

    if intent_result and intent_result.get('entity') and not company:
        entity_name = intent_result['entity']
        try:
            from investments.models import PortfolioCompany
            match = PortfolioCompany.objects.filter(
                organization=organization,
                name__icontains=entity_name,
            ).first()
            if match:
                ctx['company_id'] = _uuid_for_sql(match.pk)
                ctx['company_name'] = match.name
        except Exception:
            pass

    return ctx


# ---------------------------------------------------------------------------
# SQL Query Builder (with full schema)
# ---------------------------------------------------------------------------

def build_sql_query(intent: str, context: Dict, time_filter: Optional[str], entity: Optional[str]) -> Optional[str]:
    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel(getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash'))

        db_engine = settings.DATABASES.get('default', {}).get('ENGINE', '')
        db_type = 'SQLite' if 'sqlite' in db_engine else 'PostgreSQL'

        prompt = f"""You are a SQL query builder for a {db_type} database powering a fund management platform.
Generate a read-only SELECT query to answer the user's intent.

DATABASE SCHEMA:
{DB_SCHEMA}

QUERY CONTEXT:
Intent: {intent}
Organization ID: {context['organization_id']}
Fund ID: {context.get('fund_id') or 'not specified — query across all funds'}
Company ID: {context.get('company_id') or 'not specified'}
Company Name: {context.get('company_name') or 'not specified'}
Time filter: {time_filter or 'most recent data available'}
Entity mentioned: {entity or 'none'}

IMPORTANT: UUIDs in this database are stored WITHOUT hyphens (e.g., '{context['organization_id']}').

MANDATORY RULES:
1. ONLY use SELECT statements — never INSERT, UPDATE, DELETE, DROP
2. ALWAYS filter by organization: use organization_id directly or via JOINs through fund→scheme→investment
3. Maximum LIMIT 50
4. Use proper JOINs to traverse: investment → scheme → fund → organization
5. Return ONLY the raw SQL query text, no markdown, no explanation
6. If you truly cannot build a safe query, return exactly: CANNOT_ANSWER
7. Use column aliases for readability (e.g., pc.name AS company_name)
8. For aggregations, include both the aggregate and GROUP BY columns
9. Prefer LEFT JOIN over INNER JOIN to avoid losing data
10. All monetary amounts are in Cr (Crores) unless from mis_consolidation tables (those are in Lakhs)
11. Do NOT use PostgreSQL-specific syntax like DISTINCT ON, FILTER, LATERAL. Use standard SQL or {db_type}-compatible syntax only.
12. For "latest record per group" use ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...) in a subquery, not DISTINCT ON.
13. Keep queries simple — avoid unnecessary complexity. A simple JOIN + WHERE + GROUP BY is preferred over CTEs when possible.

SQL:"""

        response = model.generate_content(prompt)
        sql = response.text.strip()
        # Strip markdown fences
        sql = re.sub(r'^```(?:sql)?\s*', '', sql)
        sql = re.sub(r'\s*```$', '', sql)
        sql = sql.strip()

        if sql == 'CANNOT_ANSWER':
            return None
        if _is_sql_safe(sql):
            return sql
        logger.warning(f'Unsafe SQL rejected: {sql[:200]}')
        return None
    except Exception as e:
        logger.warning(f'SQL builder error: {e}')
        return None


def _is_sql_safe(sql: str) -> bool:
    sql_lower = sql.lower().strip()
    for kw in BLOCKED_KEYWORDS:
        if re.search(r'\b' + re.escape(kw) + r'\b', sql_lower):
            return False
    # Allow SELECT or WITH ... SELECT (CTEs)
    if not (sql_lower.startswith('select') or sql_lower.startswith('with')):
        return False
    # If CTE, ensure it contains a SELECT after the AS blocks
    if sql_lower.startswith('with') and 'select' not in sql_lower:
        return False
    table_pattern = re.compile(r'(?:from|join)\s+([a-z_][a-z0-9_]*)', re.IGNORECASE)
    referenced = set(m.group(1).lower() for m in table_pattern.finditer(sql))
    # Filter out CTE aliases (names defined in WITH ... AS blocks)
    cte_names = set(m.group(1).lower() for m in re.finditer(r'\b(\w+)\s+AS\s*\(', sql, re.IGNORECASE))
    actual_tables = referenced - cte_names
    for table in actual_tables:
        if table not in ALLOWED_TABLES:
            return False
    return True


# ---------------------------------------------------------------------------
# Data Fetcher
# ---------------------------------------------------------------------------

def execute_query(sql: str, max_rows: int = 50) -> Tuple[List[str], List[tuple]]:
    if not sql or not _is_sql_safe(sql):
        return [], []
    if 'limit' not in sql.lower():
        sql = sql.rstrip(';') + f' LIMIT {max_rows}'
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            columns = [col[0] for col in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(max_rows)
        return columns, rows
    except Exception as e:
        logger.warning(f'SQL execution error: {e} | SQL: {sql[:200]}')
        return [], []


# ---------------------------------------------------------------------------
# Chart Detector — decide if response should include a chart
# ---------------------------------------------------------------------------

CHART_INTENTS = {
    'portfolio_summary', 'fund_performance', 'kpi_analysis',
    'valuation_analysis', 'exit_analysis', 'lp_information',
    'accounting_query', 'company_financials', 'risk_analysis',
}


def _suggest_chart(intent: str, columns: List[str], rows: List[tuple], query: str) -> Optional[Dict]:
    """Decide if a chart would help and return chart config if so."""
    if intent not in CHART_INTENTS or len(rows) < 2:
        return None

    # Find label and numeric columns
    label_col = None
    num_cols = []
    for i, col in enumerate(columns):
        sample_vals = [r[i] for r in rows[:5] if r[i] is not None]
        if sample_vals and all(isinstance(v, (int, float)) or _is_numeric(v) for v in sample_vals):
            num_cols.append(i)
        elif not label_col and sample_vals:
            label_col = i

    if label_col is None or not num_cols:
        return None

    labels = [str(r[label_col] or '')[:30] for r in rows[:20]]

    # Build datasets
    datasets = []
    colors = ['#00d4ff', '#7c3aed', '#10b981', '#f59e0b', '#ef4444', '#3b82f6']
    for idx, ci in enumerate(num_cols[:3]):
        col_name = columns[ci]
        values = [float(r[ci]) if r[ci] is not None and _is_numeric(r[ci]) else 0 for r in rows[:20]]
        datasets.append({
            'label': col_name.replace('_', ' ').title(),
            'data': values,
            'color': colors[idx % len(colors)],
        })

    # Decide chart type
    chart_type = 'bar'
    q_lower = query.lower()
    if any(kw in q_lower for kw in ['trend', 'over time', 'history', 'monthly', 'quarterly']):
        chart_type = 'line'
    elif any(kw in q_lower for kw in ['distribution', 'breakdown', 'split', 'mix', 'composition']):
        chart_type = 'doughnut' if len(rows) <= 8 else 'bar'
    elif any(kw in q_lower for kw in ['compare', 'comparison', 'vs', 'versus']):
        chart_type = 'bar'
    elif len(rows) <= 6 and len(num_cols) == 1:
        chart_type = 'doughnut'

    return {
        'type': chart_type,
        'labels': labels,
        'datasets': datasets,
        'title': columns[label_col].replace('_', ' ').title() + ' Analysis',
    }


def _is_numeric(val) -> bool:
    if isinstance(val, (int, float)):
        return True
    try:
        float(str(val))
        return True
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Response Renderer
# ---------------------------------------------------------------------------

def render_response(query: str, intent: str, columns: List[str], rows: List[tuple], context: Dict) -> str:
    if not rows:
        return _fallback_response(intent, context)

    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel(getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash'))

        data_str = ' | '.join(columns) + '\n'
        data_str += '\n'.join(' | '.join(str(v) for v in row) for row in rows[:25])

        prompt = f"""You are a senior financial analyst AI assistant for TrackFundAI, a portfolio management platform for Indian AIFs (Alternative Investment Funds).

User asked: "{query}"
Organization: {context.get('organization_name', 'Portfolio')}
Fund context: {context.get('fund_name') or 'All funds'}

Data retrieved ({len(rows)} rows):
{data_str}

Provide a clear, professional response that:
1. Directly answers the user's question using the data above
2. Highlights key numbers — use bold (**value**) for important figures
3. Uses ₹ symbol for INR amounts, format large numbers with Cr/L suffixes
4. Notes any important trends, anomalies, or concerns visible in the data
5. If the data shows multiple records, summarize with a brief analysis (top performers, outliers, averages)
6. Use markdown formatting: **bold** for emphasis, bullet points for lists
7. Keep it concise (3-6 sentences for simple queries, more for complex analysis)
8. If comparing data, provide percentage differences
9. End with a brief insight or recommendation if relevant

Do NOT include raw tables in the response — integrate numbers into prose. Do NOT say "based on the data" or "according to the query"."""

        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.warning(f'Response render error: {e}')
        return _table_response(columns, rows)


def _fallback_response(intent: str, context: Dict) -> str:
    fallbacks = {
        'portfolio_summary': 'No portfolio company data found for your organization. Please ensure fund data has been imported.',
        'fund_performance': 'No fund performance data available. Please check that NAV records and capital accounts have been imported.',
        'company_financials': f"No financial data found for {context.get('company_name', 'the specified company')}. This company may not have MIS/P&L data uploaded yet.",
        'compliance_status': 'No compliance records found. Compliance filings may not have been set up yet.',
        'lp_information': 'No LP/investor data found. Please ensure investor data has been imported from the fund Excel.',
        'risk_analysis': 'No risk or anomaly data found. Risk scores are computed after MIS data is imported.',
        'kpi_analysis': 'No KPI data found. KPIs are populated from the Portfolio KPIs sheet in your fund Excel.',
        'exit_analysis': 'No exit event data found for the current portfolio.',
        'valuation_analysis': 'No valuation records found. Valuations are imported from the Valuations (IPEV) sheet.',
        'deal_pipeline': 'No deals found in the IC pipeline.',
        'accounting_query': 'No accounting records found. Please ensure NAV and accounting data has been imported.',
        'market_research': "I can help with market research related to your portfolio sectors. Please specify the sector or company you'd like to analyze.",
        'general_query': "I couldn't find specific data for that query. Try asking about portfolio companies, fund performance, NAV, compliance status, or LP information.",
        'out_of_scope': GUARDRAIL_RESPONSE,
    }
    return fallbacks.get(intent, "I couldn't find data to answer that question. Please try rephrasing with specific company, fund, or metric names.")


def _table_response(columns: List[str], rows: List[tuple]) -> str:
    if not rows:
        return 'No data found.'
    header = ' | '.join(columns)
    divider = ' | '.join('---' for _ in columns)
    data = '\n'.join(' | '.join(str(v) for v in row) for row in rows[:15])
    return f'Here is the data:\n\n{header}\n{divider}\n{data}'


# ---------------------------------------------------------------------------
# General Finance Knowledge Handler
# ---------------------------------------------------------------------------

def _handle_general_finance(query: str, context: Dict) -> str:
    """Answer general finance/market questions using Gemini's knowledge."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel(getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash'))

        prompt = f"""You are a senior financial analyst AI assistant for TrackFundAI, a portfolio management platform for Indian AIFs.
The user ({context.get('organization_name', 'a fund manager')}) is asking a general finance question.

User query: "{query}"

Answer this question with financial expertise. You may:
1. Explain financial concepts (IRR, MOIC, TVPI, DPI, NAV, SEBI regulations, etc.)
2. Discuss market trends, sector analysis, and investment strategies relevant to Indian markets
3. Provide analysis frameworks and methodologies (DCF, IPEV, comparable company analysis)
4. Discuss regulatory aspects (SEBI AIF regulations, FEMA, PMLA, TDS for AIFs)
5. Compare/contrast financial metrics and what they mean for fund performance

Keep the response professional, concise (3-8 sentences), and relevant to fund management.
Use markdown formatting: **bold** for key terms, bullet points for lists.
If the question is completely unrelated to finance, politely redirect."""

        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.warning(f'General finance handler error: {e}')
        return "I'm having trouble connecting to the AI service. Please try again in a moment."


# ---------------------------------------------------------------------------
# Main Chatbot Handler
# ---------------------------------------------------------------------------

class ChatbotHandler:
    def __init__(self, organization, user=None):
        self.organization = organization
        self.user = user

    def handle(self, query: str, fund=None, company=None) -> Dict[str, Any]:
        # Rate limit check
        user_key = str(self.user.pk) if self.user else str(self.organization.pk)
        if not _check_rate_limit(user_key):
            return {
                'response': 'You are sending too many requests. Please wait a moment before trying again.',
                'intent': 'rate_limited',
                'entity': None,
                'confidence': 1.0,
                'message_id': None,
                'data': {'columns': [], 'rows': []},
                'chart': None,
            }

        # Guardrail check
        if _is_off_topic(query):
            message_id = self._log_query(query, 'out_of_scope', GUARDRAIL_RESPONSE)
            return {
                'response': GUARDRAIL_RESPONSE,
                'intent': 'out_of_scope',
                'entity': None,
                'confidence': 1.0,
                'message_id': str(message_id) if message_id else None,
                'data': {'columns': [], 'rows': []},
                'chart': None,
            }

        # Step 1: Classify intent
        intent_result = classify_intent(query, self.organization.name)
        intent = intent_result.get('intent', 'general_query')
        entity = intent_result.get('entity')
        time_filter = intent_result.get('time_filter')

        # Step 2: Inject context
        ctx = build_context(self.organization, fund=fund, company=company, intent_result=intent_result)

        # Handle out_of_scope from Gemini classification
        if intent == 'out_of_scope':
            response_text = GUARDRAIL_RESPONSE
            message_id = self._log_query(query, intent, response_text)
            return {
                'response': response_text,
                'intent': intent,
                'entity': entity,
                'confidence': intent_result.get('confidence', 0),
                'message_id': str(message_id) if message_id else None,
                'data': {'columns': [], 'rows': []},
                'chart': None,
            }

        # Handle general finance knowledge queries (no SQL needed)
        if intent in ('general_query', 'market_research'):
            response_text = _handle_general_finance(query, ctx)
            message_id = self._log_query(query, intent, response_text)
            return {
                'response': response_text,
                'intent': intent,
                'entity': entity,
                'confidence': intent_result.get('confidence', 0),
                'message_id': str(message_id) if message_id else None,
                'data': {'columns': [], 'rows': []},
                'chart': None,
            }

        # Step 3: Build SQL
        sql = build_sql_query(intent, ctx, time_filter, entity)

        # Step 4: Execute
        columns = []
        rows = []
        if sql:
            columns, rows = execute_query(sql)

        # Step 5: Render response
        response_text = render_response(query, intent, columns, rows, ctx)

        # Step 6: Generate chart if appropriate
        chart = _suggest_chart(intent, columns, rows, query)

        # Log to DB
        message_id = self._log_query(query, intent, response_text)

        return {
            'response': response_text,
            'intent': intent,
            'entity': entity,
            'confidence': intent_result.get('confidence', 0),
            'message_id': str(message_id) if message_id else None,
            'data': {
                'columns': columns,
                'rows': [list(r) for r in rows[:20]],
            },
            'chart': chart,
            'sql_used': sql if settings.DEBUG else None,
        }

    def _log_query(self, query: str, intent: str, response: str):
        try:
            msg = ChatMessage.objects.create(
                organization=self.organization,
                user=self.user,
                query=query,
                intent=intent,
                response=response,
            )
            return msg.pk
        except Exception:
            return None


try:
    from .models import ChatMessage
except ImportError:
    pass
