"""
Reusable audit logging helper.
Usage:
    from accounts.audit import log_audit
    log_audit(request, 'create', 'fund', fund.id, {'name': fund.name})
"""
from .models import AuditLog


def _get_client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR')


def log_audit(request, action, resource_type, resource_id='', details=None):
    """Create an audit log entry from a DRF request."""
    return AuditLog.objects.create(
        user=request.user if request.user.is_authenticated else None,
        organization=getattr(request, 'organization', None),
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        details=details or {},
        ip_address=_get_client_ip(request),
    )
