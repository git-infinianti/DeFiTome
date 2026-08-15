from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import uuid
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal, InvalidOperation, localcontext
from itertools import combinations

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from django.conf import settings

from API.models import MessageChannelPolicy
from API.channel_event_protocol import add_payload_checksum, validate_channel_event_payload
from API.channel_console_lib import DEFAULT_ALLOWED_STAGES
from API.channel_console_service import (
    create_channel_console_asset_for_user,
    validate_channel_console_asset,
)
from API.unified_workflow_policy import (
    UNIFIED_WORKFLOW_CHANNEL_KEY,
    UNIFIED_WORKFLOW_CHANNEL_TAG,
    UNIFIED_WORKFLOW_DESCRIPTION,
    UNIFIED_WORKFLOW_POLICY_VERSION,
    UNIFIED_WORKFLOW_STRICT_RULES,
)
from Media.address_metadata import _sign_metadata_message, _verify_metadata_signature
from Media.kubo_api import KuboAPIUploader
from Tome.rpc_client import using_network_mode
from Wallet.models import UserWallet, WalletAddress, WalletProfile
from Wallet.rpc import (
    RPC,
    _resolve_fee_satoshis,
    _resolve_burn_address,
    _satoshis_to_evr,
    create_raw_asset_operation_transaction,
    create_raw_asset_transfer_transaction,
    create_raw_evr_transaction,
    get_public_restricted_asset_authority_evidence,
    get_public_transaction_evidence,
    sign_and_broadcast_raw_transaction,
)
from DeFi.message_channels import record_market_stage_event
from Wallet.rip10 import (
    build_address_metadata_asset,
    build_address_name_tag,
    build_signed_metadata,
    metadata_signature_hash,
    validate_metadata,
)
from Wallet.wallet import Wallet

from .models import (
    DecPokerAuditAuthority,
    DecPokerGameInstance,
    DecPokerHand,
    DecPokerPayoutLedgerEntry,
    DecPokerPayoutPolicy,
    DecPokerMarketValuation,
    DecPokerValuationBid,
    LimitOrder,
    OrderExecution,
    TradingPair,
)


DEC_CHANNEL_KEY = UNIFIED_WORKFLOW_CHANNEL_KEY
DEC_NETWORK_MODE = "testnet"
DEC_REQUIRED_STAGES = (
    "game_instance_created",
    "game_spend_recorded",
    "game_reward_distributed",
)
DEC_POLICY_PUBLICATION_STAGE = "payout_policy_published"
DEC_CHANNEL_ALLOWED_STAGES = tuple(DEFAULT_ALLOWED_STAGES)
DEC_PLACEHOLDER_METADATA_TXID = "0" * 64
DEC_DEFAULT_HAND_COOLDOWN_SECONDS = 30
DEC_MIN_HAND_COOLDOWN_SECONDS = 30
DEC_MAX_HAND_COOLDOWN_SECONDS = 3600
DEC_HOUSE_RULE_LEGACY = "dealer_wins_ties"
DEC_HOUSE_RULE_DEALER_BEST_TWO_OF_THREE = "dealer_best_two_of_three_wins_ties"
DEC_PAYOUT_POLICY_SCHEMA_VERSION = 1
DEC_PAYOUT_RULE_VERSION = "dec_poker_payout_v1"
DEC_PAYOUT_WIN_PROBABILITY_NUMERATOR = 7101
DEC_PAYOUT_WIN_PROBABILITY_DENOMINATOR = 20825
DEC_PAYOUT_LOSS_PROBABILITY_NUMERATOR = 13724
DEC_LEGACY_PAYOUT_WIN_PROBABILITY_NUMERATOR = 2068
DEC_LEGACY_PAYOUT_WIN_PROBABILITY_DENOMINATOR = 4165
DEC_LEGACY_PAYOUT_LOSS_PROBABILITY_NUMERATOR = 2097
DEC_PAYOUT_RTP_DISCLOSURE = (
    "Wagers settle in EVR and wins settle in a separate DEC reward asset. "
    "A percentage RTP is not stated without a versioned, independently auditable "
    "EVR-to-reward-asset valuation snapshot."
)
DEC_MARKET_VALUATION_LOOKBACK = timedelta(hours=24)
DEC_MARKET_VALUATION_SOURCE = DecPokerMarketValuation.SOURCE_VWAP
DEC_MARKET_VALUATION_MIN_CONFIRMATIONS = 1
DEC_GAME_EVENT_TYPE = "dec_game_event"
DEC_GAME_INSTANCE_AGGREGATE_TYPE = "dec_poker_game_instance"
DEC_HAND_AGGREGATE_TYPE = "dec_poker_hand"
DEC_PAYOUT_POLICY_AGGREGATE_TYPE = "dec_poker_payout_policy"
DEC_HAND_STAGE_SEQUENCES = {
    "game_spend_recorded": 1,
    "game_reward_distributed": 2,
}


def _to_decimal(value, *, default=Decimal("0")):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _decimal_string(value):
    with localcontext() as context:
        context.prec = 50
        normalized_value = _to_decimal(value).quantize(Decimal("0.00000001"))
    return format(normalized_value, "f")


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _payout_odds_for_house_rule(house_rule):
    if _normalize_dec_house_rule(house_rule) == DEC_HOUSE_RULE_LEGACY:
        return (
            DEC_LEGACY_PAYOUT_WIN_PROBABILITY_NUMERATOR,
            DEC_LEGACY_PAYOUT_WIN_PROBABILITY_DENOMINATOR,
            DEC_LEGACY_PAYOUT_LOSS_PROBABILITY_NUMERATOR,
        )
    return (
        DEC_PAYOUT_WIN_PROBABILITY_NUMERATOR,
        DEC_PAYOUT_WIN_PROBABILITY_DENOMINATOR,
        DEC_PAYOUT_LOSS_PROBABILITY_NUMERATOR,
    )


def _expected_reward_per_wager(reward_per_win, win_probability_numerator, win_probability_denominator):
    with localcontext() as context:
        context.prec = 50
        expected = (
            _to_decimal(reward_per_win)
            * Decimal(win_probability_numerator)
            / Decimal(win_probability_denominator)
        )
    return expected.quantize(Decimal("0.00000001"))


def _build_dec_poker_payout_table(instance, version, market_valuation=None):
    reward_per_win = _to_decimal(instance.reward_per_win)
    minimum_wager = _to_decimal(instance.entry_fee_evr)
    if reward_per_win <= 0:
        raise ValueError("DEC payout policy requires a positive reward per win.")
    if minimum_wager <= 0:
        raise ValueError("DEC payout policy requires a positive minimum wager.")

    house_rule = _normalize_dec_house_rule(instance.active_house_rule)
    (
        win_probability_numerator,
        win_probability_denominator,
        loss_probability_numerator,
    ) = _payout_odds_for_house_rule(house_rule)
    expected_reward = _expected_reward_per_wager(
        reward_per_win,
        win_probability_numerator,
        win_probability_denominator,
    )
    payout_table = {
        "schema_version": DEC_PAYOUT_POLICY_SCHEMA_VERSION,
        "policy_version": int(version),
        "game_rule_version": DEC_PAYOUT_RULE_VERSION,
        "house_rule": house_rule,
        "wager": {
            "currency": "EVR",
            "minimum_amount": _decimal_string(minimum_wager),
        },
        "payout": {
            "currency": str(instance.reward_asset_name),
            "win_amount": _decimal_string(reward_per_win),
            "cap_amount": _decimal_string(reward_per_win),
        },
        "outcomes": [
            {
                "result": DecPokerHand.RESULT_WIN,
                "probability_numerator": win_probability_numerator,
                "probability_denominator": win_probability_denominator,
                "payout_amount": _decimal_string(reward_per_win),
            },
            {
                "result": DecPokerHand.RESULT_LOSE,
                "probability_numerator": loss_probability_numerator,
                "probability_denominator": win_probability_denominator,
                "payout_amount": "0",
            },
        ],
        "expected_return": {
            "currency": str(instance.reward_asset_name),
            "amount_per_wager": _decimal_string(expected_reward),
        },
        "rtp": {
            "status": DecPokerPayoutPolicy.RTP_STATUS_VALUATION_REQUIRED,
            "percent": None,
            "disclosure": DEC_PAYOUT_RTP_DISCLOSURE,
        },
    }
    if market_valuation is None:
        return payout_table

    payout_table["expected_return"]["evr_amount_per_wager"] = _decimal_string(
        market_valuation.expected_return_evr
    )
    payout_table["rtp"] = {
        "status": DecPokerPayoutPolicy.RTP_STATUS_DISCLOSED,
        "percent": format(_to_decimal(market_valuation.rtp_percent).quantize(Decimal("0.000001")), "f"),
        "disclosure": (
            "Percentage RTP is derived from the versioned direct "
            f"{instance.reward_asset_name}/EVR settled-execution valuation snapshot "
            f"{market_valuation.valuation_hash}."
        ),
        "valuation": {
            "valuation_hash": str(market_valuation.valuation_hash),
            "source_type": str(market_valuation.source_type),
            "trading_pair_id": int(market_valuation.trading_pair_id),
            "base_token": str(market_valuation.trading_pair.base_token),
            "quote_token": str(market_valuation.trading_pair.quote_token),
            "price_evr_per_reward_asset": _decimal_string(
                market_valuation.price_evr_per_reward_asset
            ),
            "source_execution_count": int(market_valuation.source_execution_count),
            "source_volume": _decimal_string(market_valuation.source_volume),
            "source_started_at": market_valuation.source_started_at.isoformat(),
            "source_ended_at": market_valuation.source_ended_at.isoformat(),
        },
    }
    return payout_table


def _payout_policy_snapshot(policy):
    return {
        "policy_version": int(policy.version),
        "policy_hash": str(policy.policy_hash),
        "payout_table": policy.payout_table,
    }


def _payout_policy_matches_instance(policy, instance):
    if policy is None:
        return False
    table = policy.payout_table or {}
    payout = table.get("payout") or {}
    wager = table.get("wager") or {}
    return (
        str(table.get("house_rule") or "") == _normalize_dec_house_rule(instance.active_house_rule)
        and str(payout.get("currency") or "") == str(instance.reward_asset_name)
        and _to_decimal(payout.get("win_amount")) == _to_decimal(instance.reward_per_win)
        and _to_decimal(wager.get("minimum_amount")) == _to_decimal(instance.entry_fee_evr)
    )


