"""
Read a Windows DHCP server through the client and turn the result into sync records.

Pure Python (no Django). Field values use the plugin's own spellings (lowercase enum values, lease time in seconds,
client IDs as aa-bb-cc-...), so the NetBox side can produce the very same records from its models.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..windows import normalize
from ..windows.client import (
    DhcpClass,
    DhcpClient,
    ExclusionRange,
    OptionDefinition,
    OptionValue,
    Reservation,
    Scope,
)
from .diff import Record

__all__ = ("SKIPPED_OPTION_IDS", "OptionValue", "WindowsState", "equal", "option_key", "read", "to_records")

# Windows keeps the subnet mask (1) and the lease time (51) as options too. NetBox holds them on the prefix and on
# Subnet.valid_lifetime, so importing them as options would store the same setting twice.
SKIPPED_OPTION_IDS = frozenset({1, 51})

# PrimaryModel.description holds 200 characters.
DESCRIPTION_LENGTH = 200

# Reasons a Windows object is not imported (yet), as they appear in the job log.
POLICY_VALUES = "policy option value(s): policies come in a later release"
VENDOR_DEFINITIONS = (
    "vendor class option definition(s): netbox-plugin-dhcp allows one definition per option code and server, "
    "so they come in a later release"
)
VENDOR_VALUES = "vendor class option value(s): they need vendor class option definitions, which come in a later release"
UNSTORABLE_VALUES = (
    "option value(s) containing a comma or surrounding spaces: netbox-plugin-dhcp stores option values as one "
    "comma-separated string"
)

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


def read(client: DhcpClient) -> WindowsState:
    """Read everything import handles. One PowerShell call per list, plus one per scope and per reservation."""
    state = WindowsState(
        classes=client.list_classes(),
        option_definitions=client.list_option_definitions(),
        scopes=client.list_scopes(),
    )
    # unverified: -All is expected to return the values of every user and vendor class at each level.
    state.options.extend(client.list_option_values(all=True))
    for scope in state.scopes:
        state.exclusions.extend(client.list_exclusion_ranges(scope.scope_id))
        reservations = client.list_reservations(scope.scope_id)
        state.reservations.extend(reservations)
        for o in client.list_option_values(scope_id=scope.scope_id, all=True):
            o.scope_id, o.reserved_ip = scope.scope_id, ""
            state.options.append(o)
        for r in reservations:
            for o in client.list_option_values(scope_id=scope.scope_id, reserved_ip=r.ip_address, all=True):
                o.scope_id, o.reserved_ip = scope.scope_id, r.ip_address
                state.options.append(o)
    return state


def option_key(parent: str, option_id: int, vendor_class: str = "", user_class: str = "") -> str:
    """``server/6``, ``10.1.0.0/3``, ``10.1.0.0/10.1.0.50/15;vendor=MSFT 5.0``."""
    key = f"{parent}/{option_id}"
    if vendor_class:
        key += f";vendor={vendor_class}"
    if user_class:
        key += f";user={user_class}"
    return key


def _lease_seconds(value: str) -> int | None:
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


def _storable(values: list[str]) -> bool:
    """
    netbox-plugin-dhcp keeps a list of option values as one comma-separated string, so a value that contains a comma
    or starts or ends with a space would come back different and be proposed again on every import.
    """
    return all("," not in v and v == v.strip() for v in values)


def to_records(
    state: WindowsState,
    standard_codes: Iterable[int] = (),
    skipped_option_ids: Iterable[int] = SKIPPED_OPTION_IDS,
) -> tuple[list[Record], list[str]]:
    """
    Turn a WindowsState into records, and return notes about what was left out.

    ``standard_codes`` are the DHCPv4 option codes netbox-plugin-dhcp already defines: their Windows definitions are
    not imported (values still are, against the standard definition).
    """
    standard = set(standard_codes)
    skipped = set(skipped_option_ids)
    records: list[Record] = []
    skips: dict[str, int] = {}

    def skip(reason: str) -> None:
        skips[reason] = skips.get(reason, 0) + 1

    for c in state.classes:
        kind = c.type.lower()
        records.append(
            Record(
                "class",
                f"{kind}/{c.name}",
                {"name": c.name, "class_type": kind, "data": c.data, "description": _text(c.description)},
            )
        )

    for d in state.option_definitions:
        if d.vendor_class:
            skip(VENDOR_DEFINITIONS)
            continue
        if d.option_id in standard or d.option_id in skipped:
            continue
        records.append(
            Record(
                "optiondef",
                str(d.option_id),
                {
                    "option_id": d.option_id,
                    "name": d.name,
                    "type": d.type,
                    "multi_valued": bool(d.multi_valued),
                    "default_value": list(d.default_value),
                    "description": _text(d.description),
                    "vendor_class": "",
                },
            )
        )

    records.extend(_scope(s) for s in state.scopes)

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

    for o in state.options:
        values = [str(v) for v in o.value]
        if o.policy_name:
            skip(POLICY_VALUES)
            continue
        if o.vendor_class:
            skip(VENDOR_VALUES)
            continue
        if o.option_id in skipped:
            continue
        if not _storable(values):
            skip(UNSTORABLE_VALUES)
            continue
        parent = f"{o.scope_id}/{o.reserved_ip}" if o.reserved_ip else o.scope_id or "server"
        records.append(
            Record(
                "option",
                option_key(parent, o.option_id, "", o.user_class),
                {
                    "parent": parent,
                    "option_id": o.option_id,
                    "value": values,
                    "vendor_class": "",
                    "user_class": o.user_class,
                },
            )
        )
    notes = [f"skipped {count} {reason}" for reason, count in skips.items()]
    return records, notes


def _number(value: str) -> int | None:
    try:
        return normalize.parse_option_number(value)
    except ValueError:
        return None


def equal(kind: str, name: str, a: Any, b: Any) -> bool:
    """
    Field comparison for ``diff``. Option values match when they are equal, or the same number written in hex or
    decimal (Windows prints 0x0A, NetBox may hold 10). Text is compared exactly: a changed boot file name is a change.
    """
    if name in ("value", "default_value") and isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(x == y or (_number(x) is not None and _number(x) == _number(y)) for x, y in zip(a, b, strict=True))
    return a == b
