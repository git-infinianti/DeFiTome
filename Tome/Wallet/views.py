from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.db import OperationalError, transaction
from django.urls import reverse
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from collections import OrderedDict
from datetime import datetime
import csv
import hashlib
import hmac
import time
import requests
from .models import AssetCreationRequest, UserWallet, WalletAddress, WalletProfile, WalletPreferences, SafeTradeCredentials, TrackedAsset, TrackedAssetHolding
from .wallet import Wallet
from .asset_creation import create_asset_for_user
from .asset_tracking import classify_asset_type, sync_tracked_assets
from .rpc import RPC, create_and_send_evr_transaction, create_and_send_asset_transfer_transaction
from API.models import AtomicSwapTransferMessage, DexMarketEventMessage, MessageChannelPolicy
from Listings.models import DecPokerHand
from API.channel_console_service import (
    burn_channel_asset_for_revision,
    create_channel_console_asset_for_user,
    get_owned_admin_assets,
    scan_channel_console_assets,
    set_channel_subscription,
)
from API.channel_console_lib import DEFAULT_ALLOWED_STAGES
from API.unified_workflow_policy import (
    UNIFIED_WORKFLOW_CHANNEL_KEY,
    UNIFIED_WORKFLOW_CHANNEL_TAG,
    UNIFIED_WORKFLOW_DESCRIPTION,
    UNIFIED_WORKFLOW_POLICY_VERSION,
    UNIFIED_WORKFLOW_STRICT_RULES,
)
from hdwallet.entropies import BIP39Entropy
from hdwallet.derivations import BIP44Derivation, CHANGES
from hdwallet import cryptocurrencies
from Tome.rpc_client import get_current_network_mode
from Tome.qr import build_qr_data_uri


SAFETRADE_BASE_URL = 'https://safe.trade/api/v2'
DEFAULT_CHANNEL_DESCRIPTION = UNIFIED_WORKFLOW_DESCRIPTION


def _build_testnet_proposed_policies():
    return [
        {
            'name': 'Unified DeFiTome Workflow v5 Proposal',
            'target_version': UNIFIED_WORKFLOW_POLICY_VERSION,
            'summary': (
                'Requires canonical checksummed event envelopes and fail-closed channel-asset '
                'lineage reconciliation for shared swap, market, and DEC lifecycle events.'
            ),
            'changes': [
                'Require complete strict-stage coverage for all governed workflows.',
                'Use canonical event ids, aggregate sequences, and payload checksums for replayable events.',
                'Require a new immutable SWAPFLOWV5 channel asset before the v5 policy can activate.',
            ],
        },
    ]


def _get_testnet_proposal_by_version(target_version):
    for proposal in _build_testnet_proposed_policies():
        if int(proposal.get('target_version', 0)) == int(target_version):
            return proposal
    return None


def _active_network_mode():
    return get_current_network_mode()


def _channel_event_console(network_mode, query_params):
    channel_id = str(query_params.get('event_channel') or '').strip()
    status = str(query_params.get('event_status') or '').strip().lower()
    event_type = str(query_params.get('event_type') or '').strip().lower()
    stage = str(query_params.get('event_stage') or '').strip().lower()

    common_filters = {'policy__network_mode': network_mode}
    if channel_id.isdigit():
        common_filters['policy_id'] = int(channel_id)
    if status:
        common_filters['status'] = status
    if stage:
        common_filters['stage'] = stage

    rows = []
    if event_type in {'', 'swap'}:
        swap_messages = AtomicSwapTransferMessage.objects.filter(**common_filters).select_related(
            'policy', 'created_by', 'swap_offer'
        )[:200]
        rows.extend({
            'event_type': 'Swap',
            'object_id': message.swap_offer_id,
            'channel': message.policy,
            'stage': message.stage,
            'status': message.status,
            'actor': message.created_by,
            'payload': message.payload,
            'payload_ipfs_cid': message.payload_ipfs_cid,
            'broadcast_result': message.broadcast_result,
            'error_message': message.error_message,
            'created_at': message.created_at,
        } for message in swap_messages)

    if event_type in {'', 'market'}:
        market_messages = DexMarketEventMessage.objects.filter(**common_filters).select_related(
            'policy', 'created_by'
        )[:200]
        rows.extend({
            'event_type': 'Market',
            'object_id': message.order_id or message.trading_pair_id,
            'channel': message.policy,
            'stage': message.stage,
            'status': message.status,
            'actor': message.created_by,
            'payload': message.payload,
            'payload_ipfs_cid': message.payload_ipfs_cid,
            'broadcast_result': message.broadcast_result,
            'error_message': message.error_message,
            'created_at': message.created_at,
        } for message in market_messages)

    if event_type in {'', 'dec'}:
        dec_hands = DecPokerHand.objects.filter(
            game_instance__network_mode=network_mode,
        ).select_related(
            'game_instance__channel_policy', 'player'
        ).order_by('-created_at')
        if channel_id.isdigit():
            dec_hands = dec_hands.filter(game_instance__channel_policy_id=int(channel_id))

        for hand in dec_hands[:200]:
            for event_name, event_stage, event_status, event_txid in (
                (
                    'spend',
                    'game_spend_recorded',
                    hand.spend_message_status,
                    hand.spend_message_txid,
                ),
                (
                    'reward',
                    'game_reward_distributed',
                    hand.reward_message_status,
                    hand.reward_message_txid,
                ),
            ):
                if event_name == 'reward' and hand.reward_amount <= 0:
                    continue
                if status and status != event_status:
                    continue
                if stage and stage != event_stage:
                    continue
                event_payload = (hand.outcome_detail or {}).get('message_events', {}).get(event_name) or {}
                rows.append({
                    'event_type': 'DEC',
                    'object_id': hand.id,
                    'channel': hand.game_instance.channel_policy,
                    'stage': event_stage,
                    'status': event_status,
                    'actor': hand.player,
                    'payload': event_payload,
                    'payload_ipfs_cid': event_payload.get('payload_cid', ''),
                    'broadcast_result': event_txid,
                    'error_message': event_payload.get('reason', ''),
                    'created_at': hand.created_at,
                })

    rows.sort(key=lambda row: row['created_at'], reverse=True)
    return rows[:200], {
        'channel': channel_id,
        'status': status,
        'event_type': event_type,
        'stage': stage,
    }


def _wallet_for_network(user_wallet):
    return Wallet(
        user_wallet.entropy,
        user_wallet.passphrase,
        network_mode=_active_network_mode(),
    )


def _get_stored_network_balance(user_wallet):
    network_mode = _active_network_mode()
    if network_mode == 'mainnet':
        return user_wallet.evr_liquidity_mainnet or Decimal('0')
    return user_wallet.evr_liquidity_testnet or Decimal('0')


def _set_stored_network_balance(user_wallet, balance, updated_at=None):
    network_mode = _active_network_mode()
    balance_value = Decimal(str(balance or 0))
    update_time = updated_at or timezone.now()

    if network_mode == 'mainnet':
        user_wallet.evr_liquidity_mainnet = balance_value
        user_wallet.last_balance_update_mainnet = update_time
        # Keep legacy field aligned for backward compatibility.
        user_wallet.evr_liquidity = balance_value
        user_wallet.last_balance_update = update_time
    else:
        user_wallet.evr_liquidity_testnet = balance_value
        user_wallet.last_balance_update_testnet = update_time

    user_wallet.save()


def _sync_user_evr_balance(user_wallet):
    """
    Sync user's EVR balance from blockchain using getaddressbalance RPC command.
    
    Args:
        user_wallet: UserWallet instance to update
        
    Returns:
        Decimal: The balance amount, or None if failed
        
    Side effects:
        - Updates user_wallet.evr_liquidity with the balance from the RPC
        - Updates user_wallet.last_balance_update timestamp
        - Saves changes to database
    """
    try:
        address = _get_user_primary_address(user_wallet.user)
        if not address:
            return None
        
        # Call getaddressbalance RPC command
        balance_data = RPC.getaddressbalance(address)
        
        # Extract balance from response: {"balance": 0, "received": 0}
        if isinstance(balance_data, dict) and 'balance' in balance_data:
            balance_satoshis = Decimal(str(balance_data['balance']))
            balance_evr = (balance_satoshis / Decimal('100000000')).quantize(Decimal('0.00000001'))
            _set_stored_network_balance(user_wallet, balance_evr, timezone.now())
            return balance_satoshis
        else:
            print(f"Unexpected balance response format: {balance_data}")
            return None
            
    except Exception as e:
        print(f"Error syncing balance for user_id {user_wallet.user_id}: {str(e)}")
        return None


