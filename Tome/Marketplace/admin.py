from django.contrib import admin
from .models import MarketplaceItem, MarketplaceOrder, MarketplaceCategory, ItemCategory, MarketplaceTransaction, MarketplaceReview, MarketplaceListing

# Register your models here.
admin.site.register(MarketplaceItem)
admin.site.register(MarketplaceOrder)
admin.site.register(MarketplaceCategory)
admin.site.register(ItemCategory)
admin.site.register(MarketplaceTransaction)
admin.site.register(MarketplaceReview)
admin.site.register(MarketplaceListing)