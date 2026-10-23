import pytest

from netbox_windows_dhcp_plugin.sync import push
from netbox_windows_dhcp_plugin.sync import windows_state as ws
from netbox_windows_dhcp_plugin.sync.diff import Record
from netbox_windows_dhcp_plugin.sync.windows_writer import WindowsWriter
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import OptionKey, Policy, Reservation, Scope
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import PSError
from netbox_windows_dhcp_plugin.windows.runner import RunnerError

from .test_windows_writer import failover, option, policy, scope, wanted, windows_records


@pytest.fixture
def fake():
    return FakeDhcpServer()


@pytest.fixture
def client(fake):
    return DhcpClient(fake)


def run(client, source, managed=None):
    """One push: plan against what the fake holds, apply, return (plan, result)."""
    p = push.plan(source, windows_records(client), managed or {})
    return p, push.execute(p, WindowsWriter(client))


def state_after(managed, result):
    out = dict(managed)
    out.update(result.upserts)
    for k in result.removals:
        out.pop(k, None)
    return out


def test_first_push_creates_and_records_everything(client):
    p, result = run(client, wanted())
    assert not result.failed
    assert set(result.upserts) == {(r.kind, r.key) for r in wanted()}
    assert windows_records(client)
    # A second push finds nothing to do and keeps the same fingerprints.
    p2, result2 = run(client, wanted(), state_after({}, result))
    assert p2.changes == []
    assert result2.upserts == result.upserts


def test_objects_equal_on_both_sides_are_adopted(client):
    run(client, wanted())
    p, result = run(client, wanted(), managed={})
    assert p.changes == []
    assert set(result.upserts) == {(r.kind, r.key) for r in wanted()}


def test_unmanaged_windows_objects_are_left_alone(client, fake):
    run(client, wanted())
    p, result = run(client, [], managed={})
    assert p.changes == []
    assert len(p.unmanaged) == len(wanted())
    assert "10.1.0.0" in fake.scopes


def test_managed_objects_deleted_in_netbox_are_deleted(client, fake):
    _, first = run(client, wanted())
    managed = state_after({}, first)
    p, result = run(client, [], managed)
    assert not result.failed and not p.skipped
    assert fake.scopes == {}
    assert state_after(managed, result) == {}


def test_drifted_object_is_not_deleted(client, fake):
    _, first = run(client, wanted())
    managed = state_after({}, first)
    fake.scopes["10.1.0.0"]["name"] = "edited by hand"
    source = [r for r in wanted() if r.kind in ("class", "optiondef")]
    p, _ = run(client, source, managed)
    reasons = {(s.change.kind, s.change.key): s.reason for s in p.skipped}
    assert "changed on Windows" in reasons["scope", "10.1.0.0"]
    assert "10.1.0.0" in fake.scopes


def test_delete_that_would_cascade_unmanaged_children_is_skipped(client, fake):
    _, first = run(client, wanted())
    managed = state_after({}, first)
    # Someone adds a reservation by hand: removing the scope would take it with it.
    client.add_reservation(Reservation(scope_id="10.1.0.0", ip_address="10.1.0.99", client_id="aa-bb-cc-00-00-99"))
    source = [r for r in wanted() if r.kind in ("class", "optiondef")]
    p, result = run(client, source, managed)
    reasons = {(s.change.kind, s.change.key): s.reason for s in p.skipped}
    assert "not managed by NetBox" in reasons["scope", "10.1.0.0"]
    assert "10.1.0.0" in fake.scopes
    # The managed children were still removed.
    assert "10.1.0.50" not in fake.reservations


def test_stale_state_rows_are_dropped(client):
    managed = {("scope", "10.9.0.0"): "x"}
    p, result = run(client, [], managed)
    assert ("scope", "10.9.0.0") in result.removals


def test_duplicate_netbox_keys_are_conflicts(client):
    dup = Record("reservation", "10.1.0.0/10.1.0.50#7", {**wanted()[4].fields, push.DUPLICATE: 7})
    empty = Record("reservation", "10.1.0.0/#8", {**wanted()[4].fields, "ip_address": "", push.DUPLICATE: 8})
    p, _ = run(client, [*wanted(), dup, empty])
    assert {s.change.key for s in p.skipped} == {dup.key, empty.key}
    assert all(c.key not in (dup.key, empty.key) for c in p.changes)


