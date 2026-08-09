from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.core.paginator import Paginator
import json

from .models import (
    SolidityContract,
    ContractInteraction,
    ContractAsset,
    MessageChannelPolicy,
)
from .rpc import evrmore_rpc
from .authentication import api_key_required
from .message_channel_lib import (
    canonical_json,
    validate_channel_key,
    validate_console_payload,
)
from .channel_console_service import (
    create_channel_console_asset_for_user,
    scan_channel_console_assets as scan_channel_console_assets_service,
)
from .rpc_procedure_registry import (
    execute_allowed_rpc_procedure,
    get_rpc_procedure_catalog,
)
from Media.kubo_api import KuboAPIUploader


# ============================================================
# DOCUMENTATION & INFO VIEWS
# ============================================================

def docs(request):
    """API Documentation page with DeFi-related commands"""
    # Check if user is authenticated
    user_has_api_key = False
    if request.user.is_authenticated:
        from .models import APIKey
        user_has_api_key = APIKey.objects.filter(user=request.user, is_active=True).exists()
    
    context = {
        'user_has_api_key': user_has_api_key,
        'rpc_procedure_catalog': get_rpc_procedure_catalog(),
    }
    return render(request, 'api/docs.html', context)


def _rpc_lockdown_response():
    return JsonResponse({
        'success': False,
        'error': 'This API surface is locked down. Use the allowed RPC procedure endpoint instead.',
    }, status=403)


