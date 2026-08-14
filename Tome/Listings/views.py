from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
import base64
import logging
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import mimetypes
from django.db import IntegrityError, transaction
from django.db.models import DecimalField, Exists, F, OuterRef, Q, Sum
from django.db.models import ExpressionWrapper
from io import BytesIO
from django.utils import timezone
from django.utils.text import get_valid_filename
from django.urls import reverse
from django.views.decorators.http import require_POST
from urllib.parse import urlencode
from .models import (
    Listing, ListingItem, TradingPair, LimitOrder,
    MarketFavorite, MarketOrder, StopLossOrder, OrderExecution, BalanceLock,
    UniqueAssetMintRequest,
)
from Wallet.models import TrackedAssetHolding, WalletAddress
from Wallet.wallet import Wallet
from Wallet.asset_tracking import classify_asset_type, sync_tracked_assets
from Wallet.models import TrackedAsset
from Wallet.asset_units import amount_quantum_for_units, get_asset_units, normalize_amount_for_asset
from Wallet.rpc import (
    InsufficientSpendableBalance,
    create_and_send_atomic_asset_asset_swap_transaction,
    create_and_send_atomic_asset_evr_swap_transaction,
    create_and_send_issue_unique_transaction,
)
from Tome.rpc_client import RPC, get_current_network_mode
from Settings.access import FEATURE_MARKET_MANAGEMENT, user_has_feature_access
from Media.kubo_api import KuboAPIUploader
from Media.models import IPFSUpload
from Tome.qr import build_qr_data_uri
from DeFi.message_channels import (
    ATOMIC_SWAP_REQUIRED_STAGES,
    get_active_atomic_swap_policy,
    record_atomic_swap_stage_event,
    record_market_stage_event,
)
from .metadata import (
    build_unique_metadata_payload,
    extract_cid_from_uri,
    normalize_unique_asset_metadata,
    validate_unique_asset_metadata,
)


logger = logging.getLogger(__name__)


def _redirect_to_order_book(pair_id=None):
    if not pair_id:
        return redirect('markets')
    pair_slug = TradingPair.objects.filter(pk=pair_id).values_list('pair_slug', flat=True).first()
    if not pair_slug:
        return redirect('markets')
    return redirect('dex_orderbook', pair_slug=pair_slug)
import json
import os
import uuid
MARKET_QUOTE_TOKEN = 'EVR'
MARKETABLE_ASSET_TYPES = {
    TrackedAsset.ASSET_TYPE_MAIN,
    TrackedAsset.ASSET_TYPE_SUB,
    TrackedAsset.ASSET_TYPE_RESTRICTED,
}
UNIQUE_ASSET_TYPES = {TrackedAsset.ASSET_TYPE_UNIQUE}
ADMIN_ASSET_TYPES = {TrackedAsset.ASSET_TYPE_ADMIN}
SETTLEMENT_ASSET_TYPES = {
    TrackedAsset.ASSET_TYPE_MAIN,
    TrackedAsset.ASSET_TYPE_SUB,
}


def _is_unique_asset(symbol):
    return classify_asset_type(str(symbol or '').strip()) in UNIQUE_ASSET_TYPES


def _is_admin_asset(symbol):
    return classify_asset_type(str(symbol or '').strip()) in ADMIN_ASSET_TYPES


def _get_asset_ipfs_cid(asset_data):
    if not isinstance(asset_data, dict):
        return ''
    for key in ('ipfs_hash', 'ipfshash', 'ipfs'):
        value = str(asset_data.get(key) or '').strip()
        if value:
            return value
    return ''


def _get_nft_image_uri_template(user):
    user_wallet = getattr(user, 'user_wallet', None)
    wallet_preferences = getattr(user_wallet, 'preferences', None) if user_wallet else None
    template = getattr(wallet_preferences, 'nft_image_uri_template', '') if wallet_preferences else ''
    return template or 'ipfs://{cid}/{filename}'


def _build_nft_image_uri(user, cid, original_filename):
    template = _get_nft_image_uri_template(user)
    safe_filename = get_valid_filename(os.path.basename(str(original_filename or 'asset').strip() or 'asset'))
    return template.replace('{cid}', str(cid)).replace('{filename}', safe_filename)


def _upload_qr_image_to_ipfs(qr_payload, file_name='qrcode.svg'):
    data_uri = build_qr_data_uri(qr_payload)
    if not data_uri:
        return None

    prefix = 'data:image/svg+xml;base64,'
    if not data_uri.startswith(prefix):
        return None

    svg_bytes = base64.b64decode(data_uri[len(prefix):])
    return KuboAPIUploader().upload_fileobj(
        BytesIO(svg_bytes),
        file_name=file_name,
        pin=True,
        cid_version=0,
    )


def _is_image_filename(filename):
    mime_type, _ = mimetypes.guess_type(str(filename or ''))
    return bool(mime_type and mime_type.startswith('image/'))


def _is_uploaded_image(uploaded_file):
    if not uploaded_file:
        return False
    content_type = str(getattr(uploaded_file, 'content_type', '') or '')
    if content_type.startswith('image/'):
        return True
    return _is_image_filename(getattr(uploaded_file, 'name', ''))


def _get_settlement_token_options(network_mode):
    tracked = TrackedAsset.objects.filter(
        network_mode=network_mode,
        asset_type__in=SETTLEMENT_ASSET_TYPES,
    ).order_by('symbol')
    symbols = []
    seen = {'EVR'}
    for asset in tracked:
        symbol = str(asset.symbol or '').strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return ['EVR', *symbols]


def _get_settlement_token_units(network_mode):
    units_by_symbol = {'EVR': 8}
    for symbol in _get_settlement_token_options(network_mode):
        if symbol == 'EVR':
            continue
        units_by_symbol[symbol] = get_asset_units(symbol, network_mode=network_mode)
    return units_by_symbol


def _step_string_for_units(units):
    normalized_units = max(0, min(8, int(units or 0)))
    if normalized_units == 0:
        return '1'
    return f"0.{'0' * (normalized_units - 1)}1"


def _refresh_recent_mint_request_statuses(user, network_mode, max_items=25):
    requests = list(
        UniqueAssetMintRequest.objects.filter(
            creator=user,
            network_mode=network_mode,
            status__in=(
                UniqueAssetMintRequest.STATUS_PENDING,
                UniqueAssetMintRequest.STATUS_BROADCAST,
            ),
        ).exclude(mint_txid='').order_by('-created_at')[:max_items]
    )

    now = timezone.now()
    for mint_request in requests:
        try:
            tx_data = RPC.gettransaction(mint_request.mint_txid)
            confirmations = int((tx_data or {}).get('confirmations', 0))
            mint_request.confirmation_depth = max(0, confirmations)
            mint_request.last_checked_at = now
            mint_request.status = (
                UniqueAssetMintRequest.STATUS_CONFIRMED
                if confirmations > 0
                else UniqueAssetMintRequest.STATUS_BROADCAST
            )
            mint_request.error_message = ''
            mint_request.save(update_fields=['confirmation_depth', 'last_checked_at', 'status', 'error_message', 'updated_at'])
        except Exception as exc:
            mint_request.last_checked_at = now
            mint_request.error_message = str(exc)
            mint_request.save(update_fields=['last_checked_at', 'error_message', 'updated_at'])


def _is_marketable_token_asset(symbol):
    return classify_asset_type(str(symbol or '').strip()) in MARKETABLE_ASSET_TYPES


def _market_instrument_type(symbol):
    if classify_asset_type(str(symbol or '').strip()) == TrackedAsset.ASSET_TYPE_RESTRICTED:
        return TradingPair.INSTRUMENT_SECURITY_CAPABLE
    return TradingPair.INSTRUMENT_TOKEN


def _marketable_asset_balances(asset_balances):
    return {
        symbol: balance
        for symbol, balance in asset_balances.items()
        if symbol != MARKET_QUOTE_TOKEN and _is_marketable_token_asset(symbol)
    }


def _get_user_token_balance(user, token_symbol):
    """Return the live chain balance, defaulting to zero when unavailable."""
    balance, _error = _fetch_user_token_balance(user, token_symbol)
    return balance


def _fetch_user_token_balance(user, token_symbol):
    """Return a live chain balance and an explicit refresh error."""
    address = _get_user_primary_address(user)
    if not address:
        return Decimal('0'), 'no_wallet_address'

    try:
        if token_symbol == 'EVR':
            balance_info = RPC.getaddressbalance(address)
            if isinstance(balance_info, dict) and 'balance' in balance_info:
                return Decimal(str(balance_info['balance'])) * Decimal('0.00000001'), ''
            return Decimal('0'), 'invalid_evr_balance_response'

        balances = RPC.listassetbalancesbyaddress(address) or {}
        if not isinstance(balances, dict):
            return Decimal('0'), 'invalid_asset_balance_response'
        return Decimal(str(balances.get(str(token_symbol).upper(), 0))), ''
    except Exception as exc:
        return Decimal('0'), str(exc)


def _get_user_available_token_balance(user, token_symbol, live_balance=None):
    if live_balance is None:
        live_balance = _get_user_token_balance(user, token_symbol)
    reserved_balance = Decimal(str(BalanceLock.get_total_locked(user, token_symbol)))
    return max(Decimal('0'), live_balance - reserved_balance)


