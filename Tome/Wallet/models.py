from django.db import models
from django.contrib.auth.models import User

# Create your models here.
class UserWallet(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='user_wallet')
    name = models.CharField(max_length=256, default='My Wallet')
    entropy = models.CharField(max_length=256)
    passphrase = models.CharField(max_length=256, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"UserWallet(name={self.name}, user={self.user.username})"

class WalletAddress(models.Model):
    wallet = models.ForeignKey(UserWallet, on_delete=models.CASCADE, related_name='addresses')
    address = models.CharField(max_length=256)
    wif = models.CharField(max_length=256)
    account = models.PositiveIntegerField()
    index = models.PositiveIntegerField()
    is_change = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"WalletAddress(address={self.address}, index={self.index})"