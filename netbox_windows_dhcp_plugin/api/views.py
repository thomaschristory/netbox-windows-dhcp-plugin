from core.api.serializers import JobSerializer
from django.db.models import Count
from netbox.api.authentication import TokenWritePermission
from netbox.api.viewsets import NetBoxModelViewSet, NetBoxReadOnlyModelViewSet
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from .. import filtersets, models, review
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
        if self.action in ("test_connection", "run_import"):
            return [TokenWritePermission()]
        return super().get_permissions()

    def _changeable_server(self, request):
        # A server the user may not change is reported as not found, like any restricted object.
        self.queryset = models.WindowsServer.objects.restrict(request.user, "change").restrict(request.user, "view")
        return self.get_object()

    @action(detail=True, methods=["post"], url_path="test-connection")
    def test_connection(self, request, pk):
        """Queue a connection test and return the job."""
        from ..jobs import TestConnectionJob

        job = TestConnectionJob.enqueue(instance=self._changeable_server(request), user=request.user)
        return Response(JobSerializer(job, context={"request": request}).data, status=202)

    @action(detail=True, methods=["post"], url_path="import")
    def run_import(self, request, pk):
        """Queue an import from the server and return the job (409 if one is already queued or running)."""
        from ..integrations import branching
        from ..jobs import enqueue_import

        server = self._changeable_server(request)
        missing = branching.missing_import_permissions(request.user)
        if missing:
            raise PermissionDenied(f"importing needs the permission: {', '.join(missing)}")
        job = enqueue_import(server, request.user)
        if job is None:
            return Response({"detail": "an import from this server is already queued or running"}, status=409)
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


class _ReviewActionsMixin:
    """Apply or dismiss proposal items as the requesting user (see review.py for the permission rules)."""

    def get_permissions(self):
        # POST would map to "add"; these actions need "change", enforced through the restricted querysets below.
        if self.action in ("apply", "dismiss"):
            return [TokenWritePermission()]
        return super().get_permissions()

    def _review(self, request, items, dismiss):
        items = review.open_items(items)
        if dismiss:
            return Response({"dismissed": review.dismiss(request.user, items)})
        applied, failed, denied = review.apply(request.user, items)
        return Response({"applied": applied, "failed": failed, "missing_permissions": denied})


# The item count is an aggregate, and Django drops Meta.ordering on aggregate queries, so order explicitly
# (the API paginator rejects an unordered queryset).
class ImportProposalViewSet(
    _ReviewActionsMixin,
    _viewset(
        models.ImportProposal,
        models.ImportProposal.objects.annotate(item_count=Count("items")).order_by("-created"),
        base=NetBoxReadOnlyModelViewSet,
    ),
):
    def _proposal(self, request):
        # NetBox has already restricted the queryset to "add" (what POST maps to); start again from "change".
        self.queryset = models.ImportProposal.objects.restrict(request.user, "change").restrict(request.user, "view")
        return self.get_object()

    @action(detail=True, methods=["post"])
    def apply(self, request, pk):
        """Apply every pending item of the proposal."""
        items = self._proposal(request).items.restrict(request.user, "change")
        return self._review(request, items, dismiss=False)

    @action(detail=True, methods=["post"])
    def dismiss(self, request, pk):
        """Dismiss every pending item of the proposal."""
        items = self._proposal(request).items.restrict(request.user, "change")
        return self._review(request, items, dismiss=True)


class ImportProposalItemViewSet(
    _ReviewActionsMixin,
    _viewset(models.ImportProposalItem, models.ImportProposalItem.objects.all(), base=NetBoxReadOnlyModelViewSet),
):
    def _selected(self, request):
        ids = request.data.get("ids") if isinstance(request.data, dict) else None
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise ValidationError({"ids": "a list of item IDs is required"})
        return models.ImportProposalItem.objects.restrict(request.user, "change").filter(pk__in=ids)

    @action(detail=False, methods=["post"])
    def apply(self, request):
        """Apply the items listed in {"ids": [...]}."""
        return self._review(request, self._selected(request), dismiss=False)

    @action(detail=False, methods=["post"])
    def dismiss(self, request):
        """Dismiss the items listed in {"ids": [...]}."""
        return self._review(request, self._selected(request), dismiss=True)
