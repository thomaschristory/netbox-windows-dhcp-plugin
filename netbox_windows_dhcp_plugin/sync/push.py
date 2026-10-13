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
from .windows_state import equal
from .windows_writer import PushRefused, option_target

__all__ = ("Plan", "Result", "Skip", "execute", "fingerprint", "parents", "plan")

Key = tuple[str, str]

# What Windows gives a scope created without a lease duration (8 days), and what a push then sends.
DEFAULT_LEASE_SECONDS = 8 * 86400

# A dry run records at most this many planned changes in the sync run summary.
PLANNED_LIMIT = 1000

DRIFTED = (
    "changed on Windows since NetBox last pushed it, so it was not deleted; delete it on Windows by hand, or import "
    "it back into NetBox"
)
CONFLICT = "NetBox holds more than one object for this Windows object (or one without an address); fix it in NetBox"


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
        return v


def fingerprint(record: Record) -> str:
    """
    A hash of what an object looks like, to notice later whether someone changed it on Windows. Option numbers are
    hashed in decimal, so 0x40 on Windows and 64 in NetBox give the same fingerprint.
    """
    fields = dict(record.fields)
    for name in ("value", "default_value"):
        if isinstance(fields.get(name), list):
            fields[name] = [_canonical_value(v) for v in fields[name]]
    data = json.dumps({"kind": record.kind, "key": record.key, "fields": fields}, sort_keys=True, default=str)
    return hashlib.sha256(data.encode()).hexdigest()


# -- dependencies --------------------------------------------------------------------------------------------------


def parents(kind: str, fields: Mapping[str, Any]) -> list[Key]:
    """The objects this one needs on Windows (and that take it with them when removed)."""
    if kind in ("exclusion", "reservation"):
        return [("scope", fields["scope_id"])]
    if kind == "option":
        scope_id, ip = option_target(fields["parent"])
        out: list[Key] = [("optiondef", str(fields["option_id"]))]
        if scope_id:
            out.append(("scope", scope_id))
        if ip:
            out.append(("reservation", f"{scope_id}/{ip}"))
        if fields.get("user_class"):
            out.append(("class", f"user/{fields['user_class']}"))
        if fields.get("vendor_class"):
            out.append(("class", f"vendor/{fields['vendor_class']}"))
        return out
    return []


# -- planning ------------------------------------------------------------------------------------------------------


def _pushable(record: Record) -> bool:
    # netbox_state marks extra copies with "#<pk>", and a reservation without an address ends in "/": neither names
    # a Windows object.
    return "#" not in record.key and not record.key.endswith("/")


def _prepare(record: Record, target: Mapping[Key, Record]) -> Record:
    """Fill in what NetBox leaves to Windows, so it does not show up as a change on every push."""
    f = record.fields
    if record.kind == "reservation" and not f.get("name"):
        # Windows names a reservation after its IP when given no name.
        return Record(record.kind, record.key, {**f, "name": f["ip_address"]})
    if record.kind == "scope" and f.get("lease_seconds") is None:
        # No lease time in NetBox: keep the one Windows has, or send Windows' own default for a new scope.
        current = target.get((record.kind, record.key))
        lease = current.fields["lease_seconds"] if current else DEFAULT_LEASE_SECONDS
        return Record(record.kind, record.key, {**f, "lease_seconds": lease})
    return record


def plan(source: Iterable[Record], target: Iterable[Record], managed: Mapping[Key, str]) -> Plan:
    """Work out what to change on Windows (``target``) so it matches NetBox (``source``)."""
    result = Plan()
    target_index = index(target)
    pushable = []
    for record in source:
        if _pushable(record):
            pushable.append(_prepare(record, target_index))
        else:
            result.skipped.append(Skip(Change(CREATE, record.kind, record.key, after=dict(record.fields)), CONFLICT))
    source_index = index(pushable)

    for key, record in target_index.items():
        for parent in parents(record.kind, record.fields):
            result.children.setdefault(parent, []).append(key)

    changes = diff(pushable, target_index.values(), equal=equal)
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
        kept = [c for c in result.children.get(key, []) if c not in deleting]
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
    return result


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

    for change in plan.changes:
        key = (change.kind, change.key)
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
