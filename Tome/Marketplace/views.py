from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import (
    MarketplaceListing, MarketplaceItem, TradingPair, LimitOrder, 
    MarketOrder, StopLossOrder, OrderExecution
)
import uuid

# Create your views here.
@login_required
def marketplace(request):
    """Display all available marketplace listings"""
    listings = MarketplaceListing.objects.all().select_related('item', 'seller').order_by('-listing_date')
    return render(request, 'marketplace/index.html', {'listings': listings})

@login_required
def create_listing(request):
    """Create a new marketplace listing"""
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '').strip()
        price = request.POST.get('price', '').strip()
        quantity = request.POST.get('quantity', '').strip()
        allow_swaps = request.POST.get('allow_swaps') == 'on'
        preferred_swap_token = request.POST.get('preferred_swap_token', '').strip().upper()
        
        # Validate preferred swap token format
        if preferred_swap_token and not preferred_swap_token.isalnum():
            messages.error(request, 'Preferred swap token must be alphanumeric only.')
            return render(request, 'marketplace/create_listing.html', {
                'title': title, 'description': description, 'price': price, 'quantity': quantity
            })
        
        # Validate required fields
        if not title or not description or not price or not quantity:
            messages.error(request, 'All fields are required.')
            return render(request, 'marketplace/create_listing.html', {
                'title': title, 'description': description, 'price': price, 'quantity': quantity
            })
        
        # Validate field lengths
        if len(title) > 200:
            messages.error(request, 'Title must not exceed 200 characters.')
            return render(request, 'marketplace/create_listing.html', {
                'title': title, 'description': description, 'price': price, 'quantity': quantity
            })
        
        # Validate numeric fields
        try:
            price_decimal = Decimal(price)
            quantity_int = int(quantity)
            
            if price_decimal <= 0:
                messages.error(request, 'Price must be greater than 0.')
                return render(request, 'marketplace/create_listing.html', {
                    'title': title, 'description': description, 'price': price, 'quantity': quantity
                })
            
            if quantity_int <= 0:
                messages.error(request, 'Quantity must be greater than 0.')
                return render(request, 'marketplace/create_listing.html', {
                    'title': title, 'description': description, 'price': price, 'quantity': quantity
                })
        except (ValueError, InvalidOperation):
            messages.error(request, 'Invalid price or quantity format.')
            return render(request, 'marketplace/create_listing.html', {
                'title': title, 'description': description, 'price': price, 'quantity': quantity
            })
        
        # Create the marketplace item
        item = MarketplaceItem.objects.create(
            title=title,
            description=description,
            quantity=quantity_int,
            individual_price=price_decimal,
            total_price=price_decimal * quantity_int
        )
        
        # Create the marketplace listing
        MarketplaceListing.objects.create(
            item=item,
            seller=request.user,
            price=price_decimal,
            quantity_available=quantity_int,
            allow_swaps=allow_swaps,
            preferred_swap_token=preferred_swap_token
        )
        
        messages.success(request, 'Listing created successfully!')
        return redirect('marketplace')
    
    return render(request, 'marketplace/create_listing.html')

@login_required
def listing_detail(request, listing_id):
    """Display detailed view of a marketplace listing"""
    listing = get_object_or_404(MarketplaceListing.objects.select_related('item', 'seller'), id=listing_id)
    
    context = {
        'listing': listing,
    }
    return render(request, 'marketplace/listing_detail.html', context)

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
        ).select_related('buyer', 'seller').order_by('-created_at')[:20]
    
    context = {
        'trading_pairs': trading_pairs,
        'selected_pair': selected_pair,
        'buy_orders': buy_orders,
        'sell_orders': sell_orders,
        'recent_trades': recent_trades,
    }
    return render(request, 'marketplace/dex_orderbook.html', context)

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
    return render(request, 'marketplace/my_orders.html', context)

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
