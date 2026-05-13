from django.urls import path
from . import views

urlpatterns = [
    path('pipeline/', views.deal_pipeline_list, name='ic-pipeline-list'),
    path('pipeline/summary/', views.pipeline_summary, name='ic-pipeline-summary'),
    path('pipeline/seed-from-investments/', views.seed_from_investments, name='ic-seed-investments'),
    path('pipeline/<uuid:pk>/', views.deal_pipeline_detail, name='ic-pipeline-detail'),
    path('pipeline/<uuid:deal_pk>/presentations/', views.ic_presentation_list, name='ic-presentation-list'),
    path('presentations/<uuid:presentation_pk>/vote/', views.cast_vote, name='ic-vote'),
    path('presentations/<uuid:presentation_pk>/decision/', views.record_decision, name='ic-decision'),
]
