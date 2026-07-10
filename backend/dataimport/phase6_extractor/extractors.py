"""
Stage 2 — deterministic Python row extraction, one implementation per layout.

Layouts:
  tabular         : normal rows x columns table
  key_value       : two columns "Parameter | Value" → returns ONE dict
  wide_period     : one row per entity, columns are periods → unpivots to long
  entity_pivoted  : columns are entity IDs → returns (events[], line_items[])
"""
import re
from decimal import Decimal
from typing import Any

from ..canonical_schema import DOMAIN_FIELDS
from .coercers import coerce_by_canonical, to_date, to_decimal, to_str
from .helpers import (
    _cell_str, find_header_row, is_entity_id_header, is_junk_row,
    is_period_header, is_section_title_row, row_non_empty, slug,
)


def _norm_map(column_map: dict[str, str]) -> dict[str, str]:
    return {re.sub(r'\s+', ' ', str(k).strip().lower()): v
            for k, v in column_map.items()}


# ── Universal alias index — Python-side safety net for sparse Gemini column
# maps. Every canonical field's DOMAIN_FIELDS description string is parsed
# for aliases; any header cell that Gemini didn't map is looked up here as
# a fallback. Universal across every sheet layout and every fund format.
_ALIAS_INDEX_CACHE: dict[str, dict[str, str]] = {}
_ALIAS_ALPHA_RE = re.compile(r'[^a-z0-9\s]+')


def _normalize_alias(text: str) -> tuple[str, str]:
    """Return (space_form, compact_form) for a header / alias string.

    Universal normalisation — strips parenthesised suffixes ("(₹Cr)",
    "(Cr)"), unit suffixes ("%", "/mo", "/yr"), currency symbols and
    every non-alphanumeric character. Two forms are returned:
      space_form   — words preserved ("ltv cac ratio")
      compact_form — no separators   ("ltvcacratio")
    Matching either form catches "LTV/CAC" and "LTV:CAC" and "LTV CAC".
    """
    if not text:
        return '', ''
    s = str(text).lower()
    s = re.sub(r'\([^)]*\)', ' ', s)                       # (₹Cr) etc.
    s = re.sub(r'/\s*(mo|month|yr|year|qtr|quarter|day|week|wk)\b', ' ', s)
    s = _ALIAS_ALPHA_RE.sub(' ', s)                        # keep letters/digits/spaces
    s = ' '.join(s.split())
    return s, s.replace(' ', '')


# Strip these leading English phrases from alias tokens before registering,
# so "may appear as Gross Burn" becomes "Gross Burn". Universal — schema
# authors use several equivalent phrasings.
_ALIAS_LEAD_STRIP_RE = re.compile(
    r'^\s*(may\s+appear\s+as|appears?\s+as|also\s+(?:seen|written|known)\s+as|'
    r'look\s+for|seen\s+as|written\s+as|e\.?g\.?[,\s]|such\s+as|'
    r'for\s+example|i\.e\.?[,\s]+|number\s+of|count\s+of)\s+',
    re.I,
)


def _register_alias(index: dict[str, str], token: str, canon: str) -> None:
    token = _ALIAS_LEAD_STRIP_RE.sub('', token or '').strip()
    space_form, compact_form = _normalize_alias(token)
    for form in (space_form, compact_form):
        if form and 1 < len(form) <= 40 and form not in index:
            index[form] = canon


def _build_alias_index(domain: str) -> dict[str, str]:
    """Build (and cache) {normalized_alias: canonical_field} for a domain.

    Extraction rules (universal, description-format-agnostic):
      1. Canonical key is always an alias.
      2. Parenthesised segments are stripped everywhere first — authors
         use "(₹Cr)", "(examples like X, Y, Z)", "(SaaS)" etc. as annotations,
         never as alias lists.
      3. Description split at em-dash / en-dash yields (head_phrase, tail_list).
         • head is registered whole (it's the primary human name)
         • head is also split on '/' since "Net Retention / NDR" is a common shape
      4. tail is a comma-separated alias list — each token registered as an alias.
      5. Leading English phrases like "may appear as", "e.g.", "look for"
         are stripped from every candidate before normalisation.
    Canonical keys win collisions — a field's own key is never replaced
    by another field's alias.
    """
    if domain in _ALIAS_INDEX_CACHE:
        return _ALIAS_INDEX_CACHE[domain]
    field_map = DOMAIN_FIELDS.get(domain, {}) or {}
    index: dict[str, str] = {}
    # Pass 1: register EVERY canonical key first so their forms are locked in
    # before any description-derived alias can claim the slot.
    for canon in field_map:
        _register_alias(index, canon, canon)
    # Pass 2: description-derived aliases fill remaining gaps.
    for canon, desc in field_map.items():
        if not isinstance(desc, str):
            continue
        cleaned = re.sub(r'\([^)]*\)', ' ', desc)   # strip parens content
        parts = re.split(r'\s+[—–]\s+', cleaned, maxsplit=1)
        head = parts[0].strip()
        tail = parts[1].strip() if len(parts) > 1 else ''
        if 2 <= len(head) <= 80:
            _register_alias(index, head, canon)
            for token in head.split('/'):
                token = token.strip()
                if 2 <= len(token) <= 60:
                    _register_alias(index, token, canon)
        for token in tail.split(','):
            token = token.strip()
            if 2 <= len(token) <= 60:
                _register_alias(index, token, canon)
    _ALIAS_INDEX_CACHE[domain] = index
    return index