def publish_dec_poker_payout_policy(instance, *, market_valuation=None, actor_user=None):
    if instance is None or not getattr(instance, "pk", None):
        raise ValueError("A saved DEC game instance is required to publish a payout policy.")
    if market_valuation is not None:
        if market_valuation.game_instance_id != instance.pk:
            raise ValueError("A DEC payout policy can only use its own game's market valuation.")
        if actor_user is None:
            raise ValueError("A configured audit authority must publish a DEX-valued DEC payout policy.")

    authority_evidence = {}
    if market_valuation is not None:
        _authority, authority_evidence = _require_dec_poker_audit_authority(
            instance,
            actor_user=actor_user,
            purpose="publish a DEX-valued payout policy",
            require_game_manager=True,
        )

    with transaction.atomic():
        locked_instance = DecPokerGameInstance.objects.select_for_update().get(pk=instance.pk)
        latest_policy = locked_instance.payout_policies.order_by("-version").first()
        next_version = int(latest_policy.version) + 1 if latest_policy else 1
        payout_table = _build_dec_poker_payout_table(
            locked_instance,
            next_version,
            market_valuation=market_valuation,
        )
        policy_hash = _sha256_hex(_canonical_json(payout_table))
        payout = payout_table["payout"]
        wager = payout_table["wager"]
        rtp = payout_table["rtp"]
        publication_event = {}
        if market_valuation is not None:
            publication_policy = _active_dec_policy(
                locked_instance.network_mode,
                required_stages=(DEC_POLICY_PUBLICATION_STAGE,),
            )
            if publication_policy is None:
                raise ValueError(
                    "A verified DEC messaging channel that declares payout_policy_published "
                    "is required before publishing a DEX-valued policy."
                )
            publication_event = broadcast_dec_stage(
                locked_instance,
                DEC_POLICY_PUBLICATION_STAGE,
                actor_user,
                {
                    "payout_policy_version": next_version,
                    "payout_policy_hash": policy_hash,
                    "valuation_hash": market_valuation.valuation_hash,
                    "trading_pair_id": market_valuation.trading_pair_id,
                    "price_evr_per_reward_asset": _decimal_string(
                        market_valuation.price_evr_per_reward_asset
                    ),
                    "rtp_percent": format(_to_decimal(market_valuation.rtp_percent), "f"),
                },
            )
            if publication_event.get("status") != "broadcasted":
                raise ValueError(
                    "DEC payout-policy publication was not recorded on the required messaging channel: "
                    f"{publication_event.get('reason') or 'unknown channel error'}"
                )
        policy = DecPokerPayoutPolicy.objects.create(
            game_instance=locked_instance,
            market_valuation=market_valuation,
            version=next_version,
            game_rule_version=str(payout_table["game_rule_version"]),
            house_rule=str(payout_table["house_rule"]),
            wager_currency=str(wager["currency"]),
            payout_currency=str(payout["currency"]),
            minimum_wager_evr=_to_decimal(wager["minimum_amount"]),
            reward_per_win=_to_decimal(payout["win_amount"]),
            payout_cap_amount=_to_decimal(payout["cap_amount"]),
            win_probability_numerator=int(
                payout_table["outcomes"][0]["probability_numerator"]
            ),
            win_probability_denominator=int(
                payout_table["outcomes"][0]["probability_denominator"]
            ),
            expected_reward_per_wager=_to_decimal(
                payout_table["expected_return"]["amount_per_wager"]
            ),
            rtp_status=str(rtp["status"]),
            rtp_percent=_to_decimal(rtp["percent"]) if rtp["percent"] is not None else None,
            rtp_disclosure=str(rtp["disclosure"]),
            payout_table=payout_table,
            authority_evidence={
                "authority": authority_evidence,
                "publication_event": publication_event,
            } if market_valuation is not None else {},
            policy_hash=policy_hash,
        )
        locked_instance.active_payout_policy = policy
        locked_instance.save(update_fields=["active_payout_policy", "updated_at"])
    return policy


def ensure_dec_poker_payout_policy(instance):
    active_policy = getattr(instance, "active_payout_policy", None)
    if active_policy is None:
        return publish_dec_poker_payout_policy(instance)
    if not _payout_policy_matches_instance(active_policy, instance):
        raise ValueError(
            "The active DEC payout policy does not match this game's current terms. "
            "Publish a new payout-policy version before accepting wagers."
        )
    return active_policy


def _normalize_positive_amount(value, *, field_label, decimal_places):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_label} must be a valid decimal amount.") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{field_label} must be greater than zero.")
    quantum = Decimal("1").scaleb(-int(decimal_places))
    normalized = amount.quantize(quantum)
    if normalized != amount:
        raise ValueError(f"{field_label} supports at most {decimal_places} decimal places.")
    return normalized


def _direct_dec_poker_valuation_pair(instance, *, lock=False):
    filters = {
        "network_mode": instance.network_mode,
        "pair_key": TradingPair.build_pair_key(instance.reward_asset_name, "EVR"),
    }
    pairs = TradingPair.objects.select_for_update() if lock else TradingPair.objects
    pair = pairs.filter(**filters).first()
    if pair is None:
        return None
    if pair.base_token != instance.reward_asset_name or pair.quote_token != "EVR":
        raise ValueError(
            "DEC valuation requires a direct reward-asset/EVR pair; the existing pair is oriented "
            f"as {pair.base_token}/{pair.quote_token}."
        )
    if not pair.is_active:
        raise ValueError("The direct DEC reward-asset/EVR valuation pair is inactive.")
    return pair


def _ensure_direct_dec_poker_valuation_pair(instance, actor_user):
    pair = _direct_dec_poker_valuation_pair(instance, lock=True)
    if pair is not None:
        return pair
    return TradingPair.objects.create(
        base_token=instance.reward_asset_name,
        quote_token="EVR",
        network_mode=instance.network_mode,
        is_active=True,
        created_by=actor_user,
    )


def verify_dec_poker_audit_authority(authority):
    if authority is None or not getattr(authority, "pk", None):
        raise ValueError("A saved DEC audit authority is required.")
    if authority.network_mode != DEC_NETWORK_MODE:
        raise ValueError("DEC audit authority validation is available only on public Evrmore testnet.")
    if not WalletAddress.objects.filter(
        wallet__user=authority.authority_account,
        network_mode=authority.network_mode,
        address=authority.authority_address,
    ).exists():
        message = "The configured audit authority address is not owned by the configured authority account."
        DecPokerAuditAuthority.objects.filter(pk=authority.pk).update(
            last_verified_at=timezone.now(),
            last_verification_evidence={},
            last_verification_error=message,
        )
        raise ValueError(message)

    try:
        evidence = get_public_restricted_asset_authority_evidence(
            authority.restricted_asset_name,
            authority.required_verifier_string,
            authority.required_qualifier_name,
            authority.authority_address,
            authority.minimum_restricted_asset_balance,
            network_mode=authority.network_mode,
        )
    except Exception as exc:
        message = f"Unable to verify DEC restricted audit authority on public testnet: {exc}"
        DecPokerAuditAuthority.objects.filter(pk=authority.pk).update(
            last_verified_at=timezone.now(),
            last_verification_evidence={},
            last_verification_error=message,
        )
        raise ValueError(message) from exc

    DecPokerAuditAuthority.objects.filter(pk=authority.pk).update(
        last_verified_at=timezone.now(),
        last_verification_evidence=evidence,
        last_verification_error="" if evidence.get("is_authorized") else "Restricted authority requirements were not met.",
    )
    if not evidence.get("is_authorized"):
        raise ValueError("The configured DEC restricted audit authority is not currently authorized on public testnet.")
    return evidence


def _require_dec_poker_audit_authority(
    instance,
    *,
    actor_user,
    purpose,
    require_game_manager=False,
):
    authority = DecPokerAuditAuthority.objects.filter(
        network_mode=instance.network_mode,
        status=DecPokerAuditAuthority.STATUS_ACTIVE,
    ).select_related("authority_account").first()
    if authority is None:
        raise ValueError(
            f"An active restricted DEC audit authority is required to {purpose}."
        )
    if require_game_manager and authority.authority_account_id != instance.manager_account_id:
        raise ValueError(
            f"The DEC audit authority must be this game's manager to {purpose}."
        )
    is_authority_account = authority.authority_account_id == getattr(actor_user, "pk", None)
    is_delegated_superuser = bool(getattr(actor_user, "is_superuser", False)) and not is_authority_account
    if not is_authority_account and not is_delegated_superuser:
        raise ValueError(
            f"Only the configured DEC audit authority account or a superuser may {purpose}."
        )

    evidence = verify_dec_poker_audit_authority(authority)
    return authority, {
        **evidence,
        "operation": {
            "operator_account_id": getattr(actor_user, "pk", None),
            "operator_username": str(getattr(actor_user, "username", "") or ""),
            "authority_account_id": authority.authority_account_id,
            "manager_account_id": instance.manager_account_id,
            "delegated_superuser": is_delegated_superuser,
        },
    }


def _enforced_dec_poker_settlement_authority_evidence(instance):
    authority = DecPokerAuditAuthority.objects.filter(
        network_mode=instance.network_mode,
        status=DecPokerAuditAuthority.STATUS_ACTIVE,
        enforce_settlement_writes=True,
    ).select_related("authority_account").first()
    if authority is None:
        return None
    if authority.authority_account_id != instance.manager_account_id:
        raise ValueError(
            "DEC settlement writes are enabled for a restricted authority that does not manage this game."
        )
    _authority, evidence = _require_dec_poker_audit_authority(
        instance,
        actor_user=instance.manager_account,
        purpose="write a payout-ledger event",
    )
    return evidence


def create_dec_poker_valuation_bid(actor_user, instance, *, price_evr_per_reward_asset, reward_asset_quantity):
    if instance is None or not getattr(instance, "pk", None):
        raise ValueError("A saved DEC game instance is required to create a valuation bid.")
    if instance.network_mode != DEC_NETWORK_MODE:
        raise ValueError("DEC valuation bids are available only on public Evrmore testnet.")
    if not instance.is_active or instance.status != DecPokerGameInstance.STATUS_ACTIVE:
        raise ValueError("DEC valuation bids require an active game instance.")
    price = _normalize_positive_amount(
        price_evr_per_reward_asset,
        field_label="EVR price per reward asset",
        decimal_places=8,
    )
    quantity = _normalize_positive_amount(
        reward_asset_quantity,
        field_label="Reward asset quantity",
        decimal_places=instance.reward_asset_units,
    )
    reserved_evr = _normalize_positive_amount(
        price * quantity,
        field_label="Reserved EVR",
        decimal_places=8,
    )
    authority, authority_evidence = _require_dec_poker_audit_authority(
        instance,
        actor_user=actor_user,
        purpose="create a DEC valuation bid",
        require_game_manager=True,
    )

    with transaction.atomic():
        locked_instance = DecPokerGameInstance.objects.select_for_update().get(pk=instance.pk)
        pair = _ensure_direct_dec_poker_valuation_pair(locked_instance, actor_user)
        best_ask = LimitOrder.objects.select_for_update().filter(
            trading_pair=pair,
            side="sell",
            status__in=["pending", "partial"],
        ).order_by("price", "created_at").first()
        if best_ask is not None and price >= best_ask.price:
            raise ValueError(
                "The valuation bid must be strictly below the best open ask to remain post-only. "
                f"Best ask: {best_ask.price:.8f} EVR."
            )

        # The normal DEX reservation helper does not invoke matching; this bid stays on book.
        from .views import _create_reserved_limit_order

        order = _create_reserved_limit_order(
            locked_instance.manager_account,
            pair,
            "buy",
            price,
            quantity,
        )
        intent_nonce = uuid.uuid4().hex
        intent_payload = {
            "game_instance_id": locked_instance.pk,
            "trading_pair_id": pair.pk,
            "limit_order_id": order.pk,
            "authority_id": authority.pk,
            "operator_account_id": getattr(actor_user, "pk", None),
            "funding_account_id": locked_instance.manager_account_id,
            "authority_account_id": authority.authority_account_id,
            "delegated_superuser": bool(
                getattr(actor_user, "is_superuser", False)
                and authority.authority_account_id != getattr(actor_user, "pk", None)
            ),
            "price_evr_per_reward_asset": _decimal_string(price),
            "reward_asset_quantity": _decimal_string(quantity),
            "reserved_evr": _decimal_string(reserved_evr),
            "post_only": True,
            "intent_nonce": intent_nonce,
        }
        bid = DecPokerValuationBid.objects.create(
            game_instance=locked_instance,
            trading_pair=pair,
            limit_order=order,
            requested_by=actor_user,
            audit_authority=authority,
            price_evr_per_reward_asset=price,
            reward_asset_quantity=quantity,
            reserved_evr=reserved_evr,
            post_only=True,
            authority_evidence={
                "authority": authority_evidence,
                "intent": intent_payload,
            },
            intent_hash=_sha256_hex(_canonical_json(intent_payload)),
        )

    record_market_stage_event(
        pair,
        stage="order_created",
        actor_user=actor_user,
        order=order,
        details={
            "purpose": "dec_poker_valuation_bid",
            "dec_poker_valuation_bid_id": bid.pk,
            "post_only": True,
            "authority_asset": authority.restricted_asset_name,
            "funding_account_id": locked_instance.manager_account_id,
        },
    )
    return bid


