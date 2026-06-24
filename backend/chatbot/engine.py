"""
NL Chatbot Engine — v5 AI Analytics (v2 — fund-aware, data-complete).

Pipeline:
  User Query
    → Guardrails (reject off-topic, enforce finance-only scope)
    → Dashboard Context Check (handle "which fund is selected" instantly)
    → Intent Classifier (Gemini — 15 intents incl. fund_info)
    → Context Injector (org/fund/company + fund entity resolution)
    → SQL Query Builder (Gemini → safe parameterized query)
    → Data Fetcher (Django ORM execution)
    → Retry with broader query if first SQL returns no rows
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
    # Data Import (read-only — for file/import history queries)
    'dataimport_importjob', 'dataimport_importfile',
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
    _rate_buckets[user_id] = [t for t in bucket if now - t < RATE_WINDOW]
    if len(_rate_buckets[user_id]) >= RATE_LIMIT:
        return False
    _rate_buckets[user_id].append(now)
    return True


# ---------------------------------------------------------------------------
# Database Schema (embedded so Gemini knows exact table/column names)
# ---------------------------------------------------------------------------

# Static cross-table query hints that auto-introspection genuinely cannot
# derive — they describe application-level conventions (unit denomination,
# canonical JOIN paths) that live in business knowledge, not in the ORM.
# Kept tiny on purpose. Per-field business annotations are deliberately
# OMITTED (we will add them surgically only if Gemini is observed to
# misinterpret a specific field — never speculatively).
_QUERY_HINTS = """\
-- KEY CONVENTIONS (application-level, not derivable from ORM introspection):
-- • All monetary amounts are in INR Crore (₹ Cr) UNLESS the column lives in a
--   mis_consolidation_* table — those values are in Lakhs.
-- • UUIDs are stored WITHOUT hyphens in the DB.
-- • Canonical JOIN path for fund-scoped data:
--      investments_investment.scheme_id → funds_scheme.id → funds_scheme.fund_id → funds_fund.id → funds_fund.organization_id
-- • Common queries:
--      Companies in a fund: JOIN investments_investment i ON i.scheme_id = s.id JOIN funds_scheme s ON s.fund_id = f.id WHERE f.id = X
--      Latest NAV per scheme: SELECT * FROM accounting_navrecord WHERE scheme_id = X ORDER BY nav_date DESC LIMIT 1
--      Total commitment per fund: SUM(lp_commitment.commitment_amount) WHERE scheme_id IN (SELECT id FROM funds_scheme WHERE fund_id=X)
"""


# Auto-generated schema cache — built once per process lifetime.
_DB_SCHEMA_CACHE = None
# Apps deliberately excluded from the schema (Django internals, auth scaffolding,
# transient stores). Add to this set if a new internal app is added.
_DB_SCHEMA_EXCLUDED_APPS = {
    # Django internals / scaffolding
    'admin', 'auth', 'contenttypes', 'sessions', 'messages',
    'sites', 'staticfiles', 'humanize',
    # Third-party transient/infra tables — not business data the chatbot
    # should answer questions about
    'django_celery_beat', 'django_celery_results',
}


def _format_field(field) -> str:
    """Render one model field as 'colname(Type[, extras])' for the schema string.

    Pulls everything from Django's field metadata — no hand-written types,
    no hand-written FK target tables, no hand-written choice lists. If the
    field is added/removed/renamed in a model, this output updates on the
    next process restart with zero human effort.
    """
    from django.db import models
    col = field.column
    type_name = field.get_internal_type()  # e.g. CharField, DecimalField, ForeignKey
    extras = []

    # ForeignKey → 'FK→<target_table>'
    if isinstance(field, models.ForeignKey):
        try:
            target_table = field.related_model._meta.db_table
            extras.append(f'FK→{target_table}')
        except Exception:
            pass

    # choices=(('a','A'), …) → 'choices: a/b/c'
    if getattr(field, 'choices', None):
        choice_keys = '/'.join(str(c[0]) for c in field.choices)
        extras.append(f'choices: {choice_keys}')

    # NULL-able vs required
    if getattr(field, 'primary_key', False):
        extras.append('PK')
    elif getattr(field, 'null', False):
        extras.append('nullable')

    extras_str = (' ' + ', '.join(extras)) if extras else ''
    return f'{col}({type_name}{extras_str})'


def _build_db_schema_from_django_models() -> str:
    """Introspect every installed Django app and emit a schema string Gemini
    can use to write correct SQL.

    Universal: zero hardcoded table names, zero hardcoded column lists. Any
    table that exists in the current Django app registry is described; any
    table that does NOT exist is automatically absent. Adding a new model
    requires zero changes here — the next process restart picks it up.
    """
    from django.apps import apps
    lines = []
    # Group by app for readability
    by_app = {}
    for model in apps.get_models():
        if model._meta.app_label in _DB_SCHEMA_EXCLUDED_APPS:
            continue
        by_app.setdefault(model._meta.app_label, []).append(model)

    for app_label in sorted(by_app):
        lines.append(f'\n-- App: {app_label}')
        for model in sorted(by_app[app_label], key=lambda m: m._meta.db_table):
            table = model._meta.db_table
            field_strs = []
            for field in model._meta.get_fields():
                # Skip reverse-side relations (ManyToOneRel, OneToOneRel) — they
                # are not real columns. Only render fields that have a `column`
                # attribute (= actual DB columns).
                if not hasattr(field, 'column') or field.column is None:
                    continue
                try:
                    field_strs.append(_format_field(field))
                except Exception:
                    continue
            if field_strs:
                lines.append(f'{table}: ' + ', '.join(field_strs))
    return '\n'.join(lines)


def get_db_schema() -> str:
    """Return the schema string for Gemini prompts.

    Cached for the process lifetime — schema introspection happens once on
    first call, then re-uses the cached string for every subsequent chatbot
    query. To force a rebuild (e.g. after a `migrate` in dev), reset the
    module-level _DB_SCHEMA_CACHE to None.
    """
    global _DB_SCHEMA_CACHE
    if _DB_SCHEMA_CACHE is None:
        _DB_SCHEMA_CACHE = (
            _build_db_schema_from_django_models()
            + '\n\n'
            + _QUERY_HINTS
        )
    return _DB_SCHEMA_CACHE


# ---------------------------------------------------------------------------
# Guardrails — reject off-topic queries
# ---------------------------------------------------------------------------

OFF_TOPIC_PATTERNS = [
    r'\b(where\s+is|capital\s+of|president\s+of|prime\s+minister)\b',
    r'\b(recipe|weather|sports?\s+score|movie|song|joke|poem)\b',
    r'\b(who\s+is\s+(?!the\s+(?:fund|portfolio|investment|lp|gp|manager|trustee|custodian|auditor|sponsor)))',
    r'\b(taj\s+mahal|eiffel|statue\s+of\s+liberty)\b',
    r'\b(write\s+(?:me\s+)?(?:a\s+)?(?:code|program|script|essay|story))\b',
    r'\b(translate|define\s+the\s+word|spell)\b',
]

GUARDRAIL_RESPONSE = (
    "I'm TrackFundAI's portfolio intelligence assistant. I can only help with questions about "
    "your uploaded fund data, portfolio companies, investments, NAV, compliance, LP information, "
    "financial metrics, and market research related to your portfolio.\n\n"
    "Try asking:\n"
    "- \"What is the SEBI registration number of this fund?\"\n"
    "- \"How many portfolio companies do we have?\"\n"
    "- \"What's the total NAV across all schemes?\"\n"
    "- \"Show me overdue compliance filings\"\n"
    "- \"Which companies have the highest MOIC?\""
)


def _is_off_topic(query: str) -> bool:
    q = query.lower().strip()
    for pat in OFF_TOPIC_PATTERNS:
        if re.search(pat, q, re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# Dashboard Context Check — "which fund is selected?" etc.
# ---------------------------------------------------------------------------

_DASHBOARD_CONTEXT_PATTERNS = [
    r'which\s+fund\s+is\s+(selected|opened?|active|current|loaded)',
    r'what\s+fund\s+is\s+(on|opened?|selected|active)',
    r'current(ly)?\s+(selected|active|opened?)\s+fund',
    r'tell\s+me\s+which\s+fund',
    r'fund\s+(is\s+)?(opened?|selected|active)\s+(on|in)\s+(the\s+)?dashboard',
    r'what\s+(is|are)\s+(on|in)\s+(the\s+)?dashboard\s+right\s+now',
]


def _is_dashboard_context_query(query: str) -> bool:
    q = query.lower().strip()
    for pat in _DASHBOARD_CONTEXT_PATTERNS:
        if re.search(pat, q, re.IGNORECASE):
            return True
    return False


def _handle_dashboard_context(fund, fund_name_override=None) -> str:
    if fund:
        parts = [f'The currently selected fund on your dashboard is **{fund.name}**.']
        if fund.sebi_registration_number:
            parts.append(f'SEBI Registration: **{fund.sebi_registration_number}**')
        if fund.corpus_target:
            parts.append(f'Target Corpus: **Rs.{float(fund.corpus_target):,.2f} Cr**')
        if fund.fund_category:
            parts.append(f'Category: **{fund.fund_category.name}**')
        if fund.fund_status:
            parts.append(f'Status: **{fund.fund_status.replace("_", " ").title()}**')
        return '\n\n'.join(parts[:1]) + '\n' + '\n'.join(f'- {p}' for p in parts[1:])
    elif fund_name_override:
        return f'The currently selected fund on your dashboard is **{fund_name_override}**. However, I could not find this fund in the database — please ensure the fund Excel has been imported.'
    return 'No specific fund is currently selected on your dashboard — you are viewing **All Funds**. Select a fund from the dropdown to focus queries on a specific fund.'


# ---------------------------------------------------------------------------
# Intent Classifier
# ---------------------------------------------------------------------------

INTENT_SCHEMA = {
    'fund_info': 'User wants fund-level metadata: SEBI registration number, AIF category, fund structure (trust/LLP/company), corpus/target corpus, inception date, tenure, fund status, PAN, GSTIN, or information about linked entities such as Investment Manager, Trustee, Custodian, Sponsor, Auditor, Valuer — their names, SEBI registrations, contact details. Also: scheme details like close dates, scheme size, carry type, management fee terms, hurdle rate.',
    'portfolio_summary': 'User wants overview of portfolio companies: counts, sector breakdown, total invested, aggregate valuations, active vs exited, quoted vs unquoted, company listing.',
    'fund_performance': 'User wants fund/scheme performance metrics: IRR (gross/net), MOIC, TVPI, DPI, RVPI, NAV, NAV per unit, returns, dry powder, deployment pace, expense ratio. Also: NAV bridge, per-unit movement, NAV composition.',
    'company_financials': 'User wants P&L, revenue, EBITDA, PAT, cash, burn rate, runway, or financial details for a specific portfolio company.',
    'compliance_status': 'User wants compliance status: overdue SEBI filings (QAR/AAR/CTR), compliance calendar tasks, equity threshold alerts, FEMA compliance, AML/KYC status, compliance scores, SEBI circular actions.',
    'lp_information': 'User wants LP/investor data: commitments, capital calls (drawdowns), distributions, capital accounts, LP count, drawdown percentage, DPI per LP, LP types, KYC status, LP concentration.',
    'risk_analysis': 'User wants risk scores, anomaly alerts, budget variance alerts, watch list companies, concentration risk, macro risk exposure.',
    'kpi_analysis': 'User wants KPI trends: ARR, MRR, NRR, LTV/CAC, churn, ARPOB, occupancy, NIM, GNPA, ROE, DSCR, or any operational metrics for portfolio companies.',
    'exit_analysis': 'User wants exit events, exit scenarios, MOIC analysis, exit recommendations, holding period analysis, exit route breakdown (IPO/M&A/secondary).',
    'deal_pipeline': 'User wants IC pipeline status, deal stages, sourcing data, deal flow trends.',
    'valuation_analysis': 'User wants portfolio valuations: fair value, unrealized gains, multiples, methodology (DCF/comparable/IPEV), valuation changes, mark-to-market.',
    'accounting_query': 'User wants NAV records, management fee schedules, carried interest calculations (carry escrow, preferred return, profit above hurdle, waterfall), fund ledger entries, chart of accounts, fund P&L, balance sheet, cash flow.',
    'import_data': 'User wants information about uploaded Excel files, import history, which files were imported, sheet names in uploaded files, import status, errors during import.',
    'market_research': 'User wants sector comparisons, industry benchmarks, market analysis, macro environment, deal flow trends, regulatory changes, or analysis that requires general financial knowledge beyond the DB.',
    'general_query': 'General question about finance concepts, platform capabilities, fund structure explanations, or questions where no DB query is needed.',
    'out_of_scope': 'Question completely unrelated to finance, portfolio management, or fund data.',
}


def classify_intent(query: str, organization_name: str, fund_name: str = None) -> Dict[str, str]:
    """Use Gemini to classify user intent."""
    try:
        from api.gemini_service import generate_content

        intents_desc = '\n'.join(f'- {k}: {v}' for k, v in INTENT_SCHEMA.items())

        fund_context = ''
        if fund_name:
            fund_context = f'\nCurrently selected fund on dashboard: "{fund_name}"'

        prompt = f"""You are an intent classifier for TrackFundAI, a portfolio management platform for Indian AIFs (Alternative Investment Funds) operated by {organization_name}.{fund_context}