def _resolve_via_alias_index(header_cell: Any, alias_idx: dict[str, str]) -> str | None:
    if not alias_idx:
        return None
    space_form, compact_form = _normalize_alias(_cell_str(header_cell))
    return alias_idx.get(space_form) or alias_idx.get(compact_form)


# Header keywords that identify a period column universally.
_PERIOD_HEADER_KEYWORDS = (
    'period', 'quarter', 'fiscal year', 'financial year', 'reporting period',
    'as of', 'as at', 'as-at', 'as-of', 'fy', 'month', 'date', 'year',
    'nav date', 'valuation date', 'reporting date',
)


def _looks_like_period_value(v: Any) -> bool:
    """True if a cell value looks like a period token (Mar-20, Q1 FY25, date...)."""
    if v is None or v == '':
        return False
    if hasattr(v, 'isoformat'):  # date/datetime
        return True
    s = str(v).strip()
    if not s:
        return False
    if is_period_header(s):
        return True
    # Indian FY / quarter / month-year signatures
    if re.match(r'^(?:fy\s?\d{2,4}|q[1-4](?:\s?fy\s?\d{2,4})?)', s, re.I):
        return True
    if re.match(r'^\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?$', s):
        return True
    return False


def _auto_detect_period_col(header: tuple, sample_rows: list[tuple]) -> int:
    """Return the column index that carries the period/date, or -1.

    Two-tier heuristic (universal — no sheet-name hardcoding):
      1. Header keyword match on the first few columns (Quarter, Period, FY,
         Date, As of, Reporting Period, etc.).
      2. Fall back to content sniffing: pick the leftmost column whose
         sample cells look like periods (Mar-20, Q1 FY25, ISO dates)."""
    for ci, hv in enumerate(header):
        text = _cell_str(hv).lower()
        if not text:
            continue
        for kw in _PERIOD_HEADER_KEYWORDS:
            if kw in text:
                return ci
    # Content sniffing across the first ~5 columns
    for ci in range(min(6, len(header))):
        hits = 0
        for r in sample_rows:
            if ci < len(r) and _looks_like_period_value(r[ci]):
                hits += 1
        if hits >= 2:
            return ci
    return -1


# ─────────────────────────────────────────────────────────────────────────────
# tabular
# ─────────────────────────────────────────────────────────────────────────────