def publish_dec_poker_market_valuation(actor_user, instance, *, now=None):
    if instance is None or not getattr(instance, "pk", None):
        raise ValueError("A saved DEC game instance is required to publish a market valuation.")
    if not instance.is_active or instance.status != DecPokerGameInstance.STATUS_ACTIVE:
        raise ValueError("DEC market valuation requires an active game instance.")
    authority, authority_evidence = _require_dec_poker_audit_authority(
        instance,
        actor_user=actor_user,
        purpose="publish a DEC market valuation",
        require_game_manager=True,
    )
    pair = _direct_dec_poker_valuation_pair(instance)
    if pair is None:
        raise ValueError(
            f"Create a direct {instance.reward_asset_name}/EVR pair and obtain settled executions before publishing RTP."
        )

    valuation_now = now or timezone.now()
    execution_cutoff = valuation_now - DEC_MARKET_VALUATION_LOOKBACK
    candidate_executions = OrderExecution.objects.filter(
        trading_pair=pair,
        created_at__gte=execution_cutoff,
    ).exclude(tx_hash="").order_by("created_at", "pk")
    executions = [
        execution
        for execution in candidate_executions
        if re.fullmatch(r"[0-9a-fA-F]{64}", str(execution.tx_hash or "").strip())
    ]
    if not executions:
        raise ValueError(
            "A DEX-valued RTP requires at least one recent direct reward-asset/EVR execution with a transaction id."
        )

    transaction_evidence = {}
    for execution in executions:
        try:
            transaction_evidence[execution.pk] = get_public_transaction_evidence(
                execution.tx_hash,
                network_mode=instance.network_mode,
                minimum_confirmations=DEC_MARKET_VALUATION_MIN_CONFIRMATIONS,
            )
        except Exception as exc:
            raise ValueError(
                "Unable to independently verify a confirmed DEX settlement transaction "
                f"for execution {execution.pk}: {exc}"
            ) from exc

    with localcontext() as context:
        context.prec = 50
        source_volume = sum((_to_decimal(execution.quantity) for execution in executions), Decimal("0"))
        quoted_evr_volume = sum(
            (_to_decimal(execution.price) * _to_decimal(execution.quantity) for execution in executions),
            Decimal("0"),
        )
        if source_volume <= 0 or quoted_evr_volume <= 0:
            raise ValueError("DEX valuation executions must have positive price and reward-asset volume.")
        price = (quoted_evr_volume / source_volume).quantize(Decimal("0.00000001"))
        expected_reward = _expected_reward_per_wager(
            instance.reward_per_win,
            *_payout_odds_for_house_rule(instance.active_house_rule)[:2],
        )
        expected_return_evr = (expected_reward * price).quantize(Decimal("0.00000001"))
        rtp_percent = (
            expected_return_evr
            / _to_decimal(instance.entry_fee_evr)
            * Decimal("100")
        ).quantize(Decimal("0.000001"))

    market_evidence = {
        "schema_version": 1,
        "source": DEC_MARKET_VALUATION_SOURCE,
        "network_mode": instance.network_mode,
        "trading_pair": {
            "id": pair.pk,
            "base_token": pair.base_token,
            "quote_token": pair.quote_token,
        },
        "maximum_execution_age_seconds": int(DEC_MARKET_VALUATION_LOOKBACK.total_seconds()),
        "executions": [
            {
                "id": execution.pk,
                "transaction_id": str(execution.tx_hash),
                "transaction_evidence": transaction_evidence[execution.pk],
                "price_evr_per_reward_asset": _decimal_string(execution.price),
                "reward_asset_quantity": _decimal_string(execution.quantity),
                "executed_at": execution.created_at.isoformat(),
            }
            for execution in executions
        ],
        "source_volume": _decimal_string(source_volume),
        "quoted_evr_volume": _decimal_string(quoted_evr_volume),
        "price_evr_per_reward_asset": _decimal_string(price),
        "expected_reward_per_wager": _decimal_string(expected_reward),
        "expected_return_evr": _decimal_string(expected_return_evr),
        "rtp_percent": format(rtp_percent, "f"),
    }
    valuation_payload = {
        "game_instance_id": instance.pk,
        "market_evidence": market_evidence,
        "authority_evidence": authority_evidence,
    }
    valuation_hash = _sha256_hex(_canonical_json(valuation_payload))

    with transaction.atomic():
        locked_instance = DecPokerGameInstance.objects.select_for_update().get(pk=instance.pk)
        valuation, _created = DecPokerMarketValuation.objects.get_or_create(
            game_instance=locked_instance,
            valuation_hash=valuation_hash,
            defaults={
                "trading_pair": pair,
                "source_type": DEC_MARKET_VALUATION_SOURCE,
                "source_execution_count": len(executions),
                "source_volume": source_volume,
                "source_started_at": executions[0].created_at,
                "source_ended_at": executions[-1].created_at,
                "price_evr_per_reward_asset": price,
                "expected_return_evr": expected_return_evr,
                "rtp_percent": rtp_percent,
                "market_evidence": market_evidence,
                "authority_evidence": authority_evidence,
            },
        )
        existing_policy = valuation.payout_policies.order_by("-version").first()
        if existing_policy is not None:
            return valuation, existing_policy

    policy = publish_dec_poker_payout_policy(
        instance,
        market_valuation=valuation,
        actor_user=actor_user,
    )
    return valuation, policy


def _ledger_hash_payload(
    *,
    game_instance_id,
    hand_id,
    payout_policy_id,
    sequence,
    event_type,
    correlation_id,
    idempotency_key,
    player_identifier,
    currency,
    stake_amount,
    payout_amount,
    balance_delta,
    result,
    payout_policy_version,
    payout_policy_hash,
    odds_snapshot,
    rng_evidence,
    external_txid,
    event_data,
    occurred_at,
    previous_entry_hash,
):
    return {
        "game_instance_id": int(game_instance_id),
        "hand_id": int(hand_id),
        "payout_policy_id": int(payout_policy_id),
        "sequence": int(sequence),
        "event_type": str(event_type),
        "correlation_id": str(correlation_id),
        "idempotency_key": str(idempotency_key),
        "player_identifier": str(player_identifier),
        "currency": str(currency),
        "stake_amount": _decimal_string(stake_amount),
        "payout_amount": _decimal_string(payout_amount),
        "balance_delta": _decimal_string(balance_delta),
        "result": str(result),
        "payout_policy_version": int(payout_policy_version),
        "payout_policy_hash": str(payout_policy_hash),
        "odds_snapshot": odds_snapshot or {},
        "rng_evidence": rng_evidence or {},
        "external_txid": str(external_txid),
        "event_data": event_data or {},
        "occurred_at": occurred_at.isoformat(),
        "previous_entry_hash": str(previous_entry_hash),
    }


def _hand_rng_evidence(hand):
    return {
        "server_seed_hash": str(hand.server_seed_hash),
        "client_seed": str(hand.client_seed),
        "fairness_nonce": int(hand.fairness_nonce),
        "fairness_digest": str(hand.fairness_digest),
    }


def _append_dec_poker_ledger_entry(
    hand,
    policy,
    event_type,
    *,
    currency,
    stake_amount=Decimal("0"),
    payout_amount=Decimal("0"),
    balance_delta=Decimal("0"),
    external_txid="",
    event_data=None,
):
    existing_entry = DecPokerPayoutLedgerEntry.objects.filter(
        hand=hand,
        event_type=event_type,
    ).first()
    if existing_entry is not None:
        return existing_entry

    authority_evidence = _enforced_dec_poker_settlement_authority_evidence(
        hand.game_instance
    )
    entry_event_data = dict(event_data or {})
    if authority_evidence is not None:
        entry_event_data["audit_authority"] = authority_evidence

    with transaction.atomic():
        locked_instance = DecPokerGameInstance.objects.select_for_update().get(
            pk=hand.game_instance_id
        )
        existing_entry = DecPokerPayoutLedgerEntry.objects.filter(
            hand=hand,
            event_type=event_type,
        ).first()
        if existing_entry is not None:
            return existing_entry

        previous_entry = DecPokerPayoutLedgerEntry.objects.filter(
            game_instance=locked_instance,
        ).order_by("-sequence").first()
        previous_hash = str(previous_entry.entry_hash) if previous_entry else ""
        sequence = int(previous_entry.sequence) + 1 if previous_entry else 1
        occurred_at = timezone.now()
        odds_snapshot = policy.payout_table or {}
        rng_evidence = _hand_rng_evidence(hand)
        payload = _ledger_hash_payload(
            game_instance_id=locked_instance.pk,
            hand_id=hand.pk,
            payout_policy_id=policy.pk,
            sequence=sequence,
            event_type=event_type,
            correlation_id=hand.settlement_id,
            idempotency_key=hand.idempotency_key,
            player_identifier=f"user:{hand.player_id}",
            currency=currency,
            stake_amount=stake_amount,
            payout_amount=payout_amount,
            balance_delta=balance_delta,
            result=hand.result,
            payout_policy_version=policy.version,
            payout_policy_hash=policy.policy_hash,
            odds_snapshot=odds_snapshot,
            rng_evidence=rng_evidence,
            external_txid=external_txid,
            event_data=entry_event_data,
            occurred_at=occurred_at,
            previous_entry_hash=previous_hash,
        )
        entry_hash = _sha256_hex(_canonical_json(payload))
        return DecPokerPayoutLedgerEntry.objects.create(
            game_instance=locked_instance,
            hand=hand,
            payout_policy=policy,
            sequence=sequence,
            event_type=event_type,
            correlation_id=hand.settlement_id,
            idempotency_key=hand.idempotency_key,
            player_identifier=f"user:{hand.player_id}",
            currency=str(currency),
            stake_amount=_to_decimal(stake_amount),
            payout_amount=_to_decimal(payout_amount),
            balance_delta=_to_decimal(balance_delta),
            result=str(hand.result),
            payout_policy_version=policy.version,
            payout_policy_hash=policy.policy_hash,
            odds_snapshot=odds_snapshot,
            rng_evidence=rng_evidence,
            external_txid=str(external_txid or ""),
            event_data=entry_event_data,
            occurred_at=occurred_at,
            previous_entry_hash=previous_hash,
            entry_hash=entry_hash,
        )


def verify_dec_poker_payout_ledger(hand):
    if hand is None:
        raise ValueError("A hand record is required.")

    entries = list(
        DecPokerPayoutLedgerEntry.objects.filter(
            game_instance_id=hand.game_instance_id
        ).order_by("sequence")
    )
    hand_entries = [entry for entry in entries if entry.hand_id == hand.pk]
    expected_sequence = 1
    expected_previous_hash = ""
    sequence_matches = True
    hash_chain_matches = True
    for entry in entries:
        payload = _ledger_hash_payload(
            game_instance_id=entry.game_instance_id,
            hand_id=entry.hand_id,
            payout_policy_id=entry.payout_policy_id,
            sequence=entry.sequence,
            event_type=entry.event_type,
            correlation_id=entry.correlation_id,
            idempotency_key=entry.idempotency_key,
            player_identifier=entry.player_identifier,
            currency=entry.currency,
            stake_amount=entry.stake_amount,
            payout_amount=entry.payout_amount,
            balance_delta=entry.balance_delta,
            result=entry.result,
            payout_policy_version=entry.payout_policy_version,
            payout_policy_hash=entry.payout_policy_hash,
            odds_snapshot=entry.odds_snapshot,
            rng_evidence=entry.rng_evidence,
            external_txid=entry.external_txid,
            event_data=entry.event_data,
            occurred_at=entry.occurred_at,
            previous_entry_hash=entry.previous_entry_hash,
        )
        sequence_matches = sequence_matches and entry.sequence == expected_sequence
        hash_chain_matches = hash_chain_matches and (
            entry.previous_entry_hash == expected_previous_hash
            and entry.entry_hash == _sha256_hex(_canonical_json(payload))
        )
        expected_sequence += 1
        expected_previous_hash = entry.entry_hash

    snapshot = hand.payout_policy_snapshot or {}
    payout_table = snapshot.get("payout_table") or {}
    policy_hash_matches = bool(
        hand.payout_policy_id
        and hand.payout_policy_hash
        and hand.payout_policy_hash == snapshot.get("policy_hash")
        and hand.payout_policy_hash == _sha256_hex(_canonical_json(payout_table))
    )
    return {
        "entry_count": len(hand_entries),
        "chain_entry_count": len(entries),
        "sequence_matches": sequence_matches,
        "hash_chain_matches": hash_chain_matches,
        "policy_hash_matches": policy_hash_matches,
        "is_valid": bool(hand_entries) and sequence_matches and hash_chain_matches and policy_hash_matches,
    }


