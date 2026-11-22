"""
Plan and run a push: make a Windows DHCP server match what NetBox holds for it.

Pure Python (no Django). ``plan`` compares NetBox records (the source) with Windows records (the target) and decides
what is safe to do; ``execute`` applies that plan through a ``WindowsWriter``. The caller keeps the SyncState
bookkeeping: ``managed`` maps (kind, key) to the fingerprint last pushed, and the result says which rows to write
and which to drop.

Safety rules, because a push can delete on a production DHCP server:

- Only objects in ``managed`` are ever deleted. Anything else Windows holds was made by hand and is left alone
  (import brings it into NetBox instead).
- A managed object changed on Windows since the last push (its fingerprint no longer matches) is not deleted.
- An object whose removal would take unmanaged objects with it (a scope with a hand-made reservation, say) is not
  deleted.
- Children of an object that could not be created are not attempted, and parents of an object that could not be
  deleted are kept.
"""

from __future__ import annotations

import bisect
import hashlib
import ipaddress
import itertools
import json
import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..windows import normalize
from ..windows.psscript import NotFound, WindowsLayerError
from ..windows.runner import RunnerError
from .diff import CREATE, DELETE, KIND_ORDER, UPDATE, Change, Record, diff, index
from .windows_state import (
    NAME_FIELDS,
    SKIPPED_OPTION_IDS,
    V6_PREFIX_LENGTH,
    canonical_criteria,
    canonical_option_fields,
    class6_key,
    class_key,
    option6_target,
    optiondef_key,
    policy_key,
    records_equal,
    reservation6_key,
    same_host,
    scope6_key,
    with_option_types,
)
from .windows_state import scope_ids as windows_scope_ids
from .windows_writer import PushRefused, option_target

__all__ = ("Plan", "Result", "Skip", "execute", "fingerprint", "parents", "plan")

log = logging.getLogger(__name__)

Key = tuple[str, str]

# What Windows gives a scope created without a lease duration (8 days), and what a push then sends.
DEFAULT_LEASE_SECONDS = 8 * 86400

# What Windows gives a DHCPv6 scope created without lifetimes: preferred 8 days, valid 12 days, renewal (T1) 4 days
# and rebinding (T2) 6.4 days, that is 6 days 9:36 (read back from Windows Server 2025, and what the
# Add-DhcpServerv6Scope documentation says). T1 and T2 are half and 80 % of the preferred lifetime, the usual DHCP
# ratios, and the documentation asks for T1 < T2 < preferred lifetime: so when NetBox leaves T1 or T2 empty, a push
# works them out from the preferred lifetime it sends (see _prepare_scope6) rather than sending 4 and 6.4 days to a
# scope whose addresses stop being preferred sooner than that.
# Windows does the same for a scope created with a shorter preferred lifetime and no timers: 2 days gives T1 1 day
# and T2 1 day 14:24 (lab, Windows Server 2022), and it refuses timers that break T1 < T2 < preferred (WIN32 87).
V6_LIFETIME_DEFAULTS = {
    "preferred_lifetime": 8 * 86400,
    "valid_lifetime": 12 * 86400,
    "t1": 4 * 86400,
    "t2": 6 * 86400 + 9 * 3600 + 36 * 60,
}

# Field netbox_state sets on the extra copies of an object Windows can hold once ("#<pk>" keys): they name no Windows
# object.
DUPLICATE = "_duplicate"

# What Windows gives a failover relationship created without these settings.
FAILOVER_DEFAULTS = {"load_balance_percent": 50, "reserve_percent": 5, "max_client_lead_time": 3600}

# The sync run summary lists at most this many entries of each kind (planned changes, refused changes, errors, changes
# not attempted): the summary is stored with the run and shown whole in the UI and the API, and one lost connection
# early in a large push would otherwise store one entry per remaining change. The counts next to the lists stay exact.
PLANNED_LIMIT = 1000

DRIFTED = (
    "changed on Windows since NetBox last pushed it, so it was not deleted; delete it on Windows by hand, or import "
    "it back into NetBox"
)
# A relationship holding scopes added on Windows by hand is recorded with the fingerprint of the relationship without
# them: the scopes NetBox put there. The next push finds that set again by trying the fingerprint against the
# Windows list minus some of the scopes NetBox no longer wants; past this many candidates it gives up and keeps them.
OWNED_SEARCH_LIMIT = 10
CONFLICT = "NetBox holds more than one object for this Windows object (or one without an address); fix it in NetBox"
LEASE_OR_MASK = (
    "option {code} is the {what} on Windows, which a push sets from the {source}; remove the option from NetBox"
)


@dataclass(frozen=True)
class Skip:
    change: Change
    reason: str
    # Held back by the delete pause (see _pause_deletes), not refused for what it is.
    waiting: bool = False


@dataclass
class Plan:
    # Changes to make, in dependency order.
    changes: list[Change] = field(default_factory=list)
    # Changes the push will not make, with the reason.
    skipped: list[Skip] = field(default_factory=list)
    # Windows objects NetBox does not hold and never pushed: left alone.
    unmanaged: list[Key] = field(default_factory=list)
    # Objects that already match: recorded as managed with this fingerprint.
    in_sync: dict[Key, str] = field(default_factory=dict)
    # SyncState rows for objects that exist on neither side any more.
    stale: set[Key] = field(default_factory=set)
    # For each Windows object, the Windows objects that would go with it if it were removed.
    children: dict[Key, list[Key]] = field(default_factory=dict)
    # Relationships updated around scopes added on Windows by hand: those scopes, left out of the fingerprint the
    # update records (see _protect_memberships).
    protected: dict[Key, set[str]] = field(default_factory=dict)
    # Policies refused for a processing order that would not fit even without this push's deletes: they count for
    # the delete pause (see _order_level), unlike those the deletes alone push past the end.
    order_refusals: set[Key] = field(default_factory=set)
    # Scopes moved from one relationship to another in this push: taker key -> [(releaser key, releaser name, scope)].
    moves: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "changes": len(self.changes),
            **{a: sum(1 for c in self.changes if c.action == a) for a in (CREATE, UPDATE, DELETE)},
            "unmanaged": len(self.unmanaged),
            "planned": [c.to_dict() for c in self.changes[:PLANNED_LIMIT]],
        }
        if len(self.changes) > PLANNED_LIMIT:
            out["planned_truncated"] = True
        if self.skipped:
            _capped(out, "not_pushed", [(s.change, s.reason) for s in self.skipped])
            # Not "skipped": the result of the push uses that name for the changes it did not attempt.
            out["not_pushed_count"] = len(self.skipped)
            waiting = sum(1 for s in self.skipped if s.waiting)
            if waiting:
                # Listed first in not_pushed (plan sorts them there), and counted whatever the cap.
                out["waiting_count"] = waiting
        return out


@dataclass
class Result:
    applied: list[Change] = field(default_factory=list)
    failed: list[tuple[Change, str]] = field(default_factory=list)
    skipped: list[tuple[Change, str]] = field(default_factory=list)
    # SyncState rows to write (key -> fingerprint) and to drop.
    upserts: dict[Key, str] = field(default_factory=dict)
    removals: set[Key] = field(default_factory=set)
    # True when the push stopped early (the connection failed, or an unexpected error) and the remaining changes
    # were not attempted.
    aborted: bool = False
    # Failed changes that may have reached Windows all the same: the reply was lost, a later step of the change
    # failed, or Windows reported an error part-way. Their scopes are replicated to a failover partner like the
    # applied ones, since the next push may find nothing left to change there.
    maybe_written: list[Change] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "applied": len(self.applied),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
        }
        if self.failed:
            _capped(out, "errors", self.failed)
        if self.skipped:
            _capped(out, "not_attempted", self.skipped)
        return out


def _skip_dict(change: Change, reason: str) -> dict[str, str]:
    return {"action": change.action, "kind": change.kind, "key": change.key, "reason": reason}


def _capped(out: dict[str, Any], name: str, items: list[tuple[Change, str]]) -> None:
    """Put the first PLANNED_LIMIT changes (each with its reason) in the summary, and say so when some were left out."""
    out[name] = [_skip_dict(c, reason) for c, reason in items[:PLANNED_LIMIT]]
    if len(items) > PLANNED_LIMIT:
        out[f"{name}_truncated"] = True


# -- fingerprints --------------------------------------------------------------------------------------------------


def fingerprint(record: Record) -> str:
    """
    A hash of what an object looks like, to notice later whether someone changed it on Windows. Numeric option
    types hash in decimal; String/Binary values remain exact. A policy's processing order is left out: Windows
    renumbers policies itself whenever one is added, moved or removed.
    """
    fields = canonical_option_fields(record.kind, record.fields)
    if record.kind == "policy":
        fields.pop("processing_order", None)
    # Windows names are not case sensitive.
    for kind, name in NAME_FIELDS:
        if kind == record.kind and isinstance(fields.get(name), str):
            fields[name] = fields[name].lower()
    # Policy conditions as Windows stores them, so the NetBox spelling and the one read back hash the same.
    if record.kind == "policy" and isinstance(fields.get("criteria"), dict):
        fields["criteria"] = canonical_criteria(fields["criteria"])
    data = {"kind": record.kind, "key": record.key, "fields": fields}
    if record.kind in ("option", "option6", "optiondef"):
        # Old hashes normalized every string numerically, so even a hash matching the current exact text could
        # describe a different spelling. Never use one to authorize deletion; an equal push/import adopts v2.
        data["option_value_version"] = 2
    data = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(data.encode()).hexdigest()


