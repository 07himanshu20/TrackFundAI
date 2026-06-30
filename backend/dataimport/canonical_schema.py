"""
Canonical field definitions for each data domain.

Gemini uses these to map arbitrary Excel column headers to known field names.
Each domain corresponds to a section/sheet in a fund Excel file.
Fields include a description to help Gemini understand the semantic meaning.
"""

# ---------------------------------------------------------------------------
# Sheet domain classification — what types of data sheets exist
# ---------------------------------------------------------------------------

SHEET_DOMAINS = {
    'organization_users': 'Organization master data, key entities (manager, trustee, custodian), and GP user accounts',
    'fund_scheme_master': 'Fund master record (name, SEBI registration, category, structure) and scheme details (vintage, close dates, fees, carry)',
    'investors_aml': (
        'LP (Limited Partner) / Investor master records — names, types, KYC status, AML due diligence, '
        'bank accounts, SEBI compliance flags, commitment amounts, drawdown amounts, and distribution amounts '
        'PAID TO the investors. CRITICAL: A "Distributions" column here means money RETURNED TO the LP — '
        'it does NOT make this sheet an exits_distributions sheet. The entities on this sheet are INVESTORS '
        '(sovereign wealth funds, pension funds, DFIs, family offices, insurance companies, corporates) who '
        'have committed capital to the fund. They are NOT portfolio companies and NOT exit targets.'
    ),
    'commitments': 'LP commitments to schemes — amounts, close types, dates',
    'capital_calls': 'Capital call events and per-LP line items with payment tracking',
    'portfolio_investments': (
        'Portfolio companies (investee companies) and their investments — instrument type, ownership %, '
        'cost/invested amount, fair value, stage, sector. These are companies the fund HAS INVESTED IN. '
        'CRITICAL: "TEMPORARY INVESTMENTS" sub-sections (liquid mutual funds, overnight funds, money market '
        'instruments) are NOT portfolio companies — they are treasury/cash management instruments.'
    ),
    'valuations_kpis': 'Investment valuations (DCF, comparables) and portfolio company KPIs (MRR, burn rate, etc.)',
    'nav_accounting': 'NAV records, chart of accounts, double-entry ledger, carried interest, management fees',
    'exits_distributions': (
        'Exit events FROM portfolio companies (IPO, M&A, secondary sale, buyback, write-off) and fund-level '
        'distribution schedules to LPs. CRITICAL: The "Company" column here contains names of portfolio '
        'companies the fund has EXITED FROM — these are investee companies, NOT investors/LPs. '
        'This sheet must have exit-specific columns like Exit Date, Exit Type/Route, Proceeds, MOIC. '
        'A sheet that lists LP/investor names with a "Distributions" column is investors_aml, NOT this domain.'
    ),
    'compliance': 'SEBI reports (QAR/AAR), compliance calendar, compliance test reports, SEBI circulars, PPM amendments',
    'portfolio_hierarchy': 'Portfolio hierarchy tree: fund > sector > segment > company nodes with cross-fund mapping',
    'financials_pl_bva': 'Company-level P&L (Revenue, COGS, EBITDA, PAT), Balance Sheet, Cash Flow, and Budget vs Actual — monthly or period-based financial statements for portfolio companies',
    'quoted_unquoted': 'Quoted & Unquoted share classification, IPEV levels, share type (listed vs unlisted), listing exchange details for portfolio companies',
    'fees_register': 'Management fee schedule, fee register — periodic fee calculations (quarterly/annual), fee basis amounts, GST on fees',
    'burn_runway': 'Company-level burn rate, cash balance, runway months, SaaS metrics (MRR, ARR, churn, NRR, CAC, LTV) — operational KPIs for portfolio companies',
    'fund_pl_bs': 'Fund-level P&L and Balance Sheet — consolidated financial statements for the fund entity itself (not individual portfolio companies)',
    'lp_capital_accounts': 'LP Capital Account statements — per-investor capital account balances, contributions, distributions, carried interest allocations',
    'nav_calculation': (
        'NAV Calculation / NAV Computation sheet — step-by-step NAV build-up showing '
        'Opening NAV, investments at cost, fair value adjustment, unrealised gains, '
        'realised gains, management fees, operating expenses, Closing NAV, total units '
        'outstanding, Closing NAV per Unit. This is a KEY-VALUE or line-item sheet with '
        'labels in column A and values in column B (not a time-series table). '
        'CRITICAL: This is DIFFERENT from nav_accounting — nav_accounting stores '
        'period-wise NAV time-series (one row per month/quarter). nav_calculation is '
        'the single-period computational worksheet that derives the NAV figure.'
    ),
    'waterfall_carry': (
        'Carried Interest Waterfall / Distribution Waterfall — shows the GP/LP economics: '
        'total capital called, preferred return / hurdle amount, catch-up, carried interest '
        'provision, GP carry amount, LP share, distribution splits. May also contain '
        'performance fee calculations, clawback provisions, and waterfall tiers. '
        'Sheet names often include "Waterfall", "Carry", "Carried Interest", "Performance Fee", '
        '"GP Economics", "Distribution Waterfall". CRITICAL: This is DIFFERENT from '
        'exits_distributions (which tracks individual company exit events) and from '
        'nav_accounting (which tracks periodic NAV values).'
    ),
}

# ---------------------------------------------------------------------------
# Section sub-domain classification — types of data sections within sheets
# ---------------------------------------------------------------------------

SECTION_SUBDOMAINS = {
    'portfolio_companies': (
        'Company master / identity data — portfolio company name, sector, sub-sector, '
        'stage, city, country, website, founder names, CIN, PAN, incorporation date. '
        'These rows describe the IDENTITY of companies the fund has invested in. '
        'They do NOT contain financial investment data (cost, fair value, ownership %). '
        'Example section headers (ANY language/format): PORTFOLIO COMPANIES, '
        'INVESTEE COMPANIES, COMPANIES, COMPANY MASTER, COMPANY DETAILS, '
        'PORTFOLIO COMPANY LIST, FUND HOLDINGS, COMPANY REGISTER'
    ),
    'investments': (
        'Investment financial data — instrument type (equity, CCD, CCPS, SAFE), '
        'cost/invested amount, fair value, ownership %, IRR, MOIC, investment date, '
        'investment status. These rows describe the FINANCIAL POSITION of investments, '
        'not company identity. When company identity + investment data appear in the SAME '
        'rows (combined format), classify as investments. '
        'Example headers: INVESTMENTS, INVESTMENT DETAILS, INVESTMENT REGISTER, '
        'PORTFOLIO INVESTMENTS, DEPLOYED CAPITAL, FUND DEPLOYMENT, INVESTMENT BOOK'
    ),
    'investment_tranches': (
        'Tranche / round / drawdown details — tranche number, tranche amount, tranche date, '
        'shares acquired, price per share, pre-money valuation, post-money valuation, '
        'round name (Series A, B, etc.). One row per tranche/round per company. '
        'Example headers: INVESTMENT TRANCHES, TRANCHES, FUNDING ROUNDS, '
        'DRAWDOWN TRANCHES, ROUND DETAILS, TRANCHE REGISTER, DEAL HISTORY'
    ),
    'temporary_investments': (
        'Liquid mutual funds, overnight funds, money market instruments, CBLO, '
        'treasury bills, fixed deposits, commercial paper used for cash management. '
        'These are NOT portfolio company investments — they are treasury instruments. '
        'CRITICAL: These rows must be SKIPPED by portfolio import logic. '
        'Example headers: TEMPORARY INVESTMENTS, TREASURY INVESTMENTS, '
        'LIQUID INVESTMENTS, CASH INSTRUMENTS, MONEY MARKET, SHORT TERM INVESTMENTS, '
        'LIQUID FUND HOLDINGS, OVERNIGHT FUNDS'
    ),
    'capital_call_headers': (
        'Capital call event records — call number, call date, call percentage of commitment, '
        'total call amount, payment due date, purpose (investment, fees, expenses), status. '
        'One row per capital call event. '
        'Example headers: CAPITAL CALLS, DRAWDOWNS, CALL SCHEDULE, '
        'CAPITAL CALL REGISTER, DRAW DOWN SCHEDULE, CAPITAL DRAWDOWNS, CALL NOTICES'
    ),
    'capital_call_line_items': (
        'Per-LP capital call amounts — investor/LP name, called amount for this LP, '
        'payment status (paid/pending), amount received, cumulative called %, UTR number. '
        'One row per LP per call. '
        'Example headers: CAPITAL CALL LINE ITEMS, CALL LINE ITEMS, LP DRAWDOWNS, '
        'INVESTOR DRAWDOWNS, LP-WISE CAPITAL CALLS, INVESTOR CALL DETAILS'
    ),
    'exit_events': (
        'Exit events from portfolio companies — company name, exit type '
        '(IPO, M&A, secondary sale, buyback, write-off), exit date, exit valuation, '
        'proceeds, cost basis, realized gain/loss, MOIC, IRR. '
        'Example headers: EXIT EVENTS, EXITS, REALIZATIONS, REALIZED INVESTMENTS, '
        'PORTFOLIO EXITS, EXIT REGISTER, DIVESTMENTS, REALISATIONS'
    ),
    'distributions': (
        'Fund-level distributions to LPs — distribution number, date, type '
        '(return of capital, STCG, LTCG, dividend, carry), total gross amount, '
        'TDS, net amount. One row per distribution event. '
        'Example headers: DISTRIBUTIONS, DISTRIBUTION SCHEDULE, LP DISTRIBUTIONS, '
        'DISTRIBUTION REGISTER, PAYOUT SCHEDULE, PAYOUTS, DISTRIBUTION EVENTS'
    ),
    'nav_records': (
        'NAV TIME-SERIES — one row PER PERIOD (month/quarter). Required column '
        'shape: a Date/Period column PLUS one or more amount columns (Total '
        'NAV, NAV per unit, units outstanding, FV, cash). The presence of a '
        'date column in the row data is mandatory; pick this subdomain ONLY '
        'when rows are time-indexed. '
        'Example section headers: NAV RECORDS, NAV HISTORY, MONTHLY NAV, '
        'QUARTERLY NAV, PERIODIC NAV, NAV TIME SERIES, NAV HISTORY TABLE'
    ),
    'nav_breakdown': (
        'NAV COMPONENT DECOMPOSITION — current-period NAV broken down by '
        'component. Key-value layout: rows are NAV components (Total Fair '
        'Value of Portfolio, Cash & Equivalents, Realised Gains, Management '
        'Fee Payable, Performance Fee Payable, Borrowings, Other Liabilities, '
        'etc.) and the value column carries the amount. Final row typically '
        'sums to Total NAV. NO date column. NOT a time-series. '
        'Example section headers: FUND NAV (CURRENT PERIOD), NAV BREAKDOWN, '
        'NAV COMPONENTS, NAV BUILD-UP, NAV CALCULATION, NAV DECOMPOSITION'
    ),
    'nav_per_unit': (
        'NAV PER UNIT calculation — key-value layout showing total fund NAV, '
        'total units issued, NAV per unit, face value per unit, premium to '
        'face value, NAV as % of committed capital. Single point in time. '
        'NO date column. NOT a time-series. '
        'Example section headers: NAV PER UNIT, UNIT NAV, NAV/UNIT, '
        'PER UNIT VALUE, UNIT VALUATION'
    ),
    'fund_performance_breakdown': (
        'FUND PERFORMANCE METRICS — key-value layout listing fund-level '
        'performance multiples and returns (MOIC, TVPI, DPI, RVPI, Gross IRR, '
        'Net IRR, Total Called Capital, Total Distributions, Total FV). '
        'Rows are metric names, column carries the value. NO date column. '
        'Use this for sheets like MOIC_TVPI_DPI that decompose fund-level KPIs. '
        'Example section headers: FUND PERFORMANCE, FUND-LEVEL MULTIPLES, '
        'PERFORMANCE METRICS, FUND KPIS, FUND RETURNS, FUND MULTIPLES'
    ),
    'waterfall_breakdown': (
        'WATERFALL / CARRY computation — key-value layout showing waterfall '
        'parameters (committed capital, called capital, preferred return / '
        'hurdle, carry %, LP share, GP share, cumulative distributed, carry '
        'amount, clawback provision). Rows are waterfall components. '
        'Example section headers: WATERFALL, CARRY COMPUTATION, EUROPEAN '
        'WATERFALL, AMERICAN WATERFALL, DEAL-BY-DEAL WATERFALL'
    ),
    'schemes': (
        'Scheme details within a fund — scheme name, vintage year, first/final close dates, '
        'scheme size, hurdle rate %, carry %, carry type, tenure, management fee %, fee basis. '
        'Example headers: SCHEMES, SCHEME DETAILS, FUND SCHEMES, SCHEME MASTER, '
        'SCHEME INFORMATION, SUB-FUND DETAILS'
    ),
    'fund_master': (
        'Fund identity and metadata — fund name, SEBI registration number, SEBI category code, '
        'structure (trust/company/LLP), PAN, GSTIN, inception date, corpus target, base currency. '
        'Example headers: FUND MASTER DATA, FUND DETAILS, FUND INFORMATION, '
        'FUND MASTER, FUND OVERVIEW, FUND PROFILE'
    ),
    'entities': (
        'Key entities associated with the fund — entity type (manager, trustee, custodian, '
        'statutory auditor, legal counsel, sponsor, registrar, valuer), entity name, PAN, GSTIN, '
        'SEBI registration, contact person, email, address. '
        'Example headers: KEY ENTITIES, ENTITIES, SERVICE PROVIDERS, KEY PERSONNEL, '
        'FUND ENTITIES, RELATED PARTIES, FUND SERVICE PROVIDERS'
    ),
    'valuations': (
        'Valuation data — company name, valuation date, methodology (DCF, comparables, '
        'recent transaction, net assets, cost), fair value, enterprise value, cost basis, '
        'unrealized gain/loss, valuer name. '
        'Example headers: VALUATIONS, PORTFOLIO VALUATIONS, VALUATION DETAILS, '
        'FAIR VALUE ASSESSMENT, VALUATION REGISTER, INVESTMENT VALUATIONS'
    ),
}

