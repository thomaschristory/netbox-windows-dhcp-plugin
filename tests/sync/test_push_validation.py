"""
Issue #108: what Windows would refuse is left out of a push with a reason (shown in the dry run), and never makes the
push delete anything on Windows.
"""

import pytest

from netbox_windows_dhcp_plugin.sync import push
from netbox_windows_dhcp_plugin.sync import windows_state as ws
from netbox_windows_dhcp_plugin.sync.diff import CREATE, DELETE, Record
from netbox_windows_dhcp_plugin.sync.windows_writer import WindowsWriter
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .test_v6 import option6, reservation6, scope6
from .test_windows_writer import failover, option, policy, scope, windows_records


def reservation(ip="10.1.0.50", client="aa-bb-cc-00-11-22", scope_id="10.1.0.0", **over):
    fields = {
        "scope_id": scope_id,
        "ip_address": ip,
        "client_id": client,
        "name": "pc",
        "description": "",
        "reservation_type": "both",
    }
    fields.update(over)
    return Record("reservation", f"{scope_id}/{ip}", fields)


def exclusion(start, end, scope_id="10.1.0.0"):
    return Record(
        "exclusion", f"{scope_id}/{start}-{end}", {"scope_id": scope_id, "start_range": start, "end_range": end}
    )


def scope_at(key, mask, start, end, **over):
    record = scope(subnet_mask=mask, start_range=start, end_range=end, **over)
    return Record("scope", key, record.fields)


def reasons(p):
    return {(s.change.kind, s.change.key): s.reason for s in p.skipped}


@pytest.fixture
def fake():
    return FakeDhcpServer()


@pytest.fixture
def client(fake):
    return DhcpClient(fake)


def pushed(client, source, managed=None):
    """Push ``source`` to the fake and return the SyncState it leaves (key -> fingerprint)."""
    p = push.plan(source, windows_records(client), managed or {})
    result = push.execute(p, WindowsWriter(client))
    assert not result.failed, result.failed
    out = dict(managed or {})
    out.update(result.upserts)
    for k in result.removals:
        out.pop(k, None)
    return out


# -- item 1: objects outside their scope ------------------------------------------------------------------------------


def test_scope_range_outside_its_prefix_is_not_pushed():
    p = push.plan([scope(start_range="10.1.1.10", end_range="10.1.1.20")], [], {})
    assert "outside the scope's prefix 10.1.0.0/24" in reasons(p)["scope", "10.1.0.0"]
    assert p.changes == []


def test_scope_range_outside_its_prefix_keeps_the_windows_scope_and_its_reservations(client, fake):
    managed = pushed(client, [scope(), reservation()])
    source = [scope(start_range="10.1.0.10", end_range="10.1.1.20"), reservation(description="changed")]
    p = push.plan(source, windows_records(client), managed)
    assert "outside the scope's prefix" in reasons(p)["scope", "10.1.0.0"]
    result = push.execute(p, WindowsWriter(client))
    assert not result.applied and not result.failed
    assert "10.1.0.0" in fake.scopes and "10.1.0.50" in fake.reservations
    # The reservation update waits for its scope, as for any scope the push cannot write.
    assert "waits for scope 10.1.0.0" in reasons(p)["reservation", "10.1.0.0/10.1.0.50"]


@pytest.mark.parametrize(
    ("start", "end", "which"), [("10.1.0.0", "10.1.0.200", "network"), ("10.1.0.10", "10.1.0.255", "broadcast")]
)
def test_scope_range_holding_the_network_or_broadcast_address_is_not_pushed(start, end, which):
    p = push.plan([scope(start_range=start, end_range=end)], [], {})
    assert f"includes the {which} address" in reasons(p)["scope", "10.1.0.0"]


def test_point_to_point_scopes_have_no_network_or_broadcast_address_to_avoid():
    p = push.plan([scope_at("10.9.0.0", "255.255.255.254", "10.9.0.0", "10.9.0.1")], [], {})
    assert p.skipped == []


def test_reversed_scope_range_is_not_pushed():
    p = push.plan([scope(start_range="10.1.0.200", end_range="10.1.0.10")], [], {})
    assert "ends before it starts" in reasons(p)["scope", "10.1.0.0"]


