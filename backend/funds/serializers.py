from rest_framework import serializers
from .models import Fund, Scheme, Entity


class EntitySerializer(serializers.ModelSerializer):
    role_display = serializers.CharField(source='get_role_display', read_only=True)

    class Meta:
        model = Entity
        fields = [
            'id', 'fund', 'role', 'role_display', 'name',
            'contact_person', 'email', 'phone',
            'sebi_registration', 'address',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'fund', 'created_at', 'updated_at']


class SchemeSerializer(serializers.ModelSerializer):
    carry_type_display = serializers.CharField(
        source='get_carry_type_display', read_only=True,
    )
    fee_basis_display = serializers.CharField(
        source='get_management_fee_basis_display', read_only=True,
    )

    class Meta:
        model = Scheme
        fields = [
            'id', 'fund', 'name', 'vintage_year',
            'first_close_date', 'final_close_date', 'scheme_size',
            'hurdle_rate_pct', 'carry_pct', 'carry_type', 'carry_type_display',
            'management_fee_basis', 'fee_basis_display', 'management_fee_pct',
            'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'fund', 'created_at', 'updated_at']


class FundListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for fund list views."""
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    structure_display = serializers.CharField(source='get_structure_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    scheme_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Fund
        fields = [
            'id', 'name', 'sebi_registration_number',
            'category', 'category_display',
            'structure_type', 'structure_display',
            'inception_date', 'corpus_target', 'base_currency',
            'is_gift_city', 'status', 'status_display',
            'scheme_count', 'created_at',
        ]


class FundDetailSerializer(serializers.ModelSerializer):
    """Full serializer with nested schemes and entities."""
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    structure_display = serializers.CharField(source='get_structure_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    schemes = SchemeSerializer(many=True, read_only=True)
    entities = EntitySerializer(many=True, read_only=True)

    class Meta:
        model = Fund
        fields = [
            'id', 'name', 'sebi_registration_number',
            'category', 'category_display',
            'structure_type', 'structure_display',
            'inception_date', 'corpus_target', 'base_currency',
            'is_gift_city', 'status', 'status_display',
            'description', 'schemes', 'entities',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class FundCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating funds."""
    class Meta:
        model = Fund
        fields = [
            'name', 'sebi_registration_number', 'category',
            'structure_type', 'inception_date', 'corpus_target',
            'base_currency', 'is_gift_city', 'status', 'description',
        ]