def _get_user_primary_address(user):
    """Get the user's primary wallet address for RPC asset balance checks."""
    user_wallet = getattr(user, 'user_wallet', None)
    if not user_wallet:
        return None

    main_profile = _get_or_create_main_wallet_profile(user)
    if main_profile:
        return main_profile.address.address

    return None


def _ensure_external_wallet_address(user_wallet, index):
    network_mode = _active_network_mode()
    address_record = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=network_mode,
        account=0,
        index=index,
        is_change=False,
    ).first()

    if address_record:
        return address_record

    wallet_instance = _wallet_for_network(user_wallet)
    address = wallet_instance.get_address(index=index)
    wif = wallet_instance.get_wif(index=index)
    RPC.importprivkey(wif, str(user_wallet.entropy), False)
    address_record, _created = WalletAddress.objects.get_or_create(
        wallet=user_wallet,
        network_mode=network_mode,
        account=0,
        index=index,
        is_change=False,
        defaults={
            'address': address,
            'wif': wif,
        },
    )
    return address_record


def _ensure_change_wallet_address(user_wallet, index=0):
    network_mode = _active_network_mode()
    address_record = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=network_mode,
        account=0,
        index=index,
        is_change=True,
    ).first()

    if address_record:
        return address_record

    wallet_instance = _wallet_for_network(user_wallet)
    address = wallet_instance.get_change_address(index=index)
    wif = wallet_instance.get_change_wif(index=index)
    address_record, _created = WalletAddress.objects.get_or_create(
        wallet=user_wallet,
        network_mode=network_mode,
        account=0,
        index=index,
        is_change=True,
        defaults={
            'address': address,
            'wif': wif,
        },
    )
    return address_record


def _get_or_create_main_wallet_profile(user):
    user_wallet = getattr(user, 'user_wallet', None)
    if not user_wallet:
        return None

    network_mode = _active_network_mode()
    main_profile = WalletProfile.objects.select_related('address').filter(
        wallet=user_wallet,
        network_mode=network_mode,
        is_main=True,
    ).first()
    if main_profile:
        return main_profile

    fallback_profile = WalletProfile.objects.select_related('address').filter(
        wallet=user_wallet,
        network_mode=network_mode,
    ).order_by('created_at', 'id').first()
    if fallback_profile:
        WalletProfile.objects.filter(pk=fallback_profile.pk).update(is_main=True)
        fallback_profile.is_main = True
        return fallback_profile

    address_record = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=network_mode,
        is_change=False
    ).order_by('account', 'index').first()

    try:
        if address_record is None:
            address_record = _ensure_external_wallet_address(user_wallet, index=0)

        profile, _created = WalletProfile.objects.get_or_create(
            wallet=user_wallet,
            network_mode=network_mode,
            address=address_record,
            defaults={
                'name': 'Main',
                'is_main': True,
            },
        )
        if not profile.is_main:
            WalletProfile.objects.filter(
                wallet=user_wallet,
                network_mode=network_mode,
                is_main=True,
            ).exclude(pk=profile.pk).update(is_main=False)
            profile.is_main = True
            profile.save()
        return profile
    except Exception:
        return None


def _get_wallet_profiles(user):
    user_wallet = getattr(user, 'user_wallet', None)
    if not user_wallet:
        return WalletProfile.objects.none()

    preferences = _get_or_create_wallet_preferences(user_wallet)
    _get_or_create_main_wallet_profile(user)

    queryset = WalletProfile.objects.select_related('address').filter(
        wallet=user_wallet,
        network_mode=_active_network_mode(),
    )

    sort_order = getattr(preferences, 'profile_sort_order', 'main_first')
    if sort_order == 'name_asc':
        return queryset.order_by('name', 'address__index', 'created_at', 'id')
    if sort_order == 'name_desc':
        return queryset.order_by('-name', 'address__index', 'created_at', 'id')
    if sort_order == 'index_asc':
        return queryset.order_by('address__index', '-is_main', 'created_at', 'id')
    if sort_order == 'index_desc':
        return queryset.order_by('-address__index', '-is_main', 'created_at', 'id')
    return queryset.order_by('-is_main', 'address__index', 'created_at', 'id')


def _get_or_create_wallet_preferences(user_wallet):
    if not user_wallet:
        return None

    preferences, _created = WalletPreferences.objects.get_or_create(wallet=user_wallet)
    return preferences


def _normalize_transaction_limit(request, preferences=None):
    default_limit = getattr(preferences, 'default_transaction_limit', 'all') or 'all'
    requested_value = str(request.GET.get('limit', '') or '').strip().lower()
    limit_token = requested_value or default_limit

    if limit_token in ('', 'all'):
        return None

    try:
        return max(1, min(250, int(limit_token)))
    except (TypeError, ValueError):
        if default_limit in ('', 'all'):
            return None
        try:
            return max(1, min(250, int(default_limit)))
        except (TypeError, ValueError):
            return None


def _next_external_address_index(user_wallet):
    highest_index = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=_active_network_mode(),
        account=0,
        is_change=False,
    ).order_by('-index').values_list('index', flat=True).first()
    if highest_index is None:
        return 0
    return int(highest_index) + 1


def _redirect_send_funds_to(tab_name):
    return redirect(f"{reverse('send_funds')}#{tab_name}")


def _create_wallet_profile(request):
    user_wallet = getattr(request.user, 'user_wallet', None)
    if not user_wallet:
        messages.error(request, 'No wallet found to create a profile.')
        return _redirect_send_funds_to('profiles')

    profile_name = str(request.POST.get('profile_name', '') or '').strip()
    if not profile_name:
        messages.error(request, 'Profile name is required.')
        return _redirect_send_funds_to('profiles')

    if len(profile_name) > 100:
        messages.error(request, 'Profile name must be 100 characters or less.')
        return _redirect_send_funds_to('profiles')

    network_mode = _active_network_mode()
    if WalletProfile.objects.filter(
        wallet=user_wallet,
        network_mode=network_mode,
        name__iexact=profile_name,
    ).exists():
        messages.error(request, 'A profile with that name already exists on this network.')
        return _redirect_send_funds_to('profiles')

    try:
        with transaction.atomic():
            next_index = _next_external_address_index(user_wallet)
            address_record = _ensure_external_wallet_address(user_wallet, next_index)
            profile = WalletProfile.objects.create(
                wallet=user_wallet,
                address=address_record,
                network_mode=network_mode,
                name=profile_name,
                is_main=not WalletProfile.objects.filter(
                    wallet=user_wallet,
                    network_mode=network_mode,
                    is_main=True,
                ).exists(),
            )
    except (ValidationError, IntegrityError) as exc:
        messages.error(request, f'Unable to create wallet profile: {exc}')
        return _redirect_send_funds_to('profiles')
    except Exception as exc:
        messages.error(request, f'Unable to derive and import a new wallet address: {exc}')
        return _redirect_send_funds_to('profiles')

    messages.success(request, f'Profile "{profile.name}" created for address {profile.address.address}.')
    return _redirect_send_funds_to('profiles')


def _set_main_wallet_profile(request):
    user_wallet = getattr(request.user, 'user_wallet', None)
    if not user_wallet:
        messages.error(request, 'No wallet found to update a profile.')
        return _redirect_send_funds_to('profiles')

    profile_id = str(request.POST.get('profile_id', '') or '').strip()
    if not profile_id:
        messages.error(request, 'Profile selection is required.')
        return _redirect_send_funds_to('profiles')

    profile = WalletProfile.objects.select_related('address').filter(
        wallet=user_wallet,
        network_mode=_active_network_mode(),
        pk=profile_id,
    ).first()
    if not profile:
        messages.error(request, 'Selected profile was not found on the active network.')
        return _redirect_send_funds_to('profiles')

    WalletProfile.objects.filter(
        wallet=user_wallet,
        network_mode=profile.network_mode,
        is_main=True,
    ).exclude(pk=profile.pk).update(is_main=False)
    if not profile.is_main:
        profile.is_main = True
        profile.save()

    messages.success(request, f'"{profile.name}" is now your main wallet profile.')
    return _redirect_send_funds_to('profiles')


def _derive_user_wif_for_address(user, address):
    """Derive the signing WIF for an address from user entropy at runtime."""
    user_wallet = getattr(user, 'user_wallet', None)
    if not user_wallet:
        raise Exception('No wallet found for user.')

    wallet_instance = _wallet_for_network(user_wallet)
    try:
        return wallet_instance.get_wif_for_address(address)
    except ValueError as exc:
        raise Exception(f'Unable to derive signing key for address {address}: {str(exc)}')


