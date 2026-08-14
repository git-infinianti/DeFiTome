from decimal import Decimal, InvalidOperation
from fnmatch import fnmatchcase

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from Tome.rpc_client import get_active_rpc_endpoint_mode, get_current_network_mode, using_network_mode
from Wallet.models import WalletAddress
from Wallet.rpc import RPC, create_and_send_asset_transfer_transaction, create_and_send_issue_asset_transaction
from Wallet.wallet import Wallet

from API.models import MessageChannelPolicy
from API.rpc import evrmore_rpc
from API.channel_console_lib import (
    DEFAULT_ALLOWED_STAGES,
    build_channel_asset_name,
    build_console_metadata,
    canonical_console_metadata_bytes,
    validate_channel_asset_name,
    normalize_admin_asset,
    normalize_channel_key,
    normalize_channel_tag,
    validate_console_metadata,
)
from API.unified_workflow_policy import (
    UNIFIED_WORKFLOW_CHANNEL_KEY,
    UNIFIED_WORKFLOW_CHANNEL_TAG,
    UNIFIED_WORKFLOW_DESCRIPTION,
    UNIFIED_WORKFLOW_POLICY_VERSION,
    UNIFIED_WORKFLOW_STRICT_RULES,
)
from Media.kubo_api import KuboAPIUploader


def get_user_wallet_addresses(user, network_mode=None, include_change=True):
    user_wallet = getattr(user, 'user_wallet', None)
    if not user_wallet:
        return []

    mode = str(network_mode or get_current_network_mode()).strip().lower()

    addresses = []
    seen = set()
    queryset = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=mode,
    )
    if not include_change:
        queryset = queryset.filter(is_change=False)

    for address in queryset.order_by('is_change', 'account', 'index').values_list('address', flat=True):
        normalized = str(address or '').strip()
        if normalized and normalized not in seen:
            addresses.append(normalized)
            seen.add(normalized)

    if addresses:
        return addresses

    try:
        wallet_instance = Wallet(
            user_wallet.entropy,
            user_wallet.passphrase,
            network_mode=mode,
        )
        fallback = str(wallet_instance.get_wallet().address() or '').strip()
        if fallback:
            return [fallback]
    except Exception:
        return []

    return []


def get_user_asset_balances(user, network_mode=None):
    addresses = get_user_wallet_addresses(user, network_mode=network_mode, include_change=True)
    balances = {}

    for address in addresses:
        try:
            address_balances = evrmore_rpc.list_asset_balances_by_address(address)
        except Exception:
            continue

        if not isinstance(address_balances, dict):
            continue

        for symbol, amount in address_balances.items():
            try:
                value = Decimal(str(amount))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if value <= 0:
                continue

            normalized_symbol = str(symbol or '').strip().upper()
            if not normalized_symbol:
                continue
            balances[normalized_symbol] = balances.get(normalized_symbol, Decimal('0')) + value

    return balances, addresses


def get_owned_admin_assets(user, network_mode=None):
    balances, _addresses = get_user_asset_balances(user, network_mode=network_mode)
    admin_assets = [
        symbol for symbol, amount in balances.items()
        if symbol.endswith('!') and amount > 0
    ]
    admin_assets.sort()
    return admin_assets


def _get_primary_wallet_address_and_wif(user, network_mode=None, required_asset_name=None):
    user_wallet = getattr(user, 'user_wallet', None)
    if not user_wallet:
        raise ValueError('A user wallet is required for manual asset issuance.')

    mode = str(network_mode or get_current_network_mode()).strip().lower()
    required_asset = str(required_asset_name or '').strip().upper()

    if required_asset:
        candidate_records = WalletAddress.objects.filter(
            wallet=user_wallet,
            network_mode=mode,
            is_change=False,
        ).order_by('account', 'index')
        for address_record in candidate_records:
            try:
                balances = evrmore_rpc.list_asset_balances_by_address(address_record.address)
            except Exception:
                continue
            if not isinstance(balances, dict):
                continue
            try:
                balance_value = Decimal(str(balances.get(required_asset, 0)))
            except (InvalidOperation, TypeError, ValueError):
                balance_value = Decimal('0')
            if balance_value > 0:
                if address_record.wif:
                    return address_record.address, address_record.wif
                wallet_instance = Wallet(
                    user_wallet.entropy,
                    user_wallet.passphrase,
                    network_mode=mode,
                )
                return address_record.address, wallet_instance.get_wif_for_address(address_record.address)

    address_record = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=mode,
        is_change=False,
    ).order_by('account', 'index').first()

    if address_record and address_record.wif:
        return address_record.address, address_record.wif

    wallet_instance = Wallet(
        user_wallet.entropy,
        user_wallet.passphrase,
        network_mode=mode,
    )
    source_address = address_record.address if address_record else wallet_instance.get_address(index=0)
    return source_address, wallet_instance.get_wif_for_address(source_address)


