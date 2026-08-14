import json

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from API.channel_reconciliation import ChannelHistoryUnavailable, ingest_channel_history
from API.models import ChannelConsumer, ChannelSubscription, MessageChannelPolicy
from DeFi.channel_reconciliation import reconcile_atomic_swap_subscription


class Command(BaseCommand):
    help = (
        'Ingest verified channel-asset history and reconcile atomic-swap projections '
        'for one user or every active subscriber.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--channel', required=True, help='Verified message-channel policy key.')
        parser.add_argument('--network', required=True, choices=['testnet', 'mainnet'])
        parser.add_argument('--user', help='Optional username to subscribe and reconcile.')
        parser.add_argument('--consumer-key', default='server', help='Stable key for the consuming node or deployment.')
        parser.add_argument('--consumer-name', default='Server channel consumer')
        parser.add_argument(
            '--ingest-only',
            action='store_true',
            help='Persist verified channel observations without applying any local projection.',
        )

    def _resolve_policy(self, channel_key, network_mode):
        policy = MessageChannelPolicy.objects.filter(
            channel_key=str(channel_key).strip().lower(),
            network_mode=network_mode,
            status='active',
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        ).order_by('-version').first()
        if policy is None:
            raise CommandError('No active channel policy with verified on-chain metadata matches this key and network.')
        return policy

    def _resolve_subscriptions(self, policy, username):
        if username:
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist as exc:
                raise CommandError(f'User {username!r} does not exist.') from exc
            subscription, _created = ChannelSubscription.objects.get_or_create(
                user=user,
                policy=policy,
                defaults={
                    'role': ChannelSubscription.ROLE_OBSERVER,
                    'status': ChannelSubscription.STATUS_ACTIVE,
                },
            )
            if subscription.status != ChannelSubscription.STATUS_ACTIVE:
                raise CommandError('The requested user subscription is paused.')
            return [subscription]

        subscriptions = list(ChannelSubscription.objects.filter(
            policy=policy,
            status=ChannelSubscription.STATUS_ACTIVE,
        ).select_related('user'))
        if not subscriptions:
            raise CommandError('No active subscriptions exist; provide --user to create an observer subscription.')
        return subscriptions

    def handle(self, *args, **options):
        policy = self._resolve_policy(options['channel'], options['network'])

        try:
            ingestion = ingest_channel_history(policy)
        except ChannelHistoryUnavailable as exc:
            raise CommandError(str(exc)) from exc
        if ingestion['invalid']:
            raise CommandError(
                f"Channel history ingestion recorded {ingestion['invalid']} invalid observation(s); "
                'no projections were reconciled.'
            )

        subscription_reports = []
        if not options['ingest_only']:
            consumer_key = str(options['consumer_key']).strip()
            if not consumer_key:
                raise CommandError('A non-empty consumer key is required for projection reconciliation.')
            consumer, _created = ChannelConsumer.objects.get_or_create(
                network_mode=policy.network_mode,
                consumer_key=consumer_key,
                defaults={'display_name': str(options['consumer_name']).strip() or 'Channel consumer'},
            )
            if not consumer.is_active:
                raise CommandError('The selected channel consumer is inactive.')
            for subscription in self._resolve_subscriptions(policy, options.get('user')):
                subscription_reports.append(reconcile_atomic_swap_subscription(subscription, consumer))

        self.stdout.write(self.style.SUCCESS(json.dumps({
            'policy_id': policy.pk,
            'channel_key': policy.channel_key,
            'channel_name': policy.channel_name,
            'network_mode': policy.network_mode,
            'ingestion': ingestion,
            'ingest_only': options['ingest_only'],
            'subscriptions': subscription_reports,
        }, sort_keys=True)))