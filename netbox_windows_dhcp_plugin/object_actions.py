from django.utils.translation import gettext_lazy as _
from netbox.object_actions import ObjectAction


class TestConnection(ObjectAction):
    """Queue a connection test against a Windows server."""

    name = "test"
    label = _("Test connection")
    permissions_required = {"change"}
    url_kwargs = ["pk"]
    template_name = "netbox_windows_dhcp_plugin/buttons/test_connection.html"


class _PostAction(ObjectAction):
    """A button that POSTs to the action's view."""

    template_name = "netbox_windows_dhcp_plugin/buttons/post.html"
    icon = ""
    color = "primary"

    @classmethod
    def get_context(cls, context, obj):
        return {"icon": cls.icon, "color": cls.color}


class _BulkPostAction(_PostAction):
    """A button under an object list that POSTs the selected rows to the action's view."""

    multi = True
    template_name = "netbox_windows_dhcp_plugin/buttons/bulk_post.html"


class ImportNow(_PostAction):
    """Queue an import from a Windows server."""

    name = "import"
    label = _("Import now")
    permissions_required = {"change"}
    url_kwargs = ["pk"]
    icon = "mdi-database-import-outline"
    color = "purple"


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
