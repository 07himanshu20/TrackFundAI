"""
Utility functions for creating notifications from other modules.
Usage:
    from notifications.helpers import notify_user, notify_org_admins

    notify_user(user, 'Document Uploaded', 'A new PPM has been uploaded.', category='document')
    notify_org_admins(org, 'New Fund Created', 'Fund XYZ has been registered.', category='fund')
"""
from accounts.models import User


def notify_user(recipient, title, message, category='system', priority='normal',
                resource_type='', resource_id='', created_by=None):
    """Create a notification for a single user."""
    from .models import Notification
    return Notification.objects.create(
        organization=recipient.organization,
        recipient=recipient,
        title=title,
        message=message,
        category=category,
        priority=priority,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id else '',
        created_by=created_by,
    )


def notify_org_admins(organization, title, message, category='system', priority='normal',
                      resource_type='', resource_id='', created_by=None, exclude_user=None):
    """Create a notification for all GP admins in an organization."""
    from .models import Notification
    admins = User.objects.filter(
        organization=organization,
        role__in=['platform_admin', 'gp_admin'],
        is_active=True,
    )
    if exclude_user:
        admins = admins.exclude(pk=exclude_user.pk)

    notifications = []
    for admin in admins:
        notifications.append(Notification(
            organization=organization,
            recipient=admin,
            title=title,
            message=message,
            category=category,
            priority=priority,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else '',
            created_by=created_by,
        ))
    return Notification.objects.bulk_create(notifications)


def notify_org_users(organization, title, message, category='system', priority='normal',
                     resource_type='', resource_id='', created_by=None, exclude_user=None):
    """Create a notification for all active users in an organization."""
    from .models import Notification
    users = User.objects.filter(
        organization=organization,
        is_active=True,
    )
    if exclude_user:
        users = users.exclude(pk=exclude_user.pk)

    notifications = []
    for user in users:
        notifications.append(Notification(
            organization=organization,
            recipient=user,
            title=title,
            message=message,
            category=category,
            priority=priority,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else '',
            created_by=created_by,
        ))
    return Notification.objects.bulk_create(notifications)
