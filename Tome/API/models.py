from django.db import models
from django.contrib.auth.models import User
import secrets
import hashlib
import json


class SolidityContract(models.Model):
    """
    Model to store Solidity contract deployments for Evrmore stateless contracts.
    This represents a contract deployed on the Evrmore blockchain.
    """
    # Contract identification
    name = models.CharField(max_length=255, help_text="Contract name")
    contract_address = models.CharField(max_length=255, unique=True, help_text="Evrmore asset name or contract identifier")
    
    # Contract source and ABI
    source_code = models.TextField(blank=True, help_text="Solidity source code")
    bytecode = models.TextField(blank=True, help_text="Compiled bytecode")
    abi = models.JSONField(default=list, help_text="Contract ABI (Application Binary Interface)")
    
    # Deployment information
    deployer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='deployed_contracts')
    deployment_tx = models.CharField(max_length=255, blank=True, help_text="Transaction hash of deployment")
    deployment_block = models.IntegerField(null=True, blank=True, help_text="Block height of deployment")
    
    # Contract metadata
    description = models.TextField(blank=True, help_text="Contract description")
    ipfs_hash = models.CharField(max_length=255, blank=True, help_text="IPFS hash for contract metadata")
    
    # Contract state
    is_active = models.BooleanField(default=True, help_text="Whether contract is active")
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['contract_address']),
            models.Index(fields=['deployer', '-created_at']),
        ]
    
    def __str__(self):
        return f"{self.name} ({self.contract_address})"


class ContractInteraction(models.Model):
    """
    Model to track interactions with deployed contracts.
    This logs function calls and transactions to contracts.
    """
    contract = models.ForeignKey(SolidityContract, on_delete=models.CASCADE, related_name='interactions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='api_contract_interactions')
    
    # Interaction details
    function_name = models.CharField(max_length=255, help_text="Name of function called")
    parameters = models.JSONField(default=dict, help_text="Function parameters")
    
    # Transaction details
    tx_hash = models.CharField(max_length=255, blank=True, help_text="Transaction hash")
    block_height = models.IntegerField(null=True, blank=True, help_text="Block height")
    
    # Result
    success = models.BooleanField(default=False, help_text="Whether interaction was successful")
    result = models.JSONField(default=dict, help_text="Interaction result")
    error_message = models.TextField(blank=True, help_text="Error message if failed")
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['contract', '-created_at']),
            models.Index(fields=['user', '-created_at']),
        ]
    
    def __str__(self):
        return f"{self.function_name} on {self.contract.name} by {self.user.username}"


class ContractAsset(models.Model):
    """
    Model to link Evrmore assets to smart contracts.
    Represents assets managed by or related to a contract.
    """
    contract = models.ForeignKey(SolidityContract, on_delete=models.CASCADE, related_name='assets')
    asset_name = models.CharField(max_length=255, help_text="Evrmore asset name")
    
    # Asset properties
    quantity = models.DecimalField(max_digits=30, decimal_places=8, help_text="Total quantity")
    units = models.IntegerField(default=0, help_text="Divisibility (0-8)")
    reissuable = models.BooleanField(default=False, help_text="Whether asset can be reissued")
    has_ipfs = models.BooleanField(default=False, help_text="Whether asset has IPFS metadata")
    ipfs_hash = models.CharField(max_length=255, blank=True, help_text="IPFS hash")
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        unique_together = [['contract', 'asset_name']]
        indexes = [
            models.Index(fields=['asset_name']),
            models.Index(fields=['contract', '-created_at']),
        ]
    
    def __str__(self):
        return f"{self.asset_name} (Contract: {self.contract.name})"


