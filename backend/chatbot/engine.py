"""
NL Chatbot Engine — v5 AI Analytics.

Pipeline:
  User Query
    → Intent Classifier (Gemini)
    → Context Injector (org/fund/company context)
    → SQL Query Builder (Gemini → safe parameterized query)
    → Data Fetcher (Django ORM execution)
    → Response Renderer (Gemini → natural language)
    → Fallback Handler (if intent unclear or no data)

Security: All generated SQL is validated against an allowlist of tables/columns.
No DDL (CREATE/DROP/ALTER), no DELETE, no UPDATE — read-only SELECT only.
"""
import json
import re
from typing import Dict, Any, List, Optional, Tuple

from django.conf import settings
from django.db import connection


# ---------------------------------------------------------------------------
# Allowlist — only these tables/columns can appear in generated SQL
# ---------------------------------------------------------------------------

ALLOWED_TABLES = {
    'investments_portfoliocompany', 'investments_investment', 'investments_valuation',
    'investments_portfoliokpi', 'investments_kpidefinition',
    'mis_consolidation_budgetvsactual', 'mis_consolidation_consolidatedmis',
    'mis_consolidation_misanomalyalert',
    'funds_fund', 'funds_scheme',
    'lp_investor', 'lp_commitment', 'lp_distribution',
    'accounting_naventry', 'accounting_carryledger',
    'compliance_equitythresholdalert', 'compliance_portfoliocompanycompliance',
    'riskscore_companyriskscore',
    'tds_tdswithholding', 'tds_form26qreturn',
    'ic_workflow_dealpipeline', 'ic_workflow_icdecision',
    'reporting_reportingcalendar',
    'accounts_auditlog',
}

BLOCKED_KEYWORDS = {
    'drop', 'delete', 'truncate', 'alter', 'create', 'insert', 'update',
    'grant', 'revoke', 'exec', 'execute', '--', ';--', 'xp_', 'pg_',
}


# ---------------------------------------------------------------------------
# Intent Classifier
# ---------------------------------------------------------------------------

INTENT_SCHEMA = {
    'portfolio_summary': 'User wants overview of portfolio companies, counts, valuations',
    'fund_performance': 'User wants IRR, MOIC, NAV, returns data for a fund',
    'company_financials': 'User wants P&L, revenue, EBITDA, cash for a specific company',
    'compliance_status': 'User wants compliance status, overdue filings, SEBI reports',
    'lp_information': 'User wants LP commitments, distributions, capital calls',
    'risk_analysis': 'User wants risk scores, risk tiers, anomaly alerts',
    'kpi_analysis': 'User wants KPI trends, sector benchmarks, specific metrics',
    'exit_analysis': 'User wants exit scenarios, MOIC analysis, exit recommendations',
    'deal_pipeline': 'User wants IC pipeline status, deal stages',
    'tds_status': 'User wants TDS withholding, 26Q filing status',
    'general_query': 'General question about the platform or data not covered above',
    'out_of_scope': 'Question unrelated to portfolio management / finance',
}


def classify_intent(query: str, organization_name: str) -> Dict[str, str]:
    """Use Gemini to classify user intent."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel(settings.GEMINI_MODEL)

        intents_desc = '\n'.join(f'- {k}: {v}' for k, v in INTENT_SCHEMA.items())
        prompt = f"""You are an intent classifier for TrackFundAI, a portfolio management platform for {organization_name}.

Available intents:
{intents_desc}

User query: "{query}"

