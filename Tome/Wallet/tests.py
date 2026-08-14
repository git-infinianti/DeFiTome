from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.messages import get_messages

from Wallet import rpc, views
from Wallet.context_processors import wallet_balance
from Wallet.asset_units import get_asset_units
from Wallet.asset_creation import build_asset_operation, create_asset_for_user
from Wallet.models import AssetCreationRequest, TrackedAsset, TrackedAssetHolding, UserWallet, WalletAddress, WalletProfile, WalletPreferences
from API.models import MessageChannelPolicy
from API.channel_console_lib import DEFAULT_ALLOWED_STAGES
from Listings.models import DecPokerGameInstance, DecPokerHand
from Wallet.rip10 import (
    RIP10ValidationError,
    asset_matches_address,
    build_address_metadata_asset,
    build_address_name_tag,
    build_encryption_tag,
    build_signed_metadata,
    parse_address_metadata_asset,
    validate_metadata,
)


class RIP10AddressMetadataTests(TestCase):
    address = 'EL5MFdaF8msRaUEDu9mxSNniPSswNmNRgq'

    def test_address_metadata_asset_round_trip_and_signed_ant_metadata(self):
        asset = build_address_metadata_asset('TOMETAGS', 'ANT', self.address)
        metadata = build_signed_metadata(
            build_address_name_tag(self.address, 'DeFi Tome'),
            'metadata-signature',
        )

        self.assertEqual(asset.asset_name, 'TOMETAGS#ANT_C38D582B')
        self.assertEqual(parse_address_metadata_asset(asset.asset_name), asset)
        self.assertTrue(asset_matches_address(asset.asset_name, self.address))
        self.assertTrue(validate_metadata(asset.asset_name, self.address, metadata).is_valid)

    def test_pgp_asset_uses_aet_metadata_type_and_revision_is_supported(self):
        asset = build_address_metadata_asset('TOMETAGS', 'PGP', self.address, revision='2')
        metadata = build_signed_metadata(
            build_encryption_tag(self.address, '-----BEGIN PGP PUBLIC KEY BLOCK-----\nkey'),
            'metadata-signature',
        )

        self.assertEqual(asset.asset_name, 'TOMETAGS#PGP_C38D582B2')
        self.assertTrue(validate_metadata(asset.asset_name, self.address, metadata).is_valid)

        maximum_length_asset = build_address_metadata_asset(
            'TOMETAGMAX',
            'AIT',
            self.address,
            revision='REVISE7',
        )
        self.assertEqual(len(maximum_length_asset.asset_name), 30)

    def test_invalid_asset_format_is_rejected(self):
        with self.assertRaises(RIP10ValidationError):
            build_address_metadata_asset('TOO-LONG-ASSET', 'ANT', self.address)

        with self.assertRaises(RIP10ValidationError):
            parse_address_metadata_asset('TOMETAGS#ANT_NOT-A-CRC')


class PublicTransactionEvidenceTests(TestCase):
    @patch('Wallet.rpc.PublicRpcClient')
    def test_public_transaction_evidence_requires_and_records_confirmation(self, mock_client_class):
        transaction_id = 'a' * 64
        mock_client_class.return_value.getrawtransaction.return_value = {
            'txid': transaction_id.upper(),
            'confirmations': 2,
            'blockhash': 'b' * 64,
            'blocktime': 100,
            'time': 99,
        }

        evidence = rpc.get_public_transaction_evidence(
            transaction_id.upper(),
            network_mode='testnet',
        )

        self.assertEqual(evidence['transaction_id'], transaction_id)
        self.assertEqual(evidence['confirmations'], 2)
        self.assertEqual(evidence['block_hash'], 'b' * 64)
        mock_client_class.return_value.getrawtransaction.assert_called_once_with(transaction_id, True)

    @patch('Wallet.rpc.PublicRpcClient')
    def test_public_transaction_evidence_rejects_unconfirmed_and_malformed_ids(self, mock_client_class):
        transaction_id = 'a' * 64
        mock_client_class.return_value.getrawtransaction.return_value = {
            'txid': transaction_id,
            'confirmations': 0,
        }

        with self.assertRaisesMessage(ValueError, 'at least 1 are required'):
            rpc.get_public_transaction_evidence(transaction_id)
        with self.assertRaisesMessage(ValueError, 'canonical 64-character'):
            rpc.get_public_transaction_evidence('not-a-transaction-id')


class UniqueAssetIssuanceTests(TestCase):
    @patch('Wallet.rpc._resolve_burn_address', return_value='testnet-unique-burn-address')
    @patch('Wallet.rpc.create_and_send_asset_operation_transaction', return_value={'txid': 'tag-txid'})
    def test_issue_unique_accepts_explicit_signing_keys(self, mock_create_and_send, mock_resolve_burn):
        result = rpc.create_and_send_issue_unique_transaction(
            from_address='owner-address',
            issuer_address='recipient-address',
            root_name='TOMETAGS',
            asset_tags=['ANT_C38D582B'],
            ipfs_hashes=['QmMetadataCid'],
            owner_change_address='owner-address',
            wif_keys=['owner-wif'],
        )

        self.assertEqual(result, {'txid': 'tag-txid'})
        self.assertEqual(mock_create_and_send.call_args.kwargs['wif_keys'], ['owner-wif'])
        self.assertEqual(mock_create_and_send.call_args.kwargs['burn_address'], 'testnet-unique-burn-address')
        self.assertEqual(
            mock_create_and_send.call_args.kwargs['operation_payload'],
            {
                '_issue_new_asset': {
                    'asset_name': 'TOMETAGS#ANT_C38D582B',
                    'asset_quantity': 1.0,
                    'units': 0,
                    'reissuable': 0,
                    'has_ipfs': 1,
                    'ipfs_hash': 'QmMetadataCid',
                },
            },
        )
        mock_resolve_burn.assert_called_once_with('issue_unique_asset')

    @patch('Wallet.rpc._resolve_burn_address', return_value='testnet-unique-burn-address')
    @patch('Wallet.rpc.create_and_send_asset_operation_transaction', return_value={'txid': 'tag-txid'})
    def test_issue_unique_preserves_multi_tag_request_shape(self, mock_create_and_send, _mock_resolve_burn):
        rpc.create_and_send_issue_unique_transaction(
            from_address='owner-address',
            issuer_address='recipient-address',
            root_name='TOMETAGS',
            asset_tags=['ONE', 'TWO'],
            ipfs_hashes=['QmOne', 'QmTwo'],
        )

        self.assertEqual(
            mock_create_and_send.call_args.kwargs['operation_payload'],
            {
                'issue_unique': {
                    'root_name': 'TOMETAGS',
                    'asset_tags': ['ONE', 'TWO'],
                    'ipfs_hashes': ['QmOne', 'QmTwo'],
                },
            },
        )

    @patch('Wallet.rpc._resolve_burn_address', return_value='testnet-channel-burn-address')
    @patch('Wallet.rpc.create_and_send_asset_operation_transaction', return_value={'txid': 'channel-txid'})
    def test_messaging_channel_uses_non_remintable_raw_issue_payload(self, mock_create_and_send, mock_resolve_burn):
        result = rpc.create_and_send_issue_asset_transaction(
            from_address='owner-address',
            issuer_address='owner-address',
            asset_name='TOMETAGS~SWAPS',
            asset_quantity=Decimal('1'),
            units=0,
            reissuable=False,
            has_ipfs=True,
            ipfs_hash='QmConsoleMetadata',
            owner_change_address='owner-address',
            wif_keys=['owner-wif'],
        )

        self.assertEqual(result, {'txid': 'channel-txid'})
        operation_payload = mock_create_and_send.call_args.kwargs['operation_payload']
        self.assertEqual(operation_payload, {
            '_issue_new_asset': {
                'asset_name': 'TOMETAGS~SWAPS',
                'asset_quantity': 1.0,
                'units': 0,
                'reissuable': 0,
                'has_ipfs': 1,
                'ipfs_hash': 'QmConsoleMetadata',
            },
        })
        self.assertNotIn('remintable', operation_payload['_issue_new_asset'])
        mock_resolve_burn.assert_called_once_with('issue_msg_channel_asset')


