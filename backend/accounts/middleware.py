class OrganizationMiddleware:
    """Attach a lazy request.organization that resolves after DRF auth.

    DRF authenticates inside the view (not at middleware time), so we can't
    read request.user in process_request. Instead we install a descriptor
    that defers the lookup until the view code actually accesses
    request.organization.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.__class__.organization = _OrgDescriptor()
        return self.get_response(request)


class _OrgDescriptor:
    """Descriptor that lazily reads user.organization on first access."""

    attr = '_cached_organization'

    def __get__(self, request, objtype=None):
        if request is None:
            return self
        cached = getattr(request, self.attr, _SENTINEL)
        if cached is not _SENTINEL:
            return cached
        org = None
        user = getattr(request, 'user', None)
        if user and getattr(user, 'is_authenticated', False):
            org = getattr(user, 'organization', None)
        request._cached_organization = org
        return org

    def __set__(self, request, value):
        request._cached_organization = value


_SENTINEL = object()


class CacheInvalidationMiddleware:
    """Auto-clear Redis cache when any write operation succeeds.

    After a POST/PUT/PATCH/DELETE returns 2xx, this middleware invalidates
    the org-level API cache so subsequent GET requests fetch fresh data.

    Fund-specific invalidation happens in the import service (more targeted).
    This middleware provides a safety net for all other write endpoints.
    """

    # Paths that don't modify fund data — skip invalidation for these
    _SKIP_PATHS = {
        '/api/auth/',
        '/api/chatbot/',
        '/api/notifications/',
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Only invalidate on successful write operations
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            if 200 <= response.status_code < 300:
                path = request.path
                if not any(path.startswith(skip) for skip in self._SKIP_PATHS):
                    org = getattr(request, '_cached_organization', None)
                    if org:
                        try:
                            from config.cache_utils import invalidate_org_cache
                            invalidate_org_cache(org.id)
                        except Exception:
                            pass  # Cache invalidation is best-effort

        return response
