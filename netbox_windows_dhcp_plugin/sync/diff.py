"""
Compare two views of the same DHCP configuration and list what must change for the target to match the source.

Pure Python: no Django and no Windows imports. Both sides are lists of ``Record``: a kind (scope, reservation...), a
key that identifies the object on Windows (a scope ID, scope ID + IP...) and a flat dict of plain values. The engine
does not care which side is Windows: import passes Windows as the source and NetBox as the target, push will pass
them the other way round.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

__all__ = ("KIND_ORDER", "Change", "Record", "diff", "index")

# Parents before children: a reservation needs its scope, an option value needs its definition, its parent, its
# class and its policy; a superscope or failover relationship needs its scopes. Creates and updates follow this order,
# deletes run in reverse.
KIND_ORDER = (
    "class",
    "class6",
    "optiondef",
    "scope",
    "scope6",
    "superscope",
    "exclusion",
    "exclusion6",
    "reservation",
    "reservation6",
    "policy",
    "option",
    "option6",
    "failover",
    "macfilter",
    "filterlists",
)

CREATE = "create"
UPDATE = "update"
DELETE = "delete"


@dataclass(frozen=True)
class Record:
    kind: str
    key: str
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Change:
    action: str
    kind: str
    key: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None

    @property
    def changed_fields(self) -> list[str]:
        before, after = self.before or {}, self.after or {}
        return sorted(k for k in before.keys() | after.keys() if before.get(k, _MISSING) != after.get(k, _MISSING))

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "kind": self.kind, "key": self.key, "before": self.before, "after": self.after}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Change:
        return cls(d["action"], d["kind"], d["key"], d.get("before"), d.get("after"))


_MISSING = object()

RecordsEqual = Callable[[str, dict[str, Any], dict[str, Any]], bool]


def index(records: Iterable[Record]) -> dict[tuple[str, str], Record]:
    """Index records by (kind, key). Two records with the same key would make the result ambiguous, so refuse."""
    out: dict[tuple[str, str], Record] = {}
    for r in records:
        if (r.kind, r.key) in out:
            raise ValueError(f"duplicate record {r.kind} {r.key}")
        out[(r.kind, r.key)] = r
    return out


def _rank(kind: str) -> int:
    return KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)


def diff(
    source: Iterable[Record],
    target: Iterable[Record],
    equal_records: RecordsEqual | None = None,
) -> list[Change]:
    """
    Return the changes that make ``target`` match ``source``: deletes first (children before parents), then creates
    and updates (parents before children). Two records are the same when their fields are equal (a field missing on
    one side is a difference); ``equal_records(kind, a, b)`` replaces that comparison when equality needs more, for
    example names Windows matches without case, or the data type of an option value.
    """
    src, dst = index(source), index(target)

    deletes = [Change(DELETE, kind, key, before=dict(dst[kind, key].fields)) for kind, key in dst.keys() - src.keys()]
    upserts = []
    for kind, key in src:
        new = src[kind, key].fields
        if (kind, key) not in dst:
            upserts.append(Change(CREATE, kind, key, after=dict(new)))
        elif not (equal_records(kind, new, dst[kind, key].fields) if equal_records else new == dst[kind, key].fields):
            upserts.append(Change(UPDATE, kind, key, before=dict(dst[kind, key].fields), after=dict(new)))

    deletes.sort(key=lambda c: (-_rank(c.kind), c.key))
    # Within a kind, updates before creates, so a rename frees a name before a new object takes it. A push then puts
    # policies in processing order (push._order_policies), which Windows requires.
    upserts.sort(key=lambda c: (_rank(c.kind), c.action != UPDATE, c.key))
    return deletes + upserts
