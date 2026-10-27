from django.contrib import messages
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from extras.ui.panels import CustomFieldsPanel, TagsPanel
from netbox.object_actions import (
    AddObject,
    BulkDelete,
    BulkEdit,
    BulkExport,
    BulkImport,
    CloneObject,
    DeleteObject,
    EditObject,
)
from netbox.ui import layout
from netbox.ui.panels import CommentsPanel, JSONPanel, ObjectsTablePanel, RelatedObjectsPanel
from netbox.views import generic
from netbox.views.generic.base import BaseObjectView
from utilities.htmx import htmx_partial
from utilities.views import GetRelatedModelsMixin, GetReturnURLMixin, ViewTab, register_model_view

from . import filtersets, forms, review, tables
from .choices import ClassTypeChoices, ImportDestinationChoices
from .integrations import branching
from .jobs import (
    ImportRefused,
    TestConnectionJob,
    enqueue_import,
    enqueue_leases,
    enqueue_push,
    import_refusal,
    lease_jobs,
    lease_read_in_progress,
)
from .models import (
    ExclusionRange,
    FailoverRelationship,
    ImportProposal,
    ImportProposalItem,
    MacFilter,
    MacFilterLists,
    OptionDefinition,
    OptionValue,
    Reservation,
    Scope,
    Superscope,
    SyncRun,
    WindowsClass,
    WindowsPolicy,
    WindowsServer,
)
from .object_actions import (
    ApplyItem,
    ApplyProposal,
    ApplySelected,
    DismissItem,
    DismissProposal,
    DismissSelected,
    DryRun,
    ImportNow,
    PushNow,
    TestConnection,
)
from .ui import panels


def _register(
    model,
    queryset,
    *,
    table,
    filterset,
    filterset_form,
    form=None,
    import_form=None,
    bulk_edit_form=None,
    layout=None,
    detail_view=None,
    read_only=False,
    list_actions=None,
    detail_actions=None,
):
    """
    Register the standard list, detail, edit, import, bulk edit and delete views for a model.

    The buttons default to the views registered here only. NetBox's defaults assume every view exists (Add, Import,
    Edit, Clone, Edit Selected, Rename Selected), and a button whose view is missing links to "None".
    """
    name = model.__name__

    def make(base, **attrs):
        return type(f"{name}{base.__name__}", (base,), {"queryset": queryset, **attrs})

    if list_actions is None:
        list_actions = (
            *(() if read_only else (AddObject,)),
            *((BulkImport,) if import_form is not None and not read_only else ()),
            BulkExport,
            *((BulkEdit,) if bulk_edit_form is not None and not read_only else ()),
            BulkDelete,
        )
    if detail_actions is None:
        detail_actions = (DeleteObject,) if read_only else (CloneObject, EditObject, DeleteObject)
    register_model_view(model, "list", path="", detail=False)(
        make(
            generic.ObjectListView,
            table=table,
            filterset=filterset,
            filterset_form=filterset_form,
            actions=list_actions,
        )
    )
    register_model_view(model)(
        detail_view
        or make(generic.ObjectView, layout=layout, template_name="generic/object.html", actions=detail_actions)
    )
    register_model_view(model, "delete")(make(generic.ObjectDeleteView))
    register_model_view(model, "bulk_delete", path="delete", detail=False)(
        make(generic.BulkDeleteView, filterset=filterset, table=table)
    )
    if read_only:
        return
    edit_view = make(generic.ObjectEditView, form=form)
    register_model_view(model, "add", detail=False)(edit_view)
    register_model_view(model, "edit")(edit_view)
    if import_form is not None:
        register_model_view(model, "bulk_import", detail=False)(
            make(generic.BulkImportView, model_form=import_form, table=table)
        )
    if bulk_edit_form is not None:
        register_model_view(model, "bulk_edit", path="edit", detail=False)(
            make(generic.BulkEditView, filterset=filterset, table=table, form=bulk_edit_form)
        )


def _simple_layout(*left, right=(), bottom=(), comments=False):
    return layout.SimpleLayout(
        left_panels=[*left, TagsPanel(), *([CommentsPanel()] if comments else [])],
        right_panels=[*right, CustomFieldsPanel()],
        bottom_panels=list(bottom),
    )


