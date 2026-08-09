from django.db import migrations


def classify_restricted_markets(apps, schema_editor):
    trading_pair = apps.get_model('Listings', 'TradingPair')
    trading_pair.objects.filter(base_token__startswith='$').update(
        instrument_type='security_capable',
    )


class Migration(migrations.Migration):
    dependencies = [
        ('Listings', '0011_tradingpair_instrument_type_and_asset_name_length'),
    ]

    operations = [
        migrations.RunPython(classify_restricted_markets, migrations.RunPython.noop),
    ]