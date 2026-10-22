from django.template.context_processors import csrf
from django.utils.translation import gettext_lazy as _
from netbox.object_actions import ObjectAction


class _PostAction(ObjectAction):
    """A button that POSTs to the action's view, in a small form of its own."""

    template_name = "netbox_windows_dhcp_plugin/buttons/post.html"
    icon = ""
    color = "primary"

    @classmethod
    def get_context(cls, context, obj):
        return {"icon": cls.icon, "color": cls.color}

    @classmethod
    def render(cls, context, obj, **kwargs):
        # NetBox renders action buttons from a plain dictionary, not from the page's request context, so
        # {% csrf_token %} in our template would find no token and print nothing. Django then refuses the POST with
        # "CSRF verification failed" (403). Hand the token over the way Django's own context processor does.
        return super().render(context, obj, **{**csrf(context["request"]), **kwargs})


class _BulkPostAction(_PostAction):
    """
    A button under an object list that POSTs the selected rows to the action's view. It submits the list's own form,
    which already holds the CSRF token.
    """

    multi = True
    template_name = "netbox_windows_dhcp_plugin/buttons/bulk_post.html"


class TestConnection(_PostAction):
    """Queue a connection test against a Windows server."""

    name = "test"
    label = _("Test connection")
    permissions_required = {"change"}
    url_kwargs = ["pk"]
    icon = "mdi-lan-connect"
    color = "primary"


class ImportNow(_PostAction):
    """
    Queue an import from a Windows server. With netbox-branching, a dropdown to choose where it writes: an import
    branch (the safe default) or straight into main (issue #36), shown disabled with the reason when not allowed.
    """

    name = "import"
    label = _("Import now")
    permissions_required = {"change"}
    url_kwargs = ["pk"]
    icon = "mdi-database-import-outline"
    color = "purple"
    template_name = "netbox_windows_dhcp_plugin/buttons/import.html"

    @classmethod
    def get_context(cls, context, obj):
        from .choices import ImportDestinationChoices
        from .integrations import branching

        choices = ImportDestinationChoices.MAIN in branching.import_destinations()
        return {
            **super().get_context(context, obj),
            "choices": choices,
            "direct_refusal": branching.direct_import_refusal(context["request"].user) if choices else None,
            # Offered only while the user works in a branch; the form names it so the job never guesses.
            "active_branch": branching.active_branch_name() if choices else None,
        }


class PushNow(_PostAction):
    """Queue a push to a Windows server."""

    name = "push"
    label = _("Sync now")
    permissions_required = {"change"}
    url_kwargs = ["pk"]
    icon = "mdi-database-export-outline"
    color = "blue"


class DryRun(_PostAction):
    """Queue a dry run: plan a push without writing anything."""

    name = "dry_run"
    label = _("Dry run")
    permissions_required = {"change"}
    url_kwargs = ["pk"]
    icon = "mdi-file-compare"
    color = "gray"


class ApplyProposal(_PostAction):
    """Apply every pending item of an import proposal."""

    name = "apply"
    label = _("Apply all")
    permissions_required = {"change"}
    url_kwargs = ["pk"]
    icon = "mdi-check-all"
    color = "green"


class DismissProposal(_PostAction):
    """Dismiss every pending item of an import proposal."""

    name = "dismiss"
    label = _("Dismiss all")
    permissions_required = {"change"}
    url_kwargs = ["pk"]
    icon = "mdi-close-box-multiple-outline"
    color = "secondary"


class ApplyItem(ApplyProposal):
    label = _("Apply")
    icon = "mdi-check"


class DismissItem(DismissProposal):
    label = _("Dismiss")
    icon = "mdi-close"


class ApplySelected(_BulkPostAction):
    name = "bulk_apply"
    label = _("Apply selected")
    permissions_required = {"change"}
    icon = "mdi-check"
    color = "green"


class DismissSelected(_BulkPostAction):
    name = "bulk_dismiss"
    label = _("Dismiss selected")
    permissions_required = {"change"}
    icon = "mdi-close"
    color = "secondary"