def _normalize_network_mode(network_mode):
    mode = str(network_mode or DEC_NETWORK_MODE).strip().lower()
    if mode != DEC_NETWORK_MODE:
        raise ValueError("DEC is available only on public Evrmore testnet.")
    return DEC_NETWORK_MODE


@contextmanager
def _using_dec_testnet_rpc():
    with using_network_mode(DEC_NETWORK_MODE):
        yield


def _broadcast_raw_transaction(transaction_data, wif_keys):
    raw_tx = str((transaction_data or {}).get("raw_tx") or "").strip()
    if not raw_tx:
        raise ValueError("The raw transaction builder did not return a transaction payload.")

    txid = str(sign_and_broadcast_raw_transaction(raw_tx, wif_keys=wif_keys) or "").strip()
    if not txid:
        raise ValueError("The raw transaction workflow did not return a broadcast transaction id.")
    return {
        "txid": txid,
        **transaction_data,
    }


def _system_fee_address(network_mode, fallback_address):
    if network_mode == "testnet":
        configured = str(getattr(settings, "DEC_SYSTEM_FEE_ADDRESS_TESTNET", "") or "").strip()
        if configured:
            return configured
    if network_mode == "mainnet":
        configured = str(getattr(settings, "DEC_SYSTEM_FEE_ADDRESS_MAINNET", "") or "").strip()
        if configured:
            return configured
    configured = str(getattr(settings, "DEC_SYSTEM_FEE_ADDRESS", "") or "").strip()
    if configured:
        return configured
    return str(fallback_address or "").strip()


def _wager_treasury_bps():
    try:
        value = int(getattr(settings, "DEC_PLAYER_WAGER_TREASURY_BPS", 5000))
    except (TypeError, ValueError):
        value = 5000
    return max(0, min(10000, value))


def _ensure_system_user():
    system_user, _created = User.objects.get_or_create(
        username="system",
        defaults={
            "email": "system@defitome.local",
            "is_active": True,
            "is_staff": True,
        },
    )
    if not system_user.has_usable_password():
        system_user.set_unusable_password()
        system_user.save(update_fields=["password"])
    return system_user


def _ensure_wallet(user):
    wallet = getattr(user, "user_wallet", None)
    if wallet:
        return wallet
    return UserWallet.objects.create(
        user=user,
        name=f"{user.username} Wallet",
        entropy=secrets.token_hex(16),
        passphrase="",
    )


def _wallet_for(user_wallet, network_mode):
    return Wallet(
        user_wallet.entropy,
        user_wallet.passphrase,
        network_mode=network_mode,
    )


def _ensure_external_address(user_wallet, network_mode, index):
    record = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=network_mode,
        account=0,
        index=index,
        is_change=False,
    ).first()
    if record:
        return record

    wallet_instance = _wallet_for(user_wallet, network_mode)
    address = wallet_instance.get_address(index=index)
    wif = wallet_instance.get_wif(index=index)
    try:
        RPC.importprivkey(wif, str(user_wallet.entropy), False)
    except Exception:
        pass
    record, _created = WalletAddress.objects.get_or_create(
        wallet=user_wallet,
        network_mode=network_mode,
        account=0,
        index=index,
        is_change=False,
        defaults={
            "address": address,
            "wif": wif,
        },
    )
    return record


def _next_external_index(user_wallet, network_mode):
    highest = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=network_mode,
        account=0,
        is_change=False,
    ).order_by("-index").values_list("index", flat=True).first()
    if highest is None:
        return 0
    return int(highest) + 1


def _primary_address_and_wif(user, network_mode):
    user_wallet = getattr(user, "user_wallet", None)
    if user_wallet is None:
        raise ValueError("A wallet is required.")

    address_record = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=network_mode,
        is_change=False,
    ).order_by("account", "index").first()
    if address_record is None:
        address_record = _ensure_external_address(user_wallet, network_mode, 0)

    wif = str(address_record.wif or "").strip()
    if not wif:
        wif = _wallet_for(user_wallet, network_mode).get_wif_for_address(address_record.address)

    return address_record.address, wif


def _address_balance_evr(address):
    try:
        balance_data = RPC.getaddressbalance({"addresses": [address]})
    except Exception:
        return Decimal("0")
    satoshis = int((balance_data or {}).get("balance", 0) or 0)
    return (Decimal(satoshis) / Decimal("100000000")).quantize(Decimal("0.00000001"))


def _funded_address_and_wif(user, network_mode, minimum_evr):
    user_wallet = getattr(user, "user_wallet", None)
    if user_wallet is None:
        raise ValueError("A wallet is required.")

    candidates = []
    queryset = WalletAddress.objects.filter(
        wallet=user_wallet,
        network_mode=network_mode,
        is_change=False,
    ).order_by("account", "index")

    for address_record in queryset:
        balance = _address_balance_evr(address_record.address)
        candidates.append((balance, address_record))

    if not candidates:
        address, wif = _primary_address_and_wif(user, network_mode)
        return address, wif

    candidates.sort(key=lambda item: item[0], reverse=True)
    for balance, address_record in candidates:
        if balance >= minimum_evr:
            wif = str(address_record.wif or "").strip()
            if not wif:
                wif = _wallet_for(user_wallet, network_mode).get_wif_for_address(address_record.address)
            return address_record.address, wif

    richest_balance, richest_record = candidates[0]
    raise ValueError(
        f"Insufficient EVR on any single wallet address for DEC issuance. Needed at least {minimum_evr} EVR, richest address holds {richest_balance} EVR."
    )


def _build_reward_metadata(instance_name, reward_asset_name, network_mode):
    return {
        "schema": "defitome.dec-reward-token",
        "version": 1,
        "asset_name": reward_asset_name,
        "network_mode": network_mode,
        "game_type": "simple_poker",
        "game_instance_name": instance_name,
        "issued_at": timezone.now().isoformat(),
    }


def _upload_json(payload, file_name):
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return KuboAPIUploader().upload_bytes(
        payload_bytes,
        file_name=file_name,
        pin=True,
        cid_version=0,
    )


