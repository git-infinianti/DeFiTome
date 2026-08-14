from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


IMMUTABLE_DEC_MODEL_NAMES = (
    "DecPokerPayoutPolicy",
    "DecPokerPayoutLedgerEntry",
    "DecPokerMarketValuation",
    "DecPokerValuationBid",
)


def _trigger_name(model, operation):
    return f"{model._meta.db_table}_{operation}_immutable"


def install_immutable_dec_triggers(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return

    for model_name in IMMUTABLE_DEC_MODEL_NAMES:
        model = apps.get_model("Listings", model_name)
        table_name = schema_editor.quote_name(model._meta.db_table)
        for operation in ("UPDATE", "DELETE"):
            trigger_name = schema_editor.quote_name(_trigger_name(model, operation.lower()))
            schema_editor.execute(
                f"CREATE TRIGGER {trigger_name} BEFORE {operation} ON {table_name} "
                "BEGIN SELECT RAISE(ABORT, 'DEC audit records are immutable'); END;"
            )


def remove_immutable_dec_triggers(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return

    for model_name in IMMUTABLE_DEC_MODEL_NAMES:
        model = apps.get_model("Listings", model_name)
        for operation in ("UPDATE", "DELETE"):
            trigger_name = schema_editor.quote_name(_trigger_name(model, operation.lower()))
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("Listings", "0021_rehash_backfilled_dec_poker_ledger"),
    ]

    operations = [
        migrations.AddField(
            model_name="decpokerpayoutpolicy",
            name="authority_evidence",
            field=models.JSONField(default=dict),
        ),
        migrations.AlterField(
            model_name="decpokerpayoutpolicy",
            name="rtp_percent",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True),
        ),
        migrations.CreateModel(
            name="DecPokerAuditAuthority",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "network_mode",
                    models.CharField(
                        choices=[("testnet", "Testnet"), ("mainnet", "Mainnet")],
                        default="testnet",
                        max_length=10,
                        unique=True,
                    ),
                ),
                ("authority_address", models.CharField(max_length=128)),
                ("restricted_asset_name", models.CharField(max_length=30)),
                ("required_qualifier_name", models.CharField(max_length=64)),
                ("required_verifier_string", models.CharField(max_length=255)),
                (
                    "minimum_restricted_asset_balance",
                    models.DecimalField(decimal_places=8, default=Decimal("1"), max_digits=30),
                ),
                ("enforce_settlement_writes", models.BooleanField(default=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("active", "Active"),
                            ("suspended", "Suspended"),
                        ],
                        default="draft",
                        max_length=16,
                    ),
                ),
                ("last_verified_at", models.DateTimeField(blank=True, null=True)),
                ("last_verification_evidence", models.JSONField(default=dict)),
                ("last_verification_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "authority_account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dec_poker_audit_authorities",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("network_mode",)},
        ),
        migrations.CreateModel(
            name="DecPokerMarketValuation",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "source_type",
                    models.CharField(
                        choices=[("execution_vwap", "Settled execution VWAP")],
                        default="execution_vwap",
                        max_length=32,
                    ),
                ),
                ("source_execution_count", models.PositiveIntegerField()),
                ("source_volume", models.DecimalField(decimal_places=8, max_digits=30)),
                ("source_started_at", models.DateTimeField()),
                ("source_ended_at", models.DateTimeField()),
                ("price_evr_per_reward_asset", models.DecimalField(decimal_places=8, max_digits=30)),
                ("expected_return_evr", models.DecimalField(decimal_places=8, max_digits=30)),
                ("rtp_percent", models.DecimalField(decimal_places=6, max_digits=12)),
                ("market_evidence", models.JSONField(default=dict)),
                ("authority_evidence", models.JSONField(default=dict)),
                ("valuation_hash", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "game_instance",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="market_valuations",
                        to="Listings.decpokergameinstance",
                    ),
                ),
                (
                    "trading_pair",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dec_poker_market_valuations",
                        to="Listings.tradingpair",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddField(
            model_name="decpokerpayoutpolicy",
            name="market_valuation",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payout_policies",
                to="Listings.decpokermarketvaluation",
            ),
        ),
        migrations.CreateModel(
            name="DecPokerValuationBid",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("price_evr_per_reward_asset", models.DecimalField(decimal_places=8, max_digits=30)),
                ("reward_asset_quantity", models.DecimalField(decimal_places=8, max_digits=30)),
                ("reserved_evr", models.DecimalField(decimal_places=8, max_digits=30)),
                ("post_only", models.BooleanField(default=True)),
                ("authority_evidence", models.JSONField(default=dict)),
                ("intent_hash", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "audit_authority",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="valuation_bids",
                        to="Listings.decpokerauditauthority",
                    ),
                ),
                (
                    "game_instance",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="valuation_bids",
                        to="Listings.decpokergameinstance",
                    ),
                ),
                (
                    "limit_order",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dec_poker_valuation_bid",
                        to="Listings.limitorder",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dec_poker_valuation_bids",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "trading_pair",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dec_poker_valuation_bids",
                        to="Listings.tradingpair",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddConstraint(
            model_name="decpokermarketvaluation",
            constraint=models.UniqueConstraint(
                fields=("game_instance", "valuation_hash"),
                name="dec_poker_market_valuation_hash_unique",
            ),
        ),
        migrations.RunPython(install_immutable_dec_triggers, remove_immutable_dec_triggers),
    ]
