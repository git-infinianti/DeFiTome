"""Ingest confirmed message-channel history from channel-asset lineage evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from API.channel_event_protocol import validate_channel_event_payload
from API.models import ChannelEvent, ChannelReconciliationIssue, MessageChannelPolicy
from API.rpc import evrmore_rpc
from Media.kubo_api import KuboAPIUploader
from Tome.rpc_client import using_network_mode
from Wallet.rpc import get_public_transaction_evidence


MAX_CHANNEL_LINEAGE_DEPTH = 10_000


class ChannelHistoryUnavailable(RuntimeError):
    """Raised when a selected endpoint cannot establish channel history."""


@dataclass(frozen=True)
class ChannelAssetObservation:
    channel_name: str
    payload_ipfs_cid: str
    channel_txid: str
    block_height: int
    block_transaction_index: int
    channel_output_index: int
    block_hash: str
    raw: dict


def _first_value(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value not in (None, ''):
            return value
    return None


def _positive_integer(value, field_name):
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'Channel history observation requires an integer {field_name}.') from exc
    if normalized < 0:
        raise ValueError(f'Channel history observation {field_name} cannot be negative.')
    return normalized


def _canonical_hash(value, field_name):
    normalized = str(value or '').strip().lower()
    if len(normalized) != 64 or any(character not in '0123456789abcdef' for character in normalized):
        raise ValueError(f'Channel asset lineage requires a canonical {field_name}.')
    return normalized


def _normalized_asset_name(value):
    return str(value or '').strip().upper()


def _channel_asset_output(transaction, output_index, expected_channel_name):
    outputs = transaction.get('vout') if isinstance(transaction, dict) else None
    if not isinstance(outputs, list):
        raise ValueError('Channel asset lineage transaction is missing outputs.')

    matching_outputs = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        if _positive_integer(output.get('n'), 'channel output index') == output_index:
            matching_outputs.append(output)
    if len(matching_outputs) != 1:
        raise ValueError('Channel asset lineage does not contain the expected output.')

    output = matching_outputs[0]
    script_pub_key = output.get('scriptPubKey')
    asset = script_pub_key.get('asset') if isinstance(script_pub_key, dict) else None
    if not isinstance(asset, dict):
        raise ValueError('Channel asset lineage output is not an Evrmore asset output.')
    if _normalized_asset_name(asset.get('name')) != expected_channel_name:
        raise ValueError('Channel asset lineage output does not carry the expected channel asset.')
    try:
        asset_amount = Decimal(str(asset.get('amount')))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('Channel asset lineage output has an invalid asset quantity.') from exc
    if asset_amount != Decimal('1'):
        raise ValueError('Channel asset lineage output must carry exactly one channel unit.')
    return output, asset


def _holder_entries(raw_holders):
    if isinstance(raw_holders, dict):
        if 'address' in raw_holders:
            return [raw_holders]
        for key in ('addresses', 'result', 'items'):
            candidate = raw_holders.get(key)
            if isinstance(candidate, (list, dict)):
                raw_holders = candidate
                break
        if isinstance(raw_holders, dict):
            return list(raw_holders.items())
    if isinstance(raw_holders, list):
        return raw_holders
    raise ValueError('Channel asset index returned an unsupported holder response.')


def _channel_holder(policy):
    holders = []
    for entry in _holder_entries(evrmore_rpc.list_addresses_by_asset(policy.channel_name)):
        if isinstance(entry, tuple) and len(entry) == 2:
            address, amount = entry
        elif isinstance(entry, dict):
            address = _first_value(entry, 'address', 'holder')
            amount = _first_value(entry, 'balance', 'amount', 'quantity')
        else:
            continue
        try:
            balance = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError('Channel asset index returned an invalid holder balance.') from exc
        if balance > 0:
            holders.append((str(address or '').strip(), balance))

    if len(holders) != 1:
        raise ValueError('A one-unit channel must have exactly one holder.')
    address, balance = holders[0]
    if not address or balance != Decimal('1'):
        raise ValueError('Channel asset holder evidence must contain exactly one channel unit.')
    return address


def _current_channel_outpoint(policy, holder_address):
    raw_utxos = evrmore_rpc.get_address_utxos([holder_address], asset_name=policy.channel_name)
    if isinstance(raw_utxos, dict):
        raw_utxos = _first_value(raw_utxos, 'utxos', 'result', 'items')
    if not isinstance(raw_utxos, list):
        raise ValueError('Channel asset index returned an unsupported UTXO response.')

    expected_channel_name = _normalized_asset_name(policy.channel_name)
    matching_utxos = [
        utxo
        for utxo in raw_utxos
        if isinstance(utxo, dict)
        and _normalized_asset_name(_first_value(utxo, 'assetName', 'asset_name', 'asset')) == expected_channel_name
    ]
    if len(matching_utxos) != 1:
        raise ValueError('Channel asset index must expose exactly one unspent channel output.')

    utxo = matching_utxos[0]
    return (
        _canonical_hash(utxo.get('txid'), 'channel transaction id'),
        _positive_integer(_first_value(utxo, 'outputIndex', 'vout', 'output_index'), 'channel output index'),
    )


def _load_transaction(transaction_id, transaction_cache):
    if transaction_id in transaction_cache:
        return transaction_cache[transaction_id]

    transaction = evrmore_rpc.get_raw_transaction(transaction_id, True)
    if not isinstance(transaction, dict):
        raise ValueError('Channel asset lineage returned invalid transaction evidence.')
    if _canonical_hash(transaction.get('txid'), 'transaction id') != transaction_id:
        raise ValueError('Channel asset lineage transaction evidence did not match the requested transaction id.')
    transaction_cache[transaction_id] = transaction
    return transaction


def _block_coordinate(transaction, transaction_id, block_cache):
    block_hash = _canonical_hash(transaction.get('blockhash'), 'block hash')
    if block_hash not in block_cache:
        block = evrmore_rpc.get_block(block_hash, 1)
        if not isinstance(block, dict):
            raise ValueError('Channel asset lineage returned invalid block evidence.')
        if _canonical_hash(block.get('hash'), 'block hash') != block_hash:
            raise ValueError('Channel asset lineage block evidence did not match the transaction block hash.')
        block_cache[block_hash] = block

    block = block_cache[block_hash]
    transaction_ids = block.get('tx')
    if not isinstance(transaction_ids, list):
        raise ValueError('Channel asset lineage block evidence is missing ordered transaction ids.')
    transaction_positions = [
        index
        for index, candidate_transaction_id in enumerate(transaction_ids)
        if str(candidate_transaction_id or '').strip().lower() == transaction_id
    ]
    if len(transaction_positions) != 1:
        raise ValueError('Channel asset lineage block does not contain the channel transaction exactly once.')
    return _positive_integer(block.get('height'), 'block height'), transaction_positions[0], block_hash


def _previous_channel_outpoint(transaction, expected_channel_name, transaction_cache):
    transaction_inputs = transaction.get('vin') if isinstance(transaction, dict) else None
    if not isinstance(transaction_inputs, list):
        raise ValueError('Channel asset lineage transaction is missing inputs.')

    matching_inputs = []
    for transaction_input in transaction_inputs:
        if not isinstance(transaction_input, dict) or transaction_input.get('coinbase'):
            continue
        previous_transaction_id = _canonical_hash(transaction_input.get('txid'), 'previous transaction id')
        previous_output_index = _positive_integer(transaction_input.get('vout'), 'previous channel output index')
        previous_transaction = _load_transaction(previous_transaction_id, transaction_cache)
        try:
            _channel_asset_output(previous_transaction, previous_output_index, expected_channel_name)
        except ValueError:
            continue
        matching_inputs.append((previous_transaction_id, previous_output_index))

    if len(matching_inputs) != 1:
        raise ValueError('Channel asset lineage transfer must spend exactly one preceding channel output.')
    return matching_inputs[0]


def fetch_channel_asset_lineage_observations(policy):
    """Reconstruct channel events from the current one-unit channel asset back to issuance."""
    expected_channel_name = _normalized_asset_name(policy.channel_name)
    transaction_cache = {}
    block_cache = {}
    observations = []

    try:
        with using_network_mode(policy.network_mode):
            holder_address = _channel_holder(policy)
            transaction_id, output_index = _current_channel_outpoint(policy, holder_address)
            seen_outpoints = set()

            for _hop in range(MAX_CHANNEL_LINEAGE_DEPTH):
                outpoint = (transaction_id, output_index)
                if outpoint in seen_outpoints:
                    raise ValueError('Channel asset lineage contains a cycle.')
                seen_outpoints.add(outpoint)

                transaction = _load_transaction(transaction_id, transaction_cache)
                output, asset = _channel_asset_output(transaction, output_index, expected_channel_name)
                script_pub_key = output.get('scriptPubKey') or {}
                if script_pub_key.get('type') == 'new_asset':
                    if policy.issuance_txid and transaction_id != _canonical_hash(
                        policy.issuance_txid,
                        'policy issuance transaction id',
                    ):
                        raise ValueError('Channel asset lineage does not terminate at the policy issuance transaction.')
                    break
                if script_pub_key.get('type') != 'transfer_asset':
                    raise ValueError('Channel asset lineage output is not an asset-transfer output.')

                payload_ipfs_cid = str(asset.get('message') or '').strip()
                if not payload_ipfs_cid:
                    raise ValueError('Channel asset transfer is missing its IPFS message CID.')
                block_height, transaction_index, block_hash = _block_coordinate(
                    transaction,
                    transaction_id,
                    block_cache,
                )
                observations.append(ChannelAssetObservation(
                    channel_name=expected_channel_name,
                    payload_ipfs_cid=payload_ipfs_cid,
                    channel_txid=transaction_id,
                    block_height=block_height,
                    block_transaction_index=transaction_index,
                    channel_output_index=output_index,
                    block_hash=block_hash,
                    raw={
                        'source': 'channel_asset_lineage',
                        'holder_address': holder_address,
                        'channel_name': expected_channel_name,
                        'channel_txid': transaction_id,
                        'channel_output': output,
                    },
                ))
                transaction_id, output_index = _previous_channel_outpoint(
                    transaction,
                    expected_channel_name,
                    transaction_cache,
                )
            else:
                raise ValueError('Channel asset lineage exceeded the configured maximum depth.')
    except ChannelHistoryUnavailable:
        raise
    except ValueError as exc:
        _record_issue(
            policy,
            'invalid_channel_observation',
            {'error': str(exc), 'source': 'channel_asset_lineage'},
            severity='critical',
        )
        raise ChannelHistoryUnavailable(
            f'Channel asset lineage is invalid for {policy.channel_name}: {exc}'
        ) from exc
    except Exception as exc:
        raise ChannelHistoryUnavailable(
            f'Channel asset lineage is unavailable for {policy.channel_name}: {exc}'
        ) from exc

    return sorted(
        observations,
        key=lambda observation: (
            observation.block_height,
            observation.block_transaction_index,
            observation.channel_output_index,
        ),
    )


def _record_issue(policy, code, detail, *, event=None, aggregate_type='', aggregate_id='', severity='error'):
    return ChannelReconciliationIssue.objects.create(
        policy=policy,
        event=event,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        code=code,
        severity=severity,
        detail=detail,
    )


def _confirmed_at_from_evidence(evidence):
    timestamp = evidence.get('block_time') or evidence.get('transaction_time')
    if timestamp is None:
        return timezone.now()
    try:
        return datetime.fromtimestamp(int(timestamp), tz=datetime_timezone.utc)
    except (TypeError, ValueError, OSError):
        return timezone.now()


def _observation_matches_event(event, observation, payload):
    return (
        event.payload_checksum == payload['payload_checksum']
        and event.payload_ipfs_cid == observation.payload_ipfs_cid
        and event.channel_txid == observation.channel_txid
        and event.channel_output_index == observation.channel_output_index
        and event.block_height == observation.block_height
        and event.block_transaction_index == observation.block_transaction_index
        and event.block_hash == observation.block_hash
    )


def ingest_channel_history(policy):
    """Persist validated, publicly confirmed channel-asset lineage observations for one policy."""
    if policy.status != 'active' or policy.chain_metadata_status != MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED:
        raise ValueError('Channel history ingestion requires an active policy with verified chain metadata.')

    report = {
        'channel_name': policy.channel_name,
        'network_mode': policy.network_mode,
        'observed': 0,
        'ingested': 0,
        'already_known': 0,
        'invalid': 0,
    }
    for observation in fetch_channel_asset_lineage_observations(policy):
        report['observed'] += 1
        payload = None
        try:
            payload = KuboAPIUploader().download_json(observation.payload_ipfs_cid)
            validate_channel_event_payload(payload, policy.allowed_stages)
            if payload['network_mode'] != policy.network_mode:
                raise ValueError('Channel event network does not match its policy.')

            evidence = get_public_transaction_evidence(
                observation.channel_txid,
                network_mode=policy.network_mode,
                minimum_confirmations=1,
            )
            evidence_block_hash = str(evidence.get('block_hash') or '').strip().lower()
            if evidence_block_hash and evidence_block_hash != observation.block_hash:
                raise ValueError('Public transaction evidence does not match the channel observation block hash.')

            existing = ChannelEvent.objects.filter(policy=policy, event_id=payload['event_id']).first()
            if existing is not None:
                if _observation_matches_event(existing, observation, payload):
                    report['already_known'] += 1
                    continue
                _record_issue(
                    policy,
                    'duplicate_event_id_conflict',
                    {
                        'event_id': payload['event_id'],
                        'observed_txid': observation.channel_txid,
                        'existing_txid': existing.channel_txid,
                    },
                    event=existing,
                    aggregate_type=payload['aggregate_type'],
                    aggregate_id=payload['aggregate_id'],
                    severity='critical',
                )
                report['invalid'] += 1
                continue

            ChannelEvent.objects.create(
                policy=policy,
                event_id=payload['event_id'],
                event_type=payload['event_type'],
                event_version=payload['event_version'],
                aggregate_type=payload['aggregate_type'],
                aggregate_id=payload['aggregate_id'],
                aggregate_sequence=payload['aggregate_sequence'],
                stage=payload['stage'],
                network_mode=payload['network_mode'],
                payload=payload,
                payload_checksum=payload['payload_checksum'],
                payload_ipfs_cid=observation.payload_ipfs_cid,
                channel_txid=observation.channel_txid,
                channel_output_index=observation.channel_output_index,
                block_height=observation.block_height,
                block_transaction_index=observation.block_transaction_index,
                block_hash=observation.block_hash,
                confirmed_at=_confirmed_at_from_evidence(evidence),
                raw_observation=observation.raw,
            )
            report['ingested'] += 1
        except Exception as exc:
            _record_issue(
                policy,
                'invalid_channel_observation',
                {
                    'error': str(exc),
                    'observation': observation.raw,
                },
                aggregate_type=payload.get('aggregate_type', '') if isinstance(payload, dict) else '',
                aggregate_id=payload.get('aggregate_id', '') if isinstance(payload, dict) else '',
                severity='critical',
            )
            report['invalid'] += 1

    return report