class AssetCreationServiceTests(TestCase):
    @patch('Wallet.asset_creation._resolve_burn_address', side_effect=lambda key: f'burn-{key}')
    def test_builds_every_supported_asset_kind(self, _mock_burn_address):
        cases = (
            ('main', 'WIZROOT', None, 'burn-issue_asset'),
            ('sub', 'WIZROOT/SUB', 'WIZROOT!', 'burn-issue_sub_asset'),
            ('unique', 'WIZROOT#ONE', 'WIZROOT!', 'burn-issue_unique_asset'),
            ('messaging_channel', 'WIZROOT~MSG', 'WIZROOT!', 'burn-issue_msg_channel_asset'),
            ('qualifier', '#WIZQUAL', None, 'burn-issue_qualifier_asset'),
            ('sub_qualifier', '#WIZQUAL/#SUB', '#WIZQUAL', 'burn-issue_sub_qualifier_asset'),
            ('restricted', '$WIZROOT', 'WIZROOT!', 'burn-issue_restricted_asset'),
        )

        for kind, name, authorization, burn_address in cases:
            with self.subTest(kind=kind):
                operation = build_asset_operation(
                    kind,
                    name,
                    {'quantity': '1', 'units': '0', 'verifier_string': '#WIZQUAL'},
                )
                self.assertEqual(operation['authorization_asset_name'], authorization)
                self.assertEqual(operation['burn_address'], burn_address)

    @patch('Wallet.asset_creation.get_current_network_mode', return_value='testnet')
    @patch('Wallet.asset_creation.broadcast_signed_transaction')
    @patch(
        'Wallet.asset_creation.test_mempool_accept_signed_transaction',
        return_value={'allowed': True, 'txid': 'accepted-txid'},
    )
    @patch('Wallet.asset_creation.sign_raw_transaction', return_value='signed-hex')
    @patch('Wallet.asset_creation.create_raw_asset_operation_transaction', return_value={'raw_tx': 'raw-hex'})
    @patch('Wallet.asset_creation._resolve_burn_address', return_value='burn-address')
    def test_dry_run_records_acceptance_without_broadcast(
        self,
        _mock_burn_address,
        _mock_create_raw,
        _mock_sign,
        _mock_accept,
        mock_broadcast,
        _mock_network,
    ):
        user = User.objects.create_user(username='asset-admin', is_staff=True)

        result = create_asset_for_user(
            user=user,
            source_address='source-address',
            source_wif='source-wif',
            asset_kind='main',
            asset_name='WIZROOT',
            parameters={'quantity': '100', 'units': '2'},
            broadcast=False,
        )

        self.assertFalse(result['broadcast'])
        self.assertEqual(result['accepted_txid'], 'accepted-txid')
        self.assertEqual(result['request'].status, AssetCreationRequest.STATUS_ACCEPTED)
        mock_broadcast.assert_not_called()


class AssetCreationWizardTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(username='wizard-admin', password='testpass', is_staff=True)
        self.regular_user = User.objects.create_user(username='wizard-user', password='testpass')

    @patch('Wallet.views._active_network_mode', return_value='testnet')
    def test_admin_can_open_asset_creation_wizard(self, _mock_network):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('asset_creation_wizard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Preflight and Broadcast')
        self.assertContains(response, 'Administrator')

    def test_non_admin_is_redirected_from_asset_creation_wizard(self):
        self.client.force_login(self.regular_user)

        response = self.client.get(reverse('asset_creation_wizard'))

        self.assertRedirects(response, reverse('portfolio'))

    @patch('Wallet.views._active_network_mode', return_value='testnet')
    @patch('Wallet.views._derive_user_wif_for_address', return_value='source-wif')
    @patch('Wallet.views._get_user_primary_address', return_value='source-address')
    @patch(
        'Wallet.views.create_asset_for_user',
        return_value={'asset_name': 'WIZROOT', 'txid': '', 'accepted_txid': 'candidate-txid', 'broadcast': False},
    )
    def test_preflight_submission_does_not_request_broadcast(
        self,
        mock_create_asset,
        _mock_primary_address,
        _mock_wif,
        _mock_network,
    ):
        self.client.force_login(self.admin_user)

        response = self.client.post(reverse('asset_creation_wizard'), {
            'asset_kind': 'main',
            'asset_name': 'WIZROOT',
            'quantity': '100',
            'units': '2',
            'execution_mode': 'preflight',
        })

        self.assertRedirects(response, reverse('asset_creation_wizard'))
        self.assertFalse(mock_create_asset.call_args.kwargs['broadcast'])


class WalletBalanceSyncTests(TestCase):
    @patch('Wallet.context_processors.get_current_network_mode', return_value='testnet')
    def test_context_processor_returns_stored_network_balance_without_rpc(self, _mock_network):
        user = User.objects.create_user(username='stored-balance-user')
        user_wallet = UserWallet.objects.create(
            user=user,
            entropy='00',
            passphrase='',
            evr_liquidity_testnet=Decimal('1.23456789'),
        )
        WalletAddress.objects.create(
            wallet=user_wallet,
            network_mode='testnet',
            address='EStoredAddress',
            wif='L1stored',
            account=0,
            index=0,
            is_change=False,
        )
        request = type('Request', (), {'user': user})()

        context = wallet_balance(request)

        self.assertEqual(context['user_wallet_balance'], Decimal('1.23456789'))
        self.assertFalse(context['user_wallet_balance_is_live'])
        self.assertEqual(context['user_wallet_balance_address'], 'EStoredAddress')

    @patch('Wallet.context_processors.get_current_network_mode', return_value='testnet')
    def test_context_processor_preserves_offline_snapshot(self, _mock_network):
        user = User.objects.create_user(username='offline-balance-user')
        UserWallet.objects.create(
            user=user,
            entropy='00',
            passphrase='',
            evr_liquidity_testnet=Decimal('99.00000000'),
        )
        request = type('Request', (), {'user': user})()

        context = wallet_balance(request)

        self.assertEqual(context['user_wallet_balance'], Decimal('99.00000000'))
        self.assertFalse(context['user_wallet_balance_is_live'])

    @patch('Wallet.views._active_network_mode', return_value='testnet')
    @patch('Wallet.views._get_user_primary_address', return_value='testnet-address')
    @patch('Wallet.views.RPC.getaddressbalance', return_value={'balance': 123456789})
    def test_sync_returns_satoshis_and_persists_evr(
        self,
        _mock_get_balance,
        _mock_primary_address,
        _mock_network_mode,
    ):
        user = User.objects.create_user(username='balance-user')
        user_wallet = UserWallet.objects.create(user=user, entropy='00', passphrase='')

        balance_satoshis = views._sync_user_evr_balance(user_wallet)

        user_wallet.refresh_from_db()
        self.assertEqual(balance_satoshis, Decimal('123456789'))
        self.assertEqual(user_wallet.evr_liquidity_testnet, Decimal('1.23456789'))


