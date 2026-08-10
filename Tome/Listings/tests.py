from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import Client, TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from DeFi.models import SwapOffer
from API.models import DexMarketEventMessage, MessageChannelPolicy
from Media.models import IPFSUpload
from Wallet.models import TrackedAsset
from Wallet.rpc import InsufficientSpendableBalance
from Settings.models import MembershipPlan, UserMembership
from .models import BalanceLock, LimitOrder, Listing, ListingItem, MarketFavorite, MarketOrder, NFT, OrderExecution, TradingPair, UniqueAssetMintRequest
from .templatetags.market_tags import asset_tooltip, asset_type_label, market_symbol
from .views import _match_order, _settle_market_fill, _sync_order_balance_lock


class AtomicSwapCreationTestCase(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(username='seller', password='testpass123')
		self.client = Client()
		MessageChannelPolicy.objects.create(
			channel_key='atomic_swap_transfer',
			channel_name='SYSTEM~SWAPS',
			network_mode='testnet',
			version=1,
			status='active',
			owner_account=self.user,
			manager_account=self.user,
			allowed_stages=[
				'offer_created',
				'settlement_lock_created',
				'settlement_build_failed',
				'settlement_pending_reconciliation',
				'settlement_broadcasted',
				'swap_cancelled',
				'swap_expired',
			],
			chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
		)

	@patch(
		'Listings.views._get_user_asset_balances',
		return_value=({'COLLECTIBLE#001': Decimal('1')}, None),
	)
	@patch('Listings.views.record_atomic_swap_stage_event')
	@patch('Listings.views.RPC.getassetdata', return_value={'ipfs_hash': 'QmMetadataCid'})
	@patch(
		'Listings.views.KuboAPIUploader.download_json',
		return_value={
			'schema': 'defitome.unique-asset-metadata',
			'version': 1,
			'asset_name': 'COLLECTIBLE#001',
			'source_ipfs_cid': 'QmMetadataCid',
			'name': 'Collection Piece #001',
			'description': 'On-chain metadata',
			'image': 'ipfs://QmImageCid/image.png',
			'external_url': '',
			'attributes': [
				{'trait_type': 'root_asset', 'value': 'COLLECTIBLE'},
				{'trait_type': 'asset_tag', 'value': '001'},
			],
			'raw': {
				'schema': 'defitome.unique-asset-metadata',
				'version': 1,
				'asset_name': 'COLLECTIBLE#001',
				'name': 'Collection Piece #001',
				'description': 'On-chain metadata',
				'image': 'ipfs://QmImageCid/image.png',
				'external_url': '',
				'attributes': [
					{'trait_type': 'root_asset', 'value': 'COLLECTIBLE'},
					{'trait_type': 'asset_tag', 'value': '001'},
				],
			},
		},
	)
	def test_unique_asset_creates_an_nft_atomic_swap(self, _mock_download_json, _mock_getassetdata, mock_record_event, _mock_asset_balances):
		mock_record_event.return_value.status = 'broadcasted'
		self.client.login(username='seller', password='testpass123')

		response = self.client.post(
			reverse('create_listing'),
			{
				'title': 'Collection Piece',
				'description': 'An on-chain unique asset.',
				'price': '2',
				'token_offered': 'COLLECTIBLE#001',
				'preferred_token': 'EVR',
				'expiry_days': '7',
			},
		)

		self.assertRedirects(response, reverse('available_swap_offers'))
		listing = Listing.objects.get()
		nft = NFT.objects.get()
		swap_offer = SwapOffer.objects.get()
		self.assertEqual(listing.token_offered, 'COLLECTIBLE#001')
		self.assertTrue(listing.item.is_nft)
		self.assertEqual(nft.token_id, 'COLLECTIBLE#001')
		self.assertEqual(nft.metadata_ipfs_cid, 'QmMetadataCid')
		self.assertEqual(nft.metadata_version, 1)
		self.assertEqual(nft.image_ipfs_cid, 'QmImageCid')
		self.assertEqual(swap_offer.offer_token, 'COLLECTIBLE#001')
		self.assertEqual(swap_offer.request_token, 'EVR')

	@patch(
		'Listings.views._get_user_asset_balances',
		return_value=({'COLLECTIBLE#006': Decimal('1')}, None),
	)
	@patch('Listings.views.record_atomic_swap_stage_event')
	@patch('Listings.views.RPC.getassetdata', return_value={'ipfs_hash': 'QmMetadataCid'})
	@patch(
		'Listings.views.KuboAPIUploader.download_json',
		return_value={
			'schema': 'defitome.unique-asset-metadata',
			'version': 1,
			'asset_name': 'COLLECTIBLE#006',
			'source_ipfs_cid': 'QmMetadataCid',
			'name': 'Collection Piece #006',
			'description': 'On-chain metadata',
			'image': 'ipfs://QmImageCid/image.png',
			'external_url': '',
			'attributes': [
				{'trait_type': 'root_asset', 'value': 'COLLECTIBLE'},
				{'trait_type': 'asset_tag', 'value': '006'},
			],
			'raw': {
				'schema': 'defitome.unique-asset-metadata',
				'version': 1,
				'asset_name': 'COLLECTIBLE#006',
				'name': 'Collection Piece #006',
				'description': 'On-chain metadata',
				'image': 'ipfs://QmImageCid/image.png',
				'external_url': '',
				'attributes': [
					{'trait_type': 'root_asset', 'value': 'COLLECTIBLE'},
					{'trait_type': 'asset_tag', 'value': '006'},
				],
			},
		},
	)
	def test_atomic_swap_creation_rolls_back_when_channel_publish_fails(
		self,
		_mock_download_json,
		_mock_getassetdata,
		mock_record_event,
		_mock_asset_balances,
	):
		mock_record_event.return_value.status = 'failed'
		mock_record_event.return_value.error_message = 'No channel holder is available.'
		self.client.login(username='seller', password='testpass123')

		response = self.client.post(
			reverse('create_listing'),
			{
				'price': '2',
				'token_offered': 'COLLECTIBLE#006',
				'preferred_token': 'EVR',
				'expiry_days': '7',
			},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'No channel holder is available.')
		self.assertFalse(Listing.objects.exists())
		self.assertFalse(NFT.objects.exists())
		self.assertFalse(SwapOffer.objects.exists())

	@patch(
		'Listings.views._get_user_asset_balances',
		return_value=({'COLLECTIBLE#002': Decimal('1')}, None),
	)
	@patch('Listings.views.RPC.getassetdata', return_value={'ipfs_hash': 'QmMetadataCid'})
	@patch('Listings.views.KuboAPIUploader.download_json', return_value={'name': 'Unstructured NFT', 'description': 'Missing schema markers', 'image': 'ipfs://QmImageCid/image.png'})
	def test_non_compliant_unique_metadata_is_rejected(self, _mock_download_json, _mock_getassetdata, _mock_asset_balances):
		self.client.login(username='seller', password='testpass123')

		response = self.client.post(
			reverse('create_listing'),
			{
				'price': '2',
				'token_offered': 'COLLECTIBLE#002',
				'preferred_token': 'EVR',
				'expiry_days': '7',
			},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Unique asset metadata schema is not supported.')
		self.assertFalse(Listing.objects.exists())
		self.assertFalse(NFT.objects.exists())

	@patch(
		'Listings.views._get_user_asset_balances',
		return_value=({'TOKEN/ALPHA': Decimal('3')}, None),
	)
	def test_non_unique_asset_rejected_for_atomic_swap_creation(self, _mock_asset_balances):
		self.client.login(username='seller', password='testpass123')

		response = self.client.post(
			reverse('create_listing'),
			{
				'title': 'Fungible Token Offer',
				'description': 'Not allowed for atomic swaps now.',
				'price': '2',
				'token_offered': 'TOKEN/ALPHA',
				'preferred_token': 'EVR',
				'expiry_days': '7',
			},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Atomic swap offers only support unique assets (ROOT#TAG).')
		self.assertFalse(Listing.objects.exists())

	@patch(
		'Listings.views._get_user_asset_balances',
		return_value=({'COLLECTIBLE#003': Decimal('1')}, None),
	)
	def test_atomic_swap_creation_requires_verified_messaging_channel(self, _mock_asset_balances):
		MessageChannelPolicy.objects.all().delete()
		self.client.login(username='seller', password='testpass123')

		response = self.client.post(
			reverse('create_listing'),
			{
				'price': '2',
				'token_offered': 'COLLECTIBLE#003',
				'preferred_token': 'EVR',
				'expiry_days': '7',
			},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Atomic swaps require a verified active messaging channel for the full swap lifecycle.')
		self.assertFalse(Listing.objects.exists())
		self.assertFalse(NFT.objects.exists())
		self.assertFalse(SwapOffer.objects.exists())

	@patch(
		'Listings.views._get_user_asset_balances',
		return_value=({'COLLECTIBLE#004': Decimal('1')}, None),
	)
	def test_atomic_swap_creation_rejects_untracked_settlement_asset(self, _mock_asset_balances):
		self.client.login(username='seller', password='testpass123')

		response = self.client.post(
			reverse('create_listing'),
			{
				'price': '2',
				'token_offered': 'COLLECTIBLE#004',
				'preferred_token': 'TOKEN/SUB',
				'expiry_days': '7',
			},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Settlement asset must be EVR or a tracked main/sub asset on this network.')
		self.assertFalse(Listing.objects.exists())

	@patch('Listings.views._derive_user_wif_for_address', return_value='test-wif')
	@patch('Listings.views._get_user_primary_address', return_value='EUserAddress')
	@patch('Listings.views.create_and_send_issue_unique_transaction', return_value={'txid': 'mint-txid-001'})
	@patch('Listings.views.KuboAPIUploader.upload_fileobj')
	@patch('Listings.views.KuboAPIUploader.upload_bytes')
	@patch(
		'Listings.views._get_user_asset_balances',
		return_value=({'COLLECTIBLE!': Decimal('1')}, None),
	)
	def test_admin_holder_can_mint_unique_asset_with_metadata_cid_persistence(
		self,
		_mock_asset_balances,
		mock_upload_bytes,
		mock_upload_fileobj,
		_mock_issue_unique,
		_mock_primary_address,
		_mock_wif,
	):
		class UploadResult:
			def __init__(self, cid, name='file'):
				self.cid = cid
				self.name = name

		mock_upload_fileobj.return_value = UploadResult('QmImageUploadCid', 'photo.png')
		mock_upload_bytes.return_value = UploadResult('QmMintMetadataCid', 'metadata.json')
		image_file = SimpleUploadedFile('photo.png', b'image-bytes', content_type='image/png')
		UniqueAssetMintRequest.objects.create(
			creator=self.user,
			network_mode='testnet',
			admin_asset_symbol='COLLECTIBLE!',
			root_name='COLLECTIBLE',
			asset_tag='002',
			unique_asset_name='COLLECTIBLE#002',
			metadata_ipfs_cid='QmFailedCid',
			status=UniqueAssetMintRequest.STATUS_FAILED,
			error_message='previous failure',
		)

		self.client.login(username='seller', password='testpass123')
		response = self.client.post(
			reverse('create_listing'),
			{
				'action': 'mint_unique_asset',
				'mint_admin_asset': 'COLLECTIBLE!',
				'mint_asset_tag': '002',
				'mint_name': 'Collection Piece #002',
				'mint_description': 'Minted through listing flow',
				'mint_image_file': image_file,
			},
		)

		self.assertRedirects(response, reverse('create_listing'))
		mint_record = UniqueAssetMintRequest.objects.get()
		self.assertEqual(mint_record.unique_asset_name, 'COLLECTIBLE#002')
		self.assertEqual(mint_record.metadata_ipfs_cid, 'QmMintMetadataCid')
		self.assertEqual(mint_record.metadata_version, 1)
		self.assertEqual(mint_record.mint_txid, 'mint-txid-001')
		self.assertEqual(mint_record.status, UniqueAssetMintRequest.STATUS_BROADCAST)
		self.assertEqual(mint_record.error_message, '')
		self.assertEqual(UniqueAssetMintRequest.objects.count(), 1)
		self.assertIn('ipfs://QmImageUploadCid/photo.png', str(mint_record.metadata_json))
		media_record = IPFSUpload.objects.get(user=self.user)
		self.assertEqual(media_record.ipfs_hash, 'QmImageUploadCid')
		self.assertEqual(media_record.original_filename, 'photo.png')

	@patch('Listings.views._derive_user_wif_for_address', return_value='test-wif')
	@patch('Listings.views._get_user_primary_address', return_value='EUserAddress')
	@patch('Listings.views.create_and_send_issue_unique_transaction', return_value='mint-txid-002')
	@patch('Listings.views.KuboAPIUploader.upload_fileobj')
	@patch('Listings.views.KuboAPIUploader.upload_bytes')
	@patch(
		'Listings.views._get_user_asset_balances',
		return_value=({'COLLECTIBLE!': Decimal('1')}, None),
	)
	def test_admin_holder_can_mint_unique_asset_using_existing_media_upload(
		self,
		_mock_asset_balances,
		mock_upload_bytes,
		mock_upload_fileobj,
		_mock_issue_unique,
		_mock_primary_address,
		_mock_wif,
	):
		class UploadResult:
			def __init__(self, cid, name='file'):
				self.cid = cid
				self.name = name

		mock_upload_bytes.return_value = UploadResult('QmMintMetadataCid2', 'metadata.json')
		existing_media = IPFSUpload.objects.create(
			user=self.user,
			original_filename='existing.png',
			ipfs_hash='QmExistingMediaCid',
		)

		self.client.login(username='seller', password='testpass123')
		response = self.client.post(
			reverse('create_listing'),
			{
				'action': 'mint_unique_asset',
				'mint_admin_asset': 'COLLECTIBLE!',
				'mint_asset_tag': '003',
				'mint_name': 'Collection Piece #003',
				'mint_description': 'Minted with existing media',
				'mint_existing_upload_id': str(existing_media.pk),
			},
		)

		self.assertRedirects(response, reverse('create_listing'))
		mint_record = UniqueAssetMintRequest.objects.get(unique_asset_name='COLLECTIBLE#003')
		self.assertEqual(mint_record.metadata_ipfs_cid, 'QmMintMetadataCid2')
		self.assertEqual(mint_record.mint_txid, 'mint-txid-002')
		self.assertIn('ipfs://QmExistingMediaCid/existing.png', str(mint_record.metadata_json))
		mock_upload_fileobj.assert_not_called()
		self.assertEqual(IPFSUpload.objects.filter(user=self.user).count(), 1)

	@patch('Listings.views._derive_user_wif_for_address', return_value='test-wif')
	@patch('Listings.views._get_user_primary_address', return_value='EUserAddress')
	@patch('Listings.views.create_and_send_issue_unique_transaction', return_value='mint-txid-003')
	@patch('Listings.views.KuboAPIUploader.upload_fileobj')
	@patch('Listings.views.KuboAPIUploader.upload_bytes')
	@patch(
		'Listings.views._get_user_asset_balances',
		return_value=({'COLLECTIBLE!': Decimal('1')}, None),
	)
	def test_existing_media_selection_rejects_non_image_files(
		self,
		_mock_asset_balances,
		_mock_upload_bytes,
		mock_upload_fileobj,
		_mock_issue_unique,
		_mock_primary_address,
		_mock_wif,
	):
		existing_media = IPFSUpload.objects.create(
			user=self.user,
			original_filename='notes.txt',
			ipfs_hash='QmPlainTextCid',
		)

		self.client.login(username='seller', password='testpass123')
		response = self.client.post(
			reverse('create_listing'),
			{
				'action': 'mint_unique_asset',
				'mint_admin_asset': 'COLLECTIBLE!',
				'mint_asset_tag': '004',
				'mint_name': 'Collection Piece #004',
				'mint_description': 'Should fail for non-image existing media',
				'mint_existing_upload_id': str(existing_media.pk),
			},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Selected media must be an image file for metadata v1.')
		self.assertFalse(UniqueAssetMintRequest.objects.filter(unique_asset_name='COLLECTIBLE#004').exists())
		mock_upload_fileobj.assert_not_called()

	@patch('Listings.views._derive_user_wif_for_address', return_value='test-wif')
	@patch('Listings.views._get_user_primary_address', return_value='EUserAddress')
	@patch('Listings.views.create_and_send_issue_unique_transaction', return_value='mint-txid-004')
	@patch('Listings.views.KuboAPIUploader.upload_fileobj')
	@patch('Listings.views.KuboAPIUploader.upload_bytes')
	@patch(
		'Listings.views._get_user_asset_balances',
		return_value=({'COLLECTIBLE!': Decimal('1')}, None),
	)
	def test_mint_rejects_when_new_file_and_existing_media_are_both_provided(
		self,
		_mock_asset_balances,
		_mock_upload_bytes,
		mock_upload_fileobj,
		_mock_issue_unique,
		_mock_primary_address,
		_mock_wif,
	):
		existing_media = IPFSUpload.objects.create(
			user=self.user,
			original_filename='existing.png',
			ipfs_hash='QmExistingMediaCidB',
		)
		image_file = SimpleUploadedFile('photo.png', b'image-bytes', content_type='image/png')

		self.client.login(username='seller', password='testpass123')
		response = self.client.post(
			reverse('create_listing'),
			{
				'action': 'mint_unique_asset',
				'mint_admin_asset': 'COLLECTIBLE!',
				'mint_asset_tag': '005',
				'mint_name': 'Collection Piece #005',
				'mint_description': 'Should fail when both sources provided',
				'mint_existing_upload_id': str(existing_media.pk),
				'mint_image_file': image_file,
			},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Choose either a new image upload or an existing media image, not both.')
		self.assertFalse(UniqueAssetMintRequest.objects.filter(unique_asset_name='COLLECTIBLE#005').exists())
		mock_upload_fileobj.assert_not_called()

	@patch(
		'Listings.views._get_user_asset_balances',
		return_value=({'COLLECTIBLE#001': Decimal('1')}, None),
	)
	def test_atomic_swap_settlement_token_choices_include_fungible_assets(self, _mock_asset_balances):
		TrackedAsset.objects.create(symbol='MAINCOIN', network_mode='testnet', asset_type=TrackedAsset.ASSET_TYPE_MAIN)
		TrackedAsset.objects.create(symbol='TOKEN/SUB', network_mode='testnet', asset_type=TrackedAsset.ASSET_TYPE_SUB)
		TrackedAsset.objects.create(symbol='ADMIN!', network_mode='testnet', asset_type=TrackedAsset.ASSET_TYPE_ADMIN)

		self.client.login(username='seller', password='testpass123')
		response = self.client.get(reverse('create_listing'))

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context['settlement_token_options'], ['EVR', 'MAINCOIN', 'TOKEN/SUB'])


class MarketAuthorizationAndNetworkIsolationTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(username='market-user', password='testpass123')
		self.client = Client()

	@patch('Listings.views._get_user_asset_balances', return_value=({'TESTASSET': Decimal('5')}, None))
	def test_market_creation_requires_authorization(self, _mock_balances):
		self.client.login(username='market-user', password='testpass123')
		response = self.client.post(
			reverse('create_market'),
			{'base_token': 'TESTASSET', 'quote_token': 'EVR'},
		)

		self.assertRedirects(response, reverse('markets'))
		self.assertFalse(TradingPair.objects.exists())

	@patch('Listings.views._get_user_asset_balances', return_value=({'TESTASSET': Decimal('5')}, None))
	def test_authorized_user_can_create_market_on_active_network(self, _mock_balances):
		plan = MembershipPlan.objects.create(
			code='pro',
			name='Pro',
			feature_codes=['market_management'],
		)
		UserMembership.objects.create(user=self.user, plan=plan, status='active')

		self.client.login(username='market-user', password='testpass123')
		response = self.client.post(
			reverse('create_market'),
			{'base_token': 'TESTASSET', 'quote_token': 'EVR'},
		)

		self.assertRedirects(response, reverse('markets'))
		pair = TradingPair.objects.get()
		self.assertEqual(pair.network_mode, 'testnet')
		self.assertTrue(DexMarketEventMessage.objects.filter(
			trading_pair_id=pair.pk,
			stage='market_created',
			status='recorded',
		).exists())

	@patch('Listings.views._get_user_asset_balances', return_value=({'HELD': Decimal('5')}, None))
	def test_crafted_market_post_rejects_asset_outside_live_wallet(self, _mock_balances):
		plan = MembershipPlan.objects.create(
			code='pro-live-wallet-only',
			name='Pro Live Wallet Only',
			feature_codes=['market_management'],
		)
		UserMembership.objects.create(user=self.user, plan=plan, status='active')

		self.client.login(username='market-user', password='testpass123')
		response = self.client.post(
			reverse('create_market'),
			{'base_token': 'NOTHELD', 'quote_token': 'EVR'},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'NOTHELD is not available in your live wallet balance.')
		self.assertFalse(TradingPair.objects.exists())

	@patch(
		'Listings.views._get_user_asset_balances',
		return_value=({'TOKEN/ALPHA': Decimal('5'), 'COLLECTIBLE#001': Decimal('1'), 'ADMIN!': Decimal('1')}, None),
	)
	def test_only_token_assets_are_listed_as_market_candidates(self, _mock_balances):
		plan = MembershipPlan.objects.create(
			code='pro-token-listing',
			name='Pro Token Listing',
			feature_codes=['market_management'],
		)
		UserMembership.objects.create(user=self.user, plan=plan, status='active')

		self.client.login(username='market-user', password='testpass123')
		response = self.client.get(reverse('create_market'))

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context['available_base_tokens'], ['TOKEN/ALPHA'])
		self.assertEqual(response.context['available_quote_tokens'], ['EVR', 'TOKEN/ALPHA'])
		self.assertContains(response, 'TOKEN/ALPHA', html=False)
		self.assertContains(response, 'ALPHA · Sub asset', html=False)

	@patch('Listings.views._get_user_asset_balances', return_value=({'TOKEN/ALPHA': Decimal('5'), 'COLLECTIBLE#001': Decimal('1'), 'ADMIN!': Decimal('1')}, None))
	def test_unique_and_admin_assets_are_rejected_for_market_creation(self, _mock_balances):
		plan = MembershipPlan.objects.create(
			code='pro-token-reject',
			name='Pro Token Reject',
			feature_codes=['market_management'],
		)
		UserMembership.objects.create(user=self.user, plan=plan, status='active')

		self.client.login(username='market-user', password='testpass123')
		response_unique = self.client.post(
			reverse('create_market'),
			{'base_token': 'COLLECTIBLE#001', 'quote_token': 'EVR'},
		)
		self.assertEqual(response_unique.status_code, 200)
		self.assertContains(response_unique, 'Only token assets can be used as a market base asset. Unique and admin assets are not allowed.')
		self.assertFalse(TradingPair.objects.exists())

		response_admin = self.client.post(
			reverse('create_market'),
			{'base_token': 'ADMIN!', 'quote_token': 'EVR'},
		)
		self.assertEqual(response_admin.status_code, 200)
		self.assertContains(response_admin, 'Only token assets can be used as a market base asset. Unique and admin assets are not allowed.')
		self.assertFalse(TradingPair.objects.exists())

	def test_markets_view_only_shows_current_network(self):
		TradingPair.objects.create(base_token='MAIN', quote_token='EVR', network_mode='mainnet')
		TradingPair.objects.create(base_token='TEST', quote_token='EVR', network_mode='testnet')

		self.client.login(username='market-user', password='testpass123')
		response = self.client.get(reverse('markets'))

		self.assertEqual(response.status_code, 200)
		markets = list(response.context['markets'])
		self.assertEqual(len(markets), 1)
		self.assertEqual(markets[0].base_token, 'TEST')

	def test_authorized_manager_can_see_and_resume_paused_market(self):
		plan = MembershipPlan.objects.create(
			code='market-operator',
			name='Market Operator',
			feature_codes=['market_management'],
		)
		UserMembership.objects.create(user=self.user, plan=plan, status='active')
		pair = TradingPair.objects.create(
			base_token='TOKEN',
			quote_token='EVR',
			network_mode='testnet',
			is_active=False,
		)
		self.client.login(username='market-user', password='testpass123')

		market_page = self.client.get(reverse('markets'))

		self.assertContains(market_page, 'Paused')
		self.assertContains(market_page, 'Resume')
		self.assertEqual(self.client.get(reverse('toggle_market_status', args=[pair.pk])).status_code, 405)
		response = self.client.post(reverse('toggle_market_status', args=[pair.pk]))
		pair.refresh_from_db()
		self.assertRedirects(response, reverse('markets'))
		self.assertTrue(pair.is_active)

	def test_pair_orientation_can_change_only_before_market_activity(self):
		plan = MembershipPlan.objects.create(
			code='pair-operator',
			name='Pair Operator',
			feature_codes=['market_management'],
		)
		UserMembership.objects.create(user=self.user, plan=plan, status='active')
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')
		self.client.login(username='market-user', password='testpass123')

		response = self.client.post(reverse('reverse_market_pair', args=[pair.pk]))

		pair.refresh_from_db()
		self.assertRedirects(response, reverse('markets'))
		self.assertEqual((pair.base_token, pair.quote_token), ('EVR', 'TOKEN'))
		self.assertEqual(pair.pair_slug, 'evr-token')

		LimitOrder.objects.create(
			user=self.user,
			trading_pair=pair,
			side='buy',
			price=Decimal('1'),
			quantity=Decimal('1'),
		)
		blocked = self.client.post(reverse('reverse_market_pair', args=[pair.pk]), follow=True)
		pair.refresh_from_db()
		self.assertContains(blocked, 'Pair orientation cannot change after orders or trades exist')
		self.assertEqual((pair.base_token, pair.quote_token), ('EVR', 'TOKEN'))

	def test_pair_slug_is_readable_and_collision_safe_for_native_asset_names(self):
		simple = TradingPair.objects.create(base_token='SYSTEM0808', quote_token='EVR', network_mode='testnet')
		sub_asset = TradingPair.objects.create(base_token='ROOT/SUB', quote_token='EVR', network_mode='testnet')

		self.assertEqual(simple.pair_slug, 'system0808-evr')
		self.assertTrue(sub_asset.pair_slug.startswith('root-sub-evr-'))
		self.assertEqual(
			reverse('dex_orderbook', args=[simple.pair_slug]),
			'/defi/p2p/dex/system0808-evr/',
		)

	def test_legacy_numeric_trade_url_redirects_to_pair_slug(self):
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')
		self.client.login(username='market-user', password='testpass123')

		response = self.client.get(reverse('legacy_market_trade'), {'pair': pair.pk})

		self.assertRedirects(
			response,
			reverse('dex_orderbook', args=[pair.pair_slug]),
			status_code=301,
		)

	def test_market_favorite_toggle_adds_and_removes_for_current_user(self):
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')
		self.client.login(username='market-user', password='testpass123')
		url = reverse('toggle_market_favorite', args=[pair.pk])

		added = self.client.post(url, {'filter': 'TOKEN'})

		self.assertRedirects(added, f"{reverse('markets')}?filter=TOKEN")
		self.assertTrue(MarketFavorite.objects.filter(user=self.user, trading_pair=pair).exists())
		market_page = self.client.get(reverse('markets'))
		self.assertContains(market_page, 'Remove TOKEN/EVR from favorites')
		other_pair = TradingPair.objects.create(base_token='OTHER', quote_token='EVR', network_mode='testnet')
		favorites_page = self.client.get(reverse('markets'), {'filter': 'FAVORITES'})
		self.assertEqual(list(favorites_page.context['markets']), [pair])
		self.assertNotContains(favorites_page, reverse('dex_orderbook', args=[other_pair.pair_slug]))

		removed = self.client.post(url)

		self.assertRedirects(removed, reverse('markets'))
		self.assertFalse(MarketFavorite.objects.filter(user=self.user, trading_pair=pair).exists())

	def test_market_favorite_toggle_is_post_only_and_network_scoped(self):
		mainnet_pair = TradingPair.objects.create(base_token='MAIN', quote_token='EVR', network_mode='mainnet')
		self.client.login(username='market-user', password='testpass123')
		url = reverse('toggle_market_favorite', args=[mainnet_pair.pk])

		self.assertEqual(self.client.get(url).status_code, 405)
		self.assertEqual(self.client.post(url).status_code, 404)
		self.assertFalse(MarketFavorite.objects.exists())

	@patch('Listings.views._get_user_asset_balances', return_value=({'TOKENA': Decimal('5'), 'TOKENB': Decimal('5')}, None))
	def test_reverse_market_creation_is_rejected_as_redundant(self, _mock_balances):
		plan = MembershipPlan.objects.create(
			code='pro-no-redundant',
			name='Pro No Redundant',
			feature_codes=['market_management'],
		)
		UserMembership.objects.create(user=self.user, plan=plan, status='active')
		TradingPair.objects.create(base_token='TOKENB', quote_token='TOKENA', network_mode='testnet')

		self.client.login(username='market-user', password='testpass123')
		response = self.client.post(
			reverse('create_market'),
			{'base_token': 'TOKENA', 'quote_token': 'TOKENB'},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Redundant market rejected. TOKENB/TOKENA already exists on this network.')
		self.assertEqual(TradingPair.objects.count(), 1)

	@patch('Listings.views._get_user_asset_balances', return_value=({'TOKENA': Decimal('5')}, None))
	def test_identical_base_and_quote_are_rejected(self, _mock_balances):
		plan = MembershipPlan.objects.create(
			code='pro-no-identical',
			name='Pro No Identical',
			feature_codes=['market_management'],
		)
		UserMembership.objects.create(user=self.user, plan=plan, status='active')

		self.client.login(username='market-user', password='testpass123')
		response = self.client.post(
			reverse('create_market'),
			{'base_token': 'TOKENA', 'quote_token': 'TOKENA'},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Base token and quote token must be different.')
		self.assertFalse(TradingPair.objects.exists())

	@patch('Listings.views._get_user_asset_balances', return_value=({'TOKENA': Decimal('5')}, None))
	def test_existing_exact_market_pair_is_rejected(self, _mock_balances):
		plan = MembershipPlan.objects.create(
			code='pro-no-duplicate-pair',
			name='Pro No Duplicate Pair',
			feature_codes=['market_management'],
		)
		UserMembership.objects.create(user=self.user, plan=plan, status='active')
		TradingPair.objects.create(base_token='TOKENA', quote_token='EVR', network_mode='testnet')

		self.client.login(username='market-user', password='testpass123')
		response = self.client.post(
			reverse('create_market'),
			{'base_token': 'TOKENA', 'quote_token': 'EVR'},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Trading pair TOKENA/EVR already exists.')
		self.assertEqual(TradingPair.objects.count(), 1)

	def test_database_rejects_reversed_pair_on_same_network(self):
		TradingPair.objects.create(base_token='TOKENA', quote_token='TOKENB', network_mode='testnet')

		with self.assertRaises(IntegrityError):
			TradingPair.objects.create(base_token='TOKENB', quote_token='TOKENA', network_mode='testnet')

	@patch('Listings.views._get_user_asset_balances', return_value=({'TOKENA': Decimal('5'), 'TOKENB': Decimal('5')}, None))
	def test_market_form_synchronizes_pair_options(self, _mock_balances):
		plan = MembershipPlan.objects.create(
			code='pro-pair-options',
			name='Pro Pair Options',
			feature_codes=['market_management'],
		)
		UserMembership.objects.create(user=self.user, plan=plan, status='active')

		self.client.login(username='market-user', password='testpass123')
		response = self.client.get(reverse('create_market'))

		self.assertContains(response, 'synchronizePairOptions')
		self.assertContains(response, "onchange=\"synchronizePairOptions('base')\"")
		self.assertContains(response, "onchange=\"synchronizePairOptions('quote')\"")

	@patch('Listings.views._get_user_asset_balances', return_value=({'$EQUITY': Decimal('5')}, None))
	def test_restricted_asset_market_is_security_capable(self, _mock_balances):
		plan = MembershipPlan.objects.create(
			code='pro-restricted-market',
			name='Pro Restricted Market',
			feature_codes=['market_management'],
		)
		UserMembership.objects.create(user=self.user, plan=plan, status='active')

		self.client.login(username='market-user', password='testpass123')
		response = self.client.post(
			reverse('create_market'),
			{'base_token': '$EQUITY', 'quote_token': 'EVR'},
		)

		self.assertRedirects(response, reverse('markets'))
		pair = TradingPair.objects.get()
		self.assertEqual(pair.instrument_type, TradingPair.INSTRUMENT_SECURITY_CAPABLE)


class MarketSettlementRoutingTests(TestCase):
	def setUp(self):
		self.buyer = User.objects.create_user(username='market-buyer', password='testpass123')
		self.seller = User.objects.create_user(username='market-seller', password='testpass123')

	def test_restricted_pair_is_always_security_capable(self):
		pair = TradingPair.objects.create(
			base_token='$EQUITY',
			quote_token='EVR',
			instrument_type=TradingPair.INSTRUMENT_TOKEN,
		)

		self.assertEqual(pair.instrument_type, TradingPair.INSTRUMENT_SECURITY_CAPABLE)

	@patch('Wallet.asset_units.rpc_client.get_asset_data', return_value={'units': 0})
	def test_limit_order_rejects_fractional_quantity_for_indivisible_asset(self, _mock_asset_data):
		pair = TradingPair.objects.create(base_token='WHOLE', quote_token='EVR', network_mode='testnet')
		self.client.login(username='market-buyer', password='testpass123')

		response = self.client.post(
			reverse('place_limit_order'),
			{'pair_id': pair.pk, 'side': 'sell', 'price': '1', 'quantity': '0.5'},
			follow=True,
		)

		self.assertContains(response, 'WHOLE is indivisible and must be sent as a whole number.')
		self.assertFalse(LimitOrder.objects.exists())

	@patch('Listings.views.get_asset_units')
	@patch('Listings.views._fetch_user_token_balance')
	def test_order_form_uses_stored_pair_data_without_live_rpc(
		self,
		mock_live_balance,
		mock_get_asset_units,
	):
		TrackedAsset.objects.create(symbol='WHOLE', network_mode='testnet', units=0)
		TrackedAsset.objects.create(symbol='CENTS', network_mode='testnet', units=2)
		pair = TradingPair.objects.create(base_token='WHOLE', quote_token='CENTS', network_mode='testnet')
		self.client.login(username='market-buyer', password='testpass123')

		response = self.client.get(reverse('dex_orderbook', args=[pair.pair_slug]))

		mock_live_balance.assert_not_called()
		mock_get_asset_units.assert_not_called()
		self.assertEqual(response.context['base_amount_step'], '1')
		self.assertEqual(response.context['quote_amount_step'], '0.01')
		self.assertContains(response, 'name="price" class="form-input" placeholder="0.00" step="0.01"', html=False)
		self.assertContains(response, 'name="quantity" class="form-input" placeholder="0.00" step="1"', html=False)

	@patch('Listings.views._fetch_user_token_balance')
	@patch('Listings.views.get_asset_units')
	def test_pair_balance_endpoint_returns_live_available_units_and_buy_capacity(
		self,
		mock_get_asset_units,
		mock_live_balance,
	):
		mock_get_asset_units.side_effect = lambda symbol, _network: 2
		mock_live_balance.side_effect = lambda _user, symbol: (
			Decimal('5') if symbol == 'TOKEN' else Decimal('10'),
			'',
		)
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='CASH', network_mode='testnet')
		LimitOrder.objects.create(
			user=self.seller,
			trading_pair=pair,
			side='sell',
			price=Decimal('2'),
			quantity=Decimal('4'),
		)
		BalanceLock.objects.create(
			user=self.buyer,
			asset_symbol='TOKEN',
			amount=Decimal('2'),
			status='locked',
		)
		BalanceLock.objects.create(
			user=self.buyer,
			asset_symbol='CASH',
			amount=Decimal('3'),
			status='locked',
		)
		self.client.login(username='market-buyer', password='testpass123')

		response = self.client.get(reverse('market_pair_balances', args=[pair.pk]))

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertEqual(payload['base_live_balance'], '5')
		self.assertEqual(payload['base_available_balance'], '3')
		self.assertEqual(payload['quote_live_balance'], '10')
		self.assertEqual(payload['quote_available_balance'], '7')
		self.assertEqual(payload['base_step'], '0.01')
		self.assertEqual(payload['quote_step'], '0.01')
		self.assertEqual(payload['market_buy_capacity'], '3.5')

	@patch('Listings.views._fetch_user_token_balance', return_value=(Decimal('0'), 'node unavailable'))
	@patch('Listings.views.get_asset_units', return_value=8)
	def test_pair_balance_endpoint_marks_rpc_failure_as_not_live(self, _mock_units, _mock_balance):
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')
		self.client.login(username='market-buyer', password='testpass123')

		response = self.client.get(reverse('market_pair_balances', args=[pair.pk]))

		self.assertFalse(response.json()['balances_are_live'])
		self.assertEqual(response.json()['balance_error'], 'node unavailable')

	@patch('Listings.views.record_market_stage_event')
	@patch('Listings.views._match_order')
	@patch('Listings.views._fetch_user_token_balance', return_value=(Decimal('5'), ''))
	@patch('Listings.views.get_asset_units', return_value=2)
	def test_sell_limit_order_reserves_base_and_rejects_overcommit(
		self,
		_mock_units,
		_mock_live_balance,
		_mock_match,
		_mock_event,
	):
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')
		self.client.login(username='market-seller', password='testpass123')

		first = self.client.post(
			reverse('place_limit_order'),
			{'pair_id': pair.pk, 'side': 'sell', 'price': '1', 'quantity': '4'},
		)
		second = self.client.post(
			reverse('place_limit_order'),
			{'pair_id': pair.pk, 'side': 'sell', 'price': '1', 'quantity': '2'},
			follow=True,
		)

		self.assertRedirects(first, reverse('dex_orderbook', args=[pair.pair_slug]))
		balance_lock = BalanceLock.objects.get(status='locked')
		self.assertEqual(balance_lock.asset_symbol, 'TOKEN')
		self.assertEqual(balance_lock.amount, Decimal('4'))
		self.assertContains(second, 'Insufficient TOKEN balance.')
		self.assertEqual(LimitOrder.objects.count(), 1)

	@patch('Listings.views.record_market_stage_event')
	@patch('Listings.views._match_order')
	@patch('Listings.views._fetch_user_token_balance', return_value=(Decimal('10'), ''))
	@patch('Listings.views.get_asset_units', return_value=2)
	def test_asset_quoted_buy_reserves_quote_asset(
		self,
		_mock_units,
		_mock_live_balance,
		_mock_match,
		_mock_event,
	):
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='CASH', network_mode='testnet')
		self.client.login(username='market-buyer', password='testpass123')

		self.client.post(
			reverse('place_limit_order'),
			{'pair_id': pair.pk, 'side': 'buy', 'price': '2', 'quantity': '3'},
		)

		balance_lock = BalanceLock.objects.get(status='locked')
		self.assertEqual(balance_lock.asset_symbol, 'CASH')
		self.assertEqual(balance_lock.amount, Decimal('6'))

	def test_partial_fill_reduces_sell_reservation_to_remaining_quantity(self):
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')
		order = LimitOrder.objects.create(
			user=self.seller,
			trading_pair=pair,
			side='sell',
			price=Decimal('2'),
			quantity=Decimal('3'),
			filled_quantity=Decimal('1'),
			status='partial',
		)

		balance_lock = _sync_order_balance_lock(order)

		self.assertEqual(balance_lock.asset_symbol, 'TOKEN')
		self.assertEqual(balance_lock.amount, Decimal('2'))
		self.assertEqual(balance_lock.status, 'locked')

	def test_order_book_server_renders_open_orders_and_marks_own_order(self):
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')
		LimitOrder.objects.create(
			user=self.buyer,
			trading_pair=pair,
			side='sell',
			price=Decimal('2'),
			quantity=Decimal('3'),
		)
		self.client.login(username='market-buyer', password='testpass123')

		response = self.client.get(reverse('dex_orderbook', args=[pair.pair_slug]))

		self.assertContains(response, '2.00000000')
		self.assertContains(response, '3.0000 · Yours')
		self.assertContains(response, 'refreshPairBalances')
		self.assertContains(response, 'window.setInterval(refreshPairBalances, 15000)')
		self.assertContains(response, 'id="market-available-value"', html=False)

	def test_order_book_chart_history_is_scoped_to_selected_pair(self):
		selected_pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')
		other_pair = TradingPair.objects.create(base_token='OTHER', quote_token='EVR', network_mode='testnet')
		OrderExecution.objects.create(
			trading_pair=selected_pair,
			buyer=self.buyer,
			seller=self.seller,
			price=Decimal('1.25'),
			quantity=Decimal('2'),
		)
		OrderExecution.objects.create(
			trading_pair=other_pair,
			buyer=self.buyer,
			seller=self.seller,
			price=Decimal('99'),
			quantity=Decimal('1'),
		)
		self.client.login(username='market-buyer', password='testpass123')

		response = self.client.get(reverse('dex_orderbook', args=[selected_pair.pair_slug]))

		self.assertEqual([trade['price'] for trade in response.context['chart_trades']], ['1.25000000'])
		self.assertContains(response, 'id="chart-trades-data"', html=False)
		self.assertContains(response, reverse('dex_orderbook', args=[other_pair.pair_slug]))
		self.assertNotContains(response, '&quot;price&quot;: &quot;99.00000000&quot;')

	def test_market_and_stop_loss_redirects_preserve_selected_pair(self):
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')
		self.client.login(username='market-buyer', password='testpass123')

		market_response = self.client.post(
			reverse('place_market_order'),
			{'pair_id': pair.pk, 'side': 'buy', 'quantity': '1'},
		)
		stop_response = self.client.post(
			reverse('place_stop_loss_order'),
			{'pair_id': pair.pk, 'side': 'sell', 'trigger_price': '1', 'quantity': '1'},
		)

		expected_url = reverse('dex_orderbook', args=[pair.pair_slug])
		self.assertRedirects(market_response, expected_url)
		self.assertRedirects(stop_response, expected_url)

	def test_raw_settlement_rejects_self_trade_before_wallet_access(self):
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')

		with self.assertRaisesMessage(ValueError, 'cannot execute against their own market orders'):
			_settle_market_fill(pair, self.buyer, self.buyer, Decimal('1'), Decimal('1'))

	def test_order_book_shows_active_atomic_listing_and_marks_creator(self):
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')
		item = ListingItem.objects.create(
			title='Collectible',
			description='',
			individual_price=Decimal('2'),
			total_price=Decimal('2'),
			is_nft=True,
		)
		listing = Listing.objects.create(
			item=item,
			seller=self.buyer,
			price=Decimal('2'),
			token_offered='COLLECTIBLE#1',
			preferred_token='EVR',
			network_mode='testnet',
		)
		SwapOffer.objects.create(
			initiator=self.buyer,
			listing=listing,
			offer_token='COLLECTIBLE#1',
			offer_amount=Decimal('1'),
			request_token='EVR',
			request_amount=Decimal('2'),
			network_mode='testnet',
			expires_at=timezone.now() + timedelta(days=1),
		)
		self.client.login(username='market-buyer', password='testpass123')

		response = self.client.get(reverse('dex_orderbook', args=[pair.pair_slug]))

		self.assertContains(response, 'COLLECTIBLE#1')
		self.assertContains(response, 'title="Native coin: EVR">EVR</span>', html=False)
		self.assertContains(response, 'Yours')

	@patch('Listings.views.get_asset_units', return_value=8)
	def test_market_order_cannot_execute_only_own_liquidity(self, _mock_units):
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')
		LimitOrder.objects.create(
			user=self.buyer,
			trading_pair=pair,
			side='sell',
			price=Decimal('1'),
			quantity=Decimal('1'),
		)
		self.client.login(username='market-buyer', password='testpass123')

		response = self.client.post(
			reverse('place_market_order'),
			{'pair_id': pair.pk, 'side': 'buy', 'quantity': '1'},
			follow=True,
		)

		self.assertContains(response, 'No orders available for immediate execution.')
		self.assertFalse(OrderExecution.objects.exists())
		self.assertFalse(MarketOrder.objects.exists())

	@patch('Listings.views._match_order')
	@patch('Listings.views._fetch_user_token_balance', return_value=(Decimal('5'), ''))
	@patch('Listings.views.get_asset_units', return_value=8)
	def test_limit_order_creation_records_channel_event(self, _mock_units, _mock_balance, _mock_match):
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR', network_mode='testnet')
		self.client.login(username='market-seller', password='testpass123')

		response = self.client.post(
			reverse('place_limit_order'),
			{'pair_id': pair.pk, 'side': 'sell', 'price': '1', 'quantity': '1'},
		)

		self.assertRedirects(response, reverse('dex_orderbook', args=[pair.pair_slug]))
		order = LimitOrder.objects.get()
		self.assertTrue(DexMarketEventMessage.objects.filter(
			trading_pair_id=pair.pk,
			order_id=order.pk,
			stage='order_created',
			status='recorded',
		).exists())

	def test_market_symbols_truncate_sub_asset_for_display_only(self):
		self.assertEqual(market_symbol('ROOT/SUB'), 'SUB')
		self.assertEqual(market_symbol('ROOT/PARENT/CHILD'), 'CHILD')
		self.assertEqual(market_symbol('#ROOT/SUB'), 'SUB')
		self.assertEqual(asset_type_label('ROOT/SUB'), 'Sub asset')
		self.assertEqual(asset_tooltip('ROOT/SUB'), 'Sub asset: ROOT/SUB')
		self.assertEqual(asset_tooltip('#ROOT/SUB'), 'Sub-qualifier asset: #ROOT/SUB')
		self.assertEqual(market_symbol('$EQUITY'), '$EQUITY')
		self.assertEqual(asset_type_label('$EQUITY'), 'Restricted asset')
		self.assertEqual(asset_type_label('EVR'), 'Native coin')

	@patch('Listings.views.create_and_send_atomic_asset_evr_swap_transaction')
	@patch('Listings.views._derive_user_wif_for_address', side_effect=['buyer-wif', 'seller-wif'])
	@patch('Listings.views._get_user_primary_address', side_effect=['buyer-address', 'seller-address'])
	def test_evr_market_fill_uses_raw_atomic_asset_evr_settlement(
		self,
		_mock_address,
		_mock_wif,
		mock_settlement,
	):
		mock_settlement.return_value = {'txid': 'a' * 64}
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR')

		txid = _settle_market_fill(pair, self.buyer, self.seller, Decimal('2'), Decimal('3'))

		self.assertEqual(txid, 'a' * 64)
		mock_settlement.assert_called_once_with(
			seller_address='seller-address',
			buyer_address='buyer-address',
			asset_name='TOKEN',
			asset_quantity=Decimal('2'),
			payment_evr=Decimal('6'),
			wif_keys=['seller-wif', 'buyer-wif'],
		)

	@patch('Listings.views.create_and_send_atomic_asset_asset_swap_transaction')
	@patch('Listings.views._derive_user_wif_for_address', side_effect=['buyer-wif', 'seller-wif'])
	@patch('Listings.views._get_user_primary_address', side_effect=['buyer-address', 'seller-address'])
	def test_asset_market_fill_uses_raw_atomic_asset_asset_settlement(
		self,
		_mock_address,
		_mock_wif,
		mock_settlement,
	):
		mock_settlement.return_value = {'txid': 'b' * 64}
		pair = TradingPair.objects.create(base_token='STOCK', quote_token='USDASSET')

		txid = _settle_market_fill(pair, self.buyer, self.seller, Decimal('2'), Decimal('3'))

		self.assertEqual(txid, 'b' * 64)
		mock_settlement.assert_called_once_with(
			seller_address='seller-address',
			buyer_address='buyer-address',
			seller_asset_name='STOCK',
			seller_asset_quantity=Decimal('2'),
			buyer_asset_name='USDASSET',
			buyer_asset_quantity=Decimal('6'),
			wif_keys=['seller-wif', 'buyer-wif'],
		)

	@patch('Listings.views._settle_market_fill', side_effect=ValueError('mempool rejected'))
	def test_rejected_settlement_does_not_record_or_fill_trade(self, _mock_settlement):
		pair = TradingPair.objects.create(base_token='TOKEN', quote_token='EVR')
		buy_order = LimitOrder.objects.create(
			user=self.buyer,
			trading_pair=pair,
			side='buy',
			price=Decimal('3'),
			quantity=Decimal('2'),
		)
		LimitOrder.objects.create(
			user=self.seller,
			trading_pair=pair,
			side='sell',
			price=Decimal('3'),
			quantity=Decimal('2'),
		)

		with self.assertRaisesMessage(ValueError, 'mempool rejected'):
			_match_order(buy_order)

		buy_order.refresh_from_db()
		self.assertEqual(buy_order.filled_quantity, Decimal('0'))
		self.assertEqual(buy_order.status, 'pending')
		self.assertFalse(OrderExecution.objects.exists())

	@patch('Listings.views._settle_market_fill')
	def test_unspendable_maker_is_deferred_and_next_maker_can_fill(self, mock_settlement):
		mock_settlement.side_effect = [
			InsufficientSpendableBalance(
				'Insufficient spendable SYSTEM0808 balance. Needed: 0.56, available: 0.'
			),
			'c' * 64,
		]
		pair = TradingPair.objects.create(base_token='SYSTEM0808', quote_token='EVR')
		best_maker = LimitOrder.objects.create(
			user=self.seller,
			trading_pair=pair,
			side='sell',
			price=Decimal('1'),
			quantity=Decimal('1'),
		)
		other_seller = User.objects.create_user(username='other-seller', password='testpass123')
		next_maker = LimitOrder.objects.create(
			user=other_seller,
			trading_pair=pair,
			side='sell',
			price=Decimal('2'),
			quantity=Decimal('1'),
		)
		taker = LimitOrder.objects.create(
			user=self.buyer,
			trading_pair=pair,
			side='buy',
			price=Decimal('2'),
			quantity=Decimal('1'),
		)

		_match_order(taker)

		best_maker.refresh_from_db()
		next_maker.refresh_from_db()
		taker.refresh_from_db()
		self.assertEqual(best_maker.status, 'pending')
		self.assertEqual(best_maker.filled_quantity, Decimal('0'))
		self.assertEqual(next_maker.status, 'filled')
		self.assertEqual(taker.status, 'filled')
		self.assertEqual(OrderExecution.objects.get().seller, other_seller)

	@patch(
		'Listings.views._settle_market_fill',
		side_effect=InsufficientSpendableBalance(
			'Insufficient spendable SYSTEM0808 balance. Needed: 0.56, available: 0.'
		),
	)
	@patch('Listings.views._get_verified_available_token_balance', return_value=Decimal('10'))
	def test_market_order_defers_when_all_makers_await_spendable_change(
		self,
		_mock_balance,
		_mock_settlement,
	):
		pair = TradingPair.objects.create(base_token='SYSTEM0808', quote_token='EVR', network_mode='testnet')
		maker = LimitOrder.objects.create(
			user=self.seller,
			trading_pair=pair,
			side='sell',
			price=Decimal('1'),
			quantity=Decimal('1'),
		)
		self.client.login(username='market-buyer', password='testpass123')

		response = self.client.post(
			reverse('place_market_order'),
			{'pair_id': pair.pk, 'side': 'buy', 'quantity': '0.56'},
			follow=True,
		)

		maker.refresh_from_db()
		self.assertContains(
			response,
			'Matching orders are awaiting spendable on-chain funds or change confirmation.',
		)
		self.assertEqual(maker.status, 'pending')
		self.assertFalse(MarketOrder.objects.exists())
		self.assertFalse(OrderExecution.objects.exists())


class SwapOfferNetworkIsolationTests(TestCase):
	def setUp(self):
		self.seller = User.objects.create_user(username='seller-net', password='testpass123')
		self.buyer = User.objects.create_user(username='buyer-net', password='testpass123')
		self.client = Client()

	def test_available_swaps_filters_by_active_network(self):
		SwapOffer.objects.create(
			initiator=self.seller,
			offer_token='ASSET1',
			offer_amount=Decimal('1'),
			request_token='EVR',
			request_amount=Decimal('1'),
			expires_at=timezone.now() + timedelta(days=1),
			network_mode='testnet',
		)
		SwapOffer.objects.create(
			initiator=self.seller,
			offer_token='ASSET2',
			offer_amount=Decimal('1'),
			request_token='EVR',
			request_amount=Decimal('1'),
			expires_at=timezone.now() + timedelta(days=1),
			network_mode='mainnet',
		)

		self.client.login(username='buyer-net', password='testpass123')
		response = self.client.get(reverse('available_swap_offers'))

		self.assertEqual(response.status_code, 200)
		offers = list(response.context['offers'])
		self.assertEqual(len(offers), 1)
		self.assertEqual(offers[0].offer_token, 'ASSET1')
