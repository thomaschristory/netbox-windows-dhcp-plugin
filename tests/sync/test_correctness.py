"""Regression coverage for type-aware options, failover safety, and full-length names."""

import hashlib
import json

import pytest

from netbox_windows_dhcp_plugin.sync import push
from netbox_windows_dhcp_plugin.sync import windows_state as ws
from netbox_windows_dhcp_plugin.sync.diff import Record, diff
from netbox_windows_dhcp_plugin.sync.windows_writer import WindowsWriter
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import (
    OptionDefinition,
    OptionKey,
    OptionValue,
    Reservation,
    ReservationV6,
    Scope,
    ScopeV6,
)
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import NotFound, PSError
from netbox_windows_dhcp_plugin.windows.runner import RunnerError

from .test_push import run, state_after
from .test_v6 import option6, reservation6, scope6
from .test_windows_writer import failover, option, scope, wanted, windows_records


@pytest.fixture
def fake():
    return FakeDhcpServer()


@pytest.fixture
def client(fake):
    return DhcpClient(fake)


def definition(data_type="String", vendor=""):
    return Record(
        "optiondef",
        ws.optiondef_key(200, vendor),
        {
            "option_id": 200,
            "name": "Custom",
            "type": data_type,
            "multi_valued": False,
            "default_value": [],
            "description": "",
            "vendor_class": vendor,
        },
    )


@pytest.mark.parametrize("old,new", [("0010", "10"), ("0x10", "16"), (" 10", "10"), ("2001:0db8::1", "2001:db8::1")])
@pytest.mark.parametrize("kind", ["standard", "custom", "vendor", "binary", "unknown", "v6", "default"])
def test_text_and_binary_values_remain_exact(kind, old, new):
    if kind == "standard":
        a, b = option("server", 67, [old]), option("server", 67, [new])
    elif kind == "v6":
        a, b = option6("server", 59, [old]), option6("server", 59, [new])
    elif kind == "default":
        d = definition()
        a = Record(d.kind, d.key, {**d.fields, "default_value": [old]})
        b = Record(d.kind, d.key, {**d.fields, "default_value": [new]})
    else:
        vendor = "MSFT" if kind == "vendor" else ""
        a, b = option("server", 200, [old], vendor_class=vendor), option("server", 200, [new], vendor_class=vendor)
        if kind != "unknown":
            d = definition("BinaryData" if kind == "binary" else "String", vendor)
            a, b = ws.with_option_types([d, a])[1], ws.with_option_types([d, b])[1]
    assert not ws.records_equal(a.kind, a.fields, b.fields)
    assert len(diff([a], [b], equal_records=ws.records_equal)) == 1
    assert len(diff([b], [a], equal_records=ws.records_equal)) == 1
    assert push.fingerprint(a) != push.fingerprint(b)
    plan = push.plan([], [b], {(a.kind, a.key): push.fingerprint(a)})
    assert not plan.changes
    assert "changed on Windows" in plan.skipped[0].reason


@pytest.mark.parametrize("data_type", ["Byte", "Word", "DWord", "DWordDWord"])
@pytest.mark.parametrize("vendor", ["", "MSFT"])
def test_numeric_custom_and_vendor_values_and_defaults_compare_by_type(data_type, vendor):
    d = definition(data_type, vendor)
    a = ws.with_option_types([d, option("server", 200, ["0x10"], vendor_class=vendor)])[1]
    b = ws.with_option_types([d, option("server", 200, ["16"], vendor_class=vendor)])[1]
    assert ws.records_equal(a.kind, a.fields, b.fields)
    assert push.fingerprint(a) == push.fingerprint(b)
    a = Record(d.kind, d.key, {**d.fields, "default_value": ["0x10"]})
    b = Record(d.kind, d.key, {**d.fields, "default_value": ["16"]})
    assert ws.records_equal(a.kind, a.fields, b.fields)
    assert push.fingerprint(a) == push.fingerprint(b)


def test_standard_numeric_v4_and_v6_values_share_fingerprints():
    for make, code in ((option, 23), (option6, 7)):
        a, b = make("server", code, ["0x10"]), make("server", code, ["16"])
        assert ws.records_equal(a.kind, a.fields, b.fields)
        assert push.fingerprint(a) == push.fingerprint(b)


