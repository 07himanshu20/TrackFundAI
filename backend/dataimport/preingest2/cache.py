"""
Golden-Record Cache (document Section 10) — the determinism + cost mechanism.

A unique file is read by the model exactly once, ever. Its validated extraction
record is frozen to an immutable store and re-served on every subsequent run.
Determinism comes from this freeze, not from the model.

  key   = SHA-256(file bytes) + '.' + pipeline_version
  hit   → return frozen record, NO model call
  miss  → caller extracts once, validates, then put() freezes it

Lightweight backing = one JSON file per key under a cache dir (stands in for the
Postgres immutable table D3). pipeline_version folds in schema + prompt + code
versions, so any of those changing invalidates old records deterministically.
"""
import hashlib
import json
import logging
import os

from . import PIPELINE_VERSION
from .schema import schema_signature

logger = logging.getLogger(__name__)

# Bump when the extraction prompt or extractor code semantics change.
PROMPT_CODE_VERSION = 'ext-1.0.0'

_CACHE_DIR = os.environ.get(
    'PREINGEST2_CACHE_DIR',
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), 'media', 'preingest2_cache'))


def pipeline_version() -> str:
    return f'{PIPELINE_VERSION}+{schema_signature()}+{PROMPT_CODE_VERSION}'


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def cache_key(path: str) -> str:
    return f'{file_hash(path)}.{pipeline_version()}'


def _key_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, key + '.json')


def get(path: str):
    """Return the frozen record for this file+version, or None on miss."""
    try:
        p = _key_path(cache_key(path))
    except OSError:
        return None
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            rec = json.load(f)
        logger.info(f'[preingest2.cache] HIT {os.path.basename(path)}')
        return rec
    except Exception:  # noqa: BLE001 — a corrupt cache file is a miss
        return None


def put(path: str, record: dict) -> None:
    """Freeze a validated extraction record immutably (write-once)."""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    key = cache_key(path)
    p = _key_path(key)
    if os.path.exists(p):
        return  # immutable — never overwrite a frozen record
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(record, f, ensure_ascii=False, default=str)
    os.replace(tmp, p)
    logger.info(f'[preingest2.cache] FROZE {os.path.basename(path)} -> {key[:16]}…')


def stats() -> dict:
    try:
        n = len([f for f in os.listdir(_CACHE_DIR) if f.endswith('.json')])
    except OSError:
        n = 0
    return {'cache_dir': _CACHE_DIR, 'frozen_records': n,
            'pipeline_version': pipeline_version()}
