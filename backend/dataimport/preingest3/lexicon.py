"""
The concept lexicon — the universal part of Signal 2 (label triangulation).

It begins with the seeded synonyms per concept (contract.CONCEPT_LEXICON) and
GROWS automatically: every time a reviewer resolves an ambiguity at the gate,
the label they approved is written here for that concept (doc §06). The system
is measurably better at the 1000th client than the 1st, and no engineer edited
code to make that happen.

Matching is normalisation-based and universal — never a hardcoded header
spelling. A label matches a concept if, after stripping punctuation/notes, it
equals or contains a known synonym (or vice-versa for short synonyms).
"""
from __future__ import annotations

import json
import os
import re
import threading

from .contract import CONCEPT_LEXICON

_LEARNED_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'media', 'preingest3_lexicon.json',
)
_lock = threading.Lock()
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def normalise_label(text) -> str:
    """Lower-case, drop bracketed notes like '(loss)'/'(₹cr)', strip punctuation
    and collapse whitespace — so 'Total Revenue (₹ Cr)' and 'total revenue' meet."""
    if text is None:
        return ''
    s = str(text).lower()
    s = re.sub(r'\([^)]*\)', ' ', s)      # remove parenthetical notes
    s = _PUNCT.sub(' ', s)
    return _WS.sub(' ', s).strip()


def _load_learned() -> dict:
    if os.path.exists(_LEARNED_PATH):
        try:
            with open(_LEARNED_PATH) as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}


def synonyms(concept: str) -> list:
    """Seed synonyms ∪ learned synonyms for a concept (normalised)."""
    concept = (concept or '').strip().lower()
    base = [normalise_label(s) for s in CONCEPT_LEXICON.get(concept, [])]
    learned = [normalise_label(s) for s in _load_learned().get(concept, [])]
    # concept key itself is a valid synonym (e.g. 'revenue')
    return sorted(set(base + learned + [normalise_label(concept.replace('_', ' '))]))


# a concept token immediately negated by one of these is NOT that concept —
# 'Non-operating income' is not operating income, 'excluding tax' is not tax.
# Linguistic structure (universal), NOT a per-file blocklist of business terms.
_NEGATORS = {'non', 'excl', 'excluding', 'ex', 'less', 'before'}
# ...but these concept synonyms legitimately CONTAIN a negator as their own first
# word (EBITDA = earnings BEFORE interest…), so negation is not applied when the
# matched synonym itself begins with the negator.
def _contains_subseq(hay: list, needle: list) -> bool:
    """True if `needle` appears as a contiguous run of WHOLE words in `hay`, in at
    least one occurrence NOT negated by a preceding qualifier ('non', 'excl', …).
    So 'operating income' matches 'operating income' but not 'non operating
    income'; 'profit before tax' still matches (the negator is inside the synonym,
    not before it)."""
    if not needle or len(needle) > len(hay):
        return False
    starts_with_negator = needle[0] in _NEGATORS
    for i in range(len(hay) - len(needle) + 1):
        if hay[i:i + len(needle)] == needle:
            if not starts_with_negator and i > 0 and hay[i - 1] in _NEGATORS:
                continue                         # this occurrence is negated — keep looking
            return True
    return False


def label_matches_any(label, raw_synonyms) -> bool:
    """The ONE label matcher, against an AD-HOC synonym list (not the concept store).

    This is the universal primitive every module must use instead of hand-rolling its
    own — a hand-rolled matcher is how the raw-vs-normalised and substring bug classes
    creep back in. Two invariants hold BY CONSTRUCTION:
      • BOTH sides pass through normalise_label — so a synonym may be authored in ANY
        natural form ('Fund P&L', 'AMC fee (p.a.)', 'Carry @ 20%') and still match; a
        synonym is NEVER silently dead because its punctuation differs from the cell's.
      • Matching is WHOLE-WORD (via _contains_subseq, negator-guarded) — so a short
        synonym never substring-matches a larger unrelated token ('p l' does not match
        '…itd p l below' unless the whole 'fund p l' run is present)."""
    nl = normalise_label(label)
    if not nl:
        return False
    tokens = nl.split()
    for raw in raw_synonyms:
        syn = normalise_label(raw)          # normalise the SYNONYM through the same function
        if not syn:
            continue
        if nl == syn or _contains_subseq(tokens, syn.split()):
            return True
    return False


