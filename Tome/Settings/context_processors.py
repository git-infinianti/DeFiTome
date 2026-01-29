from .models import UserProfile


def theme_context(request):
    """Add user's theme preference to all template contexts"""
    if request.user.is_authenticated:
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            return {'user_theme': user_profile.theme}
        except UserProfile.DoesNotExist:
            # Create profile with default theme if it doesn't exist
            user_profile = UserProfile.objects.create(user=request.user)
            return {'user_theme': user_profile.theme}
    return {'user_theme': 'default'}
