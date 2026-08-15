"""Fail-closed DEC projections from validated public message-channel events."""

from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from API.models import (
    ChannelConsumer,
    ChannelEvent,
    ChannelEventApplication,
    ChannelReconciliationIssue,
    ChannelSubscription,
    ChannelSubscriptionCursor,
)
from API.rpc import evrmore_rpc
from Tome.rpc_client import using_network_mode
from Wallet.models import WalletAddress

from .models import DecPokerGameInstance, DecPokerHand, DecPokerPayoutLedgerEntry


DEC_EVENT_TYPE = 'dec_game_event'
DEC_PROJECTION_TYPE = 'dec_channel'
DEC_INSTANCE_AGGREGATE_TYPE = 'dec_poker_game_instance'
DEC_HAND_AGGREGATE_TYPE = 'dec_poker_hand'
DEC_POLICY_AGGREGATE_TYPE = 'dec_poker_payout_policy'
DEC_STAGES = {
    'game_instance_created',
    'payout_policy_published',
    'game_spend_recorded',
    'game_reward_distributed',
}


def user_holds_dec_channel_token(user, policy):
    """Return public balance evidence for the active policy's channel asset."""
    addresses = WalletAddress.objects.filter(
        wallet__user=user,
        network_mode=policy.network_mode,
    ).values_list('address', flat=True).distinct()
    evidence = []
    total = Decimal('0')
    with using_network_mode(policy.network_mode):
        for address in addresses:
            balances = evrmore_rpc.list_asset_balances_by_address(address) or {}
            try:
                balance = Decimal(str(balances.get(policy.channel_name, 0) or 0))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError('Channel balance evidence contains an invalid quantity.') from exc
            if balance > 0:
                evidence.append({'address': address, 'balance': format(balance, 'f')})
                total += balance
    return {
        'is_holder': total > 0,
        'channel_name': policy.channel_name,
        'total_balance': format(total, 'f'),
        'addresses': evidence,
    }


def _open_issue(event, code, message, detail=None):
    issue, _created = ChannelReconciliationIssue.objects.get_or_create(
        policy=event.policy,
        event=event,
        code=code,
        status=ChannelReconciliationIssue.STATUS_OPEN,
        defaults={
            'aggregate_type': event.aggregate_type,
            'aggregate_id': event.aggregate_id,
            'severity': ChannelReconciliationIssue.SEVERITY_CRITICAL,
            'detail': {'message': message, **(detail or {})},
        },
    )
    return issue


def _application(event):
    return ChannelEventApplication.objects.filter(
        event=event,
        projection_type=DEC_PROJECTION_TYPE,
        projection_key=event.aggregate_id,
    ).first()


def _event_evidence(event):
    return {
        'event_id': event.event_id,
        'txid': event.channel_txid,
        'cid': event.payload_ipfs_cid,
        'checksum': event.payload_checksum,
        'height': event.block_height,
        'stage': event.stage,
    }


def _store_application(event, status, *, result=None, error=''):
    return ChannelEventApplication.objects.create(
        event=event,
        projection_type=DEC_PROJECTION_TYPE,
        projection_key=event.aggregate_id,
        status=status,
        result=result or {},
        error_message=error,
    )


def _reject(event, instance, code, message, *, hand=None, detail=None):
    evidence = _event_evidence(event)
    instance.reconciliation_status = DecPokerGameInstance.RECONCILIATION_STATUS_REJECTED
    instance.reconciliation_error = message
    instance.reconciliation_evidence = evidence
    instance.is_active = False
    instance.save(update_fields=[
        'reconciliation_status',
        'reconciliation_error',
        'reconciliation_evidence',
        'is_active',
        'updated_at',
    ])
    if hand is not None:
        hand.reconciliation_status = DecPokerHand.RECONCILIATION_STATUS_REJECTED
        hand.reconciliation_error = message
        hand.reconciliation_evidence = evidence
        hand.settlement_status = DecPokerHand.SETTLEMENT_STATUS_FAILED
        hand.settlement_error = message
        hand.save(update_fields=[
            'reconciliation_status',
            'reconciliation_error',
            'reconciliation_evidence',
            'settlement_status',
            'settlement_error',
        ])
    _open_issue(event, code, message, detail)
    return _store_application(
        event,
        ChannelEventApplication.STATUS_BLOCKED,
        result={'code': code, 'evidence': evidence},
        error=message,
    )