def _pk_filter(name, **extra):
    return {name: lambda ctx: ctx["object"].pk, **extra}


def _table(model_name, filter_name, *, exclude=(), title=None, **extra):
    """A table of this plugin's objects related to the page's object, loaded by HTMX like NetBox's own."""
    return ObjectsTablePanel(
        model=f"netbox_windows_dhcp_plugin.{model_name}",
        filters=_pk_filter(filter_name, **extra),
        exclude_columns=list(exclude),
        title=title,
    )


class _ClassValuesPanel(ObjectsTablePanel):
    """Option values of a class: vendor class values for a vendor class, user class values for a user class."""

    def __init__(self, class_type, **kwargs):
        super().__init__(**kwargs)
        self.class_type = class_type

    def should_render(self, context):
        return context["object"].class_type == self.class_type and super().should_render(context)


#
# Windows servers
#


class WindowsServerView(GetRelatedModelsMixin, generic.ObjectView):
    queryset = WindowsServer.objects.select_related("device", "virtual_machine", "import_vrf")
    template_name = "generic/object.html"
    actions = (PushNow, DryRun, ImportNow, TestConnection, CloneObject, EditObject, DeleteObject)
    layout = layout.SimpleLayout(
        left_panels=[
            panels.WindowsServerPanel(),
            panels.WindowsServerConnectionPanel(),
            panels.WindowsServerSyncPanel(),
            TagsPanel(),
            CommentsPanel(),
        ],
        right_panels=[
            panels.ImportStatusPanel(),
            panels.WindowsServerSecurityPanel(),
            RelatedObjectsPanel(),
            CustomFieldsPanel(),
        ],
        bottom_panels=[
            _table("Scope", "server_id", exclude=["server"]),
            _table("Superscope", "server_id", exclude=["server"]),
            _table("WindowsClass", "server_id", exclude=["server"]),
            _table("WindowsPolicy", "server_id", exclude=["server"]),
            _table("OptionDefinition", "server_id", exclude=["server"]),
            _table(
                "OptionValue",
                "server_id",
                exclude=["server", "level", "scope", "reservation"],
                title=_("Server option values"),
                level="server",
            ),
            _table("MacFilter", "server_id", exclude=["server"]),
            _table("FailoverRelationship", "server_id"),
            _table("SyncRun", "windows_server_id", exclude=["windows_server"]),
        ],
    )

    def get_extra_context(self, request, instance):
        return {"related_models": self.get_related_models(request, instance)}


_register(
    WindowsServer,
    WindowsServer.objects.select_related("device", "virtual_machine", "import_vrf"),
    table=tables.WindowsServerTable,
    filterset=filtersets.WindowsServerFilterSet,
    filterset_form=forms.WindowsServerFilterForm,
    form=forms.WindowsServerForm,
    import_form=forms.WindowsServerImportForm,
    bulk_edit_form=forms.WindowsServerBulkEditForm,
    detail_view=WindowsServerView,
)


@register_model_view(WindowsServer, "test")
class WindowsServerTestConnectionView(GetReturnURLMixin, BaseObjectView):
    queryset = WindowsServer.objects.all()

    def get_required_permission(self):
        return "netbox_windows_dhcp_plugin.change_windowsserver"

    def get(self, request, pk):
        return redirect(get_object_or_404(self.queryset, pk=pk).get_absolute_url())

    def _server(self, request, pk):
        # Object permissions may limit which servers the user may change.
        return get_object_or_404(self.queryset.restrict(request.user, "change"), pk=pk)

    def post(self, request, pk):
        server = self._server(request, pk)
        job = TestConnectionJob.enqueue(instance=server, user=request.user)
        messages.success(request, _("Queued connection test (job #{id}) for {server}").format(id=job.pk, server=server))
        return redirect(self.get_return_url(request, server))


