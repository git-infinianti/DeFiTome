from django.contrib import admin
from .models import TestnetConfig, LiquidityPool, LiquidityPosition, SwapTransaction, SwapOffer, SwapEscrow, P2PSwapTransaction

# Register your models here.
admin.site.register(TestnetConfig)
admin.site.register(LiquidityPool)
admin.site.register(LiquidityPosition)
admin.site.register(SwapTransaction)
admin.site.register(SwapOffer)
admin.site.register(SwapEscrow)
admin.site.register(P2PSwapTransaction)
