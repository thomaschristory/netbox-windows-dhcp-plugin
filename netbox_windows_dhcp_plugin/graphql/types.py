from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.types import NetBoxObjectType, PrimaryObjectType

from .. import models
from . import filters

if TYPE_CHECKING:
    from dcim.graphql.types import DeviceType, MACAddressType
    from ipam.graphql.types import IPAddressType, IPRangeType, PrefixType, VRFType
    from virtualization.graphql.types import VirtualMachineType


TYPES = "netbox_windows_dhcp_plugin.graphql.types"

ServerRef = Annotated["WindowsServerType", strawberry.lazy(TYPES)]
ScopeRef = Annotated["ScopeType", strawberry.lazy(TYPES)]
WindowsClassRef = Annotated["WindowsClassType", strawberry.lazy(TYPES)]
PolicyRef = Annotated["WindowsPolicyType", strawberry.lazy(TYPES)]
OptionValueRef = Annotated["OptionValueType", strawberry.lazy(TYPES)]

__all__ = (
    "ExclusionRangeType",
    "FailoverRelationshipType",
    "ImportProposalItemType",
    "ImportProposalType",
    "MacFilterListsType",
    "MacFilterType",
    "OptionDefinitionType",
    "OptionValueType",
    "ReservationType",
    "ScopeType",
    "SuperscopeType",
    "SyncRunType",
    "WindowsClassType",
    "WindowsPolicyType",
    "WindowsServerType",
)


@strawberry_django.type(models.WindowsServer, fields="__all__", filters=filters.WindowsServerFilter, pagination=True)
class WindowsServerType(PrimaryObjectType):
    device: Annotated["DeviceType", strawberry.lazy("dcim.graphql.types")] | None
    virtual_machine: Annotated["VirtualMachineType", strawberry.lazy("virtualization.graphql.types")] | None
    import_vrf: Annotated["VRFType", strawberry.lazy("ipam.graphql.types")] | None
    scopes: list[ScopeRef]
    superscopes: list[Annotated["SuperscopeType", strawberry.lazy(TYPES)]]
    sync_runs: list[Annotated["SyncRunType", strawberry.lazy(TYPES)]]


@strawberry_django.type(
    models.Superscope,
    name="WindowsDHCPSuperscopeType",
    fields="__all__",
    filters=filters.SuperscopeFilter,
    pagination=True,
)
class SuperscopeType(NetBoxObjectType):
    server: ServerRef
    scopes: list[ScopeRef]


@strawberry_django.type(
    models.Scope,
    name="WindowsDHCPScopeType",
    fields="__all__",
    filters=filters.ScopeFilter,
    pagination=True,
)
class ScopeType(NetBoxObjectType):
    server: ServerRef
    prefix: Annotated["PrefixType", strawberry.lazy("ipam.graphql.types")]
    ip_range: Annotated["IPRangeType", strawberry.lazy("ipam.graphql.types")] | None
    superscope: Annotated["SuperscopeType", strawberry.lazy(TYPES)] | None
    reservations: list[Annotated["ReservationType", strawberry.lazy(TYPES)]]
    exclusion_ranges: list[Annotated["ExclusionRangeType", strawberry.lazy(TYPES)]]
    option_values: list[OptionValueRef]
    policies: list[PolicyRef]


@strawberry_django.type(
    models.Reservation,
    name="WindowsDHCPReservationType",
    fields="__all__",
    filters=filters.ReservationFilter,
    pagination=True,
)
class ReservationType(NetBoxObjectType):
    scope: ScopeRef
    ip_address: Annotated["IPAddressType", strawberry.lazy("ipam.graphql.types")]
    mac_address: Annotated["MACAddressType", strawberry.lazy("dcim.graphql.types")] | None
    option_values: list[OptionValueRef]


@strawberry_django.type(models.ExclusionRange, fields="__all__", filters=filters.ExclusionRangeFilter, pagination=True)
class ExclusionRangeType(NetBoxObjectType):
    scope: ScopeRef


@strawberry_django.type(models.WindowsClass, fields="__all__", filters=filters.WindowsClassFilter, pagination=True)
class WindowsClassType(PrimaryObjectType):
    server: ServerRef


@strawberry_django.type(
    models.OptionDefinition,
    name="WindowsDHCPOptionDefinitionType",
    fields="__all__",
    filters=filters.OptionDefinitionFilter,
    pagination=True,
)
class OptionDefinitionType(NetBoxObjectType):
    server: ServerRef
    vendor_class: WindowsClassRef | None
    default_value: strawberry.scalars.JSON


@strawberry_django.type(models.WindowsPolicy, fields="__all__", filters=filters.WindowsPolicyFilter, pagination=True)
class WindowsPolicyType(PrimaryObjectType):
    server: ServerRef
    scope: ScopeRef | None
    criteria: strawberry.scalars.JSON
    ip_ranges: strawberry.scalars.JSON
    option_values: list[OptionValueRef]


@strawberry_django.type(
    models.OptionValue,
    name="WindowsDHCPOptionValueType",
    fields="__all__",
    filters=filters.OptionValueFilter,
    pagination=True,
)
class OptionValueType(NetBoxObjectType):
    server: ServerRef
    scope: ScopeRef | None
    reservation: Annotated["ReservationType", strawberry.lazy(TYPES)] | None
    vendor_class: WindowsClassRef | None
    user_class: WindowsClassRef | None
    policy: PolicyRef | None
    value: strawberry.scalars.JSON


@strawberry_django.type(
    models.FailoverRelationship, fields="__all__", filters=filters.FailoverRelationshipFilter, pagination=True
)
class FailoverRelationshipType(PrimaryObjectType):
    primary_server: ServerRef
    partner_server: ServerRef
    scopes: list[ScopeRef]


@strawberry_django.type(models.MacFilter, fields="__all__", filters=filters.MacFilterFilter, pagination=True)
class MacFilterType(PrimaryObjectType):
    server: ServerRef


@strawberry_django.type(models.MacFilterLists, fields="__all__", filters=filters.MacFilterListsFilter, pagination=True)
class MacFilterListsType(NetBoxObjectType):
    server: ServerRef


# NetBox has no GraphQL type for core jobs, so the job link is REST-only.
@strawberry_django.type(models.SyncRun, exclude=("job",), filters=filters.SyncRunFilter, pagination=True)
class SyncRunType(NetBoxObjectType):
    windows_server: Annotated["WindowsServerType", strawberry.lazy(TYPES)]
    summary: strawberry.scalars.JSON


@strawberry_django.type(models.ImportProposal, fields="__all__", filters=filters.ImportProposalFilter, pagination=True)
class ImportProposalType(NetBoxObjectType):
    windows_server: Annotated["WindowsServerType", strawberry.lazy(TYPES)]
    sync_run: Annotated["SyncRunType", strawberry.lazy(TYPES)] | None
    items: list[Annotated["ImportProposalItemType", strawberry.lazy(TYPES)]]


@strawberry_django.type(
    models.ImportProposalItem,
    exclude=("object_type", "object_id"),
    filters=filters.ImportProposalItemFilter,
    pagination=True,
)
class ImportProposalItemType(NetBoxObjectType):
    proposal: Annotated["ImportProposalType", strawberry.lazy(TYPES)]
    before: strawberry.scalars.JSON | None
    after: strawberry.scalars.JSON | None
