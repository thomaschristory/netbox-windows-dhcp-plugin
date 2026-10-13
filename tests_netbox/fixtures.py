import json
from pathlib import Path

import netbox_dhcp
from ipam.models import Prefix
from netbox_dhcp.models import DHCPServer, OptionDefinition, Subnet

from netbox_windows_dhcp_plugin.choices import ClassTypeChoices
from netbox_windows_dhcp_plugin.models import WindowsClass, WindowsServer

PLUGIN = "netbox_windows_dhcp_plugin"


def dhcp_servers(count=3, start=1):
    return [DHCPServer.objects.create(name=f"dhcp{i}") for i in range(start, start + count)]


def subnets(server, count=3):
    result = []
    for i in range(1, count + 1):
        prefix = Prefix.objects.create(prefix=f"10.{i}.0.0/24")
        result.append(Subnet.objects.create(name=f"{server.name}/10.{i}.0.0", dhcp_server=server, prefix=prefix))
    return result


def ensure_standard_option_definitions():
    """
    Recreate netbox-plugin-dhcp's standard option definitions when a test database lost them.

    A data migration creates them, and a TransactionTestCase flushes every table when it ends, so a database kept
    with --keepdb no longer has them on the next run.
    """
    if OptionDefinition.objects.filter(standard=True).exists():
        return
    path = Path(netbox_dhcp.__file__).parent / "migrations" / "initial_data" / "option_definitions.json"
    # bulk_create, like the migration: sys4's save() rejects one of its own seeds (code 121 has type "internal").
    OptionDefinition.objects.bulk_create(
        OptionDefinition(standard=True, **definition) for definition in json.loads(path.read_text())
    )


def vendor_classes(server, count=1):
    return [
        WindowsClass.objects.create(
            dhcp_server=server, name=f"vendor{i}", class_type=ClassTypeChoices.VENDOR, data=f"VENDOR{i}"
        )
        for i in range(1, count + 1)
    ]


def windows_server(dhcp_server, **kwargs):
    return WindowsServer.objects.create(
        dhcp_server=dhcp_server, host=f"{dhcp_server.name}.example.com", credential_profile="lab", **kwargs
    )


class PluginViewMixin:
    def _get_base_url(self):
        return f"plugins:{PLUGIN}:{self.model._meta.model_name}_{{}}"


class PluginAPIMixin:
    view_namespace = f"plugins-api:{PLUGIN}"