@login_required
def asset_creation_wizard(request):
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, 'Admin privileges are required to create assets.')
        return redirect('portfolio')

    network_mode = _active_network_mode()
    if request.method == 'POST':
        if network_mode != 'testnet':
            messages.error(request, 'Asset creation through this wizard is restricted to testnet.')
            return redirect('asset_creation_wizard')

        source_address = _get_user_primary_address(request.user)
        if not source_address:
            messages.error(request, 'No primary wallet address is available for asset creation.')
            return redirect('asset_creation_wizard')

        try:
            source_wif = _derive_user_wif_for_address(request.user, source_address)
            result = create_asset_for_user(
                user=request.user,
                source_address=source_address,
                source_wif=source_wif,
                asset_kind=request.POST.get('asset_kind'),
                asset_name=request.POST.get('asset_name'),
                parameters={
                    'quantity': request.POST.get('quantity'),
                    'units': request.POST.get('units'),
                    'reissuable': request.POST.get('reissuable') == 'on',
                    'ipfs_hash': request.POST.get('ipfs_hash'),
                    'verifier_string': request.POST.get('verifier_string'),
                },
                broadcast=request.POST.get('execution_mode') == 'broadcast',
            )
            if result['broadcast']:
                messages.success(
                    request,
                    f"Broadcast {result['asset_name']}. TXID: {result['txid']}",
                )
            else:
                messages.success(
                    request,
                    f"Mempool accepted {result['asset_name']} without broadcast. "
                    f"Candidate TXID: {result['accepted_txid']}",
                )
        except Exception as exc:
            messages.error(request, f'Asset creation failed: {str(exc)}')
        return redirect('asset_creation_wizard')

    recent_requests = AssetCreationRequest.objects.filter(
        creator=request.user,
        network_mode=network_mode,
    )[:25]
    return render(request, 'portfolio/asset_creation.html', {
        'active_network_mode': network_mode,
        'is_testnet': network_mode == 'testnet',
        'asset_kind_choices': AssetCreationRequest.KIND_CHOICES,
        'recent_requests': recent_requests,
    })


def _get_user_asset_balances(user, sync_tracking=True):
    """Return a dict of asset balances for the user's primary address."""
    address = _get_user_primary_address(user)
    if not address:
        return {}, 'no_wallet'

    try:
        balances = RPC.listassetbalancesbyaddress(address)
    except Exception as e:
        return {}, f'rpc_error: {str(e)}'

    if not isinstance(balances, dict):
        return {}, 'invalid_response'

    asset_balances = {}
    for symbol, amount in balances.items():
        if not symbol or not isinstance(symbol, str):
            continue
        try:
            amount_decimal = Decimal(str(amount))
        except (ValueError, InvalidOperation):
            continue
        if amount_decimal > 0:
            asset_balances[symbol.upper()] = amount_decimal

    if sync_tracking:
        try:
            sync_tracked_assets(user, asset_balances)
        except OperationalError as exc:
            # Asset tracking sync is non-critical for request success.
            if 'database is locked' not in str(exc).lower():
                raise
    return asset_balances, None


def _get_stored_user_asset_balances(user, network_mode):
    return {
        holding.asset.symbol: holding.quantity
        for holding in TrackedAssetHolding.objects.select_related('asset').filter(
            user=user,
            asset__network_mode=network_mode,
            quantity__gt=0,
        )
    }


def _get_stored_admin_assets(user, network_mode):
    return sorted(
        symbol
        for symbol, quantity in _get_stored_user_asset_balances(user, network_mode).items()
        if symbol.endswith('!') and quantity > 0
    )


def _format_asset_amount(amount):
    """Format Decimal amounts by trimming trailing zeros or flooring to int if whole."""
    amount_str = format(amount, 'f')
    if '.' in amount_str:
        amount_str = amount_str.rstrip('0').rstrip('.')
    return amount_str or '0'


ASSET_PRESENTATION = {
    TrackedAsset.ASSET_TYPE_MAIN: ('Main asset', 'A', 'Fungible root asset'),
    TrackedAsset.ASSET_TYPE_SUB: ('Sub asset', '/', 'Scoped asset under a root'),
    TrackedAsset.ASSET_TYPE_UNIQUE: ('Unique asset', '#', 'Indivisible collectible'),
    TrackedAsset.ASSET_TYPE_MESSAGING: ('Message channel', '~', 'Broadcast and messaging channel'),
    TrackedAsset.ASSET_TYPE_QUALIFIER: ('Qualifier', 'Q', 'Address eligibility credential'),
    TrackedAsset.ASSET_TYPE_SUB_QUALIFIER: ('Sub qualifier', 'Q/', 'Scoped eligibility credential'),
    TrackedAsset.ASSET_TYPE_RESTRICTED: ('Restricted asset', '$', 'Verifier-controlled asset'),
    TrackedAsset.ASSET_TYPE_ADMIN: ('Administrator', '!', 'Issuance and governance control'),
}


def _build_portfolio_asset_items(user, asset_balances, network_mode):
    tracked_assets = {
        asset.symbol: asset
        for asset in TrackedAsset.objects.filter(
            network_mode=network_mode,
            symbol__in=asset_balances.keys(),
        )
    }
    type_order = {asset_type: index for index, asset_type in enumerate(ASSET_PRESENTATION)}
    items = []

    for symbol, quantity in asset_balances.items():
        tracked_asset = tracked_assets.get(symbol)
        asset_type = tracked_asset.asset_type if tracked_asset else classify_asset_type(symbol)
        type_label, marker, description = ASSET_PRESENTATION[asset_type]
        units = 0 if symbol.endswith('!') else int(tracked_asset.units if tracked_asset else 0)
        items.append({
            'symbol': symbol,
            'quantity': _format_asset_amount(quantity),
            'units': max(0, min(8, units)),
            'asset_type': asset_type,
            'type_label': type_label,
            'marker': marker,
            'description': description,
            'is_unique': asset_type == TrackedAsset.ASSET_TYPE_UNIQUE,
            'is_messaging': asset_type == TrackedAsset.ASSET_TYPE_MESSAGING,
            'is_administrator': asset_type == TrackedAsset.ASSET_TYPE_ADMIN,
        })

    return sorted(items, key=lambda item: (type_order.get(item['asset_type'], 99), item['symbol']))


def _build_recent_issuance_items(user, network_mode):
    items = []
    requests = AssetCreationRequest.objects.filter(
        creator=user,
        network_mode=network_mode,
    ).exclude(broadcast_txid='')[:8]

    for creation_request in requests:
        items.append({
            'asset_name': creation_request.asset_name,
            'asset_kind': creation_request.get_asset_kind_display(),
            'txid': creation_request.broadcast_txid,
            'confirmations': None,
            'is_confirmed': False,
        })

    return items


def _amount_quantum_for_units(units):
    normalized_units = max(0, min(8, int(units or 0)))
    return Decimal('1').scaleb(-normalized_units)


def _step_string_for_units(units):
    quantum = _amount_quantum_for_units(units)
    return format(quantum, 'f')


def _get_asset_units(symbol):
    normalized_symbol = str(symbol or '').strip().upper()
    if normalized_symbol == 'EVR':
        return 8
    if normalized_symbol.endswith('!'):
        return 0

    try:
        asset_data = RPC.getassetdata(normalized_symbol)
    except Exception:
        asset_data = None

    if isinstance(asset_data, dict):
        try:
            return max(0, min(8, int(asset_data.get('units', 8))))
        except (TypeError, ValueError):
            pass

    tracked_asset = TrackedAsset.objects.filter(
        symbol=normalized_symbol,
        network_mode=_active_network_mode(),
    ).only('units').first()
    if tracked_asset is not None:
        return max(0, min(8, int(tracked_asset.units or 0)))

    try:
        return max(0, min(8, int(asset_data.get('units', 8))))
    except (AttributeError, TypeError, ValueError):
        return 8


def _get_stored_asset_units(symbol, network_mode=None):
    normalized_symbol = str(symbol or '').strip().upper()
    if normalized_symbol == 'EVR':
        return 8
    if normalized_symbol.endswith('!'):
        return 0

    units = TrackedAsset.objects.filter(
        symbol=normalized_symbol,
        network_mode=network_mode or _active_network_mode(),
    ).values_list('units', flat=True).first()
    if units is None:
        return 8
    return max(0, min(8, int(units or 0)))


