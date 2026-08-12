"""
U3 — Three-level file identity.

Closes D3. A raw-byte hash answers only "is this the identical file?" — too
narrow, because opening a workbook in Excel and pressing save rewrites bytes
while changing nothing that matters, which under a byte cache re-invoked the
model and broke determinism. Three questions, three fingerprints (Section 07):

  L1 BYTE    — hash of the file as uploaded. Fast path only, never the sole key.
  L2 CONTENT — hash of sheet names + cell addresses + cell VALUES, canonically
               ordered. Survives re-saves / metadata edits. The golden-record
               key (delivers the determinism guarantee G1).
  L3 LAYOUT  — hash of structure with all VALUES stripped: sheet names, header
               text, label-column text. Identical for the same template in a
               different month, or a different company on the same template.
               The Layout Template Registry key (delivers the cost guarantee G4).

Canonical ordering: files are sorted by content fingerprint before processing —
never by upload order or filename — so two users submitting the same set in a
different sequence obtain the same workbook.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
from dataclasses import dataclass
from typing import List, Optional

import openpyxl

# The cache logic-version is a hash of the library's source, so ANY change to value logic
# auto-invalidates cached results (Inc-5). Preferred to a manually-bumped version string
# (which rests on remembering to bump it — forget once and a cache hit serves a pre-fix
# result, the same silent-staleness class as a content_fp-only key). It hashes the WHOLE
# package, NOT a hand-list of "value modules": tracing extract_company's real call graph
# proved a hand-list (extract/family/tiers/reconcile/periods/contract/units/quantity) SILENTLY
# MISSED 6 value-affecting modules — gate (the binding decision), lexicon (concept matching),
# profiler (grid/axis), ratecard (FX conversion code), cir, identity. Enumeration-from-memory
# is exactly the completeness hole this hash exists to close, so we don't enumerate: hashing
# every module means over-inclusion (a harmless false cache-miss on a non-value edit) but NEVER
# under-inclusion (a stale-logic hole). Also covers the use_model=True model path (locator/llm/
# namematch/templates) that a deterministic trace can't see but the cache serves under one key.
_LOGIC_VERSION: Optional[str] = None


@dataclass(frozen=True)
class FileIdentity:
    label: str
    path: str
    byte_fp: str
    content_fp: str
    layout_fp: str
    currencies: tuple = ()      # distinct currency hints seen (for Rate Card coverage)


def _sha(parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode('utf-8', 'replace'))
        h.update(b'\x1f')
    return h.hexdigest()


def _is_volatile_value(v) -> bool:
    """A VALUE that changes reporting-period to reporting-period — numbers AND
    dates/times. These are stripped from the LAYOUT fingerprint (else the same
    template next month, with new numbers and a new period-end date, would hash
    differently and be wrongly treated as a new layout — defeating U3/G4). They
    are kept in the CONTENT fingerprint, which is meant to differ per period."""
    if isinstance(v, bool):
        return False
    return isinstance(v, (int, float, _dt.datetime, _dt.date, _dt.time))


def compute_identity(label: str, path: str) -> FileIdentity:
    """Compute all three fingerprints in a single read-only pass (data_only so
    cached computed values are read, never formulas — build rule from the
    preingest layer: ALWAYS data_only=True, NEVER eval a formula)."""
    with open(path, 'rb') as fh:
        byte_fp = hashlib.sha256(fh.read()).hexdigest()

    content_parts: List[str] = []
    layout_parts: List[str] = []
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        # Non-workbook or unreadable → identity still defined by bytes; content
        # and layout fall back to the byte hash so it is treated as unique.
        return FileIdentity(label, path, byte_fp, byte_fp, byte_fp, ())

    for sn in wb.sheetnames:                       # sheetnames order is canonical
        ws = wb[sn]
        content_parts.append(f'#SHEET#{sn}')
        layout_parts.append(f'#SHEET#{sn}')
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if v is None or v == '':
                    continue
                coord = cell.coordinate
                content_parts.append(f'{coord}={v!r}')
                if not _is_volatile_value(v):       # text = structure; values stripped
                    layout_parts.append(f'{coord}={str(v).strip().lower()!r}')
    try:
        wb.close()
    except Exception:
        pass

    return FileIdentity(
        label=label, path=path, byte_fp=byte_fp,
        content_fp=_sha(content_parts) if content_parts else byte_fp,
        layout_fp=_sha(layout_parts) if layout_parts else byte_fp,
    )


def canonical_sort(identities: List[FileIdentity]) -> List[FileIdentity]:
    """Deterministic processing order — by content fingerprint, tie-broken by
    label. Upload order and filenames never influence the output."""
    return sorted(identities, key=lambda i: (i.content_fp, i.label))


def _hash_py_dir(dirpath: str) -> str:
    """Hash every *.py directly in `dirpath` (sorted, top-level only — no recursion into a
    tests/ subdir or __pycache__). Pure + deterministic so it can be negative-controlled:
    editing OR adding a module changes the digest. Under-coverage is impossible by
    construction — it hashes the whole directory, never a hand-list."""
    h = hashlib.sha256()
    try:
        names = sorted(f for f in os.listdir(dirpath) if f.endswith('.py'))
    except OSError:
        names = []
    for name in names:
        h.update(name.encode())                    # include the name → adding a module invalidates
        try:
            with open(os.path.join(dirpath, name), 'rb') as fh:
                h.update(fh.read())
        except OSError:
            h.update(b'?missing?')
        h.update(b'\x1f')
    return 'lv_' + h.hexdigest()[:16]


def net_logic_version() -> str:
    """Structural code-version for the extraction cache key: a hash of EVERY library module's
    source (see _hash_py_dir). ANY change to library logic yields a new version → cached results
    computed under the old code are missed and recomputed, so a shipped fix can never be masked
    by a stale cache hit. Whole-package by design: a hand-picked 'value modules' list silently
    missed 6 modules when traced (see module note) — over-inclusion is a safe false-miss,
    under-inclusion is a stale-logic hole. Computed once per process (source is immutable)."""
    global _LOGIC_VERSION
    if _LOGIC_VERSION is None:
        _LOGIC_VERSION = _hash_py_dir(os.path.dirname(os.path.abspath(__file__)))
    return _LOGIC_VERSION


def extraction_cache_key(content_fp: str, *, as_of, rate_card_id, anchor_cr,
                         domicile, use_model, logic_version: str = None) -> str:
    """The PROVABLY-COMPLETE key for the per-file extraction/reuse cache: content_fp PLUS
    every run input that can change the extracted CIR. content_fp ALONE is a config-staleness
    bug — a file's INR-config record served under a new rate card / as_of / anchor / domicile
    (goes live at the U6 multi-currency boundary). `entity` is EXCLUDED: empirically proven
    pure attribution (0 value-diffs when it alone changes), so keying on it would only cause
    false cache misses. `as_of` is kept explicit even though today it reaches extraction only
    via rate_card_id — future-proofing as_of-dependent period selection. logic_version defaults
    to the structural net-module hash; a caller may inject one (tests / a pinned prod version)."""
    lv = logic_version if logic_version is not None else net_logic_version()
    return _sha([content_fp, str(as_of), str(rate_card_id),
                 str(anchor_cr), str(domicile), '1' if use_model else '0', lv])
