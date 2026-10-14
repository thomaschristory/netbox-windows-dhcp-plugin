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


def test_policies_sharing_a_processing_order_are_reported():
    source = [policy(), policy(name="Phones"), policy(name="Other", processing_order=2)]
    reasons = by_key(push.plan(source, [], {}))
    assert "share processing order 1" in reasons["policy", "10.1.0.0/printers"]
    assert "share processing order 1" in reasons["policy", "10.1.0.0/phones"]
    assert ("policy", "10.1.0.0/other") not in reasons


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
