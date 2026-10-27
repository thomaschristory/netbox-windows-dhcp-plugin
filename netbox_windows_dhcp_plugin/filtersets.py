import contextlib
import ipaddress

import django_filters
from dcim.models import Device, MACAddress
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from ipam.models import VRF, IPAddress, IPRange, Prefix
from netbox.filtersets import NetBoxModelFilterSet, PrimaryModelFilterSet
from utilities.filtersets import register_filterset
from virtualization.models import VirtualMachine

from .choices import (
    ClassTypeChoices,
    FailoverModeChoices,
    FamilyChoices,
    ImportDestinationChoices,
    MacFilterListChoices,
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

__all__ = (
    "ExclusionRangeFilterSet",
    "FailoverRelationshipFilterSet",
    "ImportProposalFilterSet",
    "ImportProposalItemFilterSet",
    "MacFilterFilterSet",
    "MacFilterListsFilterSet",
    "OptionDefinitionFilterSet",
    "OptionValueFilterSet",
    "ReservationFilterSet",
    "ScopeFilterSet",
    "SuperscopeFilterSet",
    "SyncRunFilterSet",
    "WindowsClassFilterSet",
    "WindowsPolicyFilterSet",
    "WindowsServerFilterSet",
)

# Where an option value sits. A reservation's value also names its scope, so "scope" means "on the scope itself".
OPTION_LEVELS = (("server", _("Server")), ("scope", _("Scope")), ("reservation", _("Reservation")))


def _model_filter(field_name, queryset, label, **kwargs):
    return django_filters.ModelMultipleChoiceFilter(field_name=field_name, queryset=queryset, label=label, **kwargs)


def _server_id_filter(field_name="server", label=None):
    return _model_filter(field_name, WindowsServer.objects.all(), label or _("Windows server (ID)"))


def _server_name_filter(field_name="server", label=None):
    return _model_filter(
        f"{field_name}__name", WindowsServer.objects.all(), label or _("Windows server (name)"), to_field_name="name"
    )


def _scope_filter(field_name="scope"):
    return _model_filter(field_name, Scope.objects.all(), _("Scope (ID)"))


def _family_filter(field_name):
    # The address family of a scope is the one of its prefix.
    return django_filters.NumberFilter(field_name=field_name, lookup_expr="family", label=_("Address family"))


def _network_q(field_name, value):
    """Match a scope's prefix against an address or network typed in the search box, if it is one."""
    try:
        network = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError:
        return Q()
    return Q(**{f"{field_name}__net_contains_or_equals": str(network)})


@register_filterset
class WindowsServerFilterSet(PrimaryModelFilterSet):
    device_id = _model_filter("device", Device.objects.all(), _("Device (ID)"))
    device = _model_filter("device__name", Device.objects.all(), _("Device (name)"), to_field_name="name")
    virtual_machine_id = _model_filter("virtual_machine", VirtualMachine.objects.all(), _("Virtual machine (ID)"))
    virtual_machine = _model_filter(
        "virtual_machine__name", VirtualMachine.objects.all(), _("Virtual machine (name)"), to_field_name="name"
    )
    import_vrf_id = _model_filter("import_vrf", VRF.objects.all(), _("Import VRF (ID)"))
    transport = django_filters.MultipleChoiceFilter(choices=TransportChoices)
    winrm_auth = django_filters.MultipleChoiceFilter(choices=WinRMAuthChoices)

    class Meta:
        model = WindowsServer
        fields = (
            "id",
            "name",
            "host",
            "port",
            "credential_profile",
            "winrm_https",
            "verify_tls",
            "computer_name",
            "sync_enabled",
            "import_interval",
            "description",
        )

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value)
            | Q(host__icontains=value)
            | Q(computer_name__icontains=value)
            | Q(description__icontains=value)
        )


@register_filterset
class SuperscopeFilterSet(NetBoxModelFilterSet):
    server_id = _server_id_filter()
    server = _server_name_filter()

    class Meta:
        model = Superscope
        fields = ("id", "name", "description")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value))


