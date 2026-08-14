from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('DeFi', '0009_swapoffer_reconciliation_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='swapoffer',
            name='channel_event_sequence',
            field=models.PositiveIntegerField(default=0),
        ),
    ]