def _decimal_equal(left, right):
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _event_context(event):
    details = event.payload.get('details')
    if not isinstance(details, dict):
        return None, None
    game = details.get('game_instance')
    context = details.get('context')
    if not isinstance(game, dict) or not isinstance(context, dict):
        return None, None
    return game, context


def _instance_mismatches(instance, context):
    comparisons = {
        'reward_asset_name': str(context.get('reward_asset_name') or '').upper() == instance.reward_asset_name.upper(),
        'reward_supply': _decimal_equal(context.get('reward_supply'), instance.reward_supply),
        'reward_units': str(context.get('reward_units')) == str(instance.reward_asset_units),
        'entry_fee_evr': _decimal_equal(context.get('entry_fee_evr'), instance.entry_fee_evr),
        'instance_fee_evr': _decimal_equal(context.get('instance_fee_evr'), instance.instance_fee_evr),
        'issue_txid': str(context.get('issue_txid') or '') == instance.reward_issue_txid,
        'reward_transfer_txid': str(context.get('reward_transfer_txid') or '') == instance.owner_transfer_txid,
    }
    return sorted(key for key, matches in comparisons.items() if not matches)


def _hand_mismatches(hand, event, context):
    policy = hand.payout_policy
    ledger_event_type = (
        DecPokerPayoutLedgerEntry.EVENT_WAGER_SPEND_SETTLED
        if event.stage == 'game_spend_recorded'
        else DecPokerPayoutLedgerEntry.EVENT_REWARD_PAYOUT_SETTLED
    )
    ledger = hand.payout_ledger_entries.filter(event_type=ledger_event_type).first()
    if ledger is None:
        return ['ledger_entry']

    comparisons = {
        'server_seed_hash': str(context.get('server_seed_hash') or '') == hand.server_seed_hash,
        'fairness_nonce': str(context.get('fairness_nonce')) == str(hand.fairness_nonce),
        'payout_policy_version': policy is not None and str(context.get('payout_policy_version')) == str(policy.version),
        'payout_policy_hash': policy is not None and str(context.get('payout_policy_hash') or '') == policy.policy_hash,
        'ledger_entry_hash': str(context.get('ledger_entry_hash') or '') == ledger.entry_hash,
    }
    if event.stage == 'game_spend_recorded':
        comparisons.update({
            'wager_evr': _decimal_equal(context.get('wager_evr'), hand.wager_evr),
            'spend_txid': str(context.get('spend_txid') or '') == hand.spend_txid,
            'client_seed': str(context.get('client_seed') or '') == hand.client_seed,
            'treasury_amount_evr': _decimal_equal(
                context.get('treasury_amount_evr'), ledger.event_data.get('treasury_amount_evr')
            ),
            'vault_amount_evr': _decimal_equal(
                context.get('vault_amount_evr'), ledger.event_data.get('vault_amount_evr')
            ),
        })
    else:
        comparisons.update({
            'reward_asset_name': str(context.get('reward_asset_name') or '').upper() == hand.reward_asset_name.upper(),
            'reward_amount': _decimal_equal(context.get('reward_amount'), hand.reward_amount),
            'reward_txid': str(context.get('reward_txid') or '') == hand.reward_txid,
        })
    return sorted(key for key, matches in comparisons.items() if not matches)


