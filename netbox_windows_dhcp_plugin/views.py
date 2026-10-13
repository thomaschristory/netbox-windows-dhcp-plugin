from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from extras.ui.panels import CustomFieldsPanel, TagsPanel
from netbox.object_actions import BulkDelete, BulkExport, CloneObject, DeleteObject, EditObject
from netbox.ui import layout
from netbox.ui.panels import CommentsPanel, JSONPanel, ObjectsTablePanel, RelatedObjectsPanel
from netbox.views import generic
from netbox.views.generic.base import BaseObjectView
from utilities.views import GetRelatedModelsMixin, GetReturnURLMixin, register_model_view

from . import filtersets, forms, review, tables
from .integrations import branching
from .jobs import TestConnectionJob, enqueue_import
from .models import (
    ExclusionRange,
    FailoverRelationship,
    ImportProposal,
    ImportProposalItem,
    MacFilter,
    SyncRun,
    WindowsClass,
    WindowsOptionDefinition,
    WindowsOptionQualifier,
    WindowsPolicy,
    WindowsReservation,
    WindowsScope,
    WindowsServer,
)
from .object_actions import (
    ApplyItem,
    ApplyProposal,
    ApplySelected,
    DismissItem,
    DismissProposal,
    DismissSelected,
    ImportNow,
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
    """Register the standard list, detail, edit, import, bulk edit and delete views for a model."""
    name = model.__name__

    def make(base, **attrs):
        return type(f"{name}{base.__name__}", (base,), {"queryset": queryset, **attrs})

    list_attrs = {"actions": list_actions} if list_actions is not None else {}
    register_model_view(model, "list", path="", detail=False)(
        make(generic.ObjectListView, table=table, filterset=filterset, filterset_form=filterset_form, **list_attrs)
    )
    detail_attrs = {"actions": detail_actions} if detail_actions is not None else {}
    register_model_view(model)(
        detail_view or make(generic.ObjectView, layout=layout, template_name="generic/object.html", **detail_attrs)
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


def _pk_filter(name):
    return {name: lambda ctx: ctx["object"].pk}


#
# Windows servers
#


class WindowsServerView(GetRelatedModelsMixin, generic.ObjectView):
    queryset = WindowsServer.objects.all()
    template_name = "generic/object.html"
    actions = (ImportNow, TestConnection, CloneObject, EditObject, DeleteObject)
    layout = layout.SimpleLayout(
        left_panels=[panels.WindowsServerPanel(), panels.WindowsServerSyncPanel(), TagsPanel(), CommentsPanel()],
        right_panels=[panels.WindowsServerSecurityPanel(), RelatedObjectsPanel(), CustomFieldsPanel()],
        bottom_panels=[
            ObjectsTablePanel(
                model="netbox_windows_dhcp_plugin.SyncRun",
                filters=_pk_filter("windows_server_id"),
                exclude_columns=["windows_server"],
            ),
        ],
    )

    def get_extra_context(self, request, instance):
        return {"related_models": self.get_related_models(request, instance)}


_register(
    WindowsServer,
    WindowsServer.objects.select_related("dhcp_server", "import_vrf"),
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
    def post(self, request, pk):
        server = self._server(request, pk)
        missing = branching.missing_import_permissions(request.user)
        if missing:
            messages.error(request, _("Importing needs the permission: {perms}").format(perms=", ".join(missing)))
        elif (job := enqueue_import(server, request.user)) is None:
            messages.warning(request, _("An import from {server} is already queued or running").format(server=server))
        else:
            messages.success(request, _("Queued import (job #{id}) from {server}").format(id=job.pk, server=server))
        return redirect(self.get_return_url(request, server))


#
# Scope and reservation settings
#

_register(
    WindowsScope,
    WindowsScope.objects.select_related("subnet"),
    table=tables.WindowsScopeTable,
    filterset=filtersets.WindowsScopeFilterSet,
    filterset_form=forms.WindowsScopeFilterForm,
    form=forms.WindowsScopeForm,
    import_form=forms.WindowsScopeImportForm,
    bulk_edit_form=forms.WindowsScopeBulkEditForm,
    layout=_simple_layout(panels.WindowsScopePanel()),
)

_register(
    WindowsReservation,
    WindowsReservation.objects.select_related("host_reservation"),
    table=tables.WindowsReservationTable,
    filterset=filtersets.WindowsReservationFilterSet,
    filterset_form=forms.WindowsReservationFilterForm,
    form=forms.WindowsReservationForm,
    import_form=forms.WindowsReservationImportForm,
    bulk_edit_form=forms.WindowsReservationBulkEditForm,
    layout=_simple_layout(panels.WindowsReservationPanel()),
)

_register(
    ExclusionRange,
    ExclusionRange.objects.select_related("subnet"),
    table=tables.ExclusionRangeTable,
    filterset=filtersets.ExclusionRangeFilterSet,
    filterset_form=forms.ExclusionRangeFilterForm,
    form=forms.ExclusionRangeForm,
    import_form=forms.ExclusionRangeImportForm,
    bulk_edit_form=forms.ExclusionRangeBulkEditForm,
    layout=_simple_layout(panels.ExclusionRangePanel(), comments=True),
)


#
# Classes, option definitions, policies and option qualifiers
#

_register(
    WindowsClass,
    WindowsClass.objects.select_related("dhcp_server"),
    table=tables.WindowsClassTable,
    filterset=filtersets.WindowsClassFilterSet,
    filterset_form=forms.WindowsClassFilterForm,
    form=forms.WindowsClassForm,
    import_form=forms.WindowsClassImportForm,
    bulk_edit_form=forms.WindowsClassBulkEditForm,
    layout=_simple_layout(panels.WindowsClassPanel(), comments=True),
)

_register(
    WindowsOptionDefinition,
    WindowsOptionDefinition.objects.select_related("option_definition", "vendor_class"),
    table=tables.WindowsOptionDefinitionTable,
    filterset=filtersets.WindowsOptionDefinitionFilterSet,
    filterset_form=forms.WindowsOptionDefinitionFilterForm,
    form=forms.WindowsOptionDefinitionForm,
    layout=_simple_layout(panels.WindowsOptionDefinitionPanel()),
)

_register(
    WindowsPolicy,
    WindowsPolicy.objects.select_related("dhcp_server", "subnet"),
    table=tables.WindowsPolicyTable,
    filterset=filtersets.WindowsPolicyFilterSet,
    filterset_form=forms.WindowsPolicyFilterForm,
    form=forms.WindowsPolicyForm,
    bulk_edit_form=forms.WindowsPolicyBulkEditForm,
    layout=_simple_layout(
        panels.WindowsPolicyPanel(),
        right=(JSONPanel("criteria", title="Criteria"), JSONPanel("ip_ranges", title="IP ranges")),
        bottom=(
            ObjectsTablePanel(
                model="netbox_windows_dhcp_plugin.WindowsOptionQualifier",
                filters=_pk_filter("policy_id"),
                exclude_columns=["policy"],
                title="Options",
            ),
        ),
        comments=True,
    ),
)

_register(
    WindowsOptionQualifier,
    WindowsOptionQualifier.objects.select_related("option", "user_class", "vendor_class", "policy"),
    table=tables.WindowsOptionQualifierTable,
    filterset=filtersets.WindowsOptionQualifierFilterSet,
    filterset_form=forms.WindowsOptionQualifierFilterForm,
    form=forms.WindowsOptionQualifierForm,
    layout=_simple_layout(panels.WindowsOptionQualifierPanel()),
)


#
# Failover and MAC filters
#

_register(
    FailoverRelationship,
    FailoverRelationship.objects.select_related("primary_server", "partner_server").prefetch_related("subnets"),
    table=tables.FailoverRelationshipTable,
    filterset=filtersets.FailoverRelationshipFilterSet,
    filterset_form=forms.FailoverRelationshipFilterForm,
    form=forms.FailoverRelationshipForm,
    bulk_edit_form=forms.FailoverRelationshipBulkEditForm,
    layout=_simple_layout(panels.FailoverRelationshipPanel(), comments=True),
)

_register(
    MacFilter,
    MacFilter.objects.select_related("dhcp_server"),
    table=tables.MacFilterTable,
    filterset=filtersets.MacFilterFilterSet,
    filterset_form=forms.MacFilterFilterForm,
    form=forms.MacFilterForm,
    import_form=forms.MacFilterImportForm,
    bulk_edit_form=forms.MacFilterBulkEditForm,
    layout=_simple_layout(panels.MacFilterPanel(), comments=True),
)


#
# Sync runs and import proposals (written by jobs, read-only in the UI)
#

_register(
    SyncRun,
    SyncRun.objects.select_related("windows_server__dhcp_server", "job"),
    table=tables.SyncRunTable,
    filterset=filtersets.SyncRunFilterSet,
    filterset_form=forms.SyncRunFilterForm,
    layout=layout.SimpleLayout(
        left_panels=[panels.SyncRunPanel()],
        right_panels=[JSONPanel("summary", title="Summary")],
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
    ImportProposal.objects.select_related("windows_server__dhcp_server", "sync_run").annotate(
        item_count=Count("items")
    ),
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
