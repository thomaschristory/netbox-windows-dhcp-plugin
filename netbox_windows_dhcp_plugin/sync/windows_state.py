"""
Read a Windows DHCP server through the client and turn the result into sync records.

Pure Python (no Django). Field values use the plugin's own spellings (lowercase enum values, lease time in seconds,
client IDs as aa-bb-cc-...), so the NetBox side can produce the very same records from its models.
"""

from __future__ import annotations

import contextlib
import copy
import ipaddress
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass, field, fields
from typing import Any

from ..standard_options import STANDARD_V4, STANDARD_V6
from ..windows import normalize
from ..windows.client import (
    DhcpClass,
    DhcpClient,
    ExclusionRange,
    ExclusionRangeV6,
    FailoverRelationship,
    FilterListState,
    MacFilter,
    OptionDefinition,
    OptionValue,
    OptionValueV6,
    Policy,
    PolicyIPRange,
    Reservation,
    ReservationV6,
    Scope,
    ScopeV6,
    Superscope,
)
from ..windows.psscript import NotFound
from ..windows.runner import Stopped, raise_if_stopped, set_stop_check
from .diff import Record

__all__ = (
    "NAME_FIELDS",
    "SKIPPED_OPTION_IDS",
    "OptionValue",
    "WindowsState",
    "canonical_criteria",
    "class6_key",
    "class_key",
    "equal",
    "records_equal",
    "with_option_types",
    "hidden_records",
    "mac_key",
    "option6_key",
    "optiondef_key",
    "option_key",
    "partner_matches",
    "policy_key",
    "read",
    "reservation6_key",
    "resolve_partner",
    "same_host",
    "scope6_key",
    "to_records",
    "text_field",
)

# Windows keeps the subnet mask (1) and the lease time (51) as options too. NetBox holds them on the scope's prefix and
# lease duration, so importing them as options would store the same setting twice.
SKIPPED_OPTION_IDS = frozenset({1, 51})

# PrimaryModel.description holds 200 characters.
DESCRIPTION_LENGTH = 200

# Reasons a Windows object is not imported, as they appear in the job log.
PARTNER_FAILOVERS = "failover relationship(s) whose primary is another server: they are synced from that server"
V6_VENDOR_CLASSES = "DHCPv6 vendor class(es): the plugin syncs DHCPv6 user classes only"
V6_VENDOR_VALUES = "DHCPv6 vendor class option value(s): the plugin syncs DHCPv6 standard and user class values only"
V6_UNDEFINED = (
    "DHCPv6 option value(s) of codes without a standard DHCPv6 definition: DHCPv6 custom option definitions are not "
    "synced"
)
V6_EXCLUSIONS = "DHCPv6 exclusion range(s): they are not synced, and a push leaves them alone"

# Windows DHCPv6 scopes are always /64.
V6_PREFIX_LENGTH = 64

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
    v6_classes: list[DhcpClass] = field(default_factory=list)
    v6_scopes: list[ScopeV6] = field(default_factory=list)
    v6_exclusions: list[ExclusionRangeV6] = field(default_factory=list)
    v6_reservations: list[ReservationV6] = field(default_factory=list)
    # Every DHCPv6 option value, with prefix and reserved_ip filled in for scope and reservation values.
    v6_options: list[OptionValueV6] = field(default_factory=list)
    # What the read left out and why, for the job log and the run summary (see to_records).
    notes: list[str] = field(default_factory=list)


GONE_MID_READ = "{kind} {key} left out: deleted on Windows while it was read"


def _scope_options(client: DhcpClient, scope_id: str) -> list:
    out = client.list_option_values(scope_id=scope_id, all=True)
    for o in out:
        o.scope_id, o.reserved_ip = scope_id, ""
    return out


def _read_policies(client: DhcpClient, part: WindowsState, sid: str) -> None:
    """The policies of one scope (server-level ones when ``sid`` is empty), with their IP ranges and option values."""
    policies = client.list_policies(scope_id=sid)
    # One batched read for all of them (issue #100), not two PowerShell starts per policy.
    contents = client.list_policy_contents([p.name for p in policies], scope_id=sid)
    for policy in policies:
        policy.scope_id = sid
        part.policies.append(policy)
        c = contents[policy.name]
        part.policy_ranges.extend(c.ip_ranges)
        for o in c.options:
            o.scope_id, o.reserved_ip = sid, ""
        part.options.extend(c.options)


