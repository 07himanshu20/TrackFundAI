import uuid
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class Organization(models.Model):
    """Multi-tenant organization (one per fund house / GP)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True)
    subscription_tier = models.CharField(
        max_length=20,
        choices=[
            ('starter', 'Starter'),
            ('growth', 'Growth'),
            ('enterprise', 'Enterprise'),
        ],
        default='starter',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class User(AbstractUser):
    """Custom user model with organization + role."""

    ROLE_CHOICES = [
        ('platform_admin', 'Platform Admin'),
        ('gp_admin', 'GP Admin'),
        ('gp_user', 'GP User'),
        ('compliance_officer', 'Compliance Officer'),
        ('fund_accountant', 'Fund Accountant'),
        ('lp_user', 'LP User'),
        ('founder_user', 'Founder User'),
        ('external_auditor', 'External Auditor'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='users',
        null=True, blank=True,
    )
    role = models.CharField(max_length=30, choices=ROLE_CHOICES, default='gp_user')
    phone = models.CharField(max_length=20, blank=True)
    mfa_enabled = models.BooleanField(default=False)

    class Meta:
        ordering = ['username']

    def __str__(self):
        return f'{self.username} ({self.get_role_display()})'

    @property
    def is_gp(self):
        return self.role in ('platform_admin', 'gp_admin', 'gp_user',
                             'compliance_officer', 'fund_accountant')

    @property
    def is_admin(self):
        return self.role in ('platform_admin', 'gp_admin')


class FundAccess(models.Model):
    """Which funds a user can access (row-level security)."""
    ACCESS_LEVELS = [
        ('read', 'Read Only'),
        ('write', 'Read + Write'),
        ('admin', 'Full Admin'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='fund_access',
    )
    fund = models.ForeignKey(
        'funds.Fund',
        on_delete=models.CASCADE,
        related_name='user_access',
    )
    access_level = models.CharField(max_length=10, choices=ACCESS_LEVELS, default='read')
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'fund')
        ordering = ['user', 'fund']

    def __str__(self):
        return f'{self.user.username} → {self.fund.name} ({self.access_level})'


class AuditLog(models.Model):
    """Immutable audit trail — append-only, no updates or deletes."""
    ACTION_CHOICES = [
        ('create', 'Create'),
        ('read', 'Read'),
        ('update', 'Update'),
        ('delete', 'Delete'),
        ('export', 'Export'),
        ('login', 'Login'),
        ('logout', 'Logout'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    resource_type = models.CharField(max_length=100)
    resource_id = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['organization', '-timestamp']),
            models.Index(fields=['user', '-timestamp']),
            models.Index(fields=['resource_type', 'resource_id']),
        ]

    def __str__(self):
        return f'{self.action} {self.resource_type} by {self.user} at {self.timestamp}'
