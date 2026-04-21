from rest_framework import serializers
from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    priority_display = serializers.CharField(source='get_priority_display', read_only=True)

    class Meta:
        model = Notification
        fields = [
            'id', 'title', 'message',
            'category', 'category_display',
            'priority', 'priority_display',
            'resource_type', 'resource_id',
            'is_read', 'read_at',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']
