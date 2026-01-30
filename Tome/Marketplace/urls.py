from django.urls import path
from . import views

urlpatterns = [
    path('', views.marketplace, name='marketplace'),
    path('create/', views.create_listing, name='create_listing'),
    path('listing/<int:listing_id>/', views.listing_detail, name='listing_detail'),
]
