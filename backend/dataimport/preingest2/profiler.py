"""
Stage 1 — Profiling (CODE, zero tokens).

Open each workbook once with the shared workbook cache (openpyxl read_only,
data_only → cached computed VALUES, never formulas) and emit a compact
fingerprint per sheet: name, dimensions, detected header row, unit/entity hints,
and a few sample rows. Giant sheets are summarized, never fully serialized.

The fingerprint (a few KB) is the ONLY thing the classifier ever sees — the raw
workbook is never sent to the model. This removes the single largest cause of
hallucination before any token is spent.

Reuses the proven preingest.fingerprint detectors (header row, unit+currency
hints, entity hints, stacked-table detection) and adds a serialized-token
estimate per sheet used by the segmenter's token-budget rule.
"""
import json

from ..phase3_layers.workbook_cache import load_workbook
from ..preingest.fingerprint import fingerprint_workbook


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def profile_file(path: str) -> dict:
    """Return {'sheets': [fingerprint + token estimate], 'raw': workbook_data}."""
    wb = load_workbook(path)              # cached values only
    fps = fingerprint_workbook(wb)
    for fp in fps:
        # cheap serialized-size proxy: header + samples + dims
        blob = json.dumps({'h': fp.get('header'), 's': fp.get('sample_rows'),
                           'n': fp.get('n_rows'), 'c': fp.get('n_cols')})
        fp['fingerprint_tokens'] = estimate_tokens(blob)
    return {'sheets': fps, 'raw': wb}
