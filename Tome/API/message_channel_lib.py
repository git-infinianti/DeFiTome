import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from API.channel_event_protocol import (
    add_payload_checksum,
    event_payload_checksum,
    validate_channel_event_payload,
)

STRICT_STAGE_SEQUENCE = [
    'offer_created',
    'market_created',
    'order_created',
    'settlement_lock_created',
    'settlement_build_failed',
    'settlement_pending_reconciliation',
    'settlement_broadcasted',
    'swap_cancelled',
    'swap_expired',
    'game_instance_created',
    'game_spend_recorded',
    'game_reward_distributed',
    'payout_policy_published',
]

REQUIRED_PAYLOAD_KEYS = {
    'event_type',
    'event_version',
    'event_id',
    'created_at',
    'network_mode',
    'swap_offer_id',
    'stage',
    'initiator',
    'counterparty',
    'offer_token',
    'offer_amount',
    'request_token',
    'request_amount',
    'txid',
    'details',
}

KEY_PATTERN = re.compile(r'^[a-z0-9_]{2,64}$')


def canonical_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(',', ':'))


def payload_checksum(payload):
    if isinstance(payload, dict) and {'aggregate_id', 'payload_checksum'}.issubset(payload):
        return event_payload_checksum(payload)
    encoded = canonical_json(payload).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def validate_channel_key(channel_key):
    normalized = str(channel_key or '').strip().lower()
    if not KEY_PATTERN.match(normalized):
        raise ValueError('channel_key must be 2-64 chars of lowercase letters, digits, and underscore.')
    return normalized


def validate_stage(stage, allowed_stages):
    normalized = str(stage or '').strip().lower()
    if not normalized:
        raise ValueError('stage is required.')
    allowed = {str(item).strip().lower() for item in (allowed_stages or [])}
    if allowed and normalized not in allowed:
        raise ValueError(f'stage "{normalized}" is not allowed for this channel policy.')
    if normalized not in STRICT_STAGE_SEQUENCE:
        raise ValueError(f'stage "{normalized}" is not recognized by strict console rules.')
    return normalized


def validate_console_payload(payload, allowed_stages):
    if not isinstance(payload, dict):
        raise ValueError('payload must be a JSON object.')

    if 'aggregate_id' in payload:
        validate_channel_event_payload(payload, allowed_stages)
        if str(payload.get('event_type', '')).strip().lower() != 'atomic_swap_transfer':
            raise ValueError('event_type must be atomic_swap_transfer.')
        if str(payload.get('aggregate_type', '')).strip().lower() != 'atomic_swap':
            raise ValueError('aggregate_type must be atomic_swap.')
        return payload

    payload_keys = set(payload.keys())
    unexpected = payload_keys - REQUIRED_PAYLOAD_KEYS
    missing = REQUIRED_PAYLOAD_KEYS - payload_keys
    if missing:
        raise ValueError(f'payload is missing required keys: {sorted(missing)}')
    if unexpected:
        raise ValueError(f'payload contains unsupported keys: {sorted(unexpected)}')

    if str(payload.get('event_type', '')).strip().lower() != 'atomic_swap_transfer':
        raise ValueError('event_type must be atomic_swap_transfer.')

    stage = validate_stage(payload.get('stage'), allowed_stages)
    payload['stage'] = stage

    version = int(payload.get('event_version', 0))
    if version != 1:
        raise ValueError('event_version must be 1 for strict console mode.')

    swap_offer_id = int(payload.get('swap_offer_id', 0))
    if swap_offer_id <= 0:
        raise ValueError('swap_offer_id must be a positive integer.')

    network_mode = str(payload.get('network_mode', '')).strip().lower()
    if network_mode not in {'mainnet', 'testnet'}:
        raise ValueError('network_mode must be mainnet or testnet.')

    return payload


