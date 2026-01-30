from django.urls import path
from . import views

urlpatterns = [
    path('testnet/', views.testnet_home, name='testnet_home'),
    path('testnet/swap/', views.swap, name='swap'),
    path('testnet/liquidity/', views.liquidity, name='liquidity'),
    path('testnet/transactions/', views.transactions, name='transactions'),
    path('p2p/create/', views.create_swap_offer, name='create_swap_offer'),
    path('p2p/create/<int:listing_id>/', views.create_swap_offer, name='create_swap_offer_for_listing'),
    path('p2p/accept/<int:offer_id>/', views.accept_swap_offer, name='accept_swap_offer'),
    path('p2p/cancel/<int:offer_id>/', views.cancel_swap_offer, name='cancel_swap_offer'),
    path('p2p/my-offers/', views.my_swap_offers, name='my_swap_offers'),
    path('p2p/available/', views.available_swap_offers, name='available_swap_offers'),
    path('p2p/history/', views.my_swap_history, name='my_swap_history'),
]
