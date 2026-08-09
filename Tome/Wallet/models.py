from django.db import models
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.db.models import Q
from decimal import Decimal

# Create your models here.
class UserWallet(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='user_wallet')
    name = models.CharField(max_length=256, default='My Wallet')
    entropy = models.CharField(max_length=256)
    passphrase = models.CharField(max_length=256, blank=True)
    evr_liquidity = models.DecimalField(max_digits=20, decimal_places=8, default=Decimal('0'))
    last_balance_update = models.DateTimeField(blank=True, null=True)
    evr_liquidity_mainnet = models.DecimalField(max_digits=20, decimal_places=8, default=Decimal('0'))
    evr_liquidity_testnet = models.DecimalField(max_digits=20, decimal_places=8, default=Decimal('0'))
    last_balance_update_mainnet = models.DateTimeField(blank=True, null=True)
    last_balance_update_testnet = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"UserWallet(name={self.name}, user_id={self.user_id})"

class WalletAddress(models.Model):
    NETWORK_MODE_MAINNET = 'mainnet'
    NETWORK_MODE_TESTNET = 'testnet'
    NETWORK_MODE_CHOICES = [
        (NETWORK_MODE_MAINNET, 'Mainnet'),
        (NETWORK_MODE_TESTNET, 'Testnet'),
    ]

    wallet = models.ForeignKey(UserWallet, on_delete=models.CASCADE, related_name='addresses')
    network_mode = models.CharField(max_length=10, choices=NETWORK_MODE_CHOICES, default=NETWORK_MODE_MAINNET)
    address = models.CharField(max_length=256)
    wif = models.CharField(max_length=256)
    account = models.PositiveIntegerField()
    index = models.PositiveIntegerField()
    is_change = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('wallet', 'network_mode', 'account', 'index', 'is_change')
    
    def __str__(self):
        return f"WalletAddress(address={self.address}, network={self.network_mode}, index={self.index})"


class WalletProfile(models.Model):
    wallet = models.ForeignKey(UserWallet, on_delete=models.CASCADE, related_name='profiles')
    address = models.OneToOneField(WalletAddress, on_delete=models.CASCADE, related_name='wallet_profile')
    network_mode = models.CharField(
        max_length=10,
        choices=WalletAddress.NETWORK_MODE_CHOICES,
        default=WalletAddress.NETWORK_MODE_MAINNET,
    )
    name = models.CharField(max_length=100)
    is_main = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('wallet', 'network_mode'),
                condition=Q(is_main=True),
                name='wallet_one_main_profile_per_network',
            ),
            models.UniqueConstraint(
                fields=('wallet', 'network_mode', 'name'),
                name='wallet_profile_name_unique_per_network',
            ),
        ]

    def clean(self):
        if not self.address_id:
            return

        if self.address.wallet_id != self.wallet_id:
            raise ValidationError('The selected address does not belong to this wallet.')

        if self.address.network_mode != self.network_mode:
            raise ValidationError('The selected address is not on the active network for this profile.')

        if self.address.is_change:
            raise ValidationError('Change addresses cannot be assigned to wallet profiles.')

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"WalletProfile(name={self.name}, address={self.address.address}, main={self.is_main})"


