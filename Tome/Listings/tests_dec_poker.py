import hashlib
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from API.channel_console_lib import DEFAULT_ALLOWED_STAGES
from API.unified_workflow_policy import (
    UNIFIED_WORKFLOW_CHANNEL_KEY,
    UNIFIED_WORKFLOW_CHANNEL_TAG,
    UNIFIED_WORKFLOW_POLICY_VERSION,
)

from API.models import MessageChannelPolicy
from Listings.models import (
    BalanceLock,
    DecPokerAuditAuthority,
    DecPokerGameInstance,
    DecPokerHand,
    DecPokerPayoutLedgerEntry,
    DecPokerPayoutPolicy,
    LimitOrder,
    OrderExecution,
    TradingPair,
)
from Listings.dec_service import (
    DEC_HOUSE_RULE_DEALER_BEST_TWO_OF_THREE,
    DEC_HOUSE_RULE_LEGACY,
    _append_dec_poker_ledger_entry,
    _deterministic_shuffled_deck,
    _draw_simple_poker_hand,
    _claim_dec_poker_hand_slot,
    _normalize_network_mode,
    _payout_policy_snapshot,
    _pause_instance_for_insufficient_reward_reserve,
    _require_vault_reward_reserve,
    _two_card_score,
    broadcast_dec_stage,
    create_dec_poker_valuation_bid,
    create_dec_poker_instance,
    ensure_shared_dec_channel,
    ensure_dec_poker_payout_policy,
    play_dec_poker_hand,
    publish_dec_poker_market_valuation,
    resume_dec_poker_instance,
    update_dec_instance_admin,
    verify_dec_poker_hand,
    verify_dec_poker_payout_ledger,
)
from Wallet.models import UserWallet, WalletAddress, WalletProfile


class DecPokerModelTests(TestCase):
    def test_game_instance_cooldown_defaults_to_thirty_seconds(self):
        field = DecPokerGameInstance._meta.get_field("hand_cooldown_seconds")

        self.assertEqual(field.default, 30)

    def test_two_card_score_prefers_pair(self):
        pair = _two_card_score([{"rank": "Q", "suit": "C"}, {"rank": "Q", "suit": "D"}])
        high = _two_card_score([{"rank": "A", "suit": "H"}, {"rank": "K", "suit": "S"}])
        self.assertGreater(pair, high)


class DecPokerHandChoicesTests(TestCase):
    def test_result_constants_are_stable(self):
        self.assertEqual(DecPokerHand.RESULT_WIN, "win")
        self.assertEqual(DecPokerHand.RESULT_LOSE, "lose")
        self.assertEqual(DecPokerHand.RESULT_PUSH, "push")


class DecPokerAdminControlTests(TestCase):
    @patch("Listings.dec_views.get_owned_admin_assets", return_value=["TOME0808!"])
    def test_admin_renders_verified_channel_as_a_dropdown_option(self, _mock_admin_assets):
        user = get_user_model().objects.create_user(
            username="dec-admin",
            password="test-password",
            is_staff=True,
        )
        MessageChannelPolicy.objects.create(
            channel_key="tome0808_swapflow",
            channel_name="TOME0808~DECPOKER",
            network_mode="testnet",
            version=5,
            status="active",
            owner_account=user,
            manager_account=user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("dec_poker_admin"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'select name="channel_policy"')
        self.assertContains(response, 'select name="channel_admin_asset"')
        self.assertContains(response, 'select name="hand_cooldown_seconds"')
        self.assertContains(response, "TOME0808~DECPOKER")

    @patch("Listings.dec_views.get_owned_admin_assets", return_value=[])
    @patch("Listings.dec_views.create_dec_poker_instance")
    @patch(
        "Listings.dec_views.ensure_shared_dec_channel",
        return_value={
            "created": True,
            "channel_name": "TOME0808~SWAPFLOWV5",
            "chain_metadata_status": MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING,
        },
    )
    def test_instance_creation_reports_pending_unified_v5_verification(
        self,
        _mock_channel_creation,
        mock_create_instance,
        _mock_admin_assets,
    ):
        user = get_user_model().objects.create_user(
            username="dec-pending-channel-admin",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("dec_poker_admin"),
            {
                "action": "create_instance",
                "channel_admin_asset": "TOME0808!",
                "channel_tag": UNIFIED_WORKFLOW_CHANNEL_TAG,
                "title": "Pending Channel Table",
                "reward_asset_name": "PENDINGDEC",
                "reward_supply": "1000",
                "reward_per_win": "10",
                "entry_fee_evr": "0.5",
                "reward_asset_units": "2",
                "hand_cooldown_seconds": "30",
            },
            follow=True,
        )

        self.assertContains(
            response,
            "The unified v5 channel is awaiting public-testnet metadata verification before instancing can begin.",
        )
        mock_create_instance.assert_not_called()


class DecPokerChannelPolicyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="dec-channel-policy-admin",
            password="test-password",
            is_staff=True,
        )

    @patch("Listings.dec_service.validate_channel_console_asset", side_effect=ValueError("asset not yet confirmed"))
    @patch(
        "Listings.dec_service.create_channel_console_asset_for_user",
        return_value={
            "channel_asset_name": "TOME0808~SWAPFLOWV5",
            "txid": "v5-issuance-txid",
            "issuance_pending": True,
            "existing_issuance": True,
        },
    )
    def test_pending_unified_v5_channel_does_not_fall_back_to_active_v4(
        self,
        _mock_channel_creation,
        _mock_validation,
    ):
        v4 = MessageChannelPolicy.objects.create(
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            channel_name="TOME0808~SWAPFLOWV4",
            network_mode="testnet",
            version=4,
            status="active",
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )
        v5 = MessageChannelPolicy.objects.create(
            channel_key=UNIFIED_WORKFLOW_CHANNEL_KEY,
            channel_name="TOME0808~SWAPFLOWV5",
            network_mode="testnet",
            version=UNIFIED_WORKFLOW_POLICY_VERSION,
            status="draft",
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            issuance_txid="v5-issuance-txid",
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING,
        )

        result = ensure_shared_dec_channel(self.user, {
            "network_mode": "testnet",
            "channel_admin_asset": "TOME0808!",
            "channel_tag": UNIFIED_WORKFLOW_CHANNEL_TAG,
        })

        v4.refresh_from_db()
        v5.refresh_from_db()
        self.assertFalse(result["created"])
        self.assertEqual(result["policy"].pk, v5.pk)
        self.assertEqual(result["channel_name"], "TOME0808~SWAPFLOWV5")
        self.assertEqual(
            result["chain_metadata_status"],
            MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING,
        )
        self.assertEqual(v4.status, "active")
        self.assertEqual(v5.status, "draft")

        with self.assertRaisesMessage(
            ValueError,
            "A verified shared DEC messaging channel must cover every game lifecycle stage",
        ):
            create_dec_poker_instance(self.user, {
                "network_mode": "testnet",
                "title": "Pending Channel Table",
                "reward_asset_name": "PENDINGDEC",
                "reward_supply": "1000",
                "reward_per_win": "10",
                "entry_fee_evr": "0.5",
                "reward_asset_units": "2",
                "hand_cooldown_seconds": "30",
            })


class DecPokerDisplayTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="visible-player",
            password="test-password",
        )
        wallet = UserWallet.objects.create(
            user=self.user,
            entropy="00",
            passphrase="",
        )
        address = WalletAddress.objects.create(
            wallet=wallet,
            network_mode="testnet",
            address="mhQ1mhr8qfehuNn6oCdXUyH9LkddAnnch2",
            wif="test-wif",
            account=0,
            index=0,
            is_change=False,
        )
        vault_profile = WalletProfile.objects.create(
            wallet=wallet,
            address=address,
            network_mode="testnet",
            name="Visible DEC Vault",
            is_main=True,
        )
        self.instance = DecPokerGameInstance.objects.create(
            creator=self.user,
            manager_account=self.user,
            network_mode="testnet",
            title="Visible Test Table",
            reward_asset_name="VISIBLEDEC",
            reward_asset_units=2,
            reward_supply=Decimal("1000"),
            entry_fee_evr=Decimal("0.5"),
            reward_per_win=Decimal("10"),
            system_fee_address=address.address,
            vault_profile=vault_profile,
            profile_tag_asset_name="VISIBLEDEC#ANT",
            profile_tag_txid="profile-tag-txid",
            active_server_seed_hash="a" * 64,
            active_server_seed_secret="server-seed",
            active_house_rule=DEC_HOUSE_RULE_DEALER_BEST_TWO_OF_THREE,
            status=DecPokerGameInstance.STATUS_ACTIVE,
            is_active=True,
        )
        self.hand = DecPokerHand.objects.create(
            game_instance=self.instance,
            player=self.user,
            wager_evr=Decimal("0.5"),
            reward_amount=Decimal("10"),
            reward_asset_name="VISIBLEDEC",
            result=DecPokerHand.RESULT_WIN,
            player_cards=[{"rank": "A", "suit": "S"}, {"rank": "K", "suit": "H"}],
            dealer_cards=[
                {"rank": "Q", "suit": "C"},
                {"rank": "J", "suit": "D"},
                {"rank": "10", "suit": "S"},
            ],
            outcome_detail={"house_rule": DEC_HOUSE_RULE_DEALER_BEST_TWO_OF_THREE},
            client_seed="client-seed",
            server_seed_hash="a" * 64,
            server_seed_revealed="server-seed",
            fairness_nonce=1,
            fairness_digest="b" * 64,
            spend_txid="c" * 64,
        )
        self.client.force_login(self.user)

    def test_lobby_renders_live_tables_and_recent_hands(self):
        response = self.client.get(reverse("dec_poker_lobby"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Visible Test Table")
        self.assertContains(response, "1 live")
        self.assertContains(response, "visible-player")
        self.assertContains(response, "Recent Hands")

    def test_instance_page_wraps_vault_and_fairness_values(self):
        response = self.client.get(reverse("dec_poker_instance", args=[self.instance.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="mono-value"', count=3)
        self.assertContains(response, self.instance.vault_profile.address.address)
        self.assertContains(response, self.instance.active_server_seed_hash)
        self.assertContains(response, "Dealer selects the best two of three cards and wins ties.")
        self.assertContains(response, "Payout Policy")
        self.assertContains(response, "7101/20825")
        self.assertContains(response, "Valuation required")

    def _bind_hand_to_payout_policy(self, idempotency_key):
        policy = ensure_dec_poker_payout_policy(self.instance)
        self.hand.payout_policy = policy
        self.hand.payout_policy_version = policy.version
        self.hand.payout_policy_hash = policy.policy_hash
        self.hand.payout_policy_snapshot = _payout_policy_snapshot(policy)
        self.hand.idempotency_key = idempotency_key
        self.hand.save(update_fields=[
            "payout_policy",
            "payout_policy_version",
            "payout_policy_hash",
            "payout_policy_snapshot",
            "idempotency_key",
        ])
        return policy

    def _create_authority_manager(self):
        manager = get_user_model().objects.create_user(
            username="dec-system-manager",
            password="test-password",
        )
        wallet = UserWallet.objects.create(
            user=manager,
            entropy="01",
            passphrase="",
        )
        address = WalletAddress.objects.create(
            wallet=wallet,
            network_mode="testnet",
            address="mnnU7V6W4Kk2XQsSTSWQyyyZpwShuyNNcU",
            wif="test-wif",
            account=0,
            index=0,
            is_change=False,
        )
        return manager, address

    def _create_active_audit_authority(
        self,
        *,
        authority_account=None,
        authority_address=None,
        enforce_settlement_writes=False,
    ):
        return DecPokerAuditAuthority.objects.create(
            network_mode="testnet",
            authority_account=authority_account or self.user,
            authority_address=authority_address or self.instance.vault_profile.address.address,
            restricted_asset_name="$DECAUDIT",
            required_qualifier_name="#DECAUTH",
            required_verifier_string="#DECAUTH",
            minimum_restricted_asset_balance=Decimal("1"),
            enforce_settlement_writes=enforce_settlement_writes,
            status=DecPokerAuditAuthority.STATUS_ACTIVE,
        )

    def _authorized_authority_evidence(self, *, authority_address=None):
        return {
            "restricted_asset_name": "$DECAUDIT",
            "authority_address": authority_address or self.instance.vault_profile.address.address,
            "required_verifier_string": "#DECAUTH",
            "on_chain_verifier_string": "#DECAUTH",
            "required_qualifier_name": "#DECAUTH",
            "has_qualifier": True,
            "restricted_asset_balance": "1",
            "minimum_restricted_asset_balance": "1",
            "verifier_matches": True,
            "has_minimum_balance": True,
            "is_authorized": True,
        }

    def test_payout_policy_publishes_exact_cross_asset_terms(self):
        policy = ensure_dec_poker_payout_policy(self.instance)

        self.assertEqual(policy.version, 1)
        self.assertEqual(policy.win_probability_numerator, 7101)
        self.assertEqual(policy.win_probability_denominator, 20825)
        self.assertEqual(policy.rtp_status, policy.RTP_STATUS_VALUATION_REQUIRED)
        self.assertIsNone(policy.rtp_percent)
        self.assertEqual(
            policy.payout_table["expected_return"]["amount_per_wager"],
            "3.40984394",
        )
        with self.assertRaises(ValidationError):
            policy.save()

    def test_ledger_hash_chain_is_tamper_evident_and_append_only(self):
        policy = self._bind_hand_to_payout_policy("audit-ledger-hand")
        accepted_entry = _append_dec_poker_ledger_entry(
            self.hand,
            policy,
            DecPokerPayoutLedgerEntry.EVENT_WAGER_ACCEPTED,
            currency="EVR",
            stake_amount=self.hand.wager_evr,
        )
        spend_entry = _append_dec_poker_ledger_entry(
            self.hand,
            policy,
            DecPokerPayoutLedgerEntry.EVENT_WAGER_SPEND_SETTLED,
            currency="EVR",
            stake_amount=self.hand.wager_evr,
            balance_delta=-self.hand.wager_evr,
            external_txid=self.hand.spend_txid,
        )

        verification = verify_dec_poker_payout_ledger(self.hand)
        self.assertTrue(verification["is_valid"])
        self.assertEqual(accepted_entry.sequence, 1)
        self.assertEqual(spend_entry.previous_entry_hash, accepted_entry.entry_hash)
        with self.assertRaises(ValidationError):
            accepted_entry.save()

        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                DecPokerPayoutLedgerEntry.objects.filter(pk=spend_entry.pk).update(entry_hash="0" * 64)
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                DecPokerPayoutLedgerEntry.objects.filter(pk=spend_entry.pk).delete()
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                DecPokerPayoutPolicy.objects.filter(pk=policy.pk).update(
                    rtp_disclosure="tampered",
                )
        self.assertTrue(verify_dec_poker_payout_ledger(self.hand)["is_valid"])

    @patch("Listings.dec_service.record_market_stage_event")
    @patch("Listings.views._fetch_user_token_balance", return_value=(Decimal("5"), ""))
    @patch("Listings.dec_service.get_public_restricted_asset_authority_evidence")
    def test_valuation_bid_is_post_only_and_reserves_evr(
        self,
        mock_authority_evidence,
        _mock_live_balance,
        mock_market_event,
    ):
        self._create_active_audit_authority()
        mock_authority_evidence.return_value = self._authorized_authority_evidence()

        bid = create_dec_poker_valuation_bid(
            self.user,
            self.instance,
            price_evr_per_reward_asset="0.20",
            reward_asset_quantity="2.00",
        )

        self.assertTrue(bid.post_only)
        self.assertEqual(bid.trading_pair.base_token, "VISIBLEDEC")
        self.assertEqual(bid.trading_pair.quote_token, "EVR")
        self.assertEqual(bid.limit_order.side, "buy")
        self.assertEqual(bid.limit_order.status, "pending")
        self.assertFalse(OrderExecution.objects.exists())
        self.assertEqual(BalanceLock.objects.get(limit_order=bid.limit_order).amount, Decimal("0.4"))
        mock_market_event.assert_called_once()

    @patch("Listings.dec_service.record_market_stage_event")
    @patch("Listings.views._fetch_user_token_balance", return_value=(Decimal("5"), ""))
    @patch("Listings.dec_service.get_public_restricted_asset_authority_evidence")
    def test_superuser_operator_reserves_manager_evr_for_valuation_bid(
        self,
        mock_authority_evidence,
        _mock_live_balance,
        _mock_market_event,
    ):
        manager, manager_address = self._create_authority_manager()
        operator = get_user_model().objects.create_user(
            username="dec-superuser-operator",
            password="test-password",
            is_staff=True,
            is_superuser=True,
        )
        self.instance.manager_account = manager
        self.instance.save(update_fields=["manager_account", "updated_at"])
        self._create_active_audit_authority(
            authority_account=manager,
            authority_address=manager_address.address,
        )
        mock_authority_evidence.return_value = self._authorized_authority_evidence(
            authority_address=manager_address.address,
        )

        bid = create_dec_poker_valuation_bid(
            operator,
            self.instance,
            price_evr_per_reward_asset="0.20",
            reward_asset_quantity="2.00",
        )

        self.assertEqual(bid.requested_by_id, operator.pk)
        self.assertEqual(bid.limit_order.user_id, manager.pk)
        self.assertEqual(BalanceLock.objects.get(limit_order=bid.limit_order).user_id, manager.pk)
        self.assertEqual(bid.authority_evidence["intent"]["operator_account_id"], operator.pk)
        self.assertEqual(bid.authority_evidence["intent"]["funding_account_id"], manager.pk)
        self.assertTrue(bid.authority_evidence["authority"]["operation"]["delegated_superuser"])

    def test_superuser_operator_sees_manager_authority_valuation_controls(self):
        manager, manager_address = self._create_authority_manager()
        operator = get_user_model().objects.create_user(
            username="dec-superuser-controls",
            password="test-password",
            is_staff=True,
            is_superuser=True,
        )
        self.instance.manager_account = manager
        self.instance.save(update_fields=["manager_account", "updated_at"])
        self._create_active_audit_authority(
            authority_account=manager,
            authority_address=manager_address.address,
        )
        self.client.force_login(operator)

        response = self.client.get(reverse("dec_poker_admin"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="create_valuation_bid"')
        self.assertContains(response, 'value="publish_market_valuation"')

    @patch("Listings.dec_service.get_public_restricted_asset_authority_evidence")
    def test_valuation_bid_rejects_a_crossing_price(self, mock_authority_evidence):
        self._create_active_audit_authority()
        mock_authority_evidence.return_value = self._authorized_authority_evidence()
        pair = TradingPair.objects.create(
            base_token="VISIBLEDEC",
            quote_token="EVR",
            network_mode="testnet",
        )
        LimitOrder.objects.create(
            user=self.user,
            trading_pair=pair,
            side="sell",
            price=Decimal("0.2"),
            quantity=Decimal("1"),
        )

        with self.assertRaisesMessage(ValueError, "strictly below the best open ask"):
            create_dec_poker_valuation_bid(
                self.user,
                self.instance,
                price_evr_per_reward_asset="0.20",
                reward_asset_quantity="1.00",
            )

    def test_valuation_bid_rejects_an_inactive_game(self):
        self.instance.status = DecPokerGameInstance.STATUS_PAUSED
        self.instance.is_active = False
        self.instance.save(update_fields=["status", "is_active", "updated_at"])

        with self.assertRaisesMessage(ValueError, "require an active game instance"):
            create_dec_poker_valuation_bid(
                self.user,
                self.instance,
                price_evr_per_reward_asset="0.20",
                reward_asset_quantity="1.00",
            )

    def test_valuation_bid_requires_the_audit_authority_to_manage_the_game(self):
        other_authority = get_user_model().objects.create_user(
            username="other-dec-authority",
            password="test-password",
        )
        DecPokerAuditAuthority.objects.create(
            network_mode="testnet",
            authority_account=other_authority,
            authority_address="mnnU7V6W4Kk2XQsSTSWQyyyZpwShuyNNcU",
            restricted_asset_name="$DECAUDIT",
            required_qualifier_name="#DECAUTH",
            required_verifier_string="#DECAUTH",
            status=DecPokerAuditAuthority.STATUS_ACTIVE,
        )

        with self.assertRaisesMessage(ValueError, "must be this game's manager"):
            create_dec_poker_valuation_bid(
                other_authority,
                self.instance,
                price_evr_per_reward_asset="0.20",
                reward_asset_quantity="1.00",
            )

    @patch("Listings.dec_service.get_public_transaction_evidence")
    @patch("Listings.dec_service.broadcast_dec_stage")
    @patch("Listings.dec_service.get_public_restricted_asset_authority_evidence")
    def test_execution_vwap_publishes_a_versioned_rtp_policy(
        self,
        mock_authority_evidence,
        mock_broadcast,
        mock_transaction_evidence,
    ):
        manager, manager_address = self._create_authority_manager()
        operator = get_user_model().objects.create_user(
            username="dec-superuser-publisher",
            password="test-password",
            is_staff=True,
            is_superuser=True,
        )
        self.instance.manager_account = manager
        self.instance.save(update_fields=["manager_account", "updated_at"])
        self._create_active_audit_authority(
            authority_account=manager,
            authority_address=manager_address.address,
        )
        mock_authority_evidence.return_value = self._authorized_authority_evidence(
            authority_address=manager_address.address,
        )
        mock_broadcast.return_value = {"status": "broadcasted", "txid": "f" * 64, "policy_id": 1}

        def confirmed_transaction_evidence(txid, **_kwargs):
            return {
                "transaction_id": txid,
                "confirmations": 1,
                "block_hash": "c" * 64,
                "block_time": 1,
                "transaction_time": 1,
            }

        mock_transaction_evidence.side_effect = confirmed_transaction_evidence
        MessageChannelPolicy.objects.create(
            channel_key="tome0808_swapflow",
            channel_name="TOME0808~DECPOKER",
            network_mode="testnet",
            version=5,
            status="active",
            owner_account=self.user,
            manager_account=self.user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )
        pair = TradingPair.objects.create(
            base_token="VISIBLEDEC",
            quote_token="EVR",
            network_mode="testnet",
        )
        OrderExecution.objects.create(
            trading_pair=pair,
            buyer=self.user,
            seller=self.user,
            price=Decimal("0.20"),
            quantity=Decimal("2.00"),
            tx_hash="a" * 64,
        )
        OrderExecution.objects.create(
            trading_pair=pair,
            buyer=self.user,
            seller=self.user,
            price=Decimal("0.40"),
            quantity=Decimal("1.00"),
            tx_hash="b" * 64,
        )

        valuation, policy = publish_dec_poker_market_valuation(operator, self.instance)

        self.assertEqual(valuation.source_execution_count, 2)
        self.assertEqual(valuation.price_evr_per_reward_asset, Decimal("0.26666667"))
        self.assertEqual(policy.rtp_status, policy.RTP_STATUS_DISCLOSED)
        self.assertEqual(policy.market_valuation_id, valuation.pk)
        self.assertEqual(
            policy.payout_table["rtp"]["valuation"]["valuation_hash"],
            valuation.valuation_hash,
        )
        self.assertTrue(policy.authority_evidence["publication_event"]["txid"])
        self.assertEqual(
            valuation.authority_evidence["operation"]["operator_account_id"],
            operator.pk,
        )
        self.assertEqual(
            valuation.authority_evidence["operation"]["manager_account_id"],
            manager.pk,
        )
        self.assertTrue(valuation.authority_evidence["operation"]["delegated_superuser"])
        mock_broadcast.assert_called_once()
        repeated_valuation, repeated_policy = publish_dec_poker_market_valuation(
            operator,
            self.instance,
        )
        self.assertEqual(repeated_valuation.pk, valuation.pk)
        self.assertEqual(repeated_policy.pk, policy.pk)
        self.assertEqual(self.instance.market_valuations.count(), 1)
        mock_broadcast.assert_called_once()
        self.assertEqual(mock_transaction_evidence.call_count, 4)

    @patch("Listings.dec_service.record_market_stage_event")
    @patch("Listings.views._fetch_user_token_balance", return_value=(Decimal("5"), ""))
    @patch("Listings.dec_service.get_public_restricted_asset_authority_evidence")
    def test_open_valuation_bid_does_not_establish_rtp(
        self,
        mock_authority_evidence,
        _mock_live_balance,
        _mock_market_event,
    ):
        self._create_active_audit_authority()
        mock_authority_evidence.return_value = self._authorized_authority_evidence()
        create_dec_poker_valuation_bid(
            self.user,
            self.instance,
            price_evr_per_reward_asset="0.20",
            reward_asset_quantity="2.00",
        )

        with self.assertRaisesMessage(ValueError, "requires at least one recent"):
            publish_dec_poker_market_valuation(self.user, self.instance)

    @patch("Listings.dec_service.get_public_restricted_asset_authority_evidence")
    def test_market_valuation_rejects_a_noncanonical_execution_transaction_id(
        self,
        mock_authority_evidence,
    ):
        self._create_active_audit_authority()
        mock_authority_evidence.return_value = self._authorized_authority_evidence()
        pair = TradingPair.objects.create(
            base_token="VISIBLEDEC",
            quote_token="EVR",
            network_mode="testnet",
        )
        OrderExecution.objects.create(
            trading_pair=pair,
            buyer=self.user,
            seller=self.user,
            price=Decimal("0.20"),
            quantity=Decimal("1.00"),
            tx_hash="unverified-database-value",
        )

        with self.assertRaisesMessage(ValueError, "requires at least one recent"):
            publish_dec_poker_market_valuation(self.user, self.instance)

    @patch("Listings.dec_service.get_public_transaction_evidence")
    @patch("Listings.dec_service.get_public_restricted_asset_authority_evidence")
    def test_market_valuation_requires_confirmed_transaction_evidence(
        self,
        mock_authority_evidence,
        mock_transaction_evidence,
    ):
        self._create_active_audit_authority()
        mock_authority_evidence.return_value = self._authorized_authority_evidence()
        mock_transaction_evidence.side_effect = ValueError("Transaction has 0 confirmations")
        pair = TradingPair.objects.create(
            base_token="VISIBLEDEC",
            quote_token="EVR",
            network_mode="testnet",
        )
        OrderExecution.objects.create(
            trading_pair=pair,
            buyer=self.user,
            seller=self.user,
            price=Decimal("0.20"),
            quantity=Decimal("1.00"),
            tx_hash="a" * 64,
        )

        with self.assertRaisesMessage(ValueError, "independently verify a confirmed"):
            publish_dec_poker_market_valuation(self.user, self.instance)

    def test_failed_instance_is_retained_when_its_policy_is_immutable(self):
        policy = ensure_dec_poker_payout_policy(self.instance)
        self.instance.active_payout_policy = policy
        self.instance.status = DecPokerGameInstance.STATUS_FAILED
        self.instance.is_active = False
        self.instance.save(update_fields=["active_payout_policy", "status", "is_active", "updated_at"])

        with self.assertRaisesMessage(ValueError, "retained for audit"):
            create_dec_poker_instance(self.user, {
                "network_mode": "testnet",
                "title": "Replacement Table",
                "reward_asset_name": self.instance.reward_asset_name,
                "reward_supply": "1000",
                "reward_per_win": "10",
                "entry_fee_evr": "0.5",
                "reward_asset_units": "2",
                "hand_cooldown_seconds": "300",
            })

        self.assertTrue(DecPokerGameInstance.objects.filter(pk=self.instance.pk).exists())

    @patch("Listings.dec_service._claim_dec_poker_hand_slot")
    @patch("Listings.dec_service._active_dec_policy", return_value=SimpleNamespace(id=7))
    def test_settled_idempotency_key_returns_existing_hand_without_new_settlement(
        self,
        _mock_policy,
        mock_claim_hand_slot,
    ):
        self._bind_hand_to_payout_policy("repeatable-hand")

        result = play_dec_poker_hand(
            self.user,
            self.instance,
            wager_evr=Decimal("0.5"),
            client_seed="client-seed",
            idempotency_key="repeatable-hand",
        )

        self.assertEqual(result.pk, self.hand.pk)
        mock_claim_hand_slot.assert_not_called()

    def test_verify_endpoint_returns_ledger_integrity(self):
        policy = self._bind_hand_to_payout_policy("verify-ledger-hand")
        _append_dec_poker_ledger_entry(
            self.hand,
            policy,
            DecPokerPayoutLedgerEntry.EVENT_WAGER_ACCEPTED,
            currency="EVR",
            stake_amount=self.hand.wager_evr,
        )

        response = self.client.get(
            f"{reverse('dec_poker_hand_verify', args=[self.hand.id])}?format=json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ledger_verification"]["is_valid"])


class DecPokerFairnessTests(TestCase):
    def test_deterministic_shuffle_is_repeatable(self):
        first = _deterministic_shuffled_deck("server-seed", "client-seed", 7)
        second = _deterministic_shuffled_deck("server-seed", "client-seed", 7)
        self.assertEqual(first, second)

    def test_different_nonce_changes_shuffle(self):
        first = _deterministic_shuffled_deck("server-seed", "client-seed", 7)
        second = _deterministic_shuffled_deck("server-seed", "client-seed", 8)
        self.assertNotEqual(first, second)

    def test_house_wins_ties(self):
        for nonce in range(1, 500):
            result = _draw_simple_poker_hand("seed", "seed", nonce)
            if result["player_score"] == result["dealer_score"]:
                self.assertEqual(result["result"], DecPokerHand.RESULT_LOSE)
                return
        self.fail("Expected to find at least one deterministic tie scenario for fairness validation.")

    def test_dealer_best_two_of_three_is_house_favored(self):
        player_wins = 0
        trial_count = 1000
        for nonce in range(1, trial_count + 1):
            hand = _draw_simple_poker_hand(f"server-{nonce}", "client-seed", nonce)
            player_wins += int(hand["result"] == DecPokerHand.RESULT_WIN)
            self.assertEqual(len(hand["dealer_cards"]), 3)

        self.assertLess(player_wins / trial_count, 0.4)

    def test_legacy_house_rule_remains_verifiable(self):
        server_seed = "legacy-server-seed"
        hand_data = _draw_simple_poker_hand(
            server_seed,
            "legacy-client-seed",
            1,
            house_rule=DEC_HOUSE_RULE_LEGACY,
        )
        hand = SimpleNamespace(
            server_seed_revealed=server_seed,
            server_seed_hash=hashlib.sha256(server_seed.encode("utf-8")).hexdigest(),
            client_seed="legacy-client-seed",
            fairness_nonce=1,
            player_cards=hand_data["player_cards"],
            dealer_cards=hand_data["dealer_cards"],
            result=hand_data["result"],
            fairness_digest=hand_data["fairness_digest"],
            outcome_detail={"house_rule": DEC_HOUSE_RULE_LEGACY},
        )

        verification = verify_dec_poker_hand(hand)

        self.assertTrue(verification["is_valid"])
        self.assertEqual(verification["house_rule"], DEC_HOUSE_RULE_LEGACY)


class DecPokerSettlementTests(TestCase):
    def test_dec_rejects_non_testnet_networks(self):
        self.assertEqual(_normalize_network_mode("testnet"), "testnet")
        with self.assertRaisesRegex(ValueError, "public Evrmore testnet"):
            _normalize_network_mode("mainnet")

    @patch("Listings.dec_service.RPC.listassetbalancesbyaddress")
    def test_vault_reward_reserve_requires_the_main_reward_asset(self, mock_balances):
        instance = SimpleNamespace(
            reward_asset_name="DECWIN",
            vault_profile=SimpleNamespace(address=SimpleNamespace(address="vault-address")),
        )
        mock_balances.return_value = {"DECWIN": Decimal("12")}

        self.assertEqual(_require_vault_reward_reserve(instance, Decimal("10")), Decimal("12"))

        mock_balances.return_value = {"DECWIN": Decimal("9")}
        with self.assertRaisesRegex(ValueError, "DEC reward vault holds 9"):
            _require_vault_reward_reserve(instance, Decimal("10"))

    @patch("Listings.dec_service._require_vault_reward_reserve")
    def test_insufficient_reward_reserve_pauses_an_active_instance(self, mock_reserve):
        mock_reserve.side_effect = ValueError("DEC reward vault holds 0 DECNIGHT; at least 10 is required.")
        instance = SimpleNamespace(
            is_active=True,
            status="active",
            profile_tag_error="",
            save=MagicMock(),
        )

        with self.assertRaisesRegex(ValueError, "This game was paused"):
            _pause_instance_for_insufficient_reward_reserve(instance, Decimal("10"))

        self.assertFalse(instance.is_active)
        self.assertEqual(instance.status, "paused")
        self.assertIn("DEC reward vault holds 0", instance.profile_tag_error)

    def test_admin_cannot_activate_an_instance(self):
        instance = SimpleNamespace(
            is_active=False,
            status="pending",
            save=MagicMock(),
        )

        with self.assertRaisesRegex(ValueError, "verified provisioning"):
            update_dec_instance_admin(instance, status="active")

    def test_admin_can_configure_the_hand_buffer(self):
        instance = SimpleNamespace(
            hand_cooldown_seconds=300,
            save=MagicMock(),
        )

        update_dec_instance_admin(instance, hand_cooldown_seconds="30")

        self.assertEqual(instance.hand_cooldown_seconds, 30)
        instance.save.assert_called_once_with(
            update_fields=["updated_at", "hand_cooldown_seconds"]
        )

    @patch("Listings.dec_service._claim_dec_poker_hand_slot")
    @patch("Listings.dec_service._active_dec_policy")
    def test_play_binds_a_legacy_instance_to_the_verified_v4_policy(
        self,
        mock_policy,
        mock_claim_hand_slot,
    ):
        user = get_user_model().objects.create_user(
            username="legacy-policy-player",
            password="test-password",
        )
        wallet = UserWallet.objects.create(
            user=user,
            entropy="legacy-policy-entropy",
            passphrase="",
        )
        address = WalletAddress.objects.create(
            wallet=wallet,
            network_mode="testnet",
            address="mLegacyPolicyAddress",
            wif="legacy-policy-wif",
            account=0,
            index=0,
            is_change=False,
        )
        vault_profile = WalletProfile.objects.create(
            wallet=wallet,
            address=address,
            network_mode="testnet",
            name="Legacy Policy Vault",
            is_main=True,
        )
        policy = MessageChannelPolicy.objects.create(
            channel_key="tome0808_swapflow",
            channel_name="TOME0808~SWAPFLOWV5",
            network_mode="testnet",
            version=5,
            status="active",
            owner_account=user,
            manager_account=user,
            allowed_stages=list(DEFAULT_ALLOWED_STAGES),
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        )
        instance = DecPokerGameInstance.objects.create(
            creator=user,
            manager_account=user,
            network_mode="testnet",
            title="Legacy Policy Table",
            reward_asset_name="LEGACYV4",
            reward_supply=Decimal("1000"),
            entry_fee_evr=Decimal("0.5"),
            reward_per_win=Decimal("10"),
            system_fee_address=address.address,
            vault_profile=vault_profile,
            profile_tag_asset_name="LEGACYV4#ANT",
            profile_tag_txid="legacy-tag-txid",
            status=DecPokerGameInstance.STATUS_ACTIVE,
            is_active=True,
        )
        mock_policy.return_value = policy
        mock_claim_hand_slot.side_effect = ValueError("stop after policy binding")

        with self.assertRaisesRegex(ValueError, "stop after policy binding"):
            play_dec_poker_hand(user, instance, Decimal("0.5"), "client-seed")

        instance.refresh_from_db()
        self.assertEqual(instance.channel_policy, policy)

    @patch("Listings.dec_service.DecPokerGameInstance.objects")
    def test_hand_buffer_blocks_a_second_table_hand(self, mock_instances):
        locked_instance = SimpleNamespace(
            is_active=True,
            status="active",
            profile_tag_asset_name="DECWIN#ANT",
            profile_tag_txid="profile-tag-txid",
            hand_cooldown_seconds=300,
            hand_cooldown_until=timezone.now() + timedelta(seconds=300),
            save=MagicMock(),
        )
        mock_instances.select_for_update.return_value.select_related.return_value.get.return_value = locked_instance

        with self.assertRaisesRegex(ValueError, "channel-event reconciliation buffer"):
            _claim_dec_poker_hand_slot(SimpleNamespace(pk=3))

        locked_instance.save.assert_not_called()

    @patch("Listings.dec_service.DecPokerGameInstance.objects")
    def test_hand_buffer_reserves_the_next_table_hand(self, mock_instances):
        locked_instance = SimpleNamespace(
            is_active=True,
            status="active",
            profile_tag_asset_name="DECWIN#ANT",
            profile_tag_txid="profile-tag-txid",
            hand_cooldown_seconds=300,
            hand_cooldown_until=timezone.now() - timedelta(seconds=1),
            save=MagicMock(),
        )
        mock_instances.select_for_update.return_value.select_related.return_value.get.return_value = locked_instance
        before_claim = timezone.now()

        result = _claim_dec_poker_hand_slot(SimpleNamespace(pk=3))

        self.assertIs(result, locked_instance)
        self.assertGreaterEqual(
            locked_instance.hand_cooldown_until,
            before_claim + timedelta(seconds=300),
        )
        locked_instance.save.assert_called_once_with(
            update_fields=["hand_cooldown_until", "updated_at"]
        )

    @patch("Listings.dec_service.create_raw_evr_transaction")
    @patch(
        "Listings.dec_service._claim_dec_poker_hand_slot",
        side_effect=ValueError("The previous hand is still in its channel-event reconciliation buffer."),
    )
    @patch("Listings.dec_service._active_dec_policy", return_value=SimpleNamespace(id=7))
    def test_hand_buffer_blocks_play_before_raw_transaction_building(
        self,
        _mock_policy,
        _mock_claim_hand_slot,
        mock_raw_evr_transaction,
    ):
        instance = SimpleNamespace(
            is_active=True,
            status="active",
            profile_tag_asset_name="DECWIN#ANT",
            profile_tag_txid="profile-tag-txid",
            entry_fee_evr=Decimal("0.5"),
            network_mode="testnet",
        )

        with self.assertRaisesRegex(ValueError, "reconciliation buffer"):
            play_dec_poker_hand(object(), instance, Decimal("0.5"), "client-seed")

        mock_raw_evr_transaction.assert_not_called()

    @patch("Listings.dec_service.RPC.getassetdata", return_value={})
    @patch("Listings.dec_service._active_dec_policy", return_value=SimpleNamespace(id=7, channel_name="ROOT~DEC"))
    def test_resume_waits_for_reward_asset_confirmation(self, mock_policy, mock_asset_data):
        instance = SimpleNamespace(
            is_active=False,
            status="pending",
            network_mode="testnet",
            reward_issue_txid="issue-txid",
            reward_asset_name="DECWIN",
            profile_tag_error="",
            save=MagicMock(),
        )

        result = resume_dec_poker_instance(instance)

        self.assertIs(result, instance)
        self.assertEqual(instance.status, "pending")
        self.assertFalse(instance.is_active)
        self.assertIn("Waiting for the reward main asset", instance.profile_tag_error)
        mock_asset_data.assert_called_once_with("DECWIN")

    @patch("Listings.dec_service.ensure_dec_poker_payout_policy")
    @patch("Listings.dec_service.broadcast_dec_stage", return_value={"status": "broadcasted", "txid": "event-txid"})
    @patch("Listings.dec_service._ensure_instance_fairness_material")
    @patch("Listings.dec_service._require_vault_reward_reserve", return_value=Decimal("10000"))
    @patch("Listings.dec_service._primary_address_and_wif")
    @patch("Listings.dec_service.RPC.getassetdata", side_effect=[{"name": "DECWIN"}, {"name": "DECWIN#ANT"}])
    @patch("Listings.dec_service._active_dec_policy", return_value=SimpleNamespace(id=7, channel_name="ROOT~DEC"))
    def test_resume_activates_after_vault_funding_without_creator_reward_balance(
        self,
        mock_policy,
        mock_asset_data,
        mock_primary_address,
        mock_reserve,
        mock_fairness,
        mock_stage,
        mock_ensure_payout_policy,
    ):
        instance = SimpleNamespace(
            id=7,
            creator=SimpleNamespace(username="creator"),
            is_active=False,
            status="pending",
            network_mode="testnet",
            reward_issue_txid="issue-txid",
            reward_asset_name="DECWIN",
            reward_supply=Decimal("10000"),
            reward_asset_units=2,
            entry_fee_evr=Decimal("0.5"),
            instance_fee_evr=Decimal("0.1"),
            instance_fee_txid="fee-txid",
            profile_tag_asset_name="DECWIN#ANT",
            profile_tag_txid="tag-txid",
            owner_transfer_txid="vault-funding-txid",
            profile_tag_error="",
            channel_policy=None,
            save=MagicMock(),
        )

        result = resume_dec_poker_instance(instance)

        self.assertIs(result, instance)
        self.assertTrue(instance.is_active)
        self.assertEqual(instance.status, "active")
        self.assertEqual(instance.channel_policy, mock_policy.return_value)
        mock_primary_address.assert_not_called()
        mock_reserve.assert_called_once_with(instance, Decimal("10000"))
        mock_fairness.assert_called_once_with(instance)
        mock_ensure_payout_policy.assert_called_once_with(instance)
        mock_stage.assert_called_once()

    @patch("Listings.dec_service.sign_and_broadcast_raw_transaction", return_value="message-txid")
    @patch(
        "Listings.dec_service.create_raw_asset_transfer_transaction",
        return_value={"raw_tx": "raw-message", "inputs": [], "outputs": []},
    )
    @patch("Listings.dec_service._upload_json", return_value=SimpleNamespace(cid="QmDecEvent"))
    @patch("Listings.dec_service._channel_signer", return_value=("signer-address", "signer-wif"))
    @patch("Listings.dec_service._active_dec_policy", return_value=SimpleNamespace(id=7, channel_name="ROOT~DEC"))
    def test_stage_event_uses_raw_asset_transfer(
        self,
        mock_policy,
        mock_channel_signer,
        mock_upload,
        mock_raw_transfer,
        mock_sign_and_broadcast,
    ):
        instance = SimpleNamespace(id=3, network_mode="testnet", reward_asset_name="DECWIN")

        result = broadcast_dec_stage(instance, "game_spend_recorded", None, {
            "wager_evr": "1",
            "settlement_id": str(uuid.uuid4()),
        })

        self.assertEqual(result["status"], "broadcasted")
        self.assertEqual(result["txid"], "message-txid")
        mock_raw_transfer.assert_called_once_with(
            from_address="signer-address",
            to_address="signer-address",
            asset_name="ROOT~DEC",
            asset_quantity=Decimal("1"),
            message="QmDecEvent",
            expire_time=0,
        )
        mock_sign_and_broadcast.assert_called_once_with("raw-message", wif_keys=["signer-wif"])

    @patch("Listings.dec_service.sign_and_broadcast_raw_transaction", side_effect=Exception("txn-mempool-conflict"))
    @patch(
        "Listings.dec_service.create_raw_asset_transfer_transaction",
        return_value={"raw_tx": "raw-message", "inputs": [], "outputs": []},
    )
    @patch("Listings.dec_service._upload_json", return_value=SimpleNamespace(cid="QmDecEvent"))
    @patch("Listings.dec_service._channel_signer", return_value=("signer-address", "signer-wif"))
    @patch("Listings.dec_service._active_dec_policy", return_value=SimpleNamespace(id=7, channel_name="ROOT~DEC"))
    def test_stage_event_records_mempool_conflict_without_raising(
        self,
        _mock_policy,
        _mock_channel_signer,
        _mock_upload,
        _mock_raw_transfer,
        _mock_sign_and_broadcast,
    ):
        instance = SimpleNamespace(id=3, network_mode="testnet", reward_asset_name="DECWIN")

        result = broadcast_dec_stage(instance, "game_spend_recorded", None, {
            "wager_evr": "1",
            "settlement_id": str(uuid.uuid4()),
        })

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["txid"], "")
        self.assertIn("txn-mempool-conflict", result["reason"])

    @patch("Listings.dec_service._upload_json", side_effect=Exception("IPFS upload unavailable"))
    @patch("Listings.dec_service._active_dec_policy", return_value=SimpleNamespace(id=7, channel_name="ROOT~DEC"))
    def test_stage_event_records_upload_failure_without_raising(self, _mock_policy, _mock_upload):
        instance = SimpleNamespace(id=3, network_mode="testnet", reward_asset_name="DECWIN")

        result = broadcast_dec_stage(instance, "game_spend_recorded", None, {
            "wager_evr": "1",
            "settlement_id": str(uuid.uuid4()),
        })

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["txid"], "")
        self.assertEqual(result["payload_cid"], "")
        self.assertIn("IPFS upload unavailable", result["reason"])

    @patch("Listings.dec_service._append_dec_poker_ledger_entry")
    @patch("Listings.dec_service.ensure_dec_poker_payout_policy")
    @patch("Listings.dec_service._claim_dec_poker_hand_slot")
    @patch("Listings.dec_service.DecPokerHand.objects.create")
    @patch(
        "Listings.dec_service.broadcast_dec_stage",
    )
    @patch("Listings.dec_service.sign_and_broadcast_raw_transaction", side_effect=["spend-txid", "reward-txid"])
    @patch(
        "Listings.dec_service.create_raw_asset_transfer_transaction",
        return_value={"raw_tx": "raw-reward", "inputs": [], "outputs": []},
    )
    @patch(
        "Listings.dec_service.create_raw_evr_transaction",
        return_value={"raw_tx": "raw-spend", "inputs": [], "outputs": []},
    )
    @patch("Listings.dec_service._require_vault_reward_reserve", return_value=Decimal("100"))
    @patch("Listings.dec_service._primary_address_and_wif", return_value=("player-address", "player-wif"))
    @patch(
        "Listings.dec_service._draw_simple_poker_hand",
        return_value={
            "result": DecPokerHand.RESULT_WIN,
            "player_cards": [],
            "dealer_cards": [],
            "player_score": (1, 14, 13),
            "dealer_score": (1, 12, 11),
            "house_rule": DEC_HOUSE_RULE_DEALER_BEST_TWO_OF_THREE,
            "fairness_digest": "fairness-digest",
        },
    )
    @patch("Listings.dec_service._active_dec_policy", return_value=SimpleNamespace(id=7, channel_name="ROOT~DEC"))
    def test_winning_hand_builds_raw_spend_and_reward_transactions(
        self,
        mock_policy,
        mock_draw,
        mock_primary_address,
        mock_reward_reserve,
        mock_raw_evr,
        mock_raw_asset,
        mock_sign_and_broadcast,
        mock_stage_event,
        mock_hand_create,
        mock_claim_hand_slot,
        mock_ensure_payout_policy,
        mock_append_ledger_entry,
    ):
        vault_address = SimpleNamespace(address="vault-address", wif="vault-wif")
        instance = SimpleNamespace(
            id=3,
            is_active=True,
            status="active",
            profile_tag_asset_name="DECWIN#ANT",
            profile_tag_txid="profile-tag-txid",
            entry_fee_evr=Decimal("0.5"),
            network_mode="testnet",
            active_server_seed_hash="seed-hash",
            active_server_seed_secret="server-seed",
            active_house_rule=DEC_HOUSE_RULE_DEALER_BEST_TWO_OF_THREE,
            next_hand_nonce=1,
            wager_treasury_bps=5000,
            vault_profile=SimpleNamespace(address=vault_address, wallet=object()),
            system_fee_address="vault-address",
            reward_per_win=Decimal("10"),
            reward_asset_name="DECWIN",
            save=MagicMock(),
        )
        recorded_hand = SimpleNamespace(
            result=DecPokerHand.RESULT_WIN,
            settlement_id=uuid.uuid4(),
            save=MagicMock(),
        )
        mock_hand_create.return_value = recorded_hand
        mock_claim_hand_slot.return_value = instance
        mock_ensure_payout_policy.return_value = SimpleNamespace(
            version=1,
            policy_hash="p" * 64,
            payout_currency="DECWIN",
            payout_table={"schema_version": 1},
        )
        mock_append_ledger_entry.side_effect = [
            SimpleNamespace(entry_hash="accepted-ledger-hash"),
            SimpleNamespace(entry_hash="spend-ledger-hash"),
            SimpleNamespace(entry_hash="resolution-ledger-hash"),
            SimpleNamespace(entry_hash="reward-ledger-hash"),
        ]

        def failed_stage_event(*_args, **_kwargs):
            self.assertTrue(mock_hand_create.called)
            return {"status": "failed", "reason": "IPFS upload unavailable", "txid": ""}

        mock_stage_event.side_effect = failed_stage_event

        result = play_dec_poker_hand(object(), instance, Decimal("1"), "client-seed")

        self.assertIs(result, recorded_hand)
        mock_claim_hand_slot.assert_called_once_with(instance)
        mock_ensure_payout_policy.assert_called_once_with(instance)
        mock_reward_reserve.assert_called_once_with(instance, Decimal("10"))
        mock_raw_evr.assert_called_once_with(
            from_address="player-address",
            to_address="vault-address",
            amount_evr=Decimal("1"),
            extra_coin_outputs={},
        )
        mock_raw_asset.assert_called_once_with(
            from_address="vault-address",
            to_address="player-address",
            asset_name="DECWIN",
            asset_quantity=Decimal("10"),
        )
        self.assertEqual(mock_sign_and_broadcast.call_count, 2)
        mock_sign_and_broadcast.assert_any_call("raw-spend", wif_keys=["player-wif"])
        mock_sign_and_broadcast.assert_any_call("raw-reward", wif_keys=["vault-wif"])
        self.assertEqual(recorded_hand.spend_message_status, DecPokerHand.MESSAGE_STATUS_FAILED)
        self.assertEqual(recorded_hand.reward_message_status, DecPokerHand.MESSAGE_STATUS_FAILED)
        self.assertEqual(
            recorded_hand.outcome_detail["message_events"]["spend"]["reason"],
            "IPFS upload unavailable",
        )
        self.assertEqual(mock_append_ledger_entry.call_count, 4)
