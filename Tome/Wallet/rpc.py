import hashlib
import struct
from collections import OrderedDict
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP

from base58 import b58decode, b58decode_check, b58encode_check
from django.conf import settings

from Tome.rpc_client import RPC, PublicRpcClient

SATOSHIS_PER_EVR = Decimal('100000000')
DEFAULT_FEE_EVR = Decimal('0.0001')  # 0.0001 EVR as a default fee fallback
DEFAULT_MIN_RELAY_FEE_EVR_PER_KB = Decimal('0.01')
NO_ESTIMATE_FEE_FLOOR_MULTIPLIER = Decimal('2')
DEFAULT_FEE_CONF_TARGET = 2  # Target faster confirmation when estimator data is available.
DEFAULT_FEE_ESTIMATE_MODE = 'CONSERVATIVE' # Default fee estimation mode for RPC calls
FEE_SAFETY_MULTIPLIER = Decimal('1.05')  # ~5% safety margin for fee estimation
DUST_THRESHOLD_SATS = 546
MAX_SEQUENCE = 0xFFFFFFFF
RBF_SEQUENCE = 0xFFFFFFFD
LOCKTIME_SEQUENCE = 0xFFFFFFFE


class InsufficientSpendableBalance(Exception):
    """Raised when confirmed, mempool-safe UTXOs cannot fund a raw transaction."""

BURN_ADDRESS_ISSUE_ASSET = 'EXissueAssetXXXXXXXXXXXXXXXXYiYRBD'
BURN_ADDRESS_ISSUE_SUBASSET = 'EXissueSubAssetXXXXXXXXXXXXXWW1ASo'
BURN_ADDRESS_ISSUE_UNIQUE = 'EXissueUniqueAssetXXXXXXXXXXTZjZJ5'
BURN_ADDRESS_REISSUE_ASSET = 'EXReissueAssetXXXXXXXXXXXXXXY1ANQH'
BURN_ADDRESS_ISSUE_RESTRICTED = 'EXissueRestrictedXXXXXXXXXXXZZMynb'
BURN_ADDRESS_ISSUE_QUALIFIER = 'EXissueQuaLifierXXXXXXXXXXXXW5Zxyf'
BURN_ADDRESS_ISSUE_SUBQUALIFIER = 'EXissueSubQuaLifierXXXXXXXXXUgTjtu'
BURN_ADDRESS_TAG = 'EXaddTagBurnXXXXXXXXXXXXXXXXb5HLXh'

BURN_ADDRESS_FALLBACKS = {
    'issue_asset': BURN_ADDRESS_ISSUE_ASSET,
    'issue_sub_asset': BURN_ADDRESS_ISSUE_SUBASSET,
    'issue_unique_asset': BURN_ADDRESS_ISSUE_UNIQUE,
    'reissue_or_remint_asset': BURN_ADDRESS_REISSUE_ASSET,
    'issue_restricted_asset': BURN_ADDRESS_ISSUE_RESTRICTED,
    'issue_qualifier_asset': BURN_ADDRESS_ISSUE_QUALIFIER,
    'issue_sub_qualifier_asset': BURN_ADDRESS_ISSUE_SUBQUALIFIER,
    'add_null_qualifier_tag': BURN_ADDRESS_TAG,
    # Mainnet fallback only; testnet should come from getburnaddresses.
    'issue_msg_channel_asset': BURN_ADDRESS_ISSUE_SUBASSET,
}

# Create inputs and outputs for a raw transaction
def itxo(txid, vout):
    return {"txid": txid, "vout": vout}

def utxo(address, amount):
    return {address: amount}


def _to_decimal_evr(amount):
    return Decimal(str(amount)).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)


def _to_satoshis(amount):
    return int(_to_decimal_evr(amount) * SATOSHIS_PER_EVR)


def _satoshis_to_evr(satoshis):
    return (Decimal(satoshis) / SATOSHIS_PER_EVR).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)


def _evr_output_value(amount):
    return format(_to_decimal_evr(amount), 'f')


def _p2pkh_script_pub_key(address):
    try:
        decoded = b58decode_check(str(address))
    except Exception as exc:
        raise Exception(f'Unable to encode output address {address}: {str(exc)}') from exc

    if len(decoded) != 21:
        raise Exception(f'Output address is not a supported P2PKH address: {address}')

    return b'\x76\xa9\x14' + decoded[1:] + b'\x88\xac'


def _p2pkh_hash160(address):
    try:
        decoded = b58decode_check(str(address))
    except Exception as exc:
        raise Exception(f'Unable to encode output address {address}: {str(exc)}') from exc

    if len(decoded) != 21:
        raise Exception(f'Output address is not a supported P2PKH address: {address}')

    return decoded[1:]


def _temporary_output_address(address, occurrence):
    decoded = b58decode_check(str(address))
    if len(decoded) != 21:
        raise Exception(f'Output address is not a supported P2PKH address: {address}')

    seed = f'{address}:{int(occurrence)}'.encode('ascii')
    payload = decoded[:1] + hashlib.sha256(seed).digest()[:20]
    return b58encode_check(payload).decode('ascii')


def _compact_size(value):
    number = int(value)
    if number < 0:
        raise ValueError('CompactSize value cannot be negative.')
    if number < 253:
        return bytes((number,))
    if number <= 0xFFFF:
        return b'\xfd' + struct.pack('<H', number)
    if number <= 0xFFFFFFFF:
        return b'\xfe' + struct.pack('<I', number)
    return b'\xff' + struct.pack('<Q', number)


def _push_script_data(payload):
    size = len(payload)
    if size < 76:
        return bytes((size,)) + payload
    if size <= 0xFF:
        return b'\x4c' + bytes((size,)) + payload
    if size <= 0xFFFF:
        return b'\x4d' + struct.pack('<H', size) + payload
    raise ValueError('Asset payload exceeds the supported script size.')


def _decode_asset_hash(encoded_hash):
    value = str(encoded_hash or '').strip()
    if len(value) == 46:
        decoded = b58decode(value)
        if len(decoded) != 34:
            raise ValueError('IPFS asset metadata must decode to 34 bytes.')
        return decoded[:1] + _compact_size(32) + decoded[2:]
    if len(value) == 64:
        try:
            decoded = bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError('Transaction metadata must be a 64-character hexadecimal value.') from exc
        return b'\x54' + _compact_size(32) + decoded
    raise ValueError('Asset metadata must be a 46-character IPFS CID or 64-character transaction id.')


def _new_asset_script(address, asset_data):
    if not isinstance(asset_data, dict):
        raise ValueError('New asset data is required.')

    asset_name = str(asset_data.get('asset_name') or '')
    encoded_name = asset_name.encode('ascii')
    asset_quantity = _to_satoshis(asset_data.get('asset_quantity'))
    units = int(asset_data.get('units') or 0)
    reissuable = int(bool(asset_data.get('reissuable')))
    has_ipfs = int(bool(asset_data.get('has_ipfs')))

    serialized_asset = (
        _compact_size(len(encoded_name))
        + encoded_name
        + struct.pack('<q', asset_quantity)
        + bytes((units, reissuable, has_ipfs))
    )
    if has_ipfs:
        serialized_asset += _decode_asset_hash(asset_data.get('ipfs_hash'))

    asset_message = b'evrq' + serialized_asset
    return _p2pkh_script_pub_key(address) + b'\xc0' + _push_script_data(asset_message) + b'\x75'


def _qualifier_asset_script(address, operation_payload):
    qualifier = operation_payload.get('issue_qualifier') if isinstance(operation_payload, dict) else None
    if not isinstance(qualifier, dict):
        raise ValueError('Qualifier operation payload is required.')
    return _new_asset_script(address, qualifier)


def _output_entries(outputs):
    if isinstance(outputs, dict):
        return list(outputs.items())

    if isinstance(outputs, (list, tuple)):
        entries = []
        for output in outputs:
            if not isinstance(output, dict) or len(output) != 1:
                raise Exception('Each raw transaction output must contain exactly one address.')
            entries.extend(output.items())
        return entries

    raise Exception('Raw transaction outputs must be a mapping or a list of single-address mappings.')


def _resolve_burn_address(key):
    burn_key = str(key or '').strip().lower()
    try:
        addresses = RPC.getburnaddresses()
    except Exception:
        addresses = None

    if isinstance(addresses, dict):
        value = str(addresses.get(burn_key) or '').strip()
        if value:
            return value

    fallback = str(BURN_ADDRESS_FALLBACKS.get(burn_key) or '').strip()
    if fallback:
        return fallback

    raise Exception(f'No burn address available for key: {burn_key}')


def _estimate_tx_size_bytes(input_count, output_count):
    # Approximation for non-segwit style transaction weight in bytes.
    return 10 + (int(input_count) * 148) + (int(output_count) * 34)


def _raw_tx_size_bytes(raw_tx_hex):
    try:
        normalized = str(raw_tx_hex or '').strip()
    except Exception:
        return 0

    if not normalized or len(normalized) % 2 != 0:
        return 0

    return len(normalized) // 2


def _estimate_signed_tx_size_bytes(raw_tx_hex, input_count):
    unsigned_size = _raw_tx_size_bytes(raw_tx_hex)
    if unsigned_size <= 0:
        return 0

    # P2PKH-style inputs are ~41 bytes unsigned and ~148 bytes signed.
    per_input_signature_overhead = 107
    return unsigned_size + (int(input_count) * per_input_signature_overhead)