def build_swap_transfer_payload(
    swap_offer,
    stage,
    actor_username,
    txid='',
    details=None,
    aggregate_sequence=None,
):
    now = datetime.now(timezone.utc).isoformat()
    sequence = int(aggregate_sequence or 0)
    if sequence < 1:
        raise ValueError('Atomic swap channel events require a positive aggregate sequence.')
    aggregate_id = str(getattr(swap_offer, 'reconciliation_id', '') or '').strip()
    if not aggregate_id:
        raise ValueError('Atomic swap channel events require a persisted reconciliation identifier.')

    payload = {
        'event_type': 'atomic_swap_transfer',
        'event_version': 1,
        'event_id': str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f'defitome:{swap_offer.network_mode}:atomic_swap:{aggregate_id}:{sequence}',
        )),
        'created_at': now,
        'network_mode': str(swap_offer.network_mode or 'testnet').lower(),
        'aggregate_type': 'atomic_swap',
        'aggregate_id': aggregate_id,
        'aggregate_sequence': sequence,
        'stage': str(stage or '').strip().lower(),
        'correlation_id': aggregate_id,
        'transaction_id': str(txid or ''),
        'details': {
            'actor': str(actor_username or ''),
            'legacy_swap_offer_id': int(swap_offer.id),
            'participants': {
                'initiator_username': str(getattr(swap_offer.initiator, 'username', '') or ''),
                'counterparty_username': str(getattr(swap_offer.counterparty, 'username', '') or ''),
            },
            'offer': {
                'token': str(swap_offer.offer_token or ''),
                'amount': str(swap_offer.offer_amount),
            },
            'request': {
                'token': str(swap_offer.request_token or ''),
                'amount': str(swap_offer.request_amount),
            },
            'context': details or {},
        },
    }
    return add_payload_checksum(payload)


def build_market_event_payload(trading_pair, stage, actor_username, order=None, details=None):
    now = datetime.now(timezone.utc)
    normalized_stage = str(stage or '').strip().lower()
    network_mode = str(trading_pair.network_mode or 'testnet').lower()
    if order is not None:
        aggregate_type = 'dex_order'
        aggregate_source = f'{trading_pair.id}:{order.id}'
    else:
        aggregate_type = 'dex_market'
        aggregate_source = str(trading_pair.id)
    aggregate_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f'defitome:{network_mode}:{aggregate_type}:{aggregate_source}',
    ))
    aggregate_sequence = 1
    event_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f'defitome:{network_mode}:{aggregate_type}:{aggregate_id}:{aggregate_sequence}',
    ))
    event_details = {
        'actor': str(actor_username or ''),
        'market': {
            'trading_pair_id': int(trading_pair.id),
            'base_token': str(trading_pair.base_token or ''),
            'quote_token': str(trading_pair.quote_token or ''),
        },
        'context': details or {},
    }
    if order is not None:
        event_details['order'] = {
            'id': int(order.id),
            'side': str(order.side or ''),
            'price': str(order.price),
            'quantity': str(order.quantity),
            'status': str(order.status or ''),
        }
    payload = {
        'event_type': 'dex_market_event',
        'event_version': 1,
        'event_id': event_id,
        'created_at': now.isoformat(),
        'network_mode': network_mode,
        'aggregate_type': aggregate_type,
        'aggregate_id': aggregate_id,
        'aggregate_sequence': aggregate_sequence,
        'stage': normalized_stage,
        'correlation_id': aggregate_id,
        'details': event_details,
    }
    return add_payload_checksum(payload)


def validate_market_event_payload(payload, allowed_stages):
    validate_channel_event_payload(payload, allowed_stages)
    if str(payload.get('event_type') or '').strip().lower() != 'dex_market_event':
        raise ValueError('event_type must be dex_market_event.')
    stage = str(payload['stage']).strip().lower()
    if stage not in {'market_created', 'order_created'}:
        raise ValueError(f'stage "{stage}" is not a market event stage.')
    expected_aggregate_type = 'dex_order' if stage == 'order_created' else 'dex_market'
    if str(payload.get('aggregate_type') or '').strip().lower() != expected_aggregate_type:
        raise ValueError(f'{stage} requires aggregate_type {expected_aggregate_type}.')
    details = payload.get('details') or {}
    market = details.get('market') if isinstance(details, dict) else None
    if not isinstance(market, dict) or int(market.get('trading_pair_id', 0)) <= 0:
        raise ValueError('Market event details require a positive trading_pair_id.')
    if stage == 'order_created':
        order = details.get('order') if isinstance(details, dict) else None
        if not isinstance(order, dict) or int(order.get('id', 0)) <= 0:
            raise ValueError('Order-created event details require a positive order id.')
    return payload
