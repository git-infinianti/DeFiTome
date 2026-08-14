import secrets
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from Settings.access import FEATURE_DEC_GAME_INSTANCE, user_has_feature_access
from API.channel_console_service import get_owned_admin_assets
from API.models import MessageChannelPolicy
from API.unified_workflow_policy import UNIFIED_WORKFLOW_POLICY_VERSION
from Tome.rpc_client import using_network_mode

from .dec_service import (
    DEC_CHANNEL_ALLOWED_STAGES,
    DEC_CHANNEL_KEY,
    DEC_DEFAULT_HAND_COOLDOWN_SECONDS,
    DEC_NETWORK_MODE,
    create_dec_poker_valuation_bid,
    create_dec_poker_instance,
    dec_poker_hand_cooldown_remaining_seconds,
    dec_poker_house_rule_label,
    ensure_dec_poker_payout_policy,
    ensure_shared_dec_channel,
    play_dec_poker_hand,
    preview_dec_poker_instance_plan,
    publish_dec_poker_market_valuation,
    resume_dec_poker_instance,
    update_dec_instance_admin,
    verify_dec_poker_hand,
    verify_dec_poker_audit_authority,
    verify_dec_poker_payout_ledger,
)
from .models import (
    DecPokerAuditAuthority,
    DecPokerGameInstance,
    DecPokerHand,
    DecPokerMarketValuation,
    DecPokerValuationBid,
    TradingPair,
)


DEC_REWARD_SUPPLY_CHOICES = (
    "1000",
    "5000",
    "10000",
    "25000",
)
DEC_REWARD_UNIT_CHOICES = tuple(range(0, 9))
DEC_ENTRY_FEE_CHOICES = (
    "0.10",
    "0.25",
    "0.50",
    "1.00",
    "5.00",
)
DEC_REWARD_PER_WIN_CHOICES = (
    "1",
    "5",
    "10",
    "25",
    "100",
)
DEC_TREASURY_SPLIT_CHOICES = (
    (0, "0% treasury / 100% vault"),
    (2500, "25% treasury / 75% vault"),
    (5000, "50% treasury / 50% vault"),
    (7500, "75% treasury / 25% vault"),
    (10000, "100% treasury / 0% vault"),
)
DEC_INSTANCE_ADMIN_STATUS_CHOICES = (
    (DecPokerGameInstance.STATUS_PAUSED, "Paused"),
    (DecPokerGameInstance.STATUS_RETIRED, "Retired"),
)
DEC_HAND_COOLDOWN_CHOICES = (
    (30, "30 seconds"),
    (60, "1 minute"),
    (120, "2 minutes"),
    (180, "3 minutes"),
    (300, "5 minutes"),
    (600, "10 minutes"),
    (900, "15 minutes"),
    (1800, "30 minutes"),
    (3600, "60 minutes"),
)


def _can_instance_games(user):
    return user.is_staff or user.is_superuser or user_has_feature_access(user, FEATURE_DEC_GAME_INSTANCE)


def _can_manage_dec_admin(user):
    return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


def _can_configure_dec_audit_authority(user):
    return bool(user and user.is_authenticated and user.is_superuser)


def _shared_dec_channel_health(network_mode):
    policies = MessageChannelPolicy.objects.filter(
        channel_key=DEC_CHANNEL_KEY,
        network_mode=network_mode,
        version__gte=UNIFIED_WORKFLOW_POLICY_VERSION,
    ).order_by('-version')
    active_policy = policies.filter(status='active').first()
    return {
        "active_policy": active_policy,
        "all_policies": list(policies[:5]),
        "is_healthy": bool(
            active_policy
            and active_policy.chain_metadata_status == MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED
            and set(DEC_CHANNEL_ALLOWED_STAGES).issubset(set(active_policy.allowed_stages or []))
        ),
    }


def _dec_creation_choices(channel_health, user):
    active_policy = channel_health.get("active_policy")
    channel_options = []
    if active_policy is not None and channel_health.get("is_healthy"):
        channel_options.append({
            "asset_name": active_policy.channel_name,
            "label": active_policy.channel_name,
        })
    with using_network_mode(DEC_NETWORK_MODE):
        return {
            "channel_options": channel_options,
            "owned_admin_assets": get_owned_admin_assets(user, network_mode=DEC_NETWORK_MODE),
            "reward_supply_choices": DEC_REWARD_SUPPLY_CHOICES,
            "reward_unit_choices": DEC_REWARD_UNIT_CHOICES,
            "entry_fee_choices": DEC_ENTRY_FEE_CHOICES,
            "reward_per_win_choices": DEC_REWARD_PER_WIN_CHOICES,
            "treasury_split_choices": DEC_TREASURY_SPLIT_CHOICES,
            "status_choices": DEC_INSTANCE_ADMIN_STATUS_CHOICES,
            "hand_cooldown_choices": DEC_HAND_COOLDOWN_CHOICES,
            "default_hand_cooldown_seconds": DEC_DEFAULT_HAND_COOLDOWN_SECONDS,
        }