def read(client: DhcpClient, workers: int = 1) -> WindowsState:
    """
    Read everything the sync handles. One PowerShell call per list, plus a few per scope: a scope's reservation
    option values and its policies' ranges and values are read in batches (issue #100), so the number of calls does
    not grow with the number of reservations or policies (up to ``client.READ_BATCH`` of each per scope).

    The server-wide lists (IPv4 and DHCPv6) are read first, one after another. Then ``workers`` scopes, IPv4 and
    DHCPv6 alike, are read at the same time (issue #99, the ``max_concurrency`` setting); the result is the same as
    reading them one after another, in the same order. With 1 nothing runs in another thread.
    """
    state = WindowsState(
        classes=client.list_classes(),
        option_definitions=client.list_option_definitions(),
        scopes=client.list_scopes(),
        superscopes=client.list_superscopes(),
        failovers=client.list_failovers(),
        filters=client.list_filters(),
        filter_lists=client.get_filter_lists(),
    )
    # -All returns the values of every user and vendor class at each level (lab, Windows Server 2022).
    state.options.extend(client.list_option_values(all=True))
    _read_policies(client, state, "")
    state.v6_classes = client.list_v6_classes()
    state.v6_scopes = client.list_v6_scopes()
    # -All returns the values of every user class at each level, as for IPv4 (lab, Windows Server 2022).
    state.v6_options.extend(client.list_v6_option_values(all=True))
    workers = max(1, int(workers))
    if workers > 1 and len(state.scopes) + len(state.v6_scopes) > 1:
        stop: _Stop | None = _Stop()
        # A copy of the client whose commands are refused once its scope must stop (see _Stop).
        scoped = copy.copy(client)
        scoped.runner = _Stopping(client.runner)
    else:
        stop, scoped = None, client
    # A scope can disappear while it is read, for example when a failover relationship is removed on its partner:
    # its reads then fail with not found. It is gone, as if it had not been listed; the rest of the read stands.
    units = [
        _Unit(s.scope_id, "scope", lambda part, sid: _read_scope(scoped, part, sid), scoped.get_scope)
        for s in state.scopes
    ] + [
        _Unit(s.prefix, "DHCPv6 scope", lambda part, px: _read_v6_scope(scoped, part, px), scoped.get_v6_scope)
        for s in state.v6_scopes
    ]
    there = _read_units(state, units, workers, stop)
    v4 = len(state.scopes)
    state.scopes = [s for s, ok in zip(state.scopes, there[:v4], strict=True) if ok]
    state.v6_scopes = [s for s, ok in zip(state.v6_scopes, there[v4:], strict=True) if ok]
    return state


# A command not sent because its scope's read is stopping (raised by the Windows layer's stop check).
_Skipped = Stopped

# After a job timeout, the longest the read waits for the commands already sent before it lets the timeout through.
# RQ kills a job's process about a minute after its timeout, and the job still has to record the failed run.
INTERRUPT_WAIT = 30


