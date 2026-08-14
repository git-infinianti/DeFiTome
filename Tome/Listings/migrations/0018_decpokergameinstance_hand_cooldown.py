from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("Listings", "0017_decpokergameinstance_decpokerhand"),
    ]

    operations = [
        migrations.AddField(
            model_name="decpokergameinstance",
            name="hand_cooldown_seconds",
            field=models.PositiveIntegerField(default=300),
        ),
        migrations.AddField(
            model_name="decpokergameinstance",
            name="hand_cooldown_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]