def _get_stored_user_token_balance(user, token_symbol, network_mode):
    normalized_symbol = str(token_symbol or '').strip().upper()
    if normalized_symbol == MARKET_QUOTE_TOKEN:
        wallet = getattr(user, 'user_wallet', None)
        if wallet is None:
            return Decimal('0')
        field_name = f'evr_liquidity_{network_mode}'
        return Decimal(str(getattr(wallet, field_name, 0) or 0))

    quantity = TrackedAssetHolding.objects.filter(
        user=user,
        asset__symbol=normalized_symbol,
        asset__network_mode=network_mode,
    ).values_list('quantity', flat=True).first()
    return Decimal(str(quantity or 0))


def _get_stored_asset_units(token_symbol, network_mode, default_units=8):
    normalized_symbol = str(token_symbol or '').strip().upper()
    if normalized_symbol == MARKET_QUOTE_TOKEN:
        return 8
    units = TrackedAsset.objects.filter(
        symbol=normalized_symbol,
        network_mode=network_mode,
    ).values_list('units', flat=True).first()
    if units is None:
        return default_units
    return max(0, min(8, int(units)))


def _get_verified_available_token_balance(user, token_symbol):
    live_balance, balance_error = _fetch_user_token_balance(user, token_symbol)
    if balance_error:
        raise ValueError(f'Unable to verify live {token_symbol} balance: {balance_error}')
    return _get_user_available_token_balance(
        user,
        token_symbol,
        live_balance=live_balance,
    )


def _order_reservation(order):
    if order.side == 'buy':
        return order.trading_pair.quote_token, order.remaining_quantity * order.price
    return order.trading_pair.base_token, order.remaining_quantity


def _sync_order_balance_lock(order):
    asset_symbol, amount = _order_reservation(order)
    balance_lock = BalanceLock.objects.filter(limit_order=order).first()
    if amount <= 0 or order.status not in {'pending', 'partial'}:
        if balance_lock and balance_lock.status == 'locked':
            balance_lock.status = 'consumed' if order.status == 'filled' else 'released'
            balance_lock.amount = Decimal('0')
            balance_lock.released_at = timezone.now()
            balance_lock.save(update_fields=['status', 'amount', 'released_at'])
        return balance_lock

    if balance_lock is None:
        return BalanceLock.objects.create(
            user=order.user,
            asset_symbol=asset_symbol,
            amount=amount,
            status='locked',
            limit_order=order,
        )

    balance_lock.asset_symbol = asset_symbol
    balance_lock.amount = amount
    balance_lock.status = 'locked'
    balance_lock.released_at = None
    balance_lock.save(update_fields=['asset_symbol', 'amount', 'status', 'released_at'])
    return balance_lock


def _create_reserved_limit_order(user, trading_pair, side, price, quantity):
    asset_symbol = trading_pair.quote_token if side == 'buy' else trading_pair.base_token
    required_amount = price * quantity if side == 'buy' else quantity
    with transaction.atomic():
        user._meta.model.objects.select_for_update().get(pk=user.pk)
        available_balance = _get_verified_available_token_balance(user, asset_symbol)
        if available_balance < required_amount:
            raise ValueError(
                f'Insufficient {asset_symbol} balance. '
                f'Required: {required_amount:.8f}, Available: {available_balance:.8f}'
            )
        order = LimitOrder.objects.create(
            user=user,
            trading_pair=trading_pair,
            side=side,
            price=price,
            quantity=quantity,
            status='pending',
        )
        _sync_order_balance_lock(order)
    return order


def _market_buy_capacity(
    user,
    trading_pair,
    sell_orders=None,
    quote_available=None,
    base_units=None,
):
    if quote_available is None:
        quote_available = _get_user_available_token_balance(user, trading_pair.quote_token)
    remaining_quote = quote_available
    capacity = Decimal('0')
    orders = sell_orders if sell_orders is not None else LimitOrder.objects.filter(
        trading_pair=trading_pair,
        side='sell',
        status__in=['pending', 'partial'],
    ).exclude(user=user).order_by('price', 'created_at')

    for order in orders:
        if remaining_quote <= 0 or order.price <= 0:
            break
        affordable_quantity = remaining_quote / order.price
        fill_quantity = min(order.remaining_quantity, affordable_quantity)
        capacity += fill_quantity
        remaining_quote -= fill_quantity * order.price
        if fill_quantity < order.remaining_quantity:
            break

    if base_units is None:
        base_units = get_asset_units(
            trading_pair.base_token,
            trading_pair.network_mode,
        )
    return capacity.quantize(amount_quantum_for_units(base_units), rounding=ROUND_DOWN)


def _get_user_primary_address(user):
    """Get the user's primary wallet address for RPC balance checks."""
    user_wallet = getattr(user, 'user_wallet', None)
    if not user_wallet:
        return None

    address_record = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=get_current_network_mode(),
        is_change=False
    ).order_by('account', 'index').first()

    if address_record:
        return address_record.address

    # Fallback to deriving address from wallet entropy/passphrase
    try:
        wallet_instance = Wallet(
            user_wallet.entropy,
            user_wallet.passphrase,
            network_mode=get_current_network_mode(),
        )
        wallet = wallet_instance.get_wallet()
        address = wallet.address()

        WalletAddress.objects.get_or_create(
            wallet=user_wallet,
            network_mode=get_current_network_mode(),
            account=0,
            index=0,
            is_change=False,
            defaults={
                'address': address,
                'wif': wallet.wif(),
            },
        )
        return address
    except Exception:
        return None


def _derive_user_wif_for_address(user, address):
    """Derive the WIF for a user's address from stored entropy/passphrase."""
    user_wallet = getattr(user, 'user_wallet', None)
    if not user_wallet:
        raise Exception('No wallet found for user.')

    wallet_instance = Wallet(
        user_wallet.entropy,
        user_wallet.passphrase,
        network_mode=get_current_network_mode(),
    )
    return wallet_instance.get_wif_for_address(address)


def _settle_market_fill(trading_pair, buyer, seller, quantity, price):
    """Broadcast one raw atomic settlement and return its chain txid."""
    if buyer.pk == seller.pk:
        raise ValueError('Users cannot execute against their own market orders.')

    buyer_address = _get_user_primary_address(buyer)
    seller_address = _get_user_primary_address(seller)
    if not buyer_address or not seller_address:
        raise ValueError('Both market participants must have a primary wallet address.')

    buyer_wif = _derive_user_wif_for_address(buyer, buyer_address)
    seller_wif = _derive_user_wif_for_address(seller, seller_address)
    normalized_quantity = normalize_amount_for_asset(
        quantity,
        trading_pair.base_token,
        trading_pair.network_mode,
        field_label='Fill quantity',
        strict=True,
    )
    quote_quantity = normalize_amount_for_asset(
        normalized_quantity * Decimal(str(price)),
        trading_pair.quote_token,
        trading_pair.network_mode,
        field_label='Fill total',
        strict=True,
    )

    if trading_pair.quote_token == MARKET_QUOTE_TOKEN:
        result = create_and_send_atomic_asset_evr_swap_transaction(
            seller_address=seller_address,
            buyer_address=buyer_address,
            asset_name=trading_pair.base_token,
            asset_quantity=normalized_quantity,
            payment_evr=quote_quantity,
            wif_keys=[seller_wif, buyer_wif],
        )
    else:
        result = create_and_send_atomic_asset_asset_swap_transaction(
            seller_address=seller_address,
            buyer_address=buyer_address,
            seller_asset_name=trading_pair.base_token,
            seller_asset_quantity=normalized_quantity,
            buyer_asset_name=trading_pair.quote_token,
            buyer_asset_quantity=quote_quantity,
            wif_keys=[seller_wif, buyer_wif],
        )

    txid = str(result.get('txid') or '').strip()
    if len(txid) != 64:
        raise ValueError('Market settlement did not return a valid transaction id.')
    return txid


def _get_user_asset_balances(user):
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

    sync_tracked_assets(user, asset_balances)
    return asset_balances, None


def _format_asset_amount(amount):
    """Format Decimal amounts by trimming trailing zeros or flooring to int if whole."""
    amount_str = format(amount, 'f')
    if '.' in amount_str:
        amount_str = amount_str.rstrip('0').rstrip('.')
    return amount_str or '0'


