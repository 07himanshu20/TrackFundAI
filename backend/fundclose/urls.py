from django.urls import path
from . import views

urlpatterns = [
    path('', views.fund_close_list, name='fundclose-list'),
    path('<uuid:pk>/', views.fund_close_detail, name='fundclose-detail'),
    path('<uuid:close_event_pk>/clawback/', views.compute_clawback, name='fundclose-clawback'),
    path('<uuid:close_event_pk>/sebi-deregistration/', views.sebi_deregistration, name='fundclose-sebi-dereg'),
]
