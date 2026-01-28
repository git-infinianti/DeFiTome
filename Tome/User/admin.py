from django.contrib import admin
from .models import UserWallet, EmailVerification

# Register your models here.
admin.site.register(UserWallet)
admin.site.register(EmailVerification)