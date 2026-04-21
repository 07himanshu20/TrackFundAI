from rest_framework import serializers
from .models import Document, DocumentAccessLog


class DocumentSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    visibility_display = serializers.CharField(source='get_visibility_display', read_only=True)
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            'id', 'fund', 'scheme', 'title', 'description',
            'category', 'category_display',
            'visibility', 'visibility_display',
            'file', 'file_name', 'file_size', 'mime_type',
            'version', 'tags',
            'uploaded_by', 'uploaded_by_name',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'file_name', 'file_size', 'mime_type',
            'uploaded_by', 'created_at', 'updated_at',
        ]

    def get_uploaded_by_name(self, obj):
        if obj.uploaded_by:
            u = obj.uploaded_by
            name = f'{u.first_name} {u.last_name}'.strip()
            return name or u.username
        return None


class DocumentUploadSerializer(serializers.Serializer):
    """Handles multipart file upload with metadata."""
    file = serializers.FileField()
    title = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    category = serializers.ChoiceField(choices=Document.CATEGORY_CHOICES, default='other')
    visibility = serializers.ChoiceField(choices=Document.VISIBILITY_CHOICES, default='internal')
    fund_id = serializers.UUIDField(required=False, allow_null=True)
    scheme_id = serializers.UUIDField(required=False, allow_null=True)
    tags = serializers.JSONField(required=False, default=list)


class DocumentAccessLogSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = DocumentAccessLog
        fields = ['id', 'user', 'user_name', 'action', 'ip_address', 'timestamp']

    def get_user_name(self, obj):
        if obj.user:
            name = f'{obj.user.first_name} {obj.user.last_name}'.strip()
            return name or obj.user.username
        return None