def _sha256_hex(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _new_server_seed():
    return secrets.token_hex(32)


def _ensure_instance_fairness_material(instance):
    if instance.active_server_seed_secret and instance.active_server_seed_hash:
        return

    seed = _new_server_seed()
    instance.active_server_seed_secret = seed
    instance.active_server_seed_hash = _sha256_hex(seed)
    instance.active_house_rule = DEC_HOUSE_RULE_DEALER_BEST_TWO_OF_THREE
    if not instance.next_hand_nonce:
        instance.next_hand_nonce = 1
    instance.save(update_fields=[
        "active_server_seed_secret",
        "active_server_seed_hash",
        "active_house_rule",
        "next_hand_nonce",
        "updated_at",
    ])


def _estimate_instance_fee_evr(source_address, issuer_address, reward_asset_name, metadata_cid):
    tx_data = create_raw_asset_operation_transaction(
        from_address=source_address,
        operation_address=issuer_address,
        operation_payload={
            "issue": {
                "asset_name": reward_asset_name,
                "asset_quantity": float(Decimal("1")),
                "units": 0,
                "reissuable": 0,
                "has_ipfs": 1,
                "remintable": 0,
                "ipfs_hash": metadata_cid,
            }
        },
        burn_amount_evr=Decimal("500"),
        burn_address=_resolve_burn_address('issue_asset'),
        fee_evr=None,
    )
    outputs = tx_data.get("outputs") or []
    input_count = len(tx_data.get("inputs") or [])
    output_count = len(outputs)
    fee_satoshis = _resolve_fee_satoshis(
        explicit_fee_evr=None,
        input_count=input_count or 1,
        output_count=output_count or 1,
    )
    estimated_fee = _satoshis_to_evr(fee_satoshis)
    if estimated_fee <= 0:
        raise ValueError("Unable to derive an estimated issue fee for DEC instancing.")
    return (estimated_fee * Decimal("2")).quantize(Decimal("0.00000001"))


def ensure_shared_dec_channel(user, payload):
    network_mode = _normalize_network_mode(payload.get("network_mode"))
    existing = _active_dec_policy(network_mode)
    if existing:
        _refresh_dec_channel_policy(existing)
        return {
            "created": False,
            "policy": existing,
            "txid": str(existing.issuance_txid or ""),
            "channel_name": str(existing.channel_name or ""),
            "chain_metadata_status": str(existing.chain_metadata_status or ""),
        }

    existing = MessageChannelPolicy.objects.filter(
        channel_key=DEC_CHANNEL_KEY,
        network_mode=network_mode,
        version=UNIFIED_WORKFLOW_POLICY_VERSION,
        status="active",
    ).order_by("-version").first()
    if existing:
        _refresh_dec_channel_policy(existing)
        return {
            "created": False,
            "policy": existing,
            "txid": str(existing.issuance_txid or ""),
            "channel_name": str(existing.channel_name or ""),
            "chain_metadata_status": str(existing.chain_metadata_status or ""),
        }

    admin_asset = str(payload.get("channel_admin_asset") or "").strip().upper()
    channel_tag = str(payload.get("channel_tag") or UNIFIED_WORKFLOW_CHANNEL_TAG).strip()
    if not admin_asset:
        raise ValueError("A shared DEC channel admin asset is required before creating the first game.")
    if not channel_tag:
        raise ValueError("A shared DEC channel tag is required before creating the first game.")

    with _using_dec_testnet_rpc():
        creation = create_channel_console_asset_for_user(user, {
            "admin_asset": admin_asset,
            "channel_tag": channel_tag,
            "channel_key": DEC_CHANNEL_KEY,
            "channel_name": str(payload.get("channel_name") or "DeFiTome Unified v5 Console").strip(),
            "network_mode": network_mode,
            "channel_version": UNIFIED_WORKFLOW_POLICY_VERSION,
            "metadata": {
                "description": UNIFIED_WORKFLOW_DESCRIPTION,
                "allowed_stages": list(DEC_CHANNEL_ALLOWED_STAGES),
                "strict_rules": {
                    **UNIFIED_WORKFLOW_STRICT_RULES,
                    "auto_broadcast": False,
                },
                "console_type": "defitome_workflow_event",
            },
        })

    policy = MessageChannelPolicy.objects.filter(
        channel_key=DEC_CHANNEL_KEY,
        network_mode=network_mode,
        version=UNIFIED_WORKFLOW_POLICY_VERSION,
    ).first()
    if policy is None:
        raise ValueError("Unable to create the shared DEC v5 channel policy.")
    _refresh_dec_channel_policy(policy)
    return {
        "created": not bool(creation.get("existing_issuance")),
        "policy": policy,
        "txid": str(creation.get("txid") or ""),
        "channel_name": str(creation.get("channel_asset_name") or ""),
        "chain_metadata_status": str(getattr(policy, "chain_metadata_status", "") or ""),
    }


def preview_dec_poker_instance_plan(user, payload):
    network_mode = _normalize_network_mode(payload.get("network_mode"))
    reward_asset_name = _normalize_reward_asset_name(payload.get("reward_asset_name"))
    reward_asset_units = int(payload.get("reward_asset_units") or 2)
    entry_fee_evr = _to_decimal(payload.get("entry_fee_evr"), default=Decimal("0.5"))
    reward_supply = _to_decimal(payload.get("reward_supply"), default=Decimal("10000"))
    reward_per_win = _to_decimal(payload.get("reward_per_win"), default=Decimal("10"))
    hand_cooldown_seconds = _normalize_hand_cooldown_seconds(payload.get("hand_cooldown_seconds"))
    if reward_asset_units < 0 or reward_asset_units > 8:
        raise ValueError("Reward asset units must be between 0 and 8.")

    user_wallet = getattr(user, "user_wallet", None)
    if user_wallet is None:
        raise ValueError("Create your wallet before previewing DEC instancing.")

    with _using_dec_testnet_rpc():
        creator_address, _creator_wif = _primary_address_and_wif(user, network_mode)
        estimated_instance_fee = _estimate_instance_fee_evr(
            source_address=creator_address,
            issuer_address=creator_address,
            reward_asset_name=reward_asset_name,
            metadata_cid=DEC_PLACEHOLDER_METADATA_TXID,
        )

    return {
        "network_mode": network_mode,
        "reward_asset_name": reward_asset_name,
        "reward_asset_units": reward_asset_units,
        "entry_fee_evr": entry_fee_evr,
        "reward_supply": reward_supply,
        "reward_per_win": reward_per_win,
        "hand_cooldown_seconds": hand_cooldown_seconds,
        "estimated_issue_fee_evr": (estimated_instance_fee / Decimal("2")).quantize(Decimal("0.00000001")),
        "estimated_instance_fee_evr": estimated_instance_fee,
        "required_burn_evr": Decimal("500.00000000"),
        "treasury_split_percent": Decimal(_wager_treasury_bps()) / Decimal("100"),
        "vault_split_percent": Decimal(10000 - _wager_treasury_bps()) / Decimal("100"),
    }


def verify_dec_poker_hand(hand):
    if hand is None:
        raise ValueError("A hand record is required.")
    outcome_detail = hand.outcome_detail or {}
    recomputed = _draw_simple_poker_hand(
        hand.server_seed_revealed,
        hand.client_seed,
        hand.fairness_nonce,
        house_rule=outcome_detail.get("house_rule", DEC_HOUSE_RULE_LEGACY),
    )
    player_cards_match = recomputed["player_cards"] == (hand.player_cards or [])
    dealer_cards_match = recomputed["dealer_cards"] == (hand.dealer_cards or [])
    result_match = recomputed["result"] == str(hand.result or "")
    digest_match = recomputed["fairness_digest"] == str(hand.fairness_digest or "")
    commitment_match = _sha256_hex(hand.server_seed_revealed) == str(hand.server_seed_hash or "")
    return {
        "player_cards_match": player_cards_match,
        "dealer_cards_match": dealer_cards_match,
        "result_match": result_match,
        "digest_match": digest_match,
        "commitment_match": commitment_match,
        "house_rule": recomputed["house_rule"],
        "house_rule_label": dec_poker_house_rule_label(recomputed["house_rule"]),
        "is_valid": all([
            player_cards_match,
            dealer_cards_match,
            result_match,
            digest_match,
            commitment_match,
        ]),
        "recomputed": recomputed,
    }


def update_dec_instance_admin(instance, *, treasury_bps=None, hand_cooldown_seconds=None, status=None):
    if instance is None:
        raise ValueError("A DEC instance is required.")

    update_fields = ["updated_at"]
    if treasury_bps is not None:
        try:
            normalized_bps = int(treasury_bps)
        except (TypeError, ValueError) as exc:
            raise ValueError("Treasury split must be a whole-number percentage in basis points.") from exc
        if normalized_bps < 0 or normalized_bps > 10000:
            raise ValueError("Treasury split must be between 0 and 10000 basis points.")
        instance.wager_treasury_bps = normalized_bps
        update_fields.append("wager_treasury_bps")

    if hand_cooldown_seconds is not None:
        instance.hand_cooldown_seconds = _normalize_hand_cooldown_seconds(hand_cooldown_seconds)
        update_fields.append("hand_cooldown_seconds")

    if status is not None:
        normalized_status = str(status).strip().lower()
        allowed_statuses = {
            DecPokerGameInstance.STATUS_PAUSED,
            DecPokerGameInstance.STATUS_RETIRED,
        }
        if normalized_status not in allowed_statuses:
            raise ValueError("DEC instances can only be activated by verified provisioning.")
        instance.status = normalized_status
        instance.is_active = False
        update_fields.extend(["status", "is_active"])

    instance.save(update_fields=update_fields)
    return instance


def _active_dec_policy(network_mode, required_stages=()):
    required = {str(stage).strip().lower() for stage in DEC_CHANNEL_ALLOWED_STAGES}
    required.update(str(stage).strip().lower() for stage in required_stages if str(stage).strip())
    policies = MessageChannelPolicy.objects.filter(
        channel_key=DEC_CHANNEL_KEY,
        network_mode=network_mode,
        version__gte=UNIFIED_WORKFLOW_POLICY_VERSION,
        status="active",
        chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
    ).order_by("-version")

    for policy in policies:
        allowed = {str(stage).strip().lower() for stage in (policy.allowed_stages or [])}
        if required.issubset(allowed):
            return policy
    return None


def _refresh_dec_channel_policy(policy):
    if policy is None:
        return None

    try:
        with _using_dec_testnet_rpc():
            validation = validate_channel_console_asset(
                policy.channel_name,
                network_mode=DEC_NETWORK_MODE,
            )
    except Exception as exc:
        policy.chain_metadata_error = str(exc)
        policy.save(update_fields=["chain_metadata_error", "updated_at"])
        return policy

    policy.chain_metadata_status = MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED
    policy.chain_metadata_error = ""
    policy.metadata_ipfs_cid = str(validation.get("ipfs_cid") or policy.metadata_ipfs_cid)
    policy.save(update_fields=[
        "chain_metadata_status",
        "chain_metadata_error",
        "metadata_ipfs_cid",
        "updated_at",
    ])
    return policy


def _channel_signer(policy):
    candidates = []
    for account in (policy.manager_account, policy.owner_account):
        if account and account.pk not in {item.pk for item in candidates}:
            candidates.append(account)

    for account in candidates:
        user_wallet = getattr(account, "user_wallet", None)
        if user_wallet is None:
            continue

        profile = WalletProfile.objects.select_related("address").filter(
            wallet=user_wallet,
            network_mode=policy.network_mode,
            is_main=True,
        ).first()
        address_record = profile.address if profile else WalletAddress.objects.filter(
            wallet=user_wallet,
            network_mode=policy.network_mode,
            account=0,
            index=0,
            is_change=False,
        ).first()
        if address_record is None:
            continue

        balances = RPC.listassetbalancesbyaddress(address_record.address) or {}
        if _to_decimal(balances.get(policy.channel_name, 0)) >= Decimal("1"):
            wif = str(address_record.wif or "").strip()
            if not wif:
                wif = _wallet_for(user_wallet, policy.network_mode).get_wif_for_address(address_record.address)
            return address_record.address, wif

    raise ValueError(f"No policy owner or manager address holds message channel {policy.channel_name}.")


def _stable_dec_aggregate_id(instance, aggregate_type, source_key):
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"defitome:{_normalize_network_mode(instance.network_mode)}:{aggregate_type}:{source_key}",
    ))


def _build_dec_stage_payload(instance, stage, actor_user, details):
    normalized_stage = str(stage or "").strip().lower()
    if normalized_stage not in set(DEC_CHANNEL_ALLOWED_STAGES):
        raise ValueError(f"Unsupported DEC channel stage: {normalized_stage!r}.")
    instance_id = getattr(instance, "pk", None) or getattr(instance, "id", None)
    if not instance_id:
        raise ValueError("DEC channel events require a saved game instance.")

    event_context = dict(details or {})
    network_mode = _normalize_network_mode(instance.network_mode)
    transaction_id = ""
    if normalized_stage in DEC_HAND_STAGE_SEQUENCES:
        settlement_id = str(event_context.get("settlement_id") or "").strip()
        try:
            aggregate_id = str(uuid.UUID(settlement_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(
                f"DEC {normalized_stage} channel events require a hand settlement_id UUID."
            ) from exc
        aggregate_type = DEC_HAND_AGGREGATE_TYPE
        aggregate_sequence = DEC_HAND_STAGE_SEQUENCES[normalized_stage]
        transaction_id = str(
            event_context.get(
                "spend_txid" if normalized_stage == "game_spend_recorded" else "reward_txid",
            ) or ""
        ).strip()
    elif normalized_stage == "game_instance_created":
        aggregate_type = DEC_GAME_INSTANCE_AGGREGATE_TYPE
        aggregate_id = _stable_dec_aggregate_id(instance, aggregate_type, instance_id)
        aggregate_sequence = 1
        transaction_id = str(event_context.get("issue_txid") or "").strip()
    elif normalized_stage == DEC_POLICY_PUBLICATION_STAGE:
        try:
            policy_version = int(event_context.get("payout_policy_version"))
        except (TypeError, ValueError) as exc:
            raise ValueError("DEC payout-policy channel events require a positive policy version.") from exc
        if policy_version < 1:
            raise ValueError("DEC payout-policy channel events require a positive policy version.")
        aggregate_type = DEC_PAYOUT_POLICY_AGGREGATE_TYPE
        aggregate_id = _stable_dec_aggregate_id(instance, aggregate_type, policy_version)
        aggregate_sequence = 1
    else:
        raise ValueError(f"DEC channel stage {normalized_stage!r} is not replayable.")

    event_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"defitome:{network_mode}:{aggregate_type}:{aggregate_id}:{aggregate_sequence}",
    ))
    payload = {
        "event_type": DEC_GAME_EVENT_TYPE,
        "event_version": 1,
        "event_id": event_id,
        "created_at": timezone.now().isoformat(),
        "network_mode": network_mode,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "aggregate_sequence": aggregate_sequence,
        "stage": normalized_stage,
        "correlation_id": aggregate_id,
        "details": {
            "actor": str(getattr(actor_user, "username", "") or ""),
            "game_instance": {
                "id": int(instance_id),
                "reward_asset_name": str(instance.reward_asset_name or ""),
            },
            "context": event_context,
        },
    }
    if transaction_id:
        payload["transaction_id"] = transaction_id
    return add_payload_checksum(payload)


def broadcast_dec_stage(instance, stage, actor_user, details):
    network_mode = _normalize_network_mode(instance.network_mode)
    policy = None
    payload_cid = ""
    try:
        policy = _active_dec_policy(network_mode, required_stages=(stage,))
        if not policy:
            return {
                "status": "skipped",
                "reason": "no_active_verified_dec_policy",
                "txid": "",
                "policy_id": None,
            }

        payload = _build_dec_stage_payload(instance, stage, actor_user, details)
        validate_channel_event_payload(
            payload,
            getattr(policy, "allowed_stages", DEC_CHANNEL_ALLOWED_STAGES),
        )
        upload = _upload_json(payload, f"dec-{instance.id}-{stage}.json")
        payload_cid = str(upload.cid or "")
        with _using_dec_testnet_rpc():
            signer_address, signer_wif = _channel_signer(policy)
            result = _broadcast_raw_transaction(
                create_raw_asset_transfer_transaction(
                    from_address=signer_address,
                    to_address=signer_address,
                    asset_name=policy.channel_name,
                    asset_quantity=Decimal("1"),
                    message=upload.cid,
                    expire_time=0,
                ),
                [signer_wif],
            )
    except Exception as exc:
        return {
            "status": "failed",
            "reason": str(exc),
            "txid": "",
            "payload_cid": payload_cid,
            "policy_id": getattr(policy, "id", None),
        }

    txid = str(result.get("txid") or "").strip()
    return {
        "status": "broadcasted" if txid else "failed",
        "txid": txid,
        "payload_cid": payload_cid,
        "policy_id": policy.id,
    }