class RawTransactionBuilderTests(TestCase):
    @patch('Wallet.rpc.RPC.getrawtransaction')
    @patch('Wallet.rpc.RPC.getrawmempool', return_value=['mempool-tx'])
    def test_mempool_spent_outpoints_collects_transaction_inputs(self, _mock_mempool, mock_transaction):
        mock_transaction.return_value = {
            'vin': [
                {'txid': 'spent-coin', 'vout': 2},
                {'coinbase': 'coinbase-data'},
            ]
        }

        self.assertEqual(rpc._mempool_spent_outpoints(), {('spent-coin', 2)})

    @patch('Wallet.rpc._mempool_spent_outpoints', return_value={('spent-coin', 0)})
    @patch('Wallet.rpc._get_address_utxos')
    def test_operation_input_selection_excludes_mempool_spends(self, mock_utxos, _mock_mempool_spent):
        mock_utxos.return_value = [
            {'txid': 'spent-coin', 'outputIndex': 0, 'satoshis': 500000000},
            {'txid': 'available-coin', 'outputIndex': 1, 'satoshis': 500000000},
        ]

        inputs, total, _authorization_quantity = rpc._select_inputs_for_operation(
            from_address='source-address',
            required_evr_satoshis=100000000,
        )

        self.assertEqual(inputs, [{'txid': 'available-coin', 'vout': 1}])
        self.assertEqual(total, 500000000)

    @patch('Wallet.rpc.RPC.sendrawtransaction', return_value='broadcast-txid')
    @patch('Wallet.rpc.RPC.testmempoolaccept', return_value=[{'allowed': True, 'txid': 'accepted-txid'}])
    @patch('Wallet.rpc.sign_raw_transaction', return_value='signed-hex')
    def test_broadcast_requires_successful_mempool_preflight(
        self,
        _mock_sign,
        mock_test_mempool,
        mock_send,
    ):
        txid = rpc.sign_and_broadcast_raw_transaction('raw-hex', wif_keys=['wif'])

        self.assertEqual(txid, 'broadcast-txid')
        mock_test_mempool.assert_called_once_with(['signed-hex'])
        mock_send.assert_called_once_with('signed-hex')

    @patch('Wallet.rpc.RPC.sendrawtransaction')
    @patch(
        'Wallet.rpc.RPC.testmempoolaccept',
        return_value=[{'allowed': False, 'reject-reason': 'min relay fee not met'}],
    )
    @patch('Wallet.rpc.sign_raw_transaction', return_value='signed-hex')
    def test_rejected_mempool_preflight_blocks_broadcast(
        self,
        _mock_sign,
        _mock_test_mempool,
        mock_send,
    ):
        with self.assertRaisesMessage(Exception, 'min relay fee not met'):
            rpc.sign_and_broadcast_raw_transaction('raw-hex', wif_keys=['wif'])

        mock_send.assert_not_called()

    @patch('Wallet.rpc.RPC.estimatesmartfee', return_value={'feerate': Decimal('0.02'), 'blocks': 2})
    def test_fee_estimator_uses_a_valid_smart_fee_rate(self, mock_smart_fee):
        feerate, metadata = rpc._get_estimated_feerate_evr_per_kb(conf_target=2)

        self.assertEqual(feerate, Decimal('0.02'))
        mock_smart_fee.assert_called_once_with(2, 'CONSERVATIVE')

    @patch(
        'Wallet.rpc.RPC.estimatesmartfee',
        return_value={'errors': ['Insufficient data or no feerate found'], 'blocks': 6},
    )
    def test_fee_estimator_returns_none_when_smart_fee_data_is_unavailable(self, _mock_smart_fee):
        feerate, metadata = rpc._get_estimated_feerate_evr_per_kb(conf_target=6)

        self.assertIsNone(feerate)
        self.assertIn('Insufficient data or no feerate found', metadata['errors'])

    @patch('Wallet.rpc.RPC.getmempoolinfo', return_value={'mempoolminfee': Decimal('0.02')})
    def test_fee_floor_uses_mempool_minimum(self, mock_mempool_info):
        fee_floor, metadata = rpc._get_fee_floor_evr_per_kb()

        self.assertEqual(fee_floor, Decimal('0.02'))
        self.assertEqual(metadata['errors'], [])
        mock_mempool_info.assert_called_once_with()

    @patch('Wallet.rpc._get_estimated_feerate_evr_per_kb', return_value=(None, {}))
    @patch('Wallet.rpc._get_fee_floor_evr_per_kb', return_value=(Decimal('0.01'), {}))
    def test_explicit_fee_cannot_bypass_relay_floor(self, _mock_floor, _mock_estimate):
        fee_satoshis = rpc._resolve_fee_satoshis(
            explicit_fee_evr=Decimal('0.0001'),
            input_count=2,
            output_count=3,
            tx_size_bytes=500,
        )

        self.assertEqual(fee_satoshis, 1050000)

    @patch('Wallet.rpc.sign_and_broadcast_raw_transaction')
    @patch('Wallet.rpc.create_raw_transaction', return_value='rawhex')
    @patch('Wallet.rpc._select_evr_inputs', return_value=([{'txid': 'tx1', 'vout': 0}], 200000000))
    def test_create_raw_evr_transaction_returns_details_without_broadcasting(
        self,
        mock_select_evr_inputs,
        mock_create_raw_transaction,
        mock_sign_and_broadcast,
    ):
        result = rpc.create_raw_evr_transaction(
            from_address='from-address',
            to_address='to-address',
            amount_evr=Decimal('1.5'),
            change_address='change-address',
            fee_evr=Decimal('0.0001'),
        )

        self.assertEqual(result['raw_tx'], 'rawhex')
        self.assertEqual(result['inputs'], [{'txid': 'tx1', 'vout': 0}])
        self.assertIn('to-address', result['outputs'])
        self.assertIn('from-address', result['outputs'])
        self.assertNotIn('change-address', result['outputs'])
        mock_sign_and_broadcast.assert_not_called()

    @patch('Wallet.rpc.RPC.createrawtransaction')
    def test_create_raw_transaction_preserves_repeated_source_outputs(self, mock_create_raw):
        source_address = 'EL5MFdaF8msRaUEDu9mxSNniPSswNmNRgq'

        def create_with_temporary_script(_inputs, outputs):
            temporary_address = next(address for address in outputs if address != source_address)
            return f"00{rpc._p2pkh_script_pub_key(temporary_address).hex()}00"

        mock_create_raw.side_effect = create_with_temporary_script

        raw_tx = rpc.create_raw_transaction(
            [],
            [
                {source_address: '1.00000000'},
                {source_address: {'transfer': {'ASSET': 1.0}}},
            ],
        )

        source_script = rpc._p2pkh_script_pub_key(source_address).hex()
        self.assertEqual(raw_tx, f'00{source_script}00')
        rpc_outputs = mock_create_raw.call_args.args[1]
        self.assertEqual(len(rpc_outputs), 2)
        self.assertIn(source_address, rpc_outputs)

    @patch('Wallet.rpc.create_raw_transaction', return_value='raw-message-transfer')
    @patch('Wallet.rpc._resolve_fee_satoshis', return_value=1000)
    @patch('Wallet.rpc._estimate_signed_tx_size_bytes', return_value=250)
    @patch('Wallet.rpc._get_address_utxos', return_value=[{'txid': 'evr', 'outputIndex': 0, 'satoshis': 100000}])
    @patch('Wallet.rpc._select_asset_inputs', return_value=([{'txid': 'channel', 'vout': 0}], Decimal('1'), 0))
    def test_raw_asset_transfer_supports_message_channel_payload(
        self,
        _mock_asset_inputs,
        _mock_utxos,
        _mock_size,
        _mock_fee,
        mock_create_raw,
    ):
        rpc.create_raw_asset_transfer_transaction(
            from_address='channel-address',
            to_address='channel-address',
            asset_name='ROOT~SWAPS',
            asset_quantity=Decimal('1'),
            message='QmPayload',
            expire_time=0,
        )

        outputs = mock_create_raw.call_args.kwargs['outputs']
        self.assertEqual(
            outputs[0]['channel-address']['transferwithmessage'],
            {'ROOT~SWAPS': 1.0, 'message': 'QmPayload', 'expire_time': 0},
        )

    @patch('Wallet.rpc.create_raw_transaction', return_value='raw-message-transfer')
    @patch('Wallet.rpc._resolve_fee_satoshis', return_value=1000)
    @patch('Wallet.rpc._estimate_signed_tx_size_bytes', return_value=250)
    @patch('Wallet.rpc._mempool_spent_outpoints', return_value={('spent-evr', 0)})
    @patch(
        'Wallet.rpc._get_address_utxos',
        return_value=[
            {'txid': 'spent-evr', 'outputIndex': 0, 'satoshis': 100000},
            {'txid': 'available-evr', 'outputIndex': 1, 'satoshis': 100000},
        ],
    )
    @patch('Wallet.rpc._select_asset_inputs', return_value=([{'txid': 'channel', 'vout': 0}], Decimal('1'), 0))
    def test_raw_asset_transfer_excludes_mempool_spent_fee_inputs(
        self,
        _mock_asset_inputs,
        _mock_utxos,
        _mock_mempool_spent,
        _mock_size,
        _mock_fee,
        _mock_create_raw,
    ):
        result = rpc.create_raw_asset_transfer_transaction(
            from_address='channel-address',
            to_address='channel-address',
            asset_name='ROOT~SWAPS',
            asset_quantity=Decimal('1'),
            message='QmPayload',
            expire_time=0,
        )

        self.assertEqual(
            result['inputs'],
            [
                {'txid': 'channel', 'vout': 0},
                {'txid': 'available-evr', 'vout': 1},
            ],
        )

    @patch('Wallet.rpc.RPC.createrawtransaction')
    def test_create_raw_transaction_rewrites_destination_inside_asset_script(self, mock_create_raw):
        source_address = 'EL5MFdaF8msRaUEDu9mxSNniPSswNmNRgq'

        def create_with_asset_script(_inputs, outputs):
            temporary_address = next(address for address in outputs if address != source_address)
            destination_hash = rpc._p2pkh_hash160(temporary_address).hex()
            return f"00c0{destination_hash}75acc0{destination_hash}76ac00"

        mock_create_raw.side_effect = create_with_asset_script

        raw_tx = rpc.create_raw_transaction(
            [],
            [
                {source_address: '1.00000000'},
                {source_address: {'issue': {'asset_name': 'WIZROOT'}}},
            ],
        )

        destination_hash = rpc._p2pkh_hash160(source_address).hex()
        self.assertEqual(raw_tx, f'00c0{destination_hash}75acc0{destination_hash}76ac00')

    @patch('Wallet.asset_creation._resolve_burn_address', return_value='burn-address')
    def test_root_qualifier_does_not_request_qualifier_change(self, _mock_burn_address):
        operation = build_asset_operation('qualifier', '#WIZQUAL', {'quantity': '1'})

        self.assertNotIn('change_quantity', operation['operation_payload']['issue_qualifier'])

    @patch('Wallet.rpc.RPC.createrawtransaction')
    def test_create_raw_transaction_serializes_qualifier_without_broken_node_constructor(self, mock_create_raw):
        source_address = 'EL5MFdaF8msRaUEDu9mxSNniPSswNmNRgq'

        def create_with_placeholder(_inputs, outputs):
            qualifier_address = next(address for address in outputs if address != source_address)
            self.assertEqual(outputs[qualifier_address], '0.00000000')
            placeholder_script = rpc._p2pkh_script_pub_key(qualifier_address)
            return f"00{rpc._compact_size(len(placeholder_script)).hex()}{placeholder_script.hex()}00"

        mock_create_raw.side_effect = create_with_placeholder

        raw_tx = rpc.create_raw_transaction(
            [],
            [
                {source_address: '1.00000000'},
                {
                    source_address: {
                        'issue_qualifier': {
                            'asset_name': '#WIZQUAL',
                            'asset_quantity': 1.0,
                            'has_ipfs': 0,
                        }
                    }
                },
            ],
        )

        self.assertIn(rpc._p2pkh_script_pub_key(source_address).hex(), raw_tx)
        self.assertIn(b'evrq'.hex(), raw_tx)
        self.assertNotIn(rpc._p2pkh_hash160(next(
            address for address in mock_create_raw.call_args.args[1] if address != source_address
        )).hex(), raw_tx)

    @patch('Wallet.asset_creation._resolve_burn_address', return_value='burn-address')
    def test_sub_qualifier_returns_root_authorization_quantity(self, _mock_burn_address):
        operation = build_asset_operation('sub_qualifier', '#WIZQUAL/#SUB', {'quantity': '1'})

        self.assertEqual(operation['operation_payload']['issue_qualifier']['change_quantity'], 1.0)

    def test_asset_operation_keeps_authorization_and_issuance_as_separate_outputs(self):
        outputs = rpc.compose_asset_operation_outputs(
            coin_outputs={'burn-address': Decimal('100')},
            operation_address='source-address',
            operation_payload={'issue': {'asset_name': 'ROOT/SUB'}},
            owner_token_change_output=(
                'source-address',
                {'transfer': {'ROOT!': 1.0}},
            ),
        )

        self.assertEqual(
            outputs,
            [
                {'burn-address': '100.00000000'},
                {'source-address': {'transfer': {'ROOT!': 1.0}}},
                {'source-address': {'issue': {'asset_name': 'ROOT/SUB'}}},
            ],
        )

    @patch('Wallet.rpc.create_raw_transaction', return_value='raw-asset-operation')
    @patch('Wallet.rpc._get_estimated_feerate_evr_per_kb', return_value=(None, {}))
    @patch('Wallet.rpc._get_fee_floor_evr_per_kb', return_value=(Decimal('0.01'), {}))
    @patch(
        'Wallet.rpc._select_inputs_for_operation',
        return_value=([{'txid': 'owner-tx', 'vout': 0}, {'txid': 'evr-tx', 'vout': 1}], 20000000000, Decimal('2')),
    )
    def test_asset_operation_returns_full_authorization_and_evr_change_to_source(
        self,
        _mock_select_inputs,
        _mock_fee_floor,
        _mock_fee_estimate,
        _mock_create_raw,
    ):
        result = rpc.create_raw_asset_operation_transaction(
            from_address='source-address',
            operation_address='issuer-address',
            operation_payload={'issue': {'asset_name': 'ROOT~OPS'}},
            burn_amount_evr=Decimal('100'),
            burn_address='burn-address',
            authorization_asset_name='ROOT!',
            owner_token_change_output=('other-owner-address', {'transfer': {'ROOT!': 1.0}}),
            evr_change_address='other-evr-address',
            fee_evr=Decimal('0.0001'),
        )

        source_outputs = [output['source-address'] for output in result['outputs'] if 'source-address' in output]
        self.assertIn({'transfer': {'ROOT!': 2.0}}, source_outputs)
        self.assertIn('99.99071800', source_outputs)
        self.assertFalse(any('other-owner-address' in output for output in result['outputs']))
        self.assertFalse(any('other-evr-address' in output for output in result['outputs']))

    @patch('Wallet.rpc.create_raw_transaction', return_value='raw-asset-transfer')
    @patch('Wallet.rpc._get_estimated_feerate_evr_per_kb', return_value=(None, {}))
    @patch('Wallet.rpc._get_fee_floor_evr_per_kb', return_value=(Decimal('0.01'), {}))
    @patch('Wallet.rpc._get_address_utxos')
    @patch(
        'Wallet.rpc._select_asset_inputs',
        return_value=([{'txid': 'asset-tx', 'vout': 0}], Decimal('2'), 0),
    )
    def test_asset_transfer_returns_asset_and_evr_change_to_source(
        self,
        _mock_select_asset_inputs,
        mock_get_address_utxos,
        _mock_fee_floor,
        _mock_fee_estimate,
        _mock_create_raw,
    ):
        mock_get_address_utxos.return_value = [
            {'txid': 'evr-tx', 'outputIndex': 1, 'satoshis': 100000000},
        ]

        result = rpc.create_raw_asset_transfer_transaction(
            from_address='source-address',
            to_address='recipient-address',
            asset_name='ASSET',
            asset_quantity=Decimal('1'),
            change_address='other-evr-address',
            asset_change_address='other-asset-address',
            fee_evr=Decimal('0.0001'),
        )

        source_outputs = [output['source-address'] for output in result['outputs'] if 'source-address' in output]
        self.assertEqual(source_outputs[0], {'transfer': {'ASSET': 1.0}})
        self.assertEqual(source_outputs[1], '0.99531700')
        self.assertFalse(any('other-asset-address' in output for output in result['outputs']))
        self.assertFalse(any('other-evr-address' in output for output in result['outputs']))


