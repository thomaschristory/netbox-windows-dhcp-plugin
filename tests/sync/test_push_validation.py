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
    # The /23 is the /24 already on Windows with another mask: refused for that first.
    assert "the subnet mask changes from 255.255.255.0 to 255.255.254.0" in found["scope", "10.1.0.0"]


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


# -- third review round -----------------------------------------------------------------------------------------------


UNRELATED = scope_at("10.3.0.0", "255.255.255.0", "", "")  # refused: no IP range


def push_until_done(client, source, managed, limit=2):
    """Push at most ``limit`` times until a plan is empty; then one more plan must be empty too."""
    for _ in range(limit):
        p = push.plan(source, windows_records(client), managed)
        if not p.changes and not p.skipped:
            break
        managed = pushed(client, source, managed)
    p = push.plan(source, windows_records(client), managed)
    assert p.changes == [] and p.skipped == [], (p.changes, p.skipped)
    return managed


@pytest.mark.parametrize("keep_one", [False, True])
def test_scope_moved_between_two_managed_relationships_converges(client, fake, keep_one):
    s2 = scope_at("10.2.0.0", "255.255.255.0", "10.2.0.10", "10.2.0.200")
    s3 = scope_at("10.3.0.0", "255.255.255.0", "10.3.0.10", "10.3.0.200")
    first = ["10.1.0.0", "10.3.0.0"] if keep_one else ["10.1.0.0"]
    managed = pushed(client, [scope(), s2, s3, failover(scope_ids=first), failover(name="HA2", scope_ids=["10.2.0.0"])])
    rest = ["10.3.0.0"] if keep_one else []
    moved = [scope(), s2, s3, failover(scope_ids=rest), failover(name="HA2", scope_ids=["10.1.0.0", "10.2.0.0"])]
    push_until_done(client, moved, managed)
    assert sorted(fake.failovers["ha2"]["scope_ids"]) == ["10.1.0.0", "10.2.0.0"]
    assert fake.failovers["ha"]["scope_ids"] == rest


def test_scope_moved_into_a_new_relationship_converges(client, fake):
    managed = pushed(client, [scope(), failover(scope_ids=["10.1.0.0"])])
    moved = [scope(), failover(scope_ids=[]), failover(name="HA2", scope_ids=["10.1.0.0"])]
    push_until_done(client, moved, managed)
    assert fake.failovers["ha2"]["scope_ids"] == ["10.1.0.0"]


def test_scope_held_by_a_relationship_not_released_still_refuses_and_pauses(client, fake):
    # HA (managed) keeps the scope in NetBox too: HA2 cannot take it, and that refusal holds back the deletes.
    managed = pushed(client, [scope(), reservation(), failover(scope_ids=["10.1.0.0"])])
    p = push.plan(
        [scope(), failover(scope_ids=["10.1.0.0"]), failover(name="HA2", scope_ids=["10.1.0.0"])],
        windows_records(client),
        managed,
    )
    assert "already in failover relationship HA" in reasons(p)["failover", "ha2"]
    assert ("reservation", "10.1.0.0/10.1.0.50") in waiting(p)


def test_hand_made_relationship_refusal_pauses_deletes(client, fake):
    s2 = scope_at("10.2.0.0", "255.255.255.0", "10.2.0.10", "10.2.0.200")
    pushed(client, [s2, failover(name="ByHand", scope_ids=["10.2.0.0"])])
    managed = {k: v for k, v in pushed(client, [scope(), s2, reservation()]).items() if k[0] != "failover"}
    p = push.plan([scope(), s2, failover(scope_ids=["10.2.0.0"])], windows_records(client), managed)
    assert "already in failover relationship ByHand" in reasons(p)["failover", "ha"]
    assert ("reservation", "10.1.0.0/10.1.0.50") in waiting(p)