def _audit_authority_context(network_mode):
    authority = DecPokerAuditAuthority.objects.filter(
        network_mode=network_mode,
    ).select_related("authority_account").first()
    return {
        "audit_authority": authority,
        "audit_authority_accounts": get_user_model().objects.order_by("username"),
    }


def _attach_dec_valuation_context(instances):
    instance_list = list(instances)
    instance_ids = [instance.pk for instance in instance_list]
    pair_by_key = {
        pair.pair_key: pair
        for pair in TradingPair.objects.filter(
            network_mode=DEC_NETWORK_MODE,
            pair_key__in=[
                TradingPair.build_pair_key(instance.reward_asset_name, "EVR")
                for instance in instance_list
            ],
        )
    }
    latest_valuations = {}
    for valuation in DecPokerMarketValuation.objects.filter(
        game_instance_id__in=instance_ids,
    ).select_related("trading_pair").order_by("game_instance_id", "-created_at"):
        latest_valuations.setdefault(valuation.game_instance_id, valuation)
    latest_bids = {}
    for bid in DecPokerValuationBid.objects.filter(
        game_instance_id__in=instance_ids,
    ).select_related("limit_order", "trading_pair").order_by("game_instance_id", "-created_at"):
        latest_bids.setdefault(bid.game_instance_id, bid)

    for instance in instance_list:
        instance.valuation_pair = pair_by_key.get(
            TradingPair.build_pair_key(instance.reward_asset_name, "EVR")
        )
        instance.latest_market_valuation = latest_valuations.get(instance.pk)
        instance.latest_valuation_bid = latest_bids.get(instance.pk)
    return instance_list


def _payout_policy_display(policy):
    payout_table = policy.payout_table or {}
    payout = payout_table.get("payout") or {}
    expected_return = payout_table.get("expected_return") or {}
    outcomes = []
    for outcome in payout_table.get("outcomes") or []:
        numerator = Decimal(str(outcome.get("probability_numerator") or 0))
        denominator = Decimal(str(outcome.get("probability_denominator") or 0))
        probability_percent = None
        if denominator > 0:
            probability_percent = f"{(numerator / denominator * Decimal('100')):.6f}"
        outcomes.append({
            **outcome,
            "probability_percent": probability_percent,
        })
    return {
        "policy": policy,
        "outcomes": outcomes,
        "payout": payout,
        "expected_return": expected_return,
        "rtp": payout_table.get("rtp") or {},
    }


def _dec_poker_idempotency_key(request, instance):
    session_key = f"dec_poker_hand_idempotency_{instance.pk}"
    idempotency_key = str(request.session.get(session_key) or "").strip()
    if not idempotency_key:
        idempotency_key = secrets.token_urlsafe(24)
        request.session[session_key] = idempotency_key
    return session_key, idempotency_key


@login_required
def dec_poker_lobby(request):
    network_mode = DEC_NETWORK_MODE

    instances = DecPokerGameInstance.objects.filter(
        network_mode=network_mode,
        is_active=True,
    ).select_related("vault_profile__address", "channel_policy").order_by("-created_at")
    recent_hands = DecPokerHand.objects.filter(
        game_instance__network_mode=network_mode,
    ).select_related("game_instance", "player").order_by("-created_at")[:20]

    context = {
        "network_mode": network_mode,
        "instances": instances,
        "live_instance_count": instances.count(),
        "recent_hands": recent_hands,
        "show_admin_link": _can_manage_dec_admin(request.user),
    }
    return render(request, "listings/dec_poker_lobby.html", context)


