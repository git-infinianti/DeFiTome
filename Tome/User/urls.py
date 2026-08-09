from django.urls import path
from django.views.generic import RedirectView
from . import views
urlpatterns = [
    path('register/', views.register, name='register'),
    path('login/', views.login, name='login'),
    path('wallet-login/', views.wallet_login, name='wallet_login'),
    path('wallet-authentication/', views.manage_evrmore_wallet_authentication, name='evrmore_wallet_authentication'),
    path('home/', RedirectView.as_view(pattern_name='home', permanent=True), name='legacy_home'),
    path('logout/', views.logout, name='logout'),
    path('verify-email/<uuid:token>/', views.verify_email, name='verify_email'),
]  