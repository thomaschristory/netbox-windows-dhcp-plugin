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
    "MacFilterListsFilter",
    "OptionDefinitionFilter",
    "OptionValueFilter",
    "ReservationFilter",
    "ScopeFilter",
    "SuperscopeFilter",
    "SyncRunFilter",
    "WindowsClassFilter",
    "WindowsPolicyFilter",
    "WindowsServerFilter",
)


@strawberry_django.filter_type(models.WindowsServer, lookups=True)
class WindowsServerFilter(PrimaryModelFilter):
    name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    host: StrFilterLookup[str] | None = strawberry_django.filter_field()
    transport: StrFilterLookup[str] | None = strawberry_django.filter_field()
    credential_profile: StrFilterLookup[str] | None = strawberry_django.filter_field()
    device_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    virtual_machine_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.Superscope, name="WindowsDHCPSuperscopeFilter", lookups=True)
class SuperscopeFilter(NetBoxModelFilter):
    name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.Scope, name="WindowsDHCPScopeFilter", lookups=True)
class ScopeFilter(NetBoxModelFilter):
    name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    state: StrFilterLookup[str] | None = strawberry_django.filter_field()
    server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    prefix_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    ip_range_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    superscope_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.Reservation, name="WindowsDHCPReservationFilter", lookups=True)
class ReservationFilter(NetBoxModelFilter):
    name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    client_id: StrFilterLookup[str] | None = strawberry_django.filter_field()
    duid: StrFilterLookup[str] | None = strawberry_django.filter_field()
    reservation_type: StrFilterLookup[str] | None = strawberry_django.filter_field()
    iaid: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    scope_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    ip_address_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    mac_address_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.ExclusionRange, lookups=True)
class ExclusionRangeFilter(NetBoxModelFilter):
    scope_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.WindowsClass, lookups=True)
class WindowsClassFilter(PrimaryModelFilter):
    name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    class_type: StrFilterLookup[str] | None = strawberry_django.filter_field()
    family: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.OptionDefinition, name="WindowsDHCPOptionDefinitionFilter", lookups=True)
class OptionDefinitionFilter(NetBoxModelFilter):
    name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    code: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    family: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    windows_type: StrFilterLookup[str] | None = strawberry_django.filter_field()
    server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    vendor_class_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.WindowsPolicy, lookups=True)
class WindowsPolicyFilter(PrimaryModelFilter):
    name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    scope_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.OptionValue, name="WindowsDHCPOptionValueFilter", lookups=True)
class OptionValueFilter(NetBoxModelFilter):
    code: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    family: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    scope_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    reservation_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    vendor_class_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    user_class_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    policy_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.FailoverRelationship, lookups=True)
class FailoverRelationshipFilter(PrimaryModelFilter):
    name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    mode: StrFilterLookup[str] | None = strawberry_django.filter_field()
    primary_server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()
    partner_server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.MacFilter, lookups=True)
class MacFilterFilter(PrimaryModelFilter):
    mac_address: StrFilterLookup[str] | None = strawberry_django.filter_field()
    list: StrFilterLookup[str] | None = strawberry_django.filter_field()
    server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.MacFilterLists, lookups=True)
class MacFilterListsFilter(NetBoxModelFilter):
    server_id: strawberry_django.ComparisonFilterLookup[int] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.SyncRun, lookups=True)
class SyncRunFilter(NetBoxModelFilter):
    kind: StrFilterLookup[str] | None = strawberry_django.filter_field()
    status: StrFilterLookup[str] | None = strawberry_django.filter_field()
    destination: StrFilterLookup[str] | None = strawberry_django.filter_field()
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
