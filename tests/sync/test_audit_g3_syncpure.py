"""Regression tests for the g3-syncpure audit findings (pure sync layer: plan, execute, writer, diff)."""

import pytest

from netbox_windows_dhcp_plugin.sync import push
from netbox_windows_dhcp_plugin.sync import windows_state as ws
from netbox_windows_dhcp_plugin.sync.diff import Change, Record
from netbox_windows_dhcp_plugin.sync.windows_writer import PushRefused, WindowsWriter
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import PSError
from netbox_windows_dhcp_plugin.windows.runner import RunnerError

from .test_push import run, state_after
from .test_windows_writer import failover, policy, scope, wanted, windows_records


@pytest.fixture
def fake():
    return FakeDhcpServer()


@pytest.fixture
def client(fake):
    return DhcpClient(fake)


def scope_at(scope_id):
    net = scope_id.rsplit(".", 1)[0]
    return Record("scope", scope_id, {**scope().fields, "start_range": f"{net}.10", "end_range": f"{net}.200"})


# -- F57: a leased scope kept in its failover relationship must still be NetBox's on the next push ------------------


@pytest.mark.parametrize(("leaving", "staying"), [("10.1.0.0", "10.2.0.0"), ("10.2.0.0", "10.1.0.0")])
def test_leased_scope_kept_in_its_relationship_is_deleted_once_the_leases_are_gone(client, fake, leaving, staying):
    both = sorted([leaving, staying])
    _, first = run(client, [scope_at(leaving), scope_at(staying), failover(scope_ids=both)])
    managed = state_after({}, first)
    fake.add_lease(leaving, leaving.rsplit(".", 1)[0] + ".15")
    source = [scope_at(staying), failover(scope_ids=[staying])]

    _, second = run(client, source, managed)
    assert fake.failovers["ha"]["scope_ids"] == both
    assert leaving in fake.scopes
    assert any("DHCP 20007" in msg for _, msg in second.failed)
    managed = state_after(managed, second)
    # The relationship is recorded the way Windows reports it (address order), whatever the order it was written in.
    recorded = push.fingerprint(Record("failover", "ha", {**failover(scope_ids=both).fields, "auth": False}))
    assert managed[("failover", "ha")] == recorded

    fake.leases.clear()
    p, third = run(client, source, managed)
    assert not p.skipped, [(s.change.kind, s.change.key, s.reason) for s in p.skipped]
    assert not third.failed, third.failed
    assert leaving not in fake.scopes
    assert fake.failovers["ha"]["scope_ids"] == [staying]


# -- F52/F53: every list of the run summary is capped, the counts stay exact -------------------------------------


def many_changes(count):
    return [
        Change("create", "reservation", f"10.1.0.0/10.9.{i // 250}.{i % 250 + 1}", after={"scope_id": "10.1.0.0"})
        for i in range(count)
    ]


def test_summary_caps_the_refused_changes_and_keeps_their_count():
    over = push.PLANNED_LIMIT + 5
    plan = push.Plan(skipped=[push.Skip(c, "no MAC address") for c in many_changes(over)])
    summary = plan.summary()
    assert len(summary["not_pushed"]) == push.PLANNED_LIMIT
    assert summary["not_pushed_truncated"] is True
    assert summary["not_pushed_count"] == over
    assert summary["not_pushed"][0] == {
        "action": "create",
        "kind": "reservation",
        "key": "10.1.0.0/10.9.0.1",
        "reason": "no MAC address",
    }


def test_summary_below_the_cap_has_no_truncated_flag():
    plan = push.Plan(skipped=[push.Skip(c, "why") for c in many_changes(3)])
    summary = plan.summary()
    assert len(summary["not_pushed"]) == 3 and summary["not_pushed_count"] == 3
    assert "not_pushed_truncated" not in summary
    result = push.Result(failed=[(c, "boom") for c in many_changes(2)], skipped=[(c, "x") for c in many_changes(1)])
    summary = result.summary()
    assert len(summary["errors"]) == 2 and len(summary["not_attempted"]) == 1
    assert not [k for k in summary if k.endswith("_truncated")]


