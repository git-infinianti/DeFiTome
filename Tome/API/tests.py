from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch
from io import StringIO
import json
import uuid
from decimal import Decimal

from .models import (
    APIKey,
    SolidityContract,
    ContractInteraction,
    ContractAsset,
    ChannelConsumer,
    ChannelEvent,
    ChannelSubscription,
    ChannelSubscriptionCursor,
    MessageChannelPolicy,
)
from API.channel_event_protocol import add_payload_checksum
from API.channel_console_lib import DEFAULT_ALLOWED_STAGES
from API.unified_workflow_policy import (
    UNIFIED_WORKFLOW_CHANNEL_KEY,
    UNIFIED_WORKFLOW_CHANNEL_TAG,
    UNIFIED_WORKFLOW_POLICY_VERSION,
    UNIFIED_WORKFLOW_STRICT_RULES,
)
from API.channel_reconciliation import ChannelHistoryUnavailable, ingest_channel_history
from .rpc import EvrmoreRPC
from Wallet.models import UserWallet, WalletAddress
from API.channel_console_service import (
    burn_channel_asset_for_revision,
    create_channel_console_asset_for_user,
    set_channel_subscription,
    validate_channel_console_asset,
)


class ChannelReconciliationModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='channel-observer', password='testpass123')
        self.policy = MessageChannelPolicy.objects.create(
            channel_key='tome0808_swapflow',
            channel_name='TOME0808~SWAPFLOWV5',
            network_mode='testnet',
            version=5,
            status='active',
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=['offer_created'],
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )

    def _payload(self):
        return add_payload_checksum({
            'event_type': 'atomic_swap_transfer',
            'event_version': 1,
            'event_id': str(uuid.uuid4()),
            'created_at': timezone.now().isoformat(),
            'network_mode': 'testnet',
            'aggregate_type': 'atomic_swap',
            'aggregate_id': str(uuid.uuid4()),
            'aggregate_sequence': 1,
            'stage': 'offer_created',
            'details': {'offer_token': 'COLLECTIBLE#1'},
        })

    def test_verified_event_is_append_only_and_checksum_validated(self):
        payload = self._payload()
        event = ChannelEvent.objects.create(
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
            payload_ipfs_cid='QmAtomicSwapEvent',
            channel_txid='a' * 64,
            channel_output_index=2,
            block_height=42,
            block_transaction_index=1,
            block_hash='b' * 64,
            confirmed_at=timezone.now(),
            raw_observation={'channel': self.policy.channel_name},
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChannelEvent.objects.filter(pk=event.pk).update(stage='swap_cancelled')

        event.refresh_from_db()
        self.assertEqual(event.stage, 'offer_created')

    def test_aggregate_sequence_is_unique_within_a_policy_and_aggregate(self):
        payload = self._payload()
        event_fields = {
            'policy': self.policy,
            'event_type': payload['event_type'],
            'event_version': payload['event_version'],
            'aggregate_type': payload['aggregate_type'],
            'aggregate_id': payload['aggregate_id'],
            'aggregate_sequence': payload['aggregate_sequence'],
            'stage': payload['stage'],
            'network_mode': payload['network_mode'],
            'payload_checksum': payload['payload_checksum'],
            'payload_ipfs_cid': 'QmAtomicSwapEvent',
            'channel_output_index': 2,
            'block_height': 42,
            'block_transaction_index': 1,
            'block_hash': 'b' * 64,
            'confirmed_at': timezone.now(),
            'raw_observation': {'channel': self.policy.channel_name},
        }
        ChannelEvent.objects.create(
            **event_fields,
            event_id=payload['event_id'],
            payload=payload,
            channel_txid='a' * 64,
        )
        duplicate_payload = add_payload_checksum({
            **payload,
            'event_id': str(uuid.uuid4()),
        })
        duplicate_event_fields = {
            **event_fields,
            'event_id': duplicate_payload['event_id'],
            'payload': duplicate_payload,
            'payload_checksum': duplicate_payload['payload_checksum'],
            'channel_txid': 'c' * 64,
            'channel_output_index': 3,
        }

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChannelEvent.objects.bulk_create([ChannelEvent(**duplicate_event_fields)])

    def test_subscription_cursor_is_scoped_to_a_consumer_and_policy(self):
        subscription = ChannelSubscription.objects.create(
            user=self.user,
            policy=self.policy,
            role=ChannelSubscription.ROLE_OBSERVER,
        )
        consumer = ChannelConsumer.objects.create(
            network_mode='testnet',
            consumer_key='test-node',
            display_name='Test node',
        )
        cursor = ChannelSubscriptionCursor.objects.create(
            subscription=subscription,
            consumer=consumer,
        )

        self.assertEqual(cursor.status, ChannelSubscriptionCursor.STATUS_LAGGING)
        self.assertEqual(cursor.subscription.policy, self.policy)


class ChannelHistoryIngestionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='channel-ingestor', password='testpass123')
        self.policy = MessageChannelPolicy.objects.create(
            channel_key='tome0808_swapflow',
            channel_name='TOME0808~SWAPFLOWV5',
            network_mode='testnet',
            version=5,
            status='active',
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=['offer_created'],
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )
        self.payload = add_payload_checksum({
            'event_type': 'atomic_swap_transfer',
            'event_version': 1,
            'event_id': str(uuid.uuid4()),
            'created_at': timezone.now().isoformat(),
            'network_mode': 'testnet',
            'aggregate_type': 'atomic_swap',
            'aggregate_id': str(uuid.uuid4()),
            'aggregate_sequence': 1,
            'stage': 'offer_created',
            'details': {'offer_token': 'COLLECTIBLE#1'},
        })
        self.holder_address = 'msmLKdT7nnGGZocTVajs2W6ohjy13gxDyz'
        self.channel_txid = 'a' * 64
        self.issuance_txid = 'b' * 64
        self.block_hash = 'c' * 64
        self.mock_holders = self._start_patch(
            'API.channel_reconciliation.evrmore_rpc.list_addresses_by_asset'
        )
        self.mock_utxos = self._start_patch(
            'API.channel_reconciliation.evrmore_rpc.get_address_utxos'
        )
        self.mock_raw_transaction = self._start_patch(
            'API.channel_reconciliation.evrmore_rpc.get_raw_transaction'
        )
        self.mock_block = self._start_patch(
            'API.channel_reconciliation.evrmore_rpc.get_block'
        )
        self.mock_download_json = self._start_patch(
            'API.channel_reconciliation.KuboAPIUploader.download_json'
        )
        self.mock_transaction_evidence = self._start_patch(
            'API.channel_reconciliation.get_public_transaction_evidence'
        )

    def _start_patch(self, target):
        patcher = patch(target)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _configure_lineage(self, channel_txid=None, message_cid='QmAtomicSwapEvent', block_hash=None, block_height=42):
        channel_txid = channel_txid or self.channel_txid
        block_hash = block_hash or self.block_hash
        transfer_transaction = {
            'txid': channel_txid,
            'blockhash': block_hash,
            'height': block_height,
            'vin': [{'txid': self.issuance_txid, 'vout': 0}],
            'vout': [{
                'n': 2,
                'scriptPubKey': {
                    'type': 'transfer_asset',
                    'asset': {
                        'name': self.policy.channel_name,
                        'amount': 1,
                        'message': message_cid,
                    },
                },
            }],
        }
        issuance_transaction = {
            'txid': self.issuance_txid,
            'vin': [],
            'vout': [{
                'n': 0,
                'scriptPubKey': {
                    'type': 'new_asset',
                    'asset': {'name': self.policy.channel_name, 'amount': 1},
                },
            }],
        }
        transactions = {
            channel_txid: transfer_transaction,
            self.issuance_txid: issuance_transaction,
        }
        self.mock_holders.return_value = {self.holder_address: '1.00000000'}
        self.mock_utxos.return_value = [{
            'assetName': self.policy.channel_name,
            'txid': channel_txid,
            'outputIndex': 2,
        }]
        self.mock_raw_transaction.side_effect = lambda transaction_id, verbose: transactions[transaction_id]
        self.mock_block.return_value = {
            'hash': block_hash,
            'height': block_height,
            'tx': [channel_txid],
        }

    def test_ingestion_persists_only_confirmed_canonical_observations(self):
        self._configure_lineage()
        self.mock_download_json.return_value = self.payload
        self.mock_transaction_evidence.return_value = {
            'transaction_id': self.channel_txid,
            'confirmations': 1,
            'block_hash': self.block_hash,
            'block_time': 1_700_000_000,
            'transaction_time': 1_700_000_000,
        }

        report = ingest_channel_history(self.policy)

        self.assertEqual(report['ingested'], 1)
        event = ChannelEvent.objects.get()
        self.assertEqual(event.event_id, self.payload['event_id'])
        self.assertEqual(event.block_transaction_index, 0)
        self.assertEqual(event.payload_checksum, self.payload['payload_checksum'])
        self.mock_utxos.assert_called_once_with([self.holder_address], asset_name=self.policy.channel_name)

    def test_ingestion_rejects_ambiguous_channel_holders(self):
        self.mock_holders.return_value = {
            self.holder_address: 1,
            'mp2BoHfEPNfwiWFnuqQse6i9v9srYtdYFv': 1,
        }

        with self.assertRaisesMessage(ChannelHistoryUnavailable, 'exactly one holder'):
            ingest_channel_history(self.policy)

        self.mock_utxos.assert_not_called()
        issue = self.policy.reconciliation_issues.get(code='invalid_channel_observation')
        self.assertEqual(issue.severity, 'critical')

    def test_ingestion_records_an_issue_for_missing_block_transaction_coordinate(self):
        self._configure_lineage()
        self.mock_block.return_value['tx'] = []

        with self.assertRaisesMessage(ChannelHistoryUnavailable, 'does not contain the channel transaction exactly once'):
            ingest_channel_history(self.policy)

        issue = self.policy.reconciliation_issues.get(code='invalid_channel_observation')
        self.assertEqual(issue.severity, 'critical')
        self.assertIn('does not contain the channel transaction', issue.detail['error'])

    def test_ingestion_rejects_an_unmessaged_channel_transfer(self):
        self._configure_lineage(message_cid='')

        with self.assertRaisesMessage(ChannelHistoryUnavailable, 'missing its IPFS message CID'):
            ingest_channel_history(self.policy)

        issue = self.policy.reconciliation_issues.get(code='invalid_channel_observation')
        self.assertEqual(issue.severity, 'critical')

    def test_ingestion_records_mismatched_public_transaction_evidence(self):
        self._configure_lineage()
        self.mock_download_json.return_value = self.payload
        self.mock_transaction_evidence.return_value = {'block_hash': 'd' * 64}

        report = ingest_channel_history(self.policy)

        self.assertEqual(report['invalid'], 1)
        self.assertFalse(ChannelEvent.objects.exists())
        issue = self.policy.reconciliation_issues.get(code='invalid_channel_observation')
        self.assertEqual(issue.severity, 'critical')
        self.assertIn('does not match', issue.detail['error'])

    def test_ingestion_records_a_checksum_mismatch(self):
        self._configure_lineage()
        self.mock_download_json.return_value = {**self.payload, 'payload_checksum': '0' * 64}

        report = ingest_channel_history(self.policy)

        self.assertEqual(report['invalid'], 1)
        self.assertFalse(ChannelEvent.objects.exists())
        self.mock_transaction_evidence.assert_not_called()
        issue = self.policy.reconciliation_issues.get(code='invalid_channel_observation')
        self.assertEqual(issue.severity, 'critical')
        self.assertIn('checksum', issue.detail['error'])

    def test_ingestion_records_conflicting_reuse_of_an_event_id(self):
        self._configure_lineage()
        self.mock_download_json.return_value = self.payload
        self.mock_transaction_evidence.return_value = {'block_hash': self.block_hash}
        self.assertEqual(ingest_channel_history(self.policy)['ingested'], 1)

        conflicting_txid = 'd' * 64
        conflicting_block_hash = 'e' * 64
        self._configure_lineage(
            channel_txid=conflicting_txid,
            message_cid='QmConflictingEvent',
            block_hash=conflicting_block_hash,
            block_height=43,
        )
        self.mock_transaction_evidence.return_value = {'block_hash': conflicting_block_hash}

        report = ingest_channel_history(self.policy)

        self.assertEqual(report['invalid'], 1)
        self.assertEqual(ChannelEvent.objects.count(), 1)
        issue = self.policy.reconciliation_issues.get(code='duplicate_event_id_conflict')
        self.assertEqual(issue.severity, 'critical')
        self.assertEqual(issue.detail['existing_txid'], self.channel_txid)
        self.assertEqual(issue.detail['observed_txid'], conflicting_txid)


class ReconcileChannelCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='command-observer', password='testpass123')
        self.policy = MessageChannelPolicy.objects.create(
            channel_key='tome0808_swapflow',
            channel_name='TOME0808~SWAPFLOWV5',
            network_mode='testnet',
            version=5,
            status='active',
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=['offer_created'],
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )

    @patch('API.management.commands.reconcile_channel.reconcile_atomic_swap_subscription')
    @patch('API.management.commands.reconcile_channel.ingest_channel_history')
    def test_command_creates_an_observer_subscription_and_reconciles_it(
        self,
        mock_ingest,
        mock_reconcile,
    ):
        mock_ingest.return_value = {'ingested': 0, 'invalid': 0}
        mock_reconcile.return_value = {'processed': 0, 'blocked': 0}
        output = StringIO()

        call_command(
            'reconcile_channel',
            '--channel', self.policy.channel_key,
            '--network', 'testnet',
            '--user', self.user.username,
            stdout=output,
        )

        subscription = ChannelSubscription.objects.get(user=self.user, policy=self.policy)
        consumer = ChannelConsumer.objects.get(network_mode='testnet', consumer_key='server')
        mock_ingest.assert_called_once_with(self.policy)
        mock_reconcile.assert_called_once_with(subscription, consumer)
        self.assertIn('"channel_key": "tome0808_swapflow"', output.getvalue())

    @patch('API.management.commands.reconcile_channel.reconcile_atomic_swap_subscription')
    @patch('API.management.commands.reconcile_channel.ingest_channel_history')
    def test_ingest_only_does_not_create_a_consumer_or_subscription(self, mock_ingest, mock_reconcile):
        mock_ingest.return_value = {'ingested': 1, 'invalid': 0}
        output = StringIO()

        call_command(
            'reconcile_channel',
            '--channel', self.policy.channel_key,
            '--network', 'testnet',
            '--ingest-only',
            stdout=output,
        )

        self.assertFalse(ChannelConsumer.objects.exists())
        self.assertFalse(ChannelSubscription.objects.exists())
        mock_reconcile.assert_not_called()
        self.assertIn('"ingest_only": true', output.getvalue())

    @patch(
        'API.management.commands.reconcile_channel.ingest_channel_history',
        side_effect=ChannelHistoryUnavailable('Channel history is unavailable.'),
    )
    def test_unavailable_history_fails_before_creating_reconciliation_state(self, mock_ingest):
        with self.assertRaises(CommandError):
            call_command(
                'reconcile_channel',
                '--channel', self.policy.channel_key,
                '--network', 'testnet',
                '--user', self.user.username,
            )

        mock_ingest.assert_called_once_with(self.policy)
        self.assertFalse(ChannelConsumer.objects.exists())
        self.assertFalse(ChannelSubscription.objects.exists())

    @patch('API.management.commands.reconcile_channel.reconcile_atomic_swap_subscription')
    @patch('API.management.commands.reconcile_channel.ingest_channel_history')
    def test_invalid_ingestion_prevents_projection_reconciliation(self, mock_ingest, mock_reconcile):
        mock_ingest.return_value = {'ingested': 0, 'invalid': 1}

        with self.assertRaisesMessage(CommandError, 'no projections were reconciled'):
            call_command(
                'reconcile_channel',
                '--channel', self.policy.channel_key,
                '--network', 'testnet',
                '--user', self.user.username,
            )

        mock_reconcile.assert_not_called()
        self.assertFalse(ChannelConsumer.objects.exists())
        self.assertFalse(ChannelSubscription.objects.exists())


class NFTMintEndpointTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='nft-owner', password='testpass123')
        self.raw_api_key = 'nft-test-api-key'
        APIKey.objects.create(
            user=self.user,
            name='NFT tests',
            key_hash=APIKey.hash_key(self.raw_api_key),
            key_prefix=self.raw_api_key[:8],
        )

    def test_mint_nft_endpoint_is_locked_down(self):
        response = self.client.post(
            reverse('nft_mint'),
            data=json.dumps({
                'root_name': 'COLLECTIBLE',
                'asset_tag': '001',
                'ipfs_hash': 'QmExample',
            }),
            content_type='application/json',
            HTTP_X_API_KEY=self.raw_api_key,
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json().get('success'))


class IssueAssetWrapperTestCase(TestCase):
    @patch('API.rpc.RPC')
    def test_issue_asset_passes_full_parameter_shape_and_defaults_remintable(self, mock_rpc):
        mock_rpc.issue.return_value = 'issue-txid'
        rpc_client = EvrmoreRPC()

        result = rpc_client.issue_asset(
            asset_name='ROOT~OPS',
            qty=1,
            to_address='',
            change_address='',
            units=0,
            reissuable=False,
            has_ipfs=True,
            ipfs_hash='QmCid',
        )

        self.assertEqual(result, 'issue-txid')
        mock_rpc.issue.assert_called_once_with(
            'ROOT~OPS',
            1,
            '',
            '',
            0,
            False,
            True,
            'QmCid',
            '',
            0,
            '',
            False,
            False,
            False,
        )

    @patch.object(EvrmoreRPC, 'issue_unique_asset', return_value='mint-transaction-id')
    def test_nft_rpc_wrapper_uses_unique_asset_issuance(self, mock_issue_unique_asset):
        rpc_client = EvrmoreRPC()

        result = rpc_client.issue_nft_asset(
            asset_name='COLLECTIBLE#001',
            ipfs_hash='QmExample',
        )

        self.assertEqual(result, 'mint-transaction-id')
        mock_issue_unique_asset.assert_called_once_with(
            root_name='COLLECTIBLE',
            asset_tags=['001'],
            ipfs_hashes=['QmExample'],
            to_address='',
            change_address='',
        )


class APIInfoTestCase(TestCase):
    """Test API information endpoint"""
    
    def setUp(self):
        self.client = Client()
        self.api_info_url = reverse('api_info')
    
    def test_api_info_accessible(self):
        """Test that API info endpoint is accessible"""
        response = self.client.get(self.api_info_url)
        # Either success (200) or RPC error (500) is acceptable for MVP
        self.assertIn(response.status_code, [200, 500])
        
        # Check response is JSON
        self.assertEqual(response['Content-Type'], 'application/json')
        
        # Parse response
        data = response.json()
        # If RPC is available, expect success
        # If RPC is not available, expect error
        if response.status_code == 200:
            self.assertTrue(data.get('success'))
            self.assertIn('api_version', data)
            self.assertIn('endpoints', data)
        else:
            # RPC not available is acceptable in test environment
            self.assertFalse(data.get('success'))
            self.assertIn('error', data)


class ContractListTestCase(TestCase):
    """Test contract listing endpoint"""
    
    def setUp(self):
        self.client = Client()
        self.contracts_url = reverse('contracts_list')
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
    
    def test_contract_list_is_locked_down(self):
        response = self.client.get(self.contracts_url)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json().get('success'))
    
    def test_create_contract_requires_auth(self):
        """Test that creating a contract requires authentication"""
        contract_data = {
            'name': 'New Contract',
            'contract_address': 'NEW_ASSET',
            'description': 'A new contract'
        }
        
        response = self.client.post(
            self.contracts_url,
            data=json.dumps(contract_data),
            content_type='application/json'
        )
        
        self.assertEqual(response.status_code, 403)
        data = response.json()
        self.assertFalse(data.get('success'))
    
    def test_create_contract_authenticated(self):
        """Test creating a contract when authenticated"""
        self.client.login(username='testuser', password='testpass123')
        
        contract_data = {
            'name': 'New Contract',
            'contract_address': 'NEW_ASSET',
            'description': 'A new contract'
        }
        
        response = self.client.post(
            self.contracts_url,
            data=json.dumps(contract_data),
            content_type='application/json'
        )
        
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json().get('success'))
    
    def test_create_contract_missing_fields(self):
        """Test that creating a contract fails with missing required fields"""
        self.client.login(username='testuser', password='testpass123')
        
        contract_data = {
            'name': 'New Contract',
            # Missing contract_address
        }
        
        response = self.client.post(
            self.contracts_url,
            data=json.dumps(contract_data),
            content_type='application/json'
        )
        
        self.assertEqual(response.status_code, 403)
        data = response.json()
        self.assertFalse(data.get('success'))


class ContractDetailTestCase(TestCase):
    """Test contract detail endpoint"""
    
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.contract = SolidityContract.objects.create(
            name='Test Contract',
            contract_address='TEST_ASSET',
            deployer=self.user,
            description='Test description',
            source_code='contract Test {}',
            abi=[{'type': 'function', 'name': 'test'}]
        )
    
    def test_get_contract_detail(self):
        """Test getting contract details"""
        url = reverse('contract_detail', kwargs={'contract_id': self.contract.id})
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json().get('success'))
    
    def test_get_nonexistent_contract(self):
        """Test getting details of non-existent contract"""
        url = reverse('contract_detail', kwargs={'contract_id': 99999})
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json().get('success'))


class ContractInteractionTestCase(TestCase):
    """Test contract interaction endpoint"""
    
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.contract = SolidityContract.objects.create(
            name='Test Contract',
            contract_address='TEST_ASSET',
            deployer=self.user,
            description='Test description'
        )
    
    def test_interact_requires_auth(self):
        """Test that interacting with a contract requires authentication"""
        url = reverse('contract_interact', kwargs={'contract_id': self.contract.id})
        interaction_data = {
            'function_name': 'testFunction',
            'parameters': {'param1': 'value1'}
        }
        
        response = self.client.post(
            url,
            data=json.dumps(interaction_data),
            content_type='application/json'
        )
        
        self.assertEqual(response.status_code, 401)
    
    def test_interact_authenticated(self):
        """Test interacting with a contract when authenticated"""
        self.client.login(username='testuser', password='testpass123')
        
        url = reverse('contract_interact', kwargs={'contract_id': self.contract.id})
        interaction_data = {
            'function_name': 'testFunction',
            'parameters': {'param1': 'value1'}
        }
        
        response = self.client.post(
            url,
            data=json.dumps(interaction_data),
            content_type='application/json'
        )
        
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json().get('success'))
    
    def test_interact_missing_function_name(self):
        """Test that interaction fails without function_name"""
        self.client.login(username='testuser', password='testpass123')
        
        url = reverse('contract_interact', kwargs={'contract_id': self.contract.id})
        interaction_data = {
            'parameters': {'param1': 'value1'}
        }
        
        response = self.client.post(
            url,
            data=json.dumps(interaction_data),
            content_type='application/json'
        )
        
        self.assertEqual(response.status_code, 401)


class AllowedRpcProcedureApiTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='rpc-user', password='testpass123')
        self.api_key = 'rpc-api-key'
        APIKey.objects.create(
            user=self.user,
            name='RPC key',
            key_hash=APIKey.hash_key(self.api_key),
            key_prefix=self.api_key[:8],
        )

    def test_rpc_procedure_catalog_is_available(self):
        response = self.client.get(reverse('rpc_procedures'))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get('success'))
        self.assertTrue(any(group['category'] == 'Addressindex' for group in payload.get('catalog', [])))

    @patch('API.rpc_procedure_registry.evrmore_rpc.client.getblockchaininfo', return_value={'chain': 'test', 'blocks': 123})
    def test_rpc_execute_allows_whitelisted_procedure(self, mock_getblockchaininfo):
        response = self.client.post(
            reverse('rpc_execute'),
            data=json.dumps({'procedure': 'getblockchaininfo', 'params': []}),
            content_type='application/json',
            HTTP_X_API_KEY=self.api_key,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get('success'))
        self.assertEqual(payload.get('procedure'), 'getblockchaininfo')
        self.assertEqual(payload.get('result', {}).get('chain'), 'test')
        mock_getblockchaininfo.assert_called_once_with()

    def test_rpc_execute_rejects_non_whitelisted_procedure(self):
        response = self.client.post(
            reverse('rpc_execute'),
            data=json.dumps({'procedure': 'issue', 'params': ['BAD', 1]}),
            content_type='application/json',
            HTTP_X_API_KEY=self.api_key,
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json().get('success'))


