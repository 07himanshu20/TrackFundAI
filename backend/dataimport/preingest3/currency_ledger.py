"""U6 Phase 2 — the run-scoped Currency Ledger (the uncovered-currency report's source).

Every currency DECISION flows through exactly one function, `units.resolve_currency` (the Phase-2
enumeration proved it is the single complete choke: sites that can yield a foreign or conflicting
currency all reach it — company MIS, fund totals, fund ledgers — while the portfolio-FV path is
INR-by-construction and the rest are dead/verify-only). This ledger observes that choke: when a run
activates a ledger, resolve_currency records EVERY verdict here, tagged with the pipeline's current
source context. After the run, `uncovered_report()` aggregates the DETECTED currencies against the
rate card.

Why observe the decision, not the profiler's `currency_hints`: those hints are a lexical TOKEN scan
(₹/rs, rm/myr, $/usd). They are blind to a currency implied by DOMICILE with no token — a Malaysian
company with no marker resolves to MYR inside resolve_currency (domicile rule) yet carries no token,
so a token scan would miss it and a hints-based coverage check would pass while the figure silently
holds for want of a rate. Observing resolve_currency captures token-derived AND domicile-derived
currencies alike, so the report is complete by construction.

Fail-closed: a positively-resolved foreign currency with no rate in the card is UNCOVERED (drives the
prompt); a token-vs-domicile mismatch is a CONFLICT (disambiguate, not a rate); no-positive-evidence is
AMBIGUOUS (unknown currency — disclosed, cannot prompt for a rate). An empty report means an all-INR run.

DOCUMENTED ASSUMPTION: like the anchor-is-INR rule and the FV-INR-by-source path, this rests on the
whole system being an INR-reporting fund (BASE_CURRENCY = INR). Correct for an Indian AIF; the known
place to revisit if a non-INR-reporting fund is ever onboarded.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field, replace
from typing import List, Optional, Tuple

from .ratecard import BASE_CURRENCY


@dataclass
class _Obs:
    currency: Optional[str]          # resolved currency, or None when escalated
    escalate: bool
    flags: Tuple[str, ...]
    reason: str
    entity: Optional[str]
    source_file: Optional[str]


def _is_conflict(flags: Tuple[str, ...]) -> bool:
    return any(f.startswith('currency_conflict') for f in flags)


def _is_ambiguous(flags: Tuple[str, ...]) -> bool:
    return 'currency_ambiguous_no_evidence' in flags


def _is_user_confirmed(flags: Tuple[str, ...]) -> bool:
    # a figure whose currency was resolved by the fund's user-confirmed base (no file token, no
    # domicile) — weaker than file evidence, so it MUST be surfaced for per-batch human review.
    return 'currency_user_confirmed' in flags


@dataclass
class CurrencyLedger:
    """Run-scoped sink of currency verdicts. The pipeline sets the current source
    via `context(...)` as it enters each file/entity; resolve_currency calls
    `record(...)`. Read once at run end via `uncovered_report(rate_card)`."""
    _obs: List[_Obs] = field(default_factory=list)
    _entity: Optional[str] = None
    _file: Optional[str] = None

    def context(self, *, entity: Optional[str] = None, source_file: Optional[str] = None) -> None:
        self._entity, self._file = entity, source_file

    def record(self, *, currency, escalate, reason, flags) -> None:
        self._obs.append(_Obs(
            currency=(currency.upper() if isinstance(currency, str) else currency),
            escalate=bool(escalate), flags=tuple(flags or ()), reason=str(reason or ''),
            entity=self._entity, source_file=self._file))

    # ── per-worker capture / parent merge (L1 parallelism) ───────────────
    # Under process-parallel extraction each worker runs against its OWN local ledger (so the
    # module global is never written across processes — isolation is structural); the worker
    # RETURNS its observations and the parent MERGES them, in canonical file order, so the merged
    # sequence is byte-identical to what a single sequential run would have accumulated.
    def observations(self) -> List['_Obs']:
        """Snapshot of this ledger's raw observations (for a worker to return to the parent)."""
        return list(self._obs)

    def merge_observations(self, observations) -> None:
        """Append per-file observations captured by a worker, preserving order (the parent calls
        this at each file's emit point, in canonical order → equals the sequential accumulation)."""
        self._obs.extend(observations)

    # ── aggregation ─────────────────────────────────────────────────────
    @staticmethod
    def _sites(obs: List[_Obs]) -> List[dict]:
        # distinct (entity, file) sites, order-stable, no None-noise duplicates
        seen, out = set(), []
        for o in obs:
            key = (o.entity, o.source_file)
            if key in seen:
                continue
            seen.add(key)
            out.append({'entity': o.entity, 'file': o.source_file})
        return out

    def uncovered_report(self, rate_card, foreign_domiciles=None,
                         unmapped_foreign_domiciles=None) -> dict:
        """Machine-readable uncovered-currency report. Partitions the DETECTED
        currencies (INR excluded) into: uncovered (foreign, resolved, no rate →
        prompt), covered (foreign, resolved, rate present → will convert),
        conflicts (token-vs-domicile mismatch → disambiguate), ambiguous (no
        positive evidence → unknown currency, disclosed). `prompt_required` is
        True iff a rate would unblock something — i.e. any uncovered currency.

        `foreign_domiciles` (entity → implied currency, from portfolio domicile)
        makes the report SELF-CONTAINED about foreign exposure: a foreign-domiciled
        entity that produced NO currency-resolved figure this run (its statement was
        held/absent UPSTREAM, before any currency decision) is disclosed under
        `foreign_domicile_unresolved` — so covering the rate-actionable currencies
        can never read as 'all foreign exposure handled' when it is not. A rate does
        NOT surface these (they are resolution-blocked), so they do not set
        prompt_required; they are an honest completeness caveat, not a rate request.

        `unmapped_foreign_domiciles` (entity → raw domicile string) closes the last
        hole: an entity domiciled somewhere the domicile→currency map does not cover
        (non-India, unmapped) held upstream would appear in NEITHER bucket above —
        invisible. It is disclosed under `foreign_domicile_currency_unmapped` so an
        unmapped geography can never hide behind a green report, for ANY future
        fund. (Neutral where every domicile IS mapped.)

        `base_currency_applied` (condition #2 of the fund-base confirm) lists every
        (entity, file) site whose currency was resolved by the fund's user-confirmed
        base — NOT by a file token or domicile. It does NOT set prompt_required (a rate
        is not needed); it is the per-batch REVIEW surface so a newly-seen foreign
        entrant with no marker can never be silently swept into the base currency unseen.
        Empty when no figure took the fund base (e.g. an all-file-evidenced run)."""
        rates = getattr(rate_card, 'rates', {}) or {}

        # positively-resolved foreign currencies (escalate False, non-INR), grouped by ccy
        by_ccy: dict = {}
        conflicts: List[_Obs] = []
        ambiguous: List[_Obs] = []
        for o in self._obs:
            if o.escalate:
                if _is_conflict(o.flags):
                    conflicts.append(o)
                elif _is_ambiguous(o.flags):
                    ambiguous.append(o)
                continue
            if not o.currency or o.currency == BASE_CURRENCY:
                continue
            by_ccy.setdefault(o.currency, []).append(o)

        uncovered, covered = [], []
        for ccy in sorted(by_ccy):
            obs = by_ccy[ccy]
            if ccy in rates:
                r = rates[ccy]
                covered.append({'currency': ccy, 'inr_per_unit': str(r.inr_per_unit),
                                'rate_date': r.rate_date, 'source': r.source,
                                'sites': self._sites(obs), 'count': len(obs)})
            else:
                uncovered.append({'currency': ccy, 'sites': self._sites(obs), 'count': len(obs)})

        def _conf_rows(rows: List[_Obs]) -> List[dict]:
            return [{'flags': list(o.flags), 'reason': o.reason,
                     'entity': o.entity, 'file': o.source_file} for o in rows]

        # foreign-domiciled entities that never reached a currency decision this run
        observed = {o.entity for o in self._obs if o.entity}
        fdu = []
        for ent in sorted(foreign_domiciles or {}):
            ccy = foreign_domiciles[ent]
            if ccy and ccy != BASE_CURRENCY and ent not in observed:
                fdu.append({'entity': ent, 'implied_currency': ccy,
                            'reason': 'foreign-domiciled portfolio entity with no currency-resolved '
                                      'figure this run (its statement was held/absent upstream) — a '
                                      'rate would not surface it; resolve the entity/statement separately'})

        fdcu = []
        for ent in sorted(unmapped_foreign_domiciles or {}):
            if ent not in observed:
                fdcu.append({'entity': ent, 'domicile': unmapped_foreign_domiciles[ent],
                             'reason': 'foreign-domiciled (non-India) but its domicile maps to no known '
                                       'currency — possible uncovered exposure the system cannot yet name; '
                                       'add the domicile→currency mapping (and then a rate) to surface it'})

        # condition #2 review surface: distinct sites resolved via the fund's user-confirmed base
        base_applied = self._sites([o for o in self._obs if _is_user_confirmed(o.flags)])

        return {
            'as_of': getattr(rate_card, 'as_of', None),
            'card_id': getattr(rate_card, 'card_id', None),
            'uncovered': uncovered,
            'covered': covered,
            'conflicts': _conf_rows(conflicts),
            'ambiguous': _conf_rows(ambiguous),
            'foreign_domicile_unresolved': fdu,
            'foreign_domicile_currency_unmapped': fdcu,
            'base_currency_applied': base_applied,
            'prompt_required': bool(uncovered),
        }


