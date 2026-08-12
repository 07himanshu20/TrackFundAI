"""
Deterministic per-sheet structural fingerprint.

No Gemini here. For each sheet we produce a small, self-describing summary
that gives the LLM everything it needs to MAP the sheet — and nothing more:

  - dimensions (rows x cols)
  - the top rows verbatim (so the banner unit "₹ in Lakhs" and the company
    name in D2/A1 are visible)
  - the detected header row + its cells
  - up to 3 sample data rows
  - deterministic hints: candidate unit(s), candidate entity text,
    number of table-like header rows (stacked-table detector), whether the
    sheet has cached numeric values at all (missing-cache guard signal)

The fingerprint is ~150-400 tokens regardless of whether the sheet has 50 or
50,000 rows, because we never send the body — only structure + samples.
"""
import re
from decimal import Decimal

from ..phase6_extractor.helpers import (
    find_header_row,
    is_junk_row,
    is_section_title_row,
    row_non_empty,
)

# ── Unit / scale detection (HINT ONLY) ───────────────────────────────────────
# These only surface candidate scale words to the classifier; the classifier
# also sees the verbatim banner rows (top_rows) and makes the final call, so a
# scale/currency this vocabulary doesn't list is still caught from the raw text.
# Currency-agnostic: scale words apply to INR/USD/EUR/RM/etc. equally.
# Ordered most-specific first.
_UNIT_PATTERNS = [
    ('billions',  re.compile(r'\b(?:in\s+)?(?:bn|billion|billions)\b', re.I)),
    ('crores',    re.compile(r'\b(?:in\s+)?(?:cr|crore|crores)\b', re.I)),
    ('millions',  re.compile(r'\b(?:in\s+)?(?:mn|mln|million|millions)\b', re.I)),
    ('lakhs',     re.compile(r'\b(?:in\s+)?(?:lac|lacs|lakh|lakhs)\b', re.I)),
    ('thousands', re.compile(r"\b(?:in\s+)?(?:000s|'000|thousand|thousands)\b", re.I)),
]

# Currency mention (hint only) — symbol or 3-letter-ish code near a scale word.
_CURRENCY_RE = re.compile(
    r'(₹|rs\.?|inr|usd|\$|us\$|eur|€|gbp|£|rm|myr|sgd|aed|jpy|¥)', re.I)


def _cell_text(v) -> str:
    if v is None:
        return ''
    if isinstance(v, float) and v != v:  # NaN
        return ''
    return str(v).strip()


def detect_unit_hints(rows, header_idx):
    """Scan the banner region (everything at/above the header) for scale +
    currency words. Returns (unit_hints, currency_hints) — never guesses a
    multiplier, just reports what text was seen so the classifier can decide."""
    unit_hints, cur_hints = [], []
    seen_u, seen_c = set(), set()
    scan_upto = max(header_idx + 1, 6)
    for r in rows[:scan_upto]:
        for _, v in row_non_empty(r):
            t = _cell_text(v)
            if not t or len(t) > 80:
                continue
            for unit, pat in _UNIT_PATTERNS:
                if unit not in seen_u and pat.search(t):
                    unit_hints.append({'unit': unit, 'evidence': t[:60]})
                    seen_u.add(unit)
            cm = _CURRENCY_RE.search(t)
            if cm:
                sym = cm.group(0).lower()
                if sym not in seen_c:
                    cur_hints.append({'currency': cm.group(0), 'evidence': t[:60]})
                    seen_c.add(sym)
    return unit_hints, cur_hints


# ── Entity / company-name detection ──────────────────────────────────────────
# On real files the company name sits in a banner cell above the header
# (A1 "AGNIKUL COSMOS PRIVATE", D2 "Hubbler, Bengaluru"). We surface candidate
# text; Gemini decides + Python never guesses the canonical entity itself.
_ENTITY_STOP = re.compile(
    r'^(?:mis|report|monthly|financial|statement|summary|dashboard|cover|'
    r'index|contents?|particulars?|amount|total|budget|actual|profit|loss|'
    r'balance\s*sheet|cash\s*flow|p&l|pl|kpi|note|for\s+the)\b', re.I)


def detect_entity_hints(rows, header_idx):
    """Candidate company/fund-name strings from the banner region."""
    hints = []
    top = min(header_idx if header_idx > 0 else 5, 5, len(rows))
    for ri in range(top):
        for _, v in row_non_empty(rows[ri]):
            t = _cell_text(v)
            if not t or len(t) < 3 or len(t) > 60:
                continue
            if not re.search(r'[A-Za-z]', t):
                continue
            if _ENTITY_STOP.match(t):
                continue
            # a name usually has >=1 word, mostly letters, not a date/number
            if re.match(r'^[\d\W]+$', t):
                continue
            hints.append({'cell': f'r{ri + 1}', 'text': t[:60]})
    # de-dup preserving order
    out, seen = [], set()
    for h in hints:
        if h['text'].lower() not in seen:
            out.append(h)
            seen.add(h['text'].lower())
    return out[:5]


