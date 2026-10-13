import pytest

from netbox_windows_dhcp_plugin.sync import push
from netbox_windows_dhcp_plugin.sync.diff import Record
from netbox_windows_dhcp_plugin.sync.windows_writer import WindowsWriter
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import Reservation
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import PSError
from netbox_windows_dhcp_plugin.windows.runner import RunnerError

from .test_windows_writer import option, scope, wanted, windows_records


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
    dup = Record("reservation", "10.1.0.0/10.1.0.50#7", wanted()[4].fields)
    empty = Record("reservation", "10.1.0.0/#8", {**wanted()[4].fields, "ip_address": ""})
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
    assert s["create"] == len(wanted())
    assert len(s["planned"]) == len(wanted())
    assert s["planned"][0]["action"] == "create"


def test_hidden_windows_objects_stop_their_parent_being_deleted(client, fake):
    from netbox_windows_dhcp_plugin.sync import windows_state as ws
    from netbox_windows_dhcp_plugin.windows.client import DhcpClass, OptionKey

    _, first = run(client, wanted())
    managed = state_after({}, first)
    client.add_class(DhcpClass(name="MSFT", type="Vendor", data="MSFT 5.0"))
    client.set_option_value(OptionKey(option_id=15, scope_id="10.1.0.0"), ["a,b"])  # cannot be stored in NetBox
    hidden = ws.hidden_records(ws.read(client))
    source = [r for r in wanted() if r.kind in ("class", "optiondef")]
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