def test_windows_definition_types_override_standard_code_and_are_retained_without_definitions():
    state = ws.WindowsState(
        option_definitions=[
            OptionDefinition(option_id=23, name="Custom TTL", type="String"),
            OptionDefinition(option_id=200, name="Number", type="DWord"),
        ],
        options=[OptionValue(option_id=23, value=["0010"]), OptionValue(option_id=200, value=["0x10"])],
    )
    records, _ = ws.to_records(state, standard_codes={23, 200})
    string, number = records
    assert string.fields[ws.VALUE_TYPE] == "exact"
    assert number.fields[ws.VALUE_TYPE] == "number"
    assert not ws.records_equal("option", string.fields, {**string.fields, "value": ["10"]})
    assert ws.records_equal("option", number.fields, {**number.fields, "value": ["16"]})


def test_string_change_is_written_and_later_manual_change_blocks_deletion(client):
    old, new = option("server", 67, ["0010"]), option("server", 67, ["10"])
    _, first = run(client, [old])
    managed = state_after({}, first)
    plan, result = run(client, [new], managed)
    assert [(c.action, c.kind) for c in plan.changes] == [("update", "option")]
    assert client.get_option_value(OptionKey(67)).value == ["10"]
    managed = state_after(managed, result)
    client.set_option_value(OptionKey(67), ["0010"])
    plan, result = run(client, [], managed)
    assert not plan.changes and not result.applied
    assert "changed on Windows" in plan.skipped[0].reason
    assert client.get_option_value(OptionKey(67)).value == ["0010"]


def test_windows_only_numeric_definition_converges_and_stays_deletable(client):
    client.add_option_definition(OptionDefinition(option_id=200, name="Windows number", type="DWord"))
    source = [option("server", 200, ["0x10"])]
    managed = {}
    for expected in (1, 0, 0):
        plan, result = run(client, source, managed)
        assert len(plan.changes) == expected
        assert not result.failed
        managed = state_after(managed, result)
    plan, result = run(client, [], managed)
    assert [(change.action, change.kind) for change in plan.changes] == [("delete", "option")]
    assert not plan.skipped and not result.failed


def test_unknown_type_can_use_peer_definition_without_overriding_explicit_text():
    unknown = option("server", 200, ["0x10"])
    number = ws.with_option_types([definition("DWord"), option("server", 200, ["16"])])[1]
    assert ws.records_equal("option", unknown.fields, number.fields)
    assert ws.records_equal("option", number.fields, unknown.fields)
    explicit = Record(unknown.kind, unknown.key, {**unknown.fields, ws.VALUE_TYPE: "exact"})
    assert not ws.records_equal("option", explicit.fields, number.fields)
    assert ws.with_option_types([explicit], fallback=[number])[0].fields[ws.VALUE_TYPE] == "exact"


@pytest.mark.parametrize("kind", ["option", "option6", "optiondef"])
def test_ambiguous_legacy_hashes_cannot_authorize_deletion(kind):
    # The old hash of String "0010" was identical to this hash of String "10".
    if kind == "option":
        record = option("server", 67, ["10"])
    elif kind == "option6":
        record = option6("server", 59, ["10"])
    else:
        d = definition()
        record = Record(kind, d.key, {**d.fields, "default_value": ["10"]})
    legacy = hashlib.sha256(
        json.dumps({"kind": kind, "key": record.key, "fields": record.fields}, sort_keys=True).encode()
    ).hexdigest()
    assert not push.unchanged(legacy, record)
    plan = push.plan([], [record], {(kind, record.key): legacy})
    assert not plan.changes and plan.skipped
    adopted = push.plan([record], [record], {(kind, record.key): legacy})
    assert adopted.in_sync[kind, record.key] == push.fingerprint(record)


def second_scope(number=2):
    return Record(
        "scope",
        f"10.{number}.0.0",
        {**scope().fields, "start_range": f"10.{number}.0.10", "end_range": f"10.{number}.0.200"},
    )