def _create_initial_sell_orders(trading_pair, address, num_orders=100, target_total_price=Decimal('5000')):
    """
    Create initial sell orders for a trading pair with progressive pricing.
    Fetches asset balance from address and divides it across num_orders sell orders.
    Prices start low and increase progressively so total revenue equals target_total_price.
    
    Args:
        trading_pair: TradingPair to create orders for
        address: RPC address to fetch balances from
        num_orders: Number of sell orders to create (default 100)
        target_total_price: Total EVR revenue needed from all orders (default 5,000 for 10x return on 500 EVR creation cost)
    """
    try:
        balances = RPC.listassetbalancesbyaddress(address)
    except Exception as e:
        print(f"RPC error: {e}")
        return 0

    if not isinstance(balances, dict):
        print(f"Balances not a dict: {type(balances)}")
        return 0

    asset_balance = balances.get(trading_pair.base_token)
    if asset_balance is None:
        print(f"Asset {trading_pair.base_token} not found in balances: {balances}")
        return 0

    try:
        total_quantity = Decimal(str(asset_balance))
    except (ValueError, InvalidOperation) as e:
        print(f"Decimal conversion error: {e}")
        return 0

    if total_quantity <= 0:
        print(f"Total quantity <= 0: {total_quantity}")
        return 0

    quantity_per_order = total_quantity / num_orders
    price_per_token = target_total_price / total_quantity
    
    # Create progressive pricing: start at 20% below average, end at 20% above
    # This creates a fair price curve where early orders are cheaper, later orders more expensive
    start_price = price_per_token * Decimal('0.8')  # 80% of average
    end_price = price_per_token * Decimal('1.2')    # 120% of average
    price_increment = (end_price - start_price) / (num_orders - 1) if num_orders > 1 else Decimal('0')
    
    print(f"Creating {num_orders} sell orders for {trading_pair.base_token}/{trading_pair.quote_token}")
    print(f"Total quantity: {total_quantity}, quantity per order: {quantity_per_order}")
    print(f"Target total revenue: {target_total_price} EVR")
    print(f"Price per token: {price_per_token} EVR")
    print(f"Price range: {start_price} EVR (start) to {end_price} EVR (end)")

    from django.contrib.auth.models import User
    
    # Get or create system user
    system_user, created = User.objects.get_or_create(
        username='system',
        defaults={'email': 'system@defitome.local', 'is_active': True}
    )
    
    if not system_user:
        print("Could not create or get system user")
        return 0

    created_count = 0
    try:
        for i in range(num_orders):
            # Calculate price for this order (linear progression)
            order_price = start_price + (price_increment * i)
            
            LimitOrder.objects.create(
                user=system_user,
                trading_pair=trading_pair,
                side='sell',
                price=order_price,
                quantity=quantity_per_order,
                filled_quantity=Decimal('0'),
                status='pending'
            )
            created_count += 1
        
        print(f"Successfully created {created_count} sell orders")
        print(f"First order price: {start_price} EVR, Last order price: {end_price} EVR")
    except Exception as e:
        print(f"Error creating sell orders: {e}")
        return created_count

    return created_count


def _sync_markets_from_address(address):
    """Create EVR markets for assets held at an address, skipping sub-asset issuers."""
    try:
        balances = RPC.listassetbalancesbyaddress(address)
    except Exception:
        return 0

    if not isinstance(balances, dict):
        return 0

    created = 0
    for asset_symbol in balances.keys():
        if not asset_symbol or not isinstance(asset_symbol, str):
            continue
        if asset_symbol.endswith('!'):
            continue
        if asset_symbol == MARKET_QUOTE_TOKEN:
            continue
        if len(asset_symbol) > 10:
            continue

        pair_exists = TradingPair.objects.filter(
            base_token=asset_symbol,
            quote_token=MARKET_QUOTE_TOKEN,
        ).exists()
        if pair_exists:
            continue

        trading_pair = TradingPair.objects.create(
            base_token=asset_symbol,
            quote_token=MARKET_QUOTE_TOKEN,
            instrument_type=_market_instrument_type(asset_symbol),
            created_by=None,
            is_active=True,
        )
        
        # Create initial sell orders for synced markets
        _create_initial_sell_orders(trading_pair, address)
        
        created += 1

    return created