def _get_estimated_feerate_evr_per_kb(conf_target=DEFAULT_FEE_CONF_TARGET,
                                      estimate_mode=DEFAULT_FEE_ESTIMATE_MODE):
    target = max(1, min(1008, int(conf_target or DEFAULT_FEE_CONF_TARGET)))
    mode = str(estimate_mode or DEFAULT_FEE_ESTIMATE_MODE).upper()

    metadata = {'errors': [], 'sources': {}}

    smart_result = None
    for call in (lambda: RPC.estimatesmartfee(target, mode), lambda: RPC.estimatesmartfee(target)):
        try:
            smart_result = call()
            break
        except Exception as exc:
            metadata['errors'].append(f'estimatesmartfee: {str(exc)}')

    if isinstance(smart_result, dict):
        metadata['sources']['estimatesmartfee'] = smart_result
        smart_feerate = _parse_positive_decimal(smart_result.get('feerate'))
        if smart_feerate is not None:
            return smart_feerate, metadata
        else:
            metadata['errors'].extend(smart_result.get('errors') or ['estimatesmartfee returned no feerate.'])
    elif smart_result is not None:
        metadata['errors'].append(f'Unexpected estimatesmartfee response: {smart_result}')

    return None, metadata


def _parse_positive_decimal(value):
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None

    if parsed <= 0:
        return None
    return parsed


def _get_fee_floor_evr_per_kb():
    """Return a conservative fee-rate floor from relay and mempool settings."""
    candidates = [DEFAULT_MIN_RELAY_FEE_EVR_PER_KB]
    errors = []

    try:
        mempool_info = RPC.getmempoolinfo()
        if isinstance(mempool_info, dict):
            mempool_floor = _parse_positive_decimal(mempool_info.get('mempoolminfee'))
            if mempool_floor is not None:
                candidates.append(mempool_floor)
    except Exception as exc:
        errors.append(f'getmempoolinfo: {str(exc)}')

    return max(candidates), {'errors': errors}


def _resolve_fee_satoshis(explicit_fee_evr, input_count, output_count,
                          conf_target=DEFAULT_FEE_CONF_TARGET,
                          estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
                          fallback_fee_evr=DEFAULT_FEE_EVR,
                          tx_size_bytes=None):
    feerate, _meta = _get_estimated_feerate_evr_per_kb(
        conf_target=conf_target,
        estimate_mode=estimate_mode,
    )
    fee_floor, _floor_meta = _get_fee_floor_evr_per_kb()

    effective_feerate = None
    if feerate is not None and fee_floor is not None:
        effective_feerate = max(feerate, fee_floor)
    elif feerate is not None:
        effective_feerate = feerate
    elif fee_floor is not None:
        effective_feerate = fee_floor * NO_ESTIMATE_FEE_FLOOR_MULTIPLIER

    estimated_size_bytes = int(tx_size_bytes or 0)
    if estimated_size_bytes <= 0:
        estimated_size_bytes = _estimate_tx_size_bytes(input_count, output_count)

    tx_size_kb = Decimal(estimated_size_bytes) / Decimal('1000')
    if effective_feerate is None:
        effective_feerate = DEFAULT_MIN_RELAY_FEE_EVR_PER_KB * NO_ESTIMATE_FEE_FLOOR_MULTIPLIER

    estimated_fee_evr = (effective_feerate * tx_size_kb * FEE_SAFETY_MULTIPLIER).quantize(
        Decimal('0.00000001'),
        rounding=ROUND_UP,
    )
    estimated_fee_satoshis = _to_satoshis(estimated_fee_evr)

    if explicit_fee_evr is not None:
        return max(_to_satoshis(explicit_fee_evr), estimated_fee_satoshis)

    if estimated_fee_satoshis <= 0:
        return _to_satoshis(fallback_fee_evr)

    return estimated_fee_satoshis


def _sequence_for_input(locktime=0, replaceable=False):
    if replaceable:
        return RBF_SEQUENCE
    if locktime:
        return LOCKTIME_SEQUENCE
    return MAX_SEQUENCE


def _get_address_utxos(address, asset_name=None):
    last_error = None
    utxos = None

    request_obj = {'addresses': [address]}
    if asset_name:
        request_obj['assetName'] = str(asset_name)

    for call in (
        lambda: RPC.getaddressutxos(request_obj),
        lambda: RPC.getaddressutxos({**request_obj, 'chainInfo': True}),
        lambda: RPC.getaddressutxos(addresses=[address]),
        lambda: RPC.getaddressutxos(addresses=address),
    ):
        try:
            utxos = call()
            break
        except Exception as exc:
            last_error = exc

    if utxos is None and last_error is not None:
        raise Exception(f'Unable to fetch UTXOs for address {address}: {str(last_error)}')

    if not isinstance(utxos, list):
        raise Exception(f'Unexpected UTXO response for address {address}: {utxos}')
    return utxos


def _build_input_entry(utxo_item, sequence=None):
    txid = utxo_item.get('txid')
    vout = utxo_item.get('outputIndex', utxo_item.get('vout'))

    if txid is None or vout is None:
        raise Exception(f'Invalid UTXO entry: {utxo_item}')

    tx_input = {
        'txid': txid,
        'vout': int(vout),
    }

    if sequence is not None:
        tx_input['sequence'] = int(sequence)

    return tx_input


def _asset_name_from_utxo(utxo_item):
    return (
        utxo_item.get('assetName')
        or utxo_item.get('assetname')
        or utxo_item.get('asset')
    )


def _is_evr_utxo(utxo_item):
    asset_name = _asset_name_from_utxo(utxo_item)
    if not asset_name:
        return True
    return str(asset_name).upper() == 'EVR'


def _amount_from_utxo(utxo_item):
    explicit = (
        utxo_item.get('amount')
        or utxo_item.get('assetAmount')
        or utxo_item.get('assetamount')
    )
    if explicit is not None:
        return explicit

    satoshis = utxo_item.get('satoshis')
    if satoshis is None:
        return 0

    try:
        return Decimal(int(satoshis)) / Decimal('100000000')
    except (InvalidOperation, TypeError, ValueError):
        return 0


def _mempool_spent_outpoints():
    try:
        transaction_ids = RPC.getrawmempool() or []
    except Exception:
        return set()

    spent_outpoints = set()
    for transaction_id in transaction_ids:
        try:
            transaction = RPC.getrawtransaction(transaction_id, True)
        except Exception:
            continue
        for transaction_input in transaction.get('vin', []):
            previous_txid = transaction_input.get('txid')
            previous_vout = transaction_input.get('vout')
            if previous_txid is not None and previous_vout is not None:
                spent_outpoints.add((str(previous_txid), int(previous_vout)))
    return spent_outpoints


def _asset_amount_from_utxo(utxo_item):
    explicit = (
        utxo_item.get('assetAmount')
        or utxo_item.get('assetamount')
        or utxo_item.get('amount')
    )
    if explicit is not None:
        return explicit

    satoshis = utxo_item.get('satoshis')
    if satoshis is None:
        return 0

    try:
        return Decimal(int(satoshis)) / Decimal('100000000')
    except (InvalidOperation, TypeError, ValueError):
        return 0


def _find_authorization_input(utxos, authorization_asset_name, sequence=None, address=None,
                              excluded_keys=None):
    if not authorization_asset_name:
        return None

    auth_name = str(authorization_asset_name).upper()
    excluded = set(excluded_keys or set())
    for utxo_item in utxos:
        utxo_asset = _asset_name_from_utxo(utxo_item)
        if not utxo_asset:
            continue

        if str(utxo_asset).upper() != auth_name:
            continue

        if Decimal(str(_amount_from_utxo(utxo_item))) <= 0:
            continue

        input_entry = _build_input_entry(utxo_item, sequence)
        if (input_entry['txid'], input_entry['vout']) in excluded:
            continue
        return input_entry, Decimal(str(_amount_from_utxo(utxo_item)))

    if address:
        filtered_utxos = _get_address_utxos(address, authorization_asset_name)
        for utxo_item in filtered_utxos:
            utxo_asset = _asset_name_from_utxo(utxo_item)
            if str(utxo_asset or '').upper() != auth_name:
                continue
            if Decimal(str(_amount_from_utxo(utxo_item))) <= 0:
                continue
            input_entry = _build_input_entry(utxo_item, sequence)
            if (input_entry['txid'], input_entry['vout']) in excluded:
                continue
            return input_entry, Decimal(str(_amount_from_utxo(utxo_item)))

    raise Exception(f'Authorization asset input not found: {authorization_asset_name}')


def _owner_token_name(asset_name_or_root):
    asset_name = str(asset_name_or_root or '')
    if not asset_name:
        raise Exception('Asset name is required to derive owner token name.')

    if asset_name.endswith('!'):
        return asset_name

    if asset_name.startswith('$'):
        asset_name = asset_name[1:]

    root_name = asset_name.split('/')[0]
    root_name = root_name.split('~')[0]
    root_name = root_name.split('#')[0]
    return f'{root_name}!'


def _is_subqualifier(asset_name):
    normalized = str(asset_name or '')
    return normalized.startswith('#') and '/' in normalized


def _root_qualifier_name(asset_name):
    normalized = str(asset_name or '')
    if not normalized.startswith('#'):
        raise Exception('Qualifier asset names must start with #.')
    return normalized.split('/')[0]


def _select_evr_inputs(address, required_satoshis, locktime=0, replaceable=False, excluded_keys=None):
    utxos = sorted(_get_address_utxos(address), key=lambda item: int(item.get('satoshis', 0)), reverse=True)

    total_selected = 0
    selected_inputs = []
    excluded = set(excluded_keys or set()) | _mempool_spent_outpoints()
    sequence = _sequence_for_input(locktime=locktime, replaceable=replaceable)
    include_sequence = sequence != MAX_SEQUENCE

    for utxo_item in utxos:
        if not _is_evr_utxo(utxo_item):
            continue

        key = (
            utxo_item.get('txid'),
            int(utxo_item.get('outputIndex', utxo_item.get('vout', -1))),
        )
        if key in excluded:
            continue

        satoshis = int(utxo_item.get('satoshis', 0))
        if satoshis <= 0:
            continue

        selected_inputs.append(_build_input_entry(utxo_item, sequence if include_sequence else None))
        total_selected += satoshis

        if total_selected >= required_satoshis:
            break

    if total_selected < required_satoshis:
        needed = _satoshis_to_evr(required_satoshis)
        available = _satoshis_to_evr(total_selected)
        raise InsufficientSpendableBalance(
            f'Insufficient spendable EVR balance. Needed: {needed}, available: {available}.'
        )

    return selected_inputs, total_selected


