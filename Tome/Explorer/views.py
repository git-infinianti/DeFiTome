from django.shortcuts import render, redirect
from django.core.cache import cache
from django.http import Http404, JsonResponse
from Tome.rpc_client import (
    RPC,
    clear_active_network_mode,
    clear_active_rpc_endpoint_mode,
    get_active_rpc_endpoint_mode,
    get_current_network_mode,
    set_active_network_mode,
    set_active_rpc_endpoint_mode,
)
from concurrent.futures import ThreadPoolExecutor
import datetime
import time
from decimal import Decimal, InvalidOperation


def _format_amount(value, places=8):
    """Format numeric amounts while preserving fixed precision for chain values."""
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None

    quantum = Decimal('1').scaleb(-int(places))
    return format(decimal_value.quantize(quantum), f'.{int(places)}f')


def _extract_output_addresses(script_pub_key):
    if not isinstance(script_pub_key, dict):
        return []

    addresses = []
    raw_addresses = script_pub_key.get('addresses')
    if isinstance(raw_addresses, list):
        for address in raw_addresses:
            normalized = str(address or '').strip()
            if normalized:
                addresses.append(normalized)
    elif isinstance(raw_addresses, str):
        normalized = raw_addresses.strip()
        if normalized:
            addresses.append(normalized)

    if not addresses:
        for key in ('address', 'destination'):
            candidate = script_pub_key.get(key)
            normalized = str(candidate or '').strip()
            if normalized:
                addresses.append(normalized)

    deduplicated = []
    seen = set()
    for address in addresses:
        if address in seen:
            continue
        seen.add(address)
        deduplicated.append(address)
    return deduplicated


def _normalize_transaction_outputs(vouts):
    normalized_outputs = []

    for index, vout in enumerate(vouts or []):
        if not isinstance(vout, dict):
            continue

        script_pub_key = vout.get('scriptPubKey') if isinstance(vout.get('scriptPubKey'), dict) else {}
        addresses = _extract_output_addresses(script_pub_key)

        asset_data = script_pub_key.get('asset') if isinstance(script_pub_key.get('asset'), dict) else {}
        asset_name = str(asset_data.get('name') or asset_data.get('asset_name') or '').strip() or None
        asset_amount_display = None
        if asset_name:
            asset_amount_display = _format_amount(asset_data.get('amount'))

        message = asset_data.get('message')
        asset_message = str(message).strip() if message is not None else ''

        evr_value_display = _format_amount(vout.get('value'))

        normalized_outputs.append({
            'index': vout.get('n', index),
            'evr_value_display': evr_value_display,
            'output_type': script_pub_key.get('type') or 'unknown',
            'script_hex': script_pub_key.get('hex') or '',
            'addresses': addresses,
            'primary_address': addresses[0] if addresses else None,
            'asset_name': asset_name,
            'asset_amount_display': asset_amount_display,
            'asset_message': asset_message,
        })

    return normalized_outputs


def _summarize_transaction_outputs(vout_display):
    """Build a compact summary for transaction outputs."""
    evr_total = Decimal('0')
    asset_output_count = 0
    asset_names = set()

    for output in vout_display or []:
        evr_value = output.get('evr_value_display')
        if evr_value is not None:
            try:
                evr_total += Decimal(str(evr_value))
            except (InvalidOperation, TypeError, ValueError):
                pass

        asset_name = output.get('asset_name')
        if asset_name:
            asset_output_count += 1
            asset_names.add(asset_name)

    return {
        'output_count': len(vout_display or []),
        'evr_total_display': format(evr_total.quantize(Decimal('0.00000001')), '.8f'),
        'asset_output_count': asset_output_count,
        'asset_name_count': len(asset_names),
    }


def _rpc_call(method_name, *args):
    """Dispatch a direct Evrmore RPC method call.

    Uses direct method names so routed clients call chain RPC methods, not helper wrappers.
    """
    method = getattr(RPC, str(method_name), None)
    if callable(method):
        return method(*args)

    execute_sync = getattr(RPC, 'execute_command_sync', None)
    if callable(execute_sync):
        return execute_sync(method_name, *args)

    raise AttributeError(f'RPC method {method_name} is unavailable')


def _routed_rpc_call(network_mode, endpoint_mode, method_name, *args):
    set_active_network_mode(network_mode)
    set_active_rpc_endpoint_mode(endpoint_mode)
    try:
        return _rpc_call(method_name, *args)
    finally:
        clear_active_network_mode()
        clear_active_rpc_endpoint_mode()


def _demo_explorer_data(page, network_mode, blocks_per_page=10):
    block_count = 2847563
    current_time = int(time.time())
    base_height = block_count - ((page - 1) * blocks_per_page)
    blocks = [
        {
            'height': base_height - index,
            'hash': f'{"a1b2c3d4e5f6g7h8i9j0" * 3}'[:64],
            'time': current_time - (index * 60),
            'tx_count': 15 + (index * 2),
            'size': 125000 + (index * 1000),
            'difficulty': 15432.8976543,
            'confirmations': index + 1,
        }
        for index in range(blocks_per_page)
        if base_height - index >= 0
    ]
    return {
        'blocks': blocks,
        'error_message': None,
        'page': page,
        'has_next': True,
        'has_prev': page > 1,
        'network_stats': {
            'block_height': block_count,
            'difficulty': 15432.8976543,
            'hashrate': 1234567890.12,
            'chain': 'main' if network_mode == 'mainnet' else 'test',
            'blocks': block_count,
            'bestblockhash': 'a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2',
        },
        'total_blocks': block_count,
        'is_live': False,
    }


