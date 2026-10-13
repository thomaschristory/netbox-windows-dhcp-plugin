from django import forms
from django.utils.translation import gettext_lazy as _
from ipam.models import VRF
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
from netbox_dhcp.models import DHCPServer, HostReservation, Option, OptionDefinition, Subnet
from utilities.forms import add_blank_choice
from utilities.forms.fields import (
    CSVChoiceField,
    CSVModelChoiceField,
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
    JSONField,
    TagFilterField,
)
from utilities.forms.rendering import FieldSet

from .choices import (
    ClassTypeChoices,
    FailoverModeChoices,
    FailoverServerRoleChoices,
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
from .models import (
    ExclusionRange,
    FailoverRelationship,
    ImportProposal,
    ImportProposalItem,
    MacFilter,
    SyncRun,
    WindowsClass,
    WindowsOptionDefinition,
    WindowsOptionQualifier,
    WindowsPolicy,
    WindowsReservation,
    WindowsScope,
    WindowsServer,
)

BOOLEAN_CHOICES = (("", "---------"), ("True", _("Yes")), ("False", _("No")))


def _server_field(label=None, required=True, **kwargs):
    return DynamicModelChoiceField(
        queryset=DHCPServer.objects.all(), required=required, label=label or _("DHCP server"), **kwargs
    )


def _subnet_field(required=True, **kwargs):
    return DynamicModelChoiceField(queryset=Subnet.objects.all(), required=required, label=_("Subnet"), **kwargs)


def _class_field(class_type, label, required=False):
    return DynamicModelChoiceField(
        queryset=WindowsClass.objects.filter(class_type=class_type),
        required=required,
        label=label,
        query_params={"class_type": class_type},
    )


def _filter_server_field(label=None):
    return DynamicModelMultipleChoiceField(
        queryset=DHCPServer.objects.all(), required=False, label=label or _("DHCP server")
    )


def _filter_subnet_field():
    return DynamicModelMultipleChoiceField(queryset=Subnet.objects.all(), required=False, label=_("Subnet"))


def _filter_choice_field(choices, label):
    return forms.MultipleChoiceField(choices=choices, required=False, label=label)


#
# Windows servers
#


class WindowsServerForm(PrimaryModelForm):
    dhcp_server = _server_field()
    import_vrf = DynamicModelChoiceField(queryset=VRF.objects.all(), required=False, label=_("Import VRF"))

    fieldsets = (
        FieldSet("dhcp_server", "host", "transport", "port", "credential_profile", "description", name=_("Connection")),
        FieldSet("winrm_auth", "winrm_https", "kerberos_realm", "kerberos_spn", name=_("WinRM")),
        FieldSet("ssh_host_key", name=_("SSH")),
        FieldSet("verify_tls", "computer_name", name=_("Security and remoting")),
        FieldSet("sync_enabled", "import_interval", "import_vrf", name=_("Sync")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = WindowsServer
        fields = (
            "dhcp_server",
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
    dhcp_server = CSVModelChoiceField(queryset=DHCPServer.objects.all(), to_field_name="name", label=_("DHCP server"))
    transport = CSVChoiceField(choices=TransportChoices, required=False, label=_("Transport"))
    winrm_auth = CSVChoiceField(choices=WinRMAuthChoices, required=False, label=_("WinRM authentication"))
    import_vrf = CSVModelChoiceField(
        queryset=VRF.objects.all(), to_field_name="name", required=False, label=_("Import VRF")
    )

    class Meta:
        model = WindowsServer
        fields = (
            "dhcp_server",
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
    winrm_https = forms.NullBooleanField(required=False, widget=forms.Select(choices=BOOLEAN_CHOICES))
    verify_tls = forms.NullBooleanField(required=False, widget=forms.Select(choices=BOOLEAN_CHOICES))
    sync_enabled = forms.NullBooleanField(required=False, widget=forms.Select(choices=BOOLEAN_CHOICES))
    import_interval = forms.IntegerField(min_value=1, required=False, label=_("Import interval"))

    fieldsets = (
        FieldSet(
            "transport",
            "credential_profile",
            "winrm_auth",
            "winrm_https",
            "verify_tls",
            "sync_enabled",
            "import_interval",
            "description",
        ),
    )
    nullable_fields = ("import_interval", "description", "comments")


class WindowsServerFilterForm(PrimaryModelFilterSetForm):
    model = WindowsServer
    dhcp_server_id = _filter_server_field()
    transport = _filter_choice_field(TransportChoices, _("Transport"))
    winrm_auth = _filter_choice_field(WinRMAuthChoices, _("WinRM authentication"))
    sync_enabled = forms.NullBooleanField(required=False, widget=forms.Select(choices=BOOLEAN_CHOICES))
    tag = TagFilterField(model)

    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("dhcp_server_id", "transport", "winrm_auth", "sync_enabled", name=_("Windows server")),
    )


#
# Scope and reservation settings
#


class WindowsScopeForm(NetBoxModelForm):
    subnet = _subnet_field()

    fieldsets = (
        FieldSet(
            "subnet",
            "state",
            "scope_type",
            "delay_ms",
            "max_bootp_clients",
            "activate_policies",
            "preference",
            name=_("Windows scope"),
        ),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = WindowsScope
        fields = (
            "subnet",
            "state",
            "scope_type",
            "delay_ms",
            "max_bootp_clients",
            "activate_policies",
            "preference",
            "tags",
        )


class WindowsScopeImportForm(NetBoxModelImportForm):
    subnet = CSVModelChoiceField(queryset=Subnet.objects.all(), to_field_name="name", label=_("Subnet"))
    state = CSVChoiceField(choices=ScopeStateChoices, required=False, label=_("State"))
    scope_type = CSVChoiceField(choices=ScopeTypeChoices, required=False, label=_("Type"))

    class Meta:
        model = WindowsScope
        fields = (
            "subnet",
            "state",
            "scope_type",
            "delay_ms",
            "max_bootp_clients",
            "activate_policies",
            "preference",
            "tags",
        )


class WindowsScopeBulkEditForm(NetBoxModelBulkEditForm):
    model = WindowsScope
    state = forms.ChoiceField(choices=add_blank_choice(ScopeStateChoices), required=False, label=_("State"))
    scope_type = forms.ChoiceField(choices=add_blank_choice(ScopeTypeChoices), required=False, label=_("Type"))
    delay_ms = forms.IntegerField(min_value=0, required=False, label=_("Offer delay (ms)"))
    activate_policies = forms.NullBooleanField(required=False, widget=forms.Select(choices=BOOLEAN_CHOICES))

    fieldsets = (FieldSet("state", "scope_type", "delay_ms", "activate_policies"),)
    nullable_fields = ()


class WindowsScopeFilterForm(NetBoxModelFilterSetForm):
    model = WindowsScope
    subnet_id = _filter_subnet_field()
    state = _filter_choice_field(ScopeStateChoices, _("State"))
    scope_type = _filter_choice_field(ScopeTypeChoices, _("Type"))
    tag = TagFilterField(model)

    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("subnet_id", "state", "scope_type", name=_("Windows scope")),
    )


class WindowsReservationForm(NetBoxModelForm):
    host_reservation = DynamicModelChoiceField(queryset=HostReservation.objects.all(), label=_("Host reservation"))

    fieldsets = (
        FieldSet("host_reservation", "reservation_type", name=_("Windows reservation")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = WindowsReservation
        fields = ("host_reservation", "reservation_type", "tags")


class WindowsReservationImportForm(NetBoxModelImportForm):
    host_reservation = CSVModelChoiceField(
        queryset=HostReservation.objects.all(), to_field_name="name", label=_("Host reservation")
    )
    reservation_type = CSVChoiceField(choices=ReservationTypeChoices, required=False, label=_("Type"))

    class Meta:
        model = WindowsReservation
        fields = ("host_reservation", "reservation_type", "tags")


class WindowsReservationBulkEditForm(NetBoxModelBulkEditForm):
    model = WindowsReservation
    reservation_type = forms.ChoiceField(
        choices=add_blank_choice(ReservationTypeChoices), required=False, label=_("Type")
    )

    fieldsets = (FieldSet("reservation_type"),)
    nullable_fields = ()


class WindowsReservationFilterForm(NetBoxModelFilterSetForm):
    model = WindowsReservation
    reservation_type = _filter_choice_field(ReservationTypeChoices, _("Type"))
    tag = TagFilterField(model)

    fieldsets = (FieldSet("q", "filter_id", "tag"), FieldSet("reservation_type", name=_("Windows reservation")))


#
# Exclusion ranges
#


class ExclusionRangeForm(PrimaryModelForm):
    subnet = _subnet_field()

    fieldsets = (
        FieldSet("subnet", "start_address", "end_address", "description", name=_("Exclusion range")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = ExclusionRange
        fields = ("subnet", "start_address", "end_address", "description", "comments", "tags")


class ExclusionRangeImportForm(PrimaryModelImportForm):
    subnet = CSVModelChoiceField(queryset=Subnet.objects.all(), to_field_name="name", label=_("Subnet"))

    class Meta:
        model = ExclusionRange
        fields = ("subnet", "start_address", "end_address", "description", "comments", "tags")


class ExclusionRangeBulkEditForm(PrimaryModelBulkEditForm):
    model = ExclusionRange
    fieldsets = (FieldSet("description"),)
    nullable_fields = ("description", "comments")


class ExclusionRangeFilterForm(PrimaryModelFilterSetForm):
    model = ExclusionRange
    subnet_id = _filter_subnet_field()
    tag = TagFilterField(model)

    fieldsets = (FieldSet("q", "filter_id", "tag"), FieldSet("subnet_id", name=_("Exclusion range")))


#
# Classes and option definitions
#


class WindowsClassForm(PrimaryModelForm):
    dhcp_server = _server_field()

    fieldsets = (
        FieldSet("dhcp_server", "name", "class_type", "family", "data", "description", name=_("Windows class")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = WindowsClass
        fields = ("dhcp_server", "name", "class_type", "family", "data", "description", "comments", "tags")


class WindowsClassImportForm(PrimaryModelImportForm):
    dhcp_server = CSVModelChoiceField(queryset=DHCPServer.objects.all(), to_field_name="name", label=_("DHCP server"))
    class_type = CSVChoiceField(choices=ClassTypeChoices, label=_("Type"))

    class Meta:
        model = WindowsClass
        fields = ("dhcp_server", "name", "class_type", "family", "data", "description", "comments", "tags")


class WindowsClassBulkEditForm(PrimaryModelBulkEditForm):
    model = WindowsClass
    fieldsets = (FieldSet("description"),)
    nullable_fields = ("description", "comments")


class WindowsClassFilterForm(PrimaryModelFilterSetForm):
    model = WindowsClass
    dhcp_server_id = _filter_server_field()
    class_type = _filter_choice_field(ClassTypeChoices, _("Type"))
    tag = TagFilterField(model)

    fieldsets = (FieldSet("q", "filter_id", "tag"), FieldSet("dhcp_server_id", "class_type", name=_("Windows class")))


class WindowsOptionDefinitionForm(NetBoxModelForm):
    option_definition = DynamicModelChoiceField(queryset=OptionDefinition.objects.all(), label=_("Option definition"))
    vendor_class = _class_field(ClassTypeChoices.VENDOR, _("Vendor class"))

    fieldsets = (
        FieldSet("option_definition", "vendor_class", name=_("Windows option definition")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = WindowsOptionDefinition
        fields = ("option_definition", "vendor_class", "tags")


class WindowsOptionDefinitionFilterForm(NetBoxModelFilterSetForm):
    model = WindowsOptionDefinition
    tag = TagFilterField(model)

    fieldsets = (FieldSet("q", "filter_id", "tag"),)


#
# Policies and option qualifiers
#


class WindowsPolicyForm(PrimaryModelForm):
    dhcp_server = _server_field()
    subnet = _subnet_field(required=False, query_params={"dhcp_server_id": "$dhcp_server"})
    criteria = JSONField(required=False, label=_("Criteria"))
    ip_ranges = JSONField(required=False, label=_("IP ranges"))

    fieldsets = (
        FieldSet("dhcp_server", "subnet", "name", "processing_order", "enabled", "description", name=_("Policy")),
        FieldSet("condition", "criteria", name=_("Conditions")),
        FieldSet("lease_duration", "ip_ranges", name=_("Settings")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = WindowsPolicy
        fields = (
            "dhcp_server",
            "subnet",
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
    enabled = forms.NullBooleanField(required=False, widget=forms.Select(choices=BOOLEAN_CHOICES))
    condition = forms.ChoiceField(choices=add_blank_choice(PolicyConditionChoices), required=False)

    fieldsets = (FieldSet("enabled", "condition", "description"),)
    nullable_fields = ("description", "comments")


class WindowsPolicyFilterForm(PrimaryModelFilterSetForm):
    model = WindowsPolicy
    dhcp_server_id = _filter_server_field()
    subnet_id = _filter_subnet_field()
    enabled = forms.NullBooleanField(required=False, widget=forms.Select(choices=BOOLEAN_CHOICES))
    tag = TagFilterField(model)

    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("dhcp_server_id", "subnet_id", "enabled", name=_("Policy")),
    )


class WindowsOptionQualifierForm(NetBoxModelForm):
    option = DynamicModelChoiceField(queryset=Option.objects.all(), label=_("Option"))
    user_class = _class_field(ClassTypeChoices.USER, _("User class"))
    vendor_class = _class_field(ClassTypeChoices.VENDOR, _("Vendor class"))
    policy = DynamicModelChoiceField(queryset=WindowsPolicy.objects.all(), required=False, label=_("Policy"))

    fieldsets = (
        FieldSet("option", "user_class", "vendor_class", "policy", name=_("Option qualifier")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = WindowsOptionQualifier
        fields = ("option", "user_class", "vendor_class", "policy", "tags")


class WindowsOptionQualifierFilterForm(NetBoxModelFilterSetForm):
    model = WindowsOptionQualifier
    tag = TagFilterField(model)

    fieldsets = (FieldSet("q", "filter_id", "tag"),)


#
# Failover and MAC filters
#


class FailoverRelationshipForm(PrimaryModelForm):
    primary_server = _server_field(label=_("Primary server"))
    partner_server = _server_field(label=_("Partner server"))
    subnets = DynamicModelMultipleChoiceField(
        queryset=Subnet.objects.all(),
        required=False,
        label=_("Subnets"),
        query_params={"dhcp_server_id": "$primary_server"},
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
        FieldSet("subnets", name=_("Scopes")),
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
            "subnets",
            "description",
            "comments",
            "tags",
        )


class FailoverRelationshipBulkEditForm(PrimaryModelBulkEditForm):
    model = FailoverRelationship
    mode = forms.ChoiceField(choices=add_blank_choice(FailoverModeChoices), required=False)
    server_role = forms.ChoiceField(choices=add_blank_choice(FailoverServerRoleChoices), required=False)

    fieldsets = (FieldSet("mode", "server_role", "description"),)
    nullable_fields = ("description", "comments")


class FailoverRelationshipFilterForm(PrimaryModelFilterSetForm):
    model = FailoverRelationship
    primary_server_id = _filter_server_field(_("Primary server"))
    partner_server_id = _filter_server_field(_("Partner server"))
    mode = _filter_choice_field(FailoverModeChoices, _("Mode"))
    tag = TagFilterField(model)

    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("primary_server_id", "partner_server_id", "mode", name=_("Failover relationship")),
    )


class MacFilterForm(PrimaryModelForm):
    dhcp_server = _server_field()

    fieldsets = (
        FieldSet("dhcp_server", "mac_address", "list", "description", name=_("MAC filter")),
        FieldSet("tags", name=_("Tags")),
    )

    class Meta:
        model = MacFilter
        fields = ("dhcp_server", "mac_address", "list", "description", "comments", "tags")


class MacFilterImportForm(PrimaryModelImportForm):
    dhcp_server = CSVModelChoiceField(queryset=DHCPServer.objects.all(), to_field_name="name", label=_("DHCP server"))
    list = CSVChoiceField(choices=MacFilterListChoices, label=_("List"))

    class Meta:
        model = MacFilter
        fields = ("dhcp_server", "mac_address", "list", "description", "comments", "tags")


class MacFilterBulkEditForm(PrimaryModelBulkEditForm):
    model = MacFilter
    list = forms.ChoiceField(choices=add_blank_choice(MacFilterListChoices), required=False)

    fieldsets = (FieldSet("list", "description"),)
    nullable_fields = ("description", "comments")


class MacFilterFilterForm(PrimaryModelFilterSetForm):
    model = MacFilter
    dhcp_server_id = _filter_server_field()
    list = _filter_choice_field(MacFilterListChoices, _("List"))
    tag = TagFilterField(model)

    fieldsets = (FieldSet("q", "filter_id", "tag"), FieldSet("dhcp_server_id", "list", name=_("MAC filter")))


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

    fieldsets = (FieldSet("q", "filter_id"), FieldSet("windows_server_id", "kind", "status", name=_("Sync run")))


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