Available intents:
{intents_desc}

CRITICAL ROUTING RULES — read these carefully:
1. Questions about SEBI registration, AIF category, corpus, tenure, fund structure, investment manager, trustee, custodian, auditor, sponsor, management fees, hurdle rate, carry structure → "fund_info" (NOT compliance_status, NOT lp_information)
2. Questions about how many LPs, total commitments, capital calls (drawdowns), distributions, DPI per LP → "lp_information"
3. Questions about NAV, NAV per unit, IRR, MOIC, TVPI, DPI (fund-level), RVPI, returns → "fund_performance"
4. Questions about compliance filings (QAR, AAR, CTR), overdue reports, SEBI filing status → "compliance_status"
5. Questions about revenue, EBITDA, PAT, burn, runway for a specific company → "company_financials"
6. Questions about uploaded files, import status, Excel sheets → "import_data"
7. Only use "general_query" for conceptual/definitional questions (e.g., "what does MOIC mean?") or platform questions that don't need a DB query.
8. Only classify as "out_of_scope" if the query has ZERO relation to finance, investing, or portfolio management.
9. Extract company/fund names mentioned in the query as the "entity" field. If the user says "this fund" or "the fund" without a name but a fund is selected, set entity to null (the system knows the current fund).
10. For questions like "total corpus", "how much was raised", "final close" → "fund_info"

User query: "{query}"