def test_summary_of_an_aborted_push_caps_what_was_not_attempted():
    class Dead:
        def apply(self, change):
            raise RunnerError("connection lost")

    over = push.PLANNED_LIMIT + 5
    result = push.execute(push.Plan(changes=many_changes(over + 1)), Dead())
    assert result.aborted and len(result.failed) == 1 and len(result.skipped) == over
    summary = result.summary()
    assert summary["skipped"] == over
    assert len(summary["not_attempted"]) == push.PLANNED_LIMIT
    assert summary["not_attempted_truncated"] is True
    assert "errors_truncated" not in summary


def test_summary_caps_the_errors():
    over = push.PLANNED_LIMIT + 2
    summary = push.Result(failed=[(c, "boom") for c in many_changes(over)]).summary()
    assert summary["failed"] == over
    assert len(summary["errors"]) == push.PLANNED_LIMIT and summary["errors_truncated"] is True


# -- F55: policies are grouped by level once ----------------------------------------------------------------------


def test_policy_ordering_does_not_rescan_every_record_for_each_level(monkeypatch):
    def policy_at(n, name, order):
        sid = f"10.{n}.0.0"
        fields = {**policy(scope_id=sid).fields, "name": name, "scope_id": sid, "processing_order": order}
        return Record("policy", ws.policy_key(sid, name), fields)

    scopes = [scope_at(f"10.{n}.0.0") for n in range(1, 31)]
    target = scopes + [policy_at(n, "old", 1) for n in range(1, 31)]
    source = target + [policy_at(n, "new", 2) for n in range(1, 31)]
    calls = []
    real = push._policy_level
    monkeypatch.setattr(push, "_policy_level", lambda fields: calls.append(1) or real(fields))
    p = push.plan(source, target, {})
    assert [(c.action, c.key) for c in p.changes] == [
        ("create", f"10.{n}.0.0/new") for n in sorted(range(1, 31), key=str)
    ]
    # 30 levels, 90 policy records: a few passes over the policies, not one pass per level (2,700 and more).
    assert len(calls) < 400, len(calls)


# -- F56: an unexpected error stops the push but keeps the record of what was written ------------------------------


def test_unexpected_error_keeps_what_was_already_applied(client, fake, caplog):
    class Odd:
        def __init__(self, inner):
            self.inner = inner

        def apply(self, change):
            if change.kind == "scope":
                raise RuntimeError("secret-looking detail")
            self.inner.apply(change)

    p = push.plan(wanted(), windows_records(client), {})
    before_scope = [c for c in p.changes if push._kind_rank(c.kind) < push._kind_rank("scope")]
    with caplog.at_level("ERROR"):
        result = push.execute(p, Odd(WindowsWriter(client)))
    assert result.aborted
    assert result.applied == before_scope
    assert {(c.kind, c.key) for c in before_scope} <= set(result.upserts)
    ((failed, message),) = result.failed
    assert (failed.kind, failed.key) == ("scope", "10.1.0.0")
    # The type, not the message: an unexpected error text could hold anything.
    assert "RuntimeError" in message and "secret-looking" not in message
    assert ("scope", "10.1.0.0") not in result.upserts
    assert len(result.skipped) == len(p.changes) - len(before_scope) - 1
    assert all(reason == "not attempted: the push stopped on an unexpected error" for _, reason in result.skipped)
    assert result.maybe_written == [failed]
    assert "Unexpected error applying create scope 10.1.0.0" in caplog.text


def test_lost_connection_keeps_its_own_reason(client):
    class Dead:
        def apply(self, change):
            raise RunnerError("connection lost")

    result = push.execute(push.plan(wanted(), windows_records(client), {}), Dead())
    assert result.skipped[0][1] == "not attempted: the connection to the server failed"


# -- F58: failed changes that may have reached Windows are told apart from clean refusals -------------------------


def test_reservation_updated_with_a_failed_restore_counts_as_maybe_written(client, fake):
    _, first = run(client, wanted())
    managed = state_after({}, first)
    fake.reservation_set_drops_options = True
    boom = PSError(message="boom", command="Set-DhcpServerv4OptionValue")
    fake.fail = lambda op, p: boom if op == "option.set" else None
    source = [
        Record(r.kind, r.key, {**r.fields, "description": "changed"}) if r.kind == "reservation" else r
        for r in wanted()
    ]
    _, result = run(client, source, managed)
    ((failed, message),) = result.failed
    assert failed.kind == "reservation" and "was updated, but" in message
    assert fake.reservations["10.1.0.50"]["description"] == "changed"
    assert result.maybe_written == [failed]
    assert failed not in result.applied


