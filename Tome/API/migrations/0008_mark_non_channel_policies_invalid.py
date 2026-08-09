from django.db import migrations


def mark_non_channel_policies_invalid(apps, schema_editor):
    MessageChannelPolicy = apps.get_model('API', 'MessageChannelPolicy')
    for policy in MessageChannelPolicy.objects.all().only('id', 'channel_name'):
        if '~' not in str(policy.channel_name or ''):
            MessageChannelPolicy.objects.filter(id=policy.id).update(
                chain_metadata_status='invalid',
                chain_metadata_error='legacy_policy_name_is_not_a_messaging_channel_asset',
            )


def restore_pending_status(apps, schema_editor):
    MessageChannelPolicy = apps.get_model('API', 'MessageChannelPolicy')
    MessageChannelPolicy.objects.filter(
        chain_metadata_error='legacy_policy_name_is_not_a_messaging_channel_asset',
    ).update(
        chain_metadata_status='pending',
        chain_metadata_error='',
    )


class Migration(migrations.Migration):
    dependencies = [
        ('API', '0007_messagechannelpolicy_revision_burn'),
    ]

    operations = [
        migrations.RunPython(mark_non_channel_policies_invalid, restore_pending_status),
    ]