def test_reservation_outside_the_scope_range_but_inside_the_prefix_is_pushed():
    # Windows Server 2025 accepts it (lab), whatever older documentation says.
    p = push.plan([scope(), reservation(ip="10.1.0.5")], [], {})
    assert p.skipped == []


def test_reservation_outside_the_scope_prefix_is_not_pushed():
    p = push.plan([scope(), reservation(ip="10.2.0.50")], [], {})
    assert "outside the scope's prefix 10.1.0.0/24" in reasons(p)["reservation", "10.1.0.0/10.2.0.50"]
    assert [c.kind for c in p.changes] == ["scope"]


def test_reservation_inside_an_exclusion_is_pushed():
    p = push.plan([scope(), exclusion("10.1.0.10", "10.1.0.60"), reservation()], [], {})
    assert p.skipped == []


def test_reservation_moved_out_of_its_prefix_keeps_the_old_one_on_windows(client, fake):
    managed = pushed(client, [scope(), reservation(), option("10.1.0.0/10.1.0.50", 15, ["pc.example"])])
    # The option value follows its reservation in NetBox, so it now hangs off the new address.
    source = [scope(), reservation(ip="10.2.0.5"), option("10.1.0.0/10.2.0.5", 15, ["pc.example"])]
    p = push.plan(source, windows_records(client), managed)
    found = reasons(p)
    assert "outside the scope's prefix" in found["reservation", "10.1.0.0/10.2.0.5"]
    assert "waiting: nothing in the IPv4 scopes is deleted while" in found["reservation", "10.1.0.0/10.1.0.50"]
    assert "waits for reservation 10.1.0.0/10.2.0.5" in found["option", "10.1.0.0/10.2.0.5/15"]
    assert ("option", "10.1.0.0/10.1.0.50/15") in found
    assert not [c for c in p.changes if c.action == DELETE]
    result = push.execute(p, WindowsWriter(client))
    assert not result.failed
    assert "10.1.0.50" in fake.reservations
    assert [k for k in fake.options if k[2] == "10.1.0.50"]


def test_reservation_moved_to_another_scope_keeps_the_old_one(client, fake):
    other = scope_at("10.2.0.0", "255.255.255.0", "10.2.0.10", "10.2.0.200")
    managed = pushed(client, [scope(), other, reservation()])
    # Now in the other scope, at an address outside it: the client's old reservation stays.
    source = [scope(), other, reservation(ip="10.1.0.60", scope_id="10.2.0.0")]
    p = push.plan(source, windows_records(client), managed)
    assert ("reservation", "10.1.0.0/10.1.0.50") in reasons(p)
    push.execute(p, WindowsWriter(client))
    assert "10.1.0.50" in fake.reservations


def test_v6_reservation_outside_its_prefix_is_not_pushed():
    p = push.plan([scope6(), reservation6(ip="2001:db8:2::10")], [], {})
    (reason,) = [s.reason for s in p.skipped]
    assert "outside the scope's prefix 2001:db8:1::/64" in reason


def test_exclusion_past_the_scope_range_but_inside_the_prefix_is_pushed():
    # Windows Server 2025 accepts it (lab).
    p = push.plan([scope(), exclusion("10.1.0.2", "10.1.0.20"), exclusion("10.1.0.190", "10.1.0.250")], [], {})
    assert p.skipped == []


@pytest.mark.parametrize(("start", "end"), [("10.0.255.250", "10.1.0.20"), ("10.1.0.190", "10.1.1.5")])
def test_exclusion_reaching_out_of_the_prefix_on_either_side_is_not_pushed(start, end):
    p = push.plan([scope(), exclusion(start, end)], [], {})
    assert "outside the scope's prefix 10.1.0.0/24" in reasons(p)["exclusion", f"10.1.0.0/{start}-{end}"]


def test_reversed_exclusion_is_not_pushed():
    p = push.plan([scope(), exclusion("10.1.0.30", "10.1.0.20")], [], {})
    assert "ends before it starts" in reasons(p)["exclusion", "10.1.0.0/10.1.0.30-10.1.0.20"]


# -- item 2: overlaps -------------------------------------------------------------------------------------------------