@login_required
def create_listing(request):
    """Create unique-asset-only atomic swap offers with chain-backed metadata."""
    from DeFi.models import SwapOffer
    from django.utils import timezone
    from datetime import timedelta
    from django.contrib.auth.models import User
    from .models import NFT

    asset_balances, balance_error = _get_user_asset_balances(request.user)
    unique_asset_balances = {
        symbol: balance
        for symbol, balance in asset_balances.items()
        if _is_unique_asset(symbol)
    }
    admin_asset_balances = {
        symbol: balance
        for symbol, balance in asset_balances.items()
        if _is_admin_asset(symbol)
    }

    asset_options = [
        {
            'symbol': symbol,
            'balance_display': _format_asset_amount(balance),
            'balance_value': _format_asset_amount(balance),
            'is_nft': True,
        }
        for symbol, balance in sorted(unique_asset_balances.items(), key=lambda item: item[0])
    ]
    admin_asset_options = sorted(admin_asset_balances.keys())
    media_uploads = IPFSUpload.objects.filter(
        user=request.user,
    ).exclude(ipfs_hash='').order_by('-created_at')[:100]
    media_upload_options = [
        upload for upload in media_uploads
        if _is_image_filename(upload.display_filename)
    ]
    current_network = get_current_network_mode()
    active_channel_policy = get_active_atomic_swap_policy(
        current_network,
        required_stages=ATOMIC_SWAP_REQUIRED_STAGES,
    )
    settlement_token_options = _get_settlement_token_options(current_network)
    settlement_token_units = _get_settlement_token_units(current_network)
    all_users = User.objects.exclude(id=request.user.id).filter(is_active=True).order_by('username')

    if balance_error == 'no_wallet':
        messages.error(request, 'No wallet found. Please create a wallet before creating an atomic swap.')
    elif balance_error and balance_error.startswith('rpc_error'):
        messages.error(request, 'Unable to fetch asset balances from RPC. Please try again.')
    elif balance_error == 'invalid_response':
        messages.error(request, 'Unexpected RPC response while fetching asset balances.')

    def _render(extra_context=None):
        nft_image_uri_template = _get_nft_image_uri_template(request.user)
        recent_mint_requests = UniqueAssetMintRequest.objects.filter(
            creator=request.user,
            network_mode=current_network,
        ).order_by('-created_at')[:10]
        preferred_token = (extra_context or {}).get('preferred_token', 'EVR')
        settlement_amount_step = _step_string_for_units(
            settlement_token_units.get(str(preferred_token or 'EVR').strip().upper(), 8),
        )
        context = {
            'asset_options': asset_options,
            'admin_asset_options': admin_asset_options,
            'settlement_token_options': settlement_token_options,
            'settlement_amount_step': settlement_amount_step,
            'asset_balances': asset_balances,
            'all_users': all_users,
            'preferred_token': 'EVR',
            'settlement_token_units': settlement_token_units,
            'network_mode': current_network,
            'nft_image_uri_template': nft_image_uri_template,
            'recent_mint_requests': recent_mint_requests,
            'media_upload_options': media_upload_options,
        }
        if extra_context:
            context.update(extra_context)
        return render(request, 'listings/create_listing.html', context)

    if request.method == 'POST' and request.POST.get('action') == 'refresh_mint_statuses':
        _refresh_recent_mint_request_statuses(request.user, current_network)
        messages.info(request, 'Mint status refresh completed.')
        return redirect('create_listing')

    if request.method == 'POST' and request.POST.get('action') == 'mint_unique_asset':
        mint_admin_asset = request.POST.get('mint_admin_asset', '').strip().upper()
        mint_asset_tag = request.POST.get('mint_asset_tag', '').strip()
        mint_name = request.POST.get('mint_name', '').strip()
        mint_description = request.POST.get('mint_description', '').strip()
        mint_image_file = request.FILES.get('mint_image_file')
        mint_existing_upload_id = request.POST.get('mint_existing_upload_id', '').strip()
        mint_image_uri = ''
        mint_image_cid = ''
        mint_image_name = ''
        selected_media_upload = None

        if mint_image_file and mint_existing_upload_id:
            messages.error(request, 'Choose either a new image upload or an existing media image, not both.')
            return _render({
                'mint_admin_asset': mint_admin_asset,
                'mint_asset_tag': mint_asset_tag,
                'mint_name': mint_name,
                'mint_description': mint_description,
                'mint_existing_upload_id': mint_existing_upload_id,
            })

        if mint_image_file and not _is_uploaded_image(mint_image_file):
            messages.error(request, 'Metadata image must be an image file.')
            return _render({
                'mint_admin_asset': mint_admin_asset,
                'mint_asset_tag': mint_asset_tag,
                'mint_name': mint_name,
                'mint_description': mint_description,
                'mint_existing_upload_id': mint_existing_upload_id,
            })

        if not mint_admin_asset or mint_admin_asset not in admin_asset_balances:
            messages.error(request, 'Select an admin asset from your wallet to mint a unique asset.')
            return _render({'mint_asset_tag': mint_asset_tag, 'mint_name': mint_name, 'mint_description': mint_description})

        if not mint_asset_tag:
            messages.error(request, 'Unique asset tag is required.')
            return _render({'mint_admin_asset': mint_admin_asset, 'mint_asset_tag': mint_asset_tag, 'mint_name': mint_name, 'mint_description': mint_description})

        if '#' in mint_asset_tag or any(char.isspace() for char in mint_asset_tag):
            messages.error(request, 'Unique asset tag must not contain # or whitespace.')
            return _render({'mint_admin_asset': mint_admin_asset, 'mint_asset_tag': mint_asset_tag, 'mint_name': mint_name, 'mint_description': mint_description})

        if mint_existing_upload_id:
            selected_media_upload = IPFSUpload.objects.filter(
                user=request.user,
                pk=mint_existing_upload_id,
            ).exclude(ipfs_hash='').first()
            if not selected_media_upload:
                messages.error(request, 'Select a valid uploaded media file for metadata image.')
                return _render({
                    'mint_admin_asset': mint_admin_asset,
                    'mint_asset_tag': mint_asset_tag,
                    'mint_name': mint_name,
                    'mint_description': mint_description,
                    'mint_existing_upload_id': mint_existing_upload_id,
                })
            if not _is_image_filename(selected_media_upload.display_filename):
                messages.error(request, 'Selected media must be an image file for metadata v1.')
                return _render({
                    'mint_admin_asset': mint_admin_asset,
                    'mint_asset_tag': mint_asset_tag,
                    'mint_name': mint_name,
                    'mint_description': mint_description,
                    'mint_existing_upload_id': mint_existing_upload_id,
                })

        root_name = mint_admin_asset[:-1]
        if not root_name:
            messages.error(request, 'Invalid admin asset selected for unique minting.')
            return _render({'mint_admin_asset': mint_admin_asset, 'mint_asset_tag': mint_asset_tag, 'mint_name': mint_name, 'mint_description': mint_description})

        unique_asset_name = f'{root_name}#{mint_asset_tag}'
        if UniqueAssetMintRequest.objects.filter(
            network_mode=current_network,
            unique_asset_name=unique_asset_name,
        ).exclude(status=UniqueAssetMintRequest.STATUS_FAILED).exists():
            messages.error(request, f'A mint record for {unique_asset_name} already exists on this network.')
            return _render({'mint_admin_asset': mint_admin_asset, 'mint_asset_tag': mint_asset_tag, 'mint_name': mint_name, 'mint_description': mint_description})

        try:
            if mint_image_file:
                image_upload = KuboAPIUploader().upload_fileobj(
                    mint_image_file,
                    file_name=mint_image_file.name,
                    pin=True,
                    cid_version=0,
                )
                mint_image_cid = image_upload.cid
                mint_image_name = image_upload.name
                mint_image_uri = _build_nft_image_uri(request.user, image_upload.cid, image_upload.name)
                IPFSUpload.objects.create(
                    user=request.user,
                    original_filename=image_upload.name,
                    ipfs_hash=image_upload.cid,
                )
            elif selected_media_upload:
                mint_image_cid = selected_media_upload.ipfs_hash
                mint_image_name = selected_media_upload.display_filename
                mint_image_uri = _build_nft_image_uri(
                    request.user,
                    selected_media_upload.ipfs_hash,
                    selected_media_upload.display_filename,
                )
            else:
                qr_upload = _upload_qr_image_to_ipfs(unique_asset_name, file_name=f'{unique_asset_name}.svg')
                if qr_upload:
                    mint_image_cid = qr_upload.cid
                    mint_image_name = qr_upload.name
                    mint_image_uri = _build_nft_image_uri(request.user, qr_upload.cid, qr_upload.name)

            normalized_metadata, metadata_version = build_unique_metadata_payload(
                root_name=root_name,
                asset_tag=mint_asset_tag,
                name=mint_name,
                description=mint_description,
                image=mint_image_uri,
            )
            metadata_bytes = json.dumps(
                normalized_metadata,
                separators=(',', ':'),
                sort_keys=True,
            ).encode('utf-8')
            upload_result = KuboAPIUploader().upload_bytes(
                metadata_bytes,
                file_name=f'{root_name}_{mint_asset_tag}_metadata.json',
                pin=True,
                cid_version=0,
            )

            mint_record, _created = UniqueAssetMintRequest.objects.update_or_create(
                network_mode=current_network,
                unique_asset_name=unique_asset_name,
                defaults={
                    'creator': request.user,
                    'admin_asset_symbol': mint_admin_asset,
                    'root_name': root_name,
                    'asset_tag': mint_asset_tag,
                    'metadata_ipfs_cid': upload_result.cid,
                    'metadata_version': metadata_version,
                    'metadata_json': normalized_metadata,
                    'mint_txid': '',
                    'status': UniqueAssetMintRequest.STATUS_PENDING,
                    'confirmation_depth': 0,
                    'last_checked_at': None,
                    'error_message': '',
                },
            )

            issuer_address = _get_user_primary_address(request.user)
            if not issuer_address:
                raise Exception('No primary wallet address found for minting.')
            issuer_wif = _derive_user_wif_for_address(request.user, issuer_address)

            issuance_result = create_and_send_issue_unique_transaction(
                from_address=issuer_address,
                issuer_address=issuer_address,
                root_name=root_name,
                asset_tags=[mint_asset_tag],
                ipfs_hashes=[upload_result.cid],
                wif_keys=[issuer_wif],
            )
            txid = issuance_result.get('txid') if isinstance(issuance_result, dict) else issuance_result
            txid = str(txid or '').strip()
            if not txid:
                raise Exception('Unique asset broadcast did not return a transaction ID.')
            mint_record.status = UniqueAssetMintRequest.STATUS_BROADCAST
            mint_record.mint_txid = txid
            mint_record.confirmation_depth = 0
            mint_record.last_checked_at = timezone.now()
            mint_record.save(update_fields=['status', 'mint_txid', 'confirmation_depth', 'last_checked_at', 'updated_at'])

            messages.success(
                request,
                f'Unique asset mint broadcast for {unique_asset_name}. TXID: {txid}. Wait for confirmation, then create the swap.',
            )
            return redirect('create_listing')
        except Exception as exc:
            failed_record = UniqueAssetMintRequest.objects.filter(
                creator=request.user,
                network_mode=current_network,
                unique_asset_name=unique_asset_name,
            ).order_by('-created_at').first()
            if failed_record:
                failed_record.status = UniqueAssetMintRequest.STATUS_FAILED
                failed_record.error_message = str(exc)
                failed_record.save(update_fields=['status', 'error_message', 'updated_at'])
            messages.error(request, f'Unable to mint unique asset: {str(exc)}')
            return _render({
                'mint_admin_asset': mint_admin_asset,
                'mint_asset_tag': mint_asset_tag,
                'mint_name': mint_name,
                'mint_description': mint_description,
                'mint_existing_upload_id': mint_existing_upload_id,
            })

    if request.method == 'POST':
        price = request.POST.get('price', '').strip()
        token_offered = request.POST.get('token_offered', '').strip().upper()
        preferred_token = request.POST.get('preferred_token', 'EVR').strip().upper()
        counterparty_username = request.POST.get('counterparty', '').strip()
        expiry_days = request.POST.get('expiry_days', '7').strip()

        if active_channel_policy is None:
            messages.error(request, 'Atomic swaps require a verified active messaging channel for the full swap lifecycle.')
            return _render({'price': price, 'token_offered': token_offered, 'counterparty': counterparty_username, 'expiry_days': expiry_days, 'preferred_token': preferred_token})

        if not token_offered or not _is_unique_asset(token_offered):
            messages.error(request, 'Atomic swap offers only support unique assets (ROOT#TAG).')
            return _render({'price': price, 'token_offered': token_offered, 'counterparty': counterparty_username, 'expiry_days': expiry_days, 'preferred_token': preferred_token})

        if preferred_token not in settlement_token_options:
            messages.error(request, 'Settlement asset must be EVR or a tracked main/sub asset on this network.')
            return _render({'price': price, 'token_offered': token_offered, 'counterparty': counterparty_username, 'expiry_days': expiry_days, 'preferred_token': preferred_token})

        if not price:
            messages.error(request, 'Settlement amount is required.')
            return _render({'price': price, 'token_offered': token_offered, 'counterparty': counterparty_username, 'expiry_days': expiry_days, 'preferred_token': preferred_token})

        selected_balance = unique_asset_balances.get(token_offered)
        if selected_balance is None or selected_balance < Decimal('1'):
            messages.error(request, 'Selected unique asset is not available in your wallet.')
            return _render({'price': price, 'token_offered': token_offered, 'counterparty': counterparty_username, 'expiry_days': expiry_days, 'preferred_token': preferred_token})

        try:
            price_decimal = Decimal(price)
            expiry_days_int = int(expiry_days)
            if price_decimal <= 0:
                raise ValueError('Price must be greater than zero.')
            if expiry_days_int < 1 or expiry_days_int > 365:
                raise ValueError('Expiry must be between 1 and 365 days.')
            price_decimal = normalize_amount_for_asset(
                price_decimal,
                preferred_token,
                current_network,
                field_label='Settlement amount',
            )
        except (InvalidOperation, ValueError) as exc:
            messages.error(request, f'Invalid numeric input: {str(exc)}')
            return _render({'price': price, 'token_offered': token_offered, 'counterparty': counterparty_username, 'expiry_days': expiry_days, 'preferred_token': preferred_token})

        counterparty = None
        if counterparty_username:
            try:
                counterparty = User.objects.get(username=counterparty_username)
                if counterparty == request.user:
                    raise ValueError('Cannot create an atomic swap with yourself.')
            except User.DoesNotExist:
                messages.error(request, f'User {counterparty_username} not found.')
                return _render({'price': price, 'token_offered': token_offered, 'counterparty': counterparty_username, 'expiry_days': expiry_days, 'preferred_token': preferred_token})
            except ValueError as exc:
                messages.error(request, str(exc))
                return _render({'price': price, 'token_offered': token_offered, 'counterparty': counterparty_username, 'expiry_days': expiry_days, 'preferred_token': preferred_token})

        try:
            asset_data = RPC.getassetdata(token_offered)
            metadata_cid = _get_asset_ipfs_cid(asset_data)
            if not metadata_cid:
                raise Exception('The selected unique asset has no on-chain IPFS metadata hash.')
            metadata_payload = KuboAPIUploader().download_json(metadata_cid)
            normalized_metadata, metadata_version = validate_unique_asset_metadata(
                token_offered,
                metadata_payload,
                source_cid=metadata_cid,
            )
            image_cid = extract_cid_from_uri(normalized_metadata.get('image', ''))
            title = str(normalized_metadata.get('name') or token_offered).strip() or token_offered
            description = str(normalized_metadata.get('description') or '').strip()
        except Exception as exc:
            messages.error(request, f'Unable to load unique asset metadata from chain/IPFS: {str(exc)}')
            return _render({'price': price, 'token_offered': token_offered, 'counterparty': counterparty_username, 'expiry_days': expiry_days, 'preferred_token': preferred_token})

        try:
            with transaction.atomic():
                item = ListingItem.objects.create(
                    title=title,
                    description=description,
                    quantity=Decimal('1'),
                    individual_price=price_decimal,
                    total_price=price_decimal,
                    is_nft=True,
                    nft_image_ipfs_cid=image_cid or None,
                )
                listing = Listing.objects.create(
                    item=item,
                    seller=request.user,
                    price=price_decimal,
                    quantity_available=Decimal('1'),
                    network_mode=get_current_network_mode(),
                    token_offered=token_offered,
                    preferred_token=preferred_token,
                )
                NFT.objects.create(
                    listing_item=item,
                    owner=request.user,
                    creator=request.user,
                    image_ipfs_cid=image_cid,
                    metadata_ipfs_cid=metadata_cid,
                    metadata_version=metadata_version,
                    metadata_json=normalized_metadata,
                    token_id=token_offered,
                    is_listed=True,
                )

                expires_at = timezone.now() + timedelta(days=expiry_days_int)
                swap_offer = SwapOffer.objects.create(
                    initiator=request.user,
                    counterparty=counterparty,
                    listing=listing,
                    offer_token=token_offered,
                    offer_amount=Decimal('1'),
                    request_token=preferred_token,
                    request_amount=price_decimal,
                    network_mode=current_network,
                    expires_at=expires_at,
                    escrow_id=f'atomic-swap-{uuid.uuid4()}',
                )
                channel_message = record_atomic_swap_stage_event(
                    swap_offer,
                    stage='offer_created',
                    actor_username=request.user.username,
                    actor_user=request.user,
                    details={'source': 'listing_create_flow'},
                )
                if channel_message.status != 'broadcasted':
                    raise ValueError(
                        channel_message.error_message
                        or 'The atomic swap messaging channel could not publish offer_created.'
                    )

                messages.success(request, f'Unique-asset atomic swap created successfully with settlement asset {preferred_token}. Expires in {expiry_days_int} days.')
                return redirect('available_swap_offers')
        except Exception as exc:
            messages.error(request, f'Error creating atomic swap: {str(exc)}')
            return _render({'price': price, 'token_offered': token_offered, 'counterparty': counterparty_username, 'expiry_days': expiry_days, 'preferred_token': preferred_token})

    return _render({'expiry_days': '7'})

