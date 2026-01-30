from django.db import models
from django.contrib.auth.models import User

# Create your models here.

class TestnetConfig(models.Model):
    """Configuration for the DeFi testnet"""
    name = models.CharField(max_length=100, default='DeFi Tome Testnet')
    is_active = models.BooleanField(default=True)
    network_id = models.CharField(max_length=50, default='defitome-testnet-v1')
    rpc_endpoint = models.URLField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"TestnetConfig(name={self.name}, active={self.is_active})"

class LiquidityPool(models.Model):
    """Liquidity pool for token swaps on testnet"""
    name = models.CharField(max_length=100)
    token_a_symbol = models.CharField(max_length=10)
    token_b_symbol = models.CharField(max_length=10)
    token_a_reserve = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    token_b_reserve = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    total_liquidity_tokens = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    fee_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.30)  # 0.30%
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"LiquidityPool({self.token_a_symbol}/{self.token_b_symbol})"
    
    class Meta:
        unique_together = ['token_a_symbol', 'token_b_symbol']

class LiquidityPosition(models.Model):
    """User's liquidity position in a pool"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='liquidity_positions')
    pool = models.ForeignKey(LiquidityPool, on_delete=models.CASCADE, related_name='positions')
    liquidity_tokens = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"LiquidityPosition(user={self.user.username}, pool={self.pool.name}, tokens={self.liquidity_tokens})"
    
    class Meta:
        unique_together = ['user', 'pool']

class SwapTransaction(models.Model):
    """Record of a swap transaction on testnet"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='swap_transactions')
    pool = models.ForeignKey(LiquidityPool, on_delete=models.CASCADE, related_name='swaps')
    from_token = models.CharField(max_length=10)
    to_token = models.CharField(max_length=10)
    from_amount = models.DecimalField(max_digits=20, decimal_places=8)
    to_amount = models.DecimalField(max_digits=20, decimal_places=8)
    fee_amount = models.DecimalField(max_digits=20, decimal_places=8)
    tx_hash = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"SwapTransaction({self.from_amount} {self.from_token} -> {self.to_amount} {self.to_token})"

class SwapOffer(models.Model):
    """P2P swap offer between two users"""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('completed', 'Completed'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
        ('expired', 'Expired'),
    ]
    
    initiator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='initiated_swaps')
    counterparty = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_swaps', null=True, blank=True)
    marketplace_listing = models.ForeignKey('Marketplace.MarketplaceListing', on_delete=models.CASCADE, related_name='swap_offers', null=True, blank=True)
    
    # What initiator offers
    offer_token = models.CharField(max_length=10)
    offer_amount = models.DecimalField(max_digits=20, decimal_places=8)
    
    # What initiator wants
    request_token = models.CharField(max_length=10)
    request_amount = models.DecimalField(max_digits=20, decimal_places=8)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    escrow_id = models.CharField(max_length=100, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"SwapOffer({self.offer_amount} {self.offer_token} for {self.request_amount} {self.request_token})"

class SwapEscrow(models.Model):
    """Escrow for locked funds during P2P swap"""
    swap_offer = models.OneToOneField(SwapOffer, on_delete=models.CASCADE, related_name='escrow')
    initiator_locked = models.BooleanField(default=False)
    counterparty_locked = models.BooleanField(default=False)
    initiator_amount = models.DecimalField(max_digits=20, decimal_places=8)
    counterparty_amount = models.DecimalField(max_digits=20, decimal_places=8)
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"SwapEscrow(offer={self.swap_offer.id}, initiator_locked={self.initiator_locked}, counterparty_locked={self.counterparty_locked})"
    
    @property
    def is_fully_locked(self):
        return self.initiator_locked and self.counterparty_locked

class P2PSwapTransaction(models.Model):
    """Completed P2P swap transaction record"""
    swap_offer = models.OneToOneField(SwapOffer, on_delete=models.CASCADE, related_name='transaction')
    initiator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='p2p_swaps_as_initiator')
    counterparty = models.ForeignKey(User, on_delete=models.CASCADE, related_name='p2p_swaps_as_counterparty')
    
    initiator_token = models.CharField(max_length=10)
    initiator_amount = models.DecimalField(max_digits=20, decimal_places=8)
    counterparty_token = models.CharField(max_length=10)
    counterparty_amount = models.DecimalField(max_digits=20, decimal_places=8)
    
    tx_hash = models.CharField(max_length=100, blank=True)
    completed_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"P2PSwapTransaction({self.initiator.username} <-> {self.counterparty.username})"