def test_children_of_a_failed_create_are_skipped(client, fake):
    fake.fail = lambda op, p: (
        PSError("boom", "InvalidArgument", "", "Add-DhcpServerv4Scope") if op == "scope.add" else None
    )
    _, result = run(client, wanted())
    failed = {(c.kind, c.key) for c, _ in result.failed}
    skipped = {(c.kind, c.key) for c, _ in result.skipped}
    assert failed == {("scope", "10.1.0.0")}
    assert ("reservation", "10.1.0.0/10.1.0.50") in skipped
    assert ("option", "10.1.0.0/10.1.0.50/15") in skipped
    # Unrelated objects went ahead.
    assert ("option", "server/6") in result.upserts
    assert ("scope", "10.1.0.0") not in result.upserts


def test_parent_delete_is_skipped_when_a_child_delete_failed(client, fake):
    _, first = run(client, wanted())
    managed = state_after({}, first)
    fake.fail = lambda op, p: (
        PSError("no", "InvalidOperation", "", "Remove-DhcpServerv4Reservation") if op == "reservation.remove" else None
    )
    _, result = run(client, [], managed)
    skipped = {(c.kind, c.key) for c, _ in result.skipped}
    assert ("scope", "10.1.0.0") in skipped
    assert "10.1.0.0" in fake.scopes


def test_transport_failure_stops_the_push(client, fake):
    class Broken:
        def __init__(self, inner):
            self.inner = inner

        def apply(self, change):
            if change.kind == "class":
                raise RunnerError("connection lost")
            self.inner.apply(change)

    p = push.plan(wanted(), windows_records(client), {})
    result = push.execute(p, Broken(WindowsWriter(client)))
    assert len(result.failed) == 1
    assert result.aborted
    assert fake.scopes == {}


def test_lease_time_unset_in_netbox_is_not_a_change(client):
    run(client, wanted())
    source = [scope(lease_seconds=None) if r.kind == "scope" else r for r in wanted()]
    p, _ = run(client, source)
    assert p.changes == []


def test_new_scope_without_lease_time_gets_the_windows_default_and_stays_deletable(client, fake):
    source = [scope(lease_seconds=None)]
    _, first = run(client, source)
    assert fake.scopes["10.1.0.0"]["lease_duration"] == "8.00:00:00"
    p, _ = run(client, [], state_after({}, first))
    assert [(c.action, c.kind) for c in p.changes] == [("delete", "scope")]


def test_reservation_without_name_matches_the_windows_default(client):
    source = wanted()
    source[4] = Record(source[4].kind, source[4].key, {**source[4].fields, "name": ""})
    run(client, source)
    p, _ = run(client, source)
    assert p.changes == []


def test_option_value_in_hex_or_decimal_has_one_fingerprint():
    a = option("server", 23, ["0x40"])
    b = option("server", 23, ["64"])
    assert push.fingerprint(a) == push.fingerprint(b)
    assert push.fingerprint(a) != push.fingerprint(option("server", 23, ["65"]))


def test_summary_lists_the_planned_changes(client):
    p = push.plan(wanted(), windows_records(client), {})
    s = p.summary()
    # The filter list switches always exist on Windows: they are updated, not created.
    assert (s["create"], s["update"]) == (len(wanted()) - 1, 1)
    assert len(s["planned"]) == len(wanted())
    assert s["planned"][0]["action"] == "create"


def test_hidden_windows_objects_stop_their_parent_being_deleted(client, fake):
    _, first = run(client, wanted())
    managed = state_after({}, first)
    client.set_option_value(OptionKey(option_id=15, scope_id="10.1.0.0"), [" x"])  # cannot be stored in NetBox
    hidden = ws.hidden_records(ws.read(client))
    source = [r for r in wanted() if r.kind in ("class", "optiondef", "filterlists")]
    p = push.plan(source, windows_records(client), managed, hidden=hidden)
    reasons = {(s.change.kind, s.change.key): s.reason for s in p.skipped}
    assert "not managed by NetBox" in reasons["scope", "10.1.0.0"]


