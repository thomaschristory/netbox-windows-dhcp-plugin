"""
Edits of the core NetBox objects that scopes and reservations point at (issue #102).

A scope reads its prefix and IP range, a reservation its IP address and MAC address. Changing those values in IPAM
changes what the next push writes to Windows, so such an edit is a DHCP change: it needs the plugin's permission on
the scope or reservation that uses the object. This module knows which fields a push reads and which plugin objects
depend on an object; the refusal itself is `check_edit`, a pre_save receiver.
"""

from netaddr import EUI, IPNetwork

PLUGIN = "netbox_windows_dhcp_plugin"

# Model label -> the fields a push reads from it (see sync/netbox_state.py). Nothing else on these objects (status,
# VRF, tenant, description, tags, DNS name, interface) ever reaches Windows.
WATCHED = {
    "ipam.prefix": ("prefix",),
    "ipam.iprange": ("start_address", "end_address"),
    "ipam.ipaddress": ("address",),
    "dcim.macaddress": ("mac_address",),
}


def push_value(label, field, value):
    """The part of a field's value that a push sends, so that edits Windows never sees compare equal."""
    if value is None or value == "":
        return None
    if label == "dcim.macaddress":
        # Compare the 48-bit number: the stored value and the edited one can carry different display formats.
        return EUI(str(value)).value
    network = IPNetwork(value)
    if label == "ipam.prefix":
        # A prefix is the scope ID and subnet mask: network and length both count.
        return str(network.cidr)
    # Ranges and addresses: Windows gets the address alone, not the mask written next to it in NetBox.
    return str(network.ip)


def changed_fields(instance):
    """The watched fields whose pushed value differs from the stored row; () for new or unwatched objects."""
    label = instance._meta.label_lower
    fields = WATCHED.get(label)
    if not fields or instance._state.adding or instance.pk is None:
        return ()
    stored = type(instance).objects.filter(pk=instance.pk).values(*fields).first()
    if stored is None:
        return ()
    return tuple(f for f in fields if push_value(label, f, getattr(instance, f)) != push_value(label, f, stored[f]))


def dependents(instance):
    """
    The plugin objects whose Windows state depends on this core object, as [(permission codename, queryset)], with
    only the non-empty querysets.
    """
    from .models import Reservation, Scope

    label = instance._meta.label_lower
    if label == "ipam.prefix":
        found = [("change_scope", Scope.objects.filter(prefix_id=instance.pk).select_related("server"))]
    elif label == "ipam.iprange":
        found = [("change_scope", Scope.objects.filter(ip_range_id=instance.pk).select_related("server"))]
    elif label == "ipam.ipaddress":
        found = [
            (
                "change_reservation",
                Reservation.objects.filter(ip_address_id=instance.pk).select_related("scope__server", "ip_address"),
            )
        ]
    elif label == "dcim.macaddress":
        found = [
            (
                "change_reservation",
                Reservation.objects.filter(mac_address_id=instance.pk).select_related("scope__server", "ip_address"),
            )
        ]
    else:
        return []
    return [(codename, queryset) for codename, queryset in found if queryset.exists()]