def test_new_scope_overlapping_one_on_windows_is_not_pushed(client, fake):
    managed = pushed(client, [scope()])
    wide = scope_at("10.1.0.0", "255.255.254.0", "10.1.0.10", "10.1.1.200")
    narrow = scope_at("10.1.1.0", "255.255.255.0", "10.1.1.10", "10.1.1.200")
    p = push.plan([scope(), narrow], windows_records(client), managed)
    assert reasons(p) == {}
    # A /23 over two /24s: the one already on Windows as NetBox has it keeps being pushed.
    p = push.plan([wide, narrow], windows_records(client), managed)
    found = reasons(p)
    assert "shares addresses with scope 10.1.0.0/23" in found["scope", "10.1.1.0"]
    assert "shares addresses with scope 10.1.1.0/24" in found["scope", "10.1.0.0"]


def test_overlapping_scopes_neither_on_windows_are_both_left_out():
    a = scope_at("10.1.0.0", "255.255.254.0", "10.1.0.10", "10.1.1.200")
    b = scope_at("10.1.1.0", "255.255.255.0", "10.1.1.10", "10.1.1.200")
    c = scope_at("10.3.0.0", "255.255.255.0", "10.3.0.10", "10.3.0.200")
    p = push.plan([a, b, c], [], {})
    assert set(reasons(p)) == {("scope", "10.1.0.0"), ("scope", "10.1.1.0")}
    assert [ch.key for ch in p.changes] == ["10.3.0.0"]


def test_only_the_new_scope_is_left_out_when_one_is_already_on_windows(client):
    managed = pushed(client, [scope()])
    newcomer = scope_at("10.1.0.128", "255.255.255.128", "10.1.0.130", "10.1.0.140")
    p = push.plan([scope(), newcomer], windows_records(client), managed)
    assert set(reasons(p)) == {("scope", "10.1.0.128")}


def test_scope_moved_onto_overlapping_prefix_keeps_the_old_scope_on_windows(client, fake):
    managed = pushed(client, [scope(), scope_at("10.1.1.0", "255.255.255.0", "10.1.1.10", "10.1.1.200")])
    # 10.1.0.0/24 becomes 10.1.0.128/25 in NetBox while a new /23 is also added over both: neither can be pushed.
    moved = scope_at("10.1.0.128", "255.255.255.128", "10.1.0.130", "10.1.0.140")
    wide = scope_at("10.1.0.0", "255.255.254.0", "10.1.0.10", "10.1.1.200", name="wide")
    source = [moved, wide, scope_at("10.1.1.0", "255.255.255.0", "10.1.1.10", "10.1.1.200")]
    p = push.plan(source, windows_records(client), managed)
    assert {("scope", "10.1.0.128")} <= set(reasons(p))
    assert not [c for c in p.changes if c.action == DELETE]
    push.execute(p, WindowsWriter(client))
    assert {"10.1.0.0", "10.1.1.0"} <= set(fake.scopes)


def test_overlapping_exclusions_left_out_unless_already_on_windows(client):
    a, b = exclusion("10.1.0.10", "10.1.0.30"), exclusion("10.1.0.25", "10.1.0.40")
    p = push.plan([scope(), a, b], [], {})
    assert set(reasons(p)) == {("exclusion", a.key), ("exclusion", b.key)}
    assert "shares addresses with exclusion range 10.1.0.25-10.1.0.40" in reasons(p)["exclusion", a.key]
    managed = pushed(client, [scope(), a])
    p = push.plan([scope(), a, b], windows_records(client), managed)
    assert set(reasons(p)) == {("exclusion", b.key)}


def test_exclusion_widened_onto_another_keeps_the_old_one_on_windows(client, fake):
    a, c = exclusion("10.1.0.10", "10.1.0.30"), exclusion("10.1.0.50", "10.1.0.60")
    managed = pushed(client, [scope(), a, c])
    wider = exclusion("10.1.0.10", "10.1.0.55")
    p = push.plan([scope(), wider, c], windows_records(client), managed)
    assert ("exclusion", wider.key) in reasons(p)
    assert not [ch for ch in p.changes if ch.action == DELETE]
    push.execute(p, WindowsWriter(client))
    assert len(fake.exclusions) == 2


def test_adjacent_exclusions_do_not_overlap():
    p = push.plan([scope(), exclusion("10.1.0.10", "10.1.0.30"), exclusion("10.1.0.31", "10.1.0.40")], [], {})
    assert p.skipped == []


