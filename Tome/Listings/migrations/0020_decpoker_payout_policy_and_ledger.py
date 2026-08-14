import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation, localcontext

import django.db.models.deletion
from django.db import migrations, models


CURRENT_HOUSE_RULE = "dealer_best_two_of_three_wins_ties"
LEGACY_HOUSE_RULE = "dealer_wins_ties"
CURRENT_WIN_NUMERATOR = 7101
CURRENT_WIN_DENOMINATOR = 20825
CURRENT_LOSS_NUMERATOR = 13724
LEGACY_WIN_NUMERATOR = 2068
LEGACY_WIN_DENOMINATOR = 4165
LEGACY_LOSS_NUMERATOR = 2097
RTP_DISCLOSURE = (
    "Wagers settle in EVR and wins settle in a separate DEC reward asset. "
    "A percentage RTP is not stated without a versioned, independently auditable "
    "EVR-to-reward-asset valuation snapshot."
)


def _to_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _decimal_string(value):
    with localcontext() as context:
        context.prec = 50
        normalized_value = _to_decimal(value).quantize(Decimal("0.00000001"))
    return format(normalized_value, "f")


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _normalize_house_rule(house_rule):
    if str(house_rule or "").strip().lower() == LEGACY_HOUSE_RULE:
        return LEGACY_HOUSE_RULE
    return CURRENT_HOUSE_RULE


def _odds_for_house_rule(house_rule):
    if _normalize_house_rule(house_rule) == LEGACY_HOUSE_RULE:
        return (
            LEGACY_WIN_NUMERATOR,
            LEGACY_WIN_DENOMINATOR,
            LEGACY_LOSS_NUMERATOR,
        )
    return (
        CURRENT_WIN_NUMERATOR,
        CURRENT_WIN_DENOMINATOR,
        CURRENT_LOSS_NUMERATOR,
    )


def _payout_table(version, house_rule, payout_currency, reward_per_win, minimum_wager):
    normalized_house_rule = _normalize_house_rule(house_rule)
    win_numerator, win_denominator, loss_numerator = _odds_for_house_rule(
        normalized_house_rule
    )
    with localcontext() as context:
        context.prec = 50
        expected_reward = (
            _to_decimal(reward_per_win)
            * Decimal(win_numerator)
            / Decimal(win_denominator)
        ).quantize(Decimal("0.00000001"))
    return {
        "schema_version": 1,
        "policy_version": int(version),
        "game_rule_version": "dec_poker_payout_v1",
        "house_rule": normalized_house_rule,
        "wager": {
            "currency": "EVR",
            "minimum_amount": _decimal_string(minimum_wager),
        },
        "payout": {
            "currency": str(payout_currency),
            "win_amount": _decimal_string(reward_per_win),
            "cap_amount": _decimal_string(reward_per_win),
        },
        "outcomes": [
            {
                "result": "win",
                "probability_numerator": win_numerator,
                "probability_denominator": win_denominator,
                "payout_amount": _decimal_string(reward_per_win),
            },
            {
                "result": "lose",
                "probability_numerator": loss_numerator,
                "probability_denominator": win_denominator,
                "payout_amount": "0",
            },
        ],
        "expected_return": {
            "currency": str(payout_currency),
            "amount_per_wager": _decimal_string(expected_reward),
        },
        "rtp": {
            "status": "valuation_required",
            "percent": None,
            "disclosure": RTP_DISCLOSURE,
        },
    }


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


