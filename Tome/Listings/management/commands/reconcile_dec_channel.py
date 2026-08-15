import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from API.channel_reconciliation import ChannelHistoryUnavailable, ingest_channel_history
from API.models import ChannelConsumer, ChannelSubscription, MessageChannelPolicy
from Listings.channel_reconciliation import (
    reconcile_dec_subscription,
    user_holds_dec_channel_token,
)


DEFAULT_DEC_CHANNEL_KEY = 'tome0808_swapflow'


class Command(BaseCommand):
    help = 'Ingest public DEC channel history and reconcile local state for verified token holders.'

    def add_arguments(self, parser):
        parser.add_argument('--channel-key', default=DEFAULT_DEC_CHANNEL_KEY)
        parser.add_argument('--network', default='testnet', choices=['testnet', 'mainnet'])
        parser.add_argument('--user', help='Reconcile one token-holding user by username.')
        parser.add_argument('--consumer-key', default='dec-server')
        parser.add_argument('--consumer-name', default='DEC channel consumer')
        parser.add_argument(
            '--full',
            action='store_true',
            help='Replay from issuance and reset selected subscription cursors after ingestion.',
        )
        parser.add_argument(
            '--ingest-only',
            action='store_true',
            help='Persist public channel events without applying subscriber projections.',
        )

    def _policy(self, channel_key, network_mode):
        policy = MessageChannelPolicy.objects.filter(
            channel_key=str(channel_key).strip().lower(),
            network_mode=network_mode,
            status='active',
            chain_metadata_status=MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
        ).order_by('-version').first()
        if policy is None:
            raise CommandError('No active verified DEC channel policy matches this key and network.')
        return policy

    def _users(self, policy, username):
        User = get_user_model()
        if username:
            try:
                return [User.objects.get(username=username)]
            except User.DoesNotExist as exc:
                raise CommandError(f'User {username!r} does not exist.') from exc
        users = list(User.objects.filter(
            channel_subscriptions__policy=policy,
            channel_subscriptions__status=ChannelSubscription.STATUS_ACTIVE,
        ).distinct())
        if not users:
            raise CommandError('No active DEC subscribers exist; provide --user or use --ingest-only.')
        return users

    def handle(self, *args, **options):
        policy = self._policy(options['channel_key'], options['network'])
        try:
            ingestion = ingest_channel_history(policy)
        except ChannelHistoryUnavailable as exc:
            raise CommandError(str(exc)) from exc
        if ingestion['invalid']:
            raise CommandError(
                f"Channel history contains {ingestion['invalid']} invalid observation(s); reconciliation refused."
            )

        reports = []
        if not options['ingest_only']:
            consumer, _created = ChannelConsumer.objects.get_or_create(
                network_mode=policy.network_mode,
                consumer_key=str(options['consumer_key']).strip(),
                defaults={'display_name': str(options['consumer_name']).strip()},
            )
            if not consumer.is_active:
                raise CommandError('The selected DEC channel consumer is inactive.')

            for user in self._users(policy, options.get('user')):
                holder = user_holds_dec_channel_token(user, policy)
                if not holder['is_holder']:
                    raise CommandError(
                        f'User {user.username!r} does not hold active channel asset {policy.channel_name}.'
                    )
                subscription, _created = ChannelSubscription.objects.get_or_create(
                    user=user,
                    policy=policy,
                    defaults={
                        'role': ChannelSubscription.ROLE_PARTICIPANT,
                        'status': ChannelSubscription.STATUS_ACTIVE,
                    },
                )
                if subscription.status != ChannelSubscription.STATUS_ACTIVE:
                    raise CommandError(f'User {user.username!r} has a paused DEC subscription.')
                if options['full']:
                    subscription.cursors.filter(consumer=consumer).delete()
                reports.append({
                    'user': user.username,
                    'holder': holder,
                    'reconciliation': reconcile_dec_subscription(subscription, consumer),
                })

        self.stdout.write(self.style.SUCCESS(json.dumps({
            'policy_id': policy.pk,
            'channel_key': policy.channel_key,
            'channel_name': policy.channel_name,
            'network_mode': policy.network_mode,
            'full': options['full'],
            'ingest_only': options['ingest_only'],
            'ingestion': ingestion,
            'subscribers': reports,
        }, sort_keys=True)))