def _select_asset_inputs(address, asset_name, required_quantity, locktime=0, replaceable=False):
    required_quantity = Decimal(str(required_quantity))
    if required_quantity <= 0:
        raise Exception('Asset quantity must be greater than zero.')

    normalized_asset_name = str(asset_name).upper()
    sequence = _sequence_for_input(locktime=locktime, replaceable=replaceable)
    include_sequence = sequence != MAX_SEQUENCE
    selected_inputs = []
    selected_quantity = Decimal('0')
    selected_coin_satoshis = 0
    excluded = _mempool_spent_outpoints()

    for utxo_item in _get_address_utxos(address, asset_name=normalized_asset_name):
        utxo_asset_name = _asset_name_from_utxo(utxo_item)
        if not utxo_asset_name or str(utxo_asset_name).upper() != normalized_asset_name:
            continue

        try:
            asset_amount = Decimal(str(_asset_amount_from_utxo(utxo_item)))
        except (InvalidOperation, TypeError, ValueError):
            continue

        if asset_amount <= 0:
            continue

        input_entry = _build_input_entry(utxo_item, sequence if include_sequence else None)
        if (input_entry['txid'], input_entry['vout']) in excluded:
            continue
        selected_inputs.append(input_entry)
        selected_quantity += asset_amount

        # Asset UTXO satoshis encode asset quantity and are not EVR coin value.
        if _is_evr_utxo(utxo_item):
            selected_coin_satoshis += int(utxo_item.get('satoshis', 0))

        if selected_quantity >= required_quantity:
            break

    if selected_quantity < required_quantity:
        raise InsufficientSpendableBalance(
            f'Insufficient spendable {asset_name} balance. '
            f'Needed: {required_quantity}, available: {selected_quantity}.'
        )

    return selected_inputs, selected_quantity, selected_coin_satoshis


def _select_inputs_for_operation(from_address, required_evr_satoshis,
                                 authorization_asset_name=None,
                                 locktime=0, replaceable=False):
    utxos = _get_address_utxos(from_address)
    sequence = _sequence_for_input(locktime=locktime, replaceable=replaceable)
    include_sequence = sequence != MAX_SEQUENCE

    selected_inputs = []
    selected_keys = set()
    selected_total_satoshis = 0
    authorization_quantity = Decimal('0')
    mempool_spent = _mempool_spent_outpoints()

    if authorization_asset_name:
        auth_input, authorization_quantity = _find_authorization_input(
            utxos,
            authorization_asset_name=authorization_asset_name,
            sequence=sequence if include_sequence else None,
            address=from_address,
            excluded_keys=mempool_spent,
        )
        selected_inputs.append(auth_input)
        selected_keys.add((auth_input['txid'], auth_input['vout']))

    evr_candidates = sorted(utxos, key=lambda item: int(item.get('satoshis', 0)), reverse=True)

    for utxo_item in evr_candidates:
        txid = utxo_item.get('txid')
        vout = int(utxo_item.get('outputIndex', utxo_item.get('vout', -1)))
        satoshis = int(utxo_item.get('satoshis', 0))

        if txid is None or vout < 0 or satoshis <= 0:
            continue

        # Use only coin-like EVR UTXOs for fee/value funding.
        if not _is_evr_utxo(utxo_item):
            continue

        if (txid, vout) in selected_keys or (txid, vout) in mempool_spent:
            continue

        selected_inputs.append(_build_input_entry(utxo_item, sequence if include_sequence else None))
        selected_keys.add((txid, vout))
        selected_total_satoshis += satoshis

        if selected_total_satoshis >= required_evr_satoshis:
            break

    if selected_total_satoshis < required_evr_satoshis:
        needed = _satoshis_to_evr(required_evr_satoshis)
        available = _satoshis_to_evr(selected_total_satoshis)
        raise Exception(f'Insufficient EVR balance for operation. Needed: {needed}, available: {available}.')

    return selected_inputs, selected_total_satoshis, authorization_quantity


def compose_asset_operation_outputs(coin_outputs, operation_address, operation_payload,
                                    owner_token_change_output=None):
    """
    Compose outputs for asset operations in required order:
    1) Coin outputs first (including burn output)
    2) Owner/root token change output next (if required)
    3) Asset operation output last
    """
    outputs = []

    for address, amount in coin_outputs.items():
        outputs.append({address: _evr_output_value(amount)})

    if owner_token_change_output:
        change_address, payload = owner_token_change_output
        outputs.append({change_address: payload})

    outputs.append({operation_address: operation_payload})
    return outputs


def _normalize_wifs(wif_keys):
    if wif_keys is None:
        return []
    if isinstance(wif_keys, (list, tuple, set)):
        return [str(item).strip() for item in wif_keys if str(item).strip()]
    wif = str(wif_keys).strip()
    return [wif] if wif else []


def sign_raw_transaction(raw_tx, wif_keys=None):
    normalized_wifs = _normalize_wifs(wif_keys)
    sign_errors = []
    signed = None

    if normalized_wifs:
        signer = getattr(RPC, 'signrawtransaction', None)
        if signer is None:
            sign_errors.append('signrawtransaction: RPC method unavailable.')
        else:
            for call in (
                lambda: signer(raw_tx, None, normalized_wifs, 'ALL'),
                lambda: signer(raw_tx, [], normalized_wifs, 'ALL'),
                lambda: signer(raw_tx, None, normalized_wifs),
                lambda: signer(raw_tx, [], normalized_wifs),
            ):
                try:
                    signed = call()
                    break
                except Exception as exc:
                    sign_errors.append(f'signrawtransaction(privkeys): {str(exc)}')

    if signed is None:
        for method_name in ('signrawtransactionwithwallet', 'signrawtransaction'):
            signer = getattr(RPC, method_name, None)
            if signer is None:
                continue

            try:
                signed = signer(raw_tx)
                break
            except Exception as exc:
                sign_errors.append(f'{method_name}: {str(exc)}')

    if signed is None:
        details = '; '.join(sign_errors) if sign_errors else 'No signing method available on RPC client.'
        raise Exception(f'Failed to sign raw transaction. {details}')

    if isinstance(signed, dict):
        if not signed.get('complete', True):
            errors = signed.get('errors', [])
            raise Exception(f'Raw transaction signing incomplete: {errors}')
        signed_hex = signed.get('hex')
    else:
        signed_hex = str(signed)

    if not signed_hex:
        raise Exception('RPC did not return signed transaction hex.')

    return signed_hex


def broadcast_signed_transaction(signed_hex):
    return RPC.sendrawtransaction(signed_hex)


def test_mempool_accept_signed_transaction(signed_hex):
    result = RPC.testmempoolaccept([signed_hex])
    if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
        raise Exception(f'Unexpected testmempoolaccept response: {result}')

    acceptance = result[0]
    if not bool(acceptance.get('allowed')):
        reason = acceptance.get('reject-reason') or acceptance.get('reject_reason') or 'transaction rejected'
        raise Exception(f'Transaction rejected by testmempoolaccept: {reason}')
    return acceptance


def sign_and_broadcast_raw_transaction(raw_tx, wif_keys=None):
    signed_hex = sign_raw_transaction(raw_tx, wif_keys=wif_keys)
    test_mempool_accept_signed_transaction(signed_hex)
    return broadcast_signed_transaction(signed_hex)


def create_raw_asset_operation_transaction(
    from_address,
    operation_address,
    operation_payload,
    burn_amount_evr=Decimal('0'),
    burn_address=None,
    authorization_asset_name=None,
    owner_token_change_output=None,
    extra_coin_outputs=None,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
    evr_change_address=None,
    authorization_change_output_required=True,
):
    """
    Create a raw asset-operation transaction without signing or broadcasting it.
    """
    if not operation_address:
        raise Exception('operation_address is required.')

    source_address = str(from_address or '').strip()
    if not source_address:
        raise Exception('from_address is required.')

    burn_satoshis = _to_satoshis(burn_amount_evr)
    if burn_satoshis > 0 and not burn_address:
        raise Exception('burn_address is required when burn_amount_evr is greater than zero.')

    extra_outputs_count = len(extra_coin_outputs or {})
    owner_change_count = 1 if owner_token_change_output or (
        authorization_asset_name and authorization_change_output_required
    ) else 0
    asset_output_count = 1
    provisional_fee_sats = _resolve_fee_satoshis(
        explicit_fee_evr=fee_evr,
        input_count=1,
        output_count=1 + extra_outputs_count + owner_change_count + asset_output_count,
        conf_target=fee_conf_target,
        estimate_mode=fee_estimate_mode,
    )

    selected_inputs = []
    selected_total = 0
    final_fee_satoshis = provisional_fee_sats
    outputs = OrderedDict()
    raw_tx = None

    for _ in range(4):
        required_satoshis = burn_satoshis + final_fee_satoshis
        selected_inputs, selected_total, authorization_quantity = _select_inputs_for_operation(
            from_address=from_address,
            required_evr_satoshis=required_satoshis,
            authorization_asset_name=authorization_asset_name,
            locktime=locktime,
            replaceable=replaceable,
        )

        if authorization_asset_name and authorization_change_output_required:
            owner_token_change_output = (
                source_address,
                {
                    'transfer': {
                        str(authorization_asset_name): float(authorization_quantity),
                    }
                },
            )

        coin_outputs = OrderedDict()
        if burn_satoshis > 0:
            coin_outputs[burn_address] = _satoshis_to_evr(burn_satoshis)

        if extra_coin_outputs:
            for address, amount in extra_coin_outputs.items():
                coin_outputs[address] = _to_decimal_evr(amount)

        extra_coin_satoshis = sum(_to_satoshis(amount) for amount in coin_outputs.values())
        change_satoshis = selected_total - final_fee_satoshis - extra_coin_satoshis

        if change_satoshis < 0:
            needed = _satoshis_to_evr(final_fee_satoshis + extra_coin_satoshis)
            available = _satoshis_to_evr(selected_total)
            raise Exception(f'Insufficient EVR after output assembly. Needed: {needed}, available: {available}.')

        if change_satoshis >= DUST_THRESHOLD_SATS:
            coin_outputs[source_address] = _satoshis_to_evr(change_satoshis)

        outputs = compose_asset_operation_outputs(
            coin_outputs=coin_outputs,
            operation_address=operation_address,
            operation_payload=operation_payload,
            owner_token_change_output=owner_token_change_output,
        )

        raw_tx = create_raw_transaction(
            inputs=selected_inputs,
            outputs=outputs,
            locktime=locktime,
            replaceable=replaceable,
        )
        measured_signed_size = _estimate_signed_tx_size_bytes(raw_tx, len(selected_inputs))

        next_fee_satoshis = _resolve_fee_satoshis(
            explicit_fee_evr=fee_evr,
            input_count=len(selected_inputs),
            output_count=len(outputs),
            conf_target=fee_conf_target,
            estimate_mode=fee_estimate_mode,
            tx_size_bytes=measured_signed_size,
        )
        if next_fee_satoshis == final_fee_satoshis:
            break
        final_fee_satoshis = next_fee_satoshis

    if raw_tx is None:
        raw_tx = create_raw_transaction(
            inputs=selected_inputs,
            outputs=outputs,
            locktime=locktime,
            replaceable=replaceable,
        )

    return {
        'raw_tx': raw_tx,
        'inputs': selected_inputs,
        'outputs': outputs,
    }