def apply_dec_channel_event(event):
    """Compare one canonical DEC event to local state and reject any divergence."""
    with transaction.atomic():
        existing = _application(event)
        if existing is not None:
            return existing
        if (
            event.verification_status != ChannelEvent.VERIFICATION_VERIFIED
            or event.event_type != DEC_EVENT_TYPE
            or event.stage not in DEC_STAGES
        ):
            return _store_application(
                event,
                ChannelEventApplication.STATUS_BLOCKED,
                result={'code': 'unsupported_dec_event'},
                error='The channel event is not a verified canonical DEC event.',
            )

        game, context = _event_context(event)
        try:
            instance_id = int((game or {}).get('id'))
        except (TypeError, ValueError):
            instance_id = None
        instance = DecPokerGameInstance.objects.select_for_update().filter(
            pk=instance_id,
            network_mode=event.network_mode,
            channel_policy=event.policy,
        ).first()
        if instance is None:
            _open_issue(event, 'unknown_dec_instance', 'No local DEC instance matches this channel event.')
            return _store_application(
                event,
                ChannelEventApplication.STATUS_BLOCKED,
                result={'code': 'unknown_dec_instance'},
                error='No local DEC instance matches this channel event.',
            )
        if instance.reconciliation_status == DecPokerGameInstance.RECONCILIATION_STATUS_REJECTED:
            return _store_application(
                event,
                ChannelEventApplication.STATUS_BLOCKED,
                result={'code': 'dec_instance_already_rejected'},
                error=instance.reconciliation_error,
            )

        if event.aggregate_type == DEC_INSTANCE_AGGREGATE_TYPE and event.stage == 'game_instance_created':
            mismatches = _instance_mismatches(instance, context)
            policy = instance.payout_policies.filter(version=context.get('payout_policy_version')).first()
            if policy is None or policy.policy_hash != str(context.get('payout_policy_hash') or ''):
                mismatches.append('payout_policy')
            if mismatches:
                return _reject(
                    event, instance, 'dec_instance_conflict',
                    'Channel instance evidence conflicts with local DEC state.',
                    detail={'mismatched_fields': sorted(set(mismatches))},
                )
        elif event.aggregate_type == DEC_POLICY_AGGREGATE_TYPE and event.stage == 'payout_policy_published':
            policy = instance.payout_policies.filter(version=context.get('payout_policy_version')).first()
            mismatches = []
            if policy is None or policy.policy_hash != str(context.get('payout_policy_hash') or ''):
                mismatches.append('payout_policy')
            if policy is not None and policy.market_valuation is not None:
                if policy.market_valuation.valuation_hash != str(context.get('valuation_hash') or ''):
                    mismatches.append('valuation_hash')
            if mismatches:
                return _reject(
                    event, instance, 'dec_policy_conflict',
                    'Channel payout-policy evidence conflicts with local DEC state.',
                    detail={'mismatched_fields': mismatches},
                )
        elif event.aggregate_type == DEC_HAND_AGGREGATE_TYPE and event.stage in {
            'game_spend_recorded', 'game_reward_distributed',
        }:
            hand = DecPokerHand.objects.select_for_update().filter(
                settlement_id=event.aggregate_id,
                game_instance=instance,
            ).first()
            if hand is None:
                return _reject(
                    event, instance, 'unknown_dec_hand',
                    'Channel hand evidence has no matching local DEC hand.',
                )
            if event.stage == 'game_reward_distributed':
                spend_applied = ChannelEventApplication.objects.filter(
                    event__policy=event.policy,
                    event__aggregate_id=event.aggregate_id,
                    event__stage='game_spend_recorded',
                    projection_type=DEC_PROJECTION_TYPE,
                    status__in=[
                        ChannelEventApplication.STATUS_APPLIED,
                        ChannelEventApplication.STATUS_ALREADY_APPLIED,
                    ],
                ).exists()
                if not spend_applied:
                    return _reject(
                        event, instance, 'dec_stage_transition_conflict',
                        'A DEC reward event cannot be applied before its spend event.', hand=hand,
                    )
            mismatches = _hand_mismatches(hand, event, context)
            if mismatches:
                return _reject(
                    event, instance, 'dec_hand_conflict',
                    'Channel hand evidence conflicts with local DEC settlement state.',
                    hand=hand,
                    detail={'mismatched_fields': mismatches},
                )
            evidence = dict(hand.reconciliation_evidence or {})
            evidence[event.stage] = _event_evidence(event)
            hand.reconciliation_evidence = evidence
            hand.reconciliation_status = (
                DecPokerHand.RECONCILIATION_STATUS_SYNCED
                if event.stage == 'game_reward_distributed' or hand.reward_amount <= 0
                else DecPokerHand.RECONCILIATION_STATUS_PENDING
            )
            hand.reconciliation_error = ''
            hand.save(update_fields=[
                'reconciliation_evidence', 'reconciliation_status', 'reconciliation_error'
            ])
        else:
            return _reject(
                event, instance, 'illegal_dec_stage',
                'The DEC aggregate type and stage combination is illegal.',
            )

        evidence = dict(instance.reconciliation_evidence or {})
        evidence[event.stage] = _event_evidence(event)
        instance.reconciliation_evidence = evidence
        instance.reconciliation_status = DecPokerGameInstance.RECONCILIATION_STATUS_SYNCED
        instance.reconciliation_error = ''
        instance.save(update_fields=[
            'reconciliation_evidence', 'reconciliation_status',
            'reconciliation_error', 'updated_at',
        ])
        return _store_application(
            event,
            ChannelEventApplication.STATUS_APPLIED,
            result={'instance_id': instance.pk, 'stage': event.stage, 'evidence': _event_evidence(event)},
        )


