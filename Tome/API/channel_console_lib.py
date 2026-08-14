import json
import re

CONSOLE_CHANNEL_SCHEMA = 'defitome.messaging-channel-console'
CONSOLE_CHANNEL_SCHEMA_VERSION = 1

CHANNEL_TAG_PATTERN = re.compile(r'^[A-Za-z0-9_\-]{1,32}$')
CHANNEL_KEY_PATTERN = re.compile(r'^[a-z0-9_]{2,64}$')
CHANNEL_ASSET_NAME_PATTERN = re.compile(r'^[A-Z0-9._/]+~[A-Za-z0-9_\-]{1,32}$')
MAX_EVRMORE_ASSET_NAME_LENGTH = 30


DEFAULT_ALLOWED_STAGES = [
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


def normalize_admin_asset(admin_asset):
    value = str(admin_asset or '').strip().upper()
    if not value or not value.endswith('!'):
        raise ValueError('admin_asset must be an admin asset symbol ending with !')
    if any(char.isspace() for char in value):
        raise ValueError('admin_asset must not contain whitespace.')
    return value


def normalize_channel_tag(channel_tag):
    value = str(channel_tag or '').strip()
    if not CHANNEL_TAG_PATTERN.match(value):
        raise ValueError('channel_tag must be 1-32 chars using letters, digits, underscore, or hyphen.')
    return value


def normalize_channel_key(channel_key):
    value = str(channel_key or '').strip().lower()
    if not CHANNEL_KEY_PATTERN.match(value):
        raise ValueError('channel_key must be 2-64 chars of lowercase letters, digits, and underscore.')
    return value


def build_channel_asset_name(admin_asset, channel_tag):
    admin_value = normalize_admin_asset(admin_asset)
    tag_value = normalize_channel_tag(channel_tag)
    root = admin_value[:-1]
    if not root:
        raise ValueError('admin_asset root is invalid.')
    asset_name = f'{root}~{tag_value}'
    return validate_channel_asset_name(asset_name)


def validate_channel_asset_name(asset_name):
    value = str(asset_name or '').strip()
    if not value:
        raise ValueError('channel asset name is required.')
    if len(value) > MAX_EVRMORE_ASSET_NAME_LENGTH:
        raise ValueError(
            f'channel asset name exceeds max length of {MAX_EVRMORE_ASSET_NAME_LENGTH} characters.'
        )
    if not CHANNEL_ASSET_NAME_PATTERN.match(value):
        raise ValueError(
            'channel asset name must follow ROOT~TAG rules with uppercase ROOT and valid TAG characters.'
        )
    return value


def build_console_metadata(asset_name, channel_key, channel_name, metadata):
    base = metadata.copy() if isinstance(metadata, dict) else {}
    description = str(base.get('description', '') or '').strip()

    allowed_stages = base.get('allowed_stages', DEFAULT_ALLOWED_STAGES)
    if not isinstance(allowed_stages, list) or not allowed_stages:
        raise ValueError('allowed_stages must be a non-empty list.')
    normalized_stages = [str(stage).strip().lower() for stage in allowed_stages if str(stage).strip()]
    if not normalized_stages:
        raise ValueError('allowed_stages must include at least one non-empty stage.')

    strict_rules = base.get('strict_rules', {
        'console_mode': 'strict',
        'immutable_payload': True,
        'allow_unregistered_keys': False,
    })
    if not isinstance(strict_rules, dict):
        raise ValueError('strict_rules must be a JSON object.')

    payload = {
        'schema': CONSOLE_CHANNEL_SCHEMA,
        'version': CONSOLE_CHANNEL_SCHEMA_VERSION,
        'asset_name': str(asset_name).strip(),
        'channel_key': normalize_channel_key(channel_key),
        'channel_name': str(channel_name).strip().upper(),
        'description': description,
        'allowed_stages': normalized_stages,
        'strict_rules': strict_rules,
        'console_type': str(base.get('console_type', 'atomic_swap_transfer')).strip().lower(),
        'raw': base,
    }
    return payload


def validate_console_metadata(asset_name, payload):
    if not isinstance(payload, dict):
        raise ValueError('metadata payload must be a JSON object.')

    schema = str(payload.get('schema') or '').strip()
    if schema != CONSOLE_CHANNEL_SCHEMA:
        raise ValueError('metadata schema is invalid for channel console.')

    try:
        version = int(payload.get('version'))
    except (TypeError, ValueError):
        raise ValueError('metadata version is invalid.')

    if version != CONSOLE_CHANNEL_SCHEMA_VERSION:
        raise ValueError('metadata version is not supported.')

    payload_asset_name = str(payload.get('asset_name') or '').strip().upper()
    expected_name = str(asset_name or '').strip().upper()
    if payload_asset_name != expected_name:
        raise ValueError('metadata asset_name does not match channel asset.')

    _ = normalize_channel_key(payload.get('channel_key'))
    channel_name = str(payload.get('channel_name') or '').strip().upper()
    if not channel_name:
        raise ValueError('channel_name is required.')

    allowed_stages = payload.get('allowed_stages')
    if not isinstance(allowed_stages, list) or not allowed_stages:
        raise ValueError('allowed_stages must be a non-empty list.')

    strict_rules = payload.get('strict_rules')
    if not isinstance(strict_rules, dict):
        raise ValueError('strict_rules must be a JSON object.')

    return payload


def canonical_console_metadata_bytes(payload):
    return json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
