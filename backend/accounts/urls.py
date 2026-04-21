from django.urls import path
from . import views

urlpatterns = [
    path('login/', views.login_view, name='auth-login'),
    path('logout/', views.logout_view, name='auth-logout'),
    path('refresh/', views.token_refresh_view, name='auth-refresh'),
    path('me/', views.me_view, name='auth-me'),
    path('change-password/', views.change_password_view, name='auth-change-password'),
    path('audit-log/', views.audit_log_list, name='audit-log-list'),
]
