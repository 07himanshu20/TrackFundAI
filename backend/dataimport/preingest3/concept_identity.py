"""Concept-Identity resolution mechanism — Phase 1.5, model-OFF (concept_identity_build_spec v1.1).

LAYER A (this module): fully general and DIMENSION-AGNOSTIC. It reads the versioned taxonomy DATA in
contract.py (dimensions, values, conservation laws, resolution policies, conventions); a new dimension /
value / law / convention is a data edit THERE, never a code change HERE — proved by control CI-12.

Governing principle (spec §0): a name is a hypothesis about identity; the DATA is the judge. This module
builds the pieces that live above extraction:
  • decompose(name)  — turn a raw source name into a structured ConceptKey {base, qualifiers}
  • same_concept()   — the three-state match rule (same / different / unresolved), §1.2
  • identity_class / resolution_policy / capital-ladder lookups — read from taxonomy data
  • the asserted→confirmed→contradicted state + asserter enums (§2)

The three nets (§3), subsumption of the hand-built proto-nets (§0.1), and the audit replay (§5) build on
THIS in later increments. Names alone only ever reach `asserted`; only data promotes to `confirmed`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from . import lexicon
from .contract import (CONCEPT_LEXICON, CI_QUALIFIER_DIMENSIONS, CI_CAPITAL_LADDER_ORDER,
                       CI_CAPITAL_FAMILY, CI_BASE_SYNONYMS, CI_BASE_IDENTITY_CLASS,
                       CI_RESOLUTION_POLICY, CI_CONVENTION_TABLE)

# ── three-state silence (§1.2): a dimension is SPECIFIED, filled by CONVENTION, or UNRESOLVED ──
SPECIFIED = 'specified'
CONVENTION_DEFAULTED = 'convention_defaulted'
UNRESOLVED = 'unresolved'

# match-rule verdicts
SAME = 'same'
DIFFERENT = 'different'
DOUBT = 'unresolved'


class State(str, Enum):
    """Confidence in an identity (§2). Names reach ASSERTED; only the data-nets promote to CONFIRMED."""
    ASSERTED = 'asserted'
    CONFIRMED = 'confirmed'
    CONTRADICTED = 'contradicted'


class Asserter(str, Enum):
    """Who filled the ConceptKey (§2)."""
    DETERMINISTIC = 'deterministic'
    CONVENTION = 'convention'
    MODEL = 'model'
    LEGACY = 'legacy'


@dataclass(frozen=True)
class ConceptKey:
    """WHAT a source name means, structured. base = the quantity; qualifiers = the finite, universal
    cuts (a sorted tuple of (dimension, value) so the key is hashable + canonical). base None = the
    parser could not resolve it (→ model's job when the model is on; held model-off)."""
    base: Optional[str]
    qualifiers: Tuple[Tuple[str, str], ...] = ()

    def q(self) -> dict:
        return dict(self.qualifiers)

    def canonical(self) -> str:
        return f"{self.base}|" + ",".join(f"{d}={v}" for d, v in self.qualifiers)


def _tokens(norm: str) -> List[str]:
    return [t for t in norm.split() if t]


def _phrase_present(tokens: List[str], phrase: str) -> bool:
    """WORD-BOUNDARY match: single-token phrase → token membership; multi-word phrase → a consecutive
    token subsequence. This is what the census's substring scan lacked (so 'net' never matches inside
    'cabinet', and 'net of carry' matches only as three consecutive tokens)."""
    p = phrase.split()
    if not p:
        return False
    if len(p) == 1:
        return p[0] in tokens
    n = len(p)
    return any(tokens[i:i + n] == p for i in range(len(tokens) - n + 1))


def _longest_phrase_hit(tokens: List[str], phrase_map: dict):
    """Over a {phrase: value} map, the value of the LONGEST phrase that is word-boundary-present,
    plus that phrase's tokens (for consume). (None, None, ()) if nothing hits. Most-specific wins."""
    best_val, best_ph_toks, best_len = None, (), 0
    for phrase, value in phrase_map.items():
        n = len(phrase.split())
        if n > best_len and _phrase_present(tokens, phrase):
            best_val, best_ph_toks, best_len = value, tuple(phrase.split()), n
    return best_val, best_ph_toks


def _resolve_capital_rung(tokens: List[str]) -> Tuple[Optional[str], Tuple[str, ...]]:
    """Resolve the capital ladder rung when one or MORE capital-family phrases are present, consuming
    ALL of their tokens. A specific STATUS modifier ('undrawn', 'called', 'distributed', …) refines the
    generic committed base, so it WINS over a co-present 'commitment'/'committed' — 'Undrawn Commitment'
    is undrawn capital, not committed. Among equally-specific hits the longest phrase wins (stable).
    General rule, not a per-file guard: 'committed' is the base rung any explicit status overrides."""
    hits = []  # (rung, phrase_tokens, nwords)
    for phrase, rung in CI_CAPITAL_FAMILY.items():
        if _phrase_present(tokens, phrase):
            pt = tuple(phrase.split())
            hits.append((rung, pt, len(pt)))
    if not hits:
        return None, ()
    specific = [h for h in hits if h[0] != 'committed']
    pool = sorted(specific or hits, key=lambda h: -h[2])
    rung = pool[0][0]
    consumed = set()
    for _r, pt, _n in hits:
        consumed |= set(pt)
    return rung, tuple(consumed)


def _match_ci_base(tokens: List[str]) -> Tuple[Optional[str], Tuple[str, ...]]:
    """Established-danger fund bases (nav/irr/moic/…) from CI_BASE_SYNONYMS — not in the source lexicon."""
    best_base, best_syn, best_len = None, (), 0
    for base, syns in CI_BASE_SYNONYMS.items():
        for syn in syns:
            n = len(syn.split())
            if n > best_len and _phrase_present(tokens, syn):
                best_base, best_syn, best_len = base, tuple(syn.split()), n
    return best_base, best_syn


def _match_lexicon_base(norm: str) -> Tuple[Optional[str], Tuple[str, ...]]:
    """Source-concept base via the existing negator-guarded matcher (lexicon.label_matches_any), so
    base detection stays behaviour-consistent; returns the matched synonym's tokens so they are removed
    before the qualifier scan (a base's OWN words, e.g. the 'net' in 'net income', are never re-read as
    a qualifier). Longest matching synonym wins."""
    best_concept: Optional[str] = None
    best_syn: Tuple[str, ...] = ()
    for concept in CONCEPT_LEXICON:
        for syn in lexicon.synonyms(concept):
            st = tuple(_tokens(syn))
            if not st or len(st) <= len(best_syn):
                continue
            if lexicon.label_matches_any(norm, [syn]):
                best_concept, best_syn = concept, st
    return best_concept, best_syn


def _detect_qualifiers(tokens: List[str]) -> dict:
    """Scan the (base-token-stripped) label for qualifier values, most-specific (longest phrase) wins
    per dimension. Pure over the taxonomy DATA — adding a dimension/value needs no edit here."""
    quals: dict = {}
    for dim, values in CI_QUALIFIER_DIMENSIONS.items():
        best_val, best_len = None, 0
        for val, phrases in values.items():
            for ph in phrases:
                if _phrase_present(tokens, ph) and len(ph.split()) > best_len:
                    best_val, best_len = val, len(ph.split())
        if best_val is not None:
            quals[dim] = best_val
    return quals


def decompose(name: str) -> ConceptKey:
    """Raw source name → ConceptKey (base + SPECIFIED qualifiers only). Base precedence: (1) capital
    ladder ('Commitment' is capital[committed], not a bare concept); (2) established-danger fund bases
    (nav/irr/…); (3) source-lexicon concepts. A base's matched tokens are consumed before the qualifier
    scan. Convention defaults are applied later, in the match rule where the context is known — so a
    default is an assertion the nets can still contradict, never a silent baked-in qualifier."""
    norm = lexicon.normalise_label(name)
    tokens = _tokens(norm)

    rung, cap_toks = _resolve_capital_rung(tokens)                    # (1) capital ladder
    if rung is not None:
        remaining = [t for t in tokens if t not in set(cap_toks)]
        quals = _detect_qualifiers(remaining)
        quals['capital_status'] = rung
        return ConceptKey('capital', tuple(sorted(quals.items())))

    ci_base, ci_toks = _match_ci_base(tokens)                         # (2) fund-level danger bases
    if ci_base is not None:
        remaining = [t for t in tokens if t not in set(ci_toks)]
        return ConceptKey(ci_base, tuple(sorted(_detect_qualifiers(remaining).items())))

    base, consumed = _match_lexicon_base(norm)                        # (3) source-concept bases
    remaining = [t for t in tokens if t not in set(consumed)]
    return ConceptKey(base, tuple(sorted(_detect_qualifiers(remaining).items())))


def convention_default(base: Optional[str], context: Optional[str], dimension: str) -> Optional[str]:
    """The three-state 'convention_defaulted' value for a silent dimension, or None (→ unresolved).
    Small + high-confidence table only; weak/ambiguous context returns None (doubt), never a guess."""
    for row in CI_CONVENTION_TABLE:
        if row['dimension'] == dimension and row['base'] == base and \
                (row['context'] is None or row['context'] == context):
            return row['default']
    return None


def same_concept(a: ConceptKey, b: ConceptKey, *, ctx_a: Optional[str] = None,
                 ctx_b: Optional[str] = None) -> str:
    """The three-state match rule (§1.2). SAME iff base equal AND for every dimension either side
    specifies, the values agree (convention fills a silent side only when a strong row applies).
    An explicit conflict on any dimension → DIFFERENT (beats unresolved). A silent side with no
    convention → UNRESOLVED (doubt path), never a silent merge."""
    if a.base != b.base:
        return DIFFERENT
    qa, qb = a.q(), b.q()
    unresolved = False
    for d in set(qa) | set(qb):
        va, vb = qa.get(d), qb.get(d)
        if va is not None and vb is not None:
            if va != vb:
                return DIFFERENT
        else:
            spec_val = va if va is not None else vb
            silent_base = b.base if va is not None else a.base
            silent_ctx = ctx_b if va is not None else ctx_a
            conv = convention_default(silent_base, silent_ctx, d)
            if conv is None:
                unresolved = True
            elif conv != spec_val:
                return DIFFERENT
            # conv == spec_val → convention-defaulted agreement, ok
    return DOUBT if unresolved else SAME


# ── per-base conservation law + resolution policy + capital ladder (all read from taxonomy data) ──
def identity_class_of(base: Optional[str]) -> str:
    """The conservation law Net 2 applies to this base (roll_forward / monotone_ladder / flow_balance),
    or 'none' (free flow → Net 2 declared-inactive). Data-driven; a new law is a taxonomy edit."""
    return CI_BASE_IDENTITY_CLASS.get(base or '', 'none')


def resolution_policy_of(base: Optional[str], event: str) -> Optional[str]:
    """The declared resolution for a fired net on this (base, event) — the proto-net's current behaviour
    as data (hold_both / corroborate / max / …). Per-base overrides the '*' default."""
    return CI_RESOLUTION_POLICY.get((base, event)) or CI_RESOLUTION_POLICY.get(('*', event))


def ladder_rank(status: str) -> Optional[int]:
    """Position of a capital_status rung in the monotone ladder (committed ≥ called ≥ … ≥ distributed),
    or None if not a ladder rung. Used by Net 2's monotone_ladder law."""
    return CI_CAPITAL_LADDER_ORDER.index(status) if status in CI_CAPITAL_LADDER_ORDER else None