def create_and_send_asset_operation_transaction(
    from_address,
    operation_address,
    operation_payload,
    burn_amount_evr=Decimal('0'),
    burn_address=None,
    authorization_asset_name=None,
    owner_token_change_output=None,
    extra_coin_outputs=None,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
    wif_keys=None,
    evr_change_address=None,
):
    """
    Create, sign, and broadcast an asset operation transaction.
    """
    tx_data = create_raw_asset_operation_transaction(
        from_address=from_address,
        operation_address=operation_address,
        operation_payload=operation_payload,
        burn_amount_evr=burn_amount_evr,
        burn_address=burn_address,
        authorization_asset_name=authorization_asset_name,
        owner_token_change_output=owner_token_change_output,
        extra_coin_outputs=extra_coin_outputs,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
        evr_change_address=evr_change_address,
    )
    txid = sign_and_broadcast_raw_transaction(tx_data['raw_tx'], wif_keys=wif_keys)

    return {
        'txid': txid,
        **tx_data,
    }


def create_raw_evr_transaction(from_address, to_address, amount_evr, change_address=None,
                               fee_evr=None, fee_conf_target=DEFAULT_FEE_CONF_TARGET,
                               fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE, locktime=0, replaceable=False,
                               extra_coin_outputs=None):
    """
    Create a raw EVR payment transaction without signing or broadcasting it.
    """
    amount_satoshis = _to_satoshis(amount_evr)
    if amount_satoshis <= 0:
        raise Exception('Amount must be greater than zero.')

    extra_outputs_count = len(extra_coin_outputs or {})
    provisional_fee_sats = _resolve_fee_satoshis(
        explicit_fee_evr=fee_evr,
        input_count=1,
        output_count=1 + extra_outputs_count + 1,
        conf_target=fee_conf_target,
        estimate_mode=fee_estimate_mode,
    )

    selected_inputs = []
    selected_total = 0
    outputs = OrderedDict()
    raw_tx = None
    final_fee_satoshis = provisional_fee_sats

    for _ in range(4):
        required_satoshis = amount_satoshis + final_fee_satoshis
        selected_inputs, selected_total = _select_evr_inputs(
            address=from_address,
            required_satoshis=required_satoshis,
            locktime=locktime,
            replaceable=replaceable,
        )

        outputs = OrderedDict()
        if extra_coin_outputs:
            for address, amount in extra_coin_outputs.items():
                outputs[address] = _evr_output_value(amount)

        outputs[to_address] = _evr_output_value(amount_evr)

        change_satoshis = selected_total - required_satoshis
        if change_satoshis >= DUST_THRESHOLD_SATS:
            outputs[from_address] = _evr_output_value(_satoshis_to_evr(change_satoshis))

        raw_tx = create_raw_transaction(
            inputs=selected_inputs,
            outputs=outputs,
            locktime=locktime,
            replaceable=replaceable,
        )
        measured_signed_size = _estimate_signed_tx_size_bytes(raw_tx, len(selected_inputs))

        next_fee_satoshis = _resolve_fee_satoshis(
            explicit_fee_evr=fee_evr,
            input_count=len(selected_inputs),
            output_count=len(outputs),
            conf_target=fee_conf_target,
            estimate_mode=fee_estimate_mode,
            tx_size_bytes=measured_signed_size,
        )
        if next_fee_satoshis == final_fee_satoshis:
            break
        final_fee_satoshis = next_fee_satoshis

    if raw_tx is None:
        raw_tx = create_raw_transaction(
            inputs=selected_inputs,
            outputs=outputs,
            locktime=locktime,
            replaceable=replaceable,
        )

    return {
        'raw_tx': raw_tx,
        'inputs': selected_inputs,
        'outputs': dict(outputs),
    }


def create_and_send_evr_transaction(from_address, to_address, amount_evr, change_address=None,
                                    fee_evr=None, fee_conf_target=DEFAULT_FEE_CONF_TARGET,
                                    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE, locktime=0, replaceable=False,
                                    extra_coin_outputs=None, wif_keys=None):
    """
    Create, sign, and broadcast an EVR payment transaction.
    """
    tx_data = create_raw_evr_transaction(
        from_address=from_address,
        to_address=to_address,
        amount_evr=amount_evr,
        change_address=change_address,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
        extra_coin_outputs=extra_coin_outputs,
    )
    txid = sign_and_broadcast_raw_transaction(tx_data['raw_tx'], wif_keys=wif_keys)

    return {
        'txid': txid,
        **tx_data,
    }


def create_raw_asset_transfer_transaction(from_address, to_address, asset_name, asset_quantity,
                                          change_address=None, fee_evr=None,
                                          fee_conf_target=DEFAULT_FEE_CONF_TARGET,
                                          fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
                                          locktime=0, replaceable=False,
                                          asset_change_address=None, message=None,
                                          expire_time=0):
    """
    Create a raw asset transfer transaction without signing or broadcasting it.
    """
    try:
        asset_quantity_decimal = Decimal(str(asset_quantity))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise Exception('Asset quantity must be a valid decimal value.') from exc

    if asset_quantity_decimal <= 0:
        raise Exception('Asset quantity must be greater than zero.')

    sequence = _sequence_for_input(locktime=locktime, replaceable=replaceable)
    include_sequence = sequence != MAX_SEQUENCE

    # Select the asset-bearing inputs first, then add EVR inputs for fees.
    asset_inputs, selected_asset_quantity, _asset_input_coin_satoshis = _select_asset_inputs(
        address=from_address,
        asset_name=asset_name,
        required_quantity=asset_quantity_decimal,
        locktime=locktime,
        replaceable=replaceable,
    )

    selected_asset_change = selected_asset_quantity - asset_quantity_decimal

    utxos = _get_address_utxos(from_address)
    selected_keys = {(item['txid'], item['vout']) for item in asset_inputs}
    mempool_spent = _mempool_spent_outpoints()

    evr_candidates = sorted(
        [
            item for item in utxos
            if _is_evr_utxo(item)
            and int(item.get('satoshis', 0)) > 0
            and (
                item.get('txid'),
                int(item.get('outputIndex', item.get('vout', -1)))
            ) not in selected_keys
            and (
                item.get('txid'),
                int(item.get('outputIndex', item.get('vout', -1)))
            ) not in mempool_spent
        ],
        key=lambda item: int(item.get('satoshis', 0)),
        reverse=True,
    )

    evr_inputs = []
    evr_total_satoshis = 0

    def _select_evr_fee_inputs(required_fee_sats):
        nonlocal evr_inputs, evr_total_satoshis
        evr_inputs = []
        evr_total_satoshis = 0
        for utxo_item in evr_candidates:
            satoshis = int(utxo_item.get('satoshis', 0))
            if satoshis <= 0:
                continue
            evr_inputs.append(_build_input_entry(utxo_item, sequence if include_sequence else None))
            evr_total_satoshis += satoshis
            if evr_total_satoshis >= required_fee_sats:
                break

        if evr_total_satoshis < required_fee_sats:
            needed = _satoshis_to_evr(required_fee_sats)
            available = _satoshis_to_evr(evr_total_satoshis)
            raise Exception(f'Insufficient EVR for fees. Needed: {needed}, available: {available}.')

    outputs = []
    coin_change_address = from_address
    asset_change_target = from_address
    raw_tx = None

    # Iteratively re-estimate fee based on selected inputs/outputs.
    fee_satoshis = _resolve_fee_satoshis(
        explicit_fee_evr=fee_evr,
        input_count=len(asset_inputs) + 1,
        output_count=2 if selected_asset_change > 0 else 1,
        conf_target=fee_conf_target,
        estimate_mode=fee_estimate_mode,
    )

    for _ in range(4):
        _select_evr_fee_inputs(fee_satoshis)

        if message:
            transfer_payload = {
                'transferwithmessage': {
                    asset_name: float(asset_quantity_decimal),
                    'message': str(message),
                    'expire_time': int(expire_time),
                }
            }
        else:
            transfer_payload = {
                'transfer': {
                    asset_name: float(asset_quantity_decimal),
                }
            }

        outputs = [{to_address: transfer_payload}]

        if selected_asset_change > 0:
            outputs.append(
                {
                    asset_change_target: {
                        'transfer': {
                            asset_name: float(selected_asset_change),
                        }
                    }
                }
            )

        total_input_satoshis = evr_total_satoshis
        coin_change_satoshis = total_input_satoshis - fee_satoshis

        if coin_change_satoshis >= DUST_THRESHOLD_SATS:
            outputs.append({coin_change_address: _evr_output_value(_satoshis_to_evr(coin_change_satoshis))})

        candidate_inputs = [*asset_inputs, *evr_inputs]
        raw_tx = create_raw_transaction(
            inputs=candidate_inputs,
            outputs=outputs,
            locktime=locktime,
            replaceable=replaceable,
        )
        measured_signed_size = _estimate_signed_tx_size_bytes(raw_tx, len(candidate_inputs))

        next_fee_sats = _resolve_fee_satoshis(
            explicit_fee_evr=fee_evr,
            input_count=len(candidate_inputs),
            output_count=len(outputs),
            conf_target=fee_conf_target,
            estimate_mode=fee_estimate_mode,
            tx_size_bytes=measured_signed_size,
        )
        if next_fee_sats == fee_satoshis:
            break
        fee_satoshis = next_fee_sats

    inputs = [*asset_inputs, *evr_inputs]
    if raw_tx is None:
        raw_tx = create_raw_transaction(
            inputs=inputs,
            outputs=outputs,
            locktime=locktime,
            replaceable=replaceable,
        )

    return {
        'raw_tx': raw_tx,
        'inputs': inputs,
        'outputs': outputs,
    }


