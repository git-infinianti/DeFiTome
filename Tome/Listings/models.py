import hashlib
import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify

# Create your models here.
''' 
The Listings app is where all the logic related to the peer-to-peer DEX will reside. This includes models for items, categories, and transactions.
The listings will be mainly focused on listing other coins to buy/sell/trade using DeFi Tome's wallet system. As well as NFTs.
'''
# Create ListingItem model
class ListingItem(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField()
    quantity = models.DecimalField(max_digits=20, decimal_places=8, default=1)
    individual_price = models.DecimalField(max_digits=20, decimal_places=8)
    total_price = models.DecimalField(max_digits=20, decimal_places=8)
    created_at = models.DateTimeField(auto_now_add=True)
    
    # NFT fields
    is_nft = models.BooleanField(default=False)
    nft_image_ipfs_cid = models.CharField(max_length=100, blank=True, null=True, help_text="IPFS CID for NFT image")
    
    def __str__(self):
        return self.title

# Create ListingCategory model
class ListingCategory(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    
    def __str__(self):
        return self.name

# Create relationship between ListingItem and ListingCategory
class ItemCategory(models.Model):
    item = models.ForeignKey(ListingItem, on_delete=models.CASCADE, related_name='categories')
    category = models.ForeignKey(ListingCategory, on_delete=models.CASCADE, related_name='items')
    
    def __str__(self):
        return f"{self.item.title} in {self.category.name}"
    
# Create ListingTransaction model
class ListingTransaction(models.Model):
    item = models.ForeignKey(ListingItem, on_delete=models.CASCADE, related_name='transactions')
    buyer = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='purchases')
    seller = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='sales')
    quantity = models.DecimalField(max_digits=20, decimal_places=8, default=1)
    individual_price = models.DecimalField(max_digits=20, decimal_places=8)
    total_price = models.DecimalField(max_digits=20, decimal_places=8)
    transaction_date = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Transaction of {self.item.title} by {self.buyer}"

# Create ListingReview model
class ListingReview(models.Model):
    item = models.ForeignKey(ListingItem, on_delete=models.CASCADE, related_name='reviews')
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='reviews')
    rating = models.PositiveIntegerField()
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Review of {self.item.title} by {self.user.username}"

# Create Listing model
class Listing(models.Model):
    NETWORK_MODE_CHOICES = [
        ('testnet', 'Testnet'),
        ('mainnet', 'Mainnet'),
    ]

    item = models.ForeignKey(ListingItem, on_delete=models.CASCADE, related_name='listings')
    seller = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='listings')
    price = models.DecimalField(max_digits=20, decimal_places=8)
    quantity_available = models.DecimalField(max_digits=20, decimal_places=8, default=1)
    network_mode = models.CharField(max_length=10, choices=NETWORK_MODE_CHOICES, default='testnet', db_index=True)
    listing_date = models.DateTimeField(auto_now_add=True)
    
    # Atomic swap fields
    token_offered = models.CharField(max_length=255)
    preferred_token = models.CharField(max_length=255)
    
    def __str__(self):
        return f"Listing of {self.item.title} by {self.seller.username}"

class ListingOrder(models.Model):
    transaction = models.ForeignKey(ListingTransaction, on_delete=models.CASCADE, related_name='orders')
    order_date = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=50, default='Pending')
    
    def __str__(self):
        return f"Order for {self.transaction.item.title} - Status: {self.status}"

