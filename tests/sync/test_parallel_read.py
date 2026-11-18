"""Issue #99: windows_state.read reads scopes in parallel, up to max_concurrency, with the serial read's result."""

from __future__ import annotations

import threading
import time

import pytest

from netbox_windows_dhcp_plugin.sync import windows_state as ws
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows import runner as runner_mod
from netbox_windows_dhcp_plugin.windows.client import (
    ExclusionRange,
    OptionKey,
    OptionKeyV6,
    Policy,
    PolicyIPRange,
    Reservation,
    ReservationV6,
    Scope,
    ScopeV6,
)
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import PSError
from netbox_windows_dhcp_plugin.windows.runner import limit

V4_SCOPES = 12
V6_SCOPES = 5


def seeded() -> FakeDhcpServer:
    fake = FakeDhcpServer()
    c = DhcpClient(fake)
    for n in range(V4_SCOPES):
        base = f"10.{n}.0"
        sid = f"{base}.0"
        c.add_scope(Scope(name=f"s{n}", start_range=f"{base}.10", end_range=f"{base}.200", subnet_mask="255.255.255.0"))
        c.add_exclusion_range(ExclusionRange(scope_id=sid, start_range=f"{base}.10", end_range=f"{base}.19"))
        c.set_option_value(OptionKey(option_id=3, scope_id=sid), [f"{base}.1"])
        for i in range(n % 4):
            ip = f"{base}.{50 + i}"
            c.add_reservation(Reservation(scope_id=sid, ip_address=ip, client_id=f"02-00-00-00-{n:02x}-{i:02x}"))
            c.set_option_value(OptionKey(15, sid, reserved_ip=ip), [f"h{n}-{i}.example"])
        if n % 3 == 0:
            name = f"pol{n}"
            c.add_policy(Policy(name=name, scope_id=sid, criteria={"mac_address": ["EQ", f"02-4e-{n:02x}*"]}))
            c.add_policy_ip_range(PolicyIPRange(sid, name, f"{base}.220", f"{base}.230"))
            c.set_option_value(OptionKey(15, sid, policy_name=name), [f"p{n}.example"])
    for n in range(V6_SCOPES):
        prefix = f"2001:db8:{n}::"
        c.add_v6_scope(ScopeV6(prefix=prefix, name=f"v6-{n}"))
        c.add_v6_reservation(ReservationV6(prefix, f"2001:db8:{n}::10", f"00-01-00-01-aa-{n:02x}", n + 1))
        c.set_v6_option_value(OptionKeyV6(24, reserved_ip=f"2001:db8:{n}::10"), [f"v6-{n}.example"])
    fake.calls.clear()
    return fake


class Slow:
    """A runner that takes a moment per call (outside the fake's lock) and records how many calls overlap."""

    def __init__(self, inner, delay: float = 0.01, delays: dict | None = None) -> None:
        self.inner, self.delay, self.delays = inner, delay, delays or {}
        self.lock = threading.Lock()
        self.in_flight = self.peak = 0
        self.threads: set[str] = set()

    def run(self, script, params=None):
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.threads.add(threading.current_thread().name)
        try:
            time.sleep(self.delays.get((params or {}).get("scope_id"), self.delay))
            return self.inner.run(script, params)
        finally:
            with self.lock:
                self.in_flight -= 1


def everything(state: ws.WindowsState):
    records, notes = ws.to_records(state)
    return records, notes, ws.hidden_records(state)


@pytest.mark.parametrize("workers", [2, 3, 4, 8, 64])
def test_any_worker_count_gives_the_serial_read(workers):
    serial = ws.read(DhcpClient(seeded()))
    parallel = ws.read(DhcpClient(limit(seeded(), workers)), workers=workers)
    # The same objects, in the same order: the records, notes and fingerprints cannot tell the two apart.
    assert parallel == serial
    assert everything(parallel) == everything(serial)
    assert [s.scope_id for s in parallel.scopes] == [f"10.{n}.0.0" for n in range(V4_SCOPES)]
    assert len(parallel.reservations) == sum(n % 4 for n in range(V4_SCOPES))
    assert len(parallel.v6_reservations) == V6_SCOPES


