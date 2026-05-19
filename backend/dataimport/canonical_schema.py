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
        'NAV time-series data — NAV date, total NAV, NAV per unit, units outstanding, '
        'investments at fair value, cash and equivalents. One row per period. '
        'Example headers: NAV RECORDS, NAV HISTORY, NAV TIME SERIES, '
        'NET ASSET VALUE, MONTHLY NAV, PERIODIC NAV, NAV & FUND ACCOUNTING'
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
    'cost_basis': 'Original cost basis',
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
        'cash_and_equivalents': 'Cash & Cash Equivalents / Cash in Bank / Bank Balance / Liquid Assets / Cash Reserves',
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
        'unrealized_gains': 'Unrealized Gains / Unrealized Appreciation / Fair Value Adjustment / Mark-to-Market Gains — unrealized portfolio gains',
        'realized_gains': 'Realized Gains / Gains from Exits / Realized Profit — gains from actual exits/sales',
        'mgmt_fee': 'Management Fee / Fund Management Charges / Mgmt Fee — periodic management fee expense',
        'carry_provision': 'Carried Interest Provision / Carry Provision / Performance Fee Accrual / Carry Amount — GP performance fee accrual',
        'investment_income': 'Investment Income / Net Investment Income / Interest & Dividend Income — income earned on investments',
        'closing_nav_per_unit': 'Closing NAV per Unit / NAV/Unit / NAV Per Unit at period end — per-unit net asset value',
        'opening_nav_per_unit': 'Opening NAV per Unit / Opening NAV/Unit — per-unit NAV at period start',
        'total_units': 'Total Units Outstanding / Units Issued / Units — total fund units in circulation',
    },
    'fee_components': {
        'management_fee': 'Management Fee — base fee charged by the fund manager (excluding GST/tax)',
        'gst_on_management_fee': 'GST on Management Fee / Service Tax on Fee — tax levied on management fee',
    },
    'waterfall_components': {
        'carry_gross': 'Carried Interest Amount / Carry Provision / GP Carry / Performance Fee Amount — total GP performance fee',
        'preferred_return': 'Preferred Return Amount / Hurdle Amount / Hurdle Return — LP hurdle return amount before carry kicks in',
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
