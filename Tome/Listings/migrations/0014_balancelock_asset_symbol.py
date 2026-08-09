from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('Listings', '0013_tradingpair_unordered_pair_key'),
    ]

    operations = [
        migrations.AddField(
            model_name='balancelock',
            name='asset_symbol',
            field=models.CharField(db_index=True, default='EVR', max_length=255),
        ),
    ]