# Order Book DEX Models
class TradingPair(models.Model):
    """Native Evrmore asset pair traded through the market order book."""
    NETWORK_MODE_CHOICES = [
        ('testnet', 'Testnet'),
        ('mainnet', 'Mainnet'),
    ]
    INSTRUMENT_TOKEN = 'token'
    INSTRUMENT_SECURITY_CAPABLE = 'security_capable'
    INSTRUMENT_TYPE_CHOICES = [
        (INSTRUMENT_TOKEN, 'Token'),
        (INSTRUMENT_SECURITY_CAPABLE, 'Restricted / Security-capable'),
    ]

    base_token = models.CharField(max_length=255)
    quote_token = models.CharField(max_length=255)
    network_mode = models.CharField(max_length=10, choices=NETWORK_MODE_CHOICES, default='testnet', db_index=True)
    instrument_type = models.CharField(
        max_length=24,
        choices=INSTRUMENT_TYPE_CHOICES,
        default=INSTRUMENT_TOKEN,
        db_index=True,
    )
    pair_key = models.CharField(max_length=64, null=True, blank=True, editable=False)
    pair_slug = models.SlugField(max_length=255, editable=False, db_index=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='created_pairs')
    created_at = models.DateTimeField(auto_now_add=True)
    last_price = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    high_24h = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    low_24h = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    volume_24h = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    amount_24h = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    price_change_24h = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # percentage
    
    class Meta:
        unique_together = ['base_token', 'quote_token', 'network_mode']
        constraints = [
            models.UniqueConstraint(
                fields=('network_mode', 'pair_key'),
                name='trading_pair_unordered_unique_per_network',
            ),
            models.UniqueConstraint(
                fields=('network_mode', 'pair_slug'),
                name='trading_pair_slug_unique_per_network',
            ),
        ]
    
    def __str__(self):
        return f"{self.base_token}/{self.quote_token}"

    @staticmethod
    def build_pair_key(base_token, quote_token):
        canonical = '\x1f'.join(sorted((
            str(base_token or '').strip().upper(),
            str(quote_token or '').strip().upper(),
        )))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

    @staticmethod
    def build_pair_slug(base_token, quote_token):
        base = str(base_token or '').strip().upper()
        quote = str(quote_token or '').strip().upper()
        base_slug = slugify(re.sub(r'[/~#$!]+', '-', base))[:100] or 'asset'
        quote_slug = slugify(re.sub(r'[/~#$!]+', '-', quote))[:100] or 'asset'
        pair_slug = f'{base_slug}-{quote_slug}'
        if not re.fullmatch(r'[A-Z0-9]+', base) or not re.fullmatch(r'[A-Z0-9]+', quote):
            identity = hashlib.sha256(f'{base}\x1f{quote}'.encode('utf-8')).hexdigest()[:10]
            pair_slug = f'{pair_slug}-{identity}'
        return pair_slug

    def save(self, *args, **kwargs):
        self.instrument_type = (
            self.INSTRUMENT_SECURITY_CAPABLE
            if str(self.base_token or '').startswith('$')
            else self.INSTRUMENT_TOKEN
        )
        self.pair_key = self.build_pair_key(self.base_token, self.quote_token)
        self.pair_slug = self.build_pair_slug(self.base_token, self.quote_token)
        super().save(*args, **kwargs)
    
    def get_24h_stats(self):
        """Calculate 24h statistics from order executions"""
        from django.utils import timezone
        from datetime import timedelta
        from django.db.models import Sum, Min, Max, Count
        
        since = timezone.now() - timedelta(hours=24)
        executions = self.executions.filter(created_at__gte=since)
        
        if executions.exists():
            stats = executions.aggregate(
                high=Max('price'),
                low=Min('price'),
                volume=Sum('quantity'),
                amount=Count('id')
            )
            last_execution = self.executions.order_by('-created_at').first()
            first_price = executions.order_by('created_at').first().price if executions.exists() else self.last_price
            
            if last_execution:
                self.last_price = last_execution.price
                if first_price and first_price > 0:
                    self.price_change_24h = ((self.last_price - first_price) / first_price) * 100
            
            self.high_24h = stats['high'] or 0
            self.low_24h = stats['low'] or 0
            self.volume_24h = stats['volume'] or 0
            self.amount_24h = stats['amount'] or 0
            self.save()
        
        return {
            'last_price': self.last_price,
            'price_change_24h': self.price_change_24h,
            'high_24h': self.high_24h,
            'low_24h': self.low_24h,
            'volume_24h': self.volume_24h,
            'amount_24h': self.amount_24h,
        }