@register_model_view(WindowsServer, "import")
class WindowsServerImportView(WindowsServerTestConnectionView):
    """
    Queue an import into the destination the user chose (issue #36): an import branch, the branch they work in, or
    main with netbox-branching; an import proposal without it. Main is never chosen silently: it needs this page's
    confirmation, which names main as the destination even while the user works in a branch. "This branch" names
    the branch in the form, and must still be the one the user works in.
    """

    def _destination(self, data):
        return data.get("destination") or branching.import_destinations()[0]

    def get(self, request, pk):
        server = self._server(request, pk)
        if self._destination(request.GET) != ImportDestinationChoices.MAIN:
            return redirect(server.get_absolute_url())
        refusal = import_refusal(ImportDestinationChoices.MAIN, request.user)
        if refusal:
            messages.error(request, _("Cannot import directly into main: {reason}").format(reason=refusal))
            return redirect(self.get_return_url(request, server))
        return render(
            request,
            "netbox_windows_dhcp_plugin/import_main_confirm.html",
            {
                "object": server,
                "form": forms.ImportConfirmForm(initial={"destination": ImportDestinationChoices.MAIN}),
                "active_branch": branching.active_branch_name(),
                # Writing main while an import branch holds the same objects makes that branch conflict on merge.
                "open_import_branch": branching.open_import_branch_name(server),
                "return_url": self.get_return_url(request, server),
                "button_class": "warning",
            },
        )

    def post(self, request, pk):
        server = self._server(request, pk)
        destination = self._destination(request.POST)
        if destination == ImportDestinationChoices.MAIN and "_confirm" not in request.POST:
            # Only the confirmation page may queue a direct import.
            return redirect(f"{request.path}?destination={ImportDestinationChoices.MAIN}")
        branch = request.POST.get("branch", "")
        if destination == ImportDestinationChoices.CURRENT and branch != branching.active_branch_name():
            # The page was shown in another branch (or on main) than the one active now: do not guess which one.
            messages.error(
                request,
                _("Import not queued: you are no longer working in branch {branch}. Choose again.").format(
                    branch=branch or "?"
                ),
            )
            return redirect(self.get_return_url(request, server))
        missing = branching.missing_import_permissions(request.user, destination)
        if missing:
            messages.error(request, _("Importing needs the permission: {perms}").format(perms=", ".join(missing)))
            return redirect(self.get_return_url(request, server))
        refusal = import_refusal(destination, request.user, branch)
        if refusal:
            messages.error(request, _("Import not queued: {reason}").format(reason=refusal))
            return redirect(self.get_return_url(request, server))
        try:
            job = enqueue_import(server, request.user, destination, branch)
        except ImportRefused:
            # Allowed a moment ago (a setting or permission changed in between): the next try says why.
            messages.error(request, _("Import not queued: it was refused, try again"))
            return redirect(self.get_return_url(request, server))
        if job is None:
            messages.warning(request, _("An import from {server} is already queued or running").format(server=server))
        elif destination == ImportDestinationChoices.BRANCH:
            messages.success(
                request,
                _(
                    "Queued import (job #{id}) from {server}. It writes into an import branch, not into main: the "
                    "Import panel shows the branch once the job has finished."
                ).format(id=job.pk, server=server),
            )
        elif destination == ImportDestinationChoices.CURRENT:
            messages.success(
                request,
                _(
                    "Queued import (job #{id}) from {server} into branch {branch}, not into main. No new branch is "
                    "created: the Import panel shows the result once the job has finished."
                ).format(id=job.pk, server=server, branch=branch),
            )
        elif destination == ImportDestinationChoices.MAIN:
            messages.success(
                request,
                _(
                    "Queued import (job #{id}) from {server} directly into main. Nothing has been imported yet: the "
                    "Import panel shows the result once the job has finished."
                ).format(id=job.pk, server=server),
            )
        else:
            messages.success(
                request,
                _(
                    "Queued import (job #{id}) from {server}. Differences go to an import proposal for review: the "
                    "Import panel links to it once the job has finished."
                ).format(id=job.pk, server=server),
            )
        return redirect(self.get_return_url(request, server))


@register_model_view(WindowsServer, "import_status", path="import-status")
class WindowsServerImportStatusView(BaseObjectView):
    """The Import panel alone, which the server page fetches again while an import is queued or running."""

    queryset = WindowsServer.objects.all()

    def get_required_permission(self):
        return "netbox_windows_dhcp_plugin.view_windowsserver"

    def get(self, request, pk):
        server = get_object_or_404(self.queryset.restrict(request.user, "view"), pk=pk)
        return HttpResponse(panels.ImportStatusPanel().render({"request": request, "object": server}))


