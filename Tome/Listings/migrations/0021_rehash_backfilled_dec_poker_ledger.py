import hashlib
import json
from decimal import Decimal, InvalidOperation, localcontext

from django.db import migrations


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


def _entry_payload(entry, previous_entry_hash):
    return {
        "game_instance_id": int(entry.game_instance_id),
        "hand_id": int(entry.hand_id),
        "payout_policy_id": int(entry.payout_policy_id),
        "sequence": int(entry.sequence),
        "event_type": str(entry.event_type),
        "correlation_id": str(entry.correlation_id),
        "idempotency_key": str(entry.idempotency_key),
        "player_identifier": str(entry.player_identifier),
        "currency": str(entry.currency),
        "stake_amount": _decimal_string(entry.stake_amount),
        "payout_amount": _decimal_string(entry.payout_amount),
        "balance_delta": _decimal_string(entry.balance_delta),
        "result": str(entry.result),
        "payout_policy_version": int(entry.payout_policy_version),
        "payout_policy_hash": str(entry.payout_policy_hash),
        "odds_snapshot": entry.odds_snapshot or {},
        "rng_evidence": entry.rng_evidence or {},
        "external_txid": str(entry.external_txid),
        "event_data": entry.event_data or {},
        "occurred_at": entry.occurred_at.isoformat(),
        "previous_entry_hash": str(previous_entry_hash),
    }


def rehash_backfilled_ledger_entries(apps, schema_editor):
    LedgerEntry = apps.get_model("Listings", "DecPokerPayoutLedgerEntry")
    game_instance_ids = LedgerEntry.objects.order_by().values_list(
        "game_instance_id", flat=True
    ).distinct()

    for game_instance_id in game_instance_ids:
        entries = list(
            LedgerEntry.objects.filter(game_instance_id=game_instance_id).order_by("sequence")
        )
        if not entries or any(
            not isinstance(entry.event_data, dict) or not entry.event_data.get("backfilled")
            for entry in entries
        ):
            continue

        previous_entry_hash = ""
        for entry in entries:
            payload = _entry_payload(entry, previous_entry_hash)
            entry_hash = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
            LedgerEntry.objects.filter(pk=entry.pk).update(
                previous_entry_hash=previous_entry_hash,
                entry_hash=entry_hash,
            )
            previous_entry_hash = entry_hash


class Migration(migrations.Migration):

    dependencies = [
        ("Listings", "0020_decpoker_payout_policy_and_ledger"),
    ]

    operations = [
        migrations.RunPython(rehash_backfilled_ledger_entries, migrations.RunPython.noop),
    ]