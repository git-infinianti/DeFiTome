from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import uuid
from .models import (
    TestnetConfig, LiquidityPool, LiquidityPosition, SwapTransaction, 
    SwapOffer, SwapFundingLock, P2PSwapTransaction, PriceFeedSource,
    PriceFeedData, PriceFeedAggregation, CollateralAsset, InterestRateConfig,
    LendingPool, Deposit, Loan, LoanRepayment, Liquidation
)
from Wallet.asset_tracking import classify_asset_type
from Wallet.models import TrackedAsset, WalletAddress
from Wallet.wallet import Wallet
from Wallet.asset_units import normalize_amount_for_asset
from Wallet.rpc import (
    create_raw_atomic_asset_asset_swap_transaction,
    create_raw_atomic_asset_evr_swap_transaction,
    sign_and_broadcast_raw_transaction,
)
from Tome.rpc_client import RPC, get_current_network_mode
from .cleanup import purge_expired_swap_offers
from .message_channels import ATOMIC_SWAP_REQUIRED_STAGES, get_active_atomic_swap_policy, record_atomic_swap_stage_event
import statistics

# Create your views here.


def _get_user_primary_address(user):
    """Return the first external wallet address for a user."""
    user_wallet = getattr(user, 'user_wallet', None)
    if not user_wallet:
        return None

    address_record = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=get_current_network_mode(),
        is_change=False,
    ).order_by('account', 'index').first()
    if address_record:
        return address_record.address

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
    """Derive the WIF needed to sign a transaction from a user's wallet."""
    user_wallet = getattr(user, 'user_wallet', None)
    if not user_wallet:
        raise Exception('No wallet found for user.')
    return Wallet(
        user_wallet.entropy,
        user_wallet.passphrase,
        network_mode=get_current_network_mode(),
    ).get_wif_for_address(address)


def _to_decimal_amount(value):
    try:
        return Decimal(str(value or 0))
    except (ValueError, InvalidOperation):
        return Decimal('0')


def _normalize_swap_amount_for_asset(symbol, amount, network_mode):
    return normalize_amount_for_asset(
        amount,
        symbol,
        network_mode,
        field_label='Settlement amount',
    )


def _get_onchain_token_balance(user, token_symbol):
    address = _get_user_primary_address(user)
    if not address:
        return Decimal('0')

    normalized = str(token_symbol or '').strip().upper()
    try:
        if normalized == 'EVR':
            balance_data = RPC.getaddressbalance({'addresses': [address]})
            satoshis = int((balance_data or {}).get('balance', 0))
            return Decimal(satoshis) * Decimal('1e-8')

        balances = RPC.listassetbalancesbyaddress(address)
        if not isinstance(balances, dict):
            return Decimal('0')
        return _to_decimal_amount(balances.get(normalized, 0))
    except Exception:
        return Decimal('0')


def _get_locked_token_amount(user, token_symbol, network_mode, exclude_offer_id=None):
    queryset = SwapFundingLock.objects.filter(
        user=user,
        token_symbol=str(token_symbol or '').strip().upper(),
        status='locked',
        swap_offer__network_mode=network_mode,
    )
    if exclude_offer_id is not None:
        queryset = queryset.exclude(swap_offer_id=exclude_offer_id)

    aggregate_value = queryset.aggregate(total=Sum('amount'))['total']
    return _to_decimal_amount(aggregate_value)


def _get_available_token_amount(user, token_symbol, network_mode, exclude_offer_id=None):
    onchain = _get_onchain_token_balance(user, token_symbol)
    locked = _get_locked_token_amount(user, token_symbol, network_mode, exclude_offer_id=exclude_offer_id)
    available = onchain - locked
    if available < 0:
        return Decimal('0')
    return available


def _release_offer_funding_locks(swap_offer):
    SwapFundingLock.objects.filter(
        swap_offer=swap_offer,
        status='locked',
    ).update(status='released', released_at=timezone.now())


def _consume_offer_funding_locks(swap_offer):
    SwapFundingLock.objects.filter(
        swap_offer=swap_offer,
        status='locked',
    ).update(status='consumed', released_at=timezone.now())


def _derive_temp_txid(raw_tx):
    try:
        decoded = RPC.decoderawtransaction(raw_tx)
        if isinstance(decoded, dict) and decoded.get('txid'):
            return str(decoded['txid'])
    except Exception:
        pass
    return f'temp-{uuid.uuid4()}'

def testnet_home(request):
    """Display testnet home page with overview"""
    testnet_config = TestnetConfig.objects.first()
    if not testnet_config:
        testnet_config = TestnetConfig.objects.create()
    
    pools = LiquidityPool.objects.all()
    
    context = {
        'testnet_config': testnet_config,
        'pools': pools,
    }
    return render(request, 'testnet/home.html', context)

@login_required
def swap(request):
    """Handle token swaps on testnet"""
    pools = LiquidityPool.objects.all()
    
    if request.method == 'POST':
        pool_id = request.POST.get('pool_id')
        from_token = request.POST.get('from_token', '').strip()
        to_token = request.POST.get('to_token', '').strip()
        amount = request.POST.get('amount', '').strip()
        
        # Validate inputs
        if not pool_id or not from_token or not to_token or not amount:
            messages.error(request, 'All fields are required.')
            return redirect('swap')
        
        try:
            amount = Decimal(amount)
            if amount <= 0:
                messages.error(request, 'Amount must be greater than zero.')
                return redirect('swap')
        except (ValueError, Exception):
            messages.error(request, 'Invalid amount specified.')
            return redirect('swap')
        
        try:
            # Use select_for_update to lock the pool row during transaction
            with transaction.atomic():
                pool = LiquidityPool.objects.select_for_update().get(id=pool_id)
                
                # Calculate swap amount using constant product formula (x * y = k)
                if from_token == pool.token_a_symbol:
                    reserve_in = pool.token_a_reserve
                    reserve_out = pool.token_b_reserve
                elif from_token == pool.token_b_symbol:
                    reserve_in = pool.token_b_reserve
                    reserve_out = pool.token_a_reserve
                else:
                    messages.error(request, 'Invalid token for this pool.')
                    return redirect('swap')
                
                # Calculate output amount with fee
                fee = amount * pool.fee_percentage / Decimal('100')
                amount_with_fee = amount - fee
                
                # Constant product formula: (x + Δx) * (y - Δy) = x * y
                # Δy = (y * Δx) / (x + Δx)
                output_amount = (reserve_out * amount_with_fee) / (reserve_in + amount_with_fee)
                
                # Validate sufficient reserves
                if output_amount >= reserve_out:
                    messages.error(request, 'Insufficient liquidity in pool for this swap.')
                    return redirect('swap')
                
                # Update pool reserves and accumulate fees for liquidity providers
                if from_token == pool.token_a_symbol:
                    pool.token_a_reserve += amount_with_fee  # Add only amount after fee
                    pool.token_b_reserve -= output_amount
                    pool.accumulated_token_a_fees += fee  # Accumulate fee separately
                else:
                    pool.token_b_reserve += amount_with_fee  # Add only amount after fee
                    pool.token_a_reserve -= output_amount
                    pool.accumulated_token_b_fees += fee  # Accumulate fee separately
                
                pool.save()
                
                # Distribute fees proportionally to all liquidity providers using atomic updates
                if pool.total_liquidity_tokens > 0:
                    positions = LiquidityPosition.objects.filter(pool=pool)
                    for position in positions:
                        share = position.liquidity_tokens / pool.total_liquidity_tokens
                        fee_amount = fee * share
                        if from_token == pool.token_a_symbol:
                            # Use F() expression for atomic update to prevent race conditions
                            LiquidityPosition.objects.filter(id=position.id).update(
                                unclaimed_token_a_fees=F('unclaimed_token_a_fees') + fee_amount
                            )
                        else:
                            LiquidityPosition.objects.filter(id=position.id).update(
                                unclaimed_token_b_fees=F('unclaimed_token_b_fees') + fee_amount
                            )
                
                # Record transaction with unique hash
                SwapTransaction.objects.create(
                    user=request.user,
                    pool=pool,
                    from_token=from_token,
                    to_token=to_token,
                    from_amount=amount,
                    to_amount=output_amount,
                    fee_amount=fee,
                    tx_hash=f'testnet-{uuid.uuid4()}'
                )
                
                messages.success(request, f'Successfully swapped {amount} {from_token} for {output_amount:.8f} {to_token}!')
            
        except LiquidityPool.DoesNotExist:
            messages.error(request, 'Pool not found.')
        except Exception as e:
            messages.error(request, f'Error executing swap: {str(e)}')
        
        return redirect('swap')
    
    context = {
        'pools': pools,
    }
    return render(request, 'testnet/swap.html', context)

