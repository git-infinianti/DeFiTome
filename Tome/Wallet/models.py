from django.db import models
from django.contrib.auth.models import User

# Create your models here.
class UserWallet(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='user_wallet')
    entropy = models.CharField(max_length=256)
    passphrase = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"UserWallet(id={self.id})"