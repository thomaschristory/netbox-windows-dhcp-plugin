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


# The refusal names at most this many scopes or reservations, then counts the rest.
MESSAGE_LIMIT = 5

_KIND = {"change_scope": "scope", "change_reservation": "reservation"}


def _describe(codename, obj):
    if codename == "change_scope":
        return f"{obj.server.name}/{obj.scope_id}"
    return f"{obj.scope.server.name}/{obj.scope.scope_id}/{obj.ip_address.address.ip if obj.ip_address_id else obj}"


def _refusal(fields, missing):
    names = ", ".join(fields)
    lines = []
    for codename, obj in missing[:MESSAGE_LIMIT]:
        kind = _KIND[codename]
        lines.append(
            f'Changing {names} would change {kind} {_describe(codename, obj)} on Windows. You need the "change {kind}" '
            "permission on it."
        )
    if len(missing) > MESSAGE_LIMIT:
        lines.append(f"And {len(missing) - MESSAGE_LIMIT} more.")
    return " ".join(lines)


def _request_user():
    from netbox.context import current_request

    request = current_request.get()
    user = getattr(request, "user", None) if request is not None else None
    return user if user is not None and user.is_authenticated else None


def check_edit(sender, instance, raw=False, **kwargs):
    """
    pre_save receiver for the four watched models: refuse (AbortRequest) an edit that changes what a push writes to
    Windows when the user may not change every scope or reservation affected. NetBox's views and REST API turn the
    exception into an error message (or HTTP 400) and roll the request back.
    """
    if raw:
        # Fixture loading: not an edit by anyone.
        return
    fields = changed_fields(instance)
    # Read by signals._changed after the save: no push for edits Windows never sees.
    instance._nbwd_push_fields = fields
    if not fields:
        return
    from .integrations.branching import applying_branch
    from .signals import pushes_suppressed

    # Import apply writes what Windows already has; a merge or revert replays edits checked when they were made.
    if pushes_suppressed() or applying_branch():
        return
    # nbshell and management commands have no user: nothing to check, the change log still records the edit.
    user = _request_user()
    if user is None:
        return
    from utilities.exceptions import AbortRequest

    missing = []
    for codename, queryset in dependents(instance):
        allowed = set(queryset.restrict(user, "change").values_list("pk", flat=True))
        missing.extend((codename, obj) for obj in queryset if obj.pk not in allowed)
    if missing:
        raise AbortRequest(_refusal(fields, missing))


def connect():
    from dcim.models import MACAddress
    from django.db.models.signals import pre_save
    from ipam.models import IPAddress, IPRange, Prefix

    for model in (Prefix, IPRange, IPAddress, MACAddress):
        pre_save.connect(check_edit, sender=model, dispatch_uid=f"{PLUGIN}.guard.{model._meta.label_lower}")