# ---------------------------------------------------------------------------
# Canonical fields per domain
# Each entry: {field_name: description}
# ---------------------------------------------------------------------------

ORGANIZATION_USERS_FIELDS = {
    'organization_name': 'Legal name of the fund house / GP organization',
    'organization_slug': 'URL-safe short name (lowercase, hyphens)',
    'entity_type': 'Type of entity: manager, trustee, sponsor, custodian, statutory_auditor, legal_counsel, registrar, valuer',
    'entity_name': 'Legal name of the entity',
    'entity_pan': 'PAN of the entity',
    'entity_gstin': 'GSTIN of the entity',
    'entity_sebi_registration': 'SEBI registration number',
    'entity_contact_person': 'Primary contact person name',
    'entity_email': 'Contact email',
    'entity_phone': 'Contact phone number',
    'entity_address': 'Full address',
    'entity_city': 'City',
    'entity_state': 'State',
    'entity_country': 'Country (default: India)',
    'user_username': 'Login username',
    'user_first_name': 'First name of the user',
    'user_last_name': 'Last name of the user',
    'user_email': 'User email address',
    'user_role': 'User role: gp_admin, gp_user, compliance_officer, fund_accountant, lp_user, founder_user, external_auditor',
    'user_phone': 'User phone number',
    'fund_access_fund_name': 'Name of the fund this user can access',
    'fund_access_level': 'Access level: read, write, admin',
}

FUND_SCHEME_MASTER_FIELDS = {
    'fund_name': 'Name of the AIF fund',
    'sebi_registration_number': 'SEBI AIF registration number',
    'sebi_category_code': 'SEBI category: CAT_I_VCF, CAT_II, CAT_III_LVF, etc.',
    'structure_type': 'Fund structure: trust, company, or llp',
    'fund_pan': 'PAN of the fund',
    'fund_gstin': 'GSTIN of the fund',
    'inception_date': 'Date the fund was established',
    'corpus_target': 'Target fund corpus amount',
    'base_currency': 'Base currency (default INR)',
    'is_gift_city': 'Whether this is a GIFT City / IFSC offshore AIF',
    'fund_status': 'Fund status: active, closed, winding_up',
    'scheme_name': 'Name of the scheme under the fund',
    'vintage_year': 'Vintage year of the scheme',
    'first_close_date': 'Date of first close',
    'final_close_date': 'Date of final close',
    'scheme_size': 'Target scheme size in base currency',
    'tenure_years': 'Scheme tenure in years',
    'hurdle_rate_pct': 'Hurdle rate / preferred return percentage',
    'carry_pct': 'Carried interest percentage (e.g., 20)',
    'carry_type': 'Carry type: european (whole fund) or american (deal-by-deal)',
    'gp_holdback_pct': (
        'GP carry escrow holdback as % of distributed carry. Match labels like '
        '"Clawback Provision", "GP Holdback %", "Escrow Holdback %", "20% holdback". '
        'Industry default 20% — extract only when LPA explicitly states a different value '
        'or omit if not stated.'
    ),
    'management_fee_basis': 'Fee basis: committed, called, or nav',
    'management_fee_pct': 'Annual management fee percentage',
    'sponsor_commitment_pct': 'Sponsor commitment as % of scheme size',
    'scheme_status': 'Scheme status: fundraising, investing, harvesting, dissolved',
}

INVESTORS_AML_FIELDS = {
    'investor_name': 'Legal name of the LP / investor',
    'investor_type': 'Type: individual, huf, company, trust, fpi, nri, insurance, pension, sovereign, family_office, etc.',
    'contact_person': 'Primary contact person name',
    'email': 'Investor email address',
    'phone': 'Phone number',
    'address': 'Full address',
    'city': 'City',
    'state': 'State / Province',
    'country': 'Country (default: India)',
    'pan': 'PAN number (mandatory for Indian investors)',
    'aadhaar_last_4': 'Last 4 digits of Aadhaar',
    'ckyc_number': 'CERSAI KYC number',
    'kyc_status': 'KYC status: pending, in_progress, completed, expired, rejected',
    'kyc_completed_date': 'Date KYC was completed',
    'kyc_expiry_date': 'Date KYC expires',
    'is_accredited_investor': 'Whether investor is SEBI-accredited',
    'accreditation_date': 'Date of accreditation',
    'is_land_border_country': 'SEBI: investor from land-border country (China, Pakistan, etc.)',
    'land_border_country_name': 'Name of the land-border country',
    'is_politically_exposed': 'PEP (Politically Exposed Person) flag',
    'fatca_status': 'FATCA status: not_applicable, compliant, pending, non_compliant',
    'bank_name': 'Investor bank name',
    'account_number': 'Bank account number',
    'ifsc_code': 'IFSC code for Indian banks',
    'swift_code': 'SWIFT/BIC code for international transfers',
    'account_type': 'Bank account type: savings, current, nre, nro, fcnr',
    # AML fields
    'aml_risk_rating': 'AML risk rating: low, normal, high, very_high',
    'beneficial_owner_identified': 'Whether beneficial owner (UBO) has been identified',
    'beneficial_owner_name': 'Name of the ultimate beneficial owner',
    'is_land_border_country_investor': 'SEBI AML: land-border country investor flag',
    'exceeds_50pct_threshold': 'SEBI AML: >=50% corpus from land-border investors',
    'str_filed': 'Whether a Suspicious Transaction Report was filed',
    'str_reference': 'STR reference number',
    'risk_assessment_date': 'Date of last risk assessment',
    'risk_notes': 'AML risk assessment notes',
}

COMMITMENTS_FIELDS = {
    'investor_name': 'Name of the LP making the commitment',
    'scheme_name': 'Name of the scheme being committed to',
    'commitment_amount': 'Total commitment amount',
    'commitment_date': 'Date of the commitment',
    'close_type': 'Close type: first_close, subsequent_close, final_close',
    'units_allocated': 'Units allocated to this LP',
    'side_letter_exists': 'Whether a side letter exists for this LP',
    'commitment_status': 'Status: active, defaulted, transferred, cancelled',
}

CAPITAL_CALLS_FIELDS = {
    'scheme_name': 'Scheme issuing the capital call',
    'call_number': 'Sequential call number (1, 2, 3...)',
    'call_date': 'Date of the capital call',
    'payment_due_date': 'Date payment is due',
    'call_percentage': 'Percentage of commitment being called',
    'total_call_amount': 'Total amount being called across all LPs',
    'purpose': 'Purpose of the call (investment, fees, expenses)',
    'call_status': 'Status: draft, approved, sent, paid, defaulted',
    # Line item fields
    'investor_name': 'LP name for line item',
    'called_amount': 'Amount called from this LP',
    'cumulative_called_pct': 'Cumulative % of commitment called to date',
    'payment_status': 'Payment status: pending, paid, partial, defaulted',
    'amount_received': 'Amount received from this LP',
    'payment_date': 'Date payment was received',
    'utr_number': 'Unique Transaction Reference number',
}

PORTFOLIO_INVESTMENTS_FIELDS = {
    'company_name': 'Name of the portfolio company',
    'company_cin': 'CIN (Corporate Identity Number)',
    'company_pan': 'PAN of the company',
    'sector': 'Industry sector',
    'sub_sector': 'Sub-sector / vertical',
    'incorporation_date': 'Date of incorporation',
    'headquarters_city': 'City of headquarters',
    'headquarters_country': 'Country of headquarters',
    'website': 'Company website URL',
    'founder_names': 'Founder / promoter names',
    'co_investors': 'List of other VC / PE / strategic firms invested in the same company (any naming: "Co-Investors", "Other Investors", "Syndicate", "Other LPs in Round", "Investor Cap Table" — return as JSON list of strings)',
    'scheme_name': 'Scheme making the investment',
    'instrument_type': 'Instrument: equity, ccps, ccd, ncd, safe, convertible_note, term_loan',
    'ownership_pct': 'Ownership percentage',
    'total_invested': 'Total amount invested',
    'investment_date': 'Date of initial investment',
    'currency': 'Investment currency',
    'investment_status': 'Status: active, partially_exited, fully_exited, written_off',
    'board_seat': 'Whether the fund has a board seat',
    'is_lead_investor': 'Whether the fund is lead investor',
    # Tranche fields
    'tranche_number': 'Tranche / drawdown number',
    'tranche_amount': 'Amount of this tranche',
    'tranche_date': 'Date of this tranche',
    'shares_acquired': 'Shares acquired in this tranche',
    'price_per_share': 'Price per share',
    'pre_money_valuation': 'Pre-money valuation',
    'post_money_valuation': 'Post-money valuation',
    'round_name': 'Funding round name (Series A, B, etc.)',
    'stage': 'Current investment stage / funding round (Seed, Series A, Series B, Series C, Bridge, Growth Round, Pre-IPO)',
    'irr_pct': 'Gross IRR % for this investment — may appear as IRR%(Gross), Gross IRR, IRR%, IRR — if stored as decimal (0.45) multiply by 100 to get percentage (45)',
    'moic': 'Multiple on Invested Capital for THIS deal (e.g. 1.93x → emit 1.93). Primary: fair_value_of_holding / total_invested. Fallback when FV is unknown but IRR is known: (1 + irr_pct/100) ^ years_held. See Rule 24.',
    'is_quoted': 'Whether the company is publicly listed on a stock exchange. Look for: Listed/Unlisted, Quoted/Unquoted, Listing Status, Public/Private. True if Listed or Quoted.',
    'listing_exchange': 'Stock exchange where shares are listed: NSE, BSE, NYSE, NASDAQ, LSE, SGX, etc. Blank for unlisted/private companies.',
}