@login_required
def liquidity(request):
    """Manage liquidity pools on testnet"""
    pools = LiquidityPool.objects.all()
    user_positions = LiquidityPosition.objects.filter(user=request.user)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        pool_id = request.POST.get('pool_id')
        
        if action == 'add':
            token_a_amount = request.POST.get('token_a_amount', '').strip()
            token_b_amount = request.POST.get('token_b_amount', '').strip()
            
            # Validate inputs
            if not pool_id or not token_a_amount or not token_b_amount:
                messages.error(request, 'All fields are required.')
                return redirect('liquidity')
            
            try:
                token_a_amount = Decimal(token_a_amount)
                token_b_amount = Decimal(token_b_amount)
                
                if token_a_amount <= 0 or token_b_amount <= 0:
                    messages.error(request, 'Amounts must be greater than zero.')
                    return redirect('liquidity')
                
                # Use atomic transaction and row locking
                with transaction.atomic():
                    pool = LiquidityPool.objects.select_for_update().get(id=pool_id)
                    
                    # Calculate liquidity tokens to mint
                    if pool.total_liquidity_tokens == 0:
                        # First liquidity provider - use geometric mean
                        liquidity_tokens = (token_a_amount * token_b_amount).sqrt()
                    else:
                        # Validate reserves are not zero
                        if pool.token_a_reserve <= 0 or pool.token_b_reserve <= 0:
                            messages.error(request, 'Pool has invalid reserves.')
                            return redirect('liquidity')
                        
                        # Proportional to existing liquidity
                        liquidity_tokens = min(
                            (token_a_amount * pool.total_liquidity_tokens) / pool.token_a_reserve,
                            (token_b_amount * pool.total_liquidity_tokens) / pool.token_b_reserve
                        )
                    
                    # Update pool reserves
                    pool.token_a_reserve += token_a_amount
                    pool.token_b_reserve += token_b_amount
                    pool.total_liquidity_tokens += liquidity_tokens
                    pool.save()
                    
                    # Update or create user position
                    position, created = LiquidityPosition.objects.get_or_create(
                        user=request.user,
                        pool=pool,
                        defaults={'liquidity_tokens': liquidity_tokens}
                    )
                    
                    if not created:
                        position.liquidity_tokens += liquidity_tokens
                        position.save()
                    
                    messages.success(request, f'Successfully added liquidity! Received {liquidity_tokens:.8f} liquidity tokens.')
                
            except LiquidityPool.DoesNotExist:
                messages.error(request, 'Pool not found.')
            except Exception as e:
                messages.error(request, f'Error adding liquidity: {str(e)}')
        
        elif action == 'remove':
            liquidity_tokens = request.POST.get('liquidity_tokens', '').strip()
            
            if not pool_id or not liquidity_tokens:
                messages.error(request, 'All fields are required.')
                return redirect('liquidity')
            
            try:
                liquidity_tokens = Decimal(liquidity_tokens)
                
                if liquidity_tokens <= 0:
                    messages.error(request, 'Amount must be greater than zero.')
                    return redirect('liquidity')
                
                # Use atomic transaction and row locking
                with transaction.atomic():
                    pool = LiquidityPool.objects.select_for_update().get(id=pool_id)
                    position = LiquidityPosition.objects.select_for_update().get(user=request.user, pool=pool)
                    
                    if liquidity_tokens > position.liquidity_tokens:
                        messages.error(request, 'Insufficient liquidity tokens.')
                        return redirect('liquidity')
                    
                    # Validate pool has liquidity
                    if pool.total_liquidity_tokens <= 0:
                        messages.error(request, 'Pool has no liquidity.')
                        return redirect('liquidity')
                    
                    # Calculate tokens to return
                    share = liquidity_tokens / pool.total_liquidity_tokens
                    token_a_amount = pool.token_a_reserve * share
                    token_b_amount = pool.token_b_reserve * share
                    
                    # Update pool reserves
                    pool.token_a_reserve -= token_a_amount
                    pool.token_b_reserve -= token_b_amount
                    pool.total_liquidity_tokens -= liquidity_tokens
                    pool.save()
                    
                    # Update user position
                    position.liquidity_tokens -= liquidity_tokens
                    if position.liquidity_tokens == 0:
                        position.delete()
                    else:
                        position.save()
                    
                    messages.success(request, f'Successfully removed liquidity! Received {token_a_amount:.8f} {pool.token_a_symbol} and {token_b_amount:.8f} {pool.token_b_symbol}.')
                
            except LiquidityPool.DoesNotExist:
                messages.error(request, 'Pool not found.')
            except LiquidityPosition.DoesNotExist:
                messages.error(request, 'No liquidity position found.')
            except Exception as e:
                messages.error(request, f'Error removing liquidity: {str(e)}')
        
        return redirect('liquidity')
    
    context = {
        'pools': pools,
        'user_positions': user_positions,
    }
    return render(request, 'testnet/liquidity.html', context)

@login_required
def transactions(request):
    """Display user's swap transaction history"""
    user_swaps = SwapTransaction.objects.filter(user=request.user).order_by('-created_at')
    
    context = {
        'transactions': user_swaps,
    }
    return render(request, 'testnet/transactions.html', context)

