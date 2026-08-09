from django.core.management.base import BaseCommand

from Listings.reconciliation import reconcile_unique_mint_requests


class Command(BaseCommand):
    help = 'Refresh confirmation status for unique mint requests.'

    def add_arguments(self, parser):
        parser.add_argument('--network', choices=['mainnet', 'testnet'], help='Optional network filter.')
        parser.add_argument('--limit', type=int, default=200, help='Maximum records to scan.')

    def handle(self, *args, **options):
        network = options.get('network')
        limit = max(1, int(options.get('limit') or 200))

        checked, updated = reconcile_unique_mint_requests(network=network, limit=limit)

        self.stdout.write(self.style.SUCCESS(f'Checked {checked} mint request(s); updated {updated}.'))