@pytest.mark.parametrize("split", [True, False])
def test_mask_change_is_refused_in_the_plan_and_deletes_nothing(client, fake, split):
    wide = scope_at("10.1.0.0", "255.255.254.0", "10.1.0.10", "10.1.1.200")
    lower = scope_at("10.1.0.0", "255.255.255.0", "10.1.0.10", "10.1.0.200")
    upper = scope_at("10.1.1.0", "255.255.255.0", "10.1.1.10", "10.1.1.200")
    if split:
        before, after = [wide, reservation("10.1.1.50")], [lower, upper, reservation("10.1.1.50", scope_id="10.1.1.0")]
    else:
        before, after = [lower, upper, reservation("10.1.1.50", scope_id="10.1.1.0")], [wide, reservation("10.1.1.50")]
    managed = pushed(client, before)
    on_windows = windows_records(client)
    p = push.plan(after, on_windows, managed)
    assert "the subnet mask changes from" in reasons(p)["scope", "10.1.0.0"]
    assert deletes(p) == []
    result = push.execute(p, WindowsWriter(client))
    # The split's new upper scope would collide with the /23 Windows still has: refused, not failed at Windows.
    assert not result.failed and not result.applied
    assert windows_records(client) == on_windows


def test_policy_renamed_past_the_end_without_any_delete_keeps_the_old_one(client, fake):
    managed = pushed(client, [scope(), policy(), option("10.1.0.0", 3, ["10.1.0.1"], policy="Printers")])
    renamed = [
        scope(),
        policy(name="Printers2", processing_order=5),
        option("10.1.0.0", 3, ["10.1.0.1"], policy="Printers2"),
    ]
    p = push.plan(renamed, windows_records(client), managed)
    assert deletes(p) == []
    assert {("policy", "10.1.0.0/printers"), ("option", "10.1.0.0/3;policy=printers")} <= waiting(p)


def test_server_policy_renamed_and_refused_keeps_the_old_one_and_its_value(client, fake):
    old = policy(scope_id="", ip_ranges=[])
    plain = option("server", 15, ["corp.example"])
    managed = pushed(client, [old, option("server", 3, ["10.1.0.1"], policy="Printers"), plain])
    renamed = policy(name="P2", scope_id="", ip_ranges=[], criteria={})
    p = push.plan([renamed, option("server", 3, ["10.1.0.1"], policy="P2")], windows_records(client), managed)
    assert {("policy", old.key), ("option", "server/3;policy=printers")} <= waiting(p)
    assert "nothing in the server-level policies is deleted" in reasons(p)["policy", old.key]
    # A plain server option value is in no family: deleted as before.
    assert ("option", "server/15") in deletes(p)


def test_superscope_delete_waits_too(client, fake):
    managed = pushed(client, [scope(), Record("superscope", "campus", {"name": "Campus", "scope_ids": ["10.1.0.0"]})])
    p = push.plan([scope(), UNRELATED], windows_records(client), managed)
    assert ("superscope", "campus") in waiting(p)


def test_parent_outside_the_family_waits_for_its_paused_child(client, fake):
    phones = Record(
        "class", "user/phones", {"name": "Phones", "class_type": "user", "data": "phone", "description": ""}
    )
    pol = policy(criteria={"user_class": ["EQ", "Phones"]})
    managed = pushed(client, [phones, scope(), pol])
    broken = policy(name="P2", criteria={"user_class": ["EQ", "Phones"]}, ip_ranges=[["10.1.0.120", "10.1.0.100"]])
    p = push.plan([scope(), broken], windows_records(client), managed)
    assert "still holds policy 10.1.0.0/printers" in reasons(p)["class", "user/phones"]
    push.execute(p, WindowsWriter(client))
    assert "phones" in {k[1].lower() for k in fake.classes}


def test_waiting_children_do_not_depend_on_record_order():
    # Children listed before their parents: an option value of a reservation of a refused scope, and a failover
    # relationship before its scope.
    source = [
        option("10.1.0.0/10.1.0.50", 15, ["x"]),
        reservation(),
        failover(scope_ids=["10.1.0.0"]),
        scope(start_range="", end_range=""),
    ]
    found = reasons(push.plan(source, [], {}))
    assert found["option", "10.1.0.0/10.1.0.50/15"].startswith("waits for")
    assert "waits for scope 10.1.0.0" in found["reservation", "10.1.0.0/10.1.0.50"]
    assert "waits for scope 10.1.0.0" in found["failover", "ha"]