@login_required
def create_swap_offer(request, listing_id=None):
    """
    DEPRECATED: Redirects to the unified atomic swap creation flow.
    This function is kept for backward compatibility with existing URLs and templates.
    """
    messages.info(request, 'Atomic swaps are now created through the unified Atomic Swaps interface.')
    return redirect('create_listing')

@login_required
def accept_swap_offer(request, offer_id):
    """Accept and settle an atomic asset-for-EVR swap offer."""
    current_network = get_current_network_mode()
    swap_offer = get_object_or_404(SwapOffer, id=offer_id, network_mode=current_network)

    if request.method == 'POST':
        with transaction.atomic():
            swap_offer = SwapOffer.objects.select_for_update().select_related('initiator').get(
                id=offer_id,
                network_mode=current_network,
            )

            if swap_offer.status != 'pending':
                messages.error(request, 'This atomic swap is no longer available.')
                return redirect('available_swap_offers')

            if swap_offer.expires_at < timezone.now():
                swap_offer.status = 'expired'
                swap_offer.save(update_fields=['status', 'updated_at'])
                record_atomic_swap_stage_event(
                    swap_offer,
                    stage='swap_expired',
                    actor_username=request.user.username,
                    actor_user=request.user,
                    details={'reason': 'offer_expired_before_acceptance'},
                )
                messages.error(request, 'This atomic swap has expired.')
                return redirect('available_swap_offers')

            if swap_offer.counterparty and swap_offer.counterparty != request.user:
                messages.error(request, 'This atomic swap is not available to you.')
                return redirect('available_swap_offers')

            if swap_offer.initiator == request.user:
                messages.error(request, 'You cannot accept your own atomic swap.')
                return redirect('available_swap_offers')

            if get_active_atomic_swap_policy(
                current_network,
                required_stages=ATOMIC_SWAP_REQUIRED_STAGES,
            ) is None:
                messages.error(request, 'Atomic swaps require a verified active messaging channel for settlement events.')
                return redirect('available_swap_offers')

            if (
                classify_asset_type(swap_offer.offer_token) != TrackedAsset.ASSET_TYPE_UNIQUE
                or swap_offer.offer_amount != Decimal('1')
            ):
                messages.error(request, 'Atomic swaps only support one unique asset (NFT) on the offered side.')
                return redirect('available_swap_offers')

            seller_available = _get_available_token_amount(
                swap_offer.initiator,
                swap_offer.offer_token,
                current_network,
                exclude_offer_id=swap_offer.id,
            )
            if seller_available < swap_offer.offer_amount:
                messages.error(request, 'Swap initiator does not have enough unlocked asset balance for settlement.')
                return redirect('available_swap_offers')

            try:
                normalized_offer_amount = _normalize_swap_amount_for_asset(
                    swap_offer.offer_token,
                    swap_offer.offer_amount,
                    current_network,
                )
                normalized_request_amount = _normalize_swap_amount_for_asset(
                    swap_offer.request_token,
                    swap_offer.request_amount,
                    current_network,
                )
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect('available_swap_offers')

            buyer_available_settlement = _get_available_token_amount(
                request.user,
                swap_offer.request_token,
                current_network,
                exclude_offer_id=swap_offer.id,
            )
            if buyer_available_settlement < normalized_request_amount:
                messages.error(request, f'You do not have enough unlocked {swap_offer.request_token} balance for this swap.')
                return redirect('available_swap_offers')

            original_counterparty_id = swap_offer.counterparty_id
            SwapFundingLock.objects.create(
                swap_offer=swap_offer,
                user=swap_offer.initiator,
                token_symbol=swap_offer.offer_token.upper(),
                amount=normalized_offer_amount,
                status='locked',
            )
            SwapFundingLock.objects.create(
                swap_offer=swap_offer,
                user=request.user,
                token_symbol=swap_offer.request_token.upper(),
                amount=normalized_request_amount,
                status='locked',
            )
            swap_offer.counterparty = request.user
            swap_offer.status = 'settling'
            swap_offer.settlement_error = ''
            swap_offer.settlement_started_at = timezone.now()
            swap_offer.settlement_temp_txid = ''
            swap_offer.save(update_fields=[
                'counterparty',
                'status',
                'settlement_error',
                'settlement_started_at',
                'settlement_temp_txid',
                'updated_at',
            ])
            channel_message = record_atomic_swap_stage_event(
                swap_offer,
                stage='settlement_lock_created',
                actor_username=request.user.username,
                actor_user=request.user,
                details={
                    'seller_lock_amount': str(normalized_offer_amount),
                    'buyer_lock_amount': str(normalized_request_amount),
                },
            )
            if channel_message.status != 'broadcasted':
                swap_offer.status = 'pending'
                swap_offer.counterparty_id = original_counterparty_id
                swap_offer.settlement_error = (
                    channel_message.error_message
                    or 'The atomic swap messaging channel could not publish settlement_lock_created.'
                )
                swap_offer.settlement_started_at = None
                swap_offer.save(update_fields=[
                    'counterparty',
                    'status',
                    'settlement_error',
                    'settlement_started_at',
                    'updated_at',
                ])
                SwapFundingLock.objects.filter(swap_offer=swap_offer, status='locked').delete()
                messages.error(request, swap_offer.settlement_error)
                return redirect('available_swap_offers')

        try:
            seller_address = _get_user_primary_address(swap_offer.initiator)
            buyer_address = _get_user_primary_address(request.user)
            if not seller_address or not buyer_address:
                raise Exception('Both parties need a wallet address before settlement can begin.')

            seller_wif = _derive_user_wif_for_address(swap_offer.initiator, seller_address)
            buyer_wif = _derive_user_wif_for_address(request.user, buyer_address)
            if swap_offer.request_token.upper() == 'EVR':
                tx_data = create_raw_atomic_asset_evr_swap_transaction(
                    seller_address=seller_address,
                    buyer_address=buyer_address,
                    asset_name=swap_offer.offer_token,
                    asset_quantity=normalized_offer_amount,
                    payment_evr=normalized_request_amount,
                )
            else:
                tx_data = create_raw_atomic_asset_asset_swap_transaction(
                    seller_address=seller_address,
                    buyer_address=buyer_address,
                    seller_asset_name=swap_offer.offer_token,
                    seller_asset_quantity=normalized_offer_amount,
                    buyer_asset_name=swap_offer.request_token,
                    buyer_asset_quantity=normalized_request_amount,
                )
        except Exception as exc:
            with transaction.atomic():
                failed_offer = SwapOffer.objects.select_for_update().get(
                    id=offer_id,
                    network_mode=current_network,
                )
                if failed_offer.status == 'settling':
                    failed_offer.status = 'pending'
                    failed_offer.counterparty_id = original_counterparty_id
                    failed_offer.settlement_error = str(exc)
                    failed_offer.settlement_temp_txid = ''
                    failed_offer.save(update_fields=['status', 'counterparty', 'settlement_error', 'settlement_temp_txid', 'updated_at'])
                    _release_offer_funding_locks(failed_offer)
                    record_atomic_swap_stage_event(
                        failed_offer,
                        stage='settlement_build_failed',
                        actor_username=request.user.username,
                        actor_user=request.user,
                        details={'error': str(exc)},
                    )
            messages.error(request, f'Unable to build the atomic swap transaction: {str(exc)}')
            return redirect('available_swap_offers')

        temp_txid = _derive_temp_txid(tx_data['raw_tx'])
        with transaction.atomic():
            settling_offer = SwapOffer.objects.select_for_update().get(
                id=offer_id,
                network_mode=current_network,
            )
            if settling_offer.status == 'settling':
                settling_offer.settlement_temp_txid = temp_txid
                settling_offer.save(update_fields=['settlement_temp_txid', 'updated_at'])

        try:
            txid = sign_and_broadcast_raw_transaction(
                tx_data['raw_tx'],
                wif_keys=[seller_wif, buyer_wif],
            )
        except Exception as exc:
            with transaction.atomic():
                failed_offer = SwapOffer.objects.select_for_update().get(
                    id=offer_id,
                    network_mode=current_network,
                )
                if failed_offer.status == 'settling':
                    failed_offer.settlement_error = (
                        f'Broadcast outcome requires reconciliation before retrying: {str(exc)}'
                    )
                    failed_offer.save(update_fields=['settlement_error', 'updated_at'])
                    record_atomic_swap_stage_event(
                        failed_offer,
                        stage='settlement_pending_reconciliation',
                        actor_username=request.user.username,
                        actor_user=request.user,
                        txid=failed_offer.settlement_temp_txid,
                        details={'error': str(exc)},
                    )
            messages.error(request, 'The atomic swap broadcast needs reconciliation before it can be retried.')
            return redirect('available_swap_offers')

        with transaction.atomic():
            settled_offer = SwapOffer.objects.select_for_update().select_related('initiator').get(
                id=offer_id,
                network_mode=current_network,
            )
            if settled_offer.status != 'settling' or settled_offer.counterparty != request.user:
                messages.error(request, 'Atomic swap settlement state changed unexpectedly.')
                return redirect('available_swap_offers')

            settled_offer.status = 'completed'
            settled_offer.settlement_txid = txid
            settled_offer.settlement_error = ''
            settled_offer.save(update_fields=['status', 'settlement_txid', 'settlement_error', 'updated_at'])
            _consume_offer_funding_locks(settled_offer)
            P2PSwapTransaction.objects.create(
                swap_offer=settled_offer,
                initiator=settled_offer.initiator,
                counterparty=request.user,
                initiator_token=settled_offer.offer_token,
                initiator_amount=settled_offer.offer_amount,
                counterparty_token=settled_offer.request_token,
                counterparty_amount=settled_offer.request_amount,
                tx_hash=txid,
            )
            record_atomic_swap_stage_event(
                settled_offer,
                stage='settlement_broadcasted',
                actor_username=request.user.username,
                actor_user=request.user,
                txid=txid,
                details={'status': 'completed'},
            )

        messages.success(
            request,
            f'Atomic swap broadcast successfully. Transaction ID: {txid}',
        )
        return redirect('my_swap_history')
    
    context = {
        'swap_offer': swap_offer,
    }
    return render(request, 'defi/accept_swap_offer.html', context)

