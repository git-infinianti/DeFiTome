from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('Wallet', '0018_normalize_cached_evr_balances'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AssetCreationRequest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('network_mode', models.CharField(choices=[('mainnet', 'Mainnet'), ('testnet', 'Testnet')], default='testnet', max_length=10)),
                ('asset_kind', models.CharField(choices=[('main', 'Main Asset'), ('sub', 'Sub Asset'), ('unique', 'Unique Asset'), ('messaging_channel', 'Messaging Channel'), ('qualifier', 'Qualifier'), ('sub_qualifier', 'Sub Qualifier'), ('restricted', 'Restricted Asset')], max_length=32)),
                ('asset_name', models.CharField(max_length=255)),
                ('source_address', models.CharField(max_length=100)),
                ('parameters', models.JSONField(blank=True, default=dict)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('accepted', 'Mempool Accepted'), ('broadcast', 'Broadcast'), ('failed', 'Failed')], default='pending', max_length=16)),
                ('mempool_txid', models.CharField(blank=True, default='', max_length=64)),
                ('broadcast_txid', models.CharField(blank=True, default='', max_length=64)),
                ('error_message', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('creator', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='asset_creation_requests', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ('-created_at',)},
        ),
    ]