def create_and_send_asset_transfer_transaction(from_address, to_address, asset_name, asset_quantity,
                                               change_address=None, fee_evr=None,
                                               fee_conf_target=DEFAULT_FEE_CONF_TARGET,
                                               fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
                                               locktime=0, replaceable=False, wif_keys=None,
                                               asset_change_address=None, message=None,
                                               expire_time=0):
    """
    Create, sign, and broadcast an asset transfer transaction.
    """
    tx_data = create_raw_asset_transfer_transaction(
        from_address=from_address,
        to_address=to_address,
        asset_name=asset_name,
        asset_quantity=asset_quantity,
        change_address=change_address,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
        asset_change_address=asset_change_address,
        message=message,
        expire_time=expire_time,
    )
    txid = sign_and_broadcast_raw_transaction(tx_data['raw_tx'], wif_keys=wif_keys)

    return {
        'txid': txid,
        **tx_data,
    }


def create_raw_atomic_asset_evr_swap_transaction(
    seller_address,
    buyer_address,
    asset_name,
    asset_quantity,
    payment_evr,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
):
    """Create a single Evrmore transaction that exchanges an asset for EVR."""
    if not seller_address or not buyer_address:
        raise Exception('Seller and buyer addresses are required.')
    if seller_address == buyer_address:
        raise Exception('Seller and buyer addresses must be different.')
    if not asset_name:
        raise Exception('Asset name is required.')

    try:
        asset_quantity_decimal = Decimal(str(asset_quantity))
        payment_decimal = Decimal(str(payment_evr))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise Exception('Asset quantity and EVR payment must be valid decimal values.') from exc

    if asset_quantity_decimal <= 0 or payment_decimal <= 0:
        raise Exception('Asset quantity and EVR payment must be greater than zero.')

    seller_inputs, selected_asset_quantity, seller_input_satoshis = _select_asset_inputs(
        address=seller_address,
        asset_name=asset_name,
        required_quantity=asset_quantity_decimal,
        locktime=locktime,
        replaceable=replaceable,
    )
    asset_change_quantity = selected_asset_quantity - asset_quantity_decimal
    payment_satoshis = _to_satoshis(payment_decimal)
    provisional_fee_satoshis = _resolve_fee_satoshis(
        explicit_fee_evr=fee_evr,
        input_count=len(seller_inputs) + 1,
        output_count=4 if asset_change_quantity > 0 else 3,
        conf_target=fee_conf_target,
        estimate_mode=fee_estimate_mode,
    )

    final_fee_satoshis = provisional_fee_satoshis
    buyer_inputs = []
    buyer_input_satoshis = 0
    outputs = []
    raw_tx = None

    for _ in range(4):
        asset_output_count = 2 if asset_change_quantity > 0 else 1
        asset_output_satoshis = DUST_THRESHOLD_SATS * asset_output_count
        buyer_required_satoshis = (
            payment_satoshis
            + final_fee_satoshis
            + max(0, asset_output_satoshis - seller_input_satoshis)
        )
        buyer_inputs, buyer_input_satoshis = _select_evr_inputs(
            address=buyer_address,
            required_satoshis=buyer_required_satoshis,
            locktime=locktime,
            replaceable=replaceable,
        )

        seller_native_change = max(0, seller_input_satoshis - asset_output_satoshis)
        buyer_native_change = buyer_input_satoshis - buyer_required_satoshis

        outputs = [
            {
                buyer_address: {
                    'transfer': {
                        str(asset_name): float(asset_quantity_decimal),
                    }
                }
            },
        ]
        if asset_change_quantity > 0:
            outputs.append(
                {
                    seller_address: {
                        'transfer': {
                            str(asset_name): float(asset_change_quantity),
                        }
                    }
                }
            )

        seller_payout_satoshis = payment_satoshis + seller_native_change
        outputs.append({seller_address: _evr_output_value(_satoshis_to_evr(seller_payout_satoshis))})

        if buyer_native_change >= DUST_THRESHOLD_SATS:
            outputs.append({buyer_address: _evr_output_value(_satoshis_to_evr(buyer_native_change))})

        candidate_inputs = [*seller_inputs, *buyer_inputs]
        raw_tx = create_raw_transaction(
            inputs=candidate_inputs,
            outputs=outputs,
            locktime=locktime,
            replaceable=replaceable,
        )
        measured_signed_size = _estimate_signed_tx_size_bytes(raw_tx, len(candidate_inputs))

        next_fee_satoshis = _resolve_fee_satoshis(
            explicit_fee_evr=fee_evr,
            input_count=len(candidate_inputs),
            output_count=len(outputs),
            conf_target=fee_conf_target,
            estimate_mode=fee_estimate_mode,
            tx_size_bytes=measured_signed_size,
        )
        if next_fee_satoshis == final_fee_satoshis:
            break
        final_fee_satoshis = next_fee_satoshis

    if raw_tx is None:
        raw_tx = create_raw_transaction(
            inputs=[*seller_inputs, *buyer_inputs],
            outputs=outputs,
            locktime=locktime,
            replaceable=replaceable,
        )

    return {
        'raw_tx': raw_tx,
        'inputs': [*seller_inputs, *buyer_inputs],
        'outputs': outputs,
        'asset_change_quantity': asset_change_quantity,
        'fee_evr': _satoshis_to_evr(final_fee_satoshis),
    }


def create_and_send_atomic_asset_evr_swap_transaction(
    seller_address,
    buyer_address,
    asset_name,
    asset_quantity,
    payment_evr,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
    wif_keys=None,
):
    """Create, sign with both parties, and broadcast an atomic asset-for-EVR swap."""
    tx_data = create_raw_atomic_asset_evr_swap_transaction(
        seller_address=seller_address,
        buyer_address=buyer_address,
        asset_name=asset_name,
        asset_quantity=asset_quantity,
        payment_evr=payment_evr,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
    )
    txid = sign_and_broadcast_raw_transaction(tx_data['raw_tx'], wif_keys=wif_keys)

    return {
        'txid': txid,
        **tx_data,
    }


