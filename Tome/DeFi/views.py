from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import uuid
from .models import (
    TestnetConfig, LiquidityPool, LiquidityPosition, SwapTransaction, 
    SwapOffer, SwapEscrow, P2PSwapTransaction, PriceFeedSource, 
    PriceFeedData, PriceFeedAggregation, CollateralAsset, InterestRateConfig,
    LendingPool, Deposit, Loan, LoanRepayment, Liquidation
)
import statistics

# Create your views here.

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
    """Create a P2P swap offer"""
    marketplace_listing = None
    initial_data = {}
    
    if listing_id:
        from Marketplace.models import MarketplaceListing
        try:
            marketplace_listing = MarketplaceListing.objects.get(id=listing_id)
            if not marketplace_listing.allow_swaps:
                messages.error(request, 'This listing does not accept swap offers.')
                return redirect('marketplace')
            # Pre-populate with listing information
            initial_data = {
                'counterparty': marketplace_listing.seller.username,
                'preferred_token': marketplace_listing.preferred_swap_token
            }
        except MarketplaceListing.DoesNotExist:
            messages.error(request, 'Listing not found.')
            return redirect('marketplace')
    
    if request.method == 'POST':
        offer_token = request.POST.get('offer_token', '').strip().upper()
        offer_amount = request.POST.get('offer_amount', '').strip()
        request_token = request.POST.get('request_token', '').strip().upper()
        request_amount = request.POST.get('request_amount', '').strip()
        counterparty_username = request.POST.get('counterparty', '').strip()
        
        # Validate inputs
        if not all([offer_token, offer_amount, request_token, request_amount]):
            messages.error(request, 'All fields are required.')
            return redirect('create_swap_offer')
        
        # Validate token symbols are different
        if offer_token == request_token:
            messages.error(request, 'Cannot swap the same token for itself.')
            return redirect('create_swap_offer')
        
        # Validate token format (alphanumeric only)
        if not offer_token.isalnum() or not request_token.isalnum():
            messages.error(request, 'Token symbols must be alphanumeric only.')
            return redirect('create_swap_offer')
        
        try:
            offer_amount = Decimal(offer_amount)
            request_amount = Decimal(request_amount)
            
            if offer_amount <= 0 or request_amount <= 0:
                messages.error(request, 'Amounts must be greater than zero.')
                return redirect('create_swap_offer')
        except (ValueError, InvalidOperation):
            messages.error(request, 'Invalid amount format.')
            return redirect('create_swap_offer')
        
        # Get counterparty if specified
        counterparty = None
        if counterparty_username:
            try:
                from django.contrib.auth.models import User
                counterparty = User.objects.get(username=counterparty_username)
                if counterparty == request.user:
                    messages.error(request, 'Cannot create swap offer with yourself.')
                    return redirect('create_swap_offer')
            except User.DoesNotExist:
                messages.error(request, f'User {counterparty_username} not found.')
                return redirect('create_swap_offer')
        
        # Create swap offer
        expires_at = timezone.now() + timedelta(days=7)
        swap_offer = SwapOffer.objects.create(
            initiator=request.user,
            counterparty=counterparty,
            marketplace_listing=marketplace_listing,
            offer_token=offer_token,
            offer_amount=offer_amount,
            request_token=request_token,
            request_amount=request_amount,
            expires_at=expires_at,
            escrow_id=f'escrow-{uuid.uuid4()}'
        )
        
        messages.success(request, 'Swap offer created successfully!')
        return redirect('my_swap_offers')
    
    context = {
        'initial_data': initial_data,
        'marketplace_listing': marketplace_listing
    }
    return render(request, 'defi/create_swap_offer.html', context)

@login_required
def accept_swap_offer(request, offer_id):
    """Accept a P2P swap offer"""
    swap_offer = get_object_or_404(SwapOffer, id=offer_id)
    
    # Validate offer can be accepted
    if swap_offer.status != 'pending':
        messages.error(request, 'This swap offer is no longer available.')
        return redirect('available_swap_offers')
    
    if swap_offer.expires_at < timezone.now():
        swap_offer.status = 'expired'
        swap_offer.save()
        messages.error(request, 'This swap offer has expired.')
        return redirect('available_swap_offers')
    
    if swap_offer.counterparty and swap_offer.counterparty != request.user:
        messages.error(request, 'This swap offer is not available to you.')
        return redirect('available_swap_offers')
    
    if swap_offer.initiator == request.user:
        messages.error(request, 'You cannot accept your own swap offer.')
        return redirect('available_swap_offers')
    
    if request.method == 'POST':
        try:
            with transaction.atomic():
                # Update swap offer
                swap_offer.counterparty = request.user
                swap_offer.status = 'accepted'
                swap_offer.save()
                
                # Create escrow
                SwapEscrow.objects.create(
                    swap_offer=swap_offer,
                    initiator_locked=True,
                    counterparty_locked=True,
                    initiator_amount=swap_offer.offer_amount,
                    counterparty_amount=swap_offer.request_amount
                )
                
                # Execute the swap
                swap_offer.status = 'completed'
                swap_offer.save()
                
                # Create transaction record
                P2PSwapTransaction.objects.create(
                    swap_offer=swap_offer,
                    initiator=swap_offer.initiator,
                    counterparty=request.user,
                    initiator_token=swap_offer.offer_token,
                    initiator_amount=swap_offer.offer_amount,
                    counterparty_token=swap_offer.request_token,
                    counterparty_amount=swap_offer.request_amount,
                    tx_hash=f'p2p-{uuid.uuid4()}'
                )
                
                # Update escrow
                escrow = swap_offer.escrow
                escrow.released_at = timezone.now()
                escrow.save()
                
                messages.success(request, f'Swap completed! You exchanged {swap_offer.request_amount} {swap_offer.request_token} for {swap_offer.offer_amount} {swap_offer.offer_token}.')
                return redirect('my_swap_history')
        except Exception as e:
            messages.error(request, f'Error executing swap: {str(e)}')
            return redirect('available_swap_offers')
    
    context = {
        'swap_offer': swap_offer,
    }
    return render(request, 'defi/accept_swap_offer.html', context)

@login_required
def cancel_swap_offer(request, offer_id):
    """Cancel a P2P swap offer"""
    swap_offer = get_object_or_404(SwapOffer, id=offer_id, initiator=request.user)
    
    if swap_offer.status != 'pending':
        messages.error(request, 'Only pending offers can be cancelled.')
        return redirect('my_swap_offers')
    
    if request.method == 'POST':
        swap_offer.status = 'cancelled'
        swap_offer.save()
        messages.success(request, 'Swap offer cancelled successfully.')
        return redirect('my_swap_offers')
    
    context = {
        'swap_offer': swap_offer,
    }
    return render(request, 'defi/cancel_swap_offer.html', context)

@login_required
def my_swap_offers(request):
    """Display user's created swap offers"""
    offers = SwapOffer.objects.filter(initiator=request.user).order_by('-created_at')
    
    context = {
        'offers': offers,
    }
    return render(request, 'defi/my_swap_offers.html', context)

@login_required
def available_swap_offers(request):
    """Display available swap offers for the user"""
    # Show offers that are:
    # 1. Pending
    # 2. Not expired
    # 3. Either no counterparty or counterparty is current user
    # 4. Not created by current user
    offers = SwapOffer.objects.filter(
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
    """Display user's completed P2P swap history"""
    swaps = P2PSwapTransaction.objects.filter(
        Q(initiator=request.user) | Q(counterparty=request.user)
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
