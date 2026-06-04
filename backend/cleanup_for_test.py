"""One-shot cleanup: wipe all project-owned fund/import data, keep auth.

NO model names, app names, or app labels are enumerated by hand. Instead:
  - "Project apps" are detected at runtime: any app whose source module
    lives under the project root (backend/) is project-owned.
  - The auth user app is detected from settings.AUTH_USER_MODEL and is
    automatically preserved (so login still works).
  - System / third-party apps (django.contrib.*, site-packages) are auto-
    detected by module path and left alone.

This is reusable for any TrackFundAI project layout — no need to update if
new apps are added or removed.
"""
import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.apps import apps
from django.conf import settings
from django.db import transaction


def _project_root() -> str:
    """The directory containing this script — i.e. backend/."""
    return os.path.realpath(os.path.dirname(__file__))


def _is_project_app(app_config) -> bool:
    """An app is 'project-owned' iff its module is under backend/ AND not
    inside any site-packages directory."""
    path = os.path.realpath(getattr(app_config, 'path', '') or '')
    if not path:
        return False
    if 'site-packages' in path or 'dist-packages' in path:
        return False
    return path.startswith(_project_root() + os.sep) or path == _project_root()


def _auth_user_app_label() -> str:
    """Read AUTH_USER_MODEL ('accounts.User' → 'accounts')."""
    raw = getattr(settings, 'AUTH_USER_MODEL', '')
    return raw.split('.', 1)[0] if '.' in raw else raw


def _build_targets():
    """Return list of (label, model) for every model we should wipe."""
    auth_label = _auth_user_app_label()
    targets = []
    skipped = []
    for app_config in apps.get_app_configs():
        if not _is_project_app(app_config):
            skipped.append(('non-project', app_config.label))
            continue
        if app_config.label == auth_label:
            skipped.append(('auth-user', app_config.label))
            continue
        for model in app_config.get_models():
            targets.append((f'{app_config.label}.{model.__name__}', model))
    return targets, skipped


def _wipe(targets):
    deleted = {}
    remaining = list(targets)
    for _ in range(6):
        next_remaining = []
        for label, model in remaining:
            try:
                with transaction.atomic():
                    n, _detail = model.objects.all().delete()
                if n:
                    deleted[label] = deleted.get(label, 0) + n
            except Exception:
                next_remaining.append((label, model))
        if not next_remaining:
            break
        remaining = next_remaining
    return deleted, remaining


targets, skipped = _build_targets()
deleted, remaining = _wipe(targets)

print('=' * 66)
print(f'DYNAMIC CLEANUP — discovered {len(targets)} project models')
print(f'auto-detected project root: {_project_root()}')
print(f'auto-detected auth user app: "{_auth_user_app_label()}" (kept)')
print('=' * 66)

if deleted:
    print('DELETED:')
    for k in sorted(deleted):
        print(f'  {k:<48s} -{deleted[k]}')
else:
    print('  (nothing to delete — database already clean)')

if remaining:
    print('-' * 66)
    print('REMAINING after 6 passes (FK cycles?):')
    for label, _ in remaining:
        print(f'  {label}')

print('-' * 66)
print('Apps SKIPPED (auto-detected as system/auth):')
for kind, label in sorted(set(skipped)):
    print(f'  [{kind}] {label}')

# Verify auth retained
from django.contrib.auth import get_user_model
User = get_user_model()
print('-' * 66)
print(f'AUTH KEPT: {User.__module__}.{User.__name__} count={User.objects.count()}')

# Final consistency check
print('-' * 66)
print('Post-cleanup row counts (project apps only):')
any_nonzero = False
for label, model in targets:
    try:
        c = model.objects.count()
        if c:
            any_nonzero = True
            print(f'  {label:<48s} {c}')
    except Exception:
        pass
if not any_nonzero:
    print('  (all project tables at 0 rows)')
print('=' * 66)
