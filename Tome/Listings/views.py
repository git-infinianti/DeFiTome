from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.db.models import F, DecimalField, Q, Sum
from django.db.models import ExpressionWrapper
from django.utils import timezone
from .models import (
    Listing, ListingItem, TradingPair, LimitOrder,
    MarketOrder, StopLossOrder, OrderExecution
)
from Explorer.rpc import RPC
import uuid

MARKET_SYNC_ADDRESS = 'EL5MFdaF8msRaUEDu9mxSNniPSswNmNRgq'
MARKET_QUOTE_TOKEN = 'EVR'


def _get_user_token_balance(user, token_symbol):
    """
    Calculate user's balance for a specific token based on order executions.
    
    For buy orders: user receives base_token (credit) when they're the buyer
    For sell orders: user receives quote_token (credit) when they're the seller
    
    Args:
        user: User instance
        token_symbol: Token symbol to check balance for
    
    Returns:
        Decimal: The user's balance for the token
    """
    balance = Decimal('0')
    
    # Get all trading pairs that involve this token
    trading_pairs = TradingPair.objects.filter(
        Q(base_token=token_symbol) | Q(quote_token=token_symbol),
        is_active=True
    )
    
    for pair in trading_pairs:
        if pair.base_token == token_symbol:
            # User receives base_token when they're the buyer
            buy_credits = OrderExecution.objects.filter(
                trading_pair=pair,
                buyer=user
            ).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
            
            # User spends base_token when they're the seller
            sell_debits = OrderExecution.objects.filter(
                trading_pair=pair,
                seller=user
            ).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
            
            balance += buy_credits - sell_debits
        
        if pair.quote_token == token_symbol:
            # User receives quote_token when they're the seller
            sell_credits = OrderExecution.objects.filter(
                trading_pair=pair,
                seller=user
            ).aggregate(total=Sum('quantity') * F('price'), output_field=DecimalField(max_digits=20, decimal_places=8))['total__total'] or Decimal('0')
            
            # User spends quote_token when they're the buyer
            buy_debits = OrderExecution.objects.filter(
                trading_pair=pair,
                buyer=user
            ).aggregate(total=Sum('quantity') * F('price'), output_field=DecimalField(max_digits=20, decimal_places=8))['total__total'] or Decimal('0')
            
            balance += sell_credits - buy_debits
    
    return balance


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
            created_by=None,
            is_active=True,
        )
        
        # Create initial sell orders for synced markets
        _create_initial_sell_orders(trading_pair, address)
        
        created += 1

    return created

# Create your views here.
@login_required
def listings(request):
    """Display all available listings"""
    all_listings = Listing.objects.all().select_related('item', 'seller').order_by('-listing_date')
    return render(request, 'listings/index.html', {'listings': all_listings})

@login_required
def create_listing(request):
    """Create a new listing"""
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '').strip()
        price = request.POST.get('price', '').strip()
        quantity = request.POST.get('quantity', '').strip()
        token_offered = request.POST.get('token_offered', '').strip().upper()
        preferred_token = request.POST.get('preferred_token', '').strip().upper()
        
        # Validate token fields are mandatory and alphanumeric
        if not token_offered or not preferred_token:
            messages.error(request, 'Token offered and preferred token are required.')
            return render(request, 'listings/create_listing.html', {
                'title': title, 'description': description, 'price': price, 'quantity': quantity,
                'token_offered': token_offered, 'preferred_token': preferred_token
            })
        
        if not token_offered.isalnum() or not preferred_token.isalnum():
            messages.error(request, 'Token symbols must be alphanumeric only.')
            return render(request, 'listings/create_listing.html', {
                'title': title, 'description': description, 'price': price, 'quantity': quantity,
                'token_offered': token_offered, 'preferred_token': preferred_token
            })
        
        # Validate required fields
        if not title or not description or not price or not quantity:
            messages.error(request, 'All fields are required.')
            return render(request, 'listings/create_listing.html', {
                'title': title, 'description': description, 'price': price, 'quantity': quantity,
                'token_offered': token_offered, 'preferred_token': preferred_token
            })
        
        # Validate field lengths
        if len(title) > 200:
            messages.error(request, 'Title must not exceed 200 characters.')
            return render(request, 'listings/create_listing.html', {
                'title': title, 'description': description, 'price': price, 'quantity': quantity,
                'token_offered': token_offered, 'preferred_token': preferred_token
            })
        
        if len(token_offered) > 10 or len(preferred_token) > 10:
            messages.error(request, 'Token symbols must not exceed 10 characters.')
            return render(request, 'listings/create_listing.html', {
                'title': title, 'description': description, 'price': price, 'quantity': quantity,
                'token_offered': token_offered, 'preferred_token': preferred_token
            })
        
        # Validate numeric fields
        try:
            price_decimal = Decimal(price)
            quantity_int = int(quantity)
            
            if price_decimal <= 0:
                messages.error(request, 'Price must be greater than 0.')
                return render(request, 'listings/create_listing.html', {
                    'title': title, 'description': description, 'price': price, 'quantity': quantity,
                    'token_offered': token_offered, 'preferred_token': preferred_token
                })
            
            if quantity_int <= 0:
                messages.error(request, 'Quantity must be greater than 0.')
                return render(request, 'listings/create_listing.html', {
                    'title': title, 'description': description, 'price': price, 'quantity': quantity,
                    'token_offered': token_offered, 'preferred_token': preferred_token
                })
        except (ValueError, InvalidOperation):
            messages.error(request, 'Invalid price or quantity format.')
            return render(request, 'listings/create_listing.html', {
                'title': title, 'description': description, 'price': price, 'quantity': quantity,
                'token_offered': token_offered, 'preferred_token': preferred_token
            })
        
        # Create the listing item
        item = ListingItem.objects.create(
            title=title,
            description=description,
            quantity=quantity_int,
            individual_price=price_decimal,
            total_price=price_decimal * quantity_int
        )
        
        # Create the listing
        Listing.objects.create(
            item=item,
            seller=request.user,
            price=price_decimal,
            quantity_available=quantity_int,
            token_offered=token_offered,
            preferred_token=preferred_token
        )
        
        messages.success(request, 'Listing created successfully!')
        return redirect('listings')
    
    return render(request, 'listings/create_listing.html')