class MarketFavorite(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='market_favorites',
    )
    trading_pair = models.ForeignKey(
        TradingPair,
        on_delete=models.CASCADE,
        related_name='favorited_by',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'trading_pair'),
                name='unique_market_favorite_per_user',
            ),
        ]

    def __str__(self):
        return f'{self.user} - {self.trading_pair}'

class LimitOrder(models.Model):
    """Limit order in the order book"""
    ORDER_SIDE_CHOICES = [
        ('buy', 'Buy'),
        ('sell', 'Sell'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('partial', 'Partially Filled'),
        ('filled', 'Filled'),
        ('cancelled', 'Cancelled'),
    ]
    
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='limit_orders')
    trading_pair = models.ForeignKey(TradingPair, on_delete=models.CASCADE, related_name='limit_orders', db_index=True)
    side = models.CharField(max_length=4, choices=ORDER_SIDE_CHOICES, db_index=True)
    price = models.DecimalField(max_digits=20, decimal_places=8, db_index=True)
    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    filled_quantity = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.side.upper()} {self.quantity} {self.trading_pair.base_token} @ {self.price}"
    
    @property
    def remaining_quantity(self):
        return self.quantity - self.filled_quantity

class MarketOrder(models.Model):
    """Market order for instant execution"""
    ORDER_SIDE_CHOICES = [
        ('buy', 'Buy'),
        ('sell', 'Sell'),
    ]
    STATUS_CHOICES = [
        ('executed', 'Executed'),
        ('failed', 'Failed'),
    ]
    
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='market_orders')
    trading_pair = models.ForeignKey(TradingPair, on_delete=models.CASCADE, related_name='market_orders')
    side = models.CharField(max_length=4, choices=ORDER_SIDE_CHOICES)
    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    executed_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='executed')
    tx_hash = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Market {self.side.upper()} {self.quantity} {self.trading_pair.base_token}"

class StopLossOrder(models.Model):
    """Stop-loss order that triggers when price reaches a threshold"""
    ORDER_SIDE_CHOICES = [
        ('buy', 'Buy'),
        ('sell', 'Sell'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('triggered', 'Triggered'),
        ('executed', 'Executed'),
        ('cancelled', 'Cancelled'),
    ]
    
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='stop_loss_orders')
    trading_pair = models.ForeignKey(TradingPair, on_delete=models.CASCADE, related_name='stop_loss_orders')
    side = models.CharField(max_length=4, choices=ORDER_SIDE_CHOICES)
    trigger_price = models.DecimalField(max_digits=20, decimal_places=8)
    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    executed_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    tx_hash = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    triggered_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"Stop-Loss {self.side.upper()} {self.quantity} {self.trading_pair.base_token} @ {self.trigger_price}"

class OrderExecution(models.Model):
    """Record of an executed order (trade)"""
    trading_pair = models.ForeignKey(TradingPair, on_delete=models.CASCADE, related_name='executions', db_index=True)
    buyer = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='buy_executions', db_index=True)
    seller = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='sell_executions', db_index=True)
    price = models.DecimalField(max_digits=20, decimal_places=8)
    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    buyer_order = models.ForeignKey(LimitOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='buy_executions')
    seller_order = models.ForeignKey(LimitOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='sell_executions')
    tx_hash = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    def __str__(self):
        return f"Trade: {self.quantity} {self.trading_pair.base_token} @ {self.price}"

