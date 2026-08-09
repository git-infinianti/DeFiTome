from django.utils import timezone

from Explorer.rpc import RPC
from Tome.rpc_client import clear_active_network_mode, set_active_network_mode

from .models import UniqueAssetMintRequest


def reconcile_unique_mint_requests(*, network=None, limit=200):
    queryset = UniqueAssetMintRequest.objects.filter(
        status__in=[
            UniqueAssetMintRequest.STATUS_PENDING,
            UniqueAssetMintRequest.STATUS_BROADCAST,
        ]
    ).exclude(mint_txid='').order_by('-created_at')

    if network:
        queryset = queryset.filter(network_mode=network)

    checked = 0
    updated = 0
    now = timezone.now()

    if network:
        set_active_network_mode(network)

    try:
        for mint_request in queryset[:max(1, int(limit or 200))]:
            checked += 1
            try:
                tx_data = RPC.gettransaction(mint_request.mint_txid)
                confirmations = int((tx_data or {}).get('confirmations', 0))
                next_status = (
                    UniqueAssetMintRequest.STATUS_CONFIRMED
                    if confirmations > 0
                    else UniqueAssetMintRequest.STATUS_BROADCAST
                )

                changed = False
                if mint_request.confirmation_depth != confirmations:
                    mint_request.confirmation_depth = confirmations
                    changed = True
                if mint_request.status != next_status:
                    mint_request.status = next_status
                    changed = True
                if mint_request.error_message:
                    mint_request.error_message = ''
                    changed = True

                mint_request.last_checked_at = now
                if changed:
                    updated += 1
                    mint_request.save(update_fields=['confirmation_depth', 'status', 'error_message', 'last_checked_at', 'updated_at'])
                else:
                    mint_request.save(update_fields=['last_checked_at', 'updated_at'])
            except Exception as exc:
                mint_request.last_checked_at = now
                mint_request.error_message = str(exc)
                mint_request.save(update_fields=['last_checked_at', 'error_message', 'updated_at'])
    finally:
        if network:
            clear_active_network_mode()

    return checked, updated