@register_filterset
class ScopeFilterSet(NetBoxModelFilterSet):
    server_id = _server_id_filter()
    server = _server_name_filter()
    prefix_id = _model_filter("prefix", Prefix.objects.all(), _("Prefix (ID)"))
    ip_range_id = _model_filter("ip_range", IPRange.objects.all(), _("IP range (ID)"))
    superscope_id = _model_filter("superscope", Superscope.objects.all(), _("Superscope (ID)"))
    family = _family_filter("prefix__prefix")
    state = django_filters.MultipleChoiceFilter(choices=ScopeStateChoices)
    scope_type = django_filters.MultipleChoiceFilter(choices=ScopeTypeChoices)

    class Meta:
        model = Scope
        fields = (
            "id",
            "name",
            "lease_duration",
            "delay_ms",
            "max_bootp_clients",
            "activate_policies",
            "preference",
            "preferred_lifetime",
            "valid_lifetime",
            "renew_timer",
            "rebind_timer",
        )

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) | Q(description__icontains=value) | _network_q("prefix__prefix", value)
        )


@register_filterset
class ReservationFilterSet(NetBoxModelFilterSet):
    server_id = _server_id_filter("scope__server")
    server = _server_name_filter("scope__server")
    scope_id = _scope_filter()
    ip_address_id = _model_filter("ip_address", IPAddress.objects.all(), _("IP address (ID)"))
    mac_address_id = _model_filter("mac_address", MACAddress.objects.all(), _("MAC address (ID)"))
    family = _family_filter("scope__prefix__prefix")
    reservation_type = django_filters.MultipleChoiceFilter(choices=ReservationTypeChoices)

    class Meta:
        model = Reservation
        fields = ("id", "name", "client_id", "duid", "iaid")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        q = (
            Q(name__icontains=value)
            | Q(description__icontains=value)
            | Q(client_id__icontains=value)
            | Q(duid__icontains=value)
        )
        with contextlib.suppress(ValueError):
            q |= Q(ip_address__address__net_host=str(ipaddress.ip_address(value.strip())))
        return queryset.filter(q)


@register_filterset
class ExclusionRangeFilterSet(NetBoxModelFilterSet):
    server_id = _server_id_filter("scope__server")
    server = _server_name_filter("scope__server")
    scope_id = _scope_filter()

    class Meta:
        model = ExclusionRange
        fields = ("id", "start_address", "end_address", "description")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(start_address__icontains=value) | Q(end_address__icontains=value) | Q(description__icontains=value)
        )


@register_filterset
class WindowsClassFilterSet(PrimaryModelFilterSet):
    server_id = _server_id_filter()
    server = _server_name_filter()
    class_type = django_filters.MultipleChoiceFilter(choices=ClassTypeChoices)
    family = django_filters.MultipleChoiceFilter(choices=FamilyChoices)

    class Meta:
        model = WindowsClass
        fields = ("id", "name", "data", "description")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(data__icontains=value) | Q(description__icontains=value))


@register_filterset
class OptionDefinitionFilterSet(NetBoxModelFilterSet):
    server_id = _server_id_filter()
    server = _server_name_filter()
    family = django_filters.MultipleChoiceFilter(choices=FamilyChoices)
    vendor_class_id = _model_filter(
        "vendor_class", WindowsClass.objects.filter(class_type=ClassTypeChoices.VENDOR), _("Vendor class (ID)")
    )
    # Definitions every client can get, as opposed to those of a vendor class.
    has_vendor_class = django_filters.BooleanFilter(
        field_name="vendor_class", lookup_expr="isnull", exclude=True, label=_("Has a vendor class")
    )

    class Meta:
        model = OptionDefinition
        fields = ("id", "code", "name", "windows_type", "multi_valued", "description")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        q = Q(name__icontains=value) | Q(description__icontains=value) | Q(vendor_class__name__icontains=value)
        if value.strip().isdigit():
            q |= Q(code=int(value.strip()))
        return queryset.filter(q)


@register_filterset
class WindowsPolicyFilterSet(PrimaryModelFilterSet):
    server_id = _server_id_filter()
    server = _server_name_filter()
    scope_id = _scope_filter()
    # Server-level policies have no scope.
    has_scope = django_filters.BooleanFilter(
        field_name="scope", lookup_expr="isnull", exclude=True, label=_("Scope-level")
    )

    class Meta:
        model = WindowsPolicy
        fields = ("id", "name", "processing_order", "enabled", "condition", "lease_duration", "description")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value))