# ── Stacked-table detector ───────────────────────────────────────────────────
def _is_blank_row(row):
    return not any(v not in (None, '') for v in row)


def _numeric_count(row):
    return sum(
        1 for v in row
        if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)
    )


def _header_like(row):
    cells = row_non_empty(row)
    text_cells = sum(
        1 for _, v in cells
        if not isinstance(v, (int, float, Decimal))
        and not hasattr(v, 'isoformat')
        and _cell_text(v)
    )
    return text_cells >= 3 and _numeric_count(row) <= 1


def detect_table_starts(rows, primary_header_idx):
    """Find rows that begin a NEW stacked table (Agnikul 'Sheet7' = 4 tables).

    Conservative to avoid false positives on tall label-heavy sheets: a
    candidate must be header-like AND separated from the previous content by
    at least one fully-blank row (a real visual separator) AND followed within
    6 rows by a data row carrying >=2 numeric cells (a real table body)."""
    n = len(rows)
    starts = []
    if primary_header_idx >= 0:
        starts.append(primary_header_idx)
    for ri in range(n):
        if ri == primary_header_idx or ri <= primary_header_idx:
            continue
        if not _header_like(rows[ri]):
            continue
        # must be preceded by a blank separator
        if ri == 0 or not _is_blank_row(rows[ri - 1]):
            continue
        # must have a numeric body just below
        has_body = any(_numeric_count(rows[rj]) >= 2
                       for rj in range(ri + 1, min(ri + 7, n)))
        if not has_body:
            continue
        # don't flag two starts within 3 rows of each other
        if starts and ri - starts[-1] < 3:
            continue
        starts.append(ri)
    if not starts:
        return [primary_header_idx] if primary_header_idx >= 0 else []
    return starts


def _has_cached_numbers(rows, header_idx):
    """Missing-cache guard signal: does the body carry any numeric cached
    value? A formula-only workbook never opened in Excel returns all None."""
    start = header_idx + 1 if header_idx >= 0 else 0
    for r in rows[start:start + 200]:
        for _, v in row_non_empty(r):
            if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
                return True
    return False


def _sample_rows(rows, header_idx, header_len, limit=3):
    out = []
    start = header_idx + 1 if header_idx >= 0 else 0
    for r in rows[start:]:
        if not any(v not in (None, '') for v in r):
            continue
        if is_section_title_row(r) or is_junk_row(r):
            continue
        trim = [
            _cell_text(r[ci])[:32] if ci < len(r) else ''
            for ci in range(max(header_len, 1))
        ]
        out.append(trim)
        if len(out) >= limit:
            break
    return out


def fingerprint_sheet(sheet_name, rows):
    """Build the compact structural fingerprint for one sheet.

    `rows` is the full cached grid (list of tuples) from workbook_cache —
    read with data_only=True, so values are Excel's last computed results.
    """
    header_idx = find_header_row(rows)
    header = []
    if header_idx >= 0:
        header = [_cell_text(v) for v in rows[header_idx]]
        while header and not header[-1]:
            header.pop()
    header = header[:60]  # cap width so very wide sheets stay token-bounded

    top_rows = []
    for r in rows[:6]:
        cells = [(i, _cell_text(v)[:40]) for i, v in enumerate(r)
                 if _cell_text(v)][:40]
        if cells:
            top_rows.append(cells)

    max_col = max((len(r) for r in rows), default=0)
    populated = sum(1 for r in rows for v in r if v not in (None, ''))
    table_starts = detect_table_starts(rows, header_idx)
    unit_hints, currency_hints = detect_unit_hints(rows, header_idx)

    return {
        'sheet': sheet_name,
        'n_rows': len(rows),
        'n_cols': max_col,
        'populated_cells': populated,
        'header_row': header_idx + 1 if header_idx >= 0 else None,  # 1-based
        'header': header,
        'top_rows': top_rows,
        'sample_rows': _sample_rows(rows, header_idx, len(header)),
        'unit_hints': unit_hints,
        'currency_hints': currency_hints,
        'entity_hints': detect_entity_hints(rows, header_idx),
        'table_start_rows': [s + 1 for s in table_starts],  # 1-based
        'looks_multi_table': len(table_starts) > 1,
        'has_cached_numbers': _has_cached_numbers(rows, header_idx),
    }


def fingerprint_workbook(workbook_data):
    """workbook_data = workbook_cache.load_workbook() output.
    Returns list[fingerprint] in source sheet order."""
    out = []
    for sn in workbook_data['sheets']:
        rows = workbook_data['data'][sn]['rows']
        out.append(fingerprint_sheet(sn, rows))
    return out