def test_unpushable_netbox_data_is_reported_and_keeps_its_windows_twin(client, fake):
    _, first = run(client, wanted())
    managed = state_after({}, first)
    source = wanted()
    source[4] = Record(source[4].kind, source[4].key, {**source[4].fields, "client_id": ""})
    source.append(option("10.1.0.0", 51, ["3600"]))
    source.append(option("server", 77, []))
    p, result = run(client, source, managed)
    reasons = {s.change.key: s.reason for s in p.skipped}
    assert "MAC address" in reasons["10.1.0.0/10.1.0.50"]
    assert "lease time" in reasons["10.1.0.0/51"]
    assert "no value" in reasons["server/77"]
    assert p.changes == []
    assert "10.1.0.50" in fake.reservations


def test_values_of_codes_windows_does_not_define_are_not_pushed(client):
    p = push.plan([option("server", 121, ["x"])], [], {}, defined_options={6, 15})
    assert "no definition for option 121" in p.skipped[0].reason
    # Unless NetBox pushes the definition too.
    source = [wanted()[1], option("server", 200, ["x"])]
    assert push.plan(source, [], {}, defined_options={6}).skipped == []


def test_class_names_differing_in_case_match(client, fake):
    run(client, wanted())
    source = [
        Record(r.kind, r.key, {**r.fields, "name": r.fields["name"].upper()}) if r.kind == "class" else r
        for r in wanted()
    ]
    p, _ = run(client, source)
    assert p.changes == []


def test_scope_without_name_is_named_after_its_id(client, fake):
    run(client, [scope(name="")])
    assert fake.scopes["10.1.0.0"]["name"] == "10.1.0.0"
    p, _ = run(client, [scope(name="")])
    assert p.changes == []


def by_key(p):
    return {(s.change.kind, s.change.key): s.reason for s in p.skipped}


def test_policy_lease_left_empty_keeps_the_windows_value(client, fake):
    _, first = run(client, wanted())
    source = [policy(lease_seconds=None) if r.kind == "policy" and r.fields["scope_id"] else r for r in wanted()]
    p, _ = run(client, source, state_after({}, first))
    assert p.changes == []


def test_failover_settings_left_empty_keep_windows_values(client, fake):
    source = [failover(load_balance_percent=None) if r.kind == "failover" else r for r in wanted()]
    _, first = run(client, source)
    assert fake.failovers["ha"]["load_balance_percent"] == 50
    fake.failovers["ha"]["load_balance_percent"] = 70
    p, _ = run(client, source, state_after({}, first))
    assert p.changes == []


def test_policy_needs_a_condition_and_memberships_a_scope(client):
    source = [policy(criteria={}), failover(scope_ids=[]), Record("superscope", "x", {"name": "x", "scope_ids": []})]
    reasons = by_key(push.plan(source, [], {}))
    assert "condition" in reasons["policy", "10.1.0.0/printers"]
    assert "at least one scope" in reasons["failover", "ha"]
    assert "at least one scope" in reasons["superscope", "x"]


def test_values_of_vendor_options_need_a_vendor_definition(client):
    p = push.plan([option("server", 1, ["1"], vendor_class="MSFT")], [], {}, defined_options={"1", "6"})
    assert "no definition for vendor class MSFT option 1" in p.skipped[0].reason
    assert (
        push.plan([option("server", 1, ["1"], vendor_class="MSFT")], [], {}, defined_options={"msft/1"}).skipped == []
    )


def test_unmanaged_policy_keeps_its_scope(client, fake):
    _, first = run(client, wanted())
    managed = state_after({}, first)
    managed.pop(("policy", "10.1.0.0/printers"))
    managed.pop(("option", "10.1.0.0/6;policy=printers"))
    keep = ("class", "optiondef", "filterlists", "macfilter")
    p, _ = run(client, [r for r in wanted() if r.kind in keep], managed)
    assert "not managed by NetBox" in by_key(p)["scope", "10.1.0.0"]
    assert "10.1.0.0" in fake.scopes