def test_one_worker_reads_in_the_calling_thread_in_the_same_order():
    fake = seeded()
    slow = Slow(fake, delay=0)
    ws.read(DhcpClient(slow))
    default_ops = fake.ops()
    fake.calls.clear()
    ws.read(DhcpClient(limit(slow, 1)), workers=1)
    assert fake.ops() == default_ops
    assert slow.threads == {threading.current_thread().name}
    assert slow.peak == 1


def test_scopes_are_read_at_the_same_time_up_to_the_cap():
    slow = Slow(seeded())
    ws.read(DhcpClient(limit(slow, 4)), workers=4)
    assert slow.peak == 4
    # More workers than the runner's cap: the cap still holds.
    slow = Slow(seeded())
    ws.read(DhcpClient(limit(slow, 2)), workers=8)
    assert slow.peak == 2


def test_the_first_failing_scope_in_list_order_gives_the_error():
    # 10.2.0.0 is slow and fails; 10.3.0.0 fails first in time. A serial read stops on 10.2.0.0, so must this one.
    fake = seeded()

    def broken(op, p):
        if op == "reservation.list" and p.get("scope_id") in ("10.2.0.0", "10.3.0.0"):
            return PSError(message=f"broken {p['scope_id']}", category="InvalidOperation", command="Get-X")
        return None

    fake.fail = broken
    with pytest.raises(PSError, match="broken 10.2.0.0") as serial:
        ws.read(DhcpClient(fake))
    slow = Slow(fake, delay=0.005, delays={"10.2.0.0": 0.2})
    with pytest.raises(PSError, match="broken 10.2.0.0") as parallel:
        ws.read(DhcpClient(limit(slow, 4)), workers=4)
    assert type(parallel.value) is type(serial.value)
    assert str(parallel.value) == str(serial.value)


def with_more_scopes(total: int) -> FakeDhcpServer:
    fake = seeded()
    for n in range(V4_SCOPES, total):
        DhcpClient(fake).add_scope(
            Scope(name=f"s{n}", start_range=f"10.{n}.0.10", end_range=f"10.{n}.0.20", subnet_mask="255.255.255.0")
        )
    fake.calls.clear()
    return fake


def commands_per_scope(fake: FakeDhcpServer) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in fake.calls:
        if c.params.get("scope_id"):
            out[c.params["scope_id"]] = out.get(c.params["scope_id"], 0) + 1
    return out


def test_after_a_failure_later_scopes_stop_at_their_next_command_and_earlier_ones_finish():
    # 10.1.0.0 fails on its first command. 10.2.0.0 and 10.3.0.0 have one command in flight: they must not send
    # another. 10.0.0.0 comes first in list order, so it goes on (a one-at-a-time read would have read it whole) and
    # its own failure, on a later command, is the read's error.
    fake = with_more_scopes(40)

    def broken(op, p):
        sid = p.get("scope_id")
        if (op, sid) in (("exclusion.list", "10.1.0.0"), ("option.list", "10.0.0.0")):
            return PSError(message=f"broken {sid}", category="InvalidOperation", command="Get-X")
        return None

    fake.fail = broken
    with pytest.raises(PSError, match="broken 10.0.0.0") as serial:
        ws.read(DhcpClient(fake))
    fake.calls.clear()
    slow = Slow(fake, delay=0.01, delays={"10.0.0.0": 0.05, "10.2.0.0": 0.1, "10.3.0.0": 0.1})
    with pytest.raises(PSError, match="broken 10.0.0.0") as parallel:
        ws.read(DhcpClient(limit(slow, 4)), workers=4)
    assert str(parallel.value) == str(serial.value)
    sent = commands_per_scope(fake)
    assert sent["10.0.0.0"] == 3  # exclusions, reservations, then the failing option list
    assert sent["10.1.0.0"] == 1
    assert sent["10.2.0.0"] == 1 and sent["10.3.0.0"] == 1
    assert not {f"10.{n}.0.0" for n in range(4, 40)} & sent.keys()
    # Nothing is left running in the background once read() has raised.
    count = len(fake.calls)
    time.sleep(0.15)
    assert len(fake.calls) == count


class JobTimeout(Exception):
    """What RQ raises in the job's main thread when the job runs past its timeout."""


