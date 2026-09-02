"""
Phase 2.4 — the durable GOLDEN-RECORD store (the VALUE/answer cache made persistent).

Replaces the in-memory `reuse` dict with a disk-backed store so an identical file+config+logic
re-run reuses its finished extraction across PROCESSES — fixing both the dev-suite re-run cost and
the repeat-client cost — WITHOUT ever risking a stale number, by construction:

  • Keyed by extraction_cache_key = content_fp (a hash of EVERY value cell) + as_of + rate_card +
    anchor + domicile + use_model + net_logic_version + contract_sig. So ONE changed number →
    different content_fp → different key → guaranteed MISS → recompute (never a stale answer). A
    logic change → different net_logic_version → different key → the whole store is transparently
    rebuilt (self-erasing; it never freezes yesterday's understanding).
  • It is a pure SPEED layer over the verified-or-held spine: a hit returns the SAME Record the
    pipeline computed under identical inputs+logic, so it can only ever serve today's answer faster —
    it can never turn a HELD into an emit or produce a different/worse number.
  • One file per key (<key>.rec, an atomically-written pickle). A partial/corrupt entry can never
    poison the store: an unreadable file is a SILENT MISS (recompute), never an exception.

The store never keys a number by filename or layout — only by the full content fingerprint. That is
the exact bug the design forbids (safety rule #1).
"""
from __future__ import annotations

import hashlib
import os
import pickle
from typing import Dict, Optional

from .cir import Record

_EXT = '.rec'


def _path(store_dir: str, key: str) -> str:
    return os.path.join(store_dir, key + _EXT)


def org_dir(store_dir: Optional[str], org: str) -> Optional[str]:
    """Per-TENANT namespace under `store_dir` — the load-bearing isolation for a multi-tenant value cache.
    The entry key (identity.extraction_cache_key = content_fp + config + logic) is org-BLIND by design, so
    two tenants uploading a byte-identical file produce the SAME key; without this namespace the second
    tenant would be served the first's finished Record — a cross-fund WRONG NUMBER and a data leak. The
    org is HASHED (never sanitised, never interpolated raw), so no two distinct orgs can collide onto one
    directory and no org value can traverse outside the base dir. Returns None when no base store_dir is
    configured (store inactive → behaviour unchanged), so a caller can guard on it exactly like store_dir."""
    if not store_dir:
        return None
    tag = hashlib.sha256((org or '').encode('utf-8')).hexdigest()[:24]
    return os.path.join(store_dir, f'org-{tag}')


def get(store_dir: Optional[str], key: str) -> Optional[Record]:
    """The stored Record for `key`, or None on absence / unreadable entry (silent miss → recompute)."""
    if not store_dir:
        return None
    p = _path(store_dir, key)
    if not os.path.exists(p):
        return None
    try:
        with open(p, 'rb') as fh:
            rec = pickle.load(fh)
        return rec if isinstance(rec, Record) else None
    except Exception:
        return None                              # corrupt / partial / version-skew → miss, never raise


def put(store_dir: Optional[str], key: str, record: Record) -> None:
    """Persist `record` under `key`. Atomic (write-tmp-then-rename) so no partial file is ever visible
    under the real name. A write failure is swallowed — the store is a cache, never a source of truth."""
    if not store_dir or not isinstance(record, Record):
        return
    try:
        os.makedirs(store_dir, exist_ok=True)
        p = _path(store_dir, key)
        tmp = f'{p}.{os.getpid()}.tmp'
        with open(tmp, 'wb') as fh:
            pickle.dump(record, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)                        # atomic on POSIX
    except Exception:
        try:
            os.remove(tmp)
        except (OSError, NameError, UnboundLocalError):
            pass


def load(store_dir: Optional[str]) -> Dict[str, Record]:
    """Every entry as a {key: Record} dict — the same shape the pipeline's `reuse` param consumes.
    (The pipeline itself uses lazy get()/put() so it never has to load the whole store; load() is for
    callers/tests that want the dict form.)"""
    out: Dict[str, Record] = {}
    if not store_dir or not os.path.isdir(store_dir):
        return out
    for name in sorted(os.listdir(store_dir)):
        if name.endswith(_EXT):
            rec = get(store_dir, name[:-len(_EXT)])
            if rec is not None:
                out[name[:-len(_EXT)]] = rec
    return out


def save(store_dir: Optional[str], extraction: Dict[str, Record]) -> int:
    """Persist every (key, Record) from a run's extraction dict. Idempotent (identical key → identical
    content+logic → same bytes). Returns the count written."""
    if not store_dir:
        return 0
    n = 0
    for key, rec in (extraction or {}).items():
        if isinstance(rec, Record):
            put(store_dir, key, rec)
            n += 1
    return n