def _get_change_wallet_address(user, network_mode=None):
    user_wallet = getattr(user, 'user_wallet', None)
    if not user_wallet:
        raise ValueError('A user wallet is required for manual asset issuance.')

    mode = str(network_mode or get_current_network_mode()).strip().lower()
    address_record = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=mode,
        is_change=True,
    ).order_by('account', 'index').first()
    if address_record:
        return address_record.address

    wallet_instance = Wallet(
        user_wallet.entropy,
        user_wallet.passphrase,
        network_mode=mode,
    )
    return wallet_instance.get_change_address(index=0)


def _resolve_policy_version(channel_key, network_mode, requested_version):
    existing_qs = MessageChannelPolicy.objects.filter(
        channel_key=channel_key,
        network_mode=network_mode,
    )

    if requested_version is not None:
        version = int(requested_version)
        if version <= 0:
            raise ValueError('channel_version must be a positive integer.')
        if existing_qs.filter(version=version).exists():
            raise ValueError('Requested channel_version already exists for this key and network.')
        return version

    latest = existing_qs.order_by('-version').first()
    if latest is None:
        return 1
    return int(latest.version) + 1


def _validate_existing_channel_asset_binding(channel_asset_name, channel_key, network_mode):
    local_conflict = MessageChannelPolicy.objects.filter(
        channel_name=channel_asset_name,
        network_mode=network_mode,
    ).exclude(channel_key=channel_key).exists()
    if local_conflict:
        raise ValueError(
            'This channel asset name is already bound to a different local channel key policy on this network.'
        )

    try:
        existing_asset_data = evrmore_rpc.get_asset_data(channel_asset_name)
    except Exception:
        return ''

    if not (isinstance(existing_asset_data, dict) and existing_asset_data):
        return ''

    cid = _extract_asset_ipfs_hash(existing_asset_data)
    if not cid:
        raise ValueError(
            'This channel asset already exists on-chain without valid console metadata and cannot be rebound safely.'
        )

    try:
        payload = KuboAPIUploader().download_json(cid)
        validate_console_metadata(channel_asset_name, payload)
    except Exception as exc:
        raise ValueError(
            f'This channel asset already exists on-chain with unreadable or invalid console metadata: {exc}'
        )

    existing_channel_key = str(payload.get('channel_key') or '').strip().lower()
    if existing_channel_key != channel_key:
        raise ValueError(
            'This channel asset already exists on-chain and is bound to a different channel key.'
        )

    return cid


def _is_unified_workflow_v5(channel_key, policy_version):
    return (
        channel_key == UNIFIED_WORKFLOW_CHANNEL_KEY
        and int(policy_version) == UNIFIED_WORKFLOW_POLICY_VERSION
    )


def _build_unified_workflow_v5_metadata(channel_tag, metadata):
    if str(channel_tag).strip().upper() != UNIFIED_WORKFLOW_CHANNEL_TAG:
        raise ValueError(
            f'Unified workflow v{UNIFIED_WORKFLOW_POLICY_VERSION} requires channel tag '
            f'{UNIFIED_WORKFLOW_CHANNEL_TAG}.'
        )

    normalized_metadata = dict(metadata or {})
    provided_rules = normalized_metadata.get('strict_rules')
    if provided_rules is not None and not isinstance(provided_rules, dict):
        raise ValueError('strict_rules must be a JSON object.')
    normalized_metadata.update({
        'description': str(
            normalized_metadata.get('description') or UNIFIED_WORKFLOW_DESCRIPTION
        ).strip(),
        'allowed_stages': list(DEFAULT_ALLOWED_STAGES),
        'strict_rules': {
            **dict(provided_rules or {}),
            **UNIFIED_WORKFLOW_STRICT_RULES,
        },
        'console_type': 'defitome_workflow_event',
    })
    return normalized_metadata


