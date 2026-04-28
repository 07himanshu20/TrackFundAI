from rest_framework import serializers
from .models import FundCategory, Entity, Fund, Scheme


# ── Fund Category ────────────────────────────────────────────

class FundCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = FundCategory
        fields = [
            'id', 'sebi_category_code', 'name', 'sub_category',
            'leverage_permitted', 'description',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# ── Entity (organization-level) ─────────────────────────────

class EntitySerializer(serializers.ModelSerializer):
    entity_type_display = serializers.CharField(
        source='get_entity_type_display', read_only=True,
    )

    class Meta:
        model = Entity
        fields = [
            'id', 'organization', 'entity_type', 'entity_type_display',
            'entity_name', 'pan', 'gstin', 'sebi_registration',
            'contact_person', 'email', 'phone',
            'address', 'city', 'state', 'country',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


class EntityListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for dropdowns and FK selection."""
    entity_type_display = serializers.CharField(
        source='get_entity_type_display', read_only=True,
    )

    class Meta:
        model = Entity
        fields = ['id', 'entity_type', 'entity_type_display', 'entity_name', 'sebi_registration']


# ── Scheme ──────────────────────────────────────────────────

class SchemeSerializer(serializers.ModelSerializer):
    carry_type_display = serializers.CharField(
        source='get_carry_type_display', read_only=True,
    )
    fee_basis_display = serializers.CharField(
        source='get_management_fee_basis_display', read_only=True,
    )
    scheme_status_display = serializers.CharField(
        source='get_scheme_status_display', read_only=True,
    )

    class Meta:
        model = Scheme
        fields = [
            'id', 'fund', 'name', 'vintage_year',
            'first_close_date', 'final_close_date', 'dissolution_date',
            'scheme_size', 'tenure_years',
            'hurdle_rate_pct', 'carry_pct', 'carry_type', 'carry_type_display',
            'management_fee_basis', 'fee_basis_display', 'management_fee_pct',
            'sponsor_commitment_pct',
            'scheme_status', 'scheme_status_display',
            'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'fund', 'created_at', 'updated_at']


# ── Fund ────────────────────────────────────────────────────

class FundListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for fund list views."""
    category_name = serializers.CharField(
        source='fund_category.name', read_only=True, default=None,
    )
    category_code = serializers.CharField(
        source='fund_category.sebi_category_code', read_only=True, default=None,
    )
    sub_category = serializers.CharField(
        source='fund_category.sub_category', read_only=True, default=None,
    )
    structure_display = serializers.CharField(
        source='get_structure_type_display', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_fund_status_display', read_only=True,
    )
    scheme_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Fund
        fields = [
            'id', 'name', 'sebi_registration_number',
            'fund_category', 'category_name', 'category_code', 'sub_category',
            'structure_type', 'structure_display',
            'inception_date', 'corpus_target', 'base_currency',
            'pan', 'gstin', 'is_gift_city',
            'fund_status', 'status_display',
            'scheme_count', 'created_at',
        ]


class FundDetailSerializer(serializers.ModelSerializer):
    """Full serializer with nested schemes and entities."""
    category_name = serializers.CharField(
        source='fund_category.name', read_only=True, default=None,
    )
    category_code = serializers.CharField(
        source='fund_category.sebi_category_code', read_only=True, default=None,
    )
    fund_category_detail = FundCategorySerializer(
        source='fund_category', read_only=True,
    )
    structure_display = serializers.CharField(
        source='get_structure_type_display', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_fund_status_display', read_only=True,
    )

    # Linked entity details
    manager_entity_detail = EntityListSerializer(
        source='manager_entity', read_only=True,
    )
    trustee_entity_detail = EntityListSerializer(
        source='trustee_entity', read_only=True,
    )
    sponsor_entity_detail = EntityListSerializer(
        source='sponsor_entity', read_only=True,
    )
    custodian_entity_detail = EntityListSerializer(
        source='custodian_entity', read_only=True,
    )
    auditor_entity_detail = EntityListSerializer(
        source='auditor_entity', read_only=True,
    )

    schemes = SchemeSerializer(many=True, read_only=True)

    class Meta:
        model = Fund
        fields = [
            'id', 'name', 'sebi_registration_number',
            'fund_category', 'category_name', 'category_code',
            'fund_category_detail',
            'structure_type', 'structure_display',
            'manager_entity', 'manager_entity_detail',
            'trustee_entity', 'trustee_entity_detail',
            'sponsor_entity', 'sponsor_entity_detail',
            'custodian_entity', 'custodian_entity_detail',
            'auditor_entity', 'auditor_entity_detail',
            'pan', 'gstin',
            'inception_date', 'corpus_target', 'base_currency',
            'is_gift_city', 'fund_status', 'status_display',
            'description', 'schemes',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class FundCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating funds."""
    class Meta:
        model = Fund
        fields = [
            'name', 'sebi_registration_number', 'fund_category',
            'structure_type', 'inception_date', 'corpus_target',
            'base_currency', 'is_gift_city', 'fund_status', 'description',
            'pan', 'gstin',
            'manager_entity', 'trustee_entity', 'sponsor_entity',
            'custodian_entity', 'auditor_entity',
        ]
