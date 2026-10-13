"""
Read what NetBox holds for one Windows server as sync records, in the same shape ``windows_state`` produces.

Under ``activate_branch`` the queries read the branch, so a repeated import compares Windows with its own branch.
"""

import ipaddress

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from netbox_dhcp.models import HostReservation, Option, OptionDefinition, Subnet

from ..models import ExclusionRange, WindowsClass
from ..windows import normalize
from .diff import Record
from .windows_state import option_key

__all__ = ("SYS4_TYPE", "WINDOWS_TYPE", "read", "scope_id_of", "server_subnets", "split_value", "standard_codes")

# Windows option data types to netbox-plugin-dhcp types. Types sys4 has no equivalent for are kept as binary, and
# WindowsOptionDefinition.windows_type remembers the original.
SYS4_TYPE = {
    "Byte": "uint8",
    "Word": "uint16",
    "DWord": "uint32",
    "IPv4Address": "ipv4-address",
    "IPv6Address": "ipv6-address",
    "String": "string",
    "BinaryData": "binary",
    "EncapsulatedData": "binary",
    "DWordDword": "binary",
}
WINDOWS_TYPE = {
    "uint8": "Byte",
    "int8": "Byte",
    "boolean": "Byte",
    "uint16": "Word",
    "int16": "Word",
    "uint32": "DWord",
    "int32": "DWord",
    "ipv4-address": "IPv4Address",
    "ipv6-address": "IPv6Address",
    "string": "String",
    "fqdn": "String",
}


def split_value(data):
    """Option.data holds a list as one comma-separated string."""
    return [v.strip() for v in data.split(",")] if data.strip() else []


def standard_codes():
    """DHCPv4 option codes netbox-plugin-dhcp defines itself (Kea's standard set)."""
    return set(OptionDefinition.objects.filter(standard=True, space="dhcp4").values_list("code", flat=True))


def server_subnets(dhcp_server):
    """IPv4 subnets of a server, directly or through one of its shared networks (superscopes)."""
    return (
        Subnet.objects.filter(Q(dhcp_server=dhcp_server) | Q(shared_network__dhcp_server=dhcp_server))
        .filter(prefix__prefix__family=4)
        .select_related("prefix", "netbox_windows_dhcp_plugin_windows_scope")
        .prefetch_related("child_pools__ip_range")
    )


def scope_id_of(subnet):
    return str(subnet.prefix.prefix.network)


def _mask(prefix_length):
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix_length}").netmask)


def _scope(subnet):
    ws = getattr(subnet, "netbox_windows_dhcp_plugin_windows_scope", None)
    pool = next(iter(subnet.child_pools.all()), None)
    rng = pool.ip_range if pool else None
    return Record(
        "scope",
        scope_id_of(subnet),
        {
            "name": subnet.description,
            "description": subnet.comments,
            "start_range": str(rng.start_address.ip) if rng else "",
            "end_range": str(rng.end_address.ip) if rng else "",
            "subnet_mask": _mask(subnet.prefix.prefix.prefixlen),
            "state": ws.state if ws else "active",
            "scope_type": ws.scope_type if ws else "dhcp",
            "lease_seconds": subnet.valid_lifetime,
            "delay_ms": ws.delay_ms if ws else 0,
            "max_bootp_clients": ws.max_bootp_clients if ws else None,
            "activate_policies": ws.activate_policies if ws else True,
        },
    )


def _client_id(reservation):
    if reservation.hw_address_id:
        return normalize.canonical_mac(str(reservation.hw_address.mac_address))
    return reservation.client_id or ""


def _reservation(reservation, scope_id):
    wr = getattr(reservation, "netbox_windows_dhcp_plugin_windows_reservation", None)
    ip = str(reservation.ipv4_address.address.ip) if reservation.ipv4_address_id else ""
    return Record(
        "reservation",
        f"{scope_id}/{ip}",
        {
            "scope_id": scope_id,
            "ip_address": ip,
            "client_id": _client_id(reservation),
            "name": reservation.description,
            "description": reservation.comments,
            "reservation_type": wr.reservation_type if wr else "both",
        },
    )