class BalanceLock(models.Model):
    """
    Tracks asset reservations for open limit orders.

    Buy orders reserve quote assets and sell orders reserve base assets.
    The amount follows the order's remaining quantity until fill or cancellation.
    """
    STATUS_CHOICES = [
        ('locked', 'Locked'),
        ('released', 'Released'),
        ('consumed', 'Consumed'),
    ]
    
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='balance_locks', db_index=True)
    asset_symbol = models.CharField(max_length=255, default='EVR', db_index=True)
    amount = models.DecimalField(max_digits=20, decimal_places=8, db_index=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='locked', db_index=True)
    
    # Reference to the order that triggered the lock
    limit_order = models.ForeignKey(LimitOrder, on_delete=models.CASCADE, related_name='balance_lock', null=True, blank=True)
    market_order = models.ForeignKey(MarketOrder, on_delete=models.CASCADE, related_name='balance_lock', null=True, blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    released_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"BalanceLock({self.user.username}, {self.amount} {self.asset_symbol}, {self.status})"
    
    def get_total_locked(user, asset_symbol='EVR'):
        """Get the reserved amount of one asset for a user."""
        from django.db.models import Sum
        total = BalanceLock.objects.filter(
            user=user,
            asset_symbol=str(asset_symbol or '').strip().upper(),
            status='locked'
        ).aggregate(total=Sum('amount'))['total']
        return total or 0


class NFT(models.Model):
    """
    NFT model for tracking 1 of 1 unique digital assets.
    Each NFT is associated with a ListingItem that has is_nft=True and quantity=1.
    """
    listing_item = models.OneToOneField(ListingItem, on_delete=models.CASCADE, related_name='nft')
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='owned_nfts')
    creator = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='created_nfts')
    
    # IPFS image CID
    image_ipfs_cid = models.CharField(max_length=100, help_text="IPFS CID for NFT image")
    metadata_ipfs_cid = models.CharField(max_length=255, blank=True, default='', help_text="IPFS CID for standardized NFT metadata JSON")
    metadata_version = models.PositiveIntegerField(default=1, help_text="Version of the standardized NFT metadata schema")
    metadata_json = models.JSONField(default=dict, blank=True, help_text="Normalized NFT metadata payload")
    
    # Metadata
    token_id = models.CharField(max_length=100, unique=True, db_index=True, help_text="Unique token ID for the NFT")
    contract_address = models.CharField(max_length=100, blank=True, null=True, help_text="Blockchain contract address if minted on-chain")
    tx_hash = models.CharField(max_length=100, blank=True, null=True, help_text="Transaction hash for on-chain minting")
    
    # Status
    is_listed = models.BooleanField(default=False, help_text="Whether this NFT is currently listed for sale")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"NFT: {self.listing_item.title} (Owner: {self.owner.username})"
    
    def get_ipfs_url(self):
        """Get IPFS gateway URL for the image"""
        if self.image_ipfs_cid:
            return f"https://ipfs.io/ipfs/{self.image_ipfs_cid}"
        return None


class UniqueAssetMintRequest(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_BROADCAST = 'broadcast'
    STATUS_CONFIRMED = 'confirmed'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_BROADCAST, 'Broadcast'),
        (STATUS_CONFIRMED, 'Confirmed'),
        (STATUS_FAILED, 'Failed'),
    ]

    NETWORK_MODE_CHOICES = [
        ('testnet', 'Testnet'),
        ('mainnet', 'Mainnet'),
    ]

    creator = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='unique_mint_requests')
    network_mode = models.CharField(max_length=10, choices=NETWORK_MODE_CHOICES, default='testnet', db_index=True)
    admin_asset_symbol = models.CharField(max_length=255)
    root_name = models.CharField(max_length=255)
    asset_tag = models.CharField(max_length=255)
    unique_asset_name = models.CharField(max_length=255)
    metadata_ipfs_cid = models.CharField(max_length=255)
    metadata_version = models.PositiveIntegerField(default=1)
    metadata_json = models.JSONField(default=dict, blank=True)
    mint_txid = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    confirmation_depth = models.PositiveIntegerField(default=0)
    last_checked_at = models.DateTimeField(blank=True, null=True)
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('network_mode', 'unique_asset_name'),
                name='unique_mint_request_network_asset_unique',
            ),
        ]
        ordering = ('-created_at',)

    def __str__(self):
        return f"UniqueAssetMintRequest(asset={self.unique_asset_name}, status={self.status})"