def _normalize_reward_asset_name(raw_name):
    value = str(raw_name or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9._]{3,10}", value):
        raise ValueError(
            "Reward main asset name must be 3-10 characters of A-Z, 0-9, period, or underscore."
        )
    return value


def _vault_reward_balance(instance):
    reward_asset_name = _normalize_reward_asset_name(instance.reward_asset_name)
    vault_address = str(instance.vault_profile.address.address or "").strip()
    if not vault_address:
        raise ValueError("DEC reward vault has no address.")

    balances = RPC.listassetbalancesbyaddress(vault_address)
    if not isinstance(balances, dict):
        raise ValueError("DEC reward vault balance could not be verified on public testnet.")
    return _to_decimal(balances.get(reward_asset_name, 0))


def _require_vault_reward_reserve(instance, required_amount):
    required = _to_decimal(required_amount)
    if required <= 0:
        raise ValueError("DEC reward reserve requirement must be greater than zero.")

    available = _vault_reward_balance(instance)
    if available < required:
        raise ValueError(
            f"DEC reward vault holds {available} {instance.reward_asset_name}; at least {required} is required."
        )
    return available


def _pause_instance_for_insufficient_reward_reserve(instance, required_amount):
    try:
        return _require_vault_reward_reserve(instance, required_amount)
    except ValueError as exc:
        message = str(exc)
        if "DEC reward vault holds" not in message:
            raise
        instance.status = DecPokerGameInstance.STATUS_PAUSED
        instance.is_active = False
        instance.profile_tag_error = message
        instance.save(update_fields=[
            "status",
            "is_active",
            "profile_tag_error",
            "updated_at",
        ])
        raise ValueError(
            "This game was paused because its reward vault cannot fund a winning hand. "
            "Resume verified provisioning before accepting play."
        ) from exc


def _normalize_client_seed(raw_seed):
    value = str(raw_seed or "").strip()
    if not value:
        return "house-default-client-seed"
    if len(value) > 128:
        raise ValueError("Client seed must be 128 characters or less.")
    return value


def _normalize_hand_cooldown_seconds(raw_value):
    if raw_value is None or str(raw_value).strip() == "":
        return DEC_DEFAULT_HAND_COOLDOWN_SECONDS
    try:
        cooldown_seconds = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Hand buffer must be a whole number of seconds.") from exc
    if not DEC_MIN_HAND_COOLDOWN_SECONDS <= cooldown_seconds <= DEC_MAX_HAND_COOLDOWN_SECONDS:
        raise ValueError(
            "Hand buffer must be between 30 seconds and 60 minutes to allow channel events to reconcile."
        )
    return cooldown_seconds


def dec_poker_hand_cooldown_remaining_seconds(instance, now=None):
    cooldown_until = getattr(instance, "hand_cooldown_until", None)
    if cooldown_until is None:
        instance_id = getattr(instance, "pk", None)
        if instance_id:
            last_hand_at = DecPokerHand.objects.filter(
                game_instance_id=instance_id,
            ).order_by("-created_at").values_list("created_at", flat=True).first()
            if last_hand_at is not None:
                cooldown_until = last_hand_at + timedelta(
                    seconds=_normalize_hand_cooldown_seconds(
                        getattr(instance, "hand_cooldown_seconds", None)
                    )
                )
    if cooldown_until is None:
        return 0

    remaining_seconds = (cooldown_until - (now or timezone.now())).total_seconds()
    return max(0, math.ceil(remaining_seconds))


def _claim_dec_poker_hand_slot(instance):
    instance_id = getattr(instance, "pk", None)
    if not instance_id:
        raise ValueError("A persisted DEC game instance is required before playing.")

    with transaction.atomic():
        locked_instance = DecPokerGameInstance.objects.select_for_update().select_related(
            "vault_profile__address",
            "vault_profile__wallet",
        ).get(pk=instance_id)
        if (
            not locked_instance.is_active
            or locked_instance.status != DecPokerGameInstance.STATUS_ACTIVE
        ):
            raise ValueError("This game instance is not active.")
        if not locked_instance.profile_tag_asset_name or not locked_instance.profile_tag_txid:
            raise ValueError(
                "This game instance is missing a verified RIP10 vault profile and cannot accept play."
            )

        remaining_seconds = dec_poker_hand_cooldown_remaining_seconds(locked_instance)
        if remaining_seconds > 0:
            minutes, seconds = divmod(remaining_seconds, 60)
            raise ValueError(
                "The previous hand is still in its channel-event reconciliation buffer. "
                f"Try again in {minutes}m {seconds:02d}s."
            )

        cooldown_seconds = _normalize_hand_cooldown_seconds(
            locked_instance.hand_cooldown_seconds
        )
        locked_instance.hand_cooldown_until = timezone.now() + timedelta(seconds=cooldown_seconds)
        locked_instance.save(update_fields=["hand_cooldown_until", "updated_at"])

    return locked_instance


def _issue_vault_ant_tag(*, creator_address, creator_wif, vault_address, vault_wif, main_asset, title):
    asset = build_address_metadata_asset(main_asset, "ANT", vault_address)
    tag_payload = build_address_name_tag(
        vault_address,
        f"DEC Vault for {title}",
    )
    signature_hash = metadata_signature_hash(tag_payload)
    signature = _sign_metadata_message(vault_address, vault_wif, signature_hash)
    metadata = build_signed_metadata(tag_payload, signature)
    validation = validate_metadata(asset.asset_name, vault_address, metadata)
    if not validation.is_valid:
        raise ValueError("DEC activation requires a valid RIP10 vault profile tag payload.")
    if _verify_metadata_signature(vault_address, metadata) is not True:
        raise ValueError("DEC activation requires a verifiable RIP10 vault profile signature.")

    upload = _upload_json(metadata, f"{asset.asset_name.replace('#', '_')}.json")
    issue_result = _broadcast_raw_transaction(
        create_raw_asset_operation_transaction(
            from_address=creator_address,
            operation_address=vault_address,
            operation_payload={
                "_issue_new_asset": {
                    "asset_name": asset.asset_name,
                    "asset_quantity": 1.0,
                    "units": 0,
                    "reissuable": 0,
                    "has_ipfs": 1,
                    "ipfs_hash": upload.cid,
                },
            },
            burn_amount_evr=Decimal("5"),
            burn_address=_resolve_burn_address("issue_unique_asset"),
            authorization_asset_name=f"{main_asset}!",
            owner_token_change_output=(
                creator_address,
                {"transfer": {f"{main_asset}!": 1.0}},
            ),
        ),
        [creator_wif],
    )
    txid = str(issue_result.get("txid") or "").strip()
    if not txid:
        raise ValueError("DEC activation requires a broadcasted RIP10 vault profile tag transaction.")
    return {
        "asset_name": asset.asset_name,
        "txid": txid,
        "metadata_cid": upload.cid,
    }


def _set_dec_instance_pending(instance, message, update_fields=()):
    instance.status = DecPokerGameInstance.STATUS_PENDING
    instance.is_active = False
    instance.profile_tag_error = str(message or "")
    fields = {
        "status",
        "is_active",
        "profile_tag_error",
        "updated_at",
    }
    fields.update(update_fields)
    instance.save(update_fields=sorted(fields))


def create_dec_poker_instance(creator, payload):
    network_mode = _normalize_network_mode(payload.get("network_mode"))
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("Game title is required.")

    reward_asset_name = _normalize_reward_asset_name(payload.get("reward_asset_name"))
    reward_supply = _to_decimal(payload.get("reward_supply"), default=Decimal("10000"))
    reward_per_win = _to_decimal(payload.get("reward_per_win"), default=Decimal("10"))
    entry_fee_evr = _to_decimal(payload.get("entry_fee_evr"), default=Decimal("0.5"))
    reward_asset_units = int(payload.get("reward_asset_units") or 2)
    hand_cooldown_seconds = _normalize_hand_cooldown_seconds(payload.get("hand_cooldown_seconds"))
    if reward_supply <= 0:
        raise ValueError("Reward supply must be greater than zero.")
    if reward_per_win <= 0:
        raise ValueError("Reward per win must be greater than zero.")
    if entry_fee_evr <= 0:
        raise ValueError("Entry fee must be greater than zero.")
    if reward_asset_units < 0 or reward_asset_units > 8:
        raise ValueError("Reward asset units must be between 0 and 8.")

    existing_instance = DecPokerGameInstance.objects.filter(
        network_mode=network_mode,
        reward_asset_name=reward_asset_name,
    ).order_by("-created_at").first()
    if existing_instance is not None:
        if (
            existing_instance.status == DecPokerGameInstance.STATUS_FAILED
            and not str(existing_instance.reward_issue_txid or "").strip()
        ):
            raise ValueError(
                "A failed DEC instance with this reward asset is retained for audit. "
                "Recover that instance or choose a new reward asset name."
            )
        raise ValueError("A DEC game instance already uses this reward asset on the selected network.")

    policy = _active_dec_policy(network_mode, required_stages=DEC_REQUIRED_STAGES)
    if policy is None:
        raise ValueError(
            "A verified shared DEC messaging channel must cover every game lifecycle stage before instancing a game."
        )

    system_user = _ensure_system_user()
    system_wallet = _ensure_wallet(system_user)
    creator_wallet = getattr(creator, "user_wallet", None)
    if creator_wallet is None:
        raise ValueError("Create your wallet before instancing a game.")

    with transaction.atomic():
        vault_profile_name = f"DEC Vault {reward_asset_name}"
        vault_profile = WalletProfile.objects.select_for_update().filter(
            wallet=system_wallet,
            network_mode=network_mode,
            name=vault_profile_name,
        ).first()
        if vault_profile is not None:
            if DecPokerGameInstance.objects.filter(vault_profile=vault_profile).exists():
                raise ValueError("The DEC reward vault is already attached to another game instance.")
        else:
            next_index = _next_external_index(system_wallet, network_mode)
            vault_address_record = _ensure_external_address(system_wallet, network_mode, next_index)
            vault_profile = WalletProfile.objects.create(
                wallet=system_wallet,
                address=vault_address_record,
                network_mode=network_mode,
                name=vault_profile_name,
                is_main=False,
            )
        fee_address = _system_fee_address(network_mode, vault_profile.address.address)

        instance = DecPokerGameInstance.objects.create(
            creator=creator,
            manager_account=system_user,
            network_mode=network_mode,
            title=title,
            reward_asset_name=reward_asset_name,
            reward_asset_units=reward_asset_units,
            reward_supply=reward_supply,
            reward_per_win=reward_per_win,
            entry_fee_evr=entry_fee_evr,
            instance_fee_evr=Decimal("0"),
            system_fee_address=fee_address,
            wager_treasury_bps=_wager_treasury_bps(),
            hand_cooldown_seconds=hand_cooldown_seconds,
            active_house_rule=DEC_HOUSE_RULE_DEALER_BEST_TWO_OF_THREE,
            vault_profile=vault_profile,
            channel_policy=policy,
            status=DecPokerGameInstance.STATUS_PENDING,
            is_active=False,
        )
        payout_policy = publish_dec_poker_payout_policy(instance)
        instance.active_payout_policy = payout_policy

    try:
        with _using_dec_testnet_rpc():
            creator_address, creator_wif = _funded_address_and_wif(
                creator,
                network_mode,
                minimum_evr=Decimal("501"),
            )
            vault_wif = str(vault_profile.address.wif or "").strip()
            if not vault_wif:
                vault_wif = _wallet_for(system_wallet, network_mode).get_wif_for_address(vault_profile.address.address)
            metadata_payload = _build_reward_metadata(title, reward_asset_name, network_mode)
            metadata_upload = _upload_json(metadata_payload, f"{reward_asset_name}_dec_reward.json")
            instance.instance_fee_evr = _estimate_instance_fee_evr(
                source_address=creator_address,
                issuer_address=creator_address,
                reward_asset_name=reward_asset_name,
                metadata_cid=metadata_upload.cid,
            )
            instance.save(update_fields=["instance_fee_evr", "updated_at"])

            fee_tx = _broadcast_raw_transaction(
                create_raw_evr_transaction(
                    from_address=creator_address,
                    to_address=instance.system_fee_address,
                    amount_evr=instance.instance_fee_evr,
                ),
                [creator_wif],
            )
            instance.instance_fee_txid = str(fee_tx.get("txid") or "")
            instance.save(update_fields=["instance_fee_txid", "updated_at"])

            issue_tx = _broadcast_raw_transaction(
                create_raw_asset_operation_transaction(
                    from_address=creator_address,
                    operation_address=creator_address,
                    operation_payload={
                        "issue": {
                            "asset_name": reward_asset_name,
                            "asset_quantity": float(reward_supply),
                            "units": reward_asset_units,
                            "reissuable": 0,
                            "has_ipfs": 1,
                            "remintable": 0,
                            "ipfs_hash": metadata_upload.cid,
                        },
                    },
                    burn_amount_evr=Decimal("500"),
                    burn_address=_resolve_burn_address("issue_asset"),
                ),
                [creator_wif],
            )
            instance.reward_metadata_cid = metadata_upload.cid
            instance.reward_issue_txid = str(issue_tx.get("txid") or "")
            _set_dec_instance_pending(
                instance,
                "Waiting for the reward main asset to confirm on public testnet.",
                update_fields=("reward_metadata_cid", "reward_issue_txid"),
            )
            return instance
    except Exception as exc:
        instance.status = DecPokerGameInstance.STATUS_FAILED
        instance.is_active = False
        instance.profile_tag_error = str(exc)
        instance.save(update_fields=["status", "is_active", "profile_tag_error", "updated_at"])
        raise


