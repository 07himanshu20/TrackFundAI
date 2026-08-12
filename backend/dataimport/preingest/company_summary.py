"""
Type-B: collapse ONE company MIS workbook into ONE Portfolio row (TTM basis).

Universal split of labour:
  • Gemini (semantic): given the DISTINCT line-item labels the deterministic
    extractor already pulled from this company's statements, map each label to a
    canonical operating metric — revenue / ebitda_pbt / cash / headcount / none.
    Gemini never sees or emits a value.
  • Python (deterministic): for each metric, take that line item's period→value
    series, sort periods newest-first, and aggregate:
        flow metrics (revenue, ebitda_pbt) -> SUM of the trailing 12 periods (TTM)
        stock metrics (cash, headcount)     -> the single most recent value
    The reporting currency and the latest period come straight from the records.

Result: one dict of operating figures for the Portfolio_KPI sheet. Company
identity (Inv ID, sector, Fund FV) is joined later from the fund workbook.
"""
import json
import logging
import re

from ..gemini_column_mapper import _call_gemini

logger = logging.getLogger(__name__)

_MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
     'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], start=1)}


def _period_sort_key(label):
    """Best-effort universal ordering of a period label. Returns a comparable
    (year, month) tuple; unparseable labels sort oldest so they never win
    'most recent'."""
    if label is None:
        return (-1, -1)
    s = str(label).strip().lower()
    m = re.match(r'(\d{4})-(\d{2})-\d{2}', s)          # 2025-04-01
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.match(r"([a-z]{3})[a-z]*[\s\-'/]*(\d{2,4})", s)  # feb-26 / feb'26 / february 2026
    if m and m.group(1) in _MONTHS:
        y = int(m.group(2))
        y += 2000 if y < 100 else 0
        return (y, _MONTHS[m.group(1)])
    m = re.match(r'q([1-4])[\s\-/]*(?:fy)?\s*(\d{2,4})', s)  # q1-25 / q1 fy25
    if m:
        y = int(m.group(2))
        y += 2000 if y < 100 else 0
        return (y, int(m.group(1)) * 3)
    m = re.match(r'(?:fy)?\s*(\d{4})', s)              # fy2025 / 2025
    if m:
        return (int(m.group(1)), 0)
    return (-1, -1)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        or type(v).__name__ == 'Decimal'


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_LABELS_PER_CALL = 200
_METRICS = ('revenue', 'ebitda_pbt', 'cash', 'headcount')


def _map_labels(company, labels, timeout_ms=120_000):
    """Identify, from the distinct line-item labels, the ONE best label for each
    of the 4 operating metrics. Returns {label: metric}.

    Bounded-output design: Gemini returns only 4 keys (one label per metric),
    so the response CANNOT truncate no matter how many thousands of labels a
    big multi-sheet company workbook has. Labels are fed in input batches only
    as a size guard; we stop as soon as all four are found. This replaces the
    old 'map every label' call whose output grew with the label count and hit
    MAX_TOKENS on 80-sheet files."""
    picks = {m: None for m in _METRICS}
    for i in range(0, len(labels), _LABELS_PER_CALL):
        if all(picks.values()):
            break
        batch = labels[i:i + _LABELS_PER_CALL]
        prompt = f"""From the LINE-ITEM LABELS of the company MIS "{company}", pick the
SINGLE best-matching label for each metric below, or null if none fits.

  revenue     — total revenue / revenue from operations / net sales / total income
  ebitda_pbt  — EBITDA; if absent, profit before tax / operating profit
  cash        — closing cash & bank / cash balance / cash & cash equivalents
  headcount   — number of employees / headcount / total manpower

Return JSON only (exactly these 4 keys):
{{"revenue": "<label|null>", "ebitda_pbt": "<label|null>", "cash": "<label|null>", "headcount": "<label|null>"}}

LABELS:
{json.dumps(batch, ensure_ascii=False)}
"""
        try:
            resp = _call_gemini(prompt, context_label='preingest.summary',
                                timeout_ms=timeout_ms) or {}
        except Exception as e:  # noqa: BLE001 — one bad batch must not sink the file
            logger.warning(f'[preingest.summary] label batch failed for {company}: {e}')
            resp = {}
        if isinstance(resp, dict):
            for m in _METRICS:
                if picks[m] is None and resp.get(m):
                    picks[m] = resp[m]
    # invert to {label: metric} for _series_for; drop unmatched
    return {lbl: m for m, lbl in picks.items() if lbl}


def _series_for(records, want_metric, label_map):
    """Collect {period_label: value} for every line item mapped to want_metric."""
    series = {}
    for r in records:
        li = r.get('line_item')
        if li is None:
            continue
        if label_map.get(str(li).strip()) != want_metric:
            continue
        val = _num(r.get('value'))
        if val is None:
            continue
        period = r.get('period') if 'period' in r else r.get('dimension')
        if period in (None, ''):
            continue
        # if the same period appears for multiple matching labels, sum them
        series[period] = series.get(period, 0.0) + val
    return series


def _ttm(series, flow=True):
    """TTM aggregate: sum trailing 12 periods (flow) or latest value (stock)."""
    if not series:
        return None, None
    ordered = sorted(series.items(), key=lambda kv: _period_sort_key(kv[0]), reverse=True)
    latest_period = ordered[0][0]
    if flow:
        return sum(v for _, v in ordered[:12]), latest_period
    return ordered[0][1], latest_period


def summarize_company(company_name, records, timeout_ms=120_000):
    """records = the company's canonical financial records (line_item, period,
    value) from the deterministic extractor. Returns one Portfolio-row dict."""
    labels = sorted({str(r['line_item']).strip() for r in records
                     if r.get('line_item') not in (None, '')})
    row = {
        'company': company_name, 'currency': None, 'latest_period': None,
        'revenue_ttm': None, 'ebitda_ttm': None,
        'cash_balance': None, 'headcount': None, 'note': '',
    }
    if not labels:
        row['note'] = 'no financial line items extracted'
        return row

    # currency + scale unit from provenance (most common non-null) — used by the
    # normalizer to convert local operating figures to ₹ Crore.
    curr = [r.get('__currency__') for r in records if r.get('__currency__')]
    if curr:
        row['currency'] = max(set(curr), key=curr.count)
    units = [r.get('__unit__') for r in records if r.get('__unit__')]
    row['unit'] = max(set(units), key=units.count) if units else 'raw'

    label_map = _map_labels(company_name, labels, timeout_ms=timeout_ms)

    rev, p1 = _ttm(_series_for(records, 'revenue', label_map), flow=True)
    ebt, p2 = _ttm(_series_for(records, 'ebitda_pbt', label_map), flow=True)
    cash, p3 = _ttm(_series_for(records, 'cash', label_map), flow=False)
    head, _ = _ttm(_series_for(records, 'headcount', label_map), flow=False)

    row['revenue_ttm'] = rev
    row['ebitda_ttm'] = ebt
    row['cash_balance'] = cash
    row['headcount'] = head
    # latest period = newest we saw across the mapped metrics
    periods = [p for p in (p1, p2, p3) if p]
    if periods:
        row['latest_period'] = max(periods, key=_period_sort_key)
    return row
