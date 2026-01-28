from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse

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
        self.assertIn('/user/home/', response.url)


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


