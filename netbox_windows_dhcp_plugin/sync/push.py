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

import hashlib
import json
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
    class6_key,
    class_key,
    equal,
    ipv6,
    option6_target,
    optiondef_key,
    policy_key,
    reservation6_key,
    scope6_key,
)
from .windows_state import scope_ids as windows_scope_ids
from .windows_writer import PushRefused, option_target

__all__ = ("Plan", "Result", "Skip", "execute", "fingerprint", "parents", "plan")

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

# A dry run records at most this many planned changes in the sync run summary.
PLANNED_LIMIT = 1000

DRIFTED = (
    "changed on Windows since NetBox last pushed it, so it was not deleted; delete it on Windows by hand, or import "
    "it back into NetBox"
)
CONFLICT = "NetBox holds more than one object for this Windows object (or one without an address); fix it in NetBox"
LEASE_OR_MASK = (
    "option {code} is the {what} on Windows, which a push sets from the {source}; remove the option from NetBox"
)


@dataclass(frozen=True)
class Skip:
    change: Change
    reason: str


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
            out["not_pushed"] = [_skip_dict(s.change, s.reason) for s in self.skipped]
        return out


@dataclass
class Result:
    applied: list[Change] = field(default_factory=list)
    failed: list[tuple[Change, str]] = field(default_factory=list)
    skipped: list[tuple[Change, str]] = field(default_factory=list)
    # SyncState rows to write (key -> fingerprint) and to drop.
    upserts: dict[Key, str] = field(default_factory=dict)
    removals: set[Key] = field(default_factory=set)
    # True when the connection failed and the remaining changes were not attempted.
    aborted: bool = False

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "applied": len(self.applied),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
        }
        if self.failed:
            out["errors"] = [_skip_dict(c, msg) for c, msg in self.failed]
        if self.skipped:
            out["not_attempted"] = [_skip_dict(c, msg) for c, msg in self.skipped]
        return out


def _skip_dict(change: Change, reason: str) -> dict[str, str]:
    return {"action": change.action, "kind": change.kind, "key": change.key, "reason": reason}


# -- fingerprints --------------------------------------------------------------------------------------------------


def _canonical_value(v: Any) -> Any:
    try:
        return str(normalize.parse_option_number(v))
    except (ValueError, TypeError):
        pass
    # An IPv6 address in one spelling; anything else is returned as it is.
    return ipv6(v) if isinstance(v, str) else v


def fingerprint(record: Record) -> str:
    """
    A hash of what an object looks like, to notice later whether someone changed it on Windows. Option numbers are
    hashed in decimal, so 0x40 on Windows and 64 in NetBox give the same fingerprint, and policy conditions as Windows
    stores them. A policy's processing order is left out: Windows renumbers the policies of a level by itself whenever
    one is added, moved or removed, so the number is no sign that someone edited the policy.
    """
    return _fingerprint(record, keep_order=False)


def _fingerprint(record: Record, keep_order: bool) -> str:
    fields = dict(record.fields)
    if record.kind == "policy" and not keep_order:
        fields.pop("processing_order", None)
    for name in ("value", "default_value"):
        if isinstance(fields.get(name), list):
            fields[name] = [_canonical_value(v) for v in fields[name]]
    # Windows names are not case sensitive.
    for kind, name in NAME_FIELDS:
        if kind == record.kind and isinstance(fields.get(name), str):
            fields[name] = fields[name].lower()
    # Policy conditions as Windows stores them, so the NetBox spelling and the one read back hash the same. Fingerprints
    # recorded before this rule (0.1.0rc1) still match for conditions already in that form. One recorded from another
    # spelling (a MAC prefix "02-4e-42-ab*") no longer does: until the next push records it again, deleting that policy
    # in NetBox is reported as changed on Windows and skipped. It cannot be matched the old way, since the old hash was
    # of the NetBox spelling, which a deleted policy no longer has.
    if record.kind == "policy" and isinstance(fields.get("criteria"), dict):
        fields["criteria"] = canonical_criteria(fields["criteria"])
    data = json.dumps({"kind": record.kind, "key": record.key, "fields": fields}, sort_keys=True, default=str)
    return hashlib.sha256(data.encode()).hexdigest()


def unchanged(stored: str, record: Record) -> bool:
    """
    Whether ``record`` (as Windows has it now) still matches the fingerprint recorded for it. Policy fingerprints
    recorded before the processing order was left out (0.1.0rc1) included it: they still match while the policy keeps
    the number it had then. Every push records the new form for the policies it finds in step, so this only matters
    for a policy deleted in NetBox before any push since the upgrade.
    """
    if stored == fingerprint(record):
        return True
    return record.kind == "policy" and stored == _fingerprint(record, keep_order=True)


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


