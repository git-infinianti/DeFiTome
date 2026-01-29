from django.db import models
from django.contrib.auth.models import User

# Create your models here.
class UserProfile(models.Model):
    THEME_CHOICES = [
        ('default', 'Default (Dark)'),
        ('light', 'Light'),
        ('dark', 'Dark'),
    ]
    
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    theme = models.CharField(max_length=20, choices=THEME_CHOICES, default='default')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"UserProfile(user={self.user.username}, theme={self.theme})"
