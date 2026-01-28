from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from .models import UserWallet

# Create your views here.
@login_required
def portfolio(request):
    """Display user's wallet portfolio"""
    # Get the user's wallet if it exists using the OneToOne relationship
    user_wallet = getattr(request.user, 'user_wallet', None)
    
    context = {
        'user_wallet': user_wallet,
    }
    return render(request, 'portfolio/index.html', context)
