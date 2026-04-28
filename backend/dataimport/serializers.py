from rest_framework import serializers
from .models import ImportJob, ImportFile


class ImportFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportFile
        fields = [
            'id', 'original_filename', 'file_size', 'status',
            'gemini_confidence', 'sheet_names', 'error_detail',
            'created_at', 'completed_at',
        ]


class ImportJobSerializer(serializers.ModelSerializer):
    files = ImportFileSerializer(many=True, read_only=True)
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ImportJob
        fields = [
            'id', 'status', 'progress_pct', 'progress_message',
            'total_files', 'completed_files', 'error_log',
            'result_summary', 'created_at', 'completed_at',
            'files', 'uploaded_by_name',
        ]

    def get_uploaded_by_name(self, obj):
        if obj.uploaded_by:
            return obj.uploaded_by.first_name or obj.uploaded_by.username
        return None


class ImportJobStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportJob
        fields = [
            'id', 'status', 'progress_pct', 'progress_message',
            'completed_files', 'total_files', 'result_summary',
            'error_log',
        ]