@pytest.mark.parametrize("settings_change", [False, True])
@pytest.mark.parametrize("delete_scope", [False, True])
def test_protected_membership_is_not_adopted_by_repeated_pushes(client, fake, settings_change, delete_scope):
    second = second_scope()
    source = [scope(), second, failover()]
    _, first = run(client, source)
    managed = state_after({}, first)
    client.add_failover_scopes("HA", [second.key])
    desired = failover(load_balance_percent=70) if settings_change else failover()
    source = [scope(), desired] if delete_scope else [scope(), second, desired]
    for _ in range(3):
        plan, result = run(client, source, managed)
        managed = state_after(managed, result)
        assert any("added on Windows" in s.reason for s in plan.skipped)
        assert managed["failover", "ha"] == push.PROTECTED_MEMBERSHIPS
        assert fake.failovers["ha"]["scope_ids"] == ["10.1.0.0", "10.2.0.0"]
        assert second.key in fake.scopes
    assert fake.failovers["ha"]["load_balance_percent"] == (70 if settings_change else 60)
    # Accepting the Windows membership (as an import does) permits a subsequent intentional removal.
    accepted = [scope(), second, failover(scope_ids=["10.1.0.0", "10.2.0.0"], load_balance_percent=70)]
    _, result = run(client, accepted, managed)
    managed = state_after(managed, result)
    plan, result = run(client, [scope(), second, failover(load_balance_percent=70)], managed)
    assert not plan.skipped and not result.failed
    assert fake.failovers["ha"]["scope_ids"] == ["10.1.0.0"]


def test_existing_failover_can_be_emptied_and_then_converges(client, fake):
    _, first = run(client, [scope(), failover()])
    source = [scope(), failover(scope_ids=[])]
    plan, result = run(client, source, state_after({}, first))
    assert [(c.action, c.kind) for c in plan.changes] == [("update", "failover")]
    assert not plan.skipped and not result.failed
    assert fake.failovers["ha"]["scope_ids"] == [] and scope().key in fake.scopes
    plan, result = run(client, source, state_after(state_after({}, first), result))
    assert not plan.changes and not plan.skipped


@pytest.mark.parametrize("unmanaged", [False, True])
def test_emptying_existing_failover_still_protects_unaccepted_members(client, fake, unmanaged):
    _, first = run(client, [scope(), failover()])
    managed = {} if unmanaged else state_after({}, first)
    if not unmanaged:
        fake.failovers["ha"]["load_balance_percent"] = 75
    for _ in range(2):
        plan, result = run(client, [scope(), failover(scope_ids=[])], managed)
        managed = state_after(managed, result)
        assert plan.skipped and fake.failovers["ha"]["scope_ids"] == ["10.1.0.0"]


def test_emptying_failover_does_not_remove_a_scope_whose_deletion_has_leases(client, fake):
    _, first = run(client, [scope(), failover()])
    fake.add_lease(scope().key, "10.1.0.15")
    _, result = run(client, [failover(scope_ids=[])], state_after({}, first))
    assert result.failed and "DHCP 20007" in result.failed[0][1]
    assert fake.failovers["ha"]["scope_ids"] == ["10.1.0.0"] and scope().key in fake.scopes


def test_empty_failover_survives_deleting_its_former_scope(client, fake):
    _, first = run(client, [scope(), failover()])
    plan, result = run(client, [failover(scope_ids=[])], state_after({}, first))
    assert [(c.action, c.kind) for c in plan.changes] == [("update", "failover"), ("delete", "scope")]
    assert not result.failed and not fake.scopes
    assert fake.failovers["ha"]["scope_ids"] == []


def preflight_plan(client):
    scopes = [scope(), *(second_scope(i) for i in range(2, 5))]
    source = [
        *scopes,
        failover(name="A", scope_ids=[scopes[0].key, scopes[1].key]),
        failover(name="B", scope_ids=[scopes[2].key, scopes[3].key]),
    ]
    _, first = run(client, source)
    desired = [
        scopes[0],
        scopes[2],
        failover(name="A", scope_ids=[scopes[0].key]),
        failover(name="B", scope_ids=[scopes[2].key]),
    ]
    managed = state_after({}, first)
    plan = push.plan(desired, windows_records(client), managed)
    assert [(c.action, c.kind, c.key) for c in plan.changes] == [
        ("update", "failover", "a"),
        ("update", "failover", "b"),
        ("delete", "scope", "10.2.0.0"),
        ("delete", "scope", "10.4.0.0"),
    ]
    return plan, managed