@register_filterset
class OptionValueFilterSet(NetBoxModelFilterSet):
    server_id = _server_id_filter()
    server = _server_name_filter()
    scope_id = _scope_filter()
    reservation_id = _model_filter("reservation", Reservation.objects.all(), _("Reservation (ID)"))
    level = django_filters.MultipleChoiceFilter(choices=OPTION_LEVELS, method="filter_level", label=_("Level"))
    family = django_filters.MultipleChoiceFilter(choices=FamilyChoices)
    vendor_class_id = _model_filter(
        "vendor_class", WindowsClass.objects.filter(class_type=ClassTypeChoices.VENDOR), _("Vendor class (ID)")
    )
    user_class_id = _model_filter(
        "user_class", WindowsClass.objects.filter(class_type=ClassTypeChoices.USER), _("User class (ID)")
    )
    policy_id = _model_filter("policy", WindowsPolicy.objects.all(), _("Policy (ID)"))

    class Meta:
        model = OptionValue
        fields = ("id", "code")

    def filter_level(self, queryset, name, value):
        if not value:
            return queryset
        levels = {
            "server": Q(scope__isnull=True, reservation__isnull=True),
            "scope": Q(scope__isnull=False, reservation__isnull=True),
            "reservation": Q(reservation__isnull=False),
        }
        q = Q()
        for level in value:
            q |= levels[level]
        return queryset.filter(q)

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        q = (
            Q(value__icontains=value)
            | Q(vendor_class__name__icontains=value)
            | Q(user_class__name__icontains=value)
            | Q(policy__name__icontains=value)
        )
        if value.strip().isdigit():
            q |= Q(code=int(value.strip()))
        return queryset.filter(q)


@register_filterset
class FailoverRelationshipFilterSet(PrimaryModelFilterSet):
    primary_server_id = _server_id_filter("primary_server", _("Primary server (ID)"))
    partner_server_id = _server_id_filter("partner_server", _("Partner server (ID)"))
    # Either side of the relationship, for the server page.
    server_id = django_filters.ModelMultipleChoiceFilter(
        queryset=WindowsServer.objects.all(), method="filter_server", label=_("Windows server (ID), either side")
    )
    scope_id = _scope_filter("scopes")
    mode = django_filters.MultipleChoiceFilter(choices=FailoverModeChoices)

    class Meta:
        model = FailoverRelationship
        fields = ("id", "name", "max_client_lead_time", "server_role", "description")

    def filter_server(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(Q(primary_server__in=value) | Q(partner_server__in=value))

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value))


@register_filterset
class MacFilterFilterSet(PrimaryModelFilterSet):
    server_id = _server_id_filter()
    server = _server_name_filter()
    list = django_filters.MultipleChoiceFilter(choices=MacFilterListChoices)

    class Meta:
        model = MacFilter
        fields = ("id", "mac_address", "description")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(mac_address__icontains=value) | Q(description__icontains=value))


@register_filterset
class MacFilterListsFilterSet(NetBoxModelFilterSet):
    server_id = _server_id_filter()
    server = _server_name_filter()

    class Meta:
        model = MacFilterLists
        fields = ("id", "allow_list_enabled", "deny_list_enabled")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(server__name__icontains=value)


@register_filterset
class SyncRunFilterSet(NetBoxModelFilterSet):
    windows_server_id = _model_filter("windows_server", WindowsServer.objects.all(), _("Windows server (ID)"))
    kind = django_filters.MultipleChoiceFilter(choices=SyncKindChoices)
    status = django_filters.MultipleChoiceFilter(choices=SyncStatusChoices)
    destination = django_filters.MultipleChoiceFilter(choices=ImportDestinationChoices)

    class Meta:
        model = SyncRun
        fields = ("id", "branch_name")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(branch_name__icontains=value) | Q(error__icontains=value))


@register_filterset
class ImportProposalFilterSet(NetBoxModelFilterSet):
    windows_server_id = _model_filter("windows_server", WindowsServer.objects.all(), _("Windows server (ID)"))
    # The sync run page lists its proposals through this filter; without it the filter was ignored and the page
    # listed every proposal.
    sync_run_id = _model_filter("sync_run", SyncRun.objects.all(), _("Sync run (ID)"))
    status = django_filters.MultipleChoiceFilter(choices=ProposalStatusChoices)

    class Meta:
        model = ImportProposal
        fields = ("id",)

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(windows_server__name__icontains=value)


@register_filterset
class ImportProposalItemFilterSet(NetBoxModelFilterSet):
    proposal_id = _model_filter("proposal", ImportProposal.objects.all(), _("Proposal (ID)"))
    action = django_filters.MultipleChoiceFilter(choices=ProposalItemActionChoices)
    status = django_filters.MultipleChoiceFilter(choices=ProposalItemStatusChoices)

    class Meta:
        model = ImportProposalItem
        fields = ("id", "kind", "key")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(kind__icontains=value) | Q(key__icontains=value))