def test_scope_removed_from_a_managed_failover_is_deleted(client, fake):
    second = Record("scope", "10.2.0.0", {**scope().fields, "start_range": "10.2.0.10", "end_range": "10.2.0.200"})
    source = [*wanted(), second]
    source = [failover(scope_ids=["10.1.0.0", "10.2.0.0"]) if r.kind == "failover" else r for r in source]
    _, first = run(client, source)
    assert not first.failed
    managed = state_after({}, first)
    source = [failover(scope_ids=["10.1.0.0"]) if r.kind == "failover" else r for r in source if r is not second]
    p, result = run(client, source, managed)
    assert not result.failed, result.failed
    assert "10.2.0.0" not in fake.scopes
    assert fake.failovers["ha"]["scope_ids"] == ["10.1.0.0"]
    # And a second push changes nothing.
    p2, _ = run(client, source, state_after(managed, result))
    assert p2.changes == []


def test_unmanaged_failover_keeps_its_scope(client, fake):
    _, first = run(client, wanted())
    managed = state_after({}, first)
    managed.pop(("failover", "ha"))
    source = [r for r in wanted() if r.kind not in ("scope", "exclusion", "reservation", "option", "policy")]
    source = [r for r in source if r.kind not in ("failover", "superscope")]
    p, _ = run(client, source, managed)
    assert "10.1.0.0" in fake.scopes
    assert "not managed by NetBox" in by_key(p)["scope", "10.1.0.0"]


def test_every_kind_is_adopted_with_the_same_fingerprint_from_both_sides(client):
    _, first = run(client, wanted())
    for record in windows_records(client):
        match = next(r for r in wanted() if (r.kind, r.key) == (record.kind, record.key))
        assert push.fingerprint(match) == push.fingerprint(record), record.kind


def test_policy_conditions_windows_rewrites_converge(client, fake):
    """#21: a MAC prefix and a host name Windows stores in its own spelling are no change on the next push."""
    criteria = {"mac_address": ["EQ", "02-4e-42-ab*"], "fqdn": ["EQ", "*.Example.Test"]}
    source = [scope(), policy(criteria=criteria)]
    _, first = run(client, source)
    assert not first.failed, first.failed
    assert fake.policies["10.1.0.0", "printers"]["criteria"] == {
        "mac_address": ["EQ", "02-4e-42-ab-*"],
        "fqdn": ["EQ", "*.example.test"],
    }
    managed = state_after({}, first)
    p2, second = run(client, source, managed)
    assert p2.changes == []
    # The same fingerprint from both spellings, so the policy is not taken for one changed on Windows...
    assert second.upserts == first.upserts
    # ...and deleting it in NetBox deletes it on Windows.
    p3, third = run(client, [scope()], managed)
    assert not p3.skipped and not third.failed
    assert ("10.1.0.0", "printers") not in fake.policies


def orders(fake, scope_id="10.1.0.0"):
    """Policy names at one level, in Windows processing order."""
    level = [p for (sid, _), p in fake.policies.items() if sid == scope_id]
    return [(p["name"], p["processing_order"]) for p in sorted(level, key=lambda p: p["processing_order"])]


def lab_policy(name, order, **over):
    return policy(name=name, processing_order=order, ip_ranges=[], lease_seconds=None, **over)


def test_policy_creates_follow_processing_order_not_names(client, fake):
    """#24: Windows refuses order 2 before order 1 exists (DHCP 20110), whatever the names."""
    source = [scope(), lab_policy("Alpha", 2), lab_policy("Zulu", 1)]
    p, first = run(client, source)
    assert not first.failed, first.failed
    assert [c.key for c in p.changes if c.kind == "policy"] == ["10.1.0.0/zulu", "10.1.0.0/alpha"]
    assert orders(fake) == [("Zulu", 1), ("Alpha", 2)]
    p2, _ = run(client, source, state_after({}, first))
    assert p2.changes == []


def test_policy_inserted_before_others_converges_in_one_push(client, fake):
    """Windows moves the others down when a policy takes a used number; the plan follows that."""
    _, first = run(client, [scope(), lab_policy("A", 1), lab_policy("B", 2)])
    managed = state_after({}, first)
    source = [scope(), lab_policy("C", 1), lab_policy("A", 2), lab_policy("B", 3)]
    p, result = run(client, source, managed)
    assert not result.failed, result.failed
    assert orders(fake) == [("C", 1), ("A", 2), ("B", 3)]
    p2, _ = run(client, source, state_after(managed, result))
    assert p2.changes == []


