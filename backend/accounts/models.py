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
    """
    Custom user model with organization + role.
    Maps to FundOS: users table.

    Added: failed_login_count, account_locked_until, investor FK (for LP users),
    mfa_totp_secret.
    """

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

    # Security — account lockout (SOC 2 compliance)
    failed_login_count = models.PositiveIntegerField(
        default=0,
        help_text='Account lockout after 5 failures',
    )
    account_locked_until = models.DateTimeField(
        null=True, blank=True,
        help_text='Account locked until this timestamp after too many failed logins',
    )

    # LP user linkage — set when user_type is lp_user
    # This FK will be added when LP app is created (Phase 3)
    # investor_id will link to lp.Investor once that model exists

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

    @property
    def is_locked(self):
        if self.account_locked_until:
            from django.utils import timezone
            return timezone.now() < self.account_locked_until
        return False


class FundAccess(models.Model):
    """
    Which funds a user can access (row-level security).
    Maps to FundOS: fund_user_access table.

    Added: expires_at (time-bound access for auditors),
    revoked_at (soft revocation — never delete access records).
    """
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
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Time-bound access for auditors — NULL means permanent',
    )
    revoked_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Soft revocation — never delete access records for audit trail',
    )

    class Meta:
        unique_together = ('user', 'fund')
        ordering = ['user', 'fund']

    def __str__(self):
        return f'{self.user.username} → {self.fund.name} ({self.access_level})'

    @property
    def is_active(self):
        """Check if access is currently valid (not expired, not revoked)."""
        if self.revoked_at:
            return False
        if self.expires_at:
            from django.utils import timezone
            return timezone.now() < self.expires_at
        return True


class SchemeAccess(models.Model):
    """
    Scheme-level access control (finer-grained than fund-level).
    Maps to FundOS: scheme_user_access table.

    Allows restricting a user to specific schemes within a fund
    (e.g., an auditor only auditing Scheme II).
    """
    ACCESS_LEVELS = [
        ('read', 'Read Only'),
        ('write', 'Read + Write'),
        ('admin', 'Full Admin'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='scheme_access',
    )
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='user_access',
    )
    access_level = models.CharField(max_length=10, choices=ACCESS_LEVELS, default='read')
    granted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('user', 'scheme')
        ordering = ['user', 'scheme']

    def __str__(self):
        return f'{self.user.username} → {self.scheme} ({self.access_level})'

    @property
    def is_active(self):
        if self.revoked_at:
            return False
        if self.expires_at:
            from django.utils import timezone
            return timezone.now() < self.expires_at
        return True


class AuditLog(models.Model):
    """
    Immutable audit trail — append-only, no updates or deletes.
    Maps to FundOS: audit_log table.

    Added: old_values, new_values JSONB fields for SOC 2 before/after state tracking.
    """
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

    # SOC 2 / SEBI compliance — before/after state
    old_values = models.JSONField(
        default=dict, blank=True,
        help_text='Before state — SOC 2 / SEBI evidence',
    )
    new_values = models.JSONField(
        default=dict, blank=True,
        help_text='After state — full change record',
    )
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
