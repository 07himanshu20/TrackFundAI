"""Concept-Identity audit replay + identity ledger — Phase 1.5 §5/§6, model-OFF.

§5 AUDIT REPLAY. Push every existing belief (legacy synonym equivalences, convention defaults) through
the §3 nets against a real extraction. A belief the data CONTRADICTS (collision / unreconciled movement)
is quarantined → HELD (fail-closed; no model needed, so the correctness win lands model-off). The report
distinguishes THREE states and never collapses them:
  • CORROBORATED — the data confirmed the merge (nets promoted it).
  • UNTESTED     — the corpus gives no second source / no overlap to test the belief. Held on its own
                   provenance (§2). This is NOT proof the belief is correct — reporting it as
                   "confidence" would be an over-claim. It is the honest "we could not check it here".
  • CONTRADICTED — a net fired → quarantined (the wrong-belief catch).
On a corpus sparse in the danger cases, most beliefs land UNTESTED — expected, and stated plainly.

§6 LEDGER. The identity row schema + the load-bearing invariant: a cached CONFIRMED identity short-circuits
only the MODEL, NEVER the nets ("known buys cheapness, never trust"). The caching KEY (content_fp +
net_logic_version + contract_sig) already lives in golden_store — this module does NOT duplicate it; it
defines the identity row, the never-serve-a-contradicted-row rule, and the re-verification primitive
(verify_merge). Production ledger caching rides in Phase 2 (spec §9); what is load-bearing NOW is the
invariant, proven to redden by control #9.

All deterministic — no model, no I/O. The mechanism is proven by the synthetic controls (#7/#8/#9); the
real-corpus finding is produced by classify_run() over the production pipeline's own output (no
reconstruction — feedback: audits use the production path)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import concept_nets
from .concept_identity import Asserter, ConceptKey, State, identity_class_of

# audit verdicts (three-state — the honest split)
CORROBORATED = 'corroborated'
UNTESTED = 'untested'
CONTRADICTED = 'contradicted'


@dataclass(frozen=True)
class Belief:
    """A claimed identity to audit. kind='synonym' → a set of source-names asserted to be ONE concept
    (a lexicon equivalence); kind='convention' → a convention-defaulted qualifier. Frozen (hashable) so
    it keys the observation map and the dual-run diff."""
    kind: str
    concept: str
    detail: str = ''
    rule_id: str = ''


@dataclass(frozen=True)
class AuditFinding:
    belief: Belief
    verdict: str                         # corroborated | untested | contradicted
    net: Optional[str] = None            # the net that fired (contradicted only)
    resolution: Optional[str] = None     # declared disposition on a fired net
    evidence: Tuple = ()
    detail: str = ''


@dataclass
class AuditReport:
    findings: List[AuditFinding] = field(default_factory=list)

    def by_verdict(self, v: str) -> List[AuditFinding]:
        return [f for f in self.findings if f.verdict == v]

    def quarantined(self) -> List[AuditFinding]:
        """Contradicted beliefs → held, fail-closed (§5)."""
        return self.by_verdict(CONTRADICTED)

    def summary(self) -> Dict[str, int]:
        return {CORROBORATED: len(self.by_verdict(CORROBORATED)),
                UNTESTED: len(self.by_verdict(UNTESTED)),
                CONTRADICTED: len(self.by_verdict(CONTRADICTED))}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# §5 — the audit replay: run the three nets over the observations a belief would merge, classify.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def audit_belief(belief: Belief, observations, *, tol_abs=None, tol_frac=None) -> AuditFinding:
    kw = {}
    if tol_abs is not None:
        kw['tol_abs'] = tol_abs
    if tol_frac is not None:
        kw['tol_frac'] = tol_frac
    n1, n2, n3, promoted = concept_nets.run_nets(observations, **kw)
    for v in (n1, n2, n3):
        if v.fired():
            return AuditFinding(belief, CONTRADICTED, net=v.net, resolution=v.resolution,
                                evidence=v.evidence, detail=v.detail)
    if promoted:
        return AuditFinding(belief, CORROBORATED, detail='data corroborates the merge')
    return AuditFinding(belief, UNTESTED, detail='no second source / no overlap — belief not testable here')


def replay(beliefs, observations_by_belief: Dict[Belief, list], *,
           tol_for: Optional[Callable[[Belief], Tuple]] = None) -> AuditReport:
    """Audit each belief against the observations it would merge. tol_for(belief) → (tol_abs, tol_frac)
    lets a belief carry its own tolerance (e.g. a rate belief → _RATE_TOL), so the replay reproduces the
    same boundary the production path uses (never a mechanism default that happens to differ)."""
    findings = []
    for b in beliefs:
        obs = observations_by_belief.get(b, [])
        ta, tf = (tol_for(b) if tol_for else (None, None))
        findings.append(audit_belief(b, obs, tol_abs=ta, tol_frac=tf))
    return AuditReport(findings)


def diff_reports(before: AuditReport, after: AuditReport) -> List[dict]:
    """§8 dual-run diff: every belief whose verdict FLIPPED between two taxonomy/contract versions,
    surfaced so it can be gated before promotion. A flip is exactly what a contract bump must not slip
    through silently."""
    b = {f.belief: f.verdict for f in before.findings}
    a = {f.belief: f.verdict for f in after.findings}
    flips = []
    for belief in set(b) | set(a):
        vb, va = b.get(belief), a.get(belief)
        if vb != va:
            flips.append({'belief': belief, 'from': vb, 'to': va})
    return flips


# ── belief enumerators (the "existing beliefs" the replay audits) ──
def synonym_beliefs() -> List[Belief]:
    """One belief per lexicon concept carrying ≥2 synonyms — the assertion that those names are ONE
    concept. These are the legacy synonym-dict equivalences the spec (§5) names explicitly."""
    from .contract import CONCEPT_LEXICON
    out = []
    for concept, syns in CONCEPT_LEXICON.items():
        if syns and len(syns) >= 2:
            out.append(Belief('synonym', concept, detail=f"{concept} ≡ {sorted(syns)}"))
    return out


def convention_beliefs() -> List[Belief]:
    """One belief per convention-default row — an assertion (asserter=convention) the data can contradict."""
    from .contract import CI_CONVENTION_TABLE
    return [Belief('convention', r['base'],
                   detail=f"{r['base']} {r['dimension']}={r['default']} @ {r['context']}", rule_id=r['rule_id'])
            for r in CI_CONVENTION_TABLE]


# ══════════════════════════════════════════════════════════════════════════════════════════════
# §6 — identity ledger + the cache-still-verifies invariant.
# ══════════════════════════════════════════════════════════════════════════════════════════════
@dataclass
class IdentityLedgerRow:
    source_name: str
    concept_key: ConceptKey
    identity_class: str
    asserter: str                        # Asserter value
    confidence_state: str                # State value: asserted | confirmed | contradicted
    resolution_policy: Optional[str] = None
    confirming_evidence: dict = field(default_factory=dict)
    asserted_by_version: str = ''
    last_checked_contract_sig: str = ''
    last_checked_net_logic_version: str = ''


class IdentityLedger:
    """In-memory identity ledger (§6). A row is a cheap PRIOR, never a permanent truth: it NEVER
    authorises skipping the §3 nets on an actual merge (that is verify_merge's whole contract). Rule:
    a CONTRADICTED row is never returned as usable — caching a wrong belief is worse than no cache."""

    def __init__(self):
        self._rows: Dict[str, IdentityLedgerRow] = {}

    def put(self, row: IdentityLedgerRow) -> None:
        self._rows[row.concept_key.canonical()] = row

    def get(self, key: ConceptKey) -> Optional[IdentityLedgerRow]:
        row = self._rows.get(key.canonical())
        if row is None or row.confidence_state == State.CONTRADICTED.value:
            return None                  # never serve a contradicted row as usable
        return row


def verify_merge(observations, *, ledger: Optional[IdentityLedger] = None,
                 tol_abs=None, tol_frac=None):
    """The load-bearing invariant (§0.1 / §6 / control #9): a merge decision ALWAYS comes from the nets
    run on the ACTUAL data, every run. A ledger hit (a confirmed prior) lets the caller skip the MODEL
    (moot model-off) — it NEVER lets the merge skip net verification. So mutated data beneath a
    'confirmed' row is still caught. Returns (promoted, (n1, n2, n3)). The `ledger` is read ONLY to
    decide model-consultation, deliberately NOT to decide the merge."""
    kw = {}
    if tol_abs is not None:
        kw['tol_abs'] = tol_abs
    if tol_frac is not None:
        kw['tol_frac'] = tol_frac
    # nets run unconditionally — the cache is never consulted for the merge decision
    n1, n2, n3, promoted = concept_nets.run_nets(observations, **kw)
    # (ledger, when present, would only tell a Phase-2 caller "skip the model call" — never "skip the nets")
    return promoted, (n1, n2, n3)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# real-corpus classifier (§5 deliverable) — reads the PRODUCTION run's own output, no reconstruction.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def classify_run(cir) -> AuditReport:
    """Classify every identity the production run formed, from the run's OWN checks (post-INC3 the
    agreement/corroboration checks ARE net results) + its held figures. A HARD agreement check that
    PASSED → CORROBORATED; one that FAILED, or a held figure → CONTRADICTED (quarantined); every other
    concept the run emitted with no agreement check → UNTESTED (single-source, held on own provenance).
    Reads res.cir only — the real production path — never a re-extraction."""
    findings: List[AuditFinding] = []
    tested_concepts = set()
    for c in getattr(cir, 'checks', []):
        cid = str(c.get('id', ''))
        if 'agree' not in cid and 'corrobor' not in cid:
            continue
        concept = cid.split('agrees_')[-1] if 'agrees_' in cid else cid
        tested_concepts.add(concept)
        b = Belief('synonym', concept, detail=cid)
        if c.get('status') == 'pass':
            findings.append(AuditFinding(b, CORROBORATED, detail=str(c.get('detail', ''))[:120]))
        else:
            findings.append(AuditFinding(b, CONTRADICTED, net=concept_nets.NET1,
                                         detail=str(c.get('detail', ''))[:120]))
    # every emitted concept with no agreement check is UNTESTED (no second source to check the belief)
    for rec in getattr(cir, 'records', []):
        for concept, fig in rec.fields.items():
            if not hasattr(fig, 'value_cr'):
                continue
            if concept in tested_concepts:
                continue
            tested_concepts.add(concept)
            findings.append(AuditFinding(Belief('synonym', concept, detail='single-source'),
                                         UNTESTED, detail='no agreement check — single source'))
    return AuditReport(findings)
