from django.urls import path
from . import views

urlpatterns = [
    # Fund Categories (SEBI master data)
    path('categories/', views.fund_category_list, name='fund-category-list'),
    path('categories/<uuid:category_id>/', views.fund_category_detail, name='fund-category-detail'),

    # Entities (organization-level — shared across funds)
    path('entities/', views.entity_list, name='entity-list'),
    path('entities/<uuid:entity_id>/', views.entity_detail, name='entity-detail'),

    # Funds
    path('', views.fund_list, name='fund-list'),
    path('<uuid:fund_id>/', views.fund_detail, name='fund-detail'),

    # Schemes (nested under fund)
    path('<uuid:fund_id>/schemes/', views.scheme_list, name='scheme-list'),
    path('schemes/<uuid:scheme_id>/', views.scheme_detail, name='scheme-detail'),
]
