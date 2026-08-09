import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run unique mint reconciliation once or on a periodic interval.'

    def add_arguments(self, parser):
        parser.add_argument('--network', choices=['mainnet', 'testnet'], help='Optional network filter.')
        parser.add_argument('--limit', type=int, default=200, help='Maximum records to scan per pass.')
        parser.add_argument('--interval-seconds', type=int, default=0, help='Run repeatedly with this delay between passes.')

    def handle(self, *args, **options):
        from Listings.reconciliation import reconcile_unique_mint_requests

        network = options.get('network')
        limit = max(1, int(options.get('limit') or 200))
        interval_seconds = max(0, int(options.get('interval_seconds') or 0))

        while True:
            checked, updated = reconcile_unique_mint_requests(network=network, limit=limit)
            self.stdout.write(self.style.SUCCESS(f'Checked {checked} mint request(s); updated {updated}.'))

            if interval_seconds <= 0:
                break

            time.sleep(interval_seconds)