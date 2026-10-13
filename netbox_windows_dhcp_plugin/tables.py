import django_tables2 as tables
from django.utils.translation import gettext_lazy as _
from netbox.tables import NetBoxTable, PrimaryModelTable, columns

from .models import (
    ExclusionRange,
    FailoverRelationship,
    ImportProposal,
    ImportProposalItem,
    MacFilter,
    MacFilterLists,
    SyncRun,
    WindowsClass,
    WindowsOptionDefinition,
    WindowsOptionQualifier,
    WindowsPolicy,
    WindowsReservation,
    WindowsScope,
    WindowsServer,
    WindowsVendorOption,
    WindowsVendorOptionDefinition,
)

__all__ = (
    "ExclusionRangeTable",
    "FailoverRelationshipTable",
    "ImportProposalItemTable",
    "ImportProposalTable",
    "MacFilterListsTable",
    "MacFilterTable",
    "SyncRunTable",
    "WindowsClassTable",
    "WindowsOptionDefinitionTable",
    "WindowsOptionQualifierTable",
    "WindowsPolicyTable",
    "WindowsReservationTable",
    "WindowsScopeTable",
    "WindowsServerTable",
    "WindowsVendorOptionDefinitionTable",
    "WindowsVendorOptionTable",
)


class WindowsServerTable(PrimaryModelTable):
    dhcp_server = tables.Column(verbose_name=_("DHCP server"), linkify=True)
    transport = columns.ChoiceFieldColumn(verbose_name=_("Transport"))
    winrm_auth = columns.ChoiceFieldColumn(verbose_name=_("WinRM authentication"))
    winrm_https = columns.BooleanColumn(verbose_name=_("HTTPS"))
    verify_tls = columns.BooleanColumn(verbose_name=_("Verify host"))
    sync_enabled = columns.BooleanColumn(verbose_name=_("Sync"))
    import_vrf = tables.Column(verbose_name=_("Import VRF"), linkify=True)
    tags = columns.TagColumn(url_name="plugins:netbox_windows_dhcp_plugin:windowsserver_list")

    class Meta(PrimaryModelTable.Meta):
        model = WindowsServer
        fields = (
            "pk",
            "id",
            "dhcp_server",
            "host",
            "transport",
            "port",
            "credential_profile",
            "winrm_auth",
            "winrm_https",
            "verify_tls",
            "computer_name",
            "sync_enabled",
            "import_interval",
            "import_vrf",
            "description",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("dhcp_server", "host", "transport", "winrm_auth", "credential_profile", "sync_enabled")


class WindowsScopeTable(NetBoxTable):
    subnet = tables.Column(verbose_name=_("Subnet"), linkify=True)
    state = columns.ChoiceFieldColumn(verbose_name=_("State"))
    scope_type = columns.ChoiceFieldColumn(verbose_name=_("Type"))
    activate_policies = columns.BooleanColumn(verbose_name=_("Policies"))
    tags = columns.TagColumn(url_name="plugins:netbox_windows_dhcp_plugin:windowsscope_list")

    class Meta(NetBoxTable.Meta):
        model = WindowsScope
        fields = (
            "pk",
            "id",
            "subnet",
            "state",
            "scope_type",
            "delay_ms",
            "max_bootp_clients",
            "activate_policies",
            "preference",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("subnet", "state", "scope_type", "delay_ms", "activate_policies")


class WindowsReservationTable(NetBoxTable):
    host_reservation = tables.Column(verbose_name=_("Host reservation"), linkify=True)
    reservation_type = columns.ChoiceFieldColumn(verbose_name=_("Type"))
    tags = columns.TagColumn(url_name="plugins:netbox_windows_dhcp_plugin:windowsreservation_list")

    class Meta(NetBoxTable.Meta):
        model = WindowsReservation
        fields = ("pk", "id", "host_reservation", "reservation_type", "tags", "created", "last_updated")
        default_columns = ("host_reservation", "reservation_type")


class ExclusionRangeTable(PrimaryModelTable):
    range = tables.Column(verbose_name=_("Range"), linkify=True, accessor="pk", order_by=("start_address",))
    subnet = tables.Column(verbose_name=_("Subnet"), linkify=True)
    tags = columns.TagColumn(url_name="plugins:netbox_windows_dhcp_plugin:exclusionrange_list")

    class Meta(PrimaryModelTable.Meta):
        model = ExclusionRange
        fields = (
            "pk",
            "id",
            "range",
            "subnet",
            "start_address",
            "end_address",
            "description",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("range", "subnet", "description")

    def render_range(self, record):
        return str(record)


class WindowsClassTable(PrimaryModelTable):
    name = tables.Column(verbose_name=_("Name"), linkify=True)
    dhcp_server = tables.Column(verbose_name=_("DHCP server"), linkify=True)
    class_type = columns.ChoiceFieldColumn(verbose_name=_("Type"))
    tags = columns.TagColumn(url_name="plugins:netbox_windows_dhcp_plugin:windowsclass_list")

    class Meta(PrimaryModelTable.Meta):
        model = WindowsClass
        fields = (
            "pk",
            "id",
            "name",
            "dhcp_server",
            "class_type",
            "family",
            "data",
            "description",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("name", "dhcp_server", "class_type", "family", "data")


class WindowsOptionDefinitionTable(NetBoxTable):
    option_definition = tables.Column(verbose_name=_("Option definition"), linkify=True)
    tags = columns.TagColumn(url_name="plugins:netbox_windows_dhcp_plugin:windowsoptiondefinition_list")

    class Meta(NetBoxTable.Meta):
        model = WindowsOptionDefinition
        fields = ("pk", "id", "option_definition", "windows_type", "tags", "created", "last_updated")
        default_columns = ("option_definition", "windows_type")


class WindowsPolicyTable(PrimaryModelTable):
    name = tables.Column(verbose_name=_("Name"), linkify=True)
    dhcp_server = tables.Column(verbose_name=_("DHCP server"), linkify=True)
    subnet = tables.Column(verbose_name=_("Subnet"), linkify=True)
    enabled = columns.BooleanColumn(verbose_name=_("Enabled"))
    condition = columns.ChoiceFieldColumn(verbose_name=_("Condition"))
    tags = columns.TagColumn(url_name="plugins:netbox_windows_dhcp_plugin:windowspolicy_list")

    class Meta(PrimaryModelTable.Meta):
        model = WindowsPolicy
        fields = (
            "pk",
            "id",
            "name",
            "dhcp_server",
            "subnet",
            "processing_order",
            "enabled",
            "condition",
            "lease_duration",
            "description",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("name", "dhcp_server", "subnet", "processing_order", "enabled")


class WindowsOptionQualifierTable(NetBoxTable):
    option = tables.Column(verbose_name=_("Option"), linkify=True)
    user_class = tables.Column(verbose_name=_("User class"), linkify=True)
    policy = tables.Column(verbose_name=_("Policy"), linkify=True)
    tags = columns.TagColumn(url_name="plugins:netbox_windows_dhcp_plugin:windowsoptionqualifier_list")

    class Meta(NetBoxTable.Meta):
        model = WindowsOptionQualifier
        fields = ("pk", "id", "option", "user_class", "policy", "tags", "created", "last_updated")
        default_columns = ("option", "user_class", "policy")


class FailoverRelationshipTable(PrimaryModelTable):
    name = tables.Column(verbose_name=_("Name"), linkify=True)
    primary_server = tables.Column(verbose_name=_("Primary server"), linkify=True)
    partner_server = tables.Column(verbose_name=_("Partner server"), linkify=True)
    mode = columns.ChoiceFieldColumn(verbose_name=_("Mode"))
    server_role = columns.ChoiceFieldColumn(verbose_name=_("Role"))
    subnets = columns.ManyToManyColumn(verbose_name=_("Subnets"), linkify_item=True)
    tags = columns.TagColumn(url_name="plugins:netbox_windows_dhcp_plugin:failoverrelationship_list")

    class Meta(PrimaryModelTable.Meta):
        model = FailoverRelationship
        fields = (
            "pk",
            "id",
            "name",
            "primary_server",
            "partner_server",
            "mode",
            "max_client_lead_time",
            "load_balance_percent",
            "reserve_percent",
            "server_role",
            "state_switch_interval",
            "subnets",
            "description",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("name", "primary_server", "partner_server", "mode", "max_client_lead_time")


class MacFilterTable(PrimaryModelTable):
    mac_address = tables.Column(verbose_name=_("MAC address"), linkify=True)
    dhcp_server = tables.Column(verbose_name=_("DHCP server"), linkify=True)
    list = columns.ChoiceFieldColumn(verbose_name=_("List"))
    tags = columns.TagColumn(url_name="plugins:netbox_windows_dhcp_plugin:macfilter_list")

    class Meta(PrimaryModelTable.Meta):
        model = MacFilter
        fields = ("pk", "id", "mac_address", "dhcp_server", "list", "description", "tags", "created", "last_updated")
        default_columns = ("mac_address", "dhcp_server", "list", "description")


class MacFilterListsTable(NetBoxTable):
    dhcp_server = tables.Column(verbose_name=_("DHCP server"), linkify=True)
    allow_list_enabled = columns.BooleanColumn(verbose_name=_("Allow list"))
    deny_list_enabled = columns.BooleanColumn(verbose_name=_("Deny list"))
    tags = columns.TagColumn(url_name="plugins:netbox_windows_dhcp_plugin:macfilterlists_list")

    class Meta(NetBoxTable.Meta):
        model = MacFilterLists
        fields = (
            "pk",
            "id",
            "dhcp_server",
            "allow_list_enabled",
            "deny_list_enabled",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("id", "dhcp_server", "allow_list_enabled", "deny_list_enabled")


class WindowsVendorOptionDefinitionTable(PrimaryModelTable):
    name = tables.Column(verbose_name=_("Name"), linkify=True)
    vendor_class = tables.Column(verbose_name=_("Vendor class"), linkify=True)
    multi_valued = columns.BooleanColumn(verbose_name=_("Multi-valued"))
    tags = columns.TagColumn(url_name="plugins:netbox_windows_dhcp_plugin:windowsvendoroptiondefinition_list")

    class Meta(PrimaryModelTable.Meta):
        model = WindowsVendorOptionDefinition
        fields = (
            "pk",
            "id",
            "name",
            "vendor_class",
            "code",
            "windows_type",
            "multi_valued",
            "description",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("name", "vendor_class", "code", "windows_type", "multi_valued")


class WindowsVendorOptionTable(NetBoxTable):
    definition = tables.Column(verbose_name=_("Definition"), linkify=True)
    assigned_object = tables.Column(verbose_name=_("Set on"), linkify=True, orderable=False)
    user_class = tables.Column(verbose_name=_("User class"), linkify=True)
    policy = tables.Column(verbose_name=_("Policy"), linkify=True)
    value = tables.Column(verbose_name=_("Value"), orderable=False)
    tags = columns.TagColumn(url_name="plugins:netbox_windows_dhcp_plugin:windowsvendoroption_list")

    class Meta(NetBoxTable.Meta):
        model = WindowsVendorOption
        fields = (
            "pk",
            "id",
            "definition",
            "assigned_object",
            "user_class",
            "policy",
            "value",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("id", "definition", "assigned_object", "user_class", "policy", "value")

    def render_value(self, value):
        return ", ".join(str(v) for v in value) if isinstance(value, list) else value


class SyncRunTable(NetBoxTable):
    id = tables.Column(verbose_name=_("ID"), linkify=True)
    windows_server = tables.Column(verbose_name=_("Windows server"), linkify=True)
    kind = columns.ChoiceFieldColumn(verbose_name=_("Kind"))
    status = columns.ChoiceFieldColumn(verbose_name=_("Status"))
    job = tables.Column(verbose_name=_("Job"), linkify=True)
    started = columns.DateTimeColumn(verbose_name=_("Started"))
    completed = columns.DateTimeColumn(verbose_name=_("Completed"))
    actions = columns.ActionsColumn(actions=("delete",))

    class Meta(NetBoxTable.Meta):
        model = SyncRun
        fields = (
            "pk",
            "id",
            "windows_server",
            "kind",
            "status",
            "branch_name",
            "job",
            "started",
            "completed",
            "error",
            "created",
        )
        default_columns = ("id", "windows_server", "kind", "status", "branch_name", "started", "completed")


class ImportProposalTable(NetBoxTable):
    id = tables.Column(verbose_name=_("ID"), linkify=True)
    windows_server = tables.Column(verbose_name=_("Windows server"), linkify=True)
    status = columns.ChoiceFieldColumn(verbose_name=_("Status"))
    sync_run = tables.Column(verbose_name=_("Sync run"), linkify=True)
    items = columns.LinkedCountColumn(
        viewname="plugins:netbox_windows_dhcp_plugin:importproposalitem_list",
        url_params={"proposal_id": "pk"},
        verbose_name=_("Items"),
        accessor="item_count",
    )
    actions = columns.ActionsColumn(actions=("delete",))

    class Meta(NetBoxTable.Meta):
        model = ImportProposal
        fields = ("pk", "id", "windows_server", "status", "sync_run", "items", "created")
        default_columns = ("id", "windows_server", "status", "items", "created")


class ImportProposalItemTable(NetBoxTable):
    id = tables.Column(verbose_name=_("ID"), linkify=True)
    proposal = tables.Column(verbose_name=_("Proposal"), linkify=True)
    action = columns.ChoiceFieldColumn(verbose_name=_("Action"))
    status = columns.ChoiceFieldColumn(verbose_name=_("Status"))
    object = tables.Column(verbose_name=_("Object"), linkify=True, orderable=False)
    actions = columns.ActionsColumn(actions=())

    class Meta(NetBoxTable.Meta):
        model = ImportProposalItem
        fields = ("pk", "id", "proposal", "action", "status", "kind", "key", "object", "error")
        default_columns = ("id", "action", "status", "kind", "key", "object")
