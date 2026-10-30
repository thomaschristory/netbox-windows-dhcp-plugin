"""
Read what NetBox holds for one Windows server as sync records, in the same shape ``windows_state`` produces.

Under ``activate_branch`` the queries read the branch, so a repeated import compares Windows with its own branch.
"""

import contextlib
import ipaddress

from ..models import (
    ExclusionRange,
    FailoverRelationship,
    MacFilter,
    MacFilterLists,
    OptionDefinition,
    OptionValue,
    Reservation,
    Scope,
    Superscope,
    WindowsClass,
    WindowsPolicy,
    WindowsServer,
)
from ..standard_options import STANDARD_V4, STANDARD_V6
from ..windows import normalize
from . import windows_state
from .diff import Record
from .push import DUPLICATE
from .windows_state import (
    class6_key,
    class_key,
    mac_key,
    option_key,
    optiondef_key,
    policy_key,
    reservation6_key,
    scope6_key,
    text_field,
)

__all__ = (
    "partner_failovers",
    "partner_servers",
    "prefix_of",
    "read",
    "scope_id_of",
    "server_scopes",
    "server_scopes6",
    "standard_codes",
    "standard_codes6",
)


def standard_codes():
    """Standard DHCPv4 option codes: Windows ships their definitions, so they are not synced as definitions."""
    return set(STANDARD_V4)


def standard_codes6():
    """Standard DHCPv6 option codes: the only DHCPv6 values synced (DHCPv6 option definitions are not)."""
    return set(STANDARD_V6)


def server_scopes(windows_server):
    """IPv4 scopes of a server."""
    return (
        Scope.objects.filter(server=windows_server, prefix__prefix__family=4)
        .select_related("prefix", "ip_range")
        .order_by("pk")
    )


def server_scopes6(windows_server):
    """DHCPv6 scopes of a server."""
    return Scope.objects.filter(server=windows_server, prefix__prefix__family=6).select_related("prefix").order_by("pk")


def scope_id_of(scope):
    """The Windows IPv4 scope ID of a scope: its network address."""
    return str(scope.prefix.prefix.network)


def prefix_of(scope):
    """The Windows DHCPv6 scope prefix of a scope: its network address, in the one spelling both sides use."""
    return windows_state.ipv6(str(scope.prefix.prefix.network))


def _mask(prefix_length):
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix_length}").netmask)


def _scope(scope):
    rng = scope.ip_range
    return Record(
        "scope",
        scope_id_of(scope),
        {
            "name": scope.name or "",
            "description": scope.description,
            "start_range": str(rng.start_address.ip) if rng else "",
            "end_range": str(rng.end_address.ip) if rng else "",
            "subnet_mask": _mask(scope.prefix.prefix.prefixlen),
            "state": scope.state,
            "scope_type": scope.scope_type,
            "lease_seconds": scope.lease_duration,
            "delay_ms": scope.delay_ms,
            "max_bootp_clients": scope.max_bootp_clients,
            "activate_policies": scope.activate_policies,
        },
    )


def _client_id(reservation):
    if reservation.mac_address_id:
        return normalize.canonical_mac(str(reservation.mac_address.mac_address))
    # A free-text client ID: in the aa-bb-cc form Windows reports when it is a MAC address.
    try:
        return normalize.canonical_mac(reservation.client_id or "")
    except ValueError:
        return reservation.client_id or ""


def _reservation(reservation, scope_id):
    ip = str(reservation.ip_address.address.ip) if reservation.ip_address_id else ""
    return Record(
        "reservation",
        f"{scope_id}/{ip}",
        {
            "scope_id": scope_id,
            "ip_address": ip,
            "client_id": _client_id(reservation),
            "name": reservation.name or "",
            "description": reservation.description,
            "reservation_type": reservation.reservation_type,
        },
    )


def _optiondef(d):
    vendor = d.vendor_class.name if d.vendor_class_id else ""
    return Record(
        "optiondef",
        optiondef_key(d.code, vendor),
        {
            "option_id": d.code,
            "name": d.name,
            "type": d.windows_type,
            "multi_valued": bool(d.multi_valued),
            "default_value": list(d.default_value or []),
            "description": d.description or "",
            "vendor_class": vendor,
        },
    )


