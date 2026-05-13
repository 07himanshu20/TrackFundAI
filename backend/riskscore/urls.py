from django.urls import path
from . import views

urlpatterns = [
    path('', views.risk_score_list, name='risk-score-list'),
    path('summary/', views.fund_risk_summary, name='fund-risk-summary'),
    path('compute/<uuid:company_id>/', views.compute_score, name='compute-risk-score'),
    path('compute-all/', views.compute_all_scores, name='compute-all-scores'),
]