@register_model_view(WindowsServer, "push")
class WindowsServerPushView(WindowsServerTestConnectionView):
    dry_run = False

    def post(self, request, pk):
        server = self._server(request, pk)
        what = _("dry run") if self.dry_run else _("push")
        if not self.dry_run and not server.sync_enabled:
            messages.error(request, _("Sync is disabled for {server}").format(server=server))
        elif (job := enqueue_push(server, request.user, dry_run=self.dry_run)) is None:
            messages.warning(request, _("A {what} for {server} is already queued").format(what=what, server=server))
        else:
            messages.success(
                request, _("Queued {what} (job #{id}) for {server}").format(what=what, id=job.pk, server=server)
            )
        return redirect(self.get_return_url(request, server))


@register_model_view(WindowsServer, "dry_run", path="dry-run")
class WindowsServerDryRunView(WindowsServerPushView):
    dry_run = True


#
# Superscopes, scopes, reservations and exclusion ranges
#

_register(
    Superscope,
    # Ordered explicitly: Django leaves Meta.ordering out of queries with an aggregate.
    Superscope.objects.select_related("server").annotate(scope_count=Count("scopes")).order_by("server__name", "name"),
    table=tables.SuperscopeTable,
    filterset=filtersets.SuperscopeFilterSet,
    filterset_form=forms.SuperscopeFilterForm,
    form=forms.SuperscopeForm,
    import_form=forms.SuperscopeImportForm,
    bulk_edit_form=forms.SuperscopeBulkEditForm,
    layout=_simple_layout(
        panels.SuperscopePanel(),
        bottom=(_table("Scope", "superscope_id", exclude=["server", "superscope"]),),
        comments=True,
    ),
)

_register(
    Scope,
    Scope.objects.select_related("server", "prefix__vrf", "ip_range", "superscope"),
    table=tables.ScopeTable,
    filterset=filtersets.ScopeFilterSet,
    filterset_form=forms.ScopeFilterForm,
    form=forms.ScopeForm,
    import_form=forms.ScopeImportForm,
    bulk_edit_form=forms.ScopeBulkEditForm,
    layout=_simple_layout(
        panels.ScopePanel(),
        right=(panels.ScopeIPv4Panel(), panels.ScopeIPv6Panel()),
        bottom=(
            _table("Reservation", "scope_id", exclude=["scope", "server"]),
            _table("ExclusionRange", "scope_id", exclude=["scope", "server"]),
            _table(
                "OptionValue",
                "scope_id",
                exclude=["server", "level", "scope", "reservation"],
                title=_("Scope option values"),
                level="scope",
            ),
            _table("WindowsPolicy", "scope_id", exclude=["server", "scope"]),
            _table("FailoverRelationship", "scope_id"),
        ),
        comments=True,
    ),
)

_register(
    Reservation,
    Reservation.objects.select_related("scope__server", "scope__prefix", "ip_address", "mac_address"),
    table=tables.ReservationTable,
    filterset=filtersets.ReservationFilterSet,
    filterset_form=forms.ReservationFilterForm,
    form=forms.ReservationForm,
    import_form=forms.ReservationImportForm,
    bulk_edit_form=forms.ReservationBulkEditForm,
    layout=_simple_layout(
        panels.ReservationPanel(),
        right=(panels.ReservationClientPanel(),),
        bottom=(
            _table(
                "OptionValue",
                "reservation_id",
                exclude=["server", "level", "scope", "reservation", "policy"],
                title=_("Reservation option values"),
            ),
        ),
        comments=True,
    ),
)

_register(
    ExclusionRange,
    ExclusionRange.objects.select_related("scope__server", "scope__prefix"),
    table=tables.ExclusionRangeTable,
    filterset=filtersets.ExclusionRangeFilterSet,
    filterset_form=forms.ExclusionRangeFilterForm,
    form=forms.ExclusionRangeForm,
    import_form=forms.ExclusionRangeImportForm,
    bulk_edit_form=forms.ExclusionRangeBulkEditForm,
    layout=_simple_layout(panels.ExclusionRangePanel(), comments=True),
)