@login_required
def dec_poker_admin(request):
    if not _can_manage_dec_admin(request.user):
        messages.error(request, "Admin privileges are required to manage DEC games.")
        return redirect("dec_poker_lobby")

    network_mode = DEC_NETWORK_MODE
    preview = None

    if request.method == "POST":
        action = str(request.POST.get("action") or "").strip().lower()

        if action == "configure_audit_authority":
            if not _can_configure_dec_audit_authority(request.user):
                messages.error(request, "Superuser privileges are required to configure DEC audit authority.")
                return redirect("dec_poker_admin")
            try:
                account = get_user_model().objects.get(
                    username=str(request.POST.get("authority_account") or "").strip(),
                )
                minimum_balance = Decimal(
                    str(request.POST.get("minimum_restricted_asset_balance") or "1").strip()
                )
                if minimum_balance <= 0:
                    raise ValueError("Minimum restricted asset balance must be greater than zero.")
                DecPokerAuditAuthority.objects.update_or_create(
                    network_mode=network_mode,
                    defaults={
                        "authority_account": account,
                        "authority_address": str(request.POST.get("authority_address") or "").strip(),
                        "restricted_asset_name": str(
                            request.POST.get("restricted_asset_name") or ""
                        ).strip().upper(),
                        "required_qualifier_name": str(
                            request.POST.get("required_qualifier_name") or ""
                        ).strip().upper(),
                        "required_verifier_string": str(
                            request.POST.get("required_verifier_string") or ""
                        ).strip(),
                        "minimum_restricted_asset_balance": minimum_balance,
                        "enforce_settlement_writes": request.POST.get("enforce_settlement_writes") == "on",
                        "status": DecPokerAuditAuthority.STATUS_DRAFT,
                        "last_verified_at": None,
                        "last_verification_evidence": {},
                        "last_verification_error": "",
                    },
                )
                messages.info(request, "DEC audit authority saved as draft. Verify it on public testnet before activation.")
            except Exception as exc:
                messages.error(request, f"Unable to configure DEC audit authority: {exc}")
            return redirect("dec_poker_admin")

        if action == "verify_audit_authority":
            if not _can_configure_dec_audit_authority(request.user):
                messages.error(request, "Superuser privileges are required to activate DEC audit authority.")
                return redirect("dec_poker_admin")
            authority = DecPokerAuditAuthority.objects.filter(network_mode=network_mode).first()
            if authority is None:
                messages.error(request, "Configure a DEC audit authority before verification.")
                return redirect("dec_poker_admin")
            try:
                verify_dec_poker_audit_authority(authority)
                authority.status = DecPokerAuditAuthority.STATUS_ACTIVE
                authority.save(update_fields=["status", "updated_at"])
                messages.success(request, "DEC audit authority verified and activated.")
            except Exception as exc:
                messages.error(request, f"DEC audit authority remains inactive: {exc}")
            return redirect("dec_poker_admin")

        if action == "suspend_audit_authority":
            if not _can_configure_dec_audit_authority(request.user):
                messages.error(request, "Superuser privileges are required to suspend DEC audit authority.")
                return redirect("dec_poker_admin")
            authority = DecPokerAuditAuthority.objects.filter(network_mode=network_mode).first()
            if authority is not None:
                authority.status = DecPokerAuditAuthority.STATUS_SUSPENDED
                authority.save(update_fields=["status", "updated_at"])
                messages.info(request, "DEC audit authority was suspended.")
            return redirect("dec_poker_admin")

        if action == "create_valuation_bid":
            instance = get_object_or_404(
                DecPokerGameInstance,
                pk=request.POST.get("instance_id"),
                network_mode=network_mode,
            )
            try:
                bid = create_dec_poker_valuation_bid(
                    request.user,
                    instance,
                    price_evr_per_reward_asset=request.POST.get("price_evr_per_reward_asset"),
                    reward_asset_quantity=request.POST.get("reward_asset_quantity"),
                )
                messages.success(
                    request,
                    (
                        f"Posted post-only valuation bid {bid.limit_order_id}: "
                        f"{bid.reward_asset_quantity} {instance.reward_asset_name} at "
                        f"{bid.price_evr_per_reward_asset} EVR."
                    ),
                )
            except Exception as exc:
                messages.error(request, f"Unable to create DEC valuation bid: {exc}")
            return redirect("dec_poker_admin")

        if action == "publish_market_valuation":
            instance = get_object_or_404(
                DecPokerGameInstance,
                pk=request.POST.get("instance_id"),
                network_mode=network_mode,
            )
            try:
                valuation, policy = publish_dec_poker_market_valuation(request.user, instance)
                messages.success(
                    request,
                    (
                        f"Published payout policy v{policy.version} from valuation "
                        f"{valuation.valuation_hash[:12]} at {valuation.rtp_percent}% RTP."
                    ),
                )
            except Exception as exc:
                messages.error(request, f"Unable to publish DEC market valuation: {exc}")
            return redirect("dec_poker_admin")

        if action == "create_shared_channel":
            try:
                channel_result = ensure_shared_dec_channel(request.user, {
                    "network_mode": network_mode,
                    "channel_admin_asset": request.POST.get("channel_admin_asset"),
                    "channel_tag": request.POST.get("channel_tag"),
                    "channel_name": request.POST.get("channel_name"),
                })
                if channel_result["created"]:
                    if channel_result["chain_metadata_status"] == MessageChannelPolicy.CHAIN_METADATA_STATUS_PENDING:
                        messages.info(
                            request,
                            (
                                f"Submitted shared DEC channel {channel_result['channel_name']}; "
                                "the unified v5 channel is awaiting public-testnet metadata verification."
                            ),
                        )
                    else:
                        messages.success(
                            request,
                            (
                                f"Created shared DEC channel {channel_result['channel_name']} "
                                f"with metadata status {channel_result['chain_metadata_status']}."
                            ),
                        )
                else:
                    messages.info(
                        request,
                        (
                            f"Shared DEC channel {channel_result['channel_name']} already has a recorded issuance; "
                            f"metadata status is {channel_result['chain_metadata_status']}."
                        ),
                    )
            except Exception as exc:
                messages.error(request, f"Unable to create shared DEC channel: {exc}")
            return redirect("dec_poker_admin")

        if action == "preview_instance":
            try:
                preview = preview_dec_poker_instance_plan(request.user, {
                    "network_mode": network_mode,
                    "title": request.POST.get("title"),
                    "reward_asset_name": request.POST.get("reward_asset_name"),
                    "reward_supply": request.POST.get("reward_supply"),
                    "reward_per_win": request.POST.get("reward_per_win"),
                    "entry_fee_evr": request.POST.get("entry_fee_evr"),
                    "reward_asset_units": request.POST.get("reward_asset_units"),
                    "hand_cooldown_seconds": request.POST.get("hand_cooldown_seconds"),
                })
            except Exception as exc:
                messages.error(request, f"Unable to preview DEC instance: {exc}")

        if action == "create_instance":
            try:
                channel_result = ensure_shared_dec_channel(request.user, {
                    "network_mode": network_mode,
                    "channel_admin_asset": request.POST.get("channel_admin_asset"),
                    "channel_tag": request.POST.get("channel_tag"),
                    "channel_name": request.POST.get("channel_name"),
                })
                if channel_result["chain_metadata_status"] != MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED:
                    messages.info(
                        request,
                        "The unified v5 channel is awaiting public-testnet metadata verification before instancing can begin.",
                    )
                    return redirect("dec_poker_admin")
                instance = create_dec_poker_instance(request.user, {
                    "network_mode": network_mode,
                    "title": request.POST.get("title"),
                    "reward_asset_name": request.POST.get("reward_asset_name"),
                    "reward_supply": request.POST.get("reward_supply"),
                    "reward_per_win": request.POST.get("reward_per_win"),
                    "entry_fee_evr": request.POST.get("entry_fee_evr"),
                    "reward_asset_units": request.POST.get("reward_asset_units"),
                    "hand_cooldown_seconds": request.POST.get("hand_cooldown_seconds"),
                })
                messages.info(
                    request,
                    (
                        f"Created DEC instance \"{instance.title}\" and issued its reward main asset. "
                        "Resume provisioning after public testnet confirms each stage."
                    ),
                )
                if channel_result["created"]:
                    messages.info(
                        request,
                        (
                            f"Shared channel {channel_result['channel_name']} was created with "
                            f"metadata status {channel_result['chain_metadata_status']}."
                        ),
                    )
                return redirect("dec_poker_admin")
            except Exception as exc:
                messages.error(request, f"Unable to create DEC instance: {exc}")

        if action == "resume_instance":
            instance = get_object_or_404(
                DecPokerGameInstance,
                pk=request.POST.get("instance_id"),
                network_mode=DEC_NETWORK_MODE,
            )
            try:
                instance = resume_dec_poker_instance(instance)
                if instance.is_active:
                    messages.success(request, f"Activated DEC instance {instance.title}.")
                else:
                    messages.info(request, f"DEC instance {instance.title} remains pending: {instance.profile_tag_error}")
            except Exception as exc:
                messages.error(request, f"Unable to resume DEC instance: {exc}")
            return redirect("dec_poker_admin")

        if action == "update_instance":
            instance = get_object_or_404(
                DecPokerGameInstance,
                pk=request.POST.get("instance_id"),
                network_mode=DEC_NETWORK_MODE,
            )
            try:
                update_dec_instance_admin(
                    instance,
                    treasury_bps=request.POST.get("wager_treasury_bps"),
                    hand_cooldown_seconds=request.POST.get("hand_cooldown_seconds"),
                    status=request.POST.get("status") or None,
                )
                messages.success(request, f"Updated DEC instance {instance.title}.")
            except Exception as exc:
                messages.error(request, f"Unable to update DEC instance: {exc}")
            return redirect("dec_poker_admin")

    instances = DecPokerGameInstance.objects.filter(
        network_mode=network_mode,
    ).select_related("vault_profile__address", "channel_policy").order_by("-created_at")
    instances = _attach_dec_valuation_context(instances)

    channel_health = _shared_dec_channel_health(network_mode)
    return render(request, "listings/dec_poker_admin.html", {
        "network_mode": network_mode,
        "instances": instances,
        "channel_health": channel_health,
        "preview": preview,
        "can_configure_audit_authority": _can_configure_dec_audit_authority(request.user),
        **_audit_authority_context(network_mode),
        **_dec_creation_choices(channel_health, request.user),
    })


