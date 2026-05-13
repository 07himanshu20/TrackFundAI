from django.urls import path
from . import views

urlpatterns = [
    path('securities/', views.security_list, name='security-list'),
    path('securities/<uuid:security_id>/prices/', views.price_history, name='price-history'),
    path('fx-rates/', views.fx_rates, name='fx-rates'),
    path('fetch/', views.trigger_price_fetch, name='trigger-price-fetch'),
]
