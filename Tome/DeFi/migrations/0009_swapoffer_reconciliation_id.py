import uuid

from django.db import migrations, models


def backfill_reconciliation_ids(apps, schema_editor):
    SwapOffer = apps.get_model('DeFi', 'SwapOffer')
    for swap_offer in SwapOffer.objects.filter(reconciliation_id__isnull=True).iterator():
        swap_offer.reconciliation_id = uuid.uuid4()
        swap_offer.save(update_fields=['reconciliation_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('DeFi', '0008_swapoffer_settlement_temp_txid_swapfundinglock'),
    ]

    operations = [
        migrations.AddField(
            model_name='swapoffer',
            name='reconciliation_id',
            field=models.UUIDField(db_index=True, editable=False, null=True),
        ),
        migrations.RunPython(backfill_reconciliation_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='swapoffer',
            name='reconciliation_id',
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
    ]