@pytest.mark.parametrize(
    ("before", "after", "kind"),
    [
        ([exclusion("10.1.0.20", "10.1.0.30")], [exclusion("10.1.0.25", "10.1.0.35")], "exclusion"),
        ([reservation()], [reservation("10.1.0.60")], "reservation"),
        ([failover(scope_ids=["10.1.0.0"])], [failover(name="HA2", scope_ids=["10.1.0.0"])], "failover"),
    ],
)
def test_create_clashing_with_a_waiting_delete_waits_instead_of_failing(client, fake, before, after, kind):
    managed = pushed(client, [scope(), *before])
    p = push.plan([scope(), *after, UNRELATED], windows_records(client), managed)
    new = after[0]
    assert reasons(p)[new.kind, new.key].startswith("waiting: needs")
    result = push.execute(p, WindowsWriter(client))
    assert not result.failed
    # Fixed: the old one goes and the new one comes, in one push.
    push_until_done(client, [scope(), *after], managed)


def test_waiting_deletes_are_listed_first_whatever_the_cap(client):
    managed = pushed(client, [scope(), reservation()])
    # Refused for having no value, and sorted before the waiting delete by key.
    many = [option("server", 200 + i % 50, [], user_class=f"c{i}") for i in range(push.PLANNED_LIMIT + 5)]
    p = push.plan([scope(), UNRELATED, *many], windows_records(client), managed)
    summary = p.summary()
    assert summary["waiting_count"] == 1
    assert summary["not_pushed"][0]["key"] == "10.1.0.0/10.1.0.50"
    assert summary["not_pushed_truncated"] is True


def test_fake_refuses_what_windows_refuses(client):
    from netbox_windows_dhcp_plugin.windows import client as wc
    from netbox_windows_dhcp_plugin.windows.psscript import PSError

    client.add_scope(wc.Scope(name="a", start_range="10.1.0.10", end_range="10.1.0.200", subnet_mask="255.255.255.0"))
    inner = wc.Scope(name="b", start_range="10.1.0.130", end_range="10.1.0.140", subnet_mask="255.255.255.128")
    with pytest.raises(PSError, match="ResourceExists"):
        client.add_scope(inner)
    client.add_exclusion_range(wc.ExclusionRange("10.1.0.0", "10.1.0.20", "10.1.0.40"))
    for start, end in (("10.1.0.30", "10.1.0.50"), ("10.1.0.25", "10.1.0.35"), ("10.1.0.40", "10.1.0.45")):
        with pytest.raises(PSError, match="InvalidData"):
            client.add_exclusion_range(wc.ExclusionRange("10.1.0.0", start, end))
    client.add_reservation(wc.Reservation(scope_id="10.1.0.0", ip_address="10.1.0.50", client_id="aa-bb-cc-00-11-22"))
    with pytest.raises(PSError, match="ResourceExists"):
        client.add_reservation(
            wc.Reservation(scope_id="10.1.0.0", ip_address="10.1.0.51", client_id="aa-bb-cc-00-11-22")
        )
    client.add_v6_scope(wc.ScopeV6(prefix="2001:db8:1::", name="v6"))
    duid = "00-01-00-01-aa-bb-cc-dd"
    client.add_v6_reservation(wc.ReservationV6("2001:db8:1::", "2001:db8:1::10", duid, 7, "a", ""))
    with pytest.raises(PSError, match="ResourceExists"):
        client.add_v6_reservation(wc.ReservationV6("2001:db8:1::", "2001:db8:1::11", duid, 7, "b", ""))
    client.add_v6_reservation(wc.ReservationV6("2001:db8:1::", "2001:db8:1::12", duid, 8, "c", ""))


# -- final review round -----------------------------------------------------------------------------------------------