# -- item 3: one client twice in a scope ------------------------------------------------------------------------------


def test_two_new_reservations_for_one_client_are_both_left_out():
    a, b = reservation("10.1.0.50"), reservation("10.1.0.51", client="AA-BB-CC-00-11-22")
    p = push.plan([scope(), a, b], [], {})
    found = reasons(p)
    assert "reservation 10.1.0.51 in this scope is for the same client" in found["reservation", a.key]
    assert ("reservation", b.key) in found


def test_second_reservation_for_a_client_on_windows_is_left_out(client):
    a, b = reservation("10.1.0.50"), reservation("10.1.0.51")
    managed = pushed(client, [scope(), a])
    p = push.plan([scope(), a, b], windows_records(client), managed)
    assert set(reasons(p)) == {("reservation", b.key)}


def test_one_client_in_two_scopes_is_fine():
    other = scope_at("10.2.0.0", "255.255.255.0", "10.2.0.10", "10.2.0.200")
    p = push.plan([scope(), other, reservation(), reservation("10.2.0.50", scope_id="10.2.0.0")], [], {})
    assert p.skipped == []


def test_two_v6_reservations_for_one_duid_and_iaid_are_left_out():
    a, b = reservation6(), reservation6(ip="2001:db8:1::11")
    p = push.plan([scope6(), a, b], [], {})
    assert {k for k, _ in reasons(p)} == {"reservation6"}
    assert len(p.skipped) == 2
    # Another IAID is another identity association of the same client: fine.
    p = push.plan([scope6(), a, reservation6(ip="2001:db8:1::11", iaid=8)], [], {})
    assert p.skipped == []


# -- item 4: policy ranges stored by a raw save ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "ranges",
    ["10.1.0.1-10.1.0.9", [["10.1.0.1"]], [["10.1.0.1", "nope"]], [["2001:db8::1", "2001:db8::2"]], [[1, 2]], {}],
)
def test_policy_with_unreadable_ranges_is_not_pushed_and_named(ranges):
    p = push.plan([scope(), policy(ip_ranges=ranges)], [], {})
    reason = reasons(p)["policy", ws.policy_key("10.1.0.0", "Printers")]
    assert reason.startswith("policy Printers: its stored IP ranges")
    assert [c.kind for c in p.changes] == ["scope"]


def test_reasons_are_creates_in_the_dry_run_summary():
    p = push.plan([scope(), reservation(ip="10.2.0.5")], [], {})
    (entry,) = p.summary()["not_pushed"]
    assert entry["action"] == CREATE and "outside the scope's prefix" in entry["reason"]


# -- the delete pause: while anything in a family cannot be pushed, nothing in that family is deleted ----------------


def deletes(p):
    return [(c.kind, c.key) for c in p.changes if c.action == DELETE]


def waiting(p):
    return {(s.change.kind, s.change.key) for s in p.skipped if s.change.action == DELETE}


def converge(client, source, managed):
    """Push until nothing is left, then check a second plan has nothing to do (no churn)."""
    managed = pushed(client, source, managed)
    again = push.plan(source, windows_records(client), managed)
    assert again.changes == [] and again.skipped == [], (again.changes, again.skipped)
    return managed


def paused_then_resumed(client, fake, before, broken, fixed, gone):
    """
    Push ``before``, then the ``broken`` NetBox state: nothing is deleted on Windows and ``gone`` waits. Then ``fixed``:
    the deletes happen and the push converges.
    """
    managed = pushed(client, before)
    on_windows = windows_records(client)
    p = push.plan(broken, on_windows, managed)
    assert deletes(p) == []
    assert set(gone) <= waiting(p), (gone, waiting(p))
    result = push.execute(p, WindowsWriter(client))
    assert not result.removals
    assert {(r.kind, r.key) for r in on_windows} <= {(r.kind, r.key) for r in windows_records(client)}
    # Still managed: the next push can delete them.
    state = dict(managed)
    state.update(result.upserts)
    assert set(gone) <= set(state)
    p = push.plan(fixed, windows_records(client), state)
    assert set(gone) <= set(deletes(p))
    return converge(client, fixed, state)


