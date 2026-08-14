from django.contrib import admin
from .models import (
    ListingItem, ListingCategory, ItemCategory, 
    ListingTransaction, ListingReview, Listing, 
    ListingOrder, TradingPair, LimitOrder, MarketOrder, 
    StopLossOrder, OrderExecution, BalanceLock, NFT,
    DecPokerGameInstance, DecPokerHand, DecPokerPayoutLedgerEntry,
    DecPokerAuditAuthority, DecPokerMarketValuation, DecPokerPayoutPolicy,
    DecPokerValuationBid,
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
admin.site.register(BalanceLock)
admin.site.register(NFT)
admin.site.register(DecPokerGameInstance)


@admin.register(DecPokerHand)
class DecPokerHandAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'game_instance', 'player', 'result', 'wager_evr',
        'reward_amount', 'settlement_status', 'created_at',
    )
    readonly_fields = tuple(field.name for field in DecPokerHand._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DecPokerPayoutPolicy)
class DecPokerPayoutPolicyAdmin(admin.ModelAdmin):
    list_display = (
        'game_instance', 'version', 'house_rule', 'payout_currency',
        'reward_per_win', 'rtp_status', 'created_at',
    )
    readonly_fields = tuple(field.name for field in DecPokerPayoutPolicy._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DecPokerPayoutLedgerEntry)
class DecPokerPayoutLedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        'sequence', 'game_instance', 'hand', 'event_type', 'currency',
        'stake_amount', 'payout_amount', 'balance_delta', 'external_txid',
        'occurred_at',
    )
    readonly_fields = tuple(field.name for field in DecPokerPayoutLedgerEntry._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DecPokerAuditAuthority)
class DecPokerAuditAuthorityAdmin(admin.ModelAdmin):
    list_display = (
        'network_mode', 'authority_account', 'restricted_asset_name',
        'required_qualifier_name', 'status', 'enforce_settlement_writes',
        'last_verified_at',
    )
    readonly_fields = tuple(field.name for field in DecPokerAuditAuthority._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DecPokerMarketValuation)
class DecPokerMarketValuationAdmin(admin.ModelAdmin):
    list_display = (
        'game_instance', 'trading_pair', 'source_execution_count',
        'price_evr_per_reward_asset', 'rtp_percent', 'created_at',
    )
    readonly_fields = tuple(field.name for field in DecPokerMarketValuation._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DecPokerValuationBid)
class DecPokerValuationBidAdmin(admin.ModelAdmin):
    list_display = (
        'game_instance', 'trading_pair', 'limit_order', 'requested_by',
        'price_evr_per_reward_asset', 'reward_asset_quantity', 'post_only',
        'created_at',
    )
    readonly_fields = tuple(field.name for field in DecPokerValuationBid._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False