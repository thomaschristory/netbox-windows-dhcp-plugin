"""
Room for long sync keys, a port range check and an index for "the latest run of a server".

- Sync keys string together Windows names (class, policy) of up to 255 characters each, so the two key columns
  become text. PostgreSQL changes the column type without rewriting the table.
- The port validators change no SQL; the migration only records them.
- The index serves the import scheduler and the server page, which read the newest run of one kind for one server.

All four models are outside branching (not branchable), and this changes the schema, not data.
"""

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_windows_dhcp_plugin", "0001_standalone"),
    ]

    operations = [
        migrations.AlterField(
            model_name="importproposalitem",
            name="key",
            field=models.TextField(),
        ),
        migrations.AlterField(
            model_name="syncstate",
            name="key",
            field=models.TextField(),
        ),
        migrations.AlterField(
            model_name="windowsserver",
            name="port",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(65535),
                ],
            ),
        ),
        migrations.AddIndex(
            model_name="syncrun",
            index=models.Index(fields=["windows_server", "kind", "-created"], name="nbwd_syncrun_latest"),
        ),
    ]
