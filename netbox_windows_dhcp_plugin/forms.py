from dcim.models import Device, MACAddress
from django import forms
from django.utils.translation import gettext_lazy as _
from ipam.models import VRF, IPAddress, IPRange, Prefix
from netbox.forms import (
    NetBoxModelBulkEditForm,
    NetBoxModelFilterSetForm,
    NetBoxModelForm,
    NetBoxModelImportForm,
    PrimaryModelBulkEditForm,
    PrimaryModelFilterSetForm,
    PrimaryModelForm,
    PrimaryModelImportForm,
)
from utilities.forms import add_blank_choice
from utilities.forms.fields import (
    CommentField,
    CSVChoiceField,
    CSVModelChoiceField,
    CSVTypedChoiceField,
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
    JSONField,
    TagFilterField,
)
from utilities.forms.rendering import FieldSet
from virtualization.models import VirtualMachine

from .choices import (
    ClassTypeChoices,
    FailoverModeChoices,
    FailoverServerRoleChoices,
    FamilyChoices,
    ImportDestinationChoices,
    MacFilterListChoices,
    PolicyConditionChoices,
    ProposalItemActionChoices,
    ProposalItemStatusChoices,
    ProposalStatusChoices,
    ReservationTypeChoices,
    ScopeStateChoices,
    ScopeTypeChoices,
    SyncKindChoices,
    SyncStatusChoices,
    TransportChoices,
    WinRMAuthChoices,
)
from .filtersets import OPTION_LEVELS
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

BOOLEAN_CHOICES = (("", "---------"), ("True", _("Yes")), ("False", _("No")))


def _server_field(label=None, required=True, **kwargs):
    return DynamicModelChoiceField(
        queryset=WindowsServer.objects.all(), required=required, label=label or _("Server"), **kwargs
    )


def _scope_field(required=True, **kwargs):
    kwargs.setdefault("query_params", {"server_id": "$server"})
    return DynamicModelChoiceField(queryset=Scope.objects.all(), required=required, label=_("Scope"), **kwargs)


def _class_field(class_type, label, required=False, server="$server", **query_params):
    return DynamicModelChoiceField(
        queryset=WindowsClass.objects.filter(class_type=class_type),
        required=required,
        label=label,
        query_params={"class_type": class_type, "server_id": server, **query_params},
    )


def _csv_server_field(required=True, label=None):
    return CSVModelChoiceField(
        queryset=WindowsServer.objects.all(), to_field_name="name", required=required, label=label or _("Server")
    )


def _csv_scope_field(required=True):
    # Scope names repeat (and may be empty), so CSV rows name a scope by its ID.
    return CSVModelChoiceField(
        queryset=Scope.objects.all(), required=required, label=_("Scope"), help_text=_("Scope ID")
    )


def _narrow_by_server(form, data, *field_names):
    """Names repeat from server to server (Windows ships the same classes everywhere): narrow to the row's server."""
    server = data.get("server") if data else None
    if not server:
        return
    for name in field_names:
        form.fields[name].queryset = form.fields[name].queryset.filter(server__name=server)


def _csv_family_field():
    # Left out of the CSV, the model default (IPv4) applies.
    return CSVTypedChoiceField(choices=FamilyChoices, coerce=int, required=False, label=_("Address family"))


def _filter_server_field(label=None):
    return DynamicModelMultipleChoiceField(
        queryset=WindowsServer.objects.all(), required=False, label=label or _("Server")
    )


def _filter_scope_field():
    return DynamicModelMultipleChoiceField(
        queryset=Scope.objects.all(), required=False, label=_("Scope"), query_params={"server_id": "$server_id"}
    )


def _filter_choice_field(choices, label):
    return forms.MultipleChoiceField(choices=choices, required=False, label=label)


def _boolean_field(label=None):
    return forms.NullBooleanField(required=False, widget=forms.Select(choices=BOOLEAN_CHOICES), label=label)


#
# Windows servers
#