VALUATIONS_KPIS_FIELDS = {
    'company_name': 'Portfolio company name',
    'valuation_date': 'Date of valuation',
    'methodology': 'Valuation method: dcf, comparables, recent_transaction, net_assets, cost',
    'fair_value': 'Fair value of the investment',
    'fair_value_of_holding': 'FMV of fund stake',
    'enterprise_value': 'Enterprise value of the company',
    'cost_basis': 'Original cost basis (capital invested in THIS specific investment). MANDATORY on every valuations[] row — see Rule 26. cost_basis is the discriminator that lets the persister tell INV001 from INV002 when the same company has multiple investments.',
    'investment_ref': 'Source investment id (e.g. "INV001", "INV002"). Optional but preferred when the workbook has explicit investment ids — helps the persister match valuations rows to specific Investment records.',
    'unrealized_gain_loss': 'Unrealized gain or loss',
    'multiple': 'MOIC (multiple on invested capital)',
    'discount_rate': 'Discount rate used for DCF',
    'valuer_name': 'IBBI Registered Valuer name',
    'valuer_reg_number': 'Valuer registration number',
    'valuation_status': 'Status: draft, submitted, approved, rejected',
    # KPI fields
    'kpi_name': 'KPI metric name (MRR, Burn Rate, Headcount, etc.)',
    'kpi_format': 'KPI format: number, currency, percent, ratio, boolean',
    'kpi_frequency': 'Reporting frequency: monthly, quarterly, annual',
    'kpi_period': 'Reporting period date (first day of period)',
    'kpi_value': 'KPI value',
    # Burn & Runway fields (from Portfolio Financials / Burn Rate sheets)
    'gross_burn': 'Total monthly cash outflow / gross burn rate — may appear as Gross Burn, Total Burn, Monthly Expenses, Cash Outflow, Total Outflow (in Cr or Lakhs)',
    'net_burn': 'Net monthly cash burn = outflow minus revenue — may appear as Net Burn, Net Cash Burn, Net Outflow, Monthly Net Burn',
    'cash_balance': 'Cash and equivalents at period end — may appear as Cash Balance, Cash in Bank, Cash & Equivalents, Closing Cash, Cash on Hand',
    'runway_months': 'Months of runway = cash / net burn — may appear as Runway, Cash Runway, Months of Runway, Runway (Months), Runway Left',
    # SaaS Metrics
    'mrr': 'Monthly Recurring Revenue — MRR, Monthly Revenue, Recurring Revenue (for SaaS/subscription businesses)',
    'arr': 'Annual Recurring Revenue = MRR × 12 — ARR, Annual Revenue Run Rate, Annual Recurring Revenue',
    'churn_rate': 'Monthly or annual customer/revenue churn — Churn %, Churn Rate, Revenue Churn, Customer Churn, Monthly Churn',
    'nrr': 'Net Revenue Retention / Net Dollar Retention — NRR %, NDR, Net Retention, Net Dollar Retention, Net Revenue Retention',
    'cac': 'Customer Acquisition Cost — CAC, Customer Acquisition Cost, Blended CAC, Cost to Acquire',
    'ltv': 'Customer Lifetime Value — LTV, CLV, Customer LTV, Lifetime Value, Customer Value',
    'ltv_cac_ratio': 'LTV to CAC ratio — LTV/CAC, LTV:CAC, LTV CAC Ratio, Payback Multiple',
    # Sector-specific KPIs (Consumer, NBFC, Manufacturing, Real Estate, Healthcare)
    'gmv': 'Gross Merchandise Value — GMV, GMV (Cr), GMV in Crore, GMV (Lakhs), Gross Merch Value, Total GMV, Gross Sales Value',
    'revenue': 'Revenue / Net Sales — Revenue, Rev, Net Sales, Revenue (Cr), Rev(Cr), Net Revenue, Turnover, Top Line, Total Revenue',
    'gross_margin_pct': 'Gross Margin % — Gross Margin, Gross M%, GM%, Gross Margin %, Gross Profit Margin, Gross Profit %',
    'ebitda_value': 'EBITDA — EBITDA, EBITDA (Cr), Ebitda, Operating Profit, EBITDA Margin Amount',
    'ebitda_margin_pct': 'EBITDA Margin % — EBITDA %, EBITDA Margin, EBITDA%, Ebitda%, Operating Margin',
    'orders': 'Number of Orders — Orders, Order Count, Total Orders, No. of Orders, # Orders, Transactions',
    'aov': 'Average Order Value — AOV, Avg Order Value, Average Order Value, Avg Transaction Value, Average Ticket Size',
    'returns_pct': 'Return Rate % — Returns, Return %, Return Rate, RTO %, Product Returns %, Return Rate %',
    'repeat_pct': 'Repeat Customer % — Repeat %, Repeat Rate, Repeat Customer %, Retention %, Customer Retention, Repeat Customer Rate',
    'cost_to_income': 'Cost to Income Ratio — Cost:Inc, Cost to Income, Cost/Income, CI Ratio, Cost to Income Ratio',
    'headcount': 'Employee Headcount — Headcount, Employees, Team Size, FTE, Full Time Employees, Staff Count, HC',
    'nim_pct': 'Net Interest Margin % — NIM%, NIM, Net Interest Margin, NIM (%), Interest Margin',
    'gnpa_pct': 'Gross NPA % — GNPA%, Gross NPA, GNPA, Gross Non-Performing Assets %',
    'nnpa_pct': 'Net NPA % — NNPA%, Net NPA, NNPA, Net Non-Performing Assets %',
    'roe_pct': 'Return on Equity — ROE %, ROE, Return on Equity, ROE %, Return On Equity %',
    'capacity_utilization': 'Capacity Utilization — Capacity%, Capacity Utilization, Capacity Util %, Plant Utilization, Util %',
    'export_pct': 'Export Revenue % — Export%, Export Revenue %, Export Share, Exports %, Export Contribution',
    'bed_occupancy': 'Bed Occupancy Rate — Bed Occupancy, Occupancy %, Bed Occupancy %, Hospital Occupancy',
    'arpob': 'Average Revenue Per Occupied Bed — ARPOB, ARPOB (Rs/day), Avg Rev Per Bed, Revenue Per Bed',
    'cap_rate_pct': 'Capitalization Rate — Cap Rate%, Cap Rate, Capitalization Rate, Yield %',
    'investment_cost': 'Investment Cost / Deployed Capital — Cost, Investment Cost, Deployed Capital, Capital Deployed, Total Cost',
    'fair_value_holding': 'Fair Value of Holding — FV, Fair Value, FMV, Market Value, Current Value, Portfolio Value',
    'debt_to_ebitda': 'Debt to EBITDA — D/EBITDA, Debt/EBITDA, Leverage, Debt to EBITDA, Net Debt/EBITDA',
    'aum_value': 'Assets Under Management — AUM, AUM (Rs Cr), AUM(₹Cr), Total AUM, Managed Assets',
}

NAV_ACCOUNTING_FIELDS = {
    'scheme_name': 'Scheme name for NAV record',
    'nav_date': 'Date of NAV calculation',
    'total_nav': 'Total NAV of the scheme',
    'total_units_outstanding': 'Total units outstanding',
    'nav_per_unit': 'NAV per unit',
    'investments_at_fair_value': 'Total fair value of investments',
    'cash_and_equivalents': 'Cash and bank balances',
    'receivables': 'Outstanding receivables',
    'management_fee_payable': 'Management fee liability',
    'other_liabilities': 'Other liabilities',
    'depository_type': 'Depository: cdsl or nsdl',
    'depository_reconciled': 'Whether reconciled with depository',
    # Chart of accounts
    'account_code': 'Account code (e.g., 1000, 2000)',
    'account_name': 'Account name',
    'account_type': 'Type: asset, liability, equity, income, expense',
    'parent_account_code': 'Parent account code (for hierarchy)',
    # Ledger
    'journal_entry_number': 'Journal entry number',
    'entry_date': 'Date of the journal entry',
    'entry_description': 'Description of the transaction',
    'debit_account_code': 'Account code to debit',
    'credit_account_code': 'Account code to credit',
    'amount': 'Transaction amount',
    'reference_type': 'Reference type: capital_call, investment, distribution, etc.',
    # Carried interest
    'calculation_date': 'Date of carry calculation',
    'total_distributions': 'Total distributions to date',
    'total_called_capital': 'Total capital called to date',
    'preferred_return_amount': 'Preferred return / hurdle amount',
    'carry_amount_gross': 'Gross carried interest amount',
    'carry_amount_net': 'Net carried interest after clawback',
    'carry_status': 'Status: indicative, crystallised, paid',
    # Management fees
    'fee_period_start': 'Fee period start date',
    'fee_period_end': 'Fee period end date',
    'fee_basis_amount': 'Base amount for fee calculation',
    'fee_rate': 'Annual fee rate percentage',
    'fee_amount': 'Calculated fee amount',
    'gst_amount': 'GST on management fee',
}

EXITS_DISTRIBUTIONS_FIELDS = {
    'company_name': 'Portfolio company name for the exit',
    'exit_type': 'Type: ipo, merger_acquisition, secondary_sale, buyback, write_off',
    'is_actual': 'Whether this is an actual exit (vs scenario)',
    'exit_date': 'Date of exit',
    'exit_valuation': 'Company valuation at exit',
    'proceeds': 'Gross proceeds to the fund',
    'net_exit_proceeds': 'Net proceeds after transaction costs',
    'realized_gain_loss': 'Realized gain or loss',
    'gain_loss_nature': 'SEBI: ltcg, stcg, short_term_loss, long_term_loss',
    'moic': 'Multiple on invested capital at exit',
    'irr_pct': 'Gross IRR percentage at exit',
    'buyer_name': 'Acquirer / buyer name (for M&A / secondary)',
    # Distribution fields
    'scheme_name': 'Scheme making the distribution',
    'distribution_number': 'Sequential distribution number',
    'distribution_date': 'Date of distribution',
    'distribution_type': 'Type: return_of_capital, stcg, ltcg, interest, dividend, carry',
    'total_gross_amount': 'Total gross distribution amount',
    'total_tds_amount': 'Total TDS withheld',
    'total_net_amount': 'Total net distribution after TDS',
    'gp_carry_amount': (
        'Portion of this distribution paid to the GP as carried interest. '
        'Match column labels like: "GP Carry Component", "Carried Interest Distribution", '
        '"Carry Component (Cr)", "GP Carry", "Carry to GP", "GP Share of Distribution". '
        'Distinct from total_net_amount (which is the whole event). Used downstream '
        'by Python to compute clawback and net carry. Leave null when the source '
        'workbook does not publish a per-distribution carry-component split.'
    ),
    'distribution_status': 'Status: draft, approved, distributed',
    # Line item fields
    'investor_name': 'LP name for distribution line item',
    'gross_amount': 'LP gross distribution amount',
    'tds_rate': 'TDS rate applied',
    'tds_amount': 'TDS withheld for this LP',
    'net_amount': 'Net amount payable to LP',
}

COMPLIANCE_FIELDS = {
    'fund_name': 'Fund name for the compliance record',
    'scheme_name': 'Scheme name (if scheme-level)',
    # SEBI Reports
    'report_type': 'SEBI report type: qar or aar',
    'reporting_period_start': 'Start date of reporting period',
    'reporting_period_end': 'End date of reporting period',
    'report_due_date': 'Due date for filing',
    'filing_status': 'Filing status: not_started, data_collection, in_review, filed, accepted, rejected',
    'filed_date': 'Date the report was actually filed',
    'si_portal_reference_number': 'SEBI SI Portal acknowledgement number',
    # Compliance Calendar
    'compliance_type': 'Type: sebi_qar, sebi_aar, ctr_preparation, gst_filing, tds_filing, etc.',
    'calendar_title': 'Title / name of the compliance event',
    'due_date': 'Deadline date',
    'calendar_status': 'Status: upcoming, in_progress, completed, overdue',
    'completed_date': 'Date the task was completed',
    'calendar_notes': 'Notes about the compliance task',
    # CTR
    'financial_year': 'Financial year (e.g., FY2025-26)',
    'overall_compliance_status': 'CTR status: compliant, non_compliant, partially_compliant',
    'ctr_report_status': 'CTR report status: draft, in_review, submitted_to_trustee, finalized',
    'check_number': 'CTR checklist item number',
    'regulation_reference': 'SEBI regulation reference (e.g., Reg 15(1)(a))',
    'check_description': 'Description of the compliance check',
    'check_status': 'Checklist item status: compliant, non_compliant, not_applicable, pending_review',
    'evidence': 'Evidence for the compliance check',
    # SEBI Circulars
    'circular_number': 'SEBI circular number',
    'circular_date': 'Date of the circular',
    'circular_title': 'Title of the circular',
    'circular_summary': 'Summary of the circular',
    'applicability': 'Applicability: all_aif, cat_i, cat_ii, cat_iii, etc.',
    'impact_level': 'Impact: low, medium, high, critical',
    'compliance_deadline': 'Deadline for compliance with the circular',
    # PPM Amendments
    'amendment_number': 'Amendment sequence number',
    'amendment_type': 'Type: investment_strategy, fee_structure, key_personnel, etc.',
    'amendment_title': 'Short title of the amendment',
    'amendment_description': 'Description of what changed',
    'board_approval_date': 'Date of board approval',
    'trustee_approval_date': 'Date of trustee approval',
    'sebi_filing_date': 'Date filed with SEBI',
    'effective_date': 'Date the amendment takes effect',
}

