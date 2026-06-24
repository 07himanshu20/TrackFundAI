# TrackFundAI — Universal Priority Matrix

**Status:** DRAFT — awaiting user approval before Phase 3 (Flavor A + Flavor B) implementation.
**Version:** 1.0
**Last updated:** 2026-06-22

This document is the **single source of truth** for how every dashboard-visible value is selected when multiple sources disagree. It is consumed by:

1. **Phase 3 `reconciler.py`** — picks the authoritative value per field after Layers 1/2/3 return their results.
2. **Phase 3 `cross_layer_validator.py`** — flags discrepancies that exceed tolerance and records them in `FundMetric.provenance.disagreements[]`.
3. **Frontend side panel** (`openProvenancePanel`) — displays *which* priority rule was applied for the value the user is looking at, alongside the formula / source cell that produced it.

---

## The Seven Universal Principles

These principles are **structural, not file-specific**. Every Indian AIF workbook — regardless of GP, vintage, sector, or template — can be reasoned about in these terms.

| Principle | Rule | Plain English |
|---|---|---|
| **P1** | Dedicated source > Summary source | A row in a dedicated sheet (e.g., `NAVRecord` walk, per-LP capital account) is more authoritative than the same number summarised on a Cover/Overview tab. |
| **P2** | Time-series row > Snapshot cell | A value computed per period inside a ledger (NAV walk, capital call register, distribution ledger) is more authoritative than a single "as-of" cell on a summary sheet. |
| **P3** | Row sum > Aggregate cell | If a sheet shows both per-row line items and an aggregate cell, the **row sum** is authoritative. The aggregate is only used when the row detail is missing. |
| **P4** | Audited > Stated > Projected | Numbers tagged as audited/approved override numbers tagged as stated/draft, which override projected/budgeted figures. |
| **P5** | Identifier match > Label match | If a record can be matched by a strong identifier (CIN, PAN, SEBI Reg, ISIN), prefer that match over a fuzzy text-label match. |
| **P6** | Latest period > Historical period | For point-in-time scalars (NAV, AUM, total committed), the latest-dated row wins. Historical rows are kept for trend display but not for the "current value" tile. |
| **P7** | Component identity > Summary identity | Aggregate fields must equal the sum of their components (`total_X == Σ X[]`). When the workbook violates this identity, prefer the component sum and record the discrepancy. |

**Provenance hierarchy in `inputs_used` JSON:**
- `provenance_kind = 'extracted'` — read directly from a cell
- `provenance_kind = 'computed_from_db'` — Python sum/aggregate of DB rows
- `provenance_kind = 'computed_from_canonical_formula'` — applied a hard-coded textbook formula (e.g., DPI = distributions / called)
- `provenance_kind = 'computed_by_gemini'` — Gemini selected a candidate formula; Python evaluated using catalogue values only

**Conflict tolerance bands** (default; override per-field where noted):
- **Currency / amount fields:** ±1% OR ±₹0.10 Cr (whichever is larger)
- **Percent fields:** ±0.5 percentage points
- **Multiple fields (MOIC/TVPI/DPI/RVPI):** ±0.05x
- **Count fields:** exact match required (no tolerance)
- **Date fields:** exact match required

When Layer 1 / Layer 2 / Layer 3 produce values that differ beyond tolerance, the reconciler picks the higher-priority source and writes both values into `FundMetric.provenance.disagreements[]` so the side panel can show ⚠️.

---

## Side-Panel Display Contract

For every metric tile the user can click, the side panel must show four blocks in this order:

1. **The Number** — the value the user sees, formatted with unit.
2. **Priority Rule Applied** — e.g., *"Priority 1 satisfied: read from the latest `NAVRecord` row (dated 2026-03-31)."* If a lower-priority source had to be used because higher-priority sources were unavailable, list which higher priorities were absent and why.
3. **How we got it** — either:
   - **Extracted:** sheet name + cell reference (e.g., `'Cover' tab, cell B12`)
   - **Computed:** formula → substituted values → result (e.g., `(LP Distributions + Fund NAV) ÷ Called Capital = (210.50 + 1450.30) ÷ 1247.00 = 1.332x`)
4. **Reconciliation status** — if any other layer/source produced a different value within tolerance, list them with their values (informational). If outside tolerance, mark ⚠️ and explain why this source won.

This contract is binding on `_METRIC_COPY` + `openProvenancePanel` for the **8 user-specified tiles** (`net_irr`, `tvpi`, `active_fair_value`, `moic`, `carry_base`, `carry_amount_gross`, `carry_amount_net`, `gp_clawback_provision`). It is also the recommended pattern for all other tiles that grow click handlers in the future.

---

## Field Numbering Convention

- **IDN-xxx** — Identity & Master
- **SCH-xxx** — Scheme Terms
- **CAP-xxx** — Capital (committed/called/uncalled)
- **DST-xxx** — Distributions
- **PRF-xxx** — Performance (IRR, MOIC, TVPI, etc.)
- **NAV-xxx** — NAV & Units
- **INV-xxx** — Portfolio Investments
- **VAL-xxx** — Valuations
- **EXT-xxx** — Exits
- **KPI-xxx** — Portfolio KPIs
- **FIN-xxx** — Company Financials (burn/runway/P&L)
- **WTF-xxx** — Waterfall & Carry
- **FEE-xxx** — Fees
- **LPA-xxx** — LP-level Capital Account fields
- **ENT-xxx** — Service Entities
- **CMP-xxx** — Compliance

---

## A. Identity & Master Data

### IDN-001 — fund_name
**Type:** Scalar text · **Dashboard:** Header on every page · **DB:** `Fund.name`
**Principles:** P1 (dedicated > summary), P5 (identifier match)
**Priority sources (highest → lowest):**
1. Layer 1 `fund_master.fund_name` extracted from dedicated Fund Identity / Cover sheet (cell labelled "Fund Name" / "Scheme Name" / "Name of the Fund")
2. Sheet-tab text matching SEBI Reg patterns
3. PPM / Trust Deed metadata field if present
**Universality:** Every AIF has a single legal name on its SEBI registration certificate; that name appears verbatim on the Cover sheet of any institutional template.
**Tolerance:** Exact string match required (case-insensitive comparison; preserve original casing for display).
**Side-panel display when clicked:** `Priority 1: read from "Cover" sheet, cell B3 (label "Fund Name").`

