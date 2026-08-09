from django.db import models
from django.contrib.auth.models import User
from decimal import Decimal

# Constants for collateral calculations
MAX_COLLATERAL_RATIO = Decimal('999')  # Maximum healthy collateral ratio

# Create your models here.

class VaultAsset(models.Model):
    """
    Vault asset for advanced DeFi operations with toll/fee features.
    Vault assets are Evrmore assets with built-in transfer fees (tolls) for DeFi applications.
    """
    symbol = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    
    # Vault configuration
    toll_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'), 
                                         help_text="Percentage fee on transfers (0-100)")
    toll_address = models.CharField(max_length=100, help_text="Address receiving toll payments")
    
    # Asset properties
    total_supply = models.DecimalField(max_digits=30, decimal_places=8, default=Decimal('0'))
    is_reissuable = models.BooleanField(default=False)
    units = models.IntegerField(default=8, help_text="Decimal places (0-8)")
    ipfs_hash = models.CharField(max_length=255, blank=True, null=True)
    
    # DeFi usage tracking
    total_toll_collected = models.DecimalField(max_digits=30, decimal_places=8, default=Decimal('0'),
                                               help_text="Total toll collected from transfers")
    transfer_count = models.IntegerField(default=0, help_text="Total number of transfers")
    
    # Status
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"VaultAsset({self.symbol}, toll={self.toll_percentage}%)"
    
    class Meta:
        ordering = ['symbol']