def unchanged(stored: str, record: Record) -> bool:
    """
    Whether ``record`` (as Windows has it now) still matches the fingerprint recorded for it: the check that allows
    deleting a Windows object. 0.2 starts from empty SyncState tables (its first migration drops the 0.1.x ones), so
    only fingerprints in the current form exist.
    """
    return stored == fingerprint(record)


# -- dependencies --------------------------------------------------------------------------------------------------


# Objects that list their scopes ("scope_ids"): a scope leaving them is an update of theirs, not a removal.
MEMBERSHIPS = ("superscope", "failover")


def _criteria_classes(criteria: Mapping[str, list[str]]) -> list[Key]:
    """Classes a policy condition names (operator first, then names; a wildcard names no single class)."""
    out = []
    for name, class_type in (("vendor_class", "vendor"), ("user_class", "user")):
        out.extend(("class", class_key(class_type, v)) for v in criteria.get(name, [])[1:] if "*" not in v)
    return out


def parents(kind: str, fields: Mapping[str, Any]) -> list[Key]:
    """The objects this one needs on Windows (and that take it with them when removed)."""
    if kind in ("exclusion", "reservation"):
        return [("scope", fields["scope_id"])]
    if kind == "policy":
        out: list[Key] = [("scope", fields["scope_id"])] if fields.get("scope_id") else []
        return out + _criteria_classes(fields.get("criteria") or {})
    if kind in MEMBERSHIPS:
        return [("scope", s) for s in fields.get("scope_ids", [])]
    if kind == "optiondef" and fields.get("vendor_class"):
        return [("class", class_key("vendor", fields["vendor_class"]))]
    if kind in ("reservation6", "exclusion6"):
        return [("scope6", scope6_key(fields["prefix"]))]
    if kind == "option6":
        prefix, ip = option6_target(fields["parent"])
        out = [("scope6", scope6_key(prefix))] if prefix else []
        if ip:
            out.append(("reservation6", reservation6_key(prefix, ip)))
        if fields.get("user_class"):
            out.append(("class6", class6_key(fields["user_class"])))
        return out
    if kind == "option":
        scope_id, ip = option_target(fields["parent"])
        vendor = fields.get("vendor_class", "")
        out = [("optiondef", optiondef_key(fields["option_id"], vendor))]
        if scope_id:
            out.append(("scope", scope_id))
        if ip:
            out.append(("reservation", f"{scope_id}/{ip}"))
        if fields.get("user_class"):
            out.append(("class", class_key("user", fields["user_class"])))
        if vendor:
            out.append(("class", class_key("vendor", vendor)))
        if fields.get("policy"):
            out.append(("policy", policy_key(scope_id, fields["policy"])))
        return out
    return []


# -- planning ------------------------------------------------------------------------------------------------------


_MAC_CONDITION_HINTS = {
    "spaces": "remove the spaces or line breaks around it",
    "characters": "use hex digits with dashes or no separator; Windows refuses colons",
    "groups": "write each byte of a prefix as two hex digits; Windows ignores the dashes and pads an odd prefix on "
    "the left",
    "dashes": "remove the doubled dash or the dash at either end",
    "length": "a MAC address has 12 hex digits; end it with * to match a prefix",
    "long": "a prefix has at most 5 bytes (10 hex digits); Windows refuses a longer one",
    "half": "a prefix must be whole bytes; Windows pads an odd number of digits on the left",
}


def _mac_condition_reason(record: Record) -> str | None:
    """
    Why a policy's MAC address condition is not pushed, or None (#110). Saving a policy refuses such a condition, but
    one saved before that check (0.6.0 and earlier) or by a raw save (a branch merge, a script) can still be here.
    """
    if record.kind != "policy":
        return None
    for value in ((record.fields.get("criteria") or {}).get("mac_address") or [])[1:]:
        problem = normalize.mac_condition_problem(value)
        if problem:
            return (
                f"policy {record.fields.get('name')}: its MAC address condition {value!r} is not written the way "
                f"Windows reads it ({_MAC_CONDITION_HINTS[problem]}); fix it in NetBox"
            )
    return None


def _unpushable(
    record: Record, skipped_option_ids: set[int], defined: set[str] | None, current: Record | None = None
) -> str | None:
    """Why NetBox data cannot become a Windows object, or None. ``current`` is the Windows twin, if any."""
    f = record.fields
    exists = current is not None
    # netbox_state marks extra copies with "#<pk>", and a reservation without an address ends in "/": neither names
    # a Windows object.
    if DUPLICATE in f:
        return CONFLICT
    if record.kind == "scope" and not (f.get("start_range") and f.get("end_range")):
        return "the scope has no IP range to hand out; add one in NetBox"
    if record.kind == "reservation" and not f.get("client_id"):
        return "the reservation has neither a MAC address nor a client ID; add one in NetBox"
    if record.kind == "optiondef" and f["option_id"] in skipped_option_ids and not f.get("vendor_class"):
        return f"Windows defines option {f['option_id']} itself; remove the definition from NetBox"
    if record.kind == "policy" and not f.get("criteria"):
        return "a policy needs at least one condition on Windows; add one in NetBox"
    if record.kind == "policy" and not _valid_policy_ranges(f.get("ip_ranges")):
        # Only a raw save (a branch merge, a script, nbshell) can store this: the form and the API refuse it.
        return (
            f"policy {f.get('name')}: its stored IP ranges are not a list of [start, end] IPv4 address pairs; fix "
            "them in NetBox"
        )
    reason = _mac_condition_reason(record)
    if reason:
        return reason
    if record.kind == "scope":
        reason = _scope_range_problem(record)
        if reason:
            return reason
        old_mask = current.fields.get("subnet_mask") if current is not None else None
        if old_mask and f.get("subnet_mask") and old_mask != f["subnet_mask"]:
            # Set-DhcpServerv4Scope has no subnet mask: a split or a merge only works by removing the scope with its
            # reservations, options and leases. Refused here, so the delete pause keeps everything else in place.
            return (
                f"the subnet mask changes from {old_mask} to {f['subnet_mask']}, which Windows only allows by deleting "
                "the scope with its reservations, options and leases; do that on Windows by hand, then import"
            )
    if not f.get("scope_ids") and (record.kind == "superscope" or (record.kind == "failover" and not exists)):
        return f"a {record.kind} needs at least one scope on Windows; add one in NetBox, or delete it"
    if record.kind == "failover":
        if not f.get("partner_server"):
            return "the partner server has no Windows server (host) in NetBox; add one"
        if f["mode"] == "hotstandby" and not f.get("server_role"):
            return "a hot standby relationship needs the primary server's role (active or standby); set it in NetBox"
        if current is not None:
            # What the writer would refuse on an update (windows_writer._apply_failover), refused here so that it
            # counts for the delete pause and never lets another relationship release a scope for nothing.
            reason = _failover_update_problem(f, current.fields)
            if reason:
                return reason
    if record.kind == "scope6" and f.get("prefix_length") != V6_PREFIX_LENGTH:
        return f"Windows DHCPv6 scopes are /{V6_PREFIX_LENGTH} prefixes; this scope is /{f.get('prefix_length')}"
    if record.kind == "reservation6":
        if not f.get("ip_address"):
            return "the reservation has no IPv6 address; add one in NetBox"
        if not f.get("client_duid"):
            return "the reservation has no DUID; add one in NetBox"
        if f.get("iaid") is None:
            return "the reservation has no IAID; set it on the reservation in NetBox"
    if record.kind == "class6" and f.get("class_type") != "user":
        return "only DHCPv6 user classes are synced"
    if record.kind == "option6" and f.get("policy"):
        return "Windows has no DHCPv6 policies; remove the policy from the value in NetBox"
    if record.kind == "option6" and not f.get("value"):
        return "the option has no value; set one or remove the option in NetBox"
    if record.kind == "option":
        code = f["option_id"]
        vendor = f.get("vendor_class", "")
        if code in skipped_option_ids and not vendor:
            what, source = ("subnet mask", "prefix length") if code == 1 else ("lease time", "scope's lease duration")
            return LEASE_OR_MASK.format(code=code, what=what, source=source)
        if not f.get("value"):
            return "the option has no value; set one or remove the option in NetBox"
        if defined is not None and optiondef_key(code, vendor) not in defined:
            what = f"vendor class {vendor} option {code}" if vendor else f"option {code}"
            return (
                f"Windows has no definition for {what}; add an option definition for it in NetBox, or create it on "
                "Windows"
            )
    return None


def _failover_update_problem(f: Mapping[str, Any], current: Mapping[str, Any]) -> str | None:
    """Why Windows cannot turn the relationship it has (``current``) into this one in place, or None."""
    if current.get("partner_server") and not same_host(str(current["partner_server"]), str(f["partner_server"])):
        return (
            "the partner server cannot change on Windows; delete the relationship in NetBox and push, then create it "
            "again with the new partner"
        )
    hot, was_hot = f.get("mode") == "hotstandby", current.get("mode") == "hotstandby"
    if hot and was_hot and (current.get("server_role") or "") != (f.get("server_role") or ""):
        return (
            "Windows cannot change a hot standby server's role (active or standby) in place; delete the relationship "
            "in NetBox and push, then create it again with the new role"
        )
    if current.get("auth") and f.get("auth") is False:
        return "Windows cannot turn message authentication off through this cmdlet; turn it off by hand on Windows"
    return None