class AssetsListTestCase(TestCase):
    """Test assets listing endpoint"""
    
    def setUp(self):
        self.client = Client()
        self.assets_url = reverse('assets_list')
    
    def test_list_assets_accessible(self):
        """Test that assets list endpoint is accessible"""
        # This may fail if RPC is not available, but we test the endpoint exists
        response = self.client.get(self.assets_url)
        # Either success (200) or RPC error (500) is acceptable for MVP
        self.assertIn(response.status_code, [200, 500])


class AssetDetailTestCase(TestCase):
    """Test asset detail endpoint"""
    
    def setUp(self):
        self.client = Client()
    
    def test_asset_detail_accessible(self):
        """Test that asset detail endpoint is accessible"""
        url = reverse('asset_detail', kwargs={'asset_name': 'TEST_ASSET'})
        response = self.client.get(url)
        # Either success (200) or RPC error (500) is acceptable for MVP
        self.assertIn(response.status_code, [200, 500])


class BlockchainInfoTestCase(TestCase):
    """Test blockchain info endpoint"""
    
    def setUp(self):
        self.client = Client()
        self.blockchain_info_url = reverse('blockchain_info')
    
    def test_blockchain_info_accessible(self):
        """Test that blockchain info endpoint is accessible"""
        response = self.client.get(self.blockchain_info_url)
        # Either success (200) or RPC error (500) is acceptable for MVP
        self.assertIn(response.status_code, [200, 500])


class AddressBalanceTestCase(TestCase):
    """Test address balance endpoint"""
    
    def setUp(self):
        self.client = Client()
    
    def test_address_balance_accessible(self):
        """Test that address balance endpoint is accessible"""
        # Use a sample Evrmore address format
        test_address = 'EVRTestAddress123456789'
        url = reverse('address_balance', kwargs={'address': test_address})
        response = self.client.get(url)
        # Either success (200) or RPC error (500) is acceptable for MVP
        self.assertIn(response.status_code, [200, 500])


