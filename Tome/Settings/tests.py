from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from unittest.mock import MagicMock, patch

from .models import UserProfile
from Tome import rpc_client


class SettingsNetworkModeTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(
			username='network-user',
			email='network@example.com',
			password='safe-password-123',
		)
		self.client.login(username='network-user', password='safe-password-123')

	def test_user_profile_defaults_to_testnet(self):
		self.client.get(reverse('settings'))
		profile = UserProfile.objects.get(user=self.user)
		self.assertEqual(profile.network_mode, 'testnet')
		self.assertEqual(profile.rpc_endpoint_mode, 'public')

	def test_change_network_mode_to_mainnet(self):
		response = self.client.post(
			reverse('change_network_mode'),
			{'network_mode': 'mainnet'},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		profile = UserProfile.objects.get(user=self.user)
		self.assertEqual(profile.network_mode, 'mainnet')

	def test_invalid_network_mode_is_rejected(self):
		UserProfile.objects.create(user=self.user, network_mode='testnet')

		response = self.client.post(
			reverse('change_network_mode'),
			{'network_mode': 'invalid-network'},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		profile = UserProfile.objects.get(user=self.user)
		self.assertEqual(profile.network_mode, 'testnet')

	def test_change_rpc_endpoint_mode_to_local(self):
		response = self.client.post(
			reverse('change_rpc_endpoint_mode'),
			{'rpc_endpoint_mode': 'local'},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		profile = UserProfile.objects.get(user=self.user)
		self.assertEqual(profile.rpc_endpoint_mode, 'local')

	def test_invalid_rpc_endpoint_mode_is_rejected(self):
		UserProfile.objects.create(user=self.user, rpc_endpoint_mode='public')

		response = self.client.post(
			reverse('change_rpc_endpoint_mode'),
			{'rpc_endpoint_mode': 'bad-choice'},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		profile = UserProfile.objects.get(user=self.user)
		self.assertEqual(profile.rpc_endpoint_mode, 'public')


class RoutedRpcClientTests(TestCase):
	def tearDown(self):
		rpc_client.clear_active_network_mode()
		rpc_client.clear_active_rpc_endpoint_mode()

	def test_normalize_network_mode_defaults_to_testnet(self):
		self.assertEqual(rpc_client.normalize_network_mode(None), 'testnet')
		self.assertEqual(rpc_client.normalize_network_mode('invalid'), 'testnet')
		self.assertEqual(rpc_client.normalize_network_mode('mainnet'), 'mainnet')

	def test_normalize_rpc_endpoint_mode_defaults_to_public(self):
		self.assertEqual(rpc_client.normalize_rpc_endpoint_mode(None), 'public')
		self.assertEqual(rpc_client.normalize_rpc_endpoint_mode('invalid'), 'public')
		self.assertEqual(rpc_client.normalize_rpc_endpoint_mode('local'), 'local')

	def test_using_rpc_endpoint_mode_restores_the_previous_mode(self):
		rpc_client.set_active_rpc_endpoint_mode('local')

		with rpc_client.using_rpc_endpoint_mode('public') as active_mode:
			self.assertEqual(active_mode, 'public')
			self.assertEqual(rpc_client.get_active_rpc_endpoint_mode(), 'public')

		self.assertEqual(rpc_client.get_active_rpc_endpoint_mode(), 'local')

	@patch('Tome.rpc_client.RoutedEvrmoreClient._get_public_client')
	@patch('Tome.rpc_client.RoutedEvrmoreClient._get_local_client')
	def test_testnet_local_mode_uses_only_the_local_client(self, mock_local_client, mock_public_client):
		local_client = MagicMock()
		public_client = MagicMock()

		local_client.getblockchaininfo.return_value = {'chain': 'test'}
		mock_local_client.return_value = local_client
		mock_public_client.return_value = public_client

		routed_client = rpc_client.RoutedEvrmoreClient()
		rpc_client.set_active_network_mode('testnet')
		rpc_client.set_active_rpc_endpoint_mode('local')
		result = routed_client.getblockchaininfo()

		self.assertEqual(result, {'chain': 'test'})
		local_client.getblockchaininfo.assert_called_once()
		mock_public_client.assert_not_called()

	@patch('Tome.rpc_client.RoutedEvrmoreClient._get_public_client')
	@patch('Tome.rpc_client.RoutedEvrmoreClient._get_local_client')
	def test_public_failure_does_not_fall_back_to_local(self, mock_local_client, mock_public_client):
		local_client = MagicMock()
		public_client = MagicMock()

		public_client.getblockchaininfo.side_effect = Exception('public endpoint unavailable')
		mock_local_client.return_value = local_client
		mock_public_client.return_value = public_client

		routed_client = rpc_client.RoutedEvrmoreClient()
		rpc_client.set_active_network_mode('testnet')
		with self.assertRaises(Exception):
			routed_client.getblockchaininfo()

		public_client.getblockchaininfo.assert_called_once()
		mock_local_client.assert_not_called()

	@patch('Tome.rpc_client.RoutedEvrmoreClient._get_public_client')
	@patch('Tome.rpc_client.RoutedEvrmoreClient._get_local_client')
	def test_mainnet_local_mode_uses_only_the_local_client(self, mock_local_client, mock_public_client):
		local_client = MagicMock()
		public_client = MagicMock()

		local_client.getblockchaininfo.return_value = {'chain': 'main'}
		mock_local_client.return_value = local_client
		mock_public_client.return_value = public_client

		routed_client = rpc_client.RoutedEvrmoreClient()
		rpc_client.set_active_network_mode('mainnet')
		rpc_client.set_active_rpc_endpoint_mode('local')
		result = routed_client.getblockchaininfo()

		self.assertEqual(result, {'chain': 'main'})
		local_client.getblockchaininfo.assert_called_once()
		mock_public_client.assert_not_called()