FINANCIALS_PL_BVA_FIELDS = {
    # Identity
    'company_name': 'Portfolio company name — may appear as Company, Entity, Investee, Portfolio Company',
    'period': 'Reporting period — Month (Apr-24, May-24), Quarter (Q1 FY25), or Year (FY2025, 2025)',
    'period_type': 'Period granularity: monthly, quarterly, or annual',
    # P&L line items
    'revenue': 'Revenue / Net Sales / Operating Revenue / Top Line — actual for the period',
    'other_income': 'Other Income / Non-Operating Income / Interest Income',
    'total_revenue': 'Total Revenue / Total Income = Revenue + Other Income',
    'cogs': 'Cost of Goods Sold / Cost of Sales / Cost of Revenue / Direct Cost / Variable Cost',
    'gross_profit': 'Gross Profit / Gross Margin = Revenue minus COGS',
    'employee_cost': 'Employee Cost / Payroll / Salaries / Manpower Cost / People Cost / HR Cost',
    'marketing_cost': 'Marketing Cost / Advertising / Sales & Marketing / Promotion Spend',
    'rd_cost': 'R&D Cost / Research & Development / Technology Cost / Product Cost',
    'g_and_a': 'General & Administrative / G&A / Overhead / Corporate Cost / Admin Expenses',
    'total_opex': 'Total Operating Expenses / Total Opex / Total Cost / Total Expenditure',
    'ebitda': 'EBITDA / Operating Profit / Earnings Before Interest Tax Depreciation Amortisation',
    'depreciation': 'Depreciation & Amortisation / D&A / Dep. / Amortization',
    'ebit': 'EBIT / Earnings Before Interest and Tax / Operating Income (after D&A)',
    'finance_cost': 'Finance Cost / Interest Expense / Borrowing Cost / Financial Charges',
    'pbt': 'Profit Before Tax / PBT / Pre-Tax Profit / EBT',
    'tax': 'Income Tax / Tax Expense / Current Tax / Deferred Tax / Tax Provision',
    'pat': 'Profit After Tax / PAT / Net Profit / Net Income / Bottom Line / Net Earnings',
    # Balance sheet items
    'total_assets': 'Total Assets / Balance Sheet Total / Total Asset Base',
    'total_debt': 'Total Debt / Borrowings / Long-Term Debt / Bank Borrowings / Total Loans',
    'cash_and_equivalents': 'Cash & Equivalents / Cash in Bank / Bank Balance / Closing Cash / Liquid Assets',
    'net_worth': 'Net Worth / Shareholders Equity / Shareholders Funds / Total Equity / Capital & Reserves',
    # Budget vs Actual
    'budget': 'Budgeted amount / AOP / Annual Operating Plan / Plan / Target — for the period',
    'actual': 'Actual amount achieved / Real / Actuals / YTD Actual — for the period',
    'variance': 'Variance = Actual minus Budget (positive = over-achievement for revenue)',
    'variance_pct': 'Variance percentage = Variance / |Budget| × 100',
    'is_favorable': 'Whether the variance is favorable — Yes/No, Favorable/Unfavorable, Green/Red',
    'line_item': 'The P&L / Balance Sheet line item being reported (Revenue, EBITDA, PAT, etc.)',
}

PORTFOLIO_HIERARCHY_FIELDS = {
    'level': 'Hierarchy level: Fund, Sector, Segment, Company',
    'node_id': 'Unique node identifier (e.g., fund_avendus::sector_technology)',
    'label': 'Display label for this node',
    'parent_node_id': 'Parent node identifier',
    'invested': 'Total invested amount at this node',
    'fair_value': 'Current fair value at this node',
    'irr': 'IRR percentage',
    'moic': 'MOIC (multiple on invested capital)',
    'stage': 'Investment stage of the company (Series A, Series B, Bridge, etc.)',
    'headquarters_city': 'City where the company is headquartered',
}


# ---------------------------------------------------------------------------
# Master mapping: domain -> canonical fields dict
# ---------------------------------------------------------------------------

QUOTED_UNQUOTED_FIELDS = {
    'company_name': 'Portfolio company name',
    'share_type': 'Share classification: Listed / Unlisted, Quoted / Unquoted, Equity (Listed) etc.',
    'ipev_level': 'IPEV fair value hierarchy level: Level 1 (market price), Level 2 (observable), Level 3 (unobservable)',
    'listing_exchange': 'Stock exchange: NSE, BSE, NYSE, NASDAQ, etc.',
    'isin': 'ISIN code of the listed security',
    'fair_value': 'Fair value of the holding',
    'cost': 'Cost / invested amount',
}

FEES_REGISTER_FIELDS = {
    'scheme_name': 'Scheme name for the fee record',
    'fee_period': 'Fee period (Q1 FY25, Q2 FY25, etc.)',
    'fee_basis_amount': 'Base amount for fee calculation (committed / called / NAV)',
    'fee_rate': 'Annual fee rate percentage',
    'fee_amount': 'Calculated management fee amount',
    'gst_amount': 'GST on management fee',
    'total_fee': 'Total fee including GST',
}

BURN_RUNWAY_FIELDS = {
    'company_name': 'Portfolio company name',
    'period': 'Reporting period (month/quarter)',
    'gross_burn': 'Total monthly cash outflow / gross burn rate',
    'net_burn': 'Net monthly cash burn = outflow minus revenue',
    'cash_balance': 'Cash and equivalents at period end',
    'runway_months': 'Months of runway = cash / net burn',
    'mrr': 'Monthly Recurring Revenue (SaaS)',
    'arr': 'Annual Recurring Revenue (SaaS)',
    'churn_rate': 'Monthly or annual churn rate',
    'nrr': 'Net Revenue Retention / Net Dollar Retention',
}

FUND_PL_BS_FIELDS = {
    'line_item': 'Financial line item (Revenue, Expenses, Assets, Liabilities, etc.)',
    'amount': 'Amount for the line item',
    'period': 'Reporting period',
    'statement_type': 'Statement type: pl (profit & loss) or bs (balance sheet)',
}

LP_CAPITAL_ACCOUNTS_FIELDS = {
    'investor_name': 'LP / Investor name',
    'commitment': 'Total commitment amount',
    'contributions': 'Total contributions / capital called to date',
    'distributions': 'Total distributions received to date',
    'carried_interest': 'Carried interest allocation',
    'ending_balance': 'Ending capital account balance',
}

NAV_CALCULATION_FIELDS = {
    'opening_nav': 'Opening NAV / Beginning NAV — total fund NAV at start of period',
    'investments_at_cost': 'Total investments at cost / deployed capital',
    'fair_value_adjustment': 'Fair value adjustment / mark-to-market adjustment / FV change',
    'unrealised_gain_loss': 'Unrealised gain or loss on portfolio',
    'realised_gain_loss': 'Realised gain or loss from exits',
    'management_fee': 'Management fee deducted from NAV',
    'operating_expenses': 'Fund operating expenses / admin expenses / other expenses',
    'closing_nav': 'Closing NAV / Ending NAV — total fund NAV at end of period',
    'total_units_outstanding': 'Total units outstanding / units issued',
    'opening_nav_per_unit': 'Opening NAV per unit',
    'closing_nav_per_unit': 'Closing NAV per unit / NAV per unit',
    'income_accrued': 'Income accrued / interest accrued / dividend receivable',
    'carry_provision': 'Carried interest provision deducted from NAV',
}

WATERFALL_CARRY_FIELDS = {
    'total_capital_called': 'Total capital called / total contributions / total drawdowns',
    'preferred_return_amount': 'Preferred return / hurdle amount — LP preferred return before carry',
    'catch_up_amount': 'GP catch-up amount — GP share of excess until carry split is reached',
    'carried_interest_provision': 'Carried interest provision / carry amount / performance fee',
    'carry_percentage': 'Carry percentage (e.g., 20%)',
    'hurdle_rate': 'Hurdle rate / preferred return rate (e.g., 8%)',
    'gp_share': 'GP share / GP distribution amount',
    'lp_share': 'LP share / LP distribution amount',
    'clawback_provision': 'GP clawback provision amount',
    'total_distributions': 'Total distributions to LPs',
    'net_carry': 'Net carried interest after clawback',
    'carry_status': 'Carry status: indicative, crystallised, paid',
}

DOMAIN_FIELDS = {
    'organization_users': ORGANIZATION_USERS_FIELDS,
    'fund_scheme_master': FUND_SCHEME_MASTER_FIELDS,
    'investors_aml': INVESTORS_AML_FIELDS,
    'commitments': COMMITMENTS_FIELDS,
    'capital_calls': CAPITAL_CALLS_FIELDS,
    'portfolio_investments': PORTFOLIO_INVESTMENTS_FIELDS,
    'valuations_kpis': VALUATIONS_KPIS_FIELDS,
    'nav_accounting': NAV_ACCOUNTING_FIELDS,
    'exits_distributions': EXITS_DISTRIBUTIONS_FIELDS,
    'compliance': COMPLIANCE_FIELDS,
    'portfolio_hierarchy': PORTFOLIO_HIERARCHY_FIELDS,
    'financials_pl_bva': FINANCIALS_PL_BVA_FIELDS,
    'quoted_unquoted': QUOTED_UNQUOTED_FIELDS,
    'fees_register': FEES_REGISTER_FIELDS,
    'burn_runway': BURN_RUNWAY_FIELDS,
    'fund_pl_bs': FUND_PL_BS_FIELDS,
    'lp_capital_accounts': LP_CAPITAL_ACCOUNTS_FIELDS,
    'nav_calculation': NAV_CALCULATION_FIELDS,
    'waterfall_carry': WATERFALL_CARRY_FIELDS,
}

# ---------------------------------------------------------------------------
# Pass 3: Semantic Value Interpretation — Canonical definitions
# These replace ALL hardcoded keyword dictionaries in import_service.py.
# Gemini uses these descriptions to classify labels in ANY language.
# ---------------------------------------------------------------------------