def _normalize_amount_for_units(raw_amount, units):
    try:
        amount = Decimal(str(raw_amount).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Invalid amount specified.')

    if amount <= 0:
        raise ValueError('Amount must be greater than 0.')

    quantum = _amount_quantum_for_units(units)
    normalized = amount.quantize(quantum, rounding=ROUND_DOWN)
    if normalized != amount:
        if int(units or 0) == 0:
            raise ValueError('This asset is indivisible and must be sent as a whole number.')
        raise ValueError(f'Amount exceeds the allowed precision for this asset. Maximum decimal places: {int(units)}.')

    return normalized


def _get_user_wallet_addresses(user, include_change=True):
    user_wallet = getattr(user, 'user_wallet', None)
    if not user_wallet:
        return []

    addresses = OrderedDict()
    primary_address = _get_user_primary_address(user)
    if primary_address:
        addresses[primary_address] = None

    queryset = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=_active_network_mode(),
    )
    if not include_change:
        queryset = queryset.filter(is_change=False)

    for address in queryset.order_by('is_change', 'account', 'index').values_list('address', flat=True):
        normalized_address = str(address or '').strip()
        if normalized_address:
            addresses[normalized_address] = None

    return list(addresses.keys())


def _coerce_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _format_evr_delta(satoshis):
    evr_amount = (Decimal(int(satoshis)) * Decimal('1e-8')).quantize(Decimal('0.00000001'))
    return f'{evr_amount:+.8f} EVR'


def _format_signed_asset_delta(amount):
    sign = '+' if amount > 0 else '-'
    return f'{sign}{_format_asset_amount(abs(amount))}'


def _classify_transaction_direction(evr_delta_sats, asset_deltas):
    has_positive = evr_delta_sats > 0 or any(amount > 0 for amount in asset_deltas.values())
    has_negative = evr_delta_sats < 0 or any(amount < 0 for amount in asset_deltas.values())

    if has_positive and has_negative:
        return 'mixed'
    if has_positive:
        return 'received'
    if has_negative:
        return 'sent'
    return 'neutral'


def _build_transaction_summaries(addresses, txids):
    summaries = {
        txid: {
            'evr_delta_sats': 0,
            'asset_deltas': OrderedDict(),
        }
        for txid in txids
    }

    try:
        deltas = RPC.getaddressdeltas({'addresses': list(addresses)})
    except Exception as exc:
        return summaries, str(exc)

    if not isinstance(deltas, list):
        return summaries, f'Unexpected transaction delta response: {deltas}'

    target_txids = set(txids)
    for delta in deltas:
        txid = str(delta.get('txid') or '').strip()
        if txid not in target_txids:
            continue

        summary = summaries[txid]
        satoshis = delta.get('satoshis')
        if satoshis is not None:
            try:
                summary['evr_delta_sats'] += int(satoshis)
            except (TypeError, ValueError):
                pass

        asset_name = (
            delta.get('assetName')
            or delta.get('assetname')
            or delta.get('asset')
        )
        if not asset_name:
            continue

        asset_delta = None
        for key in ('assetAmount', 'amount', 'quantity'):
            if key in delta:
                asset_delta = _coerce_decimal(delta.get(key))
                if asset_delta is not None:
                    break

        if asset_delta is None:
            continue

        existing_amount = summary['asset_deltas'].get(asset_name, Decimal('0'))
        summary['asset_deltas'][asset_name] = existing_amount + asset_delta

    return summaries, None


def _normalize_transaction_time(tx_detail):
    timestamp = tx_detail.get('blocktime') or tx_detail.get('time')
    if not timestamp:
        return None

    try:
        naive_time = datetime.fromtimestamp(int(timestamp))
    except (TypeError, ValueError, OSError):
        return None

    aware_time = timezone.make_aware(naive_time, timezone.get_current_timezone())
    return timezone.localtime(aware_time)


def _build_wallet_transaction_rows(txids, tx_summaries):
    transactions = []
    detail_errors = []

    for txid in txids:
        tx_detail = {}
        try:
            tx_response = RPC.getrawtransaction(txid, True)
            if isinstance(tx_response, dict):
                tx_detail = tx_response
        except Exception as exc:
            detail_errors.append(f'{txid}: {str(exc)}')

        summary = tx_summaries.get(txid, {'evr_delta_sats': 0, 'asset_deltas': OrderedDict()})
        direction = _classify_transaction_direction(summary['evr_delta_sats'], summary['asset_deltas'])

        asset_changes = []
        for asset_name, amount in summary['asset_deltas'].items():
            if amount == 0:
                continue
            asset_changes.append({
                'asset_name': asset_name,
                'amount': amount,
                'amount_display': f'{_format_signed_asset_delta(amount)} {asset_name}',
            })

        transactions.append({
            'txid': txid,
            'direction': direction,
            'direction_label': direction.replace('_', ' ').title(),
            'confirmations': tx_detail.get('confirmations'),
            'blockhash': tx_detail.get('blockhash'),
            'blocktime': _normalize_transaction_time(tx_detail),
            'size': tx_detail.get('size'),
            'evr_delta_sats': summary['evr_delta_sats'],
            'evr_delta_display': _format_evr_delta(summary['evr_delta_sats']) if summary['evr_delta_sats'] else None,
            'asset_changes': asset_changes,
        })

    return transactions, detail_errors


def _build_transaction_limit_options(selected_limit):
    selected_token = 'all' if selected_limit is None else str(selected_limit)
    options = [
        {'value': 'all', 'label': 'All', 'is_selected': selected_token == 'all'},
    ]
    for option in (25, 50, 100, 250):
        options.append({
            'value': str(option),
            'label': f'Latest {option}',
            'is_selected': selected_token == str(option),
        })
    return options


def _build_load_more_limit_options(selected_limit):
    if selected_limit is None:
        return []

    options = []
    for option in _build_transaction_limit_options(selected_limit):
        if option['value'] == 'all':
            options.append(option)
            continue

        try:
            option_value = int(option['value'])
        except (TypeError, ValueError):
            continue

        if option_value > selected_limit:
            options.append(option)
    return options


def _fetch_safetrade_member_info(api_key, api_secret):
    """Fetch SafeTrade member profile using signed auth headers."""
    nonce = str(int(time.time() * 1000))
    payload = f"{nonce}{api_key}".encode('utf-8')
    signature = hmac.new(
        api_secret.encode('utf-8'),
        payload,
        hashlib.sha256,
    ).hexdigest()

    headers = {
        'X-Auth-Apikey': api_key,
        'X-Auth-Nonce': nonce,
        'X-Auth-Signature': signature,
    }

    try:
        response = requests.get(
            f'{SAFETRADE_BASE_URL}/trade/account/members/me',
            headers=headers,
            timeout=12,
        )
    except requests.RequestException as exc:
        return None, f'Unable to reach SafeTrade: {str(exc)}'

    response_payload = None
    if response.status_code >= 400:
        try:
            response_payload = response.json()
        except ValueError:
            response_payload = None

    if response.status_code == 401:
        if isinstance(response_payload, dict):
            errors = response_payload.get('errors')
            if isinstance(errors, list) and 'authz.apikey_untrusted_ip' in errors:
                server_ip = _get_server_public_ip()
                ip_message = f' Current server egress IP: {server_ip}.' if server_ip else ''
                return None, (
                    'SafeTrade rejected this request because the server IP is not trusted for your API key '
                    '(authz.apikey_untrusted_ip). Add this server IP to your SafeTrade API key allowlist and retry.'
                    f'{ip_message}'
                )
        return None, 'SafeTrade authentication failed. Please verify your API key, secret, and API key permissions.'

    if response.status_code >= 400:
        if isinstance(response_payload, dict):
            errors = response_payload.get('errors')
            if isinstance(errors, list) and errors:
                error_text = ', '.join(str(error) for error in errors)
                return None, f'SafeTrade returned HTTP {response.status_code}: {error_text}'
        return None, f'SafeTrade returned HTTP {response.status_code}. Please try again shortly.'

    try:
        payload = response.json()
    except ValueError:
        return None, 'SafeTrade returned an invalid JSON response.'

    member_info = payload.get('member') if isinstance(payload, dict) else None
    if member_info is None and isinstance(payload, dict):
        member_info = payload.get('data', payload)
    if not isinstance(member_info, dict):
        return None, 'SafeTrade response did not include member information.'

    return member_info, None


def _get_server_public_ip():
    """Best-effort lookup of the server's public egress IP for SafeTrade allowlisting."""
    endpoints = [
        ('https://api.ipify.org?format=json', 'json'),
        ('https://ifconfig.me/ip', 'text'),
    ]

    for url, response_type in endpoints:
        try:
            response = requests.get(url, timeout=5)
            if response.status_code != 200:
                continue

            if response_type == 'json':
                payload = response.json()
                ip_value = payload.get('ip') if isinstance(payload, dict) else None
            else:
                ip_value = response.text.strip()

            if ip_value and isinstance(ip_value, str):
                return ip_value
        except (requests.RequestException, ValueError):
            continue

    return None


