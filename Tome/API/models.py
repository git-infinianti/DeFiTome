from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
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


class ChannelConsumer(models.Model):
    """A node or deployment that independently consumes verified channel events."""

    NETWORK_MODE_CHOICES = MessageChannelPolicy.NETWORK_MODE_CHOICES

    network_mode = models.CharField(max_length=10, choices=NETWORK_MODE_CHOICES, db_index=True)
    consumer_key = models.CharField(max_length=128)
    display_name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('network_mode', 'consumer_key')
        constraints = [
            models.UniqueConstraint(
                fields=('network_mode', 'consumer_key'),
                name='channel_consumer_network_key_unique',
            ),
        ]

    def __str__(self):
        return f"ChannelConsumer(network={self.network_mode}, key={self.consumer_key})"


class ChannelSubscription(models.Model):
    """A user's opt-in to observe a verified message-channel policy."""

    ROLE_OBSERVER = 'observer'
    ROLE_PARTICIPANT = 'participant'
    ROLE_MANAGER = 'manager'
    ROLE_CHOICES = [
        (ROLE_OBSERVER, 'Observer'),
        (ROLE_PARTICIPANT, 'Participant'),
        (ROLE_MANAGER, 'Manager'),
    ]

    STATUS_ACTIVE = 'active'
    STATUS_PAUSED = 'paused'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'Active'),
        (STATUS_PAUSED, 'Paused'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='channel_subscriptions')
    policy = models.ForeignKey(
        MessageChannelPolicy,
        on_delete=models.PROTECT,
        related_name='subscriptions',
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_OBSERVER)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('policy__channel_key', 'user__username')
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'policy'),
                name='channel_subscription_user_policy_unique',
            ),
        ]
        indexes = [
            models.Index(fields=('policy', 'status'), name='API_channe_policy__f6d518_idx'),
            models.Index(fields=('user', 'status'), name='API_channe_user_id_c1e1b1_idx'),
        ]

    def __str__(self):
        return f"ChannelSubscription(user={self.user_id}, policy={self.policy_id}, status={self.status})"


class ChannelEvent(models.Model):
    """An immutable, validated observation of a confirmed channel event."""

    VERIFICATION_VERIFIED = 'verified'
    VERIFICATION_INVALID = 'invalid'
    VERIFICATION_CHOICES = [
        (VERIFICATION_VERIFIED, 'Verified'),
        (VERIFICATION_INVALID, 'Invalid'),
    ]

    policy = models.ForeignKey(
        MessageChannelPolicy,
        on_delete=models.PROTECT,
        related_name='channel_events',
    )
    event_id = models.CharField(max_length=128)
    event_type = models.CharField(max_length=80)
    event_version = models.PositiveSmallIntegerField()
    aggregate_type = models.CharField(max_length=80)
    aggregate_id = models.CharField(max_length=128)
    aggregate_sequence = models.PositiveIntegerField()
    stage = models.CharField(max_length=64)
    network_mode = models.CharField(max_length=10, choices=MessageChannelPolicy.NETWORK_MODE_CHOICES)
    payload = models.JSONField(default=dict)
    payload_checksum = models.CharField(max_length=64)
    payload_ipfs_cid = models.CharField(max_length=255)
    channel_txid = models.CharField(max_length=100)
    channel_output_index = models.PositiveIntegerField()
    block_height = models.PositiveIntegerField()
    block_transaction_index = models.PositiveIntegerField()
    block_hash = models.CharField(max_length=100)
    confirmed_at = models.DateTimeField()
    verification_status = models.CharField(
        max_length=16,
        choices=VERIFICATION_CHOICES,
        default=VERIFICATION_VERIFIED,
    )
    verification_error = models.TextField(blank=True)
    raw_observation = models.JSONField(default=dict)
    observed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('block_height', 'block_transaction_index', 'channel_output_index')
        constraints = [
            models.UniqueConstraint(
                fields=('policy', 'event_id'),
                name='channel_event_policy_event_id_unique',
            ),
            models.UniqueConstraint(
                fields=('policy', 'channel_txid', 'channel_output_index'),
                name='channel_event_policy_tx_output_unique',
            ),
            models.UniqueConstraint(
                fields=('policy', 'aggregate_type', 'aggregate_id', 'aggregate_sequence'),
                name='channel_event_policy_aggregate_sequence_unique',
            ),
        ]
        indexes = [
            models.Index(fields=('policy', 'block_height', 'block_transaction_index'), name='API_channe_policy__8de4d7_idx'),
            models.Index(fields=('aggregate_type', 'aggregate_id', 'aggregate_sequence'), name='API_channe_aggreg_127d41_idx'),
            models.Index(fields=('verification_status', 'network_mode'), name='API_channe_verific_a5d2f1_idx'),
        ]

    def clean(self):
        from API.channel_event_protocol import event_payload_checksum, validate_channel_event_payload

        validate_channel_event_payload(self.payload, self.policy.allowed_stages)
        if self.network_mode != self.policy.network_mode:
            raise ValidationError('Channel event network must match its policy network.')
        if self.event_id != str(self.payload.get('event_id') or ''):
            raise ValidationError('Channel event id must match the canonical payload.')
        if self.payload_checksum != event_payload_checksum(self.payload):
            raise ValidationError('Channel event payload checksum does not match canonical content.')

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Channel events are immutable; record a reconciliation issue instead.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"ChannelEvent(policy={self.policy_id}, event={self.event_id}, stage={self.stage})"


