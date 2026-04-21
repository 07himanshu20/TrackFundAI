# TrackFundAI — Complete Platform Architecture

**Product:** TrackFundAI  
**Company:** Trivesta Consulting Pvt. Ltd.  
**Domain:** SEBI-regulated Alternative Investment Fund (AIF) management  
**Tech Stack:** Django 4.2 + DRF 3.15 | Vanilla JS + Chart.js | PostgreSQL (SQLite for dev) | Gemini 2.5 Flash  

---

## Platform Overview

TrackFundAI is a **7-module operating system** for Indian AIFs. The existing portfolio comparison dashboard (Phase 0) becomes Module 3 inside the larger platform.

```
+-------------------------------------------------------------------+
|                       TRACKFUNDAI PLATFORM                        |
+---------+----------+----------+----------+----------+-------------+
|Module 1 |Module 2  |Module 3  |Module 4  |Module 5  |Module 6 + 7 |
|Fund     |LP        |Portfolio |Fund      |SEBI      |Users &      |
|Admin    |Mgmt      |Monitor   |Accounting|Compliance|Documents    |
|         |          |          |          |          |             |
|Funds    |Investors |Companies |NAV       |QAR/AAR   |Auth/RBAC    |
|Schemes  |KYC       |Invest-   |Ledger    |CTR       |Doc Vault    |
|Entities |Commit-   | ments    |Fees      |AML       |LP Portal    |
|Trustee  | ments    |Valuations|Carry     |PPM       |Notif-       |
|Custodian|Cap Calls |KPIs      |Expenses  |Calendar  | ications    |
|Sponsor  |Distri-   |Exits     |          |          |Audit Log    |
|         | butions  |Board Mtgs|          |          |             |
|         |Cap Accts |          |          |          |             |
+---------+----------+----------+----------+----------+-------------+
                          ^
                          |
                   +--------------+
                   | OUR CURRENT  |
                   |  DASHBOARD   |
                   |              |
                   | P&L trends   |
                   | BvA compare  |
                   | KPI table    |
                   | Cash flow    |
                   | Working cap  |
                   | AI chatbot   |
                   +--------------+
```

---

## Module-by-Module Breakdown

### Module 1: Fund Administration
**Who uses it:** Fund manager, operations team  
**What it does:**
- Register funds with SEBI (registration number, category — Cat I/II/III)
- Track fund schemes (close-ended/open-ended, tenure, corpus, hurdle rate, carry %)
- Manage entities — who is the trustee, custodian, sponsor, auditor
- Handle GIFT City / IFSC funds separately

**Tables:** `funds`, `fund_schemes`, `entities`

> This is the foundation. Everything else references a fund or scheme. Build this first.

---

### Module 2: LP Management
**Who uses it:** Investor relations team, fund admin, LPs themselves (via portal)  
**What it does:**
- Onboard investors (individuals, companies, FPIs, family offices, sovereign wealth)
- Full KYC lifecycle — PAN, Aadhaar, passport, CKYC, accreditation
- Track commitments (which LP committed how much to which scheme)
- Issue **capital calls** (drawdown notices) — "we need 10% of your commitment now"
- Process **distributions** (returns to LPs) — with TDS, capital gains breakdown
- Maintain **LP capital accounts** — the running ledger showing committed/called/returned/unrealized for each LP

**Tables:** `investors`, `investor_kyc_docs`, `investor_bank_accounts`, `commitments`, `capital_calls`, `capital_call_line_items`, `distributions`, `distribution_line_items`, `lp_capital_accounts`

> This is the revenue engine. LPs put money in, this module tracks every rupee.

---

### Module 3: Portfolio Monitoring (OUR DASHBOARD LIVES HERE)
**Who uses it:** Investment team, CFO, portfolio managers  
**What it does:**
- Track portfolio companies (investee companies the fund has invested in)
- Record investments — instrument type (equity, CCPS, CCD, NCD), tranches, ownership %
- **Valuations** — IPEV-standard fair value (DCF, comparables, recent transaction)
- **KPI tracking** — monthly/quarterly metrics from portfolio companies
- **Exit events** — secondary sales, IPOs, M&A, write-offs
- **Board meeting tracking** — governance for each portfolio company