class AtomicAssetSwapTransactionTests(TestCase):
    @patch('Wallet.rpc.sign_and_broadcast_raw_transaction', return_value='atomic-swap-txid')
    @patch('Wallet.rpc.create_raw_transaction', return_value='raw-atomic-swap')
    @patch('Wallet.rpc._select_evr_inputs')
    @patch('Wallet.rpc._get_address_utxos')
    def test_atomic_asset_for_evr_swap_uses_one_signed_transaction(
        self,
        mock_get_address_utxos,
        mock_select_evr_inputs,
        mock_create_raw_transaction,
        mock_sign_and_broadcast,
    ):
        mock_get_address_utxos.side_effect = [
            [
                {
                    'txid': 'seller-asset-tx',
                    'outputIndex': 0,
                    'satoshis': 546,
                    'assetName': 'COLLECTIBLE#1',
                    'assetAmount': '2',
                }
            ],
            [
                {
                    'txid': 'buyer-evr-tx',
                    'outputIndex': 1,
                    'satoshis': 200000000,
                }
            ],
        ]
        mock_select_evr_inputs.return_value = ([{'txid': 'buyer-evr-tx', 'vout': 1}], 200000000)

        result = rpc.create_and_send_atomic_asset_evr_swap_transaction(
            seller_address='seller-address',
            buyer_address='buyer-address',
            asset_name='COLLECTIBLE#1',
            asset_quantity=Decimal('1'),
            payment_evr=Decimal('1'),
            fee_evr=Decimal('0.0001'),
            wif_keys=['seller-wif', 'buyer-wif'],
        )

        self.assertEqual(result['txid'], 'atomic-swap-txid')
        self.assertEqual(result['raw_tx'], 'raw-atomic-swap')
        self.assertEqual(result['asset_change_quantity'], Decimal('1'))
        self.assertEqual(len(result['inputs']), 2)
        self.assertIn(
            {'buyer-address': {'transfer': {'COLLECTIBLE#1': 1.0}}},
            result['outputs'],
        )
        self.assertIn(
            {'seller-address': {'transfer': {'COLLECTIBLE#1': 1.0}}},
            result['outputs'],
        )
        mock_create_raw_transaction.assert_called_once()
        mock_sign_and_broadcast.assert_called_once_with(
            'raw-atomic-swap',
            wif_keys=['seller-wif', 'buyer-wif'],
        )

    @patch('Wallet.rpc.sign_and_broadcast_raw_transaction', return_value='asset-asset-txid')
    @patch('Wallet.rpc.create_raw_transaction', return_value='raw-asset-asset-swap')
    @patch('Wallet.rpc._select_evr_inputs')
    @patch('Wallet.rpc._get_address_utxos')
    def test_atomic_asset_for_asset_swap_excludes_asset_utxos_from_evr_fee_selection(
        self,
        mock_get_address_utxos,
        mock_select_evr_inputs,
        mock_create_raw_transaction,
        mock_sign_and_broadcast,
    ):
        mock_get_address_utxos.side_effect = [
            [
                {
                    'txid': 'seller-asset-tx',
                    'outputIndex': 0,
                    'satoshis': 546,
                    'assetName': 'SOLD#1',
                    'assetAmount': '1',
                }
            ],
            [
                {
                    'txid': 'buyer-asset-tx',
                    'outputIndex': 1,
                    'satoshis': 546,
                    'assetName': 'BOUGHT#1',
                    'assetAmount': '1',
                }
            ],
        ]

        def select_evr_inputs_side_effect(*, address, required_satoshis, locktime, replaceable, excluded_keys=None):
            self.assertEqual(address, 'buyer-address')
            self.assertIn(('buyer-asset-tx', 1), excluded_keys)
            self.assertNotIn(('seller-asset-tx', 0), excluded_keys)
            return ([{'txid': 'buyer-fee-tx', 'vout': 2}], 200000000)

        mock_select_evr_inputs.side_effect = select_evr_inputs_side_effect

        result = rpc.create_and_send_atomic_asset_asset_swap_transaction(
            seller_address='seller-address',
            buyer_address='buyer-address',
            seller_asset_name='SOLD#1',
            seller_asset_quantity=Decimal('1'),
            buyer_asset_name='BOUGHT#1',
            buyer_asset_quantity=Decimal('1'),
            fee_evr=Decimal('0.0001'),
            wif_keys=['seller-wif', 'buyer-wif'],
        )

        self.assertEqual(result['txid'], 'asset-asset-txid')
        self.assertEqual(result['raw_tx'], 'raw-asset-asset-swap')
        self.assertEqual(len(result['inputs']), 3)
        self.assertIn({'buyer-address': {'transfer': {'SOLD#1': 1.0}}}, result['outputs'])
        self.assertIn({'seller-address': {'transfer': {'BOUGHT#1': 1.0}}}, result['outputs'])
        mock_create_raw_transaction.assert_called()
        mock_sign_and_broadcast.assert_called_once_with(
            'raw-asset-asset-swap',
            wif_keys=['seller-wif', 'buyer-wif'],
        )


