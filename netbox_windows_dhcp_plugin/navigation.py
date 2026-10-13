from django.utils.translation import gettext_lazy as _
from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem


def _item(model_name, label, add=True):
    buttons = ()
    if add:
        buttons = (
            PluginMenuButton(
                link=f"plugins:netbox_windows_dhcp_plugin:{model_name}_add",
                title=_("Add"),
                icon_class="mdi mdi-plus-thick",
                permissions=[f"netbox_windows_dhcp_plugin.add_{model_name}"],
            ),
        )
    return PluginMenuItem(
        link=f"plugins:netbox_windows_dhcp_plugin:{model_name}_list",
        link_text=label,
        permissions=[f"netbox_windows_dhcp_plugin.view_{model_name}"],
        buttons=buttons,
    )


menu = PluginMenu(
    label=_("Windows DHCP"),
    icon_class="mdi mdi-microsoft-windows",
    groups=(
        (
            _("Servers"),
            (
                _item("windowsserver", _("Windows servers")),
                _item("failoverrelationship", _("Failover relationships")),
                _item("macfilter", _("MAC filters")),
            ),
        ),
        (
            _("Scopes"),
            (
                _item("windowsscope", _("Scope settings")),
                _item("exclusionrange", _("Exclusion ranges")),
                _item("windowsreservation", _("Reservation settings")),
            ),
        ),
        (
            _("Options and policies"),
            (
                _item("windowsclass", _("User and vendor classes")),
                _item("windowspolicy", _("Policies")),
                _item("windowsoptiondefinition", _("Option definition settings")),
                _item("windowsoptionqualifier", _("Option qualifiers")),
            ),
        ),
        (
            _("Sync"),
            (
                _item("syncrun", _("Sync runs"), add=False),
                _item("importproposal", _("Import proposals"), add=False),
            ),
        ),
    ),
)