S2 = scope_at("10.2.0.0", "255.255.255.0", "10.2.0.10", "10.2.0.200")
S3 = scope_at("10.3.0.0", "255.255.255.0", "10.3.0.10", "10.3.0.200")


def three_policies(scope_id="10.1.0.0"):
    ranges = (lambda a, b: [[f"10.1.0.{a}", f"10.1.0.{b}"]]) if scope_id else (lambda a, b: [])
    return [
        policy(name="A", scope_id=scope_id, processing_order=1, ip_ranges=ranges(100, 105)),
        policy(name="B", scope_id=scope_id, processing_order=2, ip_ranges=ranges(110, 115)),
        policy(name="C", scope_id=scope_id, processing_order=3, ip_ranges=ranges(120, 125)),
    ]


@pytest.mark.parametrize("scope_id", ["10.1.0.0", ""])
def test_policy_renamed_to_an_order_that_can_never_fit_keeps_the_old_one(client, fake, scope_id):
    a, b, c = three_policies(scope_id)
    parent = scope_id or "server"
    managed = pushed(client, [scope(), a, b, c, option(parent, 3, ["10.1.0.1"], policy="A")])
    # NetBox holds three policies at the level, so order 4 never fits, whatever the deletes.
    p_new = policy(name="P", scope_id=scope_id, processing_order=4, ip_ranges=a.fields["ip_ranges"])
    source = [scope(), p_new, b, c, option(parent, 3, ["10.1.0.1"], policy="P")]
    p = push.plan(source, windows_records(client), managed)
    assert deletes(p) == []
    assert {("policy", a.key), ("option", f"{parent}/3;policy=a")} <= waiting(p)
    assert "waits for policy" in reasons(p)["option", f"{parent}/3;policy=p"]
    result = push.execute(p, WindowsWriter(client))
    assert not result.failed and not result.applied
    assert sorted((v["processing_order"], v["name"]) for v in fake.policies.values()) == [(1, "A"), (2, "B"), (3, "C")]


def test_failover_changes_the_writer_refuses_are_refused_in_the_plan(client):
    managed = pushed(client, [scope(), failover(mode="hotstandby", server_role="active", load_balance_percent=None)])
    on_windows = windows_records(client)
    cases = {
        "partner": failover(mode="hotstandby", server_role="active", load_balance_percent=None, partner_server="x.y"),
        "role": failover(mode="hotstandby", server_role="standby", load_balance_percent=None),
    }
    for name, new in cases.items():
        reason = reasons(push.plan([scope(), new], on_windows, managed))["failover", "ha"]
        assert "cannot change" in reason, name


def test_scope_move_to_a_relationship_whose_partner_changes_waits(client, fake):
    managed = pushed(
        client, [scope(), S2, failover(scope_ids=["10.1.0.0"]), failover(name="HA2", scope_ids=["10.2.0.0"])]
    )
    taker = failover(name="HA2", scope_ids=["10.1.0.0", "10.2.0.0"], partner_server="dhcp3.example.com")
    p = push.plan([scope(), S2, failover(scope_ids=[]), taker], windows_records(client), managed)
    assert "partner server cannot change" in reasons(p)["failover", "ha2"]
    assert ("failover", "ha") in {(s.change.kind, s.change.key) for s in p.skipped if s.waiting}
    push.execute(p, WindowsWriter(client))
    assert fake.failovers["ha"]["scope_ids"] == ["10.1.0.0"]


def test_taker_failing_at_windows_puts_the_scope_back(client, fake):
    from netbox_windows_dhcp_plugin.windows.psscript import PSError

    managed = pushed(client, [scope(), failover(scope_ids=["10.1.0.0"])])
    moved = [scope(), failover(scope_ids=[]), failover(name="HA2", scope_ids=["10.1.0.0"], partner_server="dhcp3.x")]
    fake.fail = lambda op, p: (
        PSError("partner unreachable", "ConnectionError", "", op) if op == "failover.add" else None
    )
    p = push.plan(moved, windows_records(client), managed)
    result = push.execute(p, WindowsWriter(client))
    ((change, error),) = result.failed
    assert change.key == "ha2" and "scope(s) 10.1.0.0 put back into failover HA" in error
    assert fake.failovers["ha"]["scope_ids"] == ["10.1.0.0"]
    # The relationship is recorded as Windows now holds it, so it is not taken for a change made on Windows.
    state = {**managed, **result.upserts}
    assert push.unchanged(state["failover", "ha"], next(r for r in windows_records(client) if r.kind == "failover"))
    # Once the partner answers, the move goes through.
    fake.fail = None
    push_until_done(client, moved, state)
    assert fake.failovers["ha2"]["scope_ids"] == ["10.1.0.0"]


