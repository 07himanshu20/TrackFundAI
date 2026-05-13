from django.urls import path
from . import views

urlpatterns = [
    path('upload/', views.upload_fund_files, name='dataimport-upload'),
    path('uploaded-files/', views.uploaded_files_list, name='dataimport-uploaded-files'),
    path('stuck-imports/', views.stuck_imports_list, name='dataimport-stuck-imports'),
    path('files/<uuid:file_id>/', views.delete_imported_file, name='dataimport-delete-file'),
    path('jobs/', views.job_list, name='dataimport-jobs'),
    path('jobs/<uuid:job_id>/', views.job_detail, name='dataimport-detail'),
    path('jobs/<uuid:job_id>/stream/', views.import_stream, name='dataimport-stream'),
    path('jobs/<uuid:job_id>/status/', views.job_status, name='dataimport-status'),
]