def create_raw_atomic_asset_asset_swap_transaction(
    seller_address,
    buyer_address,
    seller_asset_name,
    seller_asset_quantity,
    buyer_asset_name,
    buyer_asset_quantity,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
):
    """Create a single Evrmore transaction that swaps one asset for another."""
    if not seller_address or not buyer_address:
        raise Exception('Seller and buyer addresses are required.')
    if seller_address == buyer_address:
        raise Exception('Seller and buyer addresses must be different.')

    seller_asset_name = str(seller_asset_name or '').strip()
    buyer_asset_name = str(buyer_asset_name or '').strip()
    if not seller_asset_name or not buyer_asset_name:
        raise Exception('Both seller and buyer asset names are required.')
    if seller_asset_name == buyer_asset_name:
        raise Exception('Asset-for-asset swaps require different assets.')

    try:
        seller_asset_quantity_decimal = Decimal(str(seller_asset_quantity))
        buyer_asset_quantity_decimal = Decimal(str(buyer_asset_quantity))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise Exception('Asset quantities must be valid decimal values.') from exc

    if seller_asset_quantity_decimal <= 0 or buyer_asset_quantity_decimal <= 0:
        raise Exception('Asset quantities must be greater than zero.')

    seller_inputs, seller_selected_quantity, seller_input_satoshis = _select_asset_inputs(
        address=seller_address,
        asset_name=seller_asset_name,
        required_quantity=seller_asset_quantity_decimal,
        locktime=locktime,
        replaceable=replaceable,
    )
    buyer_asset_inputs, buyer_selected_quantity, buyer_asset_input_satoshis = _select_asset_inputs(
        address=buyer_address,
        asset_name=buyer_asset_name,
        required_quantity=buyer_asset_quantity_decimal,
        locktime=locktime,
        replaceable=replaceable,
    )

    seller_asset_change = seller_selected_quantity - seller_asset_quantity_decimal
    buyer_asset_change = buyer_selected_quantity - buyer_asset_quantity_decimal

    provisional_fee_satoshis = _resolve_fee_satoshis(
        explicit_fee_evr=fee_evr,
        input_count=len(seller_inputs) + len(buyer_asset_inputs) + 1,
        output_count=4,
        conf_target=fee_conf_target,
        estimate_mode=fee_estimate_mode,
    )

    final_fee_satoshis = provisional_fee_satoshis
    buyer_fee_inputs = []
    buyer_fee_satoshis = 0
    outputs = []
    raw_tx = None

    for _ in range(4):
        seller_asset_output_count = 1 + (1 if seller_asset_change > 0 else 0)
        buyer_asset_output_count = 1 + (1 if buyer_asset_change > 0 else 0)
        total_asset_output_satoshis = DUST_THRESHOLD_SATS * (seller_asset_output_count + buyer_asset_output_count)

        seller_native_refund = max(0, seller_input_satoshis - (DUST_THRESHOLD_SATS * seller_asset_output_count))
        buyer_required_native = (
            total_asset_output_satoshis
            + final_fee_satoshis
            + seller_native_refund
            - seller_input_satoshis
            - buyer_asset_input_satoshis
        )
        buyer_required_native = max(0, buyer_required_native)

        buyer_fee_inputs, buyer_fee_satoshis = _select_evr_inputs(
            address=buyer_address,
            required_satoshis=buyer_required_native,
            locktime=locktime,
            replaceable=replaceable,
            excluded_keys={(item['txid'], item['vout']) for item in buyer_asset_inputs},
        )

        buyer_native_change = (
            seller_input_satoshis
            + buyer_asset_input_satoshis
            + buyer_fee_satoshis
            - total_asset_output_satoshis
            - final_fee_satoshis
            - seller_native_refund
        )
        if buyer_native_change < 0:
            continue

        buyer_transfer = {
            seller_asset_name: float(seller_asset_quantity_decimal),
        }
        if buyer_asset_change > 0:
            buyer_transfer[buyer_asset_name] = float(buyer_asset_change)

        seller_transfer = {
            buyer_asset_name: float(buyer_asset_quantity_decimal),
        }
        if seller_asset_change > 0:
            seller_transfer[seller_asset_name] = float(seller_asset_change)

        outputs = [
            {buyer_address: {'transfer': buyer_transfer}},
            {seller_address: {'transfer': seller_transfer}},
        ]

        if seller_native_refund >= DUST_THRESHOLD_SATS:
            outputs.append({seller_address: _evr_output_value(_satoshis_to_evr(seller_native_refund))})
        if buyer_native_change >= DUST_THRESHOLD_SATS:
            outputs.append({buyer_address: _evr_output_value(_satoshis_to_evr(buyer_native_change))})

        candidate_inputs = [*seller_inputs, *buyer_asset_inputs, *buyer_fee_inputs]
        raw_tx = create_raw_transaction(
            inputs=candidate_inputs,
            outputs=outputs,
            locktime=locktime,
            replaceable=replaceable,
        )
        measured_signed_size = _estimate_signed_tx_size_bytes(raw_tx, len(candidate_inputs))

        next_fee_satoshis = _resolve_fee_satoshis(
            explicit_fee_evr=fee_evr,
            input_count=len(candidate_inputs),
            output_count=len(outputs),
            conf_target=fee_conf_target,
            estimate_mode=fee_estimate_mode,
            tx_size_bytes=measured_signed_size,
        )
        if next_fee_satoshis == final_fee_satoshis:
            break
        final_fee_satoshis = next_fee_satoshis

    if raw_tx is None:
        raise Exception('Unable to construct atomic asset-for-asset swap transaction.')

    return {
        'raw_tx': raw_tx,
        'inputs': [*seller_inputs, *buyer_asset_inputs, *buyer_fee_inputs],
        'outputs': outputs,
        'seller_asset_change_quantity': seller_asset_change,
        'buyer_asset_change_quantity': buyer_asset_change,
        'fee_evr': _satoshis_to_evr(final_fee_satoshis),
    }


def create_and_send_atomic_asset_asset_swap_transaction(
    seller_address,
    buyer_address,
    seller_asset_name,
    seller_asset_quantity,
    buyer_asset_name,
    buyer_asset_quantity,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
    wif_keys=None,
):
    """Create, sign with both parties, and broadcast an atomic asset-for-asset swap."""
    tx_data = create_raw_atomic_asset_asset_swap_transaction(
        seller_address=seller_address,
        buyer_address=buyer_address,
        seller_asset_name=seller_asset_name,
        seller_asset_quantity=seller_asset_quantity,
        buyer_asset_name=buyer_asset_name,
        buyer_asset_quantity=buyer_asset_quantity,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
    )
    txid = sign_and_broadcast_raw_transaction(tx_data['raw_tx'], wif_keys=wif_keys)

    return {
        'txid': txid,
        **tx_data,
    }


def create_and_send_transfer_with_message_transaction(
    from_address,
    to_address,
    asset_name,
    asset_quantity,
    message,
    expire_time,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
    wif_keys=None,
):
    return create_and_send_asset_transfer_transaction(
        from_address=from_address,
        to_address=to_address,
        asset_name=asset_name,
        asset_quantity=asset_quantity,
        message=message,
        expire_time=expire_time,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
        wif_keys=wif_keys,
    )


def create_and_send_issue_asset_transaction(
    from_address,
    issuer_address,
    asset_name,
    asset_quantity,
    units,
    reissuable,
    has_ipfs,
    ipfs_hash='',
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
    wif_keys=None,
    owner_change_address=None,
    owner_change_quantity=1,
    evr_change_address=None,
):
    normalized_asset_name = str(asset_name)
    is_subasset = '/' in normalized_asset_name
    is_messaging_channel = '~' in normalized_asset_name
    burn_address = _resolve_burn_address(
        'issue_msg_channel_asset' if is_messaging_channel else ('issue_sub_asset' if is_subasset else 'issue_asset')
    )
    burn_amount = Decimal('100') if (is_subasset or is_messaging_channel) else Decimal('500')

    operation_name = '_issue_new_asset' if is_messaging_channel else 'issue'
    operation_payload = {
        operation_name: {
            'asset_name': normalized_asset_name,
            'asset_quantity': float(Decimal(str(asset_quantity))),
            'units': int(units),
            'reissuable': int(bool(reissuable)),
            'has_ipfs': int(bool(has_ipfs)),
        }
    }
    if not is_messaging_channel:
        operation_payload[operation_name]['remintable'] = int(bool(reissuable))
    if has_ipfs:
        operation_payload[operation_name]['ipfs_hash'] = str(ipfs_hash)

    authorization_asset_name = None
    owner_token_change_output = None
    if is_messaging_channel:
        authorization_asset_name = _owner_token_name(normalized_asset_name)
        resolved_owner_change_address = str(owner_change_address or from_address or '').strip()
        if not resolved_owner_change_address:
            raise Exception('owner_change_address or from_address is required for messaging channel issuance.')
        owner_token_change_output = (
            resolved_owner_change_address,
            {
                'transfer': {
                    _owner_token_name(normalized_asset_name): float(Decimal(str(owner_change_quantity))),
                }
            }
        )

    return create_and_send_asset_operation_transaction(
        from_address=from_address,
        operation_address=issuer_address,
        operation_payload=operation_payload,
        burn_amount_evr=burn_amount,
        burn_address=burn_address,
        authorization_asset_name=authorization_asset_name,
        owner_token_change_output=owner_token_change_output,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
        wif_keys=wif_keys,
        evr_change_address=evr_change_address,
    )


def create_and_send_issue_unique_transaction(
    from_address,
    issuer_address,
    root_name,
    asset_tags,
    ipfs_hashes=None,
    owner_change_address=None,
    owner_change_quantity=1,
    burn_per_tag_evr=Decimal('5'),
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
    wif_keys=None,
):
    if not asset_tags:
        raise Exception('asset_tags must contain at least one unique tag.')

    normalized_tags = [str(tag) for tag in asset_tags]
    normalized_ipfs_hashes = [str(item) for item in (ipfs_hashes or [])]
    if len(normalized_tags) == 1 and len(normalized_ipfs_hashes) <= 1:
        operation_payload = {
            '_issue_new_asset': {
                'asset_name': f"{str(root_name)}#{normalized_tags[0]}",
                'asset_quantity': 1.0,
                'units': 0,
                'reissuable': 0,
                'has_ipfs': int(bool(normalized_ipfs_hashes)),
            }
        }
        if normalized_ipfs_hashes:
            operation_payload['_issue_new_asset']['ipfs_hash'] = normalized_ipfs_hashes[0]
    else:
        operation_payload = {
            'issue_unique': {
                'root_name': str(root_name),
                'asset_tags': normalized_tags,
            }
        }
        if normalized_ipfs_hashes:
            operation_payload['issue_unique']['ipfs_hashes'] = normalized_ipfs_hashes

    owner_change_output = None
    if owner_change_address:
        owner_change_output = (
            owner_change_address,
            {
                'transfer': {
                    _owner_token_name(root_name): float(Decimal(str(owner_change_quantity))),
                }
            }
        )

    burn_total = Decimal(str(burn_per_tag_evr)) * Decimal(str(len(asset_tags)))

    return create_and_send_asset_operation_transaction(
        from_address=from_address,
        operation_address=issuer_address,
        operation_payload=operation_payload,
        burn_amount_evr=burn_total,
        burn_address=_resolve_burn_address('issue_unique_asset'),
        authorization_asset_name=_owner_token_name(root_name),
        owner_token_change_output=owner_change_output,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
        wif_keys=wif_keys,
    )