# ── run-scoped activation (the pipeline owns the lifecycle) ───────────────
# The active ledger keeps resolve_currency's signature and its purity for every
# caller/test unchanged: when no ledger is active (the default, and all unit tests),
# record() is never reached. The pipeline sets exactly one ledger per run.
#
# It is a ContextVar, not a plain module global, so that CONCURRENT per-file extraction
# (the AI-on speed path — I/O-bound model calls fanned across worker threads) keeps each
# file's currency observations ISOLATED to its own thread: a ContextVar has an independent
# value per thread/async-task. Were this a shared global, two files resolving in parallel
# would record into the same ledger and cross-contaminate — the exact silent ~18× (MYR read
# as INR) class the whole U6 design exists to prevent. Single-threaded behaviour is
# byte-identical (a ContextVar with default None behaves exactly like the old global).
_ACTIVE: "contextvars.ContextVar[Optional[CurrencyLedger]]" = contextvars.ContextVar(
    'currency_ledger_active', default=None)


def active() -> Optional[CurrencyLedger]:
    return _ACTIVE.get()


def set_active(ledger: Optional[CurrencyLedger]) -> None:
    _ACTIVE.set(ledger)


def observe(*, currency, escalate, reason, flags) -> None:
    """The hook resolve_currency calls. No-op unless a run has activated a ledger."""
    led = _ACTIVE.get()
    if led is not None:
        led.record(currency=currency, escalate=escalate, reason=reason, flags=flags)


def restamp(observations, *, entity, source_file):
    """Return copies of `observations` re-tagged to (entity, source_file). Used when a CACHED file's
    currency verdicts are replayed into the run ledger on a cache hit: currency detection is deterministic
    and model-free, so a served file's verdicts must still populate the uncovered-currency report exactly
    as a fresh extraction would — re-stamping to the CURRENT attribution keeps a re-attributed file
    (same content, new company) sited correctly. Values/flags are untouched; only the source tags move."""
    return [replace(o, entity=entity, source_file=source_file) for o in (observations or [])]


def context(*, entity=None, source_file=None) -> None:
    """Set the current source on the active ledger (no-op if none active)."""
    led = _ACTIVE.get()
    if led is not None:
        led.context(entity=entity, source_file=source_file)
