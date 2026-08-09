from decimal import Decimal

from Tome.rpc_client import get_current_network_mode

from .models import WalletAddress


def wallet_balance(request):
    """Add the last synchronized EVR balance without blocking rendering on RPC."""
    if request.user.is_authenticated:
        user_wallet = getattr(request.user, 'user_wallet', None)
        if user_wallet:
            network_mode = get_current_network_mode()
            address = WalletAddress.objects.filter(
                wallet=user_wallet,
                network_mode=network_mode,
                is_change=False,
            ).order_by('account', 'index').values_list('address', flat=True).first()
            if network_mode == 'mainnet':
                display_balance = Decimal(str(user_wallet.evr_liquidity_mainnet or 0))
                updated_at = user_wallet.last_balance_update_mainnet
            else:
                display_balance = Decimal(str(user_wallet.evr_liquidity_testnet or 0))
                updated_at = user_wallet.last_balance_update_testnet
            return {
                'user_wallet_balance': display_balance,
                'user_wallet_balance_is_live': False,
                'user_wallet_balance_updated_at': updated_at,
                'user_wallet_balance_address': address,
                'has_wallet': True,
            }
    
    return {
        'user_wallet_balance': None,
        'user_wallet_balance_is_live': False,
        'user_wallet_balance_updated_at': None,
        'user_wallet_balance_address': None,
        'has_wallet': False,
    }
