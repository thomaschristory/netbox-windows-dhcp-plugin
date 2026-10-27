from ipam.models import IPAddress, IPRange, Prefix
from netaddr import IPNetwork

from netbox_windows_dhcp_plugin.choices import ClassTypeChoices
from netbox_windows_dhcp_plugin.models import Reservation, Scope, WindowsClass, WindowsServer

PLUGIN = "netbox_windows_dhcp_plugin"


def windows_server(name="dhcp1", **kwargs):
    kwargs.setdefault("host", f"{name}.example.com")
    kwargs.setdefault("credential_profile", "lab")
    return WindowsServer.objects.create(name=name, **kwargs)


def windows_servers(count=3, start=1, **kwargs):
    return [windows_server(f"dhcp{i}", **kwargs) for i in range(start, start + count)]


def prefix(value, vrf=None):
    return Prefix.objects.create(prefix=IPNetwork(value), vrf=vrf)


def ip_range(start, end, vrf=None):
    return IPRange.objects.create(start_address=IPNetwork(start), end_address=IPNetwork(end), vrf=vrf)


def ip_address(value, vrf=None):
    return IPAddress.objects.create(address=IPNetwork(value), vrf=vrf)


def scope(server, network="10.1.0.0/24", start=None, end=None, **kwargs):
    """An IPv4 scope (with an IP range from .10 to .200 unless given) or, for an IPv6 network, a DHCPv6 scope."""
    net = IPNetwork(network)
    if net.version == 4 and "ip_range" not in kwargs:
        length = net.prefixlen
        start = start or f"{net.network + 10}/{length}"
        end = end or f"{net.network + 200}/{length}"
        kwargs["ip_range"] = ip_range(start, end)
    kwargs.setdefault("name", str(net.network))
    return Scope.objects.create(server=server, prefix=prefix(network), **kwargs)


def scopes(server, count=3):
    return [scope(server, f"10.{i}.0.0/24") for i in range(1, count + 1)]


def reservation(scope_, address, **kwargs):
    return Reservation.objects.create(scope=scope_, ip_address=ip_address(address), **kwargs)


def vendor_classes(server, count=1):
    return [
        WindowsClass.objects.create(
            server=server, name=f"vendor{i}", class_type=ClassTypeChoices.VENDOR, data=f"VENDOR{i}"
        )
        for i in range(1, count + 1)
    ]


class PluginViewMixin:
    def _get_base_url(self):
        return f"plugins:{PLUGIN}:{self.model._meta.model_name}_{{}}"


class PluginAPIMixin:
    view_namespace = f"plugins-api:{PLUGIN}"
