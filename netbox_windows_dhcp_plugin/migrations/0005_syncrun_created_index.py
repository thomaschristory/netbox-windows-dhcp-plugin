"""
An index on the sync run creation time, for run retention (issue #106): the import scheduler looks for the runs
older than ``sync_run_retention_days`` across all servers, oldest first. Without it PostgreSQL would read the whole
table every time.

Sync runs are outside branching (not branchable), and this changes the schema, not data.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_windows_dhcp_plugin", "0004_push_trigger"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="syncrun",
            index=models.Index(fields=["created"], name="nbwd_syncrun_created"),
        ),
    ]