def test_scopes_swapped_between_relationships_are_refused_and_pause(client, fake):
    before = [
        scope(),
        S2,
        reservation(),
        failover(scope_ids=["10.1.0.0"]),
        failover(name="HA2", scope_ids=["10.2.0.0"]),
    ]
    managed = pushed(client, before)
    swapped = [scope(), S2, failover(scope_ids=["10.2.0.0"]), failover(name="HA2", scope_ids=["10.1.0.0"])]
    p = push.plan(swapped, windows_records(client), managed)
    for key in ("ha", "ha2"):
        assert "move one scope at a time" in reasons(p)["failover", key]
    assert ("reservation", "10.1.0.0/10.1.0.50") in waiting(p)
    result = push.execute(p, WindowsWriter(client))
    assert not result.failed and not result.applied


@pytest.mark.parametrize("names", [("HA", "HA2", "HA3"), ("HB", "HA", "HC"), ("Z1", "M1", "A1")])
def test_chained_moves_converge(client, fake, names):
    # first lets 10.1 go to second, which lets 10.2 go to third: the order follows the moves, not the names.
    first, second, third = names
    managed = pushed(
        client,
        [
            scope(),
            S2,
            failover(name=first, scope_ids=["10.1.0.0"]),
            failover(name=second, scope_ids=["10.2.0.0"]),
            failover(name=third, scope_ids=[], partner_server="dhcp3.example.com"),
        ],
    )
    moved = [
        scope(),
        S2,
        failover(name=first, scope_ids=[]),
        failover(name=second, scope_ids=["10.1.0.0"]),
        failover(name=third, scope_ids=["10.2.0.0"], partner_server="dhcp3.example.com"),
    ]
    push_until_done(client, moved, managed, limit=1)
    assert {k: v["scope_ids"] for k, v in fake.failovers.items()} == {
        first.lower(): [],
        second.lower(): ["10.1.0.0"],
        third.lower(): ["10.2.0.0"],
    }


def test_a_refused_relationship_releases_nothing(client, fake):
    managed = pushed(
        client, [scope(), S2, S3, failover(scope_ids=["10.1.0.0"]), failover(name="HA2", scope_ids=["10.2.0.0"])]
    )
    WindowsWriter(client).apply(
        push.Change("create", "failover", "hm", after=failover(name="HM", scope_ids=["10.3.0.0"]).fields)
    )
    # HA lets 10.1.0.0 go but takes 10.3.0.0, held by a hand-made relationship: HA is refused, so HA2 cannot take
    # 10.1.0.0 either, rather than failing at Windows on every push.
    moved = [
        scope(),
        S2,
        S3,
        failover(scope_ids=["10.3.0.0"]),
        failover(name="HA2", scope_ids=["10.1.0.0", "10.2.0.0"]),
    ]
    p = push.plan(moved, windows_records(client), managed)
    assert "relationship HM" in reasons(p)["failover", "ha"]
    assert "relationship HA on Windows" in reasons(p)["failover", "ha2"]
    result = push.execute(p, WindowsWriter(client))
    assert not result.failed