Respond with JSON only (no markdown fences):
{{"intent": "<intent_key>", "entity": "<company/fund name if mentioned or null>", "time_filter": "<e.g. last 3 months, FY2025 or null>", "confidence": 0.0-1.0}}"""

        response = generate_content(prompt)
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


def _user_display_name(user) -> str:
    """Best display name for a Django user. Falls back to 'there' when unknown
    so prompts never produce empty addressing like 'Hi ,'. Works for any
    logged-in user, no hardcoded names."""
    if not user:
        return 'there'
    full = (user.get_full_name() or '').strip()
    if full:
        return full
    return (user.first_name or user.username or 'there').strip() or 'there'


def _user_first_name(user) -> str:
    """First name only, for casual addressing. Falls back gracefully."""
    if not user:
        return 'there'
    first = (user.first_name or '').strip()
    if first:
        return first
    full = (user.get_full_name() or '').strip()
    if full:
        return full.split()[0]
    return (user.username or 'there').strip() or 'there'


def build_context(organization, fund=None, company=None, intent_result=None, fund_name_override=None, user=None) -> Dict[str, Any]:
    ctx = {
        'organization_id': _uuid_for_sql(organization.pk),
        'organization_name': organization.name,
        'fund_id': _uuid_for_sql(fund.pk) if fund else None,
        'fund_name': fund.name if fund else fund_name_override,
        'company_id': _uuid_for_sql(company.pk) if company else None,
        'company_name': company.name if company else None,
        'user_name': _user_display_name(user),
        'user_first_name': _user_first_name(user),
    }

    entity_name = intent_result.get('entity') if intent_result else None
    if not entity_name:
        return ctx

    # Try to resolve entity as a Fund first (many queries reference fund names)
    if not fund:
        try:
            from funds.models import Fund
            # First: try exact substring match
            fund_match = Fund.objects.filter(
                organization=organization,
                name__icontains=entity_name,
            ).first()
            # If not found, try matching each significant word (handles "piramal fund III"
            # matching "Piramal Alternatives Fund III")
            if not fund_match:
                words = [w for w in entity_name.split() if len(w) > 2 and w.lower() not in ('the', 'fund', 'aif')]
                if words:
                    from django.db.models import Q
                    q_filter = Q(organization=organization)
                    for word in words:
                        q_filter &= Q(name__icontains=word)
                    fund_match = Fund.objects.filter(q_filter).first()
            if fund_match:
                ctx['fund_id'] = _uuid_for_sql(fund_match.pk)
                ctx['fund_name'] = fund_match.name
        except Exception:
            pass

    # Then try to resolve as a PortfolioCompany
    if not company:
        try:
            from investments.models import PortfolioCompany
            match = PortfolioCompany.objects.filter(
                organization=organization,
                name__icontains=entity_name,
            ).first()
            if not match:
                words = [w for w in entity_name.split() if len(w) > 2 and w.lower() not in ('the', 'ltd', 'pvt', 'limited', 'private')]
                if words:
                    from django.db.models import Q
                    q_filter = Q(organization=organization)
                    for word in words:
                        q_filter &= Q(name__icontains=word)
                    match = PortfolioCompany.objects.filter(q_filter).first()
            if match:
                ctx['company_id'] = _uuid_for_sql(match.pk)
                ctx['company_name'] = match.name
        except Exception:
            pass

    return ctx


# ---------------------------------------------------------------------------
# Template SQL — pre-built queries for common fund-level questions
# These bypass Gemini entirely → faster, cheaper, 100% reliable.
# ---------------------------------------------------------------------------

def _try_template_query(query: str, intent: str, context: Dict) -> Optional[str]:
    """Return a pre-built SQL string for common queries, or None to fall through to Gemini."""
    q = query.lower().strip()
    org_id = context['organization_id']
    fund_id = context.get('fund_id')

    # Helper: sub-select returning fund IDs for current scope
    def _fund_ids_subselect():
        if fund_id:
            return f"(SELECT '{fund_id}')"
        return f"(SELECT id FROM funds_fund WHERE organization_id = '{org_id}')"

    # Helper: fund WHERE clause (only use on outer query that has funds_fund aliased as 'f')
    def _fund_where(alias='f'):
        if fund_id:
            return f"{alias}.id = '{fund_id}'"
        return f"{alias}.organization_id = '{org_id}'"

    # ── fund_info templates ──────────────────────────────────────
    if intent == 'fund_info':
        # SEBI registration
        if any(kw in q for kw in ['sebi', 'registration', 'reg no', 'reg number']):
            return f"""SELECT f.name AS fund_name, f.sebi_registration_number, fc.name AS category, fc.sub_category,
                        f.structure_type, f.fund_status, f.pan AS fund_pan
                   FROM funds_fund f
                   LEFT JOIN funds_fundcategory fc ON f.fund_category_id = fc.id
                   WHERE {_fund_where()} LIMIT 10"""

        # Corpus / target corpus / how much raised / fund size
        if any(kw in q for kw in ['corpus', 'target', 'raised', 'fund size', 'how much']):
            return f"""SELECT f.name AS fund_name, f.corpus_target,
                        s.name AS scheme_name, s.scheme_size, s.first_close_date, s.final_close_date,
                        (SELECT SUM(c.commitment_amount) FROM lp_commitment c WHERE c.scheme_id = s.id) AS total_committed
                   FROM funds_fund f
                   LEFT JOIN funds_scheme s ON s.fund_id = f.id
                   WHERE {_fund_where()} LIMIT 10"""

        # Tenure / expiry / dissolution
        if any(kw in q for kw in ['tenure', 'expir', 'dissolut', 'wind', 'how long']):
            return f"""SELECT f.name AS fund_name, f.inception_date, f.fund_status,
                        s.name AS scheme_name, s.tenure_years, s.first_close_date, s.final_close_date,
                        s.dissolution_date, s.scheme_status
                   FROM funds_fund f
                   LEFT JOIN funds_scheme s ON s.fund_id = f.id
                   WHERE {_fund_where()} LIMIT 10"""

        # Investment Manager / Trustee / Custodian / Auditor / Sponsor
        if any(kw in q for kw in ['manager', 'trustee', 'custodian', 'auditor', 'sponsor', 'valuer',
                                    'who is the', 'entities', 'service provider']):
            return f"""SELECT f.name AS fund_name,
                        me.entity_name AS investment_manager, me.sebi_registration AS manager_sebi_reg,
                        te.entity_name AS trustee, te.sebi_registration AS trustee_sebi_reg,
                        ce.entity_name AS custodian, ce.sebi_registration AS custodian_sebi_reg,
                        ae.entity_name AS auditor,
                        se.entity_name AS sponsor
                   FROM funds_fund f
                   LEFT JOIN funds_entity me ON f.manager_entity_id = me.id
                   LEFT JOIN funds_entity te ON f.trustee_entity_id = te.id
                   LEFT JOIN funds_entity ce ON f.custodian_entity_id = ce.id
                   LEFT JOIN funds_entity ae ON f.auditor_entity_id = ae.id
                   LEFT JOIN funds_entity se ON f.sponsor_entity_id = se.id
                   WHERE {_fund_where()} LIMIT 10"""

        # Management fee / hurdle / carry / waterfall
        if any(kw in q for kw in ['management fee', 'hurdle', 'carried interest', 'carry',
                                    'waterfall', 'fee term', 'fee rate']):
            return f"""SELECT f.name AS fund_name, s.name AS scheme_name,
                        s.management_fee_pct, s.management_fee_basis,
                        s.hurdle_rate_pct, s.carry_pct, s.carry_type,
                        s.sponsor_commitment_pct
                   FROM funds_fund f
                   JOIN funds_scheme s ON s.fund_id = f.id
                   WHERE {_fund_where()} LIMIT 10"""

        # Legal structure
        if any(kw in q for kw in ['legal structure', 'structure', 'trust', 'llp', 'company']):
            return f"""SELECT f.name AS fund_name, f.structure_type, fc.name AS category,
                        fc.sub_category, f.is_gift_city, f.base_currency
                   FROM funds_fund f
                   LEFT JOIN funds_fundcategory fc ON f.fund_category_id = fc.id
                   WHERE {_fund_where()} LIMIT 10"""

        # Category / AIF category
        if any(kw in q for kw in ['category', 'aif', 'cat i', 'cat ii', 'cat iii']):
            return f"""SELECT f.name AS fund_name, fc.sebi_category_code, fc.name AS category_name,
                        fc.sub_category, fc.leverage_permitted
                   FROM funds_fund f
                   LEFT JOIN funds_fundcategory fc ON f.fund_category_id = fc.id
                   WHERE {_fund_where()} LIMIT 10"""

        # Final close
        if any(kw in q for kw in ['final close', 'first close', 'close date']):
            return f"""SELECT f.name AS fund_name, s.name AS scheme_name,
                        s.first_close_date, s.final_close_date, s.scheme_size,
                        (SELECT SUM(c.commitment_amount) FROM lp_commitment c WHERE c.scheme_id = s.id) AS total_committed
                   FROM funds_fund f
                   JOIN funds_scheme s ON s.fund_id = f.id
                   WHERE {_fund_where()} LIMIT 10"""

        # Generic fund info — return everything
        return f"""SELECT f.name AS fund_name, f.sebi_registration_number, f.corpus_target,
                    f.inception_date, f.structure_type, f.fund_status, f.base_currency,
                    fc.name AS category, fc.sub_category,
                    s.name AS scheme_name, s.scheme_size, s.vintage_year, s.tenure_years,
                    s.hurdle_rate_pct, s.carry_pct, s.management_fee_pct
               FROM funds_fund f
               LEFT JOIN funds_fundcategory fc ON f.fund_category_id = fc.id
               LEFT JOIN funds_scheme s ON s.fund_id = f.id
               WHERE {_fund_where()} LIMIT 10"""

    # ── portfolio_summary / fund_performance / valuation_analysis templates ─────
    if intent in ('portfolio_summary', 'fund_performance', 'valuation_analysis'):
        # Combined: fair value + company count (common combo query)
        if ('fair value' in q or 'total fv' in q or 'portfolio value' in q) and \
           any(kw in q for kw in ['compan', 'number', 'count', 'how many']):
            return f"""SELECT
                    COUNT(DISTINCT pc.id) AS total_companies,
                    SUM(CASE WHEN pc.is_active THEN 1 ELSE 0 END) AS active_companies,
                    COUNT(DISTINCT pc.sector) AS unique_sectors,
                    SUM(i.total_invested) AS total_cost,
                    SUM(latest_v.fair_value) AS total_fair_value,
                    SUM(latest_v.fair_value) - SUM(i.total_invested) AS unrealized_gain
                FROM investments_portfoliocompany pc
                JOIN investments_investment i ON i.portfolio_company_id = pc.id
                JOIN funds_scheme s ON i.scheme_id = s.id
                LEFT JOIN (
                    SELECT v1.investment_id, v1.fair_value
                    FROM investments_valuation v1
                    JOIN (SELECT investment_id, MAX(valuation_date) AS max_date
                          FROM investments_valuation GROUP BY investment_id) v2
                    ON v1.investment_id = v2.investment_id AND v1.valuation_date = v2.max_date
                ) latest_v ON latest_v.investment_id = i.id
                WHERE s.fund_id IN {_fund_ids_subselect()}
                  AND i.status = 'active'"""

        # Total fair value / portfolio FV (may land here via any of the 3 intents)
        if any(kw in q for kw in ['fair value', 'total fv', 'portfolio value', 'total value',
                                    'current value', 'market value']):
            return f"""SELECT
                    SUM(latest_v.fair_value) AS total_fair_value,
                    SUM(i.total_invested) AS total_cost,
                    COUNT(DISTINCT i.id) AS investment_count
                FROM investments_investment i
                JOIN funds_scheme s ON i.scheme_id = s.id
                LEFT JOIN (
                    SELECT v1.investment_id, v1.fair_value
                    FROM investments_valuation v1
                    JOIN (
                        SELECT investment_id, MAX(valuation_date) AS max_date
                        FROM investments_valuation GROUP BY investment_id
                    ) v2 ON v1.investment_id = v2.investment_id AND v1.valuation_date = v2.max_date
                ) latest_v ON latest_v.investment_id = i.id
                WHERE s.fund_id IN {_fund_ids_subselect()}
                  AND i.status = 'active'"""

        # IRR / net IRR / gross IRR (may land here if classified as fund_performance)
        if any(kw in q for kw in ['irr', 'internal rate', 'return']):
            return f"""SELECT lca.irr AS net_irr, lca.tvpi, lca.dpi, lca.rvpi, lca.moic,
                        lca.committed_capital, lca.called_capital, lca.distributed_capital,
                        lca.unrealized_value, lca.total_value, lca.as_of_date,
                        inv.investor_name
                   FROM lp_lpcapitalaccount lca
                   JOIN lp_commitment c ON lca.commitment_id = c.id
                   JOIN lp_investor inv ON c.investor_id = inv.id
                   JOIN funds_scheme s ON c.scheme_id = s.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   ORDER BY lca.as_of_date DESC LIMIT 20"""

        # TVPI / DPI / RVPI / MOIC
        if any(kw in q for kw in ['tvpi', 'dpi', 'rvpi', 'moic', 'multiple']):
            return f"""SELECT lca.tvpi, lca.dpi, lca.rvpi, lca.moic, lca.irr,
                        lca.committed_capital, lca.called_capital, lca.as_of_date,
                        inv.investor_name
                   FROM lp_lpcapitalaccount lca
                   JOIN lp_commitment c ON lca.commitment_id = c.id
                   JOIN lp_investor inv ON c.investor_id = inv.id
                   JOIN funds_scheme s ON c.scheme_id = s.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   ORDER BY lca.as_of_date DESC LIMIT 20"""

        # NAV / NAV per unit
        if any(kw in q for kw in ['nav', 'net asset', 'unit value', 'nav per unit']):
            return f"""SELECT n.nav_date, n.total_nav, n.nav_per_unit, n.total_units_outstanding,
                        n.investments_at_fair_value, n.cash_and_equivalents,
                        n.unrealized_gains, n.realized_gains,
                        s.name AS scheme_name
                   FROM accounting_navrecord n
                   JOIN funds_scheme s ON n.scheme_id = s.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   ORDER BY n.nav_date DESC LIMIT 5"""

        # Dry powder / remaining deployment
        if any(kw in q for kw in ['dry powder', 'remaining', 'undeployed', 'uninvested', 'deployment']):
            return f"""SELECT f.name AS fund_name, f.corpus_target,
                        SUM(i.total_invested) AS total_deployed,
                        (f.corpus_target - COALESCE(SUM(i.total_invested), 0)) AS dry_powder,
                        COUNT(DISTINCT i.id) AS investments_made
                   FROM funds_fund f
                   LEFT JOIN funds_scheme s ON s.fund_id = f.id
                   LEFT JOIN investments_investment i ON i.scheme_id = s.id AND i.status = 'active'
                   WHERE {_fund_where()}
                   GROUP BY f.id, f.name, f.corpus_target LIMIT 10"""

        # How many companies / portfolio count
        if any(kw in q for kw in ['how many', 'count', 'number of', 'total companies', 'active portfolio']):
            return f"""SELECT
                    COUNT(DISTINCT pc.id) AS total_companies,
                    SUM(CASE WHEN pc.is_active THEN 1 ELSE 0 END) AS active_companies,
                    SUM(CASE WHEN NOT pc.is_active THEN 1 ELSE 0 END) AS inactive_companies,
                    SUM(CASE WHEN pc.is_quoted THEN 1 ELSE 0 END) AS quoted_companies,
                    COUNT(DISTINCT pc.sector) AS unique_sectors
                FROM investments_portfoliocompany pc
                JOIN investments_investment i ON i.portfolio_company_id = pc.id
                JOIN funds_scheme s ON i.scheme_id = s.id
                WHERE s.fund_id IN {_fund_ids_subselect()}"""

        # Sector breakdown / allocation
        if any(kw in q for kw in ['sector', 'allocation', 'breakdown', 'distribution']):
            return f"""SELECT pc.sector, COUNT(DISTINCT pc.id) AS company_count,
                        SUM(i.total_invested) AS total_invested,
                        SUM(latest_v.fair_value) AS total_fair_value
                   FROM investments_portfoliocompany pc
                   JOIN investments_investment i ON i.portfolio_company_id = pc.id
                   JOIN funds_scheme s ON i.scheme_id = s.id
                   LEFT JOIN (
                       SELECT v1.investment_id, v1.fair_value
                       FROM investments_valuation v1
                       JOIN (SELECT investment_id, MAX(valuation_date) AS max_date
                             FROM investments_valuation GROUP BY investment_id) v2
                       ON v1.investment_id = v2.investment_id AND v1.valuation_date = v2.max_date
                   ) latest_v ON latest_v.investment_id = i.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   GROUP BY pc.sector ORDER BY total_fair_value DESC LIMIT 20"""

        # List all companies
        if any(kw in q for kw in ['list', 'all companies', 'show companies', 'portfolio companies']):
            return f"""SELECT pc.name AS company_name, pc.sector, pc.is_active, pc.is_quoted,
                        i.instrument_type, i.total_invested, i.investment_date, i.status,
                        latest_v.fair_value, latest_v.multiple AS moic
                   FROM investments_portfoliocompany pc
                   JOIN investments_investment i ON i.portfolio_company_id = pc.id
                   JOIN funds_scheme s ON i.scheme_id = s.id
                   LEFT JOIN (
                       SELECT v1.investment_id, v1.fair_value, v1.multiple
                       FROM investments_valuation v1
                       JOIN (SELECT investment_id, MAX(valuation_date) AS max_date
                             FROM investments_valuation GROUP BY investment_id) v2
                       ON v1.investment_id = v2.investment_id AND v1.valuation_date = v2.max_date
                   ) latest_v ON latest_v.investment_id = i.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   ORDER BY latest_v.fair_value DESC LIMIT 50"""

        # Top companies by fair value
        if any(kw in q for kw in ['top', 'highest', 'best', 'largest']):
            return f"""SELECT pc.name AS company_name, pc.sector,
                        i.total_invested AS cost, latest_v.fair_value,
                        latest_v.multiple AS moic, i.status
                   FROM investments_portfoliocompany pc
                   JOIN investments_investment i ON i.portfolio_company_id = pc.id
                   JOIN funds_scheme s ON i.scheme_id = s.id
                   LEFT JOIN (
                       SELECT v1.investment_id, v1.fair_value, v1.multiple
                       FROM investments_valuation v1
                       JOIN (SELECT investment_id, MAX(valuation_date) AS max_date
                             FROM investments_valuation GROUP BY investment_id) v2
                       ON v1.investment_id = v2.investment_id AND v1.valuation_date = v2.max_date
                   ) latest_v ON latest_v.investment_id = i.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   ORDER BY latest_v.fair_value DESC LIMIT 10"""

    # ── lp_information templates ─────────────────────────────────
    if intent == 'lp_information':
        # Capital call / drawdown / final call
        if any(kw in q for kw in ['capital call', 'drawdown', 'call', 'drawn']):
            return f"""SELECT cc.call_number, cc.call_date, cc.payment_due_date,
                        cc.call_percentage, cc.total_call_amount, cc.call_status,
                        s.name AS scheme_name
                   FROM lp_capitalcall cc
                   JOIN funds_scheme s ON cc.scheme_id = s.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   ORDER BY cc.call_date DESC LIMIT 20"""

        # LP count / how many investors
        if any(kw in q for kw in ['how many lp', 'how many investor', 'lp count', 'investor count',
                                    'number of lp', 'number of investor']):
            return f"""SELECT COUNT(DISTINCT inv.id) AS total_lps,
                        SUM(c.commitment_amount) AS total_commitment,
                        inv.investor_type, COUNT(inv.id) AS count_by_type
                   FROM lp_investor inv
                   JOIN lp_commitment c ON c.investor_id = inv.id
                   JOIN funds_scheme s ON c.scheme_id = s.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   GROUP BY inv.investor_type ORDER BY total_commitment DESC LIMIT 20"""

        # Distribution
        if any(kw in q for kw in ['distribution', 'distributed', 'payout']):
            return f"""SELECT d.distribution_number, d.distribution_date, d.distribution_type,
                        d.total_gross_amount, d.total_tds_amount, d.total_net_amount,
                        d.distribution_status, s.name AS scheme_name
                   FROM lp_distribution d
                   JOIN funds_scheme s ON d.scheme_id = s.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   ORDER BY d.distribution_date DESC LIMIT 20"""

    # ── accounting_query templates ───────────────────────────────
    if intent == 'accounting_query':
        # Carried interest / carry escrow / preferred return
        if any(kw in q for kw in ['carried interest', 'carry', 'escrow', 'preferred return',
                                    'profit above hurdle']):
            return f"""SELECT ci.calculation_date, ci.total_distributions, ci.total_called_capital,
                        ci.preferred_return_amount, ci.profit_above_hurdle,
                        ci.carry_amount_gross, ci.carry_amount_net, ci.carry_escrow_balance,
                        s.name AS scheme_name
                   FROM accounting_carriedinterest ci
                   JOIN funds_scheme s ON ci.scheme_id = s.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   ORDER BY ci.calculation_date DESC LIMIT 10"""

        # Management fees
        if any(kw in q for kw in ['management fee', 'fee schedule', 'fee amount']):
            return f"""SELECT mf.period_start, mf.period_end, mf.fee_basis_amount,
                        mf.fee_rate, mf.fee_amount, mf.fee_status,
                        s.name AS scheme_name
                   FROM accounting_managementfeeschedule mf
                   JOIN funds_scheme s ON mf.scheme_id = s.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   ORDER BY mf.period_start DESC LIMIT 20"""

    # ── risk_analysis templates ─────────────────────────────────
    if intent == 'risk_analysis':
        # Underperforming companies (MOIC < 1.0 → fair value < cost)
        if any(kw in q for kw in ['underperform', 'loss', 'losing', 'below cost', 'write down',
                                    'moic below', 'moic less', 'moic < 1', 'negative return',
                                    'worst', 'laggard', 'drag', 'impair']):
            return f"""SELECT pc.name AS company_name, pc.sector, pc.is_active,
                        i.total_invested AS cost_basis,
                        latest_v.fair_value,
                        (latest_v.fair_value - i.total_invested) AS unrealized_gain_loss,
                        CASE WHEN i.total_invested > 0
                             THEN ROUND(CAST(latest_v.fair_value AS NUMERIC) / CAST(i.total_invested AS NUMERIC), 2)
                             ELSE NULL END AS moic,
                        latest_v.valuation_date,
                        i.instrument_type, i.investment_date
                   FROM investments_portfoliocompany pc
                   JOIN investments_investment i ON i.portfolio_company_id = pc.id
                   JOIN funds_scheme s ON i.scheme_id = s.id
                   LEFT JOIN (
                       SELECT v1.investment_id, v1.fair_value, v1.valuation_date
                       FROM investments_valuation v1
                       JOIN (SELECT investment_id, MAX(valuation_date) AS max_date
                             FROM investments_valuation GROUP BY investment_id) v2
                       ON v1.investment_id = v2.investment_id AND v1.valuation_date = v2.max_date
                   ) latest_v ON latest_v.investment_id = i.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                     AND i.status = 'active'
                     AND latest_v.fair_value IS NOT NULL
                     AND latest_v.fair_value < i.total_invested
                   ORDER BY (latest_v.fair_value - i.total_invested) ASC LIMIT 20"""

        # Concentration risk (single company or sector too large)
        if any(kw in q for kw in ['concentrat', 'exposure', 'overweight', 'single name',
                                    'largest position', 'top holding']):
            return f"""SELECT pc.name AS company_name, pc.sector,
                        i.total_invested AS cost,
                        latest_v.fair_value,
                        i.ownership_pct,
                        i.percentage_stake_fully_diluted
                   FROM investments_portfoliocompany pc
                   JOIN investments_investment i ON i.portfolio_company_id = pc.id
                   JOIN funds_scheme s ON i.scheme_id = s.id
                   LEFT JOIN (
                       SELECT v1.investment_id, v1.fair_value
                       FROM investments_valuation v1
                       JOIN (SELECT investment_id, MAX(valuation_date) AS max_date
                             FROM investments_valuation GROUP BY investment_id) v2
                       ON v1.investment_id = v2.investment_id AND v1.valuation_date = v2.max_date
                   ) latest_v ON latest_v.investment_id = i.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                     AND i.status = 'active'
                   ORDER BY latest_v.fair_value DESC LIMIT 20"""

        # Budget variance / MIS anomalies
        if any(kw in q for kw in ['variance', 'budget', 'anomal', 'alert', 'mis']):
            return f"""SELECT ma.alert_type, ma.severity, ma.description, ma.is_resolved,
                        f.name AS fund_name
                   FROM mis_consolidation_misanomalyalert ma
                   JOIN funds_fund f ON ma.fund_id = f.id
                   WHERE {_fund_where()}
                   ORDER BY ma.severity DESC, ma.is_resolved ASC LIMIT 20"""

        # Watch list / at-risk (generic risk query — show all investments with MOIC and sort worst-first)
        return f"""SELECT pc.name AS company_name, pc.sector, pc.is_active,
                    i.total_invested AS cost_basis,
                    latest_v.fair_value,
                    (latest_v.fair_value - i.total_invested) AS unrealized_gain_loss,
                    CASE WHEN i.total_invested > 0
                         THEN ROUND(CAST(latest_v.fair_value AS NUMERIC) / CAST(i.total_invested AS NUMERIC), 2)
                         ELSE NULL END AS moic,
                    latest_v.valuation_date, i.status
               FROM investments_portfoliocompany pc
               JOIN investments_investment i ON i.portfolio_company_id = pc.id
               JOIN funds_scheme s ON i.scheme_id = s.id
               LEFT JOIN (
                   SELECT v1.investment_id, v1.fair_value, v1.valuation_date
                   FROM investments_valuation v1
                   JOIN (SELECT investment_id, MAX(valuation_date) AS max_date
                         FROM investments_valuation GROUP BY investment_id) v2
                   ON v1.investment_id = v2.investment_id AND v1.valuation_date = v2.max_date
               ) latest_v ON latest_v.investment_id = i.id
               WHERE s.fund_id IN {_fund_ids_subselect()}
                 AND i.status = 'active'
               ORDER BY moic ASC NULLS LAST LIMIT 20"""

    # ── compliance_status templates ─────────────────────────────
    if intent == 'compliance_status':
        # Overdue filings
        if any(kw in q for kw in ['overdue', 'pending', 'missed', 'late', 'not filed']):
            return f"""SELECT sr.report_type, sr.due_date, sr.filing_status, sr.filed_date,
                        f.name AS fund_name, s.name AS scheme_name
                   FROM compliance_sebireport sr
                   JOIN funds_fund f ON sr.fund_id = f.id
                   LEFT JOIN funds_scheme s ON sr.scheme_id = s.id
                   WHERE {_fund_where()}
                     AND sr.filing_status IN ('pending', 'overdue')
                   ORDER BY sr.due_date ASC LIMIT 20"""

        # QAR / AAR / CTR specific
        if any(kw in q for kw in ['qar', 'aar', 'ctr', 'annual return', 'quarterly']):
            return f"""SELECT sr.report_type, sr.due_date, sr.filing_status, sr.filed_date,
                        f.name AS fund_name, s.name AS scheme_name
                   FROM compliance_sebireport sr
                   JOIN funds_fund f ON sr.fund_id = f.id
                   LEFT JOIN funds_scheme s ON sr.scheme_id = s.id
                   WHERE {_fund_where()}
                   ORDER BY sr.due_date DESC LIMIT 20"""

        # Compliance calendar
        if any(kw in q for kw in ['calendar', 'upcoming', 'deadline', 'due date', 'schedule']):
            return f"""SELECT cc.title, cc.due_date, cc.status, cc.completed_date,
                        f.name AS fund_name
                   FROM compliance_compliancecalendar cc
                   LEFT JOIN funds_fund f ON cc.fund_id = f.id
                   WHERE cc.organization_id = '{org_id}'
                   ORDER BY cc.due_date ASC LIMIT 20"""

        # Equity threshold alerts
        if any(kw in q for kw in ['threshold', 'breach', 'equity', '10%', 't+30']):
            return f"""SELECT pc.name AS company_name, eta.stake_percentage,
                        eta.threshold_breached, eta.breach_date, eta.severity, eta.resolved
                   FROM compliance_equitythresholdalert eta
                   JOIN investments_investment i ON eta.investment_id = i.id
                   JOIN investments_portfoliocompany pc ON i.portfolio_company_id = pc.id
                   JOIN funds_scheme s ON i.scheme_id = s.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   ORDER BY eta.breach_date DESC LIMIT 20"""

        # Compliance scores
        if any(kw in q for kw in ['score', 'rating', 'compliance score']):
            return f"""SELECT fcs.score_date, fcs.combined_score, f.name AS fund_name
                   FROM compliance_fundcompliancescore fcs
                   JOIN funds_fund f ON fcs.fund_id = f.id
                   WHERE {_fund_where()}
                   ORDER BY fcs.score_date DESC LIMIT 10"""

        # Generic compliance query
        return f"""SELECT sr.report_type, sr.due_date, sr.filing_status, sr.filed_date,
                    f.name AS fund_name
               FROM compliance_sebireport sr
               JOIN funds_fund f ON sr.fund_id = f.id
               WHERE {_fund_where()}
               ORDER BY sr.due_date DESC LIMIT 20"""

    # ── exit_analysis templates ─────────────────────────────────
    if intent == 'exit_analysis':
        # Exit events
        if any(kw in q for kw in ['exit', 'ipo', 'secondary', 'buyback', 'strategic sale',
                                    'write off', 'realized', 'exited']):
            return f"""SELECT pc.name AS company_name, pc.sector,
                        ee.exit_type, ee.exit_date, ee.is_actual,
                        ee.exit_valuation, ee.proceeds, ee.net_exit_proceeds,
                        ee.realized_gain_loss, ee.moic, ee.irr_pct, ee.buyer_name
                   FROM investments_exitevent ee
                   JOIN investments_investment i ON ee.investment_id = i.id
                   JOIN investments_portfoliocompany pc ON i.portfolio_company_id = pc.id
                   JOIN funds_scheme s ON i.scheme_id = s.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   ORDER BY ee.exit_date DESC LIMIT 20"""

        # Fallback — same as above
        return f"""SELECT pc.name AS company_name, pc.sector,
                    ee.exit_type, ee.exit_date, ee.is_actual,
                    ee.exit_valuation, ee.proceeds, ee.net_exit_proceeds,
                    ee.realized_gain_loss, ee.moic, ee.irr_pct, ee.buyer_name
               FROM investments_exitevent ee
               JOIN investments_investment i ON ee.investment_id = i.id
               JOIN investments_portfoliocompany pc ON i.portfolio_company_id = pc.id
               JOIN funds_scheme s ON i.scheme_id = s.id
               WHERE s.fund_id IN {_fund_ids_subselect()}
               ORDER BY ee.exit_date DESC LIMIT 20"""

    # ── kpi_analysis templates ──────────────────────────────────
    if intent == 'kpi_analysis':
        # Specific company KPIs
        if context.get('company_id'):
            return f"""SELECT kd.name AS kpi_name, kd.format, pk.period, pk.value, pk.status,
                        pc.name AS company_name
                   FROM investments_portfoliokpi pk
                   JOIN investments_kpidefinition kd ON pk.kpi_definition_id = kd.id
                   JOIN investments_portfoliocompany pc ON pk.portfolio_company_id = pc.id
                   WHERE pk.portfolio_company_id = '{context['company_id']}'
                   ORDER BY pk.period DESC, kd.name LIMIT 50"""

        # Burn rate / runway
        if any(kw in q for kw in ['burn', 'runway', 'cash']):
            return f"""SELECT pc.name AS company_name,
                        cf.period, cf.gross_burn, cf.net_burn, cf.cash_balance, cf.runway_months
                   FROM investments_companyfinancials cf
                   JOIN investments_portfoliocompany pc ON cf.portfolio_company_id = pc.id
                   JOIN investments_investment i ON cf.investment_id = i.id
                   JOIN funds_scheme s ON i.scheme_id = s.id
                   WHERE s.fund_id IN {_fund_ids_subselect()}
                   ORDER BY cf.period DESC LIMIT 30"""

        # Generic KPIs across portfolio
        return f"""SELECT pc.name AS company_name, kd.name AS kpi_name, pk.period, pk.value, pk.status
               FROM investments_portfoliokpi pk
               JOIN investments_kpidefinition kd ON pk.kpi_definition_id = kd.id
               JOIN investments_portfoliocompany pc ON pk.portfolio_company_id = pc.id
               JOIN investments_investment i ON pk.investment_id = i.id
               JOIN funds_scheme s ON i.scheme_id = s.id
               WHERE s.fund_id IN {_fund_ids_subselect()}
               ORDER BY pk.period DESC, pc.name LIMIT 50"""

    # ── deal_pipeline templates ─────────────────────────────────
    if intent == 'deal_pipeline':
        return f"""SELECT dp.company_name, dp.sector, dp.stage, dp.proposed_investment_inr,
                    dp.sourced_date, f.name AS fund_name
               FROM ic_workflow_dealpipeline dp
               LEFT JOIN funds_fund f ON dp.fund_id = f.id
               WHERE dp.organization_id = '{org_id}'
               {f"AND dp.fund_id = '{fund_id}'" if fund_id else ''}
               ORDER BY dp.sourced_date DESC LIMIT 30"""

    # ── import_data templates ────────────────────────────────────
    if intent == 'import_data':
        return f"""SELECT ij.id AS job_id, ij.status, ij.total_files, ij.created_at, ij.completed_at,
                    imf.original_filename, imf.file_size, imf.status AS file_status,
                    imf.fund_name, imf.sheet_names
               FROM dataimport_importjob ij
               JOIN dataimport_importfile imf ON imf.job_id = ij.id
               WHERE ij.organization_id = '{org_id}'
               ORDER BY ij.created_at DESC LIMIT 20"""

    # No template matched — fall through to Gemini
    return None


# ---------------------------------------------------------------------------
# SQL Query Builder (with full schema + fund-aware prompting)
# ---------------------------------------------------------------------------

def build_sql_query(query: str, intent: str, context: Dict, time_filter: Optional[str], entity: Optional[str]) -> Optional[str]:
    try:
        from api.gemini_service import generate_content

        # ── Dialect awareness (Postgres-ready, SQLite-compatible today) ──
        # `connection.vendor` is Django's portable way to identify the active
        # database engine — 'sqlite', 'postgresql', 'mysql', 'oracle'. It
        # reflects whatever DATABASES['default']['ENGINE'] is set to, so this
        # code adapts automatically the day we migrate to PostgreSQL. Gemini
        # is told the dialect explicitly and given the dialect's syntax rails.
        from django.db import connection as _conn
        vendor = _conn.vendor  # 'sqlite' or 'postgresql' (or 'mysql' / 'oracle')
        db_type = {
            'sqlite':     'SQLite',
            'postgresql': 'PostgreSQL',
            'mysql':      'MySQL',
            'oracle':     'Oracle',
        }.get(vendor, vendor.title())

        # Dialect-specific syntax to AVOID (asymmetric — what's natural in
        # one dialect breaks the other). Updated to match the active DB so
        # the warning is always for the wrong-dialect features, never for
        # features Gemini could legitimately use.
        if vendor == 'sqlite':
            dialect_warnings = (
                "Do NOT use PostgreSQL-only constructs: DISTINCT ON, "
                "FILTER (WHERE ...), LATERAL joins, ::type casts, ILIKE, "
                "or array/JSONB operators. Use standard SQL only."
            )
        elif vendor == 'postgresql':
            dialect_warnings = (
                "Do NOT use SQLite-only constructs: STRFTIME(), DATETIME(), "
                "JULIANDAY(), `||` for date math, or relying on type "
                "affinity. Use standard PostgreSQL syntax (TO_CHAR, EXTRACT, "
                "DATE_TRUNC, AGE)."
            )
        else:
            dialect_warnings = (
                f"Use standard SQL features compatible with {db_type}; "
                "avoid vendor-specific extensions."
            )

        prompt = f"""You are a SQL query builder for a {db_type} database powering TrackFundAI, a fund management platform for Indian AIFs.
