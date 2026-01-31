from django.urls import path
from . import views

urlpatterns = [
    # Traditional listings
    path('', views.listings, name='listings'),
    path('create/', views.create_listing, name='create_listing'),
    path('listing/<int:listing_id>/', views.listing_detail, name='listing_detail'),
    
    # Order Book DEX
    path('dex/', views.dex_orderbook, name='dex_orderbook'),
    path('dex/limit-order/', views.place_limit_order, name='place_limit_order'),
    path('dex/market-order/', views.place_market_order, name='place_market_order'),
    path('dex/stop-loss-order/', views.place_stop_loss_order, name='place_stop_loss_order'),
    path('dex/cancel-order/<int:order_id>/', views.cancel_order, name='cancel_order'),
    path('dex/cancel-stop-loss/<int:order_id>/', views.cancel_stop_loss, name='cancel_stop_loss'),
    path('dex/my-orders/', views.my_orders, name='my_orders'),
]