def _load_live_explorer_data(page, network_mode, endpoint_mode, blocks_per_page=10):
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix='explorer-rpc') as executor:
        block_count_future = executor.submit(
            _routed_rpc_call, network_mode, endpoint_mode, 'getblockcount'
        )
        blockchain_future = executor.submit(
            _routed_rpc_call, network_mode, endpoint_mode, 'getblockchaininfo'
        )
        mining_future = executor.submit(
            _routed_rpc_call, network_mode, endpoint_mode, 'getmininginfo'
        )
        block_count = int(block_count_future.result())
        blockchain_info = blockchain_future.result()
        mining_info = mining_future.result()

        max_page = (block_count // blocks_per_page) + 1
        page = min(page, max_page)
        start_offset = (page - 1) * blocks_per_page
        heights = [
            block_count - start_offset - index
            for index in range(blocks_per_page)
            if block_count - start_offset - index >= 0
        ]

        hash_futures = {
            height: executor.submit(
                _routed_rpc_call, network_mode, endpoint_mode, 'getblockhash', height
            )
            for height in heights
        }
        hashes = {height: future.result() for height, future in hash_futures.items()}
        block_futures = {
            height: executor.submit(
                _routed_rpc_call, network_mode, endpoint_mode, 'getblock', block_hash
            )
            for height, block_hash in hashes.items()
        }
        block_data = {height: future.result() for height, future in block_futures.items()}

    blocks = [{
        'height': height,
        'hash': hashes[height],
        'time': block_data[height].get('time'),
        'tx_count': len(block_data[height].get('tx', [])),
        'size': block_data[height].get('size'),
        'difficulty': block_data[height].get('difficulty'),
        'confirmations': block_data[height].get('confirmations', 0),
    } for height in heights]
    return {
        'blocks': blocks,
        'error_message': None,
        'page': page,
        'has_next': (block_count - start_offset - blocks_per_page) >= 0,
        'has_prev': page > 1,
        'network_stats': {
            'block_height': block_count,
            'difficulty': mining_info.get('difficulty', 0),
            'hashrate': mining_info.get('networkhashps', 0),
            'chain': blockchain_info.get('chain', 'unknown'),
            'blocks': blockchain_info.get('blocks', 0),
            'bestblockhash': blockchain_info.get('bestblockhash', ''),
        },
        'total_blocks': block_count,
        'is_live': True,
    }

def explorer(request):
    """Render cached explorer data immediately and refresh live telemetry separately."""
    blocks_per_page = 10
    selected_network_mode = get_current_network_mode()
    
    # Handle search
    search_query = request.GET.get('search', '').strip()
    if search_query:
        return handle_search(request, search_query)
    
    # Get the page number from query parameter (default to 1)
    try:
        page = int(request.GET.get('page', 1))
        if page < 1:
            page = 1
    except ValueError:
        page = 1
    
    cache_key = f'explorer:{selected_network_mode}:{page}'
    if request.GET.get('refresh') == '1':
        try:
            context = _load_live_explorer_data(
                page,
                selected_network_mode,
                get_active_rpc_endpoint_mode(),
                blocks_per_page=blocks_per_page,
            )
            cache.set(cache_key, context, timeout=30)
            return JsonResponse({'success': True, 'is_live': True})
        except Exception as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=503)

    context = cache.get(cache_key) or _demo_explorer_data(
        page,
        selected_network_mode,
        blocks_per_page=blocks_per_page,
    )
    context['refresh_url'] = f'?page={page}&refresh=1'
    return render(request, 'explorer/index.html', context)


def handle_search(request, query):
    """Handle search for blocks, transactions, or addresses"""
    try:
        # Try as block height (integer)
        if query.isdigit():
            block_height = int(query)
            return redirect('block_detail', height=block_height)
        
        # Try as block hash or transaction hash (64 character hex)
        if len(query) == 64 and all(c in '0123456789abcdefABCDEF' for c in query):
            # Try block hash first
            try:
                block = _rpc_call('getblock', query)
                return redirect('block_detail', height=block.get('height'))
            except Exception:
                # Not a valid block hash, try transaction
                pass
            
            # Try transaction hash
            try:
                tx = _rpc_call('getrawtransaction', query, True)
                return redirect('transaction_detail', txid=query)
            except Exception:
                # Not a valid transaction hash
                pass
        
        # If nothing found, return to explorer with error
        return redirect('explorer')
        
    except Exception as e:
        return redirect('explorer')


