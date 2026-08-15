from decimal import Decimal
from io import StringIO
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from API.channel_event_protocol import add_payload_checksum
from API.models import (
    ChannelEvent,
    ChannelEventApplication,
    ChannelSubscription,
    MessageChannelPolicy,
)
from Listings.channel_reconciliation import (
    apply_dec_channel_event,
    user_holds_dec_channel_token,
)
from Listings.models import DecPokerGameInstance, DecPokerPayoutPolicy
from Wallet.models import UserWallet, WalletAddress, WalletProfile


class DecChannelReconciliationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='dec-reconciler', password='testpass123')
        self.wallet = UserWallet.objects.create(user=self.user, entropy='00' * 16)
        self.address = WalletAddress.objects.create(
            wallet=self.wallet,
            network_mode='testnet',
            address='msmLKdT7nnGGZocTVajs2W6ohjy13gxDyz',
            wif='',
            account=0,
            index=0,
            is_change=False,
        )
        self.profile = WalletProfile.objects.create(
            wallet=self.wallet,
            address=self.address,
            network_mode='testnet',
            name='Main',
            is_main=True,
        )
        self.policy = MessageChannelPolicy.objects.create(
            channel_key='tome0808_swapflow',
            channel_name='TOME0808~SWAPFLOWV5',
            network_mode='testnet',
            version=5,
            status='active',
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=[
                'game_instance_created',
                'payout_policy_published',
                'game_spend_recorded',
                'game_reward_distributed',
            ],
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )
        self.instance = DecPokerGameInstance.objects.create(
            creator=self.user,
            manager_account=self.user,
            network_mode='testnet',
            title='Reconciled table',
            reward_asset_name='DECTEST',
            reward_asset_units=2,
            reward_supply=Decimal('1000'),
            entry_fee_evr=Decimal('0.5'),
            reward_per_win=Decimal('10'),
            instance_fee_evr=Decimal('1'),
            instance_fee_txid='fee-txid',
            system_fee_address=self.address.address,
            vault_profile=self.profile,
            channel_policy=self.policy,
            reward_issue_txid='issue-txid',
            owner_transfer_txid='funding-txid',
            status=DecPokerGameInstance.STATUS_ACTIVE,
            is_active=True,
        )
        self.payout_policy = DecPokerPayoutPolicy.objects.create(
            game_instance=self.instance,
            version=1,
            game_rule_version='dec_poker_payout_v1',
            house_rule='dealer_best_two_of_three_wins_ties',
            wager_currency='EVR',
            payout_currency='DECTEST',
            minimum_wager_evr=Decimal('0.5'),
            reward_per_win=Decimal('10'),
            payout_cap_amount=Decimal('10'),
            win_probability_numerator=1,
            win_probability_denominator=2,
            expected_reward_per_wager=Decimal('5'),
            rtp_disclosure='Test disclosure',
            payout_table={},
            policy_hash='a' * 64,
        )

    def _instance_event(self, **context_overrides):
        context = {
            'reward_asset_name': self.instance.reward_asset_name,
            'reward_supply': '1000',
            'reward_units': 2,
            'entry_fee_evr': '0.5',
            'instance_fee_evr': '1',
            'fee_txid': self.instance.instance_fee_txid,
            'issue_txid': self.instance.reward_issue_txid,
            'reward_transfer_txid': self.instance.owner_transfer_txid,
            'payout_policy_version': self.payout_policy.version,
            'payout_policy_hash': self.payout_policy.policy_hash,
        }
        context.update(context_overrides)
        aggregate_id = str(uuid.uuid4())
        payload = add_payload_checksum({
            'event_type': 'dec_game_event',
            'event_version': 1,
            'event_id': str(uuid.uuid4()),
            'created_at': timezone.now().isoformat(),
            'network_mode': 'testnet',
            'aggregate_type': 'dec_poker_game_instance',
            'aggregate_id': aggregate_id,
            'aggregate_sequence': 1,
            'stage': 'game_instance_created',
            'details': {
                'actor': self.user.username,
                'game_instance': {
                    'id': self.instance.pk,
                    'reward_asset_name': self.instance.reward_asset_name,
                },
                'context': context,
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
            payload_ipfs_cid='QmDecInstanceEvent',
            channel_txid='b' * 64,
            channel_output_index=0,
            block_height=100,
            block_transaction_index=1,
            block_hash='c' * 64,
            confirmed_at=timezone.now(),
            raw_observation={'source': 'channel_asset_lineage'},
        )

    def test_matching_instance_event_is_applied_idempotently(self):
        event = self._instance_event()

        application = apply_dec_channel_event(event)
        repeated = apply_dec_channel_event(event)

        self.assertEqual(application.status, ChannelEventApplication.STATUS_APPLIED)
        self.assertEqual(repeated.pk, application.pk)
        self.instance.refresh_from_db()
        self.assertEqual(
            self.instance.reconciliation_status,
            DecPokerGameInstance.RECONCILIATION_STATUS_SYNCED,
        )
        self.assertEqual(self.instance.reconciliation_evidence['game_instance_created']['txid'], 'b' * 64)

    def test_conflicting_instance_event_rejects_and_deactivates_instance(self):
        event = self._instance_event(reward_supply='999')

        application = apply_dec_channel_event(event)

        self.assertEqual(application.status, ChannelEventApplication.STATUS_BLOCKED)
        self.instance.refresh_from_db()
        self.assertEqual(
            self.instance.reconciliation_status,
            DecPokerGameInstance.RECONCILIATION_STATUS_REJECTED,
        )
        self.assertFalse(self.instance.is_active)
        issue = self.policy.reconciliation_issues.get(code='dec_instance_conflict')
        self.assertEqual(issue.detail['mismatched_fields'], ['reward_supply'])

    @patch('Listings.channel_reconciliation.evrmore_rpc.list_asset_balances_by_address')
    def test_subscriber_requires_positive_active_channel_balance(self, mock_balances):
        mock_balances.return_value = {self.policy.channel_name: '1.00000000'}

        evidence = user_holds_dec_channel_token(self.user, self.policy)

        self.assertTrue(evidence['is_holder'])
        self.assertEqual(evidence['channel_name'], self.policy.channel_name)
        self.assertEqual(evidence['total_balance'], '1.00000000')


class ReconcileDecChannelCommandTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='dec-command-user', password='testpass123')
        self.policy = MessageChannelPolicy.objects.create(
            channel_key='tome0808_swapflow',
            channel_name='TOME0808~SWAPFLOWV5',
            network_mode='testnet',
            version=5,
            status='active',
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=['game_instance_created'],
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )

    @patch('Listings.management.commands.reconcile_dec_channel.reconcile_dec_subscription')
    @patch('Listings.management.commands.reconcile_dec_channel.user_holds_dec_channel_token')
    @patch('Listings.management.commands.reconcile_dec_channel.ingest_channel_history')
    def test_command_creates_subscription_only_for_verified_holder(
        self,
        mock_ingest,
        mock_holder,
        mock_reconcile,
    ):
        mock_ingest.return_value = {'ingested': 0, 'invalid': 0}
        mock_holder.return_value = {
            'is_holder': True,
            'channel_name': self.policy.channel_name,
            'total_balance': '1',
            'addresses': [],
        }
        mock_reconcile.return_value = {'processed': 0, 'applied': 0, 'blocked': 0}
        output = StringIO()

        call_command(
            'reconcile_dec_channel',
            '--user', self.user.username,
            stdout=output,
        )

        subscription = ChannelSubscription.objects.get(user=self.user, policy=self.policy)
        self.assertEqual(subscription.role, ChannelSubscription.ROLE_PARTICIPANT)
        mock_reconcile.assert_called_once()
        self.assertIn('"channel_key": "tome0808_swapflow"', output.getvalue())