def test_policies_swapping_orders(client, fake):
    _, first = run(client, [scope(), lab_policy("A", 1), lab_policy("B", 2)])
    managed = state_after({}, first)
    source = [scope(), lab_policy("A", 2), lab_policy("B", 1)]
    _, result = run(client, source, managed)
    assert not result.failed, result.failed
    assert orders(fake) == [("B", 1), ("A", 2)]
    p2, _ = run(client, source, state_after(managed, result))
    assert p2.changes == []


def test_policy_moved_down_by_an_insert_is_put_back(client, fake):
    """A policy NetBox did not change, pushed down by a new one above a hand-made one, is set back in the same push."""
    _, first = run(client, [scope()])
    client.add_policy(Policy(name="Hand", scope_id="10.1.0.0", criteria={"mac_address": ["EQ", "aa-*"]}))
    _, second = run(client, [scope(), lab_policy("A", 2)], state_after({}, first))
    managed = state_after(state_after({}, first), second)
    assert orders(fake) == [("Hand", 1), ("A", 2)]
    source = [scope(), lab_policy("C", 1), lab_policy("A", 2)]
    p, result = run(client, source, managed)
    assert not result.failed, result.failed
    # Creating C at 1 pushes Hand to 2 and A to 3; A is set back to 2.
    fixup = next(c for c in p.changes if c.key == "10.1.0.0/a")
    assert fixup.changed_fields == ["processing_order"] and fixup.before["processing_order"] == 3
    assert orders(fake) == [("C", 1), ("A", 2), ("Hand", 3)]
    p2, _ = run(client, source, state_after(managed, result))
    assert p2.changes == []


def test_policy_order_past_the_end_is_reported(client, fake):
    source = [scope(), lab_policy("A", 1), lab_policy("B", 3), option("10.1.0.0", 6, ["10.1.0.2"], policy="B")]
    p, result = run(client, source)
    reasons = by_key(p)
    assert "processing order 3" in reasons["policy", "10.1.0.0/b"]
    assert not result.failed, result.failed
    assert orders(fake) == [("A", 1)]
    # Its option value is not attempted either.
    assert any(c.kind == "option" for c, _ in result.skipped)


def test_deleting_a_policy_closes_the_gap(client, fake):
    _, first = run(client, [scope(), lab_policy("A", 1), lab_policy("B", 2), lab_policy("C", 3)])
    managed = state_after({}, first)
    source = [scope(), lab_policy("B", 1), lab_policy("C", 2)]
    _, result = run(client, source, managed)
    assert not result.failed, result.failed
    assert orders(fake) == [("B", 1), ("C", 2)]
    p2, _ = run(client, source, state_after(managed, result))
    assert p2.changes == []


def test_deleting_a_policy_without_renumbering_keeps_the_others_in_order(client, fake):
    """
    NetBox does not renumber when a policy is deleted: removing order 1 of 3 leaves 2 and 3. Windows closes the gap, so
    order 3 no longer fits; moving 2 alone would put it below 3 and reverse the order clients are matched in.
    """
    _, first = run(client, [scope(), lab_policy("A", 1), lab_policy("B", 2), lab_policy("C", 3)])
    managed = state_after({}, first)
    source = [scope(), lab_policy("B", 2), lab_policy("C", 3)]
    p, result = run(client, source, managed)
    assert not result.failed, result.failed
    assert [(c.action, c.key) for c in p.changes] == [("delete", "10.1.0.0/a")]
    assert "past the end" in by_key(p)["policy", "10.1.0.0/c"]
    assert orders(fake) == [("B", 1), ("C", 2)]
    # The next push still leaves them alone, and changes to them wait for the renumbering it asks for.
    managed = state_after(managed, result)
    source = [scope(), lab_policy("B", 2, description="edited"), lab_policy("C", 3)]
    p2, result2 = run(client, source, managed)
    assert not result2.applied and not result2.failed, (result2.applied, result2.failed)
    reasons = by_key(p2)
    assert "past the end" in reasons["policy", "10.1.0.0/c"]
    assert "renumber" in reasons["policy", "10.1.0.0/b"]
    assert orders(fake) == [("B", 1), ("C", 2)]
    # Renumbered in NetBox: the edit goes through and nothing moves.
    source = [scope(), lab_policy("B", 1, description="edited"), lab_policy("C", 2)]
    p3, result3 = run(client, source, managed)
    assert not p3.skipped and not result3.failed, (p3.skipped, result3.failed)
    assert orders(fake) == [("B", 1), ("C", 2)]
    assert fake.policies["10.1.0.0", "b"]["description"] == "edited"


