"""
Read what NetBox holds for one Windows server as sync records, in the same shape ``windows_state`` produces.

Under ``activate_branch`` the queries read the branch, so a repeated import compares Windows with its own branch.
"""

import ipaddress

from django.contrib.contenttypes.models import ContentType
from django.db.models import Prefetch, Q
from netbox_dhcp.models import HostReservation, Option, OptionDefinition, Pool, SharedNetwork, Subnet

from ..models import (
    ExclusionRange,
    FailoverRelationship,
    MacFilter,
    MacFilterLists,
    WindowsClass,
    WindowsPolicy,
    WindowsServer,
    WindowsVendorOption,
    WindowsVendorOptionDefinition,
)
from ..windows import normalize
from . import windows_state
from .diff import Record
from .windows_state import class_key, mac_key, option_key, optiondef_key, policy_key, split_values

__all__ = (
    "SYS4_TYPE",
    "WINDOWS_TYPE",
    "partner_failovers",
    "read",
    "scope_id_of",
    "server_subnets",
    "split_values",
    "standard_codes",
)

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


def standard_codes():
    """DHCPv4 option codes netbox-plugin-dhcp defines itself (Kea's standard set)."""
    return set(OptionDefinition.objects.filter(standard=True, space="dhcp4").values_list("code", flat=True))


def server_subnets(dhcp_server):
    """IPv4 subnets of a server, directly or through one of its shared networks (superscopes)."""
    return (
        Subnet.objects.filter(Q(dhcp_server=dhcp_server) | Q(shared_network__dhcp_server=dhcp_server))
        .filter(prefix__prefix__family=4)
        .select_related("prefix", "netbox_windows_dhcp_plugin_windows_scope")
        .prefetch_related(Prefetch("child_pools", queryset=Pool.objects.order_by("pk").select_related("ip_range")))
    )


def scope_id_of(subnet):
    return str(subnet.prefix.prefix.network)


def _mask(prefix_length):
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix_length}").netmask)


def _scope(subnet):
    ws = getattr(subnet, "netbox_windows_dhcp_plugin_windows_scope", None)
    # The first pool by primary key: the one the writer updates too.
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
    # A free-text client ID: in the aa-bb-cc form Windows reports when it is a MAC address.
    try:
        return normalize.canonical_mac(reservation.client_id or "")
    except ValueError:
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
    return Record(
        "optiondef",
        optiondef_key(definition.code),
        {
            "option_id": definition.code,
            "name": definition.name,
            "type": (wd.windows_type if wd and wd.windows_type else WINDOWS_TYPE.get(definition.type, "BinaryData")),
            "multi_valued": bool(definition.array),
            "default_value": list(wd.default_value) if wd else [],
            "description": definition.description,
            "vendor_class": "",
        },
    )


def _vendor_optiondef(d):
    return Record(
        "optiondef",
        optiondef_key(d.code, d.vendor_class.name),
        {
            "option_id": d.code,
            "name": d.name,
            "type": d.windows_type,
            "multi_valued": bool(d.multi_valued),
            "default_value": list(d.default_value),
            "description": d.description,
            "vendor_class": d.vendor_class.name,
        },
    )