@login_required
def dec_poker_instance(request, instance_id):
    instance = get_object_or_404(
        DecPokerGameInstance.objects.select_related("vault_profile__address"),
        pk=instance_id,
        network_mode=DEC_NETWORK_MODE,
    )
    payout_display = _payout_policy_display(ensure_dec_poker_payout_policy(instance))
    idempotency_session_key, hand_idempotency_key = _dec_poker_idempotency_key(request, instance)

    if request.method == "POST":
        action = str(request.POST.get("action") or "").strip().lower()
        if action == "play_hand":
            wager_raw = str(request.POST.get("wager_evr") or "").strip()
            client_seed = str(request.POST.get("client_seed") or "").strip()
            wager_evr = None
            if wager_raw:
                try:
                    wager_evr = Decimal(wager_raw)
                except (InvalidOperation, TypeError, ValueError):
                    messages.error(request, "Wager must be a valid EVR amount.")
                    return redirect("dec_poker_instance", instance_id=instance.id)
            try:
                hand = play_dec_poker_hand(
                    request.user,
                    instance,
                    wager_evr=wager_evr,
                    client_seed=client_seed,
                    idempotency_key=request.POST.get("idempotency_key") or hand_idempotency_key,
                )
                request.session[idempotency_session_key] = secrets.token_urlsafe(24)
                if hand.result == DecPokerHand.RESULT_WIN:
                    messages.success(
                        request,
                        f"You won {hand.reward_amount} {instance.reward_asset_name}.",
                    )
                else:
                    messages.warning(request, "House wins this hand.")
                if hand.spend_message_status == DecPokerHand.MESSAGE_STATUS_FAILED or (
                    hand.reward_amount > 0
                    and hand.reward_message_status == DecPokerHand.MESSAGE_STATUS_FAILED
                ):
                    messages.warning(request, "Hand settled, but a channel event is pending reconciliation.")
            except Exception as exc:
                messages.error(request, f"Play failed: {exc}")
            return redirect("dec_poker_instance", instance_id=instance.id)

    hands = DecPokerHand.objects.filter(
        game_instance=instance,
    ).select_related("player", "payout_policy").order_by("-created_at")[:25]
    hand_cooldown_remaining_seconds = dec_poker_hand_cooldown_remaining_seconds(instance)

    context = {
        "instance": instance,
        "hands": hands,
        "hand_cooldown_remaining_seconds": hand_cooldown_remaining_seconds,
        "house_rule_label": dec_poker_house_rule_label(instance.active_house_rule),
        "treasury_split_percent": f"{(instance.wager_treasury_bps / 100):.2f}",
        "vault_split_percent": f"{((10000 - instance.wager_treasury_bps) / 100):.2f}",
        "payout_policy": payout_display["policy"],
        "payout_outcomes": payout_display["outcomes"],
        "payout_expected_return": payout_display["expected_return"],
        "payout_rtp": payout_display["rtp"],
        "hand_idempotency_key": hand_idempotency_key,
    }
    return render(request, "listings/dec_poker_instance.html", context)


@login_required
def dec_poker_hand_verify(request, hand_id):
    hand = get_object_or_404(
        DecPokerHand.objects.select_related("game_instance", "player", "payout_policy"),
        pk=hand_id,
        game_instance__network_mode=DEC_NETWORK_MODE,
    )
    verification = verify_dec_poker_hand(hand)
    ledger_verification = verify_dec_poker_payout_ledger(hand)
    ledger_entries = hand.payout_ledger_entries.select_related("payout_policy").order_by("sequence")
    if request.GET.get("format") == "json":
        return JsonResponse({
            "success": True,
            "hand_id": hand.id,
            "verification": verification,
            "ledger_verification": ledger_verification,
        })
    return render(request, "listings/dec_poker_verify.html", {
        "hand": hand,
        "verification": verification,
        "ledger_verification": ledger_verification,
        "ledger_entries": ledger_entries,
    })