CANONICAL_VALUE_CATEGORIES = {
    # ── LP register column roles ──────────────────────────────
    # Phase 2: after Gemini's LP analyst returns, a deterministic Python
    # sweep classifies LP-register column headers into these roles and
    # sums the columns directly via openpyxl. The sums override Gemini's
    # values whenever (a) Gemini returned null OR (b) the sums disagree
    # by more than 10% — eliminating per-file non-determinism in LP-level
    # aggregations like Net Carry, Distributions to LPs, and Commitments.
    'lp_register_columns': {
        'committed':       'LP Commitment / Total Commitment / Capital Commitment / Subscription Amount / Commit (₹ Cr) — the amount each LP has committed to the fund',
        'drawdown':        'LP Drawdown / Called Capital / Drawn Capital / Capital Drawn / Drawdown Amount / Funded Capital — amount actually called from each LP to date',
        'distributions':   'LP Distributions / Distributions Paid / Distribution Received / Distributions to LP / Total Distributions — money returned TO this LP (NOT to be confused with fund-level distributions FROM portfolio exits)',
        'carry_provision': 'Carry Provision / Carry Accrual / Carry Reserve / Performance Fee Accrual / GP Carry / Carried Interest Provision / Carry Prov. — per-LP accrued carry liability shown on the LP register',
        'sponsor_amount':  'Sponsor Commitment / GP Commitment / Manager Commitment / Sponsor Amount — only when this column explicitly identifies a sponsor / GP contribution',
        'investor_name':   'Investor Name / LP Name / Limited Partner / Unitholder / Subscriber — text identifier of the LP',
        'investor_type':   'Investor Type / Type / Category / Investor Category — classifier (Pension, Sovereign Wealth, Insurance, Family Office, etc.)',
        'investor_id':     'LP ID / Investor ID / Folio / Unit Holder ID — code identifier for the LP',
        'pct_share':       '% Fund / Share % / Ownership % / LP Share — proportion of fund corpus held by this LP',
        'drawn_pct':       'Drawn % / % Drawn / Drawdown % — proportion of commitment actually called',
    },
    # ── Valuation sheet column roles (Phase 8 — IPEV compliance) ──
    # Used by valuation_python_sweep() to identify which column on a
    # portfolio-valuation sheet is the canonical Net Fair Value (post
    # DLOM/DLOC) versus an intermediate Gross FV. IPEV / Ind AS 113 require
    # Net FV (after illiquidity + minority discounts) to be the value
    # reported in NAV. Gemini's fund_analyst sometimes picks Gross FV
    # because it appears first in the sheet — this category lets a
    # deterministic Python sweep correct that universally.
    'valuation_columns': {
        'net_fv':         'Net Fair Value / Net FV / Adjusted FV / FV after DLOM/DLOC / Final FV / Carrying Value — the post-discount IPEV-compliant fair value that flows into NAV (canonical)',
        'gross_fv':       'Gross Fair Value / Gross FV / FV before Discount / Pre-Discount FV / Indicative FV / Enterprise Equity Value — intermediate value BEFORE applying DLOM/DLOC discounts (not the NAV input)',
        'cost_basis':     'Cost / Investment Cost / Acquisition Cost / Capital Invested / Cost of Investment — original capital deployed',
        'equity_pct':     'Equity % / Ownership % / Holding % / FD % / Stake — fund\'s ownership stake in the investee',
        'gain_loss':      'Unrealised Gain / Unrealised Loss / Mark-to-Market / FV minus Cost — unrealised appreciation/depreciation of the holding',
        'valuation_date': 'Valuation Date / As-of Date / Reporting Date — date at which the valuation was performed',
        'valuation_method': 'Valuation Method / Valuation Approach / Methodology — DCF / comparables / recent transaction / cost / etc.',
        'company_name':   'Company / Portfolio Company / Investee Name — identifier of the investee being valued',
    },
    # Per-portfolio-company compliance OBLIGATION TYPE (column header in a tracker grid).
    # Lives here (not in CANONICAL_ENUM_TYPES) because identity columns like
    # "Company Name" / "Sector" / "S.No" MUST be allowed to return None — only
    # the actual obligation columns should classify. classify_enum picks the
    # closest match always (no None), which would mis-classify identity cols.
    # Matches PortfolioCompanyCompliance.OBLIGATION_TYPE_CHOICES exactly.
    'compliance_obligation_type': {
        'roc_annual_return':   'ROC / MCA / ROC Annual Return / Annual Return / AOC-4 / MGT-7 / Registrar of Companies — corporate annual return filing',
        'gst_gstr3b':          'GST / GSTR-3B / GST Return / Goods and Services Tax / GST Filing — GST monthly return',
        'labour_pf_esi':       'Labour / Labour Laws / PF / ESI / EPF & ESIC / Provident Fund / Employee State Insurance — labour-law compliance',
        'labour_factories_act':'Factories Act / Labour Welfare / Factories Compliance — factory-act compliance',
        'epf_monthly':         'EPF Monthly Deposit / EPF Challan / EPF Payment — monthly EPF remittance',
        'board_meeting':       'Board Meeting / Directors Meeting / Board Compliance / Quorum — board-meeting compliance',
        'statutory_audit':     'Statutory Audit / Audit / Financial Audit / Auditor Report — statutory annual audit',
        'income_tax_tds':      'TDS / Income Tax TDS / TDS Filing / Tax Deducted at Source — TDS compliance',
        'income_tax_advance':  'Advance Tax / Income Tax Advance / Advance Income Tax — advance-tax instalments',
        'rera':                'RERA / Real Estate Regulation / Real Estate Authority — RERA compliance',
        'sector_specific':     'Sector Specific / Sector Compliance / Industry-specific / SEBI Sector / RBI Sector — sector-specific regulatory item',
    },
    'pl_line_items': {
        'revenue': 'Revenue / Net Sales / Operating Revenue — primary income from business operations',
        'other_income': 'Other Income / Non-Operating Income — interest, dividends, miscellaneous income',
        'total_revenue': 'Total Revenue / Total Income — sum of operating revenue and other income',
        'cogs': 'Cost of Goods Sold / Cost of Sales / Cost of Revenue / Direct Cost — direct costs of producing goods or services',
        'gross_profit': 'Gross Profit / Gross Margin / Contribution — revenue minus COGS',
        'employee_cost': 'Employee Cost / Payroll / Salaries / Staff Cost / Manpower / Compensation / Personnel Cost — all human resource costs',
        'marketing_cost': 'Marketing / Sales & Marketing / Advertising / Promotion / Customer Acquisition Spending — brand and growth costs',
        'rd_cost': 'R&D / Research & Development / Technology Cost / Engineering Cost / Product Cost — innovation and tech spending',
        'g_and_a': 'G&A / General & Administrative / Overhead / Corporate Cost / Admin Cost / Office Cost — administrative expenses',
        'total_opex': 'Total Operating Expenses / Total Opex / Total Cost / Total Expenditure — sum of all operating costs',
        'ebitda': 'EBITDA / Earnings Before Interest Tax Depreciation Amortisation — operating cash profit',
        'depreciation': 'Depreciation & Amortisation / D&A / Depreciation / Amortization — non-cash asset wear charge',
        'ebit': 'EBIT / Operating Income / Operating Profit — earnings after depreciation but before interest and tax',
        'finance_cost': 'Finance Cost / Interest Expense / Borrowing Cost / Interest Paid — cost of debt',
        'pbt': 'Profit Before Tax / PBT / Pre-Tax Profit / Earnings Before Tax — income before tax',
        'tax': 'Income Tax / Tax Expense / Tax Provision / Current Tax / Deferred Tax — government tax on profits',
        'pat': 'Profit After Tax / PAT / Net Profit / Net Income / Net Earnings / Bottom Line — final profit after all deductions',
        'total_assets': 'Total Assets / Balance Sheet Total — sum of all assets',
        'total_debt': 'Total Debt / Borrowings / Total Loans / Debt Outstanding — all outstanding loans',
        'cash_and_equivalents': 'Cash & Cash Equivalents / Cash in Bank / Bank Balance / Cash Balance / Closing Cash / Liquid Assets / Cash Reserves / Treasury',
        'net_worth': 'Net Worth / Shareholders Equity / Total Equity / Book Value / Capital and Reserves',
        'capex': 'Capital Expenditure / Capex / Capital Investment / PPE Addition — spending on long-term assets',
        'working_capital': 'Working Capital — current assets minus current liabilities',
        'net_working_capital': 'Net Working Capital / NWC — refined working capital metric',
        'dividend': 'Dividend / Dividends Paid / Equity Dividend — profit distributed to shareholders',
        'other_cost': 'Other Cost / Other Expense / Miscellaneous Expense / Sundry Expense — costs not in other categories',
    },
    'kpi_types': {
        'gmv': 'Gross Merchandise Value — total transaction value on e-commerce/marketplace platform',
        'revenue': 'Revenue / Net Sales / Turnover / Top Line — primary operating income',
        'gross_margin_pct': 'Gross Margin % / Gross Profit Margin — gross profit as percentage of revenue',
        'ebitda': 'EBITDA — earnings before interest, tax, depreciation, amortization (amount, not %)',
        'ebitda_pct': 'EBITDA Margin % / EBITDA % / Operating Margin — EBITDA as percentage of revenue',
        'orders': 'Order Count / Transactions / Number of Orders — volume of transactions',
        'aov': 'Average Order Value / Average Ticket Size — average revenue per transaction',
        'returns_pct': 'Returns % / Return Rate / RTO % — product/order return rate',
        'cac': 'Customer Acquisition Cost / Blended CAC — cost to acquire one customer',
        'repeat_pct': 'Repeat Customer % / Retention Rate / Customer Retention — repeat purchase rate',
        'cost_to_income': 'Cost to Income Ratio / CI Ratio — cost divided by income (banking metric)',
        'nim_pct': 'Net Interest Margin % / NIM — interest income margin (banking)',
        'gnpa_pct': 'Gross NPA % / GNPA — gross non-performing assets percentage (banking)',
        'nnpa_pct': 'Net NPA % / NNPA — net non-performing assets percentage (banking)',
        'roe_pct': 'Return on Equity % / ROE — net income as percentage of equity',
        'aum': 'Assets Under Management / AUM — total managed assets (financial services)',
        'car_pct': 'Capital Adequacy Ratio % / CAR — regulatory capital ratio (banking)',
        'd_ebitda': 'Debt/EBITDA / Leverage Ratio / Net Debt/EBITDA — leverage metric',
        'capacity_pct': 'Capacity Utilization % / Plant Utilization — manufacturing capacity usage',
        'export_pct': 'Export Revenue % / Export Share / Export Contribution — export as percentage of revenue',
        'headcount': 'Employee Headcount / FTE / Team Size / Staff Count — number of employees',
        'bed_occupancy': 'Bed Occupancy % / Hospital Occupancy — hospital bed utilization (healthcare)',
        'arpob': 'Average Revenue Per Occupied Bed / ARPOB — revenue per bed per day (healthcare)',
        'cap_rate_pct': 'Capitalization Rate % / Cap Rate / Yield % — real estate yield metric',
        'cost': 'Investment Cost / Deployed Capital / Capital Deployed / Total Cost — amount invested',
        'fv': 'Fair Value / Market Value / Current Value / Portfolio Value / FMV — current valuation',
        'moic': 'MOIC / Multiple on Invested Capital / Money Multiple — fair value divided by cost',
        'mrr': 'Monthly Recurring Revenue / MRR — monthly subscription revenue (SaaS)',
        'arr': 'Annual Recurring Revenue / ARR — annualized subscription revenue (SaaS)',
        'churn_pct': 'Churn Rate % / Revenue Churn / Customer Churn / Monthly Churn — attrition rate (SaaS)',
        'nrr_pct': 'Net Revenue Retention % / NRR / Net Dollar Retention / NDR — revenue retention (SaaS)',
        'ltv': 'Customer Lifetime Value / LTV / CLV — expected total revenue per customer (SaaS)',
        'ltv_cac': 'LTV/CAC Ratio / Payback Multiple — lifetime value divided by acquisition cost (SaaS)',
        'burn_rate': 'Burn Rate / Net Burn / Monthly Burn / Cash Burn — monthly cash consumption',
        'runway': 'Runway / Cash Runway / Runway Months — months of cash remaining',
        'pat': 'Profit After Tax / PAT / Net Profit / Net Income — bottom line',
    },
    'fund_metrics': {
        'net_irr': 'Net IRR / LP IRR / Net Return / Net Internal Rate of Return / Fund IRR — fund-level net return after fees',
        'tvpi': 'TVPI / Total Value to Paid-In — ratio of total value (FV + distributions) to invested capital',
        'portfolio_fv': 'Portfolio Fair Value / Portfolio Value / Fund NAV / Total Portfolio FV / Total FV — aggregate fair value',
    },
    'investor_types': {
        'insurance': 'Insurance Company — life insurance, general insurance, reinsurance company',
        'pension': 'Pension Fund / Domestic Pension / Retirement Fund — pension and provident funds',
        'huf': 'Hindu Undivided Family / HUF — Indian family entity for tax purposes',
        'trust': 'Trust / Family Trust / Private Trust / Charitable Trust — trust entities',
        'individual': 'Individual / HNWI / High Net Worth Individual / Natural Person — individual investors',
        'fund_of_funds': 'Fund of Funds / FoF — fund that invests in other funds',
        'fpi': 'Foreign Portfolio Investor / FPI — SEBI-registered foreign investor',
        'company': 'Corporate / Company / Bilateral DFI / Body Corporate — corporate entities',
        'nri': 'Non-Resident Indian / NRI / PIO — Indian nationals residing abroad',
        'family_office': 'Family Office — private wealth management entity for a family',
        'endowment': 'Endowment / Endowment Fund — educational or institutional endowment',
        'llp': 'Limited Liability Partnership / LLP — partnership entity',
        'sovereign': 'Sovereign Wealth Fund / Sovereign / SWF — government investment fund',
        'bank': 'Bank / Financial Institution / Scheduled Bank — banking entity',
    },
    'row_type': {
        'subtotal': 'Subtotal / Sub-Total / Group Total — aggregation of a subset of rows within a section',
        'total': 'Total / Grand Total / Sum Total / Net Total — final summary aggregation row for all items',
        'header': 'Repeated column header / Category label / Section label — not a data row (e.g., Company Name, Particulars, S.No)',
        'serial': 'Serial number / Row counter / Index number / S.No — just a numbering row, not data',
        'note': 'Note / Remark / Footnote / Footer / Annotation — commentary text, not data',
        'real_entity': 'Real company, investor, fund, scheme, or entity name — actual data row to import',
    },
    'nav_components': {
        'total_nav': 'Closing NAV / Fund NAV / Total NAV / Net Asset Value — the fund net asset value at period end',
        'total_investments': 'Total Investments / Investments at Cost / Investment Value / Cost of Investments — gross investments held at period end (cost basis)',
        'unrealized_gains': 'Unrealized Gains / Unrealized Appreciation / Fair Value Adjustment / Mark-to-Market Gains — unrealized portfolio gains',
        'realized_gains': 'Realized Gains / Gains from Exits / Realized Profit — gains from actual exits/sales',
        'mgmt_fee': 'Management Fee / Fund Management Charges / Mgmt Fee — periodic management fee expense',
        'fund_expenses': 'Fund Expenses / Operating Expenses / Other Expenses / Fund Operating Cost — fund-level operating expenses (NOT management fee)',
        'carry_provision': 'Carried Interest Provision / Carry Provision / Performance Fee Accrual / Carry Amount — GP performance fee accrual',
        'investment_income': 'Investment Income / Net Investment Income / Interest & Dividend Income — income earned on investments',
        'closing_nav_per_unit': 'Closing NAV per Unit / NAV/Unit / NAV Per Unit at period end — per-unit net asset value',
        'opening_nav_per_unit': 'Opening NAV per Unit / Opening NAV/Unit — per-unit NAV at period start',
        'total_units': 'Total Units Outstanding / Units Issued / Units — total fund units in circulation',
        'period_label': 'Period / Month / Quarter / Reporting Period — text label identifying the time bucket',
        'period_date': 'Date / As of Date / Period End — calendar date for the period',
        'draw_for_period': 'Drawdown for Period / Capital Drawn / Period Drawdown — capital drawn during the period (NOT NAV)',
    },
    'fee_components': {
        'management_fee': 'Management Fee — base fee charged by the fund manager (excluding GST/tax)',
        'gst_on_management_fee': 'GST on Management Fee / Service Tax on Fee — tax levied on management fee',
    },
    'waterfall_components': {
        'carry_gross': 'Carried Interest Amount / Carry Provision / GP Carry / Performance Fee Amount — total GP performance fee',
        'preferred_return': 'Preferred Return Amount / Hurdle Amount / Hurdle Return — LP hurdle return amount before carry kicks in',
    },
    'fund_performance_metrics': {
        # ----- Headline fund-level metrics (universal across PE/VC/Hedge) -----
        # Each entry is a dict with:
        #   'description'      — semantic prose used by classify_labels.
        #   'value_type'       — drives Pass 3.5 column-role filter:
        #       'per_step_amount': value FOR ONE waterfall step/period; the
        #           cell must be a per_period_amount column (LP Share /
        #           GP Share / Total Step / "this step's value"). NEVER a
        #           cumulative or balance column.
        #       'aggregate_total': sum across ALL steps/periods OR the
        #           final cumulative cell. Both per_period and cumulative
        #           columns are acceptable.
        #       'aggregate_cumulative': running-total / paid-in style
        #           number. Cumulative columns preferred; per_period
        #           acceptable when the per_period cell IS the cumulative
        #           total at a single fund-level summary row.
        #       'per_unit_amount': value per unit (NAV/Unit etc.).
        #       'ratio': IRR / multiple / percentage. Should not come from
        #           cumulative_total columns.
        #   'requires_variant' — None or list of allowed variant tags
        #       (e.g. ['gross', 'net'], ['pre_fee', 'post_fee']). When set,
        #       Pass 3.5 runs an extra Gemini call to tag each candidate
        #       cell's variant before disambiguation.
        #   'variant_default'  — preferred variant when extraction yields
        #       multiple. Carry-base derivation will request this default
        #       unless overridden.
        'net_irr': {
            'description': 'Net IRR / LP IRR / Net Internal Rate of Return — annualised return to LPs net of fees and carry',
            'value_type': 'ratio',
            'requires_variant': None,
            'variant_default': None,
        },
        'gross_irr': {
            'description': 'Gross IRR / Fund-Level IRR / IRR (Gross) — annualised return at the fund level before fees',
            'value_type': 'ratio',
            'requires_variant': None,
            'variant_default': None,
        },
        'moic': {
            'description': 'MOIC / Money Multiple / Investment Multiple / Total Value Multiple — (distributions + residual value) / invested capital',
            'value_type': 'ratio',
            'requires_variant': None,
            'variant_default': None,
        },
        'tvpi': {
            'description': 'TVPI / Total Value to Paid-In — (distributions + residual fund NAV) / cumulative LP paid-in capital',
            'value_type': 'ratio',
            'requires_variant': None,
            'variant_default': None,
        },
        'dpi': {
            'description': 'DPI / Distributions to Paid-In — cumulative distributions / cumulative LP paid-in',
            'value_type': 'ratio',
            'requires_variant': None,
            'variant_default': None,
        },
        'rvpi': {
            'description': 'RVPI / Residual Value to Paid-In — residual fund NAV / cumulative LP paid-in',
            'value_type': 'ratio',
            'requires_variant': None,
            'variant_default': None,
        },
        'nav': {
            'description': 'Total Fund NAV / Net Asset Value / Closing NAV — total fund net asset value at as-of date',
            'value_type': 'aggregate_total',
            'requires_variant': ['gross', 'net'],
            'variant_default': 'net',
        },
        'nav_per_unit': {
            'description': 'NAV per Unit / Unit NAV / NAV/Unit — per-unit net asset value',
            'value_type': 'per_unit_amount',
            'requires_variant': None,
            'variant_default': None,
        },
        'total_called_capital': {
            'description': 'Total Called Capital / Cumulative Drawdown / Paid-In Capital — LP capital actually called to date. Cumulative running total.',
            'value_type': 'aggregate_cumulative',
            'requires_variant': None,
            'variant_default': None,
        },
        'total_committed_capital': {
            'description': 'Total Committed Capital / Fund Size / Total Commitments. Single fund-level total.',
            'value_type': 'aggregate_total',
            'requires_variant': None,
            'variant_default': None,
        },
        'total_distributions': {
            'description': 'Total Distributions / Cumulative Distributions to LPs. NOTE: OVERLAPS with total_realised_proceeds because exit proceeds are typically distributed to LPs. Use total_realised_proceeds + total_unrealised_fair_value for fund-value calculations; do NOT also add total_distributions.',
            'value_type': 'aggregate_cumulative',
            'requires_variant': None,
            'variant_default': None,
        },
        'total_realised_proceeds': {
            'description': 'Total Realised Proceeds / Exit Proceeds / Cumulative Realisations. The sum of gross proceeds from exits. INCLUDES amounts that were distributed to LPs (the same cash flows would also appear in total_distributions). DISJOINT FROM total_unrealised_fair_value.',
            'value_type': 'aggregate_cumulative',
            'requires_variant': ['gross', 'net'],
            'variant_default': 'gross',
        },
        'total_unrealised_fair_value': {
            'description': 'Total Unrealised Fair Value / Total Portfolio FV / Residual Portfolio Value — value of investments still held. DISJOINT FROM total_realised_proceeds and total_distributions. Two semantic variants exist: GROSS (before DLOM/DLOC discounts — used in waterfall calculations), NET (after DLOM/DLOC — used in IPEV-compliant reporting).',
            'value_type': 'aggregate_total',
            'requires_variant': ['gross', 'net'],
            'variant_default': 'gross',
        },
        'total_realised_gains': {
            'description': 'Total Realised Gains / Realised Profit on Exits = total_realised_proceeds - cost_basis_of_exited_investments. NOT the same as total_realised_proceeds (which is gross proceeds, not gains).',
            'value_type': 'aggregate_cumulative',
            'requires_variant': None,
            'variant_default': None,
        },
        'total_unrealised_gains': {
            'description': 'Total Unrealised Gains / Mark-to-Market Gains / Fair Value Appreciation = total_unrealised_fair_value - cost_basis_of_live_investments.',
            'value_type': 'aggregate_cumulative',
            'requires_variant': None,
            'variant_default': None,
        },
        # ----- Distribution-waterfall components (European / American carry) -----
        # PER-STEP amounts. The disambiguator MUST pick per_period_amount
        # columns (LP Share / GP Share / Total Step), NEVER cumulative
        # columns (Cumulative Distributed / Balance Remaining).
        'return_of_capital_amount': {
            'description': 'Return of LP Committed Capital — first step of a European waterfall. PER-STEP value (Step 1 LP Share). NOT the cumulative total after step 1.',
            'value_type': 'per_step_amount',
            'requires_variant': None,
            'variant_default': None,
        },
        'preferred_return_amount': {
            'description': 'Preferred Return / Hurdle Amount / LP Pref Return — per-step LP cash entitlement at the preferred-return step (Step 2 LP Share). NOT the cumulative running total at the end of step 2. The CORRECT cell is the per-period amount column, NEVER the "Cumulative Distributed" or "Balance Remaining" column.',
            'value_type': 'per_step_amount',
            'requires_variant': None,
            'variant_default': None,
        },
        'gp_catchup_amount': {
            'description': 'GP Catch-Up Amount — per-step GP cash at the catch-up step (Step 3 GP Share). LP Share at this step is typically ZERO; the canonical value is the GP Share column, NOT the LP Share column.',
            'value_type': 'per_step_amount',
            'requires_variant': None,
            'variant_default': None,
        },
        'carry_base': {
            'description': 'Carry Base / Profit Above Hurdle / Distributable Profit Subject to Carry — the residual pool of cash REMAINING after Step 1 (return of capital) + Step 2 (preferred return) + Step 3 (catch-up). Equals the Step 4 Total Step value. = total_realised_proceeds + total_unrealised_fair_value_GROSS - total_called_capital - preferred_return_amount - gp_catchup_amount. Do NOT also add total_distributions (overlaps with total_realised_proceeds).',
            'value_type': 'per_step_amount',
            'requires_variant': None,
            'variant_default': None,
        },
        'carry_amount_gross': {
            'description': 'GP Carried Interest (Gross) / Total GP Carry = catchup + GP residual split. Aggregate total across all waterfall steps for the GP. Read from a summary row, not a single step row.',
            'value_type': 'aggregate_total',
            'requires_variant': None,
            'variant_default': None,
        },
        'carry_amount_net': {
            'description': 'GP Carried Interest (Net) = carry_amount_gross - gp_clawback_provision. Aggregate total.',
            'value_type': 'aggregate_total',
            'requires_variant': None,
            'variant_default': None,
        },
        'gp_clawback_provision': {
            'description': 'Clawback Provision / Excess Carry Returned to LPs / GP Clawback. Zero when no carry has actually been paid out yet; otherwise = previously_paid_carry - current_gross_entitlement, floored at 0.',
            'value_type': 'aggregate_total',
            'requires_variant': None,
            'variant_default': None,
        },
        'lp_total_return': {
            'description': 'LP Total Return / Total LP Distribution = return_of_capital + preferred_return + LP_share_of_residual_split. Aggregate sum across all waterfall steps for the LP side.',
            'value_type': 'aggregate_total',
            'requires_variant': None,
            'variant_default': None,
        },
        'gp_total_distribution': {
            'description': 'GP Total Distribution / Total GP Share = gp_catchup + GP_share_of_residual_split. Aggregate sum across all waterfall steps for the GP side.',
            'value_type': 'aggregate_total',
            'requires_variant': None,
            'variant_default': None,
        },
    },
    'burn_runway_metrics': {
        'gross_burn': 'Gross Burn / Total Burn / Monthly Expenses / Cash Outflow / Operating Expenses / Total Opex — total monthly cash outflow',
        'net_burn': 'Net Burn / Net Cash Burn / Net Outflow / Net Operating Cash Flow — net monthly cash burn after revenue',
        'cash_balance': 'Cash in Bank / Cash Balance / Cash & Equivalents / Bank Balance / Closing Cash / Total Cash — cash at period end',
        'runway_months': 'Runway / Cash Runway / Months of Runway / Runway Remaining — months of cash left at current burn rate',
    },
}