@pytest.mark.parametrize(
    "error",
    [
        PSError("lease query refused", "PermissionDenied", "", "Get-DhcpServerv4Lease"),
        NotFound("missing", "ObjectNotFound", "", "Get-DhcpServerv4Lease"),
        ValueError("invalid lease response"),
        RunnerError("connection lost"),
    ],
)
def test_preflight_failure_returns_progress_and_blocks_dependent_deletion(client, fake, error):
    plan, managed = preflight_plan(client)

    def fail(op, params):
        if op == "lease.list" and params["scope_id"] == "10.4.0.0":
            if isinstance(error, PSError):
                return error
            raise error
        return None

    fake.fail = fail
    result = push.execute(plan, WindowsWriter(client))
    assert result.failed[0][0].key == "b"
    assert fake.failovers["a"]["scope_ids"] == ["10.1.0.0"]
    assert fake.failovers["b"]["scope_ids"] == ["10.3.0.0", "10.4.0.0"]
    assert "10.4.0.0" in fake.scopes
    assert result.upserts["failover", "a"] != managed["failover", "a"]
    actual = next(r for r in windows_records(client) if (r.kind, r.key) == ("failover", "a"))
    assert push.unchanged(result.upserts["failover", "a"], actual)
    assert ("failover", "b") not in result.upserts
    assert result.aborted == isinstance(error, RunnerError)
    assert any(c.key == "10.4.0.0" for c, _ in result.skipped)


def test_aborted_executor_does_not_query_later_memberships(client, fake):
    plan, _ = preflight_plan(client)
    fake.calls.clear()

    def fail(op, params):
        if op == "failover.set":
            raise RunnerError("connection lost")

    fake.fail = fail
    result = push.execute(plan, WindowsWriter(client))
    assert result.aborted
    assert [c.params["scope_id"] for c in fake.calls if c.op == "lease.list"] == ["10.2.0.0"]


@pytest.mark.parametrize("length", [200, 201, 255])
def test_scope_and_reservation_names_roundtrip_and_suffix_changes_are_written(client, length):
    name = "n" * length
    reservation = wanted()[4]
    source = [
        scope(name=name),
        Record(reservation.kind, reservation.key, {**reservation.fields, "name": name}),
        scope6(name=name),
        reservation6(name=name),
    ]
    _, first = run(client, source)
    assert not first.failed
    actual = {(r.kind, r.key): r for r in windows_records(client)}
    assert all(actual[r.kind, r.key].fields["name"] == name for r in source)
    changed = [Record(r.kind, r.key, {**r.fields, "name": name[:-1] + "z"}) for r in source]
    managed = state_after({}, first)
    plan, result = run(client, changed, managed)
    assert len(plan.changes) == 4 and all(c.action == "update" for c in plan.changes)
    assert not result.failed
    assert not run(client, changed, state_after(managed, result))[0].changes


def test_description_limits_remain_field_specific():
    state = ws.WindowsState(option_definitions=[OptionDefinition(option_id=200, name="Custom", description="d" * 255)])
    records, _ = ws.to_records(state)
    assert records[0].fields["description"] == "d" * 255
    assert ws.text_field("d" * 255) == "d" * 200


def test_null_scope_and_reservation_names_still_read_as_empty():
    state = ws.WindowsState(
        scopes=[Scope(scope_id="10.1.0.0", name=None)],
        reservations=[Reservation(scope_id="10.1.0.0", ip_address="10.1.0.10", name=None)],
        v6_scopes=[ScopeV6(prefix="2001:db8:1::", name=None)],
        v6_reservations=[ReservationV6(prefix="2001:db8:1::", ip_address="2001:db8:1::10", iaid=1, name=None)],
    )
    records, _ = ws.to_records(state)
    assert len(records) == 4 and all(r.fields["name"] == "" for r in records)