def _validate_unified_workflow_v5_metadata(policy, asset_name, metadata):
    if policy is None or not _is_unified_workflow_v5(policy.channel_key, policy.version):
        return
    expected_asset_name = f'{str(asset_name).split("~", 1)[0]}~{UNIFIED_WORKFLOW_CHANNEL_TAG}'
    if str(asset_name).strip().upper() != expected_asset_name.upper():
        raise ValueError(
            f'Unified workflow v{UNIFIED_WORKFLOW_POLICY_VERSION} must use asset '
            f'tag {UNIFIED_WORKFLOW_CHANNEL_TAG}.'
        )
    if str(metadata.get('channel_key') or '').strip().lower() != UNIFIED_WORKFLOW_CHANNEL_KEY:
        raise ValueError('Unified workflow v5 metadata has an unexpected channel key.')
    allowed_stages = {str(stage).strip().lower() for stage in (metadata.get('allowed_stages') or [])}
    if allowed_stages != set(DEFAULT_ALLOWED_STAGES):
        raise ValueError('Unified workflow v5 metadata must declare the complete lifecycle stage set.')
    strict_rules = metadata.get('strict_rules')
    if not isinstance(strict_rules, dict):
        raise ValueError('Unified workflow v5 metadata requires strict rules.')
    for key, expected_value in UNIFIED_WORKFLOW_STRICT_RULES.items():
        if strict_rules.get(key) != expected_value:
            raise ValueError(f'Unified workflow v5 metadata requires strict rule {key!r}.')


def _activate_verified_unified_workflow_v5_policy(policy):
    if policy is None or not _is_unified_workflow_v5(policy.channel_key, policy.version):
        return
    with transaction.atomic():
        MessageChannelPolicy.objects.filter(
            channel_key=policy.channel_key,
            network_mode=policy.network_mode,
            status='active',
        ).exclude(pk=policy.pk).update(status='deprecated')
        MessageChannelPolicy.objects.filter(pk=policy.pk).update(status='active')


def create_channel_console_asset_for_user(user, data):
    network_mode = str(data.get('network_mode') or get_current_network_mode()).strip().lower()
    if network_mode not in {'mainnet', 'testnet'}:
        raise ValueError('network_mode must be mainnet or testnet.')
    with using_network_mode(network_mode):
        return _create_channel_console_asset_for_user(user, data, network_mode)