class DecPokerAuditAuthority(models.Model):
    STATUS_DRAFT = 'draft'
    STATUS_ACTIVE = 'active'
    STATUS_SUSPENDED = 'suspended'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'),
        (STATUS_ACTIVE, 'Active'),
        (STATUS_SUSPENDED, 'Suspended'),
    ]

    NETWORK_MODE_CHOICES = [
        ('testnet', 'Testnet'),
        ('mainnet', 'Mainnet'),
    ]

    network_mode = models.CharField(
        max_length=10,
        choices=NETWORK_MODE_CHOICES,
        default='testnet',
        unique=True,
    )
    authority_account = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='dec_poker_audit_authorities',
    )
    authority_address = models.CharField(max_length=128)
    restricted_asset_name = models.CharField(max_length=30)
    required_qualifier_name = models.CharField(max_length=64)
    required_verifier_string = models.CharField(max_length=255)
    minimum_restricted_asset_balance = models.DecimalField(
        max_digits=30,
        decimal_places=8,
        default=1,
    )
    enforce_settlement_writes = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_verification_evidence = models.JSONField(default=dict)
    last_verification_error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('network_mode',)

    def __str__(self):
        return (
            f"DecPokerAuditAuthority(network={self.network_mode}, "
            f"asset={self.restricted_asset_name}, status={self.status})"
        )


class DecPokerGameInstance(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_ACTIVE = 'active'
    STATUS_PAUSED = 'paused'
    STATUS_RETIRED = 'retired'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_ACTIVE, 'Active'),
        (STATUS_PAUSED, 'Paused'),
        (STATUS_RETIRED, 'Retired'),
        (STATUS_FAILED, 'Failed'),
    ]

    NETWORK_MODE_CHOICES = [
        ('testnet', 'Testnet'),
        ('mainnet', 'Mainnet'),
    ]

    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='dec_created_games')
    manager_account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='dec_managed_games')
    network_mode = models.CharField(max_length=10, choices=NETWORK_MODE_CHOICES, default='testnet', db_index=True)
    title = models.CharField(max_length=120)
    reward_asset_name = models.CharField(max_length=30)
    reward_asset_units = models.PositiveSmallIntegerField(default=2)
    reward_supply = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    entry_fee_evr = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    reward_per_win = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    instance_fee_evr = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    instance_fee_txid = models.CharField(max_length=100, blank=True, default='')
    system_fee_address = models.CharField(max_length=128)
    wager_treasury_bps = models.PositiveIntegerField(default=5000)
    hand_cooldown_seconds = models.PositiveIntegerField(default=30)
    hand_cooldown_until = models.DateTimeField(null=True, blank=True)

    vault_profile = models.ForeignKey('Wallet.WalletProfile', on_delete=models.PROTECT, related_name='dec_vault_instances')
    channel_policy = models.ForeignKey(
        'API.MessageChannelPolicy',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='dec_game_instances',
    )

    reward_metadata_cid = models.CharField(max_length=255, blank=True, default='')
    reward_issue_txid = models.CharField(max_length=100, blank=True, default='')
    owner_transfer_txid = models.CharField(max_length=100, blank=True, default='')
    profile_tag_asset_name = models.CharField(max_length=64, blank=True, default='')
    profile_tag_txid = models.CharField(max_length=100, blank=True, default='')
    profile_tag_error = models.TextField(blank=True, default='')
    active_server_seed_hash = models.CharField(max_length=64, blank=True, default='')
    active_server_seed_secret = models.CharField(max_length=128, blank=True, default='')
    active_house_rule = models.CharField(
        max_length=64,
        default='dealer_best_two_of_three_wins_ties',
    )
    active_payout_policy = models.ForeignKey(
        'DecPokerPayoutPolicy',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='active_for_instances',
    )
    next_hand_nonce = models.PositiveIntegerField(default=1)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=('network_mode', 'reward_asset_name'),
                name='dec_game_reward_asset_unique_per_network',
            ),
        ]

    def __str__(self):
        return f"DecPokerGameInstance(title={self.title}, reward={self.reward_asset_name}, status={self.status})"


