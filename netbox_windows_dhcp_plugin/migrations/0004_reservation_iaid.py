import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_windows_dhcp_plugin", "0003_remaining_objects"),
    ]

    operations = [
        migrations.AddField(
            model_name="windowsreservation",
            name="iaid",
            field=models.PositiveBigIntegerField(
                blank=True, null=True, validators=[django.core.validators.MaxValueValidator(4294967295)]
            ),
        ),
    ]
