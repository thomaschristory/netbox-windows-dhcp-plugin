"""In-memory fake Windows DHCP server implementing ``Runner``.

Port of ``internal/dhcp/dhcpfake/fake.go``, extended to the objects the provider does
not manage. It never runs PowerShell: it reads the op marker on the first line of the
wrapped script (``# netbox-windows-dhcp-plugin:<op>``), applies the operation to in-memory
state and returns the same JSON envelope the real script would print.

Modelled server behaviour:

* scope ID derived from start_range AND subnet_mask; duplicate subnets rejected;
  ranges must sit inside the subnet; the subnet mask cannot be changed by set;
* the ``InActive`` input state reads back as ``Inactive``;
* reservation names default to the IP address; client IDs stored lowercase-dash;
* removing a scope with leases fails with DHCP 20007 unless forced; removing a scope
  cascades to its reservations, exclusions, options, policies, failover and
  superscope membership; removing a reservation drops its options;
* a reservation Set that changes the client ID or type (DHCPv6: the DUID or IAID)
  drops the reservation's option values, as Windows does for a new client ID and IAID
  (issue #42; the type and DUID are assumed to behave the same);
  ``reservation_set_drops_options`` makes every reservation Set drop them, the worst case
  seen on Windows Server 2025 for a description change;
* not-found errors use the same codes as the server (ObjectNotFound / DHCP 20005,
  20010, 20018) so the client's not-found mapping is exercised;
* an option value on a reservation is named by the reserved IP alone: Set and Remove
  refuse a scope ID (DHCPv6: a prefix) next to it, as Windows does (issues #20, #17);
* a policy condition with a wildcard anywhere but its start or end is refused (#18);
* ``empty_result_as_object`` prints ``{}`` instead of ``null`` for "nothing found", the
  shape Windows PowerShell 5.1 gave before the envelope fixed it (issue #15);
* ``fail`` injects an error for any op; ``calls`` records every call.

The fake trusts the client to send canonical values (it does not re-validate every
field the way the real cmdlets do). Error codes for objects the Go fake did not model
are representative, not verified against a real server.

This module must not import Django or NetBox.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import normalize
from .psscript import PSError, op_of

__all__ = ["Call", "FakeDhcpServer"]


@dataclass
class Call:
    op: str
    params: dict[str, Any]


class _Fail(Exception):  # noqa: N818 - internal control flow
    def __init__(self, err: PSError) -> None:
        super().__init__(str(err))
        self.err = err


def _err(cmd: str, code: int | str, category: str, message: str) -> _Fail:
    return _Fail(PSError(message=message, category=category, id=f"DHCP {code},{cmd}", command=cmd))


def _not_found(cmd: str, what: str) -> _Fail:
    return _err(cmd, 20005, "ObjectNotFound", f"Failed to get {what} on DHCP server FAKE.")


def _invalid(cmd: str, msg: str, code: int = 20000) -> _Fail:
    return _err(cmd, code, "InvalidArgument", msg)


def _ip4(s: str) -> int:
    try:
        return int(ipaddress.IPv4Address(s))
    except ValueError:
        return 0


def _ip6(s: str) -> int:
    try:
        return int(ipaddress.IPv6Address(s))
    except ValueError:
        return 0


def _s(p: Mapping[str, Any], k: str) -> str:
    v = p.get(k)
    return v if isinstance(v, str) else ""


def _n(p: Mapping[str, Any], k: str) -> int:
    v = p.get(k)
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0


def _opt_num(v: Any) -> str:
    s = str(v)
    if s.lower().startswith("0x"):
        try:
            return str(int(s, 16))
        except ValueError:
            return s
    return s


# A few standard option definitions so option values get their real names.
_STANDARD_OPTIONS: dict[int, tuple[str, str, bool]] = {
    3: ("Router", "IPv4Address", True),
    6: ("DNS Servers", "IPv4Address", True),
    15: ("DNS Domain Name", "String", False),
    42: ("NTP Servers", "IPv4Address", True),
    44: ("WINS/NBNS Servers", "IPv4Address", True),
    66: ("Boot Server Host Name", "String", False),
    67: ("Bootfile Name", "String", False),
}


class FakeDhcpServer:
    """In-memory DHCP server. Thread-safe; state is public for seeding and assertions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.scopes: dict[str, dict[str, Any]] = {}
        self.reservations: dict[str, dict[str, Any]] = {}  # keyed by IP
        self.exclusions: list[dict[str, Any]] = []
        self.options: dict[tuple, dict[str, Any]] = {}  # keyed by _option_key
        self.leases: dict[str, list[dict[str, Any]]] = {}  # keyed by scope ID
        self.option_definitions: dict[tuple[str, int], dict[str, Any]] = {
            ("", oid): {
                "option_id": oid,
                "name": name,
                "type": typ,
                "description": "",
                "vendor_class": "",
                "multi_valued": multi,
                "default_value": [],
            }
            for oid, (name, typ, multi) in _STANDARD_OPTIONS.items()
        }
        self.classes: dict[tuple[str, str], dict[str, Any]] = {}  # (type, lower name)
        self.failovers: dict[str, dict[str, Any]] = {}  # keyed by lower name
        self.failover_secrets: dict[str, str] = {}
        self.replications: list[dict[str, Any]] = []
        self.superscopes: dict[str, list[str]] = {}
        self.filters: dict[str, dict[str, Any]] = {}  # keyed by lower MAC
        self.filter_lists: dict[str, bool] = {"allow": False, "deny": False}
        self.policies: dict[tuple[str, str], dict[str, Any]] = {}  # (scope ID or "", lower name)
        self.policy_ranges: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.v6_scopes: dict[str, dict[str, Any]] = {}
        self.v6_reservations: dict[str, dict[str, Any]] = {}
        self.v6_options: dict[tuple, dict[str, Any]] = {}
        self.v6_classes: dict[tuple[str, str], dict[str, Any]] = {}  # (type, lower name)
        self.v6_exclusions: list[dict[str, Any]] = []
        self.v6_leases: dict[str, list[dict[str, Any]]] = {}  # keyed by prefix
        self.calls: list[Call] = []
        self.fail: Callable[[str, dict[str, Any]], PSError | None] | None = None
        # True: answer "nothing found" with {} rather than null, as a server did before issue #15 was fixed, so tests
        # can check the client still reads it as missing.
        self.empty_result_as_object = False
        # True: every reservation Set (IPv4 and DHCPv6) drops the reservation's option values, as Windows Server 2025
        # did for a description change sent with the client ID and type (issue #42). Tests use it to check the push
        # puts the values back.
        self.reservation_set_drops_options = False
        # What server.names reports: the computer name, then the DNS name.
        self.server_names: list[str] = ["fake", "fake.example.test"]

    # -- Runner ----------------------------------------------------------------

    def run(self, script: str, params: Mapping[str, Any] | None = None) -> bytes:
        op = op_of(script)
        if not op:
            raise ValueError("fake: script without op marker")
        p = json.loads(json.dumps(dict(params or {})))
        with self._lock:
            self.calls.append(Call(op, copy.deepcopy(p)))
            if self.fail is not None:
                e = self.fail(op, p)
                if e is not None:
                    return self._envelope(None, e)
            handler = getattr(self, "_op_" + op.replace(".", "_"), None)
            if handler is None:
                return self._envelope(
                    None, PSError(message=f"fake: unknown op {op}", category="NotImplemented", command=op)
                )
            try:
                data = handler(p)
            except _Fail as f:
                return self._envelope(None, f.err)
            if data is None and self.empty_result_as_object:
                data = {}
            return self._envelope(copy.deepcopy(data), None)

    @staticmethod
    def _envelope(data: Any, err: PSError | None) -> bytes:
        if err is not None:
            return json.dumps({"ok": False, "error": err.to_dict()}).encode()
        return json.dumps({"ok": True, "data": data}).encode()

    def ops(self) -> list[str]:
        with self._lock:
            return [c.op for c in self.calls]

    # -- seeding helpers ---------------------------------------------------------

    def add_lease(self, scope_id: str, ip_address: str, client_id: str = "00-11-22-33-44-55", **extra: Any) -> None:
        with self._lock:
            lease = {
                "ip_address": ip_address,
                "scope_id": scope_id,
                "client_id": client_id,
                "host_name": "",
                "address_state": "Active",
                "lease_expiry_time": "2030-01-01T00:00:00Z",
                "description": "",
                "client_type": "Dhcp",
            }
            lease.update(extra)
            self.leases.setdefault(scope_id, []).append(lease)

    def add_v6_lease(self, prefix: str, ip_address: str, client_duid: str = "00-01-00-01-aa-bb", **extra: Any) -> None:
        with self._lock:
            lease = {
                "ip_address": ip_address,
                "prefix": prefix,
                "client_duid": client_duid,
                "iaid": 1,
                "host_name": "",
                # No address_state: Windows reports none for a DHCPv6 lease (issue #22).
                "lease_expiry_time": "2030-01-01T00:00:00Z",
                "description": "",
            }
            lease.update(extra)
            self.v6_leases.setdefault(prefix, []).append(lease)

    def add_v6_exclusion(self, prefix: str, start_range: str, end_range: str) -> None:
        with self._lock:
            self.v6_exclusions.append({"prefix": prefix, "start_range": start_range, "end_range": end_range})

    def add_v6_class(self, name: str, type: str = "Vendor", data: str = "", description: str = "") -> None:  # noqa: A002
        """Seed a class the client cannot create (a DHCPv6 vendor class)."""
        with self._lock:
            self.v6_classes[(type.lower(), name.lower())] = {
                "name": name,
                "type": type,
                "data": data,
                "description": description,
            }

    # -- helpers -------------------------------------------------------------------

    def _scope(self, cmd: str, scope_id: str) -> dict[str, Any]:
        sc = self.scopes.get(scope_id)
        if sc is None:
            raise _not_found(cmd, f"scope {scope_id}")
        return sc

    @staticmethod
    def _in_scope(sc: Mapping[str, Any], ip: str) -> bool:
        try:
            return normalize.network_address(ip, sc["subnet_mask"]) == sc["scope_id"]
        except ValueError:
            return False

    def _check_range(self, cmd: str, scope_id: str, mask: str, start: str, end: str) -> None:
        sc = {"scope_id": scope_id, "subnet_mask": mask}
        if not self._in_scope(sc, start) or not self._in_scope(sc, end) or _ip4(start) > _ip4(end):
            raise _invalid(cmd, "The specified IP address range either overlaps with an existing range or is invalid.")

    def _scope_from(self, scope_id: str, p: Mapping[str, Any], old: Mapping[str, Any] | None) -> dict[str, Any]:
        state = _s(p, "state")
        if state.casefold() == "inactive":
            state = "Inactive"  # the server's own casing differs from the parameter's
        old = old or {}
        return {
            "scope_id": scope_id,
            "name": _s(p, "name"),
            "description": _s(p, "description"),
            "start_range": _s(p, "start_range"),
            "end_range": _s(p, "end_range"),
            "subnet_mask": _s(p, "subnet_mask"),
            "state": state,
            "type": _s(p, "type"),
            "lease_duration": (
                normalize.format_duration(_n(p, "lease_seconds"))
                if p.get("lease_seconds") is not None
                else old.get("lease_duration", "8.00:00:00")
            ),
            "delay": p["delay"] if p.get("delay") is not None else old.get("delay", 0),
            "max_bootp_clients": (
                p["max_bootp_clients"]
                if p.get("max_bootp_clients") is not None
                else old.get("max_bootp_clients", 4294967295)
            ),
            "activate_policies": (
                p["activate_policies"] if p.get("activate_policies") is not None else old.get("activate_policies", True)
            ),
            "superscope_name": old.get("superscope_name", ""),
        }

    # -- scopes ------------------------------------------------------------------------

    def _op_dhcp_available(self, p: dict) -> Any:
        return True

    def _op_server_names(self, p: dict) -> Any:
        return list(self.server_names)

    def _op_scope_get(self, p: dict) -> Any:
        return self._scope("Get-DhcpServerv4Scope", _s(p, "scope_id"))

    def _op_scope_list(self, p: dict) -> Any:
        return sorted(self.scopes.values(), key=lambda s: _ip4(s["scope_id"]))

    def _op_scope_add(self, p: dict) -> Any:
        cmd = "Add-DhcpServerv4Scope"
        try:
            sid = normalize.network_address(_s(p, "start_range"), _s(p, "subnet_mask"))
        except ValueError as e:
            raise _invalid(cmd, str(e)) from None
        if sid in self.scopes:
            raise _invalid(
                cmd, f"Failed to add scope {sid}. The specified subnet already exists on the DHCP server.", 20052
            )
        self._check_range(cmd, sid, _s(p, "subnet_mask"), _s(p, "start_range"), _s(p, "end_range"))
        sc = self._scope_from(sid, p, None)
        self.scopes[sid] = sc
        if sid != _s(p, "scope_id"):  # the read-back uses the client's scope ID
            raise _not_found("Get-DhcpServerv4Scope", "scope " + _s(p, "scope_id"))
        return sc

    def _op_scope_set(self, p: dict) -> Any:
        cmd = "Set-DhcpServerv4Scope"
        sid = _s(p, "scope_id")
        old = self._scope(cmd, sid)
        p["subnet_mask"] = old["subnet_mask"]
        start, end = _s(p, "start_range"), _s(p, "end_range")
        self._check_range(cmd, sid, old["subnet_mask"], start, end)
        # The script's two-step range change for a range that moves one end out and the other in (issue #30) end
        # in the same state as one call, so the fake applies the result directly; the pwsh stubs model the refusal.
        sc = self._scope_from(sid, p, old)
        self.scopes[sid] = sc
        return sc

    def _op_scope_remove(self, p: dict) -> Any:
        sid = _s(p, "scope_id")
        self._scope("Remove-DhcpServerv4Scope", sid)
        if self.leases.get(sid) and p.get("force") is not True:
            raise _err(
                "Remove-DhcpServerv4Scope",
                20007,
                "PermissionDenied",
                f"Failed to delete scope {sid}. The specified DHCP element has been used by a client "
                "and cannot be removed.",
            )
        del self.scopes[sid]
        self.leases.pop(sid, None)
        for ip in [ip for ip, r in self.reservations.items() if r["scope_id"] == sid]:
            self._drop_reservation(ip)
        self.exclusions = [e for e in self.exclusions if e["scope_id"] != sid]
        for k in [k for k in self.options if k[1] == sid]:
            del self.options[k]
        for k in [k for k in self.policies if k[0] == sid]:
            del self.policies[k]
            self.policy_ranges.pop(k, None)
        for f in self.failovers.values():
            f["scope_ids"] = [s for s in f["scope_ids"] if s != sid]
        for name in list(self.superscopes):
            self.superscopes[name] = [s for s in self.superscopes[name] if s != sid]
            if not self.superscopes[name]:
                del self.superscopes[name]
        return None

    # -- reservations ------------------------------------------------------------------

    def _drop_reservation(self, ip: str) -> None:
        self.reservations.pop(ip, None)
        self._drop_reservation_options(ip)

    def _drop_reservation_options(self, ip: str) -> None:
        for k in [k for k in self.options if k[2] == ip]:
            del self.options[k]

    def _drop_v6_reservation_options(self, ip: str) -> None:
        for k in [k for k in self.v6_options if k[2] == ip]:
            del self.v6_options[k]

    def _op_reservation_get(self, p: dict) -> Any:
        self._scope("Get-DhcpServerv4Reservation", _s(p, "scope_id"))
        r = self.reservations.get(_s(p, "ip_address"))
        if r is None or r["scope_id"] != _s(p, "scope_id"):
            return None
        return r

    def _op_reservation_list(self, p: dict) -> Any:
        sid = _s(p, "scope_id")
        self._scope("Get-DhcpServerv4Reservation", sid)
        out = [r for r in self.reservations.values() if r["scope_id"] == sid]
        return sorted(out, key=lambda r: _ip4(r["ip_address"]))

    def _op_reservation_add(self, p: dict) -> Any:
        cmd = "Add-DhcpServerv4Reservation"
        sc = self._scope(cmd, _s(p, "scope_id"))
        ip = _s(p, "ip_address")
        if not self._in_scope(sc, ip):
            raise _invalid(cmd, "The specified IP address is not in the scope.")
        if ip in self.reservations:
            raise _invalid(cmd, "The specified IP address is currently taken by another client.")
        try:
            cid = normalize.canonical_mac(_s(p, "client_id"))
        except ValueError as e:
            raise _invalid(cmd, str(e)) from None
        r = {
            "scope_id": sc["scope_id"],
            "ip_address": ip,
            "client_id": cid,
            "name": _s(p, "name") or ip,  # the real server also derives a default name
            "description": _s(p, "description"),
            "type": _s(p, "type") or "Both",
        }
        self.reservations[ip] = r
        return r

    def _op_reservation_set(self, p: dict) -> Any:
        cmd = "Set-DhcpServerv4Reservation"
        ip = _s(p, "ip_address")
        r = self.reservations.get(ip)
        if r is None:
            raise _err(cmd, 20018, "ObjectNotFound", f"Failed to get reservation {ip}")
        try:
            cid = normalize.canonical_mac(_s(p, "client_id"))
        except ValueError as e:
            raise _invalid(cmd, str(e)) from None
        # A new client ID makes Windows drop the reservation's option values (#42, lab, Windows Server 2025).
        # unverified: that a new type alone does the same.
        if self.reservation_set_drops_options or cid != r["client_id"] or _s(p, "type").lower() != r["type"].lower():
            self._drop_reservation_options(ip)
        r["client_id"] = cid
        r["type"] = _s(p, "type")
        r["description"] = _s(p, "description")
        if _s(p, "name"):
            r["name"] = _s(p, "name")
        if r["scope_id"] != _s(p, "scope_id"):
            return None  # read-back is scoped
        return r

    def _op_reservation_remove(self, p: dict) -> Any:
        ip = _s(p, "ip_address")
        if ip not in self.reservations:
            raise _err("Remove-DhcpServerv4Reservation", 20018, "ObjectNotFound", f"Failed to delete reservation {ip}")
        self._drop_reservation(ip)
        return None

    # -- exclusions ---------------------------------------------------------------------

    def _exclusion(self, op: str, p: dict) -> Any:
        sid = _s(p, "scope_id")
        sc = self._scope("Get-DhcpServerv4ExclusionRange", sid)
        want = {"scope_id": sid, "start_range": _s(p, "start_range"), "end_range": _s(p, "end_range")}
        idx = next((i for i, e in enumerate(self.exclusions) if e == want), -1)
        if op == "get":
            return want if idx >= 0 else None
        if op == "add":
            cmd = "Add-DhcpServerv4ExclusionRange"
            s, e = want["start_range"], want["end_range"]
            if not self._in_scope(sc, s) or not self._in_scope(sc, e) or _ip4(s) > _ip4(e):
                raise _invalid(
                    cmd, "The specified IP address range either overlaps with an existing range or is invalid."
                )
            if idx >= 0:
                raise _invalid(cmd, "The specified IP address range is already defined on the DHCP server.")
            self.exclusions.append(want)
            return want
        if idx < 0:
            raise _invalid(
                "Remove-DhcpServerv4ExclusionRange",
                "The specified IP address range either overlaps with an existing range or is invalid.",
            )
        del self.exclusions[idx]
        return None

    def _op_exclusion_get(self, p: dict) -> Any:
        return self._exclusion("get", p)

    def _op_exclusion_add(self, p: dict) -> Any:
        return self._exclusion("add", p)

    def _op_exclusion_remove(self, p: dict) -> Any:
        return self._exclusion("remove", p)

    def _op_exclusion_list(self, p: dict) -> Any:
        sid = _s(p, "scope_id")
        self._scope("Get-DhcpServerv4ExclusionRange", sid)
        return [e for e in self.exclusions if e["scope_id"] == sid]

    # -- option values ------------------------------------------------------------------

    @staticmethod
    def _option_level(p: Mapping[str, Any]) -> tuple:
        # A reservation's values belong to its IP, not to its scope: a scope ID next to the IP is ignored (Get
        # tolerates it on Windows; Set and Remove refuse it, see _refuse_scope_and_ip).
        ip = _s(p, "reserved_ip")
        return (
            "" if ip else _s(p, "scope_id"),
            ip,
            _s(p, "vendor_class"),
            _s(p, "user_class"),
            _s(p, "policy_name"),
        )

    @classmethod
    def _option_key(cls, p: Mapping[str, Any]) -> tuple:
        return (_n(p, "option_id"), *cls._option_level(p))

    @staticmethod
    def _refuse_scope_and_ip(cmd: str, p: Mapping[str, Any], where: str, label: str) -> None:
        if _s(p, where) and _s(p, "reserved_ip"):
            raise _invalid(cmd, f"{label} and ReservedIP cannot be specified together.")

    def _check_option_level(self, p: dict) -> None:
        if _s(p, "scope_id") and not _s(p, "reserved_ip"):
            self._scope("Get-DhcpServerv4OptionValue", _s(p, "scope_id"))
        ip = _s(p, "reserved_ip")
        if ip and ip not in self.reservations:
            raise _err("Get-DhcpServerv4OptionValue", 20018, "ObjectNotFound", f"Failed to get reservation {ip}")

    def _option_name(self, option_id: int, vendor_class: str) -> str:
        d = self.option_definitions.get((vendor_class, option_id))
        return d["name"] if d else f"Option {option_id}"

    def _op_option_get(self, p: dict) -> Any:
        self._check_option_level(p)
        return self.options.get(self._option_key(p))

    def _op_option_list(self, p: dict) -> Any:
        self._check_option_level(p)
        sid, rip, vc, uc, pol = self._option_level(p)
        out = []
        for k, v in sorted(self.options.items(), key=lambda kv: kv[0][0]):
            if k[1] != sid or k[2] != rip or k[5] != pol:
                continue
            if not p.get("all") and (k[3] != vc or k[4] != uc):
                continue
            out.append(v)
        return out

    def _op_option_set(self, p: dict) -> Any:
        self._refuse_scope_and_ip("Set-DhcpServerv4OptionValue", p, "scope_id", "ScopeId")
        self._check_option_level(p)
        oid = _n(p, "option_id")
        v = {
            "option_id": oid,
            "name": self._option_name(oid, _s(p, "vendor_class")),
            "value": [_opt_num(x) for x in p.get("value") or []],
            "vendor_class": _s(p, "vendor_class"),
            "user_class": _s(p, "user_class"),
            "policy_name": _s(p, "policy_name"),
            "scope_id": self._option_level(p)[0],
            "reserved_ip": _s(p, "reserved_ip"),
        }
        self.options[self._option_key(p)] = v
        return v

    def _op_option_remove(self, p: dict) -> Any:
        self._refuse_scope_and_ip("Remove-DhcpServerv4OptionValue", p, "scope_id", "ScopeId")
        self._check_option_level(p)
        k = self._option_key(p)
        if k not in self.options:
            raise _err("Remove-DhcpServerv4OptionValue", 20010, "NotSpecified", "Failed to delete option value")
        del self.options[k]
        return None

    def set_option(self, option_id: int, value: list[str], **level: str) -> None:
        """Seed an option value directly (level keys as in OptionKey; a reservation's scope is dropped)."""
        p: dict[str, Any] = {"option_id": option_id, "value": value, **level}
        if p.get("reserved_ip"):
            p.pop("scope_id", None)
        with self._lock:
            self._op_option_set(p)

    # -- leases ---------------------------------------------------------------------------

    def _op_lease_list(self, p: dict) -> Any:
        sid = _s(p, "scope_id")
        self._scope("Get-DhcpServerv4Lease", sid)
        return list(self.leases.get(sid, []))

    # -- option definitions ----------------------------------------------------------------

    def _op_optiondef_get(self, p: dict) -> Any:
        return self.option_definitions.get((_s(p, "vendor_class"), _n(p, "option_id")))

    def _op_optiondef_list(self, p: dict) -> Any:
        return [self.option_definitions[k] for k in sorted(self.option_definitions)]

    def _op_optiondef_add(self, p: dict) -> Any:
        cmd = "Add-DhcpServerv4OptionDefinition"
        vc, oid = _s(p, "vendor_class"), _n(p, "option_id")
        if vc and ("vendor", vc.lower()) not in self.classes:
            raise _err(cmd, 20049, "ObjectNotFound", f"The vendor class {vc} does not exist.")
        if (vc, oid) in self.option_definitions:
            raise _invalid(cmd, f"Failed to add option definition {oid}. The specified option already exists.", 20009)
        d = {
            "option_id": oid,
            "name": _s(p, "name"),
            "type": _s(p, "type"),
            "description": _s(p, "description"),
            "vendor_class": vc,
            "multi_valued": bool(p.get("multi_valued")),
            "default_value": [str(x) for x in p.get("default_value") or []],
        }
        self.option_definitions[(vc, oid)] = d
        return d

    def _op_optiondef_set(self, p: dict) -> Any:
        d = self.option_definitions.get((_s(p, "vendor_class"), _n(p, "option_id")))
        if d is None:
            raise _err("Set-DhcpServerv4OptionDefinition", 20010, "ObjectNotFound", "The option does not exist.")
        d["name"] = _s(p, "name") or d["name"]
        d["description"] = _s(p, "description")
        if p.get("default_value"):
            d["default_value"] = [str(x) for x in p["default_value"]]
        return d

    def _op_optiondef_remove(self, p: dict) -> Any:
        k = (_s(p, "vendor_class"), _n(p, "option_id"))
        if k not in self.option_definitions:
            raise _err("Remove-DhcpServerv4OptionDefinition", 20010, "ObjectNotFound", "The option does not exist.")
        del self.option_definitions[k]
        for ok in [ok for ok in self.options if ok[0] == k[1] and ok[3] == k[0]]:
            del self.options[ok]
        return None

    # -- classes ----------------------------------------------------------------------------

    @staticmethod
    def _class_key(p: Mapping[str, Any]) -> tuple[str, str]:
        return (_s(p, "type").lower(), _s(p, "name").lower())

    def _op_class_get(self, p: dict) -> Any:
        return self.classes.get(self._class_key(p))

    def _op_class_list(self, p: dict) -> Any:
        t = _s(p, "type").lower()
        return [c for k, c in sorted(self.classes.items()) if not t or k[0] == t]

    def _op_class_add(self, p: dict) -> Any:
        k = self._class_key(p)
        if k in self.classes:
            raise _invalid("Add-DhcpServerv4Class", f"The class {_s(p, 'name')} already exists.", 20045)
        c = {"name": _s(p, "name"), "type": _s(p, "type"), "data": _s(p, "data"), "description": _s(p, "description")}
        self.classes[k] = c
        return c

    def _op_class_set(self, p: dict) -> Any:
        c = self.classes.get(self._class_key(p))
        if c is None:
            raise _err("Set-DhcpServerv4Class", 20049, "ObjectNotFound", f"The class {_s(p, 'name')} does not exist.")
        c["data"] = _s(p, "data")
        c["description"] = _s(p, "description")
        return c

    def _op_class_remove(self, p: dict) -> Any:
        k = self._class_key(p)
        if k not in self.classes:
            raise _err(
                "Remove-DhcpServerv4Class", 20049, "ObjectNotFound", f"The class {_s(p, 'name')} does not exist."
            )
        del self.classes[k]
        idx = 3 if k[0] == "vendor" else 4  # vendor_class or user_class in the option key
        for ok in [ok for ok in self.options if ok[idx].lower() == k[1]]:
            del self.options[ok]
        return None

    # -- failover ----------------------------------------------------------------------------

    def _failover(self, cmd: str, name: str) -> dict[str, Any]:
        f = self.failovers.get(name.lower())
        if f is None:
            raise _err(cmd, 20115, "ObjectNotFound", f"Failover relationship {name} does not exist.")
        return f

    def _check_failover_scopes(self, cmd: str, ids: list[str], name: str) -> None:
        for sid in ids:
            self._scope(cmd, sid)
            for other in self.failovers.values():
                if other["name"].lower() != name.lower() and sid in other["scope_ids"]:
                    raise _invalid(cmd, f"Scope {sid} is already part of failover relationship {other['name']}.", 20113)

    def _apply_failover_settings(self, f: dict[str, Any], p: dict) -> None:
        for k in (
            "load_balance_percent",
            "reserve_percent",
            "max_client_lead_time",
            "state_switch_interval",
            "auto_state_transition",
        ):
            if p.get(k) is not None:
                f[k] = p[k]
        if _s(p, "shared_secret"):
            self.failover_secrets[f["name"].lower()] = _s(p, "shared_secret")
            f["enable_auth"] = True

    def _op_failover_get(self, p: dict) -> Any:
        return self.failovers.get(_s(p, "name").lower())

    def _op_failover_list(self, p: dict) -> Any:
        return [self.failovers[k] for k in sorted(self.failovers)]

    def _op_failover_add(self, p: dict) -> Any:
        cmd = "Add-DhcpServerv4Failover"
        name = _s(p, "name")
        if name.lower() in self.failovers:
            raise _invalid(cmd, f"Failover relationship {name} already exists.", 20114)
        ids = list(p.get("scope_ids") or [])
        if not ids:
            raise _invalid(cmd, "At least one scope is required.")
        self._check_failover_scopes(cmd, ids, name)
        hot = _s(p, "mode") == "HotStandby"
        f = {
            "name": name,
            "partner_server": _s(p, "partner_server"),
            "mode": "HotStandby" if hot else "LoadBalance",
            "scope_ids": ids,
            "server_role": _s(p, "server_role") if hot else "",
            "load_balance_percent": None if hot else 50,
            "reserve_percent": 5 if hot else None,
            "max_client_lead_time": 3600,
            "state_switch_interval": None,
            "auto_state_transition": False,
            "enable_auth": False,
            "state": "Normal",
        }
        self._apply_failover_settings(f, p)
        self.failovers[name.lower()] = f
        return f

    def _op_failover_set(self, p: dict) -> Any:
        f = self._failover("Set-DhcpServerv4Failover", _s(p, "name"))
        # The script never sends a role: Set-DhcpServerv4Failover has none (issue #33).
        if _s(p, "mode"):
            f["mode"] = _s(p, "mode")
            # Windows makes this server active and the partner standby when the mode becomes HotStandby (lab, Windows
            # Server 2025).
            if f["mode"] == "HotStandby" and not f["server_role"]:
                f["server_role"] = "Active"
            elif f["mode"] == "LoadBalance":
                f["server_role"] = ""
        self._apply_failover_settings(f, p)
        return f

    def _op_failover_remove(self, p: dict) -> Any:
        self._failover("Remove-DhcpServerv4Failover", _s(p, "name"))
        del self.failovers[_s(p, "name").lower()]
        self.failover_secrets.pop(_s(p, "name").lower(), None)
        return None

    def _op_failover_add_scope(self, p: dict) -> Any:
        cmd = "Add-DhcpServerv4FailoverScope"
        f = self._failover(cmd, _s(p, "name"))
        ids = list(p.get("scope_ids") or [])
        self._check_failover_scopes(cmd, ids, f["name"])
        f["scope_ids"] = f["scope_ids"] + [s for s in ids if s not in f["scope_ids"]]
        return f

    def _op_failover_remove_scope(self, p: dict) -> Any:
        cmd = "Remove-DhcpServerv4FailoverScope"
        f = self._failover(cmd, _s(p, "name"))
        ids = list(p.get("scope_ids") or [])
        for sid in ids:
            if sid not in f["scope_ids"]:
                raise _invalid(cmd, f"Scope {sid} is not part of failover relationship {f['name']}.", 20116)
        # Like Windows Server 2025: the last scope can leave, and the relationship stays, empty, on both partners.
        f["scope_ids"] = [s for s in f["scope_ids"] if s not in ids]
        return f

    def _op_failover_replicate(self, p: dict) -> Any:
        cmd = "Invoke-DhcpServerv4FailoverReplication"
        # Like Windows: a relationship name or scope ids, never both (issue #51).
        if _s(p, "name") and p.get("scope_ids"):
            raise _invalid(cmd, "Parameter set cannot be resolved using the specified named parameters.")
        if _s(p, "name"):
            self._failover(cmd, _s(p, "name"))
        # Windows refuses the whole call when one scope is in no failover relationship (DHCP 20116, lab, Windows Server
        # 2025).
        in_failover = {sid for f in self.failovers.values() for sid in f["scope_ids"]}
        for sid in p.get("scope_ids") or []:
            if sid not in in_failover:
                raise _invalid(cmd, f"The scope {sid} is not part of a failover relationship.")
        self.replications.append({"name": _s(p, "name"), "scope_ids": list(p.get("scope_ids") or [])})
        return None

    # -- superscopes -------------------------------------------------------------------------

    def _superscope_view(self, name: str) -> dict[str, Any] | None:
        for k, ids in self.superscopes.items():
            if k.lower() == name.lower():
                return {"name": k, "scope_ids": list(ids)}
        return None

    def _sync_superscope_names(self) -> None:
        for sc in self.scopes.values():
            sc["superscope_name"] = ""
        for name, ids in self.superscopes.items():
            for sid in ids:
                if sid in self.scopes:
                    self.scopes[sid]["superscope_name"] = name

    def _superscope_add(self, cmd: str, name: str, ids: list[str]) -> None:
        for sid in ids:
            self._scope(cmd, sid)
        key = next((k for k in self.superscopes if k.lower() == name.lower()), name)
        for other in self.superscopes:  # a scope belongs to one superscope: adding moves it
            if other != key:
                self.superscopes[other] = [s for s in self.superscopes[other] if s not in ids]
        cur = self.superscopes.get(key, [])
        self.superscopes[key] = cur + [s for s in ids if s not in cur]
        self.superscopes = {k: v for k, v in self.superscopes.items() if v}
        self._sync_superscope_names()

    def _op_superscope_get(self, p: dict) -> Any:
        return self._superscope_view(_s(p, "name"))

    def _op_superscope_list(self, p: dict) -> Any:
        return [self._superscope_view(k) for k in sorted(self.superscopes)]

    def _op_superscope_add(self, p: dict) -> Any:
        self._superscope_add("Add-DhcpServerv4Superscope", _s(p, "name"), list(p.get("scope_ids") or []))
        return self._superscope_view(_s(p, "name"))

    def _op_superscope_set(self, p: dict) -> Any:
        name, want = _s(p, "name"), list(p.get("scope_ids") or [])
        cur = self._superscope_view(name)
        have = cur["scope_ids"] if cur else []
        if want:
            self._superscope_add("Add-DhcpServerv4Superscope", name, want)
        gone = [s for s in have if s not in want]
        if gone:
            key = self._superscope_view(name)["name"]
            self.superscopes[key] = [s for s in self.superscopes[key] if s not in gone]
            self.superscopes = {k: v for k, v in self.superscopes.items() if v}
            self._sync_superscope_names()
        return self._superscope_view(name)

    def _op_superscope_rename(self, p: dict) -> Any:
        cur = self._superscope_view(_s(p, "name"))
        if cur is None:
            raise _err("Rename-DhcpServerv4Superscope", 20005, "ObjectNotFound", "The superscope does not exist.")
        del self.superscopes[cur["name"]]
        self.superscopes[_s(p, "new_name")] = cur["scope_ids"]
        self._sync_superscope_names()
        return self._superscope_view(_s(p, "new_name"))

    def _op_superscope_remove(self, p: dict) -> Any:
        cur = self._superscope_view(_s(p, "name"))
        if cur is None:
            raise _err("Remove-DhcpServerv4Superscope", 20005, "ObjectNotFound", "The superscope does not exist.")
        del self.superscopes[cur["name"]]
        self._sync_superscope_names()
        return None

    # -- MAC filters -----------------------------------------------------------------------------

    def _op_filter_get(self, p: dict) -> Any:
        return self.filters.get(_s(p, "mac_address").lower())

    def _op_filter_list(self, p: dict) -> Any:
        lst = _s(p, "list")
        return [f for k, f in sorted(self.filters.items()) if not lst or f["list"] == lst]

    def _filter_add(self, p: dict) -> dict[str, Any]:
        mac = _s(p, "mac_address").lower()
        if mac in self.filters:
            raise _invalid("Add-DhcpServerv4Filter", f"The MAC address {mac} is already in a filter list.", 20096)
        f = {"mac_address": mac, "list": _s(p, "list"), "description": _s(p, "description")}
        self.filters[mac] = f
        return f

    def _op_filter_add(self, p: dict) -> Any:
        return self._filter_add(p)

    def _op_filter_set(self, p: dict) -> Any:
        self._op_filter_remove(p)
        return self._filter_add(p)

    def _op_filter_remove(self, p: dict) -> Any:
        mac = _s(p, "mac_address").lower()
        if mac not in self.filters:
            raise _err("Remove-DhcpServerv4Filter", 20005, "ObjectNotFound", f"The MAC address {mac} is not filtered.")
        del self.filters[mac]
        return None

    def _op_filterlist_get(self, p: dict) -> Any:
        return dict(self.filter_lists)

    def _op_filterlist_set(self, p: dict) -> Any:
        self.filter_lists = {"allow": bool(p.get("allow")), "deny": bool(p.get("deny"))}
        return dict(self.filter_lists)

    # -- policies ---------------------------------------------------------------------------------

    def _policy_key(self, cmd: str, p: Mapping[str, Any]) -> tuple[str, str]:
        sid = _s(p, "scope_id")
        if sid:
            self._scope(cmd, sid)
        return (sid, _s(p, "name").lower())

    def _op_policy_get(self, p: dict) -> Any:
        return self.policies.get(self._policy_key("Get-DhcpServerv4Policy", p))

    def _op_policy_list(self, p: dict) -> Any:
        sid = _s(p, "scope_id")
        if sid:
            self._scope("Get-DhcpServerv4Policy", sid)
        out = [v for k, v in self.policies.items() if k[0] == sid]
        return sorted(out, key=lambda x: (x["processing_order"] or 0, x["name"]))

    @staticmethod
    def _check_wildcards(cmd: str, p: dict) -> None:
        # Windows takes "*" only as the first or last character of a condition value: "*.example.test" and "host*"
        # are fine, "host*.example.test" is refused (issue #18).
        for v in (p.get("criteria") or {}).values():
            for value in [str(x) for x in (v or [])[1:]]:
                if "*" in value[1:-1]:
                    raise _invalid(cmd, "Wildcard is valid either at the beginning or at the end of the condition.")

    def _level(self, sid: str) -> list[tuple[str, str]]:
        """The policies of one level (server or scope), in processing order."""
        keys = [k for k in self.policies if k[0] == sid]
        return sorted(keys, key=lambda k: (self.policies[k]["processing_order"] or 0, k[1]))

    def _ordered(self, cmd: str, k: tuple[str, str], order: int | None) -> list[tuple[str, str]]:
        """
        The level's policies once policy ``k`` takes processing order ``order``. Windows numbers the policies of a
        level 1, 2, 3... without gaps: taking a used number moves the others down the list, and a number past the end
        fails with DHCP 20110 (seen on Windows Server 2025 for Add, with order 2 on an empty scope). No number puts a
        new policy at the end and leaves an existing one where it is.
        """
        # Set refuses a number past the end the same way, and a removal closes the gap (lab, Windows Server 2025).
        level = self._level(k[0])
        if order is None:
            return level if k in level else [*level, k]
        others = [x for x in level if x != k]
        if order < 1 or order > len(others) + 1:
            verb = "update" if k in self.policies else "create"
            raise _err(cmd, 20110, "InvalidData", f"Failed to {verb} policy {k[1]} on DHCP server FAKE.")
        others.insert(order - 1, k)
        return others

    def _renumber(self, level: list[tuple[str, str]]) -> None:
        for i, k in enumerate(level, 1):
            self.policies[k]["processing_order"] = i

    def _apply_policy(self, pol: dict[str, Any], p: dict) -> None:
        if p.get("enabled") is not None:
            pol["enabled"] = bool(p["enabled"])
        if _s(p, "condition"):
            pol["condition"] = _s(p, "condition")
        if _n(p, "lease_seconds"):
            pol["lease_duration"] = normalize.format_duration(_n(p, "lease_seconds"))
        for k, v in (p.get("criteria") or {}).items():
            if v is not None:
                # Windows stores conditions in its own spelling (seen on Windows Server 2025): a separator before a
                # MAC prefix wildcard, host names in lowercase. The operator is the first item.
                # An uppercase MAC prefix is read back in lowercase (lab, Windows Server 2025).
                items = [str(x) for x in v]
                pol["criteria"][k] = items[:1] + [normalize.canonical_policy_criterion(k, x) for x in items[1:]]

    @staticmethod
    def _order(p: dict) -> int | None:
        return None if p.get("processing_order") is None else int(p["processing_order"])

    def _op_policy_add(self, p: dict) -> Any:
        cmd = "Add-DhcpServerv4Policy"
        k = self._policy_key(cmd, p)
        if k in self.policies:
            raise _invalid(cmd, f"The policy {_s(p, 'name')} already exists.", 20151)
        if not (p.get("criteria") or {}):
            raise _invalid(cmd, "At least one condition is required.")
        self._check_wildcards(cmd, p)
        level = self._ordered(cmd, k, self._order(p))
        pol: dict[str, Any] = {
            "name": _s(p, "name"),
            "scope_id": k[0],
            "description": _s(p, "description"),
            "enabled": True,
            "processing_order": None,
            "condition": "OR",
            "criteria": {},
            "lease_duration": "",
        }
        self._apply_policy(pol, p)
        self.policies[k] = pol
        self._renumber(level)
        return pol

    def _op_policy_set(self, p: dict) -> Any:
        cmd = "Set-DhcpServerv4Policy"
        k = self._policy_key(cmd, p)
        pol = self.policies.get(k)
        if pol is None:
            raise _err(cmd, 20152, "ObjectNotFound", f"The policy {_s(p, 'name')} does not exist.")
        self._check_wildcards(cmd, p)
        level = self._ordered(cmd, k, self._order(p))
        pol["description"] = _s(p, "description")
        self._apply_policy(pol, p)
        self._renumber(level)
        return pol

    def _op_policy_remove(self, p: dict) -> Any:
        k = self._policy_key("Remove-DhcpServerv4Policy", p)
        if k not in self.policies:
            raise _err(
                "Remove-DhcpServerv4Policy", 20152, "ObjectNotFound", f"The policy {_s(p, 'name')} does not exist."
            )
        del self.policies[k]
        self.policy_ranges.pop(k, None)
        for ok in [ok for ok in self.options if ok[1] == k[0] and ok[5].lower() == k[1]]:
            del self.options[ok]
        self._renumber(self._level(k[0]))
        return None

    def _policy_for_range(self, cmd: str, p: dict) -> tuple[tuple[str, str], dict[str, Any]]:
        sc = self._scope(cmd, _s(p, "scope_id"))
        k = (sc["scope_id"], _s(p, "policy_name").lower())
        if k not in self.policies:
            raise _err(cmd, 20152, "ObjectNotFound", f"The policy {_s(p, 'policy_name')} does not exist.")
        return k, sc

    def _op_policyrange_list(self, p: dict) -> Any:
        k, _ = self._policy_for_range("Get-DhcpServerv4PolicyIPRange", p)
        return list(self.policy_ranges.get(k, []))

    def _op_policyrange_add(self, p: dict) -> Any:
        cmd = "Add-DhcpServerv4PolicyIPRange"
        k, sc = self._policy_for_range(cmd, p)
        self._check_range(cmd, sc["scope_id"], sc["subnet_mask"], _s(p, "start_range"), _s(p, "end_range"))
        r = {
            "scope_id": sc["scope_id"],
            "policy_name": _s(p, "policy_name"),
            "start_range": _s(p, "start_range"),
            "end_range": _s(p, "end_range"),
        }
        self.policy_ranges.setdefault(k, []).append(r)
        return r

    def _op_policyrange_remove(self, p: dict) -> Any:
        cmd = "Remove-DhcpServerv4PolicyIPRange"
        k, _ = self._policy_for_range(cmd, p)
        rs = self.policy_ranges.get(k, [])
        keep = [r for r in rs if (r["start_range"], r["end_range"]) != (_s(p, "start_range"), _s(p, "end_range"))]
        if len(keep) == len(rs):
            raise _invalid(cmd, "The specified IP address range does not exist in the policy.")
        self.policy_ranges[k] = keep
        return None

    # -- DHCPv6 ------------------------------------------------------------------------------------

    def _v6scope(self, cmd: str, prefix: str) -> dict[str, Any]:
        sc = self.v6_scopes.get(prefix)
        if sc is None:
            raise _not_found(cmd, f"scope {prefix}")
        return sc

    @staticmethod
    def _apply_v6scope(sc: dict[str, Any], p: dict, cmd: str) -> None:
        if _s(p, "state"):
            sc["state"] = "Inactive" if _s(p, "state").casefold() == "inactive" else _s(p, "state")
        if p.get("preference") is not None:
            sc["preference"] = int(p["preference"])
        for src, dst in (
            ("preferred_seconds", "preferred_lifetime"),
            ("valid_seconds", "valid_lifetime"),
            ("t1_seconds", "t1"),
            ("t2_seconds", "t2"),
        ):
            if _n(p, src):
                sc[dst] = normalize.format_duration(_n(p, src))
        # The cmdlet documentation asks for T1 < T2 < preferred lifetime. Refusing anything else makes the tests
        # notice a push that would send renewal and rebinding times a real server may refuse or hand to clients.
        # Windows refuses such timers with WIN32 87 (InvalidArgument) rather than storing them (lab, Windows Server
        # 2025).
        t1, t2, pref = (normalize.parse_duration(sc[k], allow_zero=True) for k in ("t1", "t2", "preferred_lifetime"))
        if not t1 < t2 < pref:
            raise _invalid(cmd, f"Invalid T1 or T2 for scope {sc['prefix']}: T1 < T2 < preferred lifetime expected.")

    def _op_v6scope_get(self, p: dict) -> Any:
        return self.v6_scopes.get(_s(p, "prefix"))

    def _op_v6scope_list(self, p: dict) -> Any:
        return [self.v6_scopes[k] for k in sorted(self.v6_scopes, key=_ip6)]

    def _op_v6scope_add(self, p: dict) -> Any:
        prefix = _s(p, "prefix")
        if prefix in self.v6_scopes:
            raise _invalid("Add-DhcpServerv6Scope", f"The scope {prefix} already exists.", 20052)
        sc = {
            "prefix": prefix,
            "name": _s(p, "name"),
            "description": _s(p, "description"),
            "state": "Active",
            "preference": 0,
            "preferred_lifetime": "8.00:00:00",
            "valid_lifetime": "12.00:00:00",
            # Windows' defaults: T1 4 days, T2 6.4 days.
            "t1": "4.00:00:00",
            "t2": "6.09:36:00",
        }
        self._apply_v6scope(sc, p, "Add-DhcpServerv6Scope")
        self.v6_scopes[prefix] = sc
        return sc

    def _op_v6scope_set(self, p: dict) -> Any:
        sc = self._v6scope("Set-DhcpServerv6Scope", _s(p, "prefix"))
        sc["name"] = _s(p, "name") or sc["name"]
        sc["description"] = _s(p, "description")
        before = dict(sc)
        try:
            self._apply_v6scope(sc, p, "Set-DhcpServerv6Scope")
        except _Fail:
            sc.clear()
            sc.update(before)
            raise
        return sc

    def _op_v6scope_remove(self, p: dict) -> Any:
        prefix = _s(p, "prefix")
        self._v6scope("Remove-DhcpServerv6Scope", prefix)
        if self.v6_leases.get(prefix) and p.get("force") is not True:
            raise _err(
                "Remove-DhcpServerv6Scope",
                20007,
                "PermissionDenied",
                f"Failed to delete scope {prefix}. The specified DHCP element has been used by a client "
                "and cannot be removed.",
            )
        del self.v6_scopes[prefix]
        self.v6_leases.pop(prefix, None)
        self.v6_exclusions = [e for e in self.v6_exclusions if e["prefix"] != prefix]
        gone = [ip for ip, r in self.v6_reservations.items() if r["prefix"] == prefix]
        for ip in gone:
            del self.v6_reservations[ip]
        # Reservation values are keyed by their IP alone, so they go with their reservation.
        for k in [k for k in self.v6_options if k[1] == prefix or k[2] in gone]:
            del self.v6_options[k]
        return None

    def _op_v6reservation_get(self, p: dict) -> Any:
        self._v6scope("Get-DhcpServerv6Reservation", _s(p, "prefix"))
        r = self.v6_reservations.get(_s(p, "ip_address"))
        return r if r is not None and r["prefix"] == _s(p, "prefix") else None

    def _op_v6reservation_list(self, p: dict) -> Any:
        self._v6scope("Get-DhcpServerv6Reservation", _s(p, "prefix"))
        out = [r for r in self.v6_reservations.values() if r["prefix"] == _s(p, "prefix")]
        return sorted(out, key=lambda r: _ip6(r["ip_address"]))

    def _op_v6reservation_add(self, p: dict) -> Any:
        cmd = "Add-DhcpServerv6Reservation"
        prefix = _s(p, "prefix")
        self._v6scope(cmd, prefix)
        ip = _s(p, "ip_address")
        if ipaddress.IPv6Address(ip) not in ipaddress.IPv6Network(f"{prefix}/64", strict=False):
            raise _invalid(cmd, "The specified IP address is not in the scope.")
        if ip in self.v6_reservations:
            raise _invalid(cmd, "The specified IP address is currently taken by another client.")
        r = {
            "prefix": prefix,
            "ip_address": ip,
            "client_duid": normalize.canonical_duid(_s(p, "client_duid")),
            "iaid": _n(p, "iaid"),
            "name": _s(p, "name") or ip,
            "description": _s(p, "description"),
        }
        self.v6_reservations[ip] = r
        return r

    def _op_v6reservation_set(self, p: dict) -> Any:
        ip = _s(p, "ip_address")
        r = self.v6_reservations.get(ip)
        if r is None:
            raise _err("Set-DhcpServerv6Reservation", 20018, "ObjectNotFound", f"Failed to get reservation {ip}")
        duid, iaid = normalize.canonical_duid(_s(p, "client_duid")), _n(p, "iaid")
        # A new IAID makes Windows drop the reservation's option values (#42, lab, Windows Server 2025).
        # unverified: that a new DUID alone does the same.
        if self.reservation_set_drops_options or duid != r["client_duid"] or iaid != r["iaid"]:
            self._drop_v6_reservation_options(ip)
        r["client_duid"] = duid
        r["iaid"] = iaid
        r["description"] = _s(p, "description")
        if _s(p, "name"):
            r["name"] = _s(p, "name")
        return r if r["prefix"] == _s(p, "prefix") else None

    def _op_v6reservation_remove(self, p: dict) -> Any:
        ip = _s(p, "ip_address")
        if ip not in self.v6_reservations:
            raise _err("Remove-DhcpServerv6Reservation", 20018, "ObjectNotFound", f"Failed to delete reservation {ip}")
        del self.v6_reservations[ip]
        self._drop_v6_reservation_options(ip)
        return None

    @staticmethod
    def _v6option_key(p: Mapping[str, Any]) -> tuple:
        # Class names are not case sensitive on Windows: "Lab" and "lab" name one value.
        # As for IPv4, a reservation's values belong to its IP: a prefix next to it is ignored.
        ip = _s(p, "reserved_ip")
        return (
            _n(p, "option_id"),
            "" if ip else _s(p, "prefix"),
            ip,
            _s(p, "vendor_class").lower(),
            _s(p, "user_class").lower(),
        )

    def _check_v6option_level(self, p: dict) -> None:
        if _s(p, "prefix") and not _s(p, "reserved_ip"):
            self._v6scope("Get-DhcpServerv6OptionValue", _s(p, "prefix"))
        ip = _s(p, "reserved_ip")
        if ip and ip not in self.v6_reservations:
            raise _err("Get-DhcpServerv6OptionValue", 20018, "ObjectNotFound", f"Failed to get reservation {ip}")

    def _op_v6option_get(self, p: dict) -> Any:
        self._check_v6option_level(p)
        return self.v6_options.get(self._v6option_key(p))

    def _op_v6option_list(self, p: dict) -> Any:
        self._check_v6option_level(p)
        key = self._v6option_key(p)
        # -All: every class at that level; otherwise only the values of the class asked for (none: standard ones).
        width = 3 if p.get("all") is True else 5
        return [v for k, v in sorted(self.v6_options.items()) if k[1:width] == key[1:width]]

    def _op_v6option_set(self, p: dict) -> Any:
        self._refuse_scope_and_ip("Set-DhcpServerv6OptionValue", p, "prefix", "Prefix")
        self._check_v6option_level(p)
        user = _s(p, "user_class")
        if user and ("user", user.lower()) not in self.v6_classes:
            raise _invalid("Set-DhcpServerv6OptionValue", f"The class {user} does not exist.", 20049)
        oid = _n(p, "option_id")
        v = {
            "option_id": oid,
            "name": f"Option {oid}",
            "value": [_opt_num(x) for x in p.get("value") or []],
            "vendor_class": _s(p, "vendor_class"),
            "user_class": _s(p, "user_class"),
            "prefix": self._v6option_key(p)[1],
            "reserved_ip": _s(p, "reserved_ip"),
        }
        self.v6_options[self._v6option_key(p)] = v
        return v

    def _op_v6option_remove(self, p: dict) -> Any:
        self._refuse_scope_and_ip("Remove-DhcpServerv6OptionValue", p, "prefix", "Prefix")
        self._check_v6option_level(p)
        k = self._v6option_key(p)
        if k not in self.v6_options:
            raise _err("Remove-DhcpServerv6OptionValue", 20010, "NotSpecified", "Failed to delete option value")
        del self.v6_options[k]
        return None

    # -- DHCPv6 exclusions, leases and classes ---------------------------------------------

    def _op_v6exclusion_list(self, p: dict) -> Any:
        prefix = _s(p, "prefix")
        self._v6scope("Get-DhcpServerv6ExclusionRange", prefix)
        return [e for e in self.v6_exclusions if e["prefix"] == prefix]

    def _op_v6lease_list(self, p: dict) -> Any:
        prefix = _s(p, "prefix")
        self._v6scope("Get-DhcpServerv6Lease", prefix)
        return list(self.v6_leases.get(prefix, []))

    def _op_v6class_list(self, p: dict) -> Any:
        return [c for _, c in sorted(self.v6_classes.items())]

    def _op_v6class_add(self, p: dict) -> Any:
        k = self._class_key(p)
        if k in self.v6_classes:
            raise _invalid("Add-DhcpServerv6Class", f"The class {_s(p, 'name')} already exists.", 20045)
        c = {"name": _s(p, "name"), "type": _s(p, "type"), "data": _s(p, "data"), "description": _s(p, "description")}
        self.v6_classes[k] = c
        return c

    def _op_v6class_set(self, p: dict) -> Any:
        c = self.v6_classes.get(self._class_key(p))
        if c is None:
            raise _err("Set-DhcpServerv6Class", 20049, "ObjectNotFound", f"The class {_s(p, 'name')} does not exist.")
        c["data"] = _s(p, "data")
        c["description"] = _s(p, "description")
        return c

    def _op_v6class_remove(self, p: dict) -> Any:
        k = self._class_key(p)
        if k not in self.v6_classes:
            raise _err(
                "Remove-DhcpServerv6Class", 20049, "ObjectNotFound", f"The class {_s(p, 'name')} does not exist."
            )
        del self.v6_classes[k]
        idx = 3 if k[0] == "vendor" else 4  # vendor_class or user_class in the option key
        for ok in [ok for ok in self.v6_options if ok[idx].lower() == k[1]]:
            del self.v6_options[ok]
        return None