Generate a read-only SELECT query to answer the user's question.

USER QUESTION: "{query}"

DATABASE SCHEMA:
{get_db_schema()}

QUERY CONTEXT:
Intent: {intent}
Organization ID: {context['organization_id']}
Fund ID: {context.get('fund_id') or 'not specified — query across all funds'}
Fund Name: {context.get('fund_name') or 'not specified'}
Company ID: {context.get('company_id') or 'not specified'}
Company Name: {context.get('company_name') or 'not specified'}
Time filter: {time_filter or 'most recent data available'}
Entity mentioned: {entity or 'none'}

IMPORTANT: UUIDs in this database are stored WITHOUT hyphens (e.g., '{context['organization_id']}').

MANDATORY RULES:
1. ONLY use SELECT statements — never INSERT, UPDATE, DELETE, DROP
2. ALWAYS filter by organization: use organization_id directly or via JOINs through fund→scheme→investment
3. LIMIT policy — DO NOT add an arbitrary safety LIMIT. The runtime applies a memory cap separately. Use LIMIT only when the user's question itself implies one ("top 10", "first 5", "latest 3"). For "list all", "show every", "breakdown by", "how many in each" — emit NO LIMIT clause.
4. Use proper JOINs to traverse: investment → scheme → fund → organization
5. Return ONLY the raw SQL query text, no markdown, no explanation
6. If you truly cannot build a safe query, return exactly: CANNOT_ANSWER
7. Use column aliases for readability (e.g., pc.name AS company_name)
8. For aggregations, include both the aggregate and GROUP BY columns
9. Prefer LEFT JOIN over INNER JOIN to avoid losing data
10. All monetary amounts are in Cr (Crores) unless from mis_consolidation tables (those are in Lakhs)
11. {dialect_warnings}
12. For "latest record per group" use ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...) in a subquery — this works in every dialect.
13. Keep queries simple — avoid unnecessary complexity.
14. If the user mentions a category / sector / status / type word (e.g. "healthcare", "financial", "active", "exited"), translate it into a WHERE filter on the appropriate column using ILIKE / LIKE — never ignore it.
15. For detail (row-listing) queries, ORDER BY a deterministic, non-null column such as the entity name (pc.name, inv.investor_name, etc.) — never order by a column that may be all-NULL or all-zero, which makes truncation non-deterministic.

