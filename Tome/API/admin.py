from django.contrib import admin
from .models import (
    SolidityContract,
    ContractInteraction,
    ContractAsset,
    APIKey,
    MessageChannelPolicy,
    AtomicSwapTransferMessage,
)


@admin.register(SolidityContract)
class SolidityContractAdmin(admin.ModelAdmin):
    """Admin interface for Solidity contracts"""
    list_display = ['name', 'contract_address', 'deployer', 'deployment_block', 'is_active', 'created_at']
    list_filter = ['is_active', 'created_at', 'deployer']
    search_fields = ['name', 'contract_address', 'description']
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        ('Contract Information', {
            'fields': ('name', 'contract_address', 'description', 'is_active')
        }),
        ('Source Code', {
            'fields': ('source_code', 'bytecode', 'abi'),
            'classes': ('collapse',)
        }),
        ('Deployment', {
            'fields': ('deployer', 'deployment_tx', 'deployment_block', 'ipfs_hash')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(ContractInteraction)
class ContractInteractionAdmin(admin.ModelAdmin):
    """Admin interface for contract interactions"""
    list_display = ['contract', 'user', 'function_name', 'success', 'tx_hash', 'created_at']
    list_filter = ['success', 'created_at', 'contract']
    search_fields = ['function_name', 'tx_hash', 'contract__name']
    readonly_fields = ['created_at']
    
    fieldsets = (
        ('Interaction Details', {
            'fields': ('contract', 'user', 'function_name', 'parameters')
        }),
        ('Transaction', {
            'fields': ('tx_hash', 'block_height', 'success')
        }),
        ('Result', {
            'fields': ('result', 'error_message')
        }),
        ('Timestamps', {
            'fields': ('created_at',)
        }),
    )


@admin.register(ContractAsset)
class ContractAssetAdmin(admin.ModelAdmin):
    """Admin interface for contract assets"""
    list_display = ['asset_name', 'contract', 'quantity', 'units', 'reissuable', 'created_at']
    list_filter = ['reissuable', 'has_ipfs', 'created_at']
    search_fields = ['asset_name', 'contract__name']
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        ('Asset Information', {
            'fields': ('contract', 'asset_name', 'quantity', 'units')
        }),
        ('Properties', {
            'fields': ('reissuable', 'has_ipfs', 'ipfs_hash')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )



@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    """Admin interface for API keys"""
    list_display = ['name', 'key_prefix', 'user', 'is_active', 'last_used', 'created_at']
    list_filter = ['is_active', 'created_at', 'user']
    search_fields = ['name', 'key_prefix', 'user__username']
    readonly_fields = ['key_hash', 'key_prefix', 'created_at', 'updated_at', 'last_used']
    
    fieldsets = (
        ('API Key Information', {
            'fields': ('name', 'user', 'key_prefix', 'is_active')
        }),
        ('Key Details', {
            'fields': ('key_hash',),
            'classes': ('collapse',)
        }),
        ('Usage', {
            'fields': ('last_used',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(MessageChannelPolicy)
class MessageChannelPolicyAdmin(admin.ModelAdmin):
    list_display = [
        'channel_key',
        'channel_name',
        'network_mode',
        'version',
        'status',
        'chain_metadata_status',
        'owner_account',
        'manager_account',
        'is_locked',
    ]
    list_filter = ['network_mode', 'status', 'chain_metadata_status', 'is_locked', 'created_at']
    search_fields = ['channel_key', 'channel_name', 'owner_account__username', 'manager_account__username']
    readonly_fields = [
        'rules_checksum',
        'metadata_ipfs_cid',
        'issuance_txid',
        'chain_metadata_status',
        'chain_metadata_error',
        'chain_metadata_checked_at',
        'revision_burn_txid',
        'revision_burned_at',
        'created_at',
        'updated_at',
    ]


@admin.register(AtomicSwapTransferMessage)
class AtomicSwapTransferMessageAdmin(admin.ModelAdmin):
    list_display = [
        'swap_offer',
        'stage',
        'status',
        'policy',
        'payload_ipfs_cid',
        'created_at',
    ]
    list_filter = ['status', 'stage', 'created_at']
    search_fields = ['swap_offer__id', 'stage', 'payload_ipfs_cid', 'broadcast_result']
    readonly_fields = ['payload_checksum', 'created_at']
