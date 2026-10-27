from core.api.serializers_.jobs import JobSerializer
from dcim.api.serializers import DeviceSerializer, MACAddressSerializer
from drf_spectacular.utils import extend_schema_field
from ipam.api.serializers import IPAddressSerializer, IPRangeSerializer, PrefixSerializer, VRFSerializer
from netbox.api.fields import ChoiceField, ContentTypeField, SerializedPKRelatedField
from netbox.api.serializers import NetBoxModelSerializer, PrimaryModelSerializer
from rest_framework import serializers
from virtualization.api.serializers import VirtualMachineSerializer

from ..choices import (
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
from ..models import (
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
from ..ui.options import OptionNames

API = "plugins-api:netbox_windows_dhcp_plugin-api"
COMMON = ("id", "url", "display_url", "display")
TAIL = ("tags", "custom_fields", "created", "last_updated")
PRIMARY_TAIL = ("description", "comments", "owner", *TAIL)


def _url(model_name):
    return serializers.HyperlinkedIdentityField(view_name=f"{API}:{model_name}-detail")


class WindowsServerSerializer(PrimaryModelSerializer):
    url = _url("windowsserver")
    device = DeviceSerializer(nested=True, required=False, allow_null=True)
    virtual_machine = VirtualMachineSerializer(nested=True, required=False, allow_null=True)
    transport = ChoiceField(choices=TransportChoices, required=False)
    winrm_auth = ChoiceField(choices=WinRMAuthChoices, required=False)
    import_vrf = VRFSerializer(nested=True, required=False, allow_null=True)

    class Meta:
        model = WindowsServer
        fields = (
            *COMMON,
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
            "reported_names",
            "import_vrf",
            "sync_enabled",
            "import_interval",
            *PRIMARY_TAIL,
        )
        brief_fields = ("id", "url", "display", "name", "description")


class SuperscopeSerializer(NetBoxModelSerializer):
    url = _url("superscope")
    server = WindowsServerSerializer(nested=True)

    class Meta:
        model = Superscope
        fields = (*COMMON, "server", "name", "description", "comments", *TAIL)
        brief_fields = ("id", "url", "display", "name", "description")


class ScopeSerializer(NetBoxModelSerializer):
    url = _url("scope")
    server = WindowsServerSerializer(nested=True)
    prefix = PrefixSerializer(nested=True)
    ip_range = IPRangeSerializer(nested=True, required=False, allow_null=True)
    superscope = SuperscopeSerializer(nested=True, required=False, allow_null=True)
    state = ChoiceField(choices=ScopeStateChoices, required=False)
    scope_type = ChoiceField(choices=ScopeTypeChoices, required=False)
    # How Windows names the scope (its network address), and its address family: both follow from the prefix.
    scope_id = serializers.CharField(read_only=True)
    family = serializers.IntegerField(read_only=True)

    class Meta:
        model = Scope
        fields = (
            *COMMON,
            "server",
            "prefix",
            "ip_range",
            "superscope",
            "scope_id",
            "family",
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
            *TAIL,
        )
        brief_fields = ("id", "url", "display", "name", "scope_id", "state")


class ReservationSerializer(NetBoxModelSerializer):
    url = _url("reservation")
    scope = ScopeSerializer(nested=True)
    ip_address = IPAddressSerializer(nested=True)
    mac_address = MACAddressSerializer(nested=True, required=False, allow_null=True)
    reservation_type = ChoiceField(choices=ReservationTypeChoices, required=False)

    class Meta:
        model = Reservation
        fields = (
            *COMMON,
            "scope",
            "ip_address",
            "mac_address",
            "client_id",
            "duid",
            "iaid",
            "name",
            "description",
            "reservation_type",
            "comments",
            *TAIL,
        )
        brief_fields = ("id", "url", "display", "name", "ip_address")


class ExclusionRangeSerializer(NetBoxModelSerializer):
    url = _url("exclusionrange")
    scope = ScopeSerializer(nested=True)

    class Meta:
        model = ExclusionRange
        fields = (*COMMON, "scope", "start_address", "end_address", "description", "comments", *TAIL)
        brief_fields = ("id", "url", "display", "start_address", "end_address", "description")


class WindowsClassSerializer(PrimaryModelSerializer):
    url = _url("windowsclass")
    server = WindowsServerSerializer(nested=True)
    class_type = ChoiceField(choices=ClassTypeChoices)
    family = ChoiceField(choices=FamilyChoices, required=False)

    class Meta:
        model = WindowsClass
        fields = (*COMMON, "server", "name", "class_type", "family", "data", *PRIMARY_TAIL)
        brief_fields = ("id", "url", "display", "name", "class_type", "description")


class OptionDefinitionSerializer(NetBoxModelSerializer):
    url = _url("optiondefinition")
    server = WindowsServerSerializer(nested=True)
    family = ChoiceField(choices=FamilyChoices, required=False)
    vendor_class = WindowsClassSerializer(nested=True, required=False, allow_null=True)

    class Meta:
        model = OptionDefinition
        fields = (
            *COMMON,
            "server",
            "family",
            "vendor_class",
            "code",
            "name",
            "windows_type",
            "multi_valued",
            "default_value",
            "description",
            *TAIL,
        )
        brief_fields = ("id", "url", "display", "code", "name")


class WindowsPolicySerializer(PrimaryModelSerializer):
    url = _url("windowspolicy")
    server = WindowsServerSerializer(nested=True)
    scope = ScopeSerializer(nested=True, required=False, allow_null=True)
    condition = ChoiceField(choices=PolicyConditionChoices, required=False)

    class Meta:
        model = WindowsPolicy
        fields = (
            *COMMON,
            "server",
            "scope",
            "name",
            "processing_order",
            "enabled",
            "condition",
            "criteria",
            "lease_duration",
            "ip_ranges",
            *PRIMARY_TAIL,
        )
        brief_fields = ("id", "url", "display", "name", "description")


class OptionValueSerializer(NetBoxModelSerializer):
    url = _url("optionvalue")
    server = WindowsServerSerializer(nested=True)
    scope = ScopeSerializer(nested=True, required=False, allow_null=True)
    reservation = ReservationSerializer(nested=True, required=False, allow_null=True)
    family = ChoiceField(choices=FamilyChoices, required=False)
    vendor_class = WindowsClassSerializer(nested=True, required=False, allow_null=True)
    user_class = WindowsClassSerializer(nested=True, required=False, allow_null=True)
    policy = WindowsPolicySerializer(nested=True, required=False, allow_null=True)
    level = serializers.CharField(read_only=True)
    option_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = OptionValue
        fields = (
            *COMMON,
            "server",
            "scope",
            "reservation",
            "level",
            "family",
            "code",
            "option_name",
            "vendor_class",
            "user_class",
            "policy",
            "value",
            *TAIL,
        )
        brief_fields = ("id", "url", "display", "code", "value")

    def validate(self, data):
        # A reservation's value belongs to its scope too (OptionValue.clean() says so). The model is validated on a
        # copy here, so set the scope on what gets saved as well, or filters by scope miss the value.
        reservation = data.get("reservation")
        if reservation is not None and data.get("scope") is None:
            data["scope"] = reservation.scope
        return super().validate(data)

    @extend_schema_field(serializers.CharField())
    def get_option_name(self, instance):
        """The option's name: from the server's definition, else the standard name (empty when unknown)."""
        # Shared by every row of a list response, so definitions are read once per server, family and vendor class.
        names = self.context.setdefault("_option_names", OptionNames())
        return names.name(instance)


class FailoverRelationshipSerializer(PrimaryModelSerializer):
    url = _url("failoverrelationship")
    primary_server = WindowsServerSerializer(nested=True)
    partner_server = WindowsServerSerializer(nested=True)
    mode = ChoiceField(choices=FailoverModeChoices, required=False)
    server_role = ChoiceField(choices=FailoverServerRoleChoices, required=False, allow_blank=True)
    # Written as a list of scope IDs, read back as nested scopes (a nested serializer cannot be written).
    scopes = SerializedPKRelatedField(
        queryset=Scope.objects.all(), serializer=ScopeSerializer, nested=True, required=False, many=True
    )

    class Meta:
        model = FailoverRelationship
        fields = (
            *COMMON,
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
            *PRIMARY_TAIL,
        )
        brief_fields = ("id", "url", "display", "name", "mode", "description")

    def validate(self, data):
        primary = data.get("primary_server", getattr(self.instance, "primary_server", None))
        # Without scopes in the request, the ones the relationship already has must still suit a new primary.
        existing = self.instance.scopes.all() if self.instance is not None else []
        scopes = data.get("scopes", existing)
        if error := FailoverRelationship.scope_error(primary, scopes):
            raise serializers.ValidationError({"scopes": error})
        return super().validate(data)


class MacFilterSerializer(PrimaryModelSerializer):
    url = _url("macfilter")
    server = WindowsServerSerializer(nested=True)
    list = ChoiceField(choices=MacFilterListChoices)

    class Meta:
        model = MacFilter
        fields = (*COMMON, "server", "mac_address", "list", *PRIMARY_TAIL)
        brief_fields = ("id", "url", "display", "mac_address", "list", "description")


class MacFilterListsSerializer(NetBoxModelSerializer):
    url = _url("macfilterlists")
    server = WindowsServerSerializer(nested=True)

    class Meta:
        model = MacFilterLists
        fields = (*COMMON, "server", "allow_list_enabled", "deny_list_enabled", *TAIL)
        brief_fields = ("id", "url", "display", "allow_list_enabled", "deny_list_enabled")


class SyncRunSerializer(NetBoxModelSerializer):
    url = _url("syncrun")
    windows_server = WindowsServerSerializer(nested=True, read_only=True)
    kind = ChoiceField(choices=SyncKindChoices, read_only=True)
    status = ChoiceField(choices=SyncStatusChoices, read_only=True)
    destination = ChoiceField(choices=ImportDestinationChoices, read_only=True)
    job = JobSerializer(nested=True, read_only=True)

    class Meta:
        model = SyncRun
        fields = (
            *COMMON,
            "windows_server",
            "kind",
            "status",
            "job",
            "started",
            "completed",
            "destination",
            "branch_name",
            "summary",
            "error",
            *TAIL,
        )
        read_only_fields = ("started", "completed", "branch_name", "summary", "error")
        brief_fields = ("id", "url", "display", "kind", "status")


class ImportProposalSerializer(NetBoxModelSerializer):
    url = _url("importproposal")
    windows_server = WindowsServerSerializer(nested=True, read_only=True)
    sync_run = SyncRunSerializer(nested=True, read_only=True)
    status = ChoiceField(choices=ProposalStatusChoices, read_only=True)

    class Meta:
        model = ImportProposal
        fields = (*COMMON, "windows_server", "sync_run", "status", *TAIL)
        brief_fields = ("id", "url", "display", "status")


class ImportProposalItemSerializer(NetBoxModelSerializer):
    url = _url("importproposalitem")
    proposal = ImportProposalSerializer(nested=True, read_only=True)
    action = ChoiceField(choices=ProposalItemActionChoices, read_only=True)
    status = ChoiceField(choices=ProposalItemStatusChoices, read_only=True)
    object_type = ContentTypeField(read_only=True)

    class Meta:
        model = ImportProposalItem
        fields = (
            *COMMON,
            "proposal",
            "action",
            "status",
            "kind",
            "key",
            "object_type",
            "object_id",
            "before",
            "after",
            "error",
            *TAIL,
        )
        read_only_fields = ("kind", "key", "object_id", "before", "after", "error")
        brief_fields = ("id", "url", "display", "action", "status")