@login_required
def cancel_swap_offer(request, offer_id):
    """Allow the creator to manually remove a cancellable atomic swap."""
    swap_offer = get_object_or_404(
        SwapOffer,
        id=offer_id,
        initiator=request.user,
        network_mode=get_current_network_mode(),
    )

    removable_statuses = {'pending', 'settling', 'accepted'}
    if swap_offer.status not in removable_statuses:
        messages.error(request, 'This swap can no longer be removed.')
        return redirect('my_swap_offers')

    if request.method == 'POST':
        with transaction.atomic():
            swap_offer = SwapOffer.objects.select_for_update().get(
                id=offer_id,
                initiator=request.user,
                network_mode=get_current_network_mode(),
            )
            if swap_offer.status not in removable_statuses:
                messages.error(request, 'This swap can no longer be removed.')
                return redirect('my_swap_offers')

            swap_offer.status = 'cancelled'
            swap_offer.settlement_error = (
                f'{swap_offer.settlement_error}\nCreator manually removed this swap.'
                if swap_offer.settlement_error else 'Creator manually removed this swap.'
            )
            swap_offer.save(update_fields=['status', 'settlement_error', 'updated_at'])
            _release_offer_funding_locks(swap_offer)
            record_atomic_swap_stage_event(
                swap_offer,
                stage='swap_cancelled',
                actor_username=request.user.username,
                actor_user=request.user,
                details={'reason': 'initiator_cancelled_swap'},
            )

        messages.success(request, 'Atomic swap removed and all settlement locks released.')
        return redirect('my_swap_offers')
    
    context = {
        'swap_offer': swap_offer,
    }
    return render(request, 'defi/cancel_swap_offer.html', context)

@login_required
def my_swap_offers(request):
    """Display a user's created atomic swaps."""
    purge_expired_swap_offers(network_mode=get_current_network_mode())
    offers = SwapOffer.objects.filter(
        initiator=request.user,
        network_mode=get_current_network_mode(),
    ).order_by('-created_at')
    
    context = {
        'offers': offers,
    }
    return render(request, 'defi/my_swap_offers.html', context)

@login_required
def available_swap_offers(request):
    """Display atomic swaps available to the user."""
    current_network = get_current_network_mode()
    purge_expired_swap_offers(network_mode=current_network)

    # Show atomic swaps that are:
    # 1. Pending
    # 2. Not expired
    # 3. Either no counterparty or counterparty is current user
    # 4. Not created by current user
    offers = SwapOffer.objects.filter(
        Q(network_mode=current_network),
        Q(status='pending'),
        Q(expires_at__gt=timezone.now()),
        Q(counterparty__isnull=True) | Q(counterparty=request.user)
    ).exclude(
        initiator=request.user
    ).order_by('-created_at')
    
    context = {
        'offers': offers,
    }
    return render(request, 'defi/available_swap_offers.html', context)

@login_required
def my_swap_history(request):
    """Display a user's completed atomic swap history."""
    swaps = P2PSwapTransaction.objects.filter(
        Q(initiator=request.user) | Q(counterparty=request.user),
        swap_offer__network_mode=get_current_network_mode(),
    ).order_by('-completed_at')
    
    context = {
        'swaps': swaps,
    }
    return render(request, 'defi/my_swap_history.html', context)