def _optiondefs(windows_server):
    definitions = (
        OptionDefinition.objects.filter(server=windows_server, family=4).select_related("vendor_class").order_by("pk")
    )
    # Windows ships the standard definitions: windows_state leaves them out, so a NetBox definition of a standard code
    # is not synced either, or every import would propose deleting it. Codes 1 and 51 stay in, so a push explains why
    # it refuses them.
    standard = standard_codes()
    return [_optiondef(d) for d in definitions if d.vendor_class_id or d.code not in standard]


def read(windows_server, for_import=False):
    """
    Return the records NetBox holds for this server. ``for_import`` reads a server without MAC filter list switches
    as having Windows' defaults (both lists off), so an import only proposes them when Windows differs; a push leaves
    the switches alone instead.
    """
    records = []

    for c in WindowsClass.objects.filter(server=windows_server, family=4).order_by("pk"):
        records.append(
            Record(
                "class",
                class_key(c.class_type, c.name),
                {"name": c.name, "class_type": c.class_type, "data": c.data, "description": text_field(c.description)},
            )
        )

    records.extend(_optiondefs(windows_server))

    scope_objects = list(server_scopes(windows_server))
    scopes = _unique((_scope(s), s.pk) for s in scope_objects)
    scope_ids = {pk: record.key for record, pk in scopes}
    records.extend(record for record, _ in scopes)
    records.extend(_superscopes(windows_server, [(s.superscope_id, scope_ids[s.pk]) for s in scope_objects]))

    exclusions = ExclusionRange.objects.filter(scope__in=list(scope_ids)).order_by("pk")
    records.extend(record for record, _ in _unique((_exclusion(e, scope_ids[e.scope_id]), e.pk) for e in exclusions))

    reservations = (
        Reservation.objects.filter(scope__in=list(scope_ids)).select_related("ip_address", "mac_address").order_by("pk")
    )
    unique = _unique((_reservation(r, scope_ids[r.scope_id]), r.pk) for r in reservations)
    reservation_parents = {pk: record.key for record, pk in unique}
    records.extend(record for record, _ in unique)

    records.extend(_policies(windows_server, scope_ids))
    records.extend(_options(windows_server, scope_ids, reservation_parents))
    records.extend(_failovers(windows_server, scope_ids))
    records.extend(_mac_filters(windows_server, for_import))
    records.extend(_v6(windows_server))
    definitions = {
        optiondef_key(d.code, d.vendor_class.name if d.vendor_class_id else ""): d.windows_type
        for d in OptionDefinition.objects.filter(server=windows_server, family=4).select_related("vendor_class")
    }
    return windows_state.with_option_types(records, definitions)


def _scope6(scope):
    prefix = prefix_of(scope)
    return Record(
        "scope6",
        scope6_key(prefix),
        {
            "prefix": prefix,
            "prefix_length": scope.prefix.prefix.prefixlen,
            "name": scope.name or "",
            "description": scope.description,
            "state": scope.state,
            "preference": scope.preference or 0,
            "preferred_lifetime": scope.preferred_lifetime,
            "valid_lifetime": scope.valid_lifetime,
            "t1": scope.renew_timer or None,
            "t2": scope.rebind_timer or None,
        },
    )


def _reservation6(reservation, prefix):
    ip = windows_state.ipv6(str(reservation.ip_address.address.ip)) if reservation.ip_address_id else ""
    duid = reservation.duid or ""
    # In the aa-bb-cc form Windows reports; a DUID NetBox holds in another form is compared as it is.
    with contextlib.suppress(ValueError):
        duid = normalize.canonical_duid(duid) if duid else ""
    return Record(
        "reservation6",
        reservation6_key(prefix, ip) if ip else f"v6/{prefix}/",
        {
            "prefix": prefix,
            "ip_address": ip,
            "client_duid": duid,
            "iaid": reservation.iaid,
            "name": reservation.name or "",
            "description": reservation.description,
        },
    )