def test_s1_scope_moved_onto_a_range_holding_the_network_address_deletes_nothing(client, fake):
    before = [scope(), reservation()]
    broken = [
        scope_at("10.2.0.0", "255.255.255.0", "10.2.0.0", "10.2.0.200"),
        reservation("10.2.0.50", scope_id="10.2.0.0"),
    ]
    fixed = [
        scope_at("10.2.0.0", "255.255.255.0", "10.2.0.10", "10.2.0.200"),
        reservation("10.2.0.50", scope_id="10.2.0.0"),
    ]
    paused_then_resumed(
        client, fake, before, broken, fixed, [("scope", "10.1.0.0"), ("reservation", "10.1.0.0/10.1.0.50")]
    )
    assert set(fake.scopes) == {"10.2.0.0"}


def test_t1b_failover_following_a_refused_scope_keeps_its_scope(client, fake):
    before = [scope(), failover(scope_ids=["10.1.0.0"])]
    broken = [scope_at("10.2.0.0", "255.255.255.0", "10.2.0.0", "10.2.0.200"), failover(scope_ids=["10.2.0.0"])]
    managed = pushed(client, before)
    p = push.plan(broken, windows_records(client), managed)
    assert deletes(p) == []
    assert "waits for scope 10.2.0.0" in reasons(p)["failover", "ha"]
    push.execute(p, WindowsWriter(client))
    assert fake.failovers["ha"]["scope_ids"] == ["10.1.0.0"]
    assert "10.1.0.0" in fake.scopes


def test_failover_losing_a_scope_waits_while_something_is_refused(client, fake):
    other = scope_at("10.2.0.0", "255.255.255.0", "10.2.0.10", "10.2.0.200")
    managed = pushed(client, [scope(), other, failover(scope_ids=["10.1.0.0", "10.2.0.0"])])
    # The relationship lets 10.2.0.0 go while an unrelated scope cannot be pushed: leaving it would remove the scope
    # from the partner, so that waits too.
    broken = [
        scope(),
        other,
        failover(scope_ids=["10.1.0.0"]),
        scope_at("10.3.0.0", "255.255.255.0", "10.3.0.0", "10.3.0.9"),
    ]
    p = push.plan(broken, windows_records(client), managed)
    assert "waiting" in reasons(p)["failover", "ha"]
    push.execute(p, WindowsWriter(client))
    assert fake.failovers["ha"]["scope_ids"] == ["10.1.0.0", "10.2.0.0"]


def test_t3_reservation_moved_into_a_scope_holding_the_broadcast_address(client, fake):
    other = scope_at("10.2.0.0", "255.255.255.0", "10.2.0.10", "10.2.0.255")
    fixed_other = scope_at("10.2.0.0", "255.255.255.0", "10.2.0.10", "10.2.0.200")
    paused_then_resumed(
        client,
        fake,
        [scope(), reservation()],
        [scope(), other, reservation("10.2.0.50", scope_id="10.2.0.0")],
        [scope(), fixed_other, reservation("10.2.0.50", scope_id="10.2.0.0")],
        [("reservation", "10.1.0.0/10.1.0.50")],
    )
    assert set(fake.reservations) == {"10.2.0.50"}


def test_s9_split_with_the_upper_half_refused_keeps_its_reservation(client, fake):
    wide = scope_at("10.1.0.0", "255.255.254.0", "10.1.0.10", "10.1.1.200")
    managed = pushed(client, [wide, reservation("10.1.1.50")])
    lower = scope_at("10.1.0.0", "255.255.255.0", "10.1.0.10", "10.1.0.200")
    upper = scope_at("10.1.1.0", "255.255.255.0", "10.1.1.10", "10.1.1.255")
    p = push.plan([lower, upper, reservation("10.1.1.50", scope_id="10.1.1.0")], windows_records(client), managed)
    assert deletes(p) == []
    assert ("reservation", "10.1.0.0/10.1.1.50") in waiting(p)
    push.execute(p, WindowsWriter(client))
    assert "10.1.1.50" in fake.reservations


