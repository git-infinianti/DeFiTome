from django.urls import path
from . import views
urlpatterns = [
    path('register/', views.register, name='register'),
    path('login/', views.login, name='login'),
    path('home/', views.home, name='home'),
    path('logout/', views.logout, name='logout'),
    path('verify-email/<uuid:token>/', views.verify_email, name='verify_email'),
]  