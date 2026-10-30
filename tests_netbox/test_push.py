"""Push to Windows: the job against the fake server, SyncState, triggers in both modes, and the UI and API actions."""

import uuid
from types import SimpleNamespace
from unittest import mock

from core.choices import JobStatusChoices, ObjectChangeActionChoices
from core.exceptions import JobFailed
from core.models import Job
from dcim.models import MACAddress
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from ipam.models import IPRange
from utilities.testing import APITestCase
from utilities.testing import TestCase as ViewTestCase

from netbox_windows_dhcp_plugin import signals
from netbox_windows_dhcp_plugin.choices import SyncKindChoices, SyncStatusChoices
from netbox_windows_dhcp_plugin.integrations import branching
from netbox_windows_dhcp_plugin.jobs import _LOCK_CLASS, DryRunJob, PushJob, enqueue_push
from netbox_windows_dhcp_plugin.models import ExclusionRange, FailoverRelationship, OptionValue, SyncRun, SyncState
from netbox_windows_dhcp_plugin.models import Reservation as NetBoxReservation
from netbox_windows_dhcp_plugin.models import Scope as NetBoxScope
from netbox_windows_dhcp_plugin.sync import diff, netbox_state, windows_state
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import OptionKey, Reservation
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import PSError

from .fixtures import PluginAPIMixin, windows_server
from .test_import import ImportTestMixin
from .test_jobs import CREDENTIALS

STANDALONE = mock.patch("netbox_windows_dhcp_plugin.integrations.branching.branching_installed", return_value=False)