**Tables:** `portfolio_companies`, `investments`, `investment_tranches`, `valuations`, `kpi_definitions`, `portfolio_kpis`, `exit_events`, `board_meetings`

> Our dashboard = the KPI tracking + visualization layer of this module.  
> What we currently do with `monthly_pl`, `budget_vs_actual`, `cash_flow`, `working_capital`, `sales_by_segment` maps directly to `portfolio_kpis` + `kpi_definitions`.

---

### Module 4: Fund Accounting
**Who uses it:** Fund accountant, CFO, auditor  
**What it does:**
- **NAV computation** — Net Asset Value per unit, per scheme, per date
- Reconcile with CDSL/NSDL depositories (mandatory for SEBI)
- **Double-entry general ledger** — every capital call, distribution, investment, fee is a journal entry
- **Management fee billing** — calculate fees based on committed/called/NAV, add GST
- **Carried interest** — waterfall calculations (hurdle -> catch-up -> carry split)
- **Fund expenses** — audit fees, legal, custodian, valuation fees

**Tables:** `nav_records`, `fund_ledger`, `management_fees`, `carried_interest`, `fund_expenses`

---

### Module 5: SEBI Compliance
**Who uses it:** Compliance officer, fund manager  
**What it does:**
- **QAR** (Quarterly Activity Report) — filed on SEBI SI Portal every quarter
- **AAR** (Annual Activity Report) — annual filing with NAV reconciliation
- **CTR** (Compliance Test Report) — annual checklist submitted to trustee
- **AML due diligence** — land-border country investor checks (SEBI Oct 2024 circular), PEP screening, beneficial ownership verification
- **PPM amendment log** — track every change to the Private Placement Memorandum
- **Compliance calendar** — all regulatory deadlines with reminders

**Tables:** `sebi_reports`, `compliance_calendar`, `compliance_test_reports`, `ctr_checklist_items`, `aml_due_diligence`, `ppm_amendments`

> India-specific. This is what makes TrackFundAI valuable in the Indian AIF market.

---

### Module 6: Users & Access Control
**Who uses it:** Everyone (admin configures, all users consume)  
**What it does:**
- User authentication (email/password + MFA)
- **RBAC** — roles like fund_admin, compliance_officer, lp_viewer, analyst, auditor_readonly
- Map users to specific funds/schemes (multi-fund isolation)
- **Audit trail** — every action logged (who changed what, when, from what IP)
- LP portal users get read-only access to their own capital account + documents

**Tables:** `users`, `organizations`, `fund_access`, `audit_logs`

---

### Module 7: Documents & Notifications
**Who uses it:** All users, especially LPs via portal  
**What it does:**
- **Document vault** — PPMs, subscription agreements, capital call notices, distribution notices, valuation reports, audit reports
- Access control per document (some LP-visible, some internal-only)
- **LP portal** — read-only dashboard for LPs to see their capital account, NAV, documents
- **Notifications** — email/in-app alerts for capital calls, distributions, compliance deadlines, KPI submission reminders

**Tables:** `documents`, `document_access`, `notifications`, `notification_preferences`

---

## Phase Plan — Build Order

