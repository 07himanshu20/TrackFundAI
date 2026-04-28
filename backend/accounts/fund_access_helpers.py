"""
Fund-level access control helpers.

Enforces FundAccess row-level security across all views.
Users only see funds they have active (non-revoked, non-expired) FundAccess to.
GP Admins who are also platform_admin bypass this check (they see all org funds).
"""
from django.db.models import Q
from django.utils import timezone

from .models import FundAccess


def get_accessible_fund_ids(user):
    """
    Return a set of fund UUIDs that the user has active access to.

    Rules:
    - platform_admin: sees all funds in their org (no FundAccess filter)
    - Everyone else: only funds with active FundAccess records
    """
    if user.role == 'platform_admin':
        from funds.models import Fund
        return set(
            Fund.objects.filter(organization=user.organization)
            .values_list('id', flat=True)
        )

    now = timezone.now()
    return set(
        FundAccess.objects.filter(
            user=user,
            revoked_at__isnull=True,
        )
        .filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=now)
        )
        .values_list('fund_id', flat=True)
    )


def filter_funds_for_user(queryset, user):
    """
    Filter a Fund queryset to only include funds the user has access to.
    Also enforces organization scoping.
    """
    org = user.organization
    if not org:
        return queryset.none()

    qs = queryset.filter(organization=org)

    if user.role == 'platform_admin':
        return qs

    fund_ids = get_accessible_fund_ids(user)
    return qs.filter(id__in=fund_ids)


def filter_by_fund_access(queryset, user, fund_field='fund'):
    """
    Filter any queryset that has a FK to Fund.

    Args:
        queryset: The queryset to filter
        user: The requesting user
        fund_field: The field path to the Fund FK (e.g., 'fund', 'scheme__fund',
                    'investment__scheme__fund', 'investor__organization' etc.)

    Returns:
        Filtered queryset
    """
    org = user.organization
    if not org:
        return queryset.none()

    if user.role == 'platform_admin':
        return queryset

    fund_ids = get_accessible_fund_ids(user)
    lookup = f'{fund_field}__id__in'
    return queryset.filter(**{lookup: fund_ids})


def user_has_fund_access(user, fund):
    """Check if a specific user has active access to a specific fund."""
    if user.role == 'platform_admin' and user.organization == fund.organization:
        return True

    now = timezone.now()
    return FundAccess.objects.filter(
        user=user,
        fund=fund,
        revoked_at__isnull=True,
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    ).exists()