class VaultCollateral(models.Model):
    """
    Collateral locked in vault assets for lending/borrowing DeFi operations.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='vault_collateral')
    vault_asset = models.ForeignKey(VaultAsset, on_delete=models.PROTECT, related_name='collateral_positions')
    
    # Collateral amounts
    collateral_amount = models.DecimalField(max_digits=30, decimal_places=8)
    borrowed_amount = models.DecimalField(max_digits=30, decimal_places=8, default=Decimal('0'))
    
    # Collateral configuration
    collateral_ratio = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('150'),
                                          help_text="Required collateral ratio (e.g., 150 = 150%)")
    liquidation_threshold = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('120'),
                                               help_text="Liquidation threshold (e.g., 120 = 120%)")
    
    # Status
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"VaultCollateral({self.user.username}, {self.collateral_amount} {self.vault_asset.symbol})"
    
    @property
    def current_ratio(self):
        """Calculate current collateral ratio"""
        if self.borrowed_amount == 0:
            return MAX_COLLATERAL_RATIO  # Very high ratio if no borrowed amount
        return (self.collateral_amount / self.borrowed_amount) * Decimal('100')
    
    @property
    def is_liquidatable(self):
        """Check if position can be liquidated"""
        return self.current_ratio < self.liquidation_threshold
    
    class Meta:
        ordering = ['-created_at']


class VaultEscrow(models.Model):
    """
    Escrow for vault asset transactions in P2P swaps and DeFi operations.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('locked', 'Locked'),
        ('released', 'Released'),
        ('cancelled', 'Cancelled'),
    ]
    
    initiator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='initiated_vault_escrows')
    counterparty = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_vault_escrows',
                                    null=True, blank=True)
    vault_asset = models.ForeignKey(VaultAsset, on_delete=models.PROTECT, related_name='escrows')
    
    # Escrow details
    amount = models.DecimalField(max_digits=30, decimal_places=8)
    toll_amount = models.DecimalField(max_digits=30, decimal_places=8, default=Decimal('0'),
                                     help_text="Toll amount deducted on release")
    
    # Status and timing
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    
    # Blockchain reference
    tx_hash = models.CharField(max_length=100, blank=True)
    
    def __str__(self):
        return f"VaultEscrow({self.amount} {self.vault_asset.symbol}, {self.status})"
    
    class Meta:
        ordering = ['-created_at']


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
    # Accumulated fees for fair distribution to liquidity providers
    accumulated_token_a_fees = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    accumulated_token_b_fees = models.DecimalField(max_digits=20, decimal_places=8, default=0)
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
    # Track unclaimed fees for fair distribution
    unclaimed_token_a_fees = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    unclaimed_token_b_fees = models.DecimalField(max_digits=20, decimal_places=8, default=0)
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
    """Atomic unique-asset offer settled with EVR or a fungible asset."""
    NETWORK_MODE_CHOICES = [
        ('testnet', 'Testnet'),
        ('mainnet', 'Mainnet'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('settling', 'Settling'),
        ('accepted', 'Accepted'),
        ('completed', 'Completed'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
        ('expired', 'Expired'),
    ]
    
    initiator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='initiated_swaps')
    counterparty = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_swaps', null=True, blank=True)
    listing = models.ForeignKey('Listings.Listing', on_delete=models.CASCADE, related_name='swap_offers', null=True, blank=True)
    
    # What initiator offers
    offer_token = models.CharField(max_length=255)
    offer_amount = models.DecimalField(max_digits=20, decimal_places=8)
    
    # What initiator wants
    request_token = models.CharField(max_length=255)
    request_amount = models.DecimalField(max_digits=20, decimal_places=8)
    
    network_mode = models.CharField(max_length=10, choices=NETWORK_MODE_CHOICES, default='testnet', db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    escrow_id = models.CharField(max_length=100, blank=True)
    expires_at = models.DateTimeField()
    settlement_temp_txid = models.CharField(max_length=100, blank=True)
    settlement_txid = models.CharField(max_length=100, blank=True)
    settlement_error = models.TextField(blank=True)
    settlement_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"SwapOffer({self.offer_amount} {self.offer_token} for {self.request_amount} {self.request_token})"


class SwapFundingLock(models.Model):
    """Reserves user balances during atomic swap settlement attempts."""

    STATUS_CHOICES = [
        ('locked', 'Locked'),
        ('released', 'Released'),
        ('consumed', 'Consumed'),
    ]

    swap_offer = models.ForeignKey(SwapOffer, on_delete=models.CASCADE, related_name='funding_locks', db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='swap_funding_locks', db_index=True)
    token_symbol = models.CharField(max_length=255, db_index=True)
    amount = models.DecimalField(max_digits=20, decimal_places=8)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='locked', db_index=True)
    lock_reason = models.CharField(max_length=50, default='atomic_swap_settlement')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    released_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return (
            f"SwapFundingLock(offer={self.swap_offer_id}, user={self.user_id}, "
            f"{self.amount} {self.token_symbol}, status={self.status})"
        )

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
    
    initiator_token = models.CharField(max_length=255)
    initiator_amount = models.DecimalField(max_digits=20, decimal_places=8)
    counterparty_token = models.CharField(max_length=255)
    counterparty_amount = models.DecimalField(max_digits=20, decimal_places=8)
    
    tx_hash = models.CharField(max_length=100, blank=True)
    completed_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"P2PSwapTransaction({self.initiator.username} <-> {self.counterparty.username})"

class PriceFeedSource(models.Model):
    """Oracle source for price feeds"""
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    oracle_address = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    reputation_score = models.DecimalField(max_digits=5, decimal_places=2, default=100.0)
    total_submissions = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"PriceFeedSource({self.name}, active={self.is_active})"
    
    class Meta:
        ordering = ['-reputation_score', 'name']

class PriceFeedData(models.Model):
    """Individual price submission from an oracle source"""
    source = models.ForeignKey(PriceFeedSource, on_delete=models.CASCADE, related_name='price_submissions')
    token_symbol = models.CharField(max_length=10)
    price_usd = models.DecimalField(max_digits=20, decimal_places=8)
    timestamp = models.DateTimeField(auto_now_add=True)
    block_number = models.IntegerField(null=True, blank=True)
    tx_hash = models.CharField(max_length=100, blank=True)
    
    def __str__(self):
        return f"PriceFeedData({self.token_symbol}=${self.price_usd} by {self.source.name})"
    
    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['token_symbol', '-timestamp']),
        ]

class PriceFeedAggregation(models.Model):
    """Aggregated price feed from multiple oracle sources"""
    token_symbol = models.CharField(max_length=10)
    aggregated_price = models.DecimalField(max_digits=20, decimal_places=8)
    median_price = models.DecimalField(max_digits=20, decimal_places=8)
    min_price = models.DecimalField(max_digits=20, decimal_places=8)
    max_price = models.DecimalField(max_digits=20, decimal_places=8)
    num_sources = models.IntegerField(default=0)
    confidence_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    timestamp = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"PriceFeedAggregation({self.token_symbol}=${self.aggregated_price}, sources={self.num_sources})"
    
    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['token_symbol', '-timestamp']),
        ]

class CollateralAsset(models.Model):
    """Supported collateral assets for lending"""
    token_symbol = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=100)
    collateral_factor = models.DecimalField(max_digits=5, decimal_places=2, default=75.0)  # Max 75% LTV
    liquidation_threshold = models.DecimalField(max_digits=5, decimal_places=2, default=80.0)  # Liquidate at 80% LTV
    liquidation_penalty = models.DecimalField(max_digits=5, decimal_places=2, default=10.0)  # 10% penalty
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"CollateralAsset({self.token_symbol}, factor={self.collateral_factor}%)"
    
    class Meta:
        ordering = ['token_symbol']

