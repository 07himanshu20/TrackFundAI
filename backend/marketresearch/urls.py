from django.urls import path
from . import views

urlpatterns = [
    path('opportunities/', views.opportunity_list, name='market-opportunity-list'),
    path('opportunities/filters/', views.filter_options, name='market-filter-options'),
    path('opportunities/<uuid:pk>/', views.opportunity_detail, name='market-opportunity-detail'),
    path('opportunities/<uuid:opportunity_pk>/generate-study/', views.generate_study, name='market-generate-study'),
    path('studies/', views.my_studies, name='market-my-studies'),
    path('studies/<uuid:study_pk>/', views.study_detail, name='market-study-detail'),
    path('studies/<uuid:study_pk>/pdf/', views.download_study_pdf, name='market-study-pdf'),
]