@login_required
def listing_detail(request, listing_id):
    """Display an atomic swap offer and its on-chain asset details."""
    listing = get_object_or_404(
        Listing.objects.select_related('item', 'seller'),
        id=listing_id,
        network_mode=get_current_network_mode(),
    )
    
    # Get NFT if this is an NFT listing
    nft = None
    if listing.item.is_nft:
        try:
            nft = listing.item.nft
        except:
            nft = None

    swap_offer = listing.swap_offers.filter(
        network_mode=get_current_network_mode(),
        status='pending',
        expires_at__gt=timezone.now(),
    ).first()
    
    context = {
        'listing': listing,
        'nft': nft,
        'nft_image_url': nft.get_ipfs_url() if nft else None,
        'swap_offer': swap_offer,
    }
    return render(request, 'listings/listing_detail.html', context)

# Order Book DEX Views
@login_required
def dex_orderbook(request, pair_slug):
    """Main DEX order book interface"""
    from DeFi.models import SwapOffer

    current_network = get_current_network_mode()

    trading_pairs = TradingPair.objects.filter(
        is_active=True,
        network_mode=current_network,
    ).order_by('base_token', 'quote_token')
    selected_pair = get_object_or_404(
        trading_pairs,
        pair_slug=pair_slug,
    )
    
    # Get order book for selected pair
    buy_orders = []
    sell_orders = []
    recent_trades = []
    chart_trades = []
    base_live_balance = Decimal('0')
    quote_live_balance = Decimal('0')
    base_available_balance = Decimal('0')
    quote_available_balance = Decimal('0')
    market_buy_capacity = Decimal('0')
    base_balance_error = 'no_selected_pair'
    quote_balance_error = 'no_selected_pair'
    atomic_listings = SwapOffer.objects.filter(
        network_mode=current_network,
        status='pending',
        expires_at__gt=timezone.now(),
        listing__isnull=False,
    ).select_related('listing', 'listing__item', 'initiator').order_by('-created_at')[:10]
    
    if selected_pair:
        # Get active buy orders (sorted by price descending - highest first)
        buy_orders = LimitOrder.objects.filter(
            trading_pair=selected_pair,
            side='buy',
            status__in=['pending', 'partial']
        ).order_by('-price')[:20]
        
        # Get active sell orders (sorted by price descending - highest first)
        sell_orders = LimitOrder.objects.filter(
            trading_pair=selected_pair,
            side='sell',
            status__in=['pending', 'partial']
        ).order_by('price', 'created_at')[:20]
        base_live_balance = _get_stored_user_token_balance(
            request.user,
            selected_pair.base_token,
            current_network,
        )
        quote_live_balance = _get_stored_user_token_balance(
            request.user,
            selected_pair.quote_token,
            current_network,
        )
        base_balance_error = 'stored_snapshot'
        quote_balance_error = 'stored_snapshot'
        base_available_balance = _get_user_available_token_balance(
            request.user,
            selected_pair.base_token,
            live_balance=base_live_balance,
        )
        quote_available_balance = _get_user_available_token_balance(
            request.user,
            selected_pair.quote_token,
            live_balance=quote_live_balance,
        )
        market_buy_capacity = _market_buy_capacity(
            request.user,
            selected_pair,
            quote_available=quote_available_balance,
            base_units=_get_stored_asset_units(
                selected_pair.base_token,
                selected_pair.network_mode,
            ),
        )
        
        # Get recent trades
        recent_trades = OrderExecution.objects.filter(
            trading_pair=selected_pair
        ).select_related('buyer', 'seller').annotate(
            total_cost=ExpressionWrapper(
                F('price') * F('quantity'),
                output_field=DecimalField(max_digits=20, decimal_places=8)
            )
        ).order_by('-created_at')[:20]
        chart_rows = list(OrderExecution.objects.filter(
            trading_pair=selected_pair,
        ).order_by('-created_at').values('created_at', 'price')[:5000])
        chart_trades = [
            {
                'time': int(row['created_at'].timestamp()),
                'price': str(row['price']),
            }
            for row in reversed(chart_rows)
        ]
    
    context = {
        'trading_pairs': trading_pairs,
        'selected_pair': selected_pair,
        'base_amount_step': format(
            amount_quantum_for_units(_get_stored_asset_units(
                selected_pair.base_token,
                selected_pair.network_mode,
            )),
            'f',
        ) if selected_pair else '0.00000001',
        'quote_amount_step': format(
            amount_quantum_for_units(_get_stored_asset_units(
                selected_pair.quote_token,
                selected_pair.network_mode,
            )),
            'f',
        ) if selected_pair else '0.00000001',
        'buy_orders': buy_orders,
        'sell_orders': sell_orders,
        'recent_trades': recent_trades,
        'chart_trades': chart_trades,
        'atomic_listings': atomic_listings,
        'base_live_balance': _format_asset_amount(base_live_balance),
        'quote_live_balance': _format_asset_amount(quote_live_balance),
        'base_available_balance': _format_asset_amount(base_available_balance),
        'quote_available_balance': _format_asset_amount(quote_available_balance),
        'market_buy_capacity': _format_asset_amount(market_buy_capacity),
        'pair_balances_are_live': not base_balance_error and not quote_balance_error,
    }
    return render(request, 'listings/dex_orderbook.html', context)


@login_required
def market_pair_balances(request, pair_id):
    trading_pair = get_object_or_404(
        TradingPair,
        pk=pair_id,
        is_active=True,
        network_mode=get_current_network_mode(),
    )
    base_units = get_asset_units(trading_pair.base_token, trading_pair.network_mode)
    quote_units = get_asset_units(trading_pair.quote_token, trading_pair.network_mode)
    base_live_balance, base_balance_error = _fetch_user_token_balance(
        request.user,
        trading_pair.base_token,
    )
    quote_live_balance, quote_balance_error = _fetch_user_token_balance(
        request.user,
        trading_pair.quote_token,
    )
    base_available_balance = _get_user_available_token_balance(
        request.user,
        trading_pair.base_token,
        live_balance=base_live_balance,
    )
    quote_available_balance = _get_user_available_token_balance(
        request.user,
        trading_pair.quote_token,
        live_balance=quote_live_balance,
    )
    return JsonResponse({
        'base_token': trading_pair.base_token,
        'quote_token': trading_pair.quote_token,
        'base_units': base_units,
        'quote_units': quote_units,
        'base_step': format(amount_quantum_for_units(base_units), 'f'),
        'quote_step': format(amount_quantum_for_units(quote_units), 'f'),
        'base_live_balance': _format_asset_amount(
            base_live_balance,
        ),
        'quote_live_balance': _format_asset_amount(
            quote_live_balance,
        ),
        'base_available_balance': _format_asset_amount(
            base_available_balance,
        ),
        'quote_available_balance': _format_asset_amount(
            quote_available_balance,
        ),
        'market_buy_capacity': _format_asset_amount(
            _market_buy_capacity(
                request.user,
                trading_pair,
                quote_available=quote_available_balance,
                base_units=base_units,
            ),
        ),
        'balances_are_live': not base_balance_error and not quote_balance_error,
        'balance_error': base_balance_error or quote_balance_error,
    })