@require_http_methods(["GET"])
def api_info(request):
    """
    Get general API information and available endpoints.
    
    Returns:
        JSON response with API version, available endpoints, and blockchain info
    """
    try:
        blockchain_info = evrmore_rpc.get_blockchain_info()
        
        return JsonResponse({
            'success': True,
            'api_version': '1.0.0',
            'blockchain': {
                'chain': blockchain_info.get('chain', 'unknown'),
                'blocks': blockchain_info.get('blocks', 0),
                'difficulty': blockchain_info.get('difficulty', 0),
            },
            'endpoints': {
                'rpc_procedures': '/api/v1/rpc/procedures/',
                'rpc_execute': '/api/v1/rpc/execute/',
            }
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


# ============================================================
# CONTRACT MANAGEMENT VIEWS
# ============================================================

@csrf_exempt
@require_http_methods(["GET", "POST"])
def contracts_list(request):
    """
    List all contracts or deploy a new contract.
    
    GET: List all deployed contracts
    POST: Deploy a new contract (requires API key)
    """
    return _rpc_lockdown_response()


@csrf_exempt
@api_key_required
@require_http_methods(["POST"])
def contracts_deploy(request):
    """
    Deploy a new contract. Requires API key authentication.
    """
    return _rpc_lockdown_response()



@require_http_methods(["GET"])
def contract_detail(request, contract_id):
    """
    Get details of a specific contract.
    
    Args:
        contract_id: ID of the contract
        
    Returns:
        JSON response with contract details
    """
    return _rpc_lockdown_response()


@csrf_exempt
@api_key_required
@require_http_methods(["POST"])
def contract_interact(request, contract_id):
    """
    Interact with a contract by calling a function. Requires API key authentication.
    
    Args:
        contract_id: ID of the contract
        
    Body:
        function_name: Name of function to call
        parameters: Function parameters (dict)
        
    Returns:
        JSON response with interaction result
    """
    
    return _rpc_lockdown_response()


# ============================================================
# ASSET MANAGEMENT VIEWS
# ============================================================

@require_http_methods(["GET"])
def assets_list(request):
    """
    List all assets on the Evrmore blockchain.
    
    Query params:
        asset: Asset name filter (default: "*" for all)
        verbose: Include detailed info (default: false)
        count: Results per page (default: 50)
        start: Starting index (default: 0)
    """
    try:
        asset = request.GET.get('asset', '*')
        verbose = request.GET.get('verbose', 'false').lower() == 'true'
        count = int(request.GET.get('count', 50))
        start = int(request.GET.get('start', 0))
        
        assets = evrmore_rpc.list_assets(asset, verbose, count, start)
        
        return JsonResponse({
            'success': True,
            'assets': assets,
            'pagination': {
                'count': count,
                'start': start,
            }
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@require_http_methods(["GET"])
def asset_detail(request, asset_name):
    """
    Get detailed information about a specific asset.
    
    Args:
        asset_name: Name of the asset
        
    Returns:
        JSON response with asset data
    """
    try:
        asset_data = evrmore_rpc.get_asset_data(asset_name)
        
        return JsonResponse({
            'success': True,
            'asset': asset_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@csrf_exempt
@api_key_required
@require_http_methods(["POST"])
def asset_issue(request):
    """
    Issue a new asset on the Evrmore blockchain. Requires API key authentication.
    
    Body:
        asset_name: Name of the asset
        qty: Quantity to issue
        to_address: Recipient address (optional)
        change_address: Change address (optional)
        units: Divisibility 0-8 (default: 0)
        reissuable: Can be reissued (default: true)
        has_ipfs: Has IPFS metadata (default: false)
        ipfs_hash: IPFS hash (optional)
    """
    
    return _rpc_lockdown_response()


@csrf_exempt
@api_key_required
@require_http_methods(["POST"])
def nft_mint(request):
    """Mint an NFT by issuing an Evrmore unique asset."""
    return _rpc_lockdown_response()


@csrf_exempt
@api_key_required
@require_http_methods(["POST"])
def asset_transfer(request):
    """
    Transfer an asset to another address. Requires API key authentication.
    
    Body:
        asset_name: Name of the asset
        qty: Quantity to transfer
        to_address: Recipient address
        message: Optional message
        expire_time: Expiration time (default: 0)
        change_address: Change address (optional)
        asset_change_address: Asset change address (optional)
    """
    
    return _rpc_lockdown_response()


# ============================================================
# BLOCKCHAIN QUERY VIEWS
# ============================================================

@require_http_methods(["GET"])
def blockchain_info(request):
    """Get general blockchain information"""
    try:
        info = evrmore_rpc.get_blockchain_info()
        return JsonResponse({
            'success': True,
            'blockchain': info
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@require_http_methods(["GET"])
def block_info(request, block_hash):
    """
    Get information about a specific block.
    
    Args:
        block_hash: Hash of the block
        
    Query params:
        verbosity: Level of detail (default: 1)
    """
    try:
        verbosity = int(request.GET.get('verbosity', 1))
        block = evrmore_rpc.get_block(block_hash, verbosity)
        
        return JsonResponse({
            'success': True,
            'block': block
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@require_http_methods(["GET"])
def address_balance(request, address):
    """
    Get balance for a specific address.
    
    Args:
        address: Evrmore address
    """
    try:
        balance = evrmore_rpc.get_address_balance(address)
        
        return JsonResponse({
            'success': True,
            'address': address,
            'balance': balance
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@require_http_methods(["GET"])
def address_transactions(request, address):
    """
    Get transaction IDs for a specific address.
    
    Args:
        address: Evrmore address
        
    Query params:
        start: Start block height (optional)
        end: End block height (optional)
    """
    try:
        start = request.GET.get('start')
        end = request.GET.get('end')
        
        if start:
            start = int(start)
        if end:
            end = int(end)
        
        txids = evrmore_rpc.get_address_txids([address], start, end)
        
        return JsonResponse({
            'success': True,
            'address': address,
            'transactions': txids
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@require_http_methods(["GET"])
def address_utxos(request, address):
    """
    Get unspent transaction outputs for an address.
    
    Args:
        address: Evrmore address
    """
    try:
        utxos = evrmore_rpc.get_address_utxos([address])
        
        return JsonResponse({
            'success': True,
            'address': address,
            'utxos': utxos
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


# ============================================================
# MESSAGING VIEWS
# ============================================================

@csrf_exempt
@api_key_required
@require_http_methods(["POST"])
def send_message(request):
    """
    Send a message to a channel. Requires API key authentication.
    
    Body:
        channel_name: Name of the channel
        ipfs_hash: IPFS hash of the message
        expire_time: Expiration time (default: 0)
    """
    
    return _rpc_lockdown_response()


@csrf_exempt
@api_key_required
@require_http_methods(["POST"])
def create_channel_console_asset(request):
    """Admin-only channel asset issuance under owned admin assets with IPFS console metadata."""
    return _rpc_lockdown_response()


@require_http_methods(["GET"])
def scan_channel_console_assets(request):
    """Scan blockchain messaging channel assets and validate attached IPFS JSON metadata."""
    return _rpc_lockdown_response()


@require_http_methods(["GET"])
def rpc_procedures(request):
    return JsonResponse({
        'success': True,
        'catalog': get_rpc_procedure_catalog(),
    })


@csrf_exempt
@api_key_required
@require_http_methods(["POST"])
def rpc_execute(request):
    try:
        data = json.loads(request.body)
        procedure = str(data.get('procedure') or '').strip().lower()
        params = data.get('params')
        if params is None:
            params = []
        if not isinstance(params, list):
            return JsonResponse({
                'success': False,
                'error': 'params must be a JSON array.',
            }, status=400)

        result = execute_allowed_rpc_procedure(procedure, params=params)
        return JsonResponse({
            'success': True,
            'procedure': procedure,
            'result': result,
        })
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=403)
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)


@require_http_methods(["GET"])
def view_messages(request):
    return _rpc_lockdown_response()


@require_http_methods(["GET"])
def view_channels(request):
    return _rpc_lockdown_response()

