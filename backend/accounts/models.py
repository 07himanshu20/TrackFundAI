import hashlib
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

    # v5: Exactly 6 roles
    ROLE_CHOICES = [
        ('platform_admin', 'Super Admin'),
        ('gp_admin', 'GP Partner'),
        ('fund_accountant', 'CFO'),
        ('analyst', 'Investment Analyst'),
        ('compliance_officer', 'Compliance Officer'),
        ('lp_user', 'LP'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='users',
        null=True, blank=True,
    )
    role = models.CharField(max_length=30, choices=ROLE_CHOICES, default='analyst')
    phone = models.CharField(max_length=20, blank=True)

    # MFA — TOTP (Google Authenticator / pyotp)
    mfa_enabled = models.BooleanField(default=False)
    mfa_totp_secret = models.CharField(
        max_length=64, blank=True, default='',
        help_text='Base32 TOTP secret (AES-256 encrypted at rest in prod)',
    )

    # MFA — SMS OTP (MSG91/Fast2SMS)
    mfa_sms_enabled = models.BooleanField(default=False)
    mfa_sms_otp = models.CharField(max_length=10, blank=True, default='')
    mfa_sms_otp_expires = models.DateTimeField(null=True, blank=True)

    # Security — account lockout (v5: 3 attempts → 24h lockout)
    login_attempts = models.PositiveIntegerField(
        default=0,
        help_text='Failed login count — resets on successful login',
    )
    lockout_until = models.DateTimeField(
        null=True, blank=True,
        help_text='Account locked until this timestamp (24h after 3rd failed attempt)',
    )

    # Legacy fields kept for backward compatibility
    failed_login_count = models.PositiveIntegerField(default=0)
    account_locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['username']

    def __str__(self):
        return f'{self.username} ({self.get_role_display()})'

    @property
    def is_gp(self):
        return self.role in ('platform_admin', 'gp_admin', 'fund_accountant',
                             'analyst', 'compliance_officer')

    @property
    def is_admin(self):
        return self.role in ('platform_admin', 'gp_admin')

    @property
    def is_locked(self):
        from django.utils import timezone
        if self.lockout_until and timezone.now() < self.lockout_until:
            return True
        if self.account_locked_until and timezone.now() < self.account_locked_until:
            return True
        return False

    def record_failed_login(self):
        """Increment attempt counter; lock after 3 attempts for 24h."""
        from django.utils import timezone
        from datetime import timedelta
        self.login_attempts += 1
        self.failed_login_count = self.login_attempts
        if self.login_attempts >= 3:
            self.lockout_until = timezone.now() + timedelta(hours=24)
            self.account_locked_until = self.lockout_until
        self.save(update_fields=['login_attempts', 'failed_login_count',
                                 'lockout_until', 'account_locked_until'])

    def reset_login_attempts(self):
        """Called on successful login."""
        self.login_attempts = 0
        self.failed_login_count = 0
        self.lockout_until = None
        self.account_locked_until = None
        self.save(update_fields=['login_attempts', 'failed_login_count',
                                 'lockout_until', 'account_locked_until'])


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

    # SHA-256 hash chain — tamper-proof audit trail (v5 requirement)
    prev_hash = models.CharField(
        max_length=64, blank=True, default='',
        help_text='SHA-256 hash of the previous AuditLog entry (genesis = empty string)',
    )
    record_hash = models.CharField(
        max_length=64, blank=True, default='',
        help_text='SHA-256(prev_hash + action + resource_type + resource_id + timestamp_iso)',
    )

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['organization', '-timestamp']),
            models.Index(fields=['user', '-timestamp']),
            models.Index(fields=['resource_type', 'resource_id']),
        ]

    def __str__(self):
        return f'{self.action} {self.resource_type} by {self.user} at {self.timestamp}'

    def compute_hash(self, prev_hash: str, timestamp_iso: str) -> str:
        """Compute SHA-256(prev_hash + action + resource_type + resource_id + timestamp_iso)."""
        payload = f'{prev_hash}{self.action}{self.resource_type}{self.resource_id}{timestamp_iso}'
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def save(self, *args, **kwargs):
        """On every new insert: fetch last record's hash, compute this record's hash."""
        if not self.pk:  # Only on creation — AuditLog is append-only
            last = AuditLog.objects.order_by('-timestamp').first()
            self.prev_hash = last.record_hash if last else ''
            # timestamp is auto_now_add — use current time for hash input
            from django.utils import timezone
            ts = timezone.now().isoformat()
            self.record_hash = self.compute_hash(self.prev_hash, ts)
        super().save(*args, **kwargs)
