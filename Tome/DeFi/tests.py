from django.test import TestCase, Client
from django.contrib.auth.models import User
from decimal import Decimal
from .models import LiquidityPool, LiquidityPosition, SwapTransaction, PriceFeedSource, PriceFeedData, PriceFeedAggregation

class FeeDistributionTestCase(TestCase):
    """Test cases for community liquidity fee distribution"""
    
    def setUp(self):
        """Set up test data"""
        # Create test users
        self.user1 = User.objects.create_user(username='provider1', password='testpass123')
        self.user2 = User.objects.create_user(username='provider2', password='testpass123')
        self.trader = User.objects.create_user(username='trader', password='testpass123')
        
        # Create a test pool
        self.pool = LiquidityPool.objects.create(
            name='ETH/USDC Pool',
            token_a_symbol='ETH',
            token_b_symbol='USDC',
            token_a_reserve=Decimal('100.0'),
            token_b_reserve=Decimal('100000.0'),
            total_liquidity_tokens=Decimal('100.0'),
            fee_percentage=Decimal('0.30')  # 0.30% fee
        )
        
        # Create liquidity positions for user1 (60% of pool) and user2 (40% of pool)
        self.position1 = LiquidityPosition.objects.create(
            user=self.user1,
            pool=self.pool,
            liquidity_tokens=Decimal('60.0')
        )
        
        self.position2 = LiquidityPosition.objects.create(
            user=self.user2,
            pool=self.pool,
            liquidity_tokens=Decimal('40.0')
        )
        
        self.client = Client()
    
    def test_fee_accumulation_on_swap(self):
        """Test that fees are properly accumulated when swaps occur"""
        self.client.login(username='trader', password='testpass123')
        
        # Perform a swap
        response = self.client.post('/defi/testnet/swap/', {
            'pool_id': self.pool.id,
            'from_token': 'ETH',
            'to_token': 'USDC',
            'amount': '10.0'
        })
        
        # Refresh pool from database
        self.pool.refresh_from_db()
        
        # Check that fees were accumulated in the pool
        expected_fee = Decimal('10.0') * Decimal('0.30') / Decimal('100')
        self.assertGreater(self.pool.accumulated_token_a_fees, Decimal('0'))
        self.assertAlmostEqual(float(self.pool.accumulated_token_a_fees), float(expected_fee), places=6)
    
    def test_fair_fee_distribution_to_providers(self):
        """Test that fees are distributed fairly based on liquidity share"""
        self.client.login(username='trader', password='testpass123')
        
        # Perform a swap
        swap_amount = Decimal('10.0')
        response = self.client.post('/defi/testnet/swap/', {
            'pool_id': self.pool.id,
            'from_token': 'ETH',
            'to_token': 'USDC',
            'amount': str(swap_amount)
        })
        
        # Refresh positions from database
        self.position1.refresh_from_db()
        self.position2.refresh_from_db()
        
        # Calculate expected fees
        total_fee = swap_amount * Decimal('0.30') / Decimal('100')
        expected_fee_user1 = total_fee * (Decimal('60.0') / Decimal('100.0'))  # 60% share
        expected_fee_user2 = total_fee * (Decimal('40.0') / Decimal('100.0'))  # 40% share
        
        # Check that fees were distributed proportionally
        self.assertAlmostEqual(float(self.position1.unclaimed_token_a_fees), float(expected_fee_user1), places=6)
        self.assertAlmostEqual(float(self.position2.unclaimed_token_a_fees), float(expected_fee_user2), places=6)
    
    def test_claim_fees(self):
        """Test that users can claim their accumulated fees"""
        # Manually add some fees to position1
        self.position1.unclaimed_token_a_fees = Decimal('1.5')
        self.position1.unclaimed_token_b_fees = Decimal('1500.0')
        self.position1.save()
        
        # Update pool accumulated fees
        self.pool.accumulated_token_a_fees = Decimal('1.5')
        self.pool.accumulated_token_b_fees = Decimal('1500.0')
        self.pool.save()
        
        # Login and claim fees
        self.client.login(username='provider1', password='testpass123')
        response = self.client.post('/defi/testnet/claim-fees/', {
            'position_id': self.position1.id
        })
        
        # Refresh from database
        self.position1.refresh_from_db()
        self.pool.refresh_from_db()
        
        # Check that fees were claimed (reset to 0)
        self.assertEqual(self.position1.unclaimed_token_a_fees, Decimal('0'))
        self.assertEqual(self.position1.unclaimed_token_b_fees, Decimal('0'))
        
        # Check that pool accumulated fees were reduced
        self.assertEqual(self.pool.accumulated_token_a_fees, Decimal('0'))
        self.assertEqual(self.pool.accumulated_token_b_fees, Decimal('0'))
    
    def test_no_fees_claimed_when_none_available(self):
        """Test that claiming with no fees available shows appropriate message"""
        self.client.login(username='provider1', password='testpass123')
        
        response = self.client.post('/defi/testnet/claim-fees/', {
            'position_id': self.position1.id
        })
        
        # Position should still have 0 fees
        self.position1.refresh_from_db()
        self.assertEqual(self.position1.unclaimed_token_a_fees, Decimal('0'))
        self.assertEqual(self.position1.unclaimed_token_b_fees, Decimal('0'))
    
    def test_pool_reserve_updated_correctly_with_fees(self):
        """Test that pool reserves are updated correctly (excluding fees)"""
        initial_reserve_a = self.pool.token_a_reserve
        swap_amount = Decimal('10.0')
        fee = swap_amount * Decimal('0.30') / Decimal('100')
        amount_after_fee = swap_amount - fee
        
        self.client.login(username='trader', password='testpass123')
        response = self.client.post('/defi/testnet/swap/', {
            'pool_id': self.pool.id,
            'from_token': 'ETH',
            'to_token': 'USDC',
            'amount': str(swap_amount)
        })
        
        self.pool.refresh_from_db()
        
        # Reserve should only increase by amount after fee
        expected_reserve = initial_reserve_a + amount_after_fee
        self.assertAlmostEqual(float(self.pool.token_a_reserve), float(expected_reserve), places=6)

