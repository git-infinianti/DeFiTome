from django.shortcuts import render, redirect
from django.contrib.auth.models import User
from django.contrib.auth import login as auth_login, authenticate, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import IntegrityError
from django.views.decorators.http import require_http_methods
from django.core.mail import send_mail
from django.conf import settings
from django.urls import reverse
from .models import EmailVerification
from django.utils import timezone

# Helper function to send verification email
def send_verification_email(request, user):
    """Send email verification link to the user
    
    Note: This function uses get_or_create which means the verification token 
    remains the same across multiple calls for the same user. The token is only 
    created once when the EmailVerification record is first created.
    """
    email_verification, created = EmailVerification.objects.get_or_create(user=user)
    
    # Generate verification link
    verification_url = request.build_absolute_uri(
        reverse('verify_email', kwargs={'token': email_verification.verification_token})
    )
    
    # Send email
    subject = 'Verify Your Email - DeFi Tome'
    message = f"""
    Hello {user.username},
    
    Thank you for registering with DeFi Tome!
    
    Please verify your email address by clicking the link below:
    {verification_url}
    
    If you did not create an account, please ignore this email.
    
    Best regards,
    The DeFi Tome Team
    """
    
    send_mail(
        subject,
        message,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
        fail_silently=False,
    )

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
    return render(request, 'home/index.html')


@require_http_methods(["GET", "POST"])
def logout(request):
    if request.user.is_authenticated:
        auth_logout(request)
        messages.success(request, 'You have been successfully logged out.')
    return redirect('login')


@login_required
def settings(request):
    # Get or create email verification record
    email_verification, created = EmailVerification.objects.get_or_create(user=request.user)
    
    context = {
        'email_verification': email_verification,
    }
    return render(request, 'settings/index.html', context)


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


@login_required
@require_http_methods(["POST"])
def resend_verification_email(request):
    """Resend email verification link"""
    email_verification, created = EmailVerification.objects.get_or_create(user=request.user)
    
    if email_verification.is_verified:
        messages.info(request, 'Your email is already verified.')
    else:
        try:
            send_verification_email(request, request.user)
            messages.success(request, 'Verification email has been resent. Please check your inbox.')
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f'Failed to send verification email to {request.user.email}: {str(e)}')
            messages.error(request, f'Failed to send verification email. Please try again later.')
    
    return redirect('settings')