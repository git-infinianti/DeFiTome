from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("Listings", "0022_decpoker_market_valuation_and_audit_authority"),
    ]

    operations = [
        migrations.AlterField(
            model_name="decpokergameinstance",
            name="hand_cooldown_seconds",
            field=models.PositiveIntegerField(default=30),
        ),
    ]