INTENT-SPECIFIC GUIDANCE:
- fund_info: Query funds_fund + funds_fundcategory + funds_entity (via manager_entity_id, trustee_entity_id, custodian_entity_id, etc.) + funds_scheme. If entity name is mentioned, filter by f.name ILIKE '%entity%'. If fund_id is given, use WHERE f.id = 'fund_id'.
- fund_performance: Query accounting_navrecord, lp_lpcapitalaccount for IRR/MOIC/TVPI/DPI. Join via scheme→fund.
- lp_information: Query lp_investor, lp_commitment, lp_capitalcall, lp_distribution. Join via scheme→fund.
- portfolio_summary: Query investments_portfoliocompany + investments_investment + investments_valuation.
- company_financials: Query mis_consolidation_budgetvsactual or investments_companyfinancials.
- compliance_status: Query compliance_sebireport, compliance_compliancecalendar.
- accounting_query: Query accounting_navrecord, accounting_carriedinterest, accounting_fundledger, accounting_managementfeeschedule.
- import_data: Query dataimport_importjob + dataimport_importfile.
- If Fund Name is given but Fund ID is not, use WHERE f.name ILIKE '%Fund Name%' to resolve it.

SQL:"""

        response = generate_content(prompt)
        sql = response.text.strip()
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
    if not (sql_lower.startswith('select') or sql_lower.startswith('with')):
        return False
    if sql_lower.startswith('with') and 'select' not in sql_lower:
        return False
    table_pattern = re.compile(r'(?:from|join)\s+([a-z_][a-z0-9_]*)', re.IGNORECASE)
    referenced = set(m.group(1).lower() for m in table_pattern.finditer(sql))
    cte_names = set(m.group(1).lower() for m in re.finditer(r'\b(\w+)\s+AS\s*\(', sql, re.IGNORECASE))
    actual_tables = referenced - cte_names
    for table in actual_tables:
        if table not in ALLOWED_TABLES:
            return False
    return True


# ---------------------------------------------------------------------------
# Dual-SQL Builder — aggregate (truth) + detail (sample)
# ---------------------------------------------------------------------------
#
# Why two queries per turn:
#   The single-query approach forced one SQL to answer both "how many X in
#   total" and "give me the rows" at once. The result was always biased: a
#   LIMIT N detail query never carries the true counts, and renderers that
#   summarized from the truncated rows hallucinated ("20 financial companies"
#   when the DB had 30; "all are Consumer & Retail" when 4 sectors existed).
#
#   Splitting into two queries gives the renderer separate authoritative
#   sources: the aggregate is the truth for any count / distribution claim;
#   the detail is the rows the user wants to inspect, scoped strictly by
#   their filter words. Neither steps on the other.
#
# Both queries:
#   • Are generated by Gemini in a single call (returns JSON {aggregate_sql,
#     detail_sql, grouping_dimension}) — no keyword templates, no regex.
#   • Honor the natural-language filter words from the user query as WHERE
#     clauses (e.g. "healthcare" → WHERE pc.sector ILIKE '%healthcare%').
#   • Pass through _is_sql_safe() before execution.

def build_aggregate_and_detail_sql(
    query: str, intent: str, context: Dict,
    time_filter: Optional[str], entity: Optional[str],
) -> Optional[Dict[str, Optional[str]]]:
    """Return {'aggregate_sql': str|None, 'detail_sql': str|None, 'grouping_dimension': str|None}.

    aggregate_sql: GROUP BY query producing authoritative counts/sums for
        the grouping dimension implied by the user's question. MUST NOT
        apply the user's category filter — it shows the FULL distribution
        so the renderer knows what categories exist and how many rows each
        has. If the question is purely scalar ("what is total NAV?"), this
        is the fund-wide aggregate without GROUP BY.
    detail_sql: row-level query that DOES apply the user's category filter.
        Used to surface the specific rows the user asked about. May be NULL
        when the user only wanted aggregates ("breakdown by sector").
    grouping_dimension: human-readable name of the dimension grouped on
        (e.g. "sector", "investor_type"). NULL when no grouping applies.
    """
    try:
        from api.gemini_service import generate_content

        from django.db import connection as _conn
        vendor = _conn.vendor
        db_type = {
            'sqlite':     'SQLite',
            'postgresql': 'PostgreSQL',
            'mysql':      'MySQL',
            'oracle':     'Oracle',
        }.get(vendor, vendor.title())

        if vendor == 'sqlite':
            dialect_note = "SQLite — use LIKE not ILIKE; LIKE in SQLite is case-insensitive by default for ASCII."
        elif vendor == 'postgresql':
            dialect_note = "PostgreSQL — use ILIKE for case-insensitive matching."
        else:
            dialect_note = f"{db_type} — use standard SQL only."

        prompt = f"""You are a SQL planner for a {db_type} database powering TrackFundAI, a fund management platform for Indian AIFs.

