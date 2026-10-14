"""
Apply one sync change to a Windows DHCP server: the push counterpart of ``mapping.Writer``.

Pure Python (no Django). Changes come from ``diff(netbox_records, windows_records)``, so their fields use the
records' spellings (lowercase enum values, lease time in seconds); this module turns them back into what the cmdlets
take. Every call goes through ``DhcpClient``, so values only ever reach PowerShell through the ``$p`` JSON literal.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..windows import normalize
from ..windows.client import (
    DhcpClass,
    DhcpClient,
    ExclusionRange,
    FailoverRelationship,
    MacFilter,
    OptionDefinition,
    OptionKey,
    OptionKeyV6,
    Policy,
    PolicyIPRange,
    Reservation,
    ReservationV6,
    Scope,
    ScopeV6,
    Superscope,
)
from .diff import CREATE, DELETE, UPDATE, Change
from .windows_state import NO_BOOTP_LIMIT, option6_target, same_host

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
    def __init__(self, client: DhcpClient, failover_secret: Callable[[str], str] | None = None) -> None:
        self.client = client
        # Returns the shared secret of a failover relationship (by name) from its credential profile. The secret
        # only ever goes from there to the cmdlet: it is never part of a change, a record or a log line.
        self.failover_secret = failover_secret or (lambda name: "")

    def scope_has_leases(self, scope_id: str) -> bool:
        return bool(self.client.list_leases(scope_id))

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
            # A scope leaves its failover relationship first. The push plan only gets here when NetBox manages that
            # relationship and no longer lists the scope in it.
            # unverified: whether Remove-DhcpServerv4Scope refuses a scope that is still in a relationship.
            relationships = [rel.name for rel in self.client.list_failovers() if change.key in rel.scope_ids]
            if relationships and self.client.list_leases(change.key):
                # Leaving the relationship removes the scope from the partner, and Windows would then refuse to
                # delete it here (DHCP 20007): check first, so a refused delete changes nothing.
                raise PushRefused(
                    f"scope {change.key}: it still has leases, so Windows would refuse to delete it (DHCP 20007); it "
                    f"stays in failover relationship {relationships[0]}"
                )
            for name in relationships:
                self.client.remove_failover_scopes(name, [change.key])
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
            policy_name=f.get("policy", ""),
        )
        if change.action == DELETE:
            self.client.remove_option_value(key)
        else:
            # Set-DhcpServerv4OptionValue is an upsert: the same call creates and updates.
            self.client.set_option_value(key, f["value"])

    # -- policies ------------------------------------------------------------------------------------------------

    @staticmethod
    def _policy(f: dict[str, Any]) -> Policy:
        lease = f.get("lease_seconds")
        return Policy(
            name=f["name"],
            scope_id=f["scope_id"],
            description=f["description"],
            enabled=bool(f["enabled"]),
            processing_order=f["processing_order"],
            condition=f["condition"].upper(),
            criteria={k: list(v) for k, v in f["criteria"].items()},
            lease_duration=normalize.format_duration(lease) if lease else "",
        )

    def _policy_ranges(self, f: dict[str, Any], pairs: list[list[str]], add: bool) -> None:
        for start, end in pairs:
            r = PolicyIPRange(scope_id=f["scope_id"], policy_name=f["name"], start_range=start, end_range=end)
            if add:
                self.client.add_policy_ip_range(r)
            else:
                self.client.remove_policy_ip_range(r)

    def _apply_policy(self, change: Change) -> None:
        if change.action == DELETE:
            # Windows removes the policy's option values and IP ranges with it.
            f = change.before or {}
            self.client.remove_policy(f["name"], f["scope_id"])
            return
        f = change.after or {}
        if change.action == CREATE:
            self.client.add_policy(self._policy(f))
            self._policy_ranges(f, f["ip_ranges"], add=True)
            return
        before = change.before or {}
        cleared = sorted(set(before.get("criteria", {})) - set(f["criteria"]))
        if cleared:
            # Set-DhcpServerv4Policy only replaces the conditions it is given; removing the policy to clear one would
            # also remove its option values.
            raise PushRefused(
                f"policy {change.key}: Windows cannot remove the {', '.join(cleared)} condition of a policy; remove "
                "it by hand on Windows, or delete the policy and create it again"
            )
        self.client.set_policy(self._policy(f))
        old = [list(p) for p in before.get("ip_ranges", [])]
        new = [list(p) for p in f["ip_ranges"]]
        self._policy_ranges(f, [p for p in old if p not in new], add=False)
        self._policy_ranges(f, [p for p in new if p not in old], add=True)

    # -- superscopes ---------------------------------------------------------------------------------------------

    def _apply_superscope(self, change: Change) -> None:
        if change.action == DELETE:
            # The member scopes stay: only the grouping goes.
            self.client.remove_superscope((change.before or {})["name"])
            return
        f = change.after or {}
        if change.action == UPDATE and (before := change.before or {}).get("name") != f["name"]:
            # Only the case differs (the key is the lowercase name).
            self.client.rename_superscope(before["name"], f["name"])
        s = Superscope(name=f["name"], scope_ids=list(f["scope_ids"]))
        if change.action == CREATE:
            self.client.add_superscope(s)
        else:
            self.client.set_superscope(s)

    # -- failover ------------------------------------------------------------------------------------------------

    def _secret(self, change: Change, name: str) -> str:
        secret = self.failover_secret(name)
        if not secret:
            raise PushRefused(
                f"failover {change.key}: message authentication is on, but the relationship has no shared secret "
                "profile, or the profile has no secret; fix it in NetBox or in the plugin settings"
            )
        return secret

    def _apply_failover(self, change: Change) -> None:
        if change.action == DELETE:
            # Windows also removes the relationship's scopes from the partner server, as the cmdlet documents.
            self.client.remove_failover((change.before or {})["name"])
            return
        f = change.after or {}
        hot = f["mode"] == "hotstandby"
        rel = FailoverRelationship(
            name=f["name"],
            partner_server=f["partner_server"],
            mode="HotStandby" if hot else "LoadBalance",
            scope_ids=list(f["scope_ids"]),
            server_role=f["server_role"] if hot else "",
            load_balance_percent=f["load_balance_percent"],
            reserve_percent=f["reserve_percent"],
            max_client_lead_time=f["max_client_lead_time"],
            state_switch_interval=f["state_switch_interval"],
            auto_state_transition=f["state_switch_interval"] is not None,
        )
        if change.action == CREATE:
            self.client.add_failover(rel, shared_secret=self._secret(change, f["name"]) if f["auth"] else "")
            return
        before = change.before or {}
        if not same_host(before.get("partner_server", ""), f["partner_server"]):
            raise PushRefused(
                f"failover {change.key}: the partner server cannot change on Windows; delete the relationship and "
                "create it again"
            )
        if before.get("auth") and not f["auth"]:
            raise PushRefused(
                f"failover {change.key}: Windows cannot turn message authentication off through this cmdlet; turn it "
                "off by hand on Windows"
            )
        turned_on = f["auth"] and not before.get("auth")
        self.client.set_failover(rel, shared_secret=self._secret(change, f["name"]) if turned_on else "")
        # The membership on Windows right now: deleting a scope earlier in this push already took it out.
        current = set(self.client.get_failover(f["name"]).scope_ids)
        wanted = set(f["scope_ids"])
        if added := sorted(wanted - current):
            self.client.add_failover_scopes(f["name"], added)
        if gone := sorted(current - wanted):
            self.client.remove_failover_scopes(f["name"], gone)

    # -- MAC filters ---------------------------------------------------------------------------------------------

    def _apply_macfilter(self, change: Change) -> None:
        if change.action == DELETE:
            self.client.remove_filter((change.before or {})["mac_address"])
            return
        f = change.after or {}
        m = MacFilter(
            mac_address=f["mac_address"],
            list=normalize.enum_of(f["list"], "Allow", "Deny"),
            description=f["description"],
        )
        if change.action == CREATE:
            self.client.add_filter(m)
        else:
            # There is no Set cmdlet: the client removes and adds the filter again.
            self.client.set_filter(m)

    def _apply_filterlists(self, change: Change) -> None:
        if change.action == DELETE:
            # The lists always exist on Windows. NetBox no longer holding the switches means "stop managing them".
            return
        f = change.after or {}
        self.client.set_filter_lists(allow=f["allow"], deny=f["deny"])

    # -- DHCPv6 --------------------------------------------------------------------------------------------------

    def _apply_class6(self, change: Change) -> None:
        f = change.after or change.before or {}
        if change.action == DELETE:
            self.client.remove_v6_class(f["name"], "User")
            return
        c = DhcpClass(name=f["name"], type="User", data=f["data"], description=f["description"])
        if change.action == CREATE:
            self.client.add_v6_class(c)
        else:
            self.client.set_v6_class(c)

    def _apply_scope6(self, change: Change) -> None:
        if change.action == DELETE:
            # Never forced: a scope that still has leases fails with DHCP 20007, and that error is reported.
            self.client.remove_v6_scope((change.before or {})["prefix"])
            return
        f = change.after or {}

        def lifetime(name: str) -> str:
            # The plan fills empty lifetimes in from Windows, so None here only means "Windows could not say".
            return normalize.format_duration(f[name]) if f.get(name) else ""

        s = ScopeV6(
            prefix=f["prefix"],
            name=f["name"],
            description=f["description"],
            state=normalize.enum_of(f["state"], *normalize.SCOPE_STATES),
            preference=int(f["preference"] or 0),
            preferred_lifetime=lifetime("preferred_lifetime"),
            valid_lifetime=lifetime("valid_lifetime"),
            t1=lifetime("t1"),
            t2=lifetime("t2"),
        )
        if change.action == CREATE:
            self.client.add_v6_scope(s)
        else:
            self.client.set_v6_scope(s)

    def _apply_reservation6(self, change: Change) -> None:
        if change.action == DELETE:
            self.client.remove_v6_reservation((change.before or {})["ip_address"])
            return
        f = change.after or {}
        r = ReservationV6(
            prefix=f["prefix"],
            ip_address=f["ip_address"],
            client_duid=f["client_duid"],
            iaid=int(f["iaid"]),
            # Windows names a reservation after its IP when given no name.
            name=f["name"] or f["ip_address"],
            description=f["description"],
        )
        if change.action == CREATE:
            self.client.add_v6_reservation(r)
        else:
            self.client.set_v6_reservation(r)

    def _apply_option6(self, change: Change) -> None:
        f = change.after or change.before or {}
        prefix, reserved_ip = option6_target(f["parent"])
        key = OptionKeyV6(
            option_id=f["option_id"], prefix=prefix, reserved_ip=reserved_ip, user_class=f.get("user_class", "")
        )
        if change.action == DELETE:
            self.client.remove_v6_option_value(key)
        else:
            # Set-DhcpServerv6OptionValue is an upsert, like the IPv4 cmdlet.
            self.client.set_v6_option_value(key, f["value"])
