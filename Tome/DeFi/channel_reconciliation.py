"""Deterministic, fail-closed projection checks for atomic-swap channel events."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from API.models import (
    ChannelConsumer,
    ChannelEvent,
    ChannelEventApplication,
    ChannelReconciliationIssue,
    ChannelSubscription,
    ChannelSubscriptionCursor,
)

from .models import SwapOffer


ATOMIC_SWAP_EVENT_TYPE = 'atomic_swap_transfer'
ATOMIC_SWAP_AGGREGATE_TYPE = 'atomic_swap'
ATOMIC_SWAP_STAGE_STATUS = {
    'offer_created': SwapOffer.STATUS_CHOICES[0][0],
    'settlement_lock_created': 'settling',
    'settlement_build_failed': 'pending',
    'settlement_pending_reconciliation': 'settling',
    'settlement_broadcasted': 'completed',
    'swap_cancelled': 'cancelled',
    'swap_expired': 'expired',
}


def _open_issue(policy, event, code, detail, severity='error'):
    existing = ChannelReconciliationIssue.objects.filter(
        policy=policy,
        event=event,
        code=code,
        status=ChannelReconciliationIssue.STATUS_OPEN,
    ).first()
    if existing is not None:
        return existing
    return ChannelReconciliationIssue.objects.create(
        policy=policy,
        event=event,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        code=code,
        severity=severity,
        detail=detail,
    )


def _application_for(event):
    return ChannelEventApplication.objects.filter(
        event=event,
        projection_type=ATOMIC_SWAP_AGGREGATE_TYPE,
        projection_key=event.aggregate_id,
    ).first()


def _store_blocked_application(event, code, detail, severity='error'):
    application = ChannelEventApplication.objects.create(
        event=event,
        projection_type=ATOMIC_SWAP_AGGREGATE_TYPE,
        projection_key=event.aggregate_id,
        status=ChannelEventApplication.STATUS_BLOCKED,
        result={'code': code},
        error_message=str(detail.get('message') or code),
    )
    _open_issue(event.policy, event, code, detail, severity=severity)
    return application


def _store_already_applied_application(event, result):
    return ChannelEventApplication.objects.create(
        event=event,
        projection_type=ATOMIC_SWAP_AGGREGATE_TYPE,
        projection_key=event.aggregate_id,
        status=ChannelEventApplication.STATUS_ALREADY_APPLIED,
        result=result,
    )


def _event_terms_match_offer(event, offer):
    details = event.payload.get('details') or {}
    offer_terms = details.get('offer') or {}
    request_terms = details.get('request') or {}
    try:
        offer_amount_matches = Decimal(str(offer_terms.get('amount'))) == Decimal(str(offer.offer_amount))
        request_amount_matches = Decimal(str(request_terms.get('amount'))) == Decimal(str(offer.request_amount))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return (
        str(offer_terms.get('token') or '').strip().upper() == str(offer.offer_token or '').strip().upper()
        and offer_amount_matches
        and str(request_terms.get('token') or '').strip().upper() == str(offer.request_token or '').strip().upper()
        and request_amount_matches
    )


def _sequence_is_contiguous(event):
    if event.aggregate_sequence == 1:
        return True
    return ChannelEvent.objects.filter(
        policy=event.policy,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        aggregate_sequence=event.aggregate_sequence - 1,
        verification_status=ChannelEvent.VERIFICATION_VERIFIED,
    ).exists()


def apply_atomic_swap_event(event):
    """Verify that a known swap projection agrees with one canonical channel event.

    This first vertical slice deliberately does not create missing offers, locks, or
    completed settlements. Those mutations require participant identity and raw
    settlement-output verification that legacy payloads do not yet provide.
    """
    with transaction.atomic():
        existing = _application_for(event)
        if existing is not None:
            return existing

        if event.verification_status != ChannelEvent.VERIFICATION_VERIFIED:
            return _store_blocked_application(
                event,
                'event_not_verified',
                {'message': 'Only verified channel events can advance an atomic-swap projection.'},
                severity='critical',
            )
        if (
            event.event_type != ATOMIC_SWAP_EVENT_TYPE
            or event.aggregate_type != ATOMIC_SWAP_AGGREGATE_TYPE
        ):
            return _store_blocked_application(
                event,
                'unsupported_atomic_event',
                {'message': 'The channel event is not a canonical atomic-swap event.'},
                severity='critical',
            )

        expected_status = ATOMIC_SWAP_STAGE_STATUS.get(event.stage)
        if expected_status is None:
            return _store_blocked_application(
                event,
                'illegal_atomic_stage',
                {'message': f'Atomic swap stage {event.stage!r} is not reconcilable.'},
                severity='critical',
            )

        if not _sequence_is_contiguous(event):
            return _store_blocked_application(
                event,
                'atomic_swap_sequence_gap',
                {
                    'message': 'The preceding verified event for this atomic swap is unavailable.',
                    'aggregate_sequence': event.aggregate_sequence,
                },
                severity='critical',
            )

        offer = SwapOffer.objects.select_for_update().filter(
            reconciliation_id=event.aggregate_id,
            network_mode=event.network_mode,
        ).first()
        if offer is None:
            return _store_blocked_application(
                event,
                'unknown_atomic_swap',
                {
                    'message': 'No local swap projection has this reconciliation identifier.',
                    'aggregate_id': event.aggregate_id,
                },
                severity='critical',
            )
        if not _event_terms_match_offer(event, offer):
            return _store_blocked_application(
                event,
                'atomic_swap_terms_mismatch',
                {
                    'message': 'Channel event terms do not match the local swap projection.',
                    'swap_offer_id': offer.pk,
                },
                severity='critical',
            )

        if event.stage == 'settlement_broadcasted':
            transaction_id = str(event.payload.get('transaction_id') or '').strip().lower()
            if not transaction_id or transaction_id != str(offer.settlement_txid or '').strip().lower():
                return _store_blocked_application(
                    event,
                    'settlement_transaction_mismatch',
                    {
                        'message': 'Settlement event transaction id does not match the local swap projection.',
                        'swap_offer_id': offer.pk,
                    },
                    severity='critical',
                )
            return _store_blocked_application(
                event,
                'settlement_term_verification_required',
                {
                    'message': 'Raw settlement-output verification is required before completion can be accepted.',
                    'swap_offer_id': offer.pk,
                },
                severity='critical',
            )

        if offer.status != expected_status:
            return _store_blocked_application(
                event,
                'atomic_swap_status_divergence',
                {
                    'message': 'Channel stage does not match the local swap projection status.',
                    'swap_offer_id': offer.pk,
                    'expected_status': expected_status,
                    'actual_status': offer.status,
                },
                severity='critical',
            )

        return _store_already_applied_application(
            event,
            {
                'swap_offer_id': offer.pk,
                'status': offer.status,
                'stage': event.stage,
            },
        )


def _events_after_cursor(cursor):
    events = ChannelEvent.objects.filter(
        policy=cursor.subscription.policy,
        verification_status=ChannelEvent.VERIFICATION_VERIFIED,
        event_type=ATOMIC_SWAP_EVENT_TYPE,
    ).order_by('block_height', 'block_transaction_index', 'channel_output_index')
    if cursor.last_seen_height is None:
        return events

    return events.filter(
        Q(block_height__gt=cursor.last_seen_height)
        | Q(
            block_height=cursor.last_seen_height,
            block_transaction_index__gt=cursor.last_seen_transaction_index or 0,
        )
        | Q(
            block_height=cursor.last_seen_height,
            block_transaction_index=cursor.last_seen_transaction_index or 0,
            channel_output_index__gt=cursor.last_seen_output_index or 0,
        )
    )


def reconcile_atomic_swap_subscription(subscription, consumer):
    if subscription.status != ChannelSubscription.STATUS_ACTIVE:
        raise ValueError('Only active channel subscriptions can be reconciled.')
    if consumer.network_mode != subscription.policy.network_mode:
        raise ValueError('Consumer network must match the subscribed channel policy.')

    cursor, _created = ChannelSubscriptionCursor.objects.get_or_create(
        subscription=subscription,
        consumer=consumer,
    )
    report = {
        'subscription_id': subscription.pk,
        'consumer_id': consumer.pk,
        'processed': 0,
        'already_applied': 0,
        'blocked': 0,
    }

    for event in _events_after_cursor(cursor):
        application = apply_atomic_swap_event(event)
        report['processed'] += 1
        if application.status == ChannelEventApplication.STATUS_BLOCKED:
            report['blocked'] += 1
            cursor.status = ChannelSubscriptionCursor.STATUS_ERROR
            cursor.error_message = application.error_message
            cursor.last_reconciled_at = timezone.now()
            cursor.save(update_fields=['status', 'error_message', 'last_reconciled_at', 'updated_at'])
            break

        report['already_applied'] += 1
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
            'last_event',
            'last_seen_txid',
            'last_seen_height',
            'last_seen_transaction_index',
            'last_seen_output_index',
            'last_seen_event_id',
            'status',
            'error_message',
            'last_reconciled_at',
            'updated_at',
        ])
    else:
        if cursor.status != ChannelSubscriptionCursor.STATUS_ERROR:
            cursor.status = ChannelSubscriptionCursor.STATUS_SYNCED
            cursor.error_message = ''
            cursor.last_reconciled_at = timezone.now()
            cursor.save(update_fields=['status', 'error_message', 'last_reconciled_at', 'updated_at'])

    return report