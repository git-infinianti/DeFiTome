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
