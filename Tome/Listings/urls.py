from django.urls import path
from django.views.generic import RedirectView
from . import views

urlpatterns = [
    # Legacy listing URLs retained as compatibility redirects.
    path('', RedirectView.as_view(pattern_name='available_swap_offers', permanent=True), name='listings'),
    path('create/', RedirectView.as_view(pattern_name='create_listing', permanent=True), name='legacy_create_listing'),
    path('listing/<int:listing_id>/', views.listing_detail, name='listing_detail'),
    path('markets/', RedirectView.as_view(pattern_name='markets', permanent=True), name='legacy_markets'),
    path('markets/create/', views.create_market),
    path('markets/<int:market_id>/toggle/', views.toggle_market_status),
    path('dex/', RedirectView.as_view(pattern_name='dex_orderbook', permanent=True), name='legacy_dex_orderbook'),
    path('dex/pair-balances/<int:pair_id>/', views.market_pair_balances),
    path('dex/limit-order/', views.place_limit_order),
    path('dex/market-order/', views.place_market_order),
    path('dex/stop-loss-order/', views.place_stop_loss_order),
    path('dex/cancel-order/<int:order_id>/', views.cancel_order),
    path('dex/cancel-stop-loss/<int:order_id>/', views.cancel_stop_loss),
    path('dex/my-orders/', RedirectView.as_view(pattern_name='my_orders', permanent=True)),
]
