from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from django.db import transaction

from API.models import MessageChannelPolicy
from API.channel_console_lib import DEFAULT_ALLOWED_STAGES
from API.channel_console_service import validate_channel_console_asset
from API.unified_workflow_policy import (
    UNIFIED_WORKFLOW_CHANNEL_KEY,
    UNIFIED_WORKFLOW_POLICY_VERSION,
)


def _ensure_user(username, email, is_staff=False, is_superuser=False):
    user, _ = User.objects.get_or_create(
        username=username,
        defaults={
            'email': email,
            'is_active': True,
            'is_staff': is_staff,
            'is_superuser': is_superuser,
        },
    )
    if not user.has_usable_password():
        user.set_unusable_password()
        user.save(update_fields=['password'])
    return user


class Command(BaseCommand):
    help = (
        'Bootstrap strict, versioned message channel policies for atomic-swap transfer events '
        'owned by system and managed by admin account.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--system-username', default='system')
        parser.add_argument('--admin-username', default='admin')
        parser.add_argument('--network-mode', default='testnet', choices=['testnet', 'mainnet'])
        parser.add_argument('--channel-name', required=True)
        parser.add_argument('--channel-key', default=UNIFIED_WORKFLOW_CHANNEL_KEY)
        parser.add_argument('--policy-version', type=int, default=UNIFIED_WORKFLOW_POLICY_VERSION)
        parser.add_argument('--auto-broadcast', action='store_true')
        parser.add_argument('--issue-channel-asset', action='store_true')
        parser.add_argument('--issue-qty', type=float, default=1.0)
        parser.add_argument('--to-address', default='')
        parser.add_argument('--change-address', default='')

    def handle(self, *args, **options):
        network_mode = options['network_mode']
        channel_key = str(options['channel_key']).strip().lower()
        channel_name = str(options['channel_name']).strip().upper()
        is_unified_workflow_v5 = (
            channel_key == UNIFIED_WORKFLOW_CHANNEL_KEY
            and options['policy_version'] == UNIFIED_WORKFLOW_POLICY_VERSION
        )

        if options.get('issue_channel_asset'):
            raise CommandError(
                'Bootstrap issuance is disabled because messaging channels require raw issuance with IPFS metadata. '
                'Create the channel through the messaging-channel creation wizard, then bootstrap its verified asset.'
            )
        if is_unified_workflow_v5 and options.get('auto_broadcast'):
            raise CommandError('Unified workflow v5 requires auto_broadcast to remain false.')

        try:
            validation = validate_channel_console_asset(channel_name, network_mode=network_mode)
        except Exception as exc:
            raise CommandError(f'Channel asset validation failed: {exc}') from exc
        channel_metadata = validation['metadata']
        metadata_channel_key = str(channel_metadata.get('channel_key') or '').strip().lower()
        if metadata_channel_key != channel_key:
            raise CommandError(
                f'Channel key mismatch: metadata binds {channel_name} to {metadata_channel_key or "an empty key"}.'
            )

        system_user = _ensure_user(
            username=options['system_username'],
            email='system@defitome.local',
            is_staff=True,
            is_superuser=False,
        )
        admin_user = _ensure_user(
            username=options['admin_username'],
            email='admin@defitome.local',
            is_staff=True,
            is_superuser=True,
        )

        strict_rules = dict(channel_metadata.get('strict_rules') or {})
        strict_rules['auto_broadcast'] = bool(options['auto_broadcast'])

        try:
            with transaction.atomic():
                policy, created = MessageChannelPolicy.objects.update_or_create(
                    channel_key=channel_key,
                    network_mode=network_mode,
                    version=options['policy_version'],
                    defaults={
                        'channel_name': channel_name,
                        'status': 'draft' if is_unified_workflow_v5 else 'active',
                        'owner_account': system_user,
                        'manager_account': admin_user,
                        'schema_name': 'defitome.atomic-swap-transfer-message',
                        'schema_version': 1,
                        'allowed_stages': channel_metadata.get('allowed_stages') or DEFAULT_ALLOWED_STAGES,
                        'strict_rules': strict_rules,
                        'metadata_ipfs_cid': validation['ipfs_cid'],
                        'chain_metadata_status': MessageChannelPolicy.CHAIN_METADATA_STATUS_VERIFIED,
                        'chain_metadata_error': '',
                        'is_locked': True,
                    },
                )
                if is_unified_workflow_v5:
                    validate_channel_console_asset(channel_name, network_mode=network_mode)
                    policy.refresh_from_db()
        except Exception as exc:
            raise CommandError(f'Channel policy activation failed: {exc}') from exc

        action = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f'{action} message channel policy: key={policy.channel_key} '
            f'network={policy.network_mode} version={policy.version} owner={system_user.username} manager={admin_user.username}'
        ))
        self.stdout.write(f'Checksum: {policy.rules_checksum}')