def test_s11_exclusion_moved_to_another_scope_onto_an_existing_one(client, fake):
    other = scope_at("10.2.0.0", "255.255.255.0", "10.2.0.10", "10.2.0.200")
    a, b = exclusion("10.1.0.20", "10.1.0.30"), exclusion("10.2.0.20", "10.2.0.30", scope_id="10.2.0.0")
    paused_then_resumed(
        client,
        fake,
        [scope(), other, a, b],
        [scope(), other, b, exclusion("10.2.0.25", "10.2.0.40", scope_id="10.2.0.0")],
        [scope(), other, b, exclusion("10.2.0.35", "10.2.0.40", scope_id="10.2.0.0")],
        [("exclusion", a.key)],
    )


@pytest.mark.parametrize("new_ip", ["10.1.0.60", "10.1.0.50"])
def test_s7_s14_reservation_losing_its_client_id(client, fake, new_ip):
    # S7 moves the address as well, S14 keeps it (then the Windows twin is held as before).
    before = [scope(), reservation(), reservation("10.1.0.70", client="aa-bb-cc-00-11-70")]
    broken = [scope(), reservation(new_ip, client=""), reservation("10.1.0.71", client="aa-bb-cc-00-11-70")]
    fixed = [scope(), reservation(new_ip), reservation("10.1.0.71", client="aa-bb-cc-00-11-70")]
    paused_then_resumed(client, fake, before, broken, fixed, [("reservation", "10.1.0.0/10.1.0.70")])
    assert set(fake.reservations) == {new_ip, "10.1.0.71"}


def test_v6_prefix_widened_in_ipam_deletes_nothing(client, fake):
    old = [scope6(), reservation6(), option6("2001:db8:1::", 23, ["2001:db8:1::53"])]
    new_prefix = "2001:db8:2::"
    broken = [
        scope6(prefix="2001:db8::", prefix_length=48),
        reservation6(ip="2001:db8::10", prefix="2001:db8::"),
        option6("2001:db8::", 23, ["2001:db8:1::53"]),
    ]
    fixed = [
        scope6(prefix=new_prefix),
        reservation6(ip="2001:db8:2::10", prefix=new_prefix),
        option6(new_prefix, 23, ["2001:db8:1::53"]),
    ]
    paused_then_resumed(
        client,
        fake,
        old,
        broken,
        fixed,
        [
            ("scope6", ws.scope6_key("2001:db8:1::")),
            ("reservation6", ws.reservation6_key("2001:db8:1::", "2001:db8:1::10")),
        ],
    )


def test_policy_renamed_with_broken_ranges_keeps_the_old_one(client, fake):
    old = policy()
    broken = policy(name="Printers2", ip_ranges=[["10.1.0.120", "10.1.0.100"]])
    paused_then_resumed(
        client,
        fake,
        [scope(), old],
        [scope(), broken],
        [scope(), policy(name="Printers2")],
        [("policy", old.key)],
    )


def test_a_reason_that_has_nothing_to_do_with_the_delete_pauses_it_too(client, fake):
    managed = pushed(client, [scope(), reservation()])
    no_range = scope_at("10.3.0.0", "255.255.255.0", "", "")
    p = push.plan([scope(), no_range], windows_records(client), managed)
    reason = reasons(p)["reservation", "10.1.0.0/10.1.0.50"]
    assert "nothing in the IPv4 scopes is deleted while scope 10.3.0.0 cannot be pushed" in reason
    # The other family and server-level objects are not held up.
    assert deletes(p) == []


def test_the_pause_names_three_blocking_objects_then_counts_the_rest(client):
    managed = pushed(client, [scope(), reservation()])
    broken = [scope()] + [scope_at(f"10.{n}.0.0", "255.255.255.0", "", "") for n in range(3, 8)]
    reason = reasons(push.plan(broken, windows_records(client), managed))["reservation", "10.1.0.0/10.1.0.50"]
    assert reason.count("scope 10.") == 3 and "and 2 more" in reason


def test_families_pause_independently(client, fake):
    managed = pushed(client, [scope(), reservation(), scope6(), reservation6()])
    # A refused DHCPv6 scope does not hold up IPv4 deletes, and the other way round.
    refused6 = scope6(prefix="2001:db8:9::", prefix_length=48)
    p = push.plan([scope(), scope6(), refused6], windows_records(client), managed)
    assert ("reservation", "10.1.0.0/10.1.0.50") in deletes(p)
    assert waiting(p) == {("reservation6", ws.reservation6_key("2001:db8:1::", "2001:db8:1::10"))}
    no_range = scope_at("10.3.0.0", "255.255.255.0", "", "")
    p = push.plan([scope(), no_range, scope6()], windows_records(client), managed)
    assert deletes(p) == [("reservation6", ws.reservation6_key("2001:db8:1::", "2001:db8:1::10"))]