#
# Classes, option definitions, policies and option values
#

_register(
    WindowsClass,
    WindowsClass.objects.select_related("server"),
    table=tables.WindowsClassTable,
    filterset=filtersets.WindowsClassFilterSet,
    filterset_form=forms.WindowsClassFilterForm,
    form=forms.WindowsClassForm,
    import_form=forms.WindowsClassImportForm,
    bulk_edit_form=forms.WindowsClassBulkEditForm,
    layout=_simple_layout(
        panels.WindowsClassPanel(),
        bottom=(
            ObjectsTablePanel(
                model="netbox_windows_dhcp_plugin.OptionDefinition",
                filters=_pk_filter("vendor_class_id"),
                exclude_columns=["server", "vendor_class"],
                title=_("Vendor option definitions"),
            ),
            _ClassValuesPanel(
                ClassTypeChoices.VENDOR,
                model="netbox_windows_dhcp_plugin.OptionValue",
                filters=_pk_filter("vendor_class_id"),
                exclude_columns=["server", "vendor_class"],
                title=_("Vendor option values"),
            ),
            _ClassValuesPanel(
                ClassTypeChoices.USER,
                model="netbox_windows_dhcp_plugin.OptionValue",
                filters=_pk_filter("user_class_id"),
                exclude_columns=["server", "user_class"],
                title=_("Option values for this user class"),
            ),
        ),
        comments=True,
    ),
)

_register(
    OptionDefinition,
    OptionDefinition.objects.select_related("server", "vendor_class"),
    table=tables.OptionDefinitionTable,
    filterset=filtersets.OptionDefinitionFilterSet,
    filterset_form=forms.OptionDefinitionFilterForm,
    form=forms.OptionDefinitionForm,
    import_form=forms.OptionDefinitionImportForm,
    bulk_edit_form=forms.OptionDefinitionBulkEditForm,
    layout=_simple_layout(panels.OptionDefinitionPanel()),
)

_register(
    WindowsPolicy,
    WindowsPolicy.objects.select_related("server", "scope"),
    table=tables.WindowsPolicyTable,
    filterset=filtersets.WindowsPolicyFilterSet,
    filterset_form=forms.WindowsPolicyFilterForm,
    form=forms.WindowsPolicyForm,
    bulk_edit_form=forms.WindowsPolicyBulkEditForm,
    layout=_simple_layout(
        panels.WindowsPolicyPanel(),
        right=(JSONPanel("criteria", title="Criteria"), JSONPanel("ip_ranges", title="IP ranges")),
        bottom=(_table("OptionValue", "policy_id", exclude=["server", "policy"], title=_("Option values")),),
        comments=True,
    ),
)

_register(
    OptionValue,
    OptionValue.objects.select_related(
        "server", "scope", "reservation__ip_address", "vendor_class", "user_class", "policy"
    ),
    table=tables.OptionValueTable,
    filterset=filtersets.OptionValueFilterSet,
    filterset_form=forms.OptionValueFilterForm,
    form=forms.OptionValueForm,
    import_form=forms.OptionValueImportForm,
    bulk_edit_form=forms.OptionValueBulkEditForm,
    layout=_simple_layout(panels.OptionValuePanel(), right=(panels.OptionQualifiersPanel(),)),
)


#
# Failover and MAC filters
#

_register(
    FailoverRelationship,
    FailoverRelationship.objects.select_related("primary_server", "partner_server").prefetch_related("scopes"),
    table=tables.FailoverRelationshipTable,
    filterset=filtersets.FailoverRelationshipFilterSet,
    filterset_form=forms.FailoverRelationshipFilterForm,
    form=forms.FailoverRelationshipForm,
    bulk_edit_form=forms.FailoverRelationshipBulkEditForm,
    layout=_simple_layout(panels.FailoverRelationshipPanel(), comments=True),
)

_register(
    MacFilter,
    MacFilter.objects.select_related("server"),
    table=tables.MacFilterTable,
    filterset=filtersets.MacFilterFilterSet,
    filterset_form=forms.MacFilterFilterForm,
    form=forms.MacFilterForm,
    import_form=forms.MacFilterImportForm,
    bulk_edit_form=forms.MacFilterBulkEditForm,
    layout=_simple_layout(panels.MacFilterPanel(), comments=True),
)

