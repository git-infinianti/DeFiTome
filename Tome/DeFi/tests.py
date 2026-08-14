from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from DeFi.cleanup import purge_expired_swap_offers
from Wallet.models import TrackedAsset
from Wallet.models import UserWallet, WalletAddress
from API.channel_event_protocol import add_payload_checksum
from API.models import (
    AtomicSwapTransferMessage,
    ChannelConsumer,
    ChannelEvent,
    ChannelEventApplication,
    ChannelReconciliationIssue,
    ChannelSubscription,
    DexMarketEventMessage,
    MessageChannelPolicy,
)
from API.channel_console_lib import DEFAULT_ALLOWED_STAGES
from DeFi.message_channels import record_atomic_swap_stage_event, record_market_stage_event
from DeFi.channel_reconciliation import reconcile_atomic_swap_subscription
from Listings.models import LimitOrder, TradingPair
from .models import (
    SwapFundingLock,
    LiquidityPool,
    LiquidityPosition,
    P2PSwapTransaction,
    PriceFeedSource,
    PriceFeedData,
    PriceFeedAggregation,
    SwapEscrow,
    SwapOffer,
    SwapTransaction,
)


class AtomicSwapAcceptanceTestCase(TestCase):
    def setUp(self):
        from DeFi.cleanup import purge_expired_swap_offers
        self.seller = User.objects.create_user(username='seller', password='testpass123')
        self.buyer = User.objects.create_user(username='buyer', password='testpass123')
        self.swap_offer = SwapOffer.objects.create(
            initiator=self.seller,
            offer_token='COLLECTIBLE#1',
            offer_amount=Decimal('1'),
            request_token='EVR',
            request_amount=Decimal('2'),
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.channel_policy = MessageChannelPolicy.objects.create(
            channel_key='tome0808_swapflow',
            channel_name='SYSTEM~SWAPS',
            network_mode='testnet',
            version=5,
            status='active',
            owner_account=self.seller,
            manager_account=self.seller,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )
        self.record_event_patcher = patch('DeFi.views.record_atomic_swap_stage_event')
        self.mock_view_record_event = self.record_event_patcher.start()
        self.mock_view_record_event.return_value.status = 'broadcasted'
        self.addCleanup(self.record_event_patcher.stop)
        self.client = Client()

    def test_swap_offer_has_a_stable_reconciliation_identifier(self):
        duplicate_offer = SwapOffer.objects.create(
            initiator=self.seller,
            offer_token='COLLECTIBLE#2',
            offer_amount=Decimal('1'),
            request_token='EVR',
            request_amount=Decimal('3'),
            expires_at=timezone.now() + timedelta(days=1),
        )

        self.assertIsNotNone(self.swap_offer.reconciliation_id)
        self.assertNotEqual(self.swap_offer.reconciliation_id, duplicate_offer.reconciliation_id)

    @patch('DeFi.message_channels.create_and_send_transfer_with_message_transaction')
    @patch('DeFi.message_channels.KuboAPIUploader.upload_bytes')
    @patch('DeFi.message_channels.RPC.listassetbalancesbyaddress')
    def test_stage_event_broadcasts_raw_message_and_records_txid(
        self,
        mock_balances,
        mock_upload,
        mock_raw_message,
    ):
        manager_wallet = UserWallet.objects.create(
            user=self.seller,
            entropy='manager-entropy',
            passphrase='',
        )
        WalletAddress.objects.create(
            wallet=manager_wallet,
            network_mode='testnet',
            address='EChannelAddress',
            wif='L1ChannelWif',
            account=0,
            index=0,
            is_change=False,
        )
        self.channel_policy.channel_name = 'ROOT~SWAPS'
        self.channel_policy.save(update_fields=['channel_name', 'updated_at'])
        mock_balances.return_value = {'ROOT~SWAPS': 1}
        mock_upload.return_value = SimpleNamespace(cid='QmSwapPayload')
        mock_raw_message.return_value = {'txid': 'raw-message-txid'}

        message = record_atomic_swap_stage_event(
            self.swap_offer,
            stage='offer_created',
            actor_username=self.seller.username,
            actor_user=self.seller,
        )

        self.assertEqual(message.status, 'broadcasted')
        self.assertEqual(message.broadcast_result, 'raw-message-txid')
        self.assertEqual(message.payload_ipfs_cid, 'QmSwapPayload')
        mock_raw_message.assert_called_once_with(
            from_address='EChannelAddress',
            to_address='EChannelAddress',
            asset_name='ROOT~SWAPS',
            asset_quantity=Decimal('1'),
            message='QmSwapPayload',
            expire_time=0,
            wif_keys=['L1ChannelWif'],
        )

    @patch('DeFi.message_channels.create_and_send_transfer_with_message_transaction')
    def test_explicit_dry_run_records_stage_without_broadcast(self, mock_raw_message):
        message = record_atomic_swap_stage_event(
            self.swap_offer,
            stage='offer_created',
            actor_username=self.seller.username,
            actor_user=self.seller,
            should_broadcast=False,
        )

        self.assertEqual(message.status, 'recorded')
        self.assertTrue(AtomicSwapTransferMessage.objects.filter(pk=message.pk).exists())
        mock_raw_message.assert_not_called()

    @patch('DeFi.message_channels.create_and_send_transfer_with_message_transaction')
    @patch('DeFi.message_channels.KuboAPIUploader.upload_bytes')
    @patch('DeFi.message_channels.RPC.listassetbalancesbyaddress')
    def test_market_order_event_broadcasts_to_eligible_console(
        self,
        mock_balances,
        mock_upload,
        mock_raw_message,
    ):
        manager_wallet = UserWallet.objects.create(
            user=self.seller,
            entropy='market-manager-entropy',
            passphrase='',
        )
        WalletAddress.objects.create(
            wallet=manager_wallet,
            network_mode='testnet',
            address='EMarketChannelAddress',
            wif='L1MarketChannelWif',
            account=0,
            index=0,
            is_change=False,
        )
        self.channel_policy.channel_name = 'ROOT~MARKETS'
        self.channel_policy.allowed_stages = list(DEFAULT_ALLOWED_STAGES)
        self.channel_policy.save(update_fields=['channel_name', 'allowed_stages', 'updated_at'])
        pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')
        order = LimitOrder.objects.create(
            user=self.seller,
            trading_pair=pair,
            side='sell',
            price=Decimal('2'),
            quantity=Decimal('3'),
        )
        mock_balances.return_value = {'ROOT~MARKETS': 1}
        mock_upload.return_value = SimpleNamespace(cid='QmMarketPayload')
        mock_raw_message.return_value = {'txid': 'raw-market-message-txid'}

        event = record_market_stage_event(pair, 'order_created', self.seller, order=order)[0]

        self.assertEqual(event.policy, self.channel_policy)
        self.assertEqual(event.status, 'broadcasted')
        self.assertEqual(event.payload['event_type'], 'dex_market_event')
        self.assertEqual(event.payload['details']['order']['id'], order.id)
        self.assertEqual(event.payload['aggregate_type'], 'dex_order')
        self.assertIn('payload_checksum', event.payload)
        self.assertTrue(DexMarketEventMessage.objects.filter(pk=event.pk).exists())
        mock_raw_message.assert_called_once_with(
            from_address='EMarketChannelAddress',
            to_address='EMarketChannelAddress',
            asset_name='ROOT~MARKETS',
            asset_quantity=Decimal('1'),
            message='QmMarketPayload',
            expire_time=0,
            wif_keys=['L1MarketChannelWif'],
        )

    @patch('DeFi.views.sign_and_broadcast_raw_transaction', return_value='chain-transaction-id')
    @patch('DeFi.views.uuid.uuid4', return_value='temp-fallback-id')
    @patch('DeFi.views._get_available_token_amount', side_effect=[Decimal('1'), Decimal('2')])
    @patch('DeFi.views.create_raw_atomic_asset_evr_swap_transaction', return_value={'raw_tx': 'raw-swap'})
    @patch('DeFi.views._derive_user_wif_for_address', side_effect=['seller-wif', 'buyer-wif'])
    @patch('DeFi.views._get_user_primary_address', side_effect=['seller-address', 'buyer-address'])
    def test_acceptance_records_only_a_broadcast_transaction(
        self,
        mock_primary_address,
        mock_derive_wif,
        mock_create_raw_swap,
        mock_available,
        mock_uuid,
        mock_broadcast,
    ):
        self.client.login(username='buyer', password='testpass123')

        response = self.client.post(reverse('accept_swap_offer', args=[self.swap_offer.id]))

        self.assertRedirects(response, reverse('my_swap_history'))
        self.swap_offer.refresh_from_db()
        self.assertEqual(self.swap_offer.status, 'completed')
        self.assertEqual(self.swap_offer.settlement_txid, 'chain-transaction-id')
        self.assertEqual(self.swap_offer.settlement_temp_txid, 'temp-temp-fallback-id')
        self.assertEqual(P2PSwapTransaction.objects.get().tx_hash, 'chain-transaction-id')
        self.assertEqual(SwapFundingLock.objects.filter(swap_offer=self.swap_offer, status='consumed').count(), 2)
        self.assertEqual(SwapFundingLock.objects.filter(swap_offer=self.swap_offer, status='locked').count(), 0)
        self.assertFalse(SwapEscrow.objects.filter(swap_offer=self.swap_offer).exists())
        mock_create_raw_swap.assert_called_once_with(
            seller_address='seller-address',
            buyer_address='buyer-address',
            asset_name='COLLECTIBLE#1',
            asset_quantity=Decimal('1'),
            payment_evr=Decimal('2'),
        )
        mock_broadcast.assert_called_once_with(
            'raw-swap',
            wif_keys=['seller-wif', 'buyer-wif'],
        )

    @patch('DeFi.views.sign_and_broadcast_raw_transaction', side_effect=RuntimeError('network timeout'))
    @patch('DeFi.views.uuid.uuid4', return_value='temp-fallback-id')
    @patch('DeFi.views._get_available_token_amount', side_effect=[Decimal('1'), Decimal('2')])
    @patch('DeFi.views.create_raw_atomic_asset_evr_swap_transaction', return_value={'raw_tx': 'raw-swap'})
    @patch('DeFi.views._derive_user_wif_for_address', side_effect=['seller-wif', 'buyer-wif'])
    @patch('DeFi.views._get_user_primary_address', side_effect=['seller-address', 'buyer-address'])
    def test_broadcast_failure_requires_reconciliation(
        self,
        mock_primary_address,
        mock_derive_wif,
        mock_create_raw_swap,
        mock_available,
        mock_uuid,
        mock_broadcast,
    ):
        self.client.login(username='buyer', password='testpass123')

        response = self.client.post(reverse('accept_swap_offer', args=[self.swap_offer.id]))

        self.assertRedirects(response, reverse('available_swap_offers'))
        self.swap_offer.refresh_from_db()
        self.assertEqual(self.swap_offer.status, 'settling')
        self.assertEqual(self.swap_offer.settlement_temp_txid, 'temp-temp-fallback-id')
        self.assertIn('reconciliation', self.swap_offer.settlement_error)
        self.assertEqual(SwapFundingLock.objects.filter(swap_offer=self.swap_offer, status='locked').count(), 2)
        self.assertFalse(P2PSwapTransaction.objects.filter(swap_offer=self.swap_offer).exists())

    @patch('DeFi.views._get_available_token_amount', side_effect=[Decimal('1'), Decimal('0')])
    def test_unfunded_counterparty_is_blocked_before_settlement(self, mock_available):
        self.client.login(username='buyer', password='testpass123')

        response = self.client.post(reverse('accept_swap_offer', args=[self.swap_offer.id]))

        self.assertRedirects(response, reverse('available_swap_offers'))
        self.swap_offer.refresh_from_db()
        self.assertEqual(self.swap_offer.status, 'pending')
        self.assertEqual(SwapFundingLock.objects.filter(swap_offer=self.swap_offer).count(), 0)

    @patch('DeFi.views.create_raw_atomic_asset_evr_swap_transaction')
    @patch('DeFi.views._get_available_token_amount', side_effect=[Decimal('1'), Decimal('2')])
    def test_channel_publish_failure_stops_settlement_before_raw_construction(
        self,
        _mock_available,
        mock_create_raw_swap,
    ):
        self.mock_view_record_event.return_value.status = 'failed'
        self.mock_view_record_event.return_value.error_message = 'Messaging channel is not postable.'
        self.client.login(username='buyer', password='testpass123')

        response = self.client.post(reverse('accept_swap_offer', args=[self.swap_offer.id]))

        self.assertRedirects(response, reverse('available_swap_offers'))
        self.swap_offer.refresh_from_db()
        self.assertEqual(self.swap_offer.status, 'pending')
        self.assertEqual(self.swap_offer.settlement_error, 'Messaging channel is not postable.')
        self.assertFalse(SwapFundingLock.objects.filter(swap_offer=self.swap_offer).exists())
        mock_create_raw_swap.assert_not_called()

    def test_creator_manual_remove_releases_existing_funding_locks(self):
        self.swap_offer.status = 'settling'
        self.swap_offer.save(update_fields=['status', 'updated_at'])
        SwapFundingLock.objects.create(
            swap_offer=self.swap_offer,
            user=self.seller,
            token_symbol=self.swap_offer.offer_token,
            amount=self.swap_offer.offer_amount,
            status='locked',
        )
        SwapFundingLock.objects.create(
            swap_offer=self.swap_offer,
            user=self.buyer,
            token_symbol='EVR',
            amount=self.swap_offer.request_amount,
            status='locked',
        )

        self.client.login(username='seller', password='testpass123')
        response = self.client.post(reverse('cancel_swap_offer', args=[self.swap_offer.id]))

        self.assertRedirects(response, reverse('my_swap_offers'))
        self.swap_offer.refresh_from_db()
        self.assertEqual(self.swap_offer.status, 'cancelled')
        self.assertEqual(SwapFundingLock.objects.filter(swap_offer=self.swap_offer, status='locked').count(), 0)
        self.assertEqual(SwapFundingLock.objects.filter(swap_offer=self.swap_offer, status='released').count(), 2)

    def test_expired_swap_offer_is_purged_and_locks_released(self):
        self.swap_offer.expires_at = timezone.now() - timedelta(minutes=1)
        self.swap_offer.save(update_fields=['expires_at', 'updated_at'])
        SwapFundingLock.objects.create(
            swap_offer=self.swap_offer,
            user=self.seller,
            token_symbol=self.swap_offer.offer_token,
            amount=self.swap_offer.offer_amount,
            status='locked',
        )

        purged = purge_expired_swap_offers(network_mode='testnet')

        self.assertEqual(purged, 1)
        self.assertFalse(SwapOffer.objects.filter(id=self.swap_offer.id).exists())
        self.assertFalse(SwapFundingLock.objects.filter(swap_offer_id=self.swap_offer.id).exists())

    @patch('DeFi.views.sign_and_broadcast_raw_transaction', return_value='asset-asset-chain-txid')
    @patch('DeFi.views.uuid.uuid4', return_value='temp-asset-asset')
    @patch('DeFi.views._get_available_token_amount', side_effect=[Decimal('1'), Decimal('6')])
    @patch('DeFi.views.create_raw_atomic_asset_asset_swap_transaction', return_value={'raw_tx': 'raw-asset-asset-swap'})
    @patch('DeFi.views._derive_user_wif_for_address', side_effect=['seller-wif', 'buyer-wif'])
    @patch('DeFi.views._get_user_primary_address', side_effect=['seller-address', 'buyer-address'])
    def test_acceptance_supports_fungible_settlement_assets(
        self,
        _mock_primary_address,
        _mock_derive_wif,
        mock_create_asset_asset_swap,
        _mock_available,
        _mock_uuid,
        _mock_broadcast,
    ):
        TrackedAsset.objects.create(
            symbol='TOKEN/SUB',
            network_mode='testnet',
            asset_type=TrackedAsset.ASSET_TYPE_SUB,
            units=2,
        )
        self.swap_offer.request_token = 'TOKEN/SUB'
        self.swap_offer.request_amount = Decimal('5.123')
        self.swap_offer.save(update_fields=['request_token', 'request_amount', 'updated_at'])

        self.client.login(username='buyer', password='testpass123')
        response = self.client.post(reverse('accept_swap_offer', args=[self.swap_offer.id]))

        self.assertRedirects(response, reverse('my_swap_history'))
        self.swap_offer.refresh_from_db()
        self.assertEqual(self.swap_offer.status, 'completed')
        mock_create_asset_asset_swap.assert_called_once_with(
            seller_address='seller-address',
            buyer_address='buyer-address',
            seller_asset_name='COLLECTIBLE#1',
            seller_asset_quantity=Decimal('1'),
            buyer_asset_name='TOKEN/SUB',
            buyer_asset_quantity=Decimal('5.12'),
        )
        self.assertTrue(
            SwapFundingLock.objects.filter(
                swap_offer=self.swap_offer,
                token_symbol='TOKEN/SUB',
                amount=Decimal('5.12'),
                status='consumed',
            ).exists()
        )

    @patch('DeFi.views._get_available_token_amount')
    def test_non_unique_offer_is_rejected_before_balance_checks(self, mock_available):
        TrackedAsset.objects.create(
            symbol='TOKEN/SUB',
            network_mode='testnet',
            asset_type=TrackedAsset.ASSET_TYPE_SUB,
            units=2,
        )
        self.swap_offer.offer_token = 'TOKEN/SUB'
        self.swap_offer.save(update_fields=['offer_token', 'updated_at'])

        self.client.login(username='buyer', password='testpass123')
        response = self.client.post(reverse('accept_swap_offer', args=[self.swap_offer.id]))

        self.assertRedirects(response, reverse('available_swap_offers'))
        self.swap_offer.refresh_from_db()
        self.assertEqual(self.swap_offer.status, 'pending')
        self.assertFalse(SwapFundingLock.objects.filter(swap_offer=self.swap_offer).exists())
        mock_available.assert_not_called()


class AtomicSwapChannelReconciliationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='reconcile-seller', password='testpass123')
        self.policy = MessageChannelPolicy.objects.create(
            channel_key='tome0808_swapflow',
            channel_name='TOME0808~SWAPFLOWV5',
            network_mode='testnet',
            version=5,
            status='active',
            owner_account=self.seller,
            manager_account=self.seller,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )
        self.offer = SwapOffer.objects.create(
            initiator=self.seller,
            offer_token='COLLECTIBLE#1',
            offer_amount=Decimal('1'),
            request_token='EVR',
            request_amount=Decimal('2'),
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.subscription = ChannelSubscription.objects.create(
            user=self.seller,
            policy=self.policy,
            role=ChannelSubscription.ROLE_PARTICIPANT,
        )
        self.consumer = ChannelConsumer.objects.create(
            network_mode='testnet',
            consumer_key='reconciliation-test-node',
            display_name='Reconciliation test node',
        )

    def _event(self, *, request_amount='2', aggregate_sequence=1, block_transaction_index=0):
        payload = add_payload_checksum({
            'event_type': 'atomic_swap_transfer',
            'event_version': 1,
            'event_id': str(uuid.uuid4()),
            'created_at': timezone.now().isoformat(),
            'network_mode': 'testnet',
            'aggregate_type': 'atomic_swap',
            'aggregate_id': str(self.offer.reconciliation_id),
            'aggregate_sequence': aggregate_sequence,
            'stage': 'offer_created',
            'correlation_id': str(self.offer.reconciliation_id),
            'transaction_id': '',
            'details': {
                'offer': {'token': 'COLLECTIBLE#1', 'amount': '1'},
                'request': {'token': 'EVR', 'amount': request_amount},
            },
        })
        return ChannelEvent.objects.create(
            policy=self.policy,
            event_id=payload['event_id'],
            event_type=payload['event_type'],
            event_version=payload['event_version'],
            aggregate_type=payload['aggregate_type'],
            aggregate_id=payload['aggregate_id'],
            aggregate_sequence=payload['aggregate_sequence'],
            stage=payload['stage'],
            network_mode=payload['network_mode'],
            payload=payload,
            payload_checksum=payload['payload_checksum'],
            payload_ipfs_cid='QmReconcileEvent',
            channel_txid=uuid.uuid4().hex + uuid.uuid4().hex,
            channel_output_index=0,
            block_height=123,
            block_transaction_index=block_transaction_index,
            block_hash='a' * 64,
            confirmed_at=timezone.now(),
            raw_observation={'channel': self.policy.channel_name},
        )

    def test_reconciliation_is_idempotent_for_a_matching_offer_projection(self):
        event = self._event()

        first_report = reconcile_atomic_swap_subscription(self.subscription, self.consumer)
        second_report = reconcile_atomic_swap_subscription(self.subscription, self.consumer)

        self.assertEqual(first_report['already_applied'], 1)
        self.assertEqual(second_report['processed'], 0)
        self.assertEqual(ChannelEventApplication.objects.filter(event=event).count(), 1)

    def test_reconciliation_orders_events_by_transaction_then_output_index(self):
        self._event(aggregate_sequence=1, block_transaction_index=0)
        self._event(aggregate_sequence=2, block_transaction_index=1)

        report = reconcile_atomic_swap_subscription(self.subscription, self.consumer)

        cursor = self.subscription.cursors.get(consumer=self.consumer)
        self.assertEqual(report['already_applied'], 2)
        self.assertEqual(cursor.last_seen_height, 123)
        self.assertEqual(cursor.last_seen_transaction_index, 1)
        self.assertEqual(cursor.last_seen_output_index, 0)

    def test_reconciliation_blocks_divergent_swap_terms(self):
        self._event(request_amount='3')

        report = reconcile_atomic_swap_subscription(self.subscription, self.consumer)

        self.assertEqual(report['blocked'], 1)
        self.assertTrue(ChannelReconciliationIssue.objects.filter(code='atomic_swap_terms_mismatch').exists())

    def test_reconciliation_blocks_a_missing_aggregate_predecessor(self):
        self._event(aggregate_sequence=2)

        report = reconcile_atomic_swap_subscription(self.subscription, self.consumer)

        self.assertEqual(report['blocked'], 1)
        self.assertTrue(ChannelReconciliationIssue.objects.filter(code='atomic_swap_sequence_gap').exists())


class FeeDistributionTestCase(TestCase):
    """Test cases for community liquidity fee distribution"""
    
    def setUp(self):
        """Set up test data"""
        # Create test users
        self.user1 = User.objects.create_user(username='provider1', password='testpass123')
        self.user2 = User.objects.create_user(username='provider2', password='testpass123')
        self.trader = User.objects.create_user(username='trader', password='testpass123')
        
        # Create a test pool
        self.pool = LiquidityPool.objects.create(
            name='ETH/USDC Pool',
            token_a_symbol='ETH',
            token_b_symbol='USDC',
            token_a_reserve=Decimal('100.0'),
            token_b_reserve=Decimal('100000.0'),
            total_liquidity_tokens=Decimal('100.0'),
            fee_percentage=Decimal('0.30')  # 0.30% fee
        )
        
        # Create liquidity positions for user1 (60% of pool) and user2 (40% of pool)
        self.position1 = LiquidityPosition.objects.create(
            user=self.user1,
            pool=self.pool,
            liquidity_tokens=Decimal('60.0')
        )
        
        self.position2 = LiquidityPosition.objects.create(
            user=self.user2,
            pool=self.pool,
            liquidity_tokens=Decimal('40.0')
        )
        
        self.client = Client()
    
    def test_fee_accumulation_on_swap(self):
        """Test that fees are properly accumulated when swaps occur"""
        self.client.login(username='trader', password='testpass123')
        
        # Perform a swap
        response = self.client.post('/defi/testnet/swap/', {
            'pool_id': self.pool.id,
            'from_token': 'ETH',
            'to_token': 'USDC',
            'amount': '10.0'
        })
        
        # Refresh pool from database
        self.pool.refresh_from_db()
        
        # Check that fees were accumulated in the pool
        expected_fee = Decimal('10.0') * Decimal('0.30') / Decimal('100')
        self.assertGreater(self.pool.accumulated_token_a_fees, Decimal('0'))
        self.assertAlmostEqual(float(self.pool.accumulated_token_a_fees), float(expected_fee), places=6)
    
    def test_fair_fee_distribution_to_providers(self):
        """Test that fees are distributed fairly based on liquidity share"""
        self.client.login(username='trader', password='testpass123')
        
        # Perform a swap
        swap_amount = Decimal('10.0')
        response = self.client.post('/defi/testnet/swap/', {
            'pool_id': self.pool.id,
            'from_token': 'ETH',
            'to_token': 'USDC',
            'amount': str(swap_amount)
        })
        
        # Refresh positions from database
        self.position1.refresh_from_db()
        self.position2.refresh_from_db()
        
        # Calculate expected fees
        total_fee = swap_amount * Decimal('0.30') / Decimal('100')
        expected_fee_user1 = total_fee * (Decimal('60.0') / Decimal('100.0'))  # 60% share
        expected_fee_user2 = total_fee * (Decimal('40.0') / Decimal('100.0'))  # 40% share
        
        # Check that fees were distributed proportionally
        self.assertAlmostEqual(float(self.position1.unclaimed_token_a_fees), float(expected_fee_user1), places=6)
        self.assertAlmostEqual(float(self.position2.unclaimed_token_a_fees), float(expected_fee_user2), places=6)
    
    def test_claim_fees(self):
        """Test that users can claim their accumulated fees"""
        # Manually add some fees to position1
        self.position1.unclaimed_token_a_fees = Decimal('1.5')
        self.position1.unclaimed_token_b_fees = Decimal('1500.0')
        self.position1.save()
        
        # Update pool accumulated fees
        self.pool.accumulated_token_a_fees = Decimal('1.5')
        self.pool.accumulated_token_b_fees = Decimal('1500.0')
        self.pool.save()
        
        # Login and claim fees
        self.client.login(username='provider1', password='testpass123')
        response = self.client.post('/defi/testnet/claim-fees/', {
            'position_id': self.position1.id
        })
        
        # Refresh from database
        self.position1.refresh_from_db()
        self.pool.refresh_from_db()
        
        # Check that fees were claimed (reset to 0)
        self.assertEqual(self.position1.unclaimed_token_a_fees, Decimal('0'))
        self.assertEqual(self.position1.unclaimed_token_b_fees, Decimal('0'))
        
        # Check that pool accumulated fees were reduced
        self.assertEqual(self.pool.accumulated_token_a_fees, Decimal('0'))
        self.assertEqual(self.pool.accumulated_token_b_fees, Decimal('0'))
    
    def test_no_fees_claimed_when_none_available(self):
        """Test that claiming with no fees available shows appropriate message"""
        self.client.login(username='provider1', password='testpass123')
        
        response = self.client.post('/defi/testnet/claim-fees/', {
            'position_id': self.position1.id
        })
        
        # Position should still have 0 fees
        self.position1.refresh_from_db()
        self.assertEqual(self.position1.unclaimed_token_a_fees, Decimal('0'))
        self.assertEqual(self.position1.unclaimed_token_b_fees, Decimal('0'))
    
    def test_pool_reserve_updated_correctly_with_fees(self):
        """Test that pool reserves are updated correctly (excluding fees)"""
        initial_reserve_a = self.pool.token_a_reserve
        swap_amount = Decimal('10.0')
        fee = swap_amount * Decimal('0.30') / Decimal('100')
        amount_after_fee = swap_amount - fee
        
        self.client.login(username='trader', password='testpass123')
        response = self.client.post('/defi/testnet/swap/', {
            'pool_id': self.pool.id,
            'from_token': 'ETH',
            'to_token': 'USDC',
            'amount': str(swap_amount)
        })
        
        self.pool.refresh_from_db()
        
        # Reserve should only increase by amount after fee
        expected_reserve = initial_reserve_a + amount_after_fee
        self.assertAlmostEqual(float(self.pool.token_a_reserve), float(expected_reserve), places=6)

class PriceFeedOracleTestCase(TestCase):
    """Test cases for price feed oracle network"""
    
    def setUp(self):
        """Set up test data"""
        # Create test user
        self.user = User.objects.create_user(username='oracle_user', password='testpass123')
        
        # Create oracle sources
        self.oracle1 = PriceFeedSource.objects.create(
            name='Oracle Alpha',
            oracle_address='0xABC123',
            is_active=True,
            reputation_score=Decimal('100.0')
        )
        
        self.oracle2 = PriceFeedSource.objects.create(
            name='Oracle Beta',
            oracle_address='0xDEF456',
            is_active=True,
            reputation_score=Decimal('95.0')
        )
        
        self.client = Client()
    
    def test_oracle_source_creation(self):
        """Test that oracle sources are created correctly"""
        self.assertEqual(PriceFeedSource.objects.count(), 2)
        self.assertTrue(self.oracle1.is_active)
        self.assertEqual(self.oracle1.total_submissions, 0)
    
    def test_submit_price_data(self):
        """Test submitting price data to oracle network"""
        self.client.login(username='oracle_user', password='testpass123')
        
        response = self.client.post('/defi/oracle/submit-price/', {
            'oracle_address': self.oracle1.oracle_address,
            'token_symbol': 'BTC',
            'price_usd': '45000.50'
        })
        
        # Check that price data was created
        self.assertEqual(PriceFeedData.objects.count(), 1)
        price_data = PriceFeedData.objects.first()
        self.assertEqual(price_data.token_symbol, 'BTC')
        self.assertEqual(price_data.price_usd, Decimal('45000.50'))
        self.assertEqual(price_data.source, self.oracle1)
        
        # Check that oracle submission count was updated
        self.oracle1.refresh_from_db()
        self.assertEqual(self.oracle1.total_submissions, 1)
    
    def test_price_aggregation_single_source(self):
        """Test price aggregation with single oracle source"""
        # Submit price from one oracle
        PriceFeedData.objects.create(
            source=self.oracle1,
            token_symbol='ETH',
            price_usd=Decimal('3000.00')
        )
        
        # Trigger aggregation
        from .views import _aggregate_price_feeds
        _aggregate_price_feeds('ETH')
        
        # Check aggregation was created
        self.assertEqual(PriceFeedAggregation.objects.filter(token_symbol='ETH').count(), 1)
        aggregation = PriceFeedAggregation.objects.filter(token_symbol='ETH').first()
        self.assertEqual(aggregation.aggregated_price, Decimal('3000.00'))
        self.assertEqual(aggregation.num_sources, 1)
        self.assertEqual(aggregation.confidence_score, Decimal('50.0'))  # Lower confidence with single source
    
    def test_price_aggregation_multiple_sources(self):
        """Test price aggregation with multiple oracle sources"""
        # Submit prices from multiple oracles
        PriceFeedData.objects.create(
            source=self.oracle1,
            token_symbol='BTC',
            price_usd=Decimal('45000.00')
        )
        
        PriceFeedData.objects.create(
            source=self.oracle2,
            token_symbol='BTC',
            price_usd=Decimal('45100.00')
        )
        
        # Trigger aggregation
        from .views import _aggregate_price_feeds
        _aggregate_price_feeds('BTC')
        
        # Check aggregation was created
        aggregation = PriceFeedAggregation.objects.filter(token_symbol='BTC').first()
        self.assertIsNotNone(aggregation)
        self.assertEqual(aggregation.num_sources, 2)
        
        # Median should be between the two prices
        self.assertGreaterEqual(aggregation.median_price, Decimal('45000.00'))
        self.assertLessEqual(aggregation.median_price, Decimal('45100.00'))
        
        # Min and max should match submitted prices
        self.assertEqual(aggregation.min_price, Decimal('45000.00'))
        self.assertEqual(aggregation.max_price, Decimal('45100.00'))
        
        # Confidence should be higher with multiple sources
        self.assertGreater(aggregation.confidence_score, Decimal('50.0'))
    
    def test_inactive_oracle_rejected(self):
        """Test that submissions from inactive oracles are rejected"""
        self.oracle1.is_active = False
        self.oracle1.save()
        
        self.client.login(username='oracle_user', password='testpass123')
        
        response = self.client.post('/defi/oracle/submit-price/', {
            'oracle_address': self.oracle1.oracle_address,
            'token_symbol': 'BTC',
            'price_usd': '45000.50'
        })
        
        # Check that submission was rejected
        self.assertRedirects(response, '/defi/oracle/submit-price/')
    
    def test_price_feeds_view(self):
        """Test that price feeds view displays correctly"""
        # Create some price data
        PriceFeedData.objects.create(
            source=self.oracle1,
            token_symbol='BTC',
            price_usd=Decimal('45000.00')
        )
        
        from .views import _aggregate_price_feeds
        _aggregate_price_feeds('BTC')
        
        response = self.client.get('/defi/oracle/price-feeds/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'BTC')
    
    def test_manage_oracle_registration(self):
        """Test oracle registration through manage view"""
        self.client.login(username='oracle_user', password='testpass123')
        
        response = self.client.post('/defi/oracle/manage/', {
            'action': 'register',
            'name': 'Oracle Gamma',
            'oracle_address': '0xGHI789',
            'description': 'Test oracle source'
        })
        
        # Check that oracle was created
        self.assertEqual(PriceFeedSource.objects.count(), 3)
        new_oracle = PriceFeedSource.objects.get(oracle_address='0xGHI789')
        self.assertEqual(new_oracle.name, 'Oracle Gamma')
        self.assertTrue(new_oracle.is_active)
    
    def test_oracle_toggle_activation(self):
        """Test toggling oracle activation status"""
        self.client.login(username='oracle_user', password='testpass123')
        
        # Deactivate oracle
        response = self.client.post('/defi/oracle/manage/', {
            'action': 'toggle',
            'oracle_address': self.oracle1.oracle_address
        })
        
        self.oracle1.refresh_from_db()
        self.assertFalse(self.oracle1.is_active)
        
        # Reactivate oracle
        response = self.client.post('/defi/oracle/manage/', {
            'action': 'toggle',
            'oracle_address': self.oracle1.oracle_address
        })
        
        self.oracle1.refresh_from_db()
        self.assertTrue(self.oracle1.is_active)


