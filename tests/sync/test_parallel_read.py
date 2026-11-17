"""Issue #99: windows_state.read reads scopes in parallel, up to max_concurrency, with the serial read's result."""

from __future__ import annotations

import threading
import time

import pytest

from netbox_windows_dhcp_plugin.sync import windows_state as ws
from netbox_windows_dhcp_plugin.windows import DhcpClient
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


def test_no_scope_after_a_failed_one_is_started():
    fake = seeded()
    for n in range(V4_SCOPES, 40):
        DhcpClient(fake).add_scope(
            Scope(name=f"s{n}", start_range=f"10.{n}.0.10", end_range=f"10.{n}.0.20", subnet_mask="255.255.255.0")
        )
    fake.calls.clear()

    def broken(op, p):
        if op == "exclusion.list" and p.get("scope_id") == "10.1.0.0":
            return PSError(message="broken", category="InvalidOperation", command="Get-X")
        return None

    fake.fail = broken
    with pytest.raises(PSError, match="broken"):
        ws.read(DhcpClient(limit(Slow(fake), 2)), workers=2)
    read = {c.params.get("scope_id") for c in fake.calls if c.op == "exclusion.list"}
    # Two at a time: by the time 10.1.0.0 fails, at most the next few have been sent, never the whole list.
    assert "10.39.0.0" not in read
    assert len(read) < 10
    # Nothing is left running in the background once read() has raised.
    count = len(fake.calls)
    time.sleep(0.1)
    assert len(fake.calls) == count


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