def test_an_interrupted_read_waits_only_for_the_commands_in_flight(monkeypatch):
    fake = with_more_scopes(40)
    slow = Slow(fake, delay=0.03)
    at_interrupt = {}

    def interrupted(future):
        time.sleep(0.05)
        at_interrupt["calls"], at_interrupt["time"] = len(fake.calls), time.monotonic()
        raise JobTimeout

    monkeypatch.setattr(ws, "_wait", interrupted)
    with pytest.raises(JobTimeout):
        ws.read(DhcpClient(limit(slow, 4)), workers=4)
    # A scope takes about five commands of 30 ms: waiting for whole scopes would take far longer than one command.
    assert time.monotonic() - at_interrupt["time"] < 0.1
    assert len(fake.calls) - at_interrupt["calls"] <= 4
    count = len(fake.calls)
    time.sleep(0.15)
    assert len(fake.calls) == count


def test_an_interrupted_parallel_run_starts_nothing_more(monkeypatch):
    starts = []

    def fn(item):
        starts.append(item)
        time.sleep(0.05)
        return item

    def interrupted(future):
        raise JobTimeout

    monkeypatch.setattr(ws, "_wait", interrupted)
    with pytest.raises(JobTimeout):
        ws._in_parallel(fn, list(range(20)), 2, ws._Stop())
    # Only what was already running; the rest is cancelled, not run while the job waits for its threads.
    assert len(starts) <= 2


def test_a_failed_item_cancels_the_items_after_it_at_once():
    starts = []

    def fn(item):
        starts.append(item)
        if item == 1:
            raise ValueError("broken 1")
        time.sleep(0.1)
        return item

    with pytest.raises(ValueError, match="broken 1"):
        ws._in_parallel(fn, list(range(20)), 2, ws._Stop())
    # Item 0 was running and finishes; nothing after item 1 starts, even while the caller still waits for item 0.
    assert sorted(starts) == [0, 1]


def failing(*pairs):
    def broken(op, p):
        sid = p.get("scope_id")
        if (op, sid) in pairs:
            return PSError(message=f"broken {sid}", category="InvalidOperation", command="Get-X")
        return None

    return broken


def test_the_lowest_failing_scope_wins_even_when_a_later_one_fails_first():
    # 10.2.0.0 fails at once; 10.0.0.0 fails later, on its second command. From then on 10.1.0.0, which sits between
    # them, must stop too: a one-at-a-time read would have stopped on 10.0.0.0 before reaching it.
    fake = with_more_scopes(20)
    fake.fail = failing(("exclusion.list", "10.2.0.0"), ("reservation.list", "10.0.0.0"))
    slow = Slow(fake, delay=0.01, delays={"10.0.0.0": 0.1, "10.1.0.0": 0.15})
    with pytest.raises(PSError, match="broken 10.0.0.0"):
        ws.read(DhcpClient(limit(slow, 4)), workers=4)
    sent = commands_per_scope(fake)
    assert sent["10.0.0.0"] == 2
    # Its first command (0 to 0.15 s) and the second one, sent before 10.0.0.0 failed at 0.2 s; nothing after.
    assert sent["10.1.0.0"] <= 2


def test_a_timeout_after_a_failure_stops_the_scopes_before_it_too(monkeypatch):
    fake = with_more_scopes(20)
    fake.fail = failing(("exclusion.list", "10.2.0.0"))
    slow = Slow(fake, delay=0.01, delays={"10.0.0.0": 0.15, "10.1.0.0": 0.15})

    def interrupted(future):
        time.sleep(0.2)  # 10.2.0.0 failed long ago; 10.0.0.0 and 10.1.0.0 are on their second command
        raise JobTimeout

    monkeypatch.setattr(ws, "_wait", interrupted)
    with pytest.raises(JobTimeout):
        ws.read(DhcpClient(limit(slow, 4)), workers=4)
    sent = commands_per_scope(fake)
    # Without the timeout they would read on (they come before the failed scope); with it they stop.
    assert sent["10.0.0.0"] <= 2 and sent["10.1.0.0"] <= 2


