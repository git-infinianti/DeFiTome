from decimal import Decimal, ROUND_DOWN

from django.db import migrations


SATOSHIS_PER_EVR = Decimal('100000000')
BALANCE_FIELDS = (
    'evr_liquidity',
    'evr_liquidity_mainnet',
    'evr_liquidity_testnet',
)


def normalize_cached_balances(apps, schema_editor):
    table_name = schema_editor.connection.ops.quote_name('Wallet_userwallet')
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f'SELECT id, {", ".join(BALANCE_FIELDS)} FROM {table_name}'
        )
        rows = cursor.fetchall()

        for row in rows:
            normalized = [
                (Decimal(str(value or 0)) / SATOSHIS_PER_EVR).quantize(
                    Decimal('0.00000001'),
                    rounding=ROUND_DOWN,
                )
                for value in row[1:]
            ]
            assignments = ', '.join(f'{field} = %s' for field in BALANCE_FIELDS)
            cursor.execute(
                f'UPDATE {table_name} SET {assignments} WHERE id = %s',
                [*normalized, row[0]],
            )


class Migration(migrations.Migration):
    dependencies = [
        ('Wallet', '0017_walletpreferences_nft_image_uri_template'),
    ]

    operations = [
        migrations.RunPython(normalize_cached_balances, migrations.RunPython.noop),
    ]