def create_and_send_reissue_asset_transaction(
    from_address,
    issuer_address,
    asset_name,
    asset_quantity,
    reissuable=True,
    ipfs_hash='',
    owner_change_address=None,
    owner_change_quantity=1,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
):
    operation_payload = {
        'reissue': {
            'asset_name': str(asset_name),
            'asset_quantity': float(Decimal(str(asset_quantity))),
            'reissuable': int(bool(reissuable)),
        }
    }
    if ipfs_hash:
        operation_payload['reissue']['ipfs_hash'] = str(ipfs_hash)
    if owner_change_address:
        operation_payload['reissue']['owner_change_address'] = str(owner_change_address)

    owner_change_output = None
    if owner_change_address:
        owner_change_output = (
            owner_change_address,
            {
                'transfer': {
                    _owner_token_name(asset_name): float(Decimal(str(owner_change_quantity))),
                }
            }
        )

    return create_and_send_asset_operation_transaction(
        from_address=from_address,
        operation_address=issuer_address,
        operation_payload=operation_payload,
        burn_amount_evr=Decimal('100'),
        burn_address=BURN_ADDRESS_REISSUE_ASSET,
        authorization_asset_name=_owner_token_name(asset_name),
        owner_token_change_output=owner_change_output,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
    )


def create_and_send_issue_restricted_transaction(
    from_address,
    issuer_address,
    asset_name,
    asset_quantity,
    verifier_string,
    units,
    reissuable,
    has_ipfs,
    ipfs_hash='',
    owner_change_address=None,
    owner_change_quantity=1,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
):
    operation_payload = {
        'issue_restricted': {
            'asset_name': str(asset_name),
            'asset_quantity': float(Decimal(str(asset_quantity))),
            'verifier_string': str(verifier_string),
            'units': int(units),
            'reissuable': int(bool(reissuable)),
            'has_ipfs': int(bool(has_ipfs)),
        }
    }
    if has_ipfs:
        operation_payload['issue_restricted']['ipfs_hash'] = str(ipfs_hash)
    if owner_change_address:
        operation_payload['issue_restricted']['owner_change_address'] = str(owner_change_address)

    owner_change_output = None
    if owner_change_address:
        owner_change_output = (
            owner_change_address,
            {
                'transfer': {
                    _owner_token_name(asset_name): float(Decimal(str(owner_change_quantity))),
                }
            }
        )

    return create_and_send_asset_operation_transaction(
        from_address=from_address,
        operation_address=issuer_address,
        operation_payload=operation_payload,
        burn_amount_evr=Decimal('1500'),
        burn_address=BURN_ADDRESS_ISSUE_RESTRICTED,
        authorization_asset_name=_owner_token_name(asset_name),
        owner_token_change_output=owner_change_output,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
    )


def create_and_send_reissue_restricted_transaction(
    from_address,
    issuer_address,
    asset_name,
    asset_quantity,
    reissuable=True,
    verifier_string='',
    ipfs_hash='',
    owner_change_address=None,
    owner_change_quantity=1,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
):
    operation_payload = {
        'reissue_restricted': {
            'asset_name': str(asset_name),
            'asset_quantity': float(Decimal(str(asset_quantity))),
            'reissuable': int(bool(reissuable)),
        }
    }
    if verifier_string:
        operation_payload['reissue_restricted']['verifier_string'] = str(verifier_string)
    if ipfs_hash:
        operation_payload['reissue_restricted']['ipfs_hash'] = str(ipfs_hash)
    if owner_change_address:
        operation_payload['reissue_restricted']['owner_change_address'] = str(owner_change_address)

    owner_change_output = None
    if owner_change_address:
        owner_change_output = (
            owner_change_address,
            {
                'transfer': {
                    _owner_token_name(asset_name): float(Decimal(str(owner_change_quantity))),
                }
            }
        )

    return create_and_send_asset_operation_transaction(
        from_address=from_address,
        operation_address=issuer_address,
        operation_payload=operation_payload,
        burn_amount_evr=Decimal('100'),
        burn_address=BURN_ADDRESS_REISSUE_ASSET,
        authorization_asset_name=_owner_token_name(asset_name),
        owner_token_change_output=owner_change_output,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
    )


def create_and_send_issue_qualifier_transaction(
    from_address,
    issuer_address,
    asset_name,
    asset_quantity=1,
    has_ipfs=False,
    ipfs_hash='',
    root_change_address=None,
    change_quantity=1,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
):
    operation_payload = {
        'issue_qualifier': {
            'asset_name': str(asset_name),
            'asset_quantity': float(Decimal(str(asset_quantity))),
            'has_ipfs': int(bool(has_ipfs)),
        }
    }
    if has_ipfs:
        operation_payload['issue_qualifier']['ipfs_hash'] = str(ipfs_hash)
    if root_change_address:
        operation_payload['issue_qualifier']['root_change_address'] = str(root_change_address)
    if change_quantity is not None:
        operation_payload['issue_qualifier']['change_quantity'] = float(Decimal(str(change_quantity)))

    is_sub = _is_subqualifier(asset_name)
    burn_amount = Decimal('100') if is_sub else Decimal('1000')
    burn_address = BURN_ADDRESS_ISSUE_SUBQUALIFIER if is_sub else BURN_ADDRESS_ISSUE_QUALIFIER
    auth_asset = _root_qualifier_name(asset_name) if is_sub else None

    owner_change_output = None
    if is_sub and root_change_address:
        owner_change_output = (
            root_change_address,
            {
                'transfer': {
                    _root_qualifier_name(asset_name): float(Decimal(str(change_quantity or 1))),
                }
            }
        )

    return create_and_send_asset_operation_transaction(
        from_address=from_address,
        operation_address=issuer_address,
        operation_payload=operation_payload,
        burn_amount_evr=burn_amount,
        burn_address=burn_address,
        authorization_asset_name=auth_asset,
        owner_token_change_output=owner_change_output,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
    )


def create_and_send_tag_addresses_transaction(
    from_address,
    qualifier_change_address,
    qualifier,
    addresses,
    change_quantity=1,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
):
    if not addresses:
        raise Exception('addresses must include at least one address.')

    operation_payload = {
        'tag_addresses': {
            'qualifier': str(qualifier),
            'addresses': [str(address) for address in addresses],
            'change_quantity': float(Decimal(str(change_quantity))),
        }
    }

    burn_amount = Decimal('0.1') * Decimal(str(len(addresses)))
    owner_change_output = (
        qualifier_change_address,
        {
            'transfer': {
                str(qualifier): float(Decimal(str(change_quantity))),
            }
        }
    )

    return create_and_send_asset_operation_transaction(
        from_address=from_address,
        operation_address=qualifier_change_address,
        operation_payload=operation_payload,
        burn_amount_evr=burn_amount,
        burn_address=BURN_ADDRESS_TAG,
        authorization_asset_name=str(qualifier),
        owner_token_change_output=owner_change_output,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
    )


def create_and_send_untag_addresses_transaction(
    from_address,
    qualifier_change_address,
    qualifier,
    addresses,
    change_quantity=1,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
):
    if not addresses:
        raise Exception('addresses must include at least one address.')

    operation_payload = {
        'untag_addresses': {
            'qualifier': str(qualifier),
            'addresses': [str(address) for address in addresses],
            'change_quantity': float(Decimal(str(change_quantity))),
        }
    }

    burn_amount = Decimal('0.1') * Decimal(str(len(addresses)))
    owner_change_output = (
        qualifier_change_address,
        {
            'transfer': {
                str(qualifier): float(Decimal(str(change_quantity))),
            }
        }
    )

    return create_and_send_asset_operation_transaction(
        from_address=from_address,
        operation_address=qualifier_change_address,
        operation_payload=operation_payload,
        burn_amount_evr=burn_amount,
        burn_address=BURN_ADDRESS_TAG,
        authorization_asset_name=str(qualifier),
        owner_token_change_output=owner_change_output,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
    )


def _get_restricted_asset_authority_evidence(
    rpc_client,
    restricted_asset_name,
    required_verifier_string,
    required_qualifier_name,
    authority_address,
    minimum_restricted_asset_balance=Decimal('1'),
):
    """Return chain evidence that an address is eligible to authorize a restricted workflow."""
    asset_name = str(restricted_asset_name or '').strip().upper()
    verifier_string = str(required_verifier_string or '').strip()
    qualifier_name = str(required_qualifier_name or '').strip().upper()
    address = str(authority_address or '').strip()
    minimum_balance = Decimal(str(minimum_restricted_asset_balance or 0))

    if not asset_name.startswith('$'):
        raise ValueError('Restricted authority assets must use the $ asset-name prefix.')
    if not verifier_string:
        raise ValueError('A restricted authority verifier string is required.')
    if not qualifier_name.startswith('#'):
        raise ValueError('Restricted authority qualifiers must use the # asset-name prefix.')
    if not address:
        raise ValueError('A restricted authority address is required.')
    if minimum_balance <= 0:
        raise ValueError('The minimum restricted authority balance must be greater than zero.')

    on_chain_verifier = str(rpc_client.getverifierstring(asset_name) or '').strip()
    has_qualifier = bool(rpc_client.checkaddresstag(address, qualifier_name))
    balances = rpc_client.listassetbalancesbyaddress(address) or {}
    restricted_balance = Decimal(str((balances or {}).get(asset_name, 0) or 0))
    verifier_matches = on_chain_verifier == verifier_string
    has_minimum_balance = restricted_balance >= minimum_balance

    return {
        'restricted_asset_name': asset_name,
        'authority_address': address,
        'required_verifier_string': verifier_string,
        'on_chain_verifier_string': on_chain_verifier,
        'required_qualifier_name': qualifier_name,
        'has_qualifier': has_qualifier,
        'restricted_asset_balance': format(restricted_balance, 'f'),
        'minimum_restricted_asset_balance': format(minimum_balance, 'f'),
        'verifier_matches': verifier_matches,
        'has_minimum_balance': has_minimum_balance,
        'is_authorized': verifier_matches and has_qualifier and has_minimum_balance,
    }


def get_restricted_asset_authority_evidence(
    restricted_asset_name,
    required_verifier_string,
    required_qualifier_name,
    authority_address,
    minimum_restricted_asset_balance=Decimal('1'),
):
    """Return restricted-authority evidence using the active routed RPC client."""
    return _get_restricted_asset_authority_evidence(
        RPC,
        restricted_asset_name,
        required_verifier_string,
        required_qualifier_name,
        authority_address,
        minimum_restricted_asset_balance,
    )


