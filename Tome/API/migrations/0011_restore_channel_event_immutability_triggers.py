from django.db import migrations


def install_channel_event_immutable_triggers(apps, schema_editor):
    if schema_editor.connection.vendor != 'sqlite':
        return

    model = apps.get_model('API', 'ChannelEvent')
    table = schema_editor.quote_name(model._meta.db_table)
    schema_editor.execute(
        f"CREATE TRIGGER {schema_editor.quote_name(model._meta.db_table + '_no_update')} "
        f"BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'Channel events are immutable'); END;"
    )
    schema_editor.execute(
        f"CREATE TRIGGER {schema_editor.quote_name(model._meta.db_table + '_no_delete')} "
        f"BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'Channel events are immutable'); END;"
    )


def remove_channel_event_immutable_triggers(apps, schema_editor):
    if schema_editor.connection.vendor != 'sqlite':
        return

    model = apps.get_model('API', 'ChannelEvent')
    schema_editor.execute(f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(model._meta.db_table + '_no_update')}")
    schema_editor.execute(f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(model._meta.db_table + '_no_delete')}")


class Migration(migrations.Migration):

    dependencies = [
        ('API', '0010_channel_event_aggregate_sequence_unique'),
    ]

    operations = [
        migrations.RunPython(install_channel_event_immutable_triggers, remove_channel_event_immutable_triggers),
    ]