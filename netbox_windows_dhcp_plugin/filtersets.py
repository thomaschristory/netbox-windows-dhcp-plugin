import django_filters
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from netbox.filtersets import NetBoxModelFilterSet, PrimaryModelFilterSet
from netbox_dhcp.models import DHCPServer, HostReservation, Option, OptionDefinition, Subnet
from utilities.filters import ContentTypeFilter
from utilities.filtersets import register_filterset

from .choices import (
    ClassTypeChoices,
    FailoverModeChoices,
    MacFilterListChoices,
    ProposalItemActionChoices,
    ProposalItemStatusChoices,
    ProposalStatusChoices,
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
    "ExclusionRangeFilterSet",
    "FailoverRelationshipFilterSet",
    "ImportProposalFilterSet",
    "ImportProposalItemFilterSet",
    "MacFilterFilterSet",
    "MacFilterListsFilterSet",
    "SyncRunFilterSet",
    "WindowsClassFilterSet",
    "WindowsOptionDefinitionFilterSet",
    "WindowsOptionQualifierFilterSet",
    "WindowsPolicyFilterSet",
    "WindowsReservationFilterSet",
    "WindowsScopeFilterSet",
    "WindowsServerFilterSet",
    "WindowsVendorOptionDefinitionFilterSet",
    "WindowsVendorOptionFilterSet",
)


def _model_filter(field_name, queryset, label):
    return django_filters.ModelMultipleChoiceFilter(field_name=field_name, queryset=queryset, label=label)


def _server_filter(field_name="dhcp_server"):
    return _model_filter(field_name, DHCPServer.objects.all(), _("DHCP server (ID)"))


def _subnet_filter(field_name="subnet"):
    return _model_filter(field_name, Subnet.objects.all(), _("Subnet (ID)"))


@register_filterset
class WindowsServerFilterSet(PrimaryModelFilterSet):
    dhcp_server_id = _server_filter()
    transport = django_filters.MultipleChoiceFilter(choices=TransportChoices)
    winrm_auth = django_filters.MultipleChoiceFilter(choices=WinRMAuthChoices)

    class Meta:
        model = WindowsServer
        fields = (
            "id",
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
            Q(host__icontains=value) | Q(dhcp_server__name__icontains=value) | Q(computer_name__icontains=value)
        )


@register_filterset
class WindowsScopeFilterSet(NetBoxModelFilterSet):
    subnet_id = _subnet_filter()
    state = django_filters.MultipleChoiceFilter(choices=ScopeStateChoices)
    scope_type = django_filters.MultipleChoiceFilter(choices=ScopeTypeChoices)

    class Meta:
        model = WindowsScope
        fields = ("id", "delay_ms", "max_bootp_clients", "activate_policies", "preference")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(subnet__name__icontains=value)


@register_filterset
class WindowsReservationFilterSet(NetBoxModelFilterSet):
    host_reservation_id = _model_filter("host_reservation", HostReservation.objects.all(), _("Host reservation (ID)"))
    reservation_type = django_filters.MultipleChoiceFilter(choices=ScopeTypeChoices)

    class Meta:
        model = WindowsReservation
        fields = ("id", "iaid")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(host_reservation__name__icontains=value)


@register_filterset
class ExclusionRangeFilterSet(PrimaryModelFilterSet):
    subnet_id = _subnet_filter()

    class Meta:
        model = ExclusionRange
        fields = ("id", "start_address", "end_address", "description")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(start_address__icontains=value) | Q(end_address__icontains=value) | Q(subnet__name__icontains=value)
        )


@register_filterset
class WindowsClassFilterSet(PrimaryModelFilterSet):
    dhcp_server_id = _server_filter()
    class_type = django_filters.MultipleChoiceFilter(choices=ClassTypeChoices)

    class Meta:
        model = WindowsClass
        fields = ("id", "name", "data", "family", "description")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(data__icontains=value))


