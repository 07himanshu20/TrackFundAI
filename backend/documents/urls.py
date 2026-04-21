from django.urls import path
from . import views

urlpatterns = [
    path('', views.document_list, name='document-list'),
    path('upload/', views.document_upload, name='document-upload'),
    path('<uuid:doc_id>/', views.document_detail, name='document-detail'),
    path('<uuid:doc_id>/download/', views.document_download, name='document-download'),
    path('<uuid:doc_id>/delete/', views.document_delete, name='document-delete'),
    path('<uuid:doc_id>/access-log/', views.document_access_log, name='document-access-log'),
]