class WalletPreferences(models.Model):
    TAB_SEND = 'send'
    TAB_RECEIVE = 'receive'
    TAB_PROFILES = 'profiles'
    TAB_CHOICES = [
        (TAB_SEND, 'Send'),
        (TAB_RECEIVE, 'Receive'),
        (TAB_PROFILES, 'Profiles'),
    ]

    TRANSACTION_LIMIT_ALL = 'all'
    TRANSACTION_LIMIT_CHOICES = [
        (TRANSACTION_LIMIT_ALL, 'All'),
        ('25', 'Latest 25'),
        ('50', 'Latest 50'),
        ('100', 'Latest 100'),
        ('250', 'Latest 250'),
    ]

    SEND_CONFIRMATION_CHOICES = [
        ('always', 'Always'),
        ('warn', 'Warn for large sends'),
        ('off', 'Disabled'),
    ]

    QR_STYLE_CHOICES = [
        ('classic', 'Classic'),
        ('high_contrast', 'High Contrast'),
        ('minimal', 'Minimal'),
    ]

    ADDRESS_LABEL_CHOICES = [
        ('full', 'Full Address'),
        ('short', 'Short Label'),
        ('masked', 'Masked Address'),
    ]

    PROFILE_SORT_CHOICES = [
        ('main_first', 'Main Profile First'),
        ('name_asc', 'Name A to Z'),
        ('name_desc', 'Name Z to A'),
        ('index_asc', 'Index Low to High'),
        ('index_desc', 'Index High to Low'),
    ]

    wallet = models.OneToOneField(UserWallet, on_delete=models.CASCADE, related_name='preferences')
    default_home_tab = models.CharField(max_length=20, choices=TAB_CHOICES, default=TAB_SEND)
    default_send_currency = models.CharField(max_length=64, default='EVR')
    default_transaction_limit = models.CharField(max_length=10, choices=TRANSACTION_LIMIT_CHOICES, default=TRANSACTION_LIMIT_ALL)
    default_confirmation_behavior = models.CharField(max_length=20, choices=SEND_CONFIRMATION_CHOICES, default='always')
    default_receive_qr_style = models.CharField(max_length=20, choices=QR_STYLE_CHOICES, default='classic')
    address_label_style = models.CharField(max_length=20, choices=ADDRESS_LABEL_CHOICES, default='full')
    profile_sort_order = models.CharField(max_length=20, choices=PROFILE_SORT_CHOICES, default='main_first')
    auto_sync_balance = models.BooleanField(default=True)
    auto_validate_recipient = models.BooleanField(default=True)
    auto_copy_receive_address = models.BooleanField(default=False)
    show_receive_qr = models.BooleanField(default=True)
    show_zero_balances = models.BooleanField(default=True)
    show_change_addresses = models.BooleanField(default=False)
    show_profile_network_badges = models.BooleanField(default=True)
    highlight_main_profile = models.BooleanField(default=True)
    hide_balance_on_open = models.BooleanField(default=False)
    compact_cards = models.BooleanField(default=False)
    confirm_external_links = models.BooleanField(default=True)
    enable_address_tooltips = models.BooleanField(default=True)
    prefer_main_profile_on_receive = models.BooleanField(default=True)
    nft_image_uri_template = models.CharField(
        max_length=255,
        default='ipfs://{cid}/{filename}',
        help_text='Template for generated NFT image URIs. Use {cid} and {filename}.',
    )
    transaction_refresh_seconds = models.PositiveIntegerField(default=30)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'Wallet Preferences'

    def __str__(self):
        return f"WalletPreferences(wallet_id={self.wallet_id}, home_tab={self.default_home_tab}, currency={self.default_send_currency})"


