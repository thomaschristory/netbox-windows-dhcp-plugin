import strawberry_django

try:
    from strawberry_django import StrFilterLookup
except ImportError:
    from strawberry_django import FilterLookup as StrFilterLookup

from netbox.graphql.filters import NetBoxModelFilter, PrimaryModelFilter

from .. import models

__all__ = (
    "ExclusionRangeFilter",
    "FailoverRelationshipFilter",
    "ImportProposalFilter",
    "ImportProposalItemFilter",
    "MacFilterFilter",
    "SyncRunFilter",
    "WindowsClassFilter",
    "WindowsOptionDefinitionFilter",
    "WindowsOptionQualifierFilter",
    "WindowsPolicyFilter",
    "WindowsReservationFilter",
    "WindowsScopeFilter",
    "WindowsServerFilter",
)


@strawberry_django.filter_type(models.WindowsServer, lookups=True)
class WindowsServerFilter(PrimaryModelFilter):
    host: StrFilterLookup[str] | None = strawberry_django.filter_field()
    transport: StrFilterLookup[str] | None = strawberry_django.filter_field()
    credential_profile: StrFilterLookup[str] | None = strawberry_django.filter_field()
    dhcp_server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.WindowsScope, lookups=True)
class WindowsScopeFilter(NetBoxModelFilter):
    state: StrFilterLookup[str] | None = strawberry_django.filter_field()
    subnet_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.WindowsReservation, lookups=True)
class WindowsReservationFilter(NetBoxModelFilter):
    reservation_type: StrFilterLookup[str] | None = strawberry_django.filter_field()
    host_reservation_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.ExclusionRange, lookups=True)
class ExclusionRangeFilter(PrimaryModelFilter):
    subnet_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.WindowsClass, lookups=True)
class WindowsClassFilter(PrimaryModelFilter):
    name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    class_type: StrFilterLookup[str] | None = strawberry_django.filter_field()
    dhcp_server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.WindowsOptionDefinition, lookups=True)
class WindowsOptionDefinitionFilter(NetBoxModelFilter):
    option_definition_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.WindowsPolicy, lookups=True)
class WindowsPolicyFilter(PrimaryModelFilter):
    name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    dhcp_server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    subnet_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.WindowsOptionQualifier, lookups=True)
class WindowsOptionQualifierFilter(NetBoxModelFilter):
    option_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.FailoverRelationship, lookups=True)
class FailoverRelationshipFilter(PrimaryModelFilter):
    name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    mode: StrFilterLookup[str] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.MacFilter, lookups=True)
class MacFilterFilter(PrimaryModelFilter):
    mac_address: StrFilterLookup[str] | None = strawberry_django.filter_field()
    list: StrFilterLookup[str] | None = strawberry_django.filter_field()
    dhcp_server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.SyncRun, lookups=True)
class SyncRunFilter(NetBoxModelFilter):
    kind: StrFilterLookup[str] | None = strawberry_django.filter_field()
    status: StrFilterLookup[str] | None = strawberry_django.filter_field()
    windows_server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.ImportProposal, lookups=True)
class ImportProposalFilter(NetBoxModelFilter):
    status: StrFilterLookup[str] | None = strawberry_django.filter_field()
    windows_server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.ImportProposalItem, lookups=True)
class ImportProposalItemFilter(NetBoxModelFilter):
    action: StrFilterLookup[str] | None = strawberry_django.filter_field()
    status: StrFilterLookup[str] | None = strawberry_django.filter_field()
    proposal_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
