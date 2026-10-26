"""
Read a Windows DHCP server through the client and turn the result into sync records.

Pure Python (no Django). Field values use the plugin's own spellings (lowercase enum values, lease time in seconds,
client IDs as aa-bb-cc-...), so the NetBox side can produce the very same records from its models.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..windows import normalize
from ..windows.client import (
    DhcpClass,
    DhcpClient,
    ExclusionRange,
    ExclusionRangeV6,
    FailoverRelationship,
    FilterListState,
    MacFilter,
    OptionDefinition,
    OptionValue,
    OptionValueV6,
    Policy,
    PolicyIPRange,
    Reservation,
    ReservationV6,
    Scope,
    ScopeV6,
    Superscope,
)
from ..windows.psscript import NotFound
from .diff import Record

__all__ = (
    "NAME_FIELDS",
    "SKIPPED_OPTION_IDS",
    "OptionValue",
    "WindowsState",
    "canonical_criteria",
    "class6_key",
    "class_key",
    "equal",
    "hidden_records",
    "join_values",
    "mac_key",
    "option6_key",
    "optiondef_key",
    "option_key",
    "policy_key",
    "read",
    "reservation6_key",
    "same_host",
    "scope6_key",
    "split_values",
    "to_records",
)

# Windows keeps the subnet mask (1) and the lease time (51) as options too. NetBox holds them on the prefix and on
# Subnet.valid_lifetime, so importing them as options would store the same setting twice.
SKIPPED_OPTION_IDS = frozenset({1, 51})

# PrimaryModel.description holds 200 characters.
DESCRIPTION_LENGTH = 200

# Reasons a Windows object is not imported, as they appear in the job log.
UNSTORABLE_VALUES = (
    "option value(s) starting or ending with a space, or ending with a backslash: netbox-plugin-dhcp stores option "
    "values as one comma-separated string, which cannot keep them"
)
PARTNER_FAILOVERS = "failover relationship(s) whose primary is another server: they are synced from that server"
V6_VENDOR_CLASSES = "DHCPv6 vendor class(es): the plugin syncs DHCPv6 user classes only"
V6_VENDOR_VALUES = "DHCPv6 vendor class option value(s): the plugin syncs DHCPv6 standard and user class values only"
V6_UNDEFINED = (
    "DHCPv6 option value(s) of codes netbox-plugin-dhcp has no standard definition for: DHCPv6 custom option "
    "definitions are not synced"
)
V6_EXCLUSIONS = "DHCPv6 exclusion range(s): they are not synced, and a push leaves them alone"

# Windows DHCPv6 scopes are always /64.
V6_PREFIX_LENGTH = 64

# Windows reports "no BOOTP client limit" as 2^32-1, which does not fit a database integer field. NetBox stores it as
# empty instead.
NO_BOOTP_LIMIT = 0xFFFFFFFF


@dataclass
class WindowsState:
    classes: list[DhcpClass] = field(default_factory=list)
    option_definitions: list[OptionDefinition] = field(default_factory=list)
    scopes: list[Scope] = field(default_factory=list)
    exclusions: list[ExclusionRange] = field(default_factory=list)
    reservations: list[Reservation] = field(default_factory=list)
    # Every option value, with scope_id and reserved_ip filled in for scope and reservation values.
    options: list[OptionValue] = field(default_factory=list)
    policies: list[Policy] = field(default_factory=list)
    policy_ranges: list[PolicyIPRange] = field(default_factory=list)
    superscopes: list[Superscope] = field(default_factory=list)
    failovers: list[FailoverRelationship] = field(default_factory=list)
    filters: list[MacFilter] = field(default_factory=list)
    filter_lists: FilterListState | None = None
    v6_classes: list[DhcpClass] = field(default_factory=list)
    v6_scopes: list[ScopeV6] = field(default_factory=list)
    v6_exclusions: list[ExclusionRangeV6] = field(default_factory=list)
    v6_reservations: list[ReservationV6] = field(default_factory=list)
    # Every DHCPv6 option value, with prefix and reserved_ip filled in for scope and reservation values.
    v6_options: list[OptionValueV6] = field(default_factory=list)


def _scope_options(client: DhcpClient, scope_id: str, reserved_ip: str = "", policy_name: str = "") -> list:
    out = client.list_option_values(scope_id=scope_id, reserved_ip=reserved_ip, policy_name=policy_name, all=True)
    for o in out:
        o.scope_id, o.reserved_ip = scope_id, reserved_ip
    return out


def read(client: DhcpClient) -> WindowsState:
    """Read everything the sync handles. One PowerShell call per list, plus a few per scope, policy and reservation."""
    state = WindowsState(
        classes=client.list_classes(),
        option_definitions=client.list_option_definitions(),
        scopes=client.list_scopes(),
        superscopes=client.list_superscopes(),
        failovers=client.list_failovers(),
        filters=client.list_filters(),
        filter_lists=client.get_filter_lists(),
    )
    # -All returns the values of every user and vendor class at each level (lab, Windows Server 2025).
    state.options.extend(client.list_option_values(all=True))
    for policy in client.list_policies():
        state.policies.append(policy)
        state.options.extend(_scope_options(client, "", policy_name=policy.name))
    # A scope can disappear while it is read, for example when a failover relationship is removed on its partner:
    # its reads then fail with not found. It is gone, as if it had not been listed; the rest of the read stands.
    state.scopes = [scope for scope in state.scopes if _read_scope(client, state, scope.scope_id)]
    _read_v6(client, state)
    return state


def _read_scope(client: DhcpClient, state: WindowsState, sid: str) -> bool:
    """Add what one scope holds to ``state``; False, and nothing added, when the scope no longer exists."""
    part = WindowsState()
    try:
        part.exclusions = client.list_exclusion_ranges(sid)
        part.reservations = client.list_reservations(sid)
        part.options = _scope_options(client, sid)
        for r in part.reservations:
            part.options.extend(_scope_options(client, sid, r.ip_address))
        for policy in client.list_policies(scope_id=sid):
            policy.scope_id = sid
            part.policies.append(policy)
            part.policy_ranges.extend(client.list_policy_ip_ranges(sid, policy.name))
            part.options.extend(_scope_options(client, sid, policy_name=policy.name))
    except NotFound:
        return False
    state.exclusions.extend(part.exclusions)
    state.reservations.extend(part.reservations)
    state.options.extend(part.options)
    state.policies.extend(part.policies)
    state.policy_ranges.extend(part.policy_ranges)
    return True


def _read_v6(client: DhcpClient, state: WindowsState) -> None:
    state.v6_classes = client.list_v6_classes()
    state.v6_scopes = client.list_v6_scopes()
    # -All returns the values of every user class at each level, as for IPv4 (lab, Windows Server 2025).
    state.v6_options.extend(client.list_v6_option_values(all=True))
    state.v6_scopes = [scope for scope in state.v6_scopes if _read_v6_scope(client, state, scope.prefix)]


def _read_v6_scope(client: DhcpClient, state: WindowsState, prefix: str) -> bool:
    """As _read_scope, for a DHCPv6 scope."""
    try:
        exclusions = client.list_v6_exclusion_ranges(prefix)
        reservations = client.list_v6_reservations(prefix)
        options = _v6_options(client, prefix)
        for r in reservations:
            options.extend(_v6_options(client, prefix, r.ip_address))
    except NotFound:
        return False
    state.v6_exclusions.extend(exclusions)
    state.v6_reservations.extend(reservations)
    state.v6_options.extend(options)
    return True


def _v6_options(client: DhcpClient, prefix: str, reserved_ip: str = "") -> list:
    out = client.list_v6_option_values(prefix=prefix, reserved_ip=reserved_ip, all=True)
    for o in out:
        o.prefix, o.reserved_ip = prefix, reserved_ip
    return out


# -- keys ----------------------------------------------------------------------------------------------------------
# Windows names (classes, policies, superscopes, failover relationships) are not case sensitive, so neither are the
# keys naming them.


def option_key(parent: str, option_id: int, vendor_class: str = "", user_class: str = "", policy: str = "") -> str:
    """``server/6``, ``10.1.0.0/3``, ``10.1.0.0/10.1.0.50/15;vendor=msft 5.0``, ``10.1.0.0/6;policy=printers``."""
    key = f"{parent}/{option_id}"
    if vendor_class:
        key += f";vendor={vendor_class.lower()}"
    if user_class:
        key += f";user={user_class.lower()}"
    if policy:
        key += f";policy={policy.lower()}"
    return key


def optiondef_key(option_id: int, vendor_class: str = "") -> str:
    """``200``, or ``msft 5.0/1`` for a vendor class definition."""
    return f"{vendor_class.lower()}/{option_id}" if vendor_class else str(option_id)


def class_key(class_type: str, name: str) -> str:
    """``user/phones``."""
    return f"{class_type.lower()}/{name.lower()}"


def policy_key(scope_id: str, name: str) -> str:
    """``server/printers`` or ``10.1.0.0/printers``."""
    return f"{scope_id or 'server'}/{name.lower()}"


def host_name(value: str) -> str:
    """A host name as both sides spell it: lowercase, without the trailing dot of a fully qualified name."""
    return value.strip().rstrip(".").lower()


def same_host(a: str, b: str) -> bool:
    """
    Whether two names point at the same failover partner. Windows reports the partner as it was given when the
    relationship was created, which may be the short name where NetBox holds the fully qualified one.
    """
    a, b = host_name(a), host_name(b)
    if a == b:
        return True
    if not a or not b or ("." in a) == ("." in b):
        return False
    return a.split(".", 1)[0] == b.split(".", 1)[0]


def ipv6(value: str) -> str:
    """An IPv6 address in one spelling (lowercase, compressed), whatever form either side wrote it in."""
    try:
        return str(ipaddress.IPv6Address(value.strip()))
    except ValueError:
        return value


def scope6_key(prefix: str) -> str:
    """``v6/2001:db8:1::``. The ``v6/`` start keeps DHCPv6 keys apart from IPv4 ones in every message."""
    return f"v6/{ipv6(prefix)}"


def reservation6_key(prefix: str, ip_address: str) -> str:
    """``v6/2001:db8:1::/2001:db8:1::10``."""
    return f"v6/{ipv6(prefix)}/{ipv6(ip_address)}"


def class6_key(name: str) -> str:
    """``v6/user/lab``: only DHCPv6 user classes are synced."""
    return f"v6/user/{name.lower()}"


def option6_key(parent: str, option_id: int, user_class: str = "") -> str:
    """``v6/server/23``, ``v6/2001:db8:1::/24``, ``v6/2001:db8:1::/2001:db8:1::10/23;user=lab``."""
    return option_key(f"v6/{parent}", option_id, user_class=user_class)


def option6_target(parent: str) -> tuple[str, str]:
    """``server``, ``2001:db8:1::`` or ``2001:db8:1::/2001:db8:1::10`` -> (prefix, reserved IP)."""
    if parent == "server":
        return "", ""
    prefix, _, ip = parent.partition("/")
    return prefix, ip


def mac_key(mac: str) -> str:
    """A MAC filter pattern as Windows matches it: ``00-15-5d-*``."""
    return mac.strip().lower().replace(":", "-")


# -- option values in netbox-plugin-dhcp -----------------------------------------------------------------------------


def join_values(values: Iterable[str]) -> str:
    """
    netbox-plugin-dhcp keeps a list of option values as one comma-separated string, like Kea's option data. A comma
    inside a value is written ``\\,``, Kea's own escape, so it survives the round trip. Other backslashes stay as they
    are (boot file paths have them), which also keeps strings stored before commas were escaped readable.
    """
    return ", ".join(v.replace(",", "\\,") for v in values)


def split_values(data: str) -> list[str]:
    """The reverse of ``join_values``: split on commas not preceded by a backslash, and trim spaces around values."""
    if not data.strip():
        return []
    out, current, i = [], [], 0
    while i < len(data):
        if data[i] == "\\" and data[i + 1 : i + 2] == ",":
            current.append(",")
            i += 2
            continue
        if data[i] == ",":
            out.append("".join(current).strip())
            current = []
        else:
            current.append(data[i])
        i += 1
    out.append("".join(current).strip())
    return out


def _storable(values: list[str]) -> bool:
    """
    A value with spaces around it would come back trimmed, and one ending in a backslash would swallow the comma
    after it, so either would be proposed again on every import.
    """
    return all(v == v.strip() and not v.endswith("\\") for v in values)


# -- records -------------------------------------------------------------------------------------------------------


def _lease_seconds(value: str) -> int | None:
    if not value:
        return None
    try:
        return normalize.parse_duration(value)
    except ValueError:
        # An unlimited lease reads back as TimeSpan.MaxValue (10675199.02:48:05.4775807), whether it was set as that or
        # as
        # 0xFFFFFFFF seconds (lab, Windows Server 2025); parse_duration refuses it, so no lease time is recorded.
        return None


def _scope(s: Scope) -> Record:
    return Record(
        "scope",
        s.scope_id,
        {
            "name": _text(s.name),
            "description": s.description or "",
            "start_range": s.start_range,
            "end_range": s.end_range,
            "subnet_mask": s.subnet_mask,
            "state": s.state.lower(),
            "scope_type": s.type.lower(),
            "lease_seconds": _lease_seconds(s.lease_duration),
            "delay_ms": s.delay or 0,
            "max_bootp_clients": None if s.max_bootp_clients == NO_BOOTP_LIMIT else s.max_bootp_clients,
            "activate_policies": True if s.activate_policies is None else bool(s.activate_policies),
        },
    )


def _client_id(value: str) -> str:
    try:
        return normalize.canonical_mac(value)
    except ValueError:
        return value


def _text(value: str | None) -> str:
    """Text that NetBox keeps in a 200-character description field, cut the same way on both sides."""
    return (value or "")[:DESCRIPTION_LENGTH]


def _ip_sort(pair: list[str]) -> tuple:
    return tuple(int(ipaddress.ip_address(a)) for a in pair)


def ip_ranges(pairs: Iterable[Iterable[str]]) -> list[list[str]]:
    """Policy IP ranges in one order on both sides."""
    return sorted(([str(a) for a in pair] for pair in pairs), key=_ip_sort)


def scope_ids(ids: Iterable[str]) -> list[str]:
    """Scope IDs in address order, so membership lists compare equal whatever order each side lists them in."""
    return sorted({str(i) for i in ids}, key=lambda i: int(ipaddress.IPv4Address(i)))


def criteria(value: dict[str, list[str]]) -> dict[str, list[str]]:
    """Policy criteria with the operator (EQ or NE) in capitals, as Windows prints it."""
    out = {}
    for name, items in value.items():
        items = [str(v) for v in items]
        if items:
            out[name] = [items[0].upper(), *items[1:]]
    return out


def canonical_criteria(value: dict[str, list[str]]) -> dict[str, list[str]]:
    """
    Policy criteria as Windows stores them, to compare and fingerprint (never to send). Windows rewrites a MAC prefix
    ("02-4e-42-ab*" reads back "02-4e-42-ab-*") and lowercases a host name, so the spelling typed in NetBox and the one
    read from Windows must not look like a change on every push.
    """
    out = {}
    for name, items in criteria(value).items():
        out[name] = [items[0], *(normalize.canonical_policy_criterion(name, v) for v in items[1:])]
    return out


def policy_record(p: Policy, ranges: list[list[str]]) -> Record:
    return Record(
        "policy",
        policy_key(p.scope_id, p.name),
        {
            "name": p.name,
            "scope_id": p.scope_id,
            "description": _text(p.description),
            "enabled": bool(p.enabled),
            "processing_order": p.processing_order,
            "condition": (p.condition or "OR").lower(),
            "criteria": criteria(p.criteria),
            "lease_seconds": _lease_seconds(p.lease_duration),
            "ip_ranges": ip_ranges(ranges),
        },
    )


def failover_record(f: FailoverRelationship) -> Record:
    hot = f.mode.lower() == "hotstandby"
    return Record(
        "failover",
        f.name.lower(),
        {
            "name": f.name,
            "partner_server": host_name(f.partner_server),
            "mode": f.mode.lower(),
            "max_client_lead_time": f.max_client_lead_time,
            "load_balance_percent": None if hot else f.load_balance_percent,
            "reserve_percent": f.reserve_percent if hot else None,
            "server_role": f.server_role.lower() if hot else "",
            # Without automatic state transition, Windows still reports an interval it does not use.
            "state_switch_interval": f.state_switch_interval if f.auto_state_transition else None,
            "auth": bool(f.enable_auth),
            "scope_ids": scope_ids(f.scope_ids),
        },
    )


def _option_parent(o: OptionValue) -> str:
    return f"{o.scope_id}/{o.reserved_ip}" if o.reserved_ip else o.scope_id or "server"


def option_record(o: OptionValue) -> Record:
    parent = _option_parent(o)
    fields: dict[str, Any] = {
        "parent": parent,
        "option_id": o.option_id,
        "value": [str(v) for v in o.value],
        "vendor_class": o.vendor_class,
        "user_class": o.user_class,
    }
    if o.policy_name:
        # Only on policy values, so the records (and fingerprints) of every other value stay as they were.
        fields["policy"] = o.policy_name
    return Record("option", option_key(parent, o.option_id, o.vendor_class, o.user_class, o.policy_name), fields)


def scope6_record(s: ScopeV6) -> Record:
    prefix = ipv6(s.prefix)
    return Record(
        "scope6",
        scope6_key(prefix),
        {
            "prefix": prefix,
            "prefix_length": V6_PREFIX_LENGTH,
            "name": _text(s.name),
            "description": s.description or "",
            "state": s.state.lower(),
            "preference": int(s.preference or 0),
            "preferred_lifetime": _lease_seconds(s.preferred_lifetime),
            "valid_lifetime": _lease_seconds(s.valid_lifetime),
            # Zero: the server works the renewal and rebinding times out itself, which NetBox writes as empty.
            "t1": _lease_seconds(s.t1) or None,
            "t2": _lease_seconds(s.t2) or None,
        },
    )


def reservation6_record(r: ReservationV6) -> Record:
    prefix, ip = ipv6(r.prefix), ipv6(r.ip_address)
    return Record(
        "reservation6",
        reservation6_key(prefix, ip),
        {
            "prefix": prefix,
            "ip_address": ip,
            "client_duid": _client_id(r.client_duid),
            "iaid": int(r.iaid),
            "name": _text(r.name),
            "description": r.description or "",
        },
    )


def _option6_parent(o: OptionValueV6) -> str:
    prefix = ipv6(o.prefix) if o.prefix else ""
    if o.reserved_ip:
        return f"{prefix}/{ipv6(o.reserved_ip)}"
    return prefix or "server"


def option6_record(o: OptionValueV6) -> Record:
    parent = _option6_parent(o)
    fields = {
        "parent": parent,
        "option_id": o.option_id,
        "value": [str(v) for v in o.value],
        "user_class": o.user_class,
    }
    if o.vendor_class:
        # Only on the hidden records of vendor class values, which a push never writes.
        fields["vendor_class"] = o.vendor_class
        key = option_key(f"v6/{parent}", o.option_id, vendor_class=o.vendor_class, user_class=o.user_class)
        return Record("option6", key, fields)
    return Record("option6", option6_key(parent, o.option_id, o.user_class), fields)


def exclusion6_record(e: ExclusionRangeV6) -> Record:
    prefix, start, end = ipv6(e.prefix), ipv6(e.start_range), ipv6(e.end_range)
    return Record(
        "exclusion6", f"v6/{prefix}/{start}-{end}", {"prefix": prefix, "start_range": start, "end_range": end}
    )


def _v6_option_skip(o: OptionValueV6, standard6: set[int] | None) -> str | None:
    """Why a DHCPv6 option value is not synced, or None."""
    if o.vendor_class:
        return V6_VENDOR_VALUES
    if standard6 is not None and o.option_id not in standard6:
        return V6_UNDEFINED
    if not _storable([str(v) for v in o.value]):
        return UNSTORABLE_VALUES
    return None


def _v6_records(state: WindowsState, standard6: set[int] | None, skip) -> list[Record]:
    records = []
    for c in state.v6_classes:
        if c.type.lower() != "user":
            skip(V6_VENDOR_CLASSES)
            continue
        records.append(
            Record(
                "class6",
                class6_key(c.name),
                {"name": c.name, "class_type": "user", "data": c.data, "description": _text(c.description)},
            )
        )
    records.extend(scope6_record(s) for s in state.v6_scopes)
    for _ in state.v6_exclusions:
        skip(V6_EXCLUSIONS)
    records.extend(reservation6_record(r) for r in state.v6_reservations)
    for o in state.v6_options:
        reason = _v6_option_skip(o, standard6)
        if reason is not None:
            skip(reason)
            continue
        records.append(option6_record(o))
    return records


def to_records(
    state: WindowsState,
    standard_codes: Iterable[int] = (),
    skipped_option_ids: Iterable[int] = SKIPPED_OPTION_IDS,
    partner_failovers: Iterable[str] = (),
    standard_codes6: Iterable[int] | None = None,
) -> tuple[list[Record], list[str]]:
    """
    Turn a WindowsState into records, and return notes about what was left out.

    ``standard_codes`` are the DHCPv4 option codes netbox-plugin-dhcp already defines: their Windows definitions are
    not imported (values still are, against the standard definition). ``partner_failovers`` are the (lowercase)
    names of failover relationships NetBox syncs from the other server. ``standard_codes6`` are the DHCPv6 codes
    netbox-plugin-dhcp defines: DHCPv6 definitions are not synced, so values of other codes are left out (None keeps
    them all).
    """
    standard = set(standard_codes)
    standard6 = None if standard_codes6 is None else set(standard_codes6)
    skipped = set(skipped_option_ids)
    partners = {n.lower() for n in partner_failovers}
    records: list[Record] = []
    skips: dict[str, int] = {}

    def skip(reason: str) -> None:
        skips[reason] = skips.get(reason, 0) + 1

    for c in state.classes:
        kind = c.type.lower()
        records.append(
            Record(
                "class",
                class_key(kind, c.name),
                {"name": c.name, "class_type": kind, "data": c.data, "description": _text(c.description)},
            )
        )

    for d in state.option_definitions:
        if not d.vendor_class and (d.option_id in standard or d.option_id in skipped):
            continue
        records.append(
            Record(
                "optiondef",
                optiondef_key(d.option_id, d.vendor_class),
                {
                    "option_id": d.option_id,
                    "name": d.name,
                    "type": d.type,
                    "multi_valued": bool(d.multi_valued),
                    "default_value": list(d.default_value),
                    "description": _text(d.description),
                    "vendor_class": d.vendor_class,
                },
            )
        )

    records.extend(_scope(s) for s in state.scopes)

    for s in state.superscopes:
        records.append(
            Record("superscope", s.name.lower(), {"name": s.name, "scope_ids": scope_ids(s.scope_ids)}),
        )

    for e in state.exclusions:
        records.append(
            Record(
                "exclusion",
                f"{e.scope_id}/{e.start_range}-{e.end_range}",
                {"scope_id": e.scope_id, "start_range": e.start_range, "end_range": e.end_range},
            )
        )

    for r in state.reservations:
        records.append(
            Record(
                "reservation",
                f"{r.scope_id}/{r.ip_address}",
                {
                    "scope_id": r.scope_id,
                    "ip_address": r.ip_address,
                    "client_id": _client_id(r.client_id),
                    "name": _text(r.name),
                    "description": r.description or "",
                    "reservation_type": r.type.lower(),
                },
            )
        )

    ranges: dict[tuple[str, str], list[list[str]]] = {}
    for r in state.policy_ranges:
        ranges.setdefault((r.scope_id, r.policy_name.lower()), []).append([r.start_range, r.end_range])
    records.extend(policy_record(p, ranges.get((p.scope_id, p.name.lower()), [])) for p in state.policies)

    for o in state.options:
        if o.option_id in skipped and not o.vendor_class:
            continue
        # Vendor class values are stored as a list (WindowsVendorOption), not in netbox-plugin-dhcp's string.
        if not o.vendor_class and not _storable([str(v) for v in o.value]):
            skip(UNSTORABLE_VALUES)
            continue
        records.append(option_record(o))

    for f in state.failovers:
        if f.name.lower() in partners:
            skip(PARTNER_FAILOVERS)
            continue
        records.append(failover_record(f))

    for m in state.filters:
        records.append(
            Record(
                "macfilter",
                mac_key(m.mac_address),
                {"mac_address": mac_key(m.mac_address), "list": m.list.lower(), "description": _text(m.description)},
            )
        )

    if (lists := state.filter_lists) is not None:
        records.append(Record("filterlists", "server", {"allow": bool(lists.allow), "deny": bool(lists.deny)}))

    records.extend(_v6_records(state, standard6, skip))

    notes = [f"skipped {count} {reason}" for reason, count in skips.items()]
    return records, notes


def _number(value: str) -> int | None:
    try:
        return normalize.parse_option_number(value)
    except ValueError:
        return None


# Fields holding a Windows name, compared without case like Windows does.
NAME_FIELDS = frozenset(
    {
        ("class", "name"),
        ("optiondef", "vendor_class"),
        ("option", "user_class"),
        ("option", "vendor_class"),
        ("option", "policy"),
        ("policy", "name"),
        ("superscope", "name"),
        ("failover", "name"),
        ("class6", "name"),
        ("option6", "user_class"),
    }
)


def equal(kind: str, name: str, a: Any, b: Any) -> bool:
    """
    Field comparison for ``diff``. Option values match when they are equal, or the same number written in hex or
    decimal (Windows prints 0x0A, NetBox may hold 10). Windows names match without case, as on Windows. Policy
    conditions match as Windows stores them (``canonical_criteria``). Other text is compared exactly: a changed boot
    file name is a change.
    """
    if (kind, name) in NAME_FIELDS and isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if (kind, name) == ("failover", "partner_server") and isinstance(a, str) and isinstance(b, str):
        return same_host(a, b)
    if (kind, name) == ("policy", "criteria") and isinstance(a, dict) and isinstance(b, dict):
        return canonical_criteria(a) == canonical_criteria(b)
    if name in ("value", "default_value") and isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_same_value(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def _same_value(x: Any, y: Any) -> bool:
    if x == y:
        return True
    if (n := _number(x)) is not None and n == _number(y):
        return True
    # DHCPv6 addresses: Windows prints them compressed and lowercase, NetBox keeps what was typed.
    x, y = str(x), str(y)
    return _is_ipv6(x) and _is_ipv6(y) and ipv6(x) == ipv6(y)


def _is_ipv6(value: str) -> bool:
    try:
        ipaddress.IPv6Address(value.strip())
    except ValueError:
        return False
    return True


def hidden_records(
    state: WindowsState,
    skipped_option_ids: Iterable[int] = SKIPPED_OPTION_IDS,
    partner_failovers: Iterable[str] = (),
    *,
    standard_codes6: Iterable[int] | None = None,
) -> list[Record]:
    """
    Windows objects ``to_records`` leaves out (values with surrounding spaces, relationships synced from the partner),
    as records whose parents can be worked out. A push never touches them, but removing their parent on Windows
    would remove them too, so they stop the parent from being deleted. Options 1 and 51 are not here: every scope has
    them, and they mean nothing without it.
    """
    skipped = set(skipped_option_ids)
    partners = {n.lower() for n in partner_failovers}
    out = []
    for o in state.options:
        if o.vendor_class or o.option_id in skipped or _storable([str(v) for v in o.value]):
            continue
        out.append(option_record(o))
    out.extend(failover_record(f) for f in state.failovers if f.name.lower() in partners)
    standard6 = None if standard_codes6 is None else set(standard_codes6)
    out.extend(option6_record(o) for o in state.v6_options if _v6_option_skip(o, standard6))
    out.extend(exclusion6_record(e) for e in state.v6_exclusions)
    return out