def _unpushable(record: Record, skipped_option_ids: set[int], defined: set[str] | None) -> str | None:
    """Why NetBox data cannot become a Windows object, or None."""
    f = record.fields
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
    if record.kind in MEMBERSHIPS and not f.get("scope_ids"):
        return f"a {record.kind} needs at least one scope on Windows; add one in NetBox, or delete it"
    if record.kind == "failover":
        if not f.get("partner_server"):
            return "the partner server has no Windows server (host) in NetBox; add one"
        if f["mode"] == "hotstandby" and not f.get("server_role"):
            return "a hot standby relationship needs the primary server's role (active or standby); set it in NetBox"
    if record.kind == "scope6" and f.get("prefix_length") != V6_PREFIX_LENGTH:
        return f"Windows DHCPv6 scopes are /{V6_PREFIX_LENGTH} prefixes; this scope is /{f.get('prefix_length')}"
    if record.kind == "reservation6":
        if not f.get("ip_address"):
            return "the reservation has no IPv6 address; add one in NetBox"
        if not f.get("client_duid"):
            return "the reservation has no DUID; add one in NetBox"
        if f.get("iaid") is None:
            return "the reservation has no IAID; set it on its Windows reservation in NetBox"
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
    target_index = index(target)
    source = list(source)
    defined = None
    if defined_options is not None:
        defined = {str(c) for c in defined_options} | {r.key for r in source if r.kind == "optiondef"}
    skipped_ids = set(skipped_option_ids)
    orders = _shared_orders(source)
    policies = {r.key for r in source if r.kind == "policy"}
    pushable = []
    for record in source:
        reason = (
            _unpushable(record, skipped_ids, defined)
            or orders.get((record.kind, record.key))
            or _missing_policy(record, policies)
        )
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
    changes = diff(pushable, [r for k, r in target_index.items() if k not in held], equal=equal)
    changes = _protect_memberships(changes, managed, source_index, target_index, result)
    changed = {(c.kind, c.key) for c in changes}
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
    result.changes = _membership_updates_first(result.changes, target_index)
    result.changes = _order_policies(result.changes, source_index, target_index, result)
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
        trusted = key in managed and unchanged(managed[key], target[key])
        before, after = (c.before or {}).get("scope_ids", []), (c.after or {}).get("scope_ids", [])
        protected = [
            s
            for s in before
            if s not in after and (not trusted or (("scope", s) not in managed and ("scope", s) not in source))
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
        if not diff([Record(c.kind, c.key, fields)], [Record(c.kind, c.key, c.before or {})], equal=equal):
            continue  # nothing else to change
        out.append(Change(c.action, c.kind, c.key, before=c.before, after=fields))
    return out


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
    ordered = [c for level in sorted(levels) for c in _order_level(level, upserts, deleted, source, target, plan)]
    rest = [c for c in changes if not (c.kind == "policy" and c.action != DELETE)]
    # Back where the diff put policies: after the deletes, before the kinds that come after them.
    last_delete = max((i for i, c in enumerate(rest) if c.action == DELETE), default=-1)
    rank = KIND_ORDER.index("policy")
    at = next((i for i in range(last_delete + 1, len(rest)) if _kind_rank(rest[i].kind) > rank), len(rest))
    return rest[:at] + ordered + rest[at:]


def _kind_rank(kind: str) -> int:
    return KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)


def _order_level(
    level: str,
    upserts: Mapping[str, Change],
    deleted: set[str],
    source: Mapping[Key, Record],
    target: Mapping[Key, Record],
    plan: Plan,
) -> list[Change]:
    """The policy creates and updates of one level, as ``_order_policies`` describes."""
    windows = sorted(
        (r for (kind, _), r in target.items() if kind == "policy" and _policy_level(r.fields) == level),
        key=lambda r: (r.fields.get("processing_order") is None, r.fields.get("processing_order") or 0, r.key),
    )
    wanted = sorted(
        (r for (kind, _), r in source.items() if kind == "policy" and _policy_level(r.fields) == level),
        key=lambda r: (r.fields.get("processing_order") is None, r.fields.get("processing_order") or 0, r.key),
    )
    level_changes = [c for c in upserts.values() if _policy_level(c.after or {}) == level]
    if [r.fields.get("processing_order") for r in windows] != list(range(1, len(windows) + 1)):
        # Not numbered the way this models Windows (it never should be): only put the changes in ascending order.
        return sorted(level_changes, key=lambda c: ((c.after or {}).get("processing_order") or 0, c.key))
    # Deletes run first, and Windows closes the gap a removed policy leaves.
    # Remove-DhcpServerv4Policy closes the gap, and Set-DhcpServerv4Policy -ProcessingOrder moves the others down (and
    # refuses a number past the end) the way Add does (lab, Windows Server 2022).
    current = [r.key for r in windows if r.key not in deleted]
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
        # An object that failed to update still exists, so its children may go ahead.
        if change.action == DELETE:
            kept.add((change.kind, change.key))
        elif change.action == CREATE:
            missing.add((change.kind, change.key))

    # Scopes whose delete comes later in the plan, and those refused ahead of time because they still have leases.
    pending = {c.key for c in plan.changes if c.action == DELETE and c.kind == "scope"}
    refused: dict[str, str] = {}

    for change in plan.changes:
        key = (change.kind, change.key)
        if change.action == DELETE and change.kind == "scope":
            pending.discard(change.key)
            if change.key in refused:
                result.failed.append((change, refused[change.key]))
                block(change)
                continue
        if change.action == UPDATE and change.kind == "failover":
            change = _keep_undeleted_scopes(change, kept, pending, refused, writer)
        if result.aborted:
            result.skipped.append((change, "not attempted: the connection to the server failed"))
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
            continue
        try:
            writer.apply(change)
        except NotFound:
            if change.action != DELETE:
                result.failed.append((change, "not found on Windows"))
                block(change)
                continue
            # Already gone: the delete is done.
        except RunnerError as exc:
            result.failed.append((change, str(exc)))
            result.aborted = True
            block(change)
            continue
        except (PushRefused, WindowsLayerError, ValueError) as exc:
            result.failed.append((change, str(exc)))
            block(change)
            continue
        result.applied.append(change)
        if change.action == DELETE:
            result.removals.add(key)
            result.upserts.pop(key, None)
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
    return Change(change.action, change.kind, change.key, change.before, {**change.after, "scope_ids": [*after, *stay]})
