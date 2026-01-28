from django.db import models
from django.contrib.auth.models import User
import uuid

# Create your models here.
class UserWallet(models.Model):
    user_id = models.UUIDField(primary_key=True, editable=False, auto_created=True)
    entropy = models.CharField(max_length=256)
    passphrase = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"UserWallet(id={self.id})"


class EmailVerification(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='email_verification')
    is_verified = models.BooleanField(default=False)
    verification_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"EmailVerification(user={self.user.username}, verified={self.is_verified})"