CANONICAL_ENUM_TYPES = {
    'exit_type': {
        'ipo': 'IPO / Initial Public Offering / Stock Exchange Listing / Public Listing — company listed on exchange',
        'merger_acquisition': 'Merger & Acquisition / Trade Sale / M&A / Acquisition / Strategic Sale — company acquired by another entity',
        'secondary_sale': 'Secondary Sale / Secondaries / Private Sale / Share Transfer — fund sold shares to another investor',
        'buyback': 'Buyback / Share Buyback / Management Buyout / MBO / Promoter Buyback — company or management repurchased shares',
        'write_off': 'Write-Off / Write-Down / Impairment / Total Loss — investment value reduced to zero or near-zero',
    },
    'distribution_type': {
        'return_of_capital': 'Return of Capital / STCG / LTCG / Capital Return / Capital + Income — principal returned to LPs',
        'income_distribution': 'Income Distribution / Interest Distribution / Yield Distribution — income distributed to LPs',
        'profit_distribution': 'Profit Distribution / Profit Share / Gains Distribution — profit/gains distributed to LPs',
    },
    'valuation_methodology': {
        'dcf': 'Discounted Cash Flow / DCF — value based on projected future cash flows',
        'comparables': 'Market Comparables / Revenue Multiple / EBITDA Multiple / P/E Multiple / EV/EBITDA / Trading Comps — value based on peer multiples',
        'recent_transaction': 'Recent Transaction / Last Round / Latest Funding Round Price — value based on last transaction',
        'net_assets': 'Net Assets / Book Value / NAV / Net Asset Value — value based on balance sheet',
        'cost': 'Cost Method / At Cost / Investment Cost — value at original purchase price',
        'option_pricing': 'Option Pricing Model / OPM / Black-Scholes — value using option pricing methodology',
    },
    'entity_type': {
        'manager': 'Investment Manager / Fund Manager / Asset Manager / Management Company — entity managing the fund',
        'trustee': 'Trustee / Trust Company / Trustee Company — entity holding fund assets in trust',
        'sponsor': 'Sponsor / GP / General Partner / Promoter — entity that established the fund',
        'custodian': 'Custodian / Fund Custodian / Depository Participant / DP — entity safekeeping securities',
        'statutory_auditor': 'Statutory Auditor / Auditor / CA Firm / Audit Firm / Chartered Accountant — entity performing audit',
        'legal_counsel': 'Legal Counsel / Legal Advisor / Law Firm / Advocate — entity providing legal services',
        'registrar': 'Registrar / RTA / Registrar & Transfer Agent / Transfer Agent — entity maintaining investor records',
        'valuer': 'Registered Valuer / Independent Valuer / Valuation Firm / Valuator — entity performing valuations',
    },
    'carry_type': {
        'european': 'European Waterfall / Whole Fund Waterfall — carry calculated on aggregate fund returns',
        'american': 'American Waterfall / Deal-by-Deal — carry calculated per individual deal exit',
    },
    'fee_basis': {
        'committed': 'Committed Capital — fees based on total LP commitments',
        'called': 'Called Capital / Drawn Capital / Invested Capital — fees based on capital actually called',
        'nav': 'NAV / Net Asset Value — fees based on fund NAV',
    },
    'structure_type': {
        'trust': 'Trust — fund structured as a trust (most common for Indian AIFs)',
        'llp': 'LLP / Limited Liability Partnership — fund structured as LLP',
        'company': 'Company / Corporate / Body Corporate — fund structured as a company',
    },
    'quoted_status': {
        'quoted': 'Quoted / Listed / Exchange-Traded — shares traded on a recognized stock exchange',
        'unquoted': 'Unquoted / Unlisted / Private / Not Listed — shares not traded on any exchange',
    },
    'payment_status': {
        'paid': 'Paid / Received / Settled / Completed / Cleared — payment has been made',
        'pending': 'Pending / Outstanding / Due / Unpaid / Awaiting — payment not yet received',
    },
    'investment_status': {
        'active': 'Active / Current / Holding — investment is currently held in portfolio',
        'partially_exited': 'Partially Exited / Partial Exit — some shares sold, some still held',
        'fully_exited': 'Fully Exited / Exited / Sold / Divested — all shares sold or distributed',
        'written_off': 'Written Off / Write-off / Impaired / Loss — investment value reduced to zero',
    },
    'scheme_status': {
        'investing': 'Investing / Investment Period / Deployment Phase — actively deploying capital',
        'fundraising': 'Fundraising / Capital Raising / Open — scheme is raising capital',
        'harvesting': 'Harvesting / Divestment Phase — exiting investments and returning capital',
        'closed': 'Closed / Fully Invested / Fully Committed — no new investments',
        'winding_up': 'Winding Up / Dissolution / Liquidation — scheme is being wound up',
    },
    'capital_call_status': {
        'paid': 'Paid / Funded / Received / Yes / Settled — capital call has been funded',
        'pending': 'Pending / Not Yet Paid / Outstanding / Awaiting — call not yet received',
        'partially_paid': 'Partially Paid / Partial — some portion has been funded',
        'overdue': 'Overdue / Past Due / Defaulted — payment is past the due date',
    },
    'close_type': {
        'first_close': 'First Close / Initial Close / 1st Close — first closing of the fund/scheme',
        'subsequent_close': 'Subsequent Close / Additional Close / 2nd Close / 3rd Close — any close after the first',
        'final_close': 'Final Close / Last Close / Closing — last/final closing of the fund/scheme',
    },
    'instrument_type': {
        'equity': 'Equity / Ordinary Shares / Common Stock — equity ownership stake',
        'safe': 'SAFE / Simple Agreement for Future Equity',
        'ccps': 'CCPS / Compulsorily Convertible Preference Shares / Convertible Preferred',
        'convertible_note': 'Convertible Note / Convertible Debenture / Convertible — debt converting to equity',
        'preference_shares': 'Preference Shares / Preferred Stock / Preferred Equity — preferential dividend/liquidation rights',
        'ccd': 'CCD / Compulsorily Convertible Debentures — mandatory debt-to-equity conversion',
        'debt': 'Debt / Loan / Senior Debt / Mezzanine — non-convertible lending',
        'warrant': 'Warrant / Option / Stock Warrant — right to purchase equity at predetermined price',
    },
    'ipev_level': {
        '1': 'Level 1 / Quoted prices in active markets — observable market prices',
        '2': 'Level 2 / Observable inputs / Market comparables / Comparable transactions',
        '3': 'Level 3 / Unobservable inputs / Model-based / DCF / Cost method',
    },
    'column_qualifier': {
        'budget': 'Budget / AOP / Plan / Target / Forecast / Planned / Budgeted — projected/planned figures',
        'actual': 'Actual / Actuals / Real / Achieved / Reported / Realized — actual/realized figures',
        'variance': 'Variance / Var / Difference / Diff / Delta — difference between budget and actual',
    },
    # Per-portfolio-company compliance status (RAG cell value in a tracker grid)
    'compliance_company_status': {
        'compliant': 'Compliant / Current / Filed / Up-to-date / OK / Done / Yes / Green / Filed On Time — obligation is fulfilled',
        'due':       'Due Soon / Pending Review / Pending / Awaiting / Upcoming / Amber — needs attention within grace period',
        'overdue':   'Overdue / Delayed / Late / Past Due / Missed / Default / Red — obligation breached / past deadline',
        'not_applicable': 'N/A / NA / Not Applicable / Exempt / — / Blank / Grey — obligation does not apply',
    },
    # Fund-level SEBI / regulatory filing OBLIGATION (row label in a fund-filings table).
    # These keys map to SEBIReport.report_type (qar / aar) where applicable; rows that
    # don't fit QAR or AAR cleanly are routed to ComplianceCalendar instead with the
    # matching compliance_type below.
    'sebi_filing_type': {
        'qar':     'SEBI QAR / Quarterly Activity Report / Quarterly Filing / Quarterly Return — SEBI quarterly filing',
        'aar':     'SEBI AAR / Annual Activity Report / Annual Filing / Annual Return — SEBI annual filing',
        'ctr':     'CTR / Compliance Test Report — annual compliance test report',
        'fatca_crs': 'FATCA / CRS / Foreign Account Tax Compliance Act / Common Reporting Standard — FATCA/CRS report',
        'fema':    'FEMA / FDI / ODI / Foreign Exchange Management Act / RBI Reporting — FEMA compliance',
        'nav_depositories': 'NAV to Depositories / NSDL / CDSL / NAV Reporting / Depository NAV — quarterly NAV upload',
        'valuation_certificate': 'Valuation Certificate / Valuation Report / Independent Valuation / Valuer Certificate',
        'other':   'Other / Misc / Miscellaneous — anything not matching the above',
    },
    # SEBI / regulatory FILING STATUS (status cell in a fund-filings table).
    # Maps to SEBIReport.FILING_STATUS_CHOICES via the renderer logic.
    'compliance_filing_status': {
        'filed':         'Filed / Filed On Time / Filed / Submitted / Done / Accepted / Received / Acknowledged — successfully filed',
        'pending':       'Pending / Pending Review / Awaiting / In Progress / Data Collection / Under Review / In Review — not yet filed',
        'overdue':       'Overdue / Late / Delayed / Missed / Past Due / Default — past deadline',
        'not_started':   'Not Started / Open / To Do — work not begun',
    },
}

