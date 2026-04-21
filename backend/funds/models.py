import uuid
from django.conf import settings
from django.db import models


class Fund(models.Model):
    """AIF fund master record with SEBI registration details."""

    CATEGORY_CHOICES = [
        ('cat_1', 'Category I'),
        ('cat_2', 'Category II'),
        ('cat_3', 'Category III'),
    ]
    STRUCTURE_CHOICES = [
        ('trust', 'Trust'),
        ('company', 'Company'),
        ('llp', 'LLP'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('closed', 'Closed'),
        ('winding_up', 'Winding Up'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='funds',
    )
    name = models.CharField(max_length=255)
    sebi_registration_number = models.CharField(max_length=50, blank=True)
    category = models.CharField(max_length=10, choices=CATEGORY_CHOICES, default='cat_2')
    structure_type = models.CharField(max_length=10, choices=STRUCTURE_CHOICES, default='trust')
    inception_date = models.DateField(null=True, blank=True)
    corpus_target = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Target corpus in base currency',
    )
    base_currency = models.CharField(max_length=3, default='INR')
    is_gift_city = models.BooleanField(
        default=False,
        help_text='GIFT City IFSC offshore AIF',
    )
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='active')
    description = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = ('organization', 'name')

    def __str__(self):
        return f'{self.name} ({self.get_category_display()})'


class Scheme(models.Model):
    """Scheme under a fund (e.g., Scheme I, Scheme II)."""

    CARRY_TYPE_CHOICES = [
        ('european', 'European (Whole Fund)'),
        ('american', 'American (Deal-by-Deal)'),
    ]
    FEE_BASIS_CHOICES = [
        ('committed', 'Committed Capital'),
        ('called', 'Called Capital'),
        ('nav', 'NAV'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fund = models.ForeignKey(Fund, on_delete=models.CASCADE, related_name='schemes')
    name = models.CharField(max_length=255)
    vintage_year = models.PositiveIntegerField(null=True, blank=True)
    first_close_date = models.DateField(null=True, blank=True)
    final_close_date = models.DateField(null=True, blank=True)
    scheme_size = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Scheme size in fund base currency',
    )

    # Carry / waterfall config
    hurdle_rate_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Hurdle rate percentage (e.g., 8.00)',
    )
    carry_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Carried interest percentage (e.g., 20.00)',
    )
    carry_type = models.CharField(
        max_length=10, choices=CARRY_TYPE_CHOICES, default='european',
    )

    # Management fee config
    management_fee_basis = models.CharField(
        max_length=10, choices=FEE_BASIS_CHOICES, default='committed',
    )
    management_fee_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Annual management fee percentage (e.g., 2.00)',
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['fund', 'name']
        unique_together = ('fund', 'name')

    def __str__(self):
        return f'{self.fund.name} — {self.name}'


class Entity(models.Model):
    """Key entities associated with a fund (manager, trustee, custodian, etc.)."""

    ROLE_CHOICES = [
        ('manager', 'Investment Manager'),
        ('trustee', 'Trustee'),
        ('sponsor', 'Sponsor'),
        ('custodian', 'Custodian'),
        ('statutory_auditor', 'Statutory Auditor'),
        ('legal_counsel', 'Legal Counsel'),
        ('registrar', 'Registrar & Transfer Agent'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fund = models.ForeignKey(Fund, on_delete=models.CASCADE, related_name='entities')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    name = models.CharField(max_length=255)
    contact_person = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    sebi_registration = models.CharField(
        max_length=50, blank=True,
        help_text='SEBI registration number (if applicable)',
    )
    address = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['fund', 'role']
        unique_together = ('fund', 'role', 'name')
        verbose_name_plural = 'entities'

    def __str__(self):
        return f'{self.get_role_display()}: {self.name}'