def read(windows_server, for_import=False):
    """
    Return the records NetBox holds for this server. ``for_import`` reads a server without MAC filter list switches
    as having Windows' defaults (both lists off), so an import only proposes them when Windows differs; a push leaves
    the switches alone instead.
    """
    dhcp = windows_server.dhcp_server
    records = []

    for c in WindowsClass.objects.filter(dhcp_server=dhcp, family=4):
        records.append(
            Record(
                "class",
                class_key(c.class_type, c.name),
                {"name": c.name, "class_type": c.class_type, "data": c.data, "description": c.description},
            )
        )

    definitions = OptionDefinition.objects.filter(dhcp_server=dhcp, family=4, standard=False).select_related(
        "netbox_windows_dhcp_plugin_windows_option_definition"
    )
    records.extend(_optiondef(d) for d in definitions)
    vendor_definitions = WindowsVendorOptionDefinition.objects.filter(
        vendor_class__dhcp_server=dhcp, vendor_class__family=4
    ).select_related("vendor_class")
    records.extend(_vendor_optiondef(d) for d in vendor_definitions)

    subnets = list(server_subnets(dhcp))
    scopes = _unique((_scope(s), s.pk) for s in subnets)
    scope_ids = {pk: record.key for record, pk in scopes}
    records.extend(record for record, _ in scopes)
    records.extend(_superscopes(dhcp))

    exclusions = ExclusionRange.objects.filter(subnet__in=subnets).order_by("pk")
    records.extend(record for record, _ in _unique((_exclusion(e, scope_ids[e.subnet_id]), e.pk) for e in exclusions))

    reservations = (
        HostReservation.objects.filter(subnet__in=subnets)
        .select_related("ipv4_address", "hw_address", "netbox_windows_dhcp_plugin_windows_reservation")
        .order_by("pk")
    )
    unique = _unique((_reservation(r, scope_ids[r.subnet_id]), r.pk) for r in reservations)
    reservation_parents = {pk: record.key for record, pk in unique}
    records.extend(record for record, _ in unique)

    records.extend(_policies(dhcp, scope_ids))
    records.extend(_options(dhcp, scope_ids, reservation_parents))
    records.extend(_failovers(windows_server, scope_ids))
    records.extend(_mac_filters(dhcp, for_import))
    return records


def _superscopes(dhcp):
    networks = (
        SharedNetwork.objects.filter(dhcp_server=dhcp, prefix__prefix__family=4)
        .prefetch_related(Prefetch("child_subnets", queryset=Subnet.objects.select_related("prefix")))
        .order_by("pk")
    )
    pairs = []
    for network in networks:
        # The Windows name is the description (the name is "<server>/<superscope>", unique across servers); a shared
        # network made by hand in NetBox may only have a name.
        name = network.description or network.name
        members = windows_state.scope_ids(scope_id_of(s) for s in network.child_subnets.all())
        pairs.append((Record("superscope", name.lower(), {"name": name, "scope_ids": members}), network.pk))
    return [record for record, _ in _unique(pairs)]


def _policies(dhcp, scope_ids):
    policies = WindowsPolicy.objects.filter(dhcp_server=dhcp).order_by("pk")
    pairs = []
    for p in policies:
        if p.subnet_id is not None and p.subnet_id not in scope_ids:
            continue  # an IPv6 subnet, or one of another server
        scope_id = scope_ids.get(p.subnet_id, "")
        record = Record(
            "policy",
            policy_key(scope_id, p.name),
            {
                "name": p.name,
                "scope_id": scope_id,
                "description": p.description,
                "enabled": p.enabled,
                "processing_order": p.processing_order,
                "condition": p.condition,
                "criteria": windows_state.criteria(p.criteria or {}),
                "lease_seconds": p.lease_duration,
                "ip_ranges": windows_state.ip_ranges(p.ip_ranges or []) if scope_id else [],
            },
        )
        pairs.append((record, p.pk))
    return [record for record, _ in _unique(pairs)]


def partner_failovers(windows_server):
    """Lowercase names of the relationships this server is the partner in: they are synced from the primary."""
    names = FailoverRelationship.objects.filter(partner_server=windows_server.dhcp_server).values_list(
        "name", flat=True
    )
    return {n.lower() for n in names}


def _partner_host(dhcp_server):
    ws = WindowsServer.objects.filter(dhcp_server=dhcp_server).first()
    return ws.host.lower() if ws else ""