@login_required
@require_POST
def place_limit_order(request):
    """Place a limit order"""
    if request.method == 'POST':
        pair_id = request.POST.get('pair_id')
        side = request.POST.get('side', '').strip().lower()
        price = request.POST.get('price', '').strip()
        quantity = request.POST.get('quantity', '').strip()
        
        # Validate inputs
        if not all([pair_id, side, price, quantity]):
            messages.error(request, 'All fields are required.')
            return _redirect_to_order_book(pair_id)
        
        if side not in ['buy', 'sell']:
            messages.error(request, 'Invalid order side.')
            return _redirect_to_order_book(pair_id)
        
        try:
            trading_pair = TradingPair.objects.get(
                id=pair_id,
                is_active=True,
                network_mode=get_current_network_mode(),
            )
            price_decimal = normalize_amount_for_asset(
                price,
                trading_pair.quote_token,
                trading_pair.network_mode,
                field_label='Price',
                strict=True,
            )
            quantity_decimal = normalize_amount_for_asset(
                quantity,
                trading_pair.base_token,
                trading_pair.network_mode,
                field_label='Quantity',
                strict=True,
            )
            
            if side == 'buy':
                normalize_amount_for_asset(
                    price_decimal * quantity_decimal,
                    trading_pair.quote_token,
                    trading_pair.network_mode,
                    field_label='Order total',
                    strict=True,
                )

            order = _create_reserved_limit_order(
                request.user,
                trading_pair,
                side,
                price_decimal,
                quantity_decimal,
            )

            record_market_stage_event(
                trading_pair,
                stage='order_created',
                actor_user=request.user,
                order=order,
            )
            
            # Try to match the order
            _match_order(order)
            
            messages.success(request, f'Limit {side} order placed successfully!')
            
        except TradingPair.DoesNotExist:
            messages.error(request, 'Trading pair not found.')
        except ValueError as exc:
            messages.error(request, str(exc))
        except InvalidOperation:
            messages.error(request, 'Invalid price or quantity format.')
        except Exception as e:
            messages.error(request, f'Error placing order: {str(e)}')
        
        return _redirect_to_order_book(pair_id)
    
    return _redirect_to_order_book()

@login_required
@require_POST
def place_market_order(request):
    """Place a market order for instant execution"""
    if request.method == 'POST':
        pair_id = request.POST.get('pair_id')
        side = request.POST.get('side', '').strip().lower()
        quantity = request.POST.get('quantity', '').strip()
        
        # Validate inputs
        if not all([pair_id, side, quantity]):
            messages.error(request, 'All fields are required.')
            return _redirect_to_order_book(pair_id)
        
        if side not in ['buy', 'sell']:
            messages.error(request, 'Invalid order side.')
            return _redirect_to_order_book(pair_id)
        
        try:
            trading_pair = TradingPair.objects.get(
                id=pair_id,
                is_active=True,
                network_mode=get_current_network_mode(),
            )
            quantity_decimal = normalize_amount_for_asset(
                quantity,
                trading_pair.base_token,
                trading_pair.network_mode,
                field_label='Quantity',
                strict=True,
            )
            
            # Get opposite side orders for matching
            if side == 'buy':
                # For buy orders, get sell orders sorted by lowest price first, then by creation time (FIFO)
                opposite_orders = LimitOrder.objects.filter(
                    trading_pair=trading_pair,
                    side='sell',
                    status__in=['pending', 'partial']
                ).exclude(user=request.user).order_by('price', 'created_at')
            else:
                # For sell orders, get buy orders sorted by highest price first, then by creation time (FIFO)
                opposite_orders = LimitOrder.objects.filter(
                    trading_pair=trading_pair,
                    side='buy',
                    status__in=['pending', 'partial']
                ).exclude(user=request.user).order_by('-price', 'created_at')
            
            if not opposite_orders.exists():
                messages.error(request, 'No orders available for immediate execution.')
                return _redirect_to_order_book(pair_id)
            
            # Calculate maximum cost for buy orders (worst case scenario)
            if side == 'buy':
                # Calculate worst-case total cost (if all matches at highest available price)
                remaining_qty = quantity_decimal
                total_cost = Decimal('0')
                
                for limit_order in opposite_orders:
                    if remaining_qty <= 0:
                        break
                    
                    available_qty = limit_order.remaining_quantity
                    fill_qty = min(remaining_qty, available_qty)
                    total_cost += fill_qty * limit_order.price
                    remaining_qty -= fill_qty
                
                # Check if user has enough quote token balance
                user_balance = _get_verified_available_token_balance(request.user, trading_pair.quote_token)
                if user_balance < total_cost:
                    messages.error(
                        request, 
                        f'Insufficient {trading_pair.quote_token} balance. Required: {total_cost:.8f}, Available: {user_balance:.8f}'
                    )
                    return _redirect_to_order_book(pair_id)
            else:
                user_balance = _get_verified_available_token_balance(request.user, trading_pair.base_token)
                if user_balance < quantity_decimal:
                    messages.error(
                        request,
                        f'Insufficient {trading_pair.base_token} balance. Required: {quantity_decimal:.8f}, Available: {user_balance:.8f}'
                    )
                    return _redirect_to_order_book(pair_id)
            
            # Execute market order
            remaining_qty = quantity_decimal
            total_cost = Decimal('0')
            executed_trades = 0
            
            with transaction.atomic():
                for limit_order in opposite_orders:
                    if remaining_qty <= 0:
                        break
                    
                    # Lock the limit order for update to prevent race conditions
                    limit_order = LimitOrder.objects.select_for_update().get(id=limit_order.id)
                    if limit_order.user == request.user:
                        continue
                    
                    # Calculate quantity to fill
                    available_qty = limit_order.remaining_quantity
                    fill_qty = min(remaining_qty, available_qty)
                    
                    # Create execution record
                    if side == 'buy':
                        buyer = request.user
                        seller = limit_order.user
                        buyer_order = None
                        seller_order = limit_order
                    else:
                        buyer = limit_order.user
                        seller = request.user
                        buyer_order = limit_order
                        seller_order = None

                    try:
                        tx_hash = _settle_market_fill(
                            trading_pair,
                            buyer=buyer,
                            seller=seller,
                            quantity=fill_qty,
                            price=limit_order.price,
                        )
                    except InsufficientSpendableBalance as exc:
                        logger.info(
                            'Deferred market fill for pair=%s maker_order=%s: %s',
                            trading_pair.pk,
                            limit_order.pk,
                            exc,
                        )
                        continue

                    OrderExecution.objects.create(
                        trading_pair=trading_pair,
                        buyer=buyer,
                        seller=seller,
                        price=limit_order.price,
                        quantity=fill_qty,
                        buyer_order=buyer_order,
                        seller_order=seller_order,
                        tx_hash=tx_hash,
                    )
                    
                    # Update limit order
                    limit_order.filled_quantity += fill_qty
                    if limit_order.filled_quantity >= limit_order.quantity:
                        limit_order.status = 'filled'
                    else:
                        limit_order.status = 'partial'
                    limit_order.save()
                    
                    total_cost += fill_qty * limit_order.price
                    remaining_qty -= fill_qty
                    executed_trades += 1
                
                # Create market order record
                filled_qty = quantity_decimal - remaining_qty
                if filled_qty > 0:
                    avg_price = total_cost / filled_qty
                else:
                    raise ValueError(
                        'Matching orders are awaiting spendable on-chain funds or change confirmation.'
                    )
                
                market_order = MarketOrder.objects.create(
                    user=request.user,
                    trading_pair=trading_pair,
                    side=side,
                    quantity=filled_qty,
                    executed_price=avg_price,
                    status='executed',
                    tx_hash=tx_hash if filled_qty > 0 else '',
                )
                
                # Create balance lock for buy orders to track EVR consumption
                if side == 'buy' and total_cost > 0:
                    BalanceLock.objects.create(
                        user=request.user,
                        asset_symbol=trading_pair.quote_token,
                        amount=total_cost,
                        status='consumed',
                        market_order=market_order
                    )
            
            if remaining_qty > 0:
                messages.warning(request, f'Market order partially executed: {filled_qty}/{quantity_decimal} {trading_pair.base_token} @ avg price {avg_price:.8f}')
            else:
                messages.success(request, f'Market {side} order executed successfully! {executed_trades} trades at avg price {avg_price:.8f}')
            
        except TradingPair.DoesNotExist:
            messages.error(request, 'Trading pair not found.')
        except ValueError as exc:
            messages.error(request, str(exc))
        except InvalidOperation:
            messages.error(request, 'Invalid quantity format.')
        except Exception as e:
            messages.error(request, f'Error executing market order: {str(e)}')
        
        return _redirect_to_order_book(pair_id)
    
    return _redirect_to_order_book()

