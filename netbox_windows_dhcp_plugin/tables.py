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
    OptionDefinition,
    OptionValue,
    Reservation,
    Scope,
    Superscope,
    SyncRun,
    WindowsClass,
    WindowsPolicy,
    WindowsServer,
)
from .ui.options import OptionNames

__all__ = (
    "ExclusionRangeTable",
    "FailoverRelationshipTable",
    "ImportProposalItemTable",
    "ImportProposalTable",
    "MacFilterListsTable",
    "MacFilterTable",
    "OptionDefinitionTable",
    "OptionValueTable",
    "ReservationTable",
    "ScopeTable",
    "SuperscopeTable",
    "SyncRunTable",
    "WindowsClassTable",
    "WindowsPolicyTable",
    "WindowsServerTable",
)


def _tags(model_name):
    return columns.TagColumn(url_name=f"plugins:netbox_windows_dhcp_plugin:{model_name}_list")


def _server_column(accessor="server", verbose_name=None):
    return tables.Column(verbose_name=verbose_name or _("Server"), accessor=accessor, linkify=True)


class WindowsServerTable(PrimaryModelTable):
    name = tables.Column(verbose_name=_("Name"), linkify=True)
    device = tables.Column(verbose_name=_("Device"), linkify=True)
    virtual_machine = tables.Column(verbose_name=_("Virtual machine"), linkify=True)
    transport = columns.ChoiceFieldColumn(verbose_name=_("Transport"))
    winrm_auth = columns.ChoiceFieldColumn(verbose_name=_("WinRM authentication"))
    winrm_https = columns.BooleanColumn(verbose_name=_("HTTPS"))
    verify_tls = columns.BooleanColumn(verbose_name=_("Verify host"))
    sync_enabled = columns.BooleanColumn(verbose_name=_("Sync"))
    import_vrf = tables.Column(verbose_name=_("Import VRF"), linkify=True)
    tags = _tags("windowsserver")

    class Meta(PrimaryModelTable.Meta):
        model = WindowsServer
        fields = (
            "pk",
            "id",
            "name",
            "device",
            "virtual_machine",
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
            "comments",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("name", "host", "transport", "winrm_auth", "credential_profile", "sync_enabled", "device")


class SuperscopeTable(NetBoxTable):
    name = tables.Column(verbose_name=_("Name"), linkify=True)
    server = _server_column()
    scopes = columns.LinkedCountColumn(
        viewname="plugins:netbox_windows_dhcp_plugin:scope_list",
        url_params={"superscope_id": "pk"},
        verbose_name=_("Scopes"),
        accessor="scope_count",
    )
    comments = columns.MarkdownColumn(verbose_name=_("Comments"))
    tags = _tags("superscope")

    class Meta(NetBoxTable.Meta):
        model = Superscope
        fields = ("pk", "id", "name", "server", "scopes", "description", "comments", "tags", "created", "last_updated")
        default_columns = ("name", "server", "scopes", "description")


class ScopeTable(NetBoxTable):
    name = tables.Column(verbose_name=_("Name"), linkify=True)
    server = _server_column()
    # The scope's network next to its name (#47): names alone can be generic or repeat across servers.
    prefix = tables.Column(verbose_name=_("Prefix"), linkify=True, order_by=("prefix__prefix",))
    ip_range = tables.Column(verbose_name=_("IP range"), linkify=True)
    superscope = tables.Column(verbose_name=_("Superscope"), linkify=True)
    state = columns.ChoiceFieldColumn(verbose_name=_("State"))
    scope_type = columns.ChoiceFieldColumn(verbose_name=_("Type"))
    activate_policies = columns.BooleanColumn(verbose_name=_("Policies"))
    description = tables.Column(verbose_name=_("Description"))
    comments = columns.MarkdownColumn(verbose_name=_("Comments"))
    tags = _tags("scope")

    class Meta(NetBoxTable.Meta):
        model = Scope
        fields = (
            "pk",
            "id",
            "name",
            "server",
            "prefix",
            "ip_range",
            "superscope",
            "state",
            "scope_type",
            "lease_duration",
            "delay_ms",
            "max_bootp_clients",
            "activate_policies",
            "preference",
            "preferred_lifetime",
            "valid_lifetime",
            "renew_timer",
            "rebind_timer",
            "description",
            "comments",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("name", "server", "prefix", "ip_range", "state", "superscope", "description")


class ReservationTable(NetBoxTable):
    ip_address = tables.Column(
        verbose_name=_("IP address"),
        linkify=lambda record: record.get_absolute_url(),
        order_by=("ip_address__address",),
    )
    name = tables.Column(verbose_name=_("Name"))
    scope = tables.Column(verbose_name=_("Scope"), linkify=True)
    server = _server_column("scope__server")
    mac_address = tables.Column(verbose_name=_("MAC address"), linkify=True)
    client_id = tables.Column(verbose_name=_("Client ID"))
    duid = tables.Column(verbose_name=_("DUID"))
    iaid = tables.Column(verbose_name=_("IAID"))
    reservation_type = columns.ChoiceFieldColumn(verbose_name=_("Type"))
    description = tables.Column(verbose_name=_("Description"))
    comments = columns.MarkdownColumn(verbose_name=_("Comments"))
    tags = _tags("reservation")

    class Meta(NetBoxTable.Meta):
        model = Reservation
        fields = (
            "pk",
            "id",
            "ip_address",
            "name",
            "scope",
            "server",
            "mac_address",
            "client_id",
            "duid",
            "iaid",
            "reservation_type",
            "description",
            "comments",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("ip_address", "name", "scope", "mac_address", "duid", "reservation_type")

    def render_ip_address(self, value):
        return str(value.address.ip)


class ExclusionRangeTable(NetBoxTable):
    range = tables.Column(verbose_name=_("Range"), linkify=True, accessor="pk", order_by=("start_address",))
    scope = tables.Column(verbose_name=_("Scope"), linkify=True)
    server = _server_column("scope__server")
    comments = columns.MarkdownColumn(verbose_name=_("Comments"))
    tags = _tags("exclusionrange")

    class Meta(NetBoxTable.Meta):
        model = ExclusionRange
        fields = (
            "pk",
            "id",
            "range",
            "scope",
            "server",
            "start_address",
            "end_address",
            "description",
            "comments",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("range", "scope", "description")

    def render_range(self, record):
        return str(record)


class WindowsClassTable(PrimaryModelTable):
    name = tables.Column(verbose_name=_("Name"), linkify=True)
    server = _server_column()
    class_type = columns.ChoiceFieldColumn(verbose_name=_("Type"))
    family = columns.ChoiceFieldColumn(verbose_name=_("Family"))
    tags = _tags("windowsclass")

    class Meta(PrimaryModelTable.Meta):
        model = WindowsClass
        fields = (
            "pk",
            "id",
            "name",
            "server",
            "class_type",
            "family",
            "data",
            "description",
            "comments",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("name", "server", "class_type", "family", "data")


class OptionDefinitionTable(NetBoxTable):
    code = tables.Column(verbose_name=_("Code"), linkify=True)
    name = tables.Column(verbose_name=_("Name"), linkify=True)
    server = _server_column()
    family = columns.ChoiceFieldColumn(verbose_name=_("Family"))
    vendor_class = tables.Column(verbose_name=_("Vendor class"), linkify=True)
    multi_valued = columns.BooleanColumn(verbose_name=_("Multi-valued"))
    default_value = columns.ArrayColumn(verbose_name=_("Default value"), orderable=False)
    tags = _tags("optiondefinition")

    class Meta(NetBoxTable.Meta):
        model = OptionDefinition
        fields = (
            "pk",
            "id",
            "code",
            "name",
            "server",
            "family",
            "vendor_class",
            "windows_type",
            "multi_valued",
            "default_value",
            "description",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("code", "name", "server", "family", "vendor_class", "windows_type", "multi_valued")


class WindowsPolicyTable(PrimaryModelTable):
    name = tables.Column(verbose_name=_("Name"), linkify=True)
    server = _server_column()
    scope = tables.Column(verbose_name=_("Scope"), linkify=True)
    enabled = columns.BooleanColumn(verbose_name=_("Enabled"))
    condition = columns.ChoiceFieldColumn(verbose_name=_("Condition"))
    tags = _tags("windowspolicy")

    class Meta(PrimaryModelTable.Meta):
        model = WindowsPolicy
        fields = (
            "pk",
            "id",
            "name",
            "server",
            "scope",
            "processing_order",
            "enabled",
            "condition",
            "lease_duration",
            "description",
            "comments",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("name", "server", "scope", "processing_order", "enabled")


class OptionValueTable(NetBoxTable):
    code = tables.Column(verbose_name=_("Code"), linkify=True)
    # From the server's definition, or the standard option list when there is none.
    option = tables.Column(verbose_name=_("Option"), empty_values=(), orderable=False)
    value = columns.ArrayColumn(verbose_name=_("Value"), orderable=False)
    level = tables.Column(verbose_name=_("Level"), empty_values=(), orderable=False)
    server = _server_column()
    scope = tables.Column(verbose_name=_("Scope"), linkify=True)
    reservation = tables.Column(verbose_name=_("Reservation"), linkify=True)
    family = columns.ChoiceFieldColumn(verbose_name=_("Family"))
    vendor_class = tables.Column(verbose_name=_("Vendor class"), linkify=True)
    user_class = tables.Column(verbose_name=_("User class"), linkify=True)
    policy = tables.Column(verbose_name=_("Policy"), linkify=True)
    tags = _tags("optionvalue")

    class Meta(NetBoxTable.Meta):
        model = OptionValue
        fields = (
            "pk",
            "id",
            "code",
            "option",
            "value",
            "level",
            "server",
            "scope",
            "reservation",
            "family",
            "vendor_class",
            "user_class",
            "policy",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("code", "option", "value", "level", "scope", "reservation", "vendor_class", "policy")

    def render_option(self, record):
        if not hasattr(self, "_option_names"):
            self._option_names = OptionNames()
        return self._option_names.name(record)

    def render_level(self, record):
        return record.level.capitalize()


class FailoverRelationshipTable(PrimaryModelTable):
    name = tables.Column(verbose_name=_("Name"), linkify=True)
    primary_server = tables.Column(verbose_name=_("Primary server"), linkify=True)
    partner_server = tables.Column(verbose_name=_("Partner server"), linkify=True)
    mode = columns.ChoiceFieldColumn(verbose_name=_("Mode"))
    server_role = columns.ChoiceFieldColumn(verbose_name=_("Role"))
    scopes = columns.ManyToManyColumn(verbose_name=_("Scopes"), linkify_item=True)
    tags = _tags("failoverrelationship")

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
            "scopes",
            "description",
            "comments",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("name", "primary_server", "partner_server", "mode", "max_client_lead_time")


class MacFilterTable(PrimaryModelTable):
    mac_address = tables.Column(verbose_name=_("MAC address"), linkify=True)
    server = _server_column()
    list = columns.ChoiceFieldColumn(verbose_name=_("List"))
    tags = _tags("macfilter")

    class Meta(PrimaryModelTable.Meta):
        model = MacFilter
        fields = (
            "pk",
            "id",
            "mac_address",
            "server",
            "list",
            "description",
            "comments",
            "tags",
            "created",
            "last_updated",
        )
        default_columns = ("mac_address", "server", "list", "description")


class MacFilterListsTable(NetBoxTable):
    id = tables.Column(verbose_name=_("ID"), linkify=True)
    server = _server_column()
    allow_list_enabled = columns.BooleanColumn(verbose_name=_("Allow list"))
    deny_list_enabled = columns.BooleanColumn(verbose_name=_("Deny list"))
    tags = _tags("macfilterlists")

    class Meta(NetBoxTable.Meta):
        model = MacFilterLists
        fields = ("pk", "id", "server", "allow_list_enabled", "deny_list_enabled", "tags", "created", "last_updated")
        default_columns = ("id", "server", "allow_list_enabled", "deny_list_enabled")


class SyncRunTable(NetBoxTable):
    id = tables.Column(verbose_name=_("ID"), linkify=True)
    windows_server = tables.Column(verbose_name=_("Windows server"), linkify=True)
    kind = columns.ChoiceFieldColumn(verbose_name=_("Kind"))
    status = columns.ChoiceFieldColumn(verbose_name=_("Status"))
    destination = columns.ChoiceFieldColumn(verbose_name=_("Destination"))
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
            "destination",
            "branch_name",
            "job",
            "started",
            "completed",
            "error",
            "created",
        )
        default_columns = (
            "id",
            "windows_server",
            "kind",
            "status",
            "destination",
            "branch_name",
            "started",
            "completed",
        )


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