# Create your views here.
@login_required
def portfolio(request):
    """Display user's wallet portfolio"""
    # Get the user's wallet if it exists using the OneToOne relationship
    user_wallet = getattr(request.user, 'user_wallet', None)
    safe_trade_credentials = getattr(request.user, 'safe_trade_credentials', None)
    safe_trade_server_ip = None
    wallet_preferences = _get_or_create_wallet_preferences(user_wallet)
    
    # Create wallet on form submission
    if request.method == 'POST':
        action = request.POST.get('action', 'create_wallet')

        if action == 'save_safetrade':
            api_key = request.POST.get('safe_trade_api_key', '').strip()
            api_secret = request.POST.get('safe_trade_api_secret', '').strip()

            if not api_key:
                messages.error(request, 'SafeTrade API key is required.')
                return redirect('portfolio')

            if not api_secret and not safe_trade_credentials:
                messages.error(request, 'SafeTrade API secret is required for first-time setup.')
                return redirect('portfolio')

            if safe_trade_credentials:
                safe_trade_credentials.api_key = api_key
                if api_secret:
                    safe_trade_credentials.api_secret = api_secret
                safe_trade_credentials.save(update_fields=['api_key', 'api_secret', 'updated_at'])
            else:
                safe_trade_credentials = SafeTradeCredentials.objects.create(
                    user=request.user,
                    api_key=api_key,
                    api_secret=api_secret,
                )

            member_info, error = _fetch_safetrade_member_info(
                safe_trade_credentials.api_key,
                safe_trade_credentials.api_secret,
            )
            if error:
                messages.error(request, error)
            else:
                safe_trade_credentials.member_info = member_info
                safe_trade_credentials.last_synced_at = timezone.now()
                safe_trade_credentials.save(update_fields=['member_info', 'last_synced_at', 'updated_at'])
                messages.success(request, 'SafeTrade credentials saved and account info synced successfully.')

            return redirect('portfolio')

        if action == 'refresh_safetrade':
            if not safe_trade_credentials:
                messages.error(request, 'Save your SafeTrade API credentials first.')
                return redirect('portfolio')

            member_info, error = _fetch_safetrade_member_info(
                safe_trade_credentials.api_key,
                safe_trade_credentials.api_secret,
            )
            if error:
                messages.error(request, error)
            else:
                safe_trade_credentials.member_info = member_info
                safe_trade_credentials.last_synced_at = timezone.now()
                safe_trade_credentials.save(update_fields=['member_info', 'last_synced_at', 'updated_at'])
                messages.success(request, 'SafeTrade account info refreshed successfully.')
            return redirect('portfolio')

        # Create wallet if it doesn't exist
        if not user_wallet:
            # Get wallet name and passphrase from form
            wallet_name = request.POST.get('wallet_name', '').strip()
            passphrase = request.POST.get('passphrase', '').strip()
            
            # Validate wallet name
            if not wallet_name:
                messages.error(request, 'Wallet name is required.')
                return render(request, 'portfolio/wallet.html', {'user_wallet': user_wallet})
            
            # Validate wallet name length
            if len(wallet_name) > 100:
                messages.error(request, 'Wallet name must be 100 characters or less.')
                return render(request, 'portfolio/wallet.html', {'user_wallet': user_wallet})
            
            # Start by generating new entropy
            entropy = BIP39Entropy.generate(128)
            
            # Save the new wallet to the database
            user_wallet = UserWallet.objects.create(
                user=request.user,
                name=wallet_name,
                entropy=entropy,
                passphrase=passphrase
            )
            # Import the wallet into the RPC and store address details
            wallet_instance = Wallet(
                user_wallet.entropy,
                user_wallet.passphrase,
                network_mode=_active_network_mode(),
            )
            wallet = wallet_instance.get_wallet().from_derivation(
                BIP44Derivation(
                    cryptocurrencies.Evrmore.COIN_TYPE,
                    0,
                    CHANGES.EXTERNAL_CHAIN,
                    0,
                )
            )
            address = wallet.address()
            wif = wallet.wif()
            RPC.importprivkey(wif, str(user_wallet.entropy), False)
            WalletAddress.objects.get_or_create(
                wallet=user_wallet,
                network_mode=_active_network_mode(),
                account=0,
                index=0,
                is_change=False,
                defaults={
                    'address': address,
                    'wif': wif,
                },
            )
            messages.success(request, f'Wallet "{wallet_name}" created successfully!')
            return redirect('portfolio')
    
    network_mode = _active_network_mode()
    asset_balances = _get_stored_user_asset_balances(request.user, network_mode) if user_wallet else {}
    asset_balance_error = None

    context = {
        'user_wallet': user_wallet,
        'wallet_preferences': wallet_preferences,
        'safe_trade_credentials': safe_trade_credentials,
        'safe_trade_member_info': safe_trade_credentials.member_info if safe_trade_credentials else None,
        'safe_trade_server_ip': safe_trade_server_ip,
        'portfolio_assets': _build_portfolio_asset_items(request.user, asset_balances, network_mode),
        'asset_balance_error': asset_balance_error,
        'recent_asset_issuances': _build_recent_issuance_items(request.user, network_mode),
    }
    return render(request, 'portfolio/wallet.html', context)


