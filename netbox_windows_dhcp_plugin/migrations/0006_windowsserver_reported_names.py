import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_windows_dhcp_plugin", "0005_syncrun_destination"),
    ]

    operations = [
        migrations.AddField(
            model_name="windowsserver",
            name="reported_names",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=255), blank=True, default=list, editable=False
            ),
        ),
    ]