def _optiondef(definition):
    wd = getattr(definition, "netbox_windows_dhcp_plugin_windows_option_definition", None)
    vendor = wd.vendor_class.name if wd and wd.vendor_class_id else ""
    return Record(
        "optiondef",
        f"{vendor}/{definition.code}" if vendor else str(definition.code),
        {
            "option_id": definition.code,
            "name": definition.name,
            "type": (wd.windows_type if wd and wd.windows_type else WINDOWS_TYPE.get(definition.type, "BinaryData")),
            "multi_valued": bool(definition.array),
            "default_value": list(wd.default_value) if wd else [],
            "description": definition.description,
            "vendor_class": vendor,
        },
    )


def read(windows_server):
    """Return the records NetBox holds for this server."""
    dhcp = windows_server.dhcp_server
    records = []

    for c in WindowsClass.objects.filter(dhcp_server=dhcp, family=4):
        records.append(
            Record(
                "class",
                f"{c.class_type}/{c.name}",
                {"name": c.name, "class_type": c.class_type, "data": c.data, "description": c.description},
            )
        )

    definitions = OptionDefinition.objects.filter(dhcp_server=dhcp, family=4, standard=False).select_related(
        "netbox_windows_dhcp_plugin_windows_option_definition__vendor_class"
    )
    records.extend(_optiondef(d) for d in definitions)

    subnets = list(server_subnets(dhcp))
    scope_ids = {s.pk: scope_id_of(s) for s in subnets}
    records.extend(_scope(s) for s in subnets)

    for e in ExclusionRange.objects.filter(subnet__in=subnets).order_by("pk"):
        scope_id = scope_ids[e.subnet_id]
        records.append(
            Record(
                "exclusion",
                f"{scope_id}/{e.start_address}-{e.end_address}",
                {"scope_id": scope_id, "start_range": e.start_address, "end_range": e.end_address},
            )
        )

    reservations = HostReservation.objects.filter(subnet__in=subnets).select_related(
        "ipv4_address", "hw_address", "netbox_windows_dhcp_plugin_windows_reservation"
    )
    reservation_parents = {}
    for r in reservations:
        record = _reservation(r, scope_ids[r.subnet_id])
        reservation_parents[r.pk] = record.key
        records.append(record)

    records.extend(_options(dhcp, scope_ids, reservation_parents))
    return records


def option_parents(dhcp, scope_ids, reservation_parents):
    """(content type, object id) -> record parent ("server", a scope ID, or scope ID/IP)."""
    ct = ContentType.objects.get_for_models(*{type(dhcp), Subnet, HostReservation})
    parents = {(ct[type(dhcp)].pk, dhcp.pk): "server"}
    parents.update({(ct[Subnet].pk, pk): sid for pk, sid in scope_ids.items()})
    parents.update({(ct[HostReservation].pk, pk): key for pk, key in reservation_parents.items()})
    return parents


def _options(dhcp, scope_ids, reservation_parents):
    parents = option_parents(dhcp, scope_ids, reservation_parents)
    q = Q()
    for type_id, object_id in parents:
        q |= Q(assigned_object_type_id=type_id, assigned_object_id=object_id)
    options = (
        Option.objects.filter(q, definition__family=4)
        .select_related(
            "definition",
            "netbox_windows_dhcp_plugin_windows_qualifier__user_class",
            "netbox_windows_dhcp_plugin_windows_qualifier__vendor_class",
        )
        .order_by("pk")
    )
    seen = set()
    for o in options:
        qualifier = getattr(o, "netbox_windows_dhcp_plugin_windows_qualifier", None)
        if qualifier and qualifier.policy_id:
            continue  # policies are not imported yet
        vendor = qualifier.vendor_class.name if qualifier and qualifier.vendor_class_id else ""
        user = qualifier.user_class.name if qualifier and qualifier.user_class_id else ""
        parent = parents[o.assigned_object_type_id, o.assigned_object_id]
        key = option_key(parent, o.definition.code, vendor, user)
        if key in seen:
            # netbox-plugin-dhcp lets the same option be set twice on one object; Windows cannot hold that, so the
            # extra copies show up as deletions.
            key = f"{key}#{o.pk}"
        seen.add(key)
        yield Record(
            "option",
            key,
            {
                "parent": parent,
                "option_id": o.definition.code,
                "value": split_value(o.data),
                "vendor_class": vendor,
                "user_class": user,
            },
        )