def _create_channel_console_asset_for_user(user, data, network_mode):
    admin_asset = normalize_admin_asset(data.get('admin_asset'))
    channel_tag = normalize_channel_tag(data.get('channel_tag'))
    channel_key = normalize_channel_key(
        data.get('channel_key') or f'{admin_asset[:-1].lower()}_{channel_tag.lower().replace("-", "_")}'
    )
    channel_asset_name = validate_channel_asset_name(build_channel_asset_name(admin_asset, channel_tag))

    requested_version = data.get('channel_version')
    existing_draft = None
    if requested_version is not None and str(requested_version).strip():
        requested_version = int(requested_version)
        existing_policy = MessageChannelPolicy.objects.filter(
            channel_key=channel_key,
            network_mode=network_mode,
            version=requested_version,
        ).first()
        if existing_policy is not None:
            if (
                existing_policy.status == 'draft'
                and existing_policy.channel_name == channel_asset_name
            ):
                existing_draft = existing_policy
                policy_version = requested_version
            else:
                raise ValueError('Requested channel_version already exists for this key and network.')
        else:
            policy_version = _resolve_policy_version(
                channel_key=channel_key,
                network_mode=network_mode,
                requested_version=requested_version,
            )
    else:
        policy_version = _resolve_policy_version(
            channel_key=channel_key,
            network_mode=network_mode,
            requested_version=None,
        )

    is_unified_workflow_v5 = _is_unified_workflow_v5(channel_key, policy_version)
    if existing_draft is not None and str(existing_draft.issuance_txid or '').strip():
        return {
            'channel_asset_name': channel_asset_name,
            'metadata_ipfs_cid': str(existing_draft.metadata_ipfs_cid or ''),
            'txid': str(existing_draft.issuance_txid),
            'channel_policy': {
                'channel_key': existing_draft.channel_key,
                'version': existing_draft.version,
                'rules_checksum': existing_draft.rules_checksum,
            },
            'owned_addresses': get_user_wallet_addresses(user, network_mode=network_mode),
            'asset_already_exists': False,
                'issuance_pending': (
                    existing_draft.chain_metadata_status
                    == MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING
                ),
                'existing_issuance': True,
        }

    metadata = data.get('metadata') or {}
    if is_unified_workflow_v5:
        metadata = _build_unified_workflow_v5_metadata(channel_tag, metadata)

    balances, owned_addresses = get_user_asset_balances(user, network_mode=network_mode)
    owned_admin_assets = get_owned_admin_assets(user, network_mode=network_mode)
    if admin_asset not in owned_admin_assets:
        raise ValueError(f'{admin_asset} is not in your owned admin-asset set for this network.')
    if balances.get(admin_asset, Decimal('0')) <= 0:
        raise ValueError(f'You must hold {admin_asset} in your wallet to create a messaging channel under it.')

    existing_metadata_cid = _validate_existing_channel_asset_binding(
        channel_asset_name=channel_asset_name,
        channel_key=channel_key,
        network_mode=network_mode,
    )
    asset_already_exists = bool(existing_metadata_cid)

    metadata_payload = build_console_metadata(
        asset_name=channel_asset_name,
        channel_key=channel_key,
        channel_name=str(data.get('channel_name') or channel_asset_name).strip(),
        metadata=metadata,
    )
    metadata_ipfs_cid = existing_metadata_cid
    if not asset_already_exists:
        metadata_bytes = canonical_console_metadata_bytes(metadata_payload)
        upload_result = KuboAPIUploader().upload_bytes(
            metadata_bytes,
            file_name=f'{channel_asset_name}_console.json',
            pin=True,
            cid_version=0,
        )
        metadata_ipfs_cid = upload_result.cid

    # Messaging channel assets are governance markers and must remain 1-of-1.
    if 'qty' in data and str(data.get('qty')).strip() not in {'', '1', '1.0', '1.00'}:
        raise ValueError('Messaging channel assets are fixed at quantity 1 and do not accept custom qty.')

    txid = ''

    if not asset_already_exists:
        source_address, source_wif = _get_primary_wallet_address_and_wif(
            user,
            network_mode=network_mode,
            required_asset_name=admin_asset,
        )
        issuer_address = str(data.get('to_address') or '').strip() or source_address
        issuance_result = create_and_send_issue_asset_transaction(
            from_address=source_address,
            issuer_address=issuer_address,
            asset_name=channel_asset_name,
            asset_quantity=Decimal('1'),
            units=0,
            reissuable=False,
            has_ipfs=True,
            ipfs_hash=metadata_ipfs_cid,
            wif_keys=[source_wif],
            owner_change_address=source_address,
        )
        txid = issuance_result.get('txid', '')

    system_user, _ = User.objects.get_or_create(
        username='system',
        defaults={
            'email': 'system@defitome.local',
            'is_active': True,
            'is_staff': True,
        },
    )

    policy_values = {
        'channel_name': channel_asset_name,
        'network_mode': network_mode,
        'version': policy_version,
        'status': (
            'draft'
            if is_unified_workflow_v5
            else 'active'
        ),
        'owner_account': system_user,
        'manager_account': user,
        'schema_name': 'defitome.atomic-swap-transfer-message',
        'schema_version': 1,
        'allowed_stages': metadata_payload.get('allowed_stages') or DEFAULT_ALLOWED_STAGES,
        'strict_rules': metadata_payload.get('strict_rules') or {
            'console_mode': 'strict',
            'immutable_payload': True,
            'allow_unregistered_keys': False,
        },
        'metadata_ipfs_cid': metadata_ipfs_cid,
        'issuance_txid': txid or str(getattr(existing_draft, 'issuance_txid', '') or ''),
        'chain_metadata_status': (
            MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED
            if asset_already_exists and not is_unified_workflow_v5
            else MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING
        ),
        'chain_metadata_error': (
            'Awaiting independent verification of the v5 channel asset metadata.'
            if is_unified_workflow_v5
            else ''
        ),
        'is_locked': True,
    }
    if existing_draft is not None:
        for field_name, value in policy_values.items():
            setattr(existing_draft, field_name, value)
        existing_draft.save()
        policy = existing_draft
    else:
        if not is_unified_workflow_v5:
            MessageChannelPolicy.objects.filter(
                channel_key=channel_key,
                network_mode=network_mode,
                status='active',
            ).update(status='deprecated')
        policy = MessageChannelPolicy.objects.create(
            channel_key=channel_key,
            **policy_values,
        )

    return {
        'channel_asset_name': channel_asset_name,
        'metadata_ipfs_cid': metadata_ipfs_cid,
        'txid': str(policy.issuance_txid or ''),
        'channel_policy': {
            'channel_key': policy.channel_key,
            'version': policy.version,
            'rules_checksum': policy.rules_checksum,
        },
        'owned_addresses': owned_addresses,
        'asset_already_exists': asset_already_exists,
        'issuance_pending': False,
        'existing_issuance': False,
    }


