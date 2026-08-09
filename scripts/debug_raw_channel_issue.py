import json
from collections import OrderedDict

import django
import requests
from django.conf import settings


def main():
    django.setup()
    from django.contrib.auth.models import User
    from Tome.rpc_client import set_active_network_mode, clear_active_network_mode
    from API.channel_console_service import _get_primary_wallet_address_and_wif
    from Wallet.rpc import (
        _select_inputs_for_operation,
        compose_asset_operation_outputs,
        _resolve_fee_satoshis,
        _resolve_burn_address,
        _satoshis_to_evr,
        _find_authorization_input,
        _get_address_utxos,
    )
    user = User.objects.get(username='admin')
    set_active_network_mode('testnet')
    try:
        from_address, _wif = _get_primary_wallet_address_and_wif(
            user,
            'testnet',
            required_asset_name='TST0806065044!',
        )
        final_fee_sats = _resolve_fee_satoshis(None, 2, 3)
        selected_inputs, selected_total = _select_inputs_for_operation(
            from_address=from_address,
            required_evr_satoshis=50000000000 + final_fee_sats,
            authorization_asset_name='TST0806065044!',
            locktime=0,
            replaceable=False,
        )
        auth_utxos = _get_address_utxos(from_address, 'TST0806065044!')
        generic_utxos = _get_address_utxos(from_address)
        auth_input = _find_authorization_input(generic_utxos, 'TST0806065044!', address=from_address)

        coin_outputs = OrderedDict()
        coin_outputs[_resolve_burn_address('issue_msg_channel_asset')] = _satoshis_to_evr(50000000000)
        extra_coin_satoshis = 50000000000
        change_satoshis = selected_total - final_fee_sats - extra_coin_satoshis
        if change_satoshis >= 546:
            coin_outputs[from_address] = _satoshis_to_evr(change_satoshis)

        outputs = compose_asset_operation_outputs(
            coin_outputs=coin_outputs,
            operation_address=from_address,
            operation_payload={
                'issue': {
                    'asset_name': 'TST0806065044~DBG04',
                    'asset_quantity': 1.0,
                    'units': 0,
                    'reissuable': 0,
                    'has_ipfs': 0,
                    'remintable': 0,
                }
            },
            owner_token_change_output=(
                from_address,
                {
                    'transfer': {
                        'TST0806065044!': 1.0,
                    }
                },
            ),
        )

        url = (
            getattr(settings, 'RPC_TESTNET_URL', None)
            or f"{getattr(settings, 'RPC_TESTNET_SCHEME', 'http')}://"
               f"{getattr(settings, 'RPC_TESTNET_HOST')}:{getattr(settings, 'RPC_TESTNET_PORT')}"
               f"{getattr(settings, 'RPC_TESTNET_PATH', '/rpc')}"
        )
        auth = (
            getattr(settings, 'RPC_TESTNET_USER', None),
            getattr(settings, 'RPC_TESTNET_PASSWORD', None),
        )
        payload = {
            'jsonrpc': '1.0',
            'id': 'diag',
            'method': 'createrawtransaction',
            'params': [selected_inputs, outputs],
        }
        response = requests.post(url, json=payload, timeout=30, auth=auth)

        print('FROM', from_address)
        print('AUTH_UTXOS', json.dumps(auth_utxos, default=str, indent=2))
        print('AUTH_INPUT', json.dumps(auth_input, default=str, indent=2))
        print('INPUTS', json.dumps(selected_inputs, default=str, indent=2))
        print('OUTPUTS', json.dumps(outputs, default=str, indent=2))
        print('STATUS', response.status_code)
        print('BODY', response.text)
    finally:
        clear_active_network_mode()


if __name__ == '__main__':
    main()
