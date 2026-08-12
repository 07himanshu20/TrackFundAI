"""
Pure entity-name similarity — no I/O, no model, no Django. Extracted from
alias_ledger so BOTH U4 resolution and the offline fund anchor can share ONE
implementation without dragging in the llm/api import chain.

Robust to corporate suffixes, abbreviation-vs-legal-name, and a distinctive name
buried in a noisy identifier ('Hubler' inside 'AVF_..Hubler_MIS').
"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import List

_SUFFIXES = {'pvt', 'private', 'ltd', 'limited', 'inc', 'llp', 'llc', 'co', 'corp',
             'corporation', 'technologies', 'technology', 'tech', 'labs', 'lab',
             'solutions', 'systems', 'india', 'group', 'fund', 'ventures', 'holdings',
             'the', 'and'}


def tokens(s: str) -> List[str]:
    """Content tokens: lower-cased words, minus corporate suffixes, pure numbers
    (dates/ids in filenames) and single characters (initials). What's left is the
    distinctive part of a name, so a name buried in a noisy filename still scores."""
    if not s:
        return []
    out = []
    for w in ''.join(c.lower() if (c.isalnum() or c.isspace()) else ' ' for c in str(s)).split():
        if len(w) <= 1 or w.isdigit() or w in _SUFFIXES:
            continue
        out.append(w)
    return out


def acronym(tokens_: List[str]) -> str:
    return ''.join(t[0] for t in tokens_ if t)


def similarity(a: str, b: str) -> float:
    """0..1 similarity robust to suffixes, to abbreviation-vs-legal-name, and to a
    distinctive name buried in a noisy identifier. Takes the STRONGEST of:
      • whole-set Jaccard and whole-string ratio (clean, similar names)
      • acronym expansion (initials vs expansion)
      • shared-distinctive-token coverage relative to the SHORTER name — so
        'Hubler' inside 'AVF_..Hubler_MIS' scores high, while 'Hubli' (no shared
        exact token) does not.
    """
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    jaccard = len(sa & sb) / len(sa | sb)
    ratio = SequenceMatcher(None, ' '.join(ta), ' '.join(tb)).ratio()
    acr = 0.9 if (acronym(ta) == ''.join(tb) or acronym(tb) == ''.join(ta)) else 0.0
    shared = sa & sb
    coverage = (len(shared) / min(len(sa), len(sb))) if shared else 0.0
    return max(jaccard, ratio, acr, coverage)
