from netbox.plugins import PluginTemplateExtension


class _WindowsServersPanel(PluginTemplateExtension):
    """The Windows DHCP servers running on this device or virtual machine, on its page (nothing when there is none)."""

    field = None

    def right_page(self):
        from .models import WindowsServer

        obj = self.context["object"]
        servers = (
            WindowsServer.objects.restrict(self.context["request"].user, "view")
            .filter(**{self.field: obj})
            .order_by("name")
        )
        if not servers:
            return ""
        return self.render("netbox_windows_dhcp_plugin/inc/windows_servers_panel.html", {"windows_servers": servers})


class DeviceWindowsServers(_WindowsServersPanel):
    models = ["dcim.device"]
    field = "device"


class VirtualMachineWindowsServers(_WindowsServersPanel):
    models = ["virtualization.virtualmachine"]
    field = "virtual_machine"


template_extensions = [DeviceWindowsServers, VirtualMachineWindowsServers]