def backfill_dec_poker_payout_audit_data(apps, schema_editor):
    Game = apps.get_model("Listings", "DecPokerGameInstance")
    Hand = apps.get_model("Listings", "DecPokerHand")
    Policy = apps.get_model("Listings", "DecPokerPayoutPolicy")
    LedgerEntry = apps.get_model("Listings", "DecPokerPayoutLedgerEntry")

    for instance in Game.objects.order_by("pk").iterator():
        hands = list(Hand.objects.filter(game_instance_id=instance.pk).order_by("created_at", "pk"))
        current_terms = (
            _normalize_house_rule(instance.active_house_rule),
            str(instance.reward_asset_name),
            _decimal_string(instance.reward_per_win),
            _decimal_string(instance.entry_fee_evr),
        )
        historical_terms = set()
        hand_terms = {}
        for hand in hands:
            outcome_detail = hand.outcome_detail if isinstance(hand.outcome_detail, dict) else {}
            reward_per_win = hand.reward_amount if _to_decimal(hand.reward_amount) > 0 else instance.reward_per_win
            terms = (
                _normalize_house_rule(outcome_detail.get("house_rule")),
                str(hand.reward_asset_name or instance.reward_asset_name),
                _decimal_string(reward_per_win),
                _decimal_string(instance.entry_fee_evr),
            )
            historical_terms.add(terms)
            hand_terms[hand.pk] = terms

        ordered_terms = sorted(term for term in historical_terms if term != current_terms)
        ordered_terms.append(current_terms)
        policies_by_terms = {}
        for version, terms in enumerate(ordered_terms, start=1):
            house_rule, payout_currency, reward_per_win, minimum_wager = terms
            payout_table = _payout_table(
                version,
                house_rule,
                payout_currency,
                reward_per_win,
                minimum_wager,
            )
            policy_hash = _sha256(_canonical_json(payout_table))
            policy = Policy.objects.create(
                game_instance_id=instance.pk,
                version=version,
                game_rule_version="dec_poker_payout_v1",
                house_rule=house_rule,
                wager_currency="EVR",
                payout_currency=payout_currency,
                minimum_wager_evr=_to_decimal(minimum_wager),
                reward_per_win=_to_decimal(reward_per_win),
                payout_cap_amount=_to_decimal(reward_per_win),
                win_probability_numerator=payout_table["outcomes"][0]["probability_numerator"],
                win_probability_denominator=payout_table["outcomes"][0]["probability_denominator"],
                expected_reward_per_wager=_to_decimal(
                    payout_table["expected_return"]["amount_per_wager"]
                ),
                rtp_status="valuation_required",
                rtp_percent=None,
                rtp_disclosure=RTP_DISCLOSURE,
                payout_table=payout_table,
                policy_hash=policy_hash,
            )
            policies_by_terms[terms] = policy

        active_policy = policies_by_terms[current_terms]
        instance.active_payout_policy_id = active_policy.pk
        instance.save(update_fields=["active_payout_policy"])

        sequence = 0
        previous_entry_hash = ""
        for hand in hands:
            policy = policies_by_terms[hand_terms[hand.pk]]
            snapshot = {
                "policy_version": int(policy.version),
                "policy_hash": str(policy.policy_hash),
                "payout_table": policy.payout_table,
            }
            hand.settlement_id = uuid.uuid4()
            hand.idempotency_key = f"legacy-{hand.pk}"
            hand.payout_policy_id = policy.pk
            hand.payout_policy_version = policy.version
            hand.payout_policy_hash = policy.policy_hash
            hand.payout_policy_snapshot = snapshot
            hand.settlement_status = "settled"
            hand.settlement_error = ""
            hand.settled_at = hand.created_at
            hand.save(update_fields=[
                "settlement_id",
                "idempotency_key",
                "payout_policy",
                "payout_policy_version",
                "payout_policy_hash",
                "payout_policy_snapshot",
                "settlement_status",
                "settlement_error",
                "settled_at",
            ])

            rng_evidence = {
                "server_seed_hash": str(hand.server_seed_hash),
                "client_seed": str(hand.client_seed),
                "fairness_nonce": int(hand.fairness_nonce),
                "fairness_digest": str(hand.fairness_digest),
            }
            events = [
                ("wager_accepted", "EVR", hand.wager_evr, Decimal("0"), Decimal("0"), ""),
                ("wager_spend_settled", "EVR", hand.wager_evr, Decimal("0"), -hand.wager_evr, hand.spend_txid),
                (
                    "hand_resolved",
                    policy.payout_currency,
                    hand.wager_evr,
                    hand.reward_amount,
                    Decimal("0"),
                    "",
                ),
            ]
            if _to_decimal(hand.reward_amount) > 0:
                events.append((
                    "reward_payout_settled",
                    policy.payout_currency,
                    Decimal("0"),
                    hand.reward_amount,
                    hand.reward_amount,
                    hand.reward_txid,
                ))

            for event_type, currency, stake_amount, payout_amount, balance_delta, external_txid in events:
                sequence += 1
                event_data = {"backfilled": True}
                payload = _ledger_hash_payload(
                    game_instance_id=instance.pk,
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
                    odds_snapshot=policy.payout_table,
                    rng_evidence=rng_evidence,
                    external_txid=external_txid,
                    event_data=event_data,
                    occurred_at=hand.created_at,
                    previous_entry_hash=previous_entry_hash,
                )
                entry_hash = _sha256(_canonical_json(payload))
                LedgerEntry.objects.create(
                    game_instance_id=instance.pk,
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
                    odds_snapshot=policy.payout_table,
                    rng_evidence=rng_evidence,
                    external_txid=external_txid,
                    event_data=event_data,
                    occurred_at=hand.created_at,
                    previous_entry_hash=previous_entry_hash,
                    entry_hash=entry_hash,
                )
                previous_entry_hash = entry_hash