class ChannelEventApplication(models.Model):
    """Idempotent projection result for a verified channel event."""

    STATUS_APPLIED = 'applied'
    STATUS_ALREADY_APPLIED = 'already_applied'
    STATUS_BLOCKED = 'blocked'
    STATUS_CHOICES = [
        (STATUS_APPLIED, 'Applied'),
        (STATUS_ALREADY_APPLIED, 'Already applied'),
        (STATUS_BLOCKED, 'Blocked'),
    ]

    event = models.ForeignKey(ChannelEvent, on_delete=models.PROTECT, related_name='applications')
    projection_type = models.CharField(max_length=80)
    projection_key = models.CharField(max_length=128)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    result = models.JSONField(default=dict)
    error_message = models.TextField(blank=True)
    applied_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('event__block_height', 'event__block_transaction_index', 'event__channel_output_index')
        constraints = [
            models.UniqueConstraint(
                fields=('event', 'projection_type', 'projection_key'),
                name='channel_event_application_unique',
            ),
        ]

    def __str__(self):
        return f"ChannelEventApplication(event={self.event_id}, status={self.status})"


class ChannelSubscriptionCursor(models.Model):
    """A consumer's durable position for a user's channel subscription."""

    STATUS_SYNCED = 'synced'
    STATUS_LAGGING = 'lagging'
    STATUS_ERROR = 'error'
    STATUS_CHOICES = [
        (STATUS_SYNCED, 'Synced'),
        (STATUS_LAGGING, 'Lagging'),
        (STATUS_ERROR, 'Error'),
    ]

    subscription = models.ForeignKey(
        ChannelSubscription,
        on_delete=models.CASCADE,
        related_name='cursors',
    )
    consumer = models.ForeignKey(ChannelConsumer, on_delete=models.PROTECT, related_name='cursors')
    last_event = models.ForeignKey(
        ChannelEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    last_seen_txid = models.CharField(max_length=100, blank=True)
    last_seen_height = models.PositiveIntegerField(null=True, blank=True)
    last_seen_transaction_index = models.PositiveIntegerField(null=True, blank=True)
    last_seen_output_index = models.PositiveIntegerField(null=True, blank=True)
    last_seen_event_id = models.CharField(max_length=128, blank=True)
    last_reconciled_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_LAGGING)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('subscription_id', 'consumer_id')
        constraints = [
            models.UniqueConstraint(
                fields=('subscription', 'consumer'),
                name='channel_subscription_cursor_unique',
            ),
        ]

    def __str__(self):
        return f"ChannelSubscriptionCursor(subscription={self.subscription_id}, consumer={self.consumer_id})"


class ChannelReconciliationIssue(models.Model):
    """A durable, reviewable failure to reconcile channel evidence and a projection."""

    SEVERITY_WARNING = 'warning'
    SEVERITY_ERROR = 'error'
    SEVERITY_CRITICAL = 'critical'
    SEVERITY_CHOICES = [
        (SEVERITY_WARNING, 'Warning'),
        (SEVERITY_ERROR, 'Error'),
        (SEVERITY_CRITICAL, 'Critical'),
    ]

    STATUS_OPEN = 'open'
    STATUS_RESOLVED = 'resolved'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open'),
        (STATUS_RESOLVED, 'Resolved'),
    ]

    policy = models.ForeignKey(
        MessageChannelPolicy,
        on_delete=models.PROTECT,
        related_name='reconciliation_issues',
    )
    event = models.ForeignKey(
        ChannelEvent,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='reconciliation_issues',
    )
    aggregate_type = models.CharField(max_length=80, blank=True)
    aggregate_id = models.CharField(max_length=128, blank=True)
    code = models.CharField(max_length=80)
    severity = models.CharField(max_length=16, choices=SEVERITY_CHOICES, default=SEVERITY_ERROR)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('status', '-created_at')
        indexes = [
            models.Index(fields=('policy', 'status', '-created_at'), name='API_channe_policy__4c7c94_idx'),
            models.Index(fields=('aggregate_type', 'aggregate_id', 'status'), name='API_channe_aggreg_b7e549_idx'),
        ]

    def __str__(self):
        return f"ChannelReconciliationIssue(policy={self.policy_id}, code={self.code}, status={self.status})"