@login_required
@require_POST
def place_stop_loss_order(request):
    """Place a stop-loss order"""
    if request.method == 'POST':
        pair_id = request.POST.get('pair_id')
        side = request.POST.get('side', '').strip().lower()
        trigger_price = request.POST.get('trigger_price', '').strip()
        quantity = request.POST.get('quantity', '').strip()
        
        # Validate inputs
        if not all([pair_id, side, trigger_price, quantity]):
            messages.error(request, 'All fields are required.')
            return _redirect_to_order_book(pair_id)
        
        if side not in ['buy', 'sell']:
            messages.error(request, 'Invalid order side.')
            return _redirect_to_order_book(pair_id)
        
        try:
            trading_pair = TradingPair.objects.get(
                id=pair_id,
                is_active=True,
                network_mode=get_current_network_mode(),
            )
            trigger_price_decimal = normalize_amount_for_asset(
                trigger_price,
                trading_pair.quote_token,
                trading_pair.network_mode,
                field_label='Trigger price',
                strict=True,
            )
            quantity_decimal = normalize_amount_for_asset(
                quantity,
                trading_pair.base_token,
                trading_pair.network_mode,
                field_label='Quantity',
                strict=True,
            )
            
            # Create stop-loss order
            StopLossOrder.objects.create(
                user=request.user,
                trading_pair=trading_pair,
                side=side,
                trigger_price=trigger_price_decimal,
                quantity=quantity_decimal,
                status='pending'
            )
            
            messages.success(request, f'Stop-loss {side} order placed successfully at trigger price {trigger_price_decimal}!')
            
        except TradingPair.DoesNotExist:
            messages.error(request, 'Trading pair not found.')
        except ValueError as exc:
            messages.error(request, str(exc))
        except InvalidOperation:
            messages.error(request, 'Invalid price or quantity format.')
        except Exception as e:
            messages.error(request, f'Error placing stop-loss order: {str(e)}')
        
        return _redirect_to_order_book(pair_id)
    
    return _redirect_to_order_book()

@login_required
@require_POST
def cancel_order(request, order_id):
    """Cancel a limit order"""
    if request.method == 'POST':
        try:
            order = LimitOrder.objects.get(id=order_id, user=request.user)
            
            if order.status not in ['pending', 'partial']:
                messages.error(request, 'Only pending or partially filled orders can be cancelled.')
                return redirect('my_orders')
            
            order.status = 'cancelled'
            order.save()
            _sync_order_balance_lock(order)
            
            messages.success(request, 'Order cancelled successfully!')
            
        except LimitOrder.DoesNotExist:
            messages.error(request, 'Order not found.')
        except Exception as e:
            messages.error(request, f'Error cancelling order: {str(e)}')
        
        return redirect('my_orders')
    
    return redirect('my_orders')

@login_required
@require_POST
def cancel_stop_loss(request, order_id):
    """Cancel a stop-loss order"""
    if request.method == 'POST':
        try:
            order = StopLossOrder.objects.get(id=order_id, user=request.user)
            
            if order.status != 'pending':
                messages.error(request, 'Only pending stop-loss orders can be cancelled.')
                return redirect('my_orders')
            
            order.status = 'cancelled'
            order.save()
            
            messages.success(request, 'Stop-loss order cancelled successfully!')
            
        except StopLossOrder.DoesNotExist:
            messages.error(request, 'Stop-loss order not found.')
        except Exception as e:
            messages.error(request, f'Error cancelling stop-loss order: {str(e)}')
        
        return redirect('my_orders')
    
    return redirect('my_orders')

@login_required
def my_orders(request):
    """Display user's active and historical orders"""
    current_network = get_current_network_mode()

    # Get user's limit orders
    limit_orders = LimitOrder.objects.filter(
        user=request.user,
        trading_pair__network_mode=current_network,
    ).select_related('trading_pair').order_by('-created_at')
    
    # Get user's market orders
    market_orders = MarketOrder.objects.filter(
        user=request.user,
        trading_pair__network_mode=current_network,
    ).select_related('trading_pair').order_by('-created_at')
    
    # Get user's stop-loss orders
    stop_loss_orders = StopLossOrder.objects.filter(
        user=request.user,
        trading_pair__network_mode=current_network,
    ).select_related('trading_pair').order_by('-created_at')
    
    # Get user's trade history
    trade_history = OrderExecution.objects.filter(
        Q(buyer=request.user) | Q(seller=request.user),
        trading_pair__network_mode=current_network,
    ).select_related('trading_pair', 'buyer', 'seller').order_by('-created_at')[:50]
    
    context = {
        'limit_orders': limit_orders,
        'market_orders': market_orders,
        'stop_loss_orders': stop_loss_orders,
        'trade_history': trade_history,
    }
    return render(request, 'listings/my_orders.html', context)

def _match_order(order):
    """
    Internal function to match a limit order with existing orders in the order book.
    
    Implements price-time priority matching:
    - Orders are matched at the best available price
    - At the same price level, orders are matched FIFO (first in, first out)
    
    Args:
        order: LimitOrder instance to match against the order book
        
    Side effects:
        - Creates OrderExecution records for matched trades
        - Updates filled_quantity and status of matched orders
        - May trigger stop-loss orders based on execution prices
    """
    with transaction.atomic():
        if order.side == 'buy':
            # Match with sell orders (price ascending, then FIFO)
            opposite_orders = LimitOrder.objects.filter(
                trading_pair=order.trading_pair,
                side='sell',
                status__in=['pending', 'partial'],
                price__lte=order.price  # Only match if sell price <= buy price
            ).exclude(user=order.user).order_by('price', 'created_at')
        else:
            # Match with buy orders (price descending, then FIFO)
            opposite_orders = LimitOrder.objects.filter(
                trading_pair=order.trading_pair,
                side='buy',
                status__in=['pending', 'partial'],
                price__gte=order.price  # Only match if buy price >= sell price
            ).exclude(user=order.user).order_by('-price', 'created_at')
        
        for opposite in opposite_orders:
            if order.remaining_quantity <= 0:
                break
            
            # Lock both orders for update to prevent race conditions
            opposite = LimitOrder.objects.select_for_update().get(id=opposite.id)
            order = LimitOrder.objects.select_for_update().get(id=order.id)
            
            # Prevent self-trading
            if order.user == opposite.user:
                continue
            
            # Calculate fill quantity
            fill_qty = min(order.remaining_quantity, opposite.remaining_quantity)
            
            # Use the price of the earlier order (maker price)
            execution_price = opposite.price
            
            # Create execution
            if order.side == 'buy':
                buyer = order.user
                seller = opposite.user
                buyer_order = order
                seller_order = opposite
            else:
                buyer = opposite.user
                seller = order.user
                buyer_order = opposite
                seller_order = order
            
            try:
                tx_hash = _settle_market_fill(
                    order.trading_pair,
                    buyer=buyer,
                    seller=seller,
                    quantity=fill_qty,
                    price=execution_price,
                )
            except InsufficientSpendableBalance as exc:
                logger.info(
                    'Deferred limit fill for pair=%s maker_order=%s taker_order=%s: %s',
                    order.trading_pair_id,
                    opposite.pk,
                    order.pk,
                    exc,
                )
                continue

            OrderExecution.objects.create(
                trading_pair=order.trading_pair,
                buyer=buyer,
                seller=seller,
                price=execution_price,
                quantity=fill_qty,
                buyer_order=buyer_order,
                seller_order=seller_order,
                tx_hash=tx_hash,
            )
            
            # Update both orders
            order.filled_quantity += fill_qty
            opposite.filled_quantity += fill_qty
            
            # Update statuses
            if order.filled_quantity >= order.quantity:
                order.status = 'filled'
            elif order.filled_quantity > 0:
                order.status = 'partial'
            
            if opposite.filled_quantity >= opposite.quantity:
                opposite.status = 'filled'
            elif opposite.filled_quantity > 0:
                opposite.status = 'partial'
            
            order.save()
            opposite.save()
            _sync_order_balance_lock(order)
            _sync_order_balance_lock(opposite)
        
        # Check stop-loss orders that might be triggered
        _check_stop_loss_triggers(order.trading_pair)

def _check_stop_loss_triggers(trading_pair):
    """
    Check and trigger stop-loss orders based on latest execution prices.
    
    Stop-loss logic:
    - Sell stop-loss: Triggers when price DROPS to or below trigger price (protect long positions)
    - Buy stop-loss: Triggers when price RISES to or above trigger price (protect short positions)
    
    Args:
        trading_pair: TradingPair instance to check stop-loss orders for
        
    Side effects:
        - Updates stop-loss order status to 'triggered' then 'executed'
        - Creates market orders to execute the stop-loss
    """
    # Get the latest execution price for the pair
    latest_execution = OrderExecution.objects.filter(
        trading_pair=trading_pair
    ).order_by('-created_at').first()
    
    if not latest_execution:
        return
    
    current_price = latest_execution.price
    
    with transaction.atomic():
        # Check sell stop-loss orders (trigger when price drops to or below trigger price)
        sell_stops = StopLossOrder.objects.filter(
            trading_pair=trading_pair,
            side='sell',
            status='pending',
            trigger_price__gte=current_price  # Trigger when current_price <= trigger_price
        )
        
        # Check buy stop-loss orders (trigger when price rises to or above trigger price)
        buy_stops = StopLossOrder.objects.filter(
            trading_pair=trading_pair,
            side='buy',
            status='pending',
            trigger_price__lte=current_price  # Trigger when current_price >= trigger_price
        )
        
        # Trigger and execute stop-loss orders
        for stop_order in list(sell_stops) + list(buy_stops):
            stop_order.status = 'triggered'
            stop_order.triggered_at = timezone.now()
            stop_order.save()
            
            stop_order.save(update_fields=['status', 'triggered_at'])

