from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('API', '0005_dexmarketeventmessage'),
    ]

    operations = [
        migrations.AddField(
            model_name='messagechannelpolicy',
            name='chain_metadata_checked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='messagechannelpolicy',
            name='chain_metadata_error',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='messagechannelpolicy',
            name='chain_metadata_status',
            field=models.CharField(choices=[('pending', 'Pending confirmation'), ('verified', 'Verified'), ('metadata_missing', 'Metadata missing'), ('invalid', 'Invalid metadata')], db_index=True, default='pending', max_length=24),
        ),
        migrations.AddField(
            model_name='messagechannelpolicy',
            name='issuance_txid',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='messagechannelpolicy',
            name='metadata_ipfs_cid',
            field=models.CharField(blank=True, max_length=255),
        ),
    ]