def label_coverage(label, raw_synonyms) -> float:
    """Best whole-word match COVERAGE — len(normalised synonym) / len(normalised label) —
    over the synonyms, or 0.0 if none matches. For TIGHTEST-match ranking: a row that IS
    the label ('Carry %' → coverage ~1.0) out-anchors a heading that merely mentions it
    ('Management & performance fee working' → low coverage). Same normalise-both-sides and
    whole-word guarantees as label_matches_any — the ranking layer over the same matcher."""
    nl = normalise_label(label)
    if not nl:
        return 0.0
    tokens = nl.split()
    best = 0.0
    for raw in raw_synonyms:
        syn = normalise_label(raw)
        if syn and (nl == syn or _contains_subseq(tokens, syn.split())):
            best = max(best, len(syn) / len(nl))
    return best


def matches(label, concept: str) -> bool:
    """Does a sheet label denote this concept? Delegates to label_matches_any over the
    concept's (already-normalised) synonyms — so concept matching and ad-hoc synonym
    matching are the SAME code path, and cannot drift."""
    return label_matches_any(label, synonyms(concept))


def match_strength(label, concept: str) -> str:
    """'exact' (label normalises to a synonym), 'contains' (a synonym appears as
    whole words inside a longer label), or 'none'. Lets triangulation grade an
    exact label as strong evidence and a containment as soft."""
    nl = normalise_label(label)
    if not nl:
        return 'none'
    tokens = nl.split()
    contains = False
    for syn in synonyms(concept):
        if not syn:
            continue
        if nl == syn:
            return 'exact'
        if not contains and _contains_subseq(tokens, syn.split()):
            contains = True
    return 'contains' if contains else 'none'


def best_concept(label, candidates=None):
    """Return the concept a label best denotes, or None. Prefers an exact
    normalised match over a containment match."""
    nl = normalise_label(label)
    if not nl:
        return None
    concepts = candidates or list(CONCEPT_LEXICON.keys())
    tokens = nl.split()
    exact, contained = None, None
    for c in concepts:
        for syn in synonyms(c):
            if nl == syn:
                exact = c
                break
            if contained is None and _contains_subseq(tokens, syn.split()):
                contained = c
        if exact:
            return exact
    return contained


def match_detail(label, concept: str):
    """Richer than match_strength: returns (strength, coverage) where coverage is
    the fraction of the LABEL's whole-word tokens covered by the best-matching
    synonym. A single short synonym word inside a long unrelated label ('cash' in
    'cash flow from operating activities before income tax') scores tiny coverage,
    so the caller can down-rank a loose cross-concept hit versus a real line
    ('closing cash balance'). strength ∈ {'exact','contains','none'}."""
    nl = normalise_label(label)
    if not nl:
        return 'none', 0.0
    tokens = nl.split()
    n = len(tokens) or 1
    strength, best_cov = 'none', 0.0
    for syn in synonyms(concept):
        if not syn:
            continue
        syn_tok = syn.split()
        if nl == syn:
            return 'exact', 1.0
        if _contains_subseq(tokens, syn_tok):
            cov = len(syn_tok) / n
            if cov > best_cov:
                strength, best_cov = 'contains', cov
    return strength, best_cov


def learn(concept: str, label) -> None:
    """Persist a reviewer-approved label for a concept (grows the lexicon). Idempotent."""
    concept = (concept or '').strip().lower()
    norm = normalise_label(label)
    if not concept or not norm:
        return
    with _lock:
        data = _load_learned()
        vals = data.setdefault(concept, [])
        if norm not in [normalise_label(v) for v in vals]:
            vals.append(str(label).strip())
            os.makedirs(os.path.dirname(_LEARNED_PATH), exist_ok=True)
            tmp = _LEARNED_PATH + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, _LEARNED_PATH)
