import uuid
from django.conf import settings
from django.db import models


class FundCategory(models.Model):
    """
    SEBI AIF category master data.
    Maps to FundOS: fund_categories table.
    Stores SEBI category codes, sub-categories, and regulatory flags.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sebi_category_code = models.CharField(
        max_length=20, unique=True,
        help_text='SEBI category code — e.g., CAT_I_VCF, CAT_II, CAT_III_LVF',
    )
    name = models.CharField(
        max_length=100,
        help_text='Category I AIF / Category II AIF / Category III AIF',
    )
    sub_category = models.CharField(
        max_length=100, blank=True,
        help_text='Venture Capital Fund, Angel Fund, PE Fund, Hedge Fund, etc.',
    )
    leverage_permitted = models.BooleanField(
        default=False,
        help_text='Category III only — TRUE for hedge funds / leveraged strategies',
    )
    description = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sebi_category_code']
        verbose_name_plural = 'fund categories'

    def __str__(self):
        if self.sub_category:
            return f'{self.name} — {self.sub_category} ({self.sebi_category_code})'
        return f'{self.name} ({self.sebi_category_code})'


class Entity(models.Model):
    """
    Key entities in the AIF ecosystem — organization-level (shared across funds).
    Maps to FundOS: entities table.

    An entity can serve multiple roles across multiple funds (e.g., one trustee
    company acts as trustee for many funds). The fund-entity linkage is done
    via FK fields on the Fund model (manager_entity, trustee_entity, etc.).
    """
    ENTITY_TYPE_CHOICES = [
        ('manager', 'Investment Manager'),
        ('trustee', 'Trustee'),
        ('sponsor', 'Sponsor'),
        ('custodian', 'Custodian'),
        ('statutory_auditor', 'Statutory Auditor'),
        ('legal_counsel', 'Legal Counsel'),
        ('registrar', 'Registrar & Transfer Agent'),
        ('valuer', 'Registered Valuer'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='entities',
        help_text='Owning organization — entities are shared across funds within an org',
    )
    entity_type = models.CharField(max_length=20, choices=ENTITY_TYPE_CHOICES)
    entity_name = models.CharField(max_length=255, help_text='Legal name of the entity')

    # India regulatory identifiers
    pan = models.CharField(
        max_length=10, blank=True,
        help_text='PAN — mandatory for Indian entities',
    )
    gstin = models.CharField(
        max_length=15, blank=True,
        help_text='GSTIN (Goods and Services Tax Identification Number)',
    )
    sebi_registration = models.CharField(
        max_length=50, blank=True,
        help_text='SEBI registration number (custodian, manager, etc.)',
    )

    # Contact information
    contact_person = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)

    # Address
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, default='India')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['entity_type', 'entity_name']
        unique_together = ('organization', 'entity_type', 'entity_name')
        verbose_name_plural = 'entities'

    def __str__(self):
        return f'{self.get_entity_type_display()}: {self.entity_name}'


class Fund(models.Model):
    """
    AIF fund master record with SEBI registration details.
    Maps to FundOS: funds table.

    Now references FundCategory via FK (instead of simple CharField)
    and links to Entity records for manager, trustee, custodian, etc.
    """
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

    # SEBI registration
    sebi_registration_number = models.CharField(
        max_length=50, blank=True,
        help_text='SEBI AIF registration number — unique per fund',
    )
    fund_category = models.ForeignKey(
        FundCategory,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='funds',
        help_text='SEBI AIF category (Category I/II/III + sub-category)',
    )

    # Structure
    structure_type = models.CharField(max_length=10, choices=STRUCTURE_CHOICES, default='trust')

    # Entity linkages — shared entities across funds
    manager_entity = models.ForeignKey(
        Entity, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='managed_funds',
        help_text='Investment Manager entity',
    )
    trustee_entity = models.ForeignKey(
        Entity, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='trustee_funds',
        help_text='Trustee entity',
    )
    sponsor_entity = models.ForeignKey(
        Entity, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='sponsored_funds',
        help_text='Sponsor entity',
    )
    custodian_entity = models.ForeignKey(
        Entity, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='custodian_funds',
        help_text='Custodian entity',
    )
    auditor_entity = models.ForeignKey(
        Entity, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='audited_funds',
        help_text='Statutory Auditor entity',
    )

    # India regulatory identifiers (fund-level)
    pan = models.CharField(
        max_length=10, blank=True,
        help_text='PAN of the fund (trust/company/LLP)',
    )
    gstin = models.CharField(
        max_length=15, blank=True,
        help_text='GSTIN of the fund',
    )

    # Fund details
    inception_date = models.DateField(null=True, blank=True)
    corpus_target = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Target corpus in base currency',
    )
    base_currency = models.CharField(max_length=3, default='INR')
    is_gift_city = models.BooleanField(
        default=False,
        help_text='GIFT City IFSC offshore AIF flag',
    )
    fund_status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='active')
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
        cat = self.fund_category.sebi_category_code if self.fund_category else '—'
        return f'{self.name} ({cat})'


class Scheme(models.Model):
    """
    Scheme under a fund (e.g., Scheme I, Scheme II).
    Maps to FundOS: fund_schemes table.

    Added: scheme_status lifecycle, tenure_years, dissolution_date,
    sponsor_commitment_pct to match FundOS schema.
    """
    CARRY_TYPE_CHOICES = [
        ('european', 'European (Whole Fund)'),
        ('american', 'American (Deal-by-Deal)'),
    ]
    FEE_BASIS_CHOICES = [
        ('committed', 'Committed Capital'),
        ('called', 'Called Capital'),
        ('nav', 'NAV'),
    ]
    SCHEME_STATUS_CHOICES = [
        ('fundraising', 'Fundraising'),
        ('investing', 'Investing'),
        ('harvesting', 'Harvesting'),
        ('dissolved', 'Dissolved'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fund = models.ForeignKey(Fund, on_delete=models.CASCADE, related_name='schemes')
    name = models.CharField(max_length=255)
    vintage_year = models.PositiveIntegerField(null=True, blank=True)
    first_close_date = models.DateField(null=True, blank=True)
    final_close_date = models.DateField(null=True, blank=True)
    dissolution_date = models.DateField(
        null=True, blank=True,
        help_text='Actual or expected dissolution date',
    )
    scheme_size = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Target scheme size in fund base currency',
    )
    tenure_years = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Scheme tenure in years (e.g., 10)',
    )

    # Carry / waterfall config
    hurdle_rate_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Hurdle rate percentage (e.g., 8.00 = 8% preferred return)',
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

    # Sponsor commitment
    sponsor_commitment_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Sponsor commitment as % of scheme size (SEBI min varies by category)',
    )

    # Lifecycle
    scheme_status = models.CharField(
        max_length=15, choices=SCHEME_STATUS_CHOICES, default='fundraising',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['fund', 'name']
        unique_together = ('fund', 'name')

    def __str__(self):
        return f'{self.fund.name} — {self.name}'