USER QUESTION: "{query}"

INTENT: {intent}
FUND ID:    {context.get('fund_id') or 'not specified — query across all funds in the org'}
ORG ID:     {context['organization_id']}
COMPANY ID: {context.get('company_id') or 'not specified'}
ENTITY MENTIONED IN QUERY: {entity or 'none'}
TIME FILTER: {time_filter or 'most recent / all-time'}

DATABASE SCHEMA:
{get_db_schema()}

YOUR JOB
========
Produce TWO complementary SELECT queries that together answer the question accurately, with no sampling bias:

1. aggregate_sql — the AUTHORITATIVE TRUTH query.
   • Groups by the dimension the user's question implies (sector, investor_type, instrument_type, status, exit_type, etc.).
   • Returns per-group COUNTs / SUMs across the full scoped dataset.
   • MUST NOT apply the user's category filter (e.g. don't filter to just "healthcare") — it must show the FULL distribution so the renderer knows every group and its true size. The filter belongs in detail_sql only.
   • If the question has NO natural grouping (e.g. "what is the total NAV?" — a scalar query), aggregate_sql is the fund-wide SUM/COUNT without GROUP BY.
   • Emit NO LIMIT — the natural cardinality of GROUP BY is the bound. (Runtime has a memory safety cap.)
   • Always include organization/fund scoping in the WHERE.

2. detail_sql — the FILTERED ROW LIST.
   • Selects the per-row columns the user wants to see (name, dates, values, etc.).
   • Applies the user's category filter using LIKE/ILIKE on the appropriate text column (per dialect: {dialect_note}).
   • If the user said "list all" / "show every" — NO LIMIT clause. If the user said "top N" / "first N" / "latest N" — use LIMIT N exactly. Otherwise emit NO LIMIT.
   • ORDER BY a deterministic, non-null column such as the entity name (pc.name, inv.investor_name, ee.exit_date). NEVER order by a column that may be all-NULL or all-zero (e.g. fair_value when valuations are absent), because that makes any cap non-deterministic and hides rows arbitrarily.
   • If the question is purely about aggregates ("breakdown by X", "how many Y in each Z"), detail_sql may be NULL.

3. grouping_dimension — short human label for the dimension you grouped on (e.g. "sector", "investor_type", "exit_type"), or null when no grouping.

HARD RULES
==========
• Only SELECT — no DML/DDL.
• Always scope to organization/fund via the canonical JOIN: scheme → fund → organization.
• UUIDs are stored WITHOUT hyphens.
• All monetary columns are in INR Crore (Cr) unless the table starts with mis_consolidation_ (those are Lakhs).
• Use standard SQL compatible with {db_type}.
• {dialect_note}
• If you genuinely cannot construct a safe pair, return both fields as null.