def resume_dec_poker_instance(instance):
    if instance is None:
        raise ValueError("A DEC instance is required.")

    network_mode = _normalize_network_mode(instance.network_mode)
    if not instance.reward_issue_txid:
        raise ValueError("This DEC instance has no recorded reward-asset issuance transaction to resume.")

    policy = _active_dec_policy(network_mode, required_stages=DEC_REQUIRED_STAGES)
    if policy is None:
        raise ValueError("A verified shared DEC messaging channel is required before provisioning can resume.")

    with _using_dec_testnet_rpc():
        if instance.is_active and instance.status == DecPokerGameInstance.STATUS_ACTIVE:
            try:
                _require_vault_reward_reserve(instance, instance.reward_per_win)
            except ValueError as exc:
                _set_dec_instance_pending(
                    instance,
                    f"Active instance reserve verification failed: {exc}",
                )
            else:
                return instance

        reward_asset_data = RPC.getassetdata(instance.reward_asset_name)
        if not isinstance(reward_asset_data, dict) or not reward_asset_data:
            _set_dec_instance_pending(
                instance,
                "Waiting for the reward main asset to confirm on public testnet.",
            )
            return instance

        if not instance.profile_tag_txid:
            creator_address, creator_wif = _primary_address_and_wif(instance.creator, network_mode)
            creator_balances = RPC.listassetbalancesbyaddress(creator_address) or {}
            owner_balance = _to_decimal(creator_balances.get(f"{instance.reward_asset_name}!", 0))
            if owner_balance < Decimal("1"):
                _set_dec_instance_pending(
                    instance,
                    "Waiting for the creator's confirmed reward owner asset UTXO on public testnet.",
                )
                return instance

            vault_address = instance.vault_profile.address.address
            system_wallet = instance.vault_profile.wallet
            vault_wif = str(instance.vault_profile.address.wif or "").strip()
            if not vault_wif:
                vault_wif = _wallet_for(system_wallet, network_mode).get_wif_for_address(vault_address)

            tag_result = _issue_vault_ant_tag(
                creator_address=creator_address,
                creator_wif=creator_wif,
                vault_address=vault_address,
                vault_wif=vault_wif,
                main_asset=instance.reward_asset_name,
                title=instance.title,
            )
            instance.profile_tag_asset_name = str(tag_result.get("asset_name") or "")
            instance.profile_tag_txid = str(tag_result.get("txid") or "")
            _set_dec_instance_pending(
                instance,
                "Waiting for the RIP10 vault profile tag to confirm on public testnet.",
                update_fields=("profile_tag_asset_name", "profile_tag_txid"),
            )
            return instance

        profile_tag_data = RPC.getassetdata(instance.profile_tag_asset_name)
        if not isinstance(profile_tag_data, dict) or not profile_tag_data:
            _set_dec_instance_pending(
                instance,
                "Waiting for the RIP10 vault profile tag to confirm on public testnet.",
            )
            return instance

        if not instance.owner_transfer_txid:
            creator_address, creator_wif = _primary_address_and_wif(instance.creator, network_mode)
            creator_balances = RPC.listassetbalancesbyaddress(creator_address) or {}
            reward_balance = _to_decimal(creator_balances.get(instance.reward_asset_name, 0))
            if reward_balance < instance.reward_supply:
                _set_dec_instance_pending(
                    instance,
                    "Waiting for the creator's confirmed reward main asset UTXO on public testnet.",
                )
                return instance

            vault_address = instance.vault_profile.address.address
            reward_transfer = _broadcast_raw_transaction(
                create_raw_asset_transfer_transaction(
                    from_address=creator_address,
                    to_address=vault_address,
                    asset_name=instance.reward_asset_name,
                    asset_quantity=instance.reward_supply,
                ),
                [creator_wif],
            )
            # This legacy field records the raw transaction that funds the payout vault.
            instance.owner_transfer_txid = str(reward_transfer.get("txid") or "")
            _set_dec_instance_pending(
                instance,
                "Waiting for the payout vault's reward main asset balance to confirm on public testnet.",
                update_fields=("owner_transfer_txid",),
            )
            return instance

        try:
            _require_vault_reward_reserve(instance, instance.reward_supply)
        except ValueError:
            _set_dec_instance_pending(
                instance,
                "Waiting for the payout vault's reward main asset balance to confirm on public testnet.",
            )
            return instance

    _ensure_instance_fairness_material(instance)
    payout_policy = ensure_dec_poker_payout_policy(instance)
    broadcast_dec_stage(
        instance,
        "game_instance_created",
        instance.creator,
        {
            "reward_asset_name": instance.reward_asset_name,
            "reward_supply": str(instance.reward_supply),
            "reward_units": instance.reward_asset_units,
            "entry_fee_evr": str(instance.entry_fee_evr),
            "instance_fee_evr": str(instance.instance_fee_evr),
            "fee_txid": instance.instance_fee_txid,
            "issue_txid": instance.reward_issue_txid,
            "reward_transfer_txid": instance.owner_transfer_txid,
            "payout_policy_version": payout_policy.version,
            "payout_policy_hash": payout_policy.policy_hash,
        },
    )
    instance.channel_policy = policy
    instance.profile_tag_error = ""
    instance.status = DecPokerGameInstance.STATUS_ACTIVE
    instance.is_active = True
    instance.save(update_fields=[
        "channel_policy",
        "profile_tag_error",
        "status",
        "is_active",
        "updated_at",
    ])
    return instance


def _card_deck():
    suits = ["C", "D", "H", "S"]
    ranks = ["A", "K", "Q", "J", "10", "9", "8", "7", "6", "5", "4", "3", "2"]
    return [{"rank": rank, "suit": suit} for suit in suits for rank in ranks]


def _rank_value(rank):
    values = {
        "2": 2,
        "3": 3,
        "4": 4,
        "5": 5,
        "6": 6,
        "7": 7,
        "8": 8,
        "9": 9,
        "10": 10,
        "J": 11,
        "Q": 12,
        "K": 13,
        "A": 14,
    }
    return values.get(str(rank), 0)


def _two_card_score(cards):
    values = sorted([_rank_value(card.get("rank")) for card in cards], reverse=True)
    is_pair = len(values) == 2 and values[0] == values[1]
    if is_pair:
        return (2, values[0], values[1])
    return (1, values[0], values[1])


def _best_two_card_score(cards):
    if len(cards) < 2:
        raise ValueError("At least two cards are required to calculate a poker score.")
    return max(_two_card_score(pair) for pair in combinations(cards, 2))


def _normalize_dec_house_rule(house_rule):
    normalized_rule = str(house_rule or "").strip().lower()
    if normalized_rule == DEC_HOUSE_RULE_LEGACY:
        return DEC_HOUSE_RULE_LEGACY
    return DEC_HOUSE_RULE_DEALER_BEST_TWO_OF_THREE


def dec_poker_house_rule_label(house_rule):
    normalized_rule = _normalize_dec_house_rule(house_rule)
    if normalized_rule == DEC_HOUSE_RULE_LEGACY:
        return "Dealer plays two cards and wins ties."
    return "Dealer selects the best two of three cards and wins ties."


def _deterministic_shuffled_deck(server_seed, client_seed, nonce):
    deck = _card_deck()
    decorated = []
    for card in deck:
        card_code = f"{card['rank']}{card['suit']}"
        digest = _sha256_hex(f"{server_seed}:{client_seed}:{nonce}:{card_code}")
        decorated.append((digest, card))
    decorated.sort(key=lambda item: item[0])
    return [item[1] for item in decorated]


def _draw_simple_poker_hand(
    server_seed,
    client_seed,
    nonce,
    house_rule=DEC_HOUSE_RULE_DEALER_BEST_TWO_OF_THREE,
):
    normalized_house_rule = _normalize_dec_house_rule(house_rule)
    deck = _deterministic_shuffled_deck(server_seed, client_seed, nonce)
    player_cards = deck[:2]
    player_score = _two_card_score(player_cards)
    if normalized_house_rule == DEC_HOUSE_RULE_LEGACY:
        dealer_cards = deck[2:4]
        dealer_score = _two_card_score(dealer_cards)
    else:
        dealer_cards = deck[2:5]
        dealer_score = _best_two_card_score(dealer_cards)
    if player_score > dealer_score:
        result = DecPokerHand.RESULT_WIN
    else:
        result = DecPokerHand.RESULT_LOSE

    return {
        "player_cards": player_cards,
        "dealer_cards": dealer_cards,
        "player_score": player_score,
        "dealer_score": dealer_score,
        "result": result,
        "house_rule": normalized_house_rule,
        "fairness_digest": _sha256_hex(f"{server_seed}:{client_seed}:{nonce}"),
    }


def _normalize_dec_poker_idempotency_key(idempotency_key):
    normalized_key = str(idempotency_key or "").strip()
    if not normalized_key:
        return secrets.token_urlsafe(24)
    if len(normalized_key) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", normalized_key):
        raise ValueError("The DEC hand idempotency key is invalid.")
    return normalized_key


