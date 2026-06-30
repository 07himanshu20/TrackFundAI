"""
Cache utilities for TrackFundAI.

Provides fund/org-scoped cache invalidation so that after an Excel import
(or any data mutation), stale cached API responses are cleared immediately.

Usage in views:
    from config.cache_utils import cached_api_view, invalidate_fund_cache

    @cached_api_view(timeout=300)  # 5 min
    def my_view(request):
        ...

    # After import or data change:
    invalidate_fund_cache(org_id, fund_id)
"""
import hashlib
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

# ── Cache key construction ────────────────────────────────────────────────

# Prefix for all API response cache keys
_API_PREFIX = 'api'

# Registry key that holds the set of all cache keys for a given org+fund
_REGISTRY_PREFIX = 'reg'


def _make_view_key(org_id, view_name, query_string, path_kwargs=None):
    """Build a deterministic cache key for an API response.

    Includes URL path kwargs (e.g. node_id, company_id, scheme_id) so views
    like /api/portfolio/node/<id>/ get distinct cache entries per id.
    Without this, all calls to the same view share one entry — silently
    returning the wrong row.
    """
    parts = [query_string]
    if path_kwargs:
        for k in sorted(path_kwargs):
            parts.append(f'{k}={path_kwargs[k]}')
    key_material = '|'.join(parts)
    qs_hash = hashlib.md5(key_material.encode()).hexdigest()[:12]
    return f'{_API_PREFIX}:{org_id}:{view_name}:{qs_hash}'


def _make_registry_key(org_id, fund_id=None):
    """Key that holds the list of all cached view keys for an org (optionally per fund)."""
    if fund_id:
        return f'{_REGISTRY_PREFIX}:{org_id}:fund:{fund_id}'
    return f'{_REGISTRY_PREFIX}:{org_id}:all'


# ── Caching decorator for views ──────────────────────────────────────────

def cached_api_view(timeout=300):
    """
    Decorator for DRF @api_view GET endpoints.

    - Caches the full JSON response keyed by (org, view_name, query_string).
    - Registers the key in an org+fund registry so invalidation can find it.
    - Only caches GET requests from authenticated users with an org.

    Usage:
        @api_view(['GET'])
        @permission_classes([IsGPUser])
        @cached_api_view(timeout=600)
        def my_list_view(request):
            ...
    """
    from functools import wraps
    from rest_framework.response import Response

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            # Only cache GET requests with valid org
            if request.method != 'GET':
                return view_func(request, *args, **kwargs)

            org = getattr(request, 'organization', None)
            if not org:
                return view_func(request, *args, **kwargs)

            org_id = str(org.id)
            view_name = view_func.__name__
            query_string = request.META.get('QUERY_STRING', '')
            cache_key = _make_view_key(org_id, view_name, query_string, kwargs)

            # Check cache — gracefully skip if Redis is unavailable
            try:
                cached = cache.get(cache_key)
                if cached is not None:
                    return Response(cached)
            except Exception:
                pass  # Redis down — fall through to live query

            # Execute view
            response = view_func(request, *args, **kwargs)

            # Only cache successful responses
            if response.status_code == 200:
                try:
                    cache.set(cache_key, response.data, timeout)
                    _register_key(org_id, cache_key, query_string)
                except Exception:
                    pass  # Redis down — skip caching

            return response
        return wrapper
    return decorator


def _register_key(org_id, cache_key, query_string):
    """Track cache keys so invalidation can find and delete them."""
    # Always register under org-wide registry
    org_reg_key = _make_registry_key(org_id)
    _add_to_set(org_reg_key, cache_key)

    # If query string contains a fund= param, also register under that fund
    fund_id = _extract_param(query_string, 'fund')
    if fund_id:
        fund_reg_key = _make_registry_key(org_id, fund_id)
        _add_to_set(fund_reg_key, cache_key)


def _extract_param(query_string, param_name):
    """Extract a parameter value from a query string."""
    from urllib.parse import parse_qs
    params = parse_qs(query_string)
    values = params.get(param_name, [])
    return values[0] if values else None


def _add_to_set(registry_key, cache_key):
    """Add a cache key to a registry set (stored as a list in cache)."""
    try:
        current = cache.get(registry_key) or []
        if cache_key not in current:
            current.append(cache_key)
            cache.set(registry_key, current, 7200)
    except Exception:
        pass  # Redis down — skip registry


# ── Cache invalidation ───────────────────────────────────────────────────

def invalidate_fund_cache(org_id, fund_id=None):
    """
    Clear all cached API responses for a given organization + fund.

    Called after:
    - Excel import completes
    - Manual data creation/update/delete via API
    - Any operation that changes fund data

    If fund_id is provided, clears only caches for that fund.
    Always clears the org-wide cache (since overview pages aggregate all funds).
    """
    org_id = str(org_id)
    keys_cleared = 0

    try:
        # Always clear org-wide cached views (overview, all-funds pages)
        org_reg_key = _make_registry_key(org_id)
        org_keys = cache.get(org_reg_key) or []
        if org_keys:
            cache.delete_many(org_keys)
            keys_cleared += len(org_keys)
            cache.delete(org_reg_key)

        # Clear fund-specific cached views
        if fund_id:
            fund_id = str(fund_id)
            fund_reg_key = _make_registry_key(org_id, fund_id)
            fund_keys = cache.get(fund_reg_key) or []
            if fund_keys:
                cache.delete_many(fund_keys)
                keys_cleared += len(fund_keys)
                cache.delete(fund_reg_key)
    except Exception:
        pass  # Redis down — skip invalidation

    if keys_cleared:
        logger.info(
            f'Cache invalidated: org={org_id}, fund={fund_id or "all"}, '
            f'keys_cleared={keys_cleared}'
        )


def invalidate_org_cache(org_id):
    """Clear ALL cached API responses for an entire organization."""
    invalidate_fund_cache(org_id, fund_id=None)
