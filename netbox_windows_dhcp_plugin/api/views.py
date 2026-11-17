from core.api.serializers import JobSerializer
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


class WindowsServerViewSet(
    _viewset(
        models.WindowsServer, models.WindowsServer.objects.select_related("device", "virtual_machine", "import_vrf")
    )
):
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
        """Queue a connection test and return the job (409 naming the waiting job if one has yet to start)."""
        from ..jobs import enqueue_test, waiting_test_job

        server = self._changeable_server(request)
        job = enqueue_test(server, request.user)
        if job is None:
            # Name the waiting job, so a client can look at it (or delete it when no worker will ever run it). It
            # is null when that job started between the two lookups.
            waiting = waiting_test_job(server)
            return Response(
                {
                    "detail": "a connection test for this server is already queued",
                    "job": waiting.pk if waiting else None,
                },
                status=409,
            )
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
        from ..jobs import ImportRefused, enqueue_import, import_refusal

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
        refusal = import_refusal(destination, request.user, branch)
        if refusal:
            raise PermissionDenied(refusal)
        try:
            job = enqueue_import(server, request.user, destination, branch)
        except ImportRefused:
            # Allowed a moment ago (a setting or permission changed in between): the next try says why.
            raise PermissionDenied("the import was refused; try again") from None
        if job is None:
            return Response({"detail": "an import from this server is already queued or running"}, status=409)
        return Response(JobSerializer(job, context={"request": request}).data, status=202)

    def _push(self, request, dry_run):
        from ..jobs import enqueue_push

        server = self._changeable_server(request)
        if not dry_run and not server.sync_enabled:
            return Response({"detail": "sync is disabled for this server"}, status=409)
        triggers = [{"source": "sync_now", "user": request.user, "request_id": getattr(request, "id", None)}]
        job = enqueue_push(server, request.user, dry_run=dry_run, triggers=triggers)
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


SuperscopeViewSet = _viewset(models.Superscope, models.Superscope.objects.select_related("server"))
ScopeViewSet = _viewset(models.Scope, models.Scope.objects.select_related("server", "prefix", "ip_range", "superscope"))
# A nested scope shows its name ("<server>/<scope id>"), which reads the scope's server and prefix, and a nested
# reservation shows its IP address. Load them with the rows, or every row of a list costs extra queries.
ReservationViewSet = _viewset(
    models.Reservation,
    models.Reservation.objects.select_related("scope__server", "scope__prefix", "ip_address", "mac_address"),
)
ExclusionRangeViewSet = _viewset(
    models.ExclusionRange, models.ExclusionRange.objects.select_related("scope__server", "scope__prefix")
)
WindowsClassViewSet = _viewset(models.WindowsClass, models.WindowsClass.objects.select_related("server"))
OptionDefinitionViewSet = _viewset(
    models.OptionDefinition, models.OptionDefinition.objects.select_related("server", "vendor_class")
)
WindowsPolicyViewSet = _viewset(
    models.WindowsPolicy, models.WindowsPolicy.objects.select_related("server", "scope__server", "scope__prefix")
)
OptionValueViewSet = _viewset(
    models.OptionValue,
    models.OptionValue.objects.select_related(
        "server", "scope__server", "scope__prefix", "reservation__ip_address", "vendor_class", "user_class", "policy"
    ),
)
FailoverRelationshipViewSet = _viewset(
    models.FailoverRelationship,
    models.FailoverRelationship.objects.select_related("primary_server", "partner_server").prefetch_related(
        "scopes__server", "scopes__prefix"
    ),
)
MacFilterViewSet = _viewset(models.MacFilter, models.MacFilter.objects.select_related("server"))
MacFilterListsViewSet = _viewset(models.MacFilterLists, models.MacFilterLists.objects.select_related("server"))
SyncRunViewSet = _viewset(
    models.SyncRun,
    models.SyncRun.objects.prefetch_related("triggers__user"),
    base=NetBoxReadOnlyModelViewSet,
)


def _review_jobs():
    from core.models import Job
    from django.db.models import Prefetch

    from ..jobs import ReviewJob

    return Prefetch(
        "jobs", queryset=Job.objects.filter(name=ReviewJob.name).order_by("-created"), to_attr="review_jobs"
    )


class _ReviewActionsMixin:
    """Apply or dismiss proposal items as the requesting user (see review.py for the permission rules)."""

    def get_permissions(self):
        # POST would map to "add"; these actions need "change", enforced through the restricted querysets below.
        if self.action in ("apply", "dismiss"):
            return [TokenWritePermission()]
        return super().get_permissions()

    # Apply all / Dismiss all answer with the one job of the proposal; a list of items may span proposals.
    whole_proposal = False

    def _review(self, request, items, dismiss):
        items = review.open_items(items)
        # In a branch the objects would go to the branch and the sync state to main (see review.apply_refusal).
        if not dismiss and (refusal := review.apply_refusal()):
            return Response({"detail": refusal}, status=409)
        if review.in_background(items):
            return self._queue(request, items, dismiss)
        if dismiss:
            return Response({"dismissed": review.dismiss(request.user, items)})
        applied, failed, denied = review.apply(request.user, items)
        return Response({"applied": applied, "failed": failed, "missing_permissions": denied})

    def _queue(self, request, items, dismiss):
        """More open items than review_job_threshold: 202 with the background job(s) instead of the counts (#101)."""
        from ..jobs import ReviewBusy

        try:
            jobs = review.queue(request.user, items, dismiss=dismiss, whole_proposal=self.whole_proposal)
        except ReviewBusy as exc:
            job = review.visible_job(request.user, exc.job)
            return Response(
                {
                    "detail": "applying or dismissing items of this proposal is already queued or running (a job "
                    "that no worker finishes stops blocking new reviews 2 hours after it was queued)",
                    # Only a job the requester asked for or may view: another user's job ID is not theirs to learn.
                    "job": job.pk if job else None,
                },
                status=409,
            )
        context = {"request": request}
        if self.whole_proposal:
            return Response(JobSerializer(jobs[0], context=context).data, status=202)
        return Response({"jobs": JobSerializer(jobs, many=True, context=context).data}, status=202)


class ImportProposalViewSet(
    _ReviewActionsMixin,
    _viewset(
        models.ImportProposal,
        # The newest review job of each proposal, for the serializer's review_job, in one query for the page.
        models.ImportProposal.objects.prefetch_related(_review_jobs()),
        base=NetBoxReadOnlyModelViewSet,
    ),
):
    whole_proposal = True

    def _proposal(self, request):
        # NetBox has already restricted the queryset to "add" (what POST maps to); start again from "change".
        self.queryset = models.ImportProposal.objects.restrict(request.user, "change").restrict(request.user, "view")
        return self.get_object()

    @action(detail=True, methods=["post"])
    def apply(self, request, pk):
        """Apply every pending item of the proposal (202 with a job above review_job_threshold)."""
        items = self._proposal(request).items.restrict(request.user, "change")
        return self._review(request, items, dismiss=False)

    @action(detail=True, methods=["post"])
    def dismiss(self, request, pk):
        """Dismiss every pending item of the proposal (202 with a job above review_job_threshold)."""
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
        """Apply the items listed in {"ids": [...]} (202 with {"jobs": [...]} above review_job_threshold)."""
        return self._review(request, self._selected(request), dismiss=False)

    @action(detail=False, methods=["post"])
    def dismiss(self, request):
        """Dismiss the items listed in {"ids": [...]} (202 with {"jobs": [...]} above review_job_threshold)."""
        return self._review(request, self._selected(request), dismiss=True)