def test_windows_error_and_lost_reply_count_as_maybe_written_but_a_refusal_does_not(client, fake):
    _, first = run(client, [scope()])
    managed = state_after({}, first)
    # A refusal raised before anything is sent: the subnet mask cannot change.
    _, result = run(client, [scope(subnet_mask="255.255.0.0")], managed)
    assert len(result.failed) == 1 and result.maybe_written == []
    # An error reported by Windows: part of the change may have been made.
    fake.fail = lambda op, p: (
        PSError("boom", "InvalidOperation", "", "Set-DhcpServerv4Scope") if op == "scope.set" else None
    )
    _, result = run(client, [scope(description="new")], managed)
    assert [c.key for c in result.maybe_written] == ["10.1.0.0"] and not result.aborted
    # Not found on an update: nothing was there to change.
    gone = PSError.from_dict({"message": "gone", "category": "ObjectNotFound", "command": "Set-DhcpServerv4Scope"})
    fake.fail = lambda op, p: gone if op == "scope.set" else None
    _, result = run(client, [scope(description="new")], managed)
    assert result.failed[0][1] == "not found on Windows" and result.maybe_written == []

    class LostReply:
        def apply(self, change):
            raise RunnerError("timed out")

    fake.fail = None
    result = push.execute(push.plan([scope(description="new")], windows_records(client), managed), LostReply())
    assert [c.key for c in result.maybe_written] == ["10.1.0.0"] and result.aborted


def test_push_refused_says_whether_it_wrote():
    assert PushRefused("no").wrote is False
    assert PushRefused("partly", wrote=True).wrote is True
    assert str(PushRefused("partly", wrote=True)) == "partly"


# -- F65: "not found" while leaving a failover relationship is not "scope deleted" -------------------------------


def not_found(command):
    return PSError.from_dict({"message": "gone", "category": "ObjectNotFound", "command": command})


def two_scopes_in_ha(client):
    source = [scope_at("10.1.0.0"), scope_at("10.2.0.0"), failover(scope_ids=["10.1.0.0", "10.2.0.0"])]
    _, first = run(client, source)
    return state_after({}, first)


def test_scope_delete_goes_on_when_its_relationship_vanished_meanwhile(client, fake):
    managed = two_scopes_in_ha(client)

    def fail(op, p):
        if op == "failover.remove_scope":
            # Someone removed the relationship (on the partner, say) after the push listed it.
            fake.failovers.pop("ha", None)
            return not_found("Remove-DhcpServerv4FailoverScope")
        return None

    change = Change("delete", "scope", "10.2.0.0", before=scope_at("10.2.0.0").fields)
    fake.fail = fail
    result = push.execute(push.Plan(changes=[change]), WindowsWriter(client))
    assert "failover.remove_scope" in fake.ops() and "scope.remove" in fake.ops()
    assert "10.2.0.0" not in fake.scopes
    assert result.applied == [change] and ("scope", "10.2.0.0") in result.removals
    assert managed  # the earlier push recorded it


def test_scope_still_on_windows_is_not_recorded_as_deleted(client, fake):
    two_scopes_in_ha(client)
    # The failover step reports "not found" and the scope delete then fails: the scope is still there.
    errors = {
        "failover.remove_scope": not_found("Remove-DhcpServerv4FailoverScope"),
        "scope.remove": PSError("in a relationship", "InvalidOperation", "DHCP 20113", "Remove-DhcpServerv4Scope"),
    }
    fake.fail = lambda op, p: errors.get(op)
    change = Change("delete", "scope", "10.2.0.0", before=scope_at("10.2.0.0").fields)
    result = push.execute(push.Plan(changes=[change]), WindowsWriter(client))
    assert "10.2.0.0" in fake.scopes
    assert result.applied == [] and ("scope", "10.2.0.0") not in result.removals
    assert [c.key for c, _ in result.failed] == ["10.2.0.0"]


def test_scope_already_gone_is_still_recorded_as_deleted(client, fake):
    two_scopes_in_ha(client)
    fake.fail = lambda op, p: not_found("Remove-DhcpServerv4Scope") if op == "scope.remove" else None
    change = Change("delete", "scope", "10.2.0.0", before=scope_at("10.2.0.0").fields)
    result = push.execute(push.Plan(changes=[change]), WindowsWriter(client))
    assert result.applied == [change] and ("scope", "10.2.0.0") in result.removals
