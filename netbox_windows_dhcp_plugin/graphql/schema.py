import strawberry
import strawberry_django

from .types import (
    ExclusionRangeType,
    FailoverRelationshipType,
    ImportProposalItemType,
    ImportProposalType,
    MacFilterListsType,
    MacFilterType,
    OptionDefinitionType,
    OptionValueType,
    ReservationType,
    ScopeType,
    SuperscopeType,
    SyncRunType,
    WindowsClassType,
    WindowsPolicyType,
    WindowsServerType,
)


@strawberry.type(name="Query")
class WindowsDHCPQuery:
    windows_dhcp_server: WindowsServerType = strawberry_django.field()
    windows_dhcp_server_list: list[WindowsServerType] = strawberry_django.field()
    windows_dhcp_superscope: SuperscopeType = strawberry_django.field()
    windows_dhcp_superscope_list: list[SuperscopeType] = strawberry_django.field()
    windows_dhcp_scope: ScopeType = strawberry_django.field()
    windows_dhcp_scope_list: list[ScopeType] = strawberry_django.field()
    windows_dhcp_reservation: ReservationType = strawberry_django.field()
    windows_dhcp_reservation_list: list[ReservationType] = strawberry_django.field()
    windows_dhcp_exclusion_range: ExclusionRangeType = strawberry_django.field()
    windows_dhcp_exclusion_range_list: list[ExclusionRangeType] = strawberry_django.field()
    windows_dhcp_class: WindowsClassType = strawberry_django.field()
    windows_dhcp_class_list: list[WindowsClassType] = strawberry_django.field()
    windows_dhcp_option_definition: OptionDefinitionType = strawberry_django.field()
    windows_dhcp_option_definition_list: list[OptionDefinitionType] = strawberry_django.field()
    windows_dhcp_policy: WindowsPolicyType = strawberry_django.field()
    windows_dhcp_policy_list: list[WindowsPolicyType] = strawberry_django.field()
    windows_dhcp_option_value: OptionValueType = strawberry_django.field()
    windows_dhcp_option_value_list: list[OptionValueType] = strawberry_django.field()
    windows_dhcp_failover_relationship: FailoverRelationshipType = strawberry_django.field()
    windows_dhcp_failover_relationship_list: list[FailoverRelationshipType] = strawberry_django.field()
    windows_dhcp_mac_filter: MacFilterType = strawberry_django.field()
    windows_dhcp_mac_filter_list: list[MacFilterType] = strawberry_django.field()
    windows_dhcp_mac_filter_lists: MacFilterListsType = strawberry_django.field()
    windows_dhcp_mac_filter_lists_list: list[MacFilterListsType] = strawberry_django.field()
    windows_dhcp_sync_run: SyncRunType = strawberry_django.field()
    windows_dhcp_sync_run_list: list[SyncRunType] = strawberry_django.field()
    windows_dhcp_import_proposal: ImportProposalType = strawberry_django.field()
    windows_dhcp_import_proposal_list: list[ImportProposalType] = strawberry_django.field()
    windows_dhcp_import_proposal_item: ImportProposalItemType = strawberry_django.field()
    windows_dhcp_import_proposal_item_list: list[ImportProposalItemType] = strawberry_django.field()


schema = [WindowsDHCPQuery]
