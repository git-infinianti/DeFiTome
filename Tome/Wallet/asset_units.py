from decimal import Decimal, InvalidOperation, ROUND_DOWN

from Tome.rpc_client import get_current_network_mode

from API.rpc import evrmore_rpc as rpc_client
from .models import TrackedAsset


def amount_quantum_for_units(units):
    normalized_units = max(0, min(8, int(units or 0)))
    return Decimal('1').scaleb(-normalized_units)


def get_asset_units(symbol, network_mode=None, default_units=8):
    normalized_symbol = str(symbol or '').strip().upper()
    if normalized_symbol == 'EVR':
        return 8

    resolved_network_mode = network_mode or get_current_network_mode()

    try:
        asset_data = rpc_client.get_asset_data(normalized_symbol)
    except Exception:
        asset_data = None

    if isinstance(asset_data, dict):
        try:
            return max(0, min(8, int(asset_data.get('units', 8))))
        except (TypeError, ValueError):
            pass

    tracked_asset = TrackedAsset.objects.filter(
        symbol=normalized_symbol,
        network_mode=resolved_network_mode,
    ).only('units').first()
    if tracked_asset is not None:
        return max(0, min(8, int(tracked_asset.units or 0)))

    if isinstance(asset_data, dict):
        try:
            return max(0, min(8, int(asset_data.get('units', default_units or 8))))
        except (TypeError, ValueError):
            pass

    return max(0, min(8, int(default_units or 8)))


def get_tracked_asset_units(symbol, network_mode, default_units=8):
    return get_asset_units(symbol, network_mode=network_mode, default_units=default_units)


def normalize_amount_for_asset(raw_amount, symbol, network_mode=None, *, field_label='amount', strict=False):
    try:
        amount = Decimal(str(raw_amount).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'Invalid {field_label} specified.')

    if amount <= 0:
        raise ValueError(f'{field_label.capitalize()} must be greater than zero.')

    units = get_asset_units(symbol, network_mode=network_mode)
    quantum = amount_quantum_for_units(units)
    normalized = amount.quantize(quantum, rounding=ROUND_DOWN)
    if strict and normalized != amount:
        if int(units or 0) == 0:
            raise ValueError(f'{symbol} is indivisible and must be sent as a whole number.')
        raise ValueError(
            f'{field_label.capitalize()} exceeds the allowed precision for {symbol}. '
            f'Maximum decimal places: {int(units)}.'
        )

    return normalized