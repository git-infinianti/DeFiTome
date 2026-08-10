import hashlib
import re

from django.db import migrations, models
from django.utils.text import slugify


def build_pair_slug(base_token, quote_token):
    base = str(base_token or '').strip().upper()
    quote = str(quote_token or '').strip().upper()
    base_slug = slugify(re.sub(r'[/~#$!]+', '-', base))[:100] or 'asset'
    quote_slug = slugify(re.sub(r'[/~#$!]+', '-', quote))[:100] or 'asset'
    pair_slug = f'{base_slug}-{quote_slug}'
    if not re.fullmatch(r'[A-Z0-9]+', base) or not re.fullmatch(r'[A-Z0-9]+', quote):
        identity = hashlib.sha256(f'{base}\x1f{quote}'.encode('utf-8')).hexdigest()[:10]
        pair_slug = f'{pair_slug}-{identity}'
    return pair_slug


def populate_pair_slugs(apps, schema_editor):
    TradingPair = apps.get_model('Listings', 'TradingPair')
    for pair in TradingPair.objects.all().iterator():
        pair.pair_slug = build_pair_slug(pair.base_token, pair.quote_token)
        pair.save(update_fields=['pair_slug'])


class Migration(migrations.Migration):
    dependencies = [
        ('Listings', '0015_marketfavorite'),
    ]

    operations = [
        migrations.AddField(
            model_name='tradingpair',
            name='pair_slug',
            field=models.SlugField(blank=True, editable=False, max_length=255),
        ),
        migrations.RunPython(populate_pair_slugs, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='tradingpair',
            name='pair_slug',
            field=models.SlugField(db_index=True, editable=False, max_length=255),
        ),
        migrations.AddConstraint(
            model_name='tradingpair',
            constraint=models.UniqueConstraint(
                fields=('network_mode', 'pair_slug'),
                name='trading_pair_slug_unique_per_network',
            ),
        ),
    ]