@login_required
def claim_fees(request):
    """Claim accumulated fees from liquidity provision"""
    user_positions = LiquidityPosition.objects.filter(user=request.user)
    
    if request.method == 'POST':
        position_id = request.POST.get('position_id')
        
        if not position_id:
            messages.error(request, 'Position ID is required.')
            return redirect('claim_fees')
        
        # Validate position_id is a valid integer
        try:
            position_id = int(position_id)
        except (ValueError, TypeError):
            messages.error(request, 'Invalid position ID.')
            return redirect('claim_fees')
        
        try:
            with transaction.atomic():
                position = LiquidityPosition.objects.select_for_update().get(
                    id=position_id, 
                    user=request.user
                )
                pool = LiquidityPool.objects.select_for_update().get(id=position.pool.id)
                
                # Check if there are fees to claim
                if position.unclaimed_token_a_fees == 0 and position.unclaimed_token_b_fees == 0:
                    messages.info(request, 'No fees available to claim.')
                    return redirect('claim_fees')
                
                # Claim the fees
                claimed_token_a = position.unclaimed_token_a_fees
                claimed_token_b = position.unclaimed_token_b_fees
                
                # Validate pool has sufficient accumulated fees to prevent negative values
                if pool.accumulated_token_a_fees < claimed_token_a or pool.accumulated_token_b_fees < claimed_token_b:
                    messages.error(request, 'Pool has insufficient accumulated fees. Please contact support.')
                    return redirect('claim_fees')
                
                # Deduct from pool accumulated fees
                pool.accumulated_token_a_fees -= claimed_token_a
                pool.accumulated_token_b_fees -= claimed_token_b
                pool.save()
                
                # Reset unclaimed fees
                position.unclaimed_token_a_fees = 0
                position.unclaimed_token_b_fees = 0
                position.save()
                
                fee_message = []
                if claimed_token_a > 0:
                    fee_message.append(f'{claimed_token_a:.8f} {pool.token_a_symbol}')
                if claimed_token_b > 0:
                    fee_message.append(f'{claimed_token_b:.8f} {pool.token_b_symbol}')
                
                messages.success(request, f'Successfully claimed fees: {" and ".join(fee_message)}!')
                
        except LiquidityPosition.DoesNotExist:
            messages.error(request, 'Position not found.')
        except Exception as e:
            messages.error(request, f'Error claiming fees: {str(e)}')
        
        return redirect('claim_fees')
    
    context = {
        'user_positions': user_positions,
    }
    return render(request, 'defi/claim_fees.html', context)

def price_feeds(request):
    """Display current price feeds from oracle network"""
    # Get latest aggregated prices for each token
    latest_prices = {}
    tokens = PriceFeedAggregation.objects.values_list('token_symbol', flat=True).distinct()
    
    for token in tokens:
        latest_price = PriceFeedAggregation.objects.filter(token_symbol=token).order_by('-timestamp').first()
        if latest_price:
            latest_prices[token] = latest_price
    
    # Get active oracle sources
    oracle_sources = PriceFeedSource.objects.filter(is_active=True)
    
    context = {
        'latest_prices': latest_prices,
        'oracle_sources': oracle_sources,
    }
    return render(request, 'oracle/price_feeds.html', context)

@login_required
def submit_price(request):
    """Submit price data to oracle network (oracle node functionality)"""
    if request.method == 'POST':
        oracle_address = request.POST.get('oracle_address', '').strip()
        token_symbol = request.POST.get('token_symbol', '').strip().upper()
        price_usd = request.POST.get('price_usd', '').strip()
        
        # Validate inputs
        if not oracle_address or not token_symbol or not price_usd:
            messages.error(request, 'All fields are required.')
            return redirect('submit_price')
        
        try:
            price_usd = Decimal(price_usd)
            if price_usd <= 0:
                messages.error(request, 'Price must be greater than zero.')
                return redirect('submit_price')
        except (ValueError, InvalidOperation):
            messages.error(request, 'Invalid price format.')
            return redirect('submit_price')
        
        # Get or create oracle source
        source, created = PriceFeedSource.objects.get_or_create(
            oracle_address=oracle_address,
            defaults={
                'name': f'Oracle {oracle_address[:8]}...',
                'is_active': True
            }
        )
        
        if not source.is_active:
            messages.error(request, 'This oracle source is not active.')
            return redirect('submit_price')
        
        # Create price submission
        PriceFeedData.objects.create(
            source=source,
            token_symbol=token_symbol,
            price_usd=price_usd,
            tx_hash=f'oracle-{uuid.uuid4()}'
        )
        
        # Update source submission count atomically
        PriceFeedSource.objects.filter(id=source.id).update(
            total_submissions=F('total_submissions') + 1
        )
        
        # Trigger price aggregation for this token
        _aggregate_price_feeds(token_symbol)
        
        messages.success(request, f'Price submitted successfully: {token_symbol} = ${price_usd}')
        return redirect('price_feeds')
    
    # Get user's oracle sources if any
    oracle_sources = PriceFeedSource.objects.all().order_by('-created_at')[:10]
    
    context = {
        'oracle_sources': oracle_sources,
    }
    return render(request, 'oracle/submit_price.html', context)

def price_history(request, token_symbol):
    """Display price history for a specific token"""
    token_symbol = token_symbol.upper()
    
    # Get aggregated price history
    price_history = PriceFeedAggregation.objects.filter(
        token_symbol=token_symbol
    ).order_by('-timestamp')[:100]
    
    # Get recent individual submissions (with source info)
    recent_submissions = PriceFeedData.objects.filter(
        token_symbol=token_symbol
    ).select_related('source').order_by('-timestamp')[:50]
    
    context = {
        'token_symbol': token_symbol,
        'price_history': price_history,
        'recent_submissions': recent_submissions,
    }
    return render(request, 'oracle/price_history.html', context)

@login_required
def manage_oracle(request):
    """Manage oracle source settings"""
    if request.method == 'POST':
        action = request.POST.get('action')
        oracle_address = request.POST.get('oracle_address', '').strip()
        
        if action == 'register':
            name = request.POST.get('name', '').strip()
            description = request.POST.get('description', '').strip()
            
            if not oracle_address or not name:
                messages.error(request, 'Oracle address and name are required.')
                return redirect('manage_oracle')
            
            # Check if oracle already exists
            if PriceFeedSource.objects.filter(oracle_address=oracle_address).exists():
                messages.error(request, 'This oracle address is already registered.')
                return redirect('manage_oracle')
            
            # Create new oracle source
            PriceFeedSource.objects.create(
                name=name,
                description=description,
                oracle_address=oracle_address,
                is_active=True
            )
            
            messages.success(request, f'Oracle {name} registered successfully!')
            return redirect('manage_oracle')
        
        elif action == 'toggle':
            try:
                source = PriceFeedSource.objects.get(oracle_address=oracle_address)
                source.is_active = not source.is_active
                source.save()
                status = 'activated' if source.is_active else 'deactivated'
                messages.success(request, f'Oracle {source.name} {status}.')
            except PriceFeedSource.DoesNotExist:
                messages.error(request, 'Oracle source not found.')
            
            return redirect('manage_oracle')
    
    # Get all oracle sources
    oracle_sources = PriceFeedSource.objects.all().order_by('-reputation_score')
    
    context = {
        'oracle_sources': oracle_sources,
    }
    return render(request, 'oracle/manage_oracle.html', context)