class WindowsServerForm(PrimaryModelForm):
    device = DynamicModelChoiceField(queryset=Device.objects.all(), required=False, label=_("Device"))
    virtual_machine = DynamicModelChoiceField(
        queryset=VirtualMachine.objects.all(), required=False, label=_("Virtual machine")
    )
    import_vrf = DynamicModelChoiceField(queryset=VRF.objects.all(), required=False, label=_("Import VRF"))

    fieldsets = (
        FieldSet("name", "device", "virtual_machine", "description", name=_("Server")),
        FieldSet("host", "transport", "port", "credential_profile", name=_("Connection")),
        FieldSet("winrm_auth", "winrm_https", "kerberos_realm", "kerberos_spn", name=_("WinRM")),
        FieldSet("ssh_host_key", name=_("SSH")),
        FieldSet("verify_tls", "computer_name", name=_("Security and remoting")),
        FieldSet("sync_enabled", "import_interval", "import_vrf", name=_("Sync")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = WindowsServer
        fields = (
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
            "kerberos_realm",
            "kerberos_spn",
            "ssh_host_key",
            "computer_name",
            "import_vrf",
            "sync_enabled",
            "import_interval",
            "description",
            "comments",
            "tags",
        )


class WindowsServerImportForm(PrimaryModelImportForm):
    device = CSVModelChoiceField(queryset=Device.objects.all(), to_field_name="name", required=False, label=_("Device"))
    virtual_machine = CSVModelChoiceField(
        queryset=VirtualMachine.objects.all(), to_field_name="name", required=False, label=_("Virtual machine")
    )
    transport = CSVChoiceField(choices=TransportChoices, required=False, label=_("Transport"))
    winrm_auth = CSVChoiceField(choices=WinRMAuthChoices, required=False, label=_("WinRM authentication"))
    import_vrf = CSVModelChoiceField(
        queryset=VRF.objects.all(), to_field_name="name", required=False, label=_("Import VRF")
    )

    class Meta:
        model = WindowsServer
        fields = (
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
            "kerberos_realm",
            "kerberos_spn",
            "computer_name",
            "import_vrf",
            "sync_enabled",
            "import_interval",
            "description",
            "comments",
            "tags",
        )


class WindowsServerBulkEditForm(PrimaryModelBulkEditForm):
    model = WindowsServer
    transport = forms.ChoiceField(choices=add_blank_choice(TransportChoices), required=False, label=_("Transport"))
    credential_profile = forms.CharField(max_length=100, required=False, label=_("Credential profile"))
    winrm_auth = forms.ChoiceField(
        choices=add_blank_choice(WinRMAuthChoices), required=False, label=_("WinRM authentication")
    )
    winrm_https = _boolean_field()
    verify_tls = _boolean_field()
    sync_enabled = _boolean_field()
    import_interval = forms.IntegerField(min_value=1, required=False, label=_("Import interval"))
    import_vrf = DynamicModelChoiceField(queryset=VRF.objects.all(), required=False, label=_("Import VRF"))

    fieldsets = (
        FieldSet(
            "transport",
            "credential_profile",
            "winrm_auth",
            "winrm_https",
            "verify_tls",
            "sync_enabled",
            "import_interval",
            "import_vrf",
            "description",
        ),
    )
    nullable_fields = ("import_interval", "import_vrf", "description", "comments")


class WindowsServerFilterForm(PrimaryModelFilterSetForm):
    model = WindowsServer
    device_id = DynamicModelMultipleChoiceField(queryset=Device.objects.all(), required=False, label=_("Device"))
    virtual_machine_id = DynamicModelMultipleChoiceField(
        queryset=VirtualMachine.objects.all(), required=False, label=_("Virtual machine")
    )
    transport = _filter_choice_field(TransportChoices, _("Transport"))
    winrm_auth = _filter_choice_field(WinRMAuthChoices, _("WinRM authentication"))
    sync_enabled = _boolean_field()
    tag = TagFilterField(model)

    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("device_id", "virtual_machine_id", name=_("Runs on")),
        FieldSet("transport", "winrm_auth", "sync_enabled", name=_("Windows server")),
    )


#
# Superscopes, scopes and reservations
#


class SuperscopeForm(NetBoxModelForm):
    server = _server_field()
    comments = CommentField()

    fieldsets = (
        FieldSet("server", "name", "description", name=_("Superscope")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = Superscope
        fields = ("server", "name", "description", "comments", "tags")


class SuperscopeImportForm(NetBoxModelImportForm):
    server = _csv_server_field()

    class Meta:
        model = Superscope
        fields = ("server", "name", "description", "comments", "tags")


class SuperscopeBulkEditForm(NetBoxModelBulkEditForm):
    model = Superscope
    description = forms.CharField(max_length=200, required=False, label=_("Description"))
    comments = CommentField()

    fieldsets = (FieldSet("description"),)
    nullable_fields = ("description", "comments")


class SuperscopeFilterForm(NetBoxModelFilterSetForm):
    model = Superscope
    server_id = _filter_server_field()
    tag = TagFilterField(model)

    fieldsets = (FieldSet("q", "filter_id", "tag"), FieldSet("server_id", name=_("Superscope")))


SCOPE_FIELDS = (
    "server",
    "prefix",
    "ip_range",
    "superscope",
    "name",
    "description",
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
    "comments",
    "tags",
)


class ScopeForm(NetBoxModelForm):
    server = _server_field()
    # quick_add: create the prefix and the IP range from this form, as for a reservation's IP and MAC address. The
    # pop-up cannot see the prefix chosen here, so a range outside it is caught when the scope is saved.
    prefix = DynamicModelChoiceField(queryset=Prefix.objects.all(), label=_("Prefix"), selector=True, quick_add=True)
    ip_range = DynamicModelChoiceField(
        queryset=IPRange.objects.all(),
        required=False,
        label=_("IP range"),
        selector=True,
        quick_add=True,
        help_text=_("IPv4 scopes: the addresses the scope hands out (start and end of the scope)"),
    )
    superscope = DynamicModelChoiceField(
        queryset=Superscope.objects.all(),
        required=False,
        label=_("Superscope"),
        query_params={"server_id": "$server"},
    )
    comments = CommentField()

    fieldsets = (
        FieldSet("server", "name", "prefix", "ip_range", "superscope", "state", "description", name=_("Scope")),
        FieldSet(
            "scope_type",
            "lease_duration",
            "delay_ms",
            "max_bootp_clients",
            "activate_policies",
            name=_("IPv4 settings"),
        ),
        FieldSet(
            "preference",
            "preferred_lifetime",
            "valid_lifetime",
            "renew_timer",
            "rebind_timer",
            name=_("DHCPv6 settings"),
        ),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = Scope
        fields = SCOPE_FIELDS


class ScopeImportForm(NetBoxModelImportForm):
    server = _csv_server_field()
    prefix = CSVModelChoiceField(
        queryset=Prefix.objects.all(),
        to_field_name="prefix",
        label=_("Prefix"),
        help_text=_("For example 10.0.0.0/24; must be unique across VRFs"),
    )
    ip_range = CSVModelChoiceField(
        queryset=IPRange.objects.all(), required=False, label=_("IP range"), help_text=_("IP range ID")
    )
    superscope = CSVModelChoiceField(
        queryset=Superscope.objects.all(), to_field_name="name", required=False, label=_("Superscope")
    )
    state = CSVChoiceField(choices=ScopeStateChoices, required=False, label=_("State"))
    scope_type = CSVChoiceField(choices=ScopeTypeChoices, required=False, label=_("Type"))
    # Left out of the CSV, the model default (no delay) applies.
    delay_ms = forms.IntegerField(min_value=0, required=False, label=_("Offer delay (ms)"))

    class Meta:
        model = Scope
        fields = SCOPE_FIELDS

    def __init__(self, data=None, *args, **kwargs):
        super().__init__(data, *args, **kwargs)
        _narrow_by_server(self, data, "superscope")


class ScopeBulkEditForm(NetBoxModelBulkEditForm):
    model = Scope
    state = forms.ChoiceField(choices=add_blank_choice(ScopeStateChoices), required=False, label=_("State"))
    scope_type = forms.ChoiceField(choices=add_blank_choice(ScopeTypeChoices), required=False, label=_("Type"))
    lease_duration = forms.IntegerField(min_value=0, required=False, label=_("Lease duration (s)"))
    delay_ms = forms.IntegerField(min_value=0, required=False, label=_("Offer delay (ms)"))
    max_bootp_clients = forms.IntegerField(min_value=0, required=False, label=_("Max BOOTP clients"))
    activate_policies = _boolean_field(_("Activate policies"))
    preferred_lifetime = forms.IntegerField(min_value=0, required=False, label=_("Preferred lifetime (s)"))
    valid_lifetime = forms.IntegerField(min_value=0, required=False, label=_("Valid lifetime (s)"))
    comments = CommentField()

    fieldsets = (
        FieldSet("state", name=_("Scope")),
        FieldSet("scope_type", "lease_duration", "delay_ms", "max_bootp_clients", "activate_policies", name=_("IPv4")),
        FieldSet("preferred_lifetime", "valid_lifetime", name=_("DHCPv6")),
    )
    nullable_fields = ("lease_duration", "max_bootp_clients", "preferred_lifetime", "valid_lifetime", "comments")


class ScopeFilterForm(NetBoxModelFilterSetForm):
    model = Scope
    server_id = _filter_server_field()
    superscope_id = DynamicModelMultipleChoiceField(
        queryset=Superscope.objects.all(),
        required=False,
        label=_("Superscope"),
        query_params={"server_id": "$server_id"},
    )
    family = forms.ChoiceField(choices=add_blank_choice(FamilyChoices), required=False, label=_("Address family"))
    state = _filter_choice_field(ScopeStateChoices, _("State"))
    scope_type = _filter_choice_field(ScopeTypeChoices, _("Type"))
    tag = TagFilterField(model)

    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("server_id", "superscope_id", "family", "state", "scope_type", name=_("Scope")),
    )


RESERVATION_FIELDS = (
    "scope",
    "ip_address",
    "name",
    "description",
    "mac_address",
    "client_id",
    "reservation_type",
    "duid",
    "iaid",
    "comments",
    "tags",
)


class ReservationForm(NetBoxModelForm):
    # Only to narrow the scope list; a reservation belongs to its scope's server.
    server = _server_field(required=False)
    scope = _scope_field()
    # quick_add: a "+" button that creates the IP or MAC address in a pop-up and selects it, so a new reservation does
    # not need a trip to IPAM or Devices first.
    ip_address = DynamicModelChoiceField(
        queryset=IPAddress.objects.all(), label=_("IP address"), selector=True, quick_add=True
    )
    mac_address = DynamicModelChoiceField(
        queryset=MACAddress.objects.all(), required=False, label=_("MAC address"), selector=True, quick_add=True
    )
    comments = CommentField()

    fieldsets = (
        FieldSet("server", "scope", "ip_address", "name", "description", name=_("Reservation")),
        FieldSet("mac_address", "client_id", "reservation_type", name=_("IPv4 client")),
        FieldSet("duid", "iaid", name=_("DHCPv6 client")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = Reservation
        fields = RESERVATION_FIELDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.scope_id:
            self.initial.setdefault("server", self.instance.scope.server_id)


class ReservationImportForm(NetBoxModelImportForm):
    scope = _csv_scope_field()
    ip_address = CSVModelChoiceField(
        queryset=IPAddress.objects.all(),
        to_field_name="address",
        label=_("IP address"),
        help_text=_("With its mask, for example 10.0.0.5/24; must be unique across VRFs"),
    )
    mac_address = CSVModelChoiceField(
        queryset=MACAddress.objects.all(), to_field_name="mac_address", required=False, label=_("MAC address")
    )
    reservation_type = CSVChoiceField(choices=ReservationTypeChoices, required=False, label=_("Type"))

    class Meta:
        model = Reservation
        fields = RESERVATION_FIELDS


class ReservationBulkEditForm(NetBoxModelBulkEditForm):
    model = Reservation
    reservation_type = forms.ChoiceField(
        choices=add_blank_choice(ReservationTypeChoices), required=False, label=_("Type")
    )
    comments = CommentField()

    fieldsets = (FieldSet("reservation_type"),)
    nullable_fields = ("comments",)


class ReservationFilterForm(NetBoxModelFilterSetForm):
    model = Reservation
    server_id = _filter_server_field()
    scope_id = _filter_scope_field()
    family = forms.ChoiceField(choices=add_blank_choice(FamilyChoices), required=False, label=_("Address family"))
    reservation_type = _filter_choice_field(ReservationTypeChoices, _("Type"))
    tag = TagFilterField(model)

    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("server_id", "scope_id", "family", "reservation_type", name=_("Reservation")),
    )


#
# Exclusion ranges
#


class ExclusionRangeForm(NetBoxModelForm):
    server = _server_field(required=False)
    scope = _scope_field()
    comments = CommentField()

    fieldsets = (
        FieldSet("server", "scope", "start_address", "end_address", "description", name=_("Exclusion range")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = ExclusionRange
        fields = ("scope", "start_address", "end_address", "description", "comments", "tags")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.scope_id:
            self.initial.setdefault("server", self.instance.scope.server_id)


class ExclusionRangeImportForm(NetBoxModelImportForm):
    scope = _csv_scope_field()

    class Meta:
        model = ExclusionRange
        fields = ("scope", "start_address", "end_address", "description", "comments", "tags")


class ExclusionRangeBulkEditForm(NetBoxModelBulkEditForm):
    model = ExclusionRange
    description = forms.CharField(max_length=200, required=False, label=_("Description"))
    comments = CommentField()

    fieldsets = (FieldSet("description"),)
    nullable_fields = ("description", "comments")


class ExclusionRangeFilterForm(NetBoxModelFilterSetForm):
    model = ExclusionRange
    server_id = _filter_server_field()
    scope_id = _filter_scope_field()
    tag = TagFilterField(model)

    fieldsets = (FieldSet("q", "filter_id", "tag"), FieldSet("server_id", "scope_id", name=_("Exclusion range")))


#
# Classes and option definitions
#


class WindowsClassForm(PrimaryModelForm):
    server = _server_field()

    fieldsets = (
        FieldSet("server", "name", "class_type", "family", "data", "description", name=_("Windows class")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = WindowsClass
        fields = ("server", "name", "class_type", "family", "data", "description", "comments", "tags")


class WindowsClassImportForm(PrimaryModelImportForm):
    server = _csv_server_field()
    family = _csv_family_field()
    class_type = CSVChoiceField(choices=ClassTypeChoices, label=_("Type"))

    class Meta:
        model = WindowsClass
        fields = ("server", "name", "class_type", "family", "data", "description", "comments", "tags")


class WindowsClassBulkEditForm(PrimaryModelBulkEditForm):
    model = WindowsClass
    fieldsets = (FieldSet("description"),)
    nullable_fields = ("description", "comments")


class WindowsClassFilterForm(PrimaryModelFilterSetForm):
    model = WindowsClass
    server_id = _filter_server_field()
    class_type = _filter_choice_field(ClassTypeChoices, _("Type"))
    family = _filter_choice_field(FamilyChoices, _("Address family"))
    tag = TagFilterField(model)

    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("server_id", "class_type", "family", name=_("Windows class")),
    )


OPTION_DEFINITION_FIELDS = (
    "server",
    "family",
    "vendor_class",
    "code",
    "name",
    "windows_type",
    "multi_valued",
    "default_value",
    "description",
    "tags",
)


class OptionDefinitionForm(NetBoxModelForm):
    server = _server_field()
    vendor_class = _class_field(ClassTypeChoices.VENDOR, _("Vendor class"), family="$family")
    default_value = JSONField(required=False, label=_("Default value"), help_text=_('For example ["0"]'))

    fieldsets = (
        FieldSet("server", "family", "vendor_class", "code", "name", "description", name=_("Option definition")),
        FieldSet("windows_type", "multi_valued", "default_value", name=_("Value")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = OptionDefinition
        fields = OPTION_DEFINITION_FIELDS


class OptionDefinitionImportForm(NetBoxModelImportForm):
    server = _csv_server_field()
    family = _csv_family_field()
    vendor_class = CSVModelChoiceField(
        queryset=WindowsClass.objects.filter(class_type=ClassTypeChoices.VENDOR),
        to_field_name="name",
        required=False,
        label=_("Vendor class"),
    )
    # Left out, the model default (String) applies.
    windows_type = forms.CharField(max_length=50, required=False, label=_("Windows type"))

    class Meta:
        model = OptionDefinition
        fields = OPTION_DEFINITION_FIELDS

    def __init__(self, data=None, *args, **kwargs):
        super().__init__(data, *args, **kwargs)
        _narrow_by_server(self, data, "vendor_class")
        if data and str(data.get("family", "")).isdigit():
            self.fields["vendor_class"].queryset = self.fields["vendor_class"].queryset.filter(family=data["family"])


class OptionDefinitionBulkEditForm(NetBoxModelBulkEditForm):
    model = OptionDefinition
    windows_type = forms.CharField(max_length=50, required=False, label=_("Windows type"))
    multi_valued = _boolean_field(_("Multi-valued"))
    description = forms.CharField(required=False, label=_("Description"))

    fieldsets = (FieldSet("windows_type", "multi_valued", "description"),)
    nullable_fields = ("description",)


class OptionDefinitionFilterForm(NetBoxModelFilterSetForm):
    model = OptionDefinition
    server_id = _filter_server_field()
    family = _filter_choice_field(FamilyChoices, _("Address family"))
    vendor_class_id = DynamicModelMultipleChoiceField(
        queryset=WindowsClass.objects.filter(class_type=ClassTypeChoices.VENDOR),
        required=False,
        label=_("Vendor class"),
        query_params={"class_type": ClassTypeChoices.VENDOR, "server_id": "$server_id"},
    )
    has_vendor_class = _boolean_field(_("Has a vendor class"))
    code = forms.IntegerField(min_value=1, max_value=65535, required=False, label=_("Code"))
    multi_valued = _boolean_field(_("Multi-valued"))
    tag = TagFilterField(model)

    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet(
            "server_id",
            "family",
            "vendor_class_id",
            "has_vendor_class",
            "code",
            "multi_valued",
            name=_("Option definition"),
        ),
    )


#
# Policies and option values
#


class WindowsPolicyForm(PrimaryModelForm):
    server = _server_field()
    scope = _scope_field(required=False, help_text=_("Leave empty for a server-level policy"))
    criteria = JSONField(required=False, label=_("Criteria"))
    ip_ranges = JSONField(required=False, label=_("IP ranges"))

    fieldsets = (
        FieldSet("server", "scope", "name", "processing_order", "enabled", "description", name=_("Policy")),
        FieldSet("condition", "criteria", name=_("Conditions")),
        FieldSet("lease_duration", "ip_ranges", name=_("Settings")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = WindowsPolicy
        fields = (
            "server",
            "scope",
            "name",
            "processing_order",
            "enabled",
            "condition",
            "criteria",
            "lease_duration",
            "ip_ranges",
            "description",
            "comments",
            "tags",
        )


class WindowsPolicyBulkEditForm(PrimaryModelBulkEditForm):
    model = WindowsPolicy
    enabled = _boolean_field(_("Enabled"))
    condition = forms.ChoiceField(choices=add_blank_choice(PolicyConditionChoices), required=False)

    fieldsets = (FieldSet("enabled", "condition", "description"),)
    nullable_fields = ("description", "comments")


class WindowsPolicyFilterForm(PrimaryModelFilterSetForm):
    model = WindowsPolicy
    server_id = _filter_server_field()
    scope_id = _filter_scope_field()
    has_scope = _boolean_field(_("Scope-level"))
    enabled = _boolean_field(_("Enabled"))
    tag = TagFilterField(model)

    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("server_id", "scope_id", "has_scope", "enabled", name=_("Policy")),
    )


OPTION_VALUE_FIELDS = (
    "server",
    "scope",
    "reservation",
    "family",
    "code",
    "vendor_class",
    "user_class",
    "policy",
    "value",
    "tags",
)


class OptionValueForm(NetBoxModelForm):
    server = _server_field()
    scope = _scope_field(
        required=False,
        query_params={"server_id": "$server", "family": "$family"},
        help_text=_("Leave scope and reservation empty for a server-level value"),
    )
    reservation = DynamicModelChoiceField(
        queryset=Reservation.objects.all(),
        required=False,
        label=_("Reservation"),
        query_params={"server_id": "$server", "scope_id": "$scope"},
    )
    vendor_class = _class_field(ClassTypeChoices.VENDOR, _("Vendor class"), family="$family")
    user_class = _class_field(ClassTypeChoices.USER, _("User class"), family="$family")
    policy = DynamicModelChoiceField(
        queryset=WindowsPolicy.objects.all(),
        required=False,
        label=_("Policy"),
        query_params={"server_id": "$server", "scope_id": "$scope"},
    )
    value = JSONField(label=_("Value"), help_text=_('A list of strings, for example ["10.0.0.1", "10.0.0.2"]'))

    fieldsets = (
        FieldSet("server", "family", "code", "value", name=_("Option value")),
        FieldSet("scope", "reservation", name=_("Set on")),
        FieldSet("vendor_class", "user_class", "policy", name=_("Qualifiers")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = OptionValue
        fields = OPTION_VALUE_FIELDS


class OptionValueImportForm(NetBoxModelImportForm):
    server = _csv_server_field()
    family = _csv_family_field()
    scope = _csv_scope_field(required=False)
    reservation = CSVModelChoiceField(
        queryset=Reservation.objects.all(), required=False, label=_("Reservation"), help_text=_("Reservation ID")
    )
    vendor_class = CSVModelChoiceField(
        queryset=WindowsClass.objects.filter(class_type=ClassTypeChoices.VENDOR),
        to_field_name="name",
        required=False,
        label=_("Vendor class"),
    )
    user_class = CSVModelChoiceField(
        queryset=WindowsClass.objects.filter(class_type=ClassTypeChoices.USER),
        to_field_name="name",
        required=False,
        label=_("User class"),
    )
    policy = CSVModelChoiceField(
        queryset=WindowsPolicy.objects.all(), to_field_name="name", required=False, label=_("Policy")
    )

    class Meta:
        model = OptionValue
        fields = OPTION_VALUE_FIELDS

    def __init__(self, data=None, *args, **kwargs):
        super().__init__(data, *args, **kwargs)
        _narrow_by_server(self, data, "vendor_class", "user_class", "policy")
        if data and str(data.get("family", "")).isdigit():
            for name in ("vendor_class", "user_class"):
                self.fields[name].queryset = self.fields[name].queryset.filter(family=data["family"])
        if data and str(data.get("scope", "")).isdigit():
            # Server and scope policies may share a name.
            self.fields["policy"].queryset = self.fields["policy"].queryset.filter(scope_id=data["scope"])
        elif data:
            self.fields["policy"].queryset = self.fields["policy"].queryset.filter(scope__isnull=True)


class OptionValueBulkEditForm(NetBoxModelBulkEditForm):
    model = OptionValue
    value = JSONField(required=False, label=_("Value"), help_text=_('A list of strings, for example ["10.0.0.1"]'))

    fieldsets = (FieldSet("value"),)
    nullable_fields = ()


class OptionValueFilterForm(NetBoxModelFilterSetForm):
    model = OptionValue
    server_id = _filter_server_field()
    level = _filter_choice_field(OPTION_LEVELS, _("Level"))
    scope_id = _filter_scope_field()
    family = _filter_choice_field(FamilyChoices, _("Address family"))
    code = forms.IntegerField(min_value=1, max_value=65535, required=False, label=_("Code"))
    vendor_class_id = DynamicModelMultipleChoiceField(
        queryset=WindowsClass.objects.filter(class_type=ClassTypeChoices.VENDOR),
        required=False,
        label=_("Vendor class"),
        query_params={"class_type": ClassTypeChoices.VENDOR, "server_id": "$server_id"},
    )
    user_class_id = DynamicModelMultipleChoiceField(
        queryset=WindowsClass.objects.filter(class_type=ClassTypeChoices.USER),
        required=False,
        label=_("User class"),
        query_params={"class_type": ClassTypeChoices.USER, "server_id": "$server_id"},
    )
    policy_id = DynamicModelMultipleChoiceField(
        queryset=WindowsPolicy.objects.all(),
        required=False,
        label=_("Policy"),
        query_params={"server_id": "$server_id"},
    )
    tag = TagFilterField(model)

    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("server_id", "level", "scope_id", "family", "code", name=_("Option value")),
        FieldSet("vendor_class_id", "user_class_id", "policy_id", name=_("Qualifiers")),
    )


#
# Failover and MAC filters
#


class FailoverRelationshipForm(PrimaryModelForm):
    primary_server = _server_field(label=_("Primary server"))
    partner_server = _server_field(label=_("Partner server"))
    scopes = DynamicModelMultipleChoiceField(
        queryset=Scope.objects.all(),
        required=False,
        label=_("Scopes"),
        query_params={"server_id": "$primary_server", "family": 4},
    )

    fieldsets = (
        FieldSet("name", "primary_server", "partner_server", "mode", "description", name=_("Failover relationship")),
        FieldSet(
            "max_client_lead_time",
            "load_balance_percent",
            "reserve_percent",
            "server_role",
            "state_switch_interval",
            "shared_secret_profile",
            name=_("Settings"),
        ),
        FieldSet("scopes", name=_("Scopes")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = FailoverRelationship
        fields = (
            "name",
            "primary_server",
            "partner_server",
            "mode",
            "max_client_lead_time",
            "load_balance_percent",
            "reserve_percent",
            "server_role",
            "state_switch_interval",
            "shared_secret_profile",
            "scopes",
            "description",
            "comments",
            "tags",
        )

    def clean(self):
        super().clean()
        # The scope picker only filters what it offers; a posted form may still name any scope.
        primary = self.cleaned_data.get("primary_server")
        if error := FailoverRelationship.scope_error(primary, self.cleaned_data.get("scopes") or []):
            raise forms.ValidationError({"scopes": error})
        return self.cleaned_data


class FailoverRelationshipBulkEditForm(PrimaryModelBulkEditForm):
    model = FailoverRelationship
    mode = forms.ChoiceField(choices=add_blank_choice(FailoverModeChoices), required=False)
    server_role = forms.ChoiceField(choices=add_blank_choice(FailoverServerRoleChoices), required=False)

    fieldsets = (FieldSet("mode", "server_role", "description"),)
    nullable_fields = ("description", "comments")


class FailoverRelationshipFilterForm(PrimaryModelFilterSetForm):
    model = FailoverRelationship
    server_id = _filter_server_field(_("Server (either side)"))
    primary_server_id = _filter_server_field(_("Primary server"))
    partner_server_id = _filter_server_field(_("Partner server"))
    mode = _filter_choice_field(FailoverModeChoices, _("Mode"))
    tag = TagFilterField(model)

    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("server_id", "primary_server_id", "partner_server_id", "mode", name=_("Failover relationship")),
    )


class MacFilterForm(PrimaryModelForm):
    server = _server_field()

    fieldsets = (
        FieldSet("server", "mac_address", "list", "description", name=_("MAC filter")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = MacFilter
        fields = ("server", "mac_address", "list", "description", "comments", "tags")


class MacFilterImportForm(PrimaryModelImportForm):
    server = _csv_server_field()
    list = CSVChoiceField(choices=MacFilterListChoices, label=_("List"))

    class Meta:
        model = MacFilter
        fields = ("server", "mac_address", "list", "description", "comments", "tags")


class MacFilterBulkEditForm(PrimaryModelBulkEditForm):
    model = MacFilter
    list = forms.ChoiceField(choices=add_blank_choice(MacFilterListChoices), required=False)

    fieldsets = (FieldSet("list", "description"),)
    nullable_fields = ("description", "comments")


class MacFilterFilterForm(PrimaryModelFilterSetForm):
    model = MacFilter
    server_id = _filter_server_field()
    list = _filter_choice_field(MacFilterListChoices, _("List"))
    tag = TagFilterField(model)

    fieldsets = (FieldSet("q", "filter_id", "tag"), FieldSet("server_id", "list", name=_("MAC filter")))


class MacFilterListsForm(NetBoxModelForm):
    server = _server_field()

    fieldsets = (
        FieldSet("server", "allow_list_enabled", "deny_list_enabled", name=_("MAC filter lists")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = MacFilterLists
        fields = ("server", "allow_list_enabled", "deny_list_enabled", "tags")


class MacFilterListsBulkEditForm(NetBoxModelBulkEditForm):
    model = MacFilterLists
    allow_list_enabled = _boolean_field(_("Allow list enabled"))
    deny_list_enabled = _boolean_field(_("Deny list enabled"))

    fieldsets = (FieldSet("allow_list_enabled", "deny_list_enabled"),)
    nullable_fields = ()


class MacFilterListsFilterForm(NetBoxModelFilterSetForm):
    model = MacFilterLists
    server_id = _filter_server_field()
    allow_list_enabled = _boolean_field(_("Allow list enabled"))
    deny_list_enabled = _boolean_field(_("Deny list enabled"))
    tag = TagFilterField(model)

    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("server_id", "allow_list_enabled", "deny_list_enabled", name=_("MAC filter lists")),
    )


#
# Sync runs and import proposals (read-only)
#


class SyncRunFilterForm(NetBoxModelFilterSetForm):
    model = SyncRun
    windows_server_id = DynamicModelMultipleChoiceField(
        queryset=WindowsServer.objects.all(), required=False, label=_("Windows server")
    )
    kind = _filter_choice_field(SyncKindChoices, _("Kind"))
    status = _filter_choice_field(SyncStatusChoices, _("Status"))
    destination = _filter_choice_field(ImportDestinationChoices, _("Destination"))

    fieldsets = (
        FieldSet("q", "filter_id"),
        FieldSet("windows_server_id", "kind", "status", "destination", name=_("Sync run")),
    )


class ImportConfirmForm(forms.Form):
    """The hidden field of the page that confirms an import straight into main."""

    destination = forms.CharField(widget=forms.HiddenInput)


class ImportProposalFilterForm(NetBoxModelFilterSetForm):
    model = ImportProposal
    windows_server_id = DynamicModelMultipleChoiceField(
        queryset=WindowsServer.objects.all(), required=False, label=_("Windows server")
    )
    status = _filter_choice_field(ProposalStatusChoices, _("Status"))

    fieldsets = (FieldSet("q", "filter_id"), FieldSet("windows_server_id", "status", name=_("Import proposal")))


class ImportProposalItemFilterForm(NetBoxModelFilterSetForm):
    model = ImportProposalItem
    action = _filter_choice_field(ProposalItemActionChoices, _("Action"))
    status = _filter_choice_field(ProposalItemStatusChoices, _("Status"))

    fieldsets = (FieldSet("q", "filter_id"), FieldSet("action", "status", name=_("Item")))
