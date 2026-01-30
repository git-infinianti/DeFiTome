from django.core.management.base import BaseCommand
from decimal import Decimal
from DeFi.models import (
    CollateralAsset, InterestRateConfig, LendingPool
)


class Command(BaseCommand):
    help = 'Initialize lending pools and collateral assets for testing'

    def handle(self, *args, **options):
        self.stdout.write('Setting up lending data...')
        
        # Create collateral assets
        collateral_assets = [
            {
                'token_symbol': 'BTC',
                'name': 'Bitcoin',
                'collateral_factor': Decimal('75.0'),
                'liquidation_threshold': Decimal('80.0'),
                'liquidation_penalty': Decimal('10.0'),
            },
            {
                'token_symbol': 'ETH',
                'name': 'Ethereum',
                'collateral_factor': Decimal('75.0'),
                'liquidation_threshold': Decimal('80.0'),
                'liquidation_penalty': Decimal('10.0'),
            },
            {
                'token_symbol': 'EVR',
                'name': 'Evrmore',
                'collateral_factor': Decimal('70.0'),
                'liquidation_threshold': Decimal('75.0'),
                'liquidation_penalty': Decimal('12.0'),
            },
        ]
        
        for asset_data in collateral_assets:
            asset, created = CollateralAsset.objects.get_or_create(
                token_symbol=asset_data['token_symbol'],
                defaults=asset_data
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f'Created collateral asset: {asset.token_symbol}'))
            else:
                self.stdout.write(f'Collateral asset already exists: {asset.token_symbol}')
        
        # Create interest rate configs
        interest_configs = [
            {
                'token_symbol': 'USDT',
                'base_rate': Decimal('2.0'),
                'optimal_utilization': Decimal('80.0'),
                'slope_1': Decimal('4.0'),
                'slope_2': Decimal('75.0'),
            },
            {
                'token_symbol': 'USDC',
                'base_rate': Decimal('2.0'),
                'optimal_utilization': Decimal('80.0'),
                'slope_1': Decimal('4.0'),
                'slope_2': Decimal('75.0'),
            },
            {
                'token_symbol': 'DAI',
                'base_rate': Decimal('2.5'),
                'optimal_utilization': Decimal('75.0'),
                'slope_1': Decimal('5.0'),
                'slope_2': Decimal('80.0'),
            },
        ]
        
        for config_data in interest_configs:
            config, created = InterestRateConfig.objects.get_or_create(
                token_symbol=config_data['token_symbol'],
                defaults=config_data
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f'Created interest rate config: {config.token_symbol}'))
            else:
                self.stdout.write(f'Interest rate config already exists: {config.token_symbol}')
        
        # Create lending pools
        pools = [
            {
                'token_symbol': 'USDT',
                'name': 'USDT Lending Pool',
                'total_deposits': Decimal('100000.0'),
                'total_borrows': Decimal('50000.0'),
            },
            {
                'token_symbol': 'USDC',
                'name': 'USDC Lending Pool',
                'total_deposits': Decimal('80000.0'),
                'total_borrows': Decimal('40000.0'),
            },
            {
                'token_symbol': 'DAI',
                'name': 'DAI Lending Pool',
                'total_deposits': Decimal('60000.0'),
                'total_borrows': Decimal('30000.0'),
            },
        ]
        
        for pool_data in pools:
            token_symbol = pool_data['token_symbol']
            try:
                config = InterestRateConfig.objects.get(token_symbol=token_symbol)
                pool, created = LendingPool.objects.get_or_create(
                    token_symbol=token_symbol,
                    defaults={
                        'name': pool_data['name'],
                        'total_deposits': pool_data['total_deposits'],
                        'total_borrows': pool_data['total_borrows'],
                        'total_reserves': Decimal('0'),
                        'interest_rate_config': config,
                        'is_active': True,
                    }
                )
                if created:
                    self.stdout.write(self.style.SUCCESS(f'Created lending pool: {pool.token_symbol}'))
                else:
                    self.stdout.write(f'Lending pool already exists: {pool.token_symbol}')
            except InterestRateConfig.DoesNotExist:
                self.stdout.write(self.style.ERROR(f'Interest rate config not found for: {token_symbol}'))
        
        self.stdout.write(self.style.SUCCESS('Lending data setup complete!'))
