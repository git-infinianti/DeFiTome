from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase
from django.test import TestCase
from django.urls import reverse

from Media.address_metadata import (
	AddressMetadataTagIssuanceError,
	discover_address_metadata_tags,
	issue_address_metadata_tag,
)
from Media.kubo_api import KuboAPIUploader
from Media.models import AddressMetadataTag
from Wallet.models import UserWallet, WalletAddress
from Wallet.rip10 import build_address_name_tag, build_signed_metadata


class KuboAPIUploaderTests(SimpleTestCase):
	@patch('Media.kubo_api.httpx.Client')
	def test_download_json_uses_configured_kubo_cat_endpoint(self, mock_client_class):
		response = MagicMock()
		response.iter_bytes.return_value = [b'{"tag":', b'{"tag_type":"ANT"}}']
		stream_context = MagicMock()
		stream_context.__enter__.return_value = response
		client = MagicMock()
		client.stream.return_value = stream_context
		client_context = MagicMock()
		client_context.__enter__.return_value = client
		mock_client_class.return_value = client_context

		uploader = KuboAPIUploader(api_base_url='http://kubo.test/api/v0')
		result = uploader.download_json('QmMetadataCid')

		self.assertEqual(result, {'tag': {'tag_type': 'ANT'}})
		client.stream.assert_called_once_with(
			'POST',
			'http://kubo.test/api/v0/cat',
			params={'arg': 'QmMetadataCid'},
		)
		response.raise_for_status.assert_called_once()

	def test_download_rejects_invalid_or_oversized_cid(self):
		uploader = KuboAPIUploader(api_base_url='http://kubo.test/api/v0')

		with self.assertRaises(ValueError):
			uploader.download_bytes('')

		with self.assertRaises(ValueError):
			uploader.download_bytes('bad cid')


