import re
from decimal import Decimal, InvalidOperation

from Tome.rpc_client import get_current_network_mode

from .asset_tracking import classify_asset_type
from .models import AssetCreationRequest, TrackedAsset
from .rpc import (
    _owner_token_name,
    _resolve_burn_address,
    broadcast_signed_transaction,
    create_raw_asset_operation_transaction,
    sign_raw_transaction,
    test_mempool_accept_signed_transaction,
)


ASSET_NAME_PATTERN = re.compile(r'^[A-Z0-9._$/#~]+$')


def _positive_decimal(value, field_name, default=None):
    raw_value = default if value in (None, '') else value
    try:
        parsed = Decimal(str(raw_value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f'{field_name} must be a valid number.') from exc
    if parsed <= 0:
        raise ValueError(f'{field_name} must be greater than zero.')
    return parsed


def _normalized_asset_name(asset_kind, value):
    name = str(value or '').strip().upper()
    if not name or not ASSET_NAME_PATTERN.fullmatch(name):
        raise ValueError('Asset name contains unsupported characters.')

    rules = {
        AssetCreationRequest.KIND_MAIN: not any(marker in name for marker in '/#~$!'),
        AssetCreationRequest.KIND_SUB: '/' in name and not name.startswith('#'),
        AssetCreationRequest.KIND_UNIQUE: '#' in name and not name.startswith('#'),
        AssetCreationRequest.KIND_MESSAGING: '~' in name,
        AssetCreationRequest.KIND_QUALIFIER: name.startswith('#') and '/' not in name,
        AssetCreationRequest.KIND_SUB_QUALIFIER: (
            name.startswith('#')
            and '/#' in name
            and name.count('/') == 1
        ),
        AssetCreationRequest.KIND_RESTRICTED: name.startswith('$') and len(name) > 1,
    }
    if not rules.get(asset_kind, False):
        raise ValueError(f'Asset name does not match the selected {asset_kind} type.')
    return name


def _issue_payload(asset_name, quantity, units, reissuable, ipfs_hash):
    payload = {
        'issue': {
            'asset_name': asset_name,
            'asset_quantity': float(quantity),
            'units': units,
            'reissuable': int(reissuable),
            'has_ipfs': int(bool(ipfs_hash)),
            'remintable': int(reissuable),
        }
    }
    if ipfs_hash:
        payload['issue']['ipfs_hash'] = ipfs_hash
    return payload


def build_asset_operation(asset_kind, asset_name, parameters):
    kind = str(asset_kind or '').strip()
    valid_kinds = {choice[0] for choice in AssetCreationRequest.KIND_CHOICES}
    if kind not in valid_kinds:
        raise ValueError('Unsupported asset type.')

    name = _normalized_asset_name(kind, asset_name)
    quantity = _positive_decimal(parameters.get('quantity'), 'Quantity', default='1')
    try:
        units = int(parameters.get('units') or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError('Units must be a whole number from 0 through 8.') from exc
    if units < 0 or units > 8:
        raise ValueError('Units must be a whole number from 0 through 8.')

    reissuable = bool(parameters.get('reissuable'))
    ipfs_hash = str(parameters.get('ipfs_hash') or '').strip()
    authorization_asset_name = None
    burn_key = ''
    burn_amount = Decimal('0')

    if kind == AssetCreationRequest.KIND_MAIN:
        operation_payload = _issue_payload(name, quantity, units, reissuable, ipfs_hash)
        burn_key, burn_amount = 'issue_asset', Decimal('500')
    elif kind == AssetCreationRequest.KIND_SUB:
        operation_payload = _issue_payload(name, quantity, units, reissuable, ipfs_hash)
        authorization_asset_name = _owner_token_name(name)
        burn_key, burn_amount = 'issue_sub_asset', Decimal('100')
    elif kind == AssetCreationRequest.KIND_MESSAGING:
        operation_payload = {
            '_issue_new_asset': {
                'asset_name': name,
                'asset_quantity': 1.0,
                'units': 0,
                'reissuable': 0,
                'has_ipfs': int(bool(ipfs_hash)),
            }
        }
        if ipfs_hash:
            operation_payload['_issue_new_asset']['ipfs_hash'] = ipfs_hash
        authorization_asset_name = _owner_token_name(name)
        burn_key, burn_amount = 'issue_msg_channel_asset', Decimal('100')
        quantity, units, reissuable = Decimal('1'), 0, False
    elif kind == AssetCreationRequest.KIND_UNIQUE:
        operation_payload = {
            '_issue_new_asset': {
                'asset_name': name,
                'asset_quantity': 1.0,
                'units': 0,
                'reissuable': 0,
                'has_ipfs': int(bool(ipfs_hash)),
            }
        }
        if ipfs_hash:
            operation_payload['_issue_new_asset']['ipfs_hash'] = ipfs_hash
        root_name = name.split('#', 1)[0]
        authorization_asset_name = _owner_token_name(root_name)
        burn_key, burn_amount = 'issue_unique_asset', Decimal('5')
        quantity, units, reissuable = Decimal('1'), 0, False
    elif kind in {AssetCreationRequest.KIND_QUALIFIER, AssetCreationRequest.KIND_SUB_QUALIFIER}:
        operation_payload = {
            'issue_qualifier': {
                'asset_name': name,
                'asset_quantity': float(quantity),
                'has_ipfs': int(bool(ipfs_hash)),
            }
        }
        if ipfs_hash:
            operation_payload['issue_qualifier']['ipfs_hash'] = ipfs_hash
        if kind == AssetCreationRequest.KIND_SUB_QUALIFIER:
            operation_payload['issue_qualifier']['change_quantity'] = 1.0
            authorization_asset_name = name.split('/', 1)[0]
            burn_key, burn_amount = 'issue_sub_qualifier_asset', Decimal('100')
        else:
            burn_key, burn_amount = 'issue_qualifier_asset', Decimal('1000')
        units, reissuable = 0, False
    else:
        verifier_string = str(parameters.get('verifier_string') or '').strip()
        if not verifier_string:
            raise ValueError('Restricted assets require a verifier string.')
        operation_payload = {
            'issue_restricted': {
                'asset_name': name,
                'asset_quantity': float(quantity),
                'verifier_string': verifier_string,
                'units': units,
                'reissuable': int(reissuable),
                'has_ipfs': int(bool(ipfs_hash)),
                'owner_change_address': None,
            }
        }
        if ipfs_hash:
            operation_payload['issue_restricted']['ipfs_hash'] = ipfs_hash
        authorization_asset_name = _owner_token_name(name)
        burn_key, burn_amount = 'issue_restricted_asset', Decimal('1500')

    return {
        'asset_name': name,
        'quantity': quantity,
        'units': units,
        'reissuable': reissuable,
        'ipfs_hash': ipfs_hash,
        'operation_payload': operation_payload,
        'authorization_asset_name': authorization_asset_name,
        'authorization_change_output_required': kind != AssetCreationRequest.KIND_RESTRICTED,
        'burn_address': _resolve_burn_address(burn_key),
        'burn_amount_evr': burn_amount,
    }


def create_asset_for_user(user, source_address, source_wif, asset_kind, asset_name,
                          parameters=None, broadcast=False):
    network_mode = get_current_network_mode()
    if network_mode != 'testnet':
        raise ValueError('The asset creation wizard is restricted to testnet.')
    if not (user and (user.is_staff or user.is_superuser)):
        raise ValueError('Admin privileges are required to create assets.')

    parameters = dict(parameters or {})
    request_record = AssetCreationRequest.objects.create(
        creator=user,
        network_mode=network_mode,
        asset_kind=str(asset_kind or ''),
        asset_name=str(asset_name or '').strip().upper() or '(invalid)',
        source_address=source_address,
        parameters=parameters,
    )

    try:
        operation = build_asset_operation(asset_kind, asset_name, parameters)
        request_record.asset_name = operation['asset_name']
        request_record.parameters = {
            **parameters,
            'quantity': str(operation['quantity']),
            'units': operation['units'],
            'reissuable': operation['reissuable'],
            'burn_amount_evr': str(operation['burn_amount_evr']),
        }

        raw_transaction = create_raw_asset_operation_transaction(
            from_address=source_address,
            operation_address=source_address,
            operation_payload=operation['operation_payload'],
            burn_amount_evr=operation['burn_amount_evr'],
            burn_address=operation['burn_address'],
            authorization_asset_name=operation['authorization_asset_name'],
            authorization_change_output_required=operation['authorization_change_output_required'],
        )
        signed_hex = sign_raw_transaction(raw_transaction['raw_tx'], wif_keys=[source_wif])
        acceptance = test_mempool_accept_signed_transaction(signed_hex)
        request_record.mempool_txid = str(acceptance.get('txid') or '')
        request_record.status = AssetCreationRequest.STATUS_ACCEPTED

        txid = ''
        if broadcast:
            txid = str(broadcast_signed_transaction(signed_hex) or '')
            request_record.broadcast_txid = txid
            request_record.status = AssetCreationRequest.STATUS_BROADCAST
            TrackedAsset.objects.update_or_create(
                symbol=operation['asset_name'],
                network_mode=network_mode,
                defaults={
                    'asset_type': classify_asset_type(operation['asset_name']),
                    'total_quantity': operation['quantity'],
                    'ipfs_hash': operation['ipfs_hash'] or None,
                    'is_reissuable': operation['reissuable'],
                    'units': operation['units'],
                },
            )

        request_record.save()
        return {
            'request': request_record,
            'asset_name': operation['asset_name'],
            'accepted_txid': request_record.mempool_txid,
            'txid': txid,
            'broadcast': bool(broadcast),
        }
    except Exception as exc:
        request_record.status = AssetCreationRequest.STATUS_FAILED
        request_record.error_message = str(exc)
        request_record.save(update_fields=('asset_name', 'parameters', 'status', 'error_message', 'updated_at'))
        raise