@login_required
def listing_detail(request, listing_id):
    """Display detailed view of a listing"""
    listing = get_object_or_404(Listing.objects.select_related('item', 'seller'), id=listing_id)
    
    context = {
        'listing': listing,
    }
    return render(request, 'listings/listing_detail.html', context)

# Order Book DEX Views
@login_required
def dex_orderbook(request):
    """Main DEX order book interface"""
    # Get all active trading pairs
    trading_pairs = TradingPair.objects.filter(is_active=True)
    
    # Get selected pair or default to first
    selected_pair_id = request.GET.get('pair')
    if selected_pair_id:
        try:
            selected_pair = TradingPair.objects.get(id=selected_pair_id, is_active=True)
        except TradingPair.DoesNotExist:
            selected_pair = trading_pairs.first() if trading_pairs.exists() else None
    else:
        selected_pair = trading_pairs.first() if trading_pairs.exists() else None
    
    # Get order book for selected pair
    buy_orders = []
    sell_orders = []
    recent_trades = []
    
    if selected_pair:
        # Get active buy orders (sorted by price descending - highest first)
        buy_orders = LimitOrder.objects.filter(
            trading_pair=selected_pair,
            side='buy',
            status__in=['pending', 'partial']
        ).order_by('-price')[:20]
        
        # Get active sell orders (sorted by price ascending - lowest first)
        sell_orders = LimitOrder.objects.filter(
            trading_pair=selected_pair,
            side='sell',
            status__in=['pending', 'partial']
        ).order_by('price')[:20]
        
        # Get recent trades
        recent_trades = OrderExecution.objects.filter(
            trading_pair=selected_pair
        ).select_related('buyer', 'seller').annotate(
            total_cost=ExpressionWrapper(
                F('price') * F('quantity'),
                output_field=DecimalField(max_digits=20, decimal_places=8)
            )
        ).order_by('-created_at')[:20]
    
    context = {
        'trading_pairs': trading_pairs,
        'selected_pair': selected_pair,
        'buy_orders': buy_orders,
        'sell_orders': sell_orders,
        'recent_trades': recent_trades,
    }
    return render(request, 'listings/dex_orderbook.html', context)

@login_required
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
            return redirect('dex_orderbook')
        
        if side not in ['buy', 'sell']:
            messages.error(request, 'Invalid order side.')
            return redirect('dex_orderbook')
        
        try:
            price_decimal = Decimal(price)
            quantity_decimal = Decimal(quantity)
            
            if price_decimal <= 0 or quantity_decimal <= 0:
                messages.error(request, 'Price and quantity must be greater than zero.')
                return redirect('dex_orderbook')
            
            trading_pair = TradingPair.objects.get(id=pair_id, is_active=True)
            
            # Create limit order
            order = LimitOrder.objects.create(
                user=request.user,
                trading_pair=trading_pair,
                side=side,
                price=price_decimal,
                quantity=quantity_decimal,
                status='pending'
            )
            
            # Try to match the order
            _match_order(order)
            
            messages.success(request, f'Limit {side} order placed successfully!')
            
        except TradingPair.DoesNotExist:
            messages.error(request, 'Trading pair not found.')
        except (ValueError, InvalidOperation):
            messages.error(request, 'Invalid price or quantity format.')
        except Exception as e:
            messages.error(request, f'Error placing order: {str(e)}')
        
        return redirect('dex_orderbook')
    
    return redirect('dex_orderbook')

