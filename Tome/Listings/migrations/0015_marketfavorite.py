from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('Listings', '0014_balancelock_asset_symbol'),
    ]

    operations = [
        migrations.CreateModel(
            name='MarketFavorite',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('trading_pair', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='favorited_by', to='Listings.tradingpair')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='market_favorites', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'constraints': [
                    models.UniqueConstraint(fields=('user', 'trading_pair'), name='unique_market_favorite_per_user'),
                ],
            },
        ),
    ]