class Migration(migrations.Migration):

    dependencies = [
        ("Listings", "0019_decpokergameinstance_active_house_rule"),
    ]

    operations = [
        migrations.CreateModel(
            name="DecPokerPayoutPolicy",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("version", models.PositiveIntegerField()),
                ("game_rule_version", models.CharField(max_length=64)),
                ("house_rule", models.CharField(max_length=64)),
                ("wager_currency", models.CharField(default="EVR", max_length=30)),
                ("payout_currency", models.CharField(max_length=30)),
                ("minimum_wager_evr", models.DecimalField(decimal_places=8, max_digits=20)),
                ("reward_per_win", models.DecimalField(decimal_places=8, max_digits=30)),
                ("payout_cap_amount", models.DecimalField(decimal_places=8, max_digits=30)),
                ("win_probability_numerator", models.PositiveIntegerField()),
                ("win_probability_denominator", models.PositiveIntegerField()),
                ("expected_reward_per_wager", models.DecimalField(decimal_places=8, max_digits=30)),
                ("rtp_status", models.CharField(choices=[("valuation_required", "External valuation required"), ("disclosed", "Disclosed percentage")], default="valuation_required", max_length=32)),
                ("rtp_percent", models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True)),
                ("rtp_disclosure", models.TextField()),
                ("payout_table", models.JSONField(default=dict)),
                ("policy_hash", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("game_instance", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payout_policies", to="Listings.decpokergameinstance")),
            ],
            options={"ordering": ("game_instance_id", "-version")},
        ),
        migrations.AddField(
            model_name="decpokergameinstance",
            name="active_payout_policy",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="active_for_instances", to="Listings.decpokerpayoutpolicy"),
        ),
        migrations.AddField(
            model_name="decpokerhand",
            name="idempotency_key",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="decpokerhand",
            name="payout_policy",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="hands", to="Listings.decpokerpayoutpolicy"),
        ),
        migrations.AddField(
            model_name="decpokerhand",
            name="payout_policy_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="decpokerhand",
            name="payout_policy_snapshot",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="decpokerhand",
            name="payout_policy_version",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="decpokerhand",
            name="settled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="decpokerhand",
            name="settlement_error",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="decpokerhand",
            name="settlement_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="decpokerhand",
            name="settlement_status",
            field=models.CharField(choices=[("accepted", "Accepted"), ("settling", "Settling"), ("settled", "Settled"), ("reconciliation_required", "Reconciliation required"), ("failed", "Failed")], default="settled", max_length=32),
        ),
        migrations.CreateModel(
            name="DecPokerPayoutLedgerEntry",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sequence", models.PositiveIntegerField()),
                ("event_type", models.CharField(choices=[("wager_accepted", "Wager accepted"), ("wager_spend_settled", "Wager spend settled"), ("hand_resolved", "Hand resolved"), ("reward_payout_settled", "Reward payout settled"), ("reconciliation_required", "Reconciliation required")], max_length=40)),
                ("correlation_id", models.UUIDField()),
                ("idempotency_key", models.CharField(max_length=64)),
                ("player_identifier", models.CharField(max_length=64)),
                ("currency", models.CharField(max_length=30)),
                ("stake_amount", models.DecimalField(decimal_places=8, default=0, max_digits=30)),
                ("payout_amount", models.DecimalField(decimal_places=8, default=0, max_digits=30)),
                ("balance_delta", models.DecimalField(decimal_places=8, default=0, max_digits=30)),
                ("result", models.CharField(blank=True, default="", max_length=10)),
                ("payout_policy_version", models.PositiveIntegerField()),
                ("payout_policy_hash", models.CharField(max_length=64)),
                ("odds_snapshot", models.JSONField(default=dict)),
                ("rng_evidence", models.JSONField(default=dict)),
                ("external_txid", models.CharField(blank=True, default="", max_length=100)),
                ("event_data", models.JSONField(default=dict)),
                ("occurred_at", models.DateTimeField()),
                ("previous_entry_hash", models.CharField(blank=True, default="", max_length=64)),
                ("entry_hash", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("game_instance", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payout_ledger_entries", to="Listings.decpokergameinstance")),
                ("hand", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payout_ledger_entries", to="Listings.decpokerhand")),
                ("payout_policy", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ledger_entries", to="Listings.decpokerpayoutpolicy")),
            ],
            options={"ordering": ("game_instance_id", "sequence")},
        ),
        migrations.RunPython(backfill_dec_poker_payout_audit_data, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="decpokerhand",
            name="settlement_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddConstraint(
            model_name="decpokerhand",
            constraint=models.UniqueConstraint(condition=~models.Q(("idempotency_key", "")), fields=("game_instance", "player", "idempotency_key"), name="dec_poker_hand_idempotency_key_unique"),
        ),
        migrations.AddConstraint(
            model_name="decpokerpayoutpolicy",
            constraint=models.UniqueConstraint(fields=("game_instance", "version"), name="dec_poker_payout_policy_version_unique"),
        ),
        migrations.AddConstraint(
            model_name="decpokerpayoutledgerentry",
            constraint=models.UniqueConstraint(fields=("game_instance", "sequence"), name="dec_poker_ledger_sequence_unique"),
        ),
        migrations.AddConstraint(
            model_name="decpokerpayoutledgerentry",
            constraint=models.UniqueConstraint(fields=("hand", "event_type"), name="dec_poker_ledger_hand_event_unique"),
        ),
    ]