def test_children_of_a_waiting_create_wait_too(client):
    managed = pushed(client, [scope(), reservation()])
    narrow = scope_at("10.1.0.128", "255.255.255.128", "10.1.0.130", "10.1.0.200")
    p = push.plan(
        [narrow, reservation("10.1.0.150", scope_id="10.1.0.128"), UNRELATED], windows_records(client), managed
    )
    assert p.changes == []
    assert reasons(p)["scope", "10.1.0.128"].startswith("waiting: needs scope 10.1.0.0")
    assert reasons(p)["reservation", "10.1.0.128/10.1.0.150"].startswith("waiting: needs scope 10.1.0.128")
    assert p.summary()["waiting_count"] == len(waiting(p)) + 2


def test_many_rekeyed_scopes_plan_quickly():
    import time

    def at(i, half):
        a, b = divmod(i, 256)
        key = f"{10 + a}.{b}.0.{128 if half else 0}"
        mask = "255.255.255.128" if half else "255.255.255.0"
        return scope_at(key, mask, f"{10 + a}.{b}.0.{130 if half else 10}", f"{10 + a}.{b}.0.200")

    target = [at(i, False) for i in range(6000)]
    managed = {(r.kind, r.key): push.fingerprint(r) for r in target}
    started = time.monotonic()
    refused = scope_at("172.16.0.0", "255.255.255.0", "", "")  # outside the 10.x scopes above
    p = push.plan([at(i, True) for i in range(6000)] + [refused], target, managed)
    assert time.monotonic() - started < 2.0
    assert p.summary()["waiting_count"] == 12000


# -- put-back records what Windows holds ------------------------------------------------------------------------------

S4 = scope_at("10.4.0.0", "255.255.255.0", "10.4.0.10", "10.4.0.200")
S5 = scope_at("10.5.0.0", "255.255.255.0", "10.5.0.10", "10.5.0.200")


def only_ha2_fails(op, p):
    from netbox_windows_dhcp_plugin.windows.psscript import PSError

    if op == "failover.add_scope" and p.get("name", "").lower() == "ha2":
        return PSError("partner unreachable", "ConnectionError", "", op)
    return None


def failed_once_then_converges(client, fake, before, moved, expected):
    """Push ``moved`` with HA2 failing at Windows, then without: it converges on the next push, nothing after."""
    managed = pushed(client, before)
    fake.fail = only_ha2_fails
    p = push.plan(moved, windows_records(client), managed)
    result = push.execute(p, WindowsWriter(client))
    assert any("put back into failover HA" in error for _, error in result.failed), result.failed
    state = {**managed, **result.upserts}
    for k in result.removals:
        state.pop(k, None)
    fake.fail = None
    push_until_done(client, moved, state, limit=1)
    assert {k: sorted(v["scope_ids"]) for k, v in fake.failovers.items()} == expected


HA2 = dict(name="HA2", partner_server="dhcp3.x")


def test_put_back_when_the_releaser_also_changed_a_setting(client, fake):
    failed_once_then_converges(
        client,
        fake,
        [scope(), S2, S4, failover(scope_ids=["10.1.0.0", "10.2.0.0"]), failover(scope_ids=["10.4.0.0"], **HA2)],
        [
            scope(),
            S2,
            S4,
            failover(scope_ids=["10.2.0.0"], max_client_lead_time=7200),
            failover(scope_ids=["10.1.0.0", "10.4.0.0"], **HA2),
        ],
        {"ha": ["10.2.0.0"], "ha2": ["10.1.0.0", "10.4.0.0"]},
    )
    assert fake.failovers["ha"]["max_client_lead_time"] == 7200


def test_put_back_when_the_releaser_also_let_another_scope_go(client, fake):
    failed_once_then_converges(
        client,
        fake,
        [scope(), S2, S4, failover(scope_ids=["10.1.0.0", "10.2.0.0"]), failover(scope_ids=["10.4.0.0"], **HA2)],
        [scope(), S2, S4, failover(scope_ids=[]), failover(scope_ids=["10.1.0.0", "10.4.0.0"], **HA2)],
        {"ha": [], "ha2": ["10.1.0.0", "10.4.0.0"]},
    )