def reconcile_dec_policy_events(policy):
    report = {'processed': 0, 'applied': 0, 'blocked': 0}
    events = ChannelEvent.objects.filter(
        policy=policy,
        event_type=DEC_EVENT_TYPE,
        verification_status=ChannelEvent.VERIFICATION_VERIFIED,
    ).order_by('block_height', 'block_transaction_index', 'channel_output_index', 'event_id')
    for event in events:
        application = apply_dec_channel_event(event)
        report['processed'] += 1
        if application.status == ChannelEventApplication.STATUS_BLOCKED:
            report['blocked'] += 1
        else:
            report['applied'] += 1
    return report


def reconcile_dec_subscription(subscription, consumer):
    if subscription.status != ChannelSubscription.STATUS_ACTIVE:
        raise ValueError('Only active DEC channel subscriptions can be reconciled.')
    if consumer.network_mode != subscription.policy.network_mode:
        raise ValueError('Consumer network must match the DEC channel policy.')

    cursor, _created = ChannelSubscriptionCursor.objects.get_or_create(
        subscription=subscription,
        consumer=consumer,
    )
    report = {'processed': 0, 'applied': 0, 'blocked': 0}
    events = ChannelEvent.objects.filter(
        policy=subscription.policy,
        event_type=DEC_EVENT_TYPE,
        verification_status=ChannelEvent.VERIFICATION_VERIFIED,
    ).order_by('block_height', 'block_transaction_index', 'channel_output_index', 'event_id')
    if cursor.last_seen_height is not None:
        events = events.filter(block_height__gte=cursor.last_seen_height)

    for event in events:
        coordinate = (
            event.block_height,
            event.block_transaction_index,
            event.channel_output_index,
        )
        cursor_coordinate = (
            cursor.last_seen_height or 0,
            cursor.last_seen_transaction_index or 0,
            cursor.last_seen_output_index or 0,
        )
        if cursor.last_seen_height is not None and coordinate <= cursor_coordinate:
            continue
        application = apply_dec_channel_event(event)
        report['processed'] += 1
        if application.status == ChannelEventApplication.STATUS_BLOCKED:
            report['blocked'] += 1
            cursor.status = ChannelSubscriptionCursor.STATUS_ERROR
            cursor.error_message = application.error_message
            cursor.last_reconciled_at = timezone.now()
            cursor.save(update_fields=['status', 'error_message', 'last_reconciled_at', 'updated_at'])
            break

        report['applied'] += 1
        cursor.last_event = event
        cursor.last_seen_txid = event.channel_txid
        cursor.last_seen_height = event.block_height
        cursor.last_seen_transaction_index = event.block_transaction_index
        cursor.last_seen_output_index = event.channel_output_index
        cursor.last_seen_event_id = event.event_id
        cursor.status = ChannelSubscriptionCursor.STATUS_SYNCED
        cursor.error_message = ''
        cursor.last_reconciled_at = timezone.now()
        cursor.save(update_fields=[
            'last_event', 'last_seen_txid', 'last_seen_height',
            'last_seen_transaction_index', 'last_seen_output_index',
            'last_seen_event_id', 'status', 'error_message',
            'last_reconciled_at', 'updated_at',
        ])
    else:
        cursor.status = ChannelSubscriptionCursor.STATUS_SYNCED
        cursor.error_message = ''
        cursor.last_reconciled_at = timezone.now()
        cursor.save(update_fields=['status', 'error_message', 'last_reconciled_at', 'updated_at'])
    return report