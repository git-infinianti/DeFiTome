from django.db import models

# Create your models here.
class UserWallet(models.Model):
    user_id = models.UUIDField(primary_key=True, editable=False, auto_created=True)
    entropy = models.CharField(max_length=256)
    passphrase = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"UserWallet(id={self.id})"