def test_policies_renumbered_by_windows_can_still_be_deleted(client, fake):
    """
    Windows renumbers the policies below a deleted one by itself. Those it moved, even ones the push left alone, are
    not taken for policies changed on Windows when NetBox deletes them later.
    """
    _, first = run(client, [scope(), lab_policy("A", 1), lab_policy("B", 2), lab_policy("C", 3)])
    managed = state_after({}, first)
    # A deleted in NetBox without renumbering: B and C are held back (past the end) while Windows moves them up.
    _, second = run(client, [scope(), lab_policy("B", 2), lab_policy("C", 3)], managed)
    managed = state_after(managed, second)
    assert orders(fake) == [("B", 1), ("C", 2)]
    p, result = run(client, [scope()], managed)
    assert not p.skipped and not result.failed, (p.skipped, result.failed)
    assert orders(fake) == []


def test_policy_fingerprints_recorded_with_the_order_still_match():
    """0.1.0rc1 hashed the processing order: those SyncState rows still allow a delete while the order holds."""
    record = Record("policy", "10.1.0.0/a", lab_policy("A", 1).fields)
    legacy = push._fingerprint(record, keep_order=True)
    assert legacy != push.fingerprint(record)
    p = push.plan([scope()], [scope(), record], {("policy", record.key): legacy})
    assert [(c.action, c.key) for c in p.changes] == [("delete", record.key)] and not p.skipped
    moved = Record("policy", record.key, {**record.fields, "processing_order": 2})
    p = push.plan([scope()], [scope(), moved], {("policy", record.key): legacy})
    assert p.changes == [] and "changed on Windows" in p.skipped[0].reason


def test_new_policy_past_the_end_leaves_the_others_alone(client, fake):
    """Only a create is past the end: the policies already on Windows are still set as NetBox says."""
    _, first = run(client, [scope(), lab_policy("A", 1), lab_policy("B", 2)])
    managed = state_after({}, first)
    source = [scope(), lab_policy("A", 2), lab_policy("B", 1), lab_policy("N", 5)]
    p, result = run(client, source, managed)
    assert not result.failed, result.failed
    assert "past the end" in by_key(p)["policy", "10.1.0.0/n"]
    assert orders(fake) == [("B", 1), ("A", 2)]


def test_policies_sharing_a_processing_order_are_reported():
    source = [policy(), policy(name="Phones"), policy(name="Other", processing_order=2)]
    reasons = by_key(push.plan(source, [], {}))
    assert "share processing order 1" in reasons["policy", "10.1.0.0/printers"]
    assert "share processing order 1" in reasons["policy", "10.1.0.0/phones"]
    # Without the two at 1, order 2 would leave a gap, which Windows refuses.
    assert "past the end" in reasons["policy", "10.1.0.0/other"]


def test_last_scope_of_a_relationship_is_replaced_before_it_is_deleted(client, fake):
    second = Record("scope", "10.2.0.0", {**scope().fields, "start_range": "10.2.0.10", "end_range": "10.2.0.200"})
    source = [*wanted(), second]
    source = [failover(scope_ids=["10.2.0.0"]) if r.kind == "failover" else r for r in source]
    _, first = run(client, source)
    managed = state_after({}, first)
    source = [failover(scope_ids=["10.1.0.0"]) if r.kind == "failover" else r for r in source if r is not second]
    p, result = run(client, source, managed)
    assert not result.failed, result.failed
    assert p.changes[0].kind == "failover"
    assert "10.2.0.0" not in fake.scopes
    assert fake.failovers["ha"]["scope_ids"] == ["10.1.0.0"]