def _extract_asset_ipfs_hash(asset_data):
    if not isinstance(asset_data, dict):
        return ''
    for key in (
        'ipfs_hash',
        'ipfshash',
        'ipfs',
        'permanent_ipfs_hash',
        'permanent_ipfshash',
        'permanent_ipfs',
    ):
        value = str(asset_data.get(key) or '').strip()
        if value:
            return value
    return ''


def _channel_policy_for_asset(asset_name, network_mode):
    return MessageChannelPolicy.objects.filter(
        channel_name=asset_name,
        network_mode=network_mode,
    ).order_by('-version').first()


def _update_policy_chain_metadata(policy, status, error='', cid=''):
    if policy is None:
        return
    now = timezone.now()
    update_values = {
        'chain_metadata_status': status,
        'chain_metadata_error': str(error or ''),
        'chain_metadata_checked_at': now,
        'updated_at': now,
    }
    if cid:
        update_values['metadata_ipfs_cid'] = cid
    MessageChannelPolicy.objects.filter(
        channel_name=policy.channel_name,
        network_mode=policy.network_mode,
    ).update(**update_values)


def _validated_channel_console(asset_name):
    normalized_name = validate_channel_asset_name(str(asset_name or '').strip().upper())
    asset_data = evrmore_rpc.get_asset_data(normalized_name)
    if not (isinstance(asset_data, dict) and asset_data):
        raise ValueError('Messaging channel asset was not found on the selected network.')
    cid = _extract_asset_ipfs_hash(asset_data)
    if not cid:
        raise ValueError('Messaging channel asset has no on-chain IPFS metadata CID.')
    payload = KuboAPIUploader().download_json(cid)
    validate_console_metadata(normalized_name, payload)
    return normalized_name, cid, payload


def validate_channel_console_asset(asset_name, network_mode=None):
    resolved_network_mode = str(network_mode or get_current_network_mode()).strip().lower()
    if resolved_network_mode not in {'mainnet', 'testnet'}:
        raise ValueError('network_mode must be mainnet or testnet.')
    with using_network_mode(resolved_network_mode):
        normalized_name, cid, payload = _validated_channel_console(asset_name)
    policy = _channel_policy_for_asset(normalized_name, resolved_network_mode)
    _validate_unified_workflow_v5_metadata(
        policy,
        normalized_name,
        payload,
    )
    if policy is not None and _is_unified_workflow_v5(policy.channel_key, policy.version):
        _update_policy_chain_metadata(
            policy,
            MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
            cid=cid,
        )
        _activate_verified_unified_workflow_v5_policy(policy)
    return {
        'asset_name': normalized_name,
        'ipfs_cid': cid,
        'metadata': payload,
        'network_mode': resolved_network_mode,
    }