@login_required
def place_market_order(request):
    """Place a market order for instant execution"""
    if request.method == 'POST':
        pair_id = request.POST.get('pair_id')
        side = request.POST.get('side', '').strip().lower()
        quantity = request.POST.get('quantity', '').strip()
        
        # Validate inputs
        if not all([pair_id, side, quantity]):
            messages.error(request, 'All fields are required.')
            return redirect('dex_orderbook')
        
        if side not in ['buy', 'sell']:
            messages.error(request, 'Invalid order side.')
            return redirect('dex_orderbook')
        
        try:
            quantity_decimal = Decimal(quantity)
            
            if quantity_decimal <= 0:
                messages.error(request, 'Quantity must be greater than zero.')
                return redirect('dex_orderbook')
            
            trading_pair = TradingPair.objects.get(id=pair_id, is_active=True)
            
            # Get opposite side orders for matching
            if side == 'buy':
                # For buy orders, get sell orders sorted by lowest price first, then by creation time (FIFO)
                opposite_orders = LimitOrder.objects.filter(
                    trading_pair=trading_pair,
                    side='sell',
                    status__in=['pending', 'partial']
                ).order_by('price', 'created_at')
            else:
                # For sell orders, get buy orders sorted by highest price first, then by creation time (FIFO)
                opposite_orders = LimitOrder.objects.filter(
                    trading_pair=trading_pair,
                    side='buy',
                    status__in=['pending', 'partial']
                ).order_by('-price', 'created_at')
            
            if not opposite_orders.exists():
                messages.error(request, 'No orders available for immediate execution.')
                return redirect('dex_orderbook')
            
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
                user_balance = _get_user_token_balance(request.user, trading_pair.quote_token)
                if user_balance < total_cost:
                    messages.error(
                        request, 
                        f'Insufficient {trading_pair.quote_token} balance. Required: {total_cost:.8f}, Available: {user_balance:.8f}'
                    )
                    return redirect('dex_orderbook')
            
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
                    
                    OrderExecution.objects.create(
                        trading_pair=trading_pair,
                        buyer=buyer,
                        seller=seller,
                        price=limit_order.price,
                        quantity=fill_qty,
                        buyer_order=buyer_order,
                        seller_order=seller_order,
                        tx_hash=f'testnet-{uuid.uuid4()}'
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
                    avg_price = Decimal('0')
                
                MarketOrder.objects.create(
                    user=request.user,
                    trading_pair=trading_pair,
                    side=side,
                    quantity=filled_qty,
                    executed_price=avg_price,
                    status='executed',
                    tx_hash=f'testnet-{uuid.uuid4()}'
                )
            
            if remaining_qty > 0:
                messages.warning(request, f'Market order partially executed: {filled_qty}/{quantity_decimal} {trading_pair.base_token} @ avg price {avg_price:.8f}')
            else:
                messages.success(request, f'Market {side} order executed successfully! {executed_trades} trades at avg price {avg_price:.8f}')
            
        except TradingPair.DoesNotExist:
            messages.error(request, 'Trading pair not found.')
        except (ValueError, InvalidOperation):
            messages.error(request, 'Invalid quantity format.')
        except Exception as e:
            messages.error(request, f'Error executing market order: {str(e)}')
        
        return redirect('dex_orderbook')
    
    return redirect('dex_orderbook')

@login_required
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
            return redirect('dex_orderbook')
        
        if side not in ['buy', 'sell']:
            messages.error(request, 'Invalid order side.')
            return redirect('dex_orderbook')
        
        try:
            trigger_price_decimal = Decimal(trigger_price)
            quantity_decimal = Decimal(quantity)
            
            if trigger_price_decimal <= 0 or quantity_decimal <= 0:
                messages.error(request, 'Trigger price and quantity must be greater than zero.')
                return redirect('dex_orderbook')
            
            trading_pair = TradingPair.objects.get(id=pair_id, is_active=True)
            
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
        except (ValueError, InvalidOperation):
            messages.error(request, 'Invalid price or quantity format.')
        except Exception as e:
            messages.error(request, f'Error placing stop-loss order: {str(e)}')
        
        return redirect('dex_orderbook')
    
    return redirect('dex_orderbook')

@login_required
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
            
            messages.success(request, 'Order cancelled successfully!')
            
        except LimitOrder.DoesNotExist:
            messages.error(request, 'Order not found.')
        except Exception as e:
            messages.error(request, f'Error cancelling order: {str(e)}')
        
        return redirect('my_orders')
    
    return redirect('my_orders')

@login_required
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
    # Get user's limit orders
    limit_orders = LimitOrder.objects.filter(user=request.user).select_related('trading_pair').order_by('-created_at')
    
    # Get user's market orders
    market_orders = MarketOrder.objects.filter(user=request.user).select_related('trading_pair').order_by('-created_at')
    
    # Get user's stop-loss orders
    stop_loss_orders = StopLossOrder.objects.filter(user=request.user).select_related('trading_pair').order_by('-created_at')
    
    # Get user's trade history
    trade_history = OrderExecution.objects.filter(
        Q(buyer=request.user) | Q(seller=request.user)
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
            ).order_by('price', 'created_at')
        else:
            # Match with buy orders (price descending, then FIFO)
            opposite_orders = LimitOrder.objects.filter(
                trading_pair=order.trading_pair,
                side='buy',
                status__in=['pending', 'partial'],
                price__gte=order.price  # Only match if buy price >= sell price
            ).order_by('-price', 'created_at')
        
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
            
            OrderExecution.objects.create(
                trading_pair=order.trading_pair,
                buyer=buyer,
                seller=seller,
                price=execution_price,
                quantity=fill_qty,
                buyer_order=buyer_order,
                seller_order=seller_order,
                tx_hash=f'testnet-{uuid.uuid4()}'
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
            
            # Note: In a production system, this would create and execute a market order
            # For this implementation, we're marking it as executed with the trigger price
            # A full implementation would need to match against the order book
            stop_order.executed_price = current_price
            stop_order.status = 'executed'
            stop_order.tx_hash = f'testnet-{uuid.uuid4()}'
            stop_order.save()

# Markets Views
@login_required
def markets_view(request):
    """Display all trading pairs/markets like SafeTrade interface"""
    _sync_markets_from_address(MARKET_SYNC_ADDRESS)

    # Get filter from query params
    filter_token = request.GET.get('filter', 'ALL').upper()
    
    # Get all active trading pairs
    markets = TradingPair.objects.filter(is_active=True).select_related('created_by')
    
    # Update 24h stats for all markets (in production, this should be a background task)
    for market in markets:
        market.get_24h_stats()
    
    # Apply filter
    if filter_token != 'ALL':
        markets = markets.filter(Q(base_token=filter_token) | Q(quote_token=filter_token))
    
    # Get unique quote tokens for filter buttons
    all_markets = TradingPair.objects.filter(is_active=True)
    quote_tokens = set()
    for market in all_markets:
        quote_tokens.add(market.quote_token)
        quote_tokens.add(market.base_token)
    quote_tokens = sorted(list(quote_tokens))
    
    # Get user's favorite markets (placeholder - implement favorites later)
    # favorites = request.user.favorite_markets.all() if hasattr(request.user, 'favorite_markets') else []
    
    context = {
        'markets': markets.order_by('-volume_24h'),
        'filter_token': filter_token,
        'quote_tokens': quote_tokens,
        # 'favorites': favorites,
    }
    return render(request, 'listings/markets.html', context)

@login_required
def create_market(request):
    """Allow users to create new trading pairs/markets"""
    if request.method == 'POST':
        base_token = request.POST.get('base_token', '').strip().upper()
        quote_token = request.POST.get('quote_token', '').strip().upper()
        
        # Validate inputs
        if not base_token or not quote_token:
            messages.error(request, 'Both base token and quote token are required.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token
            })
        
        # Validate alphanumeric
        if not base_token.isalnum() or not quote_token.isalnum():
            messages.error(request, 'Token symbols must be alphanumeric only.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token
            })
        
        # Validate they're not the same
        if base_token == quote_token:
            messages.error(request, 'Base token and quote token must be different.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token
            })
        
        # Check if pair already exists
        if TradingPair.objects.filter(base_token=base_token, quote_token=quote_token).exists():
            messages.error(request, f'Trading pair {base_token}/{quote_token} already exists.')
            return render(request, 'listings/create_market.html', {
                'base_token': base_token,
                'quote_token': quote_token
            })
        
        # Check if reverse pair exists
        if TradingPair.objects.filter(base_token=quote_token, quote_token=base_token).exists():
            messages.warning(request, f'Reverse pair {quote_token}/{base_token} already exists. Consider using that instead.')
        
        # Create the trading pair
        trading_pair = TradingPair.objects.create(
            base_token=base_token,
            quote_token=quote_token,
            created_by=request.user,
            is_active=True
        )
        
        messages.success(request, f'Market {base_token}/{quote_token} created successfully!')
        return redirect('markets')
    
    return render(request, 'listings/create_market.html')