CANONICAL_METADATA_FIELDS = {
    'scheme_lifecycle': {
        'tenure_years': {'desc': 'Fund/scheme tenure or duration in years', 'type': 'int'},
        'first_close_date': {'desc': 'Date of initial/first close of the scheme', 'type': 'date'},
        'final_close_date': {'desc': 'Date of final/last close of the scheme', 'type': 'date'},
        'scheme_size': {'desc': 'Total fund corpus or scheme size (in base currency units)', 'type': 'decimal'},
        'hurdle_rate_pct': {'desc': 'Hurdle rate or preferred return percentage', 'type': 'pct'},
        'carry_pct': {'desc': 'Carried interest or performance fee percentage', 'type': 'pct'},
        'carry_type': {'desc': 'Carry/waterfall type: european (whole fund) or american (deal-by-deal)', 'type': 'enum'},
        'management_fee_pct': {'desc': 'Annual management fee percentage', 'type': 'pct'},
        'management_fee_basis': {'desc': 'Fee basis: committed, called, or nav', 'type': 'enum'},
        'sponsor_commitment_pct': {'desc': 'Sponsor/GP commitment as percentage of scheme size', 'type': 'pct'},
        'vintage_year': {'desc': 'Vintage year or inception year of the scheme', 'type': 'int'},
        # Phase 9 — time-dependent fee structure.
        # Indian AIFs commonly charge a higher mgmt fee during the
        # Investment Period (typically on Committed Capital) and a lower
        # fee post-IP (typically on NAV). The single management_fee_pct /
        # management_fee_basis fields capture whichever rate Gemini
        # picked; these two additional fields capture the post-IP rate
        # explicitly so the pipeline can switch to it once the fund is
        # past its IP. Universal — when a file lists only one rate,
        # these stay null and no switch happens.
        'investment_period_years': {'desc': 'Investment Period length in years — the deployment window from final close, after which management fee terms typically change', 'type': 'int'},
        'mgmt_fee_pct_post_ip': {'desc': 'Post-Investment-Period management fee rate (when the file lists a separate rate for after the IP ends)', 'type': 'pct'},
        'mgmt_fee_basis_post_ip': {'desc': 'Post-Investment-Period fee basis (typically "nav" once active deployment ends). One of: committed, called, nav', 'type': 'enum'},
    },
    'fund_identity': {
        'fund_name': {'desc': 'Name of the AIF fund or scheme', 'type': 'str'},
        'sebi_registration_number': {'desc': 'SEBI AIF registration number (format: IN/AIF*/XX-XX/XXXXX)', 'type': 'str'},
        'category': {'desc': 'AIF category — I, II, or III', 'type': 'str'},
        'structure_type': {'desc': 'Legal structure of the fund — trust, llp, or company', 'type': 'enum'},
        'fund_pan': {'desc': 'PAN number of the fund entity', 'type': 'str'},
        'fund_gstin': {'desc': 'GSTIN of the fund entity', 'type': 'str'},
        'is_gift_city': {'desc': 'Whether this is a GIFT City / IFSC offshore AIF (yes/no/true/false)', 'type': 'bool'},
    },
}


# ---------------------------------------------------------------------------
# Pass 4: Derivable Fund-Level Metrics
# ---------------------------------------------------------------------------
# Headline fund-level metrics that the dashboard must show. If a direct value
# is missing in the imported Excel, Gemini is asked to (a) enumerate all
# canonical formulas to compute the metric, (b) pick the formula whose inputs
# are ALL present in DerivationContext, (c) compute the value.
#
# NO formulas are listed here — formula selection is entirely Gemini's job.
# We only describe each metric so Gemini knows what we are asking it to derive.