class DecPokerPayoutPolicy(models.Model):
    RTP_STATUS_VALUATION_REQUIRED = 'valuation_required'
    RTP_STATUS_DISCLOSED = 'disclosed'
    RTP_STATUS_CHOICES = [
        (RTP_STATUS_VALUATION_REQUIRED, 'External valuation required'),
        (RTP_STATUS_DISCLOSED, 'Disclosed percentage'),
    ]

    game_instance = models.ForeignKey(
        DecPokerGameInstance,
        on_delete=models.CASCADE,
        related_name='payout_policies',
    )
    market_valuation = models.ForeignKey(
        'DecPokerMarketValuation',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='payout_policies',
    )
    version = models.PositiveIntegerField()
    game_rule_version = models.CharField(max_length=64)
    house_rule = models.CharField(max_length=64)
    wager_currency = models.CharField(max_length=30, default='EVR')
    payout_currency = models.CharField(max_length=30)
    minimum_wager_evr = models.DecimalField(max_digits=20, decimal_places=8)
    reward_per_win = models.DecimalField(max_digits=30, decimal_places=8)
    payout_cap_amount = models.DecimalField(max_digits=30, decimal_places=8)
    win_probability_numerator = models.PositiveIntegerField()
    win_probability_denominator = models.PositiveIntegerField()
    expected_reward_per_wager = models.DecimalField(max_digits=30, decimal_places=8)
    rtp_status = models.CharField(
        max_length=32,
        choices=RTP_STATUS_CHOICES,
        default=RTP_STATUS_VALUATION_REQUIRED,
    )
    rtp_percent = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    rtp_disclosure = models.TextField()
    payout_table = models.JSONField(default=dict)
    authority_evidence = models.JSONField(default=dict)
    policy_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('game_instance_id', '-version')
        constraints = [
            models.UniqueConstraint(
                fields=('game_instance', 'version'),
                name='dec_poker_payout_policy_version_unique',
            ),
        ]

    def __str__(self):
        return f"DecPokerPayoutPolicy(game={self.game_instance_id}, version={self.version})"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('DEC payout policies are immutable after publication.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('DEC payout policies cannot be deleted.')


class DecPokerMarketValuation(models.Model):
    SOURCE_VWAP = 'execution_vwap'
    SOURCE_CHOICES = [
        (SOURCE_VWAP, 'Settled execution VWAP'),
    ]

    game_instance = models.ForeignKey(
        DecPokerGameInstance,
        on_delete=models.PROTECT,
        related_name='market_valuations',
    )
    trading_pair = models.ForeignKey(
        TradingPair,
        on_delete=models.PROTECT,
        related_name='dec_poker_market_valuations',
    )
    source_type = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_VWAP)
    source_execution_count = models.PositiveIntegerField()
    source_volume = models.DecimalField(max_digits=30, decimal_places=8)
    source_started_at = models.DateTimeField()
    source_ended_at = models.DateTimeField()
    price_evr_per_reward_asset = models.DecimalField(max_digits=30, decimal_places=8)
    expected_return_evr = models.DecimalField(max_digits=30, decimal_places=8)
    rtp_percent = models.DecimalField(max_digits=12, decimal_places=6)
    market_evidence = models.JSONField(default=dict)
    authority_evidence = models.JSONField(default=dict)
    valuation_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=('game_instance', 'valuation_hash'),
                name='dec_poker_market_valuation_hash_unique',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('DEC market valuations are immutable after publication.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('DEC market valuations cannot be deleted.')

    def __str__(self):
        return (
            f"DecPokerMarketValuation(game={self.game_instance_id}, "
            f"price={self.price_evr_per_reward_asset}, executions={self.source_execution_count})"
        )


class DecPokerValuationBid(models.Model):
    game_instance = models.ForeignKey(
        DecPokerGameInstance,
        on_delete=models.PROTECT,
        related_name='valuation_bids',
    )
    trading_pair = models.ForeignKey(
        TradingPair,
        on_delete=models.PROTECT,
        related_name='dec_poker_valuation_bids',
    )
    limit_order = models.OneToOneField(
        LimitOrder,
        on_delete=models.PROTECT,
        related_name='dec_poker_valuation_bid',
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='dec_poker_valuation_bids',
    )
    audit_authority = models.ForeignKey(
        DecPokerAuditAuthority,
        on_delete=models.PROTECT,
        related_name='valuation_bids',
    )
    price_evr_per_reward_asset = models.DecimalField(max_digits=30, decimal_places=8)
    reward_asset_quantity = models.DecimalField(max_digits=30, decimal_places=8)
    reserved_evr = models.DecimalField(max_digits=30, decimal_places=8)
    post_only = models.BooleanField(default=True)
    authority_evidence = models.JSONField(default=dict)
    intent_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('DEC valuation bids are immutable after creation.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('DEC valuation bids cannot be deleted.')

    def __str__(self):
        return (
            f"DecPokerValuationBid(game={self.game_instance_id}, "
            f"order={self.limit_order_id}, price={self.price_evr_per_reward_asset})"
        )


class DecPokerHand(models.Model):
    RESULT_WIN = 'win'
    RESULT_LOSE = 'lose'
    RESULT_PUSH = 'push'
    RESULT_CHOICES = [
        (RESULT_WIN, 'Win'),
        (RESULT_LOSE, 'Lose'),
        (RESULT_PUSH, 'Push'),
    ]

    MESSAGE_STATUS_BROADCASTED = 'broadcasted'
    MESSAGE_STATUS_FAILED = 'failed'
    MESSAGE_STATUS_SKIPPED = 'skipped'
    MESSAGE_STATUS_CHOICES = [
        (MESSAGE_STATUS_BROADCASTED, 'Broadcasted'),
        (MESSAGE_STATUS_FAILED, 'Failed'),
        (MESSAGE_STATUS_SKIPPED, 'Skipped'),
    ]

    SETTLEMENT_STATUS_ACCEPTED = 'accepted'
    SETTLEMENT_STATUS_SETTLING = 'settling'
    SETTLEMENT_STATUS_SETTLED = 'settled'
    SETTLEMENT_STATUS_RECONCILIATION_REQUIRED = 'reconciliation_required'
    SETTLEMENT_STATUS_FAILED = 'failed'
    SETTLEMENT_STATUS_CHOICES = [
        (SETTLEMENT_STATUS_ACCEPTED, 'Accepted'),
        (SETTLEMENT_STATUS_SETTLING, 'Settling'),
        (SETTLEMENT_STATUS_SETTLED, 'Settled'),
        (SETTLEMENT_STATUS_RECONCILIATION_REQUIRED, 'Reconciliation required'),
        (SETTLEMENT_STATUS_FAILED, 'Failed'),
    ]

    game_instance = models.ForeignKey(DecPokerGameInstance, on_delete=models.CASCADE, related_name='hands')
    player = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='dec_poker_hands')
    payout_policy = models.ForeignKey(
        DecPokerPayoutPolicy,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='hands',
    )
    payout_policy_version = models.PositiveIntegerField(default=0)
    payout_policy_hash = models.CharField(max_length=64, blank=True, default='')
    payout_policy_snapshot = models.JSONField(default=dict)
    settlement_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    idempotency_key = models.CharField(max_length=64, blank=True, default='')
    settlement_status = models.CharField(
        max_length=32,
        choices=SETTLEMENT_STATUS_CHOICES,
        default=SETTLEMENT_STATUS_SETTLED,
    )
    settlement_error = models.TextField(blank=True, default='')
    settled_at = models.DateTimeField(null=True, blank=True)
    wager_evr = models.DecimalField(max_digits=20, decimal_places=8)
    reward_amount = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    reward_asset_name = models.CharField(max_length=30, blank=True, default='')
    result = models.CharField(max_length=10, choices=RESULT_CHOICES)
    player_cards = models.JSONField(default=list)
    dealer_cards = models.JSONField(default=list)
    outcome_detail = models.JSONField(default=dict)
    client_seed = models.CharField(max_length=128, blank=True, default='')
    server_seed_hash = models.CharField(max_length=64, blank=True, default='')
    server_seed_revealed = models.CharField(max_length=128, blank=True, default='')
    fairness_nonce = models.PositiveIntegerField(default=1)
    fairness_digest = models.CharField(max_length=64, blank=True, default='')

    spend_txid = models.CharField(max_length=100, blank=True, default='')
    reward_txid = models.CharField(max_length=100, blank=True, default='')
    spend_message_txid = models.CharField(max_length=100, blank=True, default='')
    reward_message_txid = models.CharField(max_length=100, blank=True, default='')
    spend_message_status = models.CharField(max_length=16, choices=MESSAGE_STATUS_CHOICES, default=MESSAGE_STATUS_SKIPPED)
    reward_message_status = models.CharField(max_length=16, choices=MESSAGE_STATUS_CHOICES, default=MESSAGE_STATUS_SKIPPED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=('game_instance', 'player', 'idempotency_key'),
                condition=~models.Q(idempotency_key=''),
                name='dec_poker_hand_idempotency_key_unique',
            ),
        ]

    def __str__(self):
        return f"DecPokerHand(game={self.game_instance_id}, player={self.player_id}, result={self.result})"


