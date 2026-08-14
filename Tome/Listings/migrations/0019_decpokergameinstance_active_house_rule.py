from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("Listings", "0018_decpokergameinstance_hand_cooldown"),
    ]

    operations = [
        migrations.AddField(
            model_name="decpokergameinstance",
            name="active_house_rule",
            field=models.CharField(
                default="dealer_best_two_of_three_wins_ties",
                max_length=64,
            ),
        ),
    ]