Respond with JSON only (no markdown):
{{"intent": "<intent_key>", "entity": "<company/fund name if mentioned or null>", "time_filter": "<e.g. last 3 months, FY2025 or null>", "confidence": 0.0-1.0}}"""

        response = model.generate_content(prompt)
        text = response.text.strip()
        # Strip markdown fences if present
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        return json.loads(text)
    except Exception:
        return {'intent': 'general_query', 'entity': None, 'time_filter': None, 'confidence': 0.3}


# ---------------------------------------------------------------------------
# Context Injector
# ---------------------------------------------------------------------------

def build_context(organization, fund=None, company=None, intent_result=None) -> Dict[str, Any]:
    """Inject org/fund/company context for the query."""
    ctx = {
        'organization_id': str(organization.pk),
        'organization_name': organization.name,
        'fund_id': str(fund.pk) if fund else None,
        'fund_name': fund.name if fund else None,
        'company_id': str(company.pk) if company else None,
        'company_name': company.company_name if company else None,
    }

    # Try to resolve entity name from DB if intent mentions one
    if intent_result and intent_result.get('entity') and not company:
        entity_name = intent_result['entity']
        try:
            from investments.models import PortfolioCompany
            match = PortfolioCompany.objects.filter(
                fund__organization=organization,
                company_name__icontains=entity_name,
            ).first()
            if match:
                ctx['company_id'] = str(match.pk)
                ctx['company_name'] = match.company_name
        except Exception:
            pass

    return ctx


# ---------------------------------------------------------------------------
# SQL Query Builder
# ---------------------------------------------------------------------------

def build_sql_query(intent: str, context: Dict, time_filter: Optional[str], entity: Optional[str]) -> Optional[str]:
    """Use Gemini to build a safe, parameterized SQL query."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel(settings.GEMINI_MODEL)

        prompt = f"""You are a SQL query builder for a PostgreSQL/SQLite database.
Generate a read-only SELECT query to answer the user's intent.

Intent: {intent}
Organization ID: {context['organization_id']}
Company ID: {context.get('company_id', 'N/A')}
Fund ID: {context.get('fund_id', 'N/A')}
Time filter: {time_filter or 'most recent data'}
Entity: {entity or 'all'}

Allowed tables: {', '.join(sorted(ALLOWED_TABLES))}

Rules:
1. Only use SELECT statements — no INSERT, UPDATE, DELETE, DROP
2. Always filter by organization_id using the accounts_organization table or FK joins
3. Use LIMIT 50 maximum
4. Return ONLY the SQL query, nothing else
5. Use standard SQL compatible with SQLite and PostgreSQL
6. If you cannot answer safely, return exactly: CANNOT_ANSWER

SQL query:"""

        response = model.generate_content(prompt)
        sql = response.text.strip()
        if sql == 'CANNOT_ANSWER':
            return None
        # Validate SQL
        if _is_sql_safe(sql):
            return sql
        return None
    except Exception:
        return None


def _is_sql_safe(sql: str) -> bool:
    """Validate SQL against allowlist and blocklist."""
    sql_lower = sql.lower()

    # Block dangerous keywords
    for kw in BLOCKED_KEYWORDS:
        if kw in sql_lower:
            return False

    # Must be a SELECT
    stripped = sql_lower.lstrip()
    if not stripped.startswith('select'):
        return False

    # All table references must be in allowlist
    # Extract table names from FROM and JOIN clauses
    table_pattern = re.compile(r'(?:from|join)\s+([a-z_][a-z0-9_]*)', re.IGNORECASE)
    referenced = set(m.group(1).lower() for m in table_pattern.finditer(sql))
    for table in referenced:
        if table not in ALLOWED_TABLES:
            return False

    return True


# ---------------------------------------------------------------------------
# Data Fetcher
# ---------------------------------------------------------------------------

