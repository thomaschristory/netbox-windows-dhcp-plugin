"""
The standalone data model (0.2.0). It replaces the 0.1.x migrations, which depended on netbox-plugin-dhcp.

0.2.0 is a fresh start: the first operation drops the tables 0.1.x created (if any), so an upgraded install begins
empty and is filled again by importing from each Windows server. The data netbox-plugin-dhcp holds is not touched.
The name differs from 0.1.x's 0001_initial on purpose: Django would otherwise take it as already applied.
"""

import django.contrib.postgres.fields
import django.core.validators
import django.db.models.deletion
import django.db.models.functions.text
import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models

# Tables of 0.1.x (migrations 0001_initial to 0006), most of them unknown to this model.
OLD_TABLES = (
    "windowsserver",
    "windowsscope",
    "windowsreservation",
    "exclusionrange",
    "windowsclass",
    "windowsoptiondefinition",
    "windowspolicy",
    "windowsoptionqualifier",
    "failoverrelationship_subnets",
    "failoverrelationship",
    "macfilter",
    "macfilterlists",
    "windowsvendoroptiondefinition",
    "windowsvendoroption",
    "syncstate",
    "syncrun",
    "importproposal",
    "importproposalitem",
)
DROP_OLD_TABLES = "DROP TABLE IF EXISTS {} CASCADE".format(
    ", ".join(f"netbox_windows_dhcp_plugin_{t}" for t in OLD_TABLES)
)


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("core", "0026_populate_job_execution_time"),
        ("dcim", "0199_macaddress"),
        ("extras", "0144_customfield_status"),
        ("ipam", "0097_merge_ipaddress_host_index_and_multi_protocol_services"),
        ("users", "0016_default_ordering_indexes"),
        ("virtualization", "0001_squashed_0022"),
    ]

    operations = [
        migrations.RunSQL(
            sql=DROP_OLD_TABLES,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.CreateModel(
            name="ImportProposal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("status", models.CharField(default="open", max_length=10)),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "verbose_name": "import proposal",
                "verbose_name_plural": "import proposals",
                "ordering": ("-created",),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name="ImportProposalItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("sequence", models.PositiveIntegerField(default=0)),
                ("action", models.CharField(max_length=10)),
                ("status", models.CharField(default="pending", max_length=10)),
                ("kind", models.CharField(max_length=50)),
                ("key", models.CharField(max_length=255)),
                ("object_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("before", models.JSONField(blank=True, null=True)),
                ("after", models.JSONField(blank=True, null=True)),
                ("error", models.TextField(blank=True)),
                (
                    "object_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
                (
                    "proposal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="netbox_windows_dhcp_plugin.importproposal",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "verbose_name": "import proposal item",
                "verbose_name_plural": "import proposal items",
                "ordering": ("proposal", "sequence", "pk"),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name="Scope",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
                ("state", models.CharField(default="active", max_length=10)),
                ("scope_type", models.CharField(default="dhcp", max_length=10)),
                ("lease_duration", models.PositiveIntegerField(blank=True, null=True)),
                ("delay_ms", models.PositiveIntegerField(default=0)),
                ("max_bootp_clients", models.PositiveIntegerField(blank=True, null=True)),
                ("activate_policies", models.BooleanField(default=True)),
                ("preference", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("preferred_lifetime", models.PositiveIntegerField(blank=True, null=True)),
                ("valid_lifetime", models.PositiveIntegerField(blank=True, null=True)),
                ("renew_timer", models.PositiveIntegerField(blank=True, null=True)),
                ("rebind_timer", models.PositiveIntegerField(blank=True, null=True)),
                ("comments", models.TextField(blank=True)),
                (
                    "ip_range",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_scopes",
                        to="ipam.iprange",
                    ),
                ),
                (
                    "prefix",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_scopes",
                        to="ipam.prefix",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "verbose_name": "scope",
                "verbose_name_plural": "scopes",
                "ordering": ("server", "prefix"),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name="Reservation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("client_id", models.CharField(blank=True, max_length=255)),
                ("duid", models.CharField(blank=True, max_length=255)),
                (
                    "iaid",
                    models.PositiveBigIntegerField(
                        blank=True, null=True, validators=[django.core.validators.MaxValueValidator(4294967295)]
                    ),
                ),
                ("name", models.CharField(blank=True, max_length=255)),
                ("description", models.TextField(blank=True)),
                ("reservation_type", models.CharField(default="both", max_length=10)),
                ("comments", models.TextField(blank=True)),
                (
                    "ip_address",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_reservations",
                        to="ipam.ipaddress",
                    ),
                ),
                (
                    "mac_address",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_reservations",
                        to="dcim.macaddress",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
                (
                    "scope",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reservations",
                        to="netbox_windows_dhcp_plugin.scope",
                    ),
                ),
            ],
            options={
                "verbose_name": "reservation",
                "verbose_name_plural": "reservations",
                "ordering": ("scope", "ip_address"),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name="ExclusionRange",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("start_address", models.GenericIPAddressField()),
                ("end_address", models.GenericIPAddressField()),
                ("description", models.CharField(blank=True, max_length=200)),
                ("comments", models.TextField(blank=True)),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
                (
                    "scope",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="exclusion_ranges",
                        to="netbox_windows_dhcp_plugin.scope",
                    ),
                ),
            ],
            options={
                "verbose_name": "exclusion range",
                "verbose_name_plural": "exclusion ranges",
                "ordering": ("scope", "start_address"),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name="Superscope",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("name", models.CharField(max_length=255)),
                ("description", models.CharField(blank=True, max_length=200)),
                ("comments", models.TextField(blank=True)),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "verbose_name": "superscope",
                "verbose_name_plural": "superscopes",
                "ordering": ("server", "name"),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.AddField(
            model_name="scope",
            name="superscope",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="scopes",
                to="netbox_windows_dhcp_plugin.superscope",
            ),
        ),
        migrations.CreateModel(
            name="SyncRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("kind", models.CharField(max_length=10)),
                ("status", models.CharField(default="pending", max_length=10)),
                ("started", models.DateTimeField(blank=True, null=True)),
                ("completed", models.DateTimeField(blank=True, null=True)),
                ("branch_name", models.CharField(blank=True, max_length=255)),
                ("destination", models.CharField(blank=True, max_length=10)),
                ("summary", models.JSONField(blank=True, default=dict)),
                ("error", models.TextField(blank=True)),
                (
                    "job",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="core.job",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "verbose_name": "sync run",
                "verbose_name_plural": "sync runs",
                "ordering": ("-created",),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.AddField(
            model_name="importproposal",
            name="sync_run",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="import_proposals",
                to="netbox_windows_dhcp_plugin.syncrun",
            ),
        ),
        migrations.CreateModel(
            name="WindowsServer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("description", models.CharField(blank=True, max_length=200)),
                ("comments", models.TextField(blank=True)),
                ("name", models.CharField(max_length=100, unique=True)),
                ("host", models.CharField(max_length=255)),
                ("transport", models.CharField(default="winrm", max_length=10)),
                ("port", models.PositiveIntegerField(blank=True, null=True)),
                ("credential_profile", models.CharField(max_length=100)),
                ("winrm_auth", models.CharField(default="ntlm", max_length=10)),
                ("winrm_https", models.BooleanField(default=True)),
                ("verify_tls", models.BooleanField(default=True)),
                ("kerberos_realm", models.CharField(blank=True, max_length=255)),
                ("kerberos_spn", models.CharField(blank=True, max_length=255)),
                ("ssh_host_key", models.TextField(blank=True)),
                ("computer_name", models.CharField(blank=True, max_length=255)),
                (
                    "reported_names",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=255), blank=True, default=list, editable=False
                    ),
                ),
                ("sync_enabled", models.BooleanField(default=True)),
                ("import_interval", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "device",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(app_label)s_windows_servers",
                        to="dcim.device",
                    ),
                ),
                (
                    "import_vrf",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="ipam.vrf",
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="users.owner",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
                (
                    "virtual_machine",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(app_label)s_windows_servers",
                        to="virtualization.virtualmachine",
                    ),
                ),
            ],
            options={
                "verbose_name": "Windows server",
                "verbose_name_plural": "Windows servers",
                "ordering": ("name",),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name="WindowsPolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("description", models.CharField(blank=True, max_length=200)),
                ("comments", models.TextField(blank=True)),
                ("name", models.CharField(max_length=255)),
                ("processing_order", models.PositiveSmallIntegerField(default=1)),
                ("enabled", models.BooleanField(default=True)),
                ("condition", models.CharField(default="or", max_length=5)),
                ("criteria", models.JSONField(blank=True, default=dict)),
                ("lease_duration", models.PositiveIntegerField(blank=True, null=True)),
                ("ip_ranges", models.JSONField(blank=True, default=list)),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="users.owner",
                    ),
                ),
                (
                    "scope",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="policies",
                        to="netbox_windows_dhcp_plugin.scope",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
                (
                    "server",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="policies",
                        to="netbox_windows_dhcp_plugin.windowsserver",
                    ),
                ),
            ],
            options={
                "verbose_name": "policy",
                "verbose_name_plural": "policies",
                "ordering": ("server", "scope", "processing_order", "name"),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name="WindowsClass",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("description", models.CharField(blank=True, max_length=200)),
                ("comments", models.TextField(blank=True)),
                ("name", models.CharField(max_length=255)),
                ("class_type", models.CharField(max_length=10)),
                ("data", models.CharField(max_length=255)),
                ("family", models.PositiveSmallIntegerField(default=4)),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="users.owner",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
                (
                    "server",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="classes",
                        to="netbox_windows_dhcp_plugin.windowsserver",
                    ),
                ),
            ],
            options={
                "verbose_name": "class",
                "verbose_name_plural": "classes",
                "ordering": ("server", "family", "class_type", "name"),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name="SyncState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("kind", models.CharField(max_length=50)),
                ("key", models.CharField(max_length=255)),
                ("object_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("fingerprint", models.CharField(max_length=64)),
                ("last_pushed", models.DateTimeField(auto_now=True)),
                (
                    "object_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
                (
                    "windows_server",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sync_states",
                        to="netbox_windows_dhcp_plugin.windowsserver",
                    ),
                ),
            ],
            options={
                "ordering": ("windows_server", "kind", "key"),
            },
        ),
        migrations.AddField(
            model_name="syncrun",
            name="windows_server",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="sync_runs",
                to="netbox_windows_dhcp_plugin.windowsserver",
            ),
        ),
        migrations.AddField(
            model_name="superscope",
            name="server",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="superscopes",
                to="netbox_windows_dhcp_plugin.windowsserver",
            ),
        ),
        migrations.AddField(
            model_name="scope",
            name="server",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="scopes",
                to="netbox_windows_dhcp_plugin.windowsserver",
            ),
        ),
        migrations.CreateModel(
            name="OptionValue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("family", models.PositiveSmallIntegerField(default=4)),
                (
                    "code",
                    models.PositiveSmallIntegerField(
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(65535),
                        ]
                    ),
                ),
                ("value", models.JSONField(default=list)),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
                (
                    "reservation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="option_values",
                        to="netbox_windows_dhcp_plugin.reservation",
                    ),
                ),
                (
                    "scope",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="option_values",
                        to="netbox_windows_dhcp_plugin.scope",
                    ),
                ),
                (
                    "user_class",
                    models.ForeignKey(
                        blank=True,
                        limit_choices_to={"class_type": "user"},
                        null=True,
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="user_class_option_values",
                        to="netbox_windows_dhcp_plugin.windowsclass",
                    ),
                ),
                (
                    "vendor_class",
                    models.ForeignKey(
                        blank=True,
                        limit_choices_to={"class_type": "vendor"},
                        null=True,
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="vendor_class_option_values",
                        to="netbox_windows_dhcp_plugin.windowsclass",
                    ),
                ),
                (
                    "policy",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="option_values",
                        to="netbox_windows_dhcp_plugin.windowspolicy",
                    ),
                ),
                (
                    "server",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="option_values",
                        to="netbox_windows_dhcp_plugin.windowsserver",
                    ),
                ),
            ],
            options={
                "verbose_name": "option value",
                "verbose_name_plural": "option values",
                "ordering": ("server", "family", "scope", "reservation", "code"),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name="OptionDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("family", models.PositiveSmallIntegerField(default=4)),
                (
                    "code",
                    models.PositiveSmallIntegerField(
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(65535),
                        ]
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("windows_type", models.CharField(default="String", max_length=50)),
                ("multi_valued", models.BooleanField(default=False)),
                ("default_value", models.JSONField(blank=True, default=list)),
                ("description", models.TextField(blank=True)),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
                (
                    "vendor_class",
                    models.ForeignKey(
                        blank=True,
                        limit_choices_to={"class_type": "vendor"},
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="option_definitions",
                        to="netbox_windows_dhcp_plugin.windowsclass",
                    ),
                ),
                (
                    "server",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="option_definitions",
                        to="netbox_windows_dhcp_plugin.windowsserver",
                    ),
                ),
            ],
            options={
                "verbose_name": "option definition",
                "verbose_name_plural": "option definitions",
                "ordering": ("server", "family", "vendor_class", "code"),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name="MacFilterLists",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("allow_list_enabled", models.BooleanField(default=False)),
                ("deny_list_enabled", models.BooleanField(default=False)),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
                (
                    "server",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mac_filter_lists",
                        to="netbox_windows_dhcp_plugin.windowsserver",
                    ),
                ),
            ],
            options={
                "verbose_name": "MAC filter lists",
                "verbose_name_plural": "MAC filter lists",
                "ordering": ("server",),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name="MacFilter",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("description", models.CharField(blank=True, max_length=200)),
                ("comments", models.TextField(blank=True)),
                ("mac_address", models.CharField(max_length=32)),
                ("list", models.CharField(max_length=10)),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="users.owner",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
                (
                    "server",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mac_filters",
                        to="netbox_windows_dhcp_plugin.windowsserver",
                    ),
                ),
            ],
            options={
                "verbose_name": "MAC filter",
                "verbose_name_plural": "MAC filters",
                "ordering": ("server", "list", "mac_address"),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.AddField(
            model_name="importproposal",
            name="windows_server",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="import_proposals",
                to="netbox_windows_dhcp_plugin.windowsserver",
            ),
        ),
        migrations.CreateModel(
            name="FailoverRelationship",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("description", models.CharField(blank=True, max_length=200)),
                ("comments", models.TextField(blank=True)),
                ("name", models.CharField(max_length=126)),
                ("mode", models.CharField(default="loadbalance", max_length=15)),
                ("max_client_lead_time", models.PositiveIntegerField(default=3600)),
                ("load_balance_percent", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("reserve_percent", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("server_role", models.CharField(blank=True, max_length=10)),
                ("state_switch_interval", models.PositiveIntegerField(blank=True, null=True)),
                ("shared_secret_profile", models.CharField(blank=True, max_length=100)),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="users.owner",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
                (
                    "scopes",
                    models.ManyToManyField(
                        blank=True, related_name="failover_relationships", to="netbox_windows_dhcp_plugin.scope"
                    ),
                ),
                (
                    "partner_server",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="failover_partner",
                        to="netbox_windows_dhcp_plugin.windowsserver",
                    ),
                ),
                (
                    "primary_server",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="failover_primary",
                        to="netbox_windows_dhcp_plugin.windowsserver",
                    ),
                ),
            ],
            options={
                "verbose_name": "failover relationship",
                "verbose_name_plural": "failover relationships",
                "ordering": ("name",),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.AddConstraint(
            model_name="reservation",
            constraint=models.UniqueConstraint(
                fields=("scope", "ip_address"), name="netbox_windows_dhcp_plugin_reservation_unique_address"
            ),
        ),
        migrations.AddConstraint(
            model_name="exclusionrange",
            constraint=models.UniqueConstraint(
                fields=("scope", "start_address", "end_address"),
                name="netbox_windows_dhcp_plugin_exclusionrange_unique_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="windowspolicy",
            constraint=models.UniqueConstraint(
                fields=("server", "scope", "name"),
                name="netbox_windows_dhcp_plugin_windowspolicy_unique_name",
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name="windowsclass",
            constraint=models.UniqueConstraint(
                models.F("server"),
                models.F("family"),
                models.F("class_type"),
                django.db.models.functions.text.Lower("name"),
                name="netbox_windows_dhcp_plugin_windowsclass_unique_name",
                violation_error_message="This server already has a class of that type with this name.",
            ),
        ),
        migrations.AddConstraint(
            model_name="syncstate",
            constraint=models.UniqueConstraint(
                fields=("windows_server", "kind", "key"), name="netbox_windows_dhcp_plugin_syncstate_unique_key"
            ),
        ),
        migrations.AddConstraint(
            model_name="superscope",
            constraint=models.UniqueConstraint(
                fields=("server", "name"), name="netbox_windows_dhcp_plugin_superscope_unique_name"
            ),
        ),
        migrations.AddConstraint(
            model_name="scope",
            constraint=models.UniqueConstraint(
                fields=("server", "prefix"), name="netbox_windows_dhcp_plugin_scope_unique_prefix"
            ),
        ),
        migrations.AddConstraint(
            model_name="optionvalue",
            constraint=models.UniqueConstraint(
                fields=("server", "family", "scope", "reservation", "code", "vendor_class", "user_class", "policy"),
                name="netbox_windows_dhcp_plugin_optionvalue_unique_value",
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name="optiondefinition",
            constraint=models.UniqueConstraint(
                fields=("server", "family", "vendor_class", "code"),
                name="netbox_windows_dhcp_plugin_optiondefinition_unique_code",
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name="macfilter",
            constraint=models.UniqueConstraint(
                fields=("server", "mac_address"), name="netbox_windows_dhcp_plugin_macfilter_unique_mac"
            ),
        ),
        migrations.AddConstraint(
            model_name="failoverrelationship",
            constraint=models.UniqueConstraint(
                fields=("primary_server", "name"), name="netbox_windows_dhcp_plugin_failoverrelationship_unique_name"
            ),
        ),
    ]
