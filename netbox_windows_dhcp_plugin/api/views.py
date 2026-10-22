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
        if self.action in ("test_connection", "run_import", "push", "dry_run"):
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
        """
        Queue an import from the server and return the job (409 if one is already queued or running).

        ``destination`` chooses where it writes: "branch" (a new or open import branch, the default with
        netbox-branching), "current" (the branch named in ``branch``, or the request's active branch) or "main" with
        it, "proposal" without it. A destination that is unknown here is a 400; one the user may not use is a 403.
        """
        from ..integrations import branching
        from ..jobs import ImportRefused, enqueue_import

        server = self._changeable_server(request)
        data = request.data if hasattr(request.data, "get") else {}
        destination = data.get("destination") or branching.import_destinations()[0]
        if destination not in branching.import_destinations():
            raise ValidationError({"destination": f"choose one of: {', '.join(branching.import_destinations())}"})
        branch = ""
        if destination == "current":
            branch = data.get("branch") or branching.active_branch_name() or ""
            if not branch:
                raise ValidationError({"branch": "name the branch to import into, or send the request in a branch"})
        missing = branching.missing_import_permissions(request.user, destination)
        if missing:
            raise PermissionDenied(f"importing needs the permission: {', '.join(missing)}")
        try:
            job = enqueue_import(server, request.user, destination, branch)
        except ImportRefused as exc:
            raise PermissionDenied(str(exc)) from None
        if job is None:
            return Response({"detail": "an import from this server is already queued or running"}, status=409)
        return Response(JobSerializer(job, context={"request": request}).data, status=202)

    def _push(self, request, dry_run):
        from ..jobs import enqueue_push

        server = self._changeable_server(request)
        if not dry_run and not server.sync_enabled:
            return Response({"detail": "sync is disabled for this server"}, status=409)
        job = enqueue_push(server, request.user, dry_run=dry_run)
        if job is None:
            what = "a dry run" if dry_run else "a push"
            return Response({"detail": f"{what} for this server is already queued"}, status=409)
        return Response(JobSerializer(job, context={"request": request}).data, status=202)

    @action(detail=True, methods=["post"])
    def push(self, request, pk):
        """Queue a push to the server and return the job (409 if one is already waiting to run)."""
        return self._push(request, dry_run=False)

    @action(detail=True, methods=["post"], url_path="dry-run")
    def dry_run(self, request, pk):
        """Queue a dry run (plan a push without writing) and return the job."""
        return self._push(request, dry_run=True)


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
MacFilterListsViewSet = _viewset(models.MacFilterLists, models.MacFilterLists.objects.select_related("dhcp_server"))
WindowsVendorOptionDefinitionViewSet = _viewset(
    models.WindowsVendorOptionDefinition, models.WindowsVendorOptionDefinition.objects.select_related("vendor_class")
)
WindowsVendorOptionViewSet = _viewset(
    models.WindowsVendorOption,
    models.WindowsVendorOption.objects.select_related(
        "definition__vendor_class", "assigned_object_type", "user_class", "policy"
    ).prefetch_related("assigned_object"),
)
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
