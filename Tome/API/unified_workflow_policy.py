"""Shared immutable-policy defaults for the DeFiTome workflow channel."""

UNIFIED_WORKFLOW_CHANNEL_KEY = 'tome0808_swapflow'
UNIFIED_WORKFLOW_POLICY_VERSION = 5
UNIFIED_WORKFLOW_CHANNEL_TAG = f'SWAPFLOWV{UNIFIED_WORKFLOW_POLICY_VERSION}'
UNIFIED_WORKFLOW_DESCRIPTION = (
    'Unified v5 DeFiTome messaging console for replayable swap, market, and DEC lifecycle events.'
)
UNIFIED_WORKFLOW_STRICT_RULES = {
    'console_mode': 'strict',
    'immutable_payload': True,
    'allow_unregistered_keys': False,
    'event_protocol': 'defitome.channel-event',
    'event_protocol_version': 1,
    'event_checksum': 'sha256',
    'auto_broadcast': False,
    'reconciliation_source': 'channel_asset_lineage',
    'reconciliation_order': [
        'block_height',
        'block_transaction_index',
        'channel_output_index',
    ],
    'reconciliation_fail_closed': True,
}