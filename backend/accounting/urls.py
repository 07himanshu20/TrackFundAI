from django.urls import path
from . import views

urlpatterns = [
    # Chart of Accounts
    path('chart-of-accounts/', views.chart_of_accounts_list, name='coa-list'),
    path('chart-of-accounts/<uuid:account_id>/', views.chart_of_accounts_detail, name='coa-detail'),

    # NAV Records
    path('nav/', views.nav_record_list, name='nav-list'),
    path('nav/<uuid:nav_id>/', views.nav_record_detail, name='nav-detail'),

    # Carried Interest
    path('carry/', views.carried_interest_list, name='carry-list'),
    path('carry/<uuid:carry_id>/', views.carried_interest_detail, name='carry-detail'),

    # Fund Ledger (journal entries)
    path('ledger/', views.fund_ledger_list, name='ledger-list'),
    path('ledger/<uuid:entry_id>/', views.fund_ledger_detail, name='ledger-detail'),

    # Management Fee Schedule
    path('fees/', views.management_fee_list, name='fee-list'),
    path('fees/<uuid:fee_id>/', views.management_fee_detail, name='fee-detail'),
]