class InterestRateConfig(models.Model):
    """Configuration for interest rates in lending pools"""
    token_symbol = models.CharField(max_length=10, unique=True)
    base_rate = models.DecimalField(max_digits=5, decimal_places=2, default=2.0)  # Base APR 2%
    optimal_utilization = models.DecimalField(max_digits=5, decimal_places=2, default=80.0)  # Optimal at 80%
    slope_1 = models.DecimalField(max_digits=5, decimal_places=2, default=4.0)  # Slope before optimal
    slope_2 = models.DecimalField(max_digits=5, decimal_places=2, default=75.0)  # Slope after optimal
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"InterestRateConfig({self.token_symbol}, base={self.base_rate}%)"
    
    def calculate_borrow_rate(self, utilization_rate):
        """Calculate borrow APR based on utilization rate"""
        if utilization_rate <= self.optimal_utilization:
            rate = self.base_rate + (utilization_rate / self.optimal_utilization) * self.slope_1
        else:
            excess = utilization_rate - self.optimal_utilization
            rate = self.base_rate + self.slope_1 + (excess / (Decimal('100') - self.optimal_utilization)) * self.slope_2
        return rate
    
    def calculate_supply_rate(self, utilization_rate, borrow_rate):
        """Calculate supply APR based on utilization and borrow rate"""
        # Supply rate = Borrow rate * Utilization rate * (1 - reserve factor)
        reserve_factor = Decimal('0.10')  # 10% reserve
        return borrow_rate * utilization_rate / Decimal('100') * (Decimal('1') - reserve_factor)

class LendingPool(models.Model):
    """Lending pool for a specific token"""
    token_symbol = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=100)
    total_deposits = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    total_borrows = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    total_reserves = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    interest_rate_config = models.ForeignKey(InterestRateConfig, on_delete=models.PROTECT, related_name='pools')
    last_accrual_time = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"LendingPool({self.token_symbol}, deposits={self.total_deposits}, borrows={self.total_borrows})"
    
    @property
    def available_liquidity(self):
        """Calculate available liquidity for borrowing"""
        return self.total_deposits - self.total_borrows
    
    @property
    def utilization_rate(self):
        """Calculate utilization rate percentage"""
        if self.total_deposits == 0:
            return Decimal('0')
        return (self.total_borrows / self.total_deposits) * Decimal('100')
    
    @property
    def current_borrow_rate(self):
        """Get current borrow APR"""
        return self.interest_rate_config.calculate_borrow_rate(self.utilization_rate)
    
    @property
    def current_supply_rate(self):
        """Get current supply APR"""
        return self.interest_rate_config.calculate_supply_rate(self.utilization_rate, self.current_borrow_rate)
    
    class Meta:
        ordering = ['token_symbol']