def _v6(windows_server):
    records = []
    # Only user classes are synced: Windows DHCPv6 vendor classes are left out on both sides.
    for c in WindowsClass.objects.filter(server=windows_server, family=6, class_type="user").order_by("pk"):
        records.append(
            Record(
                "class6",
                class6_key(c.name),
                {"name": c.name, "class_type": c.class_type, "data": c.data, "description": text_field(c.description)},
            )
        )
    scopes = _unique((_scope6(s), s.pk) for s in server_scopes6(windows_server))
    # Children of an extra copy point at its "#<pk>" key, as for IPv4, so a push never writes them into the original.
    prefixes = {pk: record.key.removeprefix("v6/") for record, pk in scopes}
    records.extend(record for record, _ in scopes)

    reservations = Reservation.objects.filter(scope__in=list(prefixes)).select_related("ip_address").order_by("pk")
    unique = _unique((_reservation6(r, prefixes[r.scope_id]), r.pk) for r in reservations)
    reservation_parents = {pk: record.key.removeprefix("v6/") for record, pk in unique}
    records.extend(record for record, _ in unique)
    records.extend(_options6(windows_server, prefixes, reservation_parents))
    return records


def _parent(value, scope_ids, reservation_parents):
    """The record parent of an option value ("server", a scope ID, or scope ID/IP), or None when it is not synced."""
    if value.reservation_id is not None:
        return reservation_parents.get(value.reservation_id)
    if value.scope_id is not None:
        return scope_ids.get(value.scope_id)
    return "server"


def _options6(windows_server, prefixes, reservation_parents):
    # Only values of standard DHCPv6 codes, the ones windows_state keeps: DHCPv6 option definitions are not synced,
    # so a value of another code is left alone on both sides. DHCPv6 vendor class values are not synced either.
    values = (
        OptionValue.objects.filter(
            server=windows_server, family=6, code__in=standard_codes6(), vendor_class__isnull=True
        )
        .select_related("user_class", "policy")
        .order_by("pk")
    )
    seen = set()
    for o in values:
        parent = _parent(o, prefixes, reservation_parents)
        if parent is None:
            continue  # on a scope of another family or server: not this server's DHCPv6 data
        user = o.user_class.name if o.user_class_id else ""
        key = windows_state.option6_key(parent, o.code, user)
        fields = {"parent": parent, "option_id": o.code, "value": list(o.value), "user_class": user}
        if o.policy_id:
            # Windows has no DHCPv6 policies: the push refuses this value rather than writing it unqualified.
            fields["policy"] = o.policy.name
        if key in seen:
            key = f"{key}#{o.pk}"
            fields[DUPLICATE] = o.pk
        seen.add(key)
        yield Record("option6", key, fields)


def _member_ids(keys):
    """
    Scope IDs for a membership list. An extra copy of a scope ("<scope ID>#<pk>") is not a scope Windows can hold, and
    a push deletes it, so it is left out.
    """
    return windows_state.scope_ids(k for k in keys if "#" not in k)


def _superscopes(windows_server, memberships):
    """``memberships``: (superscope pk, scope record key) for each IPv4 scope of the server."""
    members = {}
    for superscope_pk, key in memberships:
        if superscope_pk is not None:
            members.setdefault(superscope_pk, []).append(key)
    pairs = []
    for s in Superscope.objects.filter(server=windows_server).order_by("pk"):
        record = Record("superscope", s.name.lower(), {"name": s.name, "scope_ids": _member_ids(members.get(s.pk, []))})
        pairs.append((record, s.pk))
    return [record for record, _ in _unique(pairs)]