def _aggregate_price_feeds(token_symbol):
    """Internal function to aggregate price feeds from multiple sources"""
    # Get recent price submissions (last 5 minutes)
    cutoff_time = timezone.now() - timedelta(minutes=5)
    recent_prices = PriceFeedData.objects.filter(
        token_symbol=token_symbol,
        timestamp__gte=cutoff_time,
        source__is_active=True
    ).order_by('-timestamp')
    
    if not recent_prices:
        return
    
    # Get unique sources (one submission per source)
    sources_seen = set()
    price_values = []
    
    for price_data in recent_prices:
        if price_data.source_id not in sources_seen:
            sources_seen.add(price_data.source_id)
            price_values.append(float(price_data.price_usd))
    
    if not price_values:
        return
    
    # Calculate aggregated metrics
    median_price = Decimal(str(statistics.median(price_values)))
    avg_price = Decimal(str(statistics.mean(price_values)))
    min_price = Decimal(str(min(price_values)))
    max_price = Decimal(str(max(price_values)))
    
    # Calculate confidence score based on number of sources and price variance
    num_sources = len(price_values)
    if num_sources > 1:
        std_dev = statistics.stdev(price_values)
        # Prevent division by zero
        if float(avg_price) > 0:
            # Confidence decreases with higher variance
            # Max 50% penalty for high variance
            variance_penalty = min(std_dev / float(avg_price) * 100, 50)
            confidence = max(0, 100 - variance_penalty)
        else:
            confidence = 50
    else:
        confidence = 50  # Lower confidence with single source
    
    # Create aggregation record
    PriceFeedAggregation.objects.create(
        token_symbol=token_symbol,
        aggregated_price=median_price,  # Use median as aggregated price
        median_price=median_price,
        min_price=min_price,
        max_price=max_price,
        num_sources=num_sources,
        confidence_score=Decimal(str(confidence))
    )


# Lending Views

def lending_home(request):
    """Display lending home page with overview"""
    lending_pools = LendingPool.objects.filter(is_active=True)
    collateral_assets = CollateralAsset.objects.filter(is_active=True)
    
    # Get user deposits and loans if authenticated
    user_deposits = []
    user_loans = []
    if request.user.is_authenticated:
        user_deposits = Deposit.objects.filter(user=request.user)
        user_loans = Loan.objects.filter(user=request.user, status='active')
    
    context = {
        'lending_pools': lending_pools,
        'collateral_assets': collateral_assets,
        'user_deposits': user_deposits,
        'user_loans': user_loans,
    }
    return render(request, 'lending/home.html', context)

@login_required
def deposit_funds(request):
    """Handle deposits to earn interest"""
    lending_pools = LendingPool.objects.filter(is_active=True)
    
    if request.method == 'POST':
        pool_id = request.POST.get('pool_id')
        amount = request.POST.get('amount', '').strip()
        
        # Validate inputs
        if not pool_id or not amount:
            messages.error(request, 'All fields are required.')
            return redirect('deposit_funds')
        
        try:
            amount = Decimal(amount)
            if amount <= 0:
                messages.error(request, 'Amount must be greater than zero.')
                return redirect('deposit_funds')
        except (ValueError, InvalidOperation):
            messages.error(request, 'Invalid amount specified.')
            return redirect('deposit_funds')
        
        try:
            with transaction.atomic():
                pool = LendingPool.objects.select_for_update().get(id=pool_id, is_active=True)
                
                # Get or create deposit record
                deposit, created = Deposit.objects.get_or_create(
                    user=request.user,
                    pool=pool,
                    defaults={'principal_amount': Decimal('0')}
                )
                
                # Update deposit amount
                deposit.principal_amount += amount
                deposit.last_interest_update = timezone.now()
                deposit.save()
                
                # Update pool totals
                pool.total_deposits += amount
                pool.save()
                
                messages.success(request, f'Successfully deposited {amount} {pool.token_symbol}. You are now earning {pool.current_supply_rate:.2f}% APR!')
                return redirect('lending_home')
                
        except LendingPool.DoesNotExist:
            messages.error(request, 'Invalid lending pool selected.')
            return redirect('deposit_funds')
        except Exception as e:
            messages.error(request, f'Error processing deposit: {str(e)}')
            return redirect('deposit_funds')
    
    # Get user's existing deposits
    user_deposits = Deposit.objects.filter(user=request.user)
    
    context = {
        'lending_pools': lending_pools,
        'user_deposits': user_deposits,
    }
    return render(request, 'lending/deposit.html', context)

@login_required
def borrow_funds(request):
    """Handle borrowing against collateral"""
    lending_pools = LendingPool.objects.filter(is_active=True)
    collateral_assets = CollateralAsset.objects.filter(is_active=True)
    
    if request.method == 'POST':
        pool_id = request.POST.get('pool_id')
        collateral_asset_id = request.POST.get('collateral_asset_id')
        borrow_amount = request.POST.get('borrow_amount', '').strip()
        collateral_amount = request.POST.get('collateral_amount', '').strip()
        
        # Validate inputs
        if not pool_id or not collateral_asset_id or not borrow_amount or not collateral_amount:
            messages.error(request, 'All fields are required.')
            return redirect('borrow_funds')
        
        try:
            borrow_amount = Decimal(borrow_amount)
            collateral_amount = Decimal(collateral_amount)
            
            if borrow_amount <= 0 or collateral_amount <= 0:
                messages.error(request, 'Amounts must be greater than zero.')
                return redirect('borrow_funds')
        except (ValueError, InvalidOperation):
            messages.error(request, 'Invalid amounts specified.')
            return redirect('borrow_funds')
        
        try:
            with transaction.atomic():
                pool = LendingPool.objects.select_for_update().get(id=pool_id, is_active=True)
                collateral_asset = CollateralAsset.objects.get(id=collateral_asset_id, is_active=True)
                
                # Check if pool has sufficient liquidity
                if borrow_amount > pool.available_liquidity:
                    messages.error(request, f'Insufficient liquidity. Available: {pool.available_liquidity} {pool.token_symbol}')
                    return redirect('borrow_funds')
                
                # Calculate maximum borrowable amount based on collateral
                # Using 1:1 price for simplicity - in production would use oracle prices
                max_borrow = collateral_amount * collateral_asset.collateral_factor / Decimal('100')
                
                if borrow_amount > max_borrow:
                    messages.error(request, f'Borrow amount exceeds maximum. Max: {max_borrow:.8f} {pool.token_symbol}')
                    return redirect('borrow_funds')
                
                # Create loan
                loan = Loan.objects.create(
                    user=request.user,
                    pool=pool,
                    collateral_asset=collateral_asset,
                    principal_amount=borrow_amount,
                    collateral_amount=collateral_amount,
                    accrued_interest=Decimal('0'),
                    status='active'
                )
                
                # Update pool totals
                pool.total_borrows += borrow_amount
                pool.save()
                
                messages.success(request, f'Successfully borrowed {borrow_amount} {pool.token_symbol} with {collateral_amount} {collateral_asset.token_symbol} as collateral.')
                return redirect('lending_home')
                
        except LendingPool.DoesNotExist:
            messages.error(request, 'Invalid lending pool selected.')
            return redirect('borrow_funds')
        except CollateralAsset.DoesNotExist:
            messages.error(request, 'Invalid collateral asset selected.')
            return redirect('borrow_funds')
        except Exception as e:
            messages.error(request, f'Error processing borrow: {str(e)}')
            return redirect('borrow_funds')
    
    # Get user's existing loans
    user_loans = Loan.objects.filter(user=request.user, status='active')
    
    context = {
        'lending_pools': lending_pools,
        'collateral_assets': collateral_assets,
        'user_loans': user_loans,
    }
    return render(request, 'lending/borrow.html', context)