class PushTestMixin(ImportTestMixin):
    """NetBox holds what the seeded fake server holds (imported and applied); pushes go to ``self.target``."""

    def setUp(self):
        super().setUp()
        self.apply_all()
        self.target = self.fake
        PushJob.runner_factory = staticmethod(lambda server: self.target)
        self.addCleanup(setattr, PushJob, "runner_factory", None)

    def _run(self, job_class=PushJob, **kwargs):
        job = Job.objects.create(
            object=self.server, name=job_class.name, status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4()
        )
        job_class(job).run(**kwargs)
        return SyncRun.objects.filter(job=job).first()

    def remaining(self):
        """Differences left between NetBox and the target server."""
        records, _ = windows_state.to_records(
            windows_state.read(DhcpClient(self.target)), standard_codes=netbox_state.standard_codes()
        )
        # As an import sees it: no MAC filter lists object in NetBox means Windows' defaults.
        return diff.diff(
            netbox_state.read(self.server, for_import=True), records, equal_records=windows_state.records_equal
        )


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class PushJobTest(PushTestMixin, TestCase):
    def test_push_to_an_empty_server_creates_everything(self):
        self.target = FakeDhcpServer()
        run = self._run()
        self.assertEqual(run.kind, SyncKindChoices.PUSH)
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(self.remaining(), [])
        self.assertEqual(run.summary["failed"], 0)
        self.assertEqual(SyncState.objects.filter(windows_server=self.server).count(), run.summary["create"])

    def test_push_of_matching_data_changes_nothing_and_adopts_it(self):
        run = self._run()
        self.assertEqual(run.summary["changes"], 0)
        self.assertNotIn("scope.set", self.fake.ops()[-5:])
        kinds = set(SyncState.objects.values_list("kind", flat=True))
        self.assertEqual(kinds, {"class", "optiondef", "scope", "exclusion", "reservation", "option"})

    def test_netbox_edits_reach_windows(self):
        self._run()
        scope = NetBoxScope.objects.get()
        scope.snapshot()
        scope.name = "renamed in NetBox"
        scope.state = "inactive"
        scope.save()
        run = self._run()
        self.assertEqual(run.summary["update"], 1)
        self.assertEqual(self.fake.scopes["10.1.0.0"]["name"], "renamed in NetBox")
        self.assertEqual(self.fake.scopes["10.1.0.0"]["state"], "Inactive")

    def test_option_value_with_commas_and_spaces_round_trips(self):
        """A value holding a comma and spaces comes in from Windows and goes back out exactly as it was."""
        value = [" a, b ", "c,d"]
        DhcpClient(self.fake).set_option_value(OptionKey(option_id=224, scope_id="10.1.0.0"), value)
        self.apply_all()
        self.assertEqual(OptionValue.objects.get(code=224).value, value)
        self.target = FakeDhcpServer()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(self.target.options[(224, "10.1.0.0", "", "", "", "")]["value"], value)
        self.assertEqual(self.remaining(), [])
        self.assertEqual(self._run(DryRunJob).summary["changes"], 0)

    def test_reservation_description_change_keeps_its_option_values(self):
        """Issue #42: Windows may drop a reservation's values on update; the push puts them back."""
        self._run()  # adopts everything
        self.fake.reservation_set_drops_options = True
        reservation = NetBoxReservation.objects.get()
        reservation.snapshot()
        reservation.description = "changed in NetBox"
        reservation.save()
        dry = self._run(DryRunJob)
        self.assertEqual([(c["action"], c["kind"]) for c in dry.summary["planned"]], [("update", "reservation")])
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual((run.summary["update"], run.summary["failed"]), (1, 0))
        self.assertEqual(self.fake.reservations["10.1.0.50"]["description"], "changed in NetBox")
        self.assertEqual(self.fake.options[(3, "", "10.1.0.50", "", "", "")]["value"], ["10.1.0.254"])
        self.assertEqual(self._run(DryRunJob).summary["changes"], 0)

    def test_failed_reservation_value_restore_says_whether_the_next_push_recreates_it(self):
        """Issue #42 review: a value NetBox does not hold never comes back by itself, and the error says so."""
        self._run()  # adopts everything
        # Added by hand on Windows after the import: NetBox does not hold it.
        DhcpClient(self.fake).set_option_value(OptionKey(15, reserved_ip="10.1.0.50"), ["by.hand"])
        self.fake.reservation_set_drops_options = True
        self.fake.fail = lambda op, p: (
            PSError("denied", "PermissionDenied", "", "Set-DhcpServerv4OptionValue") if op == "option.set" else None
        )
        reservation = NetBoxReservation.objects.get()
        reservation.snapshot()
        reservation.description = "changed in NetBox"
        reservation.save()
        with self.assertRaises(JobFailed):
            self._run()
        run = SyncRun.objects.get(kind=SyncKindChoices.PUSH, status=SyncStatusChoices.FAILED)
        self.assertIn("option 3 (NetBox holds this value, so the next push recreates it)", run.error)
        self.assertIn("option 15 (NetBox does not hold this value", run.error)
        self.assertIn("put it back by hand", run.error)

    def test_deleted_in_netbox_is_deleted_on_windows_once_managed(self):
        self._run()  # adopts everything
        NetBoxReservation.objects.get().delete()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertNotIn("10.1.0.50", self.fake.reservations)
        self.assertFalse(SyncState.objects.filter(kind="reservation").exists())

    def test_unmanaged_windows_objects_are_left_alone(self):
        DhcpClient(self.fake).add_reservation(
            Reservation(scope_id="10.1.0.0", ip_address="10.1.0.99", client_id="aa-bb-cc-00-00-99")
        )
        run = self._run()
        # The reservation, and the MAC filter list switches NetBox holds no object for.
        self.assertEqual(run.summary["unmanaged"], 2)
        self.assertIn("10.1.0.99", self.fake.reservations)

    def test_dry_run_writes_nothing(self):
        self.target = FakeDhcpServer()
        managed = set(SyncState.objects.values_list("kind", "key", "fingerprint"))
        run = self._run(DryRunJob)
        self.assertEqual(run.kind, SyncKindChoices.DRY_RUN)
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED)
        self.assertGreater(run.summary["create"], 0)
        self.assertEqual(len(run.summary["planned"]), run.summary["changes"])
        self.assertEqual(self.target.scopes, {})
        self.assertEqual(set(SyncState.objects.values_list("kind", "key", "fingerprint")), managed)

    def test_sync_disabled_pushes_nothing(self):
        self.server.sync_enabled = False
        self.server.save()
        self.target = FakeDhcpServer()
        self.assertIsNone(self._run())
        self.assertEqual(self.target.scopes, {})
        # A dry run still works.
        self.assertEqual(self._run(DryRunJob).status, SyncStatusChoices.COMPLETED)

    def test_windows_errors_on_one_object_fail_the_run_but_not_the_rest(self):
        self.target = FakeDhcpServer()
        self.target.fail = lambda op, p: (
            PSError("denied", "PermissionDenied", "", "Add-DhcpServerv4Class") if op == "class.add" else None
        )
        with self.assertRaises(JobFailed):
            self._run()
        run = SyncRun.objects.get(kind=SyncKindChoices.PUSH)
        self.assertEqual(run.status, SyncStatusChoices.FAILED)
        self.assertIn("Add-DhcpServerv4Class", run.error)
        self.assertIn("10.1.0.0", self.target.scopes)

    def test_job_log_counts_what_was_applied_not_what_was_planned(self):
        from types import SimpleNamespace

        from netbox_windows_dhcp_plugin.jobs import push_log_line
        from netbox_windows_dhcp_plugin.sync import push
        from netbox_windows_dhcp_plugin.sync.pusher import PushOutcome

        plan = SimpleNamespace(changes=["a", "b", "c", "d"])
        result = push.Result(applied=["a"], failed=[("b", "denied")], skipped=[("c", "x"), ("d", "x")])
        self.assertEqual(
            push_log_line("dhcp1", PushOutcome(plan=plan, result=result), dry_run=False),
            "Push to dhcp1: changed 1 of 4 object(s), 1 failed, 2 not attempted",
        )
        self.assertEqual(
            push_log_line("dhcp1", PushOutcome(plan=plan), dry_run=True), "Push to dhcp1: would change 4 object(s)"
        )
        empty = SimpleNamespace(changes=[])
        self.assertEqual(
            push_log_line("dhcp1", PushOutcome(plan=empty, result=push.Result()), dry_run=False),
            "Push to dhcp1: nothing to change",
        )
        replication = PushOutcome(plan=empty, result=push.Result(), replication_errors=["campus: denied"])
        self.assertEqual(
            push_log_line("dhcp1", replication, dry_run=False), "Push to dhcp1, failover replication failed (1)"
        )

    def test_busy_server_reschedules_the_push(self):
        other = connection.copy()
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s, %s)", [_LOCK_CLASS, self.server.pk])
            self.assertIsNone(self._run())
        finally:
            other.close()
        retry = PushJob.get_jobs(self.server).get(status=JobStatusChoices.STATUS_SCHEDULED)
        self.assertIsNotNone(retry.scheduled)

    def test_one_waiting_push_per_server(self):
        self.assertIsNotNone(enqueue_push(self.server))
        self.assertIsNone(enqueue_push(self.server))
        # A dry run is queued on its own.
        self.assertIsNotNone(enqueue_push(self.server, dry_run=True))

    def test_applying_an_import_records_the_objects_as_managed(self):
        # No push has run yet, but a NetBox delete of an imported object still reaches Windows.
        self.assertTrue(SyncState.objects.filter(kind="reservation").exists())
        NetBoxReservation.objects.get().delete()
        self._run()
        self.assertNotIn("10.1.0.50", self.fake.reservations)

    def test_scope_with_policies_is_not_deleted(self):
        from netbox_windows_dhcp_plugin.windows.client import Policy

        DhcpClient(self.fake).add_policy(
            Policy(name="phones", scope_id="10.1.0.0", criteria={"vendor_class": ["EQ,MSFT"]})
        )
        NetBoxScope.objects.get().delete()
        run = self._run()
        self.assertIn("10.1.0.0", self.fake.scopes)
        reasons = {s["key"]: s["reason"] for s in run.summary["not_pushed"]}
        self.assertIn("policy 10.1.0.0/phones", reasons["10.1.0.0"])

    def test_lease_time_option_is_not_pushed(self):
        OptionValue.objects.create(server=self.server, scope=NetBoxScope.objects.get(), code=51, value=["3600"])
        run = self._run()
        self.assertEqual(run.summary["changes"], 0)
        self.assertTrue(any("lease time" in s["reason"] for s in run.summary["not_pushed"]))

    def test_gives_up_after_the_last_attempt(self):
        from netbox_windows_dhcp_plugin.jobs import PUSH_ATTEMPTS

        other = connection.copy()
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s, %s)", [_LOCK_CLASS, self.server.pk])
            with self.assertRaises(JobFailed):
                self._run(attempt=PUSH_ATTEMPTS)
        finally:
            other.close()
        self.assertFalse(PushJob.get_jobs(self.server).filter(status=JobStatusChoices.STATUS_SCHEDULED).exists())

    def test_orphaned_waiting_jobs_do_not_block_new_pushes(self):
        from datetime import timedelta

        from django.utils import timezone

        job = enqueue_push(self.server)
        Job.objects.filter(pk=job.pk).update(created=timezone.now() - timedelta(hours=1))
        self.assertIsNotNone(enqueue_push(self.server))

    def test_merge_branch_name_is_recorded(self):
        run = self._run(branch="feature/dhcp")
        self.assertEqual(run.branch_name, "feature/dhcp")


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class SyncCorrectnessPushTest(PushTestMixin, TestCase):
    def test_string_option_edits_are_written_and_manual_drift_blocks_deletion(self):
        value = OptionValue.objects.create(server=self.server, code=67, value=["0010"])
        self._run()
        value.value = ["10"]
        value.save()
        run = self._run()
        self.assertEqual(run.summary["update"], 1)
        self.assertEqual(self.client.get_option_value(OptionKey(67)).value, ["10"])
        self.client.set_option_value(OptionKey(67), ["0010"])
        value.delete()
        run = self._run()
        self.assertTrue(any("changed on Windows" in s["reason"] for s in run.summary["not_pushed"]))
        self.assertEqual(self.client.get_option_value(OptionKey(67)).value, ["0010"])

    def test_import_detects_numeric_looking_string_changes(self):
        self.client.set_option_value(OptionKey(67), ["0010"])
        self.apply_all()
        self.client.set_option_value(OptionKey(67), ["10"])
        changes = [c for c in self.changes() if c.kind == "option" and c.key == "server/67"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].action, "update")
        self.apply_all()
        self.assertEqual(OptionValue.objects.get(server=self.server, code=67).value, ["10"])
        self.assertEqual(self._run().summary["changes"], 0)

    def test_netbox_reader_uses_model_types_for_custom_and_vendor_options(self):
        from netbox_windows_dhcp_plugin.models import OptionDefinition
        from netbox_windows_dhcp_plugin.sync import push

        cases = [
            (224, "", "String", False),
            (224, "", "BinaryData", False),
            (224, "", "DWord", True),
            (2, "MSFT", "String", False),
            (2, "MSFT", "DWord", True),
        ]
        for code, vendor, data_type, numeric in cases:
            with self.subTest(code=code, vendor=vendor, data_type=data_type):
                definition = OptionDefinition.objects.get(server=self.server, code=code)
                definition.windows_type = data_type
                definition.default_value = ["0x10"]
                definition.save()
                option = OptionValue.objects.get(server=self.server, code=code)
                option.value = ["0x10"]
                option.save()
                records = netbox_state.read(self.server)
                value = next(r for r in records if r.kind == "option" and r.fields["option_id"] == code)
                default = next(r for r in records if r.kind == "optiondef" and r.fields["option_id"] == code)
                for record, field in ((value, "value"), (default, "default_value")):
                    decimal = diff.Record(record.kind, record.key, {**record.fields, field: ["16"]})
                    self.assertEqual(windows_state.records_equal(record.kind, record.fields, decimal.fields), numeric)
                    self.assertEqual(push.fingerprint(record) == push.fingerprint(decimal), numeric)

    def test_full_length_names_import_and_push_for_both_families(self):
        from .test_v6 import seed_v6

        seed_v6(self.fake)
        for length in (200, 201, 255):
            with self.subTest(length=length):
                name = "n" * length
                for objects in (
                    self.fake.scopes,
                    self.fake.reservations,
                    self.fake.v6_scopes,
                    self.fake.v6_reservations,
                ):
                    for fields in objects.values():
                        fields["name"] = name
                self.apply_all()
                objects = [*NetBoxScope.objects.all(), *NetBoxReservation.objects.all()]
                self.assertEqual(len(objects), 4)
                self.assertTrue(all(obj.name == name for obj in objects))
                for obj in objects:
                    obj.name = name[:-1] + "z"
                    obj.save()
                run = self._run()
                self.assertEqual(run.summary["update"], 4)
                self.assertEqual(self._run(DryRunJob).summary["changes"], 0)


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class FailoverPushTest(PushTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.partner = windows_server("dhcp2")
        self.relationship = FailoverRelationship.objects.create(
            name="fo", primary_server=self.server, partner_server=self.partner
        )
        self.relationship.scopes.add(NetBoxScope.objects.get())
        # As Windows holds it with the settings NetBox defaults to, so only scope changes need replicating.
        self.fake.failovers["fo"] = {
            "name": "fo",
            "partner_server": "dhcp2.example.com",
            "scope_ids": ["10.1.0.0"],
            "max_client_lead_time": 3600,
        }

    def test_clearing_last_membership_preserves_scope_and_converges(self):
        self._run()
        self.relationship.scopes.clear()
        run = self._run()
        self.assertEqual(run.summary["update"], 1)
        self.assertEqual(self.fake.failovers["fo"]["scope_ids"], [])
        self.assertIn("10.1.0.0", self.fake.scopes)
        self.assertEqual(self._run().summary["changes"], 0)

    def _protected_membership(self, settings_change):
        from netbox_windows_dhcp_plugin.sync import push
        from netbox_windows_dhcp_plugin.windows.client import Scope

        self.client.add_scope(
            Scope(name="second", start_range="10.2.0.10", end_range="10.2.0.200", subnet_mask="255.255.255.0")
        )
        self.apply_all()
        self._run()
        self.client.add_failover_scopes("fo", ["10.2.0.0"])
        if settings_change:
            self.relationship.load_balance_percent = 70
            self.relationship.save()
        for _ in range(3):
            run = self._run()
            self.assertTrue(any("added on Windows" in s["reason"] for s in run.summary["not_pushed"]))
            self.assertEqual(self.fake.failovers["fo"]["scope_ids"], ["10.1.0.0", "10.2.0.0"])
            state = SyncState.objects.get(windows_server=self.server, kind="failover", key="fo")
            self.assertEqual(state.fingerprint, push.PROTECTED_MEMBERSHIPS)
        if settings_change:
            self.assertEqual(self.fake.failovers["fo"]["load_balance_percent"], 70)

    def test_protected_membership_survives_repeated_push_jobs(self):
        self._protected_membership(settings_change=False)

    def test_protected_membership_survives_unrelated_settings_write(self):
        self._protected_membership(settings_change=True)

    def test_later_preflight_failure_persists_completed_relationship_write(self):
        from netbox_windows_dhcp_plugin.sync import push
        from netbox_windows_dhcp_plugin.windows.client import FailoverRelationship as WindowsFailover
        from netbox_windows_dhcp_plugin.windows.client import Scope

        for number in (2, 3, 4):
            self.client.add_scope(
                Scope(
                    name=f"scope{number}",
                    start_range=f"10.{number}.0.10",
                    end_range=f"10.{number}.0.200",
                    subnet_mask="255.255.255.0",
                )
            )
        self.client.add_failover_scopes("fo", ["10.2.0.0"])
        self.client.add_failover(
            WindowsFailover(name="zz", partner_server="dhcp2.example.com", scope_ids=["10.3.0.0", "10.4.0.0"])
        )
        self.apply_all()
        self._run()
        NetBoxScope.objects.get(prefix__prefix="10.2.0.0/24").delete()
        NetBoxScope.objects.get(prefix__prefix="10.4.0.0/24").delete()
        self.fake.fail = lambda op, p: (
            PSError("lease query refused", "PermissionDenied", "", "Get-DhcpServerv4Lease")
            if op == "lease.list" and p["scope_id"] == "10.4.0.0"
            else None
        )
        with self.assertRaises(JobFailed):
            self._run()
        state = SyncState.objects.get(windows_server=self.server, kind="failover", key="fo")
        actual = windows_state.failover_record(self.client.get_failover("fo"))
        self.assertEqual(state.fingerprint, push.fingerprint(actual))
        self.assertEqual(self.fake.failovers["fo"]["scope_ids"], ["10.1.0.0"])
        self.assertEqual(self.fake.failovers["zz"]["scope_ids"], ["10.3.0.0", "10.4.0.0"])
        self.assertIn("10.4.0.0", self.fake.scopes)

    def test_primary_replicates_changed_scopes(self):
        self._change_scope()
        self._run()
        # Only the scoped parameter set: Windows refuses a relationship name with scope ids (#51).
        self.assertEqual(self.fake.replications[-1], {"name": "", "scope_ids": ["10.1.0.0"]})

    def test_nothing_changed_nothing_replicated(self):
        run = self._run()
        self.assertEqual(run.summary["changes"], 0, run.summary.get("planned"))
        self.assertEqual(self.fake.replications, [])

    def _fail_replication(self):
        self.fake.fail = lambda op, p: (
            PSError("denied", "PermissionDenied", "", "Invoke-DhcpServerv4FailoverReplication")
            if op == "failover.replicate"
            else None
        )

    def _change_scope(self):
        scope = NetBoxScope.objects.get()
        scope.snapshot()
        scope.name = "changed"
        scope.save()

    def test_failed_replication_is_retried_by_the_next_push(self):
        self._fail_replication()
        self._change_scope()
        with self.assertRaises(JobFailed):
            self._run()
        failed = SyncRun.objects.get(kind=SyncKindChoices.PUSH)
        self.assertEqual(failed.summary["replication_pending"], {"fo": ["10.1.0.0"]})
        self.assertIn("failover replication fo", failed.error)
        # Nothing left to write, but the partner is still behind: the next push replicates again.
        self.fake.fail = None
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED)
        self.assertEqual(self.fake.replications, [{"name": "", "scope_ids": ["10.1.0.0"]}])
        self.assertNotIn("replication_pending", run.summary)
        # Once copied, it is not copied again.
        self._run()
        self.assertEqual(len(self.fake.replications), 1)

    def test_pending_replication_survives_a_push_that_stopped_before_writing(self):
        self._fail_replication()
        self._change_scope()
        with self.assertRaises(JobFailed):
            self._run()
        # A later push that never reached Windows (a connection error) records no summary.
        SyncRun.objects.create(
            windows_server=self.server,
            kind=SyncKindChoices.PUSH,
            status=SyncStatusChoices.FAILED,
            completed=timezone.now(),
            error="connection refused",
        )
        self.fake.fail = None
        self._run()
        self.assertEqual(self.fake.replications, [{"name": "", "scope_ids": ["10.1.0.0"]}])

    def test_scopes_not_yet_in_the_windows_relationship_wait_for_the_next_push(self):
        # The scope is not in the relationship on Windows (say its addition failed): replicating it would make Windows
        # refuse the whole call, so it waits instead. That alone does not fail the push. Here NetBox cannot push the
        # relationship either (hot standby without a role), so nothing adds the scope to it on Windows.
        self.relationship.mode = "hotstandby"
        self.relationship.save()
        self.fake.failovers["fo"]["scope_ids"] = []
        self._change_scope()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED)
        self.assertEqual(self.fake.replications, [])
        self.assertEqual(run.summary["replication_pending"], {"fo": ["10.1.0.0"]})
        # Once the scope is in the relationship, the next push replicates it.
        self.fake.failovers["fo"]["scope_ids"] = ["10.1.0.0"]
        run = self._run()
        self.assertEqual(self.fake.replications, [{"name": "", "scope_ids": ["10.1.0.0"]}])
        self.assertNotIn("replication_pending", run.summary)

    def test_pending_replication_skips_scopes_no_longer_in_the_relationship(self):
        self._fail_replication()
        self._change_scope()
        with self.assertRaises(JobFailed):
            self._run()
        failed = SyncRun.objects.get(kind=SyncKindChoices.PUSH)
        failed.summary["replication_pending"] = {"fo": ["10.1.0.0", "10.9.9.0"], "gone": ["10.1.0.0"]}
        failed.save()
        self.fake.fail = None
        self._run()
        self.assertEqual(self.fake.replications, [{"name": "", "scope_ids": ["10.1.0.0"]}])

    def test_partner_leaves_failover_scopes_to_the_primary(self):
        # Make this server the partner instead.
        self.relationship.primary_server, self.relationship.partner_server = self.partner, self.server
        self.relationship.save()
        self.target = FakeDhcpServer()
        run = self._run()
        self.assertEqual(self.target.scopes, {})
        self.assertTrue(any("failover relationship fo" in s["reason"] for s in run.summary["not_pushed"]))
        # Server-level objects are still pushed.
        self.assertTrue(self.target.classes)


