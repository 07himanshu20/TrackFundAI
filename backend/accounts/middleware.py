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
