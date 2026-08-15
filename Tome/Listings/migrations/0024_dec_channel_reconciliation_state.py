from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Listings', '0023_alter_decpokergameinstance_hand_cooldown_seconds'),
    ]

    operations = [
        migrations.AddField(
            model_name='decpokergameinstance',
            name='reconciliation_error',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='decpokergameinstance',
            name='reconciliation_evidence',
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name='decpokergameinstance',
            name='reconciliation_status',
            field=models.CharField(choices=[('pending', 'Pending'), ('synced', 'Synced'), ('rejected', 'Rejected')], db_index=True, default='pending', max_length=16),
        ),
        migrations.AddField(
            model_name='decpokerhand',
            name='reconciliation_error',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='decpokerhand',
            name='reconciliation_evidence',
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name='decpokerhand',
            name='reconciliation_status',
            field=models.CharField(choices=[('pending', 'Pending'), ('synced', 'Synced'), ('rejected', 'Rejected')], db_index=True, default='pending', max_length=16),
        ),
    ]