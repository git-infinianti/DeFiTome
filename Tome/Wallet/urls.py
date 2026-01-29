from django.urls import path
from . import views

urlpatterns = [
    path('portfolio/', views.portfolio, name='portfolio'),
    path('portfolio/backup/', views.backup_wallet, name='backup_wallet'),
    path('portfolio/send/', views.send_funds, name='send_funds'),
    path('portfolio/receive/', views.recieve_funds, name='recieve_funds'),
]
