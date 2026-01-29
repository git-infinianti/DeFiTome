from django.shortcuts import render, redirect
from django.contrib.auth.models import User
from django.contrib.auth import login as auth_login, authenticate, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import IntegrityError
from django.views.decorators.http import require_http_methods
from .models import EmailVerification
from django.utils import timezone
from Settings.views import send_verification_email
from Marketplace.models import MarketplaceListing

# Create your views here.
def register(request):
    # Redirect if user is already logged in
    if request.user.is_authenticated:
        return redirect('home')
    
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        confirm_password = request.POST.get('confirm_password', '')
        
        # Validate required fields
        if not username or not email or not password or not confirm_password:
            messages.error(request, 'All fields are required.')
            return render(request, 'register/index.html')
        
        # Validate passwords match
        if password != confirm_password:
            messages.error(request, 'Passwords do not match.')
            return render(request, 'register/index.html')
        
        # Check if username already exists
        if User.objects.filter(username=username).exists():
            messages.error(request, 'Username already exists.')
            return render(request, 'register/index.html')
        
        # Check if email already exists
        if User.objects.filter(email=email).exists():
            messages.error(request, 'Email already registered.')
            return render(request, 'register/index.html')
        
        # Create user with race condition handling
        try:
            user = User.objects.create_user(username=username, email=email, password=password)
            
            # Send verification email
            try:
                send_verification_email(request, user)
                messages.info(request, 'A verification email has been sent to your email address. Please verify your email.')
            except Exception as e:
                # Log error but don't prevent registration
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f'Failed to send verification email to {user.email}: {str(e)}')
                messages.warning(request, 'Account created but failed to send verification email. You can resend it from settings.')
            
            # Log the user in
            auth_login(request, user)
            
            messages.success(request, 'Registration successful!')
            return redirect('home')
        except IntegrityError:
            messages.error(request, 'Username or email already exists.')
            return render(request, 'register/index.html')
    
    return render(request, 'register/index.html')


def login(request):
    # Redirect if user is already logged in
    if request.user.is_authenticated:
        return redirect('home')
    
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        
        # Validate required fields
        if not username or not password:
            messages.error(request, 'Both username and password are required.')
            return render(request, 'login/index.html')
        
        # Check if username exists (per requirements: redirect to register if user doesn't exist)
        if not User.objects.filter(username=username).exists():
            messages.error(request, 'User does not exist. Please register first.')
            return redirect('register')
        
        # Authenticate the user
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            # Login successful
            auth_login(request, user)
            messages.success(request, 'Login successful!')
            return redirect('home')
        else:
            # Wrong password
            messages.error(request, 'Invalid password. Please try again.')
            return render(request, 'login/index.html')
    
    return render(request, 'login/index.html')


@login_required
def home(request):
    # Get the 3 most recent marketplace listings
    recent_listings = MarketplaceListing.objects.all().select_related('item', 'seller').order_by('-listing_date')[:3]
    return render(request, 'home/index.html', {'listings': recent_listings})


@require_http_methods(["GET", "POST"])
def logout(request):
    if request.user.is_authenticated:
        auth_logout(request)
        messages.success(request, 'You have been successfully logged out.')
    return redirect('login')


def verify_email(request, token):
    """Handle email verification via token"""
    try:
        email_verification = EmailVerification.objects.get(verification_token=token)
        
        if not email_verification.is_verified:
            email_verification.is_verified = True
            email_verification.verified_at = timezone.now()
            email_verification.save()
            messages.success(request, 'Your email has been successfully verified!')
        else:
            messages.info(request, 'Your email is already verified.')
        
        # Redirect to login if not authenticated, otherwise to home
        if request.user.is_authenticated:
            return redirect('home')
        else:
            return redirect('login')
    except EmailVerification.DoesNotExist:
        messages.error(request, 'Invalid verification link.')
        return redirect('login')


