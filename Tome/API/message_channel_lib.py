import hashlib
import json
import re
from datetime import datetime, timezone

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


def build_swap_transfer_payload(swap_offer, stage, actor_username, txid='', details=None):
    now = datetime.now(timezone.utc).isoformat()
    event_id = f'swap-{swap_offer.id}-{stage}-{int(datetime.now(timezone.utc).timestamp())}'
    return {
        'event_type': 'atomic_swap_transfer',
        'event_version': 1,
        'event_id': event_id,
        'created_at': now,
        'network_mode': str(swap_offer.network_mode or 'testnet').lower(),
        'swap_offer_id': int(swap_offer.id),
        'stage': str(stage or '').strip().lower(),
        'initiator': str(getattr(swap_offer.initiator, 'username', '') or ''),
        'counterparty': str(getattr(swap_offer.counterparty, 'username', '') or ''),
        'offer_token': str(swap_offer.offer_token or ''),
        'offer_amount': str(swap_offer.offer_amount),
        'request_token': str(swap_offer.request_token or ''),
        'request_amount': str(swap_offer.request_amount),
        'txid': str(txid or ''),
        'details': details or {
            'actor': str(actor_username or ''),
        },
    }


def build_market_event_payload(trading_pair, stage, actor_username, order=None, details=None):
    now = datetime.now(timezone.utc)
    normalized_stage = str(stage or '').strip().lower()
    event_id = f'market-{trading_pair.id}-{normalized_stage}-{int(now.timestamp())}'
    payload = {
        'event_type': 'dex_market_event',
        'event_version': 1,
        'event_id': event_id,
        'created_at': now.isoformat(),
        'network_mode': str(trading_pair.network_mode or 'testnet').lower(),
        'stage': normalized_stage,
        'trading_pair_id': int(trading_pair.id),
        'base_token': str(trading_pair.base_token or ''),
        'quote_token': str(trading_pair.quote_token or ''),
        'actor': str(actor_username or ''),
        'details': details or {},
    }
    if order is not None:
        payload['order'] = {
            'id': int(order.id),
            'side': str(order.side or ''),
            'price': str(order.price),
            'quantity': str(order.quantity),
            'status': str(order.status or ''),
        }
    return payload


def validate_market_event_payload(payload, allowed_stages):
    if not isinstance(payload, dict):
        raise ValueError('payload must be a JSON object.')
    if str(payload.get('event_type') or '').strip().lower() != 'dex_market_event':
        raise ValueError('event_type must be dex_market_event.')
    if int(payload.get('event_version', 0)) != 1:
        raise ValueError('event_version must be 1 for strict console mode.')
    if int(payload.get('trading_pair_id', 0)) <= 0:
        raise ValueError('trading_pair_id must be a positive integer.')
    network_mode = str(payload.get('network_mode') or '').strip().lower()
    if network_mode not in {'mainnet', 'testnet'}:
        raise ValueError('network_mode must be mainnet or testnet.')
    stage = validate_stage(payload.get('stage'), allowed_stages)
    if stage not in {'market_created', 'order_created'}:
        raise ValueError(f'stage "{stage}" is not a market event stage.')
    payload['stage'] = stage
    return payload