```
PHASE 0  [DONE]
  + Portfolio monitoring UI (our dashboard)
  + MIS Excel parsing (Gemini-powered)
  + Fund > Sector > Segment > Company hierarchy
  + KPI comparison (row x col table, charts, AI chatbot)
  - Everything stored in JSON file, no database

PHASE 1: Foundation (Module 1 + Module 6)  [DONE]
  |  PostgreSQL setup                            [DONE]
  |  Fund & scheme tables                        [DONE]
  |  Entity management                           [DONE]
  |  User auth + RBAC                            [DONE]
  |  Doc Vault                                   [DONE]
  |  LP Portal                                   [DONE]
  |  Notifications                               [DONE]
  |  Audit Log (full integration)                [DONE]
  |
  |  WHY FIRST: Everything references funds + users.
  |  Nothing else works without auth + fund structure.

PHASE 2: Portfolio Monitoring Database (Module 3)  [DONE]
  |  PortfolioSnapshot + PortfolioNode tables     [DONE]
  |  portfolio.json imported to PostgreSQL         [DONE]
  |  service.py reads from PostgreSQL (JSON fallback) [DONE]
  |  builder.py writes to DB via save_portfolio_to_db() [DONE]
  |  MIS parser writes to DB instead of JSON file [DONE]
  |  KPI comparison, charts, chatbot — unchanged  [DONE — verified]
  |  All nav pages connected with auth             [DONE]
  |  investments app: 7 models, 18+ endpoints      [DONE]
  |    - Investment + InvestmentTranche (6 endpoints)  [DONE]
  |    - Valuation with approval workflow (4 endpoints) [DONE]
  |    - Founder Portal / KPI tracking (5 endpoints)   [DONE]
  |    - Exit scenarios (2 endpoints)                  [DONE]
  |    - Board meetings (2 endpoints)                  [DONE]
  |    - Board pack generation (1 endpoint)            [DONE]
  |  investments.html + investments.js frontend     [DONE]
  |  Nav updated on all 5 pages                    [DONE]
  |
  |  Our dashboard is now Module 3 of TrackFundAI.

PHASE 3: LP Management (Module 2)
  |  Investor onboarding + KYC
  |  Commitment tracking
  |  Capital call generation + notices
  |  Distribution processing (with TDS calculation)
  |  LP capital accounts (running ledger)

PHASE 4: Fund Accounting (Module 4)
  |  NAV computation engine
  |  General ledger (double-entry)
  |  Management fee calculator
  |  Carried interest waterfall
  |  Depository reconciliation (CDSL/NSDL)

PHASE 5: SEBI Compliance (Module 5)
  |  QAR/AAR report generation
  |  CTR checklist management
  |  AML due diligence workflow
  |  Compliance calendar + alerts
  |  PPM amendment tracking

PHASE 6: Documents & LP Portal Expansion (Module 7)
  |  Document vault with advanced access control
  |  LP-facing portal (read-only dashboard)
  |  Notification engine (email + in-app)
  |  Capital call / distribution notice delivery

PHASE 7: Global Expansion
  |  GIFT City IFSC compliance
  |  UAE / ADGM fund structures
  |  Singapore VCC structures
  |  Multi-currency support
```

---

## Complete API Map — All Endpoints (~107 total)

### Phase 1 — Foundation (~15 endpoints)

```
AUTH
  POST   /api/auth/login/                     -> JWT token pair
  POST   /api/auth/logout/                    -> invalidate token
  POST   /api/auth/refresh/                   -> refresh JWT
  GET    /api/auth/me/                        -> current user profile
  PUT    /api/auth/me/                        -> update profile
  POST   /api/auth/change-password/           -> change password

FUND ADMINISTRATION
  GET    /api/funds/                           -> list funds (filtered by org)
  POST   /api/funds/                           -> create fund
  GET    /api/funds/{id}/                      -> fund detail
  PUT    /api/funds/{id}/                      -> update fund
  DELETE /api/funds/{id}/                      -> delete fund
  GET    /api/funds/{id}/schemes/              -> list schemes
  POST   /api/funds/{id}/schemes/              -> create scheme
  GET    /api/funds/schemes/{id}/              -> scheme detail
  PUT    /api/funds/schemes/{id}/              -> update scheme
  DELETE /api/funds/schemes/{id}/              -> delete scheme
  GET    /api/funds/{id}/entities/             -> list entities
  POST   /api/funds/{id}/entities/             -> add entity
  PUT    /api/funds/entities/{id}/             -> update entity
  DELETE /api/funds/entities/{id}/             -> delete entity

DOCUMENT VAULT
  GET    /api/documents/                       -> list documents (filtered)
  POST   /api/documents/upload/                -> upload document
  GET    /api/documents/{id}/                  -> document detail
  GET    /api/documents/{id}/download/         -> download file
  DELETE /api/documents/{id}/                  -> delete document
  GET    /api/documents/{id}/access-log/       -> who viewed/downloaded

NOTIFICATIONS
  GET    /api/notifications/                   -> user's notifications
  PUT    /api/notifications/{id}/read/         -> mark as read
  POST   /api/notifications/mark-all-read/     -> mark all read
  GET    /api/notifications/unread-count/       -> badge count

AUDIT LOG
  GET    /api/audit-log/                       -> filtered audit trail (admin only)
```

