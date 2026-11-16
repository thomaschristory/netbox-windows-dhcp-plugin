"""
Two sync run indexes for run retention (issue #106):

- on the creation time: the import scheduler looks for the runs older than ``sync_run_retention_days`` across all
  servers, oldest first. Without it PostgreSQL would read the whole table every time;
- on (server, kind, completion time): the newest push of a server that reached Windows, which every push reads and
  retention keeps. Without it PostgreSQL sorts all of the server's pushes each time.

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
        migrations.AddIndex(
            model_name="syncrun",
            index=models.Index(fields=["windows_server", "kind", "-completed"], name="nbwd_syncrun_completed"),
        ),
    ]
