import hashlib

from django.db import migrations, models


def build_pair_key(base_token, quote_token):
    canonical = '\x1f'.join(sorted((
        str(base_token or '').strip().upper(),
        str(quote_token or '').strip().upper(),
    )))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def backfill_pair_keys(apps, schema_editor):
    trading_pair = apps.get_model('Listings', 'TradingPair')
    claimed = set()
    for pair in trading_pair.objects.order_by('created_at', 'id').iterator():
        key = (pair.network_mode, build_pair_key(pair.base_token, pair.quote_token))
        if key in claimed:
            continue
        trading_pair.objects.filter(pk=pair.pk).update(pair_key=key[1])
        claimed.add(key)


class Migration(migrations.Migration):
    dependencies = [
        ('Listings', '0012_classify_restricted_market_instruments'),
    ]

    operations = [
        migrations.AddField(
            model_name='tradingpair',
            name='pair_key',
            field=models.CharField(blank=True, editable=False, max_length=64, null=True),
        ),
        migrations.RunPython(backfill_pair_keys, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='tradingpair',
            constraint=models.UniqueConstraint(
                fields=('network_mode', 'pair_key'),
                name='trading_pair_unordered_unique_per_network',
            ),
        ),
    ]