class TrackedAsset(models.Model):
    NETWORK_MODE_MAINNET = 'mainnet'
    NETWORK_MODE_TESTNET = 'testnet'
    NETWORK_MODE_CHOICES = [
        (NETWORK_MODE_MAINNET, 'Mainnet'),
        (NETWORK_MODE_TESTNET, 'Testnet'),
    ]

    ASSET_TYPE_MAIN = 'main'
    ASSET_TYPE_SUB = 'sub'
    ASSET_TYPE_UNIQUE = 'unique'
    ASSET_TYPE_MESSAGING = 'messaging_channel'
    ASSET_TYPE_QUALIFIER = 'qualifier'
    ASSET_TYPE_SUB_QUALIFIER = 'sub_qualifier'
    ASSET_TYPE_RESTRICTED = 'restricted'
    ASSET_TYPE_ADMIN = 'administrator'

    ASSET_TYPE_CHOICES = (
        (ASSET_TYPE_MAIN, 'Main'),
        (ASSET_TYPE_SUB, 'Sub'),
        (ASSET_TYPE_UNIQUE, 'Unique'),
        (ASSET_TYPE_MESSAGING, 'Messaging Channel'),
        (ASSET_TYPE_QUALIFIER, 'Qualifier'),
        (ASSET_TYPE_SUB_QUALIFIER, 'Sub Qualifier'),
        (ASSET_TYPE_RESTRICTED, 'Restricted'),
        (ASSET_TYPE_ADMIN, 'Administrator'),
    )

    symbol = models.CharField(max_length=255)
    network_mode = models.CharField(max_length=10, choices=NETWORK_MODE_CHOICES, default=NETWORK_MODE_MAINNET)
    asset_type = models.CharField(max_length=32, choices=ASSET_TYPE_CHOICES, default=ASSET_TYPE_MAIN)
    total_quantity = models.DecimalField(max_digits=30, decimal_places=8, default=Decimal('0'))
    
    # Asset metadata fields
    ipfs_hash = models.CharField(max_length=255, blank=True, null=True, help_text="IPFS hash for asset metadata")
    has_toll = models.BooleanField(default=False, help_text="Whether asset has transfer toll enabled")
    toll_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'), help_text="Toll percentage for transfers")
    toll_address = models.CharField(max_length=100, blank=True, null=True, help_text="Address receiving toll payments")
    is_reissuable = models.BooleanField(default=True, help_text="Whether asset can be reissued")
    units = models.IntegerField(default=0, help_text="Decimal places for asset divisibility (0-8)")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('symbol', 'network_mode'),
                name='tracked_asset_symbol_network_unique',
            ),
        ]

    def __str__(self):
        return f"TrackedAsset(symbol={self.symbol}, network={self.network_mode}, type={self.asset_type})"


class TrackedAssetHolding(models.Model):
    asset = models.ForeignKey(TrackedAsset, on_delete=models.CASCADE, related_name='holdings')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='asset_holdings')
    quantity = models.DecimalField(max_digits=30, decimal_places=8, default=Decimal('0'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('asset', 'user')

    def __str__(self):
        return f"TrackedAssetHolding(asset={self.asset.symbol}, user_id={self.user_id}, qty={self.quantity})"


class AssetCreationRequest(models.Model):
    KIND_MAIN = 'main'
    KIND_SUB = 'sub'
    KIND_UNIQUE = 'unique'
    KIND_MESSAGING = 'messaging_channel'
    KIND_QUALIFIER = 'qualifier'
    KIND_SUB_QUALIFIER = 'sub_qualifier'
    KIND_RESTRICTED = 'restricted'
    KIND_CHOICES = (
        (KIND_MAIN, 'Main Asset'),
        (KIND_SUB, 'Sub Asset'),
        (KIND_UNIQUE, 'Unique Asset'),
        (KIND_MESSAGING, 'Messaging Channel'),
        (KIND_QUALIFIER, 'Qualifier'),
        (KIND_SUB_QUALIFIER, 'Sub Qualifier'),
        (KIND_RESTRICTED, 'Restricted Asset'),
    )

    STATUS_PENDING = 'pending'
    STATUS_ACCEPTED = 'accepted'
    STATUS_BROADCAST = 'broadcast'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'Pending'),
        (STATUS_ACCEPTED, 'Mempool Accepted'),
        (STATUS_BROADCAST, 'Broadcast'),
        (STATUS_FAILED, 'Failed'),
    )

    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='asset_creation_requests')
    network_mode = models.CharField(max_length=10, choices=TrackedAsset.NETWORK_MODE_CHOICES, default='testnet')
    asset_kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    asset_name = models.CharField(max_length=255)
    source_address = models.CharField(max_length=100)
    parameters = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    mempool_txid = models.CharField(max_length=64, blank=True, default='')
    broadcast_txid = models.CharField(max_length=64, blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'AssetCreationRequest(asset={self.asset_name}, status={self.status})'


class SafeTradeCredentials(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='safe_trade_credentials')
    api_key = models.CharField(max_length=255)
    api_secret = models.CharField(max_length=255)
    member_info = models.JSONField(blank=True, null=True)
    last_synced_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"SafeTradeCredentials(user_id={self.user_id})"
    