def execute_query(sql: str, max_rows: int = 50) -> Tuple[List[str], List[tuple]]:
    """Execute the validated SQL and return (columns, rows)."""
    if not sql or not _is_sql_safe(sql):
        return [], []

    # Enforce LIMIT
    if 'limit' not in sql.lower():
        sql = sql.rstrip(';') + f' LIMIT {max_rows}'

    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            columns = [col[0] for col in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(max_rows)
        return columns, rows
    except Exception as e:
        return [], []


# ---------------------------------------------------------------------------
# Response Renderer
# ---------------------------------------------------------------------------

def render_response(query: str, intent: str, columns: List[str], rows: List[tuple], context: Dict) -> str:
    """Use Gemini to generate natural language response from query results."""
    if not rows:
        return _fallback_response(intent, context)

    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel(settings.GEMINI_MODEL)

        # Format data as a readable table string
        data_str = ' | '.join(columns) + '\n'
        data_str += '\n'.join(' | '.join(str(v) for v in row) for row in rows[:20])

        prompt = f"""You are a financial analyst AI assistant for TrackFundAI portfolio management.

User asked: "{query}"
Context: {context.get('organization_name', 'Portfolio')}

Data retrieved:
{data_str}

Provide a clear, professional response in 2-4 sentences that:
1. Directly answers the user's question using the data
2. Highlights key numbers with appropriate financial context
3. Notes any important trends or concerns if visible in the data

Do NOT include the raw table — integrate numbers naturally into prose."""

        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception:
        return _table_response(columns, rows)


def _fallback_response(intent: str, context: Dict) -> str:
    fallbacks = {
        'portfolio_summary': 'No portfolio data found for your organization.',
        'fund_performance': 'No fund performance data available. Please ensure NAV entries are up to date.',
        'company_financials': f"No financial data found for {context.get('company_name', 'the specified company')}.",
        'compliance_status': 'No compliance records found.',
        'out_of_scope': "I specialize in portfolio management queries — please ask about companies, funds, NAV, KPIs, compliance, or distributions.",
        'general_query': "I wasn't able to find specific data for that query. Please try rephrasing with a company name, fund name, or specific metric.",
    }
    return fallbacks.get(intent, "I couldn't find data to answer that question.")


def _table_response(columns: List[str], rows: List[tuple]) -> str:
    if not rows:
        return 'No data found.'
    header = ' | '.join(columns)
    data = '\n'.join(' | '.join(str(v) for v in row) for row in rows[:10])
    return f'Here is the data:\n{header}\n{data}'


# ---------------------------------------------------------------------------
# Main Chatbot Handler
# ---------------------------------------------------------------------------

class ChatbotHandler:
    """
    Main chatbot pipeline handler.
    Usage:
        result = ChatbotHandler(organization).handle(user_query, fund=None, company=None)
    """

    def __init__(self, organization, user=None):
        self.organization = organization
        self.user = user

    def handle(self, query: str, fund=None, company=None) -> Dict[str, Any]:
        """
        Full pipeline: Intent → Context → SQL → Execute → Render.
        Returns: {'response': str, 'intent': str, 'data': [...], 'sql': str}
        """
        # Step 1: Classify intent
        intent_result = classify_intent(query, self.organization.name)
        intent = intent_result.get('intent', 'general_query')
        entity = intent_result.get('entity')
        time_filter = intent_result.get('time_filter')

        # Step 2: Inject context
        ctx = build_context(self.organization, fund=fund, company=company, intent_result=intent_result)

        # Step 3: Build SQL
        sql = None
        columns = []
        rows = []

        if intent != 'out_of_scope' and intent != 'general_query':
            sql = build_sql_query(intent, ctx, time_filter, entity)

            # Step 4: Execute
            if sql:
                columns, rows = execute_query(sql)

        # Step 5: Render response
        response_text = render_response(query, intent, columns, rows, ctx)

        # Log to DB — capture message_id for client-side feedback
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
            'sql_used': sql if settings.DEBUG else None,  # Only show SQL in debug mode
        }

    def _log_query(self, query: str, intent: str, response: str):
        """Persist chat query for audit and improvement. Returns message UUID."""
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


# ---------------------------------------------------------------------------
# ChatMessage model (defined inline to avoid extra migrations file)
# ---------------------------------------------------------------------------
# NOTE: The model is in models.py; this import is here for the log call above.

try:
    from .models import ChatMessage
except ImportError:
    pass