_register(
    MacFilterLists,
    MacFilterLists.objects.select_related("server"),
    table=tables.MacFilterListsTable,
    filterset=filtersets.MacFilterListsFilterSet,
    filterset_form=forms.MacFilterListsFilterForm,
    form=forms.MacFilterListsForm,
    bulk_edit_form=forms.MacFilterListsBulkEditForm,
    layout=_simple_layout(panels.MacFilterListsPanel()),
)


#
# Sync runs and import proposals (written by jobs, read-only in the UI)
#

_register(
    SyncRun,
    SyncRun.objects.select_related("windows_server", "job"),
    table=tables.SyncRunTable,
    filterset=filtersets.SyncRunFilterSet,
    filterset_form=forms.SyncRunFilterForm,
    layout=layout.SimpleLayout(
        left_panels=[panels.SyncRunPanel()],
        right_panels=[panels.ImportStatusPanel(title=_("Import result")), JSONPanel("summary", title="Summary")],
        bottom_panels=[
            ObjectsTablePanel(
                model="netbox_windows_dhcp_plugin.ImportProposal",
                filters=_pk_filter("sync_run_id"),
                exclude_columns=["sync_run"],
            ),
        ],
    ),
    read_only=True,
)

_register(
    ImportProposal,
    ImportProposal.objects.select_related("windows_server", "sync_run").annotate(item_count=Count("items")),
    table=tables.ImportProposalTable,
    filterset=filtersets.ImportProposalFilterSet,
    filterset_form=forms.ImportProposalFilterForm,
    layout=layout.SimpleLayout(
        left_panels=[panels.ImportProposalPanel()],
        bottom_panels=[
            ObjectsTablePanel(
                model="netbox_windows_dhcp_plugin.ImportProposalItem",
                filters=_pk_filter("proposal_id"),
                exclude_columns=["proposal"],
            ),
        ],
    ),
    read_only=True,
    detail_actions=(ApplyProposal, DismissProposal, DeleteObject),
)

_register(
    ImportProposalItem,
    ImportProposalItem.objects.select_related("proposal", "object_type"),
    table=tables.ImportProposalItemTable,
    filterset=filtersets.ImportProposalItemFilterSet,
    filterset_form=forms.ImportProposalItemFilterForm,
    layout=layout.SimpleLayout(
        left_panels=[panels.ImportProposalItemPanel()],
        right_panels=[JSONPanel("before", title="NetBox"), JSONPanel("after", title="Windows")],
    ),
    read_only=True,
    list_actions=(BulkExport, ApplySelected, DismissSelected, BulkDelete),
    detail_actions=(ApplyItem, DismissItem, DeleteObject),
)


class _ReviewView(GetReturnURLMixin, BaseObjectView):
    """POST-only views that apply or dismiss proposal items as the requesting user."""

    queryset = ImportProposalItem.objects.all()
    dismiss = False

    def get_required_permission(self):
        return "netbox_windows_dhcp_plugin.change_importproposalitem"

    def get(self, request, **kwargs):
        return redirect(self.get_return_url(request))

    def _items(self, request, obj):
        """The items to act on, restricted to those the user may change."""
        return ImportProposalItem.objects.restrict(request.user, "change").filter(pk=obj.pk)

    def post(self, request, pk=None):
        obj = get_object_or_404(self.queryset.restrict(request.user, "change"), pk=pk) if pk is not None else None
        items = review.open_items(self._items(request, obj))
        if self.dismiss:
            count = review.dismiss(request.user, items)
            messages.success(request, _("Dismissed {count} item(s)").format(count=count))
        else:
            applied, failed, denied = review.apply(request.user, items)
            messages.success(request, _("Applied {applied} item(s)").format(applied=applied))
            if failed:
                messages.warning(request, _("{failed} item(s) failed, see their error").format(failed=failed))
            if denied:
                messages.error(request, _("Not applied, missing permission: {perms}").format(perms=", ".join(denied)))
        return redirect(self.get_return_url(request, obj))


