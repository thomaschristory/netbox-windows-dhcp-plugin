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
from .diff import CREATE, DELETE, UPDATE, Change, Record, diff, index
from .windows_state import (
    NAME_FIELDS,
    SKIPPED_OPTION_IDS,
    V6_PREFIX_LENGTH,
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

# What Windows gives a DHCPv6 scope created without lifetimes.
# unverified: T1 and T2 defaults (4 days, and 6 days 10 hours, as the scope wizard shows).
V6_LIFETIME_DEFAULTS = {
    "preferred_lifetime": 8 * 86400,
    "valid_lifetime": 12 * 86400,
    "t1": 4 * 86400,
    "t2": 6 * 86400 + 10 * 3600,
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
    hashed in decimal, so 0x40 on Windows and 64 in NetBox give the same fingerprint.
    """
    fields = dict(record.fields)
    for name in ("value", "default_value"):
        if isinstance(fields.get(name), list):
            fields[name] = [_canonical_value(v) for v in fields[name]]
    # Windows names are not case sensitive.
    for kind, name in NAME_FIELDS:
        if kind == record.kind and isinstance(fields.get(name), str):
            fields[name] = fields[name].lower()
    data = json.dumps({"kind": record.kind, "key": record.key, "fields": fields}, sort_keys=True, default=str)
    return hashlib.sha256(data.encode()).hexdigest()


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
        return "the subnet has no pool (IP range) to use as the scope range; add one in NetBox"
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
        return f"Windows DHCPv6 scopes are /{V6_PREFIX_LENGTH} prefixes; this subnet is /{f.get('prefix_length')}"
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
            what, source = ("subnet mask", "prefix length") if code == 1 else ("lease time", "subnet's valid lifetime")
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


def _prepare_scope6(record: Record, current: Record | None) -> Record:
    """Lifetimes left empty in NetBox keep what Windows has, or get Windows' defaults; a name is required."""
    f = dict(record.fields)
    if not f.get("name"):
        f["name"] = f["prefix"]
    for name, default in V6_LIFETIME_DEFAULTS.items():
        if f.get(name) is None:
            f[name] = current.fields.get(name) if current else default
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
        if managed[key] != fingerprint(target_index[key]):
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
        trusted = key in managed and managed[key] == fingerprint(target[key])
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
    deletes. Windows refuses to remove the last scope of a relationship, so it must first gain the scopes NetBox now
    lists, which only works when those already exist on Windows (otherwise the usual order stands, and the next push
    finishes the job).
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
            result.upserts[key] = fingerprint(Record(change.kind, change.key, change.after or {}))
    return result


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
