from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('API', '0006_messagechannelpolicy_chain_metadata'),
    ]

    operations = [
        migrations.AddField(
            model_name='messagechannelpolicy',
            name='revision_burn_txid',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='messagechannelpolicy',
            name='revision_burned_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]