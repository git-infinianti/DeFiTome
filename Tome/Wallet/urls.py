from django.urls import path
from . import views

urlpatterns = [
    path('portfolio/', views.portfolio, name='portfolio'),
    path('portfolio/backup/', views.backup_wallet, name='backup_wallet'),
]