class DecPokerPayoutLedgerEntry(models.Model):
    EVENT_WAGER_ACCEPTED = 'wager_accepted'
    EVENT_WAGER_SPEND_SETTLED = 'wager_spend_settled'
    EVENT_HAND_RESOLVED = 'hand_resolved'
    EVENT_REWARD_PAYOUT_SETTLED = 'reward_payout_settled'
    EVENT_RECONCILIATION_REQUIRED = 'reconciliation_required'
    EVENT_TYPE_CHOICES = [
        (EVENT_WAGER_ACCEPTED, 'Wager accepted'),
        (EVENT_WAGER_SPEND_SETTLED, 'Wager spend settled'),
        (EVENT_HAND_RESOLVED, 'Hand resolved'),
        (EVENT_REWARD_PAYOUT_SETTLED, 'Reward payout settled'),
        (EVENT_RECONCILIATION_REQUIRED, 'Reconciliation required'),
    ]

    game_instance = models.ForeignKey(
        DecPokerGameInstance,
        on_delete=models.PROTECT,
        related_name='payout_ledger_entries',
    )
    hand = models.ForeignKey(
        DecPokerHand,
        on_delete=models.PROTECT,
        related_name='payout_ledger_entries',
    )
    payout_policy = models.ForeignKey(
        DecPokerPayoutPolicy,
        on_delete=models.PROTECT,
        related_name='ledger_entries',
    )
    sequence = models.PositiveIntegerField()
    event_type = models.CharField(max_length=40, choices=EVENT_TYPE_CHOICES)
    correlation_id = models.UUIDField()
    idempotency_key = models.CharField(max_length=64)
    player_identifier = models.CharField(max_length=64)
    currency = models.CharField(max_length=30)
    stake_amount = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    payout_amount = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    balance_delta = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    result = models.CharField(max_length=10, blank=True, default='')
    payout_policy_version = models.PositiveIntegerField()
    payout_policy_hash = models.CharField(max_length=64)
    odds_snapshot = models.JSONField(default=dict)
    rng_evidence = models.JSONField(default=dict)
    external_txid = models.CharField(max_length=100, blank=True, default='')
    event_data = models.JSONField(default=dict)
    occurred_at = models.DateTimeField()
    previous_entry_hash = models.CharField(max_length=64, blank=True, default='')
    entry_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('game_instance_id', 'sequence')
        constraints = [
            models.UniqueConstraint(
                fields=('game_instance', 'sequence'),
                name='dec_poker_ledger_sequence_unique',
            ),
            models.UniqueConstraint(
                fields=('hand', 'event_type'),
                name='dec_poker_ledger_hand_event_unique',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('DEC payout ledger entries are append-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('DEC payout ledger entries cannot be deleted.')

    def __str__(self):
        return f"DecPokerPayoutLedgerEntry(hand={self.hand_id}, event={self.event_type}, sequence={self.sequence})"