def test_scope_added_to_a_managed_relationship_by_hand_stays(client, fake):
    source = [scope(), failover()]
    _, first = run(client, source)
    client.add_scope(
        Scope(
            scope_id="10.2.0.0", name="W", start_range="10.2.0.10", end_range="10.2.0.20", subnet_mask="255.255.255.0"
        )
    )
    client.add_failover_scopes("HA", ["10.2.0.0"])
    p, result = run(client, source, state_after({}, first))
    assert not [c for c in result.applied if c.kind == "failover"]
    assert "added on Windows" in by_key(p)["failover", "ha"]
    assert fake.failovers["ha"]["scope_ids"] == ["10.1.0.0", "10.2.0.0"]


def test_policy_values_need_their_policy_in_netbox(client, fake):
    client.add_policy(Policy(name="Printers", criteria={"mac_address": ["EQ", "00-15-5d*"]}))
    p, result = run(client, [option("server", 6, ["10.9.9.9"], policy="Printers")])
    assert not result.applied
    assert "not in NetBox" in by_key(p)["option", "server/6;policy=printers"]


def test_windows_names_ending_in_hash_digits_are_pushed(client):
    record = Record(
        "class", "user/phones#2", {"name": "Phones#2", "class_type": "user", "data": "x", "description": ""}
    )
    assert push.plan([record], [], {}).skipped == []


def test_leased_scope_stays_in_its_relationship_when_the_update_runs_first(client, fake):
    second = Record("scope", "10.2.0.0", {**scope().fields, "start_range": "10.2.0.10", "end_range": "10.2.0.200"})
    _, first = run(client, [scope(), second, failover(scope_ids=["10.1.0.0", "10.2.0.0"])])
    fake.add_lease("10.2.0.0", "10.2.0.15")
    _, result = run(client, [scope(), failover(scope_ids=["10.1.0.0"])], state_after({}, first))
    assert fake.failovers["ha"]["scope_ids"] == ["10.1.0.0", "10.2.0.0"]
    assert "10.2.0.0" in fake.scopes
    assert any("DHCP 20007" in msg for _, msg in result.failed)


@pytest.mark.parametrize("seed", range(40))
def test_policy_orders_converge_in_one_push(client, fake, seed):
    """
    Random starting points (managed and hand-made policies on Windows) and random NetBox orders 1..k: one push puts
    every NetBox policy at its number without a Windows error, and the next push changes nothing.
    """
    import random

    rnd = random.Random(seed)
    names = [f"P{i}" for i in range(rnd.randint(1, 6))]
    first_names = rnd.sample(names, rnd.randint(0, len(names)))
    first = [scope()] + [lab_policy(n, i) for i, n in enumerate(rnd.sample(first_names, len(first_names)), 1)]
    _, r1 = run(client, first)
    assert not r1.failed, r1.failed
    managed = state_after({}, r1)
    for i in range(rnd.randint(0, 2)):
        level = len([k for k in fake.policies if k[0] == "10.1.0.0"])
        client.add_policy(
            Policy(
                name=f"Hand{i}",
                scope_id="10.1.0.0",
                criteria={"mac_address": ["EQ", "aa-*"]},
                processing_order=rnd.randint(1, level + 1),
            )
        )
    wanted_names = rnd.sample(names, rnd.randint(0, len(names)))
    source = [scope()] + [lab_policy(n, i) for i, n in enumerate(rnd.sample(wanted_names, len(wanted_names)), 1)]
    p, result = run(client, source, managed)
    assert not result.failed, result.failed
    assert not [s for s in p.skipped if s.change.kind == "policy" and "past the end" in s.reason]
    got = dict(orders(fake))
    for record in source[1:]:
        assert got[record.fields["name"]] == record.fields["processing_order"], (orders(fake), record.key)
    # Nothing left to create or update. A managed policy a hand-made one moved down counts as changed on Windows,
    # so its delete may wait for a push where its number is back (the drift check), which is not this test's point.
    p2, _ = run(client, source, state_after(managed, result))
    assert [c for c in p2.changes if c.action != "delete"] == [], p2.changes


