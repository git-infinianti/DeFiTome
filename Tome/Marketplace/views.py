from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from .models import MarketplaceListing

# Create your views here.
@login_required
def marketplace(request):
    """Display all available marketplace listings"""
    listings = MarketplaceListing.objects.all().select_related('item', 'seller').order_by('-listing_date')
    return render(request, 'marketplace/index.html', {'listings': listings})