class APIKey(models.Model):
    """
    Model to store API keys for authenticated API access.
    Each user can have multiple API keys for different applications.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='api_keys')
    name = models.CharField(max_length=255, help_text="Descriptive name for this API key")
    
    # Store hashed key for security
    key_hash = models.CharField(max_length=64, unique=True, help_text="SHA256 hash of the API key")
    
    # Display prefix (first 8 chars) for user reference
    key_prefix = models.CharField(max_length=8, help_text="First 8 characters of key for identification")
    
    # Key status
    is_active = models.BooleanField(default=True, help_text="Whether this API key is active")
    
    # Usage tracking
    last_used = models.DateTimeField(null=True, blank=True, help_text="Last time this key was used")
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['key_hash']),
            models.Index(fields=['user', '-created_at']),
        ]
    
    def __str__(self):
        return f"{self.name} ({self.key_prefix}...)"
    
    @staticmethod
    def generate_key():
        """Generate a secure random API key"""
        return secrets.token_urlsafe(32)
    
    @staticmethod
    def hash_key(key):
        """Hash an API key using SHA256"""
        return hashlib.sha256(key.encode()).hexdigest()
    
    def verify_key(self, key):
        """Verify if a provided key matches this API key"""
        return self.key_hash == self.hash_key(key)


class MessageChannelPolicy(models.Model):
    """Versioned policy for strict message-channel broadcasts."""

    NETWORK_MODE_CHOICES = [
        ('mainnet', 'Mainnet'),
        ('testnet', 'Testnet'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('deprecated', 'Deprecated'),
    ]
    CHAIN_METADATA_STATUS_PENDING = 'pending'
    CHAIN_METADATA_STATUS_VERIFIED = 'verified'
    CHAIN_METADATA_STATUS_MISSING = 'metadata_missing'
    CHAIN_METADATA_STATUS_INVALID = 'invalid'
    CHAIN_METADATA_STATUS_CHOICES = [
        (CHAIN_METADATA_STATUS_PENDING, 'Pending confirmation'),
        (CHAIN_METADATA_STATUS_VERIFIED, 'Verified'),
        (CHAIN_METADATA_STATUS_MISSING, 'Metadata missing'),
        (CHAIN_METADATA_STATUS_INVALID, 'Invalid metadata'),
    ]

    channel_key = models.CharField(max_length=64)
    channel_name = models.CharField(max_length=255)
    network_mode = models.CharField(max_length=10, choices=NETWORK_MODE_CHOICES, default='testnet')
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='active')

    owner_account = models.ForeignKey(User, on_delete=models.PROTECT, related_name='owned_message_channel_policies')
    manager_account = models.ForeignKey(User, on_delete=models.PROTECT, related_name='managed_message_channel_policies')

    schema_name = models.CharField(max_length=120, default='defitome.atomic-swap-transfer-message')
    schema_version = models.PositiveIntegerField(default=1)
    allowed_stages = models.JSONField(default=list)
    strict_rules = models.JSONField(default=dict)
    rules_checksum = models.CharField(max_length=64, blank=True)
    metadata_ipfs_cid = models.CharField(max_length=255, blank=True)
    issuance_txid = models.CharField(max_length=100, blank=True)
    chain_metadata_status = models.CharField(
        max_length=24,
        choices=CHAIN_METADATA_STATUS_CHOICES,
        default='pending',
        db_index=True,
    )
    chain_metadata_error = models.TextField(blank=True)
    chain_metadata_checked_at = models.DateTimeField(null=True, blank=True)
    revision_burn_txid = models.CharField(max_length=100, blank=True)
    revision_burned_at = models.DateTimeField(null=True, blank=True)

    is_locked = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('channel_key', '-version')
        constraints = [
            models.UniqueConstraint(
                fields=('channel_key', 'network_mode', 'version'),
                name='message_channel_policy_key_network_version_unique',
            ),
        ]
        indexes = [
            models.Index(fields=('channel_key', 'network_mode', 'status')),
            models.Index(fields=('owner_account', 'network_mode')),
        ]

    def __str__(self):
        return (
            f"MessageChannelPolicy(key={self.channel_key}, network={self.network_mode}, "
            f"version={self.version}, status={self.status})"
        )

    def save(self, *args, **kwargs):
        rules_payload = {
            'channel_key': self.channel_key,
            'channel_name': self.channel_name,
            'network_mode': self.network_mode,
            'version': self.version,
            'schema_name': self.schema_name,
            'schema_version': self.schema_version,
            'allowed_stages': self.allowed_stages,
            'strict_rules': self.strict_rules,
            'is_locked': self.is_locked,
        }
        canonical = json.dumps(rules_payload, sort_keys=True, separators=(',', ':'))
        self.rules_checksum = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        super().save(*args, **kwargs)


class AtomicSwapTransferMessage(models.Model):
    """Audit log for strict atomic-swap transfer stage messaging."""

    STATUS_CHOICES = [
        ('recorded', 'Recorded'),
        ('broadcasted', 'Broadcasted'),
        ('failed', 'Failed'),
        ('skipped', 'Skipped'),
    ]

    policy = models.ForeignKey(
        MessageChannelPolicy,
        on_delete=models.PROTECT,
        related_name='messages',
        null=True,
        blank=True,
    )
    swap_offer = models.ForeignKey('DeFi.SwapOffer', on_delete=models.CASCADE, related_name='transfer_messages')
    stage = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    payload_checksum = models.CharField(max_length=64)
    payload_ipfs_cid = models.CharField(max_length=255, blank=True)
    broadcast_result = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='recorded')
    error_message = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('swap_offer', 'stage', '-created_at')),
            models.Index(fields=('status', '-created_at')),
        ]

    def __str__(self):
        return (
            f"AtomicSwapTransferMessage(swap_offer={self.swap_offer_id}, stage={self.stage}, "
            f"status={self.status})"
        )


class DexMarketEventMessage(models.Model):
    """Audit and broadcast result for market and order publication events."""

    STATUS_CHOICES = AtomicSwapTransferMessage.STATUS_CHOICES

    policy = models.ForeignKey(
        MessageChannelPolicy,
        on_delete=models.PROTECT,
        related_name='market_messages',
        null=True,
        blank=True,
    )
    trading_pair_id = models.PositiveIntegerField()
    order_id = models.PositiveIntegerField(null=True, blank=True)
    stage = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    payload_checksum = models.CharField(max_length=64)
    payload_ipfs_cid = models.CharField(max_length=255, blank=True)
    broadcast_result = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='recorded')
    error_message = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('trading_pair_id', 'stage', '-created_at')),
            models.Index(fields=('status', '-created_at')),
        ]

    def __str__(self):
        return f"DexMarketEventMessage(pair={self.trading_pair_id}, stage={self.stage}, status={self.status})"