def test_a_timeout_does_not_wait_for_a_hung_command_past_the_bound(monkeypatch):
    fake = with_more_scopes(8)
    release = threading.Event()

    class Hung(Slow):
        def run(self, script, params=None):
            if (params or {}).get("scope_id") == "10.1.0.0":
                release.wait(10)  # a WinRM command that never answers
            return super().run(script, params)

    def interrupted(future):
        time.sleep(0.1)
        raise JobTimeout

    monkeypatch.setattr(ws, "_wait", interrupted)
    monkeypatch.setattr(ws, "INTERRUPT_WAIT", 0.3)
    started = time.monotonic()
    try:
        with pytest.raises(JobTimeout):
            ws.read(DhcpClient(limit(Hung(fake, delay=0.01), 4)), workers=4)
        # The serial lists take a moment first; then 0.1 s to the timeout and at most 0.3 s more.
        assert time.monotonic() - started < 2
    finally:
        release.set()


def test_a_timeout_while_queueing_still_stops_and_cancels(monkeypatch):
    starts = []

    def fn(item):
        starts.append(item)
        time.sleep(0.05)
        return item

    real = ws.ThreadPoolExecutor

    class Interrupted(real):
        submitted = 0

        def submit(self, *args, **kwargs):
            Interrupted.submitted += 1
            if Interrupted.submitted == 6:
                raise JobTimeout
            return super().submit(*args, **kwargs)

    monkeypatch.setattr(ws, "ThreadPoolExecutor", Interrupted)
    with pytest.raises(JobTimeout):
        ws._in_parallel(fn, list(range(20)), 2, ws._Stop())
    time.sleep(0.2)
    # Five were queued, two threads: the two running finish, the three waiting are cancelled.
    assert len(starts) <= 2


def test_a_command_waiting_for_a_free_slot_is_not_sent_once_stopped():
    fake = FakeDhcpServer()
    gate = threading.Event()

    class Holding:
        def run(self, script, params=None):
            gate.wait(5)
            return fake.run(script, params)

    limited = limit(Holding(), 1)
    stop = {"now": False}
    first = threading.Thread(target=lambda: DhcpClient(limited).list_scopes())
    first.start()
    time.sleep(0.05)
    outcome = []

    def second():
        runner_mod.set_stop_check(lambda: stop["now"])
        try:
            DhcpClient(limited).list_scopes()
            outcome.append("sent")
        except runner_mod.Stopped:
            outcome.append("stopped")

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.05)
    stop["now"] = True  # the read stops while the command waits for the slot
    gate.set()
    first.join()
    t.join()
    assert outcome == ["stopped"]
    assert fake.ops() == ["scope.list"]


def test_scopes_deleted_mid_read_are_left_out_in_order():
    fake = seeded()
    c = DhcpClient(fake)
    listed, listed6 = c.list_scopes(), c.list_v6_scopes()
    for sid in ("10.4.0.0", "10.9.0.0"):
        c.remove_scope(sid, force=True)
    c.remove_v6_scope("2001:db8:1::", force=True)
    serial_client = DhcpClient(fake)
    serial_client.list_scopes, serial_client.list_v6_scopes = (lambda: listed), (lambda: listed6)
    parallel_client = DhcpClient(limit(Slow(fake), 4))
    parallel_client.list_scopes, parallel_client.list_v6_scopes = (lambda: listed), (lambda: listed6)
    serial = ws.read(serial_client)
    parallel = ws.read(parallel_client, workers=4)
    assert parallel == serial
    assert parallel.notes == [
        "scope 10.4.0.0 left out: deleted on Windows while it was read",
        "scope 10.9.0.0 left out: deleted on Windows while it was read",
        "DHCPv6 scope 2001:db8:1:: left out: deleted on Windows while it was read",
    ]
    assert "10.4.0.0" not in {s.scope_id for s in parallel.scopes}


def test_a_scope_whose_reservation_goes_mid_read_is_read_again_in_parallel():
    fake = seeded()
    gone = {"done": False}

    def delete_one(op, p):
        if op == "option.list_reserved" and "10.3.0.50" in p["ips"] and not gone["done"]:
            gone["done"] = True
            fake._drop_reservation("10.3.0.50")
        return None

    fake.fail = delete_one
    state = ws.read(DhcpClient(limit(Slow(fake), 4)), workers=4)
    assert gone["done"]
    assert [r.ip_address for r in state.reservations if r.scope_id == "10.3.0.0"] == ["10.3.0.51", "10.3.0.52"]
    assert state.notes == []
