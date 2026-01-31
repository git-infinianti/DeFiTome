from django.contrib import admin
from .models import (
    ListingItem, ListingCategory, ItemCategory, 
    ListingTransaction, ListingReview, Listing, 
    ListingOrder, TradingPair, LimitOrder, MarketOrder, 
    StopLossOrder, OrderExecution
)

# Register your models here.
admin.site.register(ListingItem)
admin.site.register(ListingOrder)
admin.site.register(ListingCategory)
admin.site.register(ItemCategory)
admin.site.register(ListingTransaction)
admin.site.register(ListingReview)
admin.site.register(Listing)
admin.site.register(TradingPair)
admin.site.register(LimitOrder)
admin.site.register(MarketOrder)
admin.site.register(StopLossOrder)
admin.site.register(OrderExecution)