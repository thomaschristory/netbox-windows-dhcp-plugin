from ipam.models import Prefix
from netbox_dhcp.models import DHCPServer, Subnet

from netbox_windows_dhcp_plugin.models import WindowsServer

PLUGIN = "netbox_windows_dhcp_plugin"


def dhcp_servers(count=3):
    return [DHCPServer.objects.create(name=f"dhcp{i}") for i in range(1, count + 1)]


def subnets(server, count=3):
    result = []
    for i in range(1, count + 1):
        prefix = Prefix.objects.create(prefix=f"10.{i}.0.0/24")
        result.append(Subnet.objects.create(name=f"{server.name}/10.{i}.0.0", dhcp_server=server, prefix=prefix))
    return result


def windows_server(dhcp_server, **kwargs):
    return WindowsServer.objects.create(
        dhcp_server=dhcp_server, host=f"{dhcp_server.name}.example.com", credential_profile="lab", **kwargs
    )


class PluginViewMixin:
    def _get_base_url(self):
        return f"plugins:{PLUGIN}:{self.model._meta.model_name}_{{}}"


class PluginAPIMixin:
    view_namespace = f"plugins-api:{PLUGIN}"