class _Stop:
    """
    Which scopes of a parallel read may still send commands. When the scope at index ``i`` fails, the scopes after it
    stop: a one-at-a-time read would never have reached them. The scopes before it go on, since the lowest failing
    index gives the read's error, as it would one at a time, whichever failed first. When the job itself is
    interrupted (a timeout), every scope stops.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._first_failed: int | None = None

    def enter(self, index: int) -> None:
        """The calling worker thread now reads the scope at ``index``: its commands are sent only while allowed."""
        set_stop_check(lambda: self.stopped(index))

    def leave(self) -> None:
        set_stop_check(None)

    def fail(self, index: int) -> None:
        with self._lock:
            if self._first_failed is None or index < self._first_failed:
                self._first_failed = index

    def stop_all(self) -> None:
        self.fail(-1)

    def stopped(self, index: int) -> bool:
        with self._lock:
            return self._first_failed is not None and index > self._first_failed


class _Stopping:
    """
    A runner that refuses a scope's commands once that scope must stop (see _Stop). After a failure or a job timeout,
    the scopes still being read stop at their next command, so the read only waits for the commands already sent to
    Windows, not for whole scopes: a WinRM command can take minutes, and a job past its timeout has about a minute
    before the worker is killed. The Windows layer checks too while a command waits for a free slot or for an SSH
    session.
    """

    def __init__(self, runner: Any) -> None:
        self.runner = runner

    def run(self, script: str, params: Any = None) -> bytes:
        raise_if_stopped()
        return self.runner.run(script, params)


@dataclass
class _Unit:
    """One scope to read: ``read_one(part, key)`` fills ``part``, ``get_one(key)`` checks the scope still exists."""

    key: str
    kind: str
    read_one: Callable[[WindowsState, str], None]
    get_one: Callable[[str], Any]


def _read_units(state: WindowsState, units: list[_Unit], workers: int, stop: _Stop | None) -> list[bool]:
    """
    Read each unit, up to ``workers`` at a time, and add what was read to ``state`` in the order of ``units``.
    Returns, for each unit, whether its scope is still there (see _gone_or_read).

    Each unit fills its own WindowsState, so threads share nothing but the client, whose runner is thread safe and
    capped by the same ``max_concurrency`` (runner.Limited). Merging in list order keeps records and notes in the
    order a one-at-a-time read gives.
    """

    def run(unit: _Unit) -> tuple[bool, WindowsState]:
        part = WindowsState()
        read_it = lambda: unit.read_one(part, unit.key)  # noqa: E731
        return _gone_or_read(read_it, unit.get_one, unit.key, part, unit.kind), part

    if stop is None:
        # Lazy: a failing scope stops the read before the next one is sent, exactly as before issue #99.
        results: Iterable[tuple[bool, WindowsState]] = (run(unit) for unit in units)
    else:
        results = _in_parallel(run, units, workers, stop)
    there = []
    for still_there, part in results:
        _merge(state, part)
        there.append(still_there)
    return there


def _wait(future: Future) -> Any:
    """The result of one unit (a seam for tests: a job timeout arrives as an exception while the main thread waits)."""
    return future.result()


def _in_parallel(fn: Callable[[Any], Any], items: list, workers: int, stop: _Stop) -> list:
    """
    ``[fn(item) for item in items]`` on up to ``workers`` threads, ``stop`` deciding which items' commands are still
    sent (see _Stop and _Stopping).

    When an item fails, the items after it are cancelled if not started and stopped at their next command if
    running; the items before it finish, and the error of the first failing item in list order is raised: the one a
    one-at-a-time read stops on. When the calling thread is interrupted (a job timeout), every item stops. Either
    way only the commands already sent to Windows are waited for (one cannot be taken back halfway), and nothing runs
    once this returns.
    """
    futures: list[Future] = []
    # Set once every item is queued, so a failing item can always cancel the ones after it.
    queued = threading.Event()

    def guarded(index: int, item: Any) -> Any:
        queued.wait()
        # A stopped item that starts anyway sends nothing: its first command is refused.
        stop.enter(index)
        try:
            return fn(item)
        except BaseException:
            stop.fail(index)
            # From the worker, so the items after it stop now, not when the calling thread reaches this one.
            for f in futures[index + 1 :]:
                f.cancel()
            raise
        finally:
            stop.leave()

    # Not a with block: after a job timeout the read must not wait for the threads without a bound.
    pool = ThreadPoolExecutor(max_workers=min(workers, len(items)), thread_name_prefix="windows-dhcp-read")
    try:
        try:
            futures.extend(pool.submit(guarded, i, item) for i, item in enumerate(items))
        finally:
            queued.set()
        # In list order: every item before the one that raises here returned, so its error is the first one.
        results = [_wait(f) for f in futures]
    except BaseException as exc:
        stop.stop_all()
        pool.shutdown(wait=False, cancel_futures=True)
        if _raised_by_an_item(futures, exc) or isinstance(exc, _Skipped | CancelledError):
            # An item failed: wait for the commands in flight, so nothing runs once the read has failed.
            futures_wait(_not_cancelled(futures))
            if isinstance(exc, _Skipped | CancelledError):  # not expected: see above
                raise _first_error(futures) from None
            raise
        # Interrupted (a job timeout): wait a bounded time for the commands in flight, then let the interrupt through
        # so the job can still record its failure. A command still running is left to its thread; the job closes the
        # client, which ends an SSH command.
        futures_wait(_not_cancelled(futures), timeout=INTERRUPT_WAIT)
        raise
    pool.shutdown(wait=True)
    return results


def _not_cancelled(futures: list[Future]) -> list[Future]:
    # A future cancelled while queued never counts as done for concurrent.futures.wait: its thread never takes it.
    return [f for f in futures if not f.cancelled()]


def _raised_by_an_item(futures: list[Future], exc: BaseException) -> bool:
    return any(f.done() and not f.cancelled() and f.exception() is exc for f in futures)


def _first_error(futures: list[Future]) -> BaseException:
    """The first real error of finished futures, in list order (not a stop, not a cancellation)."""
    for f in futures:
        if f.cancelled():
            continue
        error = f.exception()
        if error is not None and not isinstance(error, _Skipped):
            return error
    return RuntimeError("a parallel read of the server stopped without an error")  # not reached


def _merge(state: WindowsState, part: WindowsState) -> None:
    """Add the lists of ``part`` (what one scope holds, and its notes) to ``state``."""
    for f in fields(WindowsState):
        value = getattr(part, f.name)
        if isinstance(value, list) and value:
            getattr(state, f.name).extend(value)


def _gone_or_read(read_scope: Any, get_scope: Any, key: str, state: WindowsState, kind: str) -> bool:
    """
    Run ``read_scope``; False, with a note on ``state``, when the scope itself turns out to be gone. Not found can
    also mean something inside it went away mid-read (a reservation, a policy), so the scope is checked, and read once
    more if it is still there. A second failure is raised: dropping a scope that exists would make an import delete it
    from NetBox.
    """
    for attempt in (1, 2):
        try:
            read_scope()
            return True
        except NotFound:
            try:
                get_scope(key)
            except NotFound:
                # Without the note this is silent: an import or push simply would not see the scope.
                state.notes.append(GONE_MID_READ.format(kind=kind, key=key))
                return False
            if attempt == 2:
                raise
    return True  # not reached


def _read_scope(client: DhcpClient, state: WindowsState, sid: str) -> None:
    """Add what one scope holds to ``state``; nothing is added when a read fails."""
    part = WindowsState()
    part.exclusions = client.list_exclusion_ranges(sid)
    part.reservations = client.list_reservations(sid)
    part.options = _scope_options(client, sid)
    # A reservation deleted after the list above makes this raise NotFound, and _gone_or_read reads the scope again.
    values = client.list_reservation_option_values(sid, [r.ip_address for r in part.reservations])
    for r in part.reservations:
        for o in values[r.ip_address]:
            o.scope_id, o.reserved_ip = sid, r.ip_address
        part.options.extend(values[r.ip_address])
    _read_policies(client, part, sid)
    state.exclusions.extend(part.exclusions)
    state.reservations.extend(part.reservations)
    state.options.extend(part.options)
    state.policies.extend(part.policies)
    state.policy_ranges.extend(part.policy_ranges)


def _read_v6_scope(client: DhcpClient, state: WindowsState, prefix: str) -> None:
    """As _read_scope, for a DHCPv6 scope."""
    exclusions = client.list_v6_exclusion_ranges(prefix)
    reservations = client.list_v6_reservations(prefix)
    options = _v6_options(client, prefix)
    values = client.list_v6_reservation_option_values(prefix, [r.ip_address for r in reservations])
    for r in reservations:
        for o in values[r.ip_address]:
            o.prefix, o.reserved_ip = prefix, r.ip_address
        options.extend(values[r.ip_address])
    state.v6_exclusions.extend(exclusions)
    state.v6_reservations.extend(reservations)
    state.v6_options.extend(options)


def _v6_options(client: DhcpClient, prefix: str) -> list:
    out = client.list_v6_option_values(prefix=prefix, all=True)
    for o in out:
        o.prefix, o.reserved_ip = prefix, ""
    return out


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


def partner_matches(name: str, servers: Iterable[tuple[str, Iterable[str]]]) -> list[str]:
    """
    The hosts (``host_name`` form) of the ``servers`` a Windows partner name points at. ``servers`` holds each other
    Windows server's host as NetBox knows it (the DHCP server itself) and the names it reported for itself. Windows
    names a partner as it was given when the relationship was created, and on the partner's side by the other
    server's computer name: a host match wins, the reported names are only tried when no host matches.
    """
    servers = [(host, list(names)) for host, names in servers]
    hosts = sorted({host_name(h) for h, _ in servers if same_host(h, name)})
    if hosts:
        return hosts
    return sorted({host_name(h) for h, names in servers if any(same_host(n, name) for n in names)})


def resolve_partner(name: str, servers: Iterable[tuple[str, Iterable[str]]]) -> str:
    """
    A Windows partner name as NetBox would write it: the host of the one server that reported it as its own name,
    otherwise the name as Windows gave it. Two servers reporting the same name match neither.
    """
    servers = list(servers)
    if any(same_host(h, name) for h, _ in servers):
        return host_name(name)
    matches = partner_matches(name, servers)
    return matches[0] if len(matches) == 1 else host_name(name)


def ipv6(value: str) -> str:
    """An IPv6 address in one spelling (lowercase, compressed), whatever form either side wrote it in."""
    try:
        return str(ipaddress.IPv6Address(value.strip()))
    except ValueError:
        return value


def scope6_key(prefix: str) -> str:
    """``v6/2001:db8:1::``. The ``v6/`` start keeps DHCPv6 keys apart from IPv4 ones in every message."""
    return f"v6/{ipv6(prefix)}"


def reservation6_key(prefix: str, ip_address: str) -> str:
    """``v6/2001:db8:1::/2001:db8:1::10``."""
    return f"v6/{ipv6(prefix)}/{ipv6(ip_address)}"


def class6_key(name: str) -> str:
    """``v6/user/lab``: only DHCPv6 user classes are synced."""
    return f"v6/user/{name.lower()}"


def option6_key(parent: str, option_id: int, user_class: str = "") -> str:
    """``v6/server/23``, ``v6/2001:db8:1::/24``, ``v6/2001:db8:1::/2001:db8:1::10/23;user=lab``."""
    return option_key(f"v6/{parent}", option_id, user_class=user_class)


def option6_target(parent: str) -> tuple[str, str]:
    """``server``, ``2001:db8:1::`` or ``2001:db8:1::/2001:db8:1::10`` -> (prefix, reserved IP)."""
    if parent == "server":
        return "", ""
    prefix, _, ip = parent.partition("/")
    return prefix, ip


def mac_key(mac: str) -> str:
    """A MAC filter pattern as Windows matches it: ``00-15-5d-*``."""
    return mac.strip().lower().replace(":", "-")


# -- records -------------------------------------------------------------------------------------------------------


def _lease_seconds(value: str) -> int | None:
    if not value:
        return None
    try:
        return normalize.parse_duration(value)
    except ValueError:
        # An unlimited lease reads back as TimeSpan.MaxValue (10675199.02:48:05.4775807), whether it was set as that or
        # as 0xFFFFFFFF seconds (lab, Windows Server 2022); parse_duration refuses it, so no lease time is recorded.
        return None


def _scope(s: Scope) -> Record:
    return Record(
        "scope",
        s.scope_id,
        {
            "name": s.name or "",
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


def text_field(value: str | None) -> str:
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


def canonical_criteria(value: dict[str, list[str]]) -> dict[str, list[str]]:
    """
    Policy criteria as Windows stores them, to compare and fingerprint (never to send). Windows rewrites a MAC prefix
    ("02-4e-42-ab*" reads back "02-4e-42-ab-*") and lowercases a host name, so the spelling typed in NetBox and the one
    read from Windows must not look like a change on every push.
    """
    out = {}
    for name, items in criteria(value).items():
        out[name] = [items[0], *(normalize.canonical_policy_criterion(name, v) for v in items[1:])]
    return out


def policy_record(p: Policy, ranges: list[list[str]]) -> Record:
    return Record(
        "policy",
        policy_key(p.scope_id, p.name),
        {
            "name": p.name,
            "scope_id": p.scope_id,
            "description": text_field(p.description),
            "enabled": bool(p.enabled),
            "processing_order": p.processing_order,
            "condition": (p.condition or "OR").lower(),
            "criteria": criteria(p.criteria),
            "lease_seconds": _lease_seconds(p.lease_duration),
            "ip_ranges": ip_ranges(ranges),
        },
    )


def failover_record(f: FailoverRelationship, partner_servers: Iterable[tuple[str, Iterable[str]]] = ()) -> Record:
    hot = f.mode.lower() == "hotstandby"
    return Record(
        "failover",
        f.name.lower(),
        {
            "name": f.name,
            "partner_server": resolve_partner(f.partner_server, partner_servers),
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


def scope6_record(s: ScopeV6) -> Record:
    prefix = ipv6(s.prefix)
    return Record(
        "scope6",
        scope6_key(prefix),
        {
            "prefix": prefix,
            "prefix_length": V6_PREFIX_LENGTH,
            "name": s.name or "",
            "description": s.description or "",
            "state": s.state.lower(),
            "preference": int(s.preference or 0),
            "preferred_lifetime": _lease_seconds(s.preferred_lifetime),
            "valid_lifetime": _lease_seconds(s.valid_lifetime),
            # Zero: the server works the renewal and rebinding times out itself, which NetBox writes as empty.
            "t1": _lease_seconds(s.t1) or None,
            "t2": _lease_seconds(s.t2) or None,
        },
    )


def reservation6_record(r: ReservationV6) -> Record:
    prefix, ip = ipv6(r.prefix), ipv6(r.ip_address)
    return Record(
        "reservation6",
        reservation6_key(prefix, ip),
        {
            "prefix": prefix,
            "ip_address": ip,
            "client_duid": _client_id(r.client_duid),
            "iaid": int(r.iaid),
            "name": r.name or "",
            "description": r.description or "",
        },
    )


def _option6_parent(o: OptionValueV6) -> str:
    prefix = ipv6(o.prefix) if o.prefix else ""
    if o.reserved_ip:
        return f"{prefix}/{ipv6(o.reserved_ip)}"
    return prefix or "server"


def option6_record(o: OptionValueV6) -> Record:
    parent = _option6_parent(o)
    fields = {
        "parent": parent,
        "option_id": o.option_id,
        "value": [str(v) for v in o.value],
        "user_class": o.user_class,
    }
    if o.vendor_class:
        # Only on the hidden records of vendor class values, which a push never writes.
        fields["vendor_class"] = o.vendor_class
        key = option_key(f"v6/{parent}", o.option_id, vendor_class=o.vendor_class, user_class=o.user_class)
        return Record("option6", key, fields)
    return Record("option6", option6_key(parent, o.option_id, o.user_class), fields)


def exclusion6_record(e: ExclusionRangeV6) -> Record:
    prefix, start, end = ipv6(e.prefix), ipv6(e.start_range), ipv6(e.end_range)
    return Record(
        "exclusion6", f"v6/{prefix}/{start}-{end}", {"prefix": prefix, "start_range": start, "end_range": end}
    )


def _v6_option_skip(o: OptionValueV6, standard6: set[int] | None) -> str | None:
    """Why a DHCPv6 option value is not synced, or None."""
    if o.vendor_class:
        return V6_VENDOR_VALUES
    if standard6 is not None and o.option_id not in standard6:
        return V6_UNDEFINED
    return None


def _v6_records(state: WindowsState, standard6: set[int] | None, skip) -> list[Record]:
    records = []
    for c in state.v6_classes:
        if c.type.lower() != "user":
            skip(V6_VENDOR_CLASSES)
            continue
        records.append(
            Record(
                "class6",
                class6_key(c.name),
                {"name": c.name, "class_type": "user", "data": c.data, "description": text_field(c.description)},
            )
        )
    records.extend(scope6_record(s) for s in state.v6_scopes)
    for _ in state.v6_exclusions:
        skip(V6_EXCLUSIONS)
    records.extend(reservation6_record(r) for r in state.v6_reservations)
    for o in state.v6_options:
        reason = _v6_option_skip(o, standard6)
        if reason is not None:
            skip(reason)
            continue
        records.append(option6_record(o))
    return records


def to_records(
    state: WindowsState,
    standard_codes: Iterable[int] = (),
    skipped_option_ids: Iterable[int] = SKIPPED_OPTION_IDS,
    partner_failovers: Iterable[str] = (),
    standard_codes6: Iterable[int] | None = None,
    partner_servers: Iterable[tuple[str, Iterable[str]]] = (),
) -> tuple[list[Record], list[str]]:
    """
    Turn a WindowsState into records, and return notes about what was left out.

    ``standard_codes`` are the standard DHCPv4 option codes (``standard_options.STANDARD_V4``): Windows ships their
    definitions, so they are not imported (values still are, by code). ``partner_failovers`` are the (lowercase)
    names of failover relationships NetBox syncs from the other server. ``standard_codes6`` are the standard DHCPv6
    codes: DHCPv6 definitions are not synced, so values of other codes are left out (None keeps them all).
    ``partner_servers`` are the other Windows servers as ``partner_matches`` takes them: a failover partner Windows
    names by a computer name one of them reported is written as that server's host, as NetBox holds it.
    """
    standard = set(standard_codes)
    partner_servers = list(partner_servers)
    standard6 = None if standard_codes6 is None else set(standard_codes6)
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
                {"name": c.name, "class_type": kind, "data": c.data, "description": text_field(c.description)},
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
                    "description": d.description or "",
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
                    "name": r.name or "",
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
        records.append(option_record(o))

    for f in state.failovers:
        if f.name.lower() in partners:
            skip(PARTNER_FAILOVERS)
            continue
        records.append(failover_record(f, partner_servers))

    for m in state.filters:
        records.append(
            Record(
                "macfilter",
                mac_key(m.mac_address),
                {
                    "mac_address": mac_key(m.mac_address),
                    "list": m.list.lower(),
                    "description": text_field(m.description),
                },
            )
        )

    if (lists := state.filter_lists) is not None:
        records.append(Record("filterlists", "server", {"allow": bool(lists.allow), "deny": bool(lists.deny)}))

    records.extend(_v6_records(state, standard6, skip))

    notes = [*state.notes, *(f"skipped {count} {reason}" for reason, count in skips.items())]
    definitions = {optiondef_key(d.option_id, d.vendor_class): d.type for d in state.option_definitions}
    return with_option_types(records, definitions), notes


VALUE_TYPE = "_value_type"
NUMERIC_TYPES = frozenset(
    {"byte", "word", "dword", "dworddword", "uint8", "uint16", "uint32", "uint64", "int32", "boolean"}
)


def _value_type(data_type: str) -> str:
    data_type = data_type.lower()
    if data_type in NUMERIC_TYPES:
        return "number"
    if data_type in ("ipv6address", "ipv6-address"):
        return "ipv6"
    # String, BinaryData, and unknown types must never be guessed from their contents.
    return "exact"


def option_value_type(kind: str, fields: dict[str, Any]) -> str:
    if kind == "optiondef":
        return _value_type(fields.get("type", ""))
    if kind not in ("option", "option6"):
        return "exact"
    if VALUE_TYPE in fields:
        return fields[VALUE_TYPE]
    standard = STANDARD_V6 if kind == "option6" else STANDARD_V4
    definition = None if fields.get("vendor_class") else standard.get(fields.get("option_id"))
    return _value_type(definition[1]) if definition else "unknown"


def with_option_types(
    records: Iterable[Record],
    definitions: dict[str, str] | None = None,
    fallback: Iterable[Record] = (),
) -> list[Record]:
    """Carry the effective type with values, including custom/vendor definitions omitted from a partial diff."""
    records = list(records)
    definitions = {
        **(definitions or {}),
        **{r.key: r.fields["type"] for r in records if r.kind == "optiondef"},
    }
    fallback_types = {}
    for r in fallback:
        if r.kind == "optiondef":
            fallback_types["option", r.key] = option_value_type(r.kind, r.fields)
        elif r.kind in ("option", "option6"):
            key = optiondef_key(r.fields["option_id"], r.fields.get("vendor_class", ""))
            fallback_types.setdefault((r.kind, key), option_value_type(r.kind, r.fields))
    out = []
    for r in records:
        if r.kind in ("option", "option6"):
            key = optiondef_key(r.fields["option_id"], r.fields.get("vendor_class", ""))
            typ = (
                _value_type(definitions[key])
                if r.kind == "option" and key in definitions
                else option_value_type(r.kind, r.fields)
            )
            # A value can be managed without importing its Windows definition. Only an unknown type may inherit
            # the effective Windows type; an explicit String/Binary definition must retain exact comparison.
            if typ == "unknown":
                typ = fallback_types.get((r.kind, key), typ)
            r = Record(r.kind, r.key, {**r.fields, VALUE_TYPE: typ})
        out.append(r)
    return out


def canonical_option_fields(kind: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Comparable/hashable values without changing the spelling sent to Windows or imported into NetBox."""
    out = dict(fields)
    typ = option_value_type(kind, fields)
    if kind in ("option", "option6"):
        out[VALUE_TYPE] = typ
    name = "default_value" if kind == "optiondef" else "value"
    if kind not in ("option", "option6", "optiondef") or not isinstance(out.get(name), list):
        return out
    values = []
    for value in out[name]:
        if typ == "number":
            with contextlib.suppress(TypeError, ValueError):
                value = str(normalize.parse_option_number(value))
        elif typ == "ipv6" and isinstance(value, str):
            value = ipv6(value)
        values.append(value)
    out[name] = values
    return out


