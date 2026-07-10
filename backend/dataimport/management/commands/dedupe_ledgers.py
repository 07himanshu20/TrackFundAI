"""
Fix U7 — One-time dedupe for ledger tables.

Historical duplicate rows in Scheme / CapitalCall / Distribution / ExitEvent /
Commitment survive from earlier import runs that pre-dated the current
`update_or_create` natural keys. Since 2026-07-08, every persister uses
`update_or_create` on a natural key, so future imports do not create new
duplicates. This command removes the historical ones.

Usage:
    # Show what WOULD be deleted, but change nothing:
    python manage.py dedupe_ledgers --dry-run

    # Actually delete:
    python manage.py dedupe_ledgers --commit

    # Restrict to one organization (safer for staging validation):
    python manage.py dedupe_ledgers --commit --organization "<org id or name>"

Safety:
  • Groups rows by a natural-key tuple. Within a group, the NEWEST row wins
    (by `updated_at` if present, else `created_at`, else `id`). All others
    in the group are deleted.
  • Cascade deletes are relied upon — deleting an ExitEvent cascades any
    dependent rows; the FKs are set up for it.
  • Runs inside a transaction; --dry-run reports counts and rolls back.
  • Never touches Fund itself (the fund table is small and its unique
    constraint `(organization, name)` already prevents duplicates).
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F, Max


def _pick_survivor(rows):
    """Given a list of model instances (same natural key), return the one to
    KEEP. Preference order:
      1. Highest updated_at (newest edit)
      2. Highest created_at (newest create)
      3. Highest id (last insert)
    Ties are broken deterministically by id.
    """
    def _sortkey(r):
        return (
            getattr(r, 'updated_at', None) or getattr(r, 'created_at', None),
            getattr(r, 'created_at', None),
            str(getattr(r, 'id', '')),
        )
    return max(rows, key=_sortkey)


def _dedupe_by_key(model_class, key_fn, *, qs=None, dry_run=True, logger=print, name=None):
    """Delete duplicate rows in `model_class` grouped by `key_fn(row)`.
    Returns (groups_scanned, duplicates_deleted)."""
    if qs is None:
        qs = model_class.objects.all()
    groups: dict = {}
    for row in qs.iterator():
        key = key_fn(row)
        if key is None:
            continue
        groups.setdefault(key, []).append(row)
    deleted = 0
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        survivor = _pick_survivor(rows)
        for r in rows:
            if r.id == survivor.id:
                continue
            if not dry_run:
                r.delete()
            deleted += 1
    label = name or model_class.__name__
    logger(f'  {label:20s} groups={len(groups):>6d}   duplicates_to_delete={deleted}')
    return len(groups), deleted


class Command(BaseCommand):
    help = 'One-time dedupe of ledger tables (Fix U7).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit', action='store_true',
            help='Actually delete. Without this flag the command runs dry (no writes).',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Explicit dry-run (default when --commit is omitted).',
        )
        parser.add_argument(
            '--organization', type=str, default=None,
            help='Restrict scope to this organization (id or name).',
        )

    def handle(self, *args, **opts):
        dry_run = not opts.get('commit')
        org_arg = opts.get('organization')

        # Import inside handle() so Django app registry is ready.
        from accounts.models import Organization
        from funds.models import Scheme
        from lp.models import CapitalCall, Distribution, Commitment
        from investments.models import ExitEvent

        org_filter = None
        if org_arg:
            org = (Organization.objects.filter(id=org_arg).first()
                   or Organization.objects.filter(name__iexact=org_arg).first())
            if not org:
                raise CommandError(f'Organization not found: {org_arg!r}')
            org_filter = org
            self.stdout.write(f'Scope: organization = {org.name!r} ({org.id})')
        else:
            self.stdout.write('Scope: ALL organizations')

        self.stdout.write(('DRY RUN — no writes will be made' if dry_run
                           else 'COMMIT — duplicates WILL be deleted'))
        self.stdout.write('')

        # Natural-key definitions match the persister's update_or_create keys
        # (see phase2_persister.py:823, 1044-1047, 1658-1661, 1730-1733, 916-919).
        NATURAL_KEYS = [
            (
                'Scheme',
                Scheme,
                lambda r: (str(r.fund_id), (r.name or '').strip().lower()),
                lambda qs, org: qs.filter(fund__organization=org) if org else qs,
            ),
            (
                'CapitalCall',
                CapitalCall,
                lambda r: (str(r.scheme_id), r.call_number),
                lambda qs, org: qs.filter(scheme__fund__organization=org) if org else qs,
            ),
            (
                'Distribution',
                Distribution,
                lambda r: (str(r.scheme_id), r.distribution_number),
                lambda qs, org: qs.filter(scheme__fund__organization=org) if org else qs,
            ),
            (
                'ExitEvent',
                ExitEvent,
                lambda r: (str(r.investment_id), r.exit_date.isoformat() if r.exit_date else None),
                lambda qs, org: qs.filter(investment__scheme__fund__organization=org) if org else qs,
            ),
            (
                'Commitment',
                Commitment,
                lambda r: (str(r.investor_id), str(r.scheme_id)),
                lambda qs, org: qs.filter(scheme__fund__organization=org) if org else qs,
            ),
        ]

        totals = {'groups': 0, 'deleted': 0}

        try:
            with transaction.atomic():
                for label, model_class, key_fn, scope_fn in NATURAL_KEYS:
                    qs = scope_fn(model_class.objects.all(), org_filter)
                    groups, deleted = _dedupe_by_key(
                        model_class, key_fn,
                        qs=qs,
                        dry_run=dry_run,
                        logger=self.stdout.write,
                        name=label,
                    )
                    totals['groups'] += groups
                    totals['deleted'] += deleted
                if dry_run:
                    # Force rollback so the dry-run leaves no side effects.
                    transaction.set_rollback(True)
        except Exception as e:
            raise CommandError(f'Dedupe failed: {e!r}')

        self.stdout.write('')
        self.stdout.write(f'TOTAL groups scanned: {totals["groups"]}')
        self.stdout.write(f'TOTAL duplicates {"WOULD BE" if dry_run else ""} '
                          f'deleted: {totals["deleted"]}')
        if dry_run:
            self.stdout.write('')
            self.stdout.write('Re-run with --commit to actually delete.')
