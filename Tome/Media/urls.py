from django.urls import path
from . import views

urlpatterns = [
    path('address-tags/', views.address_metadata_tag_list, name='address_metadata_tag_list'),
    path('address-tags/create/', views.address_metadata_tag_create, name='address_metadata_tag_create'),
    path('address-tags/lookup/', views.address_metadata_tag_lookup, name='address_metadata_tag_lookup'),
    path('address-tags/<int:pk>/verify/', views.address_metadata_tag_verify, name='address_metadata_tag_verify'),
    path('', views.media_list, name='media_list'),
    path('upload/', views.media_upload, name='media_upload'),
    path('<int:pk>/preview/', views.media_preview, name='media_preview'),
    path('<int:pk>/edit/', views.media_edit, name='media_edit'),
    path('<int:pk>/delete/', views.media_delete, name='media_delete'),
]