# Markets Views
@login_required
def markets_view(request):
    """Display all trading pairs/markets like SafeTrade interface"""
    current_network = get_current_network_mode()
    can_manage_markets = user_has_feature_access(request.user, FEATURE_MARKET_MANAGEMENT)

    # Get filter from query params
    filter_token = request.GET.get('filter', 'ALL').upper()
    
    # Get all active trading pairs
    markets = TradingPair.objects.filter(network_mode=current_network).select_related('created_by')
    if not can_manage_markets:
        markets = markets.filter(is_active=True)
    else:
        markets = markets.annotate(
            has_limit_orders=Exists(LimitOrder.objects.filter(trading_pair=OuterRef('pk'))),
            has_market_orders=Exists(MarketOrder.objects.filter(trading_pair=OuterRef('pk'))),
            has_stop_orders=Exists(StopLossOrder.objects.filter(trading_pair=OuterRef('pk'))),
            has_executions=Exists(OrderExecution.objects.filter(trading_pair=OuterRef('pk'))),
        )
    favorite_market_ids = set(MarketFavorite.objects.filter(
        user=request.user,
        trading_pair__network_mode=current_network,
    ).values_list('trading_pair_id', flat=True))
    
    # Update 24h stats for all markets (in production, this should be a background task)
    for market in markets:
        market.get_24h_stats()
    
    # Apply filter
    if filter_token == 'FAVORITES':
        markets = markets.filter(pk__in=favorite_market_ids)
    elif filter_token != 'ALL':
        markets = markets.filter(Q(base_token=filter_token) | Q(quote_token=filter_token))
    
    # Get unique quote tokens for filter buttons
    all_markets = TradingPair.objects.filter(network_mode=current_network)
    if not can_manage_markets:
        all_markets = all_markets.filter(is_active=True)
    quote_tokens = set()
    for market in all_markets:
        quote_tokens.add(market.quote_token)
        quote_tokens.add(market.base_token)
    quote_tokens = sorted(list(quote_tokens))
    
    context = {
        'markets': markets.order_by('-volume_24h'),
        'filter_token': filter_token,
        'quote_tokens': quote_tokens,
        'can_manage_markets': can_manage_markets,
        'current_network': current_network,
        'favorite_market_ids': favorite_market_ids,
    }
    return render(request, 'listings/markets.html', context)


@login_required
@require_POST
def toggle_market_favorite(request, market_id):
    market = get_object_or_404(
        TradingPair,
        pk=market_id,
        is_active=True,
        network_mode=get_current_network_mode(),
    )
    favorite, created = MarketFavorite.objects.get_or_create(
        user=request.user,
        trading_pair=market,
    )
    if not created:
        favorite.delete()
    return _redirect_to_markets(request.POST.get('filter'))


def _redirect_to_markets(filter_token=None):
    url = reverse('markets')
    normalized_filter = str(filter_token or '').strip().upper()
    if normalized_filter and normalized_filter != 'ALL':
        url = f"{url}?{urlencode({'filter': normalized_filter})}"
    return redirect(url)

@login_required
def create_market(request):
    """Allow users to create new trading pairs/markets"""
    if not user_has_feature_access(request.user, FEATURE_MARKET_MANAGEMENT):
        messages.error(request, 'You are not authorized to create or manage markets.')
        return redirect('markets')

    current_network = get_current_network_mode()

    # Fetch user's available assets (excluding EVR)
    user_assets, error = _get_user_asset_balances(request.user)
    marketable_assets = _marketable_asset_balances(user_assets)
    available_base_tokens = sorted(marketable_assets.keys())
    # Quote tokens: EVR plus any other marketable token assets the user has.
    available_quote_tokens = ['EVR'] + sorted(marketable_assets.keys())
    
    if request.method == 'POST':
        base_token = request.POST.get('base_token', '').strip().upper()
        quote_token = request.POST.get('quote_token', '').strip().upper()
        
        # Validate inputs
        if not base_token or not quote_token:
            messages.error(request, 'Both base token and quote token are required.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token,
                'available_base_tokens': available_base_tokens,
                'available_quote_tokens': available_quote_tokens,
                'user_assets': user_assets,
                'current_network': current_network,
            })

        if not _is_marketable_token_asset(base_token):
            messages.error(request, 'Only token assets can be used as a market base asset. Unique and admin assets are not allowed.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token,
                'available_base_tokens': available_base_tokens,
                'available_quote_tokens': available_quote_tokens,
                'user_assets': user_assets,
                'current_network': current_network,
            })

        if quote_token != MARKET_QUOTE_TOKEN and not _is_marketable_token_asset(quote_token):
            messages.error(request, 'Quote assets must be EVR or a token asset. Unique and admin assets are not allowed.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token,
                'available_base_tokens': available_base_tokens,
                'available_quote_tokens': available_quote_tokens,
                'user_assets': user_assets,
                'current_network': current_network,
            })

        if base_token not in available_base_tokens:
            messages.error(request, f'{base_token} is not available in your live wallet balance.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token,
                'available_base_tokens': available_base_tokens,
                'available_quote_tokens': available_quote_tokens,
                'user_assets': user_assets,
                'current_network': current_network,
            })

        if quote_token not in available_quote_tokens:
            messages.error(request, f'{quote_token} is not available in your live wallet balance.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token,
                'available_base_tokens': available_base_tokens,
                'available_quote_tokens': available_quote_tokens,
                'user_assets': user_assets,
                'current_network': current_network,
            })
        
        # Validate base token is not EVR
        if base_token == 'EVR':
            messages.error(request, 'Base token cannot be EVR. EVR is reserved as the quote token.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token,
                'available_base_tokens': available_base_tokens,
                'available_quote_tokens': available_quote_tokens,
                'user_assets': user_assets,
                'current_network': current_network,
            })
        
        # Validate they're not the same
        if base_token == quote_token:
            messages.error(request, 'Base token and quote token must be different.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token,
                'available_base_tokens': available_base_tokens,
                'available_quote_tokens': available_quote_tokens,
                'user_assets': user_assets,
                'current_network': current_network,
            })
        
        # Check if pair already exists
        if TradingPair.objects.filter(
            base_token=base_token,
            quote_token=quote_token,
            network_mode=current_network,
        ).exists():
            messages.error(request, f'Trading pair {base_token}/{quote_token} already exists.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token,
                'available_base_tokens': available_base_tokens,
                'available_quote_tokens': available_quote_tokens,
                'user_assets': user_assets,
                'current_network': current_network,
            })
        
        # Check if reverse pair exists
        if TradingPair.objects.filter(
            base_token=quote_token,
            quote_token=base_token,
            network_mode=current_network,
        ).exists():
            messages.error(request, f'Redundant market rejected. {quote_token}/{base_token} already exists on this network.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token,
                'available_base_tokens': available_base_tokens,
                'available_quote_tokens': available_quote_tokens,
                'user_assets': user_assets,
                'current_network': current_network,
            })
        
        # Create the trading pair
        try:
            with transaction.atomic():
                trading_pair = TradingPair.objects.create(
                    base_token=base_token,
                    quote_token=quote_token,
                    network_mode=current_network,
                    instrument_type=_market_instrument_type(base_token),
                    created_by=request.user,
                    is_active=True
                )
        except IntegrityError:
            messages.error(request, f'Trading pair {base_token}/{quote_token} already exists in either orientation.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token,
                'available_base_tokens': available_base_tokens,
                'available_quote_tokens': available_quote_tokens,
                'user_assets': user_assets,
                'current_network': current_network,
            })
        record_market_stage_event(
            trading_pair,
            stage='market_created',
            actor_user=request.user,
        )
        
        messages.success(request, f'Market {base_token}/{quote_token} created successfully!')
        return redirect('markets')
    
    return render(request, 'listings/create_market.html', {
        'available_base_tokens': available_base_tokens,
        'available_quote_tokens': available_quote_tokens,
        'user_assets': user_assets,
        'current_network': current_network,
    })


@login_required
@require_POST
def toggle_market_status(request, market_id):
    """Allow authorized users to pause/resume a market on the active network."""
    if not user_has_feature_access(request.user, FEATURE_MARKET_MANAGEMENT):
        messages.error(request, 'You are not authorized to modify markets.')
        return redirect('markets')

    market = get_object_or_404(
        TradingPair,
        id=market_id,
        network_mode=get_current_network_mode(),
    )
    market.is_active = not market.is_active
    market.save(update_fields=['is_active'])

    state = 'active' if market.is_active else 'paused'
    messages.success(request, f'Market {market.base_token}/{market.quote_token} is now {state}.')
    return redirect('markets')


@login_required
def legacy_dex_orderbook(request):
    pair_id = request.GET.get('pair')
    if pair_id:
        pair = TradingPair.objects.filter(
            pk=pair_id,
            is_active=True,
            network_mode=get_current_network_mode(),
        ).first()
        if pair:
            return redirect('dex_orderbook', pair_slug=pair.pair_slug, permanent=True)
    return redirect('markets', permanent=True)


@login_required
@require_POST
def reverse_market_pair(request, market_id):
    if not user_has_feature_access(request.user, FEATURE_MARKET_MANAGEMENT):
        messages.error(request, 'You are not authorized to modify markets.')
        return redirect('markets')

    market = get_object_or_404(
        TradingPair,
        id=market_id,
        network_mode=get_current_network_mode(),
    )
    has_history = (
        LimitOrder.objects.filter(trading_pair=market).exists()
        or MarketOrder.objects.filter(trading_pair=market).exists()
        or StopLossOrder.objects.filter(trading_pair=market).exists()
        or OrderExecution.objects.filter(trading_pair=market).exists()
    )
    if has_history:
        messages.error(
            request,
            'Pair orientation cannot change after orders or trades exist because prices and quantities would be inverted.',
        )
        return redirect('markets')

    market.base_token, market.quote_token = market.quote_token, market.base_token
    market.save()
    messages.success(request, f'Market orientation changed to {market.base_token}/{market.quote_token}.')
    return redirect('markets')
