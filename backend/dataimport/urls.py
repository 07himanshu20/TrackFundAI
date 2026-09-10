from django.urls import path
from . import views
from . import preingest_views as pv
from . import preingest2_views as pv2
from . import preingest3_views as pv3

urlpatterns = [
    path('upload/', views.upload_fund_files, name='dataimport-upload'),

    # preingest3 — CIR extraction engine + review-gate write-back loop
    path('preingest3/', pv3.upload, name='preingest3-upload'),
    path('preingest3/list/', pv3.job_list, name='preingest3-list'),
    path('preingest3/files/<uuid:file_id>/', pv3.delete_file, name='preingest3-delete-file'),
    path('preingest3/<uuid:job_id>/', pv3.job_detail, name='preingest3-detail'),
    path('preingest3/<uuid:job_id>/run/', pv3.rerun, name='preingest3-run'),
    path('preingest3/<uuid:job_id>/resolve/', pv3.resolve, name='preingest3-resolve'),
    path('preingest3/<uuid:job_id>/ratecard/', pv3.ratecard, name='preingest3-ratecard'),
    path('preingest3/<uuid:job_id>/base-currency/', pv3.base_currency, name='preingest3-base-currency'),
    path('preingest3/<uuid:job_id>/download/', pv3.download, name='preingest3-download'),

    # Pre-ingestion consolidation layer (many raw files -> one TFAI.xlsx)
    path('preingest/', pv.preingest_upload, name='preingest-upload'),
    path('preingest/list/', pv.preingest_list, name='preingest-list'),
    path('preingest/<uuid:job_id>/', pv.preingest_detail, name='preingest-detail'),
    path('preingest/<uuid:job_id>/download/', pv.preingest_download, name='preingest-download'),

    # preingest2 — document-faithful pipeline (Stages 0-8, testing)
    path('preingest2/', pv2.preingest2_upload, name='preingest2-upload'),
    path('preingest2/<uuid:job_id>/', pv2.preingest2_detail, name='preingest2-detail'),
    path('preingest2/<uuid:job_id>/download/', pv2.preingest2_download, name='preingest2-download'),
    path('uploaded-files/', views.uploaded_files_list, name='dataimport-uploaded-files'),
    path('stuck-imports/', views.stuck_imports_list, name='dataimport-stuck-imports'),
    path('files/<uuid:file_id>/', views.delete_imported_file, name='dataimport-delete-file'),
    path('jobs/', views.job_list, name='dataimport-jobs'),
    path('jobs/<uuid:job_id>/', views.job_detail, name='dataimport-detail'),
    path('jobs/<uuid:job_id>/stream/', views.import_stream, name='dataimport-stream'),
    path('jobs/<uuid:job_id>/status/', views.job_status, name='dataimport-status'),
    path('derived-metrics/', views.derived_metrics_list, name='dataimport-derived-metrics'),
]