class _ProposalReviewView(_ReviewView):
    queryset = ImportProposal.objects.all()

    def get_required_permission(self):
        return "netbox_windows_dhcp_plugin.change_importproposal"

    def _items(self, request, obj):
        return obj.items.restrict(request.user, "change")


class _ItemReviewView(_ReviewView):
    pass


class _BulkReviewView(_ReviewView):
    filterset = filtersets.ImportProposalItemFilterSet

    def _items(self, request, obj):
        qs = ImportProposalItem.objects.restrict(request.user, "change")
        if request.POST.get("_all"):
            return self.filterset(request.GET, qs, request=request).qs
        return qs.filter(pk__in=[pk for pk in request.POST.getlist("pk") if pk.isdigit()])


for _dismiss, _verb in ((False, "apply"), (True, "dismiss")):
    _name = _verb.capitalize()
    _attrs = {"dismiss": _dismiss}
    register_model_view(ImportProposal, _verb)(type(f"ImportProposal{_name}View", (_ProposalReviewView,), _attrs))
    register_model_view(ImportProposalItem, _verb)(type(f"ImportProposalItem{_name}View", (_ItemReviewView,), _attrs))
    register_model_view(ImportProposalItem, f"bulk_{_verb}", path=_verb, detail=False)(
        type(f"ImportProposalItemBulk{_name}View", (_BulkReviewView,), _attrs)
    )


#
# Live leases, as a tab on the scope page
#


@register_model_view(Scope, "leases", path="leases")
class ScopeLeasesView(generic.ObjectView):
    """
    The last lease read of a scope, and a button to read again. The read runs in a job (LeaseJob); while it runs, the
    panel asks the server for itself again every few seconds (HTMX) until the job is done.
    """

    queryset = Scope.objects.select_related("server", "prefix")
    template_name = "netbox_windows_dhcp_plugin/scope_leases.html"
    # Viewing the scope is not enough: the leases come from its Windows server, which the user must be able to see.
    additional_permissions = ["netbox_windows_dhcp_plugin.view_windowsserver"]
    tab = ViewTab(
        label=_("Leases"),
        permission="netbox_windows_dhcp_plugin.view_windowsserver",
        weight=3000,
    )

    @staticmethod
    def _server(request, scope, action="view"):
        """The scope's Windows server, if this user may see it (or, with ``action="change"``, change it)."""
        return WindowsServer.objects.restrict(request.user, action).filter(pk=scope.server_id).first()

    def get_extra_context(self, request, instance):
        from core.choices import JobStatusChoices

        server = self._server(request, instance)
        job = lease_jobs(instance, server).first() if server else None
        running = lease_read_in_progress(job)
        return {
            # Waiting or running long past any reasonable read: the worker lost it.
            "lost": job is not None and not running and job.status in JobStatusChoices.ENQUEUED_STATE_CHOICES,
            "windows_server": server,
            # Reading leases connects to Windows, like the connection test: it needs the change permission.
            "can_load": server is not None and self._server(request, instance, "change") is not None,
            "in_branch": branching.in_branch(),
            "job": job,
            "running": running,
            "result": (job.data or {}) if job is not None and not running else {},
        }

    def get(self, request, *args, **kwargs):
        if htmx_partial(request):
            instance = self.get_object(**kwargs)
            context = {"object": instance, **self.get_extra_context(request, instance)}
            return render(request, "netbox_windows_dhcp_plugin/inc/lease_panel.html", context)
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        scope = self.get_object(**kwargs)
        # The server must be visible to the user as well as changeable, as on the page the button is on.
        server = self._server(request, scope) and self._server(request, scope, "change")
        if server is None:
            messages.error(request, _("Loading leases needs permission to change this scope's Windows server"))
        elif branching.in_branch():
            # The job runs against main, where this scope may differ or not exist.
            messages.error(request, _("Leases are read for scopes as they are in main: switch to main to load them"))
        elif enqueue_leases(scope, request.user) is None:
            messages.warning(request, _("Leases of {scope} are already being read").format(scope=scope))
        return redirect(reverse("plugins:netbox_windows_dhcp_plugin:scope_leases", kwargs={"pk": scope.pk}))