def _policies(windows_server, scope_ids):
    policies = WindowsPolicy.objects.filter(server=windows_server).order_by("pk")
    pairs = []
    for p in policies:
        if p.scope_id is not None and p.scope_id not in scope_ids:
            continue  # a DHCPv6 scope, or one of another server
        scope_id = scope_ids.get(p.scope_id, "")
        record = Record(
            "policy",
            policy_key(scope_id, p.name),
            {
                "name": p.name,
                "scope_id": scope_id,
                "description": text_field(p.description),
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
    names = FailoverRelationship.objects.filter(partner_server=windows_server).values_list("name", flat=True)
    return {n.lower() for n in names}


def partner_servers(windows_server):
    """
    Every other Windows server as ``windows_state.partner_matches`` takes them: the host Windows knows the DHCP server
    by (computer_name through a management host) and the names it reported for itself.
    """
    others = WindowsServer.objects.exclude(pk=windows_server.pk).order_by("pk")
    return [(ws.dhcp_host, ws.reported_names) for ws in others]


def _failovers(windows_server, scope_ids):
    relationships = (
        FailoverRelationship.objects.filter(primary_server=windows_server)
        .select_related("partner_server")
        .prefetch_related("scopes")
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
                # The name the partner is known by on Windows: the DHCP server itself, not a management host in front.
                "partner_server": windows_state.host_name(f.partner_server.dhcp_host),
                "mode": f.mode,
                "max_client_lead_time": f.max_client_lead_time,
                "load_balance_percent": None if hot else f.load_balance_percent,
                "reserve_percent": f.reserve_percent if hot else None,
                "server_role": f.server_role if hot else "",
                "state_switch_interval": f.state_switch_interval,
                # Windows only says whether a secret is set; the secret itself never leaves the credential profile.
                # Without a profile NetBox does not manage authentication (None): an imported relationship cannot
                # know its secret, and Windows refuses to turn authentication off anyway.
                "auth": True if f.shared_secret_profile else None,
                # Scopes of other servers cannot be in the relationship on this one; they are left out.
                "scope_ids": _member_ids(scope_ids[s.pk] for s in f.scopes.all() if s.pk in scope_ids),
            },
        )
        pairs.append((record, f.pk))
    return [record for record, _ in _unique(pairs)]


def _mac_filters(windows_server, for_import):
    pairs = []
    for m in MacFilter.objects.filter(server=windows_server).order_by("pk"):
        key = mac_key(m.mac_address)
        fields = {"mac_address": key, "list": m.list, "description": text_field(m.description)}
        pairs.append((Record("macfilter", key, fields), m.pk))
    records = [record for record, _ in _unique(pairs)]
    lists = MacFilterLists.objects.filter(server=windows_server).first()
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
    Make record keys unique. NetBox can hold what Windows cannot: two scopes on one network address (two prefix
    lengths, or two VRFs), two reservations for one address (in two VRFs). The extra ones get "#<pk>" appended, so they
    show up as deletions instead of making the import fail, and the writer finds exactly that object. They are also
    marked with a ``_duplicate`` field (the primary key), since a Windows name can itself end in "#<digits>".
    """
    seen = set()
    out = []
    for record, pk in pairs:
        key, fields = record.key, record.fields
        if key in seen or key.endswith("/"):
            key, fields = f"{key}#{pk}", {**fields, DUPLICATE: pk}
        seen.add(key)
        out.append((Record(record.kind, key, fields), pk))
    return out


def _options(windows_server, scope_ids, reservation_parents):
    values = (
        OptionValue.objects.filter(server=windows_server, family=4)
        .select_related("vendor_class", "user_class", "policy")
        .order_by("pk")
    )
    seen = set()
    for o in values:
        parent = _parent(o, scope_ids, reservation_parents)
        if parent is None:
            continue  # on a scope of another family or server: not this server's IPv4 data
        vendor = o.vendor_class.name if o.vendor_class_id else ""
        user = o.user_class.name if o.user_class_id else ""
        policy = o.policy.name if o.policy_id else ""
        yield _option_record(seen, o.pk, parent, o.code, list(o.value), vendor, user, policy)


def _option_record(seen, pk, parent, code, value, vendor, user, policy):
    key = option_key(parent, code, vendor, user, policy)
    fields = {"parent": parent, "option_id": code, "value": value, "vendor_class": vendor, "user_class": user}
    if policy:
        fields["policy"] = policy
    if key in seen:
        # Class and policy names are not case sensitive on Windows, but they are in NetBox: two values can end up on
        # one Windows key. Windows cannot hold that, so the extra copies show up as deletions.
        key = f"{key}#{pk}"
        fields[DUPLICATE] = pk
    seen.add(key)
    return Record("option", key, fields)