### Phase 2 — Portfolio Monitoring DB Migration (~18 endpoints)

```
INVESTMENTS
  GET    /api/schemes/{id}/investments/        -> list investments under scheme
  POST   /api/schemes/{id}/investments/        -> create investment
  GET    /api/investments/{id}/                -> investment detail
  PUT    /api/investments/{id}/                -> update investment
  GET    /api/investments/{id}/tranches/       -> list tranches
  POST   /api/investments/{id}/tranches/       -> add tranche

VALUATIONS
  GET    /api/investments/{id}/valuations/     -> valuation history
  POST   /api/investments/{id}/valuations/     -> submit valuation
  PUT    /api/valuations/{id}/                 -> update valuation
  POST   /api/valuations/{id}/approve/         -> approve valuation

FOUNDER PORTAL
  GET    /api/founder/companies/               -> founder's companies
  POST   /api/founder/companies/{id}/submit-kpi/  -> submit monthly KPIs
  GET    /api/founder/companies/{id}/kpi-history/  -> KPI history
  GET    /api/investments/{id}/kpis/           -> KPI submissions (GP view)
  PUT    /api/kpis/{id}/review/                -> review/approve KPI

EXIT SCENARIOS
  GET    /api/investments/{id}/exit-scenarios/  -> list scenarios
  POST   /api/investments/{id}/exit-scenarios/  -> model new scenario

BOARD PACKS
  POST   /api/schemes/{id}/board-pack/generate/ -> auto-generate board pack PDF

EXISTING (PRESERVED — zero changes)
  GET    /api/portfolio/                        -> hierarchical root
  GET    /api/portfolio/node/{id}/              -> node detail
  GET    /api/portfolio/compare/                -> comparison engine
  POST   /api/portfolio/chat/                   -> AI chatbot
  POST   /api/portfolio/reload/                 -> reload data
```

### Phase 3 — LP Management (~22 endpoints)

```
INVESTORS
  GET    /api/investors/                        -> LP directory
  POST   /api/investors/                        -> onboard new LP
  GET    /api/investors/{id}/                   -> LP profile
  PUT    /api/investors/{id}/                   -> update LP
  POST   /api/investors/{id}/verify-kyc/        -> trigger KYC verification
  POST   /api/investors/{id}/verify-bank/       -> penny drop verification
  GET    /api/investors/{id}/capital-account/    -> capital account statement

COMMITMENTS
  GET    /api/schemes/{id}/commitments/         -> list commitments
  POST   /api/schemes/{id}/commitments/         -> add commitment
  PUT    /api/commitments/{id}/                 -> update commitment

CAPITAL CALLS
  GET    /api/schemes/{id}/capital-calls/       -> list capital calls
  POST   /api/schemes/{id}/capital-calls/       -> initiate capital call
  GET    /api/capital-calls/{id}/               -> call detail + line items
  POST   /api/capital-calls/{id}/send-notices/  -> send WhatsApp + email
  POST   /api/capital-calls/{id}/match-utr/     -> UTR reconciliation
  PUT    /api/capital-call-items/{id}/          -> update line item

DISTRIBUTIONS
  GET    /api/schemes/{id}/distributions/       -> list distributions
  POST   /api/schemes/{id}/distributions/       -> create distribution
  GET    /api/distributions/{id}/               -> distribution detail
  POST   /api/distributions/{id}/process/       -> process payments

UNIT ALLOTMENT
  POST   /api/schemes/{id}/allot-units/         -> allot units at NAV

LP PORTAL
  GET    /api/lp/dashboard/                     -> IRR, TVPI, DPI, RVPI, MOIC
  GET    /api/lp/capital-account/               -> statement (PDF download)
  GET    /api/lp/documents/                     -> document vault
  GET    /api/lp/notifications/                 -> notification history

WATERFALL SIMULATOR
  POST   /api/schemes/{id}/waterfall/simulate/  -> interactive slider simulation
```

