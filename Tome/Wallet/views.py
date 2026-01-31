from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from decimal import Decimal
from .models import UserWallet
from .wallet import Wallet
from .rpc import RPC
from hdwallet.entropies import BIP39Entropy


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
        # Get wallet address
        wallet_instance = Wallet(user_wallet.entropy, user_wallet.passphrase)
        wallet = wallet_instance.get_wallet()
        address = wallet.address()
        
        # Call getaddressbalance RPC command
        balance_data = RPC.getaddressbalance(address)
        
        # Extract balance from response: {"balance": 0, "received": 0}
        if isinstance(balance_data, dict) and 'balance' in balance_data:
            balance = Decimal(str(balance_data['balance']))
            user_wallet.evr_liquidity = balance
            user_wallet.last_balance_update = timezone.now()
            user_wallet.save()
            return balance
        else:
            print(f"Unexpected balance response format: {balance_data}")
            return None
            
    except Exception as e:
        print(f"Error syncing balance for user {user_wallet.user.username}: {str(e)}")
        return None


# Create your views here.
@login_required
def portfolio(request):
    """Display user's wallet portfolio"""
    # Get the user's wallet if it exists using the OneToOne relationship
    user_wallet = getattr(request.user, 'user_wallet', None)
    
    # Create wallet on form submission
    if request.method == 'POST':
        # Create wallet if it doesn't exist
        if not user_wallet:
            # Get wallet name and passphrase from form
            wallet_name = request.POST.get('wallet_name', '').strip()
            passphrase = request.POST.get('passphrase', '').strip()
            
            # Validate wallet name
            if not wallet_name:
                messages.error(request, 'Wallet name is required.')
                return render(request, 'portfolio/index.html', {'user_wallet': user_wallet})
            
            # Validate wallet name length
            if len(wallet_name) > 100:
                messages.error(request, 'Wallet name must be 100 characters or less.')
                return render(request, 'portfolio/index.html', {'user_wallet': user_wallet})
            
            # Start by generating new entropy
            entropy = BIP39Entropy.generate(128)
            
            # Save the new wallet to the database
            user_wallet = UserWallet.objects.create(
                user=request.user,
                name=wallet_name,
                entropy=entropy,
                passphrase=passphrase
            )
            
            messages.success(request, f'Wallet "{wallet_name}" created successfully!')
            return redirect('portfolio')
    
    context = {
        'user_wallet': user_wallet,
    }
    return render(request, 'portfolio/index.html', context)

@login_required
def sync_balance(request):
    """Sync user's EVR balance from the blockchain"""
    user_wallet = getattr(request.user, 'user_wallet', None)
    
    if not user_wallet:
        messages.error(request, 'No wallet found to sync balance.')
        return redirect('portfolio')
    
    try:
        balance = _sync_user_evr_balance(user_wallet)
        if balance is not None:
            messages.success(request, f'Balance synced successfully! Current balance: {balance} EVR')
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
def recieve_funds(request):
    """Display wallet address for receiving funds"""
    user_wallet = getattr(request.user, 'user_wallet', None)
    
    if not user_wallet:
        messages.error(request, 'No wallet found to receive funds.')
        return redirect('portfolio')
    
    # Get wallet address
    wallet_instance = Wallet(user_wallet.entropy, user_wallet.passphrase)
    wallet = wallet_instance.get_wallet()
    address = wallet.address()
    
    context = {
        'address': address,
    }
    return render(request, 'portfolio/receive.html', context)

@login_required
def send_funds(request):
    """Handle sending funds from the user's wallet"""
    user_wallet = getattr(request.user, 'user_wallet', None)
    
    if not user_wallet:
        messages.error(request, 'No wallet found to send funds.')
        return redirect('portfolio')
    
    if request.method == 'POST':
        recipient_address = request.POST.get('recipient_address', '').strip()
        amount = request.POST.get('amount', '').strip()
        
        # Validate inputs
        if not recipient_address or not amount:
            messages.error(request, 'Recipient address and amount are required.')
            return redirect('send_funds')
        
        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
        except ValueError:
            messages.error(request, 'Invalid amount specified.')
            return redirect('send_funds')
        
        # Get wallet instance
        wallet_instance = Wallet(user_wallet.entropy, user_wallet.passphrase)
        wallet = wallet_instance.get_wallet()
        
        # Create and send transaction via RPC
        try:
            txid = RPC.sendtoaddress(recipient_address, amount, wallet)
            messages.success(request, f'Successfully sent {amount} to {recipient_address}. Transaction ID: {txid}')
        except Exception as e:
            messages.error(request, f'Error sending funds: {str(e)}')
        
        return redirect('send_funds')
    
    return render(request, 'portfolio/send.html')