def get_public_restricted_asset_authority_evidence(
    restricted_asset_name,
    required_verifier_string,
    required_qualifier_name,
    authority_address,
    minimum_restricted_asset_balance=Decimal('1'),
    network_mode='testnet',
):
    """Return restricted-authority evidence directly from the configured public endpoint."""
    normalized_network = str(network_mode or 'testnet').strip().lower()
    if normalized_network == 'mainnet':
        endpoint = getattr(
            settings,
            'EVRMORE_PUBLIC_RPC_MAINNET_URL',
            'https://evr-rpc-mainnet.evrmorecoin.org/rpc',
        )
    elif normalized_network == 'testnet':
        endpoint = getattr(
            settings,
            'EVRMORE_PUBLIC_RPC_TESTNET_URL',
            'https://evr-rpc-testnet.evrmorecoin.org/rpc',
        )
    else:
        raise ValueError('Restricted authority evidence requires a supported Evrmore network.')

    return _get_restricted_asset_authority_evidence(
        PublicRpcClient(
            endpoint,
            timeout=getattr(settings, 'RPC_PUBLIC_TIMEOUT_SECONDS', 10),
        ),
        restricted_asset_name,
        required_verifier_string,
        required_qualifier_name,
        authority_address,
        minimum_restricted_asset_balance,
    )


def get_public_transaction_evidence(
    transaction_id,
    network_mode='testnet',
    minimum_confirmations=1,
):
    """Return direct public-RPC evidence that a transaction is confirmed on the selected network."""
    txid = str(transaction_id or '').strip().lower()
    if len(txid) != 64 or any(character not in '0123456789abcdef' for character in txid):
        raise ValueError('A canonical 64-character hexadecimal transaction id is required.')
    try:
        required_confirmations = int(minimum_confirmations)
    except (TypeError, ValueError) as exc:
        raise ValueError('Minimum transaction confirmations must be a whole number.') from exc
    if required_confirmations < 1:
        raise ValueError('At least one transaction confirmation is required.')

    normalized_network = str(network_mode or 'testnet').strip().lower()
    if normalized_network == 'mainnet':
        endpoint = getattr(
            settings,
            'EVRMORE_PUBLIC_RPC_MAINNET_URL',
            'https://evr-rpc-mainnet.evrmorecoin.org/rpc',
        )
    elif normalized_network == 'testnet':
        endpoint = getattr(
            settings,
            'EVRMORE_PUBLIC_RPC_TESTNET_URL',
            'https://evr-rpc-testnet.evrmorecoin.org/rpc',
        )
    else:
        raise ValueError('Transaction evidence requires a supported Evrmore network.')

    transaction_data = PublicRpcClient(
        endpoint,
        timeout=getattr(settings, 'RPC_PUBLIC_TIMEOUT_SECONDS', 10),
    ).getrawtransaction(txid, True)
    if not isinstance(transaction_data, dict):
        raise ValueError('Public RPC returned invalid transaction evidence.')
    observed_txid = str(transaction_data.get('txid') or '').strip().lower()
    if observed_txid != txid:
        raise ValueError('Public RPC transaction evidence did not match the requested transaction id.')
    try:
        confirmations = int(transaction_data.get('confirmations') or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError('Public RPC transaction evidence contained invalid confirmations.') from exc
    if confirmations < required_confirmations:
        raise ValueError(
            f'Transaction {txid} has {confirmations} confirmations; '
            f'at least {required_confirmations} are required.'
        )

    return {
        'transaction_id': txid,
        'confirmations': confirmations,
        'block_hash': str(transaction_data.get('blockhash') or ''),
        'block_time': transaction_data.get('blocktime'),
        'transaction_time': transaction_data.get('time'),
    }


def create_and_send_freeze_addresses_transaction(
    from_address,
    owner_change_address,
    asset_name,
    addresses,
    owner_change_quantity=1,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
):
    operation_payload = {
        'freeze_addresses': {
            'asset_name': str(asset_name),
            'addresses': [str(address) for address in addresses],
        }
    }

    owner_change_output = (
        owner_change_address,
        {
            'transfer': {
                _owner_token_name(asset_name): float(Decimal(str(owner_change_quantity))),
            }
        }
    )

    return create_and_send_asset_operation_transaction(
        from_address=from_address,
        operation_address=owner_change_address,
        operation_payload=operation_payload,
        authorization_asset_name=_owner_token_name(asset_name),
        owner_token_change_output=owner_change_output,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
    )


def create_and_send_unfreeze_addresses_transaction(
    from_address,
    owner_change_address,
    asset_name,
    addresses,
    owner_change_quantity=1,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
):
    operation_payload = {
        'unfreeze_addresses': {
            'asset_name': str(asset_name),
            'addresses': [str(address) for address in addresses],
        }
    }

    owner_change_output = (
        owner_change_address,
        {
            'transfer': {
                _owner_token_name(asset_name): float(Decimal(str(owner_change_quantity))),
            }
        }
    )

    return create_and_send_asset_operation_transaction(
        from_address=from_address,
        operation_address=owner_change_address,
        operation_payload=operation_payload,
        authorization_asset_name=_owner_token_name(asset_name),
        owner_token_change_output=owner_change_output,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
    )


def create_and_send_freeze_asset_transaction(
    from_address,
    owner_change_address,
    asset_name,
    owner_change_quantity=1,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
):
    operation_payload = {
        'freeze_asset': {
            'asset_name': str(asset_name),
        }
    }

    owner_change_output = (
        owner_change_address,
        {
            'transfer': {
                _owner_token_name(asset_name): float(Decimal(str(owner_change_quantity))),
            }
        }
    )

    return create_and_send_asset_operation_transaction(
        from_address=from_address,
        operation_address=owner_change_address,
        operation_payload=operation_payload,
        authorization_asset_name=_owner_token_name(asset_name),
        owner_token_change_output=owner_change_output,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
    )


def create_and_send_unfreeze_asset_transaction(
    from_address,
    owner_change_address,
    asset_name,
    owner_change_quantity=1,
    fee_evr=None,
    fee_conf_target=DEFAULT_FEE_CONF_TARGET,
    fee_estimate_mode=DEFAULT_FEE_ESTIMATE_MODE,
    locktime=0,
    replaceable=False,
):
    operation_payload = {
        'unfreeze_asset': {
            'asset_name': str(asset_name),
        }
    }

    owner_change_output = (
        owner_change_address,
        {
            'transfer': {
                _owner_token_name(asset_name): float(Decimal(str(owner_change_quantity))),
            }
        }
    )

    return create_and_send_asset_operation_transaction(
        from_address=from_address,
        operation_address=owner_change_address,
        operation_payload=operation_payload,
        authorization_asset_name=_owner_token_name(asset_name),
        owner_token_change_output=owner_change_output,
        fee_evr=fee_evr,
        fee_conf_target=fee_conf_target,
        fee_estimate_mode=fee_estimate_mode,
        locktime=locktime,
        replaceable=replaceable,
    )

def create_raw_transaction(inputs, outputs, locktime=0, replaceable=False):
    """
    Create a raw transaction on the Evrmore network.
    
    Args:
        inputs (list): List of dicts with 'txid' and 'vout' keys
                       Example: [{"txid": "...", "vout": 0}]
        outputs (dict): Dict mapping addresses to amounts or operation objects
                Example: {"EVR_ADDRESS": 0.5}
        locktime (int): Optional locktime value
        replaceable (bool): Optional BIP125 RBF flag
    
    Returns:
        str: Raw transaction hex string
    
    Raises:
        Exception: If RPC call fails
    """
    try:
        rpc_outputs = OrderedDict()
        script_replacements = []
        local_asset_script_replacements = []
        address_occurrences = {}

        for address, payload in _output_entries(outputs):
            occurrence = address_occurrences.get(address, 0)
            address_occurrences[address] = occurrence + 1

            rpc_address = address
            if occurrence:
                rpc_address = _temporary_output_address(address, occurrence)
                while rpc_address in rpc_outputs:
                    occurrence += 1
                    rpc_address = _temporary_output_address(address, occurrence)
                script_replacements.append((rpc_address, address))

            if isinstance(payload, dict) and (
                'issue_qualifier' in payload or '_issue_new_asset' in payload
            ):
                rpc_outputs[rpc_address] = _evr_output_value(0)
                asset_data = payload.get('issue_qualifier') or payload.get('_issue_new_asset')
                local_asset_script_replacements.append((rpc_address, asset_data))
            else:
                rpc_outputs[rpc_address] = payload

        if locktime or replaceable:
            raw_tx = RPC.createrawtransaction(inputs, rpc_outputs, locktime, replaceable)
        else:
            raw_tx = RPC.createrawtransaction(inputs, rpc_outputs)

        raw_tx = str(raw_tx).lower()
        for asset_address, asset_data in local_asset_script_replacements:
            placeholder_script = _p2pkh_script_pub_key(asset_address)
            asset_script = _new_asset_script(asset_address, asset_data)
            placeholder_output = (_compact_size(len(placeholder_script)) + placeholder_script).hex()
            asset_output = (_compact_size(len(asset_script)) + asset_script).hex()
            if raw_tx.count(placeholder_output) != 1:
                raise Exception(
                    f'Unable to locate asset placeholder output for address {asset_address}.'
                )
            raw_tx = raw_tx.replace(placeholder_output, asset_output)

        for temporary_address, source_address in script_replacements:
            temporary_hash = _p2pkh_hash160(temporary_address).hex()
            source_hash = _p2pkh_hash160(source_address).hex()
            if raw_tx.count(temporary_hash) < 1:
                raise Exception(
                    f'Unable to locate temporary output destination for source address {source_address}.'
                )
            raw_tx = raw_tx.replace(temporary_hash, source_hash)

        return raw_tx
    except Exception as e:
        raise Exception(f"Failed to create raw transaction: {str(e)}")
    
