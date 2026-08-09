from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('Wallet', '0014_remove_nft_vault_asset_types'),
    ]

    operations = [
        migrations.CreateModel(
            name='WalletPreferences',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('default_home_tab', models.CharField(choices=[('send', 'Send'), ('receive', 'Receive'), ('profiles', 'Profiles')], default='send', max_length=20)),
                ('default_send_currency', models.CharField(default='EVR', max_length=64)),
                ('default_transaction_limit', models.CharField(choices=[('all', 'All'), ('25', 'Latest 25'), ('50', 'Latest 50'), ('100', 'Latest 100'), ('250', 'Latest 250')], default='all', max_length=10)),
                ('default_confirmation_behavior', models.CharField(choices=[('always', 'Always'), ('warn', 'Warn for large sends'), ('off', 'Disabled')], default='always', max_length=20)),
                ('default_receive_qr_style', models.CharField(choices=[('classic', 'Classic'), ('high_contrast', 'High Contrast'), ('minimal', 'Minimal')], default='classic', max_length=20)),
                ('address_label_style', models.CharField(choices=[('full', 'Full Address'), ('short', 'Short Label'), ('masked', 'Masked Address')], default='full', max_length=20)),
                ('profile_sort_order', models.CharField(choices=[('main_first', 'Main Profile First'), ('name_asc', 'Name A to Z'), ('name_desc', 'Name Z to A'), ('index_asc', 'Index Low to High'), ('index_desc', 'Index High to Low')], default='main_first', max_length=20)),
                ('auto_sync_balance', models.BooleanField(default=True)),
                ('auto_validate_recipient', models.BooleanField(default=True)),
                ('auto_copy_receive_address', models.BooleanField(default=False)),
                ('show_receive_qr', models.BooleanField(default=True)),
                ('show_zero_balances', models.BooleanField(default=True)),
                ('show_change_addresses', models.BooleanField(default=False)),
                ('show_profile_network_badges', models.BooleanField(default=True)),
                ('highlight_main_profile', models.BooleanField(default=True)),
                ('hide_balance_on_open', models.BooleanField(default=False)),
                ('compact_cards', models.BooleanField(default=False)),
                ('confirm_external_links', models.BooleanField(default=True)),
                ('enable_address_tooltips', models.BooleanField(default=True)),
                ('prefer_main_profile_on_receive', models.BooleanField(default=True)),
                ('transaction_refresh_seconds', models.PositiveIntegerField(default=30)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('wallet', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='preferences', to='Wallet.userwallet')),
            ],
            options={
                'verbose_name_plural': 'Wallet Preferences',
            },
        ),
    ]