RETURN FORMAT — JSON ONLY, no markdown fences:
{{
  "aggregate_sql": "SELECT ...",
  "detail_sql": "SELECT ...",
  "grouping_dimension": "sector"
}}
"""
        response = generate_content(prompt)
        text = response.text.strip()
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        parsed = json.loads(text)

        agg = parsed.get('aggregate_sql')
        det = parsed.get('detail_sql')
        dim = parsed.get('grouping_dimension')

        # Validate each independently — one may be null by design.
        if agg and not _is_sql_safe(agg):
            logger.warning(f'Unsafe aggregate SQL rejected: {agg[:200]}')
            agg = None
        if det and not _is_sql_safe(det):
            logger.warning(f'Unsafe detail SQL rejected: {det[:200]}')
            det = None

        return {'aggregate_sql': agg, 'detail_sql': det, 'grouping_dimension': dim}
    except Exception as e:
        logger.warning(f'Dual-SQL builder error: {e}')
        return None


# ---------------------------------------------------------------------------
# Data Fetcher
# ---------------------------------------------------------------------------

# Safety cap on rows fetched per query. NOT a presentation cap and NOT
# auto-injected as a LIMIT clause. Exists only to keep a runaway query from
# streaming millions of rows into memory. Real GROUP BY aggregates almost
# never exceed a few hundred groups; real per-fund detail queries
# (companies, LPs, NAV history) are likewise typically well below this. If
# the cap is hit, `meta['truncated']` is set and render_response() tells
# Gemini explicitly that the visible rows are a partial slice.
ROW_SAFETY_CAP = 2000


def execute_query(sql: str, max_rows: int = ROW_SAFETY_CAP) -> Tuple[List[str], List[tuple]]:
    """Run Gemini-generated SQL and return (columns, rows).

    Backward-compatible 2-tuple return. For truncation metadata use
    execute_query_with_meta().
    """
    cols, rows, _ = execute_query_with_meta(sql, max_rows=max_rows)
    return cols, rows


def execute_query_with_meta(sql: str, max_rows: int = ROW_SAFETY_CAP) -> Tuple[List[str], List[tuple], Dict]:
    """Run SQL with truncation detection.

    Does NOT force-inject a LIMIT clause. Whatever LIMIT the SQL itself
    contains (or doesn't contain) is honored; the executor only enforces a
    memory safety cap of max_rows. The previous behavior — auto-appending
    `LIMIT 50` to every Gemini-generated query — was the root of the "20 of
    30 financial companies visible" class of bug: a sector-clustered
    insertion order would systematically hide later groups beneath the
    arbitrary 50-row cap.
    """
    meta = {'truncated': False, 'true_count': None}
    if not sql or not _is_sql_safe(sql):
        return [], [], meta
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            columns = [col[0] for col in cursor.description] if cursor.description else []
            # Fetch one extra row past the safety cap so we can detect when
            # the real result set is bigger than what we kept.
            rows = cursor.fetchmany(max_rows + 1)
        if len(rows) > max_rows:
            meta['truncated'] = True
            rows = rows[:max_rows]
        return columns, rows, meta
    except Exception as e:
        logger.warning(f'SQL execution error: {e} | SQL: {sql[:200]}')
        return [], [], meta


def _build_dataset_profile(columns: List[str], rows: List[tuple]) -> str:
    """Compute a faithful distinct-value frequency profile from the FULL
    returned rows (not just the first 25 the prompt shows).

    For each column with <= 20 distinct values, list every value with its
    visible-sample count.  For high-cardinality columns (names, dates),
    just report the cardinality.  This gives Gemini the TRUE distribution
    of the visible data, so it cannot wrongly claim "all are X" based on
    a sample-window slice.
    """
    if not rows or not columns:
        return '(no data)'
    lines = []
    for ci, cname in enumerate(columns):
        vals = [r[ci] for r in rows if r[ci] is not None]
        if not vals:
            lines.append(f'  {cname}: all NULL ({len(rows)} rows)')
            continue
        unique = {}
        for v in vals:
            key = str(v) if not isinstance(v, str) else v
            unique[key] = unique.get(key, 0) + 1
            if len(unique) > 21:
                break
        if len(unique) <= 20:
            # Low cardinality — full breakdown
            sorted_vals = sorted(unique.items(), key=lambda kv: -kv[1])
            shown = ', '.join(f'"{k}"={v}' for k, v in sorted_vals)
            lines.append(f'  {cname} ({len(unique)} distinct): {shown}')
        else:
            lines.append(f'  {cname}: {len(unique)}+ distinct values (high cardinality — sample-only)')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Chart Detector — decide if response should include a chart
# ---------------------------------------------------------------------------

CHART_INTENTS = {
    'portfolio_summary', 'fund_performance', 'kpi_analysis',
    'valuation_analysis', 'exit_analysis', 'lp_information',
    'accounting_query', 'company_financials', 'risk_analysis',
    'fund_info',
}


def _suggest_chart(
    intent: str, columns: List[str], rows: List[tuple], query: str,
    agg_columns: Optional[List[str]] = None,
    agg_rows: Optional[List[tuple]] = None,
    grouping_dimension: Optional[str] = None,
) -> Optional[Dict]:
    """Decide if a chart would help and return chart config if so.

    The aggregate result (already grouped & summed across the full scoped
    dataset) is preferred whenever it has 2+ groups — that is the chart
    that actually answers "show me the breakdown" without the sample-bias
    that plagued the prior chart logic. If the aggregate is scalar (1 row
    of totals) or absent, we fall back to the detail rows.
    """
    if intent not in CHART_INTENTS:
        return None

    # Choose source: prefer aggregate when it has >= 2 groups (i.e. a real
    # breakdown). Otherwise fall back to detail rows. This makes the chart
    # mirror the same TRUTH the renderer used, not a truncated slice.
    if agg_rows and agg_columns and len(agg_rows) >= 2:
        src_cols, src_rows = agg_columns, agg_rows
        from_aggregate = True
    elif rows and len(rows) >= 2:
        src_cols, src_rows = columns, rows
        from_aggregate = False
    else:
        return None

    label_col = None
    num_cols = []
    for i, col in enumerate(src_cols):
        sample_vals = [r[i] for r in src_rows[:5] if r[i] is not None]
        if sample_vals and all(isinstance(v, (int, float)) or _is_numeric(v) for v in sample_vals):
            num_cols.append(i)
        elif label_col is None and sample_vals:
            label_col = i

    if label_col is None or not num_cols:
        return None

    # Chart breadth: aggregate is already condensed → show all groups.
    # Detail can be long → cap visual width.
    visual_cap = len(src_rows) if from_aggregate else min(len(src_rows), 20)

    labels = [str(r[label_col] or '')[:30] for r in src_rows[:visual_cap]]

    datasets = []
    colors = ['#00d4ff', '#7c3aed', '#10b981', '#f59e0b', '#ef4444', '#3b82f6']
    for idx, ci in enumerate(num_cols[:3]):
        col_name = src_cols[ci]
        values = [
            float(r[ci]) if r[ci] is not None and _is_numeric(r[ci]) else 0
            for r in src_rows[:visual_cap]
        ]
        datasets.append({
            'label': col_name.replace('_', ' ').title(),
            'data': values,
            'color': colors[idx % len(colors)],
        })

    chart_type = 'bar'
    q_lower = query.lower()
    if any(kw in q_lower for kw in ['trend', 'over time', 'history', 'monthly', 'quarterly']):
        chart_type = 'line'
    elif any(kw in q_lower for kw in ['distribution', 'breakdown', 'split', 'mix', 'composition', 'proportion']):
        chart_type = 'doughnut' if len(src_rows) <= 8 else 'bar'
    elif any(kw in q_lower for kw in ['compare', 'comparison', 'vs', 'versus']):
        chart_type = 'bar'
    elif len(src_rows) <= 6 and len(num_cols) == 1:
        chart_type = 'doughnut'

    title_base = (grouping_dimension or src_cols[label_col]).replace('_', ' ').title()
    return {
        'type': chart_type,
        'labels': labels,
        'datasets': datasets,
        'title': f'{title_base} Breakdown' if from_aggregate else f'{title_base} Analysis',
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

def render_response(
    query: str, intent: str, columns: List[str], rows: List[tuple],
    context: Dict, meta: Optional[Dict] = None,
    agg_columns: Optional[List[str]] = None,
    agg_rows: Optional[List[tuple]] = None,
    agg_meta: Optional[Dict] = None,
    grouping_dimension: Optional[str] = None,
) -> str:
    """Generate the user-facing prose answer.

    Two result sets are supported:
      • aggregate (agg_columns/agg_rows) — authoritative TRUTH from a
        GROUP BY / aggregate query that scanned the full scoped dataset.
        Used for every count / distribution / "how many" claim.
      • detail (columns/rows) — the filtered row list the user wants to
        inspect (e.g. only the healthcare companies). Used to name and
        quote individual entities.

    Either may be empty; if both are empty we fall back. The renderer
    forwards `truncated` flags so Gemini never overclaims on a partial
    sample.
    """
    if not rows and not agg_rows:
        return _fallback_response(intent, context, query)

    meta = meta or {}
    agg_meta = agg_meta or {}
    detail_truncated = bool(meta.get('truncated'))
    agg_truncated = bool(agg_meta.get('truncated'))

    try:
        from api.gemini_service import generate_content

        # ── TRUE TOTALS block (aggregate) ────────────────────────
        if agg_columns and agg_rows:
            truth_str = ' | '.join(agg_columns) + '\n'
            truth_str += '\n'.join(' | '.join(str(v) for v in row) for row in agg_rows)
            truth_count_note = (
                f'Aggregate scanned the FULL scoped dataset and produced {len(agg_rows)} group(s)'
                + (' (capped — there are more groups than shown).' if agg_truncated else '.')
            )
            if grouping_dimension:
                truth_count_note += f' Grouping dimension: **{grouping_dimension}**.'
        else:
            truth_str = '(no aggregate query was applicable for this question)'
            truth_count_note = ''

        # ── DETAIL SAMPLE block ──────────────────────────────────
        if columns and rows:
            sample_preview_limit = 25
            sample_str = ' | '.join(columns) + '\n'
            sample_str += '\n'.join(' | '.join(str(v) for v in row) for row in rows[:sample_preview_limit])
            profile_str = _build_dataset_profile(columns, rows)
            if detail_truncated:
                detail_visibility = (
                    f'DETAIL VISIBILITY WARNING: the detail query returned MORE rows '
                    f'than the {len(rows)}-row memory safety cap. The visible rows are '
                    f'a partial slice — for any total / count claim, USE TRUE TOTALS only.'
                )
            else:
                detail_visibility = (
                    f'DETAIL is COMPLETE: all {len(rows)} matching detail rows are shown.'
                )
        else:
            sample_str = '(no detail rows — the question was answered purely by the aggregate)'
            profile_str = '(n/a)'
            detail_visibility = ''

        prompt = f"""You are a senior financial analyst AI assistant for TrackFundAI, a portfolio management platform for Indian AIFs (Alternative Investment Funds).

You are speaking with {context.get('user_name', 'the user')} (address them as "{context.get('user_first_name', 'there')}" when appropriate — never as a company name, organization, or fund). Treat them as the human fund professional asking the question.

User asked: "{query}"
Fund context: {context.get('fund_name') or 'All funds'}

═══════════════════════════════════════════════════════════════════
TRUE TOTALS (AUTHORITATIVE — scanned the full scoped dataset)
═══════════════════════════════════════════════════════════════════
{truth_count_note}

{truth_str}

═══════════════════════════════════════════════════════════════════
DETAIL SAMPLE (the rows matching the user's filter, for naming / quoting)
═══════════════════════════════════════════════════════════════════
{detail_visibility}

{sample_str}

DETAIL PROFILE (distinct-value frequencies across the {len(rows)} detail rows shown):
{profile_str}

═══════════════════════════════════════════════════════════════════
ANSWERING RULES — read carefully. The prior renderer hallucinated counts because it conflated SAMPLE with TRUTH.

A. AUTHORITY ORDER (this is the single most important rule)
   1. For ANY "how many X", "count of Y", "breakdown by Z", "all the W",
      "what percentage", "what sectors / types / categories exist" claim
      → use TRUE TOTALS. ONLY TRUE TOTALS.
   2. For naming specific entities or quoting per-row values
      ("which healthcare companies", "what was Acme's FV", "list the LPs")
      → use DETAIL SAMPLE.
   3. NEVER derive a total / count / "all are" claim from DETAIL SAMPLE
      or DETAIL PROFILE. Those describe only the visible filtered slice.

B. NO HALLUCINATION
   • Do not invent rows, names, or numbers that are absent from both blocks.
   • If the user asks for a category that does NOT appear in TRUE TOTALS,
     say it is not present in the data — then list what IS present using
     TRUE TOTALS rows.
   • If a value is NULL across the visible rows, say so explicitly.

C. FORMATTING
   • Lead with the direct answer — no filler, no restating the question.
   • Bold (**value**) the key numbers.
   • INR amounts: Rs.XX.XX Cr or Rs.XX.XX L.
   • Markdown bullets for lists.
   • Concise: 3–6 sentences for simple queries, more for analysis.
   • Address the user as "{context.get('user_first_name', 'there')}" — never as a company / firm / fund.
   • Never say "based on the data", "according to the query", "the dataset shows".
"""

        response = generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.warning(f'Response render error: {e}')
        return _table_response(columns, rows) if rows else _table_response(agg_columns, agg_rows)


def _fallback_response(intent: str, context: Dict, query: str = '') -> str:
    fund_hint = f" for **{context['fund_name']}**" if context.get('fund_name') else ''
    fallbacks = {
        'fund_info': f"No fund metadata found{fund_hint}. This data comes from the fund master record. Please ensure the fund has been imported with SEBI registration, corpus, and entity details.",
        'portfolio_summary': f'No portfolio company data found{fund_hint}. Please ensure fund data has been imported from the Excel file.',
        'fund_performance': f'No fund performance data available{fund_hint}. Performance metrics (IRR, MOIC, TVPI) require NAV records and LP capital accounts to be imported.',
        'company_financials': f"No financial data found for {context.get('company_name', 'the specified company')}. This company may not have MIS/P&L data uploaded yet.",
        'compliance_status': f'No compliance records found{fund_hint}. Compliance filings (QAR, AAR, CTR) need to be configured for SEBI reporting.',
        'lp_information': f'No LP/investor data found{fund_hint}. Please ensure the Investor/Commitment sheets have been imported from the fund Excel.',
        'risk_analysis': f'No risk or anomaly data found{fund_hint}. Risk scores are computed after MIS data is imported.',
        'kpi_analysis': f'No KPI data found{fund_hint}. KPIs are populated from the Portfolio KPIs sheet in your fund Excel.',
        'exit_analysis': f'No exit event data found{fund_hint}.',
        'valuation_analysis': f'No valuation records found{fund_hint}. Valuations are imported from the Valuations (IPEV) sheet.',
        'deal_pipeline': f'No deals found in the IC pipeline{fund_hint}.',
        'accounting_query': f'No accounting records found{fund_hint}. Please ensure NAV, management fees, and fund ledger data have been imported.',
        'import_data': 'No import records found. Upload an Excel file via the Data Upload page to begin.',
        'market_research': "I can help with market research related to your portfolio sectors. Please specify the sector or company you'd like to analyze.",
        'general_query': "I couldn't find specific data for that query. Try asking about portfolio companies, fund performance, NAV, compliance status, or LP information.",
        'out_of_scope': GUARDRAIL_RESPONSE,
    }
    return fallbacks.get(intent, f"I couldn't find data to answer that question{fund_hint}. Please try rephrasing with specific company, fund, or metric names.")


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
        from api.gemini_service import generate_content

        prompt = f"""You are a senior financial analyst AI assistant for TrackFundAI, a portfolio management platform for Indian AIFs.
You are speaking with {context.get('user_name', 'a fund manager')} (address them personally as "{context.get('user_first_name', 'there')}" when appropriate — never as a company, firm, or organization). They are asking a general finance question.
{f"Current fund context: {context['fund_name']}" if context.get('fund_name') else ''}

User query: "{query}"

Answer this question with financial expertise. You may:
1. Explain financial concepts (IRR, MOIC, TVPI, DPI, NAV, SEBI regulations, etc.)
2. Discuss market trends, sector analysis, and investment strategies relevant to Indian markets
3. Provide analysis frameworks and methodologies (DCF, IPEV, comparable company analysis)
4. Discuss regulatory aspects (SEBI AIF regulations, FEMA, PMLA, TDS for AIFs)
5. Compare/contrast financial metrics and what they mean for fund performance
6. Explain what data/reports TrackFundAI can generate (QAR, AAR, MIS reports, board packs)

Keep the response professional, concise (3-8 sentences), and relevant to fund management.
Use markdown formatting: **bold** for key terms, bullet points for lists.
If the question is completely unrelated to finance, politely redirect."""

        response = generate_content(prompt)
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

    def handle(self, query: str, fund=None, company=None, fund_name_override=None) -> Dict[str, Any]:
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

        # Dashboard context check — "which fund is selected?" etc.
        if _is_dashboard_context_query(query):
            response_text = _handle_dashboard_context(fund, fund_name_override)
            message_id = self._log_query(query, 'dashboard_context', response_text)
            return {
                'response': response_text,
                'intent': 'dashboard_context',
                'entity': fund.name if fund else None,
                'confidence': 1.0,
                'message_id': str(message_id) if message_id else None,
                'data': {'columns': [], 'rows': []},
                'chart': None,
            }

        # Step 1: Classify intent
        fund_name_for_classifier = fund.name if fund else fund_name_override
        intent_result = classify_intent(query, self.organization.name, fund_name_for_classifier)
        intent = intent_result.get('intent', 'general_query')
        entity = intent_result.get('entity')
        time_filter = intent_result.get('time_filter')

        # Step 2: Inject context (resolve fund/company names from entity)
        ctx = build_context(
            self.organization, fund=fund, company=company,
            intent_result=intent_result, fund_name_override=fund_name_override,
            user=self.user,
        )

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

        # Step 3: Build SQL.
        #
        # Routing decision:
        #   • Multi-row data intents (portfolio_summary, fund_performance,
        #     lp_information, valuation_analysis, kpi_analysis,
        #     exit_analysis, risk_analysis, accounting_query,
        #     company_financials, compliance_status) go through the dual-SQL
        #     path: Gemini produces an authoritative aggregate (truth) +
        #     a filtered detail query (sample). The renderer then quotes
        #     numbers from the aggregate, never from the truncated detail —
        #     fixing the entire class of "20 of 30 visible" bugs.
        #   • Bounded intents (fund_info — 1 fund; deal_pipeline,
        #     import_data — small admin sets) keep the keyword template
        #     fast path: their results are naturally small and there is no
        #     sample bias to worry about.
        DUAL_SQL_INTENTS = {
            'portfolio_summary', 'fund_performance', 'lp_information',
            'valuation_analysis', 'kpi_analysis', 'exit_analysis',
            'risk_analysis', 'accounting_query', 'company_financials',
            'compliance_status',
        }

        sql = None
        columns: List[str] = []
        rows: List[tuple] = []
        result_meta: Dict = {'truncated': False}
        agg_columns: List[str] = []
        agg_rows: List[tuple] = []
        agg_meta: Dict = {'truncated': False}
        grouping_dim: Optional[str] = None

        if intent in DUAL_SQL_INTENTS:
            # Dual-SQL path — replaces the brittle keyword templates that
            # caused the sample-bias bugs. Gemini sees the user's natural
            # language entity ("healthcare", "financial") and translates it
            # into a WHERE clause; the aggregate carries the truth counts.
            sql_pair = build_aggregate_and_detail_sql(query, intent, ctx, time_filter, entity)
            if sql_pair:
                grouping_dim = sql_pair.get('grouping_dimension')
                agg_sql = sql_pair.get('aggregate_sql')
                detail_sql = sql_pair.get('detail_sql')
                if agg_sql:
                    agg_columns, agg_rows, agg_meta = execute_query_with_meta(agg_sql)
                if detail_sql:
                    columns, rows, result_meta = execute_query_with_meta(detail_sql)
                    sql = detail_sql

            # Last-resort fallback: if dual-SQL yielded nothing at all, try
            # the single-pass builder (still no keyword templates).
            if not rows and not agg_rows:
                gemini_sql = build_sql_query(query, intent, ctx, time_filter, entity)
                if gemini_sql:
                    cols2, rows2, meta2 = execute_query_with_meta(gemini_sql)
                    if rows2:
                        columns, rows, result_meta = cols2, rows2, meta2
                        sql = gemini_sql
        else:
            # Bounded-cardinality intents — keyword templates are still safe
            # here because the natural row count is small (1 fund, a few
            # schemes, a handful of deal-pipeline rows). No sample bias.
            sql = _try_template_query(query, intent, ctx)
            if sql:
                columns, rows, result_meta = execute_query_with_meta(sql)
            if not rows:
                gemini_sql = build_sql_query(query, intent, ctx, time_filter, entity)
                if gemini_sql:
                    cols2, rows2, meta2 = execute_query_with_meta(gemini_sql)
                    if rows2:
                        columns, rows, result_meta = cols2, rows2, meta2
                        sql = gemini_sql

        # Step 4b: If still nothing and we have a fund context, broaden once.
        if not rows and not agg_rows and ctx.get('fund_id'):
            sql2 = build_sql_query(
                query + ' (HINT: previous SQL returned 0 rows — try broader joins, different tables, or remove restrictive filters)',
                intent, ctx, time_filter, entity,
            )
            if sql2 and sql2 != sql:
                cols3, rows3, meta3 = execute_query_with_meta(sql2)
                if rows3:
                    columns, rows, result_meta = cols3, rows3, meta3
                    sql = sql2

        # Step 5: Render — give renderer both truth (aggregate) and sample
        # (detail) so it never overclaims from a partial slice.
        response_text = render_response(
            query, intent, columns, rows, ctx, result_meta,
            agg_columns=agg_columns, agg_rows=agg_rows, agg_meta=agg_meta,
            grouping_dimension=grouping_dim,
        )

        # Step 6: Chart — prefers aggregate when present so the picture
        # matches the truth instead of a biased slice.
        chart = _suggest_chart(
            intent, columns, rows, query,
            agg_columns=agg_columns, agg_rows=agg_rows,
            grouping_dimension=grouping_dim,
        )

        # Log to DB
        message_id = self._log_query(query, intent, response_text)

        # Frontend table preview: prefer detail rows; fall back to aggregate
        # (e.g. when the user only asked for a breakdown).
        preview_cols = columns if rows else agg_columns
        preview_rows = rows if rows else agg_rows

        return {
            'response': response_text,
            'intent': intent,
            'entity': entity,
            'confidence': intent_result.get('confidence', 0),
            'message_id': str(message_id) if message_id else None,
            'data': {
                'columns': preview_cols,
                'rows': [list(r) for r in preview_rows[:20]],
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