class Deposit(models.Model):
    """User deposit in a lending pool"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='deposits')
    pool = models.ForeignKey(LendingPool, on_delete=models.CASCADE, related_name='deposits')
    principal_amount = models.DecimalField(max_digits=30, decimal_places=8)
    accrued_interest = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    last_interest_update = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"Deposit({self.user.username}, {self.principal_amount} {self.pool.token_symbol})"
    
    @property
    def total_balance(self):
        """Total balance including accrued interest"""
        return self.principal_amount + self.accrued_interest
    
    class Meta:
        unique_together = ['user', 'pool']
        ordering = ['-created_at']

class Loan(models.Model):
    """User loan backed by collateral"""
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('repaid', 'Repaid'),
        ('liquidated', 'Liquidated'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='loans')
    pool = models.ForeignKey(LendingPool, on_delete=models.CASCADE, related_name='loans')
    collateral_asset = models.ForeignKey(CollateralAsset, on_delete=models.PROTECT, related_name='loans')
    
    # Loan details
    principal_amount = models.DecimalField(max_digits=30, decimal_places=8)
    accrued_interest = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    collateral_amount = models.DecimalField(max_digits=30, decimal_places=8)
    
    # Status and timestamps
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    last_interest_update = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    repaid_at = models.DateTimeField(null=True, blank=True)
    liquidated_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"Loan({self.user.username}, {self.principal_amount} {self.pool.token_symbol}, status={self.status})"
    
    @property
    def total_debt(self):
        """Total debt including accrued interest"""
        return self.principal_amount + self.accrued_interest
    
    @property
    def health_factor(self):
        """Calculate loan health factor (>1 is healthy, <1 can be liquidated)"""
        # Health Factor = (Collateral Value * Liquidation Threshold) / Total Debt
        # For simplicity, using 1:1 price ratio - in production would use oracle prices
        collateral_value = self.collateral_amount
        threshold = self.collateral_asset.liquidation_threshold / Decimal('100')
        if self.total_debt == 0:
            return Decimal('999')  # Very healthy
        return (collateral_value * threshold) / self.total_debt
    
    class Meta:
        ordering = ['-created_at']

class LoanRepayment(models.Model):
    """Record of loan repayments"""
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name='repayments')
    amount = models.DecimalField(max_digits=30, decimal_places=8)
    interest_paid = models.DecimalField(max_digits=30, decimal_places=8)
    principal_paid = models.DecimalField(max_digits=30, decimal_places=8)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"LoanRepayment({self.loan.id}, amount={self.amount})"
    
    class Meta:
        ordering = ['-created_at']

class Liquidation(models.Model):
    """Record of liquidated loans"""
    loan = models.OneToOneField(Loan, on_delete=models.CASCADE, related_name='liquidation')
    liquidator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='liquidations_performed', null=True, blank=True)
    collateral_seized = models.DecimalField(max_digits=30, decimal_places=8)
    debt_covered = models.DecimalField(max_digits=30, decimal_places=8)
    liquidation_penalty = models.DecimalField(max_digits=30, decimal_places=8)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Liquidation(loan={self.loan.id}, debt_covered={self.debt_covered})"
    
    class Meta:
        ordering = ['-created_at']

class FixedRateBond(models.Model):
    """Fixed-term bond with guaranteed fixed interest rate"""
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('matured', 'Matured'),
        ('redeemed', 'Redeemed'),
    ]
    
    TERM_CHOICES = [
        (30, '30 Days'),
        (90, '90 Days'),
        (180, '180 Days'),
        (365, '1 Year'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='fixed_rate_bonds')
    token_symbol = models.CharField(max_length=10)
    principal_amount = models.DecimalField(max_digits=30, decimal_places=8)
    fixed_rate_apr = models.DecimalField(max_digits=5, decimal_places=2)  # Annual rate
    term_days = models.IntegerField(choices=TERM_CHOICES)
    
    # Calculated fields
    maturity_amount = models.DecimalField(max_digits=30, decimal_places=8)
    expected_interest = models.DecimalField(max_digits=30, decimal_places=8)
    
    # Status and dates
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    issued_at = models.DateTimeField(auto_now_add=True)
    maturity_date = models.DateTimeField()
    redeemed_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"FixedRateBond({self.user.username}, {self.principal_amount} {self.token_symbol} @ {self.fixed_rate_apr}% for {self.term_days} days)"
    
    @property
    def is_matured(self):
        """Check if bond has reached maturity"""
        from django.utils import timezone
        return timezone.now() >= self.maturity_date
    
    @property
    def days_remaining(self):
        """Calculate days until maturity"""
        from django.utils import timezone
        if self.is_matured:
            return 0
        delta = self.maturity_date - timezone.now()
        return max(0, delta.days)
    
    class Meta:
        ordering = ['-issued_at']

class VariableRateSavings(models.Model):
    """Variable rate savings account with dynamic APR based on market conditions"""
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('withdrawn', 'Withdrawn'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='variable_rate_savings')
    pool = models.ForeignKey(LendingPool, on_delete=models.CASCADE, related_name='variable_savings')
    principal_amount = models.DecimalField(max_digits=30, decimal_places=8)
    accrued_interest = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    
    # Rate tracking
    opening_rate = models.DecimalField(max_digits=5, decimal_places=2)
    current_rate = models.DecimalField(max_digits=5, decimal_places=2)
    
    # Status and dates
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    last_rate_update = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"VariableRateSavings({self.user.username}, {self.principal_amount} {self.pool.token_symbol} @ {self.current_rate}%)"
    
    @property
    def total_balance(self):
        """Total balance including accrued interest"""
        return self.principal_amount + self.accrued_interest
    
    class Meta:
        ordering = ['-created_at']

class InterestRateSnapshot(models.Model):
    """Historical snapshot of interest rates for analytics and charts"""
    token_symbol = models.CharField(max_length=10)
    rate_type = models.CharField(max_length=20, choices=[
        ('variable_supply', 'Variable Supply'),
        ('variable_borrow', 'Variable Borrow'),
        ('fixed_30d', 'Fixed 30 Day'),
        ('fixed_90d', 'Fixed 90 Day'),
        ('fixed_180d', 'Fixed 180 Day'),
        ('fixed_365d', 'Fixed 1 Year'),
    ])
    rate_apr = models.DecimalField(max_digits=5, decimal_places=2)
    utilization_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    
    def __str__(self):
        return f"InterestRateSnapshot({self.token_symbol} {self.rate_type} @ {self.rate_apr}%)"
    
    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['token_symbol', 'rate_type', '-timestamp']),
        ]

class DeFiEvent(models.Model):
    """General event log for DeFi actions"""
    EVENT_TYPE_CHOICES = [
        ('swap', 'Swap'),
        ('liquidity_add', 'Liquidity Add'),
        ('liquidity_remove', 'Liquidity Remove'),
        ('borrow', 'Borrow'),
        ('repay', 'Repay'),
        ('liquidation', 'Liquidation'),
        ('bond_purchase', 'Bond Purchase'),
        ('savings_deposit', 'Savings Deposit'),
        ('other', 'Other'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='defi_events')
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES)
    description = models.TextField()
    related_pool = models.ForeignKey(LendingPool, on_delete=models.SET_NULL, null=True, blank=True, related_name='events')
    related_loan = models.ForeignKey(Loan, on_delete=models.SET_NULL, null=True, blank=True, related_name='events')
    related_swap = models.ForeignKey(SwapTransaction, on_delete=models.SET_NULL, null=True, blank=True, related_name='events')
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"DeFiEvent({self.user.username}, {self.event_type}, {self.created_at})"
    
    class Meta:
        ordering = ['-created_at']

class DeFiAnalytics(models.Model):
    """Aggregated analytics data for DeFi activity"""
    date = models.DateField(db_index=True)
    total_volume = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    total_liquidity = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    total_borrowed = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    total_interest_accrued = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    unique_users = models.IntegerField(default=0)
    
    def __str__(self):
        return f"DeFiAnalytics({self.date}, volume={self.total_volume})"
    
    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['date']),
        ]
        
class DeFiConfig(models.Model):
    """Global configuration for DeFi features"""
    key = models.CharField(max_length=100, unique=True)
    value = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"DeFiConfig({self.key}={self.value})"
    
    class Meta:
        ordering = ['key']
        
class DeFiAnnouncement(models.Model):
    """Announcements related to DeFi features"""
    title = models.CharField(max_length=200)
    content = models.TextField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"DeFiAnnouncement({self.title}, active={self.is_active})"
    
    class Meta:
        ordering = ['-created_at']

class DeFiFAQ(models.Model):
    """Frequently Asked Questions related to DeFi features"""
    question = models.CharField(max_length=300)
    answer = models.TextField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"DeFiFAQ({self.question}, active={self.is_active})"
    
    class Meta:
        ordering = ['-created_at']
        
class DeFiTutorial(models.Model):
    """Tutorials for using DeFi features"""
    title = models.CharField(max_length=200)
    content = models.TextField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"DeFiTutorial({self.title}, active={self.is_active})"
    
    class Meta:
        ordering = ['-created_at']

class DeFiGovernanceProposal(models.Model):
    """Governance proposals for DeFi protocol changes"""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('active', 'Active'),
        ('passed', 'Passed'),
        ('rejected', 'Rejected'),
        ('executed', 'Executed'),
    ]
    
    title = models.CharField(max_length=200)
    description = models.TextField()
    proposer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='proposed_governance')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    votes_for = models.IntegerField(default=0)
    votes_against = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"DeFiGovernanceProposal({self.title}, status={self.status})"
    
    class Meta:
        ordering = ['-created_at']

class Contract(models.Model):
    """Smart contract details for DeFi features"""
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=100, unique=True)
    abi = models.TextField()
    network = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"Contract({self.name}, {self.address})"
    
    class Meta:
        ordering = ['name']
        
class ContractAudit(models.Model):
    """Audit reports for DeFi smart contracts"""
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name='audits')
    auditor_name = models.CharField(max_length=100)
    report_url = models.URLField()
    summary = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    def __str__(self):
        return f"ContractAudit({self.contract.name} by {self.auditor_name})"
    
    class Meta:
        ordering = ['-created_at']

class ContractInteraction(models.Model):
    """Record of user interactions with DeFi smart contracts"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='defi_contract_interactions')
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name='interactions')
    method = models.CharField(max_length=100)
    parameters = models.TextField()
    tx_hash = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    def __str__(self):
        return f"ContractInteraction({self.user.username} -> {self.contract.name}.{self.method})"
    
    class Meta:
        ordering = ['-created_at']