def set_channel_subscription(asset_name, subscribe, network_mode=None):
    resolved_network_mode = str(network_mode or get_current_network_mode()).strip().lower()
    if resolved_network_mode not in {'mainnet', 'testnet'}:
        raise ValueError('network_mode must be mainnet or testnet.')
    validation = validate_channel_console_asset(asset_name, network_mode=resolved_network_mode)
    normalized_name = validation['asset_name']
    cid = validation['ipfs_cid']
    with using_network_mode(resolved_network_mode):
        if subscribe:
            result = evrmore_rpc.subscribe_to_channel(normalized_name)
        else:
            result = evrmore_rpc.unsubscribe_from_channel(normalized_name)
    return {
        'asset_name': normalized_name,
        'ipfs_cid': cid,
        'subscribed': bool(subscribe),
        'rpc_result': result,
    }


def burn_channel_asset_for_revision(user, asset_name, network_mode=None):
    resolved_network_mode = str(network_mode or get_current_network_mode()).strip().lower()
    if resolved_network_mode not in {'mainnet', 'testnet'}:
        raise ValueError('network_mode must be mainnet or testnet.')
    normalized_name = validate_channel_asset_name(str(asset_name or '').strip().upper())

    managed_policies = MessageChannelPolicy.objects.filter(
        channel_name=normalized_name,
        network_mode=resolved_network_mode,
        manager_account=user,
    )
    if not managed_policies.exists():
        raise ValueError('You may only burn messaging channel assets managed by your account.')
    if managed_policies.exclude(revision_burn_txid='').exists():
        raise ValueError('This messaging channel asset already has a recorded revision burn.')

    with using_network_mode(resolved_network_mode):
        burn_addresses = RPC.getburnaddresses()
        burn_address = str(
            burn_addresses.get('global_burn_address') if isinstance(burn_addresses, dict) else ''
        ).strip()
        if not burn_address:
            raise ValueError('The selected network node did not provide a global asset burn address.')

        source_address, source_wif = _get_primary_wallet_address_and_wif(
            user,
            network_mode=resolved_network_mode,
            required_asset_name=normalized_name,
        )
        result = create_and_send_asset_transfer_transaction(
            from_address=source_address,
            to_address=burn_address,
            asset_name=normalized_name,
            asset_quantity=Decimal('1'),
            change_address=source_address,
            asset_change_address=source_address,
            wif_keys=[source_wif],
        )

    txid = str(result.get('txid') or '').strip()
    if not txid:
        raise ValueError('Raw revision burn did not return a transaction id.')
    now = timezone.now()
    with transaction.atomic():
        managed_policies.update(
            status='deprecated',
            revision_burn_txid=txid,
            revision_burned_at=now,
            updated_at=now,
        )
    return {
        'asset_name': normalized_name,
        'burn_address': burn_address,
        'txid': txid,
    }


def scan_channel_console_assets(asset_pattern='*~*', count=100, start=0, network_mode=None):
    resolved_network_mode = str(network_mode or get_current_network_mode()).strip().lower()
    if resolved_network_mode not in {'mainnet', 'testnet'}:
        raise ValueError('network_mode must be mainnet or testnet.')
    with using_network_mode(resolved_network_mode):
        return _scan_channel_console_assets(asset_pattern, count, start, resolved_network_mode)