@login_required
def messaging_channel_management(request):
    """Admin panel for creating and reviewing strict messaging channel console assets."""
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, 'Admin privileges are required to manage messaging channels.')
        return redirect('portfolio')

    current_network_mode = _active_network_mode()
    owned_admin_assets = _get_stored_admin_assets(request.user, current_network_mode)
    is_testnet = current_network_mode == 'testnet'
    selected_policy = None

    if is_testnet and request.GET.get('policy_id'):
        selected_policy = MessageChannelPolicy.objects.filter(
            id=request.GET.get('policy_id'),
            manager_account=request.user,
        ).first()

    creation_result = None
    if request.method == 'POST':
        action = str(request.POST.get('action') or 'create_channel').strip().lower()

        if action in {'subscribe_channel', 'unsubscribe_channel'}:
            asset_name = str(request.POST.get('asset_name') or '').strip().upper()
            try:
                result = set_channel_subscription(
                    asset_name,
                    subscribe=action == 'subscribe_channel',
                    network_mode=current_network_mode,
                )
                verb = 'Subscribed to' if result['subscribed'] else 'Unsubscribed from'
                messages.success(request, f"{verb} verified channel {result['asset_name']}.")
            except Exception as exc:
                messages.error(request, f'Unable to update channel subscription: {exc}')
            return redirect('messaging_channel_management')

        if action == 'burn_channel_revision':
            asset_name = str(request.POST.get('asset_name') or '').strip().upper()
            if request.POST.get('confirm_revision_burn') != '1':
                messages.error(request, 'Revision burn confirmation is required.')
                return redirect('messaging_channel_management')
            try:
                result = burn_channel_asset_for_revision(
                    request.user,
                    asset_name,
                    network_mode=current_network_mode,
                )
                messages.success(
                    request,
                    f"Burned {result['asset_name']} for revision in raw transaction {result['txid']}.",
                )
            except Exception as exc:
                messages.error(request, f'Unable to burn messaging channel for revision: {exc}')
            return redirect('messaging_channel_management')

        if action == 'deprecate_policy':
            policy_id = request.POST.get('policy_id')
            policy = MessageChannelPolicy.objects.filter(
                id=policy_id,
                manager_account=request.user,
            ).first()
            if not policy:
                messages.error(request, 'Policy not found or not managed by your account.')
            elif policy.status != 'active':
                messages.info(request, 'Policy is already not active.')
            else:
                policy.status = 'deprecated'
                policy.save(update_fields=['status', 'updated_at'])
                messages.success(request, f'Deprecated policy {policy.channel_key} v{policy.version}.')
            return redirect('messaging_channel_management')

        if action == 'promote_proposal_to_draft':
            if not is_testnet:
                messages.error(request, 'Draft promotion from proposals is restricted to testnet.')
                return redirect('messaging_channel_management')

            policy_id = request.POST.get('policy_id')
            proposal_version = request.POST.get('proposal_version')
            source_policy = MessageChannelPolicy.objects.filter(
                id=policy_id,
                manager_account=request.user,
                network_mode='testnet',
            ).first()
            proposal = _get_testnet_proposal_by_version(proposal_version or 0)

            if not source_policy or not proposal:
                messages.error(request, 'Unable to resolve the selected policy proposal for draft promotion.')
                return redirect('messaging_channel_management')

            draft_version = int(proposal['target_version'])
            existing_draft = MessageChannelPolicy.objects.filter(
                channel_key=source_policy.channel_key,
                network_mode='testnet',
                version=draft_version,
            ).first()
            if existing_draft:
                messages.info(request, f'Draft v{draft_version} already exists for {source_policy.channel_key}.')
                return redirect('messaging_channel_management')

            strict_rules = dict(source_policy.strict_rules or {})
            strict_rules.update({
                'description': proposal['summary'],
                'proposal_changes': proposal['changes'],
                'proposal_name': proposal['name'],
                'proposal_status': 'draft',
            })

            draft_channel_name = source_policy.channel_name
            chain_metadata_status = source_policy.chain_metadata_status
            chain_metadata_error = source_policy.chain_metadata_error
            if source_policy.channel_key == UNIFIED_WORKFLOW_CHANNEL_KEY:
                strict_rules.update({
                    'requires_new_channel_asset': True,
                    'required_channel_tag': UNIFIED_WORKFLOW_CHANNEL_TAG,
                    **UNIFIED_WORKFLOW_STRICT_RULES,
                })
                source_root = source_policy.channel_name.split('~', 1)[0].strip().upper()
                draft_channel_name = f'{source_root}~{UNIFIED_WORKFLOW_CHANNEL_TAG}'
                chain_metadata_status = MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING
                chain_metadata_error = (
                    'A v5 draft requires issuance and verification of its distinct immutable channel asset.'
                )

            MessageChannelPolicy.objects.create(
                channel_key=source_policy.channel_key,
                channel_name=draft_channel_name,
                network_mode='testnet',
                version=draft_version,
                status='draft',
                owner_account=source_policy.owner_account,
                manager_account=request.user,
                schema_name=source_policy.schema_name,
                schema_version=source_policy.schema_version,
                allowed_stages=source_policy.allowed_stages,
                strict_rules=strict_rules,
                chain_metadata_status=chain_metadata_status,
                chain_metadata_error=chain_metadata_error,
                is_locked=source_policy.is_locked,
            )
            messages.success(request, f'Created draft policy v{draft_version} for {source_policy.channel_key} from the selected proposal.')
            return redirect('messaging_channel_management')

        allowed_stages_raw = str(request.POST.get('allowed_stages') or '').strip()
        allowed_stages = [
            stage.strip().lower()
            for stage in allowed_stages_raw.split(',')
            if stage.strip()
        ]
        if not allowed_stages:
            allowed_stages = list(DEFAULT_ALLOWED_STAGES)

        strict_rules = {
            'console_mode': 'strict',
            'immutable_payload': request.POST.get('immutable_payload') == 'on',
            'allow_unregistered_keys': request.POST.get('allow_unregistered_keys') == 'on',
            'auto_broadcast': request.POST.get('auto_broadcast') == 'on',
        }

        payload = {
            'admin_asset': request.POST.get('admin_asset'),
            'channel_tag': request.POST.get('channel_tag'),
            'channel_key': request.POST.get('channel_key'),
            'channel_name': request.POST.get('channel_name'),
            'network_mode': request.POST.get('network_mode') or current_network_mode,
            'metadata': {
                'description': request.POST.get('description') or '',
                'allowed_stages': allowed_stages,
                'strict_rules': strict_rules,
            },
        }

        selected_admin_asset_value = str(payload.get('admin_asset') or '').strip().upper()
        if selected_admin_asset_value not in owned_admin_assets:
            messages.error(request, 'Select an admin asset that you currently own on this network.')
            return redirect('messaging_channel_management')

        requested_version = str(request.POST.get('channel_version') or '').strip()
        if requested_version:
            payload['channel_version'] = requested_version

        to_address = str(request.POST.get('to_address') or '').strip()
        change_address = str(request.POST.get('change_address') or '').strip()
        if to_address:
            payload['to_address'] = to_address
        if change_address:
            payload['change_address'] = change_address

        try:
            creation_result = create_channel_console_asset_for_user(request.user, payload)
            if creation_result.get('asset_already_exists'):
                messages.info(
                    request,
                    (
                        f"Channel asset {creation_result['channel_asset_name']} already exists on-chain; "
                        'created a new local policy version without re-issuing the asset.'
                    ),
                )
            messages.success(
                request,
                f"Created channel {creation_result['channel_asset_name']} with policy version {creation_result['channel_policy']['version']}",
            )
            return redirect('messaging_channel_management')
        except ValueError as exc:
            messages.error(request, str(exc))
        except Exception as exc:
            messages.error(request, f'Failed to create messaging channel: {exc}')

    if request.GET.get('export') == 'scan_json':
        count = max(1, min(200, int(request.GET.get('count', 50))))
        start = max(0, int(request.GET.get('start', 0)))
        cache_key = f'channel-scan:{current_network_mode}:{start}:{count}'
        scan_export = None if request.GET.get('refresh') == '1' else cache.get(cache_key)
        if scan_export is None:
            scan_export = scan_channel_console_assets(
                asset_pattern='*~*',
                count=count,
                start=start,
                network_mode=current_network_mode,
            )
            cache.set(cache_key, scan_export, timeout=30)
        return JsonResponse({
            'success': True,
            'network_mode': current_network_mode,
            'count': count,
            'start': start,
            'valid_channels': scan_export.get('valid_channels', []),
            'pending_channels': scan_export.get('pending_channels', []),
            'invalid_channels': scan_export.get('invalid_channels', []),
            'scan_error': scan_export.get('scan_error', ''),
            'subscription_state_available': scan_export.get('subscription_state_available', False),
        })

    if request.GET.get('export') == 'scan_csv':
        count = max(1, min(200, int(request.GET.get('count', 50))))
        start = max(0, int(request.GET.get('start', 0)))
        scan_export = scan_channel_console_assets(
            asset_pattern='*~*',
            count=count,
            start=start,
            network_mode=current_network_mode,
        )

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="channel_scan.csv"'
        writer = csv.writer(response)
        writer.writerow(['status', 'asset_name', 'channel_key', 'ipfs_cid', 'allowed_stages', 'console_type', 'error'])

        for row in scan_export.get('valid_channels', []):
            writer.writerow([
                'valid',
                row.get('asset_name', ''),
                row.get('channel_key', ''),
                row.get('ipfs_cid', ''),
                '|'.join(row.get('allowed_stages', []) or []),
                row.get('console_type', ''),
                '',
            ])
        for row in scan_export.get('pending_channels', []):
            writer.writerow([
                'pending',
                row.get('asset_name', ''),
                row.get('channel_key', ''),
                row.get('intended_ipfs_cid', ''),
                '',
                '',
                'awaiting_confirmation',
            ])
        for row in scan_export.get('invalid_channels', []):
            writer.writerow([
                'invalid',
                row.get('asset_name', ''),
                '',
                '',
                '',
                '',
                row.get('error', ''),
            ])
        return response

    scan_result = {
        'valid_channels': [],
        'pending_channels': [],
        'invalid_channels': [],
        'scan_error': '',
        'network_mode': current_network_mode,
        'is_deferred': True,
    }

    managed_policies = MessageChannelPolicy.objects.filter(
        manager_account=request.user,
    ).order_by('channel_key', '-version')
    event_rows, event_filters = _channel_event_console(current_network_mode, request.GET)
    event_channels = MessageChannelPolicy.objects.filter(
        network_mode=current_network_mode,
    ).order_by('channel_name', '-version')

    selected_admin_asset = ''
    selected_channel_tag = UNIFIED_WORKFLOW_CHANNEL_TAG
    selected_channel_key = UNIFIED_WORKFLOW_CHANNEL_KEY
    selected_channel_name = ''
    selected_channel_version = str(UNIFIED_WORKFLOW_POLICY_VERSION)
    selected_description = DEFAULT_CHANNEL_DESCRIPTION
    selected_allowed_stages = ', '.join(DEFAULT_ALLOWED_STAGES)
    selected_strict_rules = {
        'immutable_payload': True,
        'allow_unregistered_keys': False,
        'auto_broadcast': False,
    }

    if selected_policy:
        selected_channel_key = selected_policy.channel_key
        selected_channel_name = selected_policy.channel_name
        selected_channel_version = str(int(selected_policy.version))
        if '~' in selected_policy.channel_name:
            root = selected_policy.channel_name.split('~', 1)[0].strip().upper()
            selected_admin_asset = f'{root}!'
            selected_channel_tag = selected_policy.channel_name.split('~', 1)[1].strip()
        if selected_policy.allowed_stages:
            selected_allowed_stages = ', '.join(selected_policy.allowed_stages)
        if isinstance(selected_policy.strict_rules, dict):
            selected_description = str(selected_policy.strict_rules.get('description') or DEFAULT_CHANNEL_DESCRIPTION).strip()
            selected_strict_rules.update({
                'immutable_payload': bool(selected_policy.strict_rules.get('immutable_payload', True)),
                'allow_unregistered_keys': bool(selected_policy.strict_rules.get('allow_unregistered_keys', False)),
                'auto_broadcast': bool(selected_policy.strict_rules.get('auto_broadcast', False)),
            })

    context = {
        'default_allowed_stages': ', '.join(DEFAULT_ALLOWED_STAGES),
        'active_network_mode': current_network_mode,
        'scan_result': scan_result,
        'creation_result': creation_result,
        'owned_admin_assets': owned_admin_assets,
        'managed_policies': managed_policies,
        'event_rows': event_rows,
        'event_filters': event_filters,
        'event_channels': event_channels,
        'selected_policy': selected_policy,
        'selected_admin_asset': selected_admin_asset,
        'selected_channel_tag': selected_channel_tag,
        'selected_channel_key': selected_channel_key,
        'selected_channel_name': selected_channel_name,
        'selected_channel_version': selected_channel_version,
        'selected_description': selected_description,
        'selected_allowed_stages': selected_allowed_stages,
        'selected_strict_rules': selected_strict_rules,
        'is_testnet': is_testnet,
        'proposed_policies': _build_testnet_proposed_policies() if is_testnet else [],
    }
    return render(request, 'portfolio/messaging_channels.html', context)


