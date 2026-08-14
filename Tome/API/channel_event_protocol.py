"""Canonical validation helpers for replayable message-channel events."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime


CANONICAL_EVENT_VERSION = 1
REQUIRED_EVENT_KEYS = {
    "aggregate_id",
    "aggregate_sequence",
    "aggregate_type",
    "created_at",
    "details",
    "event_id",
    "event_type",
    "event_version",
    "network_mode",
    "payload_checksum",
    "stage",
}
OPTIONAL_EVENT_KEYS = {
    "causation_id",
    "correlation_id",
    "producer_address",
    "producer_signature",
    "transaction_id",
}
CHECKSUM_EXCLUDED_KEYS = {"payload_checksum", "producer_signature"}


def canonical_event_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_for_checksum(payload):
    if not isinstance(payload, dict):
        raise ValueError("Channel event payload must be a JSON object.")
    return {
        key: value
        for key, value in payload.items()
        if key not in CHECKSUM_EXCLUDED_KEYS
    }


def event_payload_checksum(payload):
    return hashlib.sha256(canonical_event_json(payload_for_checksum(payload)).encode("utf-8")).hexdigest()


def add_payload_checksum(payload):
    normalized = dict(payload)
    normalized["payload_checksum"] = event_payload_checksum(normalized)
    return normalized


def validate_channel_event_payload(payload, allowed_stages):
    if not isinstance(payload, dict):
        raise ValueError("Channel event payload must be a JSON object.")

    payload_keys = set(payload)
    missing = REQUIRED_EVENT_KEYS - payload_keys
    unexpected = payload_keys - REQUIRED_EVENT_KEYS - OPTIONAL_EVENT_KEYS
    if missing:
        raise ValueError(f"Channel event payload is missing required keys: {sorted(missing)}")
    if unexpected:
        raise ValueError(f"Channel event payload contains unsupported keys: {sorted(unexpected)}")

    if int(payload["event_version"]) != CANONICAL_EVENT_VERSION:
        raise ValueError(f"Channel event version must be {CANONICAL_EVENT_VERSION}.")
    try:
        uuid.UUID(str(payload["event_id"]))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Channel event id must be a UUID.") from exc

    for key in ("event_type", "aggregate_type", "aggregate_id", "stage"):
        if not str(payload.get(key) or "").strip():
            raise ValueError(f"Channel event {key} is required.")
    if int(payload["aggregate_sequence"]) < 1:
        raise ValueError("Channel event aggregate_sequence must be positive.")

    network_mode = str(payload["network_mode"] or "").strip().lower()
    if network_mode not in {"mainnet", "testnet"}:
        raise ValueError("Channel event network_mode must be mainnet or testnet.")
    payload["network_mode"] = network_mode

    allowed = {str(stage or "").strip().lower() for stage in (allowed_stages or [])}
    stage = str(payload["stage"] or "").strip().lower()
    if stage not in allowed:
        raise ValueError(f"Channel event stage {stage!r} is not allowed by this policy.")
    payload["stage"] = stage

    if not isinstance(payload["details"], dict):
        raise ValueError("Channel event details must be a JSON object.")
    try:
        datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Channel event created_at must be ISO-8601.") from exc

    expected_checksum = event_payload_checksum(payload)
    if str(payload.get("payload_checksum") or "") != expected_checksum:
        raise ValueError("Channel event payload checksum does not match canonical content.")
    return payload