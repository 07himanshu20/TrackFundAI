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
    'investors_aml': 'LP/investor records with KYC status, AML due diligence, bank accounts, and SEBI compliance flags',
    'commitments': 'LP commitments to schemes — amounts, close types, dates',
    'capital_calls': 'Capital call events and per-LP line items with payment tracking',
    'portfolio_investments': 'Portfolio companies, investments (instrument type, ownership), tranches, and board meetings',
    'valuations_kpis': 'Investment valuations (DCF, comparables) and portfolio company KPIs (MRR, burn rate, etc.)',
    'nav_accounting': 'NAV records, chart of accounts, double-entry ledger, carried interest, management fees',
    'exits_distributions': 'Exit events (IPO, M&A, secondary) and LP distribution payouts with TDS',
    'compliance': 'SEBI reports (QAR/AAR), compliance calendar, compliance test reports, SEBI circulars, PPM amendments',
    'portfolio_hierarchy': 'Portfolio hierarchy tree: fund > sector > segment > company nodes with cross-fund mapping',
    'financials_pl_bva': 'Company-level P&L (Revenue, COGS, EBITDA, PAT), Balance Sheet, Cash Flow, and Budget vs Actual — monthly or period-based financial statements for portfolio companies',
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
}
