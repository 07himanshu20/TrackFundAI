from django.urls import path
from . import views

urlpatterns = [
    # Investors
    path('investors/', views.investor_list, name='investor-list'),
    path('investors/<uuid:investor_id>/', views.investor_detail, name='investor-detail'),
    path('investors/<uuid:investor_id>/verify-kyc/', views.verify_kyc, name='investor-verify-kyc'),
    path('investors/<uuid:investor_id>/verify-bank/', views.verify_bank, name='investor-verify-bank'),

    # Bank Accounts
    path('bank-accounts/', views.bank_account_list, name='bank-account-list'),
    path('bank-accounts/<uuid:account_id>/', views.bank_account_detail, name='bank-account-detail'),

    # Commitments
    path('commitments/', views.commitment_list, name='commitment-list'),
    path('commitments/<uuid:commitment_id>/', views.commitment_detail, name='commitment-detail'),

    # Capital Calls
    path('capital-calls/', views.capital_call_list, name='capital-call-list'),
    path('capital-calls/<uuid:call_id>/', views.capital_call_detail, name='capital-call-detail'),
    path('capital-calls/<uuid:call_id>/line-items/', views.capital_call_line_item_list, name='capital-call-line-items'),
    path('capital-calls/<uuid:call_id>/send-notices/', views.send_call_notices, name='capital-call-send-notices'),
    path('capital-calls/<uuid:call_id>/match-utr/', views.match_utr, name='capital-call-match-utr'),

    # Distributions
    path('distributions/', views.distribution_list, name='distribution-list'),
    path('distributions/<uuid:distribution_id>/', views.distribution_detail, name='distribution-detail'),
    path('distributions/<uuid:distribution_id>/line-items/', views.distribution_line_item_list, name='distribution-line-items'),
    path('distributions/<uuid:distribution_id>/process/', views.process_distribution, name='distribution-process'),

    # LP Capital Accounts
    path('capital-accounts/', views.lp_capital_account_list, name='lp-capital-account-list'),
    path('capital-accounts/<uuid:account_id>/', views.lp_capital_account_detail, name='lp-capital-account-detail'),

    # Unit Allotment
    path('schemes/<uuid:scheme_id>/allot-units/', views.allot_units, name='allot-units'),

    # LP Portal Dashboard
    path('dashboard/', views.lp_dashboard, name='lp-dashboard'),

    # Waterfall Simulator
    path('schemes/<uuid:scheme_id>/waterfall/simulate/', views.waterfall_simulate, name='waterfall-simulate'),
]
