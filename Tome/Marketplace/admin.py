from django.contrib import admin
from .models import (
    MarketplaceItem, MarketplaceCategory, ItemCategory, 
    MarketplaceTransaction, MarketplaceReview, MarketplaceListing, 
    MarketplaceOrder, TradingPair, LimitOrder, MarketOrder, 
    StopLossOrder, OrderExecution
)

# Register your models here.
admin.site.register(MarketplaceItem)
admin.site.register(MarketplaceOrder)
admin.site.register(MarketplaceCategory)
admin.site.register(ItemCategory)
admin.site.register(MarketplaceTransaction)
admin.site.register(MarketplaceReview)
admin.site.register(MarketplaceListing)
admin.site.register(TradingPair)
admin.site.register(LimitOrder)
admin.site.register(MarketOrder)
admin.site.register(StopLossOrder)
admin.site.register(OrderExecution)