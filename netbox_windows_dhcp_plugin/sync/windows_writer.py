"""
Apply one sync change to a Windows DHCP server: the push counterpart of ``mapping.Writer``.

Pure Python (no Django). Changes come from ``diff(netbox_records, windows_records)``, so their fields use the
records' spellings (lowercase enum values, lease time in seconds); this module turns them back into what the cmdlets
take. Every call goes through ``DhcpClient``, so values only ever reach PowerShell through the ``$p`` JSON literal.
"""

from __future__ import annotations

from typing import Any

from ..windows import normalize
from ..windows.client import DhcpClass, DhcpClient, ExclusionRange, OptionDefinition, OptionKey, Reservation, Scope
from .diff import CREATE, DELETE, UPDATE, Change
from .windows_state import NO_BOOTP_LIMIT

__all__ = ("PushRefused", "WindowsWriter", "option_target")


class PushRefused(Exception):
    """A change the push will not make on Windows, because it would destroy more than it says. Meant for the log."""


def option_target(parent: str) -> tuple[str, str]:
    """``server``, ``10.1.0.0`` or ``10.1.0.0/10.1.0.50`` -> (scope ID, reserved IP)."""
    if parent == "server":
        return "", ""
    scope_id, _, ip = parent.partition("/")
    return scope_id, ip


class WindowsWriter:
    def __init__(self, client: DhcpClient) -> None:
        self.client = client

    def apply(self, change: Change) -> None:
        handler = getattr(self, f"_apply_{change.kind}", None)
        if handler is None:
            raise PushRefused(f"{change.kind} {change.key}: pushing this kind of object is not supported yet")
        handler(change)

    # -- classes -------------------------------------------------------------------------------------------------

    def _apply_class(self, change: Change) -> None:
        f = change.after or change.before or {}
        if change.action == DELETE:
            self.client.remove_class(f["name"], f["class_type"])
            return
        c = DhcpClass(name=f["name"], type=f["class_type"], data=f["data"], description=f["description"])
        if change.action == CREATE:
            self.client.add_class(c)
        else:
            self.client.set_class(c)

    # -- option definitions --------------------------------------------------------------------------------------

    def _apply_optiondef(self, change: Change) -> None:
        if change.action == DELETE:
            f = change.before or {}
            self.client.remove_option_definition(f["option_id"], f.get("vendor_class", ""))
            return
        f = change.after or {}
        if change.action == UPDATE:
            before = change.before or {}
            for name in ("type", "multi_valued"):
                if before.get(name) != f.get(name):
                    # Windows can only change these by removing the definition, which removes every value using it.
                    raise PushRefused(
                        f"option definition {change.key}: the {name.replace('_', ' ')} cannot change on Windows "
                        "without removing every value of the option; delete the definition and create it again"
                    )
            if before.get("default_value") and not f.get("default_value"):
                # Set-DhcpServerv4OptionDefinition cannot clear a default value, so it would be sent on every push.
                raise PushRefused(
                    f"option definition {change.key}: Windows cannot clear a default value; set another default, or "
                    "delete the definition and create it again"
                )
        d = OptionDefinition(
            option_id=f["option_id"],
            name=f["name"],
            type=f["type"],
            description=f["description"],
            vendor_class=f.get("vendor_class", ""),
            multi_valued=bool(f["multi_valued"]),
            default_value=list(f["default_value"]),
        )
        if change.action == CREATE:
            self.client.add_option_definition(d)
        else:
            self.client.set_option_definition(d)

    # -- scopes --------------------------------------------------------------------------------------------------

    def _scope(self, scope_id: str, f: dict[str, Any], current_lease: int | None, create: bool) -> Scope:
        lease = f["lease_seconds"]
        if lease is None:
            # NetBox has no lease time (Subnet.valid_lifetime is empty): keep what Windows has, or its 8 day default.
            lease = current_lease
        # Without one, a new scope gets Windows' default; an existing one whose lease could not be read (unlimited)
        # keeps it.
        default = Scope.lease_duration if create else ""
        duration = normalize.format_duration(lease) if lease else default
        return Scope(
            scope_id=scope_id,
            name=f["name"],
            description=f["description"],
            start_range=f["start_range"],
            end_range=f["end_range"],
            subnet_mask=f["subnet_mask"],
            state=normalize.enum_of(f["state"], *normalize.SCOPE_STATES),
            type=normalize.enum_of(f["scope_type"], *normalize.SCOPE_TYPES),
            lease_duration=duration,
            delay=f["delay_ms"],
            max_bootp_clients=NO_BOOTP_LIMIT if f["max_bootp_clients"] is None else f["max_bootp_clients"],
            activate_policies=f["activate_policies"],
        )

    def _apply_scope(self, change: Change) -> None:
        if change.action == DELETE:
            # Never forced: a scope that still has leases fails with DHCP 20007, and that error is reported.
            self.client.remove_scope(change.key)
            return
        f = change.after or {}
        if change.action == CREATE:
            self.client.add_scope(self._scope(change.key, f, None, create=True))
            return
        before = change.before or {}
        if before.get("subnet_mask") != f["subnet_mask"]:
            raise PushRefused(
                f"scope {change.key}: the subnet mask cannot change on Windows without removing the scope with its "
                "reservations and leases; delete the scope and create it again"
            )
        self.client.set_scope(self._scope(change.key, f, before.get("lease_seconds"), create=False))

    # -- exclusions ----------------------------------------------------------------------------------------------

    @staticmethod
    def _exclusion(f: dict[str, Any]) -> ExclusionRange:
        return ExclusionRange(scope_id=f["scope_id"], start_range=f["start_range"], end_range=f["end_range"])

    def _apply_exclusion(self, change: Change) -> None:
        # There is no Set cmdlet for exclusions: an update is a remove and an add.
        if change.action in (DELETE, UPDATE):
            self.client.remove_exclusion_range(self._exclusion(change.before or {}))
        if change.action in (CREATE, UPDATE):
            self.client.add_exclusion_range(self._exclusion(change.after or {}))

    # -- reservations --------------------------------------------------------------------------------------------

    def _apply_reservation(self, change: Change) -> None:
        if change.action == DELETE:
            # Reservation IPs are unique on a server, so Remove takes the IP alone.
            self.client.remove_reservation((change.before or {})["ip_address"])
            return
        f = change.after or {}
        r = Reservation(
            scope_id=f["scope_id"],
            ip_address=f["ip_address"],
            client_id=f["client_id"],
            # Windows names a reservation after its IP when given no name.
            name=f["name"] or f["ip_address"],
            description=f["description"],
            type=normalize.enum_of(f["reservation_type"], *normalize.SCOPE_TYPES),
        )
        if change.action == CREATE:
            self.client.add_reservation(r)
        else:
            self.client.set_reservation(r)

    # -- option values -------------------------------------------------------------------------------------------

    def _apply_option(self, change: Change) -> None:
        f = change.after or change.before or {}
        scope_id, reserved_ip = option_target(f["parent"])
        key = OptionKey(
            option_id=f["option_id"],
            scope_id=scope_id,
            reserved_ip=reserved_ip,
            vendor_class=f.get("vendor_class", ""),
            user_class=f.get("user_class", ""),
        )
        if change.action == DELETE:
            self.client.remove_option_value(key)
        else:
            # Set-DhcpServerv4OptionValue is an upsert: the same call creates and updates.
            self.client.set_option_value(key, f["value"])
