from django.contrib import admin
from .models import ImportJob, ImportFile


class ImportFileInline(admin.TabularInline):
    model = ImportFile
    extra = 0
    readonly_fields = ('original_filename', 'file_size', 'status', 'gemini_confidence')


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    list_display = ('id', 'organization', 'uploaded_by', 'status', 'progress_pct',
                    'total_files', 'completed_files', 'created_at')
    list_filter = ('status',)
    inlines = [ImportFileInline]


@admin.register(ImportFile)
class ImportFileAdmin(admin.ModelAdmin):
    list_display = ('original_filename', 'job', 'status', 'gemini_confidence', 'created_at')
    list_filter = ('status',)
