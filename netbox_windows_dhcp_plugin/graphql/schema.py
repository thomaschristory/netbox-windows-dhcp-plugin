import strawberry
import strawberry_django

from .types import (
    ExclusionRangeType,
    FailoverRelationshipType,
    ImportProposalItemType,
    ImportProposalType,
    MacFilterType,
    SyncRunType,
    WindowsClassType,
    WindowsOptionDefinitionType,
    WindowsOptionQualifierType,
    WindowsPolicyType,
    WindowsReservationType,
    WindowsScopeType,
    WindowsServerType,
)


@strawberry.type(name="Query")
class WindowsDHCPQuery:
    windows_dhcp_server: WindowsServerType = strawberry_django.field()
    windows_dhcp_server_list: list[WindowsServerType] = strawberry_django.field()
    windows_dhcp_scope: WindowsScopeType = strawberry_django.field()
    windows_dhcp_scope_list: list[WindowsScopeType] = strawberry_django.field()
    windows_dhcp_reservation: WindowsReservationType = strawberry_django.field()
    windows_dhcp_reservation_list: list[WindowsReservationType] = strawberry_django.field()
    windows_dhcp_exclusion_range: ExclusionRangeType = strawberry_django.field()
    windows_dhcp_exclusion_range_list: list[ExclusionRangeType] = strawberry_django.field()
    windows_dhcp_class: WindowsClassType = strawberry_django.field()
    windows_dhcp_class_list: list[WindowsClassType] = strawberry_django.field()
    windows_dhcp_option_definition: WindowsOptionDefinitionType = strawberry_django.field()
    windows_dhcp_option_definition_list: list[WindowsOptionDefinitionType] = strawberry_django.field()
    windows_dhcp_policy: WindowsPolicyType = strawberry_django.field()
    windows_dhcp_policy_list: list[WindowsPolicyType] = strawberry_django.field()
    windows_dhcp_option_qualifier: WindowsOptionQualifierType = strawberry_django.field()
    windows_dhcp_option_qualifier_list: list[WindowsOptionQualifierType] = strawberry_django.field()
    windows_dhcp_failover_relationship: FailoverRelationshipType = strawberry_django.field()
    windows_dhcp_failover_relationship_list: list[FailoverRelationshipType] = strawberry_django.field()
    windows_dhcp_mac_filter: MacFilterType = strawberry_django.field()
    windows_dhcp_mac_filter_list: list[MacFilterType] = strawberry_django.field()
    windows_dhcp_sync_run: SyncRunType = strawberry_django.field()
    windows_dhcp_sync_run_list: list[SyncRunType] = strawberry_django.field()
    windows_dhcp_import_proposal: ImportProposalType = strawberry_django.field()
    windows_dhcp_import_proposal_list: list[ImportProposalType] = strawberry_django.field()
    windows_dhcp_import_proposal_item: ImportProposalItemType = strawberry_django.field()
    windows_dhcp_import_proposal_item_list: list[ImportProposalItemType] = strawberry_django.field()


schema = [WindowsDHCPQuery]