def _scan_channel_console_assets(asset_pattern, count, start, resolved_network_mode):
    normalized_count = max(1, min(200, int(count)))
    normalized_start = max(0, int(start))
    try:
        listing = evrmore_rpc.list_assets(
            asset=asset_pattern,
            verbose=False,
            count=normalized_count,
            start=normalized_start,
        )
    except Exception as exc:
        return {
            'valid_channels': [],
            'pending_channels': [],
            'invalid_channels': [],
            'scan_error': str(exc),
            'scanned_count': 0,
            'start': normalized_start,
            'next_start': normalized_start,
            'has_more': False,
            'network_mode': resolved_network_mode,
            'subscription_state_available': False,
        }
    if isinstance(listing, dict):
        asset_names = list(listing.keys())
    elif isinstance(listing, list):
        asset_names = [str(item) for item in listing]
    else:
        asset_names = []

    configured_channel_names = MessageChannelPolicy.objects.filter(
        network_mode=resolved_network_mode,
    ).values_list('channel_name', flat=True).distinct()
    discovered = {
        str(asset_name or '').strip()
        for asset_name in asset_names
        if str(asset_name or '').strip()
    }
    for channel_name in configured_channel_names:
        normalized_name = str(channel_name or '').strip()
        if normalized_name and fnmatchcase(normalized_name, asset_pattern):
            discovered.add(normalized_name)
    asset_names = sorted(discovered)

    valid = []
    pending = []
    invalid = []
    subscribed_channels = None
    if get_active_rpc_endpoint_mode() == 'local':
        try:
            subscribed_channels = {
                str(channel_name or '').strip()
                for channel_name in (evrmore_rpc.view_all_message_channels() or [])
            }
        except Exception:
            subscribed_channels = None

    for asset_name in asset_names:
        if '~' not in asset_name:
            continue
        policy = _channel_policy_for_asset(asset_name, resolved_network_mode)
        intended_cid = str(getattr(policy, 'metadata_ipfs_cid', '') or '')
        issuance_txid = str(getattr(policy, 'issuance_txid', '') or '')
        try:
            asset_data = evrmore_rpc.get_asset_data(asset_name)
            if not (isinstance(asset_data, dict) and asset_data):
                if issuance_txid and getattr(policy, 'chain_metadata_status', '') == MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING:
                    pending.append({
                        'asset_name': asset_name,
                        'channel_key': getattr(policy, 'channel_key', ''),
                        'intended_ipfs_cid': intended_cid,
                        'issuance_txid': issuance_txid,
                        'status': 'pending_confirmation',
                    })
                    continue
                error = 'asset_not_found_on_selected_network'
                _update_policy_chain_metadata(
                    policy,
                    MessageChannelPolicy.CHAIN_METADATA_STATUS_MISSING,
                    error=error,
                )
                invalid.append({
                    'asset_name': asset_name,
                    'error': error,
                    'intended_ipfs_cid': intended_cid,
                    'issuance_txid': issuance_txid,
                })
                continue

            cid = _extract_asset_ipfs_hash(asset_data)
            if not cid:
                error = 'confirmed_asset_missing_ipfs_hash'
                _update_policy_chain_metadata(
                    policy,
                    MessageChannelPolicy.CHAIN_METADATA_STATUS_MISSING,
                    error=error,
                )
                invalid.append({
                    'asset_name': asset_name,
                    'error': error,
                    'intended_ipfs_cid': intended_cid,
                    'issuance_txid': issuance_txid,
                })
                continue

            if intended_cid and cid != intended_cid:
                error = f'on_chain_cid_mismatch: expected {intended_cid}, found {cid}'
                _update_policy_chain_metadata(
                    policy,
                    MessageChannelPolicy.CHAIN_METADATA_STATUS_INVALID,
                    error=error,
                )
                invalid.append({
                    'asset_name': asset_name,
                    'error': error,
                    'ipfs_cid': cid,
                    'intended_ipfs_cid': intended_cid,
                    'issuance_txid': issuance_txid,
                })
                continue

            payload = KuboAPIUploader().download_json(cid)
            validate_console_metadata(asset_name, payload)
            _validate_unified_workflow_v5_metadata(policy, asset_name, payload)
            _update_policy_chain_metadata(
                policy,
                MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
                cid=cid,
            )
            _activate_verified_unified_workflow_v5_policy(policy)
            valid.append({
                'asset_name': asset_name,
                'ipfs_cid': cid,
                'channel_key': payload.get('channel_key'),
                'allowed_stages': payload.get('allowed_stages', []),
                'console_type': payload.get('console_type', ''),
                'issuance_txid': issuance_txid,
                'status': 'verified',
                'is_subscribed': (
                    asset_name in subscribed_channels
                    if subscribed_channels is not None
                    else None
                ),
            })
        except Exception as exc:
            _update_policy_chain_metadata(
                policy,
                MessageChannelPolicy.CHAIN_METADATA_STATUS_INVALID,
                error=str(exc),
            )
            invalid.append({
                'asset_name': asset_name,
                'error': str(exc),
                'intended_ipfs_cid': intended_cid,
                'issuance_txid': issuance_txid,
            })

    return {
        'valid_channels': valid,
        'pending_channels': pending,
        'invalid_channels': invalid,
        'scan_error': '',
        'scanned_count': len(asset_names),
        'start': normalized_start,
        'next_start': normalized_start + len(asset_names),
        'has_more': len(asset_names) == normalized_count,
        'network_mode': resolved_network_mode,
        'subscription_state_available': subscribed_channels is not None,
    }
