"""
Stage MAP — the ONLY place Gemini is used in pre-ingestion.

Input : the structural fingerprints (no data rows).
Output: for every sheet, a MAP describing how to extract it deterministically:
        {domain, layout, unit, unit_confidence, entity, entity_confidence,
         header_row, label_column, value_columns, column_map, skip, notes}.

Gemini never emits a data value. It only decides *where* the data is and
*what each column means*. mover.py then copies the actual cached values.

Batching: sheets are chunked (~16 per call) so each call stays small — this
keeps input/output tokens low, which is what actually suppresses hallucination
(not row-chunking). A 67-sheet workbook becomes ~4 small calls, not one huge
one and not 67 tiny ones.
"""
import json
import logging
import time

from ..canonical_schema import DOMAIN_FIELDS
from ..gemini_column_mapper import _call_gemini

logger = logging.getLogger(__name__)

SHEETS_PER_CALL = 8
_BATCH_RETRIES = 3
_BATCH_BACKOFF_S = 8


def _call_with_retry(prompt, label, timeout_ms):
    """Wrap _call_gemini with a batch-level retry for transient network drops
    (RemoteDisconnected / ConnectionError / TransportError). A single flaky
    connection must never drop an entire client file's classification."""
    last = None
    for attempt in range(1, _BATCH_RETRIES + 1):
        try:
            return _call_gemini(prompt, context_label=label, timeout_ms=timeout_ms) or {}
        except Exception as e:  # noqa: BLE001 — transient network/HTTP errors
            last = e
            transient = any(t in type(e).__name__ for t in
                            ('Transport', 'Connection', 'Timeout', 'RemoteDisconnected')) \
                or 'aborted' in str(e).lower() or 'RESOURCE_EXHAUSTED' in str(e)
            if attempt < _BATCH_RETRIES and transient:
                wait = _BATCH_BACKOFF_S * attempt
                logger.warning(f'[preingest.map] {label} transient error '
                               f'({type(e).__name__}); retry {attempt}/{_BATCH_RETRIES} '
                               f'in {wait}s')
                time.sleep(wait)
                continue
            raise
    raise last


def _domain_catalogue():
    return json.dumps(
        {d: list(DOMAIN_FIELDS[d].keys()) for d in sorted(DOMAIN_FIELDS.keys())},
        indent=0,
    )


def _domain_menu():
    return ', '.join(sorted(DOMAIN_FIELDS.keys()))


def _fingerprint_block(fp):
    """Render one fingerprint compactly for the prompt."""
    lines = [f'\n=== SHEET "{fp["sheet"]}" ===']
    lines.append(f'  dimensions: {fp["n_rows"]} rows x {fp["n_cols"]} cols, '
                 f'{fp["populated_cells"]} populated cells')
    lines.append(f'  detected_header_row (1-based): {fp["header_row"]}')
    if fp['unit_hints']:
        lines.append(f'  scale_text_seen: {fp["unit_hints"]}')
    if fp.get('currency_hints'):
        lines.append(f'  currency_text_seen: {fp["currency_hints"]}')
    if fp['entity_hints']:
        lines.append(f'  entity_text_seen: {[h["text"] for h in fp["entity_hints"]]}')
    if fp['looks_multi_table']:
        lines.append(f'  MULTIPLE_TABLES: header-like rows at (1-based) '
                     f'{fp["table_start_rows"][:12]}')
    if not fp['has_cached_numbers']:
        lines.append('  WARNING: no cached numeric values found in body '
                     '(file may be formula-only / never opened in Excel)')
    lines.append(f'  header: {fp["header"]}')
    for i, s in enumerate(fp['sample_rows']):
        lines.append(f'  sample{i + 1}: {s}')
    for tr in fp['top_rows'][:4]:
        lines.append(f'  top_row_cells: {tr}')
    return '\n'.join(lines)