def block_detail(request, height):
    """Display detailed information about a specific block"""
    error_message = None
    block_data = None
    transactions = []
    
    try:
        # Get block hash for this height
        block_hash = _rpc_call('getblockhash', int(height))
        
        # Get block details with full transaction data
        block = _rpc_call('getblock', block_hash, 2)
        
        block_data = {
            'height': block.get('height'),
            'hash': block.get('hash'),
            'confirmations': block.get('confirmations'),
            'size': block.get('size'),
            'version': block.get('version'),
            'merkleroot': block.get('merkleroot'),
            'time': block.get('time'),
            'nonce': block.get('nonce'),
            'bits': block.get('bits'),
            'difficulty': block.get('difficulty'),
            'previousblockhash': block.get('previousblockhash'),
            'nextblockhash': block.get('nextblockhash'),
            'tx_count': len(block.get('tx', [])),
        }
        
        # Get transaction details
        for tx in block.get('tx', [])[:20]:  # Limit to first 20 transactions
            if isinstance(tx, dict):
                transactions.append({
                    'txid': tx.get('txid'),
                    'size': tx.get('size'),
                    'vout_count': len(tx.get('vout', [])),
                    'vin_count': len(tx.get('vin', [])),
                })
            else:
                # If tx is just a string (txid), fetch details
                transactions.append({
                    'txid': tx,
                })
        
    except Exception as e:
        # Demo mode for block details
        demo_mode = request.GET.get('demo', 'true') == 'true'
        
        if demo_mode and not error_message:
            block_data = {
                'height': int(height),
                'hash': 'a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2',
                'confirmations': 145,
                'size': 125487,
                'version': 4,
                'merkleroot': 'b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2a1',
                'time': int(time.time()) - 3600,
                'nonce': 2847563421,
                'bits': '1a0a8b5f',
                'difficulty': 15432.8976543,
                'previousblockhash': 'c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2a1b2' if int(height) > 0 else None,
                'nextblockhash': 'd4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2a1b2c3',
                'tx_count': 25,
            }
            
            # Generate mock transactions
            for i in range(20):
                transactions.append({
                    'txid': f'{i}1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f',
                    'size': 250 + (i * 10),
                    'vout_count': 2,
                    'vin_count': 1,
                })
            error_message = None
        else:
            error_message = f"Error fetching block details: {str(e)}"
    
    context = {
        'block': block_data,
        'transactions': transactions,
        'error_message': error_message,
    }
    return render(request, 'explorer/block_detail.html', context)


def transaction_detail(request, txid):
    """Display detailed information about a specific transaction"""
    error_message = None
    tx_data = None
    
    try:
        # Get transaction details
        tx = _rpc_call('getrawtransaction', txid, True)
        
        tx_data = {
            'txid': tx.get('txid'),
            'hash': tx.get('hash'),
            'size': tx.get('size'),
            'version': tx.get('version'),
            'locktime': tx.get('locktime'),
            'blockhash': tx.get('blockhash'),
            'confirmations': tx.get('confirmations', 0),
            'time': tx.get('time'),
            'blocktime': tx.get('blocktime'),
            'vin': tx.get('vin', []),
            'vout': tx.get('vout', []),
        }
        tx_data['vout_display'] = _normalize_transaction_outputs(tx_data['vout'])
        tx_data['output_summary'] = _summarize_transaction_outputs(tx_data['vout_display'])
        
        # Get block height if transaction is in a block
        if tx_data['blockhash']:
            try:
                block = _rpc_call('getblock', tx_data['blockhash'])
                tx_data['block_height'] = block.get('height')
            except:
                tx_data['block_height'] = None
        
    except Exception as e:
        # Demo mode for transaction details
        demo_mode = request.GET.get('demo', 'true') == 'true'
        
        if demo_mode and not error_message:
            tx_data = {
                'txid': txid,
                'hash': txid,
                'size': 250,
                'version': 2,
                'locktime': 0,
                'blockhash': 'a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2',
                'confirmations': 145,
                'time': int(time.time()) - 3600,
                'blocktime': int(time.time()) - 3600,
                'block_height': 2847500,
                'vin': [
                    {
                        'txid': 'prev123abc456def789',
                        'vout': 0,
                        'scriptSig': {'hex': '483045022100abcd...'},
                        'sequence': 4294967295,
                    }
                ],
                'vout': [
                    {
                        'n': 0,
                        'value': 50.0,
                        'scriptPubKey': {
                            'hex': '76a914abc123def456...88ac',
                            'addresses': ['ELSomeAddress123456789ABCDEFGH'],
                            'type': 'pubkeyhash',
                        }
                    },
                    {
                        'n': 1,
                        'value': 25.5,
                        'scriptPubKey': {
                            'hex': '76a914def456abc123...88ac',
                            'addresses': ['ELAnotherAddr987654321ZYXWVU'],
                            'type': 'pubkeyhash',
                        }
                    }
                ],
            }
            tx_data['vout_display'] = _normalize_transaction_outputs(tx_data['vout'])
            tx_data['output_summary'] = _summarize_transaction_outputs(tx_data['vout_display'])
            error_message = None
        else:
            error_message = f"Error fetching transaction details: {str(e)}"
    
    context = {
        'transaction': tx_data,
        'error_message': error_message,
    }
    return render(request, 'explorer/transaction_detail.html', context)