DERIVABLE_FUND_METRICS = {
    'net_irr': {
        'label': 'Net IRR',
        'unit': 'percent',
        'description': (
            'Net Internal Rate of Return — the annualised, time-weighted rate of return '
            'realised by LPs on their net cash flows into and out of the fund, net of all '
            'fees, expenses and carried interest. Typically computed via XIRR over the full '
            'series of LP contributions (capital calls, negative) and LP distributions '
            '(positive), with the current residual NAV included as a synthetic terminal '
            'inflow at the as-of date.'
        ),
    },
    'moic': {
        'label': 'MOIC (TVPI Gross of Cost)',
        'unit': 'multiple',
        'description': (
            'Multiple on Invested Capital — total value (realised distributions + current '
            'unrealised value) divided by total invested capital at the portfolio level. '
            'Reflects gross multiple before LP-level fee drag.'
        ),
    },
    'tvpi': {
        'label': 'TVPI (Total Value to Paid-In)',
        'unit': 'multiple',
        'description': (
            'Total Value to Paid-In — (cumulative distributions to LPs + residual fund NAV) '
            'divided by cumulative LP paid-in capital (cumulative capital called from LPs).'
        ),
    },
    'dpi': {
        'label': 'DPI (Distributions to Paid-In)',
        'unit': 'multiple',
        'description': (
            'Distributions to Paid-In — cumulative distributions paid to LPs divided by '
            'cumulative LP paid-in capital. Measures realised return to date.'
        ),
    },
    'rvpi': {
        'label': 'RVPI (Residual Value to Paid-In)',
        'unit': 'multiple',
        'description': (
            'Residual Value to Paid-In — current unrealised fund NAV divided by cumulative '
            'LP paid-in capital. Measures unrealised value still in the fund.'
        ),
    },
    'nav': {
        'label': 'NAV (Net Asset Value)',
        'unit': 'currency',
        'description': (
            'Net Asset Value of the fund as of the latest available date — the fair value '
            'of all portfolio holdings plus cash and other assets, minus accrued expenses, '
            'fees and carry provision. If no direct NAV record exists, derive from '
            'aggregate fair value of investments minus liabilities.'
        ),
    },
    # ── Distribution-waterfall components ────────────────────────────────
    # The four numbers the dashboard's "Carry & Clawback Analysis" panel
    # displays: carry_base, carry_amount_gross, carry_amount_net,
    # gp_clawback_provision. Pre-requisite supporting amounts —
    # return_of_capital_amount, preferred_return_amount, gp_catchup_amount,
    # lp_total_return, gp_total_distribution — are derived as part of the
    # same waterfall. Gemini chooses the correct waterfall formulation
    # (European whole-fund vs American deal-by-deal; compound vs simple
    # preferred return) based on the scheme's LPA terms passed as inputs
    # (lpa_carry_type, lpa_hurdle_rate_pct, lpa_carry_pct, etc.).
    'return_of_capital_amount': {
        'label': 'Return of LP Committed Capital',
        'unit': 'currency',
        'description': (
            'European waterfall step 1: cumulative cash returned to LPs as principal '
            'BEFORE any preferred return or carry — limited to LP committed capital '
            '(or paid-in capital, depending on LPA wording). In a deal-by-deal '
            '(American) waterfall, this is the per-deal cost-basis return.'
        ),
    },
    'preferred_return_amount': {
        'label': 'Preferred Return / Hurdle Amount',
        'unit': 'currency',
        'description': (
            'Cumulative preferred return (hurdle amount) cleared to LPs before GP carry '
            'kicks in. For a COMPOUND hurdle: LP_committed_or_paid_in × ((1 + hurdle)^years - 1). '
            'For a SIMPLE hurdle: LP_committed_or_paid_in × hurdle × years. The hurdle '
            'rate (lpa_hurdle_rate_pct), the base (LP committed vs paid-in per LPA), '
            'and the compounding convention (compound vs simple, per LPA) must be '
            'taken from the LPA inputs. Years = years_since_inception (or the LPA '
            'average hold period if explicitly provided).'
        ),
    },
    'gp_catchup_amount': {
        'label': 'GP Catch-Up Amount',
        'unit': 'currency',
        'description': (
            'Step 3 of a European waterfall with catch-up: amount paid to GP after the '
            'preferred return so that GP receives its target carry % of the combined '
            '(preferred return + catch-up) pool. With a 100% catch-up (most common): '
            'min((carry_pct / (1 - carry_pct)) × (return_of_capital_amount + preferred_return_amount), '
            'remaining_proceeds_after_pref). If the LPA waterfall has NO catch-up '
            'tier, return 0.'
        ),
    },
    'carry_base': {
        'label': 'Carry Base (Profit Above Hurdle)',
        'unit': 'currency',
        'description': (
            'Profit pool on which GP carry is computed. European whole-fund formula: '
            'max(total_value − total_called_capital − preferred_return_amount − '
            'gp_catchup_amount, 0), where total_value = total_distributions + '
            'total_realised_proceeds + total_unrealised_fair_value (i.e. the residual '
            'NAV). If total_value ≤ LP minimum (called + preferred return), carry '
            'base is 0 — the fund has not yet returned principal+hurdle to LPs.'
        ),
    },
    'carry_amount_gross': {
        'label': 'GP Carry (Gross)',
        'unit': 'currency',
        'description': (
            'Total GP carried-interest entitlement BEFORE clawback. European with '
            'catch-up: gp_catchup_amount + carry_base × lpa_carry_pct. European '
            'without catch-up: carry_base × lpa_carry_pct. American (deal-by-deal): '
            'sum of per-deal carry across exited deals. Use lpa_carry_type to pick '
            'the formulation.'
        ),
    },
    'gp_clawback_provision': {
        'label': 'GP Clawback Provision',
        'unit': 'currency',
        'description': (
            'Excess GP carry that must be returned to LPs at fund term (or held in '
            'escrow under the clawback reserve). Computed as: max(carry_actually_paid '
            '- carry_amount_gross_entitlement, 0). For interim periods (fund still '
            'active) clawback is the ESCROWED reserve = carry_amount_gross × '
            'clawback_reserve_pct (from LPA, typically 30%); if the LPA has no '
            'explicit reserve % AND no carry has been paid yet, return 0.'
        ),
    },
    'carry_amount_net': {
        'label': 'GP Carry (Net)',
        'unit': 'currency',
        'description': (
            'GP carried interest AFTER clawback provision: carry_amount_gross − '
            'gp_clawback_provision. This is what the GP actually keeps net of '
            'clawback escrow / true-up.'
        ),
    },
    'lp_total_return': {
        'label': 'LP Total Return',
        'unit': 'currency',
        'description': (
            'Total cash returned to LPs across the entire waterfall: '
            'return_of_capital_amount + preferred_return_amount + LP share of '
            'residual proceeds (residual × (1 - carry_pct)).'
        ),
    },
    'gp_total_distribution': {
        'label': 'GP Total Distribution',
        'unit': 'currency',
        'description': (
            'Total cash to GP across the entire waterfall = gp_catchup_amount + '
            'carry_amount_gross (the GP share of residual is already included in '
            'carry_amount_gross when computed via the residual-split formula).'
        ),
    },
}


# Inputs the derivation service makes available to Gemini for every Pass 4 call.
# Each entry: {key, description, unit}. Values are filled at runtime from the
# DB. Gemini is told which inputs have non-null values and which are missing,
# and must pick a formula whose required inputs are all present.
DERIVATION_CONTEXT_INPUTS = {
    'total_committed_capital': {
        'description': 'Sum of all LP commitments to the scheme (₹)',
        'unit': 'currency',
    },
    'total_called_capital': {
        'description': 'Cumulative LP capital actually called/drawn down to date (₹)',
        'unit': 'currency',
    },
    'total_invested_capital': {
        'description': 'Cumulative capital deployed into portfolio companies (₹). At the portfolio level this is the sum of Investment.total_invested across all live + exited investments.',
        'unit': 'currency',
    },
    'total_distributions_to_lps': {
        'description': 'Cumulative gross distributions paid to LPs to date (₹) — includes return of capital, profit distributions, income distributions.',
        'unit': 'currency',
    },
    'total_realised_proceeds': {
        'description': 'Cumulative gross proceeds from exits at the portfolio level (₹) — sum of ExitEvent.exit_proceeds across all exits.',
        'unit': 'currency',
    },
    'total_realised_gains': {
        'description': 'Cumulative realised gains (₹) — sum of (exit_proceeds - cost_of_exited_stake) across all exits.',
        'unit': 'currency',
    },
    'total_unrealised_fair_value': {
        'description': 'Aggregate latest fair value of unrealised holdings (₹) — sum of latest Valuation.fair_value for each live investment.',
        'unit': 'currency',
    },
    'total_unrealised_gains': {
        'description': 'Aggregate unrealised gains on live holdings (₹) — sum of (latest_fair_value - cost_basis) across live investments.',
        'unit': 'currency',
    },
    'fund_nav_latest': {
        'description': 'Most recent total NAV reported for the scheme, if a NAVRecord exists (₹).',
        'unit': 'currency',
    },
    'fund_units_outstanding': {
        'description': 'Total units outstanding on the most recent NAV date.',
        'unit': 'units',
    },
    'cashflow_series': {
        'description': (
            'Time-stamped LP cashflow series for XIRR: list of {date, amount} where '
            'capital calls are NEGATIVE (LP outflow) and distributions are POSITIVE '
            '(LP inflow). The residual NAV at the as-of date is appended as a synthetic '
            'POSITIVE terminal cashflow when computing IRR.'
        ),
        'unit': 'series',
    },
    'inception_date': {
        'description': 'Scheme inception / first close date.',
        'unit': 'date',
    },
    'as_of_date': {
        'description': 'As-of date for the derivation (today).',
        'unit': 'date',
    },
    'years_since_inception': {
        'description': 'Years elapsed between inception_date and as_of_date.',
        'unit': 'years',
    },
    'accrued_management_fees': {
        'description': 'Cumulative accrued management fees on the scheme (₹), if computed.',
        'unit': 'currency',
    },
    'accrued_carried_interest': {
        'description': 'Cumulative accrued GP carry provision on the scheme (₹), if computed.',
        'unit': 'currency',
    },
    # ----- LPA-extracted economic terms (Limited Partner Agreement) -----
    # These come from the LPA / Private Placement Memorandum and are needed
    # whenever a derivation must apply fee drag, hurdle compounding, or carry
    # waterfall mechanics to convert gross numbers into net numbers.
    'lpa_management_fee_pct': {
        'description': 'Annual management fee % from the LPA (e.g. 2.0 means 2.0% p.a.). Apply to management_fee_basis amount to compute annual fee drag.',
        'unit': 'percent',
    },
    'lpa_management_fee_basis': {
        'description': 'Base on which management fee is charged per the LPA — "committed" (on committed capital), "called" (on called/drawn capital), or "nav" (on NAV). Determines which capital base the fee % applies to.',
        'unit': 'enum',
    },
    'lpa_hurdle_rate_pct': {
        'description': 'Preferred return / hurdle rate % from the LPA (e.g. 8.0 means LPs must earn 8.0% p.a. compounded before GP can take carry).',
        'unit': 'percent',
    },
    'lpa_carry_pct': {
        'description': 'Carried interest / performance fee % from the LPA (e.g. 20.0 means GP receives 20% of profits above hurdle).',
        'unit': 'percent',
    },
    'lpa_carry_type': {
        'description': 'Waterfall type from the LPA — "european" (whole-fund / aggregate waterfall) or "american" (deal-by-deal). Determines whether carry is computed on aggregate fund returns or per-investment.',
        'unit': 'enum',
    },
    'lpa_sponsor_commitment_pct': {
        'description': 'GP/sponsor commitment as % of total scheme size per the LPA.',
        'unit': 'percent',
    },
    'lpa_tenure_years': {
        'description': 'Scheme tenure / fund life in years per the LPA.',
        'unit': 'years',
    },
}


# ---------------------------------------------------------------------------
# Pass 3.5 — value_type ↔ column_role compatibility matrix
# ---------------------------------------------------------------------------
# Used by _extract_explicit_performance_metrics to filter candidate cells
# BEFORE disambiguation. Each canonical metric's value_type (declared in
# CANONICAL_VALUE_CATEGORIES['fund_performance_metrics']) maps to the set
# of column_role values that can legitimately host that metric.
#
# Free-form (non-tabular) candidates have column_role=None — they are
# accepted for any value_type because we cannot constrain them without
# column-header context. The disambiguation step still chooses among
# them.

VALUE_TYPE_TO_ALLOWED_COLUMN_ROLES = {
    # Per-step amounts MUST come from per_period_amount columns. Cumulative
    # / running-total columns and ratio/percent columns are excluded.
    'per_step_amount': {'per_period_amount'},
    # Aggregate totals can appear as the final cumulative cell OR as a
    # per_period (single fund-level summary row, e.g. "Total Proceeds:
    # 2465.69"). Both are acceptable.
    'aggregate_total': {'per_period_amount', 'cumulative_total'},
    # Cumulative running totals — prefer cumulative_total columns; allow
    # per_period_amount fallback for the single summary-row case.
    'aggregate_cumulative': {'cumulative_total', 'per_period_amount'},
    # Per-unit (NAV/Unit etc.) is a single value, typically in a summary
    # row's per_period_amount column.
    'per_unit_amount': {'per_period_amount'},
    # Ratios / multiples / percentages.
    'ratio': {'ratio_percent', 'per_period_amount'},
}


def is_role_compatible(value_type, column_role):
    """Return True iff a candidate cell with `column_role` can legitimately
    host a canonical metric whose `value_type` is the given one.

    Free-form (no section / no role) cells are accepted for every value_type
    so that Pass 3.5 does not regress on workbooks without tabular sections.
    """
    if not column_role:
        return True  # Free-form cell — accept; disambiguation handles it.
    allowed = VALUE_TYPE_TO_ALLOWED_COLUMN_ROLES.get(value_type)
    if allowed is None:
        return True  # Unknown value_type — fail open (accept).
    return column_role in allowed