@login_required
def repay_loan(request):
    """Handle loan repayment"""
    if request.method == 'POST':
        loan_id = request.POST.get('loan_id')
        amount = request.POST.get('amount', '').strip()
        
        # Validate inputs
        if not loan_id or not amount:
            messages.error(request, 'All fields are required.')
            return redirect('manage_positions')
        
        try:
            amount = Decimal(amount)
            if amount <= 0:
                messages.error(request, 'Amount must be greater than zero.')
                return redirect('manage_positions')
        except (ValueError, InvalidOperation):
            messages.error(request, 'Invalid amount specified.')
            return redirect('manage_positions')
        
        try:
            with transaction.atomic():
                loan = Loan.objects.select_for_update().get(id=loan_id, user=request.user, status='active')
                pool = LendingPool.objects.select_for_update().get(id=loan.pool_id)
                
                total_debt = loan.total_debt
                
                # Check if amount exceeds debt
                if amount > total_debt:
                    amount = total_debt
                
                # Calculate interest and principal portions
                if amount >= loan.accrued_interest:
                    interest_paid = loan.accrued_interest
                    principal_paid = amount - interest_paid
                else:
                    interest_paid = amount
                    principal_paid = Decimal('0')
                
                # Update loan
                loan.accrued_interest -= interest_paid
                loan.principal_amount -= principal_paid
                
                # Record repayment
                LoanRepayment.objects.create(
                    loan=loan,
                    amount=amount,
                    interest_paid=interest_paid,
                    principal_paid=principal_paid
                )
                
                # Update pool totals
                pool.total_borrows -= principal_paid
                pool.total_reserves += interest_paid
                pool.save()
                
                # If fully repaid, mark as such
                if loan.principal_amount <= Decimal('0.00000001') and loan.accrued_interest <= Decimal('0.00000001'):
                    loan.status = 'repaid'
                    loan.repaid_at = timezone.now()
                    messages.success(request, f'Loan fully repaid! {loan.collateral_amount} {loan.collateral_asset.token_symbol} collateral returned.')
                else:
                    messages.success(request, f'Successfully repaid {amount} {pool.token_symbol}. Remaining debt: {loan.total_debt:.8f}')
                
                loan.save()
                return redirect('manage_positions')
                
        except Loan.DoesNotExist:
            messages.error(request, 'Invalid loan selected.')
            return redirect('manage_positions')
        except Exception as e:
            messages.error(request, f'Error processing repayment: {str(e)}')
            return redirect('manage_positions')
    
    return redirect('manage_positions')

@login_required
def withdraw_deposit(request):
    """Handle withdrawal of deposits"""
    if request.method == 'POST':
        deposit_id = request.POST.get('deposit_id')
        amount = request.POST.get('amount', '').strip()
        
        # Validate inputs
        if not deposit_id or not amount:
            messages.error(request, 'All fields are required.')
            return redirect('manage_positions')
        
        try:
            amount = Decimal(amount)
            if amount <= 0:
                messages.error(request, 'Amount must be greater than zero.')
                return redirect('manage_positions')
        except (ValueError, InvalidOperation):
            messages.error(request, 'Invalid amount specified.')
            return redirect('manage_positions')
        
        try:
            with transaction.atomic():
                deposit = Deposit.objects.select_for_update().get(id=deposit_id, user=request.user)
                pool = LendingPool.objects.select_for_update().get(id=deposit.pool_id)
                
                # Check available balance
                available = deposit.total_balance
                if amount > available:
                    messages.error(request, f'Insufficient balance. Available: {available:.8f} {pool.token_symbol}')
                    return redirect('manage_positions')
                
                # Check pool liquidity
                if amount > pool.available_liquidity:
                    messages.error(request, f'Insufficient pool liquidity. Try a smaller amount.')
                    return redirect('manage_positions')
                
                # Withdraw from accrued interest first, then principal
                withdrawn_interest = Decimal('0')
                withdrawn_principal = Decimal('0')
                
                if amount <= deposit.accrued_interest:
                    withdrawn_interest = amount
                    deposit.accrued_interest -= amount
                else:
                    withdrawn_interest = deposit.accrued_interest
                    withdrawn_principal = amount - withdrawn_interest
                    deposit.accrued_interest = Decimal('0')
                    deposit.principal_amount -= withdrawn_principal
                
                deposit.save()
                
                # Update pool totals
                pool.total_deposits -= withdrawn_principal
                pool.total_reserves -= withdrawn_interest
                pool.save()
                
                # If deposit is now effectively zero, delete it
                if deposit.total_balance <= Decimal('0.00000001'):
                    deposit.delete()
                
                messages.success(request, f'Successfully withdrew {amount:.8f} {pool.token_symbol}')
                return redirect('manage_positions')
                
        except Deposit.DoesNotExist:
            messages.error(request, 'Invalid deposit selected.')
            return redirect('manage_positions')
        except Exception as e:
            messages.error(request, f'Error processing withdrawal: {str(e)}')
            return redirect('manage_positions')
    
    return redirect('manage_positions')

@login_required
def manage_positions(request):
    """View and manage user's deposits and loans"""
    from django.db.models import Sum
    
    user_deposits = Deposit.objects.filter(user=request.user).select_related('pool')
    user_loans = Loan.objects.filter(user=request.user).exclude(status='repaid').select_related('pool', 'collateral_asset')
    
    # Calculate totals using aggregation
    deposits_agg = user_deposits.aggregate(
        total=Sum('principal_amount') 
    )
    loans_agg = user_loans.aggregate(
        total=Sum('principal_amount')
    )
    
    total_deposited = deposits_agg['total'] or Decimal('0')
    total_borrowed = loans_agg['total'] or Decimal('0')
    
    context = {
        'user_deposits': user_deposits,
        'user_loans': user_loans,
        'total_deposited': total_deposited,
        'total_borrowed': total_borrowed,
    }
    return render(request, 'lending/manage.html', context)