class AssetUnitsResolutionTests(TestCase):
    @patch('Wallet.asset_units.rpc_client.get_asset_data', return_value={'units': 3})
    def test_get_asset_units_prefers_on_chain_metadata(self, mock_get_asset_data):
        TrackedAsset.objects.create(symbol='TOKEN/SUB', network_mode='testnet', units=7)

        self.assertEqual(get_asset_units('TOKEN/SUB', network_mode='testnet'), 3)
        mock_get_asset_data.assert_called_once_with('TOKEN/SUB')


class WalletTransactionsViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='history-user',
            email='history@example.com',
            password='testpass123',
        )
        self.user_wallet = UserWallet.objects.create(
            user=self.user,
            name='History Wallet',
            entropy='test-entropy',
            passphrase='',
        )
        WalletAddress.objects.create(
            wallet=self.user_wallet,
            network_mode='testnet',
            address='EHistoryAddress123',
            wif='L1historywif',
            account=0,
            index=0,
            is_change=False,
        )
        WalletAddress.objects.create(
            wallet=self.user_wallet,
            network_mode='testnet',
            address='EHistoryChange456',
            wif='L1historychangewif',
            account=0,
            index=1,
            is_change=True,
        )

    def test_portfolio_links_to_wallet_transactions(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('portfolio'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('wallet_transactions'))

    def test_portfolio_shows_admin_messaging_channel_link_for_staff_user(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.force_login(self.user)

        response = self.client.get(reverse('portfolio'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('messaging_channel_management'))

    def test_portfolio_hides_admin_messaging_channel_link_for_non_staff_user(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('portfolio'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse('messaging_channel_management'))

    @patch('Wallet.views._active_network_mode', return_value='testnet')
    def test_portfolio_displays_assets_by_chain_role(self, _mock_network):
        for symbol, quantity in {
            'ROOT': Decimal('12.5'),
            'ROOT#ONE': Decimal('1'),
            '#ACCESS': Decimal('1'),
            '$ROOT': Decimal('4'),
            'ROOT!': Decimal('1'),
        }.items():
            asset = TrackedAsset.objects.create(
                symbol=symbol,
                network_mode='testnet',
                asset_type=views.classify_asset_type(symbol),
            )
            views.TrackedAssetHolding.objects.create(
                asset=asset,
                user=self.user,
                quantity=quantity,
            )
        self.client.force_login(self.user)

        response = self.client.get(reverse('portfolio'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Fungible root asset')
        self.assertContains(response, 'Indivisible collectible')
        self.assertContains(response, 'Address eligibility credential')
        self.assertContains(response, 'Verifier-controlled asset')
        self.assertContains(response, 'Issuance and governance control')

    @patch('Wallet.views.RPC.getrawtransaction')
    @patch('Wallet.views._get_user_asset_balances')
    @patch('Wallet.views._get_server_public_ip')
    @patch('Wallet.views._active_network_mode', return_value='testnet')
    def test_admin_portfolio_renders_issuance_without_network_calls(
        self,
        _mock_network,
        mock_server_ip,
        mock_balances,
        mock_transaction,
    ):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        AssetCreationRequest.objects.create(
            creator=self.user,
            network_mode='testnet',
            asset_kind='main',
            asset_name='TOME0808',
            source_address='EHistoryAddress123',
            status=AssetCreationRequest.STATUS_BROADCAST,
            broadcast_txid='a' * 64,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse('portfolio'))

        self.assertContains(response, 'TOME0808')
        self.assertContains(response, 'Pending confirmation')
        mock_server_ip.assert_not_called()
        mock_balances.assert_not_called()
        mock_transaction.assert_not_called()

    @patch('Wallet.views._active_network_mode', return_value='testnet')
    @patch('Wallet.views.RPC.getrawtransaction')
    @patch('Wallet.views.RPC.getaddressdeltas')
    @patch('Wallet.views.RPC.getaddresstxids')
    def test_wallet_transactions_page_renders_history(
        self,
        mock_getaddresstxids,
        mock_getaddressdeltas,
        mock_getrawtransaction,
        mock_network_mode,
    ):
        self.client.force_login(self.user)
        mock_getaddresstxids.return_value = ['tx-older', 'tx-old', 'tx-new']
        mock_getaddressdeltas.return_value = [
            {'txid': 'tx-old', 'satoshis': -250000000},
            {'txid': 'tx-new', 'satoshis': 125000000},
            {'txid': 'tx-new', 'assetName': 'TST', 'assetAmount': '3'},
            {'txid': 'tx-older', 'satoshis': 100000000},
        ]
        mock_getrawtransaction.side_effect = [
            {'confirmations': 6, 'time': 1722900000, 'size': 212},
            {'confirmations': 12, 'time': 1722800000, 'size': 190},
            {'confirmations': 20, 'time': 1722700000, 'size': 188},
        ]

        response = self.client.get(reverse('wallet_transactions'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portfolio/transactions.html')
        transactions = response.context['transactions']
        self.assertEqual([item['txid'] for item in transactions], ['tx-new', 'tx-old', 'tx-older'])
        self.assertEqual(response.context['address_count'], 2)
        self.assertEqual(response.context['total_indexed_transactions'], 3)
        self.assertFalse(response.context['has_more_transactions'])
        self.assertIsNone(response.context['limit'])
        self.assertTrue(response.context['showing_all_transactions'])
        self.assertEqual(transactions[0]['direction'], 'received')
        self.assertEqual(transactions[0]['evr_delta_display'], '+1.25000000 EVR')
        self.assertEqual(transactions[0]['asset_changes'][0]['amount_display'], '+3 TST')
        self.assertEqual(transactions[1]['direction'], 'sent')
        self.assertEqual(transactions[1]['evr_delta_display'], '-2.50000000 EVR')
        mock_getaddresstxids.assert_called_once_with({'addresses': ['EHistoryAddress123', 'EHistoryChange456']})
        mock_network_mode.assert_called()


class MessagingChannelManagementViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='channel-panel-user',
            email='channel-panel@example.com',
            password='testpass123',
            is_staff=True,
        )
        UserWallet.objects.create(
            user=self.user,
            name='Channel Panel Wallet',
            entropy='channel-panel-entropy',
            passphrase='',
        )
        self.system_user = User.objects.create_user(username='system', password='unused')

    @patch('Wallet.views._get_stored_admin_assets', return_value=['ROOT!', 'OPS!'])
    @patch('Wallet.views.scan_channel_console_assets', return_value={'valid_channels': [], 'invalid_channels': []})
    def test_admin_can_open_messaging_channel_management_page(self, _mock_scan, _mock_admin_assets):
        self.client.force_login(self.user)

        response = self.client.get(reverse('messaging_channel_management'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portfolio/messaging_channels.html')
        self.assertContains(response, '<option value="ROOT!"', html=False)
        self.assertContains(response, '<option value="OPS!"', html=False)
        self.assertContains(response, 'value="5"', html=False)
        self.assertContains(response, 'Unified v5 DeFiTome messaging console for replayable swap, market, and DEC lifecycle events.')
        self.assertContains(response, 'Proposed Policies')

    @patch('Wallet.views._get_stored_admin_assets', return_value=[])
    @patch('Wallet.views.scan_channel_console_assets', return_value={'valid_channels': [], 'invalid_channels': []})
    def test_console_lists_failed_dec_event_from_another_player(self, _mock_scan, _mock_admin_assets):
        policy = MessageChannelPolicy.objects.create(
            channel_key='tome0808_swapflow',
            channel_name='TOME0808~SWAPFLOWV5',
            network_mode='testnet',
            version=5,
            status='active',
            owner_account=self.system_user,
            manager_account=self.user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )
        address = WalletAddress.objects.create(
            wallet=self.user.user_wallet,
            network_mode='testnet',
            address='mReconcileConsoleAddress',
            wif='reconcile-console-wif',
            account=0,
            index=0,
            is_change=False,
        )
        profile = WalletProfile.objects.create(
            wallet=self.user.user_wallet,
            address=address,
            network_mode='testnet',
            name='Console DEC Vault',
            is_main=True,
        )
        player = User.objects.create_user(username='dec-channel-player', password='testpass123')
        instance = DecPokerGameInstance.objects.create(
            creator=self.user,
            manager_account=self.system_user,
            network_mode='testnet',
            title='Console DEC Table',
            reward_asset_name='CONSOLEDEC',
            reward_supply=Decimal('1000'),
            entry_fee_evr=Decimal('0.5'),
            reward_per_win=Decimal('10'),
            system_fee_address=address.address,
            vault_profile=profile,
            channel_policy=policy,
            status=DecPokerGameInstance.STATUS_ACTIVE,
            is_active=True,
        )
        hand = DecPokerHand.objects.create(
            game_instance=instance,
            player=player,
            wager_evr=Decimal('0.5'),
            reward_amount=Decimal('0'),
            reward_asset_name='CONSOLEDEC',
            result=DecPokerHand.RESULT_LOSE,
            spend_message_status=DecPokerHand.MESSAGE_STATUS_FAILED,
            outcome_detail={
                'message_events': {
                    'spend': {
                        'reason': 'channel UTXO is pending confirmation',
                    },
                },
            },
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse('messaging_channel_management') + '?event_type=dec&event_status=failed'
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'DEC #{hand.id}')
        self.assertContains(response, 'game_spend_recorded')
        self.assertContains(response, player.username)
        self.assertContains(response, 'channel UTXO is pending confirmation')

    @patch('Wallet.views._get_stored_admin_assets', return_value=[])
    @patch('Wallet.views.scan_channel_console_assets', return_value={'valid_channels': [], 'invalid_channels': []})
    @patch('Wallet.views.set_channel_subscription', return_value={'asset_name': 'ROOT~OPS', 'subscribed': True})
    def test_admin_can_subscribe_to_verified_scan_channel(
        self,
        mock_subscription,
        _mock_scan,
        _mock_admin_assets,
    ):
        self.client.force_login(self.user)

        response = self.client.post(reverse('messaging_channel_management'), {
            'action': 'subscribe_channel',
            'asset_name': 'ROOT~OPS',
        })

        self.assertEqual(response.status_code, 302)
        mock_subscription.assert_called_once_with('ROOT~OPS', subscribe=True, network_mode='testnet')

    @patch('Wallet.views._get_stored_admin_assets', return_value=[])
    @patch('Wallet.views.scan_channel_console_assets', return_value={'valid_channels': [], 'invalid_channels': []})
    @patch('Wallet.views.burn_channel_asset_for_revision', return_value={'asset_name': 'ROOT~OPS', 'txid': 'burn-txid'})
    def test_admin_can_confirm_revision_burn(
        self,
        mock_burn,
        _mock_scan,
        _mock_admin_assets,
    ):
        self.client.force_login(self.user)

        response = self.client.post(reverse('messaging_channel_management'), {
            'action': 'burn_channel_revision',
            'asset_name': 'ROOT~OPS',
            'confirm_revision_burn': '1',
        })

        self.assertEqual(response.status_code, 302)
        mock_burn.assert_called_once_with(self.user, 'ROOT~OPS', network_mode='testnet')

    @patch('Wallet.views._get_stored_admin_assets', return_value=['ROOT!'])
    @patch('Wallet.views.scan_channel_console_assets', return_value={'valid_channels': [], 'invalid_channels': []})
    @patch('Wallet.views.create_channel_console_asset_for_user')
    def test_admin_can_submit_channel_creation_form(self, mock_create_channel, _mock_scan, _mock_admin_assets):
        mock_create_channel.return_value = {
            'channel_asset_name': 'ROOT~OPS',
            'metadata_ipfs_cid': 'QmCid',
            'txid': 'txid-1',
            'channel_policy': {
                'channel_key': 'root_ops_console',
                'version': 1,
                'rules_checksum': 'checksum',
            },
            'owned_addresses': ['EAddr1'],
        }
        self.client.force_login(self.user)

        response = self.client.post(reverse('messaging_channel_management'), {
            'admin_asset': 'ROOT!',
            'channel_tag': 'OPS',
            'channel_key': 'root_ops_console',
            'channel_name': 'ROOT~OPS',
            'network_mode': 'testnet',
            'description': 'ops channel',
            'allowed_stages': 'offer_created, settlement_lock_created',
            'immutable_payload': 'on',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Created channel ROOT~OPS with policy version 1')
        self.assertContains(response, 'name="channel_version" type="number" min="1" step="1" placeholder="auto" value="5"', html=False)
        mock_create_channel.assert_called_once()
        sent_payload = mock_create_channel.call_args.args[1]
        self.assertNotIn('qty', sent_payload)

    @patch('Wallet.views._get_stored_admin_assets', return_value=['TOME0808!'])
    @patch('Wallet.views.scan_channel_console_assets', return_value={'valid_channels': [], 'invalid_channels': []})
    @patch('Wallet.views.create_channel_console_asset_for_user')
    def test_admin_sees_pending_message_for_reused_channel_issuance(
        self,
        mock_create_channel,
        _mock_scan,
        _mock_admin_assets,
    ):
        mock_create_channel.return_value = {
            'channel_asset_name': 'TOME0808~SWAPFLOWV5',
            'txid': 'v5-issuance-txid',
            'existing_issuance': True,
            'issuance_pending': True,
        }
        self.client.force_login(self.user)

        response = self.client.post(reverse('messaging_channel_management'), {
            'admin_asset': 'TOME0808!',
            'channel_tag': 'SWAPFLOWV5',
            'channel_key': 'tome0808_swapflow',
            'channel_name': 'DeFiTome Unified v5 Console',
            'channel_version': '5',
            'network_mode': 'testnet',
            'description': 'Unified v5 channel',
            'allowed_stages': 'offer_created',
            'immutable_payload': 'on',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'Channel asset TOME0808~SWAPFLOWV5 already has an issuance transaction and is awaiting on-chain metadata verification.',
        )
        self.assertNotContains(response, 'Created channel TOME0808~SWAPFLOWV5 with policy version 5')

    @patch('Wallet.views._get_stored_admin_assets', return_value=['ROOT!'])
    @patch('Wallet.views.scan_channel_console_assets', return_value={'valid_channels': [], 'invalid_channels': []})
    def test_admin_can_deprecate_managed_policy(self, _mock_scan, _mock_admin_assets):
        policy = MessageChannelPolicy.objects.create(
            channel_key='root_ops_console',
            channel_name='ROOT~OPS',
            network_mode='testnet',
            version=1,
            status='active',
            owner_account=self.system_user,
            manager_account=self.user,
            schema_name='defitome.atomic-swap-transfer-message',
            schema_version=1,
            allowed_stages=['offer_created'],
            strict_rules={'console_mode': 'strict'},
            is_locked=True,
        )
        self.client.force_login(self.user)

        response = self.client.post(reverse('messaging_channel_management'), {
            'action': 'deprecate_policy',
            'policy_id': str(policy.id),
        })

        self.assertEqual(response.status_code, 302)
        policy.refresh_from_db()
        self.assertEqual(policy.status, 'deprecated')

    @patch('Wallet.views.scan_channel_console_assets', return_value={'valid_channels': [{'asset_name': 'ROOT~OPS'}], 'invalid_channels': []})
    def test_scan_json_export_returns_management_scan_payload(self, _mock_scan):
        self.client.force_login(self.user)

        response = self.client.get(reverse('messaging_channel_management') + '?export=scan_json')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get('success'))
        self.assertIn('valid_channels', payload)

    @patch('Wallet.views._get_stored_admin_assets', return_value=['ROOT!'])
    @patch('Wallet.views.scan_channel_console_assets', return_value={'valid_channels': [], 'invalid_channels': []})
    @patch('Wallet.views.create_channel_console_asset_for_user')
    def test_channel_creation_rejects_unowned_admin_asset_selection(self, mock_create_channel, _mock_scan, _mock_admin_assets):
        self.client.force_login(self.user)

        response = self.client.post(reverse('messaging_channel_management'), {
            'action': 'create_channel',
            'admin_asset': 'OTHER!',
            'channel_tag': 'OPS',
            'channel_key': 'root_ops_console',
            'network_mode': 'testnet',
            'allowed_stages': 'offer_created',
        })

        self.assertEqual(response.status_code, 302)
        mock_create_channel.assert_not_called()

    @patch('Wallet.views.scan_channel_console_assets', return_value={'valid_channels': [{'asset_name': 'ROOT~OPS', 'channel_key': 'root_ops_console', 'ipfs_cid': 'QmCid', 'allowed_stages': ['offer_created'], 'console_type': 'atomic_swap_transfer'}], 'invalid_channels': []})
    def test_scan_csv_export_returns_csv(self, _mock_scan):
        self.client.force_login(self.user)

        response = self.client.get(reverse('messaging_channel_management') + '?export=scan_csv')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv')
        self.assertIn('channel_scan.csv', response['Content-Disposition'])
        self.assertIn('ROOT~OPS', response.content.decode('utf-8'))

    @patch('Wallet.views._active_network_mode', return_value='mainnet')
    @patch('Wallet.views._get_stored_admin_assets', return_value=['ROOT!'])
    @patch('Wallet.views.scan_channel_console_assets', return_value={'valid_channels': [], 'invalid_channels': []})
    def test_mainnet_hides_proposed_policies_and_load_workflow(self, _mock_scan, _mock_admin_assets, _mock_network):
        policy = MessageChannelPolicy.objects.create(
            channel_key='root_ops_console',
            channel_name='ROOT~OPS',
            network_mode='mainnet',
            version=1,
            status='active',
            owner_account=self.system_user,
            manager_account=self.user,
            schema_name='defitome.atomic-swap-transfer-message',
            schema_version=1,
            allowed_stages=['offer_created'],
            strict_rules={'console_mode': 'strict'},
            is_locked=True,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse('messaging_channel_management') + f'?policy_id={policy.id}')

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Proposed Policies')
        self.assertNotContains(response, f'?policy_id={policy.id}')
        self.assertContains(response, 'restricted to testnet')

    @patch('Wallet.views._get_stored_admin_assets', return_value=['ROOT!'])
    @patch('Wallet.views.scan_channel_console_assets', return_value={'valid_channels': [], 'invalid_channels': []})
    def test_testnet_can_promote_proposal_to_draft_policy(self, _mock_scan, _mock_admin_assets):
        policy = MessageChannelPolicy.objects.create(
            channel_key='root_ops_console',
            channel_name='ROOT~OPS',
            network_mode='testnet',
            version=1,
            status='active',
            owner_account=self.system_user,
            manager_account=self.user,
            schema_name='defitome.atomic-swap-transfer-message',
            schema_version=1,
            allowed_stages=['offer_created'],
            strict_rules={'console_mode': 'strict'},
            is_locked=True,
        )
        self.client.force_login(self.user)

        response = self.client.post(reverse('messaging_channel_management'), {
            'action': 'promote_proposal_to_draft',
            'policy_id': str(policy.id),
            'proposal_version': '5',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        draft = MessageChannelPolicy.objects.get(
            channel_key='root_ops_console',
            network_mode='testnet',
            version=5,
        )
        self.assertEqual(draft.status, 'draft')
        self.assertEqual(draft.strict_rules.get('proposal_status'), 'draft')
        self.assertContains(response, 'Created draft policy v5 for root_ops_console')


class SendFundsViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='send-user',
            email='send@example.com',
            password='testpass123',
        )
        self.user_wallet = UserWallet.objects.create(
            user=self.user,
            name='Send Wallet',
            entropy='000102030405060708090a0b0c0d0e0f',
            passphrase='',
        )
        WalletAddress.objects.create(
            wallet=self.user_wallet,
            network_mode='testnet',
            address='ESendAddress123',
            wif='L1sendwif',
            account=0,
            index=0,
            is_change=False,
        )

    @patch('Wallet.views._active_network_mode', return_value='testnet')
    def test_get_user_primary_address_prefers_main_profile(self, mock_network_mode):
        first_address = WalletAddress.objects.get(wallet=self.user_wallet, index=0, is_change=False)
        second_address = WalletAddress.objects.create(
            wallet=self.user_wallet,
            network_mode='testnet',
            address='ESendAddress456',
            wif='L1sendwif2',
            account=0,
            index=1,
            is_change=False,
        )
        WalletProfile.objects.create(
            wallet=self.user_wallet,
            address=first_address,
            network_mode='testnet',
            name='Primary',
            is_main=False,
        )
        WalletProfile.objects.create(
            wallet=self.user_wallet,
            address=second_address,
            network_mode='testnet',
            name='Trading',
            is_main=True,
        )

        primary_address = views._get_user_primary_address(self.user)

        self.assertEqual(primary_address, 'ESendAddress456')

    @patch('Wallet.views._active_network_mode', return_value='testnet')
    @patch('Wallet.views.RPC.importprivkey')
    def test_create_profile_derives_next_external_address(self, mock_importprivkey, mock_network_mode):
        self.client.force_login(self.user)
        WalletProfile.objects.create(
            wallet=self.user_wallet,
            address=WalletAddress.objects.get(wallet=self.user_wallet, index=0, is_change=False),
            network_mode='testnet',
            name='Main',
            is_main=True,
        )

        response = self.client.post(
            reverse('send_funds'),
            {
                'action': 'create_profile',
                'profile_name': 'Treasury',
            },
        )

        self.assertEqual(response.status_code, 302)
        created_profile = WalletProfile.objects.get(wallet=self.user_wallet, name='Treasury')
        self.assertEqual(created_profile.address.index, 1)
        self.assertFalse(created_profile.is_main)
        self.assertTrue(created_profile.address.address)
        mock_importprivkey.assert_called_once()

    @patch('Wallet.views._active_network_mode', return_value='testnet')
    def test_set_main_profile_updates_send_receive_source(self, mock_network_mode):
        self.client.force_login(self.user)
        first_address = WalletAddress.objects.get(wallet=self.user_wallet, index=0, is_change=False)
        second_address = WalletAddress.objects.create(
            wallet=self.user_wallet,
            network_mode='testnet',
            address='ESendAddress456',
            wif='L1sendwif2',
            account=0,
            index=1,
            is_change=False,
        )
        WalletProfile.objects.create(
            wallet=self.user_wallet,
            address=first_address,
            network_mode='testnet',
            name='Main',
            is_main=True,
        )
        profile = WalletProfile.objects.create(
            wallet=self.user_wallet,
            address=second_address,
            network_mode='testnet',
            name='Trading',
            is_main=False,
        )

        response = self.client.post(
            reverse('send_funds'),
            {
                'action': 'set_main_profile',
                'profile_id': str(profile.id),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db()
        self.assertTrue(profile.is_main)
        self.assertEqual(views._get_user_primary_address(self.user), 'ESendAddress456')
        messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertIn('"Trading" is now your main wallet profile.', messages)

    @patch('Wallet.views._active_network_mode', return_value='testnet')
    @patch('Wallet.views.build_qr_data_uri', return_value='data:image/png;base64,abc')
    @patch('Wallet.views._sync_user_evr_balance')
    @patch('Wallet.views._get_user_asset_balances')
    @patch('Wallet.views.RPC.getassetdata')
    def test_send_funds_get_uses_stored_asset_metadata_without_rpc(
        self,
        mock_get_assetdata,
        mock_get_user_asset_balances,
        mock_sync_balance,
        mock_build_qr,
        mock_network_mode,
    ):
        tracked_asset = TrackedAsset.objects.create(
            symbol='WHOLE',
            network_mode='testnet',
            units=0,
        )
        TrackedAssetHolding.objects.create(
            asset=tracked_asset,
            user=self.user,
            quantity=Decimal('2'),
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse('send_funds'))

        self.assertEqual(response.status_code, 200)
        mock_get_assetdata.assert_not_called()
        mock_get_user_asset_balances.assert_not_called()
        mock_sync_balance.assert_not_called()
        asset_option = response.context['asset_options'][0]
        self.assertEqual(asset_option['symbol'], 'WHOLE')
        self.assertEqual(asset_option['units'], 0)
        self.assertEqual(asset_option['step'], '1')
        self.assertEqual(asset_option['min_value'], '1')

    @patch('Wallet.views._active_network_mode', return_value='testnet')
    @patch('Wallet.views.build_qr_data_uri', return_value='data:image/png;base64,abc')
    @patch('Wallet.views._sync_user_evr_balance', return_value=Decimal('100000000'))
    @patch('Wallet.views._get_user_asset_balances', return_value=({'WHOLE': Decimal('2')}, None))
    @patch('Wallet.views._get_asset_units', return_value=0)
    @patch('Wallet.views.create_and_send_asset_transfer_transaction')
    def test_send_funds_rejects_fractional_amount_for_indivisible_asset(
        self,
        mock_send_asset,
        mock_get_asset_units,
        mock_get_user_asset_balances,
        mock_sync_balance,
        mock_build_qr,
        mock_network_mode,
    ):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('send_funds'),
            {
                'currency': 'WHOLE',
                'recipient_address': 'ERecipient123',
                'amount': '1.5',
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This asset is indivisible and must be sent as a whole number.')
        mock_send_asset.assert_not_called()

    @patch('Wallet.views._active_network_mode', return_value='testnet')
    @patch('Wallet.views.build_qr_data_uri', return_value='data:image/png;base64,abc')
    @patch('Wallet.views._sync_user_evr_balance', return_value=Decimal('100000000'))
    @patch('Wallet.views._get_user_asset_balances', return_value=({'WHOLE': Decimal('2')}, None))
    @patch('Wallet.views._get_asset_units', return_value=0)
    @patch('Wallet.views._get_wallet_profiles', return_value=[])
    @patch('Wallet.views._get_or_create_main_wallet_profile', return_value=None)
    @patch('Wallet.views._get_user_primary_address', return_value='ESendAddress123')
    @patch('Wallet.views._derive_user_wif_for_address', return_value='L1sendwif')
    @patch('Wallet.views.create_and_send_asset_transfer_transaction', return_value={'txid': 'asset-txid'})
    def test_send_funds_keeps_asset_and_evr_change_at_source_address(
        self,
        mock_send_asset,
        mock_derive_wif,
        mock_get_primary_address,
        mock_main_profile,
        mock_wallet_profiles,
        mock_get_asset_units,
        mock_get_user_asset_balances,
        mock_sync_balance,
        mock_build_qr,
        mock_network_mode,
    ):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('send_funds'),
            {
                'currency': 'WHOLE',
                'recipient_address': 'ERecipient123',
                'amount': '1',
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_send_asset.called, response.content.decode())
        called_kwargs = mock_send_asset.call_args.kwargs
        self.assertEqual(called_kwargs['from_address'], 'ESendAddress123')
        self.assertNotIn('asset_change_address', called_kwargs)
        self.assertNotIn('change_address', called_kwargs)
        self.assertEqual(called_kwargs['wif_keys'], ['L1sendwif'])
        self.assertContains(response, 'Successfully sent 1 to ERecipient123. Transaction ID: asset-txid')

    @patch('Wallet.views.RPC.getassetdata', return_value={'units': 8})
    def test_get_asset_units_forces_admin_assets_to_be_indivisible(self, mock_getassetdata):
        self.assertEqual(views._get_asset_units('ROOT!'), 0)
        mock_getassetdata.assert_not_called()

    @patch('Wallet.views.RPC.getassetdata', return_value=None)
    @patch('Wallet.views._active_network_mode', return_value='testnet')
    def test_get_asset_units_uses_tracked_asset_for_active_network(self, mock_network_mode, mock_getassetdata):
        from Wallet.models import TrackedAsset

        TrackedAsset.objects.create(symbol='SAME', network_mode='mainnet', units=2)
        TrackedAsset.objects.create(symbol='SAME', network_mode='testnet', units=5)

        self.assertEqual(views._get_asset_units('SAME'), 5)


class WalletPreferencesViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='prefs-user',
            email='prefs@example.com',
            password='testpass123',
        )
        self.user_wallet = UserWallet.objects.create(
            user=self.user,
            name='Prefs Wallet',
            entropy='prefs-entropy',
            passphrase=''
        )

    def test_wallet_preferences_page_renders(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('wallet_preferences'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portfolio/preferences.html')
        self.assertContains(response, 'Wallet Preferences')

    def test_wallet_preferences_save_persists_values(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('wallet_preferences'),
            {
                'default_home_tab': 'profiles',
                'default_send_currency': 'WHOLE',
                'default_transaction_limit': '50',
                'default_confirmation_behavior': 'warn',
                'default_receive_qr_style': 'minimal',
                'address_label_style': 'masked',
                'profile_sort_order': 'name_desc',
                'auto_sync_balance': 'on',
                'auto_validate_recipient': 'on',
                'auto_copy_receive_address': 'on',
                'show_receive_qr': 'on',
                'show_zero_balances': 'on',
                'show_change_addresses': 'on',
                'show_profile_network_badges': 'on',
                'highlight_main_profile': 'on',
                'hide_balance_on_open': 'on',
                'compact_cards': 'on',
                'confirm_external_links': 'on',
                'enable_address_tooltips': 'on',
                'prefer_main_profile_on_receive': 'on',
                'nft_image_uri_template': 'https://ipfs.io/ipfs/{cid}/{filename}',
                'transaction_refresh_seconds': '45',
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        preferences = WalletPreferences.objects.get(wallet=self.user_wallet)
        self.assertEqual(preferences.default_home_tab, 'profiles')
        self.assertEqual(preferences.default_send_currency, 'WHOLE')
        self.assertEqual(preferences.default_transaction_limit, '50')
        self.assertEqual(preferences.default_confirmation_behavior, 'warn')
        self.assertEqual(preferences.default_receive_qr_style, 'minimal')
        self.assertEqual(preferences.address_label_style, 'masked')
        self.assertEqual(preferences.profile_sort_order, 'name_desc')
        self.assertTrue(preferences.auto_sync_balance)
        self.assertTrue(preferences.auto_validate_recipient)
        self.assertTrue(preferences.auto_copy_receive_address)
        self.assertTrue(preferences.show_receive_qr)
        self.assertTrue(preferences.show_zero_balances)
        self.assertTrue(preferences.show_change_addresses)
        self.assertTrue(preferences.show_profile_network_badges)
        self.assertTrue(preferences.highlight_main_profile)
        self.assertTrue(preferences.hide_balance_on_open)
        self.assertTrue(preferences.compact_cards)
        self.assertTrue(preferences.confirm_external_links)
        self.assertTrue(preferences.enable_address_tooltips)
        self.assertTrue(preferences.prefer_main_profile_on_receive)
        self.assertEqual(preferences.nft_image_uri_template, 'https://ipfs.io/ipfs/{cid}/{filename}')
        self.assertEqual(preferences.transaction_refresh_seconds, 45)
