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
    FailoverRelationship,
    FilterListState,
    MacFilter,
    OptionDefinition,
    OptionValue,
    Policy,
    PolicyIPRange,
    Reservation,
    Scope,
    Superscope,
)
from .diff import Record

__all__ = (
    "NAME_FIELDS",
    "SKIPPED_OPTION_IDS",
    "OptionValue",
    "WindowsState",
    "class_key",
    "equal",
    "hidden_records",
    "join_values",
    "mac_key",
    "optiondef_key",
    "option_key",
    "policy_key",
    "read",
    "same_host",
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
    # unverified: -All is expected to return the values of every user and vendor class at each level.
    state.options.extend(client.list_option_values(all=True))
    for policy in client.list_policies():
        state.policies.append(policy)
        state.options.extend(_scope_options(client, "", policy_name=policy.name))
    for scope in state.scopes:
        sid = scope.scope_id
        state.exclusions.extend(client.list_exclusion_ranges(sid))
        reservations = client.list_reservations(sid)
        state.reservations.extend(reservations)
        state.options.extend(_scope_options(client, sid))
        for r in reservations:
            state.options.extend(_scope_options(client, sid, r.ip_address))
        for policy in client.list_policies(scope_id=sid):
            policy.scope_id = sid
            state.policies.append(policy)
            state.policy_ranges.extend(client.list_policy_ip_ranges(sid, policy.name))
            state.options.extend(_scope_options(client, sid, policy_name=policy.name))
    return state


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
        # unverified: an unlimited lease is reported as a value parse_duration does not accept.
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


def to_records(
    state: WindowsState,
    standard_codes: Iterable[int] = (),
    skipped_option_ids: Iterable[int] = SKIPPED_OPTION_IDS,
    partner_failovers: Iterable[str] = (),
) -> tuple[list[Record], list[str]]:
    """
    Turn a WindowsState into records, and return notes about what was left out.

    ``standard_codes`` are the DHCPv4 option codes netbox-plugin-dhcp already defines: their Windows definitions are
    not imported (values still are, against the standard definition). ``partner_failovers`` are the (lowercase)
    names of failover relationships NetBox syncs from the other server.
    """
    standard = set(standard_codes)
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
    }
)


def equal(kind: str, name: str, a: Any, b: Any) -> bool:
    """
    Field comparison for ``diff``. Option values match when they are equal, or the same number written in hex or
    decimal (Windows prints 0x0A, NetBox may hold 10). Windows names match without case, as on Windows. Other text is
    compared exactly: a changed boot file name is a change.
    """
    if (kind, name) in NAME_FIELDS and isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if (kind, name) == ("failover", "partner_server") and isinstance(a, str) and isinstance(b, str):
        return same_host(a, b)
    if name in ("value", "default_value") and isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(x == y or (_number(x) is not None and _number(x) == _number(y)) for x, y in zip(a, b, strict=True))
    return a == b


def hidden_records(
    state: WindowsState,
    skipped_option_ids: Iterable[int] = SKIPPED_OPTION_IDS,
    partner_failovers: Iterable[str] = (),
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
    return out
