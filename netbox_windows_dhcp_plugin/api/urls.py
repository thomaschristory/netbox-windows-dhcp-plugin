from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_windows_dhcp_plugin"

router = NetBoxRouter()
router.register("windows-servers", views.WindowsServerViewSet)
router.register("windows-scopes", views.WindowsScopeViewSet)
router.register("windows-reservations", views.WindowsReservationViewSet)
router.register("exclusion-ranges", views.ExclusionRangeViewSet)
router.register("windows-classes", views.WindowsClassViewSet)
router.register("windows-option-definitions", views.WindowsOptionDefinitionViewSet)
router.register("windows-policies", views.WindowsPolicyViewSet)
router.register("windows-option-qualifiers", views.WindowsOptionQualifierViewSet)
router.register("failover-relationships", views.FailoverRelationshipViewSet)
router.register("mac-filters", views.MacFilterViewSet)
router.register("mac-filter-lists", views.MacFilterListsViewSet)
router.register("vendor-option-definitions", views.WindowsVendorOptionDefinitionViewSet)
router.register("vendor-options", views.WindowsVendorOptionViewSet)
router.register("sync-runs", views.SyncRunViewSet)
router.register("import-proposals", views.ImportProposalViewSet)
router.register("import-proposal-items", views.ImportProposalItemViewSet)

urlpatterns = router.urls
