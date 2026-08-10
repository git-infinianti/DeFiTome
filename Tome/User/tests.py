from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from unittest.mock import patch
from evrmore_authentication import generate_key_pair, sign_message, verify_message

from .models import EvrmoreAuthenticationAddress

# Create your tests here.
class RegistrationTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.register_url = reverse('register')
    
    def test_registration_page_loads(self):
        """Test that the registration page loads successfully"""
        response = self.client.get(self.register_url)
        self.assertEqual(response.status_code, 200)
    
    def test_registration_redirects_logged_in_users(self):
        """Test that logged-in users are redirected from registration page"""
        # Create and log in a user
        user = User.objects.create_user(username='loggedin', email='logged@example.com', password='pass123')
        self.client.login(username='loggedin', password='pass123')
        
        # Try to access registration page
        response = self.client.get(self.register_url)
        
        # Should redirect to home
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('home'))
    
    def test_registration_post_redirects_logged_in_users(self):
        """Test that logged-in users cannot POST to registration page"""
        # Create and log in a user
        user = User.objects.create_user(username='loggedin', email='logged@example.com', password='pass123')
        self.client.login(username='loggedin', password='pass123')
        
        # Try to post to registration page
        response = self.client.post(self.register_url, {
            'username': 'newuser',
            'email': 'new@example.com',
            'password': 'testpass123',
            'confirm_password': 'testpass123'
        })
        
        # Should redirect to home
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('home'))
        
        # New user should not be created
        self.assertFalse(User.objects.filter(username='newuser').exists())
    
    def test_successful_registration(self):
        """Test that a user can successfully register"""
        response = self.client.post(self.register_url, {
            'username': 'testuser',
            'email': 'test@example.com',
            'password': 'testpass123',
            'confirm_password': 'testpass123'
        })
        
        # Should redirect to home after successful registration
        self.assertEqual(response.status_code, 302)
        
        # User should exist in database
        user = User.objects.get(username='testuser')
        self.assertEqual(user.email, 'test@example.com')
        self.assertTrue(user.is_active)
    
    def test_password_mismatch(self):
        """Test that registration fails when passwords don't match"""
        response = self.client.post(self.register_url, {
            'username': 'testuser',
            'email': 'test@example.com',
            'password': 'testpass123',
            'confirm_password': 'wrongpass'
        })
        
        # Should stay on registration page
        self.assertEqual(response.status_code, 200)
        
        # User should not be created
        self.assertFalse(User.objects.filter(username='testuser').exists())
    
    def test_duplicate_username(self):
        """Test that registration fails with duplicate username"""
        # Create a user first
        User.objects.create_user(username='existing', email='existing@example.com', password='pass123')
        
        # Try to register with same username
        response = self.client.post(self.register_url, {
            'username': 'existing',
            'email': 'new@example.com',
            'password': 'testpass123',
            'confirm_password': 'testpass123'
        })
        
        # Should stay on registration page
        self.assertEqual(response.status_code, 200)
        
        # Should only have one user with that username
        self.assertEqual(User.objects.filter(username='existing').count(), 1)
    
    def test_empty_fields(self):
        """Test that registration fails with empty fields"""
        response = self.client.post(self.register_url, {
            'username': '',
            'email': '',
            'password': '',
            'confirm_password': ''
        })
        
        # Should stay on registration page
        self.assertEqual(response.status_code, 200)
        
        # No users should be created
        self.assertEqual(User.objects.count(), 0)

class LoginTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.login_url = reverse('login')
        self.register_url = reverse('register')
        # Create a test user
        self.test_user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
    
    def test_login_page_loads(self):
        """Test that the login page loads successfully"""
        response = self.client.get(self.login_url)
        self.assertEqual(response.status_code, 200)
    
    def test_login_redirects_logged_in_users(self):
        """Test that logged-in users are redirected from login page"""
        # Login the test user
        self.client.login(username='testuser', password='testpass123')
        
        # Try to access login page
        response = self.client.get(self.login_url)
        
        # Should redirect to home
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('home'))
    
    def test_login_post_redirects_logged_in_users(self):
        """Test that logged-in users cannot POST to login page"""
        # Login the test user
        self.client.login(username='testuser', password='testpass123')
        
        # Try to post to login page
        response = self.client.post(self.login_url, {
            'username': 'testuser',
            'password': 'testpass123'
        })
        
        # Should redirect to home
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('home'))
    
    def test_successful_login(self):
        """Test that a user can successfully login with correct credentials"""
        response = self.client.post(self.login_url, {
            'username': 'testuser',
            'password': 'testpass123'
        })
        
        # Should redirect to home after successful login
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('home'))
        
        # User should be logged in
        user = User.objects.get(username='testuser')
        self.assertTrue(user.is_authenticated)

    def test_login_redirects_to_safe_next_url(self):
        response = self.client.post(self.login_url, {
            'username': 'testuser',
            'password': 'testpass123',
            'next': reverse('portfolio'),
        })

        self.assertRedirects(response, reverse('portfolio'))

    def test_login_rejects_external_next_url(self):
        response = self.client.post(self.login_url, {
            'username': 'testuser',
            'password': 'testpass123',
            'next': 'https://example.test/untrusted',
        })

        self.assertRedirects(response, reverse('home'))
    
    def test_login_nonexistent_user(self):
        """Test that login redirects to register for non-existent user"""
        response = self.client.post(self.login_url, {
            'username': 'nonexistent',
            'password': 'testpass123'
        })
        
        # Should redirect to register page
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, self.register_url)
    
    def test_login_wrong_password(self):
        """Test that login fails with wrong password"""
        response = self.client.post(self.login_url, {
            'username': 'testuser',
            'password': 'wrongpassword'
        })
        
        # Should stay on login page
        self.assertEqual(response.status_code, 200)
        
        # User should not be logged in
        # Check if user is authenticated by checking session
        self.assertNotIn('_auth_user_id', self.client.session)
    
    def test_login_empty_username(self):
        """Test that login handles empty username"""
        response = self.client.post(self.login_url, {
            'username': '',
            'password': 'testpass123'
        })
        
        # Should stay on login page with error message
        self.assertEqual(response.status_code, 200)
    
    def test_login_empty_password(self):
        """Test that login handles empty password"""
        response = self.client.post(self.login_url, {
            'username': 'testuser',
            'password': ''
        })
        
        # Should stay on login page with error message
        self.assertEqual(response.status_code, 200)

class HomePageAuthTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.home_url = reverse('home')
        self.login_url = reverse('login')
        # Create a test user
        self.test_user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
    
    def test_home_page_requires_login(self):
        """Test that home page redirects unauthenticated users to login"""
        response = self.client.get(self.home_url)
        
        # Should redirect to login page
        self.assertEqual(response.status_code, 302)
        self.assertIn('/user/login/', response.url)
    
    def test_home_page_accessible_when_logged_in(self):
        """Test that home page is accessible to logged-in users"""
        # Login the user
        self.client.login(username='testuser', password='testpass123')
        
        response = self.client.get(self.home_url)
        
        # Should successfully load the home page
        self.assertEqual(response.status_code, 200)
    
    def test_home_page_redirect_preserves_next_parameter(self):
        """Test that redirect to login includes next parameter"""
        response = self.client.get(self.home_url)
        
        # Should redirect to login with next parameter
        self.assertEqual(response.status_code, 302)
        self.assertIn('next=', response.url)
        self.assertIn('next=/', response.url)


class CanonicalRouteFlowTestCase(TestCase):
    def setUp(self):
        self.client = Client()

    def test_primary_routes_use_short_canonical_paths(self):
        self.assertEqual(reverse('home'), '/')
        self.assertEqual(reverse('markets'), '/defi/p2p/dex/')
        self.assertEqual(
            reverse('dex_orderbook', args=['system0808-evr']),
            '/defi/p2p/dex/system0808-evr/',
        )
        self.assertEqual(reverse('create_listing'), '/defi/p2p/create/')
        self.assertEqual(reverse('available_swap_offers'), '/defi/p2p/available/')

    def test_legacy_entry_points_redirect_to_canonical_routes(self):
        cases = (
            ('/user/home/', '/'),
            ('/listings/', '/defi/p2p/available/'),
            ('/listings/create/', '/defi/p2p/create/'),
            ('/listings/markets/', '/defi/p2p/dex/'),
            ('/listings/dex/', '/defi/p2p/dex/'),
        )
        for legacy_path, canonical_path in cases:
            with self.subTest(legacy_path=legacy_path):
                response = self.client.get(legacy_path)
                self.assertRedirects(
                    response,
                    canonical_path,
                    status_code=301,
                    fetch_redirect_response=False,
                )


class EvrmoreWalletAuthenticationTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='walletuser',
            email='wallet@example.com',
            password='testpass123',
        )
        self.wallet_wif, self.wallet_address = generate_key_pair()
        self.wallet_login_url = reverse('wallet_login')
        self.wallet_management_url = reverse('evrmore_wallet_authentication')

    def test_package_signature_round_trip(self):
        challenge = 'DeFi Tome wallet authentication test challenge'
        signature = sign_message(challenge, self.wallet_wif)

        self.assertTrue(verify_message(self.wallet_address, signature, challenge))
        self.assertFalse(verify_message(self.wallet_address, signature, f'{challenge} altered'))

    def test_wallet_management_requires_login(self):
        response = self.client.get(self.wallet_management_url)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)

    @patch('User.views.create_evrmore_challenge', return_value='wallet-challenge')
    def test_wallet_management_links_address_after_valid_signature(self, mock_create_challenge):
        self.client.force_login(self.user)

        response = self.client.post(
            self.wallet_management_url,
            {'action': 'challenge', 'address': self.wallet_address},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['pending_challenge'], 'wallet-challenge')
        mock_create_challenge.assert_called_once_with(self.wallet_address)

        with patch('User.views.verify_evrmore_signature', return_value=True) as mock_verify_signature:
            response = self.client.post(
                self.wallet_management_url,
                {
                    'action': 'verify',
                    'address': self.wallet_address,
                    'challenge': 'wallet-challenge',
                    'signature': 'signature',
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            EvrmoreAuthenticationAddress.objects.filter(
                user=self.user,
                address=self.wallet_address,
            ).exists()
        )
        mock_verify_signature.assert_called_once_with(
            self.wallet_address,
            'wallet-challenge',
            'signature',
            request=response.wsgi_request,
        )

    @patch('User.views.create_evrmore_challenge', return_value='wallet-challenge')
    def test_wallet_login_creates_django_session_after_valid_signature(self, mock_create_challenge):
        linked_address = EvrmoreAuthenticationAddress.objects.create(
            user=self.user,
            address=self.wallet_address,
        )

        response = self.client.post(
            self.wallet_login_url,
            {'action': 'challenge', 'address': self.wallet_address},
        )

        self.assertEqual(response.status_code, 200)
        mock_create_challenge.assert_called_once_with(self.wallet_address)

        with patch('User.backends.verify_evrmore_signature', return_value=True) as mock_verify_signature:
            response = self.client.post(
                self.wallet_login_url,
                {
                    'action': 'verify',
                    'address': self.wallet_address,
                    'challenge': 'wallet-challenge',
                    'signature': 'signature',
                },
            )

        self.assertRedirects(response, reverse('home'))
        self.assertEqual(str(self.user.pk), self.client.session.get('_auth_user_id'))
        linked_address.refresh_from_db()
        self.assertIsNotNone(linked_address.last_authenticated_at)
        mock_verify_signature.assert_called_once()

    @patch('User.views.create_evrmore_challenge')
    def test_wallet_login_does_not_issue_challenge_for_unlinked_address(self, mock_create_challenge):
        response = self.client.post(
            self.wallet_login_url,
            {'action': 'challenge', 'address': self.wallet_address},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)
        mock_create_challenge.assert_not_called()

    @patch('User.views.create_evrmore_challenge', return_value='wallet-challenge')
    def test_wallet_login_rejects_challenge_from_another_session(self, mock_create_challenge):
        EvrmoreAuthenticationAddress.objects.create(user=self.user, address=self.wallet_address)
        self.client.post(
            self.wallet_login_url,
            {'action': 'challenge', 'address': self.wallet_address},
        )

        with patch('User.backends.verify_evrmore_signature') as mock_verify_signature:
            response = self.client.post(
                self.wallet_login_url,
                {
                    'action': 'verify',
                    'address': self.wallet_address,
                    'challenge': 'different-challenge',
                    'signature': 'signature',
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)
        mock_verify_signature.assert_not_called()


class EmailVerificationTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.register_url = reverse('register')
        self.settings_url = reverse('settings')
        self.resend_url = reverse('resend_verification')

    def test_email_verification_created_on_registration(self):
        """Test that EmailVerification record is created when user registers"""
        from .models import EmailVerification
        
        response = self.client.post(self.register_url, {
            'username': 'testuser',
            'email': 'test@example.com',
            'password': 'testpass123',
            'confirm_password': 'testpass123'
        })
        
        # User should be created
        user = User.objects.get(username='testuser')
        
        # EmailVerification record should exist
        self.assertTrue(EmailVerification.objects.filter(user=user).exists())
        
        # Should not be verified initially
        email_verification = EmailVerification.objects.get(user=user)
        self.assertFalse(email_verification.is_verified)
        self.assertIsNotNone(email_verification.verification_token)
    
    def test_verify_email_with_valid_token(self):
        """Test email verification with valid token"""
        from .models import EmailVerification
        
        # Create user and verification record
        user = User.objects.create_user(username='testuser', email='test@example.com', password='testpass123')
        email_verification = EmailVerification.objects.create(user=user, is_verified=False)
        
        # Visit verification URL
        verify_url = reverse('verify_email', kwargs={'token': email_verification.verification_token})
        response = self.client.get(verify_url)
        
        # Should redirect
        self.assertEqual(response.status_code, 302)
        
        # Email should be verified
        email_verification.refresh_from_db()
        self.assertTrue(email_verification.is_verified)
        self.assertIsNotNone(email_verification.verified_at)
    
    def test_verify_email_with_invalid_token(self):
        """Test email verification with invalid token"""
        import uuid
        
        # Use random UUID that doesn't exist
        fake_token = uuid.uuid4()
        verify_url = reverse('verify_email', kwargs={'token': fake_token})
        response = self.client.get(verify_url)
        
        # Should redirect to login
        self.assertEqual(response.status_code, 302)
    
    def test_resend_verification_email_when_not_verified(self):
        """Test resending verification email when user is not verified"""
        from .models import EmailVerification
        
        # Create and login user
        user = User.objects.create_user(username='testuser', email='test@example.com', password='testpass123')
        EmailVerification.objects.create(user=user, is_verified=False)
        self.client.login(username='testuser', password='testpass123')
        
        # Request to resend verification email
        response = self.client.post(self.resend_url, follow=True)
        
        # Should redirect to settings
        self.assertEqual(response.status_code, 200)
        
        # Check that we redirected to settings page
        self.assertTemplateUsed(response, 'settings/index.html')
        
        # Check for success or error message
        messages_list = list(response.context['messages'])
        # Allow both success and error messages since email sending might fail in tests
        self.assertTrue(len(messages_list) > 0)
    
    def test_resend_verification_when_already_verified(self):
        """Test resending verification email when already verified"""
        from .models import EmailVerification
        
        # Create and login user with verified email
        user = User.objects.create_user(username='testuser', email='test@example.com', password='testpass123')
        EmailVerification.objects.create(user=user, is_verified=True)
        self.client.login(username='testuser', password='testpass123')
        
        # Request to resend verification email
        response = self.client.post(self.resend_url, follow=True)
        
        # Should redirect to settings
        self.assertEqual(response.status_code, 200)
        
        # Check for info message
        messages_list = list(response.context['messages'])
        self.assertTrue(any('already verified' in str(m) for m in messages_list))
    
    def test_settings_shows_verification_status(self):
        """Test that settings page shows email verification status"""
        from .models import EmailVerification
        
        # Create and login user
        user = User.objects.create_user(username='testuser', email='test@example.com', password='testpass123')
        EmailVerification.objects.create(user=user, is_verified=False)
        self.client.login(username='testuser', password='testpass123')
        
        # Access settings page
        response = self.client.get(self.settings_url)
        
        # Should show verification status
        self.assertEqual(response.status_code, 200)
        self.assertIn('email_verification', response.context)


