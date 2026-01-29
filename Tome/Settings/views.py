from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.contrib import messages
from django.core.mail import send_mail
from django.conf import settings as django_settings
from django.urls import reverse
from User.models import EmailVerification
from .models import UserProfile


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
        django_settings.DEFAULT_FROM_EMAIL,
        [user.email],
        fail_silently=False,
    )


@login_required
def settings(request):
    # Get or create email verification record
    email_verification, created = EmailVerification.objects.get_or_create(user=request.user)
    
    # Get or create user profile for theme preference
    user_profile, created = UserProfile.objects.get_or_create(user=request.user)
    
    context = {
        'email_verification': email_verification,
        'user_profile': user_profile,
    }
    return render(request, 'settings/index.html', context)


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


@login_required
@require_http_methods(["POST"])
def change_theme(request):
    """Change user's theme preference"""
    theme = request.POST.get('theme', 'default')
    
    # Validate theme choice
    valid_themes = ['default', 'light', 'dark']
    if theme not in valid_themes:
        messages.error(request, 'Invalid theme selection.')
        return redirect('settings')
    
    # Get or create user profile
    user_profile, created = UserProfile.objects.get_or_create(user=request.user)
    user_profile.theme = theme
    user_profile.save()
    
    messages.success(request, f'Theme changed to {dict(UserProfile.THEME_CHOICES)[theme]}.')
    return redirect('settings')
