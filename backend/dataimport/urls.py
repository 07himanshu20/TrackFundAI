from django.urls import path
from . import views

urlpatterns = [
    path('upload/', views.upload_fund_files, name='dataimport-upload'),
    path('jobs/', views.job_list, name='dataimport-jobs'),
    path('jobs/<uuid:job_id>/', views.job_detail, name='dataimport-detail'),
    path('jobs/<uuid:job_id>/stream/', views.import_stream, name='dataimport-stream'),
    path('jobs/<uuid:job_id>/status/', views.job_status, name='dataimport-status'),
]
