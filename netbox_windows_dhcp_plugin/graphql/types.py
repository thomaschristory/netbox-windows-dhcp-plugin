from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.types import NetBoxObjectType, PrimaryObjectType

from .. import models
from . import filters

if TYPE_CHECKING:
    from ipam.graphql.types import VRFType
    from netbox_dhcp.graphql.types import (
        NetBoxDHCPDHCPServerType,
        NetBoxDHCPHostReservationType,
        NetBoxDHCPOptionDefinitionType,
        NetBoxDHCPOptionType,
        NetBoxDHCPSubnetType,
    )


DHCP_TYPES = "netbox_dhcp.graphql.types"
TYPES = "netbox_windows_dhcp_plugin.graphql.types"

DHCPServerType = Annotated["NetBoxDHCPDHCPServerType", strawberry.lazy(DHCP_TYPES)]
SubnetType = Annotated["NetBoxDHCPSubnetType", strawberry.lazy(DHCP_TYPES)]
WindowsClassRef = Annotated["WindowsClassType", strawberry.lazy(TYPES)]

__all__ = (
    "ExclusionRangeType",
    "FailoverRelationshipType",
    "ImportProposalItemType",
    "ImportProposalType",
    "MacFilterListsType",
    "MacFilterType",
    "SyncRunType",
    "WindowsClassType",
    "WindowsOptionDefinitionType",
    "WindowsOptionQualifierType",
    "WindowsPolicyType",
    "WindowsReservationType",
    "WindowsScopeType",
    "WindowsServerType",
    "WindowsVendorOptionDefinitionType",
    "WindowsVendorOptionType",
)


@strawberry_django.type(models.WindowsServer, fields="__all__", filters=filters.WindowsServerFilter, pagination=True)
class WindowsServerType(PrimaryObjectType):
    dhcp_server: DHCPServerType
    import_vrf: Annotated["VRFType", strawberry.lazy("ipam.graphql.types")] | None
    sync_runs: list[Annotated["SyncRunType", strawberry.lazy(TYPES)]]


@strawberry_django.type(models.WindowsScope, fields="__all__", filters=filters.WindowsScopeFilter, pagination=True)
class WindowsScopeType(NetBoxObjectType):
    subnet: SubnetType


@strawberry_django.type(
    models.WindowsReservation, fields="__all__", filters=filters.WindowsReservationFilter, pagination=True
)
class WindowsReservationType(NetBoxObjectType):
    host_reservation: Annotated["NetBoxDHCPHostReservationType", strawberry.lazy(DHCP_TYPES)]


@strawberry_django.type(models.ExclusionRange, fields="__all__", filters=filters.ExclusionRangeFilter, pagination=True)
class ExclusionRangeType(PrimaryObjectType):
    subnet: SubnetType


@strawberry_django.type(models.WindowsClass, fields="__all__", filters=filters.WindowsClassFilter, pagination=True)
class WindowsClassType(PrimaryObjectType):
    dhcp_server: DHCPServerType


@strawberry_django.type(
    models.WindowsOptionDefinition, fields="__all__", filters=filters.WindowsOptionDefinitionFilter, pagination=True
)
class WindowsOptionDefinitionType(NetBoxObjectType):
    option_definition: Annotated["NetBoxDHCPOptionDefinitionType", strawberry.lazy(DHCP_TYPES)]
    default_value: strawberry.scalars.JSON


@strawberry_django.type(models.WindowsPolicy, fields="__all__", filters=filters.WindowsPolicyFilter, pagination=True)
class WindowsPolicyType(PrimaryObjectType):
    dhcp_server: DHCPServerType
    subnet: SubnetType | None
    criteria: strawberry.scalars.JSON
    ip_ranges: strawberry.scalars.JSON


@strawberry_django.type(
    models.WindowsOptionQualifier, fields="__all__", filters=filters.WindowsOptionQualifierFilter, pagination=True
)
class WindowsOptionQualifierType(NetBoxObjectType):
    option: Annotated["NetBoxDHCPOptionType", strawberry.lazy(DHCP_TYPES)]
    user_class: WindowsClassRef | None
    policy: Annotated["WindowsPolicyType", strawberry.lazy(TYPES)] | None


@strawberry_django.type(
    models.FailoverRelationship, fields="__all__", filters=filters.FailoverRelationshipFilter, pagination=True
)
class FailoverRelationshipType(PrimaryObjectType):
    primary_server: DHCPServerType
    partner_server: DHCPServerType
    subnets: list[SubnetType]


@strawberry_django.type(models.MacFilter, fields="__all__", filters=filters.MacFilterFilter, pagination=True)
class MacFilterType(PrimaryObjectType):
    dhcp_server: DHCPServerType


@strawberry_django.type(models.MacFilterLists, fields="__all__", filters=filters.MacFilterListsFilter, pagination=True)
class MacFilterListsType(NetBoxObjectType):
    dhcp_server: DHCPServerType


@strawberry_django.type(
    models.WindowsVendorOptionDefinition,
    fields="__all__",
    filters=filters.WindowsVendorOptionDefinitionFilter,
    pagination=True,
)
class WindowsVendorOptionDefinitionType(PrimaryObjectType):
    vendor_class: WindowsClassRef
    default_value: strawberry.scalars.JSON
    values: list[Annotated["WindowsVendorOptionType", strawberry.lazy(TYPES)]]


@strawberry_django.type(
    models.WindowsVendorOption,
    exclude=("assigned_object_type", "assigned_object_id"),
    filters=filters.WindowsVendorOptionFilter,
    pagination=True,
)
class WindowsVendorOptionType(NetBoxObjectType):
    definition: Annotated["WindowsVendorOptionDefinitionType", strawberry.lazy(TYPES)]
    user_class: WindowsClassRef | None
    policy: Annotated["WindowsPolicyType", strawberry.lazy(TYPES)] | None
    value: strawberry.scalars.JSON

    # The parent is one of three netbox-plugin-dhcp types, so GraphQL sees a union.
    @strawberry_django.field
    def assigned_object(
        self,
    ) -> Annotated[
        DHCPServerType | SubnetType | Annotated["NetBoxDHCPHostReservationType", strawberry.lazy(DHCP_TYPES)],
        strawberry.union("WindowsVendorOptionAssignmentType"),
    ]:
        return self.assigned_object


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
