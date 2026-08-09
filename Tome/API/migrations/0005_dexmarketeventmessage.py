import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('API', '0004_messagechannelpolicy_atomicswaptransfermessage_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='DexMarketEventMessage',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('trading_pair_id', models.PositiveIntegerField()),
                ('order_id', models.PositiveIntegerField(blank=True, null=True)),
                ('stage', models.CharField(max_length=64)),
                ('payload', models.JSONField(default=dict)),
                ('payload_checksum', models.CharField(max_length=64)),
                ('payload_ipfs_cid', models.CharField(blank=True, max_length=255)),
                ('broadcast_result', models.CharField(blank=True, max_length=255)),
                ('status', models.CharField(choices=[('recorded', 'Recorded'), ('broadcasted', 'Broadcasted'), ('failed', 'Failed'), ('skipped', 'Skipped')], default='recorded', max_length=16)),
                ('error_message', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('policy', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='market_messages', to='API.messagechannelpolicy')),
            ],
            options={'ordering': ('-created_at',)},
        ),
        migrations.AddIndex(
            model_name='dexmarketeventmessage',
            index=models.Index(fields=['trading_pair_id', 'stage', '-created_at'], name='API_dexmark_trading_2824b5_idx'),
        ),
        migrations.AddIndex(
            model_name='dexmarketeventmessage',
            index=models.Index(fields=['status', '-created_at'], name='API_dexmark_status_c6f042_idx'),
        ),
    ]