### Phase 4 — Fund Accounting (~16 endpoints)

```
NAV
  GET    /api/schemes/{id}/nav/                 -> NAV history
  POST   /api/schemes/{id}/nav/calculate/       -> trigger NAV calculation
  POST   /api/schemes/{id}/nav/reconcile/       -> CDSL/NSDL reconciliation

GENERAL LEDGER
  GET    /api/schemes/{id}/ledger/              -> chart of accounts
  POST   /api/schemes/{id}/ledger/              -> create account
  GET    /api/schemes/{id}/journal-entries/      -> list journal entries
  POST   /api/schemes/{id}/journal-entries/      -> create journal entry
  GET    /api/schemes/{id}/trial-balance/        -> trial balance report

MANAGEMENT FEES
  GET    /api/schemes/{id}/management-fees/     -> fee history
  POST   /api/schemes/{id}/management-fees/calculate/  -> auto-calculate fee + GST

WATERFALL
  GET    /api/schemes/{id}/waterfall/           -> waterfall calculation history
  POST   /api/schemes/{id}/waterfall/calculate/ -> run waterfall

TALLY SYNC
  POST   /api/schemes/{id}/tally/import/        -> import trial balance from Tally
  POST   /api/schemes/{id}/tally/export/        -> export journal entries to Tally

FINANCIAL STATEMENTS
  GET    /api/schemes/{id}/financials/bs/       -> balance sheet
  GET    /api/schemes/{id}/financials/is/       -> income statement
  GET    /api/schemes/{id}/financials/cf/       -> cash flow statement
```

### Phase 5 — SEBI Compliance (~18 endpoints)

```
CALENDAR
  GET    /api/compliance/calendar/              -> all upcoming deadlines
  POST   /api/compliance/calendar/              -> add custom deadline
  PUT    /api/compliance/calendar/{id}/         -> update status

QAR
  GET    /api/compliance/qar/                   -> QAR submission history
  POST   /api/compliance/qar/generate/          -> auto-generate QAR
  PUT    /api/compliance/qar/{id}/              -> edit draft
  POST   /api/compliance/qar/{id}/submit/       -> submit to SI Portal

AAR
  GET    /api/compliance/aar/                   -> AAR history
  POST   /api/compliance/aar/generate/          -> auto-generate AAR
  PUT    /api/compliance/aar/{id}/              -> edit draft
  POST   /api/compliance/aar/{id}/ca-review/    -> route to CA
  POST   /api/compliance/aar/{id}/submit/       -> submit to SI Portal

CTR
  GET    /api/compliance/ctr/                   -> CTR checklists
  POST   /api/compliance/ctr/                   -> create CTR for fund/year
  GET    /api/compliance/ctr/{id}/items/        -> list checklist items
  PUT    /api/compliance/ctr/items/{id}/        -> update item status

AML
  GET    /api/compliance/aml/                   -> AML records
  GET    /api/compliance/aml/alerts/            -> flagged items
  POST   /api/compliance/aml/screen/            -> screen investor
  POST   /api/compliance/aml/{id}/notify-custodian/ -> send custodian alert

SEBI CIRCULARS
  GET    /api/compliance/circulars/             -> parsed circulars
  POST   /api/compliance/circulars/parse/       -> parse new circular (AI)
  GET    /api/compliance/circulars/{id}/actions/ -> fund-specific action items
  PUT    /api/compliance/circular-actions/{id}/ -> update action status
```

