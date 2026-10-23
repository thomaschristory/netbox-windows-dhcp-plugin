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
from ipam.models import IPRange
from netbox_dhcp.models import HostReservation, Option, Subnet
from utilities.testing import APITestCase
from utilities.testing import TestCase as ViewTestCase

from netbox_windows_dhcp_plugin import signals
from netbox_windows_dhcp_plugin.choices import SyncKindChoices, SyncStatusChoices
from netbox_windows_dhcp_plugin.integrations import branching
from netbox_windows_dhcp_plugin.jobs import _LOCK_CLASS, DryRunJob, PushJob, enqueue_push
from netbox_windows_dhcp_plugin.models import FailoverRelationship, SyncRun, SyncState, WindowsScope
from netbox_windows_dhcp_plugin.sync import diff, netbox_state, windows_state
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import Reservation
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .fixtures import PluginAPIMixin, dhcp_servers, windows_server
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
        return diff.diff(netbox_state.read(self.server, for_import=True), records, equal=windows_state.equal)


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
        subnet = Subnet.objects.get()
        subnet.snapshot()
        subnet.description = "renamed in NetBox"
        subnet.save()
        ws = WindowsScope.objects.get(subnet=subnet)
        ws.state = "inactive"
        ws.save()
        run = self._run()
        self.assertEqual(run.summary["update"], 1)
        self.assertEqual(self.fake.scopes["10.1.0.0"]["name"], "renamed in NetBox")
        self.assertEqual(self.fake.scopes["10.1.0.0"]["state"], "Inactive")

    def test_reservation_description_change_keeps_its_option_values(self):
        """Issue #42: Windows may drop a reservation's values on update; the push puts them back."""
        self._run()  # adopts everything
        self.fake.reservation_set_drops_options = True
        reservation = HostReservation.objects.get()
        reservation.snapshot()
        # The Windows description is the sys4 comments field (the sys4 description is the Windows name).
        reservation.comments = "changed in NetBox"
        reservation.save()
        dry = self._run(DryRunJob)
        self.assertEqual([(c["action"], c["kind"]) for c in dry.summary["planned"]], [("update", "reservation")])
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual((run.summary["update"], run.summary["failed"]), (1, 0))
        self.assertEqual(self.fake.reservations["10.1.0.50"]["description"], "changed in NetBox")
        self.assertEqual(self.fake.options[(3, "", "10.1.0.50", "", "", "")]["value"], ["10.1.0.254"])
        self.assertEqual(self._run(DryRunJob).summary["changes"], 0)

    def test_deleted_in_netbox_is_deleted_on_windows_once_managed(self):
        self._run()  # adopts everything
        HostReservation.objects.get().delete()
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
        from netbox_windows_dhcp_plugin.windows.psscript import PSError

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
        HostReservation.objects.get().delete()
        self._run()
        self.assertNotIn("10.1.0.50", self.fake.reservations)

    def test_scope_with_policies_is_not_deleted(self):
        from netbox_windows_dhcp_plugin.windows.client import Policy

        DhcpClient(self.fake).add_policy(
            Policy(name="phones", scope_id="10.1.0.0", criteria={"vendor_class": ["EQ,MSFT"]})
        )
        Subnet.objects.get().delete()
        run = self._run()
        self.assertIn("10.1.0.0", self.fake.scopes)
        reasons = {s["key"]: s["reason"] for s in run.summary["not_pushed"]}
        self.assertIn("policy 10.1.0.0/phones", reasons["10.1.0.0"])

    def test_lease_time_option_is_not_pushed(self):
        from netbox_dhcp.models import OptionDefinition

        definition = OptionDefinition.objects.filter(code=51, space="dhcp4", standard=True).first()
        if definition is None:  # netbox-plugin-dhcp (like Kea) may not define it: use a server definition
            definition = OptionDefinition.objects.create(
                dhcp_server=self.dhcp, family=4, space="dhcp4", code=51, name="lease-time", type="uint32"
            )
        Option.objects.create(definition=definition, data="3600", assigned_object=Subnet.objects.get())
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
class FailoverPushTest(PushTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        (self.partner_dhcp,) = dhcp_servers(1, start=2)
        self.relationship = FailoverRelationship.objects.create(
            name="fo", primary_server=self.dhcp, partner_server=self.partner_dhcp
        )
        self.relationship.subnets.add(Subnet.objects.get())
        self.fake.failovers["fo"] = {"name": "fo", "scope_ids": ["10.1.0.0"]}

    def test_primary_replicates_changed_scopes(self):
        subnet = Subnet.objects.get()
        subnet.snapshot()
        subnet.description = "changed"
        subnet.save()
        self._run()
        self.assertEqual(self.fake.replications[-1]["scope_ids"], ["10.1.0.0"])

    def test_nothing_changed_nothing_replicated(self):
        self._run()
        self.assertEqual(self.fake.replications, [])

    def test_partner_leaves_failover_scopes_to_the_primary(self):
        # Make this server the partner instead.
        self.relationship.primary_server, self.relationship.partner_server = self.partner_dhcp, self.dhcp
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
        reservation = HostReservation.objects.get()
        objects = [
            Subnet.objects.get(),
            reservation,
            reservation.ipv4_address,
            reservation.hw_address,
            IPRange.objects.get(),
            Subnet.objects.get().prefix,
            *Option.objects.all(),
            *WindowsScope.objects.all(),
        ]
        for obj in objects:
            with self.subTest(obj=obj):
                self.assertEqual(self.servers(obj), {self.dhcp.pk})

    def test_unrelated_objects_map_to_nothing(self):
        mac = MACAddress.objects.create(mac_address="00:00:00:00:00:01")
        self.assertEqual(self.servers(mac), set())
        self.assertEqual(signals.servers_for("dcim.site", 1, {"name": "x"}), set())

    def test_object_change_covers_before_and_after(self):
        (other,) = dhcp_servers(1, start=5)
        subnet = Subnet.objects.get()
        subnet.snapshot()
        subnet.dhcp_server = other
        change = subnet.to_objectchange(ObjectChangeActionChoices.ACTION_UPDATE)
        self.assertEqual(signals.servers_for_change(change), {self.dhcp.pk, other.pk})


class StandaloneTriggerTest(ImportTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.apply_all()

    def push_jobs(self):
        return PushJob.get_jobs(self.server)

    def test_save_queues_one_debounced_push_after_commit(self):
        subnet = Subnet.objects.get()
        with STANDALONE, self.captureOnCommitCallbacks(execute=True):
            subnet.description = "x"
            subnet.save()
            HostReservation.objects.get().save()
        job = self.push_jobs().get()
        self.assertEqual(job.status, JobStatusChoices.STATUS_SCHEDULED)

    def test_delete_queues_a_push(self):
        with STANDALONE, self.captureOnCommitCallbacks(execute=True):
            HostReservation.objects.get().delete()
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
            Subnet.objects.get().save()
        self.assertFalse(self.push_jobs().exists())

    def test_saves_inside_branches_or_merges_are_ignored(self):
        with (
            mock.patch(
                "netbox_windows_dhcp_plugin.integrations.branching.save_belongs_to_branching", return_value=True
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            Subnet.objects.get().save()
        self.assertFalse(self.push_jobs().exists())

    def test_direct_edits_of_main_push_with_branching_too(self):
        # No branch active and nothing merging: an edit of main is pushed, whether or not branching is installed.
        with self.captureOnCommitCallbacks(execute=True):
            Subnet.objects.get().save()
        self.assertEqual(self.push_jobs().count(), 1)

    def test_moving_a_subnet_pushes_both_servers(self):
        (other_dhcp,) = dhcp_servers(1, start=7)
        other = windows_server(other_dhcp)
        subnet = Subnet.objects.get()
        subnet.snapshot()
        subnet.dhcp_server = other_dhcp
        with STANDALONE, self.captureOnCommitCallbacks(execute=True):
            subnet.save()
        self.assertEqual(self.push_jobs().count(), 1)
        self.assertEqual(PushJob.get_jobs(other).count(), 1)

    def test_deleting_a_reservation_address_queues_a_push(self):
        with STANDALONE, self.captureOnCommitCallbacks(execute=True):
            HostReservation.objects.get().ipv4_address.delete()
        self.assertEqual(self.push_jobs().count(), 1)


class _Changes(list):
    def select_related(self, *args):
        return self


class BranchTriggerTest(ImportTestMixin, TestCase):
    """The merge and revert receivers, with a stand-in branch (merging a real one is covered by netbox-branching)."""

    def setUp(self):
        super().setUp()
        self.apply_all()
        subnet = Subnet.objects.get()
        subnet.snapshot()
        subnet.description = "merged"
        self.branch = SimpleNamespace(
            pk=1,
            name="feature",
            get_merged_changes=lambda: _Changes([subnet.to_objectchange(ObjectChangeActionChoices.ACTION_UPDATE)]),
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
        self.server = windows_server(dhcp_servers(1)[0])

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
        self.server = windows_server(dhcp_servers(1)[0])
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
