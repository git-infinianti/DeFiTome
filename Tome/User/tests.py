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

