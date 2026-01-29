from django.urls import path
from . import views

urlpatterns = [
    path('', views.settings, name='settings'),
    path('resend-verification/', views.resend_verification_email, name='resend_verification'),
    path('change-theme/', views.change_theme, name='change_theme'),
]