# Fixed/Variable Rate Instruments
@login_required
def rates_marketplace(request):
    """Marketplace view for fixed and variable rate instruments"""
    from .models import FixedRateBond, VariableRateSavings, InterestRateSnapshot, LendingPool
    from django.db.models import Avg
    
    # Get all lending pools with current rates
    lending_pools = LendingPool.objects.filter(is_active=True).select_related('interest_rate_config')
    
    # Get fixed rate options (simulate available fixed rate products)
    fixed_rate_options = {
        30: Decimal('4.5'),   # 30 days: 4.5% APR
        90: Decimal('5.2'),   # 90 days: 5.2% APR
        180: Decimal('6.0'),  # 180 days: 6.0% APR
        365: Decimal('7.5'),  # 365 days: 7.5% APR
    }
    
    # Get user's active bonds and savings
    user_bonds = FixedRateBond.objects.filter(user=request.user, status='active')
    user_savings = VariableRateSavings.objects.filter(user=request.user, status='active').select_related('pool')
    
    # Get recent rate history for charts
    recent_snapshots = InterestRateSnapshot.objects.filter(
        token_symbol='TOME'
    ).order_by('rate_type', '-timestamp').distinct('rate_type')[:10]
    
    context = {
        'lending_pools': lending_pools,
        'fixed_rate_options': fixed_rate_options,
        'user_bonds': user_bonds,
        'user_savings': user_savings,
        'recent_snapshots': recent_snapshots,
    }
    return render(request, 'defi/rates_marketplace.html', context)

@login_required
def purchase_fixed_bond(request):
    """Purchase a fixed-rate bond"""
    from .models import FixedRateBond
    from datetime import timedelta
    from django.utils import timezone
    
    if request.method == 'POST':
        token_symbol = request.POST.get('token_symbol', 'TOME').strip().upper()
        amount = request.POST.get('amount', '').strip()
        term_days = request.POST.get('term_days', '').strip()
        
        # Validate inputs
        if not amount or not term_days:
            messages.error(request, 'Amount and term are required.')
            return redirect('rates_marketplace')
        
        try:
            amount_decimal = Decimal(amount)
            term_days_int = int(term_days)
            
            if amount_decimal <= 0:
                messages.error(request, 'Amount must be greater than zero.')
                return redirect('rates_marketplace')
            
            if term_days_int not in [30, 90, 180, 365]:
                messages.error(request, 'Invalid term length.')
                return redirect('rates_marketplace')
            
            # Determine fixed rate based on term
            fixed_rates = {
                30: Decimal('4.5'),
                90: Decimal('5.2'),
                180: Decimal('6.0'),
                365: Decimal('7.5'),
            }
            fixed_rate = fixed_rates[term_days_int]
            
            # Calculate maturity values
            # Interest = Principal * (Rate / 100) * (Days / 365)
            expected_interest = amount_decimal * (fixed_rate / Decimal('100')) * (Decimal(term_days_int) / Decimal('365'))
            maturity_amount = amount_decimal + expected_interest
            maturity_date = timezone.now() + timedelta(days=term_days_int)
            
            # Create the bond
            bond = FixedRateBond.objects.create(
                user=request.user,
                token_symbol=token_symbol,
                principal_amount=amount_decimal,
                fixed_rate_apr=fixed_rate,
                term_days=term_days_int,
                maturity_amount=maturity_amount,
                expected_interest=expected_interest,
                maturity_date=maturity_date,
                status='active'
            )
            
            messages.success(request, f'Fixed-rate bond purchased! You will receive {maturity_amount} {token_symbol} after {term_days_int} days.')
            return redirect('rates_marketplace')
            
        except (ValueError, InvalidOperation) as e:
            messages.error(request, 'Invalid amount or term format.')
            return redirect('rates_marketplace')
        except Exception as e:
            messages.error(request, f'Error purchasing bond: {str(e)}')
            return redirect('rates_marketplace')
    
    return redirect('rates_marketplace')

@login_required
def open_variable_savings(request):
    """Open a variable-rate savings account"""
    from .models import VariableRateSavings, LendingPool
    
    if request.method == 'POST':
        pool_id = request.POST.get('pool_id')
        amount = request.POST.get('amount', '').strip()
        
        # Validate inputs
        if not pool_id or not amount:
            messages.error(request, 'Pool and amount are required.')
            return redirect('rates_marketplace')
        
        try:
            amount_decimal = Decimal(amount)
            
            if amount_decimal <= 0:
                messages.error(request, 'Amount must be greater than zero.')
                return redirect('rates_marketplace')
            
            pool = LendingPool.objects.get(id=pool_id, is_active=True)
            current_rate = pool.current_supply_rate
            
            # Create or update variable savings
            savings, created = VariableRateSavings.objects.get_or_create(
                user=request.user,
                pool=pool,
                status='active',
                defaults={
                    'principal_amount': amount_decimal,
                    'opening_rate': current_rate,
                    'current_rate': current_rate,
                }
            )
            
            if not created:
                # Update existing savings
                savings.principal_amount += amount_decimal
                savings.current_rate = current_rate
                savings.save()
            
            messages.success(request, f'Variable-rate savings opened! Current APR: {current_rate}%')
            return redirect('rates_marketplace')
            
        except LendingPool.DoesNotExist:
            messages.error(request, 'Lending pool not found.')
            return redirect('rates_marketplace')
        except (ValueError, InvalidOperation):
            messages.error(request, 'Invalid amount format.')
            return redirect('rates_marketplace')
        except Exception as e:
            messages.error(request, f'Error opening savings: {str(e)}')
            return redirect('rates_marketplace')
    
    return redirect('rates_marketplace')

@login_required
def redeem_bond(request, bond_id):
    """Redeem a matured fixed-rate bond"""
    from .models import FixedRateBond
    from django.utils import timezone
    
    if request.method == 'POST':
        try:
            bond = FixedRateBond.objects.get(id=bond_id, user=request.user, status='active')
            
            if not bond.is_matured:
                messages.warning(request, f'Bond is not yet matured. {bond.days_remaining} days remaining.')
                return redirect('rates_marketplace')
            
            # Redeem the bond
            bond.status = 'redeemed'
            bond.redeemed_at = timezone.now()
            bond.save()
            
            messages.success(request, f'Bond redeemed successfully! You received {bond.maturity_amount} {bond.token_symbol}.')
            
        except FixedRateBond.DoesNotExist:
            messages.error(request, 'Bond not found.')
        except Exception as e:
            messages.error(request, f'Error redeeming bond: {str(e)}')
        
        return redirect('rates_marketplace')
    
    return redirect('rates_marketplace')

@login_required
def withdraw_variable_savings(request, savings_id):
    """Withdraw from variable-rate savings"""
    from .models import VariableRateSavings
    from django.utils import timezone
    
    if request.method == 'POST':
        try:
            savings = VariableRateSavings.objects.get(id=savings_id, user=request.user, status='active')
            
            # Withdraw the savings
            total_withdrawal = savings.total_balance
            savings.status = 'withdrawn'
            savings.withdrawn_at = timezone.now()
            savings.save()
            
            messages.success(request, f'Savings withdrawn successfully! You received {total_withdrawal} {savings.pool.token_symbol}.')
            
        except VariableRateSavings.DoesNotExist:
            messages.error(request, 'Savings account not found.')
        except Exception as e:
            messages.error(request, f'Error withdrawing savings: {str(e)}')
        
        return redirect('rates_marketplace')
    
    return redirect('rates_marketplace')