class AddressMetadataTagServiceTests(TestCase):
	address = 'EL5MFdaF8msRaUEDu9mxSNniPSswNmNRgq'

	def setUp(self):
		network_mode_patch = patch('Media.address_metadata.get_current_network_mode', return_value='testnet')
		network_mode_patch.start()
		self.addCleanup(network_mode_patch.stop)
		self.user = User.objects.create_user(username='tagger', password='test-password')
		self.wallet = UserWallet.objects.create(
			user=self.user,
			name='Tag Wallet',
			entropy='00000000000000000000000000000000',
			passphrase='',
		)
		WalletAddress.objects.create(
			wallet=self.wallet,
			network_mode='testnet',
			address=self.address,
			wif='owner-wif',
			account=0,
			index=0,
			is_change=False,
		)

	@patch('Media.address_metadata._verify_metadata_signature', return_value=True)
	@patch('Media.address_metadata._sign_metadata_message', return_value='metadata-signature')
	@patch(
		'Media.address_metadata.create_and_send_issue_unique_transaction',
		return_value={'txid': 'tag-transaction-id'},
	)
	@patch('Media.address_metadata.KuboAPIUploader')
	@patch('Media.address_metadata.RPC')
	def test_issue_uploads_signed_metadata_and_uses_owner_token_wif(
		self,
		mock_rpc,
		mock_uploader_class,
		mock_issue_unique,
		_mock_sign,
		_mock_verify,
	):
		mock_rpc.listassetbalancesbyaddress.return_value = {'TOMETAGS!': '1'}
		mock_uploader_class.return_value.upload_bytes.return_value = MagicMock(cid='QmMetadataCid')

		tag_record = issue_address_metadata_tag(
			user=self.user,
			main_asset='TOMETAGS',
			tag_type='ANT',
			target_address=self.address,
			tag_payload=build_address_name_tag(self.address, 'DeFi Tome'),
		)

		self.assertEqual(tag_record.status, AddressMetadataTag.Status.BROADCAST)
		self.assertEqual(tag_record.asset_name, 'TOMETAGS#ANT_C38D582B')
		self.assertEqual(tag_record.ipfs_cid, 'QmMetadataCid')
		self.assertEqual(tag_record.transaction_id, 'tag-transaction-id')
		self.assertTrue(tag_record.signature_verified)
		self.assertEqual(
			mock_issue_unique.call_args.kwargs['wif_keys'],
			['owner-wif'],
		)
		self.assertEqual(
			mock_issue_unique.call_args.kwargs['asset_tags'],
			['ANT_C38D582B'],
		)
		self.assertEqual(
			mock_uploader_class.return_value.upload_bytes.call_args.kwargs['cid_version'],
			0,
		)

	@patch('Media.address_metadata._verify_metadata_signature', return_value=True)
	@patch('Media.address_metadata._sign_metadata_message', return_value='metadata-signature')
	@patch(
		'Media.address_metadata.create_and_send_issue_unique_transaction',
		side_effect=RuntimeError('node connection lost'),
	)
	@patch('Media.address_metadata.KuboAPIUploader')
	@patch('Media.address_metadata.RPC')
	def test_ambiguous_broadcast_is_persisted_for_manual_verification(
		self,
		mock_rpc,
		mock_uploader_class,
		_mock_issue_unique,
		_mock_sign,
		_mock_verify,
	):
		mock_rpc.listassetbalancesbyaddress.return_value = {'TOMETAGS!': '1'}
		mock_uploader_class.return_value.upload_bytes.return_value = MagicMock(cid='QmMetadataCid')

		with self.assertRaises(AddressMetadataTagIssuanceError) as error:
			issue_address_metadata_tag(
				user=self.user,
				main_asset='TOMETAGS',
				tag_type='ANT',
				target_address=self.address,
				tag_payload=build_address_name_tag(self.address, 'DeFi Tome'),
			)

		tag_record = error.exception.tag
		self.assertEqual(tag_record.status, AddressMetadataTag.Status.BROADCAST_UNKNOWN)
		self.assertEqual(tag_record.ipfs_cid, 'QmMetadataCid')
		self.assertIn('node connection lost', tag_record.error_message)

	@patch('Media.address_metadata._verify_metadata_signature', return_value=True)
	@patch('Media.address_metadata.KuboAPIUploader')
	@patch('Media.address_metadata.RPC')
	def test_discovery_filters_by_crc32_and_verifies_ipfs_metadata(
		self,
		mock_rpc,
		mock_uploader_class,
		_mock_verify,
	):
		metadata = build_signed_metadata(
			build_address_name_tag(self.address, 'DeFi Tome'),
			'metadata-signature',
		)
		mock_rpc.listassetbalancesbyaddress.return_value = {
			'TOMETAGS#ANT_C38D582B': '1',
			'TOMETAGS#ANT_00000000': '1',
		}
		mock_rpc.getassetdata.return_value = {'ipfs_hash': 'QmMetadataCid'}
		mock_uploader_class.return_value.download_json.return_value = metadata

		results = discover_address_metadata_tags(self.address)

		self.assertEqual(len(results), 1)
		self.assertEqual(results[0].asset_name, 'TOMETAGS#ANT_C38D582B')
		self.assertTrue(results[0].is_valid)


class AddressMetadataTagViewTests(TestCase):
	address = 'EL5MFdaF8msRaUEDu9mxSNniPSswNmNRgq'

	def setUp(self):
		self.user = User.objects.create_user(username='tag-view-user', password='test-password')
		UserWallet.objects.create(
			user=self.user,
			name='Tag View Wallet',
			entropy='00000000000000000000000000000000',
			passphrase='',
		)
		self.client.force_login(self.user)

	@patch('Media.views.issue_address_metadata_tag')
	@patch('Media.views.list_controlled_addresses', return_value=[address])
	def test_create_ant_submits_a_typed_payload(self, _mock_addresses, mock_issue):
		mock_issue.return_value = AddressMetadataTag.objects.create(
			user=self.user,
			target_address=self.address,
			main_asset='TOMETAGS',
			tag_type='ANT',
			asset_name='TOMETAGS#ANT_C38D582B',
			transaction_id='tag-transaction-id',
			status=AddressMetadataTag.Status.BROADCAST,
		)

		response = self.client.post(
			reverse('address_metadata_tag_create'),
			{
				'main_asset': 'TOMETAGS',
				'tag_type': 'ANT',
				'target_address': self.address,
				'address_name': 'DeFi Tome',
				'address_name_mime': 'text/x-markdown; charset=UTF-8',
			},
		)

		self.assertRedirects(response, reverse('address_metadata_tag_list'))
		self.assertEqual(mock_issue.call_args.kwargs['tag_type'], 'ANT')
		self.assertEqual(
			mock_issue.call_args.kwargs['tag_payload']['address_name'],
			'DeFi Tome',
		)
