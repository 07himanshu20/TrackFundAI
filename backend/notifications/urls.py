from django.urls import path
from . import views

urlpatterns = [
    path('', views.notification_list, name='notification-list'),
    path('<uuid:notif_id>/read/', views.notification_mark_read, name='notification-mark-read'),
    path('mark-all-read/', views.notification_mark_all_read, name='notification-mark-all-read'),
    path('unread-count/', views.notification_unread_count, name='notification-unread-count'),
]
