from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
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


# -- Reservations using an object (issue #107) -----------------------------------------------------------------------
#
# A reservation's IP address and MAC address are protected: NetBox refuses to delete them, or the device, virtual
# machine or interface they are assigned to, while a reservation uses them. Deleting the address would leave the
# reservation without one, and taking the reservation with it would remove it from Windows on the next push, as a
# side effect nobody asked for. These panels show the reservations on the pages of the objects they hold, so whoever
# decommissions a server sees them before the delete is refused.


def _assigned_to(interfaces):
    """Reservations whose IP address or MAC address is assigned to one of these interfaces (a queryset of them)."""
    # IP and MAC addresses point at their interface through a generic foreign key (type and ID). Matching the type
    # and an ID subquery keeps this to one SQL query, with no duplicate rows when both addresses are on the device.
    object_type = ContentType.objects.get_for_model(interfaces.model)
    ids = interfaces.values("pk")
    return Q(ip_address__assigned_object_type=object_type, ip_address__assigned_object_id__in=ids) | Q(
        mac_address__assigned_object_type=object_type, mac_address__assigned_object_id__in=ids
    )


class _ReservationsPanel(PluginTemplateExtension):
    """The reservations (IPv4 and DHCPv6) that use this object, on its page (nothing when there is none)."""

    def lookup(self, obj):
        raise NotImplementedError

    def right_page(self):
        from .models import Reservation

        obj = self.context["object"]
        # One query: the reservations the user may view, with everything the table shows joined in.
        reservations = list(
            Reservation.objects.restrict(self.context["request"].user, "view")
            .filter(self.lookup(obj))
            .select_related("scope__server", "scope__prefix", "ip_address", "mac_address")
            .order_by("scope__server__name", "ip_address__address", "pk")
        )
        if not reservations:
            return ""
        return self.render("netbox_windows_dhcp_plugin/inc/reservations_panel.html", {"reservations": reservations})


class DeviceReservations(_ReservationsPanel):
    models = ["dcim.device"]

    def lookup(self, obj):
        from dcim.models import Interface

        return _assigned_to(Interface.objects.filter(device=obj))


class VirtualMachineReservations(_ReservationsPanel):
    models = ["virtualization.virtualmachine"]

    def lookup(self, obj):
        from virtualization.models import VMInterface

        return _assigned_to(VMInterface.objects.filter(virtual_machine=obj))


class InterfaceReservations(_ReservationsPanel):
    models = ["dcim.interface", "virtualization.vminterface"]

    def lookup(self, obj):
        return _assigned_to(type(obj).objects.filter(pk=obj.pk))


class IPAddressReservations(_ReservationsPanel):
    models = ["ipam.ipaddress"]

    def lookup(self, obj):
        return Q(ip_address=obj)


class MACAddressReservations(_ReservationsPanel):
    models = ["dcim.macaddress"]

    def lookup(self, obj):
        return Q(mac_address=obj)


template_extensions = [
    DeviceWindowsServers,
    VirtualMachineWindowsServers,
    DeviceReservations,
    VirtualMachineReservations,
    InterfaceReservations,
    IPAddressReservations,
    MACAddressReservations,
]
