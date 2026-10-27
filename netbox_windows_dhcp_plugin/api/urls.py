from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_windows_dhcp_plugin"

router = NetBoxRouter()
router.register("windows-servers", views.WindowsServerViewSet)
router.register("superscopes", views.SuperscopeViewSet)
router.register("scopes", views.ScopeViewSet)
router.register("reservations", views.ReservationViewSet)
router.register("exclusion-ranges", views.ExclusionRangeViewSet)
router.register("windows-classes", views.WindowsClassViewSet)
router.register("option-definitions", views.OptionDefinitionViewSet)
router.register("windows-policies", views.WindowsPolicyViewSet)
router.register("option-values", views.OptionValueViewSet)
router.register("failover-relationships", views.FailoverRelationshipViewSet)
router.register("mac-filters", views.MacFilterViewSet)
router.register("mac-filter-lists", views.MacFilterListsViewSet)
router.register("sync-runs", views.SyncRunViewSet)
router.register("import-proposals", views.ImportProposalViewSet)
router.register("import-proposal-items", views.ImportProposalItemViewSet)

urlpatterns = router.urls
