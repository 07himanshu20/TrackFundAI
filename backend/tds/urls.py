from django.urls import path
from . import views

urlpatterns = [
    path('withholding/', views.tds_withholding_list, name='tds-list'),
    path('withholding/<uuid:pk>/', views.tds_withholding_detail, name='tds-detail'),
    path('26q/', views.form26q_list, name='form26q-list'),
    path('26q/<uuid:pk>/compute/', views.compute_form26q, name='form26q-compute'),
    path('26q/<uuid:pk>/file/', views.file_form26q, name='form26q-file'),
]
