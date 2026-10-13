from core.api.serializers import JobSerializer
from django.db.models import Count
from netbox.api.authentication import TokenWritePermission
from netbox.api.viewsets import NetBoxModelViewSet, NetBoxReadOnlyModelViewSet
from rest_framework.decorators import action
from rest_framework.response import Response

from .. import filtersets, models
from . import serializers


def _viewset(model, queryset, base=NetBoxModelViewSet):
    name = model.__name__
    return type(
        f"{name}ViewSet",
        (base,),
        {
            "queryset": queryset,
            "serializer_class": getattr(serializers, f"{name}Serializer"),
            "filterset_class": getattr(filtersets, f"{name}FilterSet"),
        },
    )


class WindowsServerViewSet(_viewset(models.WindowsServer, models.WindowsServer.objects.select_related("dhcp_server"))):
    def get_permissions(self):
        # NetBox maps POST to the "add" permission. A connection test needs "change" on the server instead,
        # enforced below; here only check that the token may write (as NetBox does for render-config).
        if self.action == "test_connection":
            return [TokenWritePermission()]
        return super().get_permissions()

    @action(detail=True, methods=["post"], url_path="test-connection")
    def test_connection(self, request, pk):
        """Queue a connection test and return the job."""
        from ..jobs import TestConnectionJob

        # A server the user may not change is reported as not found, like any restricted object.
        self.queryset = models.WindowsServer.objects.restrict(request.user, "change").restrict(request.user, "view")
        server = self.get_object()
        job = TestConnectionJob.enqueue(instance=server, user=request.user)
        return Response(JobSerializer(job, context={"request": request}).data, status=202)


WindowsScopeViewSet = _viewset(models.WindowsScope, models.WindowsScope.objects.select_related("subnet"))
WindowsReservationViewSet = _viewset(
    models.WindowsReservation, models.WindowsReservation.objects.select_related("host_reservation")
)
ExclusionRangeViewSet = _viewset(models.ExclusionRange, models.ExclusionRange.objects.select_related("subnet"))
WindowsClassViewSet = _viewset(models.WindowsClass, models.WindowsClass.objects.select_related("dhcp_server"))
WindowsOptionDefinitionViewSet = _viewset(models.WindowsOptionDefinition, models.WindowsOptionDefinition.objects.all())
WindowsPolicyViewSet = _viewset(models.WindowsPolicy, models.WindowsPolicy.objects.select_related("dhcp_server"))
WindowsOptionQualifierViewSet = _viewset(models.WindowsOptionQualifier, models.WindowsOptionQualifier.objects.all())
FailoverRelationshipViewSet = _viewset(
    models.FailoverRelationship, models.FailoverRelationship.objects.prefetch_related("subnets")
)
MacFilterViewSet = _viewset(models.MacFilter, models.MacFilter.objects.select_related("dhcp_server"))
SyncRunViewSet = _viewset(models.SyncRun, models.SyncRun.objects.all(), base=NetBoxReadOnlyModelViewSet)
# The item count is an aggregate, and Django drops Meta.ordering on aggregate queries, so order explicitly
# (the API paginator rejects an unordered queryset).
ImportProposalViewSet = _viewset(
    models.ImportProposal,
    models.ImportProposal.objects.annotate(item_count=Count("items")).order_by("-created"),
    base=NetBoxReadOnlyModelViewSet,
)
ImportProposalItemViewSet = _viewset(
    models.ImportProposalItem, models.ImportProposalItem.objects.all(), base=NetBoxReadOnlyModelViewSet
)
