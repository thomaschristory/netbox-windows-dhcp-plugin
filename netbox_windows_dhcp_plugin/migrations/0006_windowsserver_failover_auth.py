"""
The failover authentication of a Windows server (issue #141): how failover writes and replication log on when not as
the rest of the server's calls. Empty (the default) keeps every existing server as it was.

Windows servers are outside branching (not branchable), and this changes the schema, not data.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_windows_dhcp_plugin", "0005_syncrun_retention_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="windowsserver",
            name="failover_auth",
            field=models.CharField(blank=True, default="", max_length=10),
        ),
    ]