def test_server_level_deletes_are_not_paused(client):
    phones = Record(
        "class", "user/phones", {"name": "Phones", "class_type": "user", "data": "phone", "description": ""}
    )
    managed = pushed(client, [phones, scope()])
    p = push.plan([scope(), scope_at("10.3.0.0", "255.255.255.0", "", "")], windows_records(client), managed)
    assert deletes(p) == [("class", "user/phones")]


def test_policy_ranges_must_run_upwards_inside_the_prefix():
    for ranges in ([["10.1.0.120", "10.1.0.100"]], [["10.1.0.100", "10.1.1.5"]]):
        p = push.plan([scope(), policy(ip_ranges=ranges)], [], {})
        assert "reversed or outside the scope's prefix" in reasons(p)["policy", ws.policy_key("10.1.0.0", "Printers")]


def test_writer_refuses_bad_policy_ranges_before_touching_windows(client, fake):
    pushed(client, [scope(), policy()])
    before = dict(fake.policy_ranges)
    change = push.Change(
        "update", "policy", policy().key, before=policy().fields, after=policy(ip_ranges=[["10.1.0.9", "x"]]).fields
    )
    with pytest.raises(push.PushRefused):
        WindowsWriter(client).apply(change)
    assert fake.policy_ranges == before


# -- kept from the first review round ---------------------------------------------------------------------------------


def test_exclusions_sharing_one_address_overlap():
    a, b = exclusion("10.1.0.10", "10.1.0.20"), exclusion("10.1.0.20", "10.1.0.30")
    assert set(reasons(push.plan([scope(), a, b], [], {}))) == {("exclusion", a.key), ("exclusion", b.key)}


def test_free_text_client_ids_match_whatever_their_separators():
    a, b = reservation("10.1.0.50", client="AABBCC001122"), reservation("10.1.0.51", client="aa:bb:cc:00:11:22")
    assert len(push.plan([scope(), a, b], [], {}).skipped) == 2


def test_scope_sharing_addresses_with_a_hand_made_windows_scope_is_not_pushed(client):
    pushed(client, [scope()])  # on Windows, but NetBox does not manage it
    newcomer = scope_at("10.1.0.128", "255.255.255.128", "10.1.0.130", "10.1.0.140")
    p = push.plan([newcomer], windows_records(client), {})
    assert "made on Windows outside NetBox" in reasons(p)["scope", "10.1.0.128"]
    # A managed scope NetBox deleted goes first in the same push, so the new one can follow.
    managed = {("scope", "10.1.0.0"): push.fingerprint(windows_records(client)[0])}
    assert push.plan([newcomer], windows_records(client), managed).skipped == []


def test_children_of_a_second_scope_for_one_network_say_why():
    doubled = Record("scope", "10.1.0.0#7", {**scope(subnet_mask="255.255.254.0").fields, "_duplicate": 7})
    p = push.plan([scope(), doubled, reservation("10.1.1.50", scope_id="10.1.0.0#7")], [], {})
    assert "is a second NetBox scope for one Windows scope" in reasons(p)["reservation", "10.1.0.0#7/10.1.1.50"]


def test_a_large_plan_with_refusals_stays_fast():
    import time

    def records(scope_id):
        out = [scope_at(scope_id, "255.255.224.0", "10.1.0.10", "10.1.31.200")]
        for i in range(4000):
            ip = f"10.1.{i // 200}.{10 + i % 200}"
            out.append(reservation(ip, client=f"02-00-00-00-{i // 256:02x}-{i % 256:02x}", scope_id=scope_id))
        return out

    target = records("10.1.0.0")
    managed = {(r.kind, r.key): push.fingerprint(r) for r in target}
    started = time.monotonic()
    p = push.plan(records("10.2.0.0"), target, managed)
    elapsed = time.monotonic() - started
    assert deletes(p) == [] and len(waiting(p)) == 4001
    assert elapsed < 1.5, elapsed
