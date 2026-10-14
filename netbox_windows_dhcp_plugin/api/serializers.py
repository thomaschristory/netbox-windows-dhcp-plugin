from core.api.serializers_.jobs import JobSerializer
from django.contrib.contenttypes.models import ContentType
from drf_spectacular.utils import extend_schema_field
from ipam.api.serializers import VRFSerializer
from netbox.api.fields import ChoiceField, ContentTypeField
from netbox.api.serializers import NetBoxModelSerializer, PrimaryModelSerializer
from netbox_dhcp.api.serializers import (
    DHCPServerSerializer,
    HostReservationSerializer,
    OptionDefinitionSerializer,
    OptionSerializer,
    SubnetSerializer,
)
from rest_framework import serializers
from utilities.api import get_serializer_for_model

from ..choices import (
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
from ..models import (
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
from ..models.dhcp import VENDOR_OPTION_PARENTS

API = "plugins-api:netbox_windows_dhcp_plugin-api"
COMMON = ("id", "url", "display_url", "display")
TAIL = ("tags", "custom_fields", "created", "last_updated")
PRIMARY_TAIL = ("description", "comments", "owner", *TAIL)


def _url(model_name):
    return serializers.HyperlinkedIdentityField(view_name=f"{API}:{model_name}-detail")


class WindowsServerSerializer(PrimaryModelSerializer):
    url = _url("windowsserver")
    dhcp_server = DHCPServerSerializer(nested=True)
    transport = ChoiceField(choices=TransportChoices, required=False)
    winrm_auth = ChoiceField(choices=WinRMAuthChoices, required=False)
    import_vrf = VRFSerializer(nested=True, required=False, allow_null=True)

    class Meta:
        model = WindowsServer
        fields = (
            *COMMON,
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
            *PRIMARY_TAIL,
        )
        brief_fields = ("id", "url", "display", "host", "description")


class WindowsScopeSerializer(NetBoxModelSerializer):
    url = _url("windowsscope")
    subnet = SubnetSerializer(nested=True)
    state = ChoiceField(choices=ScopeStateChoices, required=False)
    scope_type = ChoiceField(choices=ScopeTypeChoices, required=False)

    class Meta:
        model = WindowsScope
        fields = (
            *COMMON,
            "subnet",
            "state",
            "scope_type",
            "delay_ms",
            "max_bootp_clients",
            "activate_policies",
            "preference",
            *TAIL,
        )
        brief_fields = ("id", "url", "display", "state")


class WindowsReservationSerializer(NetBoxModelSerializer):
    url = _url("windowsreservation")
    host_reservation = HostReservationSerializer(nested=True)
    reservation_type = ChoiceField(choices=ReservationTypeChoices, required=False)

    class Meta:
        model = WindowsReservation
        fields = (*COMMON, "host_reservation", "reservation_type", *TAIL)
        brief_fields = ("id", "url", "display", "reservation_type")


class ExclusionRangeSerializer(PrimaryModelSerializer):
    url = _url("exclusionrange")
    subnet = SubnetSerializer(nested=True)

    class Meta:
        model = ExclusionRange
        fields = (*COMMON, "subnet", "start_address", "end_address", *PRIMARY_TAIL)
        brief_fields = ("id", "url", "display", "start_address", "end_address", "description")


class WindowsClassSerializer(PrimaryModelSerializer):
    url = _url("windowsclass")
    dhcp_server = DHCPServerSerializer(nested=True)
    class_type = ChoiceField(choices=ClassTypeChoices)

    class Meta:
        model = WindowsClass
        fields = (*COMMON, "dhcp_server", "name", "class_type", "family", "data", *PRIMARY_TAIL)
        brief_fields = ("id", "url", "display", "name", "class_type", "description")


class WindowsOptionDefinitionSerializer(NetBoxModelSerializer):
    url = _url("windowsoptiondefinition")
    option_definition = OptionDefinitionSerializer(nested=True)

    class Meta:
        model = WindowsOptionDefinition
        fields = (*COMMON, "option_definition", "windows_type", "default_value", *TAIL)
        brief_fields = ("id", "url", "display")


class WindowsPolicySerializer(PrimaryModelSerializer):
    url = _url("windowspolicy")
    dhcp_server = DHCPServerSerializer(nested=True)
    subnet = SubnetSerializer(nested=True, required=False, allow_null=True)
    condition = ChoiceField(choices=PolicyConditionChoices, required=False)

    class Meta:
        model = WindowsPolicy
        fields = (
            *COMMON,
            "dhcp_server",
            "subnet",
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


class WindowsOptionQualifierSerializer(NetBoxModelSerializer):
    url = _url("windowsoptionqualifier")
    option = OptionSerializer(nested=True)
    user_class = WindowsClassSerializer(nested=True, required=False, allow_null=True)
    policy = WindowsPolicySerializer(nested=True, required=False, allow_null=True)

    class Meta:
        model = WindowsOptionQualifier
        fields = (*COMMON, "option", "user_class", "policy", *TAIL)
        brief_fields = ("id", "url", "display")


class FailoverRelationshipSerializer(PrimaryModelSerializer):
    url = _url("failoverrelationship")
    primary_server = DHCPServerSerializer(nested=True)
    partner_server = DHCPServerSerializer(nested=True)
    mode = ChoiceField(choices=FailoverModeChoices, required=False)
    server_role = ChoiceField(choices=FailoverServerRoleChoices, required=False, allow_blank=True)
    subnets = SubnetSerializer(nested=True, many=True, required=False)

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
            "subnets",
            *PRIMARY_TAIL,
        )
        brief_fields = ("id", "url", "display", "name", "mode", "description")


class MacFilterSerializer(PrimaryModelSerializer):
    url = _url("macfilter")
    dhcp_server = DHCPServerSerializer(nested=True)
    list = ChoiceField(choices=MacFilterListChoices)

    class Meta:
        model = MacFilter
        fields = (*COMMON, "dhcp_server", "mac_address", "list", *PRIMARY_TAIL)
        brief_fields = ("id", "url", "display", "mac_address", "list", "description")


class MacFilterListsSerializer(NetBoxModelSerializer):
    url = _url("macfilterlists")
    dhcp_server = DHCPServerSerializer(nested=True)

    class Meta:
        model = MacFilterLists
        fields = (*COMMON, "dhcp_server", "allow_list_enabled", "deny_list_enabled", *TAIL)
        brief_fields = ("id", "url", "display", "allow_list_enabled", "deny_list_enabled")


class WindowsVendorOptionDefinitionSerializer(PrimaryModelSerializer):
    url = _url("windowsvendoroptiondefinition")
    vendor_class = WindowsClassSerializer(nested=True)

    class Meta:
        model = WindowsVendorOptionDefinition
        fields = (
            *COMMON,
            "vendor_class",
            "code",
            "name",
            "windows_type",
            "multi_valued",
            "default_value",
            *PRIMARY_TAIL,
        )
        brief_fields = ("id", "url", "display", "code", "name", "description")


class WindowsVendorOptionSerializer(NetBoxModelSerializer):
    url = _url("windowsvendoroption")
    definition = WindowsVendorOptionDefinitionSerializer(nested=True)
    # Written as "app_label.model" plus assigned_object_id, read back with the parent nested, like netbox-plugin-dhcp
    # options.
    assigned_object_type = ContentTypeField(queryset=ContentType.objects.filter(VENDOR_OPTION_PARENTS))
    assigned_object = serializers.SerializerMethodField(read_only=True)
    user_class = WindowsClassSerializer(nested=True, required=False, allow_null=True)
    policy = WindowsPolicySerializer(nested=True, required=False, allow_null=True)

    class Meta:
        model = WindowsVendorOption
        fields = (
            *COMMON,
            "definition",
            "assigned_object_type",
            "assigned_object_id",
            "assigned_object",
            "user_class",
            "policy",
            "value",
            *TAIL,
        )
        brief_fields = ("id", "url", "display", "value")

    @extend_schema_field(serializers.JSONField(allow_null=True))
    def get_assigned_object(self, instance):
        if instance.assigned_object is None:
            return None
        serializer = get_serializer_for_model(instance.assigned_object)
        return serializer(instance.assigned_object, nested=True, context={"request": self.context["request"]}).data


class SyncRunSerializer(NetBoxModelSerializer):
    url = _url("syncrun")
    windows_server = WindowsServerSerializer(nested=True, read_only=True)
    kind = ChoiceField(choices=SyncKindChoices, read_only=True)
    status = ChoiceField(choices=SyncStatusChoices, read_only=True)
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