def extract_tabular(sheet_rows: list[tuple], column_map: dict[str, str],
                    domain: str = '') -> list[dict]:
    hdr_idx = find_header_row(sheet_rows)
    if hdr_idx < 0:
        return []
    header = sheet_rows[hdr_idx]
    norm_map = _norm_map(column_map)

    # Universal Python-side alias fallback. Gemini's Stage 1 sometimes maps
    # only a subset of a sheet's columns (e.g. only 2 of 15 on the
    # "SaaS Metrics & Burn" sheet on Multiples IV — missing every ARR /
    # MRR / NRR / Churn / CAC / LTV column, and mis-mapping Sector →
    # company_name). The alias index built from DOMAIN_FIELDS descriptions
    # fills the gap.
    #
    # Precedence rule: an alias-index hit is an EXACT header→canonical
    # match ("Company Name" literally alias for company_name). Gemini's
    # column_map is a NATURAL-LANGUAGE guess. So when both propose a column
    # for the same canonical but disagree on WHICH column, the alias-index
    # column wins. Universal — the alias hit is deterministic; Gemini isn't.
    alias_idx = _build_alias_index(domain)

    candidates: dict[str, list[int]] = {}
    alias_forced_ci: dict[str, int] = {}   # canon -> preferred column from alias hit
    for ci, hv in enumerate(header):
        key = re.sub(r'\s+', ' ', _cell_str(hv).lower())
        gemini_canon = norm_map.get(key)
        if gemini_canon:
            candidates.setdefault(gemini_canon, []).append(ci)
            continue
        alias_canon = _resolve_via_alias_index(hv, alias_idx)
        if alias_canon:
            candidates.setdefault(alias_canon, []).append(ci)
            alias_forced_ci.setdefault(alias_canon, ci)

    # Sample the first few data rows so we can score column candidates
    sample_rows: list[tuple] = []
    for ri in range(hdr_idx + 1, min(hdr_idx + 8, len(sheet_rows))):
        r = sheet_rows[ri]
        if row_non_empty(r) and not is_junk_row(r):
            sample_rows.append(r)
        if len(sample_rows) >= 3:
            break

    def _score(ci: int) -> int:
        score = 0
        for r in sample_rows:
            if ci >= len(r):
                continue
            v = r[ci]
            if v is None:
                continue
            s = str(v).strip()
            if not s:
                continue
            score += 1
            score += min(len(s) // 10, 5)
            if ' ' in s:
                score += 3
            if re.match(r'^(lp|pc|inv|co|d|cc|f\d*c)\-?\d+$', s.lower()):
                score -= 5
        return score

    canon_cols: dict[str, int] = {}
    for canon, cols in candidates.items():
        # Universal precedence: an alias-index hit is an EXACT header→canonical
        # match. When it disagrees with Gemini's guess, alias always wins.
        if canon in alias_forced_ci and alias_forced_ci[canon] in cols:
            canon_cols[canon] = alias_forced_ci[canon]
            continue
        if len(cols) == 1:
            canon_cols[canon] = cols[0]
            continue
        # For ID canonicals like lp_id / company_id, prefer the SHORT ID-looking
        # column (lowest score). For name canonicals, prefer the HIGH-scoring
        # (real-name) column.
        if canon.endswith('_id') or canon in ('lp_id', 'company_id', 'entity_id'):
            canon_cols[canon] = min(cols, key=_score)
        else:
            canon_cols[canon] = max(cols, key=_score)

    # Universal period auto-detect + granularity resolver.
    #
    # Gemini is inconsistent about which period column it maps. On the same
    # sheet across runs it may map "FY" → `financial_year`, then "FY" →
    # `period`, then not map anything at all. Meanwhile the sheet also
    # publishes a finer-granularity Quarter/Month column. If we accept the
    # coarser column, quarterly rows collapse to annual ones via same-date
    # update_or_create in the persister (13 → 7).
    #
    # Rule (universal): whenever a period-like column exists, prefer the
    # column with the MOST DISTINCT sample values (i.e. finest granularity).
    # This works for any layout because higher granularity = more variance
    # across rows.
    _current_period_col = None
    for c in ('period', 'nav_date', 'investment_date', 'valuation_date',
              'call_date', 'distribution_date', 'exit_date', 'financial_year'):
        if c in canon_cols:
            _current_period_col = canon_cols[c]
            break

    detected_period_col = _auto_detect_period_col(header, sample_rows)

    def _distinctness(ci: int) -> int:
        if ci is None or ci < 0:
            return 0
        seen: set = set()
        for r in sample_rows:
            if ci < len(r) and r[ci] not in (None, ''):
                seen.add(str(r[ci]).strip())
        return len(seen)

    if detected_period_col >= 0:
        curr_gran = _distinctness(_current_period_col) if _current_period_col is not None else 0
        det_gran = _distinctness(detected_period_col)
        # Prefer detected column when it's strictly finer, OR when nothing
        # was mapped before. Never overwrite a legitimate date field
        # (nav_date etc.) — only touch the free 'period' slot.
        if det_gran > curr_gran and detected_period_col != _current_period_col:
            canon_cols['period'] = detected_period_col

    if not canon_cols:
        return []

    out: list[dict] = []
    for ri in range(hdr_idx + 1, len(sheet_rows)):
        row = sheet_rows[ri]
        if not row_non_empty(row):
            continue
        if is_junk_row(row) or is_section_title_row(row):
            continue
        rec: dict[str, Any] = {}
        any_val = False
        for canon, ci in canon_cols.items():
            if ci >= len(row):
                continue
            v = coerce_by_canonical(canon, row[ci])
            if v is not None:
                rec[canon] = v
                any_val = True
        if any_val:
            out.append(rec)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# key_value
# ─────────────────────────────────────────────────────────────────────────────

def extract_key_value(sheet_rows: list[tuple]) -> dict[str, Any]:
    """Return ONE dict aggregating every (label, value) pair. First-writer wins.

    Universal: strips box-drawing characters (├─, └─, ┌─, │) and leading
    dashes/dots that fund managers use to render tree/hierarchical labels.
    Without this, "├─ CLAWBACK PROVISION" becomes slug 'clawback_provision'
    only after the box-drawing char is stripped.

    The result dict has slug keys → value, PLUS a special key
    '__labels__' that maps slug → original (post-strip) human label. This
    lets downstream consumers (Phase 4 reconciler, provenance display)
    know the exact wording of the source row."""
    out: dict[str, Any] = {}
    labels: dict[str, str] = {}
    for row in sheet_rows:
        cells = row_non_empty(row)
        if len(cells) < 2:
            continue
        if is_junk_row(row) or is_section_title_row(row):
            continue
        _, key_val = cells[0]
        _, val_val = cells[1]
        label = _cell_str(key_val)
        if not label or len(label) > 200:
            continue
        # Strip tree-drawing characters and leading punctuation so labels
        # like "├─ CLAWBACK PROVISION (Distributed – Entitlement, INR Cr)"
        # normalize to "CLAWBACK PROVISION (Distributed – Entitlement, INR Cr)"
        # and then slug cleanly.
        label = re.sub(r'^[\s│├└┌┐┘┤┬┴┼─\-•\.\*]+', '', label).strip()
        # Also drop parenthetical trailing units so
        # "Carry Base (Total Profit above Capital, INR Cr)" → "Carry Base"
        # → slug 'carry_base' matches _WATERFALL_SLUG_ALIAS.
        label_short = re.sub(r'\s*\([^)]*\)\s*$', '', label).strip()
        s = slug(label_short) or slug(label)
        if not s:
            continue

        # Fix (2026-07-10) — trailing-parenthesis semantic disambiguation.
        # When two rows in the same KV sheet share a base label but differ
        # only by the trailing "(...)" unit hint — e.g.
        #     "GP Commitment (INR Cr) | 25"      ← 25 crore, an AMOUNT
        #     "GP Commitment (%)      | 2.50%"   ← 2.5%, a PERCENTAGE
        # the current strip-parens-then-slug collapses both to the same slug
        # `gp_commitment` and the first-writer-wins rule silently drops the
        # semantically-distinct second row.
        #
        # Fix: when the trailing parenthesis carries a recognised unit
        # marker, emit an ADDITIONAL slug that includes a unit suffix
        # (`_pct` for %/percent, `_inr_cr` for INR Cr, etc.). Both slugs
        # get stored so downstream consumers can pick the semantically
        # correct one via the alias map. Universal — works for any
        # `<Field> (<unit>)` pattern.
        s_extra: str | None = None
        _paren = re.search(r'\(([^)]+)\)\s*$', label)
        if _paren:
            _u = _paren.group(1).strip().lower()
            _u = re.sub(r'\s+', ' ', _u)
            if _u in ('%', 'pct', 'percent', 'p.a.', 'p.a', 'per annum',
                      '% p.a.', 'per year'):
                s_extra = s + '_pct'
            elif re.match(r'^(inr|₹|rs|rs\.|usd|eur|gbp)\s*(cr|crore|crores|lakh|lakhs|mn|million|bn|billion)?$', _u):
                # Preserve unit in slug when it disambiguates AMOUNT vs PCT
                _unit_tail = 'inr_cr' if ('cr' in _u or _u in ('inr', '₹', 'rs', 'rs.')) else 'amount'
                s_extra = s + '_' + _unit_tail

        v: Any = None
        d = to_decimal(val_val)
        if d is not None:
            v = d
        else:
            dt = to_date(val_val)
            if dt is not None:
                v = dt
            else:
                v = to_str(val_val)
        if v is None:
            continue
        # Store under short slug (backwards-compat) AND the unit-qualified
        # slug (when present) — first-writer-wins per slug.
        for _slug in (s, s_extra):
            if _slug and _slug not in out:
                out[_slug] = v
                labels[_slug] = label
    if labels:
        out['__labels__'] = labels
    return out


# ─────────────────────────────────────────────────────────────────────────────
# wide_period — entity rows × period columns
# ─────────────────────────────────────────────────────────────────────────────

def extract_wide_period(sheet_rows: list[tuple], column_map: dict[str, str],
                        domain: str = '') -> list[dict]:
    hdr_idx = find_header_row(sheet_rows)
    if hdr_idx < 0:
        return []
    header = sheet_rows[hdr_idx]
    norm_map = _norm_map(column_map)
    alias_idx = _build_alias_index(domain)

    # Universal column-scoring for entity columns — mirrors extract_tabular.
    # When multiple headers map to the same canonical (e.g. both "Co_ID" and
    # "Company_Name" map to `company_name`), score each candidate column
    # against sample data rows and pick the best one. Long text with spaces
    # (real company names) beats short ID-shaped values (PC001). Without this,
    # wide-period sheets emitted `company_name='PC001'` and the persister's
    # `portfolio_company__name` FK lookup found nothing → auto-valuation
    # fallback wrongly fired.
    candidates: dict[str, list[int]] = {}
    alias_forced_ci: dict[str, int] = {}
    period_cols: dict[int, str] = {}
    period_value_field = 'period_value'

    for ci, hv in enumerate(header):
        text = _cell_str(hv)
        if not text:
            continue
        key = re.sub(r'\s+', ' ', text.lower())
        if is_period_header(text):
            period_cols[ci] = text
            gemini_canon = norm_map.get(key)
            if gemini_canon:
                period_value_field = gemini_canon
            continue
        gemini_canon = norm_map.get(key)
        if gemini_canon:
            candidates.setdefault(gemini_canon, []).append(ci)
            continue
        alias_canon = _resolve_via_alias_index(hv, alias_idx)
        if alias_canon:
            candidates.setdefault(alias_canon, []).append(ci)
            alias_forced_ci.setdefault(alias_canon, ci)

    # Sample data rows to score column candidates (same 3-row window as tabular)
    sample_rows: list[tuple] = []
    for ri in range(hdr_idx + 1, min(hdr_idx + 8, len(sheet_rows))):
        r = sheet_rows[ri]
        if row_non_empty(r) and not is_junk_row(r):
            sample_rows.append(r)
        if len(sample_rows) >= 3:
            break

    def _score(ci: int) -> int:
        score = 0
        for r in sample_rows:
            if ci >= len(r):
                continue
            v = r[ci]
            if v is None:
                continue
            s = str(v).strip()
            if not s:
                continue
            score += 1
            score += min(len(s) // 10, 5)
            if ' ' in s:
                score += 3
            if re.match(r'^(lp|pc|inv|co|d|cc|f\d*c)\-?\d+$', s.lower()):
                score -= 5
        return score

    entity_cols: dict[str, int] = {}
    for canon, cols in candidates.items():
        if canon in alias_forced_ci and alias_forced_ci[canon] in cols:
            entity_cols[canon] = alias_forced_ci[canon]
            continue
        if len(cols) == 1:
            entity_cols[canon] = cols[0]
            continue
        # ID canonicals prefer short (low-score) col; everything else prefers rich text
        if canon.endswith('_id') or canon in ('lp_id', 'company_id', 'entity_id'):
            entity_cols[canon] = min(cols, key=_score)
        else:
            entity_cols[canon] = max(cols, key=_score)

    if not period_cols or not entity_cols:
        # No periods detected — fall back to tabular so we don't lose the rows
        return extract_tabular(sheet_rows, column_map, domain=domain)

    out: list[dict] = []
    for ri in range(hdr_idx + 1, len(sheet_rows)):
        row = sheet_rows[ri]
        if not row_non_empty(row):
            continue
        if is_junk_row(row) or is_section_title_row(row):
            continue
        ent: dict[str, Any] = {}
        any_ent = False
        for canon, ci in entity_cols.items():
            if ci >= len(row):
                continue
            v = coerce_by_canonical(canon, row[ci])
            if v is not None:
                ent[canon] = v
                any_ent = True
        if not any_ent:
            continue
        for ci, period_label in period_cols.items():
            if ci >= len(row) or row[ci] in (None, ''):
                continue
            val = to_decimal(row[ci])
            if val is None:
                continue
            rec = dict(ent)
            rec['period'] = period_label
            rec['valuation_date'] = to_date(period_label) or period_label
            rec[period_value_field] = val
            out.append(rec)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# entity_pivoted — LP columns × attribute rows OR event rows × LP columns
# ─────────────────────────────────────────────────────────────────────────────

def extract_entity_pivoted(
    sheet_rows: list[tuple],
    column_map: dict[str, str],
    sheet_name: str,
) -> tuple[list[dict], list[dict]]:
    """Extract entity-pivoted sheet. Returns (events[], line_items[]).

    events[]     — one dict per data row using LEFT (event-level) fields
    line_items[] — one dict per (event, entity_id) whose amount is non-zero
    """
    hdr_idx = find_header_row(sheet_rows)
    if hdr_idx < 0:
        return [], []
    header = sheet_rows[hdr_idx]

    entity_cols: dict[int, str] = {}
    for ci, hv in enumerate(header):
        text = _cell_str(hv)
        text_stripped = re.sub(r'\s*\([^)]*\)\s*$', '', text).strip()
        if is_entity_id_header(text_stripped):
            entity_cols[ci] = text_stripped

    norm_map = _norm_map(column_map)
    canon_cols: dict[str, int] = {}
    for ci, hv in enumerate(header):
        if ci in entity_cols:
            continue
        key = re.sub(r'\s+', ' ', _cell_str(hv).lower())
        canon = norm_map.get(key)
        if canon:
            canon_cols.setdefault(canon, ci)

    events: list[dict] = []
    line_items: list[dict] = []
    for ri in range(hdr_idx + 1, len(sheet_rows)):
        row = sheet_rows[ri]
        if not row_non_empty(row):
            continue
        if is_junk_row(row) or is_section_title_row(row):
            continue

        event: dict[str, Any] = {}
        any_event_field = False
        for canon, ci in canon_cols.items():
            if ci >= len(row) or row[ci] in (None, ''):
                continue
            v = coerce_by_canonical(canon, row[ci])
            if v is not None:
                event[canon] = v
                any_event_field = True
        if not any_event_field:
            continue
        events.append(event)

        for ci, entity_id in entity_cols.items():
            if ci >= len(row) or row[ci] in (None, ''):
                continue
            amt = to_decimal(row[ci])
            if amt is None:
                continue
            line_items.append({
                'entity_id': entity_id,
                'amount': amt,
                'event_index': len(events) - 1,
            })

    return events, line_items


# ─────────────────────────────────────────────────────────────────────────────
# dispatch — call the right extractor for the given layout
# ─────────────────────────────────────────────────────────────────────────────

def _find_all_header_rows(sheet_rows: list[tuple],
                          min_text_cells: int = 3) -> list[int]:
    """Return row indices of ALL header-shaped rows in the sheet.

    Detects multi-section tabular sheets (two or more stacked tables in one
    physical sheet, separated by a blank row and/or a section-title row).
    Each returned index is a candidate header row that can anchor its own
    tabular extraction slice.

    A header candidate must:
      1. Have at least `min_text_cells` non-numeric, non-date text cells
      2. Not itself be a bare section-title row
      3. For candidates AFTER the first: be preceded by at least one blank
         row OR a section-title row (proves a section boundary)

    Universal — no sheet, domain, or fund hardcoding. Sheets with a single
    header return a one-element list; the caller falls back to the existing
    single-header extractor path.
    """
    n = len(sheet_rows)
    if n == 0:
        return []

    def _text_cell_count(row: tuple) -> int:
        return sum(
            1 for _, v in row_non_empty(row)
            if not isinstance(v, (int, float, Decimal))
            and not hasattr(v, 'isoformat')
            and _cell_str(v)
        )

    def _is_header_candidate(ri: int) -> bool:
        cells = row_non_empty(sheet_rows[ri])
        if len(cells) < min_text_cells:
            return False
        if _text_cell_count(sheet_rows[ri]) < min_text_cells:
            return False
        if is_section_title_row(sheet_rows[ri]):
            return False
        return True

    headers: list[int] = []
    for ri in range(n):
        if not _is_header_candidate(ri):
            continue
        if not headers:
            headers.append(ri)
            continue
        last = headers[-1]
        if ri - last < 3:
            continue
        has_separator = False
        for pi in range(last + 1, ri):
            if not row_non_empty(sheet_rows[pi]):
                has_separator = True
                break
            if is_section_title_row(sheet_rows[pi]):
                has_separator = True
                break
        if has_separator:
            headers.append(ri)
    return headers


def _find_deeper_table_start(sheet_rows: list[tuple],
                             min_text_cells: int = 5) -> int:
    """Universal: scan a sheet for a tabular section that starts AFTER the
    first 20 rows. Returns the row index of the deeper table header, or -1
    if none. Used to detect multi-section sheets (KV top + tabular bottom).

    The row must have >= min_text_cells text cells and must not be a bare
    section-title row.
    """
    start = 20
    best = -1
    best_score = 0
    for ri in range(start, len(sheet_rows)):
        cells = row_non_empty(sheet_rows[ri])
        if len(cells) < min_text_cells:
            continue
        text_cells = sum(
            1 for _, v in cells
            if not isinstance(v, (int, float, Decimal))
            and not hasattr(v, 'isoformat')
            and _cell_str(v)
        )
        if text_cells >= min_text_cells and text_cells > best_score:
            if not is_section_title_row(sheet_rows[ri]):
                best_score = text_cells
                best = ri
    return best


# Solution A — Multi-section extractor for stacked tables.
#
# Some workbooks pack two logical tables into one sheet (e.g. Sequoia's
# "Exits & Distributions" — Exits on rows 3-10, blank separator on 11,
# "DISTRIBUTION SCHEDULE" title on row 12, its own header on row 13, data
# on rows 14-16). Gemini classifies the sheet once by the top table only,
# and the tabular extractor stops when the top table ends — the second
# section is silently lost.
#
# `_find_stacked_table_start` scans below the primary section for another
# header row. Universal: keyword hints are per-domain but the mechanism
# works for any two-table sheet.
def _find_stacked_table_start(sheet_rows: list[tuple],
                              min_start: int = 5,
                              keyword_hints: list[str] | None = None,
                              min_text_cells: int = 3) -> int:
    """Scan for a header-like row below the primary section. Returns index
    of the deeper header, or -1 if none. A header candidate must:
      1. Sit below min_start rows
      2. Be preceded by at least one fully-blank row (separator)
      3. Contain min_text_cells+ text cells (looks like column headers)
      4. NOT be a bare title row
      5. If keyword_hints given, EITHER this row OR the two rows above
         must contain at least one hint keyword (e.g. "distribution")
    """
    kw_lc = [k.lower() for k in (keyword_hints or [])]

    def _row_text(row) -> str:
        return ' '.join(
            _cell_str(v) for _, v in row_non_empty(row)
        ).lower()

    for ri in range(min_start, len(sheet_rows)):
        cells = row_non_empty(sheet_rows[ri])
        if len(cells) < min_text_cells:
            continue
        # Must be preceded by at least one blank row (section separator)
        prev_blank = False
        for pi in range(max(0, ri - 3), ri):
            if not row_non_empty(sheet_rows[pi]):
                prev_blank = True
                break
        if not prev_blank:
            continue
        # Header-shaped row: mostly text, short strings, no big numbers
        text_cells = sum(
            1 for _, v in cells
            if not isinstance(v, (int, float, Decimal))
            and not hasattr(v, 'isoformat')
            and _cell_str(v)
        )
        if text_cells < min_text_cells:
            continue
        if is_section_title_row(sheet_rows[ri]):
            continue
        # Keyword gate: this row or preceding 2 rows must mention a hint word
        if kw_lc:
            surrounding = ' '.join(
                _row_text(sheet_rows[i])
                for i in range(max(0, ri - 2), ri + 1)
            )
            if not any(k in surrounding for k in kw_lc):
                continue
        return ri
    return -1


def extract_sheet(sheet_rows: list[tuple], layout: str,
                  column_map: dict[str, str], sheet_name: str,
                  domain: str = '') -> dict:
    """Run the appropriate extractor for `layout`. Returns a dict describing
    what came out — the key depends on the layout:
      tabular / wide_period → {'rows': [...]}
      key_value             → {'kv': {...}}
      entity_pivoted        → {'events': [...], 'line_items': [...]}

    Universal override: for the waterfall_carry domain we ALWAYS run
    key_value extraction (in addition to whatever Gemini said), because
    waterfall workings sheets are almost always label→value layouts even
    when they visually look like a table. Populates BOTH 'rows' and 'kv'
    so downstream consumers see whichever they expect.

    Universal multi-section rescue (added 2026-07-03): when Gemini classifies
    a long sheet as "key_value" but a proper tabular section exists further
    down (e.g. NAV_CALC has "SECTION A — Line Item | Amount" as KV at rows
    5-17 and "SECTION C — MONTHLY NAV HISTORY (36 Months)" as a table at
    rows 31-67), also run the tabular extractor on the deeper slice so the
    hidden history is recovered. This is additive — the KV output is
    preserved unchanged; a new `rows` array carries the deeper rows.
    """
    if layout == 'entity_pivoted':
        events, line_items = extract_entity_pivoted(sheet_rows, column_map, sheet_name)
        result = {'events': events, 'line_items': line_items}
    elif layout == 'key_value':
        result = {'kv': extract_key_value(sheet_rows)}
    elif layout == 'wide_period':
        result = {'rows': extract_wide_period(sheet_rows, column_map, domain=domain)}
    else:
        # Fix D — universal multi-section tabular extraction.
        #
        # Some workbooks stack two logically-separate tables into one
        # tabular sheet (e.g. TrackFundAI Master CAPITAL_CALLS: top =
        # fund-level call ledger; bottom = LP-level per-call pivot). The
        # single-header extractor picks the row with the MOST text cells,
        # which is often the LP-pivot bottom, and silently skips the top.
        #
        # Solution: detect ALL header rows, extract each slice through the
        # existing single-header extractor, merge with signature dedup so
        # rows caught by both slices only appear once. Non-regressing:
        # single-header sheets get one header index and identical output
        # to the pre-Fix-D path.
        _header_rows = _find_all_header_rows(sheet_rows)
        if len(_header_rows) > 1:
            _merged_rows: list[dict] = []
            _seen_sigs: set = set()
            for _i, _hi in enumerate(_header_rows):
                _end = (_header_rows[_i + 1] if _i + 1 < len(_header_rows)
                        else len(sheet_rows))
                _slice = sheet_rows[_hi:_end]
                _slice_rows = extract_tabular(_slice, column_map, domain=domain)
                for _r in _slice_rows:
                    _sig = tuple(sorted(
                        (k, str(v)) for k, v in _r.items() if v is not None
                    ))
                    if _sig and _sig not in _seen_sigs:
                        _merged_rows.append(_r)
                        _seen_sigs.add(_sig)
            result = {'rows': _merged_rows}
        else:
            result = {'rows': extract_tabular(sheet_rows, column_map, domain=domain)}

    # Universal booster: waterfall sheets nearly always contain KV pairs
    # ("Carry Base | 1430.60", "Clawback Provision | 10") — extract both
    # forms so unified_builder can read whichever is populated.
    if domain == 'waterfall_carry' and 'kv' not in result:
        result['kv'] = extract_key_value(sheet_rows)

    # Universal multi-section rescue for KV sheets with a hidden tabular tail.
    # Only fires when the sheet is long (>=30 rows) and a deeper header exists.
    # Uses domain-specific alias index by re-running tabular on the sliced tail.
    # For nav_calculation sheets whose deeper section is a monthly NAV walk,
    # temporarily use 'nav_accounting' as the alias-index domain so that "Fund
    # NAV", "Portfolio FV", "Cash", "NAV/Unit" get aliased to total_nav,
    # investments_at_fair_value, cash_and_equivalents, nav_per_unit fields.
    if layout == 'key_value' and len(sheet_rows) >= 30 and 'rows' not in result:
        deeper_idx = _find_deeper_table_start(sheet_rows)
        if deeper_idx > 20:
            # Effective alias-index domain for the deeper section:
            # nav_calculation sheets almost always publish a NAV walk deep
            # inside them; pretend it's a nav_accounting section so the
            # alias index picks up total_nav / investments_at_fair_value.
            _deeper_domain = 'nav_accounting' if domain == 'nav_calculation' else domain
            deeper_rows = extract_tabular(
                sheet_rows[deeper_idx:],
                column_map={},          # empty — alias index handles it
                domain=_deeper_domain,
            )
            if deeper_rows:
                result['rows'] = deeper_rows

    # ── Solution A — Stacked-section rescue for tabular sheets ────────────
    #
    # Some workbooks stack TWO tables in one tabular sheet, separated by a
    # blank row and a title. The primary extractor above stops when the
    # first section's data runs out, so the second table is dropped.
    #
    # This rescue re-scans the sheet for a stacked sub-section whose
    # neighbourhood contains a domain-relevant keyword, extracts it as a
    # tabular block, and MERGES the resulting rows into result['rows'].
    # unified_builder.py routes them by row shape (distribution rows have
    # distribution_number / total_gross_amount but no exit_date).
    #
    # Currently enabled for exits_distributions (Sequoia "DISTRIBUTION
    # SCHEDULE" below Exits table). Additive: only fires when the keyword
    # signal is present; sheets with a single table see no change.
    _STACKED_HINTS_BY_DOMAIN = {
        'exits_distributions': [
            'distribution', 'distributed', 'payout', 'dividend', 'dist#',
            'distribution schedule', 'lp distribution',
        ],
    }
    if (layout == 'tabular'
            and domain in _STACKED_HINTS_BY_DOMAIN
            and 'rows' in result
            and len(sheet_rows) >= 8):
        sub_idx = _find_stacked_table_start(
            sheet_rows,
            min_start=max(4, len(result['rows'])),
            keyword_hints=_STACKED_HINTS_BY_DOMAIN[domain],
        )
        if sub_idx > 0:
            sub_rows = extract_tabular(
                sheet_rows[sub_idx:],
                column_map={},          # empty — alias index handles it
                domain=domain,
            )
            # Dedup guard: only append rows that don't already exist by
            # a strong identity signal (distribution_number+amount OR
            # distribution_date+amount). Prevents double-counting if the
            # primary extractor accidentally caught these.
            existing_sigs = {
                (str(r.get('distribution_number') or ''),
                 str(r.get('distribution_date') or ''),
                 str(r.get('total_gross_amount') or r.get('total_net_amount') or ''))
                for r in result['rows']
            }
            for r in sub_rows:
                sig = (str(r.get('distribution_number') or ''),
                       str(r.get('distribution_date') or ''),
                       str(r.get('total_gross_amount') or r.get('total_net_amount') or ''))
                if sig != ('', '', '') and sig not in existing_sigs:
                    result['rows'].append(r)
                    existing_sigs.add(sig)

    # ── Fix C — Wide-period rescue for financials_pl_bva ────────────────────
    # Monthly P&L / MIS sheets typically ship as:
    #    Line Item          | Oct-24 | Nov-24 | Dec-24 | ... | H2 Total
    #    Portfolio Revenue  |   254  |   241  |   ...  | ... |   ...
    # Gemini frequently classifies these as `tabular` because the sheet does
    # have a proper header row and looks table-shaped. But the "columns" are
    # actually periods — the correct interpretation is wide_period → unpivot
    # into row-per-period.
    #
    # Detection is universal:
    #   • domain == financials_pl_bva
    #   • Gemini said tabular
    #   • header row has >= 3 period-shaped columns (via helpers.is_period_header)
    # When those three signals coincide we ALSO run extract_wide_period and
    # replace the tabular rows with the unpivoted rows. Additive safety net:
    # if wide-period detection finds nothing, tabular output is preserved.
    #
    # No Gemini tokens, no new sheet-name rules, no per-file overrides.
    # Sheets whose tabular extraction was already correct (no period columns
    # in the header) are unaffected.
    if (layout == 'tabular'
            and domain == 'financials_pl_bva'
            and 'rows' in result
            and len(sheet_rows) >= 4):
        hdr_idx = find_header_row(sheet_rows)
        if hdr_idx >= 0:
            header = sheet_rows[hdr_idx]
            period_col_count = sum(
                1 for hv in header if is_period_header(_cell_str(hv))
            )
            if period_col_count >= 3:
                wp_rows = extract_wide_period(sheet_rows, column_map, domain=domain)
                if wp_rows:
                    # Prefer unpivoted rows — they preserve the value×period
                    # matrix that tabular extraction inherently flattens.
                    result['rows'] = wp_rows

    # Fix 3 (companion) — Wide-period rescue for NAV sheets. Sequoia's
    # `NAV & Accounting` sheet publishes NAV as a pivot: row-labels are
    # balance-sheet components, columns are periods (Oct-24, Nov-24, ...).
    # Gemini often classifies these as `tabular` because the layout has a
    # header row. Run wide-period extraction when >=3 period columns are
    # detected so unified_builder's line-item → NAV pivot has rows to work
    # with. Additive: if wide-period returns nothing, tabular rows are
    # preserved.
    if (layout == 'tabular'
            and domain in ('nav_accounting', 'nav_calculation')
            and 'rows' in result
            and len(sheet_rows) >= 4):
        hdr_idx = find_header_row(sheet_rows)
        if hdr_idx >= 0:
            header = sheet_rows[hdr_idx]
            period_col_count = sum(
                1 for hv in header if is_period_header(_cell_str(hv))
            )
            if period_col_count >= 3:
                wp_rows = extract_wide_period(sheet_rows, column_map, domain=domain)
                if wp_rows:
                    # Merge without duplicates: tabular rows stay for
                    # single-row shape (NAV Summary key/value block) and
                    # wide-period rows add the per-period history.
                    _existing_sigs = {
                        tuple(sorted((k, str(v)) for k, v in r.items() if v is not None))
                        for r in result['rows']
                    }
                    for r in wp_rows:
                        _sig = tuple(sorted((k, str(v)) for k, v in r.items() if v is not None))
                        if _sig and _sig not in _existing_sigs:
                            result['rows'].append(r)
                            _existing_sigs.add(_sig)

    # ── Fix 3 — Headerless MIS fallback for financials_pl_bva ──────────────
    # Some fund MIS sheets omit the period-header row entirely — the sheet
    # is a title + implicit monthly columns + a trailing total column, e.g.
    #    Row 1: 'MONTHLY P&L (MIS) | ... | Portfolio Aggregate | Rs Crore'
    #    Row 2: 'REVENUE'
    #    Row 3: 'Portfolio Revenue (Aggregate)', 260.3, 259.6, ..., 1586.5
    # There is no header row at all — `find_header_row` returns -1, the
    # wide-period rescue above cannot fire, and tabular extraction yields
    # zero rows (no header → no column mapping).
    #
    # This secondary rescue extracts one fund-level row per data row shaped
    # as [text_label, num, num, num, ...]. It emits {line_item, period_value}
    # using the LAST numeric column (universally the period aggregate/total
    # in AIF MIS sheets). The pivot in unified_builder.py groups them into a
    # single "__fund_total__" bucket, attaches the sentinel PortfolioCompany,
    # and the persister derives GM% / EBITDA% from Revenue + COGS + EBITDA.
    #
    # Universal safeguards:
    #   • Fires only when the earlier extractors produced zero rows.
    #   • Row 0 must be a non-numeric label; ≥3 numeric cells required.
    #   • Junk / section-title rows are filtered.
    #   • Rescue rows still go through _canon_pl_line_item at pivot time,
    #     so labels that aren't real P&L / BS line items are silently
    #     dropped — no data fabrication.
    if (domain == 'financials_pl_bva'
            and 'rows' in result
            and not result['rows']
            and len(sheet_rows) >= 3):
        result['rows'] = _extract_headerless_pl_totals(sheet_rows)

    return result


def _extract_headerless_pl_totals(sheet_rows: list[tuple]) -> list[dict]:
    """Emit {line_item, period_value} rows for label + numeric-total shape.

    Selection rule (universal):
      • row's first non-empty cell is a text label (not a date, not numeric)
      • the row contains at least 3 numeric cells after the label
      • the row is not junk (subtotals, disclaimers) and not a section title
    The LAST numeric cell in the row is chosen as `period_value` — this is
    the universal AIF MIS convention (trailing "H2 Total", "FY Total",
    "Cumulative" column). Callers use the pivot in unified_builder to route
    these into fund-level KPI persistence.
    """
    out: list[dict] = []
    for row in sheet_rows:
        cells = row_non_empty(row)
        if len(cells) < 4:
            continue
        # First non-empty cell must be a text label.
        _first_idx, first_val = cells[0]
        if isinstance(first_val, (int, float, Decimal)):
            continue
        if hasattr(first_val, 'isoformat'):     # datetime / date object
            continue
        label = _cell_str(first_val)
        if not label:
            continue
        if is_junk_row(row) or is_section_title_row(row):
            continue
        # Collect trailing numeric cells.
        numerics: list = []
        for _, v in cells[1:]:
            if isinstance(v, (int, float, Decimal)):
                numerics.append(v)
                continue
            nv = to_decimal(v)
            if nv is not None:
                numerics.append(nv)
        if len(numerics) < 3:
            continue
        # Last numeric is the period total (universal MIS convention).
        period_val = to_decimal(numerics[-1])
        if period_val is None:
            continue
        out.append({'line_item': label, 'period_value': period_val})
    return out