### IDN-002 — sebi_registration_number
**Type:** Scalar text · **DB:** `Fund.sebi_registration_number`
**Principles:** P1, P5 (the identifier IS the field)
**Priority sources:**
1. Layer 1 `fund_master.sebi_registration_number` (regex-matched cell against `IN/AIF[123]/\d{2}-\d{2}/\d{7}`)
2. Footer / cover stamp text containing same regex on any sheet
**Universality:** SEBI Reg No format is mandated by SEBI Master Circular for AIFs — universal across all Indian AIFs.

### IDN-003 — fund_category
**Type:** Enum (cat_i / cat_ii / cat_iii) · **DB:** `Fund.fund_category` (FK to `FundCategory`)
**Principles:** P1, P5
**Priority sources:**
1. Layer 1 `fund_master.category` extracted from Cover/PPM label
2. Inferred from SEBI Reg No segment (`AIF1` → Cat I, `AIF2` → Cat II, `AIF3` → Cat III)
**Conflict handling:** If extracted category contradicts SEBI Reg segment, prefer SEBI Reg (it's identifier-derived; P5 wins over label).

### IDN-004 — structure_type
**Type:** Enum (trust/llp/company) · **DB:** `Fund.structure_type`
**Priority sources:**
1. Layer 1 `fund_master.structure_type` from PPM/Trust Deed text
2. Inferred from entity-suffix in fund name ("Trust" → trust, "LLP" → llp)
**Universality:** Every Indian AIF is one of three structures; SEBI doesn't permit others.

### IDN-005 — vintage_year
**Type:** Integer (year) · **DB:** `Scheme.vintage_year`
**Principles:** P1, P6
**Priority sources:**
1. Layer 1 `fund_master.vintage_year` from dedicated cell
2. `YEAR(first_close_date)` if first close is present
3. `YEAR(inception_date)` from `Fund.inception_date`
**Universality:** "Vintage" is universally defined as the year of first capital call OR first close; both are auditable events.

### IDN-006 — first_close_date · IDN-007 — final_close_date
**Type:** Date · **DB:** `Scheme.first_close_date`, `Scheme.final_close_date`
**Principles:** P1, P5 (matched by close-type label)
**Priority sources:**
1. Layer 1 `fund_master.first_close_date` / `final_close_date` (extracted from dedicated cell)
2. MIN(`Commitment.commitment_date` WHERE close_type='first_close') / MAX(close_type='final_close') from LP commitment ledger
3. First / last capital-call date as proxy (if commitments not parsed)
**Tolerance:** Exact date required; if Layer 1 cell value disagrees with commitment ledger by >1 day, prefer commitment ledger (P3: row sum logic — many LPs > one summary cell).

### IDN-008 — tenure_years
**Type:** Integer · **DB:** `Scheme.tenure_years`
**Priority sources:**
1. Layer 1 `fund_master.tenure_years` extracted from PPM-stated tenure
2. Computed: `(dissolution_date - first_close_date).years` if both present
**Universality:** PPM-stated tenure is the legal life; computed tenure is the realised life. Both are valid; prefer stated for "fund design", computed for "actual remaining life".

### IDN-009 — manager_entity (and sponsor/trustee/custodian/auditor)
**Type:** FK → `Entity` · **DB:** `Fund.manager_entity` (and the four siblings)
**Principles:** P1, P5 (PAN/SEBI Reg matching)
**Priority sources:**
1. Layer 1 `entities[]` block where `entity_type` matches the slot, joined by PAN/SEBI Reg if present
2. Joined by exact-name match (case-insensitive)
3. Joined by fuzzy-name match (Levenshtein ≥ 0.9)
**Universality:** Every AIF has exactly one of each of these 5 service entities — required by SEBI.

### IDN-010 — base_currency
**Type:** Enum (INR/USD/etc.) · **DB:** `Fund.base_currency`
**Priority sources:**
1. Layer 1 `fund_master.base_currency` cell
2. Inferred from currency symbol presence on hero amounts (₹ → INR, $ → USD)
3. Default `INR` for non-GIFT-City funds; `USD` for GIFT City
**Universality:** Domestic AIFs are INR by SEBI rule; GIFT-City AIFs may be USD/EUR.

---

## B. Scheme Terms

### SCH-001 — scheme_size (target corpus)
**Type:** Decimal (Cr) · **DB:** `Scheme.scheme_size`
**Principles:** P1, P3 (row sum > aggregate)
**Priority sources:**
1. Layer 1 `fund_master.scheme_size` from PPM
2. SUM(`Commitment.commitment_amount`) across all LPs (drawn vs target reconciliation)
**Note:** Scheme size is the **target**, not the raised amount. If LP commitments exceed scheme size (over-subscription), record both but use scheme_size for the displayed "Target Corpus".

### SCH-002 — hurdle_rate_pct
**Type:** Percent · **DB:** `Scheme.hurdle_rate_pct`
**Principles:** P1
**Priority sources:**
1. Layer 1 `fund_master.hurdle_rate_pct` from PPM/LPA terms cell
2. Implied from waterfall walk if preferred return ÷ called capital pattern is consistent across periods
**Tolerance:** Exact match required when stated in PPM (it's a contractual rate, not a measurement).

### SCH-003 — carry_pct
**Type:** Percent (GP share) · **DB:** `Scheme.carry_pct`
**Priority sources:**
1. Layer 1 `fund_master.carry_pct` from LPA
2. Implied from `CarriedInterest.carry_amount_gross ÷ carry_base` if consistent across multiple periods
**Universality:** Standard AIF carry is 20%; some funds use 15% or 25%. Always stated in LPA.

### SCH-004 — carry_type
**Type:** Enum (european/american) · **DB:** `Scheme.carry_type`
**Priority sources:**
1. Layer 1 `fund_master.carry_type` (PPM cell or label "Whole Fund" → european, "Deal-by-Deal" → american)
2. Inferred from waterfall structure: if carry computed per-deal in the ledger → american; if computed on fund total → european
**Universality:** SEBI permits both; PPM mandates disclosure.

### SCH-005 — management_fee_pct
**Type:** Percent · **DB:** `Scheme.management_fee_pct`
**Priority sources:**
1. Layer 1 `fund_master.management_fee_pct`
2. Implied from `ManagementFeeSchedule.fee_amount ÷ fee_basis_amount` averaged across periods
**Tolerance:** 0.05 pp (some funds step down post-investment-period; record both rates if multiple regimes detected).

### SCH-006 — management_fee_basis
**Type:** Enum (committed/called/nav) · **DB:** `Scheme.management_fee_basis`
**Priority sources:**
1. Layer 1 cell
2. Inferred: if `fee_basis_amount` ≈ `total_committed_capital` per period → committed; if ≈ `total_called_capital` → called; if ≈ `fund_nav` → nav
**Universality:** SEBI requires disclosure of basis in PPM.

### SCH-007 — sponsor_commitment_pct
**Type:** Percent · **DB:** `Scheme.sponsor_commitment_pct`
**Priority sources:**
1. Layer 1 cell from PPM
2. Computed: `(sponsor commitment amount ÷ total commitment)` if sponsor LP record is identifiable
**Universality:** SEBI minimum is 2.5% for Cat I/II and 5% for Cat III — always present.

### SCH-008 — scheme_status
**Type:** Enum (fundraising/investing/harvesting/dissolved) · **DB:** `Scheme.scheme_status`
**Priority sources:**
1. Layer 1 cell
2. Inferred from lifecycle dates: pre-final-close → fundraising; post-final-close & pre-end-of-investment-period → investing; later → harvesting; post-dissolution-date → dissolved.

---

## C. Capital (Commitment, Called, Uncalled)

### CAP-001 — total_committed_capital
**Type:** Decimal (Cr) · **Dashboard:** LP page hero, Overview "Committed" tile · **DB:** `FundMetric('committed_capital')`
**Principles:** P3 (row sum > aggregate), P1
**Priority sources:**
1. SUM(`Commitment.commitment_amount`) across all LPs of the scheme  ← **AUTHORITATIVE**
2. Layer 1 `fund_performance.total_committed_capital` (extracted summary cell)
3. Scheme target (only as fallback if no LP commitment ledger present)
**Reasoning:** The LP commitment ledger is the legal record of who committed how much. Any summary cell is a hand-typed roll-up.
**Conflict:** If P1 disagrees with P2 by >1%, P1 wins. Side-panel ⚠️ shows both values.

### CAP-002 — total_called_capital
**Type:** Decimal (Cr) · **DB:** `FundMetric('called_capital')`
**Principles:** P3, P1
**Priority sources:**
1. SUM(`CapitalCallLineItem.called_amount` WHERE payment_status IN ('paid','partial')) — actual drawdown
2. SUM(`CapitalCallLineItem.called_amount`) regardless of payment_status — issued drawdown
3. Layer 1 `fund_performance.total_called_capital` summary cell
4. Latest `LPCapitalAccount.called_capital` SUM across LPs (alternative ledger)
**Note:** Choice between (1) and (2) depends on definition. Default to (2) ("called") because dashboards usually mean "called for the fund" not "received in bank". Surface (1) as `cash_received_capital` if needed.

### CAP-003 — total_uncalled_capital
**Type:** Decimal (Cr)
**Priority sources:**
1. **Computed:** `CAP-001 − CAP-002` (definitional identity, always preferred)
2. Layer 1 extracted summary cell (only as cross-check)
**Universality:** Mathematical identity; never override with stated value.

### CAP-004 — drawdown_pct
**Type:** Percent
**Priority sources:**
1. **Computed:** `CAP-002 / CAP-001`
2. Layer 1 extracted cell (cross-check)

### CAP-005 — sponsor_commitment_amount
**Type:** Decimal (Cr)
**Priority sources:**
1. `Commitment.commitment_amount` of the LP flagged as sponsor (matched via investor name = sponsor entity name OR `is_sponsor` flag)
2. `SCH-007 × CAP-001`

### CAP-006 — per_lp_commitment_amount (table row)
**Type:** Decimal (Cr) per LP row
**Principles:** P5 (identifier match), P3
**Priority sources:**
1. `Commitment.commitment_amount` row matched to LP by PAN
2. Matched by exact-name (case-insensitive)
3. Matched by fuzzy-name (≥0.9)
4. From Layer 2 `investors[].commitment` if dedicated commitment ledger absent
**Tolerance:** Exact match per LP (no aggregation tolerance — each row is its own field).

---

## D. Distributions

### DST-001 — total_distributions (to LPs)
**Type:** Decimal (Cr) · **DB:** `FundMetric('total_distributions')`
**Principles:** P3, P7 (component identity)
**Priority sources:**
1. SUM(`DistributionLineItem.net_amount`) — per-LP per-distribution ledger SUM  ← **AUTHORITATIVE**
2. SUM(`Distribution.total_net_amount`) — header-level SUM
3. Layer 1 `fund_performance.total_distributions` (extracted cell)
**Conflict:** If (1) and (2) differ by >1%, log P7 violation (header ≠ sum-of-lines) and prefer (1). If (1)/(2) disagree with (3), log and prefer (1)/(2).
**Zero handling (Rule 34):** A zero is valid. If no distribution rows exist AND no summary cell shows non-zero, the fund has not distributed — do not inflate.

### DST-002 — return_of_capital_amount
**Type:** Decimal (Cr) · **DB:** `FundMetric('return_of_capital_amount')`
**Principles:** P3, P5 (matched by `distribution_type`)
**Priority sources:**
1. SUM(`DistributionLineItem.net_amount` WHERE parent `Distribution.distribution_type` IN ('return_of_capital','stcg','ltcg'))
2. Layer 1 `fund_performance.return_of_capital_amount`
**Note:** STCG/LTCG count as capital distributions per ILPA convention (used for DPI calculation).

### DST-003 — income_distribution_amount
**Priority sources:**
1. SUM(`DistributionLineItem.net_amount` WHERE distribution_type IN ('interest','dividend'))
2. Layer 1 extracted cell
**Note:** Income distributions do NOT count toward DPI (capital-only metric).

### DST-004 — profit_share_amount (carry distribution to LPs net of GP carry)
**Priority sources:**
1. SUM(`DistributionLineItem.net_amount` WHERE distribution_type='carry' AND payee is LP)
2. Layer 1 extracted cell

### DST-005 — total_tds_amount
**Priority sources:**
1. SUM(`DistributionLineItem.tds_amount`) — per-line
2. SUM(`Distribution.total_tds_amount`) — header
3. Layer 1 extracted cell

### DST-006 — distribution_date (per row)
**Type:** Date per distribution event
**Priority sources:**
1. `Distribution.distribution_date`
2. Layer 1 `distributions[].date`

---

## E. Performance Metrics

### PRF-001 — net_irr  ★ PRIORITY TILE
**Type:** Percent (annualised) · **Dashboard:** Overview hero, AI Analytics tile · **DB:** `FundMetric('net_irr')`
**Principles:** P1, P4 (audited > stated), P6
**Priority sources:**
1. Layer 1 `fund_performance.net_irr` from dedicated Fund Performance sheet, latest period, **only if** marked as audited/approved (`status='approved'`)
2. Layer 1 same cell unaudited
3. **Computed via XIRR** over LP cashflows: capital calls (out) + distributions (in) + ending NAV (terminal positive)
4. SUM-weighted average of `LPCapitalAccount.irr` across all LPs at latest `as_of_date`
**Conflict tolerance:** ±0.5 pp between sources (IRR is inherently sensitive to timing).
**Reasoning:** Net IRR is the most-quoted fund metric and the most-disputed because it depends on convention (cashflow basis, day-count, gross-of-fees vs net-of-fees). Always prefer the audited figure when present. When computing, use ACT/365 day-count and treat NAV as a positive terminal cashflow on the as-of date.
**Side-panel display:** `Priority 1 satisfied: extracted from 'Fund Performance' sheet, cell D17, audited by [Auditor] on [date].`
OR
`Priority 3 satisfied: P1/P2 sources absent. Computed XIRR over 24 cashflows: 18 capital calls (-₹X total) + 5 distributions (+₹Y total) + ending NAV (+₹Z on 2026-03-31) → 15.8%.`

### PRF-002 — gross_irr
**Priority sources:**
1. Layer 1 `fund_performance.gross_irr` audited
2. Layer 1 unaudited
3. Computed XIRR using gross cashflows: capital invested at company level (out) + exit proceeds + valuation marks (in)
**Note:** Gross IRR ≠ Net IRR + fees offset; mechanism is structurally different (per-investment vs LP-level cashflows).

### PRF-003 — tvpi  ★ PRIORITY TILE
**Type:** Multiple (x) · **DB:** `FundMetric('tvpi')`
**Principles:** P7 (identity), P1
**Priority sources:**
1. **Computed via canonical identity:** `(DST-001 + NAV-001) ÷ CAP-002` — definitionally TVPI
2. Layer 1 `fund_performance.tvpi` extracted cell
3. SUM-weighted average of `LPCapitalAccount.tvpi`
**Conflict:** If (1) and (2) differ by >0.05x, prefer (1) and ⚠️ surface (2) as "GP-stated TVPI: X.XXx".
**Reasoning:** TVPI is a definitional ratio — only the formula (1) is non-disputable. Extracted cells can carry stale data; computed values are always current with DB state.

### PRF-004 — dpi
**Priority sources:**
1. **Computed:** `DST-002 ÷ CAP-002` (capital distributions only, per ILPA)
2. Layer 1 `fund_performance.dpi` extracted
**Conflict tolerance:** ±0.05x.

### PRF-005 — rvpi
**Priority sources:**
1. **Computed:** `NAV-001 ÷ CAP-002`
2. Layer 1 extracted
**Identity check:** `RVPI + DPI ≈ TVPI` (tolerance ±0.05x). If violated, side panel shows ⚠️.

### PRF-006 — moic (portfolio-level)  ★ PRIORITY TILE
**Type:** Multiple (x) · **DB:** `FundMetric('moic')`
**Principles:** P3, P7
**Priority sources:**
1. **Computed:** `PRF-008 ÷ PRF-009` (total FV ÷ total cost)
2. Layer 1 `fund_performance.portfolio_moic` extracted
3. Average of `Investment.fair_value ÷ Investment.total_invested` weighted by total_invested
**Conflict:** Source (1) is the authoritative ratio identity.

### PRF-007 — per_investment_moic
**Type:** Multiple per row · **DB:** `Investment` (computed at API time)
**Priority sources:**
1. **Computed:** `(latest Valuation.fair_value_of_holding + ExitEvent.proceeds) ÷ Investment.total_invested`
2. Layer 2 `portfolio_investments[].moic` extracted

### PRF-008 — active_fair_value (total FV of active holdings)  ★ PRIORITY TILE
**Type:** Decimal (Cr) · **DB:** `FundMetric('active_fair_value')`
**Principles:** P3 (row sum > aggregate), P7
**Priority sources:**
1. **Computed (DB sum):** SUM(`Valuation.fair_value_of_holding`) over latest valuation per investment WHERE `Investment.status` ∈ ('active','partially_exited')  ← **AUTHORITATIVE**
2. Layer 1 `fund_performance.fund_nav_latest` ← only as fallback (this is fund NAV, not pure holdings — differs by cash/fees)
3. Layer 2 `total_unrealised_fv_holding` aggregate from dedicated portfolio summary
**Conflict:** Source (1) is row-sum; sources (2)/(3) are aggregate cells. P3 says row-sum wins.
**Side-panel display:** `Priority 1: computed from DB. 4 active investments: A=₹150.50 + B=₹200.25 + C=₹89.00 + D=₹45.75 = ₹485.50 Cr.`

### PRF-009 — invested_cost (total cost of active holdings)
**Priority sources:**
1. **Computed:** SUM(`Investment.total_invested` WHERE status ∈ ('active','partially_exited'))
2. Layer 2 extracted `total_invested_cost` aggregate

### PRF-010 — unrealised_gain (active holdings)
**Priority sources:**
1. **Computed:** `PRF-008 − PRF-009`
2. Layer 1/2 extracted cell

### PRF-011 — per_investment_irr
**Type:** Percent per row · **DB:** `Investment.irr_pct`
**Priority sources:**
1. Layer 2 `portfolio_investments[].irr_pct` extracted from dedicated portfolio sheet
2. Computed XIRR from `InvestmentTranche.amount/date` (negative) + `ExitEvent.proceeds/date` (positive) + latest `Valuation.fair_value_of_holding` (positive terminal)
3. `ExitEvent.irr_on_exit` for fully-exited investments only

---

## F. NAV & Units

### NAV-001 — fund_nav (latest)
**Type:** Decimal (Cr) · **DB:** `FundMetric('fund_nav')` + `NAVRecord.total_nav`
**Principles:** P1, P2, P6
**Priority sources:**
1. `NAVRecord` row with MAX(`nav_date`) — latest entry in the NAV ledger walk  ← **AUTHORITATIVE**
2. Layer 1 `fund_performance.fund_nav_latest` extracted
3. **Computed:** `SUM(Valuation.fair_value_of_holding) + cash_and_equivalents - management_fee_payable - other_liabilities` from latest period
4. Any cell labelled "NAV" / "Net Asset Value" / "Closing NAV" on a summary sheet
**Tolerance:** ±1% between sources.
**Reasoning:** NAV walks are auditable ledgers reconciled against custodian/depository balances. The latest row IS the as-of NAV by definition. Computed-from-components is a derivation; the walk is the source.
**Universality:** Every institutional AIF maintains a NAV walk — SEBI requires quarterly NAV declaration.

### NAV-002 — nav_per_unit (latest)
**Priority sources:**
1. `NAVRecord.nav_per_unit` (latest row)
2. **Computed:** `NAV-001 ÷ NAV-003`
**Identity check:** `nav_per_unit × total_units == total_nav` (tolerance ±0.5%).

### NAV-003 — total_units_outstanding
**Priority sources:**
1. `NAVRecord.total_units_outstanding` (latest)
2. SUM(`Commitment.units_allocated`)
3. Layer 1 extracted cell

### NAV-004 — unrealized_gains (per NAV period)
**Priority sources:**
1. `NAVRecord.unrealized_gains`
2. Layer 1 `fund_performance.unrealized_gains` extracted

### NAV-005 — realized_gains (per NAV period)
**Priority sources:**
1. `NAVRecord.realized_gains`
2. Layer 1 `fund_performance.realized_gains` extracted

---

## G. Portfolio Investments (per-company)

### INV-001 — portfolio_company.name
**Type:** Text per row · **DB:** `PortfolioCompany.name`
**Principles:** P5
**Priority sources:**
1. Layer 2 `portfolio_investments[].company_name` from dedicated Portfolio sheet
2. Matched against existing `PortfolioCompany` by CIN > PAN > exact-name > fuzzy-name (≥0.9)
**De-duplication rule:** Within a single import, all rows referring to the same legal entity (matched by CIN/PAN) become ONE `PortfolioCompany` row with multiple `Investment` rows (one per scheme).

### INV-002 — cin (Corporate Identification Number)
**Priority sources:**
1. Layer 2 cell matching `[ULC]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}` regex
2. ROC lookup (out of scope for import)
**Universality:** Every Indian Pvt/Public company has a CIN; foreign cos do not.

### INV-003 — sector
**Type:** Enum-ish text · **DB:** `PortfolioCompany.sector`
**Principles:** P5
**Priority sources:**
1. Layer 2 `portfolio_investments[].sector` from the row's Sector column (NEVER inferred from sheet grouping)
2. Existing `PortfolioCompany.sector` if non-empty (preserved on idempotent re-import)
**Critical rule:** Per `CLAUDE.md` §6 — never apply loose-substring matching for "Sector" header (blocked when single-word length < 8 chars). Use exact-first matching.

### INV-004 — sub_sector
**Priority sources:**
1. Layer 2 `portfolio_investments[].sub_sector` cell
2. Inferred from sector + business description (no inference at import time; left null if missing)

### INV-005 — investment.total_invested
**Type:** Decimal (Cr) · **DB:** `Investment.total_invested`
**Principles:** P3, P7
**Priority sources:**
1. SUM(`InvestmentTranche.amount`) over all tranches of this Investment
2. Layer 2 `portfolio_investments[].total_invested` extracted
**Conflict:** P3 — row sum (tranches) wins; aggregate is fallback only.

### INV-006 — instrument_type
**Type:** Enum · **DB:** `Investment.instrument_type`
**Priority sources:**
1. Layer 2 cell mapped through Gemini enum classifier
2. Default to `equity` if absent and `ownership_pct` > 0

### INV-007 — ownership_pct
**Priority sources:**
1. Layer 2 `portfolio_investments[].ownership_pct` cell
2. Latest tranche's `ownership_pct`
**Note:** Two distinct fields exist: `ownership_pct` (per share-class) and `percentage_stake_fully_diluted` — extract separately when both present.

### INV-008 — investment_date
**Priority sources:**
1. MIN(`InvestmentTranche.date`) — first cheque date
2. Layer 2 `portfolio_investments[].investment_date`
**Tolerance:** Exact date.

### INV-009 — investment.status
**Type:** Enum (active/partially_exited/fully_exited/written_off) · **DB:** `Investment.status`
**Priority sources:**
1. Derived from ExitEvent presence: any `ExitEvent` with `is_actual=True` AND proceeds ≥ cost → fully_exited; partial exit → partially_exited; write-off event → written_off
2. Layer 2 `portfolio_investments[].status` cell

### INV-010 — is_quoted (listed status)
**Type:** Boolean · **DB:** `PortfolioCompany.is_quoted`
**Priority sources:**
1. Layer 2 cell mapped through Gemini quoted_status enum classifier
2. Presence of `listing_exchange` value implies True

### INV-011 — listing_exchange
**Type:** Text (NSE/BSE/etc.) · **DB:** `PortfolioCompany.listing_exchange`
**Priority sources:**
1. Layer 2 cell

### INV-012 — board_seat
**Type:** Boolean · **DB:** `Investment.board_seat`
**Priority sources:**
1. Layer 2 cell with bool conversion
2. Presence of `BoardMeeting` records for this Investment

---

## H. Valuations

### VAL-001 — valuation_date
**Priority sources:**
1. Layer 2 `valuations[].valuation_date` extracted column
2. Inferred from sheet header period (e.g., "31-Mar-2026 Valuations")
**Tolerance:** Quarter-end snapping permitted (if cell shows month, snap to month-end).

### VAL-002 — fair_value (per share / per unit)
**Type:** Decimal · **DB:** `Valuation.fair_value`
**Priority sources:**
1. Layer 2 `valuations[].fair_value` cell

### VAL-003 — fair_value_of_holding (fund's share value)
**Type:** Decimal (Cr) · **DB:** `Valuation.fair_value_of_holding`
**Principles:** P3, P7
**Priority sources:**
1. Layer 2 `valuations[].fair_value_of_holding` cell  ← **AUTHORITATIVE**
2. **Computed:** `VAL-002 × shares_held` from `InvestmentTranche.shares_acquired` SUM
3. **Computed:** `VAL-004 × INV-007` (enterprise_value × ownership_pct)
**Conflict:** Source (1) is the GP-stated fund-share value. If sum across all investments ≠ NAV portfolio-component by >1%, ⚠️ flag (P7 violation).

### VAL-004 — enterprise_value
**Priority sources:**
1. Layer 2 `valuations[].enterprise_value`
2. `VAL-002 × total_shares_outstanding` (rarely computable from workbook)

### VAL-005 — methodology
**Type:** Enum (dcf/comparables/recent_transaction/net_assets/cost/option_pricing) · **DB:** `Valuation.methodology`
**Priority sources:**
1. Layer 2 cell mapped through Gemini enum classifier (`valuation_methodology`)

### VAL-006 — ipev_level
**Type:** Integer (1/2/3) per IPEV/Ind AS 113 · **DB:** `Valuation.ipev_level`
**Priority sources:**
1. Layer 2 cell
2. Inferred from methodology: `cost` for recent investment → Level 3; `recent_transaction` → Level 2; `comparables` (listed peers) → Level 2; `dcf` / illiquid → Level 3; `quoted` → Level 1

### VAL-007 — dlom_pct (Discount for Lack of Marketability)
**Priority sources:**
1. Layer 2 cell
2. Default null (not always disclosed)

### VAL-008 — multiple (per valuation)
**Priority sources:**
1. Layer 2 cell
2. **Computed:** `VAL-003 ÷ cost_basis`

### VAL-009 — status (Valuation.status: draft/submitted/approved/rejected)
**Priority sources:**
1. Layer 2 cell
2. Default `approved` if extracted from a quarterly valuation report (those are typically signed off)

---

## I. Exits

### EXT-001 — exit_type
**Type:** Enum (ipo/merger_acquisition/secondary_sale/buyback/write_off) · **DB:** `ExitEvent.exit_type`
**Priority sources:**
1. Layer 2 cell mapped via Gemini enum classifier (`exit_type`)
2. Inferred: `write_off` if proceeds = 0 OR realized_gain_loss < 0 with full divestment; `ipo` if mentions listing/RHP/DRHP; default `secondary_sale` otherwise

### EXT-002 — exit_date
**Priority sources:**
1. Layer 2 `exits[].exit_date` cell
2. Latest `DistributionLineItem.payment_date` if distribution `related_exit_event` FK present

### EXT-003 — exit_valuation
**Priority sources:**
1. Layer 2 cell

### EXT-004 — proceeds (gross to fund)
**Type:** Decimal (Cr) · **DB:** `ExitEvent.proceeds`
**Priority sources:**
1. Layer 2 cell
2. SUM(`DistributionLineItem.gross_amount` WHERE `related_exit_event` matches)

### EXT-005 — net_exit_proceeds (after tax/fees)
**Priority sources:**
1. Layer 2 cell
2. `EXT-004 - associated TDS`

### EXT-006 — realized_gain_loss
**Priority sources:**
1. Layer 2 cell
2. **Computed:** `EXT-004 - Investment.total_invested × (ownership_pct exited)`

### EXT-007 — moic (exit)
**Priority sources:**
1. **Computed:** `EXT-004 ÷ cost basis of exited portion`
2. Layer 2 cell

### EXT-008 — irr_on_exit
**Priority sources:**
1. **Computed:** XIRR over investment cashflows ending with EXT-004 on EXT-002
2. Layer 2 cell

---

## J. Portfolio KPIs (per-company, per-period)

KPIs are sector-specific and stored in `PortfolioKPI`. The priority pattern is the same across all KPI keys (revenue, ebitda, gmv, mrr, arr, churn_pct, etc.); per-key entries below are condensed.

### KPI-COMMON-001 — per-period kpi.value
**Type:** Decimal · **DB:** `PortfolioKPI.value`
**Principles:** P4 (audited > stated > projected), P6
**Priority sources:**
1. Layer 3 `portfolio_kpis_periodic[]` row matched by (investment_id, period_end_date, kpi_key) where `source='excel_upload'` AND `status='approved'`
2. Same row with `status='submitted'`
3. Same row with `status='draft'`
4. Aggregated MIS sheet value where company name + period match
**Conflict:** Within same period, prefer most recent `reviewed_at` timestamp.

### KPI-001 to KPI-035 — individual KPI definitions
The 35 KPI types Phase 3 must recognise (universal across sectors, derived from `KPIDefinition.sector_template`):

| Slug | Format | Sector Template |
|---|---|---|
| revenue | currency | generic |
| ebitda | currency | generic |
| ebitda_pct | percent | generic |
| gross_margin_pct | percent | generic |
| pat | currency | generic |
| headcount | number | generic |
| gmv | currency | consumer |
| orders | number | consumer |
| aov | currency | consumer |
| returns_pct | percent | consumer |
| cac | currency | saas, consumer |
| repeat_pct | percent | consumer |
| mrr | currency | saas |
| arr | currency | saas |
| churn_pct | percent | saas |
| nrr_pct | percent | saas |
| ltv | currency | saas |
| ltv_cac | ratio | saas |
| burn_rate | currency | saas |
| runway | number | saas, generic |
| cost_to_income | percent | nbfc, banking |
| nim_pct | percent | nbfc, banking |
| gnpa_pct | percent | nbfc, banking |
| nnpa_pct | percent | nbfc, banking |
| car_pct | percent | nbfc, banking |
| aum | currency | nbfc |
| roe_pct | percent | generic |
| d_ebitda | ratio | manufacturing |
| capacity_pct | percent | manufacturing |
| export_pct | percent | manufacturing |
| bed_occupancy | percent | healthcare |
| arpob | currency | healthcare |
| cap_rate_pct | percent | realestate |
| fv | currency | generic |
| moic | ratio | generic |

For each: Priority 1 is dedicated per-company KPI sheet (Layer 3), Priority 2 is MIS aggregated tab, Priority 3 is summary card. Universal because every sector template defines these KPIs in a `KPIDefinition` row; the data exists wherever the GP collected it.

---

## K. Company Financials (Monthly/Quarterly)

### FIN-001 — gross_burn (monthly)
**Type:** Decimal · **DB:** `CompanyFinancials.gross_burn`
**Priority sources:**
1. Layer 3 `monthly_cf[]` row matched by (investment_id, period)
2. **Computed:** Total Opex per month (excluding D&A, finance cost) from monthly P&L

### FIN-002 — net_burn
**Priority sources:**
1. Layer 3 cell
2. **Computed:** `gross_burn − revenue` (per month)

### FIN-003 — cash_balance (period-end)
**Priority sources:**
1. Layer 3 `monthly_bs[].cash_and_equivalents`

### FIN-004 — runway_months
**Priority sources:**
1. Layer 3 cell
2. **Computed:** `FIN-003 ÷ FIN-002` (cash ÷ net burn)
**Note:** Runway = inf if `net_burn ≤ 0` (cash-positive month).

### FIN-005 — per-period P&L line items (revenue, cogs, gross_profit, opex_breakdown, ebitda, pat, etc.)
**Type:** Decimal per month per line · **DB:** `(if extended P&L model exists in monthly tables)`
**Principles:** P3, P7
**Priority sources:**
1. Layer 3 `monthly_pl[]` row, matched by (investment_id, period_end, line_item)
2. P&L line classified by Gemini against canonical `pl_line_items` enum (revenue, cogs, gross_profit, employee_cost, marketing_cost, rd_cost, g_and_a, total_opex, ebitda, depreciation, ebit, finance_cost, pbt, tax, pat)
**Identity check (P7):** `gross_profit ≡ revenue − cogs`; `ebitda ≡ gross_profit − total_opex`; `pat ≡ pbt − tax`. Violations → ⚠️.

---

## L. Waterfall & Carry  ★ PRIORITY BLOCK

### WTF-001 — carry_base  ★ PRIORITY TILE
**Type:** Decimal (Cr) · **DB:** `FundMetric('carry_base')` + `CarriedInterest.carry_base`
**Principles:** P1, P7
**Priority sources:**
1. Layer 1 `fund_performance.carry_base` extracted from dedicated Waterfall sheet
2. **Computed via canonical formula:** `(DST-001 + NAV-001) − CAP-002 − WTF-002`
3. Latest `CarriedInterest.carry_base` row
**Conflict:** ±₹0.10 Cr tolerance. If Layer 1 ≠ computed, ⚠️ flag and prefer Layer 1 (GP's contractual interpretation, since hurdle/preferred-return basis may include LPA-specific adjustments).
**Side-panel display:**
- Extracted: `Priority 1: read from 'Waterfall' tab, cell D12. Computed cross-check: ₹325.50 Cr (within ±₹0.10 Cr tolerance — agreement).`
- Computed: `Priority 2: Layer 1 source absent. Formula: (Distributions + NAV) − Called Capital − Preferred Return = (210.50 + 1450.30) − 1247.00 − 87.50 = ₹326.30 Cr.`

### WTF-002 — preferred_return_amount (hurdle)
**Priority sources:**
1. Layer 1 `fund_performance.preferred_return_amount` extracted
2. **Computed:** `CAP-002 × ((1 + SCH-002)^years − 1)` where years = (as_of_date − first_close_date) in years
3. `CarriedInterest.preferred_return_amount` latest row

### WTF-003 — gp_catchup_amount
**Priority sources:**
1. Layer 1 `fund_performance.gp_catchup_amount` extracted
2. **Computed:** `(WTF-002 × SCH-003) ÷ (1 − SCH-003)` — full catch-up formula

### WTF-004 — carry_amount_gross  ★ PRIORITY TILE
**Type:** Decimal (Cr) · **DB:** `FundMetric('carry_amount_gross')`
**Principles:** P1
**Priority sources:**
1. Layer 1 `fund_performance.carry_amount_gross` extracted
2. **Computed:** `WTF-003 + (WTF-001 × SCH-003)` — catch-up + carry on residual
3. `CarriedInterest.carry_amount_gross` latest row
**Identity check:** Gross carry ≤ WTF-001 × SCH-003 / (1 − SCH-003) (mathematical maximum if full catch-up applies).

### WTF-005 — carry_amount_net  ★ PRIORITY TILE
**Type:** Decimal (Cr) · **DB:** `FundMetric('carry_amount_net')`
**Priority sources:**
1. Layer 1 extracted
2. **Computed:** `WTF-004 − WTF-006`
3. `CarriedInterest.carry_amount_net` latest

### WTF-006 — gp_clawback_provision  ★ PRIORITY TILE
**Type:** Decimal (Cr) · **DB:** `FundMetric('gp_clawback_provision')`
**Principles:** P1
**Priority sources:**
1. Layer 1 extracted
2. **Computed:** `WTF-004 × 0.20` (industry-standard 20% reserve)
3. Custom rate from PPM if `clawback_reserve_pct` was extracted

### WTF-007 — return_of_capital_amount (waterfall tier 1)
**Same as DST-002.** Cross-listed because it's the first tier of the waterfall.

### WTF-008 — fund_performance_distributions (total per waterfall)
**Same as DST-001.** Cross-listed.

---

## M. Fees

### FEE-001 — management_fee_pct
**Same as SCH-005.**

### FEE-002 — fee_basis_amount (per period)
**Priority sources:**
1. `ManagementFeeSchedule.fee_basis_amount` per period
2. Layer 1 extracted
**Note:** Basis amount is what `SCH-006` (committed/called/nav) resolves to numerically for the period.

### FEE-003 — fee_amount (per period, pre-GST)
**Priority sources:**
1. `ManagementFeeSchedule.fee_amount`
2. **Computed:** `FEE-002 × FEE-001 × (days/365)` for partial periods
3. Layer 1 extracted

### FEE-004 — gst_amount (per period)
**Priority sources:**
1. `ManagementFeeSchedule.gst_amount`
2. **Computed:** `FEE-003 × 0.18` (standard 18%)

### FEE-005 — total_fee_with_gst (per period)
**Priority sources:**
1. `ManagementFeeSchedule.total_fee_with_gst`
2. **Computed:** `FEE-003 + FEE-004`

---

## N. LP-level Capital Account (per LP, per period)

### LPA-001 — lp_committed_capital
**Type:** Decimal (Cr) per LP · **DB:** `LPCapitalAccount.committed_capital`
**Priority sources:**
1. `LPCapitalAccount.committed_capital` (latest row per LP)
2. `Commitment.commitment_amount` matched by LP
**Identity (P7):** Per-LP committed values must SUM to CAP-001 ±1%.

### LPA-002 — lp_called_capital
**Priority sources:**
1. `LPCapitalAccount.called_capital` (latest)
2. SUM(`CapitalCallLineItem.called_amount` WHERE `commitment.investor = LP`)
**Identity:** SUM across LPs == CAP-002 ±1%.

### LPA-003 — lp_distributed_capital
**Priority sources:**
1. `LPCapitalAccount.distributed_capital` (latest)
2. SUM(`DistributionLineItem.net_amount` WHERE `commitment.investor = LP`)

### LPA-004 — lp_unrealized_value
**Priority sources:**
1. `LPCapitalAccount.unrealized_value` (latest)
2. **Computed:** `LPA-001/CAP-001 × NAV-001` (pro-rata share of fund NAV)

### LPA-005 — lp_total_value
**Priority sources:**
1. `LPCapitalAccount.total_value` (latest)
2. **Computed:** `LPA-003 + LPA-004`

### LPA-006 — lp_irr (per-LP)
**Priority sources:**
1. `LPCapitalAccount.irr`
2. **Computed:** XIRR over per-LP cashflows (capital call line items + distribution line items + LPA-004 terminal)

### LPA-007 — lp_tvpi
**Priority sources:**
1. `LPCapitalAccount.tvpi`
2. **Computed:** `LPA-005 ÷ LPA-002`

### LPA-008 — lp_dpi / lp_rvpi / lp_moic
Same pattern: stored field > computed identity.

---

## O. Service Entities

### ENT-001 — entity_type
**Type:** Enum (manager/trustee/sponsor/custodian/statutory_auditor/legal_counsel/registrar/valuer) · **DB:** `Entity.entity_type`
**Priority sources:**
1. Layer 1 `entities[].entity_type` cell mapped via Gemini enum (`entity_type`)
**Universality:** SEBI mandates the first 5 for every AIF; remaining 3 are optional.

### ENT-002 — entity_name · ENT-003 — pan · ENT-004 — sebi_registration · ENT-005 — gstin
**Priority sources:**
1. Layer 1 `entities[]` extracted cells
2. Existing `Entity` row matched by PAN/SEBI-Reg (preserves other fields on re-import)

---

## P. Compliance

Compliance fields are largely operational (filing status, deadlines) — most don't have meaningful priority disputes because they're directly entered or system-tracked, not extracted from Excel. The exceptions:

### CMP-001 — filing_status (per SEBI report)
**Priority sources:**
1. Layer 1 `compliance[].filings[].status` if extracted from dedicated Compliance sheet
2. Existing `SEBIReport.filing_status` (preserved on re-import)

### CMP-002 — sebi_filing_score · CMP-003 — equity_threshold_score · CMP-004 — portfolio_company_score · CMP-005 — combined_score
**Type:** Decimal · **DB:** `FundComplianceScore.*`
**Priority sources:**
1. **Computed** by compliance engine — NOT from Excel
**Note:** These are system-derived; Excel import never overrides them.

### CMP-006 — equity_threshold_breach (per investment)
**Priority sources:**
1. **Computed:** `Investment.percentage_stake_fully_diluted >= 10.00`
2. Layer 2 cell if extracted

### CMP-007 to CMP-010 — calendar events, alerts, audit logs
System-managed, not subject to priority matrix.

---

## Q. Cross-Cutting Reconciliation Identities

These are **mathematical identities** that the reconciler verifies after Layer 1/2/3 merge. Violations are surfaced in side-panel ⚠️ but do NOT block persistence (data quality flag only).

| Identity | Tolerance | Tier |
|---|---|---|
| `total_committed_capital ≡ Σ Commitment.commitment_amount` | ±1% | P7 |
| `total_called_capital ≡ Σ CapitalCallLineItem.called_amount` | ±1% | P7 |
| `total_distributions ≡ Σ DistributionLineItem.net_amount` | ±1% | P7 |
| `active_fair_value ≡ Σ latest Valuation.fair_value_of_holding (active)` | ±1% | P7 |
| `invested_cost ≡ Σ Investment.total_invested (active)` | ±1% | P7 |
| `nav_per_unit × total_units_outstanding ≡ total_nav` | ±0.5% | P7 |
| `gross_profit ≡ revenue − cogs` (per period per co) | ±₹0.10 Cr | P7 |
| `ebitda ≡ gross_profit − total_opex` (per period per co) | ±₹0.10 Cr | P7 |
| `pat ≡ pbt − tax` (per period per co) | ±₹0.10 Cr | P7 |
| `tvpi ≡ (distributions + nav) ÷ called` | ±0.05x | P7 |
| `rvpi + dpi ≡ tvpi` | ±0.05x | P7 |
| `moic ≡ active_fv ÷ invested_cost` | ±0.05x | P7 |
| `carry_amount_net ≡ carry_amount_gross − gp_clawback_provision` | ±₹0.10 Cr | P7 |

---

## R. Universality Notes

All priority rules above are derived from **structural** features of AIF accounting that hold across every Indian AIF regardless of GP, sector, vintage, or template, because they map to:

1. **SEBI-mandated artefacts** (NAV walk, Compliance Test Report, Quarterly Activity Report, LP commitment ledger) — every Cat I/II/III AIF must maintain these.
2. **Accounting identities** (P&L cascade, balance sheet identity, NAV identity) — universally true.
3. **Mathematical formulas** (TVPI, DPI, MOIC, IRR) — definitions, not conventions.
4. **Identifier formats** (CIN, PAN, SEBI Reg, GSTIN) — government-mandated, regex-stable.
5. **Standard enums** (entity_type, exit_type, methodology, IPEV level) — industry-wide vocabularies.

No rule above references a specific GP, fund name, sheet name, or sector template. The matrix is therefore portable to any AIF workbook the platform ingests in the future.

---

## S. Side-Panel Display Contract — 8 Priority Tiles

For each of these tiles, the side panel MUST show:
1. The Number (value + unit)
2. **Priority Rule Applied** (which Px was satisfied; cite source absences if higher priorities skipped)
3. How we got it (extracted cell OR formula → values → result)
4. Reconciliation status (other source values within / outside tolerance)

| Tile | Metric key | Field ID | Currently wired in `wireProvenance`? |
|---|---|---|---|
| Net IRR % | `net_irr` | PRF-001 | ✅ (v5-dashboard.js:1085) |
| TVPI | `tvpi` | PRF-003 | ✅ (v5-dashboard.js:1080) |
| Total Fair Value | `active_fair_value` | PRF-008 | ✅ (_METRIC_COPY entry) |
| MOIC | `moic` | PRF-006 | ✅ (v5-dashboard.js:1074) |
| Carry Base | `carry_base` | WTF-001 | ✅ (v5-dashboard.js:2623) |
| GP Carry Gross | `carry_amount_gross` | WTF-004 | ✅ (v5-dashboard.js:2624) |
| GP Carry Net | `carry_amount_net` | WTF-005 | ✅ (v5-dashboard.js:2625) |
| Clawback Provision | `gp_clawback_provision` | WTF-006 | ✅ (v5-dashboard.js:2626) |

**Implementation note (post-matrix-approval):** `FundMetric.provenance` JSON must be extended with a new key `priority_rule_applied: 'P1' | 'P2' | ... | 'P7'` (or combination like `'P1+P6'`) written by the reconciler. `openProvenancePanel` must read this key and render it as the new "Priority Rule Applied" section.

---

## T. Open Questions for Approval

Before Phase 3 implementation begins, please confirm:

1. **Tolerance bands** — are the defaults (±1% currency, ±0.5pp percent, ±0.05x multiple) acceptable, or do you want tighter on any specific field?
2. **Conflict surface UX** — when sources disagree within tolerance, show ⚠️ silently (only on side-panel click) OR also show a small dot on the tile itself? I recommend the former (cleaner default view, opt-in detail).
3. **Net IRR computation fallback** — if Layer 1 extracted Net IRR is absent, do you want Python's XIRR fallback to display with a "computed" badge, or display "—" and skip? I recommend computing-with-badge (better than blank).
4. **Per-LP priority rules** (LPA-001 to LPA-008) — these matter for LP statements but rarely surface as dashboard tiles. Do you want side-panel provenance wired for these too, or skip until LP-portal rollout?

---

**END — awaiting approval to proceed with Phase 3 implementation.**
