"""Code quality audit, group g5 (jobs, signals, branching integration): one test class per finding."""

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from core.choices import JobNotificationChoices, JobStatusChoices, ObjectChangeActionChoices
from core.exceptions import JobFailed
from core.models import Job, ObjectChange
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from extras.models import Notification, Tag

from netbox_windows_dhcp_plugin import jobs, signals
from netbox_windows_dhcp_plugin.choices import ImportDestinationChoices, SyncKindChoices, SyncStatusChoices
from netbox_windows_dhcp_plugin.integrations import branching
from netbox_windows_dhcp_plugin.jobs import (
    _LOCK_CLASS,
    PUSH_TIMEOUT,
    ImportJob,
    PushJob,
    ScheduleImportsJob,
    TestConnectionJob,
    due_servers,
    enqueue_import,
)
from netbox_windows_dhcp_plugin.models import Reservation, Scope, SyncRun
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import PSError

from .fixtures import ip_address, windows_server
from .test_import import ImportTestMixin
from .test_jobs import CREDENTIALS
from .test_push import PushTestMixin

BRANCHING = "netbox_windows_dhcp_plugin.integrations.branching"
RESERVATION_TABLE = Reservation._meta.db_table
PLUGIN_TABLES = "netbox_windows_dhcp_plugin_"


def _job(server, job_class, **kwargs):
    return Job.objects.create(
        object=server, name=job_class.name, status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4(), **kwargs
    )


class F19BranchHelpersTest(TestCase):
    """in_branch() is active_branch_name() said another way; the save check keeps its own fail-safe."""

    def test_in_branch_follows_the_active_branch_name(self):
        with mock.patch(f"{BRANCHING}.active_branch_name", return_value="feature"):
            self.assertTrue(branching.in_branch())
        with mock.patch(f"{BRANCHING}.active_branch_name", return_value=None):
            self.assertFalse(branching.in_branch())

    def test_nothing_is_a_branch_without_branching(self):
        with mock.patch(f"{BRANCHING}.branching_installed", return_value=False):
            self.assertFalse(branching.in_branch())
            self.assertIsNone(branching.active_branch_name())
            self.assertFalse(branching.save_belongs_to_branching())

    def test_main_is_not_a_branch(self):
        # Whatever is installed: no branch is active in a test.
        self.assertFalse(branching.in_branch())
        self.assertFalse(branching.save_belongs_to_branching())


