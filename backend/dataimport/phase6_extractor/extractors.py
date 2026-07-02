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
        if s not in out:
            out[s] = v
            labels[s] = label
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
    """
    if layout == 'entity_pivoted':
        events, line_items = extract_entity_pivoted(sheet_rows, column_map, sheet_name)
        result = {'events': events, 'line_items': line_items}
    elif layout == 'key_value':
        result = {'kv': extract_key_value(sheet_rows)}
    elif layout == 'wide_period':
        result = {'rows': extract_wide_period(sheet_rows, column_map, domain=domain)}
    else:
        result = {'rows': extract_tabular(sheet_rows, column_map, domain=domain)}

    # Universal booster: waterfall sheets nearly always contain KV pairs
    # ("Carry Base | 1430.60", "Clawback Provision | 10") — extract both
    # forms so unified_builder can read whichever is populated.
    if domain == 'waterfall_carry' and 'kv' not in result:
        result['kv'] = extract_key_value(sheet_rows)

    return result
