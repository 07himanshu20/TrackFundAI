from rest_framework import serializers
from .models import User, Organization, FundAccess, SchemeAccess, AuditLog


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ['id', 'name', 'slug', 'subscription_tier', 'is_active']
        read_only_fields = ['id']


class UserSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(
        source='organization.name', read_only=True, default=None,
    )

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name',
            'role', 'phone', 'organization', 'organization_name',
            'mfa_enabled', 'is_active', 'date_joined',
        ]
        read_only_fields = ['id', 'date_joined']


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)


class FundAccessSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user.username', read_only=True)
    fund_name = serializers.CharField(source='fund.name', read_only=True)
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = FundAccess
        fields = [
            'id', 'user', 'user_name', 'fund', 'fund_name',
            'access_level', 'granted_at', 'expires_at', 'revoked_at',
            'is_active',
        ]
        read_only_fields = ['id', 'granted_at']


class SchemeAccessSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user.username', read_only=True)
    scheme_name = serializers.CharField(source='scheme.name', read_only=True)
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = SchemeAccess
        fields = [
            'id', 'user', 'user_name', 'scheme', 'scheme_name',
            'access_level', 'granted_at', 'expires_at', 'revoked_at',
            'is_active',
        ]
        read_only_fields = ['id', 'granted_at']


class AuditLogSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    action_display = serializers.CharField(source='get_action_display', read_only=True)
    module = serializers.CharField(source='resource_type', read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            'id', 'user', 'user_name', 'action', 'action_display',
            'resource_type', 'module', 'resource_id',
            'old_values', 'new_values', 'details',
            'ip_address', 'timestamp',
            'record_hash', 'prev_hash',
        ]

    def get_user_name(self, obj):
        if obj.user:
            name = f'{obj.user.first_name} {obj.user.last_name}'.strip()
            return name or obj.user.username
        return None