class ServersForTest(ImportTestMixin, TestCase):
    """Which servers a change affects, from the data the change log records."""

    def setUp(self):
        super().setUp()
        self.apply_all()

    def servers(self, obj):
        return signals.servers_for(obj._meta.label_lower, obj.pk, signals._data(obj))

    def test_every_watched_object_maps_to_its_server(self):
        reservation = NetBoxReservation.objects.get()
        objects = [
            NetBoxScope.objects.get(),
            reservation,
            reservation.ip_address,
            reservation.mac_address,
            IPRange.objects.get(),
            NetBoxScope.objects.get().prefix,
            *ExclusionRange.objects.all(),
            *OptionValue.objects.all(),
        ]
        for obj in objects:
            with self.subTest(obj=obj):
                self.assertEqual(self.servers(obj), {self.server.pk})

    def test_unrelated_objects_map_to_nothing(self):
        mac = MACAddress.objects.create(mac_address="00:00:00:00:00:01")
        self.assertEqual(self.servers(mac), set())
        self.assertEqual(signals.servers_for("dcim.site", 1, {"name": "x"}), set())

    def test_object_change_covers_before_and_after(self):
        other = windows_server("dhcp5")
        scope = NetBoxScope.objects.get()
        scope.snapshot()
        scope.server = other
        change = scope.to_objectchange(ObjectChangeActionChoices.ACTION_UPDATE)
        self.assertEqual(signals.servers_for_change(change), {self.server.pk, other.pk})


