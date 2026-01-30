from django.urls import path
from . import views

urlpatterns = [
    path('testnet/', views.testnet_home, name='testnet_home'),
    path('testnet/swap/', views.swap, name='swap'),
    path('testnet/liquidity/', views.liquidity, name='liquidity'),
    path('testnet/transactions/', views.transactions, name='transactions'),
    path('testnet/claim-fees/', views.claim_fees, name='claim_fees'),
    path('p2p/create/', views.create_swap_offer, name='create_swap_offer'),
    path('p2p/create/<int:listing_id>/', views.create_swap_offer, name='create_swap_offer_for_listing'),
    path('p2p/accept/<int:offer_id>/', views.accept_swap_offer, name='accept_swap_offer'),
    path('p2p/cancel/<int:offer_id>/', views.cancel_swap_offer, name='cancel_swap_offer'),
    path('p2p/my-offers/', views.my_swap_offers, name='my_swap_offers'),
    path('p2p/available/', views.available_swap_offers, name='available_swap_offers'),
    path('p2p/history/', views.my_swap_history, name='my_swap_history'),
    path('oracle/price-feeds/', views.price_feeds, name='price_feeds'),
    path('oracle/submit-price/', views.submit_price, name='submit_price'),
    path('oracle/price-history/<str:token_symbol>/', views.price_history, name='price_history'),
    path('oracle/manage/', views.manage_oracle, name='manage_oracle'),
    path('lending/', views.lending_home, name='lending_home'),
    path('lending/deposit/', views.deposit_funds, name='deposit_funds'),
    path('lending/borrow/', views.borrow_funds, name='borrow_funds'),
    path('lending/repay/', views.repay_loan, name='repay_loan'),
    path('lending/withdraw/', views.withdraw_deposit, name='withdraw_deposit'),
    path('lending/manage/', views.manage_positions, name='manage_positions'),
    # Fixed/Variable Rate Instruments
    path('rates/', views.rates_marketplace, name='rates_marketplace'),
    path('rates/fixed-bond/purchase/', views.purchase_fixed_bond, name='purchase_fixed_bond'),
    path('rates/variable-savings/open/', views.open_variable_savings, name='open_variable_savings'),
    path('rates/bond/redeem/<int:bond_id>/', views.redeem_bond, name='redeem_bond'),
    path('rates/savings/withdraw/<int:savings_id>/', views.withdraw_variable_savings, name='withdraw_variable_savings'),
]