@login_required
def wallet_preferences(request):
    """Display and update wallet-specific preferences."""
    user_wallet = getattr(request.user, 'user_wallet', None)
    if not user_wallet:
        messages.error(request, 'No wallet found to configure preferences.')
        return redirect('portfolio')

    preferences = _get_or_create_wallet_preferences(user_wallet)

    if request.method == 'POST':
        preferences.default_home_tab = str(request.POST.get('default_home_tab', preferences.default_home_tab) or preferences.default_home_tab).strip()
        preferences.default_send_currency = str(request.POST.get('default_send_currency', preferences.default_send_currency) or preferences.default_send_currency).strip().upper() or 'EVR'
        preferences.default_transaction_limit = str(request.POST.get('default_transaction_limit', preferences.default_transaction_limit) or preferences.default_transaction_limit).strip().lower()
        preferences.default_confirmation_behavior = str(request.POST.get('default_confirmation_behavior', preferences.default_confirmation_behavior) or preferences.default_confirmation_behavior).strip().lower()
        preferences.default_receive_qr_style = str(request.POST.get('default_receive_qr_style', preferences.default_receive_qr_style) or preferences.default_receive_qr_style).strip().lower()
        preferences.address_label_style = str(request.POST.get('address_label_style', preferences.address_label_style) or preferences.address_label_style).strip().lower()
        preferences.profile_sort_order = str(request.POST.get('profile_sort_order', preferences.profile_sort_order) or preferences.profile_sort_order).strip().lower()
        preferences.auto_sync_balance = request.POST.get('auto_sync_balance') == 'on'
        preferences.auto_validate_recipient = request.POST.get('auto_validate_recipient') == 'on'
        preferences.auto_copy_receive_address = request.POST.get('auto_copy_receive_address') == 'on'
        preferences.show_receive_qr = request.POST.get('show_receive_qr') == 'on'
        preferences.show_zero_balances = request.POST.get('show_zero_balances') == 'on'
        preferences.show_change_addresses = request.POST.get('show_change_addresses') == 'on'
        preferences.show_profile_network_badges = request.POST.get('show_profile_network_badges') == 'on'
        preferences.highlight_main_profile = request.POST.get('highlight_main_profile') == 'on'
        preferences.hide_balance_on_open = request.POST.get('hide_balance_on_open') == 'on'
        preferences.compact_cards = request.POST.get('compact_cards') == 'on'
        preferences.confirm_external_links = request.POST.get('confirm_external_links') == 'on'
        preferences.enable_address_tooltips = request.POST.get('enable_address_tooltips') == 'on'
        preferences.prefer_main_profile_on_receive = request.POST.get('prefer_main_profile_on_receive') == 'on'
        preferences.nft_image_uri_template = str(
            request.POST.get('nft_image_uri_template', preferences.nft_image_uri_template)
            or preferences.nft_image_uri_template
        ).strip()

        refresh_seconds_raw = str(request.POST.get('transaction_refresh_seconds', preferences.transaction_refresh_seconds) or preferences.transaction_refresh_seconds).strip()
        try:
            preferences.transaction_refresh_seconds = max(5, min(300, int(refresh_seconds_raw)))
        except (TypeError, ValueError):
            preferences.transaction_refresh_seconds = 30

        if preferences.default_home_tab not in {choice[0] for choice in WalletPreferences.TAB_CHOICES}:
            preferences.default_home_tab = WalletPreferences.TAB_SEND
        if preferences.default_transaction_limit not in {choice[0] for choice in WalletPreferences.TRANSACTION_LIMIT_CHOICES}:
            preferences.default_transaction_limit = WalletPreferences.TRANSACTION_LIMIT_ALL
        if preferences.default_confirmation_behavior not in {choice[0] for choice in WalletPreferences.SEND_CONFIRMATION_CHOICES}:
            preferences.default_confirmation_behavior = 'always'
        if preferences.default_receive_qr_style not in {choice[0] for choice in WalletPreferences.QR_STYLE_CHOICES}:
            preferences.default_receive_qr_style = 'classic'
        if preferences.address_label_style not in {choice[0] for choice in WalletPreferences.ADDRESS_LABEL_CHOICES}:
            preferences.address_label_style = 'full'
        if preferences.profile_sort_order not in {choice[0] for choice in WalletPreferences.PROFILE_SORT_CHOICES}:
            preferences.profile_sort_order = 'main_first'
        if not preferences.nft_image_uri_template:
            preferences.nft_image_uri_template = 'ipfs://{cid}/{filename}'
        if '{cid}' not in preferences.nft_image_uri_template:
            messages.error(request, 'NFT image URI template must include {cid}.')
            return redirect('wallet_preferences')
        if '{filename}' not in preferences.nft_image_uri_template:
            messages.error(request, 'NFT image URI template must include {filename}.')
            return redirect('wallet_preferences')

        preferences.save()
        messages.success(request, 'Wallet preferences updated.')
        return redirect('wallet_preferences')

    context = {
        'user_wallet': user_wallet,
        'preferences': preferences,
        'tab_options': WalletPreferences.TAB_CHOICES,
        'transaction_limit_options': WalletPreferences.TRANSACTION_LIMIT_CHOICES,
        'confirmation_options': WalletPreferences.SEND_CONFIRMATION_CHOICES,
        'qr_style_options': WalletPreferences.QR_STYLE_CHOICES,
        'address_label_options': WalletPreferences.ADDRESS_LABEL_CHOICES,
        'profile_sort_options': WalletPreferences.PROFILE_SORT_CHOICES,
    }
    return render(request, 'portfolio/preferences.html', context)

@login_required
def sync_balance(request):
    """Explicitly refresh the user's EVR and asset balance snapshots."""
    user_wallet = getattr(request.user, 'user_wallet', None)
    
    if not user_wallet:
        messages.error(request, 'No wallet found to sync balance.')
        return redirect('portfolio')
    
    try:
        balance = _sync_user_evr_balance(user_wallet)
        _asset_balances, asset_error = _get_user_asset_balances(request.user)
        if balance is not None:
            # Convert from base unit (satoshis) to display unit by multiplying by 1e-8
            display_balance = balance * Decimal('1e-8')
            if asset_error:
                messages.warning(
                    request,
                    f'EVR synced to {display_balance:.8f}; asset balances could not be refreshed.',
                )
            else:
                messages.success(request, f'Balances synced successfully! Current EVR: {display_balance:.8f}')
        else:
            messages.error(request, 'Failed to sync balance. Please try again.')
    except Exception as e:
        messages.error(request, f'Error syncing balance: {str(e)}')
    
    return redirect('portfolio')

@login_required
def backup_wallet(request):
    """Allow user to backup their wallet mnemonic"""
    user_wallet = getattr(request.user, 'user_wallet', None)
    
    if not user_wallet:
        messages.error(request, 'No wallet found to backup.')
        return redirect('portfolio')
    
    # Generate mnemonic from stored entropy
    wallet_instance = Wallet(user_wallet.entropy, user_wallet.passphrase)
    mnemonic = wallet_instance.get_mnemonic()
    
    context = {
        'mnemonic': mnemonic,
    }
    return render(request, 'portfolio/backup.html', context)