class StandaloneTriggerTest(ImportTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.apply_all()

    def push_jobs(self):
        return PushJob.get_jobs(self.server)

    def test_save_queues_one_debounced_push_after_commit(self):
        scope = NetBoxScope.objects.get()
        with STANDALONE, self.captureOnCommitCallbacks(execute=True):
            scope.name = "x"
            scope.save()
            NetBoxReservation.objects.get().save()
        job = self.push_jobs().get()
        self.assertEqual(job.status, JobStatusChoices.STATUS_SCHEDULED)

    def test_delete_queues_a_push(self):
        with STANDALONE, self.captureOnCommitCallbacks(execute=True):
            NetBoxReservation.objects.get().delete()
        self.assertEqual(self.push_jobs().count(), 1)

    def test_applying_an_import_queues_no_push(self):
        self.fake.scopes["10.1.0.0"]["name"] = "renamed on Windows"
        with STANDALONE, self.captureOnCommitCallbacks(execute=True):
            self.apply_all()
        self.assertFalse(self.push_jobs().exists())

    def test_sync_disabled_servers_are_not_queued(self):
        self.server.sync_enabled = False
        self.server.save()
        with STANDALONE, self.captureOnCommitCallbacks(execute=True):
            NetBoxScope.objects.get().save()
        self.assertFalse(self.push_jobs().exists())

    def test_saves_inside_branches_or_merges_are_ignored(self):
        with (
            mock.patch(
                "netbox_windows_dhcp_plugin.integrations.branching.save_belongs_to_branching", return_value=True
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            NetBoxScope.objects.get().save()
        self.assertFalse(self.push_jobs().exists())

    def test_direct_edits_of_main_push_with_branching_too(self):
        # No branch active and nothing merging: an edit of main is pushed, whether or not branching is installed.
        with self.captureOnCommitCallbacks(execute=True):
            NetBoxScope.objects.get().save()
        self.assertEqual(self.push_jobs().count(), 1)

    def test_moving_a_scope_pushes_both_servers(self):
        other = windows_server("dhcp7")
        scope = NetBoxScope.objects.get()
        with signals.push_suppressed():
            OptionValue.objects.filter(scope=scope).delete()
        scope.snapshot()
        scope.server = other
        with STANDALONE, self.captureOnCommitCallbacks(execute=True):
            scope.save()
        self.assertEqual(self.push_jobs().count(), 1)
        self.assertEqual(PushJob.get_jobs(other).count(), 1)

    def test_editing_a_reservation_address_queues_a_push(self):
        # The reservation protects its address from deletion; an edit of it still reaches the server.
        address = NetBoxReservation.objects.get().ip_address
        address.snapshot()
        address.address = "10.1.0.51/24"
        with STANDALONE, self.captureOnCommitCallbacks(execute=True):
            address.save()
        self.assertEqual(self.push_jobs().count(), 1)


class _Changes(list):
    def select_related(self, *args):
        return self


class BranchTriggerTest(ImportTestMixin, TestCase):
    """The merge and revert receivers, with a stand-in branch (merging a real one is covered by netbox-branching)."""

    def setUp(self):
        super().setUp()
        self.apply_all()
        scope = NetBoxScope.objects.get()
        scope.snapshot()
        scope.name = "merged"
        self.branch = SimpleNamespace(
            pk=1,
            name="feature",
            get_merged_changes=lambda: _Changes([scope.to_objectchange(ObjectChangeActionChoices.ACTION_UPDATE)]),
        )

    def test_merge_queues_a_push_per_server(self):
        with self.captureOnCommitCallbacks(execute=True):
            branching._on_merge(None, branch=self.branch, user=None)
        job = PushJob.get_jobs(self.server).get()
        self.assertEqual(job.status, JobStatusChoices.STATUS_PENDING)

    def test_revert_uses_the_servers_captured_before(self):
        branching._on_pre_revert(None, branch=self.branch, user=None)
        self.branch.get_merged_changes = lambda: _Changes()  # a reverted branch no longer has merged changes
        with self.captureOnCommitCallbacks(execute=True):
            branching._on_revert(None, branch=self.branch, user=None)
        self.assertEqual(PushJob.get_jobs(self.server).count(), 1)

    def test_errors_never_break_the_merge(self):
        self.branch.get_merged_changes = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertLogs("netbox_windows_dhcp_plugin.integrations.branching", "ERROR"):
            branching._on_merge(None, branch=self.branch, user=None)
        self.assertFalse(PushJob.get_jobs(self.server).exists())


class PushViewTest(ViewTestCase):
    def setUp(self):
        super().setUp()
        self.server = windows_server()

    def test_buttons_and_views(self):
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_windowsserver", "netbox_windows_dhcp_plugin.change_windowsserver"
        )
        response = self.client.get(self.server.get_absolute_url())
        self.assertContains(response, f"/windowsservers/{self.server.pk}/push/")
        self.assertContains(response, f"/windowsservers/{self.server.pk}/dry-run/")
        self.client.post(f"/plugins/windows-dhcp/windowsservers/{self.server.pk}/push/")
        self.client.post(f"/plugins/windows-dhcp/windowsservers/{self.server.pk}/dry-run/")
        self.assertEqual(sorted(self.server.jobs.values_list("name", flat=True)), [DryRunJob.name, PushJob.name])

    def test_view_only_cannot_push(self):
        self.add_permissions("netbox_windows_dhcp_plugin.view_windowsserver")
        response = self.client.post(f"/plugins/windows-dhcp/windowsservers/{self.server.pk}/push/")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.server.jobs.exists())


class PushAPITest(PluginAPIMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.server = windows_server()
        self.url = f"/api/plugins/windows-dhcp/windows-servers/{self.server.pk}/"

    def post(self, action):
        return self.client.post(self.url + action, {}, format="json", **self.header)

    def test_push_and_dry_run(self):
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_windowsserver", "netbox_windows_dhcp_plugin.change_windowsserver"
        )
        response = self.post("push/")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["name"], PushJob.name)
        self.assertEqual(self.post("push/").status_code, 409)
        response = self.post("dry-run/")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["name"], DryRunJob.name)

    def test_push_needs_change(self):
        self.add_permissions("netbox_windows_dhcp_plugin.view_windowsserver")
        self.assertEqual(self.post("push/").status_code, 404)
        self.assertEqual(self.post("dry-run/").status_code, 404)

    def test_push_refused_when_sync_disabled(self):
        self.server.sync_enabled = False
        self.server.save()
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_windowsserver", "netbox_windows_dhcp_plugin.change_windowsserver"
        )
        self.assertEqual(self.post("push/").status_code, 409)
        self.assertEqual(self.post("dry-run/").status_code, 202)
