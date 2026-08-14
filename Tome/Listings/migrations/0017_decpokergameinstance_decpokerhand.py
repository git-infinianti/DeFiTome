from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("API", "0008_mark_non_channel_policies_invalid"),
        ("Listings", "0016_tradingpair_pair_slug"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("Wallet", "0017_walletpreferences_nft_image_uri_template"),
    ]

    operations = [
        migrations.CreateModel(
            name="DecPokerGameInstance",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("network_mode", models.CharField(choices=[("testnet", "Testnet"), ("mainnet", "Mainnet")], db_index=True, default="testnet", max_length=10)),
                ("title", models.CharField(max_length=120)),
                ("reward_asset_name", models.CharField(max_length=30)),
                ("reward_asset_units", models.PositiveSmallIntegerField(default=2)),
                ("reward_supply", models.DecimalField(decimal_places=8, default=0, max_digits=30)),
                ("entry_fee_evr", models.DecimalField(decimal_places=8, default=0, max_digits=20)),
                ("reward_per_win", models.DecimalField(decimal_places=8, default=0, max_digits=30)),
                ("instance_fee_evr", models.DecimalField(decimal_places=8, default=0, max_digits=20)),
                ("instance_fee_txid", models.CharField(blank=True, default="", max_length=100)),
                ("system_fee_address", models.CharField(max_length=128)),
                ("wager_treasury_bps", models.PositiveIntegerField(default=5000)),
                ("reward_metadata_cid", models.CharField(blank=True, default="", max_length=255)),
                ("reward_issue_txid", models.CharField(blank=True, default="", max_length=100)),
                ("owner_transfer_txid", models.CharField(blank=True, default="", max_length=100)),
                ("profile_tag_asset_name", models.CharField(blank=True, default="", max_length=64)),
                ("profile_tag_txid", models.CharField(blank=True, default="", max_length=100)),
                ("profile_tag_error", models.TextField(blank=True, default="")),
                ("active_server_seed_hash", models.CharField(blank=True, default="", max_length=64)),
                ("active_server_seed_secret", models.CharField(blank=True, default="", max_length=128)),
                ("next_hand_nonce", models.PositiveIntegerField(default=1)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("active", "Active"), ("paused", "Paused"), ("retired", "Retired"), ("failed", "Failed")], default="pending", max_length=12)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("channel_policy", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="dec_game_instances", to="API.messagechannelpolicy")),
                ("creator", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="dec_created_games", to=settings.AUTH_USER_MODEL)),
                ("manager_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="dec_managed_games", to=settings.AUTH_USER_MODEL)),
                ("vault_profile", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="dec_vault_instances", to="Wallet.walletprofile")),
            ],
            options={
                "ordering": ("-created_at",),
            },
        ),
        migrations.CreateModel(
            name="DecPokerHand",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("wager_evr", models.DecimalField(decimal_places=8, max_digits=20)),
                ("reward_amount", models.DecimalField(decimal_places=8, default=0, max_digits=30)),
                ("reward_asset_name", models.CharField(blank=True, default="", max_length=30)),
                ("result", models.CharField(choices=[("win", "Win"), ("lose", "Lose"), ("push", "Push")], max_length=10)),
                ("player_cards", models.JSONField(default=list)),
                ("dealer_cards", models.JSONField(default=list)),
                ("outcome_detail", models.JSONField(default=dict)),
                ("client_seed", models.CharField(blank=True, default="", max_length=128)),
                ("server_seed_hash", models.CharField(blank=True, default="", max_length=64)),
                ("server_seed_revealed", models.CharField(blank=True, default="", max_length=128)),
                ("fairness_nonce", models.PositiveIntegerField(default=1)),
                ("fairness_digest", models.CharField(blank=True, default="", max_length=64)),
                ("spend_txid", models.CharField(blank=True, default="", max_length=100)),
                ("reward_txid", models.CharField(blank=True, default="", max_length=100)),
                ("spend_message_txid", models.CharField(blank=True, default="", max_length=100)),
                ("reward_message_txid", models.CharField(blank=True, default="", max_length=100)),
                ("spend_message_status", models.CharField(choices=[("broadcasted", "Broadcasted"), ("failed", "Failed"), ("skipped", "Skipped")], default="skipped", max_length=16)),
                ("reward_message_status", models.CharField(choices=[("broadcasted", "Broadcasted"), ("failed", "Failed"), ("skipped", "Skipped")], default="skipped", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("game_instance", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="hands", to="Listings.decpokergameinstance")),
                ("player", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="dec_poker_hands", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddConstraint(
            model_name="decpokergameinstance",
            constraint=models.UniqueConstraint(fields=("network_mode", "reward_asset_name"), name="dec_game_reward_asset_unique_per_network"),
        ),
    ]
