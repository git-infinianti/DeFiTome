from django.urls import path

from Listings import views


urlpatterns = [
    path('', views.markets_view, name='markets'),
    path('create/', views.create_market, name='create_market'),
    path('<int:market_id>/favorite/', views.toggle_market_favorite, name='toggle_market_favorite'),
    path('<int:market_id>/toggle/', views.toggle_market_status, name='toggle_market_status'),
    path('<int:market_id>/reverse/', views.reverse_market_pair, name='reverse_market_pair'),
    path('trade/', views.legacy_dex_orderbook, name='legacy_market_trade'),
    path('trade/pair-balances/<int:pair_id>/', views.market_pair_balances, name='market_pair_balances'),
    path('orders/limit/', views.place_limit_order, name='place_limit_order'),
    path('orders/market/', views.place_market_order, name='place_market_order'),
    path('orders/stop-loss/', views.place_stop_loss_order, name='place_stop_loss_order'),
    path('orders/<int:order_id>/cancel/', views.cancel_order, name='cancel_order'),
    path('orders/stop-loss/<int:order_id>/cancel/', views.cancel_stop_loss, name='cancel_stop_loss'),
    path('orders/', views.my_orders, name='my_orders'),
    path('<slug:pair_slug>/', views.dex_orderbook, name='dex_orderbook'),
]