def build_map_prompt(fingerprints, file_label):
    blocks = '\n'.join(_fingerprint_block(fp) for fp in fingerprints)
    return f"""You are a fund and company-finance data analyst (works across any
market, currency, and reporting convention). You are given
STRUCTURAL FINGERPRINTS of the sheets in one client Excel workbook
("{file_label}"). You DO NOT see the full data — only headers, a couple of
sample rows, and detected hints. Your job is to describe HOW to extract each
sheet. You must NEVER output a data value; only mapping instructions.

For EACH sheet return an object with:
  domain          — canonical business area. Pick ONE of:
                    {_domain_menu()}.
                    Use null for Cover/Index/Dashboard/config/narrative sheets
                    (e.g. "To do list", "Help needed", "Board minutes", "Main"/
                    control sheets, colour-key sheets).
  layout          — one of:
                    "tabular"        : normal rows x columns
                    "key_value"      : label column + a value column (Fund_Overview)
                    "wide_period"    : one column of line-item labels, and MANY
                                       period columns (months/quarters) each
                                       holding values (P&L, Balance Sheet, Cashflow
                                       with month columns). VERY COMMON in MIS.
                    "entity_pivoted" : columns are entity IDs, rows are attributes.
  unit            — the reporting SCALE from the banner text, independent of
                    currency (INR/USD/EUR/RM/etc.): one of
                    "raw", "thousands", "lakhs", "millions", "crores", "billions".
                    "raw" means no scaling word present (plain currency units).
                    Read whatever the sheet printed ("in '000", "₹ Lakhs",
                    "USD Mn", "RM'000", "figures in millions") and pick the
                    matching scale; do NOT assume a market.
  currency        — the currency code/symbol if the sheet states one
                    (INR, USD, EUR, RM, ...), else null.
  unit_confidence — "high" if a scale word is explicitly printed on the sheet,
                    else "low".
  entity          — the company/fund NAME this sheet reports on, copied verbatim
                    from entity_text_seen, else null.
  entity_confidence — "high" if a clear company name banner exists, else "low".
  header_row      — the 1-based row number that holds the column headers
                    (use detected_header_row unless the samples prove otherwise);
                    null if the sheet has no tabular header.
  label_column    — for key_value/wide_period: the raw header (or "col:<index>")
                    of the column holding the line-item labels. null otherwise.
  value_columns   — for wide_period/entity_pivoted: the list of raw period/entity
                    header texts whose columns carry values. [] otherwise.
  column_map      — {{ raw_header_text -> canonical_field_name }} using the
                    catalogue below. Map AGGRESSIVELY: every column with a
                    plausible canonical meaning, even if the client named it
                    differently (e.g. "Total CoGS" -> cogs, "Employee Cost" ->
                    employee_benefit_expense). Leave unmappable columns out.
  skip            — true if this sheet carries no ingestible data (cover/config/
                    narrative/duplicate-of-another). false otherwise.
  notes           — short free text: flag risks (ambiguous unit, looks like a
                    duplicate/version of another sheet, multiple stacked tables,
                    entity name spelled differently from the filename).

Return JSON only, no markdown:
{{
  "sheets": {{
    "<sheet_name>": {{
      "domain": <str|null>, "layout": <str>, "unit": <str>,
      "currency": <str|null>, "unit_confidence": <str>, "entity": <str|null>,
      "entity_confidence": <str>, "header_row": <int|null>,
      "label_column": <str|null>, "value_columns": [<str>...],
      "column_map": {{ "<raw>": "<canonical>" }},
      "skip": <bool>, "notes": <str>
    }}
  }}
}}

Canonical fields per domain:
{_domain_catalogue()}

SHEETS:
{blocks}
"""


def _map_batch(batch, file_label, timeout_ms):
    """Map one batch of fingerprints → {sheet_name: raw_map}. If the call fails
    for ANY reason (most importantly output truncation / MAX_TOKENS on a dense
    block of sheets), split the batch in half and recurse — down to a single
    sheet, whose output can't overflow. A single sheet that still fails is
    dropped (its caller marks it skip). Universal: no sheet count can stall."""
    prompt = build_map_prompt(batch, file_label)
    logger.info(f'[preingest.map] {file_label}: {len(batch)} sheets, '
                f'~{len(prompt) // 4:,} input tokens')
    try:
        resp = _call_with_retry(prompt, f'preingest.map:{file_label}', timeout_ms)
        return resp.get('sheets', {}) if isinstance(resp, dict) else {}
    except Exception as e:  # noqa: BLE001 — truncation/parse/transient exhausted
        if len(batch) > 1:
            mid = len(batch) // 2
            logger.warning(f'[preingest.map] {file_label} batch of {len(batch)} '
                           f'failed ({type(e).__name__}); splitting')
            out = {}
            out.update(_map_batch(batch[:mid], file_label, timeout_ms))
            out.update(_map_batch(batch[mid:], file_label, timeout_ms))
            return out
        logger.warning(f'[preingest.map] {file_label} sheet '
                       f'"{batch[0]["sheet"]}" unmappable: {e}')
        return {}


def map_file(fingerprints, file_label, timeout_ms=240_000):
    """Run the MAP stage for one file. Returns {sheet_name: map_dict}.

    Sheets are batched small; each batch auto-splits on truncation.
    """
    result = {}
    for i in range(0, len(fingerprints), SHEETS_PER_CALL):
        batch = fingerprints[i:i + SHEETS_PER_CALL]
        sheets = _map_batch(batch, file_label, timeout_ms)
        for fp in batch:
            sn = fp['sheet']
            m = sheets.get(sn)
            if not isinstance(m, dict):
                # Gemini returned nothing usable for this sheet — mark skip so
                # the mover never guesses. Surfaced in the review report.
                m = {'domain': None, 'skip': True,
                     'notes': 'no map returned by classifier'}
            m.setdefault('layout', 'tabular')
            m.setdefault('unit', 'raw')
            m.setdefault('currency', None)
            m.setdefault('unit_confidence', 'low')
            m.setdefault('entity', None)
            m.setdefault('entity_confidence', 'low')
            m.setdefault('header_row', fp['header_row'])
            m.setdefault('label_column', None)
            m.setdefault('value_columns', [])
            m.setdefault('column_map', {})
            m.setdefault('skip', m.get('domain') is None)
            m.setdefault('notes', '')
            result[sn] = m
    return result