def play_dec_poker_hand(player, instance, wager_evr=None, client_seed=None, idempotency_key=None):
    if instance.reconciliation_status == DecPokerGameInstance.RECONCILIATION_STATUS_REJECTED:
        raise ValueError("This game instance was rejected during channel reconciliation.")
    if not instance.is_active or instance.status != DecPokerGameInstance.STATUS_ACTIVE:
        raise ValueError("This game instance is not active.")
    if not instance.profile_tag_asset_name or not instance.profile_tag_txid:
        raise ValueError("This game instance is missing a verified RIP10 vault profile and cannot accept play.")

    wager = _to_decimal(wager_evr, default=instance.entry_fee_evr)
    if wager < instance.entry_fee_evr:
        raise ValueError(f"Minimum wager is {instance.entry_fee_evr} EVR for this game.")

    network_mode = _normalize_network_mode(instance.network_mode)
    policy = _active_dec_policy(network_mode, required_stages=DEC_REQUIRED_STAGES)
    if policy is None:
        raise ValueError("A verified shared DEC messaging channel is required before playing.")
    if (
        isinstance(instance, DecPokerGameInstance)
        and isinstance(policy, MessageChannelPolicy)
        and instance.channel_policy_id != policy.id
    ):
        instance.channel_policy = policy
        instance.save(update_fields=["channel_policy", "updated_at"])

    normalized_idempotency_key = _normalize_dec_poker_idempotency_key(idempotency_key)
    if getattr(instance, "pk", None):
        existing_hand = DecPokerHand.objects.filter(
            game_instance=instance,
            player=player,
            idempotency_key=normalized_idempotency_key,
        ).first()
        if existing_hand is not None:
            if existing_hand.settlement_status == DecPokerHand.SETTLEMENT_STATUS_SETTLED:
                return existing_hand
            raise ValueError(
                "This wager request already exists and requires settlement reconciliation before it can be retried."
            )

    instance = _claim_dec_poker_hand_slot(instance)
    payout_policy = ensure_dec_poker_payout_policy(instance)

    _ensure_instance_fairness_material(instance)
    normalized_client_seed = _normalize_client_seed(client_seed)
    committed_server_seed_hash = str(instance.active_server_seed_hash or "")
    revealed_server_seed = str(instance.active_server_seed_secret or "")
    fairness_nonce = int(instance.next_hand_nonce or 1)
    hand_data = _draw_simple_poker_hand(
        revealed_server_seed,
        normalized_client_seed,
        fairness_nonce,
        house_rule=instance.active_house_rule,
    )

    treasury_ratio = Decimal(instance.wager_treasury_bps) / Decimal("10000")
    treasury_amount = (wager * treasury_ratio).quantize(Decimal("0.00000001"))
    vault_amount = (wager - treasury_amount).quantize(Decimal("0.00000001"))
    if treasury_amount <= 0:
        treasury_amount = wager
        vault_amount = Decimal("0")

    vault_address = instance.vault_profile.address.address
    spend_amount = treasury_amount
    extra_coin_outputs = {}
    if vault_amount > 0:
        if instance.system_fee_address == vault_address:
            spend_amount = wager
        else:
            extra_coin_outputs[vault_address] = format(vault_amount, 'f')

    reward_amount = Decimal("0")
    reward_transaction_data = None
    vault_wif = ""
    with _using_dec_testnet_rpc():
        _pause_instance_for_insufficient_reward_reserve(instance, instance.reward_per_win)
        player_address, player_wif = _primary_address_and_wif(player, network_mode)
        if hand_data["result"] == DecPokerHand.RESULT_WIN:
            reward_amount = Decimal(str(instance.reward_per_win))
            system_wallet = instance.vault_profile.wallet
            vault_wif = str(instance.vault_profile.address.wif or "").strip()
            if not vault_wif:
                vault_wif = _wallet_for(system_wallet, network_mode).get_wif_for_address(vault_address)
            reward_transaction_data = create_raw_asset_transfer_transaction(
                from_address=vault_address,
                to_address=player_address,
                asset_name=instance.reward_asset_name,
                asset_quantity=reward_amount,
            )

        spend_transaction_data = create_raw_evr_transaction(
            from_address=player_address,
            to_address=instance.system_fee_address,
            amount_evr=spend_amount,
            extra_coin_outputs=extra_coin_outputs,
        )

    outcome_detail = {
        "player_score": list(hand_data["player_score"]),
        "dealer_score": list(hand_data["dealer_score"]),
        "house_rule": hand_data["house_rule"],
        "payout_policy": _payout_policy_snapshot(payout_policy),
        "message_events": {
            "spend": {
                "status": DecPokerHand.MESSAGE_STATUS_SKIPPED,
                "txid": "",
            },
            "reward": {
                "status": DecPokerHand.MESSAGE_STATUS_SKIPPED,
                "txid": "",
            },
        },
    }
    with transaction.atomic():
        hand = DecPokerHand.objects.create(
            game_instance=instance,
            player=player,
            payout_policy=payout_policy,
            payout_policy_version=payout_policy.version,
            payout_policy_hash=payout_policy.policy_hash,
            payout_policy_snapshot=_payout_policy_snapshot(payout_policy),
            idempotency_key=normalized_idempotency_key,
            settlement_status=DecPokerHand.SETTLEMENT_STATUS_ACCEPTED,
            wager_evr=wager,
            reward_amount=reward_amount,
            reward_asset_name=instance.reward_asset_name if reward_amount > 0 else "",
            result=hand_data["result"],
            player_cards=hand_data["player_cards"],
            dealer_cards=hand_data["dealer_cards"],
            outcome_detail=outcome_detail,
            client_seed=normalized_client_seed,
            server_seed_hash=committed_server_seed_hash,
            server_seed_revealed=revealed_server_seed,
            fairness_nonce=fairness_nonce,
            fairness_digest=hand_data["fairness_digest"],
            spend_txid="",
            reward_txid="",
            spend_message_txid="",
            reward_message_txid="",
            spend_message_status=DecPokerHand.MESSAGE_STATUS_SKIPPED,
            reward_message_status=DecPokerHand.MESSAGE_STATUS_SKIPPED,
        )
        _append_dec_poker_ledger_entry(
            hand,
            payout_policy,
            DecPokerPayoutLedgerEntry.EVENT_WAGER_ACCEPTED,
            currency="EVR",
            stake_amount=wager,
            event_data={
                "treasury_amount_evr": _decimal_string(treasury_amount),
                "vault_amount_evr": _decimal_string(vault_amount),
            },
        )

    spend_result = None
    reward_txid = ""
    try:
        with _using_dec_testnet_rpc():
            spend_result = _broadcast_raw_transaction(spend_transaction_data, [player_wif])
        hand.spend_txid = str(spend_result.get("txid") or "")
        hand.settlement_status = DecPokerHand.SETTLEMENT_STATUS_SETTLING
        hand.save(update_fields=["spend_txid", "settlement_status"])
        spend_ledger_entry = _append_dec_poker_ledger_entry(
            hand,
            payout_policy,
            DecPokerPayoutLedgerEntry.EVENT_WAGER_SPEND_SETTLED,
            currency="EVR",
            stake_amount=wager,
            balance_delta=-wager,
            external_txid=hand.spend_txid,
            event_data={
                "treasury_amount_evr": _decimal_string(treasury_amount),
                "vault_amount_evr": _decimal_string(vault_amount),
            },
        )
        if reward_transaction_data is not None:
            with _using_dec_testnet_rpc():
                reward_result = _broadcast_raw_transaction(reward_transaction_data, [vault_wif])
            reward_txid = str(reward_result.get("txid") or "")

        with transaction.atomic():
            instance.active_server_seed_secret = _new_server_seed()
            instance.active_server_seed_hash = _sha256_hex(instance.active_server_seed_secret)
            instance.active_house_rule = DEC_HOUSE_RULE_DEALER_BEST_TWO_OF_THREE
            instance.next_hand_nonce = fairness_nonce + 1
            instance.save(update_fields=[
                "active_server_seed_secret",
                "active_server_seed_hash",
                "active_house_rule",
                "next_hand_nonce",
                "updated_at",
            ])

            hand.reward_txid = reward_txid
            hand.settlement_status = DecPokerHand.SETTLEMENT_STATUS_SETTLED
            hand.settlement_error = ""
            hand.settled_at = timezone.now()
            hand.save(update_fields=[
                "reward_txid",
                "settlement_status",
                "settlement_error",
                "settled_at",
            ])
            resolution_ledger_entry = _append_dec_poker_ledger_entry(
                hand,
                payout_policy,
                DecPokerPayoutLedgerEntry.EVENT_HAND_RESOLVED,
                currency=payout_policy.payout_currency,
                stake_amount=wager,
                payout_amount=reward_amount,
                event_data={
                    "spend_ledger_hash": spend_ledger_entry.entry_hash,
                    "result": hand.result,
                },
            )
            reward_ledger_entry = None
            if reward_amount > 0:
                reward_ledger_entry = _append_dec_poker_ledger_entry(
                    hand,
                    payout_policy,
                    DecPokerPayoutLedgerEntry.EVENT_REWARD_PAYOUT_SETTLED,
                    currency=payout_policy.payout_currency,
                    payout_amount=reward_amount,
                    balance_delta=reward_amount,
                    external_txid=reward_txid,
                    event_data={
                        "resolution_ledger_hash": resolution_ledger_entry.entry_hash,
                    },
                )
    except Exception as exc:
        hand.settlement_status = (
            DecPokerHand.SETTLEMENT_STATUS_RECONCILIATION_REQUIRED
            if hand.spend_txid
            else DecPokerHand.SETTLEMENT_STATUS_FAILED
        )
        hand.settlement_error = str(exc)
        hand.save(update_fields=["settlement_status", "settlement_error"])
        if hand.spend_txid:
            _append_dec_poker_ledger_entry(
                hand,
                payout_policy,
                DecPokerPayoutLedgerEntry.EVENT_RECONCILIATION_REQUIRED,
                currency=payout_policy.payout_currency,
                stake_amount=wager,
                payout_amount=reward_amount,
                external_txid=hand.spend_txid,
                event_data={"reason": str(exc)},
            )
        raise ValueError(
            "DEC wager settlement did not complete. The recorded request will not be broadcast again automatically."
        ) from exc

    spend_message = broadcast_dec_stage(
        instance,
        "game_spend_recorded",
        player,
        {
            "wager_evr": str(wager),
            "treasury_amount_evr": str(treasury_amount),
            "vault_amount_evr": str(vault_amount),
            "spend_txid": str(spend_result.get("txid") or ""),
            "server_seed_hash": committed_server_seed_hash,
            "client_seed": normalized_client_seed,
            "fairness_nonce": fairness_nonce,
            "payout_policy_version": payout_policy.version,
            "payout_policy_hash": payout_policy.policy_hash,
            "ledger_entry_hash": spend_ledger_entry.entry_hash,
            "settlement_id": str(hand.settlement_id),
        },
    )
    reward_message = {
        "status": DecPokerHand.MESSAGE_STATUS_SKIPPED,
        "txid": "",
    }
    if reward_amount > 0:
        reward_message = broadcast_dec_stage(
            instance,
            "game_reward_distributed",
            player,
            {
                "reward_asset_name": instance.reward_asset_name,
                "reward_amount": str(reward_amount),
                "reward_txid": reward_txid,
                "server_seed_hash": committed_server_seed_hash,
                "fairness_nonce": fairness_nonce,
                "payout_policy_version": payout_policy.version,
                "payout_policy_hash": payout_policy.policy_hash,
                "ledger_entry_hash": reward_ledger_entry.entry_hash,
                "settlement_id": str(hand.settlement_id),
            },
        )

    outcome_detail["message_events"] = {
        "spend": spend_message,
        "reward": reward_message,
    }
    hand.outcome_detail = outcome_detail
    hand.spend_message_txid = str(spend_message.get("txid") or "")
    hand.reward_message_txid = str(reward_message.get("txid") or "")
    hand.spend_message_status = str(
        spend_message.get("status") or DecPokerHand.MESSAGE_STATUS_SKIPPED
    )
    hand.reward_message_status = str(
        reward_message.get("status") or DecPokerHand.MESSAGE_STATUS_SKIPPED
    )
    hand.save(update_fields=[
        "outcome_detail",
        "spend_message_txid",
        "reward_message_txid",
        "spend_message_status",
        "reward_message_status",
    ])

    return hand
