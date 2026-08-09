from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('Listings', '0010_uniqueassetmintrequest_confirmation_depth_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='tradingpair',
            name='base_token',
            field=models.CharField(max_length=255),
        ),
        migrations.AlterField(
            model_name='tradingpair',
            name='quote_token',
            field=models.CharField(max_length=255),
        ),
        migrations.AddField(
            model_name='tradingpair',
            name='instrument_type',
            field=models.CharField(
                choices=[
                    ('token', 'Token'),
                    ('security_capable', 'Restricted / Security-capable'),
                ],
                db_index=True,
                default='token',
                max_length=24,
            ),
        ),
    ]