from django.urls import path
from . import views

urlpatterns = [
    path('portfolio/', views.portfolio, name='portfolio'),
    path('portfolio/preferences/', views.wallet_preferences, name='wallet_preferences'),
    path('portfolio/backup/', views.backup_wallet, name='backup_wallet'),
    path('portfolio/transactions/', views.wallet_transactions, name='wallet_transactions'),
    path('portfolio/send/', views.send_funds, name='send_funds'),
    path('portfolio/receive/', views.recieve_funds, name='recieve_funds'),
    path('portfolio/admin/assets/create/', views.asset_creation_wizard, name='asset_creation_wizard'),
    path('portfolio/admin/messaging-channels/', views.messaging_channel_management, name='messaging_channel_management'),
    path('portfolio/sync-balance/', views.sync_balance, name='sync_balance'),
    path('portfolio/validate-address/', views.validate_address, name='validate_address'),
    path('portfolio/address-qr/', views.address_qr, name='address_qr'),
]
