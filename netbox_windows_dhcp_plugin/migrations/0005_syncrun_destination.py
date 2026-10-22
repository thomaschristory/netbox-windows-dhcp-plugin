from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_windows_dhcp_plugin", "0004_reservation_iaid"),
    ]

    operations = [
        migrations.AddField(
            model_name="syncrun",
            name="destination",
            field=models.CharField(blank=True, max_length=10),
        ),
    ]
