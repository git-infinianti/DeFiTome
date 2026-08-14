import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from hdwallet.entropies import BIP39Entropy

from DeFi.models import SwapOffer
from Listings.models import BalanceLock, LimitOrder, Listing, ListingItem, TradingPair
from Listings.views import _sync_order_balance_lock
from Tome.rpc_client import (
    RPC,
    clear_active_network_mode,
    clear_active_rpc_endpoint_mode,
    set_active_network_mode,
    set_active_rpc_endpoint_mode,
)
from Wallet.models import (
    TrackedAsset,
    TrackedAssetHolding,
    UserWallet,
    WalletAddress,
    WalletPreferences,
    WalletProfile,
)
from Wallet.rpc import (
    _select_asset_inputs,
    _select_evr_inputs,
    _satoshis_to_evr,
    _to_satoshis,
    create_raw_transaction,
    sign_and_broadcast_raw_transaction,
)
from Wallet.wallet import Wallet


NETWORK_MODE = 'testnet'
ASSET_NAME = 'SYSTEM0808'
SIMULATION_PASSWORD = 'SimTestnet!0808'
EVR_PER_USER = Decimal('5')
ASSET_PER_USER = Decimal('20')
FUNDING_FEE_EVR = Decimal('0.02')


class Command(BaseCommand):
    help = 'Create and fund testnet simulation users with resting market activity.'

    def handle(self, *args, **options):
        set_active_network_mode(NETWORK_MODE)
        set_active_rpc_endpoint_mode('local')
        try:
            system_user = User.objects.get(username='system')
            system_wallet = Wallet(
                system_user.user_wallet.entropy,
                system_user.user_wallet.passphrase,
                network_mode=NETWORK_MODE,
            )
            system_address = system_wallet.get_address(0)
            chain_info = RPC.getblockchaininfo()
            if not isinstance(chain_info, dict) or chain_info.get('chain') != 'test':
                raise CommandError('Refusing to seed activity because RPC is not connected to testnet.')
            TrackedAsset.objects.update_or_create(
                symbol=ASSET_NAME,
                network_mode=NETWORK_MODE,
                defaults={
                    'asset_type': TrackedAsset.ASSET_TYPE_MAIN,
                    'total_quantity': Decimal('1000'),
                    'units': 2,
                    'is_reissuable': True,
                },
            )
            simulation_users = [self._ensure_simulation_user(index) for index in range(1, 11)]
            funding_txid = self._fund_users(system_wallet, system_address, simulation_users)
            market_report = self._seed_market_records(system_user, simulation_users)
        finally:
            clear_active_network_mode()
            clear_active_rpc_endpoint_mode()

        report = {
            'network': NETWORK_MODE,
            'asset': ASSET_NAME,
            'funding_txid': funding_txid,
            'credentials': {
                'usernames': [user.username for user, _address in simulation_users],
                'password': SIMULATION_PASSWORD,
            },
            **market_report,
        }
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))

    def _ensure_simulation_user(self, index):
        username = f'simuser{index:02d}'
        user, _created = User.objects.get_or_create(
            username=username,
            defaults={
                'email': f'{username}@defitome.test',
                'is_staff': False,
                'is_superuser': False,
            },
        )
        user.is_active = True
        user.is_staff = False
        user.is_superuser = False
        user.set_password(SIMULATION_PASSWORD)
        user.save(update_fields=['password', 'is_active', 'is_staff', 'is_superuser'])

        user_wallet, _created = UserWallet.objects.get_or_create(
            user=user,
            defaults={
                'name': f'{username} Testnet Wallet',
                'entropy': BIP39Entropy.generate(128),
                'passphrase': '',
            },
        )
        wallet = Wallet(user_wallet.entropy, user_wallet.passphrase, network_mode=NETWORK_MODE)
        address_record, _created = WalletAddress.objects.get_or_create(
            wallet=user_wallet,
            network_mode=NETWORK_MODE,
            account=0,
            index=0,
            is_change=False,
            defaults={
                'address': wallet.get_address(0),
                'wif': wallet.get_wif(0),
            },
        )
        WalletProfile.objects.get_or_create(
            wallet=user_wallet,
            address=address_record,
            network_mode=NETWORK_MODE,
            defaults={'name': 'Main', 'is_main': True},
        )
        WalletPreferences.objects.get_or_create(wallet=user_wallet)
        return user, address_record.address

    def _fund_users(self, system_wallet, system_address, simulation_users):
        funding_requirements = []
        for user, address in simulation_users:
            evr_response = RPC.getaddressbalance(address) or {}
            evr_balance = _satoshis_to_evr(int(evr_response.get('balance', 0)))
            asset_balances = RPC.listassetbalancesbyaddress(address) or {}
            asset_balance = Decimal(str(asset_balances.get(ASSET_NAME, 0)))
            funding_requirements.append({
                'user': user,
                'address': address,
                'evr': max(Decimal('0'), EVR_PER_USER - evr_balance),
                'asset': max(Decimal('0'), ASSET_PER_USER - asset_balance),
            })

        total_evr = sum((item['evr'] for item in funding_requirements), Decimal('0'))
        total_asset = sum((item['asset'] for item in funding_requirements), Decimal('0'))
        funding_txid = None
        if total_evr > 0 or total_asset > 0:
            asset_inputs = []
            selected_asset_quantity = Decimal('0')
            if total_asset > 0:
                asset_inputs, selected_asset_quantity, _coin_satoshis = _select_asset_inputs(
                    system_address,
                    ASSET_NAME,
                    total_asset,
                )

            evr_required_satoshis = _to_satoshis(total_evr + FUNDING_FEE_EVR)
            evr_inputs, selected_evr_satoshis = _select_evr_inputs(
                system_address,
                evr_required_satoshis,
            )
            outputs = []
            for item in funding_requirements:
                if item['evr'] > 0:
                    outputs.append({item['address']: float(item['evr'])})
                if item['asset'] > 0:
                    outputs.append({
                        item['address']: {
                            'transfer': {ASSET_NAME: float(item['asset'])},
                        },
                    })

            asset_change = selected_asset_quantity - total_asset
            if asset_change > 0:
                outputs.append({
                    system_address: {'transfer': {ASSET_NAME: float(asset_change)}},
                })
            evr_change_satoshis = selected_evr_satoshis - evr_required_satoshis
            if evr_change_satoshis > 0:
                outputs.append({system_address: float(_satoshis_to_evr(evr_change_satoshis))})

            raw_tx = create_raw_transaction([*asset_inputs, *evr_inputs], outputs)
            funding_txid = sign_and_broadcast_raw_transaction(
                raw_tx,
                wif_keys=[system_wallet.get_wif(0)],
            )

        tracked_asset = TrackedAsset.objects.get(symbol=ASSET_NAME, network_mode=NETWORK_MODE)
        now = timezone.now()
        for item in funding_requirements:
            user_wallet = item['user'].user_wallet
            user_wallet.evr_liquidity_testnet = EVR_PER_USER
            user_wallet.last_balance_update_testnet = now
            user_wallet.save(update_fields=['evr_liquidity_testnet', 'last_balance_update_testnet'])
            TrackedAssetHolding.objects.update_or_create(
                asset=tracked_asset,
                user=item['user'],
                defaults={'quantity': ASSET_PER_USER},
            )
        return funding_txid

    def _seed_market_records(self, system_user, simulation_users):
        with transaction.atomic():
            tracked_asset, _created = TrackedAsset.objects.update_or_create(
                symbol=ASSET_NAME,
                network_mode=NETWORK_MODE,
                defaults={
                    'asset_type': TrackedAsset.ASSET_TYPE_MAIN,
                    'total_quantity': Decimal('1000'),
                    'units': 2,
                    'is_reissuable': True,
                },
            )
            TrackedAssetHolding.objects.update_or_create(
                asset=tracked_asset,
                user=system_user,
                defaults={'quantity': Decimal('800')},
            )
            pair, _created = TradingPair.objects.get_or_create(
                base_token=ASSET_NAME,
                quote_token='EVR',
                network_mode=NETWORK_MODE,
                defaults={'created_by': system_user, 'is_active': True},
            )
            pair.created_by = system_user
            pair.is_active = True
            pair.save(update_fields=['created_by', 'is_active', 'instrument_type', 'pair_key'])

            BalanceLock.objects.filter(
                user__username__startswith='simuser',
                limit_order__trading_pair=pair,
                status='locked',
            ).delete()
            LimitOrder.objects.filter(
                user__username__startswith='simuser',
                trading_pair=pair,
                status__in=['pending', 'partial'],
            ).delete()

            orders = []
            for index, (user, _address) in enumerate(simulation_users, start=1):
                order_specs = (
                    ('buy', Decimal('0.10') + Decimal(index - 1) * Decimal('0.01'), Decimal('2')),
                    ('buy', Decimal('0.20') + Decimal(index - 1) * Decimal('0.01'), Decimal('1')),
                    ('sell', Decimal('1.00') + Decimal(index - 1) * Decimal('0.10'), Decimal('2')),
                    ('sell', Decimal('2.00') + Decimal(index - 1) * Decimal('0.10'), Decimal('1')),
                )
                for side, price, quantity in order_specs:
                    order = LimitOrder.objects.create(
                        user=user,
                        trading_pair=pair,
                        side=side,
                        price=price,
                        quantity=quantity,
                        status='pending',
                    )
                    _sync_order_balance_lock(order)
                    orders.append(order)

            item, _created = ListingItem.objects.get_or_create(
                title='SYSTEM0808 Testnet Fungible Lot',
                defaults={
                    'description': 'System-owned testnet fungible inventory for raw atomic settlement.',
                    'quantity': Decimal('5'),
                    'individual_price': Decimal('2'),
                    'total_price': Decimal('2'),
                    'is_nft': False,
                },
            )
            listing, _created = Listing.objects.update_or_create(
                item=item,
                seller=system_user,
                network_mode=NETWORK_MODE,
                defaults={
                    'price': Decimal('2'),
                    'quantity_available': Decimal('5'),
                    'token_offered': ASSET_NAME,
                    'preferred_token': 'EVR',
                },
            )
            expires_at = timezone.now() + timedelta(days=30)
            system_offer, _created = SwapOffer.objects.update_or_create(
                escrow_id='seed-system0808-listing-evr',
                defaults={
                    'initiator': system_user,
                    'counterparty': None,
                    'listing': listing,
                    'offer_token': ASSET_NAME,
                    'offer_amount': Decimal('5'),
                    'request_token': 'EVR',
                    'request_amount': Decimal('2'),
                    'network_mode': NETWORK_MODE,
                    'status': 'pending',
                    'expires_at': expires_at,
                },
            )
            fake_offers = []
            for index, (user, _address) in enumerate(simulation_users[:5], start=1):
                offer, _created = SwapOffer.objects.update_or_create(
                    escrow_id=f'seed-simuser{index:02d}-system0808-evr',
                    defaults={
                        'initiator': user,
                        'counterparty': None,
                        'listing': None,
                        'offer_token': ASSET_NAME,
                        'offer_amount': Decimal('1'),
                        'request_token': 'EVR',
                        'request_amount': Decimal(index) / Decimal('4'),
                        'network_mode': NETWORK_MODE,
                        'status': 'pending',
                        'expires_at': expires_at,
                    },
                )
                fake_offers.append(offer.id)

        return {
            'pair_id': pair.id,
            'resting_order_count': len(orders),
            'listing_id': listing.id,
            'system_swap_id': system_offer.id,
            'simulation_swap_ids': fake_offers,
        }