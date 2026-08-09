from django.shortcuts import render, redirect
from django.contrib.auth.models import User
from django.contrib.auth import login as auth_login, authenticate, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import IntegrityError
from django.db.models import Q
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from .models import EmailVerification, EvrmoreAuthenticationAddress
from .evrmore_auth import (
    EvrmoreAuthenticationUnavailable,
    create_evrmore_challenge,
    normalize_evrmore_address,
    verify_evrmore_signature,
)
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from Settings.views import send_verification_email
from DeFi.models import SwapOffer
from Listings.models import LimitOrder, TradingPair
from Tome.rpc_client import get_current_network_mode


WALLET_LOGIN_ADDRESS_SESSION_KEY = 'evrmore_wallet_login_address'
WALLET_LOGIN_CHALLENGE_SESSION_KEY = 'evrmore_wallet_login_challenge'
WALLET_LINK_ADDRESS_SESSION_KEY = 'evrmore_wallet_link_address'
WALLET_LINK_CHALLENGE_SESSION_KEY = 'evrmore_wallet_link_challenge'


def _requested_next_url(request):
    next_url = request.POST.get('next') or request.GET.get('next')
    if (
        next_url
        and next_url.startswith('/')
        and not next_url.startswith('//')
        and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return next_url
    return None


def _post_login_redirect(request):
    return _requested_next_url(request) or reverse('home')


def _clear_wallet_challenge(request, address_key, challenge_key):
    request.session.pop(address_key, None)
    request.session.pop(challenge_key, None)


def _wallet_authentication_context(request, address=None, challenge=None):
    return {
        'address': address,
        'challenge': challenge,
        'next_url': _requested_next_url(request),
    }


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
            auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            
            messages.success(request, 'Registration successful!')
            return redirect(_post_login_redirect(request))
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
            return redirect(_post_login_redirect(request))
        else:
            # Wrong password
            messages.error(request, 'Invalid password. Please try again.')
            return render(request, 'login/index.html')
    
    return render(request, 'login/index.html', {'next_url': _requested_next_url(request)})


@require_http_methods(["GET", "POST"])
def wallet_login(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'challenge':
            address = normalize_evrmore_address(request.POST.get('address', ''))
            if address is None:
                messages.error(request, 'Enter a valid Evrmore P2PKH address.')
                return render(request, 'wallet_auth/login.html', _wallet_authentication_context(request))

            is_linked = EvrmoreAuthenticationAddress.objects.filter(
                address=address,
                user__is_active=True,
            ).exists()
            if not is_linked:
                messages.error(request, 'This wallet is not linked to an active account.')
                return render(request, 'wallet_auth/login.html', _wallet_authentication_context(request))

            try:
                challenge = create_evrmore_challenge(address)
            except EvrmoreAuthenticationUnavailable:
                messages.error(request, 'Wallet authentication is temporarily unavailable. Please try again later.')
                return render(request, 'wallet_auth/login.html', _wallet_authentication_context(request))

            request.session[WALLET_LOGIN_ADDRESS_SESSION_KEY] = address
            request.session[WALLET_LOGIN_CHALLENGE_SESSION_KEY] = challenge
            return render(
                request,
                'wallet_auth/login.html',
                _wallet_authentication_context(request, address=address, challenge=challenge),
            )

        if action == 'verify':
            address = normalize_evrmore_address(request.POST.get('address', ''))
            challenge = request.POST.get('challenge', '').strip()
            signature = request.POST.get('signature', '')
            expected_address = request.session.get(WALLET_LOGIN_ADDRESS_SESSION_KEY)
            expected_challenge = request.session.get(WALLET_LOGIN_CHALLENGE_SESSION_KEY)

            if address != expected_address or challenge != expected_challenge:
                _clear_wallet_challenge(
                    request,
                    WALLET_LOGIN_ADDRESS_SESSION_KEY,
                    WALLET_LOGIN_CHALLENGE_SESSION_KEY,
                )
                messages.error(request, 'The wallet challenge is invalid or has expired. Request a new one.')
                return render(request, 'wallet_auth/login.html', _wallet_authentication_context(request))

            user = authenticate(
                request,
                evrmore_address=address,
                challenge=challenge,
                signature=signature,
            )
            if user is not None:
                _clear_wallet_challenge(
                    request,
                    WALLET_LOGIN_ADDRESS_SESSION_KEY,
                    WALLET_LOGIN_CHALLENGE_SESSION_KEY,
                )
                auth_login(request, user)
                messages.success(request, 'Wallet authentication successful!')
                return redirect(_post_login_redirect(request))

            messages.error(request, 'The wallet signature could not be verified. Please try again.')
            return render(
                request,
                'wallet_auth/login.html',
                _wallet_authentication_context(request, address=expected_address, challenge=expected_challenge),
            )

        if action == 'cancel':
            _clear_wallet_challenge(
                request,
                WALLET_LOGIN_ADDRESS_SESSION_KEY,
                WALLET_LOGIN_CHALLENGE_SESSION_KEY,
            )
            return render(request, 'wallet_auth/login.html', _wallet_authentication_context(request))

        messages.error(request, 'Invalid wallet authentication request.')

    return render(request, 'wallet_auth/login.html', _wallet_authentication_context(request))


@login_required
@require_http_methods(["GET", "POST"])
def manage_evrmore_wallet_authentication(request):
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'challenge':
            address = normalize_evrmore_address(request.POST.get('address', ''))
            if address is None:
                messages.error(request, 'Enter a valid Evrmore P2PKH address.')
            elif EvrmoreAuthenticationAddress.objects.filter(address=address, user=request.user).exists():
                messages.info(request, 'This wallet is already linked to your account.')
            elif EvrmoreAuthenticationAddress.objects.filter(address=address).exists():
                messages.error(request, 'This wallet is already linked to another account.')
            else:
                try:
                    challenge = create_evrmore_challenge(address)
                except EvrmoreAuthenticationUnavailable:
                    messages.error(request, 'Wallet authentication is temporarily unavailable. Please try again later.')
                else:
                    request.session[WALLET_LINK_ADDRESS_SESSION_KEY] = address
                    request.session[WALLET_LINK_CHALLENGE_SESSION_KEY] = challenge

        elif action == 'verify':
            address = normalize_evrmore_address(request.POST.get('address', ''))
            challenge = request.POST.get('challenge', '').strip()
            signature = request.POST.get('signature', '')
            expected_address = request.session.get(WALLET_LINK_ADDRESS_SESSION_KEY)
            expected_challenge = request.session.get(WALLET_LINK_CHALLENGE_SESSION_KEY)

            if address != expected_address or challenge != expected_challenge:
                _clear_wallet_challenge(
                    request,
                    WALLET_LINK_ADDRESS_SESSION_KEY,
                    WALLET_LINK_CHALLENGE_SESSION_KEY,
                )
                messages.error(request, 'The wallet challenge is invalid or has expired. Request a new one.')
            elif verify_evrmore_signature(address, challenge, signature, request=request):
                linked_address, created = EvrmoreAuthenticationAddress.objects.get_or_create(
                    address=address,
                    defaults={'user': request.user},
                )
                _clear_wallet_challenge(
                    request,
                    WALLET_LINK_ADDRESS_SESSION_KEY,
                    WALLET_LINK_CHALLENGE_SESSION_KEY,
                )
                if linked_address.user_id != request.user.id:
                    messages.error(request, 'This wallet is already linked to another account.')
                elif created:
                    messages.success(request, 'Evrmore wallet linked for sign-in.')
                else:
                    messages.info(request, 'This wallet is already linked to your account.')
            else:
                messages.error(request, 'The wallet signature could not be verified. Please try again.')

        elif action == 'unlink':
            address = normalize_evrmore_address(request.POST.get('address', ''))
            deleted, _ = EvrmoreAuthenticationAddress.objects.filter(
                address=address,
                user=request.user,
            ).delete()
            if deleted:
                messages.success(request, 'Evrmore wallet sign-in removed.')
            else:
                messages.error(request, 'The requested wallet link was not found.')

        elif action == 'cancel':
            _clear_wallet_challenge(
                request,
                WALLET_LINK_ADDRESS_SESSION_KEY,
                WALLET_LINK_CHALLENGE_SESSION_KEY,
            )

        else:
            messages.error(request, 'Invalid wallet authentication request.')

    linked_addresses = request.user.evrmore_authentication_addresses.order_by('address')
    return render(
        request,
        'wallet_auth/manage.html',
        {
            'linked_addresses': linked_addresses,
            'pending_address': request.session.get(WALLET_LINK_ADDRESS_SESSION_KEY),
            'pending_challenge': request.session.get(WALLET_LINK_CHALLENGE_SESSION_KEY),
        },
    )


@login_required
def home(request):
    network_mode = get_current_network_mode()
    available_swaps = SwapOffer.objects.filter(
        network_mode=network_mode,
        status='pending',
        expires_at__gt=timezone.now(),
    ).filter(
        Q(counterparty__isnull=True) | Q(counterparty=request.user),
    ).exclude(initiator=request.user).select_related('initiator').order_by('-created_at')[:4]
    markets = TradingPair.objects.filter(
        network_mode=network_mode,
        is_active=True,
    ).order_by('-volume_24h', 'base_token', 'quote_token')[:6]
    open_order_count = LimitOrder.objects.filter(
        user=request.user,
        trading_pair__network_mode=network_mode,
        status__in=('pending', 'partial'),
    ).count()
    open_offer_count = SwapOffer.objects.filter(
        initiator=request.user,
        network_mode=network_mode,
        status__in=('pending', 'settling'),
    ).count()
    return render(request, 'home/index.html', {
        'available_swaps': available_swaps,
        'markets': markets,
        'open_order_count': open_order_count,
        'open_offer_count': open_offer_count,
        'network_mode': network_mode,
    })


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


