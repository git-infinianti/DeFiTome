from django.db import models

# Create your models here.
''' 
The Marketplace app is where all the logic related to the peer-to-peer marketplace will reside. This includes models for items, categories, and transactions.
The marketplace will be mainly focused on listing other coins to buy/sell/trade using DeFi Tome's wallet system. Aswell as NFTs.
'''
# Create MarketplaceItem model
class MarketplaceItem(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField()
    quantity = models.PositiveIntegerField(default=1)
    individual_price = models.DecimalField(max_digits=20, decimal_places=8)
    total_price = models.DecimalField(max_digits=20, decimal_places=8)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.title

# Create MarketplaceCategory model
class MarketplaceCategory(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    
    def __str__(self):
        return self.name

# Create relationship between MarketplaceItem and MarketplaceCategory
class ItemCategory(models.Model):
    item = models.ForeignKey(MarketplaceItem, on_delete=models.CASCADE, related_name='categories')
    category = models.ForeignKey(MarketplaceCategory, on_delete=models.CASCADE, related_name='items')
    
    def __str__(self):
        return f"{self.item.title} in {self.category.name}"
    
# Create MarketplaceTransaction model
class MarketplaceTransaction(models.Model):
    item = models.ForeignKey(MarketplaceItem, on_delete=models.CASCADE, related_name='transactions')
    buyer = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='purchases')
    seller = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='sales')
    quantity = models.PositiveIntegerField(default=1)
    individual_price = models.DecimalField(max_digits=20, decimal_places=8)
    total_price = models.DecimalField(max_digits=20, decimal_places=8)
    transaction_date = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Transaction of {self.item.title} by {self.buyer}"

# Create MarketplaceReview model
class MarketplaceReview(models.Model):
    item = models.ForeignKey(MarketplaceItem, on_delete=models.CASCADE, related_name='reviews')
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='reviews')
    rating = models.PositiveIntegerField()
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Review of {self.item.title} by {self.user.username}"

# Create MarketplaceListing model
class MarketplaceListing(models.Model):
    item = models.ForeignKey(MarketplaceItem, on_delete=models.CASCADE, related_name='listings')
    seller = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='listings')
    price = models.DecimalField(max_digits=20, decimal_places=8)
    quantity_available = models.PositiveIntegerField(default=1)
    listing_date = models.DateTimeField(auto_now_add=True)
    
    # P2P swap fields
    allow_swaps = models.BooleanField(default=True)
    preferred_swap_token = models.CharField(max_length=10, blank=True, default='')
    
    def __str__(self):
        return f"Listing of {self.item.title} by {self.seller.username}"

class MarketplaceOrder(models.Model):
    transaction = models.ForeignKey(MarketplaceTransaction, on_delete=models.CASCADE, related_name='orders')
    order_date = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=50, default='Pending')
    
    def __str__(self):
        return f"Order for {self.transaction.item.title} - Status: {self.status}"