"""
DB-backed, org-scoped alias store for the preingest3 review-gate write-back loop.

The standalone library (preingest3.alias_ledger.AliasLedger) persists to a single
JSON file guarded by threading.Lock — correct for one process, UNSAFE across the
multiple gunicorn/uwsgi workers a real deployment runs (lost/corrupted writes to
the exact data the 'learn once' promise depends on). This adapter presents the SAME
duck-typed interface (lookup / learn / purge_auto) the pipeline injects, but backed
by the transactional, per-organization PreIngestAlias table.

Key normalisation mirrors the library exactly (namematch.tokens) so a binding learnt
here is found by the same lookup the library would do.
"""
from __future__ import annotations

from typing import List, Optional

from django.db import transaction

from .models import PreIngestAlias
from .preingest3.namematch import tokens as _tokens


def _key(ident: str) -> str:
    return ' '.join(_tokens(ident))


class DbAliasLedger:
    """org-scoped alias ledger over PreIngestAlias. `org` is an Organization pk."""

    def __init__(self, organization, confirmed_by=None):
        self.org = organization
        self.confirmed_by = confirmed_by

    def lookup(self, identifiers: List[str]) -> Optional[str]:
        keys = [k for k in (_key(i) for i in identifiers) if k]
        if not keys:
            return None
        # a human-confirmed binding wins over an auto one on the same run
        rows = list(PreIngestAlias.objects.filter(organization=self.org, key__in=keys))
        if not rows:
            return None
        rows.sort(key=lambda r: 0 if r.src == 'human' else 1)
        return rows[0].entity_id

    def learn(self, identifiers: List[str], entity_id: str, *, provenance: str = 'auto'):
        keys = [k for k in (_key(i) for i in identifiers) if k]
        with transaction.atomic():
            for key in keys:
                existing = (PreIngestAlias.objects
                            .select_for_update()
                            .filter(organization=self.org, key=key)
                            .first())
                # a human confirmation is never overwritten by a later auto guess
                if existing and existing.src == 'human' and provenance != 'human':
                    continue
                if existing:
                    existing.entity_id = entity_id
                    existing.src = provenance
                    if provenance == 'human':
                        existing.confirmed_by = self.confirmed_by
                    existing.save(update_fields=['entity_id', 'src', 'confirmed_by', 'updated_at'])
                else:
                    PreIngestAlias.objects.create(
                        organization=self.org, key=key, entity_id=entity_id,
                        src=provenance,
                        confirmed_by=self.confirmed_by if provenance == 'human' else None)

    def purge_auto(self):
        """Drop every auto-resolved binding for this org (keep human) — recovery
        from a poisoned run."""
        PreIngestAlias.objects.filter(organization=self.org, src='auto').delete()
