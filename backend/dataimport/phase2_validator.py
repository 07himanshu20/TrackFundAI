"""
Phase 2 cross-validation rules. Catches LLM errors (e.g. FV column trap,
waterfall inversion) before persisting.

Returns (ok, violations, hint). If ok=False, single_call_extractor retries
the Gemini call with `hint` appended to the prompt.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _num(v) -> float:
    """Coerce to float, returning NaN on failure."""
    if v is None:
        return float('nan')
    try:
        return float(v)
    except (ValueError, TypeError):
        return float('nan')


def _isnan(x: float) -> bool:
    return x != x


def validate_extraction(data) -> tuple[bool, list[str], str]:
    """Run all cross-validation rules.

    Returns:
        ok          — True if all rules pass
        violations  — human-readable list of failed checks
        hint        — short retry hint for the LLM (most likely root cause)
    """
    violations: list[str] = []

    # ── Top-level shape guard ───────────────────────────────────────────
    # Gemini must return a single JSON OBJECT. Sometimes it returns a JSON
    # array (list) instead — usually after a retry where the prompt context
    # got confused. Treating a list as a dict crashes every downstream
    # `data.get(...)` call. Reject with a corrective hint so the next retry
    # gets the shape right.
    if not isinstance(data, dict):
        actual_type = type(data).__name__
        size_note = f' (list with {len(data)} item{"s" if len(data) != 1 else ""})' if isinstance(data, list) else ''
        violations.append(
            f'Top-level extraction is a {actual_type}{size_note}, expected a JSON object. '
            f'The top-level JSON shape MUST be a single object containing the canonical '
            f'top-level keys (fund_master, investors, capital_calls, portfolio_investments, '
            f'valuations, waterfall, fund_performance, etc.). Re-emit as one object.'
        )
        hint = (
            'Your previous output was a JSON array at the top level. The schema '
            'requires a single JSON OBJECT — wrap everything as: '
            '{"fund_master": {...}, "investors": [...], "valuations": [...], '
            '"waterfall": {...}, "fund_performance": {...}, ...}. NEVER return an '
            'array at the top level.'
        )
        return False, violations, hint

    fp = data.get('fund_performance') or {}
    fm = data.get('fund_master') or {}
    wf = data.get('waterfall') or {}
    investors = data.get('investors') or []
    investments = data.get('portfolio_investments') or []

    # Defensive coercion — if Gemini emitted any of these blocks as a list
    # instead of the documented object, coerce to empty object so downstream
    # `wf.get('...')` calls don't crash. Surface a violation so the retry
    # corrects the shape.
    if not isinstance(fp, dict):
        violations.append(f'`fund_performance` must be an object, got {type(fp).__name__}.')
        fp = {}
    if not isinstance(fm, dict):
        violations.append(f'`fund_master` must be an object, got {type(fm).__name__}.')
        fm = {}
    if not isinstance(wf, dict):
        violations.append(f'`waterfall` must be an object, got {type(wf).__name__}.')
        wf = {}
    if not isinstance(investors, list):
        violations.append(f'`investors` must be an array, got {type(investors).__name__}.')
        investors = []
    if not isinstance(investments, list):
        violations.append(f'`portfolio_investments` must be an array, got {type(investments).__name__}.')
        investments = []

    # ---- Rule 1: TVPI = DPI + RVPI (algebraic identity) ----
    tvpi = _num(fp.get('tvpi'))
    dpi  = _num(fp.get('dpi'))
    rvpi = _num(fp.get('rvpi'))
    if not (_isnan(tvpi) or _isnan(dpi) or _isnan(rvpi)):
        diff = abs(tvpi - (dpi + rvpi))
        if diff > 0.02:
            violations.append(
                f'TVPI identity broken: TVPI={tvpi:.3f} but DPI+RVPI='
                f'{dpi+rvpi:.3f} (diff={diff:.3f}, tolerance 0.02)'
            )

    # ---- Rule 2: MOIC ≈ TVPI for unrealised funds (FV-trap canary) ----
    moic = _num(fp.get('moic_portfolio'))
    if not (_isnan(moic) or _isnan(tvpi)):
        # If the fund is mostly unrealised (DPI < 0.5), MOIC should be close to TVPI.
        # Big gap (>0.5) usually means the LLM grabbed equity value of whole companies
        # instead of fund's holding share (the "FV trap" from Multiples bug).
        if not _isnan(dpi) and dpi < 0.5 and abs(moic - tvpi) > 0.5:
            violations.append(
                f'MOIC/TVPI divergence: MOIC={moic:.3f}, TVPI={tvpi:.3f} '
                f'(diff={abs(moic-tvpi):.3f}). For a fund with DPI={dpi:.3f} '
                f'(mostly unrealised), MOIC should be close to TVPI. Likely the FV '
                f'column used was Equity Value of whole company instead of FV Holding.'
            )

    # ---- Rule 3: Total Committed ≈ sum of LP commitments ----
    stated_committed = _num(fp.get('total_committed_capital'))
    if not _isnan(stated_committed) and investors:
        sum_commits = sum(_num(i.get('commitment_amount') or i.get('commitment'))
                          for i in investors)
        sum_commits = sum_commits if not _isnan(sum_commits) else 0
        if sum_commits > 0:
            pct_diff = abs(sum_commits - stated_committed) / max(stated_committed, 1)
            if pct_diff > 0.01:
                violations.append(
                    f'Commitment mismatch: stated total={stated_committed:.2f} '
                    f'but sum of LP commitments={sum_commits:.2f} '
                    f'(diff={pct_diff*100:.1f}%, tolerance 1%)'
                )

    # ---- Rule 4: Total Called ≈ sum of LP drawdowns ----
    stated_called = _num(fp.get('total_called_capital'))
    if not _isnan(stated_called) and investors:
        sum_calls = sum(_num(i.get('drawdown') or i.get('capital_called'))
                        for i in investors)
        sum_calls = sum_calls if not _isnan(sum_calls) else 0
        if sum_calls > 0:
            pct_diff = abs(sum_calls - stated_called) / max(stated_called, 1)
            if pct_diff > 0.02:
                violations.append(
                    f'Called-capital mismatch: stated={stated_called:.2f} but sum '
                    f'of LP drawdowns={sum_calls:.2f} ({pct_diff*100:.1f}% diff)'
                )

    # ---- Rule 5: Waterfall self-consistency ----
    carry_gross    = _num(wf.get('carry_amount_gross') or fp.get('carry_amount_gross'))
    step3          = _num(wf.get('step_3_catchup_amount'))
    step4b         = _num(wf.get('step_4b_gp_residual_carry'))
    if not (_isnan(carry_gross) or _isnan(step3) or _isnan(step4b)):
        expected = step3 + step4b
        if abs(carry_gross - expected) > 0.5:
            violations.append(
                f'Carry self-consistency broken: gross={carry_gross:.2f} but '
                f'Step3+Step4b={expected:.2f} (diff={abs(carry_gross-expected):.2f})'
            )

    # ---- Rule 6: Carry must be zero if Available After ROC+Pref ≤ 0 ----
    available = _num(wf.get('available_after_roc_and_pref'))
    if not (_isnan(available) or _isnan(carry_gross)):
        if available <= 0 and carry_gross > 0.5:
            violations.append(
                f'Carry sign violation: Available After ROC+Pref={available:.2f} '
                f'(non-positive) but carry_gross={carry_gross:.2f}. A fund in ROC '
                f'phase has ZERO gross carry.'
            )

    # ---- Rule 7: net_carry = gross_carry - clawback ----
    net_carry = _num(wf.get('net_carry') or fp.get('carry_amount_net'))
    clawback  = _num(wf.get('clawback_provision') or fp.get('gp_clawback_provision'))
    if not (_isnan(net_carry) or _isnan(carry_gross) or _isnan(clawback)):
        expected_net = carry_gross - clawback
        if abs(net_carry - expected_net) > 0.5:
            violations.append(
                f'Net carry mismatch: stated={net_carry:.2f} but '
                f'gross−clawback={expected_net:.2f}'
            )

    # ---- Rule 8: Counts vs claims ----
    n_companies_stated = _num(fp.get('portfolio_companies'))
    if not _isnan(n_companies_stated) and investments:
        # Unique companies in investments array
        unique_co = len({(i.get('company_name') or '').strip() for i in investments
                        if i.get('company_name')})
        if unique_co > 0 and abs(unique_co - n_companies_stated) > max(2, 0.05 * n_companies_stated):
            violations.append(
                f'Company count mismatch: stated={n_companies_stated} but '
                f'unique company names in portfolio_investments={unique_co}'
            )

    # ---- Rule 9: Required identity fields ----
    if not fm.get('fund_name'):
        violations.append('Missing fund_name in fund_master')
    if not fm.get('inception_date'):
        violations.append('Missing inception_date in fund_master (cannot compute fund age)')

    # ---- Rule 10: FV aggregate identity (Rule 18 of system prompt) ----
    # fund_performance.total_unrealised_fv_holding MUST equal
    # sum(valuations[].fair_value_of_holding) within 1% tolerance.
    valuations_raw = data.get('valuations') or []
    valuations = [v for v in valuations_raw if isinstance(v, dict)] if isinstance(valuations_raw, list) else []
    stated_fv = _num(fp.get('total_unrealised_fv_holding'))
    if valuations and not _isnan(stated_fv):
        sum_fv = 0.0
        any_holding = False
        for v in valuations:
            fvh = _num(v.get('fair_value_of_holding') or v.get('fv_holding'))
            if not _isnan(fvh):
                sum_fv += fvh
                any_holding = True
        if any_holding and sum_fv > 0:
            pct_diff = abs(sum_fv - stated_fv) / max(stated_fv, 1)
            if pct_diff > 0.01:
                violations.append(
                    f'FV aggregate mismatch: total_unrealised_fv_holding={stated_fv:.2f} '
                    f'but sum(valuations[].fair_value_of_holding)={sum_fv:.2f} '
                    f'({pct_diff*100:.1f}% diff, tolerance 1%). Rule 18 requires '
                    f'equality — the aggregate must be the sum of per-row holdings.'
                )

    # ---- Rule 11: Terminal NAV in net_irr_cashflows (Rule 19) ----
    cashflows = fp.get('net_irr_cashflows') or []
    as_of = fp.get('as_of_date')
    nav_latest = _num(fp.get('fund_nav_latest') or fp.get('total_unrealised_fv_holding'))
    if cashflows and as_of and not _isnan(nav_latest) and nav_latest > 0:
        last = cashflows[-1] if cashflows else {}
        last_type = (last.get('type') or '').lower() if isinstance(last, dict) else ''
        last_date = str(last.get('date') or '') if isinstance(last, dict) else ''
        if last_type != 'distribution' or last_date != str(as_of):
            violations.append(
                f'net_irr_cashflows missing terminal NAV entry: last cashflow is '
                f'{last_type}@{last_date}; expected distribution@{as_of} with '
                f'amount=fund_nav_latest ({nav_latest:.2f}). Rule 19 requires the '
                f'synthetic terminal NAV row so XIRR is not biased negative.'
            )

    # ---- Rule 12: Preferred return mandatory (Rule 20) ----
    if wf and 'step_2_preferred_return' not in wf and 'preferred_return_amount' not in wf:
        violations.append(
            'Missing step_2_preferred_return in waterfall block. Rule 20 requires '
            'this field be emitted on every import — compute the accrued amount '
            '(LP_called × ((1+hurdle)^years − 1)) if no payment has been made.'
        )

    # ---- Rule 13: Per-investment irr_pct mandatory (Rule 21) ----
    if investments:
        missing_irr = sum(1 for i in investments if i.get('irr_pct') is None)
        if missing_irr > len(investments) * 0.5:
            violations.append(
                f'Per-investment irr_pct missing on {missing_irr}/{len(investments)} '
                f'portfolio_investments rows. Rule 21 requires irr_pct on every row '
                f'(compute inline from tranches + latest FV if not stated).'
            )

    # ---- Rule 14: NAV trap guard (Rule 25) ----
    # fund_nav_latest is NET NAV; it cannot exceed gross sum of FMV holdings.
    # If it does, Gemini grabbed the gross FMV sum and called it NAV.
    nav_latest = _num(fp.get('fund_nav_latest'))
    if valuations and not _isnan(nav_latest):
        sum_fvh = 0.0
        any_h = False
        for v in valuations:
            fvh = _num(v.get('fair_value_of_holding') or v.get('fv_holding'))
            if not _isnan(fvh):
                sum_fvh += fvh
                any_h = True
        if any_h and sum_fvh > 0 and nav_latest > sum_fvh * 1.01:  # 1% tolerance for rounding
            violations.append(
                f'NAV trap: fund_nav_latest={nav_latest:.2f} exceeds sum of '
                f'fair_value_of_holding={sum_fvh:.2f}. Net NAV cannot exceed '
                f'gross FMV. You likely grabbed the gross FMV sum cell and '
                f'called it NAV. Per Rule 25, find the NET NAV cell on a '
                f'NAV_Workings / Fund_NAV sheet (it equals Gross FMV + cash '
                f'− accrued fees − accrued expenses).'
            )

    # ---- Rule 14b: NAV cross-block consistency (Rule 25 HARD GUARD #2) ----
    # fund_performance.fund_nav_latest MUST equal the latest nav_records
    # entry's total_nav within 1%. If they disagree, fund_nav_latest is
    # almost always the one that's wrong (it picked up gross FMV by mistake).
    nav_records = data.get('nav_records') or []
    if isinstance(nav_records, list) and nav_records:
        nav_rows = [r for r in nav_records if isinstance(r, dict)]
        # Date resolver (consider all aliases for the period date)
        def _row_date(r):
            return str(r.get('period_end') or r.get('nav_date')
                       or r.get('date') or r.get('period') or '').strip()

        # ---- Rule 14b-i: NAV WALK INTEGRITY (Rule 25 HARD GUARD #3) ----
        # Every row MUST have a period_end. Without dates the walk has no
        # ordering and "latest" is undefined — silent corruption otherwise.
        rows_missing_date = [i for i, r in enumerate(nav_rows) if not _row_date(r)]
        if rows_missing_date and len(rows_missing_date) == len(nav_rows):
            violations.append(
                f'NAV walk integrity broken: all {len(nav_rows)} nav_records[] '
                f'rows are missing `period_end`. Per Rule 25 HARD GUARD #3, '
                f'every row in the NAV walk MUST have an ISO date (YYYY-MM-DD). '
                f'Re-emit the array sorted ascending by period_end so the last '
                f'row is the latest period.'
            )
        elif rows_missing_date:
            violations.append(
                f'NAV walk has {len(rows_missing_date)}/{len(nav_rows)} rows '
                f'missing `period_end`. Per Rule 25 HARD GUARD #3, every row '
                f'MUST have an ISO date. Affected row indices: {rows_missing_date[:5]}.'
            )

        # ---- Rule 14b-ii: Cross-block consistency (only if dates valid) ----
        if not rows_missing_date and not _isnan(nav_latest):
            latest_nav_row = max(nav_rows, key=_row_date, default=None)
            if latest_nav_row:
                nav_in_walk = _num(latest_nav_row.get('total_nav') or latest_nav_row.get('closing_nav'))
                if not _isnan(nav_in_walk) and nav_in_walk > 0:
                    diff = abs(nav_latest - nav_in_walk)
                    tol  = max(0.01 * nav_in_walk, 0.5)  # 1% or ₹0.5 Cr floor
                    if diff > tol:
                        violations.append(
                            f'NAV cross-block mismatch: fund_performance.fund_nav_latest='
                            f'{nav_latest:.2f} but latest nav_records.total_nav='
                            f'{nav_in_walk:.2f} (diff={diff:.2f}, tolerance={tol:.2f}). '
                            f'Per Rule 25 HARD GUARD #2, these MUST agree within 1%. The '
                            f'nav_records walk is canonical — overwrite fund_nav_latest '
                            f'with {nav_in_walk:.2f} and re-derive MOIC, TVPI, RVPI, IRR, '
                            f'carry_base, GP carry, and clawback from the corrected NAV.'
                        )

        # ---- Rule 14b-iii: NAV DISTRIBUTION SANITY (Rule 25 HARD GUARD #4) ----
        # NAVs grow/shrink smoothly. A single total_nav that's > 2.5× the
        # median of the rest is almost certainly a gross-FMV cell that got
        # mislabelled. Universal check — no fund's NAV legitimately triples
        # quarter-over-quarter without a major liquidity event.
        nav_values = [_num(r.get('total_nav') or r.get('closing_nav')) for r in nav_rows]
        nav_values = [v for v in nav_values if not _isnan(v) and v > 0]
        if len(nav_values) >= 3:  # need at least 3 to compute a meaningful median
            sorted_vals = sorted(nav_values)
            n = len(sorted_vals)
            median = sorted_vals[n // 2] if n % 2 else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
            max_val = max(nav_values)
            if median > 0 and max_val > median * 2.5:
                violations.append(
                    f'NAV walk distribution suspect: max total_nav={max_val:.2f} '
                    f'is {max_val/median:.1f}× the median ({median:.2f}). Per Rule 25 '
                    f'HARD GUARD #4, real NAVs do not jump 2.5× within a single walk. '
                    f'The outlier row is almost certainly a gross FMV cell mislabelled '
                    f'as total_nav. Re-scan the NAV_Workings sheet for the NET NAV '
                    f'column (after fees + accruals) and replace the outlier value.'
                )

    # ---- Rule 15: TVPI sanity for unrealised funds ----
    if not _isnan(tvpi) and not _isnan(dpi):
        if dpi < 0.5 and tvpi > 2.5:
            violations.append(
                f'TVPI sanity: TVPI={tvpi:.2f} is implausibly high for an unrealised '
                f'fund (DPI={dpi:.2f}). This usually indicates NAV trap (Rule 25) — '
                f'fund_nav_latest is inflated by using gross FMV instead of net NAV.'
            )

    # ---- Rule 16: carry_base ≡ available_after_roc_and_pref (Rule 28) ----
    cb = _num(wf.get('carry_base'))
    aarp = _num(wf.get('available_after_roc_and_pref'))
    if not (_isnan(cb) or _isnan(aarp)):
        if abs(cb - aarp) > 0.5:
            violations.append(
                f'carry_base identity broken: carry_base={cb:.2f} but '
                f'available_after_roc_and_pref={aarp:.2f} (diff={abs(cb-aarp):.2f}). '
                f'Per Rule 28 these are the SAME quantity: '
                f'(Distributions + NAV) − Called − Preferred Return. '
                f'You likely computed carry_base without subtracting Preferred Return.'
            )

    # ---- Rule 17b: FV AGGREGATE-vs-ROW IDENTITY (Rule 33a) ----
    # fund_performance.total_unrealised_fv_holding MUST equal sum of
    # valuations[].fair_value_of_holding. Mismatch is the hallmark of
    # a hallucinated aggregate (Pro emitted a "plausible" total but the
    # supporting row-level data tells a different story).
    if isinstance(valuations, list) and valuations:
        sum_fvh = 0.0
        any_fvh = False
        for v in valuations:
            if isinstance(v, dict):
                fvh = _num(v.get('fair_value_of_holding') or v.get('fv_holding'))
                if not _isnan(fvh):
                    sum_fvh += fvh
                    any_fvh = True
        agg_fv = _num(fp.get('total_unrealised_fv_holding'))
        if any_fvh and sum_fvh > 0 and not _isnan(agg_fv):
            pct_diff = abs(agg_fv - sum_fvh) / max(sum_fvh, 1.0)
            if pct_diff > 0.01:  # >1% disagreement
                violations.append(
                    f'FV aggregate-vs-row mismatch: '
                    f'fund_performance.total_unrealised_fv_holding={agg_fv:.2f} '
                    f'but sum(valuations[].fair_value_of_holding)={sum_fvh:.2f} '
                    f'(diff={abs(agg_fv-sum_fvh):.2f}, {pct_diff*100:.1f}%). '
                    f'Per Rule 33a these MUST agree. Either the aggregate is '
                    f'hallucinated, or the per-row valuations are incomplete. '
                    f'Re-emit so they reconcile, or set aggregate = sum(rows).'
                )

    # ---- Rule 17c: VALUATIONS MUST EXIST WHEN INVESTMENTS EXIST (Rule 32 + 33a) ----
    # If portfolio_investments[] has rows but valuations[] is empty, the
    # extraction is incomplete. Empty valuations means no per-investment FV
    # and total_unrealised_fv_holding has no supporting data — that's the
    # exact "Pro skipped a sheet" failure mode.
    if isinstance(investments, list) and len(investments) > 0:
        if not isinstance(valuations, list) or len(valuations) == 0:
            # Only fail if Pro ALSO emitted a non-zero fund-level FV (otherwise
            # an early-stage fund with no valuations yet would correctly have
            # empty arrays and zero aggregates).
            agg_fv = _num(fp.get('total_unrealised_fv_holding'))
            if not _isnan(agg_fv) and agg_fv > 0:
                violations.append(
                    f'Valuations missing despite {len(investments)} investments '
                    f'and total_unrealised_fv_holding={agg_fv:.2f}. Per Rule 32 + 33a '
                    f'every aggregate must be supported by row-level data. Re-scan '
                    f'the Valuations / IPEV sheet and emit ONE valuations[] row per '
                    f'investment (cost_basis + fair_value_of_holding mandatory).'
                )

    # ---- Rule 17d: DISTRIBUTIONS AGGREGATE-vs-ROW IDENTITY (Rule 33b) ----
    # fund_performance.total_distributions MUST equal sum(distributions[].total_net_amount).
    # If distributions[] is empty, total_distributions MUST be 0 or omitted —
    # Cover-sheet "estimated DPI" values are forward-looking, NOT realised.
    distributions = data.get('distributions') or []
    if isinstance(distributions, list):
        sum_dist = 0.0
        for d in distributions:
            if isinstance(d, dict):
                amt = _num(d.get('total_net_amount') or d.get('net_distribution'))
                if _isnan(amt):
                    amt = _num(d.get('total_gross_amount') or d.get('gross_amount'))
                if not _isnan(amt):
                    sum_dist += amt
        agg_dist = _num(fp.get('total_distributions'))
        if not _isnan(agg_dist):
            # Tolerance: 1% of stated total, with floor of 0.5 Cr
            tol = max(0.01 * max(abs(agg_dist), abs(sum_dist)), 0.5)
            if abs(agg_dist - sum_dist) > tol:
                if len(distributions) == 0 and agg_dist > 0:
                    violations.append(
                        f'Distributions hallucinated: total_distributions={agg_dist:.2f} '
                        f'but distributions[] is EMPTY. Per Rule 33b + 34, if no '
                        f'distribution rows exist, total_distributions MUST be 0 (or '
                        f'omitted). Cover-sheet "estimated DPI" / "target DPI" numbers '
                        f'are forward-looking, NOT realised. Set total_distributions=0.'
                    )
                else:
                    violations.append(
                        f'Distributions aggregate-vs-row mismatch: '
                        f'total_distributions={agg_dist:.2f} but '
                        f'sum(distributions[].total_net_amount)={sum_dist:.2f} '
                        f'(diff={abs(agg_dist-sum_dist):.2f}). Per Rule 33b these '
                        f'MUST agree within 1%. Re-emit so they reconcile.'
                    )

    # ---- Rule 17e: PROVENANCE CITATION (Rule 32) ----
    # Every aggregate field in fund_performance + waterfall must have a
    # provenance citation. Missing provenance is treated as a hallucination
    # risk and rejected (after the safer aggregate-vs-row checks above,
    # which catch the most dangerous cases first).
    fp_prov = fp.get('provenance') if isinstance(fp.get('provenance'), dict) else {}
    wf_prov = wf.get('provenance') if isinstance(wf.get('provenance'), dict) else {}
    # The aggregate fields most likely to be hallucinated are FV, NAV, and
    # distributions — those are the ones we hard-require citations for.
    _critical_fp_keys = ('total_unrealised_fv_holding', 'fund_nav_latest',
                         'total_distributions', 'total_called_capital',
                         'total_invested_capital')
    missing_prov = []
    for k in _critical_fp_keys:
        v = _num(fp.get(k))
        if not _isnan(v) and v > 0 and not fp_prov.get(k):
            missing_prov.append(k)
    if missing_prov:
        violations.append(
            f'Provenance citations missing for fund_performance fields: '
            f'{missing_prov}. Per Rule 32, every aggregate in fund_performance '
            f'and waterfall MUST have an entry in the provenance sub-object '
            f'naming either a cell reference (e.g. "Cover!C11") or a formula '
            f'expression (e.g. "sum(Valuations!I4:I128)"). Aggregates without '
            f'a cited source are treated as suspicious and rejected.'
        )

    # ---- Rule 17: years_since_inception sanity ----
    # If inception_date is present, check step_2_years_compounded ≈ (as_of − inception)/365.25
    incep = fm.get('inception_date')
    as_of = fp.get('as_of_date')
    years_used = _num(wf.get('step_2_years_compounded'))
    if incep and as_of and not _isnan(years_used):
        try:
            from datetime import datetime
            def _parse_date(d):
                if isinstance(d, str):
                    return datetime.strptime(d[:10], '%Y-%m-%d')
                return None
            di, da = _parse_date(incep), _parse_date(as_of)
            if di and da:
                expected_years = (da - di).days / 365.25
                if abs(expected_years - years_used) > 0.6:  # >7 months off
                    violations.append(
                        f'years_since_inception mismatch: step_2_years_compounded='
                        f'{years_used:.2f} but (as_of − inception_date) = '
                        f'{expected_years:.2f}. Per Rule 27 use the gap from '
                        f'INCEPTION (not final close) to as_of_date.'
                    )
        except Exception:
            pass

    # ---- Pick the most likely root-cause hint ----
    hint = ''
    if any('FV column' in v or 'MOIC/TVPI' in v for v in violations):
        hint = (
            'Re-examine the Valuations / IPEV sheet for a "FV Holding" '
            'or "Fund Share" column — that is the correct fund-level FV. '
            'Do NOT use the "FV" column from Portfolio Investments which '
            'typically contains the equity value of the whole company. '
            'After correction, MOIC and TVPI should both ≈ RVPI for an '
            'unrealised fund.'
        )
    elif any('Carry sign' in v for v in violations):
        hint = (
            'The fund is in the Return-of-Capital phase. Set carry_amount_gross, '
            'carry_amount_net, gp_clawback_provision, step_3_catchup_amount, and '
            'step_4b_gp_residual_carry all to 0. Total Value − Called − Preferred '
            'Return is non-positive, so no carry has been earned.'
        )
    elif any('Commitment mismatch' in v or 'Called-capital mismatch' in v for v in violations):
        hint = (
            'Reconcile fund_performance totals with the Investors register. '
            'Sum the per-LP commitment_amount and drawdown columns explicitly.'
        )
    elif any('Carry self-consistency' in v or 'Net carry mismatch' in v for v in violations):
        hint = (
            'Make the European waterfall numbers self-consistent. '
            'carry_amount_gross MUST equal step_3_catchup_amount + step_4b_gp_residual_carry. '
            'carry_amount_net MUST equal carry_amount_gross − clawback_provision.'
        )
    elif any('FV aggregate mismatch' in v for v in violations):
        hint = (
            'Recompute fund_performance.total_unrealised_fv_holding as the EXACT '
            'arithmetic sum of every valuations[].fair_value_of_holding you emit. '
            'Do not pick a sub-total cell from the workbook — those are usually '
            'section subtotals, not the portfolio total. Per Rule 18 these MUST '
            'be equal.'
        )
    elif any('terminal NAV' in v for v in violations):
        hint = (
            'Append a synthetic terminal cashflow to net_irr_cashflows: '
            '{"date": "<as_of_date>", "amount": <fund_nav_latest>, "type": "distribution"}. '
            'Without this terminal entry XIRR computes a deeply negative return. '
            'Per Rule 19, this entry must always be present and must be the LAST '
            'item in the array.'
        )
    elif any('step_2_preferred_return' in v for v in violations):
        hint = (
            'Emit step_2_preferred_return in the waterfall block on every import. '
            'If the fund has not yet paid preferred return, compute the accrued '
            'amount: LP_called × ((1+hurdle_rate)^years_since_final_close − 1). '
            'Never omit this field. Per Rule 20.'
        )
    elif any('Per-investment irr_pct missing' in v for v in violations):
        hint = (
            'Emit irr_pct on EVERY portfolio_investments[] row. If not stated in '
            'the source, compute deal-level XIRR inline from the investment\'s '
            'tranche dates+amounts and the latest fair_value_of_holding. Per '
            'Rule 21.'
        )
    elif any('NAV walk integrity broken' in v or 'NAV walk has' in v for v in violations):
        hint = (
            'Re-emit nav_records[] with `period_end` populated on EVERY row '
            '(ISO YYYY-MM-DD). Sort the array ascending by period_end so the '
            'LAST entry is the most recent period (= as_of_date). Without '
            'dates the walk has no ordering and "latest" is undefined. '
            'See Rule 25 HARD GUARD #3.'
        )
    elif any('NAV walk distribution suspect' in v for v in violations):
        hint = (
            'One total_nav in your nav_records[] walk is a wild outlier vs the '
            'rest (>2.5× median). Real NAVs grow/shrink smoothly — this is '
            'almost certainly a GROSS FMV cell mislabelled as Net NAV. Open '
            'the NAV_Workings / Fund_NAV sheet for that period and find the '
            'NET NAV column (after subtracting accrued fees + accrued expenses '
            'from gross holdings). Replace the outlier with the correct net '
            'value, then re-emit the entire walk. See Rule 25 HARD GUARD #4.'
        )
    elif any('NAV cross-block mismatch' in v for v in violations):
        hint = (
            'Set fund_performance.fund_nav_latest equal to the latest '
            'nav_records[].total_nav. They are the SAME quantity (Net NAV at '
            'the as_of_date) and MUST agree to within 1%. The nav_records '
            'walk is canonical because it reads the workbook NAV_Workings '
            'sheet directly. After fixing fund_nav_latest, re-derive every '
            'downstream metric that uses NAV: MOIC, TVPI, RVPI, IRR, '
            'carry_base, GP carry (gross + net), and clawback_provision.'
        )
    elif any('NAV trap' in v or 'TVPI sanity' in v for v in violations):
        hint = (
            'fund_nav_latest is NET NAV — the "Closing NAV" / "Net NAV" cell on '
            'a NAV_Workings / Fund_NAV / NAV_Walk sheet (latest period). It is '
            'NOT the sum of investment FMVs. The formula is: '
            'Net NAV = Gross FMV + cash − accrued management fees − accrued expenses. '
            'Find the dedicated NAV sheet, take the latest period\'s Closing NAV cell. '
            'See Rule 25 + the INPUT PRIORITY table for fund_nav_latest.'
        )
    elif any('FV aggregate-vs-row mismatch' in v for v in violations):
        hint = (
            'fund_performance.total_unrealised_fv_holding must EXACTLY equal '
            'the arithmetic sum of valuations[].fair_value_of_holding. Either '
            'recompute the aggregate from the rows you emitted, or re-emit the '
            'rows so their sum matches the aggregate. Per Rules 33a + 34, the '
            'aggregate is forbidden to be invented — it must be supported by '
            'row-level data.'
        )
    elif any('Valuations missing despite' in v for v in violations):
        hint = (
            'You emitted N investments and a non-zero total_unrealised_fv_holding '
            'but NO per-investment valuations[]. That is a hallucinated aggregate. '
            'Re-scan the Valuations / IPEV sheet and emit ONE valuations[] row per '
            'portfolio_investment with cost_basis + fair_value_of_holding. If the '
            'workbook genuinely has no Valuations sheet, set '
            'total_unrealised_fv_holding=0. Per Rules 32 + 33a + 34.'
        )
    elif any('Distributions hallucinated' in v or 'Distributions aggregate-vs-row' in v for v in violations):
        hint = (
            'fund_performance.total_distributions must EXACTLY equal '
            'sum(distributions[].total_net_amount). If you emitted NO distribution '
            'rows, total_distributions MUST be 0 — Cover-sheet "estimated DPI" / '
            '"target DPI" values are forward-looking projections, NOT realised '
            'distributions. Per Rule 33b + 34, set total_distributions=0 when '
            'distributions[] is empty.'
        )
    elif any('Provenance citations missing' in v for v in violations):
        hint = (
            'Every aggregate value in fund_performance and waterfall MUST have '
            'a matching entry in the provenance sub-object naming either a '
            'specific cell (e.g. "Cover!C11", "Valuations!I129") or a formula '
            'expression (e.g. "sum(Valuations!I4:I128)"). Aggregates without '
            'a cited source are treated as suspicious and rejected. Per Rule 32. '
            'Example: provenance: { total_unrealised_fv_holding: '
            '"sum(Valuations!I4:I128)" }.'
        )
    elif any('carry_base identity broken' in v for v in violations):
        hint = (
            'carry_base and available_after_roc_and_pref are the SAME quantity. '
            'Both = (Total Distributions + Net NAV) − Total Called − Preferred Return. '
            'You computed carry_base WITHOUT subtracting Preferred Return — that is '
            'wrong. Recompute: carry_base = TV − Called − Pref. See Rule 28.'
        )
    elif any('years_since_inception mismatch' in v for v in violations):
        hint = (
            'step_2_years_compounded must equal (as_of_date − inception_date) / 365.25. '
            'Use the FUND INCEPTION date, not the final close date. See Rule 27.'
        )

    ok = not violations
    return ok, violations, hint
