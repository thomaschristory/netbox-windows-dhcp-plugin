from django.utils.translation import gettext_lazy as _
from netbox.object_actions import ObjectAction


class TestConnection(ObjectAction):
    """Queue a connection test against a Windows server."""

    name = "test"
    label = _("Test connection")
    permissions_required = {"change"}
    url_kwargs = ["pk"]
    template_name = "netbox_windows_dhcp_plugin/buttons/test_connection.html"