def test_put_back_when_another_scope_of_the_releaser_was_deleted(client, fake):
    failed_once_then_converges(
        client,
        fake,
        [scope(), S2, S4, failover(scope_ids=["10.1.0.0", "10.2.0.0"]), failover(scope_ids=["10.4.0.0"], **HA2)],
        [scope(), S4, failover(scope_ids=[]), failover(scope_ids=["10.1.0.0", "10.4.0.0"], **HA2)],
        {"ha": [], "ha2": ["10.1.0.0", "10.4.0.0"]},
    )
    assert "10.2.0.0" not in fake.scopes


def test_put_back_with_one_releaser_and_two_takers(client, fake):
    ha3 = dict(name="HA3", partner_server="dhcp4.x")
    failed_once_then_converges(
        client,
        fake,
        [
            scope(),
            S2,
            S4,
            S5,
            failover(scope_ids=["10.1.0.0", "10.2.0.0"]),
            failover(scope_ids=["10.4.0.0"], **HA2),
            failover(scope_ids=["10.5.0.0"], **ha3),
        ],
        [
            scope(),
            S2,
            S4,
            S5,
            failover(scope_ids=[]),
            failover(scope_ids=["10.1.0.0", "10.4.0.0"], **HA2),
            failover(scope_ids=["10.2.0.0", "10.5.0.0"], **ha3),
        ],
        {"ha": [], "ha2": ["10.1.0.0", "10.4.0.0"], "ha3": ["10.2.0.0", "10.5.0.0"]},
    )


def test_put_back_does_not_leave_other_deletes_waiting(client, fake):
    before = [
        scope(),
        S2,
        S3,
        S4,
        failover(scope_ids=["10.1.0.0", "10.2.0.0"]),
        failover(scope_ids=["10.4.0.0"], **HA2),
    ]
    moved = [scope(), S2, S4, failover(scope_ids=["10.2.0.0"], max_client_lead_time=7200)]
    moved.append(failover(scope_ids=["10.1.0.0", "10.4.0.0"], **HA2))
    failed_once_then_converges(client, fake, before, moved, {"ha": ["10.2.0.0"], "ha2": ["10.1.0.0", "10.4.0.0"]})
    assert "10.3.0.0" not in fake.scopes


def test_lost_connection_on_the_taker_says_where_the_scope_is(client, fake):
    from netbox_windows_dhcp_plugin.windows.runner import RunnerError

    managed = pushed(client, [scope(), S4, failover(scope_ids=["10.1.0.0"]), failover(scope_ids=["10.4.0.0"], **HA2)])
    moved = [scope(), S4, failover(scope_ids=[]), failover(scope_ids=["10.1.0.0", "10.4.0.0"], **HA2)]
    p = push.plan(moved, windows_records(client), managed)

    class Writer(WindowsWriter):
        def apply(self, change):
            if change.key == "ha2":
                raise RunnerError("connection lost")
            super().apply(change)

    result = push.execute(p, Writer(client))
    assert result.aborted
    ((_, error),) = result.failed
    assert "10.1.0.0" in error and "in no failover relationship now, and the next push adds them to HA2" in error
    state = {**managed, **result.upserts}
    push_until_done(client, moved, state, limit=1)
    assert sorted(fake.failovers["ha2"]["scope_ids"]) == ["10.1.0.0", "10.4.0.0"]


def test_values_under_a_policy_refused_only_for_its_order_are_still_written(client, fake):
    old = option("10.1.0.0", 3, ["10.1.0.1"], policy="Printers")
    managed = pushed(client, [scope(), policy(), old])
    # The policy is on Windows already: refused for its number only, so its new value is created as usual and the old
    # one goes, never leaving the policy without either.
    source = [scope(), policy(processing_order=9), option("10.1.0.0", 6, ["10.1.0.1"], policy="Printers")]
    p = push.plan(source, windows_records(client), managed)
    assert [(c.action, c.key) for c in p.changes] == [
        ("delete", "10.1.0.0/3;policy=printers"),
        ("create", "10.1.0.0/6;policy=printers"),
    ]
    result = push.execute(p, WindowsWriter(client))
    assert not result.failed
    assert {k[0] for k in fake.options if "printers" in str(k).lower()} == {6}
