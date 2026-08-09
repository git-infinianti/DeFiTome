from django.utils import timezone

from .models import SwapFundingLock, SwapOffer


def purge_expired_swap_offers(*, network_mode=None, limit=500):
    """Delete expired swap offers that are no longer eligible for settlement."""
    now = timezone.now()
    queryset = SwapOffer.objects.filter(
        expires_at__lte=now,
        status__in=['pending', 'expired'],
    ).exclude(
        status__in=['settling', 'completed', 'cancelled', 'rejected'],
    ).order_by('expires_at', 'id')

    if network_mode:
        queryset = queryset.filter(network_mode=network_mode)

    purged = 0
    for swap_offer in queryset[:max(1, int(limit or 500))]:
        SwapFundingLock.objects.filter(
            swap_offer=swap_offer,
            status='locked',
        ).update(status='released', released_at=now)
        swap_offer.delete()
        purged += 1

    return purged