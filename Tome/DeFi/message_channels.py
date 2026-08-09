import json
from decimal import Decimal

from API.message_channel_lib import (
    build_market_event_payload,
    build_swap_transfer_payload,
    payload_checksum,
    validate_console_payload,
    validate_market_event_payload,
)
from API.models import AtomicSwapTransferMessage, DexMarketEventMessage, MessageChannelPolicy
from Media.kubo_api import KuboAPIUploader
from Tome.rpc_client import RPC, using_network_mode
from Wallet.models import WalletAddress, WalletProfile
from Wallet.rpc import create_and_send_transfer_with_message_transaction


ATOMIC_SWAP_CHANNEL_KEY = 'atomic_swap_transfer'
ATOMIC_SWAP_REQUIRED_STAGES = (
    'offer_created',
    'settlement_lock_created',
    'settlement_build_failed',
    'settlement_pending_reconciliation',
    'settlement_broadcasted',
    'swap_cancelled',
    'swap_expired',
)


def get_active_atomic_swap_policy(network_mode, required_stages=()):
    required = {str(stage).strip().lower() for stage in required_stages if str(stage).strip()}
    policies = MessageChannelPolicy.objects.filter(
        channel_key=ATOMIC_SWAP_CHANNEL_KEY,
        network_mode=network_mode,
        status='active',
        chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
    ).order_by('-version')
    for policy in policies:
        allowed = {str(stage).strip().lower() for stage in (policy.allowed_stages or [])}
        if required.issubset(allowed):
            return policy
    return None


def _get_channel_signer(policy):
    with using_network_mode(policy.network_mode):
        return _get_channel_signer_for_active_network(policy)


def _get_channel_signer_for_active_network(policy):
    candidates = []
    for account in (policy.manager_account, policy.owner_account):
        if account and account.pk not in {item.pk for item in candidates}:
            candidates.append(account)

    for account in candidates:
        user_wallet = getattr(account, 'user_wallet', None)
        if not user_wallet:
            continue

        profile = WalletProfile.objects.select_related('address').filter(
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
        if not address_record:
            continue

        balances = RPC.listassetbalancesbyaddress(address_record.address) or {}
        if Decimal(str(balances.get(policy.channel_name, 0))) >= Decimal('1'):
            return address_record.address, address_record.wif

    raise ValueError(f'No policy owner or manager address holds message channel {policy.channel_name}.')


def _broadcast_channel_message(message, policy, payload, file_name):
    with using_network_mode(policy.network_mode):
        return _broadcast_channel_message_for_active_network(message, policy, payload, file_name)


def _broadcast_channel_message_for_active_network(message, policy, payload, file_name):
    try:
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        upload_result = KuboAPIUploader().upload_bytes(
            payload_bytes,
            file_name=file_name,
            pin=True,
            cid_version=0,
        )
        channel_address, channel_wif = _get_channel_signer(policy)
        broadcast_result = create_and_send_transfer_with_message_transaction(
            from_address=channel_address,
            to_address=channel_address,
            asset_name=policy.channel_name,
            asset_quantity=Decimal('1'),
            message=upload_result.cid,
            expire_time=0,
            wif_keys=[channel_wif],
        )
        message.payload_ipfs_cid = upload_result.cid
        message.broadcast_result = str(broadcast_result.get('txid') or '')
        message.status = 'broadcasted'
        message.save(update_fields=['payload_ipfs_cid', 'broadcast_result', 'status'])
    except Exception as exc:
        message.status = 'failed'
        message.error_message = str(exc)
        message.save(update_fields=['status', 'error_message'])

    return message


def record_atomic_swap_stage_event(
    swap_offer,
    stage,
    actor_username,
    actor_user=None,
    txid='',
    details=None,
    should_broadcast=None,
):
    payload = build_swap_transfer_payload(
        swap_offer=swap_offer,
        stage=stage,
        actor_username=actor_username,
        txid=txid,
        details=details,
    )

    policy = get_active_atomic_swap_policy(
        str(swap_offer.network_mode or 'testnet').lower(),
        required_stages=(stage,),
    )
    if policy:
        validate_console_payload(payload, policy.allowed_stages)

    if should_broadcast is None:
        should_broadcast = policy is not None

    checksum = payload_checksum(payload)
    message = AtomicSwapTransferMessage.objects.create(
        policy=policy,
        swap_offer=swap_offer,
        stage=payload['stage'],
        payload=payload,
        payload_checksum=checksum,
        created_by=actor_user,
        status='recorded',
    )

    if not should_broadcast or not policy:
        return message

    return _broadcast_channel_message(
        message,
        policy,
        payload,
        f'swap-{swap_offer.id}-{payload["stage"]}.json',
    )


def record_market_stage_event(trading_pair, stage, actor_user, order=None, details=None):
    payload = build_market_event_payload(
        trading_pair,
        stage=stage,
        actor_username=getattr(actor_user, 'username', ''),
        order=order,
        details=details,
    )
    policies = [
        policy
        for policy in MessageChannelPolicy.objects.filter(
            network_mode=trading_pair.network_mode,
            status='active',
        ).order_by('channel_key', '-version')
        if payload['stage'] in {
            str(item).strip().lower() for item in (policy.allowed_stages or [])
        }
    ]

    if not policies:
        return [DexMarketEventMessage.objects.create(
            trading_pair_id=trading_pair.id,
            order_id=getattr(order, 'id', None),
            stage=payload['stage'],
            payload=payload,
            payload_checksum=payload_checksum(payload),
            created_by=actor_user,
            status='recorded',
        )]

    messages = []
    for policy in policies:
        policy_payload = dict(payload)
        validate_market_event_payload(policy_payload, policy.allowed_stages)
        message = DexMarketEventMessage.objects.create(
            policy=policy,
            trading_pair_id=trading_pair.id,
            order_id=getattr(order, 'id', None),
            stage=policy_payload['stage'],
            payload=policy_payload,
            payload_checksum=payload_checksum(policy_payload),
            created_by=actor_user,
            status='recorded',
        )
        messages.append(_broadcast_channel_message(
            message,
            policy,
            policy_payload,
            f'market-{trading_pair.id}-{getattr(order, "id", "pair")}-{policy_payload["stage"]}.json',
        ))

    return messages