### Phase 6 — Documents & Notifications Expansion (~12 endpoints)

```
DOCUMENTS (expansion of Phase 1 vault)
  POST   /api/documents/{id}/watermark/         -> generate watermarked PDF

NOTIFICATIONS (expansion)
  POST   /api/notifications/send/               -> send notification
  POST   /api/notifications/send-bulk/          -> bulk send (capital call notices)
  GET    /api/notifications/templates/          -> message templates

PDF GENERATION
  POST   /api/documents/generate/capital-call-notice/  -> call notice PDF
  POST   /api/documents/generate/distribution-notice/  -> distribution notice
  POST   /api/documents/generate/lp-statement/         -> LP capital account PDF
  POST   /api/documents/generate/lp-letter/            -> AI-generated LP letter
```

---

## Endpoint Count Summary

| Phase | Scope | Endpoints |
|-------|-------|-----------|
| Phase 1 | Auth + Fund Admin + Doc Vault + Notifications + Audit Log | ~30 |
| Phase 2 | Investments + Valuations + Founder Portal | ~18 |
| Phase 3 | LPs + Capital Calls + Distributions + LP Portal | ~22 |
| Phase 4 | NAV + Ledger + Waterfall + Tally | ~16 |
| Phase 5 | QAR + AAR + CTR + AML + Circulars | ~18 |
| Phase 6 | Document expansion + PDF Gen + Bulk Notifications | ~12 |
| **Total** | | **~107 + 6 existing = ~113** |

---

## Django App Structure

```
backend/
  config/          # settings, urls, wsgi
  accounts/        # User, Organization, FundAccess, AuditLog
  funds/           # Fund, Scheme, Entity
  documents/       # Document, DocumentAccess (Phase 1)
  notifications/   # Notification, NotificationPreference (Phase 1)
  api/             # Existing portfolio endpoints (Phase 0 — preserved)
  investments/     # Investment, Tranche, Valuation, KPIDefinition, PortfolioKPI, ExitEvent, BoardMeeting (Phase 2) [DONE]
  lp/              # Investor, Commitment, CapitalCall, Distribution (Phase 3)
  accounting/      # NAV, Ledger, Fees, Waterfall (Phase 4)
  compliance/      # QAR, AAR, CTR, AML, Calendar (Phase 5)

frontend/
  index.html       # Portfolio dashboard (Phase 0)
  login.html       # JWT login page (Phase 1)
  fund-admin.html  # Fund administration (Phase 1)
  investments.html # Investments, valuations, KPIs, exits, board meetings (Phase 2) [DONE]
  doc-vault.html   # Document vault (Phase 1)
  lp-portal.html   # LP portal (Phase 1)
  styles.css       # Global design system
  auth.js          # JWT utilities
  fund-admin.js    # Fund admin logic
  doc-vault.js     # Document vault logic
  lp-portal.js     # LP portal logic
```

---

## Key Architecture Decisions

1. **No frontend framework** — Vanilla JS + Chart.js. Fast, no build step, simple deployment.
2. **JWT auth** — 2h access tokens, 7d refresh tokens with auto-rotation.
3. **Multi-tenancy** — Organization-based data isolation via lazy middleware descriptor.
4. **Backward compatibility** — Existing portfolio endpoints use `@permission_classes([AllowAny])`.
5. **PostgreSQL-ready** — `DATABASE_URL` env var switches from SQLite to Postgres.
6. **SEBI-native** — Fund categories, GIFT City, QAR/AAR/CTR built into the data model.
7. **service.py migration** — When Phase 2 rewires the dashboard, `service.py` reads from DB but returns the exact same JSON shape so frontend JS is untouched.
