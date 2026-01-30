from django.contrib import admin
from .models import (
    TestnetConfig, LiquidityPool, LiquidityPosition, SwapTransaction, 
    SwapOffer, SwapEscrow, P2PSwapTransaction, PriceFeedSource, 
    PriceFeedData, PriceFeedAggregation, CollateralAsset, InterestRateConfig,
    LendingPool, Deposit, Loan, LoanRepayment, Liquidation,
    FixedRateBond, VariableRateSavings, InterestRateSnapshot
)

# Register your models here.
admin.site.register(TestnetConfig)
admin.site.register(LiquidityPool)
admin.site.register(LiquidityPosition)
admin.site.register(SwapTransaction)
admin.site.register(SwapOffer)
admin.site.register(SwapEscrow)
admin.site.register(P2PSwapTransaction)
admin.site.register(PriceFeedSource)
admin.site.register(PriceFeedData)
admin.site.register(PriceFeedAggregation)
admin.site.register(CollateralAsset)
admin.site.register(InterestRateConfig)
admin.site.register(LendingPool)
admin.site.register(Deposit)
admin.site.register(Loan)
admin.site.register(LoanRepayment)
admin.site.register(Liquidation)
admin.site.register(FixedRateBond)
admin.site.register(VariableRateSavings)
admin.site.register(InterestRateSnapshot)