class ModelTestCase(TestCase):
    """Test model creation and relationships"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
    
    def test_create_solidity_contract(self):
        """Test creating a SolidityContract model instance"""
        contract = SolidityContract.objects.create(
            name='Test Contract',
            contract_address='TEST_ASSET',
            deployer=self.user,
            description='Test description'
        )
        
        self.assertEqual(contract.name, 'Test Contract')
        self.assertEqual(contract.deployer, self.user)
        self.assertTrue(contract.is_active)
        self.assertIsNotNone(contract.created_at)
    
    def test_create_contract_interaction(self):
        """Test creating a ContractInteraction model instance"""
        contract = SolidityContract.objects.create(
            name='Test Contract',
            contract_address='TEST_ASSET',
            deployer=self.user
        )
        
        interaction = ContractInteraction.objects.create(
            contract=contract,
            user=self.user,
            function_name='testFunction',
            parameters={'param1': 'value1'},
            success=True
        )
        
        self.assertEqual(interaction.contract, contract)
        self.assertEqual(interaction.user, self.user)
        self.assertEqual(interaction.function_name, 'testFunction')
        self.assertTrue(interaction.success)
    
    def test_create_contract_asset(self):
        """Test creating a ContractAsset model instance"""
        contract = SolidityContract.objects.create(
            name='Test Contract',
            contract_address='TEST_ASSET',
            deployer=self.user
        )
        
        asset = ContractAsset.objects.create(
            contract=contract,
            asset_name='TEST_TOKEN',
            quantity=1000,
            units=2,
            reissuable=True
        )
        
        self.assertEqual(asset.contract, contract)
        self.assertEqual(asset.asset_name, 'TEST_TOKEN')
        self.assertEqual(asset.quantity, 1000)
    
    def test_contract_string_representation(self):
        """Test string representation of models"""
        contract = SolidityContract.objects.create(
            name='Test Contract',
            contract_address='TEST_ASSET',
            deployer=self.user
        )
        
        self.assertEqual(str(contract), 'Test Contract (TEST_ASSET)')
    
    def test_contract_asset_unique_together(self):
        """Test that contract and asset_name combination is unique"""
        contract = SolidityContract.objects.create(
            name='Test Contract',
            contract_address='TEST_ASSET',
            deployer=self.user
        )
        
        ContractAsset.objects.create(
            contract=contract,
            asset_name='UNIQUE_TOKEN',
            quantity=100
        )
        
        # Try to create duplicate - should raise error
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            ContractAsset.objects.create(
                contract=contract,
                asset_name='UNIQUE_TOKEN',
                quantity=200
            )


class StrictMessageChannelTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='strict-api-user', password='testpass123')
        self.api_key = 'strict-api-key'
        APIKey.objects.create(
            user=self.user,
            name='Strict channel key',
            key_hash=APIKey.hash_key(self.api_key),
            key_prefix=self.api_key[:8],
        )
        self.system_user = User.objects.create_user(username='system', password='unused')
        self.admin_user = User.objects.create_user(username='admin', password='unused')
        self.policy = MessageChannelPolicy.objects.create(
            channel_key='atomic_swap_transfer',
            channel_name='MSG.SWAP.TRANSFER',
            network_mode='testnet',
            version=1,
            status='active',
            owner_account=self.system_user,
            manager_account=self.admin_user,
            schema_name='defitome.atomic-swap-transfer-message',
            schema_version=1,
            allowed_stages=['offer_created', 'settlement_lock_created'],
            strict_rules={
                'console_mode': 'strict',
                'immutable_payload': True,
                'allow_unregistered_keys': False,
            },
            is_locked=True,
        )

    def test_send_message_endpoint_is_locked_down(self):
        payload = {
            'event_type': 'atomic_swap_transfer',
            'event_version': 1,
            'event_id': 'evt-1',
            'created_at': '2026-08-07T00:00:00+00:00',
            'network_mode': 'testnet',
            'swap_offer_id': 42,
            'stage': 'offer_created',
            'initiator': 'seller',
            'counterparty': 'buyer',
            'offer_token': 'COLLECTIBLE#001',
            'offer_amount': '1',
            'request_token': 'EVR',
            'request_amount': '2',
            'txid': '',
            'details': {'actor': 'seller'},
        }

        response = self.client.post(
            reverse('send_message'),
            data=json.dumps({
                'channel_key': 'atomic_swap_transfer',
                'network_mode': 'testnet',
                'payload': payload,
            }),
            content_type='application/json',
            HTTP_X_API_KEY=self.api_key,
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json().get('success'))

    def test_send_message_endpoint_remains_locked_down_for_invalid_stage_payloads(self):
        payload = {
            'event_type': 'atomic_swap_transfer',
            'event_version': 1,
            'event_id': 'evt-2',
            'created_at': '2026-08-07T00:00:00+00:00',
            'network_mode': 'testnet',
            'swap_offer_id': 43,
            'stage': 'settlement_broadcasted',
            'initiator': 'seller',
            'counterparty': 'buyer',
            'offer_token': 'COLLECTIBLE#001',
            'offer_amount': '1',
            'request_token': 'EVR',
            'request_amount': '2',
            'txid': '',
            'details': {'actor': 'seller'},
        }

        response = self.client.post(
            reverse('send_message'),
            data=json.dumps({
                'channel_key': 'atomic_swap_transfer',
                'network_mode': 'testnet',
                'payload': payload,
            }),
            content_type='application/json',
            HTTP_X_API_KEY=self.api_key,
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json().get('success'))


class ChannelConsoleAssetWorkflowTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='admin-channel-user',
            password='testpass123',
            is_staff=True,
        )
        self.api_key = 'admin-channel-api-key'
        APIKey.objects.create(
            user=self.user,
            name='Channel console key',
            key_hash=APIKey.hash_key(self.api_key),
            key_prefix=self.api_key[:8],
        )

    def test_create_channel_console_asset_endpoint_is_locked_down(self):
        response = self.client.post(
            reverse('create_channel_console_asset'),
            data=json.dumps({
                'admin_asset': 'ROOT!',
                'channel_tag': 'SWAPFLOW',
                'channel_key': 'root_swapflow_console',
                'metadata': {
                    'description': 'Atomic swap transfer console',
                    'allowed_stages': ['offer_created', 'settlement_lock_created'],
                    'strict_rules': {
                        'console_mode': 'strict',
                        'immutable_payload': True,
                        'allow_unregistered_keys': False,
                    },
                },
            }),
            content_type='application/json',
            HTTP_X_API_KEY=self.api_key,
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json().get('success'))

    def test_scan_channel_console_assets_endpoint_is_locked_down(self):
        response = self.client.get(reverse('scan_channel_console_assets'))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json().get('success'))


class ChannelConsoleServiceTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='service-admin',
            password='testpass123',
            is_staff=True,
        )
        self.user_wallet = UserWallet.objects.create(
            user=self.user,
            name='Service Wallet',
            entropy='service-entropy',
            passphrase='',
        )
        WalletAddress.objects.create(
            wallet=self.user_wallet,
            network_mode='testnet',
            address='EAddrOne',
            wif='L1one',
            account=0,
            index=0,
            is_change=False,
        )
        WalletAddress.objects.create(
            wallet=self.user_wallet,
            network_mode='testnet',
            address='EAddrTwo',
            wif='L1two',
            account=0,
            index=1,
            is_change=True,
        )

    @patch('API.channel_console_service.evrmore_rpc.list_assets', side_effect=ConnectionError('node unavailable'))
    def test_channel_scan_reports_rpc_failure(self, _mock_list_assets):
        from API.channel_console_service import scan_channel_console_assets

        result = scan_channel_console_assets(count=50, start=0)

        self.assertEqual(result['scan_error'], 'node unavailable')
        self.assertEqual(result['scanned_count'], 0)
        self.assertFalse(result['has_more'])

    @patch('API.channel_console_service.KuboAPIUploader.download_json')
    @patch('API.channel_console_service.evrmore_rpc.get_asset_data')
    @patch('API.channel_console_service.evrmore_rpc.list_assets')
    def test_channel_scan_returns_valid_console_and_pagination(
        self,
        mock_list_assets,
        mock_get_asset_data,
        mock_download_json,
    ):
        from API.channel_console_service import scan_channel_console_assets

        mock_list_assets.return_value = ['ROOT~OPS']
        mock_get_asset_data.return_value = {'ipfs_hash': 'QmConsoleCid'}
        mock_download_json.return_value = {
            'schema': 'defitome.messaging-channel-console',
            'version': 1,
            'asset_name': 'ROOT~OPS',
            'channel_key': 'root_ops_console',
            'channel_name': 'ROOT~OPS',
            'allowed_stages': ['offer_created', 'market_created', 'order_created'],
            'strict_rules': {'console_mode': 'strict'},
            'console_type': 'dex_events',
        }

        result = scan_channel_console_assets(count=1, start=4)

        self.assertEqual(result['valid_channels'][0]['asset_name'], 'ROOT~OPS')
        self.assertEqual(result['next_start'], 5)
        self.assertTrue(result['has_more'])

    @patch('API.channel_console_service.evrmore_rpc.view_all_message_channels', return_value=['ROOT~OPS'])
    @patch('API.channel_console_service.get_active_rpc_endpoint_mode', return_value='local')
    @patch('API.channel_console_service.KuboAPIUploader.download_json')
    @patch('API.channel_console_service.evrmore_rpc.get_asset_data', return_value={'ipfs_hash': 'QmConsoleCid'})
    @patch('API.channel_console_service.evrmore_rpc.list_assets', return_value=[])
    def test_verified_scan_backfills_metadata_for_every_policy_version(
        self,
        _mock_list_assets,
        _mock_get_asset_data,
        mock_download_json,
        _mock_endpoint_mode,
        _mock_subscriptions,
    ):
        from API.channel_console_service import scan_channel_console_assets

        for version in (1, 2):
            MessageChannelPolicy.objects.create(
                channel_key='root_ops_console',
                channel_name='ROOT~OPS',
                network_mode='testnet',
                version=version,
                status='active' if version == 2 else 'deprecated',
                owner_account=self.user,
                manager_account=self.user,
                allowed_stages=['offer_created'],
            )
        mock_download_json.return_value = {
            'schema': 'defitome.messaging-channel-console',
            'version': 1,
            'asset_name': 'ROOT~OPS',
            'channel_key': 'root_ops_console',
            'channel_name': 'ROOT~OPS',
            'allowed_stages': ['offer_created'],
            'strict_rules': {'console_mode': 'strict'},
        }

        result = scan_channel_console_assets(network_mode='testnet')

        self.assertTrue(result['valid_channels'][0]['is_subscribed'])
        policies = MessageChannelPolicy.objects.filter(channel_name='ROOT~OPS')
        self.assertEqual(policies.filter(metadata_ipfs_cid='QmConsoleCid').count(), 2)
        self.assertEqual(
            policies.filter(chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED).count(),
            2,
        )

    @patch('API.channel_console_service.evrmore_rpc.view_all_message_channels')
    @patch('API.channel_console_service.get_active_rpc_endpoint_mode', return_value='public')
    @patch('API.channel_console_service.KuboAPIUploader.download_json')
    @patch('API.channel_console_service.evrmore_rpc.get_asset_data', return_value={'ipfs_hash': 'QmConsoleCid'})
    @patch('API.channel_console_service.evrmore_rpc.list_assets', return_value=['ROOT~OPS'])
    def test_public_channel_scan_skips_node_local_subscription_rpc(
        self,
        _mock_list_assets,
        _mock_get_asset_data,
        mock_download_json,
        _mock_endpoint_mode,
        mock_subscriptions,
    ):
        from API.channel_console_service import scan_channel_console_assets

        mock_download_json.return_value = {
            'schema': 'defitome.messaging-channel-console',
            'version': 1,
            'asset_name': 'ROOT~OPS',
            'channel_key': 'root_ops_console',
            'channel_name': 'ROOT~OPS',
            'allowed_stages': ['offer_created'],
            'strict_rules': {'console_mode': 'strict'},
        }

        result = scan_channel_console_assets(network_mode='testnet')

        mock_subscriptions.assert_not_called()
        self.assertFalse(result['subscription_state_available'])
        self.assertIsNone(result['valid_channels'][0]['is_subscribed'])

    @patch('API.channel_console_service.evrmore_rpc.subscribe_to_channel', return_value=[])
    @patch('API.channel_console_service.KuboAPIUploader.download_json')
    @patch('API.channel_console_service.evrmore_rpc.get_asset_data', return_value={'ipfs_hash': 'QmConsoleCid'})
    def test_subscription_requires_and_uses_verified_channel_metadata(
        self,
        _mock_get_asset_data,
        mock_download_json,
        mock_subscribe,
    ):
        mock_download_json.return_value = {
            'schema': 'defitome.messaging-channel-console',
            'version': 1,
            'asset_name': 'ROOT~OPS',
            'channel_key': 'root_ops_console',
            'channel_name': 'ROOT~OPS',
            'allowed_stages': ['offer_created'],
            'strict_rules': {'console_mode': 'strict'},
        }

        result = set_channel_subscription('root~ops', True, network_mode='testnet')

        self.assertTrue(result['subscribed'])
        mock_subscribe.assert_called_once_with('ROOT~OPS')

    @patch('API.channel_console_service.evrmore_rpc.subscribe_to_channel')
    @patch('API.channel_console_service.evrmore_rpc.get_asset_data', return_value={})
    def test_subscription_rejects_channel_without_chain_metadata(self, _mock_get_asset_data, mock_subscribe):
        with self.assertRaisesMessage(ValueError, 'was not found'):
            set_channel_subscription('ROOT~OPS', True, network_mode='testnet')

        mock_subscribe.assert_not_called()

    @patch('API.channel_console_service.create_and_send_asset_transfer_transaction', return_value={'txid': 'burn-txid'})
    @patch('API.channel_console_service.RPC.getburnaddresses', return_value={'global_burn_address': 'n1BurnAddress'})
    @patch('API.channel_console_service.evrmore_rpc.list_asset_balances_by_address', return_value={'ROOT~OPS': Decimal('1')})
    def test_revision_burn_uses_raw_transfer_and_source_preserving_change(
        self,
        _mock_balances,
        _mock_burn_addresses,
        mock_raw_transfer,
    ):
        policy = MessageChannelPolicy.objects.create(
            channel_key='root_ops_console',
            channel_name='ROOT~OPS',
            network_mode='testnet',
            version=1,
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=['offer_created'],
        )

        result = burn_channel_asset_for_revision(self.user, 'ROOT~OPS', network_mode='testnet')

        self.assertEqual(result['txid'], 'burn-txid')
        mock_raw_transfer.assert_called_once_with(
            from_address='EAddrOne',
            to_address='n1BurnAddress',
            asset_name='ROOT~OPS',
            asset_quantity=Decimal('1'),
            change_address='EAddrOne',
            asset_change_address='EAddrOne',
            wif_keys=['L1one'],
        )
        policy.refresh_from_db()
        self.assertEqual(policy.status, 'deprecated')
        self.assertEqual(policy.revision_burn_txid, 'burn-txid')
        self.assertIsNotNone(policy.revision_burned_at)

    @patch('API.channel_console_service.KuboAPIUploader.download_json')
    @patch('API.channel_console_service.evrmore_rpc.get_asset_data')
    @patch('API.channel_console_service.evrmore_rpc.list_assets', return_value=['ROOT~PERMANENT'])
    def test_channel_scan_accepts_permanent_ipfs_hash_field(
        self,
        _mock_list_assets,
        mock_get_asset_data,
        mock_download_json,
    ):
        from API.channel_console_service import scan_channel_console_assets

        mock_get_asset_data.return_value = {'permanent_ipfs_hash': 'QmPermanentCid'}
        mock_download_json.return_value = {
            'schema': 'defitome.messaging-channel-console',
            'version': 1,
            'asset_name': 'ROOT~PERMANENT',
            'channel_key': 'permanent_console',
            'channel_name': 'ROOT~PERMANENT',
            'allowed_stages': ['offer_created'],
            'strict_rules': {'console_mode': 'strict'},
        }

        result = scan_channel_console_assets(network_mode='testnet')

        self.assertEqual(result['valid_channels'][0]['ipfs_cid'], 'QmPermanentCid')

    @patch('API.channel_console_service.evrmore_rpc.get_asset_data', return_value=None)
    @patch('API.channel_console_service.evrmore_rpc.list_assets', return_value=[])
    def test_channel_scan_keeps_unconfirmed_issuance_pending(self, _mock_list_assets, _mock_get_asset_data):
        from API.channel_console_service import scan_channel_console_assets

        policy = MessageChannelPolicy.objects.create(
            channel_key='pending_console',
            channel_name='ROOT~PENDING',
            network_mode='testnet',
            version=1,
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=['offer_created'],
            metadata_ipfs_cid='QmPendingCid',
            issuance_txid='pending-txid',
        )

        result = scan_channel_console_assets(network_mode='testnet')

        self.assertEqual(result['pending_channels'][0]['asset_name'], 'ROOT~PENDING')
        self.assertFalse(result['invalid_channels'])
        policy.refresh_from_db()
        self.assertEqual(policy.chain_metadata_status, MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING)

    @patch('API.channel_console_service.KuboAPIUploader.download_json')
    @patch('API.channel_console_service.evrmore_rpc.get_asset_data')
    @patch('API.channel_console_service.evrmore_rpc.list_assets', return_value=[])
    @patch('API.channel_console_service.get_current_network_mode', return_value='testnet')
    def test_channel_scan_includes_configured_policy_when_middle_wildcard_returns_empty(
        self,
        _mock_network_mode,
        _mock_list_assets,
        mock_get_asset_data,
        mock_download_json,
    ):
        from API.channel_console_service import scan_channel_console_assets

        MessageChannelPolicy.objects.create(
            channel_key='configured_console',
            channel_name='ROOT~CONFIGURED',
            network_mode='testnet',
            version=1,
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=['offer_created'],
        )
        mock_get_asset_data.return_value = {'ipfs_hash': 'QmConfiguredCid'}
        mock_download_json.return_value = {
            'schema': 'defitome.messaging-channel-console',
            'version': 1,
            'asset_name': 'ROOT~CONFIGURED',
            'channel_key': 'configured_console',
            'channel_name': 'ROOT~CONFIGURED',
            'allowed_stages': ['offer_created'],
            'strict_rules': {'console_mode': 'strict'},
        }

        result = scan_channel_console_assets(asset_pattern='*~*', count=50, start=0)

        self.assertEqual(result['valid_channels'][0]['asset_name'], 'ROOT~CONFIGURED')

    @patch('API.channel_console_service.create_and_send_issue_asset_transaction', return_value={'txid': 'txid-1'})
    @patch('API.channel_console_service.KuboAPIUploader.upload_bytes')
    @patch('API.channel_console_service.evrmore_rpc.list_asset_balances_by_address')
    def test_create_channel_aggregates_balances_and_auto_increments_policy_version(
        self,
        mock_balances,
        mock_upload_bytes,
        _mock_issue_asset,
    ):
        class UploadResult:
            def __init__(self, cid):
                self.cid = cid

        mock_upload_bytes.return_value = UploadResult('QmMetaCid')

        def _balance_side_effect(address):
            if address == 'EAddrOne':
                return {'ROOT!': Decimal('0.5')}
            if address == 'EAddrTwo':
                return {'ROOT!': Decimal('0.75')}
            return {}

        mock_balances.side_effect = _balance_side_effect

        payload = {
            'admin_asset': 'ROOT!',
            'channel_tag': 'OPS',
            'channel_key': 'root_ops_console',
            'network_mode': 'testnet',
            'metadata': {
                'allowed_stages': ['offer_created'],
                'strict_rules': {
                    'console_mode': 'strict',
                    'immutable_payload': True,
                    'allow_unregistered_keys': False,
                },
            },
        }

        first = create_channel_console_asset_for_user(self.user, payload)
        second = create_channel_console_asset_for_user(self.user, payload)

        self.assertEqual(first['channel_policy']['version'], 1)
        self.assertEqual(second['channel_policy']['version'], 2)

        active = MessageChannelPolicy.objects.get(
            channel_key='root_ops_console',
            network_mode='testnet',
            version=2,
        )
        old = MessageChannelPolicy.objects.get(
            channel_key='root_ops_console',
            network_mode='testnet',
            version=1,
        )
        self.assertEqual(active.status, 'active')
        self.assertEqual(old.status, 'deprecated')
        self.assertCountEqual(first['owned_addresses'], ['EAddrOne', 'EAddrTwo'])

    @patch('API.channel_console_service.create_and_send_issue_asset_transaction', return_value={'txid': 'txid-raw-1'})
    @patch('API.channel_console_service.KuboAPIUploader.upload_bytes')
    @patch('API.channel_console_service.evrmore_rpc.list_asset_balances_by_address', return_value={'ROOT!': Decimal('1')})
    def test_create_channel_uses_manual_issue_transaction_helper(
        self,
        _mock_balances,
        mock_upload_bytes,
        mock_issue_helper,
    ):
        class UploadResult:
            def __init__(self, cid):
                self.cid = cid

        mock_upload_bytes.return_value = UploadResult('QmMetaCid')

        result = create_channel_console_asset_for_user(self.user, {
            'admin_asset': 'ROOT!',
            'channel_tag': 'OPS',
            'channel_key': 'root_ops_console_manual',
            'network_mode': 'testnet',
            'metadata': {
                'allowed_stages': ['offer_created'],
                'strict_rules': {'console_mode': 'strict'},
            },
        })

        self.assertEqual(result['txid'], 'txid-raw-1')
        policy = MessageChannelPolicy.objects.get(channel_key='root_ops_console_manual')
        self.assertEqual(policy.metadata_ipfs_cid, 'QmMetaCid')
        self.assertEqual(policy.issuance_txid, 'txid-raw-1')
        self.assertEqual(policy.chain_metadata_status, MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING)
        mock_issue_helper.assert_called_once_with(
            from_address='EAddrOne',
            issuer_address='EAddrOne',
            asset_name='ROOT~OPS',
            asset_quantity=Decimal('1'),
            units=0,
            reissuable=False,
            has_ipfs=True,
            ipfs_hash='QmMetaCid',
            wif_keys=['L1one'],
            owner_change_address='EAddrOne',
        )

    @patch('API.channel_console_service.create_and_send_issue_asset_transaction', return_value={'txid': 'v5-issue-txid'})
    @patch('API.channel_console_service.KuboAPIUploader.upload_bytes')
    @patch('API.channel_console_service.evrmore_rpc.list_asset_balances_by_address', return_value={'TOME0808!': Decimal('1')})
    def test_unified_v5_issuance_keeps_v4_active_until_chain_metadata_is_verified(
        self,
        _mock_balances,
        mock_upload_bytes,
        mock_issue_asset,
    ):
        class UploadResult:
            cid = 'QmUnifiedV5Metadata'

        v4 = MessageChannelPolicy.objects.create(
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            channel_name='TOME0808~SWAPFLOWV4',
            network_mode='testnet',
            version=4,
            status='active',
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )
        mock_upload_bytes.return_value = UploadResult()

        result = create_channel_console_asset_for_user(self.user, {
            'admin_asset': 'TOME0808!',
            'channel_tag': UNIFIED_WORKFLOW_CHANNEL_TAG,
            'channel_key': UNIFIED_WORKFLOW_CHANNEL_KEY,
            'channel_version': UNIFIED_WORKFLOW_POLICY_VERSION,
            'network_mode': 'testnet',
            'metadata': {'strict_rules': {'allow_unregistered_keys': True}},
        })

        v4.refresh_from_db()
        v5 = MessageChannelPolicy.objects.get(
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            network_mode='testnet',
            version=UNIFIED_WORKFLOW_POLICY_VERSION,
        )
        metadata = json.loads(mock_upload_bytes.call_args.args[0].decode('utf-8'))

        self.assertEqual(result['channel_asset_name'], 'TOME0808~SWAPFLOWV5')
        self.assertEqual(v4.status, 'active')
        self.assertEqual(v5.status, 'draft')
        self.assertEqual(v5.chain_metadata_status, MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING)
        self.assertEqual(v5.strict_rules['reconciliation_source'], 'channel_asset_lineage')
        self.assertEqual(metadata['allowed_stages'], DEFAULT_ALLOWED_STAGES)
        self.assertEqual(metadata['strict_rules']['allow_unregistered_keys'], False)
        mock_issue_asset.assert_called_once_with(
            from_address='EAddrOne',
            issuer_address='EAddrOne',
            asset_name='TOME0808~SWAPFLOWV5',
            asset_quantity=Decimal('1'),
            units=0,
            reissuable=False,
            has_ipfs=True,
            ipfs_hash='QmUnifiedV5Metadata',
            wif_keys=['L1one'],
            owner_change_address='EAddrOne',
        )

    @patch('API.channel_console_service.create_and_send_issue_asset_transaction')
    @patch('API.channel_console_service.KuboAPIUploader.download_json')
    @patch('API.channel_console_service.evrmore_rpc.get_asset_data', return_value={'ipfs_hash': 'QmUnifiedV5Metadata'})
    @patch('API.channel_console_service.evrmore_rpc.list_asset_balances_by_address', return_value={'TOME0808!': Decimal('1')})
    def test_reusing_unified_v5_draft_preserves_issuance_transaction_id(
        self,
        _mock_balances,
        _mock_asset_data,
        mock_download_json,
        mock_issue_asset,
    ):
        MessageChannelPolicy.objects.create(
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            channel_name='TOME0808~SWAPFLOWV5',
            network_mode='testnet',
            version=UNIFIED_WORKFLOW_POLICY_VERSION,
            status='draft',
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            strict_rules=dict(UNIFIED_WORKFLOW_STRICT_RULES),
            metadata_ipfs_cid='QmUnifiedV5Metadata',
            issuance_txid='v5-issue-txid',
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING,
        )
        mock_download_json.return_value = {
            'schema': 'defitome.messaging-channel-console',
            'version': 1,
            'asset_name': 'TOME0808~SWAPFLOWV5',
            'channel_key': UNIFIED_WORKFLOW_CHANNEL_KEY,
            'channel_name': 'DeFiTome Unified v5 Console',
            'allowed_stages': list(DEFAULT_ALLOWED_STAGES),
            'strict_rules': dict(UNIFIED_WORKFLOW_STRICT_RULES),
            'console_type': 'defitome_workflow_event',
        }

        result = create_channel_console_asset_for_user(self.user, {
            'admin_asset': 'TOME0808!',
            'channel_tag': UNIFIED_WORKFLOW_CHANNEL_TAG,
            'channel_key': UNIFIED_WORKFLOW_CHANNEL_KEY,
            'channel_version': UNIFIED_WORKFLOW_POLICY_VERSION,
            'network_mode': 'testnet',
        })

        policy = MessageChannelPolicy.objects.get(
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            network_mode='testnet',
            version=UNIFIED_WORKFLOW_POLICY_VERSION,
        )
        self.assertEqual(policy.issuance_txid, 'v5-issue-txid')
        self.assertEqual(result['txid'], 'v5-issue-txid')
        mock_issue_asset.assert_not_called()

    @patch('API.channel_console_service.create_and_send_issue_asset_transaction')
    @patch('API.channel_console_service.KuboAPIUploader.upload_bytes')
    @patch('API.channel_console_service.evrmore_rpc.get_asset_data', return_value=None)
    def test_pending_unified_v5_draft_is_not_issued_twice_before_rpc_confirmation(
        self,
        _mock_asset_data,
        mock_upload_bytes,
        mock_issue_asset,
    ):
        MessageChannelPolicy.objects.create(
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            channel_name='TOME0808~SWAPFLOWV5',
            network_mode='testnet',
            version=UNIFIED_WORKFLOW_POLICY_VERSION,
            status='draft',
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            strict_rules=dict(UNIFIED_WORKFLOW_STRICT_RULES),
            metadata_ipfs_cid='QmUnifiedV5Metadata',
            issuance_txid='v5-issue-txid',
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING,
        )

        result = create_channel_console_asset_for_user(self.user, {
            'admin_asset': 'TOME0808!',
            'channel_tag': UNIFIED_WORKFLOW_CHANNEL_TAG,
            'channel_key': UNIFIED_WORKFLOW_CHANNEL_KEY,
            'channel_version': UNIFIED_WORKFLOW_POLICY_VERSION,
            'network_mode': 'testnet',
        })

        self.assertTrue(result['issuance_pending'])
        self.assertEqual(result['txid'], 'v5-issue-txid')
        mock_upload_bytes.assert_not_called()
        mock_issue_asset.assert_not_called()

    @patch('API.channel_console_service.KuboAPIUploader.download_json')
    @patch('API.channel_console_service.evrmore_rpc.get_asset_data', return_value={'ipfs_hash': 'QmUnifiedV5Metadata'})
    def test_bootstrap_promotes_only_verified_unified_v5_policy(
        self,
        _mock_asset_data,
        mock_download_json,
    ):
        v4 = MessageChannelPolicy.objects.create(
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            channel_name='TOME0808~SWAPFLOWV4',
            network_mode='testnet',
            version=4,
            status='active',
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )
        mock_download_json.return_value = {
            'schema': 'defitome.messaging-channel-console',
            'version': 1,
            'asset_name': 'TOME0808~SWAPFLOWV5',
            'channel_key': UNIFIED_WORKFLOW_CHANNEL_KEY,
            'channel_name': 'DeFiTome Unified v5 Console',
            'allowed_stages': list(DEFAULT_ALLOWED_STAGES),
            'strict_rules': dict(UNIFIED_WORKFLOW_STRICT_RULES),
            'console_type': 'defitome_workflow_event',
        }

        call_command(
            'bootstrap_message_channels',
            channel_name='TOME0808~SWAPFLOWV5',
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            policy_version=UNIFIED_WORKFLOW_POLICY_VERSION,
            network_mode='testnet',
            stdout=StringIO(),
        )

        v4.refresh_from_db()
        v5 = MessageChannelPolicy.objects.get(
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            network_mode='testnet',
            version=UNIFIED_WORKFLOW_POLICY_VERSION,
        )
        self.assertEqual(v4.status, 'deprecated')
        self.assertEqual(v5.status, 'active')
        self.assertEqual(v5.chain_metadata_status, MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED)
        self.assertEqual(v5.strict_rules, dict(UNIFIED_WORKFLOW_STRICT_RULES))

    def test_bootstrap_rejects_auto_broadcast_for_unified_v5(self):
        with self.assertRaisesMessage(CommandError, 'Unified workflow v5 requires auto_broadcast to remain false'):
            call_command(
                'bootstrap_message_channels',
                channel_name='TOME0808~SWAPFLOWV5',
                channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
                policy_version=UNIFIED_WORKFLOW_POLICY_VERSION,
                network_mode='testnet',
                auto_broadcast=True,
                stdout=StringIO(),
            )

    @patch('API.channel_console_service.KuboAPIUploader.download_json')
    @patch('API.channel_console_service.evrmore_rpc.get_asset_data', return_value={'ipfs_hash': 'QmUnifiedV5Metadata'})
    def test_unified_v5_metadata_rejections_leave_v5_pending(
        self,
        _mock_asset_data,
        mock_download_json,
    ):
        v4 = MessageChannelPolicy.objects.create(
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            channel_name='TOME0808~SWAPFLOWV4',
            network_mode='testnet',
            version=4,
            status='active',
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )
        v5 = MessageChannelPolicy.objects.create(
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            channel_name='TOME0808~SWAPFLOWV5',
            network_mode='testnet',
            version=UNIFIED_WORKFLOW_POLICY_VERSION,
            status='draft',
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            strict_rules=dict(UNIFIED_WORKFLOW_STRICT_RULES),
            metadata_ipfs_cid='QmUnifiedV5Metadata',
            issuance_txid='a' * 64,
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING,
        )
        missing_rule = dict(UNIFIED_WORKFLOW_STRICT_RULES)
        missing_rule.pop('reconciliation_fail_closed')
        cases = (
            (
                'TOME0808~WRONGTAG',
                list(DEFAULT_ALLOWED_STAGES),
                dict(UNIFIED_WORKFLOW_STRICT_RULES),
                'must use asset tag',
            ),
            (
                'TOME0808~SWAPFLOWV5',
                list(DEFAULT_ALLOWED_STAGES[:-1]),
                dict(UNIFIED_WORKFLOW_STRICT_RULES),
                'complete lifecycle stage set',
            ),
            (
                'TOME0808~SWAPFLOWV5',
                list(DEFAULT_ALLOWED_STAGES),
                missing_rule,
                "requires strict rule 'reconciliation_fail_closed'",
            ),
        )

        for asset_name, allowed_stages, strict_rules, expected_error in cases:
            with self.subTest(asset_name=asset_name, expected_error=expected_error):
                v5.channel_name = asset_name
                v5.save(update_fields=['channel_name', 'updated_at'])
                mock_download_json.return_value = {
                    'schema': 'defitome.messaging-channel-console',
                    'version': 1,
                    'asset_name': asset_name,
                    'channel_key': UNIFIED_WORKFLOW_CHANNEL_KEY,
                    'channel_name': 'DeFiTome Unified v5 Console',
                    'allowed_stages': allowed_stages,
                    'strict_rules': strict_rules,
                    'console_type': 'defitome_workflow_event',
                }

                with self.assertRaisesMessage(ValueError, expected_error):
                    validate_channel_console_asset(asset_name, network_mode='testnet')

                v4.refresh_from_db()
                v5.refresh_from_db()
                self.assertEqual(v4.status, 'active')
                self.assertEqual(v5.status, 'draft')
                self.assertEqual(
                    v5.chain_metadata_status,
                    MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING,
                )

    @patch('API.channel_console_service.KuboAPIUploader.download_json')
    @patch('API.channel_console_service.evrmore_rpc.get_asset_data')
    @patch('API.channel_console_service.evrmore_rpc.list_assets', return_value=[])
    def test_verified_unified_v5_scan_promotes_v5_and_deprecates_v4(
        self,
        _mock_list_assets,
        mock_get_asset_data,
        mock_download_json,
    ):
        v4 = MessageChannelPolicy.objects.create(
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            channel_name='TOME0808~SWAPFLOWV4',
            network_mode='testnet',
            version=4,
            status='active',
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )
        v5 = MessageChannelPolicy.objects.create(
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            channel_name='TOME0808~SWAPFLOWV5',
            network_mode='testnet',
            version=UNIFIED_WORKFLOW_POLICY_VERSION,
            status='draft',
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            strict_rules=dict(UNIFIED_WORKFLOW_STRICT_RULES),
            metadata_ipfs_cid='QmUnifiedV5Metadata',
            issuance_txid='a' * 64,
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING,
        )
        mock_get_asset_data.side_effect = lambda asset_name: (
            {'ipfs_hash': 'QmUnifiedV5Metadata'}
            if asset_name == 'TOME0808~SWAPFLOWV5'
            else {}
        )
        mock_download_json.return_value = {
            'schema': 'defitome.messaging-channel-console',
            'version': 1,
            'asset_name': 'TOME0808~SWAPFLOWV5',
            'channel_key': UNIFIED_WORKFLOW_CHANNEL_KEY,
            'channel_name': 'DeFiTome Unified v5 Console',
            'description': 'Unified v5 channel',
            'allowed_stages': list(DEFAULT_ALLOWED_STAGES),
            'strict_rules': dict(UNIFIED_WORKFLOW_STRICT_RULES),
            'console_type': 'defitome_workflow_event',
        }

        from API.channel_console_service import scan_channel_console_assets

        scan_channel_console_assets(network_mode='testnet')

        v4.refresh_from_db()
        v5.refresh_from_db()
        self.assertEqual(v4.status, 'deprecated')
        self.assertEqual(v5.status, 'active')
        self.assertEqual(v5.chain_metadata_status, MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED)

    @patch('API.channel_console_service.create_and_send_issue_asset_transaction', return_value={'txid': 'txid-1'})
    @patch('API.channel_console_service.KuboAPIUploader.upload_bytes')
    @patch('API.channel_console_service.evrmore_rpc.list_asset_balances_by_address', return_value={'ROOT!': Decimal('1')})
    def test_create_channel_rejects_custom_qty_for_one_of_one_channel_assets(
        self,
        _mock_balances,
        mock_upload_bytes,
        _mock_issue_asset,
    ):
        class UploadResult:
            def __init__(self, cid):
                self.cid = cid

        mock_upload_bytes.return_value = UploadResult('QmMetaCid')

        with self.assertRaisesMessage(ValueError, 'Messaging channel assets are fixed at quantity 1'):
            create_channel_console_asset_for_user(self.user, {
                'admin_asset': 'ROOT!',
                'channel_tag': 'OPS',
                'channel_key': 'root_ops_console',
                'network_mode': 'testnet',
                'qty': '2',
                'metadata': {
                    'allowed_stages': ['offer_created'],
                    'strict_rules': {'console_mode': 'strict'},
                },
            })

    @patch('API.channel_console_service.evrmore_rpc.list_asset_balances_by_address', return_value={'ROOT!': Decimal('1')})
    def test_create_channel_rejects_invalid_channel_asset_name(self, _mock_balances):
        with self.assertRaisesMessage(ValueError, 'channel asset name exceeds max length'):
            create_channel_console_asset_for_user(self.user, {
                'admin_asset': 'THISISAREALLYLONGROOTASSETNAME!',
                'channel_tag': 'OPS',
                'channel_key': 'root_ops_console',
                'network_mode': 'testnet',
                'metadata': {
                    'allowed_stages': ['offer_created'],
                    'strict_rules': {'console_mode': 'strict'},
                },
            })

    @patch('API.channel_console_service.evrmore_rpc.list_asset_balances_by_address', return_value={'ROOT!': Decimal('1')})
    @patch('API.channel_console_service.evrmore_rpc.get_asset_data', return_value={'ipfs_hash': 'QmExistingCid'})
    @patch('API.channel_console_service.KuboAPIUploader.download_json', return_value={
        'schema': 'defitome.messaging-channel-console',
        'version': 1,
        'asset_name': 'ROOT~OPS',
        'channel_key': 'different_key',
        'channel_name': 'ROOT~OPS',
        'description': '',
        'allowed_stages': ['offer_created'],
        'strict_rules': {'console_mode': 'strict'},
        'console_type': 'atomic_swap_transfer',
    })
    def test_create_channel_rejects_existing_onchain_asset_bound_to_different_key(
        self,
        _mock_download_json,
        _mock_get_asset_data,
        _mock_balances,
    ):
        with self.assertRaisesMessage(ValueError, 'bound to a different channel key'):
            create_channel_console_asset_for_user(self.user, {
                'admin_asset': 'ROOT!',
                'channel_tag': 'OPS',
                'channel_key': 'root_ops_console',
                'network_mode': 'testnet',
                'metadata': {
                    'allowed_stages': ['offer_created'],
                    'strict_rules': {'console_mode': 'strict'},
                },
            })

