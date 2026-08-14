from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('API', '0009_channel_reconciliation_models'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='channelevent',
            constraint=models.UniqueConstraint(
                fields=('policy', 'aggregate_type', 'aggregate_id', 'aggregate_sequence'),
                name='channel_event_policy_aggregate_sequence_unique',
            ),
        ),
    ]