def _failovers(windows_server, scope_ids):
    relationships = (
        FailoverRelationship.objects.filter(primary_server=windows_server.dhcp_server)
        .select_related("partner_server")
        .prefetch_related("subnets")
        .order_by("pk")
    )
    pairs = []
    for f in relationships:
        hot = f.mode == "hotstandby"
        record = Record(
            "failover",
            f.name.lower(),
            {
                "name": f.name,
                "partner_server": _partner_host(f.partner_server),
                "mode": f.mode,
                "max_client_lead_time": f.max_client_lead_time,
                "load_balance_percent": None if hot else f.load_balance_percent,
                "reserve_percent": f.reserve_percent if hot else None,
                "server_role": f.server_role if hot else "",
                "state_switch_interval": f.state_switch_interval,
                # Windows only says whether a secret is set; the secret itself never leaves the credential profile.
                "auth": bool(f.shared_secret_profile),
                # Subnets of other servers cannot be in the relationship on this one; they are left out.
                "scope_ids": windows_state.scope_ids(scope_ids[s.pk] for s in f.subnets.all() if s.pk in scope_ids),
            },
        )
        pairs.append((record, f.pk))
    return [record for record, _ in _unique(pairs)]


def _mac_filters(dhcp, for_import):
    pairs = []
    for m in MacFilter.objects.filter(dhcp_server=dhcp).order_by("pk"):
        key = mac_key(m.mac_address)
        pairs.append(
            (Record("macfilter", key, {"mac_address": key, "list": m.list, "description": m.description}), m.pk)
        )
    records = [record for record, _ in _unique(pairs)]
    lists = MacFilterLists.objects.filter(dhcp_server=dhcp).first()
    if lists is not None:
        records.append(
            Record("filterlists", "server", {"allow": lists.allow_list_enabled, "deny": lists.deny_list_enabled})
        )
    elif for_import:
        records.append(Record("filterlists", "server", {"allow": False, "deny": False}))
    return records


def _exclusion(e, scope_id):
    return Record(
        "exclusion",
        f"{scope_id}/{e.start_address}-{e.end_address}",
        {"scope_id": scope_id, "start_range": e.start_address, "end_range": e.end_address},
    )


def _unique(pairs):
    """
    Make record keys unique. NetBox can hold what Windows cannot: two subnets on one prefix, two reservations for
    one address, a reservation without an address. The extra ones get "#<pk>" appended, so they show up as deletions
    instead of making the import fail, and the writer finds exactly that object.
    """
    seen = set()
    out = []
    for record, pk in pairs:
        key = record.key
        if key in seen or key.endswith("/"):
            key = f"{key}#{pk}"
        seen.add(key)
        out.append((Record(record.kind, key, record.fields), pk))
    return out


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
            "netbox_windows_dhcp_plugin_windows_qualifier__policy",
        )
        .order_by("pk")
    )
    seen = set()
    for o in options:
        qualifier = getattr(o, "netbox_windows_dhcp_plugin_windows_qualifier", None)
        user = qualifier.user_class.name if qualifier and qualifier.user_class_id else ""
        policy = qualifier.policy.name if qualifier and qualifier.policy_id else ""
        parent = parents[o.assigned_object_type_id, o.assigned_object_id]
        yield _option_record(seen, o.pk, parent, o.definition.code, split_values(o.data), "", user, policy)

    vendor_options = (
        WindowsVendorOption.objects.filter(q)
        .select_related("definition__vendor_class", "user_class", "policy")
        .order_by("pk")
    )
    for o in vendor_options:
        parent = parents[o.assigned_object_type_id, o.assigned_object_id]
        user = o.user_class.name if o.user_class_id else ""
        policy = o.policy.name if o.policy_id else ""
        vendor = o.definition.vendor_class.name
        yield _option_record(seen, o.pk, parent, o.definition.code, list(o.value), vendor, user, policy)


def _option_record(seen, pk, parent, code, value, vendor, user, policy):
    key = option_key(parent, code, vendor, user, policy)
    if key in seen:
        # netbox-plugin-dhcp lets the same option be set twice on one object; Windows cannot hold that, so the extra
        # copies show up as deletions.
        key = f"{key}#{pk}"
    seen.add(key)
    fields = {"parent": parent, "option_id": code, "value": value, "vendor_class": vendor, "user_class": user}
    if policy:
        fields["policy"] = policy
    return Record("option", key, fields)