@register_filterset
class WindowsOptionDefinitionFilterSet(NetBoxModelFilterSet):
    option_definition_id = _model_filter(
        "option_definition", OptionDefinition.objects.all(), _("Option definition (ID)")
    )

    class Meta:
        model = WindowsOptionDefinition
        fields = ("id",)

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(option_definition__name__icontains=value)


@register_filterset
class WindowsPolicyFilterSet(PrimaryModelFilterSet):
    dhcp_server_id = _server_filter()
    subnet_id = _subnet_filter()

    class Meta:
        model = WindowsPolicy
        fields = ("id", "name", "processing_order", "enabled", "condition", "lease_duration", "description")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(name__icontains=value)


@register_filterset
class WindowsOptionQualifierFilterSet(NetBoxModelFilterSet):
    option_id = _model_filter("option", Option.objects.all(), _("Option (ID)"))
    user_class_id = _model_filter("user_class", WindowsClass.objects.all(), _("User class (ID)"))
    policy_id = _model_filter("policy", WindowsPolicy.objects.all(), _("Policy (ID)"))

    class Meta:
        model = WindowsOptionQualifier
        fields = ("id",)

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(user_class__name__icontains=value) | Q(policy__name__icontains=value))


@register_filterset
class FailoverRelationshipFilterSet(PrimaryModelFilterSet):
    primary_server_id = _server_filter("primary_server")
    partner_server_id = _server_filter("partner_server")
    subnet_id = _subnet_filter("subnets")
    mode = django_filters.MultipleChoiceFilter(choices=FailoverModeChoices)

    class Meta:
        model = FailoverRelationship
        fields = ("id", "name", "max_client_lead_time", "server_role", "description")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(name__icontains=value)


@register_filterset
class MacFilterFilterSet(PrimaryModelFilterSet):
    dhcp_server_id = _server_filter()
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
    dhcp_server_id = _server_filter()

    class Meta:
        model = MacFilterLists
        fields = ("id", "allow_list_enabled", "deny_list_enabled")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(dhcp_server__name__icontains=value)


@register_filterset
class WindowsVendorOptionDefinitionFilterSet(PrimaryModelFilterSet):
    dhcp_server_id = _server_filter("vendor_class__dhcp_server")
    vendor_class_id = _model_filter("vendor_class", WindowsClass.objects.all(), _("Vendor class (ID)"))

    class Meta:
        model = WindowsVendorOptionDefinition
        fields = ("id", "code", "name", "windows_type", "multi_valued", "description")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) | Q(vendor_class__name__icontains=value) | Q(description__icontains=value)
        )


@register_filterset
class WindowsVendorOptionFilterSet(NetBoxModelFilterSet):
    dhcp_server_id = _server_filter("definition__vendor_class__dhcp_server")
    vendor_class_id = _model_filter("definition__vendor_class", WindowsClass.objects.all(), _("Vendor class (ID)"))
    definition_id = _model_filter(
        "definition", WindowsVendorOptionDefinition.objects.all(), _("Vendor option definition (ID)")
    )
    code = django_filters.NumberFilter(field_name="definition__code", label=_("Code"))
    assigned_object_type = ContentTypeFilter()
    user_class_id = _model_filter("user_class", WindowsClass.objects.all(), _("User class (ID)"))
    policy_id = _model_filter("policy", WindowsPolicy.objects.all(), _("Policy (ID)"))

    class Meta:
        model = WindowsVendorOption
        fields = ("id", "assigned_object_id")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(definition__name__icontains=value) | Q(definition__vendor_class__name__icontains=value)
        )


@register_filterset
class SyncRunFilterSet(NetBoxModelFilterSet):
    windows_server_id = _model_filter("windows_server", WindowsServer.objects.all(), _("Windows server (ID)"))
    kind = django_filters.MultipleChoiceFilter(choices=SyncKindChoices)
    status = django_filters.MultipleChoiceFilter(choices=SyncStatusChoices)

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
    status = django_filters.MultipleChoiceFilter(choices=ProposalStatusChoices)

    class Meta:
        model = ImportProposal
        fields = ("id",)

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(windows_server__dhcp_server__name__icontains=value)


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