class F20AffectedServersTest(ImportTestMixin, TestCase):
    """The merge receivers read only the changes a push cares about, and stop once every server is found."""

    def setUp(self):
        super().setUp()
        self.apply_all()
        self.request_id = uuid.uuid4()
        self.user = get_user_model().objects.create_user(username="audit-g5-changes")

    def _change(self, instance, action=ObjectChangeActionChoices.ACTION_UPDATE):
        instance.snapshot()
        change = instance.to_objectchange(action)
        change.request_id = self.request_id
        change.user = self.user
        change.save()
        return change

    def _branch(self):
        changes = ObjectChange.objects.filter(request_id=self.request_id).order_by("pk")
        return SimpleNamespace(pk=1, name="feature", get_merged_changes=lambda: changes)

    def test_unrelated_changes_are_left_in_the_database(self):
        unrelated = self._change(Tag.objects.create(name="audit-g5", slug="audit-g5"))
        watched = self._change(Scope.objects.get())
        with mock.patch.object(signals, "servers_for_change", wraps=signals.servers_for_change) as resolve:
            ids = branching._affected_servers(self._branch())
        self.assertEqual(ids, {self.server.pk})
        seen = [call.args[0].pk for call in resolve.call_args_list]
        self.assertEqual(seen, [watched.pk])
        self.assertNotIn(unrelated.pk, seen)

    def test_stops_once_every_pushable_server_is_found(self):
        for reservation in (Reservation.objects.get(), Reservation.objects.get(), Reservation.objects.get()):
            self._change(reservation)
        with mock.patch.object(signals, "servers_for_change", wraps=signals.servers_for_change) as resolve:
            ids = branching._affected_servers(self._branch())
        self.assertEqual(ids, {self.server.pk})
        self.assertEqual(resolve.call_count, 1)

    def test_every_server_of_the_branch_is_still_found(self):
        other = windows_server("dhcp2")
        scope = Scope.objects.get()
        self._change(Reservation.objects.get())
        scope.snapshot()
        scope.server = other
        change = scope.to_objectchange(ObjectChangeActionChoices.ACTION_UPDATE)
        change.request_id = self.request_id
        change.user = self.user
        change.save()
        self.assertEqual(branching._affected_servers(self._branch()), {self.server.pk, other.pk})

    def test_no_server_to_push_reads_no_change(self):
        type(self.server).objects.update(sync_enabled=False)
        self._change(Scope.objects.get())
        branch = self._branch()
        branch.get_merged_changes = mock.Mock(side_effect=AssertionError("not needed"))
        self.assertEqual(branching._affected_servers(branch), set())

    def test_watched_content_types_cover_every_watched_label(self):
        labels = {f"{ct.app_label}.{ct.model}" for ct in branching._watched_content_types()}
        self.assertEqual(labels, set(signals.WATCHED))


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class F21ReportedNamesTest(TestCase):
    def setUp(self):
        self.server = windows_server()
        self.fake = FakeDhcpServer()
        TestConnectionJob.runner_factory = staticmethod(lambda server: self.fake)
        self.addCleanup(setattr, TestConnectionJob, "runner_factory", None)

    def _run(self):
        TestConnectionJob(_job(self.server, TestConnectionJob)).run()
        self.server.refresh_from_db()
        return self.server.sync_runs.get()

    def test_names_the_column_cannot_hold_are_dropped(self):
        self.fake.server_names = ["dhcp1", "x" * 300, "bad\x00name", "dhcp1.example.com", "y" * 255]
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(self.server.reported_names, ["dhcp1", "dhcp1.example.com", "y" * 255])

    def test_the_list_is_capped(self):
        self.fake.server_names = [f"name{i}" for i in range(100)]
        self._run()
        self.assertEqual(self.server.reported_names, [f"name{i}" for i in range(jobs.REPORTED_NAMES_LIMIT)])

    def test_only_unusable_names_keep_what_was_known(self):
        type(self.server).objects.filter(pk=self.server.pk).update(reported_names=["known"])
        self.server.refresh_from_db()
        self.fake.server_names = ["x" * 300]
        self.assertEqual(self._run().status, SyncStatusChoices.COMPLETED)
        self.assertEqual(self.server.reported_names, ["known"])


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class F22FinishTest(PushTestMixin, TestCase):
    """A connection that will not close still leaves every kind of run saved with its final status."""

    def setUp(self):
        super().setUp()
        for job_class in (TestConnectionJob, ImportJob):
            job_class.runner_factory = staticmethod(lambda server: self.fake)
            self.addCleanup(setattr, job_class, "runner_factory", None)

        def close():
            raise OSError("transport already gone")

        self.fake.close = close

    def _finished(self, job):
        run = SyncRun.objects.get(job=job)
        self.assertIsNotNone(run.completed)
        return run

    def test_connection_test(self):
        job = _job(self.server, TestConnectionJob)
        with self.assertLogs("netbox.jobs.TestConnectionJob", level="WARNING") as logs:
            TestConnectionJob(job).run()
        self.assertEqual(self._finished(job).status, SyncStatusChoices.COMPLETED)
        self.assertTrue(any("Could not close the connection: OSError" in line for line in logs.output), logs.output)
        self.assertFalse(any("transport already gone" in line for line in logs.output), logs.output)

    def test_push(self):
        job = _job(self.server, PushJob)
        PushJob(job).run()
        self.assertEqual(self._finished(job).status, SyncStatusChoices.COMPLETED)

    def test_failed_run_keeps_its_own_error(self):
        self.fake.fail = lambda op, params: PSError(message="Access is denied", category="PermissionDenied")
        job = _job(self.server, PushJob)
        with self.assertRaises(JobFailed):
            PushJob(job).run()
        run = self._finished(job)
        self.assertEqual(run.status, SyncStatusChoices.FAILED)
        self.assertIn("Access is denied", run.error)

    def test_unexpected_error_is_still_recorded(self):
        job = _job(self.server, PushJob)
        with (
            mock.patch("netbox_windows_dhcp_plugin.sync.pusher.run", side_effect=RuntimeError("bug")),
            self.assertRaises(RuntimeError),
        ):
            PushJob(job).run()
        run = self._finished(job)
        self.assertEqual((run.status, run.error), (SyncStatusChoices.FAILED, "unexpected error, see the job log"))

    def test_import_survives_the_close_after_reading(self):
        # The import closes its connection as soon as the server is read, before it writes anything. A close that
        # fails there must not throw away a server that was read in full.
        job = _job(self.server, ImportJob)
        with (
            mock.patch(f"{BRANCHING}.branching_installed", return_value=False),
            self.assertLogs("netbox.jobs.ImportJob", level="WARNING") as logs,
        ):
            ImportJob(job).run()
        run = self._finished(job)
        self.assertEqual((run.status, run.error), (SyncStatusChoices.COMPLETED, ""))
        self.assertTrue(any("Could not close the connection: OSError" in line for line in logs.output), logs.output)
        self.assertFalse(any("transport already gone" in line for line in logs.output), logs.output)


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class F24BusyPushNotificationTest(PushTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username="audit-g5-merger")

    def _busy(self, notifications):
        job = Job.objects.create(
            object=self.server,
            name=PushJob.name,
            status=JobStatusChoices.STATUS_PENDING,
            job_id=uuid.uuid4(),
            user=self.user,
            notifications=notifications,
        )
        other = connection.copy()
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s, %s)", [_LOCK_CLASS, self.server.pk])
            # Through handle(), as the worker runs it: that is where NetBox notifies.
            PushJob.handle(job)
        finally:
            other.close()
        job.refresh_from_db()
        return job

    def test_waiting_attempt_does_not_notify_and_the_retry_will(self):
        job = self._busy(JobNotificationChoices.NOTIFICATION_ALWAYS)
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertFalse(Notification.objects.filter(user=self.user).exists())
        self.assertFalse(SyncRun.objects.filter(job=job).exists())
        retry = PushJob.get_jobs(self.server).get(status=JobStatusChoices.STATUS_SCHEDULED)
        self.assertEqual(retry.notifications, JobNotificationChoices.NOTIFICATION_ALWAYS)
        self.assertEqual(retry.user, self.user)

    def test_attempt_that_queued_no_retry_still_notifies(self):
        # Another push is already waiting (a save on main, quiet unless it fails), so the busy merge push queues
        # no retry of its own. Nothing carries its setting on: it must keep it and tell the user who merged.
        waiting = Job.objects.create(
            object=self.server,
            name=PushJob.name,
            status=JobStatusChoices.STATUS_SCHEDULED,
            scheduled=timezone.now() + timedelta(seconds=60),
            job_id=uuid.uuid4(),
            notifications=JobNotificationChoices.NOTIFICATION_ON_FAILURE,
        )
        with self.assertLogs("netbox.jobs.PushJob", level="INFO") as logs:
            job = self._busy(JobNotificationChoices.NOTIFICATION_ALWAYS)
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(job.notifications, JobNotificationChoices.NOTIFICATION_ALWAYS)
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 1)
        self.assertTrue(any("another push is already waiting" in line for line in logs.output), logs.output)
        self.assertFalse(any("queued again" in line for line in logs.output), logs.output)
        # The waiting push is left as it was: it belongs to whoever queued it.
        scheduled = PushJob.get_jobs(self.server).filter(status=JobStatusChoices.STATUS_SCHEDULED)
        self.assertEqual(list(scheduled), [waiting])
        waiting.refresh_from_db()
        self.assertEqual(waiting.notifications, JobNotificationChoices.NOTIFICATION_ON_FAILURE)
        self.assertIsNone(waiting.user)

    def test_retry_of_a_quiet_push_stays_quiet(self):
        self._busy(JobNotificationChoices.NOTIFICATION_ON_FAILURE)
        retry = PushJob.get_jobs(self.server).get(status=JobStatusChoices.STATUS_SCHEDULED)
        self.assertEqual(retry.notifications, JobNotificationChoices.NOTIFICATION_ON_FAILURE)
        self.assertFalse(Notification.objects.filter(user=self.user).exists())

    def test_giving_up_still_notifies(self):
        job = Job.objects.create(
            object=self.server,
            name=PushJob.name,
            status=JobStatusChoices.STATUS_PENDING,
            job_id=uuid.uuid4(),
            user=self.user,
            notifications=JobNotificationChoices.NOTIFICATION_ALWAYS,
        )
        other = connection.copy()
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s, %s)", [_LOCK_CLASS, self.server.pk])
            PushJob.handle(job, attempt=jobs.PUSH_ATTEMPTS)
        finally:
            other.close()
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 1)


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class F25BranchImportWithoutUserTest(ImportTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        ImportJob.runner_factory = staticmethod(lambda server: self.fake)
        self.addCleanup(setattr, ImportJob, "runner_factory", None)

    def test_fails_before_the_server_is_contacted(self):
        self.fake.calls.clear()
        job = _job(self.server, ImportJob)
        destinations = [
            ImportDestinationChoices.BRANCH,
            ImportDestinationChoices.CURRENT,
            ImportDestinationChoices.MAIN,
        ]
        with (
            mock.patch(f"{BRANCHING}.import_destinations", return_value=destinations),
            mock.patch("netbox_windows_dhcp_plugin.jobs.server_lock", side_effect=AssertionError("no lock needed")),
            self.assertRaises(JobFailed),
        ):
            ImportJob(job).run(destination=ImportDestinationChoices.BRANCH)
        run = SyncRun.objects.get(job=job)
        self.assertEqual(run.status, SyncStatusChoices.FAILED)
        self.assertEqual(run.error, "imports into a branch need a user: set import_user for scheduled imports")
        self.assertEqual(self.fake.calls, [])


class F26ImportTimeoutTest(TestCase):
    def setUp(self):
        self.server = windows_server()

    def _timeout(self, branching_kwargs):
        with (
            mock.patch("netbox_windows_dhcp_plugin.jobs.branching_job_kwargs", return_value=branching_kwargs),
            mock.patch.object(ImportJob, "enqueue") as enqueue,
        ):
            enqueue_import(self.server, None)
        return enqueue.call_args.kwargs["job_timeout"]

    def test_import_gets_as_long_as_a_push(self):
        self.assertEqual(PUSH_TIMEOUT, 3600)
        self.assertEqual(self._timeout({}), PUSH_TIMEOUT)

    def test_a_longer_branching_timeout_wins(self):
        self.assertEqual(self._timeout({"job_timeout": 7200}), 7200)

    def test_a_shorter_or_missing_branching_timeout_does_not_cut_the_import(self):
        self.assertEqual(self._timeout({"job_timeout": 300}), PUSH_TIMEOUT)
        self.assertEqual(self._timeout({"job_timeout": None}), PUSH_TIMEOUT)

    def test_the_real_job_is_queued_with_it(self):
        job = enqueue_import(self.server, None)
        self.assertIsNotNone(job)
        self.assertEqual(job.name, ImportJob.name)


class F27SchedulerTest(TestCase):
    def setUp(self):
        self.first = windows_server("dhcp1", import_interval=60)
        self.second = windows_server("dhcp2", import_interval=60)

    def _schedule(self, enqueue):
        job = Job.objects.create(
            name=ScheduleImportsJob.name, status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4()
        )
        with (
            mock.patch("netbox_windows_dhcp_plugin.jobs.enqueue_import", side_effect=enqueue) as queued,
            self.assertLogs("netbox.jobs.ScheduleImportsJob", level="INFO") as logs,
        ):
            ScheduleImportsJob(job).run()
        return queued, logs.output

    def test_one_failing_server_does_not_stop_the_others(self):
        def enqueue(server, user):
            if server == self.first:
                raise RuntimeError("queue unavailable")
            return object()

        queued, output = self._schedule(enqueue)
        self.assertEqual([call.args[0] for call in queued.call_args_list], [self.first, self.second])
        self.assertTrue(any("Could not queue an import for dhcp1" in line for line in output), output)
        self.assertTrue(any("Queued an import for dhcp2" in line for line in output), output)

    def test_queued_is_only_logged_when_a_job_was_queued(self):
        _, output = self._schedule(lambda server, user: None if server == self.first else object())
        self.assertFalse(any("Queued an import for dhcp1" in line for line in output), output)
        self.assertTrue(any("Queued an import for dhcp2" in line for line in output), output)

    def test_due_servers_reads_the_newest_import_of_each_server_at_once(self):
        now = timezone.now()
        old = SyncRun.objects.create(windows_server=self.first, kind=SyncKindChoices.IMPORT)
        SyncRun.objects.filter(pk=old.pk).update(created=now - timedelta(hours=3))
        SyncRun.objects.create(windows_server=self.first, kind=SyncKindChoices.IMPORT)
        # Only imports count: a recent push does not postpone the next import.
        stale = SyncRun.objects.create(windows_server=self.second, kind=SyncKindChoices.IMPORT)
        SyncRun.objects.filter(pk=stale.pk).update(created=now - timedelta(hours=3))
        SyncRun.objects.create(windows_server=self.second, kind=SyncKindChoices.PUSH)
        third = windows_server("dhcp3", import_interval=60)
        windows_server("dhcp4", import_interval=0)
        with CaptureQueriesContext(connection) as queries:
            due = list(due_servers())
        self.assertEqual(due, [self.second, third])
        # The busy jobs and the servers, however many servers there are (the content type may add one).
        self.assertLessEqual(len(queries), 3, [q["sql"] for q in queries])


class F38CoreSaveLookupTest(ImportTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.apply_all()
        self.addCleanup(signals._forget_pending)

    def _plugin_queries(self, queries):
        return [q["sql"] for q in queries if PLUGIN_TABLES in q["sql"]]

    def test_creating_core_objects_looks_nothing_up(self):
        from dcim.models import MACAddress
        from ipam.models import IPRange, Prefix
        from netaddr import IPNetwork

        with (
            mock.patch.object(signals, "servers_for", wraps=signals.servers_for) as resolve,
            CaptureQueriesContext(connection) as queries,
        ):
            ip_address("10.99.0.1/24")
            Prefix.objects.create(prefix=IPNetwork("10.99.0.0/24"))
            IPRange.objects.create(start_address=IPNetwork("10.99.0.10/24"), end_address=IPNetwork("10.99.0.20/24"))
            MACAddress.objects.create(mac_address="AA:BB:CC:00:99:01")
        resolve.assert_not_called()
        self.assertEqual(self._plugin_queries(queries), [])

    def test_editing_a_reserved_address_looks_it_up_once_and_pushes(self):
        address = Reservation.objects.get().ip_address
        address.snapshot()
        address.description = "edited"
        with (
            mock.patch.object(signals, "servers_for", wraps=signals.servers_for) as resolve,
            self.captureOnCommitCallbacks(execute=True),
            CaptureQueriesContext(connection) as queries,
        ):
            address.save()
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(len([sql for sql in self._plugin_queries(queries) if RESERVATION_TABLE in sql]), 1)
        self.assertEqual(PushJob.get_jobs(self.server).count(), 1)

    def test_deleting_a_reserved_mac_still_pushes(self):
        mac = Reservation.objects.get().mac_address
        self.assertIsNotNone(mac)
        with self.captureOnCommitCallbacks(execute=True):
            signals._changed(type(mac), mac)
        self.assertEqual(PushJob.get_jobs(self.server).count(), 1)

    def test_plugin_models_still_count_the_server_they_left(self):
        other = windows_server("dhcp7")
        scope = Scope.objects.get()
        scope.snapshot()
        scope.server = other
        with mock.patch.object(signals, "queue_pushes") as pushes, self.captureOnCommitCallbacks(execute=True):
            signals._changed(Scope, scope)
        pushes.assert_called_once()
        self.assertEqual(pushes.call_args.args[0], {self.server.pk, other.pk})

    def test_creating_a_plugin_object_still_pushes(self):
        scope = Scope.objects.get()
        with mock.patch.object(signals, "queue_pushes") as pushes, self.captureOnCommitCallbacks(execute=True):
            signals._changed(Scope, scope, created=True)
        pushes.assert_called_once()