@pytest.mark.parametrize("seed", range(60))
def test_policy_orders_with_gaps_never_reverse_netbox_order(client, fake, seed):
    """
    Random NetBox numbers, gaps allowed (a policy deleted without renumbering the others, or a typo): a push never
    fails on Windows, and two NetBox policies end up either in NetBox's order or in the order they already had.
    """
    import random

    rnd = random.Random(seed)
    names = [f"P{i}" for i in range(rnd.randint(1, 6))]
    first_names = rnd.sample(names, rnd.randint(0, len(names)))
    first = [scope()] + [lab_policy(n, i) for i, n in enumerate(rnd.sample(first_names, len(first_names)), 1)]
    _, r1 = run(client, first)
    managed = state_after({}, r1)
    for i in range(rnd.randint(0, 2)):
        level = len([k for k in fake.policies if k[0] == "10.1.0.0"])
        client.add_policy(
            Policy(
                name=f"Hand{i}",
                scope_id="10.1.0.0",
                criteria={"mac_address": ["EQ", "aa-*"]},
                processing_order=rnd.randint(1, level + 1),
            )
        )
    before = [n for n, _ in orders(fake)]
    wanted_names = rnd.sample(names, rnd.randint(0, len(names)))
    numbers = rnd.sample(range(1, 9), len(wanted_names))
    source = [scope()] + [lab_policy(n, o) for n, o in zip(wanted_names, numbers, strict=True)]
    _, result = run(client, source, managed)
    assert not result.failed, result.failed
    after = [n for n, _ in orders(fake)]
    netbox = {r.fields["name"]: r.fields["processing_order"] for r in source[1:]}
    on_windows = [n for n in after if n in netbox]
    for i, a in enumerate(on_windows):
        for b in on_windows[i + 1 :]:
            # a is above b on Windows after the push.
            kept = a in before and b in before and before.index(a) < before.index(b)
            assert netbox[a] < netbox[b] or kept, (before, after, netbox)


# -- issue #42: reservation updates keep the reservation's option values ------------------------------------------


def with_description(source, description):
    out = list(source)
    res = out[4]
    out[4] = Record(res.kind, res.key, {**res.fields, "description": description})
    return out


def test_reservation_description_change_keeps_its_option_values(client, fake):
    _, first = run(client, wanted())
    managed = state_after({}, first)
    # The worst case seen on Windows Server 2025: the update drops the reservation's values.
    fake.reservation_set_drops_options = True
    source = with_description(wanted(), "changed")
    p, result = run(client, source, managed)
    # The dry run shows only the reservation update, as before the fix.
    assert [(c.action, c.kind) for c in p.changes] == [("update", "reservation")]
    assert len(result.applied) == 1 and not result.failed
    assert fake.options[(15, "", "10.1.0.50", "", "", "")]["value"] == ["pc.example"]
    p2, _ = run(client, source, state_after(managed, result))
    assert p2.changes == []


def test_reservation_option_deleted_in_the_same_push_stays_deleted(client, fake):
    _, first = run(client, wanted())
    managed = state_after({}, first)
    fake.reservation_set_drops_options = True
    source = [r for r in with_description(wanted(), "changed") if r.key != wanted()[8].key]
    p, result = run(client, source, managed)
    assert sorted((c.action, c.kind) for c in p.changes) == [("delete", "option"), ("update", "reservation")]
    assert not result.failed
    assert not [k for k in fake.options if k[2] == "10.1.0.50"]
    assert run(client, source, state_after(managed, result))[0].changes == []


def test_reservation_option_updated_in_the_same_push_ends_with_the_new_value(client, fake):
    _, first = run(client, wanted())
    managed = state_after({}, first)
    fake.reservation_set_drops_options = True
    source = with_description(wanted(), "changed")
    source[8] = option("10.1.0.0/10.1.0.50", 15, ["new.example"])
    p, result = run(client, source, managed)
    assert not result.failed
    assert fake.options[(15, "", "10.1.0.50", "", "", "")]["value"] == ["new.example"]
    assert run(client, source, state_after(managed, result))[0].changes == []
