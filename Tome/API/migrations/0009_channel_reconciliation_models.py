from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def install_channel_event_immutable_triggers(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return

    model = apps.get_model("API", "ChannelEvent")
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
    if schema_editor.connection.vendor != "sqlite":
        return

    model = apps.get_model("API", "ChannelEvent")
    schema_editor.execute(f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(model._meta.db_table + '_no_update')}")
    schema_editor.execute(f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(model._meta.db_table + '_no_delete')}")


class Migration(migrations.Migration):

    dependencies = [
        ("API", "0008_mark_non_channel_policies_invalid"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ChannelConsumer",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("network_mode", models.CharField(choices=[("mainnet", "Mainnet"), ("testnet", "Testnet")], db_index=True, max_length=10)),
                ("consumer_key", models.CharField(max_length=128)),
                ("display_name", models.CharField(max_length=120)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ("network_mode", "consumer_key")},
        ),
        migrations.CreateModel(
            name="ChannelEvent",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.CharField(max_length=128)),
                ("event_type", models.CharField(max_length=80)),
                ("event_version", models.PositiveSmallIntegerField()),
                ("aggregate_type", models.CharField(max_length=80)),
                ("aggregate_id", models.CharField(max_length=128)),
                ("aggregate_sequence", models.PositiveIntegerField()),
                ("stage", models.CharField(max_length=64)),
                ("network_mode", models.CharField(choices=[("mainnet", "Mainnet"), ("testnet", "Testnet")], max_length=10)),
                ("payload", models.JSONField(default=dict)),
                ("payload_checksum", models.CharField(max_length=64)),
                ("payload_ipfs_cid", models.CharField(max_length=255)),
                ("channel_txid", models.CharField(max_length=100)),
                ("channel_output_index", models.PositiveIntegerField()),
                ("block_height", models.PositiveIntegerField()),
                ("block_transaction_index", models.PositiveIntegerField()),
                ("block_hash", models.CharField(max_length=100)),
                ("confirmed_at", models.DateTimeField()),
                ("verification_status", models.CharField(choices=[("verified", "Verified"), ("invalid", "Invalid")], default="verified", max_length=16)),
                ("verification_error", models.TextField(blank=True)),
                ("raw_observation", models.JSONField(default=dict)),
                ("observed_at", models.DateTimeField(auto_now_add=True)),
                ("policy", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="channel_events", to="API.messagechannelpolicy")),
            ],
            options={"ordering": ("block_height", "block_transaction_index", "channel_output_index")},
        ),
        migrations.CreateModel(
            name="ChannelSubscription",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("observer", "Observer"), ("participant", "Participant"), ("manager", "Manager")], default="observer", max_length=16)),
                ("status", models.CharField(choices=[("active", "Active"), ("paused", "Paused")], default="active", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("policy", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="subscriptions", to="API.messagechannelpolicy")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="channel_subscriptions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("policy__channel_key", "user__username")},
        ),
        migrations.CreateModel(
            name="ChannelReconciliationIssue",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("aggregate_type", models.CharField(blank=True, max_length=80)),
                ("aggregate_id", models.CharField(blank=True, max_length=128)),
                ("code", models.CharField(max_length=80)),
                ("severity", models.CharField(choices=[("warning", "Warning"), ("error", "Error"), ("critical", "Critical")], default="error", max_length=16)),
                ("status", models.CharField(choices=[("open", "Open"), ("resolved", "Resolved")], default="open", max_length=16)),
                ("detail", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("event", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reconciliation_issues", to="API.channelevent")),
                ("policy", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reconciliation_issues", to="API.messagechannelpolicy")),
            ],
            options={"ordering": ("status", "-created_at")},
        ),
        migrations.CreateModel(
            name="ChannelEventApplication",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("projection_type", models.CharField(max_length=80)),
                ("projection_key", models.CharField(max_length=128)),
                ("status", models.CharField(choices=[("applied", "Applied"), ("already_applied", "Already applied"), ("blocked", "Blocked")], max_length=20)),
                ("result", models.JSONField(default=dict)),
                ("error_message", models.TextField(blank=True)),
                ("applied_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="applications", to="API.channelevent")),
            ],
            options={"ordering": ("event__block_height", "event__block_transaction_index", "event__channel_output_index")},
        ),
        migrations.CreateModel(
            name="ChannelSubscriptionCursor",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("last_seen_txid", models.CharField(blank=True, max_length=100)),
                ("last_seen_height", models.PositiveIntegerField(blank=True, null=True)),
                ("last_seen_transaction_index", models.PositiveIntegerField(blank=True, null=True)),
                ("last_seen_output_index", models.PositiveIntegerField(blank=True, null=True)),
                ("last_seen_event_id", models.CharField(blank=True, max_length=128)),
                ("last_reconciled_at", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(choices=[("synced", "Synced"), ("lagging", "Lagging"), ("error", "Error")], default="lagging", max_length=16)),
                ("error_message", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("consumer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="cursors", to="API.channelconsumer")),
                ("last_event", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="API.channelevent")),
                ("subscription", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cursors", to="API.channelsubscription")),
            ],
            options={"ordering": ("subscription_id", "consumer_id")},
        ),
        migrations.AddConstraint(
            model_name="channelconsumer",
            constraint=models.UniqueConstraint(fields=("network_mode", "consumer_key"), name="channel_consumer_network_key_unique"),
        ),
        migrations.AddConstraint(
            model_name="channelevent",
            constraint=models.UniqueConstraint(fields=("policy", "event_id"), name="channel_event_policy_event_id_unique"),
        ),
        migrations.AddConstraint(
            model_name="channelevent",
            constraint=models.UniqueConstraint(fields=("policy", "channel_txid", "channel_output_index"), name="channel_event_policy_tx_output_unique"),
        ),
        migrations.AddConstraint(
            model_name="channelsubscription",
            constraint=models.UniqueConstraint(fields=("user", "policy"), name="channel_subscription_user_policy_unique"),
        ),
        migrations.AddConstraint(
            model_name="channeleventapplication",
            constraint=models.UniqueConstraint(fields=("event", "projection_type", "projection_key"), name="channel_event_application_unique"),
        ),
        migrations.AddConstraint(
            model_name="channelsubscriptioncursor",
            constraint=models.UniqueConstraint(fields=("subscription", "consumer"), name="channel_subscription_cursor_unique"),
        ),
        migrations.AddIndex(
            model_name="channelsubscription",
            index=models.Index(fields=["policy", "status"], name="API_channe_policy__f6d518_idx"),
        ),
        migrations.AddIndex(
            model_name="channelsubscription",
            index=models.Index(fields=["user", "status"], name="API_channe_user_id_c1e1b1_idx"),
        ),
        migrations.AddIndex(
            model_name="channelevent",
            index=models.Index(fields=["policy", "block_height", "block_transaction_index"], name="API_channe_policy__8de4d7_idx"),
        ),
        migrations.AddIndex(
            model_name="channelevent",
            index=models.Index(fields=["aggregate_type", "aggregate_id", "aggregate_sequence"], name="API_channe_aggreg_127d41_idx"),
        ),
        migrations.AddIndex(
            model_name="channelevent",
            index=models.Index(fields=["verification_status", "network_mode"], name="API_channe_verific_a5d2f1_idx"),
        ),
        migrations.AddIndex(
            model_name="channelreconciliationissue",
            index=models.Index(fields=["policy", "status", "-created_at"], name="API_channe_policy__4c7c94_idx"),
        ),
        migrations.AddIndex(
            model_name="channelreconciliationissue",
            index=models.Index(fields=["aggregate_type", "aggregate_id", "status"], name="API_channe_aggreg_b7e549_idx"),
        ),
        migrations.RunPython(install_channel_event_immutable_triggers, remove_channel_event_immutable_triggers),
    ]