@login_required
def wallet_transactions(request):
    """Display transaction history for the user's primary wallet address."""
    user_wallet = getattr(request.user, 'user_wallet', None)

    if not user_wallet:
        messages.error(request, 'No wallet found to view transactions.')
        return redirect('portfolio')

    preferences = _get_or_create_wallet_preferences(user_wallet)

    wallet_addresses = _get_user_wallet_addresses(request.user, include_change=True)
    if not wallet_addresses:
        messages.error(request, 'Unable to determine your wallet addresses.')
        return redirect('portfolio')

    limit = _normalize_transaction_limit(request, preferences)
    if request.GET.get('limit') is None and preferences and preferences.default_transaction_limit == 'all':
        limit = None

    raw_txids = []
    total_indexed_transactions = 0
    has_more_transactions = False
    txids_error = None
    try:
        txid_response = RPC.getaddresstxids({'addresses': wallet_addresses})
        if not isinstance(txid_response, list):
            raise Exception(f'Unexpected transaction history response: {txid_response}')

        deduplicated_txids = OrderedDict()
        for txid in reversed(txid_response):
            normalized_txid = str(txid or '').strip()
            if not normalized_txid or normalized_txid in deduplicated_txids:
                continue
            deduplicated_txids[normalized_txid] = None
        total_indexed_transactions = len(deduplicated_txids)
        if limit is None:
            raw_txids = list(deduplicated_txids.keys())
        else:
            raw_txids = list(deduplicated_txids.keys())[:limit]
            has_more_transactions = total_indexed_transactions > len(raw_txids)
    except Exception as exc:
        txids_error = str(exc)

    tx_summaries, deltas_error = _build_transaction_summaries(wallet_addresses, raw_txids) if raw_txids else ({}, None)
    transactions, detail_errors = _build_wallet_transaction_rows(raw_txids, tx_summaries) if raw_txids else ([], [])

    context = {
        'user_wallet': user_wallet,
        'wallet_preferences': preferences,
        'address': wallet_addresses[0],
        'address_count': len(wallet_addresses),
        'network_mode': _active_network_mode(),
        'limit': limit,
        'showing_all_transactions': limit is None,
        'limit_options': _build_transaction_limit_options(limit),
        'load_more_limit_options': _build_load_more_limit_options(limit),
        'transactions': transactions,
        'total_indexed_transactions': total_indexed_transactions,
        'has_more_transactions': has_more_transactions,
        'txids_error': txids_error,
        'deltas_error': deltas_error,
        'detail_errors': detail_errors,
    }
    return render(request, 'portfolio/transactions.html', context)

@login_required
def recieve_funds(request):
    """Display wallet address for receiving funds"""
    user_wallet = getattr(request.user, 'user_wallet', None)
    
    if not user_wallet:
        messages.error(request, 'No wallet found to receive funds.')
        return redirect('portfolio')

    preferences = _get_or_create_wallet_preferences(user_wallet)
    
    # Get wallet address
    address = _get_user_primary_address(request.user)
    address_qr_data_uri = build_qr_data_uri(address)
    
    context = {
        'address': address,
        'address_qr_data_uri': address_qr_data_uri,
        'wallet_preferences': preferences,
    }
    return render(request, 'portfolio/receive.html', context)

@login_required
def send_funds(request):
    """Handle sending funds from the user's wallet"""
    user_wallet = getattr(request.user, 'user_wallet', None)
    
    if not user_wallet:
        messages.error(request, 'No wallet found to send funds.')
        return redirect('portfolio')

    preferences = _get_or_create_wallet_preferences(user_wallet)

    if request.method == 'POST':
        action = str(request.POST.get('action', 'send_funds') or 'send_funds').strip().lower()
        if action == 'create_profile':
            return _create_wallet_profile(request)
        if action == 'set_main_profile':
            return _set_main_wallet_profile(request)
    
    network_mode = _active_network_mode()
    asset_balances = _get_stored_user_asset_balances(request.user, network_mode)
    evr_balance = _get_stored_network_balance(user_wallet)
    asset_options = []
    for symbol, amount in sorted(asset_balances.items()):
        if symbol == 'EVR':
            continue
        units = _get_stored_asset_units(symbol, network_mode)
        step = _step_string_for_units(units)
        asset_options.append({
            'symbol': symbol,
            'balance_display': _format_asset_amount(amount),
            'balance_value': str(amount),
            'units': units,
            'step': step,
            'min_value': step,
        })
    receive_address = _get_user_primary_address(request.user)
    receive_address_qr_data_uri = build_qr_data_uri(receive_address)
    main_profile = _get_or_create_main_wallet_profile(request.user)
    wallet_profiles = _get_wallet_profiles(request.user)
    default_send_currency = (preferences.default_send_currency if preferences else 'EVR').upper()
    default_home_tab = preferences.default_home_tab if preferences else 'send'

    if request.method == 'POST':
        asset_balances, _ = _get_user_asset_balances(request.user, sync_tracking=False)
        evr_balance_sats = _sync_user_evr_balance(user_wallet)
        if evr_balance_sats is not None:
            evr_balance = evr_balance_sats * Decimal('1e-8')

        currency = request.POST.get('currency', 'EVR').strip().upper()
        recipient_address = request.POST.get('recipient_address', '').strip()
        amount = request.POST.get('amount', '').strip()
        amount_units = 8 if currency == 'EVR' else _get_asset_units(currency)
        
        # Validate inputs
        if not recipient_address or not amount:
            messages.error(request, 'Recipient address and amount are required.')
            return redirect('send_funds')
        
        try:
            amount_decimal = _normalize_amount_for_units(amount, amount_units)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('send_funds')

        if currency == 'EVR':
            if amount_decimal > evr_balance:
                messages.error(request, 'Amount exceeds your EVR balance.')
                return redirect('send_funds')
        else:
            asset_balance = asset_balances.get(currency)
            if asset_balance is None:
                messages.error(request, 'Selected asset not found in your wallet.')
                return redirect('send_funds')
            if amount_decimal > asset_balance:
                messages.error(request, 'Amount exceeds your asset balance.')
                return redirect('send_funds')
        
        from_address = _get_user_primary_address(request.user)
        if not from_address:
            messages.error(request, 'Unable to determine a source wallet address.')
            return redirect('send_funds')

        try:
            sender_wif = _derive_user_wif_for_address(request.user, from_address)
        except Exception as e:
            messages.error(request, f'Unable to derive sender signing key: {str(e)}')
            return redirect('send_funds')

        # Create and send transaction via createrawtransaction
        try:
            if currency == 'EVR':
                tx_result = create_and_send_evr_transaction(
                    from_address=from_address,
                    to_address=recipient_address,
                    amount_evr=amount_decimal,
                    wif_keys=[sender_wif],
                )
            else:
                tx_result = create_and_send_asset_transfer_transaction(
                    from_address=from_address,
                    to_address=recipient_address,
                    asset_name=currency,
                    asset_quantity=amount_decimal,
                    wif_keys=[sender_wif],
                )

            txid = tx_result['txid']
            messages.success(request, f'Successfully sent {amount_decimal} to {recipient_address}. Transaction ID: {txid}')
        except Exception as e:
            messages.error(request, f'Error sending funds: {str(e)}')
        
        return redirect('send_funds')
    
    return render(request, 'portfolio/send.html', {
        'asset_options': asset_options,
        'evr_balance': evr_balance,
        'main_profile': main_profile,
        'wallet_profiles': wallet_profiles,
        'wallet_preferences': preferences,
        'default_send_currency': default_send_currency,
        'default_home_tab': default_home_tab,
        'receive_address': receive_address,
        'receive_address_qr_data_uri': receive_address_qr_data_uri,
    })


@login_required
@require_http_methods(["GET"])
def validate_address(request):
    """Validate an Evrmore address via RPC."""
    address = request.GET.get('address', '').strip()
    if not address:
        return JsonResponse({'isvalid': False})

    try:
        result = RPC.validateaddress(address)
        if isinstance(result, dict) and 'isvalid' in result:
            return JsonResponse({'isvalid': bool(result['isvalid'])})
    except Exception:
        pass

    return JsonResponse({'isvalid': False})


@login_required
@require_http_methods(["GET"])
def address_qr(request):
    """Generate a QR image payload for a provided address-like string."""
    address = request.GET.get('address', '').strip()
    if not address:
        return JsonResponse({'ok': False, 'error': 'Address is required.'}, status=400)

    if len(address) > 256:
        return JsonResponse({'ok': False, 'error': 'Address exceeds maximum length.'}, status=400)

    qr_data_uri = build_qr_data_uri(address)
    if not qr_data_uri:
        return JsonResponse({'ok': False, 'error': 'Unable to generate QR code.'}, status=500)

    return JsonResponse({
        'ok': True,
        'address': address,
        'qr_data_uri': qr_data_uri,
    })