class PriceFeedOracleTestCase(TestCase):
    """Test cases for price feed oracle network"""
    
    def setUp(self):
        """Set up test data"""
        # Create test user
        self.user = User.objects.create_user(username='oracle_user', password='testpass123')
        
        # Create oracle sources
        self.oracle1 = PriceFeedSource.objects.create(
            name='Oracle Alpha',
            oracle_address='0xABC123',
            is_active=True,
            reputation_score=Decimal('100.0')
        )
        
        self.oracle2 = PriceFeedSource.objects.create(
            name='Oracle Beta',
            oracle_address='0xDEF456',
            is_active=True,
            reputation_score=Decimal('95.0')
        )
        
        self.client = Client()
    
    def test_oracle_source_creation(self):
        """Test that oracle sources are created correctly"""
        self.assertEqual(PriceFeedSource.objects.count(), 2)
        self.assertTrue(self.oracle1.is_active)
        self.assertEqual(self.oracle1.total_submissions, 0)
    
    def test_submit_price_data(self):
        """Test submitting price data to oracle network"""
        self.client.login(username='oracle_user', password='testpass123')
        
        response = self.client.post('/defi/oracle/submit-price/', {
            'oracle_address': self.oracle1.oracle_address,
            'token_symbol': 'BTC',
            'price_usd': '45000.50'
        })
        
        # Check that price data was created
        self.assertEqual(PriceFeedData.objects.count(), 1)
        price_data = PriceFeedData.objects.first()
        self.assertEqual(price_data.token_symbol, 'BTC')
        self.assertEqual(price_data.price_usd, Decimal('45000.50'))
        self.assertEqual(price_data.source, self.oracle1)
        
        # Check that oracle submission count was updated
        self.oracle1.refresh_from_db()
        self.assertEqual(self.oracle1.total_submissions, 1)
    
    def test_price_aggregation_single_source(self):
        """Test price aggregation with single oracle source"""
        # Submit price from one oracle
        PriceFeedData.objects.create(
            source=self.oracle1,
            token_symbol='ETH',
            price_usd=Decimal('3000.00')
        )
        
        # Trigger aggregation
        from .views import _aggregate_price_feeds
        _aggregate_price_feeds('ETH')
        
        # Check aggregation was created
        self.assertEqual(PriceFeedAggregation.objects.filter(token_symbol='ETH').count(), 1)
        aggregation = PriceFeedAggregation.objects.filter(token_symbol='ETH').first()
        self.assertEqual(aggregation.aggregated_price, Decimal('3000.00'))
        self.assertEqual(aggregation.num_sources, 1)
        self.assertEqual(aggregation.confidence_score, Decimal('50.0'))  # Lower confidence with single source
    
    def test_price_aggregation_multiple_sources(self):
        """Test price aggregation with multiple oracle sources"""
        # Submit prices from multiple oracles
        PriceFeedData.objects.create(
            source=self.oracle1,
            token_symbol='BTC',
            price_usd=Decimal('45000.00')
        )
        
        PriceFeedData.objects.create(
            source=self.oracle2,
            token_symbol='BTC',
            price_usd=Decimal('45100.00')
        )
        
        # Trigger aggregation
        from .views import _aggregate_price_feeds
        _aggregate_price_feeds('BTC')
        
        # Check aggregation was created
        aggregation = PriceFeedAggregation.objects.filter(token_symbol='BTC').first()
        self.assertIsNotNone(aggregation)
        self.assertEqual(aggregation.num_sources, 2)
        
        # Median should be between the two prices
        self.assertGreaterEqual(aggregation.median_price, Decimal('45000.00'))
        self.assertLessEqual(aggregation.median_price, Decimal('45100.00'))
        
        # Min and max should match submitted prices
        self.assertEqual(aggregation.min_price, Decimal('45000.00'))
        self.assertEqual(aggregation.max_price, Decimal('45100.00'))
        
        # Confidence should be higher with multiple sources
        self.assertGreater(aggregation.confidence_score, Decimal('50.0'))
    
    def test_inactive_oracle_rejected(self):
        """Test that submissions from inactive oracles are rejected"""
        self.oracle1.is_active = False
        self.oracle1.save()
        
        self.client.login(username='oracle_user', password='testpass123')
        
        response = self.client.post('/defi/oracle/submit-price/', {
            'oracle_address': self.oracle1.oracle_address,
            'token_symbol': 'BTC',
            'price_usd': '45000.50'
        })
        
        # Check that submission was rejected
        self.assertRedirects(response, '/defi/oracle/submit-price/')
    
    def test_price_feeds_view(self):
        """Test that price feeds view displays correctly"""
        # Create some price data
        PriceFeedData.objects.create(
            source=self.oracle1,
            token_symbol='BTC',
            price_usd=Decimal('45000.00')
        )
        
        from .views import _aggregate_price_feeds
        _aggregate_price_feeds('BTC')
        
        response = self.client.get('/defi/oracle/price-feeds/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'BTC')
    
    def test_manage_oracle_registration(self):
        """Test oracle registration through manage view"""
        self.client.login(username='oracle_user', password='testpass123')
        
        response = self.client.post('/defi/oracle/manage/', {
            'action': 'register',
            'name': 'Oracle Gamma',
            'oracle_address': '0xGHI789',
            'description': 'Test oracle source'
        })
        
        # Check that oracle was created
        self.assertEqual(PriceFeedSource.objects.count(), 3)
        new_oracle = PriceFeedSource.objects.get(oracle_address='0xGHI789')
        self.assertEqual(new_oracle.name, 'Oracle Gamma')
        self.assertTrue(new_oracle.is_active)
    
    def test_oracle_toggle_activation(self):
        """Test toggling oracle activation status"""
        self.client.login(username='oracle_user', password='testpass123')
        
        # Deactivate oracle
        response = self.client.post('/defi/oracle/manage/', {
            'action': 'toggle',
            'oracle_address': self.oracle1.oracle_address
        })
        
        self.oracle1.refresh_from_db()
        self.assertFalse(self.oracle1.is_active)
        
        # Reactivate oracle
        response = self.client.post('/defi/oracle/manage/', {
            'action': 'toggle',
            'oracle_address': self.oracle1.oracle_address
        })
        
        self.oracle1.refresh_from_db()
        self.assertTrue(self.oracle1.is_active)