def _valid_policy_ranges(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for pair in value:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return False
        if any(_ipv4(a) is None for a in pair):
            return False
    return True


def _ipv4(value: Any) -> ipaddress.IPv4Address | None:
    try:
        return ipaddress.IPv4Address(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _scope_network(record: Record) -> ipaddress.IPv4Network | None:
    """The subnet of an IPv4 scope record (its key is the network address), or None when it does not parse."""
    try:
        return ipaddress.IPv4Network(f"{record.key}/{record.fields.get('subnet_mask')}")
    except ValueError:
        return None


def _scope_range(record: Record | None) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address] | None:
    if record is None:
        return None
    start, end = _ipv4(record.fields.get("start_range")), _ipv4(record.fields.get("end_range"))
    return (start, end) if start is not None and end is not None else None


def _scope_range_problem(record: Record) -> str | None:
    """
    An IPv4 scope's range must sit inside its prefix: Windows works the scope out from the range and the mask, so a
    range from another subnet would create a different scope. NetBox can hold such a range after an IPAM edit of the
    prefix or the IP range, which the plugin's own checks never see.
    """
    network, bounds = _scope_network(record), _scope_range(record)
    if network is None or bounds is None:
        return None  # the client refuses what does not parse, with its own message
    start, end = bounds
    if start not in network or end not in network:
        return (
            f"the IP range {start}-{end} is outside the scope's prefix {network}; fix the range or the prefix in NetBox"
        )
    if start > end:
        return f"the IP range {start}-{end} ends before it starts; fix it in NetBox"
    # /31 and /32 have no network or broadcast address to leave out.
    # Windows refuses a range holding the network or broadcast address (lab, Windows Server 2025); no client could
    # use either address anyway.
    if network.prefixlen <= 30 and (start == network.network_address or end == network.broadcast_address):
        which = "network" if start == network.network_address else "broadcast"
        return (
            f"the IP range {start}-{end} includes the {which} address of {network}, which no client can use; "
            "start the range after the network address and end it before the broadcast address in NetBox"
        )
    return None


def _overlaps(items: Iterable[tuple[Key, int, int]]) -> dict[Key, Key]:
    """
    For each (key, first, last) interval that overlaps another, one interval it overlaps. One pass in address order:
    an interval overlaps an earlier one exactly when it starts before the furthest end seen so far.
    """
    out: dict[Key, Key] = {}
    owner: Key | None = None
    furthest = -1
    for key, first, last in sorted(items, key=lambda i: (i[1], i[2], i[0])):
        if owner is not None and first <= furthest:
            out.setdefault(key, owner)
            out.setdefault(owner, key)
        if last > furthest:
            owner, furthest = key, last
    return out


def _identity(value: Any) -> str:
    """A client ID or DUID compared the way Windows reads it: hex digits, without separators or case."""
    return re.sub(r"[\s:.-]", "", str(value)).lower()


def _client_of(record: Record) -> tuple[str, ...] | None:
    """
    Who a reservation is for, wherever it sits: the client ID (IPv4), or the DUID and IAID (DHCPv6). Windows holds one
    reservation per client in a scope.
    """
    f = record.fields
    if record.kind == "reservation":
        return ("v4", _identity(f["client_id"])) if f.get("client_id") else None
    if f.get("client_duid") and f.get("iaid") is not None:
        return ("v6", _identity(f["client_duid"]), str(f["iaid"]))
    return None


def _scope_of(record: Record) -> str:
    """The scope key a reservation or exclusion record hangs off (the prefix for DHCPv6)."""
    return str(record.fields.get("scope_id") or record.fields.get("prefix") or "")


def _conflicts(
    source: list[Record], target: Mapping[Key, Record], managed: Mapping[Key, str] | None = None
) -> dict[Key, str]:
    """
    Reasons that need more than one record: an object outside its scope, two scopes or two exclusion ranges sharing
    addresses, two reservations for one client in a scope. Windows refuses all of these, so the push leaves them out
    and says why. When one of the clashing objects is already on Windows as NetBox has it, that one keeps being
    pushed and only the newcomers are left out; otherwise all of them are. A scope on Windows that NetBox neither holds
    nor manages counts as already there: the push leaves it alone, so a NetBox scope sharing its addresses could never
    be created.
    """
    managed = managed or {}
    out: dict[Key, str] = {}
    live = [r for r in source if DUPLICATE not in r.fields]
    scopes = {r.key: r for r in live if r.kind == "scope"}
    scopes6 = {r.fields.get("prefix"): r for r in live if r.kind == "scope6"}
    # Second NetBox scopes for one Windows scope ("#<pk>" keys): their own reason is CONFLICT, and what hangs off them
    # gets a reason saying so, rather than a check against a scope that cannot exist on Windows.
    doubled = {r.key for r in source if r.kind in ("scope", "scope6") and DUPLICATE in r.fields}

    def on_windows(record: Record, *names: str) -> bool:
        current = target.get((record.kind, record.key))
        return current is not None and all(
            str(current.fields.get(n)).lower() == str(record.fields.get(n)).lower() for n in names
        )

    # Scopes of one server sharing addresses (a /23 over a /24, say), with the Windows scopes the push leaves alone.
    # Windows refuses a scope sharing addresses with another (lab, Windows Server 2025: a /25 inside a /24 gives
    # ResourceExists).
    networks = {("scope", key): n for key, r in scopes.items() if (n := _scope_network(r)) is not None}
    for key, r in target.items():
        if key[0] != "scope" or (n := _scope_network(r)) is None:
            continue
        if key[1] not in scopes and key not in managed:
            networks[("windows", key[1])] = n
        elif key[1] in scopes and not on_windows(scopes[key[1]], "subnet_mask"):
            # A scope whose mask changes in NetBox (refused) stays on Windows with its old mask, which a new scope
            # of the split or merge would collide with.
            networks[("windows", key[1])] = n
    pairs = _overlaps((key, int(n.network_address), int(n.broadcast_address)) for key, n in networks.items())
    for (kind, key), other in pairs.items():
        if kind != "scope" or on_windows(scopes[key], "subnet_mask"):
            continue
        if other[0] != "windows":
            where = "another scope of this server in NetBox"
        elif other[1] in scopes:
            where = "as Windows still has it (its subnet mask cannot change in place)"
        else:
            where = "a scope made on Windows outside NetBox (import it, or remove it there)"
        out[kind, key] = (
            f"the scope {networks[kind, key]} shares addresses with scope {networks[other]}, {where}; Windows refuses "
            "two scopes sharing addresses: fix the prefixes in NetBox"
        )

    by_scope: defaultdict[str, list[tuple[Key, int, int]]] = defaultdict(list)
    for r in live:
        key = (r.kind, r.key)
        if r.kind == "policy" and (reason := _policy_ranges_place(r, scopes.get(r.fields.get("scope_id") or ""))):
            out.setdefault(key, reason)
            continue
        if r.kind not in ("reservation", "reservation6", "exclusion"):
            continue
        if _scope_of(r) in doubled or f"v6/{_scope_of(r)}" in doubled:
            out.setdefault(
                key,
                f"its scope {_scope_of(r)} is a second NetBox scope for one Windows scope (same network address); "
                "fix the scopes in NetBox",
            )
            continue
        if r.kind == "reservation":
            reason = _reservation_place(r, scopes.get(r.fields["scope_id"]))
        elif r.kind == "reservation6":
            reason = _reservation6_place(r, scopes6.get(r.fields.get("prefix")))
        else:
            reason = _exclusion_place(r, scopes.get(r.fields["scope_id"]))
            start, end = _ipv4(r.fields.get("start_range")), _ipv4(r.fields.get("end_range"))
            if reason is None and start is not None and end is not None:
                by_scope[r.fields["scope_id"]].append((key, int(start), int(end)))
        if reason:
            out.setdefault(key, reason)

    # Windows refuses an exclusion range sharing even one address with another of the scope, and one inside another
    # (lab, Windows Server 2025: "overlaps with an existing range or is invalid").
    by_key = {(r.kind, r.key): r for r in live}
    for scope_id, ranges in by_scope.items():
        for key, other in _overlaps(ranges).items():
            if not on_windows(by_key[key]):
                start, end = by_key[other].fields["start_range"], by_key[other].fields["end_range"]
                out.setdefault(
                    key,
                    f"the exclusion range shares addresses with exclusion range {start}-{end} of scope {scope_id}; "
                    "merge or fix them in NetBox",
                )

    # Windows refuses a second reservation for one client in a scope (lab, Windows Server 2025: ResourceExists for
    # IPv4; the same DUID and IAID for DHCPv6, while another IAID of the same DUID is accepted).
    clients: defaultdict[tuple[str, ...], list[Record]] = defaultdict(list)
    for r in live:
        if r.kind in ("reservation", "reservation6") and (client := _client_of(r)) is not None:
            clients[(_scope_of(r), *client)].append(r)
    for records in clients.values():
        if len(records) < 2:
            continue
        names = ("client_id",) if records[0].kind == "reservation" else ("client_duid", "iaid")
        kept = [r for r in records if on_windows(r, *names)]
        for r in records:
            if r in kept:
                continue
            others = ", ".join(o.fields.get("ip_address") or o.key for o in records if o is not r)
            what = "client" if r.kind == "reservation" else "DUID and IAID"
            out.setdefault(
                (r.kind, r.key),
                f"reservation {others} in this scope is for the same {what}, and Windows holds one reservation per "
                "client in a scope; keep one of them in NetBox",
            )
    return out


def _reservation_place(record: Record, scope: Record | None) -> str | None:
    """
    An IPv4 reservation must be inside its scope's prefix. Inside the prefix but outside the scope's IP range is
    accepted (lab, Windows Server 2025), whatever older documentation says.
    """
    ip = _ipv4(record.fields.get("ip_address"))
    network = _scope_network(scope) if scope is not None else None
    if ip is None or network is None:
        return None
    if ip not in network:
        return f"the address {ip} is outside the scope's prefix {network}; fix the address or the scope in NetBox"
    return None


def _policy_ranges_place(record: Record, scope: Record | None) -> str | None:
    """A scope policy's IP ranges must run upwards and sit inside the scope's prefix (shape: see _unpushable)."""
    network = _scope_network(scope) if scope is not None else None
    ranges = record.fields.get("ip_ranges")
    if network is None or not _valid_policy_ranges(ranges):
        return None
    for start, end in ranges:
        first, last = ipaddress.IPv4Address(start), ipaddress.IPv4Address(end)
        if first > last or first not in network or last not in network:
            return (
                f"policy {record.fields.get('name')}: its IP range {start}-{end} is reversed or outside the scope's "
                f"prefix {network}; fix it in NetBox"
            )
    return None


def _reservation6_place(record: Record, scope: Record | None) -> str | None:
    if scope is None or not record.fields.get("ip_address"):
        return None
    try:
        network = ipaddress.IPv6Network(f"{scope.fields['prefix']}/{scope.fields.get('prefix_length')}")
        ip = ipaddress.IPv6Address(record.fields["ip_address"])
    except ValueError:
        return None
    if ip not in network:
        return f"the address {ip} is outside the scope's prefix {network}; fix the address or the scope in NetBox"
    return None


def _exclusion_place(record: Record, scope: Record | None) -> str | None:
    """
    An exclusion range must be inside its scope's prefix and run upwards. One reaching past the scope's IP range but
    inside the prefix is accepted (lab, Windows Server 2025).
    """
    start, end = _ipv4(record.fields.get("start_range")), _ipv4(record.fields.get("end_range"))
    network = _scope_network(scope) if scope is not None else None
    if start is None or end is None or network is None:
        return None
    if start not in network or end not in network:
        return f"the exclusion range {start}-{end} is outside the scope's prefix {network}; fix it in NetBox"
    if start > end:
        return f"the exclusion range {start}-{end} ends before it starts; fix it in NetBox"
    return None


# -- the delete pause -------------------------------------------------------------------------------------------------

# What the delete pause covers: the IPv4 scope tree, the DHCPv6 scope tree, and the server-level policies with the
# option values set through them. Classes, option definitions, plain server option values and MAC filters are in
# none: nothing NetBox holds can be moved onto them under another key.
FAMILIES = {"v4": "IPv4 scopes", "v6": "DHCPv6 scopes", "policy": "server-level policies"}
# How many blocking objects a pause reason names before "and N more".
PAUSE_NAMES = 3


def family(kind: str, fields: Mapping[str, Any]) -> str | None:
    """The family an object belongs to for the delete pause ("v4", "v6", "policy"), or None."""
    if kind in ("scope", "superscope", "reservation", "exclusion", "failover"):
        return "v4"
    if kind == "policy":
        return "v4" if fields.get("scope_id") else "policy"
    if kind == "option":
        scope_id, _ = option_target(fields.get("parent") or "server")
        if scope_id:
            return "v4"
        return "policy" if fields.get("policy") else None
    if kind in ("scope6", "reservation6"):
        return "v6"
    if kind == "option6":
        prefix, _ = option6_target(fields.get("parent") or "server")
        return "v6" if prefix else None
    return None


def _waiting_children(source: list[Record], refused: dict[Key, str]) -> dict[Key, str]:
    """
    Objects whose parent cannot be pushed cannot be pushed either (a reservation of a refused scope, a value of a
    refused reservation, a failover relationship holding a refused scope...), however far down, whatever order the
    records come in.
    """
    out: dict[Key, str] = {}
    pending = [r for r in source if (r.kind, r.key) not in refused]
    grown = True
    while grown:
        grown = False
        rest = []
        for r in pending:
            parent = next((p for p in parents(r.kind, r.fields) if p in refused or p in out), None)
            if parent is None:
                rest.append(r)
                continue
            out[r.kind, r.key] = f"waits for {parent[0]} {parent[1]}, which cannot be pushed"
            grown = True
        pending = rest
    return out


def _pause_deletes(plan: Plan, changes: list[Change], refusals: set[Key]) -> list[Change]:
    """
    While anything in a family cannot be pushed, nothing in that family is deleted. A NetBox object has no lasting
    identity in the records (a scope is keyed by its network, a reservation by its address, a policy by its name), so
    an object moved or renamed looks like a new object plus a deleted one; when the new one cannot be pushed, deleting
    the old one would leave nothing on Windows. Rather than guess which deletes belong to which refusal, every delete
    of the family waits (shown with the reason) until nothing there is refused; creates and updates that do not need a
    refused object still go ahead. A failover relationship losing scopes waits too: leaving it removes the scopes from
    the partner.

    ``refusals`` are the NetBox objects refused for what they are (``plan``'s reasons, their children, a scope held by
    a relationship this push does not release, a processing order that would not fit even without the deletes). A
    policy refused only because the planned deletes close the gaps before it is not one: pausing the deletes would
    keep it refused for good.

    A create that could only succeed once a waiting delete is done (the same client's reservation in the scope, an
    exclusion range or a scope over the old one's addresses, a relationship taking a scope the waiting one still
    holds) waits as well, instead of failing at Windows on every push.
    """
    blocking: dict[str, list[Key]] = {}
    for s in plan.skipped:
        key = (s.change.kind, s.change.key)
        if s.change.action == DELETE or key not in refusals:
            continue
        fam = family(s.change.kind, s.change.after or s.change.before or {})
        if fam is not None and key not in blocking.setdefault(fam, []):
            blocking[fam].append(key)
    blocking = {fam: keys for fam, keys in blocking.items() if keys}

    def reason(fam: str) -> str:
        keys = blocking[fam]
        names = ", ".join(f"{k} {n}" for k, n in keys[:PAUSE_NAMES])
        if len(keys) > PAUSE_NAMES:
            names += f" and {len(keys) - PAUSE_NAMES} more"
        return (
            f"waiting: nothing in the {FAMILIES[fam]} is deleted while {names} cannot be pushed; fix them in NetBox "
            "and the next push deletes it"
        )

    out = []
    paused: dict[Key, Change] = {}
    for c in changes:
        fam = family(c.kind, c.before or c.after or {})
        key = (c.kind, c.key)
        dropped = c.action == UPDATE and c.kind == "failover" and _drops_scopes(c)
        if fam in blocking and (c.action == DELETE or dropped):
            plan.skipped.append(Skip(c, reason(fam), waiting=True))
            paused[key] = c
            continue
        # Deletes come children first: a parent outside the family (a class) holding a paused child stays too.
        held = [k for k in plan.children.get(key, []) if k in paused] if c.action == DELETE else []
        if held:
            reason_held = f"waiting: still holds {held[0][0]} {held[0][1]}, whose delete waits"
            plan.skipped.append(Skip(c, reason_held, waiting=True))
            paused[key] = c
            continue
        out.append(c)
    index = _PausedIndex(paused)
    final = []
    # Objects refused as creates (a new policy whose number does not fit): nothing can be written under them. A refused
    # update's object is on Windows already, so what hangs off it can still be written.
    late = {(s.change.kind, s.change.key) for s in plan.skipped if s.change.action == CREATE and not s.waiting}
    waiting: set[Key] = set()  # creates and updates held back here; what hangs off them waits with them
    for c in out:
        if c.action == DELETE:
            final.append(c)
            continue
        if (blocker := index.blocker(c)) is not None:
            why = f"waiting: needs {blocker[0]} {blocker[1]} gone from Windows first, and that delete waits"
        elif (parent := next((p for p in parents(c.kind, c.after or {}) if p in waiting), None)) is not None:
            why = f"waiting: needs {parent[0]} {parent[1]} first, which waits"
        elif (parent := next((p for p in parents(c.kind, c.after or {}) if p in late), None)) is not None:
            # A parent refused after the first pass (a policy whose processing order does not fit): not a wait.
            late.add((c.kind, c.key))
            plan.skipped.append(Skip(c, f"waits for {parent[0]} {parent[1]}, which cannot be pushed"))
            continue
        else:
            final.append(c)
            continue
        waiting.add((c.kind, c.key))
        plan.skipped.append(Skip(c, why, waiting=True))
    return final


class _PausedIndex:
    """The paused deletes and relationship updates, looked up by what a create or update could clash with."""

    def __init__(self, paused: Mapping[Key, Change]) -> None:
        # (first address, last address, key), in address order: the scopes on Windows never share addresses, so the
        # one starting last at or before a new scope's end is the only one that can overlap it.
        self.scopes: list[tuple[int, int, Key]] = []
        self.exclusions: defaultdict[str, list[tuple[Key, int, int]]] = defaultdict(list)
        self.clients: dict[tuple[str, ...], Key] = {}
        self.held_scopes: dict[str, Key] = {}
        for key, c in paused.items():
            before = c.before or {}
            if c.kind == "failover":
                # Paused, the relationship keeps every scope it has on Windows.
                for scope_id in before.get("scope_ids") or []:
                    self.held_scopes.setdefault(scope_id, key)
            elif c.action != DELETE:
                continue
            elif c.kind == "scope" and (n := _scope_network(Record("scope", c.key, before))) is not None:
                self.scopes.append((int(n.network_address), int(n.broadcast_address), key))
            elif c.kind == "exclusion":
                s, e = _ipv4(before.get("start_range")), _ipv4(before.get("end_range"))
                if s is not None and e is not None:
                    self.exclusions[str(before.get("scope_id"))].append((key, int(s), int(e)))
            elif c.kind in ("reservation", "reservation6"):
                record = Record(c.kind, c.key, before)
                if (client := _client_of(record)) is not None:
                    self.clients.setdefault((c.kind, _scope_of(record), *client), key)

        self.scopes.sort()
        self.starts = [first for first, _, _ in self.scopes]

    def blocker(self, change: Change) -> Key | None:
        """The paused change ``change`` would clash with on Windows, or None."""
        f = change.after or {}
        record = Record(change.kind, change.key, f)
        if change.kind == "scope" and change.action == CREATE:
            mine = _scope_network(record)
            if mine is None:
                return None
            i = bisect.bisect_right(self.starts, int(mine.broadcast_address)) - 1
            if i >= 0 and self.scopes[i][1] >= int(mine.network_address):
                return self.scopes[i][2]
            return None
        if change.kind == "exclusion":
            s, e = _ipv4(f.get("start_range")), _ipv4(f.get("end_range"))
            if s is None or e is None:
                return None
            ranges = self.exclusions.get(str(f.get("scope_id")), [])
            return next((k for k, s2, e2 in ranges if int(s) <= e2 and s2 <= int(e)), None)
        if change.kind in ("reservation", "reservation6") and change.action == CREATE:
            client = _client_of(record)
            return self.clients.get((change.kind, _scope_of(record), *client)) if client is not None else None
        if change.kind == "failover":
            new = set(f.get("scope_ids") or []) - set((change.before or {}).get("scope_ids") or [])
            return next(
                (
                    self.held_scopes[s]
                    for s in sorted(new)
                    if self.held_scopes.get(s, (None, change.key))[1] != change.key
                ),
                None,
            )
        return None


def _drops_scopes(change: Change) -> bool:
    before, after = (change.before or {}).get("scope_ids") or [], (change.after or {}).get("scope_ids") or []
    return bool(set(before) - set(after))


def _shared_orders(source: list[Record]) -> dict[Key, str]:
    """
    Policies sharing a processing order at one level. Windows keeps policies in a strict order and renumbers the
    others when one takes a used number, so both would show as changed on every push.
    """
    levels: dict[tuple[str, Any], list[Record]] = defaultdict(list)
    for r in source:
        if r.kind == "policy" and r.fields.get("processing_order") is not None:
            levels[r.fields["scope_id"], r.fields["processing_order"]].append(r)
    out = {}
    for (_, order), records in levels.items():
        if len(records) > 1:
            names = ", ".join(sorted(r.fields["name"] for r in records))
            for r in records:
                out[r.kind, r.key] = f"policies {names} share processing order {order}; give each its own in NetBox"
    return out


def _prepare(record: Record, target: Mapping[Key, Record]) -> Record:
    """Fill in what NetBox leaves to Windows, so it does not show up as a change on every push."""
    f = record.fields
    if record.kind == "reservation" and not f.get("name"):
        # Windows names a reservation after its IP when given no name.
        return Record(record.kind, record.key, {**f, "name": f["ip_address"]})
    if record.kind == "reservation6" and not f.get("name"):
        return Record(record.kind, record.key, {**f, "name": f["ip_address"]})
    if record.kind == "scope6":
        return _prepare_scope6(record, target.get((record.kind, record.key)))
    if record.kind == "scope" and not f.get("name"):
        # Windows requires a scope name: use the scope ID, as for an unnamed reservation.
        record = Record(record.kind, record.key, {**f, "name": record.key})
        f = record.fields
    current = target.get((record.kind, record.key))
    if record.kind == "policy" and f.get("lease_seconds") is None and current:
        # No lease time in NetBox: keep the one Windows has (Set cannot clear it anyway).
        return Record(record.kind, record.key, {**f, "lease_seconds": current.fields["lease_seconds"]})
    if record.kind == "failover":
        # Settings left empty in NetBox keep what Windows has, or get Windows' own defaults.
        defaults = current.fields if current else FAILOVER_DEFAULTS
        filled = {k: defaults.get(k, FAILOVER_DEFAULTS[k]) for k in FAILOVER_DEFAULTS if f.get(k) is None}
        if f["mode"] == "hotstandby":
            filled.pop("load_balance_percent", None)
        else:
            filled.pop("reserve_percent", None)
        return Record(record.kind, record.key, {**f, **filled}) if filled else record
    if record.kind == "scope" and f.get("lease_seconds") is None:
        # No lease time in NetBox: keep the one Windows has (None when it could not be read, which the writer then
        # leaves alone), or send Windows' own default for a new scope.
        lease = current.fields["lease_seconds"] if current else DEFAULT_LEASE_SECONDS
        return Record(record.kind, record.key, {**f, "lease_seconds": lease})
    return record


def v6_timers(preferred: int) -> dict[str, int]:
    """Renewal (T1) and rebinding (T2) times for a preferred lifetime: half and 80 %, as Windows' defaults are."""
    return {"t1": preferred // 2, "t2": preferred * 4 // 5}


def _derive_timers(f: Mapping[str, int | None], missing: list[str], preferred: int) -> dict[str, int]:
    """
    Values for the empty timers in ``missing`` that keep T1 < T2 < preferred lifetime. With both empty: Windows' half
    and 80 %. With one set in NetBox, the empty one is worked out from it, since the default for the empty one alone
    could land on the wrong side of it (a T2 of 1 day with a default T1 of 4 days, say).
    """
    derived = v6_timers(preferred)
    if len(missing) == 2:
        return derived
    if missing == ["t1"]:
        # Renewal stays at half the preferred lifetime when that is still before T2, otherwise at the same 5/8 of T2
        # that Windows' defaults keep between T1 and T2.
        t2 = f["t2"]
        return {"t1": derived["t1"] if derived["t1"] < t2 else t2 * 5 // 8}
    t1 = f["t1"]
    t2 = derived["t2"]
    if t2 <= t1:
        # Rebinding goes halfway between the renewal time NetBox sets and the preferred lifetime.
        t2 = t1 + (preferred - t1) // 2
    return {"t2": t2}


def _prepare_scope6(record: Record, current: Record | None) -> Record:
    """
    Lifetimes left empty in NetBox keep what Windows has, or get Windows' defaults; a name is required. T1 and T2 left
    empty follow the preferred lifetime: a new scope gets half and 80 % of it (4 and 6.4 days for the default 8), and
    an existing one keeps its Windows values unless they no longer fit with the preferred lifetime and the timer
    NetBox sets. When NetBox sets only one of T1 and T2, the other is worked out so that T1 < T2 < preferred lifetime
    holds. A timer NetBox sets is never changed: if it leaves no room (T2 past the preferred lifetime, say), Windows
    refuses the scope and the push reports its message.
    """
    f = dict(record.fields)
    if not f.get("name"):
        f["name"] = f["prefix"]
    for name in ("preferred_lifetime", "valid_lifetime"):
        if f.get(name) is None:
            f[name] = current.fields.get(name) if current else V6_LIFETIME_DEFAULTS[name]
    missing = [name for name in ("t1", "t2") if f.get(name) is None]
    if not missing:
        return Record(record.kind, record.key, f)
    for name in missing:
        f[name] = current.fields.get(name) if current else None
    preferred = f["preferred_lifetime"]
    # A preferred lifetime Windows did not report (None) gives nothing to work from: keep the Windows timers.
    if preferred is None:
        return Record(record.kind, record.key, f)
    t1, t2 = f.get("t1"), f.get("t2")
    # A timer Windows did not report (None) is left alone on an existing scope, as the writer does with any None.
    if current is None or (t1 is not None and t2 is not None and not t1 < t2 < preferred):
        # A new scope, or Windows' timers no longer fit (the preferred lifetime or the other timer changed in
        # NetBox): work out the empty ones instead.
        f.update(_derive_timers(f, missing, preferred))
    return Record(record.kind, record.key, f)


def plan(
    source: Iterable[Record],
    target: Iterable[Record],
    managed: Mapping[Key, str],
    hidden: Iterable[Record] = (),
    defined_options: Iterable[int] | None = None,
    skipped_option_ids: Iterable[int] = SKIPPED_OPTION_IDS,
) -> Plan:
    """
    Work out what to change on Windows (``target``) so it matches NetBox (``source``).

    ``hidden`` are Windows objects the push does not handle (see ``windows_state.hidden_records``): they only stop
    their parents from being deleted. ``defined_options`` are the option definition keys (``windows_state
    .optiondef_key``) Windows has; values of other options are not pushed unless NetBox also pushes a definition.
    None skips that check.
    """
    result = Plan()
    target_index = index(with_option_types(target))
    source = with_option_types(source, fallback=target_index.values())
    defined = None
    if defined_options is not None:
        defined = {str(c) for c in defined_options} | {r.key for r in source if r.kind == "optiondef"}
    skipped_ids = set(skipped_option_ids)
    orders = _shared_orders(source)
    conflicts = _conflicts(source, target_index, managed)
    policies = {r.key for r in source if r.kind == "policy"}
    refused: dict[Key, str] = {}
    for record in source:
        reason = (
            _unpushable(record, skipped_ids, defined, target_index.get((record.kind, record.key)))
            or orders.get((record.kind, record.key))
            or _missing_policy(record, policies)
            or conflicts.get((record.kind, record.key))
        )
        if reason is not None:
            refused[record.kind, record.key] = reason
    refused.update(_waiting_children(source, refused))
    pushable = []
    for record in source:
        reason = refused.get((record.kind, record.key))
        if reason is None:
            pushable.append(_prepare(record, target_index))
        else:
            result.skipped.append(Skip(Change(CREATE, record.kind, record.key, after=dict(record.fields)), reason))
    source_index = index(pushable)

    for record in [*target_index.values(), *hidden]:
        for parent in parents(record.kind, record.fields):
            result.children.setdefault(parent, []).append((record.kind, record.key))

    # A NetBox object that cannot be pushed still says "keep this": never delete its Windows counterpart.
    held = {(s.change.kind, s.change.key) for s in result.skipped}
    changes = diff(pushable, [r for k, r in target_index.items() if k not in held], equal_records=records_equal)
    changed = {(c.kind, c.key) for c in changes}
    changes = _protect_memberships(changes, managed, source_index, target_index, result)
    membership_changes = {(c.kind, c.key): c for c in changes if (c.kind, c.key) in result.protected}
    for key in result.protected:
        fields = membership_changes[key].after if key in membership_changes else target_index[key].fields
        source_index[key] = Record(*key, fields)
    for key in source_index:
        if key in target_index and key not in changed:
            result.in_sync[key] = fingerprint(target_index[key])
    result.stale = {k for k in managed if k not in source_index and k not in target_index}

    # Deletes come first, children before parents, so a skipped child is known before its parent is looked at.
    deleting = {(c.kind, c.key) for c in changes if c.action == DELETE}
    for change in changes:
        key = (change.kind, change.key)
        if change.action != DELETE:
            result.changes.append(change)
            continue
        if key not in managed:
            result.unmanaged.append(key)
            deleting.discard(key)
            continue
        if not unchanged(managed[key], target_index[key]):
            result.skipped.append(Skip(change, DRIFTED))
            deleting.discard(key)
            continue
        kept = [c for c in result.children.get(key, []) if c not in deleting and not _released(c, key, source_index)]
        if kept:
            kind, name = kept[0]
            result.skipped.append(
                Skip(
                    change,
                    f"still holds {len(kept)} object(s) not managed by NetBox or kept on Windows (for example "
                    f"{kind} {name}), which removing it would remove too; not deleted",
                )
            )
            deleting.discard(key)
            continue
        result.changes.append(change)
    already = len(result.skipped)
    result.changes = _scopes_in_other_relationships(result.changes, target_index, result)
    refusals = set(refused) | {(s.change.kind, s.change.key) for s in result.skipped[already:]}
    result.changes = _membership_updates_first(result.changes, target_index)
    result.changes = _order_policies(result.changes, source_index, target_index, result)
    result.changes = _pause_deletes(result, result.changes, refusals | result.order_refusals)
    # What waits comes first, so the capped summary and job log always show it.
    result.skipped.sort(key=lambda s: not s.waiting)
    return result


def _missing_policy(record: Record, policies: set[str]) -> str | None:
    """A policy value names a policy by name only: without that policy in NetBox it would land in a hand-made one."""
    name = record.fields.get("policy") if record.kind == "option" else None
    if not name:
        return None
    scope_id, _ = option_target(record.fields["parent"])
    if policy_key(scope_id, name) in policies:
        return None
    level = f"scope {scope_id}" if scope_id else "the server"
    return f"policy {name} is not in NetBox at the level of this value ({level}); add it, or remove the value"


def _protect_memberships(
    changes: list[Change],
    managed: Mapping[Key, str],
    source: Mapping[Key, Record],
    target: Mapping[Key, Record],
    plan: Plan,
) -> list[Change]:
    """
    Removing a scope from a failover relationship removes it from the partner server. Only do that for scopes NetBox
    manages, and only when the relationship is unchanged on Windows since the last push: a scope added by hand stays.
    """
    out = []
    for c in changes:
        if c.action != UPDATE or c.kind != "failover":
            out.append(c)
            continue
        key = (c.kind, c.key)
        before, after = (c.before or {}).get("scope_ids", []), (c.after or {}).get("scope_ids", [])
        owned = _owned_scope_ids(managed.get(key), target[key], after)
        protected = [
            s
            for s in before
            if s not in after
            and (owned is None or s not in owned or (("scope", s) not in managed and ("scope", s) not in source))
        ]
        if not protected:
            out.append(c)
            continue
        plan.skipped.append(
            Skip(
                c,
                f"scope(s) {', '.join(protected)} stay in the relationship: they were added on Windows, or are not "
                "managed by NetBox, and leaving it would remove them from the partner",
            )
        )
        fields = {**(c.after or {}), "scope_ids": windows_scope_ids([*after, *protected])}
        # Record the relationship without the scopes NetBox did not put there, so they stay protected next time
        # while NetBox can still take out the ones it added. Unknown ownership counts every kept scope as hand-added.
        hand_added = {s for s in protected if owned is None or s not in owned}
        plan.protected[key] = hand_added
        if records_equal(c.kind, fields, c.before or {}):
            # Nothing else to change: Windows already holds this, so it can be recorded now.
            plan.in_sync[key] = _fingerprint_without(Change(c.action, c.kind, c.key, c.before, fields), hand_added)
            continue
        out.append(Change(c.action, c.kind, c.key, before=c.before, after=fields))
    return out


def _fingerprint_without(change: Change, hand_added: set[str]) -> str:
    """The fingerprint of a failover relationship as ``change`` leaves it on Windows, minus scopes added by hand."""
    fields = _as_on_windows(change)
    fields["scope_ids"] = [s for s in fields.get("scope_ids") or [] if s not in hand_added]
    return fingerprint(Record(change.kind, change.key, fields))


def _owned_scope_ids(stored: str | None, record: Record, wanted: Iterable[str]) -> set[str] | None:
    """
    The scopes of a Windows failover relationship that NetBox put there, or None when the recorded fingerprint cannot
    tell (never pushed, or changed on Windows in some other way). The fingerprint covers the relationship as NetBox
    left it: scopes NetBox still wants plus some of those it no longer wants; anything else was added by hand.
    """
    if stored is None:
        return None
    on_windows = list(record.fields.get("scope_ids") or [])
    wanted = set(wanted)
    kept = [s for s in on_windows if s in wanted]
    extra = [s for s in on_windows if s not in wanted]
    if len(extra) > OWNED_SEARCH_LIMIT:
        return set(on_windows) if unchanged(stored, record) else None
    for size in range(len(extra), -1, -1):
        for subset in itertools.combinations(extra, size):
            ids = windows_scope_ids([*kept, *subset])
            if unchanged(stored, Record(record.kind, record.key, {**record.fields, "scope_ids": ids})):
                return set(ids)
    return None


def _scopes_in_other_relationships(changes: list[Change], target: Mapping[Key, Record], plan: Plan) -> list[Change]:
    """
    Windows puts a scope in one failover relationship at most, and refuses to add it to a second one. A relationship
    NetBox does not know about (made by hand, or renamed on Windows) would make every push fail on it: leave such a
    create or update out with a reason instead, unless this push deletes the other relationship first, or updates it
    without that scope (a simple move from one relationship to another in NetBox): then the change that lets the scope
    go is put before the ones taking it, and ``plan.moves`` records the move so a taker failing at Windows can put the
    scope back (see execute).

    Only a release this push really makes counts: a refused update releases nothing, so its takers are refused too.
    Moves that go round in a circle (two relationships swapping scopes) cannot be done one change at a time, since each
    taker needs the other's release first; they are refused, asking for one scope at a time.
    """
    deleted = {c.key for c in changes if c.kind == "failover" and c.action == DELETE}
    holder: dict[str, tuple[str, str]] = {}  # scope ID -> (name, key) of the relationship holding it on Windows
    for (kind, key), record in target.items():
        if kind == "failover" and key not in deleted:
            for scope_id in record.fields.get("scope_ids") or []:
                holder[scope_id] = (record.fields.get("name") or key, key)
    upserts = [c for c in changes if c.kind == "failover" and c.action != DELETE]
    refused: dict[str, str] = {}
    while True:
        released = {
            c.key: set((c.before or {}).get("scope_ids") or []) - set((c.after or {}).get("scope_ids") or [])
            for c in upserts
            if c.action == UPDATE and c.key not in refused
        }
        deps: defaultdict[str, set[str]] = defaultdict(set)  # taker -> relationships whose release it needs
        moves: defaultdict[str, list[tuple[str, str, str]]] = defaultdict(list)
        grown = False
        for c in upserts:
            if c.key in refused:
                continue
            new = [
                s
                for s in (c.after or {}).get("scope_ids") or []
                if s not in set((c.before or {}).get("scope_ids") or []) and s in holder and holder[s][1] != c.key
            ]
            taken = [s for s in new if s not in released.get(holder[s][1], ())]
            if taken:
                others = ", ".join(sorted({holder[s][0] for s in taken}))
                refused[c.key] = (
                    f"scope(s) {', '.join(taken)} are already in failover relationship {others} on Windows, which a "
                    "scope can only be in once; import that relationship into NetBox, or remove the scopes from it "
                    "on Windows"
                )
                grown = True
                continue
            for s in new:
                deps[c.key].add(holder[s][1])
                moves[c.key].append((holder[s][1], holder[s][0], s))
        for key in list(deps):
            if _in_cycle(key, deps):
                names = sorted({key, *deps[key]})
                refused[key] = (
                    f"scopes move both ways between failover relationships {', '.join(names)}; Windows holds a scope "
                    "in one relationship at a time, so move one scope at a time: remove it from one relationship in "
                    "NetBox, push, then add it to the other"
                )
                grown = True
        if not grown:
            break
    plan.moves = {k: v for k, v in moves.items() if v}
    out = []
    for c in changes:
        if c.kind == "failover" and c.action != DELETE and c.key in refused:
            plan.skipped.append(Skip(c, refused[c.key]))
            continue
        out.append(c)
    # Each relationship comes after the ones whose release it needs (a chain of moves, whatever the names), in the
    # places the relationship creates and updates already hold. Cycles were refused above.
    slots = [i for i, c in enumerate(out) if c.kind == "failover" and c.action != DELETE]
    pending = [out[i] for i in slots]
    ordered: list[Change] = []
    done: set[str] = set()
    while pending:
        ready = [c for c in pending if not (deps.get(c.key, set()) & {p.key for p in pending}) - done]
        if not ready:  # cannot happen once cycles are refused; keep the rest as they are
            ready = pending
        ordered.extend(ready)
        done.update(c.key for c in ready)
        pending = [c for c in pending if c.key not in done]
    for i, c in zip(slots, ordered, strict=True):
        out[i] = c
    return out


def _in_cycle(start: str, deps: Mapping[str, set[str]]) -> bool:
    """Whether ``start`` can reach itself through ``deps``."""
    seen: set[str] = set()
    stack = list(deps.get(start, ()))
    while stack:
        node = stack.pop()
        if node == start:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(deps.get(node, ()))
    return False


def _membership_updates_first(changes: list[Change], target: Mapping[Key, Record]) -> list[Change]:
    """
    A scope leaving a superscope or failover relationship and being deleted: the membership update goes before the
    deletes. Windows has no empty superscope (removing its last scope deletes it), so it must first gain the scopes
    NetBox now lists, which only works when those already exist on Windows (otherwise the usual order stands, and the
    next push finishes the job). A failover relationship may be left empty, but the same order keeps it simple.
    """
    deleted = {c.key for c in changes if c.action == DELETE and c.kind == "scope"}
    first = []
    for c in changes:
        if c.action != UPDATE or c.kind not in MEMBERSHIPS:
            continue
        before, after = set((c.before or {}).get("scope_ids", [])), set((c.after or {}).get("scope_ids", []))
        if (before - after) & deleted and all(("scope", s) in target for s in after - before):
            first.append(c)
    return first + [c for c in changes if c not in first] if first else changes


def _policy_level(fields: Mapping[str, Any]) -> str:
    return fields.get("scope_id") or ""


def _order_policies(
    changes: list[Change], source: Mapping[Key, Record], target: Mapping[Key, Record], plan: Plan
) -> list[Change]:
    """
    Policy creates and updates in an order Windows accepts. Windows numbers the policies of each level (the server,
    or one scope) 1, 2, 3... without gaps: a policy given a used number moves the others down the list, and a number
    past the end is refused (DHCP 20110, seen when creating order 2 on an empty scope). So, level by level, policies
    are set in ascending processing order, whatever their names. Following what Windows does at each step also shows
    which policies NetBox did not change get moved down by an insert: those are set back in the same push.
    """
    upserts = {c.key: c for c in changes if c.kind == "policy" and c.action != DELETE}
    deleted = {c.key for c in changes if c.kind == "policy" and c.action == DELETE}
    levels = {_policy_level(c.after or {}) for c in upserts.values()}
    levels |= {_policy_level(c.before or {}) for c in changes if c.kind == "policy" and c.action == DELETE}
    if not levels:
        return changes
    # Grouped once: filtering every record again for each level made a plan with policy changes in many scopes take
    # time growing with scopes x records.
    windows, wanted = _policies_by_level(target), _policies_by_level(source)
    level_changes: defaultdict[str, list[Change]] = defaultdict(list)
    for c in upserts.values():
        level_changes[_policy_level(c.after or {})].append(c)
    ordered = [
        c
        for level in sorted(levels)
        for c in _order_level(level, upserts, deleted, wanted[level], windows[level], level_changes[level], plan)
    ]
    rest = [c for c in changes if not (c.kind == "policy" and c.action != DELETE)]
    # Back where the diff put policies: after the deletes, before the kinds that come after them.
    last_delete = max((i for i, c in enumerate(rest) if c.action == DELETE), default=-1)
    rank = KIND_ORDER.index("policy")
    at = next((i for i in range(last_delete + 1, len(rest)) if _kind_rank(rest[i].kind) > rank), len(rest))
    return rest[:at] + ordered + rest[at:]


def _policies_by_level(records: Mapping[Key, Record]) -> defaultdict[str, list[Record]]:
    """The policies among ``records``, by level, each level in processing order (policies without a number last)."""
    out: defaultdict[str, list[Record]] = defaultdict(list)
    for (kind, _), r in records.items():
        if kind == "policy":
            out[_policy_level(r.fields)].append(r)
    for policies in out.values():
        policies.sort(
            key=lambda r: (r.fields.get("processing_order") is None, r.fields.get("processing_order") or 0, r.key)
        )
    return out


def _kind_rank(kind: str) -> int:
    return KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)


def _order_level(
    level: str,
    upserts: Mapping[str, Change],
    deleted: set[str],
    wanted: list[Record],
    windows: list[Record],
    level_changes: list[Change],
    plan: Plan,
) -> list[Change]:
    """
    The policy creates and updates of one level, as ``_order_policies`` describes. ``wanted`` (NetBox) and ``windows``
    are the policies of that level in processing order, ``level_changes`` its creates and updates.
    """
    if [r.fields.get("processing_order") for r in windows] != list(range(1, len(windows) + 1)):
        # Not numbered the way this models Windows (it never should be): only put the changes in ascending order.
        return sorted(level_changes, key=lambda c: ((c.after or {}).get("processing_order") or 0, c.key))
    # Deletes run first, and Windows closes the gap a removed policy leaves.
    # Remove-DhcpServerv4Policy closes the gap, and Set-DhcpServerv4Policy -ProcessingOrder moves the others down (and
    # refuses a number past the end) the way Add does (lab, Windows Server 2022).
    current = [r.key for r in windows if r.key not in deleted]
    # A new policy (a new name: a rename's new policy too) refused for its number holds the deletes back (see
    # _pause_deletes), since one of them may be what it replaces; unless the number only fails to fit because of
    # those deletes, and fits among the policies NetBox holds at the level. A policy already on Windows refused for
    # its number never does: it stays on Windows whatever happens, so no delete can be its replacement, and holding
    # the deletes back would keep a NetBox that did not renumber after a delete stuck for good.
    removed = len(windows) - len(current)

    def never_fits(order: int, room: int) -> bool:
        return order > room + removed or order > len(wanted)

    where = f"scope {level}" if level else "the server level"
    out: list[Change] = []
    restored: list[str] = []  # policies NetBox did not change, set back after an insert moved them down
    stuck: list[Skip] = []  # policies already on Windows whose number does not fit
    for record in wanted:
        key, order = record.key, record.fields.get("processing_order")
        change = upserts.get(key)
        if key in current:
            pos = current.index(key) + 1
            if order is None:
                order = pos  # No number: Set leaves it where it is.
            if order == pos and change is None:
                continue
            if change is None:
                # NetBox did not change it, but an insert above it moved it down: set it back. Everything else
                # already matches, so the change only shows the processing order.
                before = {**record.fields, "processing_order": pos}
                change = Change(UPDATE, "policy", key, before=before, after=dict(record.fields))
                restored.append(key)
            if order > len(current):
                stuck.append(Skip(change, _past_the_end(order, where, len(current))))
                continue
            current.remove(key)
            current.insert(order - 1, key)
        elif change is None:
            continue
        elif order is None:
            current.append(key)  # Windows puts it at the end.
        elif order > len(current) + 1:
            # A new policy that does not fit is simply not created: the plan carries on as if it did not exist.
            plan.skipped.append(Skip(change, _past_the_end(order, where, len(current) + 1)))
            if never_fits(order, len(current) + 1):
                plan.order_refusals.add(("policy", key))
            continue
        else:
            current.insert(order - 1, key)
        out.append(change)
    if stuck:
        # A policy already on Windows cannot take its number (typically: a policy before it was deleted in NetBox,
        # which does not renumber the others, while Windows closed the gap). Moving the others without it would
        # change the order clients are matched in (deleting order 1 of 3 and setting 2 back to 2 puts it below 3),
        # so nothing at this level is created or updated until NetBox numbers its policies 1, 2, 3... again.
        plan.skipped.extend(stuck)
        names = ", ".join((s.change.after or {}).get("name") or s.change.key for s in stuck)
        reason = (
            f"not pushed with the other policies at {where}: the processing order of {names} is past the end, and "
            "applying the rest alone would reorder the policies Windows holds; renumber the policies in NetBox"
        )
        plan.skipped.extend(Skip(c, reason) for c in out if c.key in upserts)
        # New policies held back with them are refused creates: they hold the deletes back.
        plan.order_refusals.update(("policy", c.key) for c in out if c.key in upserts and c.action == CREATE)
        return []
    for key in restored:
        plan.in_sync.pop(("policy", key), None)
    return out


def _past_the_end(order: int, where: str, highest: int) -> str:
    return (
        f"processing order {order} is past the end: Windows numbers the policies at {where} 1, 2, 3... without gaps "
        f"and would accept at most {highest} here (DHCP 20110); renumber the policies in NetBox"
    )


def _released(child: Key, parent: Key, source: Mapping[Key, Record]) -> bool:
    """A superscope or failover relationship lets a scope go when NetBox still holds it, without that scope."""
    wanted = source.get(child)
    return (
        child[0] in MEMBERSHIPS
        and parent[0] == "scope"
        and wanted is not None
        and parent[1] not in wanted.fields["scope_ids"]
    )


# -- execution -----------------------------------------------------------------------------------------------------


def execute(plan: Plan, writer: Any) -> Result:
    """Apply a plan, one change at a time. Errors on one object do not stop the others; a lost connection does."""
    result = Result(upserts=dict(plan.in_sync), removals=set(plan.stale))
    # Objects that could not be created (children cannot follow) or deleted (parents must stay).
    missing: set[Key] = {(s.change.kind, s.change.key) for s in plan.skipped if s.change.action == CREATE}
    kept: set[Key] = {(s.change.kind, s.change.key) for s in plan.skipped if s.change.action == DELETE}
    kept.update(plan.unmanaged)
    children: defaultdict[Key, list[Key]] = defaultdict(list, plan.children)

    def block(change: Change) -> None:
        # Failed membership updates have not safely released their scopes: their deletes must wait too.
        if change.action == DELETE or (change.action == UPDATE and change.kind in MEMBERSHIPS):
            kept.add((change.kind, change.key))
        elif change.action == CREATE:
            missing.add((change.kind, change.key))

    # Scopes whose delete comes later in the plan, and those refused ahead of time because they still have leases.
    pending = {c.key for c in plan.changes if c.action == DELETE and c.kind == "scope"}
    refused: dict[str, str] = {}
    released: dict[str, Change] = {}  # relationships whose update ran in this push, as written

    def put_back(change: Change, entries: list[tuple[Change, str]], usable: bool = True) -> None:
        """
        A relationship taking scopes another one let go in this push failed: put the scopes back where they were, so
        they do not end up in no relationship at all (best effort; the message says how it went). Without a usable
        connection (lost, or the push stopped on an unexpected error) nothing more is sent: the message says where
        the scopes are.
        """
        if change.kind != "failover" or not entries or entries[-1][0] is not change:
            return
        back: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
        for releaser, name, scope_id in plan.moves.get(change.key, []):
            if releaser in released:
                back[releaser, name].append(scope_id)
        taker = (change.after or {}).get("name") or change.key
        notes = []
        for (releaser, name), scope_ids in back.items():
            scopes = ", ".join(scope_ids)
            try:
                if not usable:
                    raise RuntimeError("the push stopped before it could")
                writer.restore_failover_scopes(name, scope_ids)
            except Exception as exc:  # noqa: BLE001 - reported with the failure it follows
                notes.append(
                    f"putting scope(s) {scopes} back into failover {name} failed too ({exc}): they are in no failover "
                    f"relationship now, and the next push adds them to {taker}"
                )
                continue
            # Record the relationship as Windows now holds it: as this push wrote it, plus the scopes put back. Its
            # other changes stay written, and the next push finds it in step and tries the move again.
            written = released[releaser]
            after = {**(written.after or {})}
            after["scope_ids"] = windows_scope_ids([*(after.get("scope_ids") or []), *scope_ids])
            again = Change(written.action, written.kind, written.key, written.before, after)
            released[releaser] = again
            rkey = ("failover", releaser)
            if rkey in plan.protected:
                result.upserts[rkey] = _fingerprint_without(again, plan.protected[rkey])
            else:
                result.upserts[rkey] = fingerprint(Record(again.kind, again.key, _as_on_windows(again)))
            notes.append(f"scope(s) {scopes} put back into failover {name}")
        if notes:
            entries[-1] = (change, f"{entries[-1][1]}; {'; '.join(notes)}")

    stopped = ""
    for change in plan.changes:
        key = (change.kind, change.key)
        if result.aborted:
            result.skipped.append((change, f"not attempted: {stopped}"))
            put_back(change, result.skipped, usable=False)
            continue
        if change.action == DELETE and change.kind == "scope":
            pending.discard(change.key)
            if change.key in refused:
                result.failed.append((change, refused[change.key]))
                block(change)
                continue
        if change.action == DELETE:
            blocked = [c for c in children[key] if c in kept]
        else:
            blocked = [p for p in parents(change.kind, change.after or {}) if p in missing]
        if blocked:
            kind, name = blocked[0]
            verb = "kept" if change.action == DELETE else "not created"
            result.skipped.append((change, f"not attempted: {kind} {name} was {verb}"))
            block(change)
            put_back(change, result.skipped)
            continue
        try:
            if change.action == UPDATE and change.kind == "failover":
                change = _keep_undeleted_scopes(change, kept, pending, refused, writer)
            writer.apply(change)
        except NotFound:
            if change.action != DELETE:
                result.failed.append((change, "not found on Windows"))
                block(change)
                put_back(change, result.failed)
                continue
            # Already gone: the delete is done.
        except RunnerError as exc:
            result.failed.append((change, str(exc)))
            result.maybe_written.append(change)
            result.aborted, stopped = True, "the connection to the server failed"
            block(change)
            put_back(change, result.failed, usable=False)
            continue
        except (PushRefused, WindowsLayerError, ValueError) as exc:
            result.failed.append((change, str(exc)))
            # An error from Windows can come after part of the change was made (a policy created, then its address
            # range refused). A refusal of ours and a bad value come before any write, unless the refusal says so.
            if getattr(exc, "wrote", False) or isinstance(exc, WindowsLayerError):
                result.maybe_written.append(change)
            block(change)
            put_back(change, result.failed)
            continue
        except Exception as exc:
            # A bug, or an answer from Windows nothing here expected. Stop, but keep the record of what was already
            # written: without it the caller could not save which objects NetBox now manages, nor which scopes still
            # need replicating to a failover partner. Only the type goes in the run (the message could hold
            # anything); the traceback goes to the log. This also catches the job timeout of the queue worker (an
            # ordinary exception there, raised once): on purpose, since what follows an aborted push is only local
            # bookkeeping, with no further call to the server.
            log.exception("Unexpected error applying %s %s %s", change.action, change.kind, change.key)
            result.failed.append((change, f"unexpected error ({type(exc).__name__}), see the worker log"))
            result.maybe_written.append(change)
            result.aborted, stopped = True, "the push stopped on an unexpected error"
            block(change)
            put_back(change, result.failed, usable=False)
            continue
        result.applied.append(change)
        if change.kind == "failover" and change.action == UPDATE:
            released[change.key] = change
        if change.action == DELETE:
            result.removals.add(key)
            result.upserts.pop(key, None)
        elif key in plan.protected:
            # From the change as written: scopes kept back while it ran (still holding leases) are NetBox's too.
            result.upserts[key] = _fingerprint_without(change, plan.protected[key])
        else:
            result.upserts[key] = fingerprint(Record(change.kind, change.key, _as_on_windows(change)))
    return result


def _as_on_windows(change: Change) -> dict[str, Any]:
    """
    What Windows holds after ``change``, for its fingerprint. A failover relationship without a shared secret profile
    leaves authentication as it was (``auth`` None in NetBox), so the fingerprint takes Windows' value: off for a new
    relationship, unchanged for an update. Otherwise a later delete would look like a change made on Windows.
    """
    fields = dict(change.after or {})
    if change.kind == "failover" and fields.get("auth") is None:
        fields["auth"] = bool((change.before or {}).get("auth"))
    return fields


def _keep_undeleted_scopes(
    change: Change, kept: set[Key], pending: set[str], refused: dict[str, str], writer: Any
) -> Change:
    """
    A scope leaves its failover relationship only if it is really deleted: one whose delete failed stays in, and so
    does one still to be deleted that has leases (Windows would refuse the delete with DHCP 20007, after the scope
    had already left the partner).
    """
    before, after = (change.before or {}).get("scope_ids", []), (change.after or {}).get("scope_ids", [])
    stay = []
    for sid in (s for s in before if s not in after):
        if ("scope", sid) in kept:
            stay.append(sid)
        elif sid in pending and getattr(writer, "scope_has_leases", None) and writer.scope_has_leases(sid):
            refused[sid] = (
                "the scope still has leases, so Windows would refuse to delete it (DHCP 20007); it stays in its "
                "failover relationship"
            )
            stay.append(sid)
    if not stay:
        return change
    # In address order, as Windows reports the list: the fingerprint recorded from this change is compared with what
    # Windows holds on the next push, and a list in another order would never match (the kept scope would then pass for
    # one added on Windows by hand, and stay in the relationship for good).
    scope_ids = windows_scope_ids([*after, *stay])
    return Change(change.action, change.kind, change.key, change.before, {**change.after, "scope_ids": scope_ids})