def records_equal(kind: str, a: dict[str, Any], b: dict[str, Any]) -> bool:
    if kind in ("option", "option6"):
        a_type, b_type = option_value_type(kind, a), option_value_type(kind, b)
        if a_type == "unknown":
            a = {**a, VALUE_TYPE: b_type}
        if b_type == "unknown":
            b = {**b, VALUE_TYPE: a_type}
    a, b = canonical_option_fields(kind, a), canonical_option_fields(kind, b)
    return a.keys() == b.keys() and all(equal(kind, name, a[name], b[name]) for name in a)


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
        ("class6", "name"),
        ("option6", "user_class"),
    }
)


def equal(kind: str, name: str, a: Any, b: Any) -> bool:
    """
    Field comparison for ``diff``. Windows names match without case, and policy conditions match as Windows stores
    them. Option values compare exactly here: only ``records_equal`` has enough context to normalize numeric types.
    """
    if (kind, name) in NAME_FIELDS and isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if (kind, name) == ("optiondef", "type") and isinstance(a, str) and isinstance(b, str):
        # Windows takes -Type without case and reports its own spelling ("string" reads back as "String"). Not in
        # NAME_FIELDS on purpose: those are lowercased in fingerprints, which would change every stored one.
        return a.casefold() == b.casefold()
    if (kind, name) == ("failover", "partner_server") and isinstance(a, str) and isinstance(b, str):
        return same_host(a, b)
    if (kind, name) == ("failover", "auth") and (a is None or b is None):
        # None: NetBox has no shared secret profile, so it leaves authentication as Windows has it.
        return True
    if (kind, name) == ("policy", "criteria") and isinstance(a, dict) and isinstance(b, dict):
        return canonical_criteria(a) == canonical_criteria(b)
    return a == b


def hidden_records(
    state: WindowsState,
    *,
    partner_failovers: Iterable[str] = (),
    standard_codes6: Iterable[int] | None = None,
) -> list[Record]:
    """
    Windows objects ``to_records`` leaves out (relationships synced from the partner, DHCPv6 values and exclusions it
    does not sync), as records whose parents can be worked out. A push never touches them, but removing their parent
    on Windows would remove them too, so they stop the parent from being deleted. Options 1 and 51 are not here:
    every scope has them, and they mean nothing without it.
    """
    partners = {n.lower() for n in partner_failovers}
    out = [failover_record(f) for f in state.failovers if f.name.lower() in partners]
    standard6 = None if standard_codes6 is None else set(standard_codes6)
    out.extend(option6_record(o) for o in state.v6_options if _v6_option_skip(o, standard6))
    out.extend(exclusion6_record(e) for e in state.v6_exclusions)
    return out
