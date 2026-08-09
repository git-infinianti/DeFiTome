from django.core.management.base import BaseCommand

from DeFi.cleanup import purge_expired_swap_offers


class Command(BaseCommand):
    help = 'Purge expired atomic swap offers and release any lingering settlement locks.'

    def add_arguments(self, parser):
        parser.add_argument('--network', choices=['mainnet', 'testnet'], help='Optional network filter.')
        parser.add_argument('--limit', type=int, default=500, help='Maximum records to purge.')

    def handle(self, *args, **options):
        network = options.get('network')
        limit = max(1, int(options.get('limit') or 500))

        purged = purge_expired_swap_offers(network_mode=network, limit=limit)
        self.stdout.write(self.style.SUCCESS(f'Purged {purged} expired swap offer(s).'))