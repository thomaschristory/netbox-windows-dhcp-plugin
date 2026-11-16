"""Sync run retention (issue #106): old runs go, runs the plugin still reads stay."""

import uuid
from datetime import timedelta
from unittest import mock

from core.choices import JobStatusChoices
from core.models import Job
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from extras.models import Bookmark, JournalEntry, Tag

from netbox_windows_dhcp_plugin import import_status, retention
from netbox_windows_dhcp_plugin.choices import (
    ImportDestinationChoices,
    ProposalStatusChoices,
    PushTriggerSourceChoices,
    SyncKindChoices,
    SyncStatusChoices,
)
from netbox_windows_dhcp_plugin.jobs import ABANDONED_JOB, PushJob, ScheduleImportsJob, due_servers
from netbox_windows_dhcp_plugin.models import ImportProposal, PushTrigger, SyncRun
from netbox_windows_dhcp_plugin.sync import pusher

from .fixtures import windows_server

BRANCHING = "netbox_windows_dhcp_plugin.integrations.branching"

PUSH = SyncKindChoices.PUSH
IMPORT = SyncKindChoices.IMPORT
DONE = SyncStatusChoices.COMPLETED


def retention_config(days):
    return {**settings.PLUGINS_CONFIG, "netbox_windows_dhcp_plugin": {"sync_run_retention_days": days}}


class RetentionTestMixin:
    def setUp(self):
        cache.delete(retention.IDLE_CACHE_KEY)
        self.addCleanup(cache.delete, retention.IDLE_CACHE_KEY)
        self.server = windows_server()
        self.now = timezone.now()

    def run_(self, days_ago, kind=PUSH, server=None, status=DONE, **fields):
        """A run created ``days_ago`` days ago (completed at the same time unless given)."""
        when = self.now - timedelta(days=days_ago)
        finished = status not in (SyncStatusChoices.PENDING, SyncStatusChoices.RUNNING)
        fields.setdefault("completed", when if finished else None)
        run = SyncRun.objects.create(windows_server=server or self.server, kind=kind, status=status, **fields)
        SyncRun.objects.filter(pk=run.pk).update(created=when)
        run.refresh_from_db()
        return run

    def prune(self, days=90, **kwargs):
        with override_settings(PLUGINS_CONFIG=retention_config(days)):
            return retention.prune_sync_runs(now=self.now, **kwargs)

    def remaining(self):
        return set(SyncRun.objects.values_list("pk", flat=True))


class RetentionSettingTest(TestCase):
    def test_default_is_90_days(self):
        with override_settings(PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, "netbox_windows_dhcp_plugin": {}}):
            self.assertEqual(retention.retention_days(), 90)

    def test_zero_and_none_keep_every_run(self):
        for value in (0, None):
            with override_settings(PLUGINS_CONFIG=retention_config(value)):
                self.assertIsNone(retention.retention_days())

    def test_anything_but_a_positive_whole_number_is_refused(self):
        for value in (-1, "90", 1.5, True, 36501, 10**6):
            with override_settings(PLUGINS_CONFIG=retention_config(value)), self.assertRaises(ValueError):
                retention.retention_days()

    def test_a_hundred_years_is_the_most(self):
        with override_settings(PLUGINS_CONFIG=retention_config(36500)):
            self.assertEqual(retention.retention_days(), 36500)

    def test_startup_check_warns_about_a_bad_value(self):
        with (
            override_settings(PLUGINS_CONFIG=retention_config(-5)),
            self.assertLogs("netbox_windows_dhcp_plugin.retention", level="WARNING") as logs,
        ):
            retention.check_setting()
        self.assertIn("no sync run is deleted until it is fixed", logs.output[0])

    def test_startup_check_is_quiet_for_a_good_value(self):
        with (
            override_settings(PLUGINS_CONFIG=retention_config(30)),
            self.assertNoLogs("netbox_windows_dhcp_plugin.retention", level="WARNING"),
        ):
            retention.check_setting()


class PruneTest(RetentionTestMixin, TestCase):
    def test_old_runs_go_and_recent_ones_stay(self):
        old = [self.run_(200), self.run_(100)]
        recent = [self.run_(89), self.run_(1)]
        self.assertEqual(self.prune(), 2)
        self.assertEqual(self.remaining(), {run.pk for run in recent})
        self.assertFalse(SyncRun.objects.filter(pk__in=[run.pk for run in old]).exists())

    def test_zero_keeps_every_run(self):
        self.run_(400)
        self.run_(1)
        self.assertEqual(self.prune(days=0), 0)
        self.assertEqual(SyncRun.objects.count(), 2)

    def test_batches_are_capped_and_oldest_first(self):
        runs = [self.run_(days) for days in (300, 200, 250, 150)]
        recent = self.run_(1)
        self.assertEqual(self.prune(batch=2), 2)
        self.assertEqual(self.remaining(), {runs[1].pk, runs[3].pk, recent.pk})
        self.assertEqual(self.prune(batch=2), 2)
        self.assertEqual(self.remaining(), {recent.pk})

    def test_newest_run_of_each_kind_is_kept_however_old(self):
        kept = {}
        for kind in SyncKindChoices.values():
            self.run_(500, kind=kind)
            kept[kind] = self.run_(400, kind=kind)
        other = windows_server("dhcp2")
        other_import = self.run_(300, kind=IMPORT, server=other)
        self.assertEqual(self.prune(), len(kept))
        self.assertEqual(self.remaining(), {run.pk for run in kept.values()} | {other_import.pk})

    def test_import_schedule_still_sees_the_last_import(self):
        self.server.import_interval = 60
        self.server.save()
        self.run_(200, kind=IMPORT)
        last = self.run_(100, kind=IMPORT)
        self.prune(days=1)
        self.assertEqual(self.remaining(), {last.pk})
        # Still timed from that import, as before the prune (it is overdue, so the server is due).
        self.assertEqual(list(due_servers(now=self.now)), [self.server])
        self.assertEqual(list(due_servers(now=self.now - timedelta(days=100) + timedelta(minutes=1))), [])

    def test_newest_push_that_reached_windows_is_kept(self):
        pending = {"replication_pending": {"campus": ["198.18.1.0"]}}
        wrote = self.run_(300, summary=pending)
        self.run_(400, summary={"applied": 1})
        # A newer push that stopped before writing (no summary) is the newest push, yet the replication still to do
        # is read from the one before it.
        stopped = self.run_(200, status=SyncStatusChoices.FAILED, summary={})
        self.prune()
        self.assertEqual(self.remaining(), {wrote.pk, stopped.pk})
        self.assertEqual(pusher._pending_replication(self.server), {"campus": {"198.18.1.0"}})

    def test_replication_run_is_the_newest_completed_not_the_newest_created(self):
        late = self.run_(300, summary={"applied": 1}, completed=self.now - timedelta(days=100))
        self.run_(200, summary={"applied": 2}, completed=self.now - timedelta(days=250))
        newest = self.run_(150, kind=PUSH, status=SyncStatusChoices.FAILED, summary={})
        self.prune()
        self.assertIn(late.pk, self.remaining())
        self.assertIn(newest.pk, self.remaining())

    def test_runs_of_open_proposals_are_kept(self):
        open_run = self.run_(300, kind=IMPORT)
        closed_run = self.run_(250, kind=IMPORT)
        self.run_(1, kind=IMPORT)
        ImportProposal.objects.create(windows_server=self.server, sync_run=open_run)
        closed = ImportProposal.objects.create(
            windows_server=self.server, sync_run=closed_run, status=ProposalStatusChoices.APPLIED
        )
        self.prune()
        self.assertIn(open_run.pk, self.remaining())
        self.assertNotIn(closed_run.pk, self.remaining())
        closed.refresh_from_db()
        self.assertIsNone(closed.sync_run)

    def test_runs_whose_job_is_still_queued_or_running_are_kept(self):
        live = {}
        for status in JobStatusChoices.ENQUEUED_STATE_CHOICES:
            job = Job.objects.create(object=self.server, name=PushJob.name, status=status, job_id=uuid.uuid4())
            live[status] = self.run_(300, status=SyncStatusChoices.RUNNING, job=job)
        ended = Job.objects.create(
            object=self.server, name=PushJob.name, status=JobStatusChoices.STATUS_ERRORED, job_id=uuid.uuid4()
        )
        # Left "running" by a job that ended without finishing it: nothing will ever finish it.
        stranded = self.run_(300, status=SyncStatusChoices.RUNNING, job=ended)
        orphan = self.run_(300, status=SyncStatusChoices.PENDING)
        self.run_(1)
        self.prune()
        for run in live.values():
            self.assertIn(run.pk, self.remaining())
        self.assertNotIn(stranded.pk, self.remaining())
        self.assertNotIn(orphan.pk, self.remaining())

    def test_runs_of_abandoned_jobs_are_not_kept(self):
        # A worker killed mid-job leaves the job "running" for ever: past ABANDONED_JOB nothing will finish its run.
        job = Job.objects.create(
            object=self.server, name=PushJob.name, status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4()
        )
        Job.objects.filter(pk=job.pk).update(created=self.now - ABANDONED_JOB - timedelta(minutes=1))
        abandoned = self.run_(300, status=SyncStatusChoices.RUNNING, job=job)
        self.run_(1)
        self.prune()
        self.assertNotIn(abandoned.pk, self.remaining())

    def test_runs_with_a_journal_entry_are_kept(self):
        noted = self.run_(300)
        plain = self.run_(250)
        self.run_(1)
        JournalEntry.objects.create(assigned_object=noted, comments="Failover outage, see ticket 42")
        self.prune()
        self.assertIn(noted.pk, self.remaining())
        self.assertNotIn(plain.pk, self.remaining())
        self.assertEqual(JournalEntry.objects.count(), 1)

    def test_tags_and_bookmarks_go_with_the_run(self):
        old = self.run_(300)
        self.run_(1)
        tag = Tag.objects.create(name="audit", slug="audit")
        old.tags.add(tag)
        user = get_user_model().objects.create_user(username="reader")
        Bookmark.objects.create(object=old, user=user)
        self.prune()
        self.assertNotIn(old.pk, self.remaining())
        self.assertFalse(Bookmark.objects.exists())
        self.assertTrue(Tag.objects.filter(pk=tag.pk).exists())

    @mock.patch(f"{BRANCHING}.branching_installed", return_value=True)
    def test_the_two_newest_import_branches_keep_their_newest_run(self, _):
        def branch_run(days, name, **fields):
            return self.run_(days, kind=IMPORT, branch_name=name, destination=ImportDestinationChoices.BRANCH, **fields)

        branch_run(500, "dhcp-import/dhcp1/a")
        older_a = branch_run(400, "dhcp-import/dhcp1/a")
        branch_run(350, "dhcp-import/dhcp1/b")
        newest_b = branch_run(300, "dhcp-import/dhcp1/b")
        # A branch the user chose is not an import branch, and does not push the import branches out.
        chosen = self.run_(250, kind=IMPORT, branch_name="mine", destination=ImportDestinationChoices.CURRENT)
        branch_run(600, "dhcp-import/dhcp1/old")
        newest_import = self.run_(200, kind=IMPORT)
        self.prune()
        self.assertEqual(self.remaining(), {older_a.pk, newest_b.pk, newest_import.pk})
        self.assertNotIn(chosen.pk, self.remaining())
        # What the Import panel looks back through still finds the earlier branch.
        runs = SyncRun.objects.filter(windows_server=self.server, kind=IMPORT).order_by("-created")
        earlier = import_status.import_branch_runs(runs).exclude(branch_name="dhcp-import/dhcp1/b").first()
        self.assertEqual(earlier, older_a)

    @mock.patch(f"{BRANCHING}.branching_installed", return_value=False)
    def test_without_branching_branch_runs_are_not_looked_for(self, _):
        # Left by an earlier install with netbox-branching: the panel no longer reads them.
        self.run_(300, kind=IMPORT, branch_name="dhcp-import/dhcp1/a", destination=ImportDestinationChoices.BRANCH)
        newest = self.run_(200, kind=IMPORT)
        self.prune()
        self.assertEqual(self.remaining(), {newest.pk})

    def test_push_triggers_go_with_their_run_and_waiting_ones_stay(self):
        old = self.run_(300)
        self.run_(1)
        PushTrigger.objects.create(windows_server=self.server, sync_run=old, source=PushTriggerSourceChoices.SYNC_NOW)
        waiting = PushTrigger.objects.create(windows_server=self.server, source=PushTriggerSourceChoices.SYNC_NOW)
        self.prune()
        self.assertEqual(list(PushTrigger.objects.all()), [waiting])

    def test_other_servers_runs_do_not_protect_this_ones(self):
        other = windows_server("dhcp2")
        self.run_(1, server=other)
        old = self.run_(300)
        self.run_(1)
        self.prune()
        self.assertNotIn(old.pk, self.remaining())


class SchedulerPruneTest(RetentionTestMixin, TestCase):
    def scheduler(self):
        job = Job.objects.create(
            name=ScheduleImportsJob.name, status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4()
        )
        return ScheduleImportsJob(job)

    def test_scheduler_deletes_old_runs_and_says_so(self):
        self.run_(300)
        self.run_(1)
        with (
            override_settings(PLUGINS_CONFIG=retention_config(90)),
            self.assertLogs("netbox.jobs.ScheduleImportsJob", level="INFO") as logs,
        ):
            self.scheduler().run()
        self.assertEqual(SyncRun.objects.count(), 1)
        self.assertTrue(any("Deleted 1 sync run(s) older than 90 days" in line for line in logs.output), logs.output)

    def test_caught_up_scheduler_waits_an_hour(self):
        self.run_(1)
        with override_settings(PLUGINS_CONFIG=retention_config(90)):
            self.assertEqual(retention.prune_when_due(), 0)
            self.assertTrue(cache.get(retention.IDLE_CACHE_KEY))
            self.run_(300)
            self.assertEqual(retention.prune_when_due(), 0)
            self.assertEqual(SyncRun.objects.count(), 2)
            cache.delete(retention.IDLE_CACHE_KEY)
            self.assertEqual(retention.prune_when_due(), 1)

    def test_failed_tick_waits_15_minutes(self):
        with (
            mock.patch.object(retention, "prune_sync_runs", side_effect=RuntimeError("database gone")),
            mock.patch.object(cache, "set", wraps=cache.set) as stored,
            self.assertRaises(RuntimeError),
        ):
            retention.prune_when_due()
        stored.assert_called_once_with(retention.IDLE_CACHE_KEY, True, retention.FAILURE_SECONDS)
        with mock.patch.object(retention, "prune_sync_runs") as pruned:
            self.assertEqual(retention.prune_when_due(), 0)
        pruned.assert_not_called()

    def test_unexpected_errors_are_logged_with_their_traceback(self):
        with (
            mock.patch.object(retention, "prune_sync_runs", side_effect=RuntimeError("database gone")),
            self.assertLogs("netbox.jobs.ScheduleImportsJob", level="ERROR") as logs,
        ):
            self.scheduler().run()
        self.assertIn("Could not delete old sync runs: database gone", logs.output[0])
        self.assertIsNotNone(logs.records[0].exc_info)

    def test_a_bad_setting_is_logged_without_a_traceback(self):
        with (
            override_settings(PLUGINS_CONFIG=retention_config(-1)),
            self.assertLogs("netbox.jobs.ScheduleImportsJob", level="ERROR") as logs,
        ):
            self.scheduler().run()
        self.assertIn("from 1 to 36500", logs.output[0])
        self.assertIsNone(logs.records[0].exc_info)

    def test_unreadable_cache_still_prunes(self):
        self.run_(300)
        self.run_(1)
        with (
            override_settings(PLUGINS_CONFIG=retention_config(90)),
            mock.patch.object(cache, "get", side_effect=ConnectionError("redis down")),
            mock.patch.object(cache, "set", side_effect=ConnectionError("redis down")),
            self.assertLogs("netbox_windows_dhcp_plugin.retention", level="WARNING") as logs,
        ):
            self.assertEqual(retention.prune_when_due(), 1)
        self.assertEqual(len(logs.output), 2, logs.output)

    def test_unwritable_cache_does_not_hide_a_failure(self):
        with (
            mock.patch.object(retention, "prune_sync_runs", side_effect=RuntimeError("database gone")),
            mock.patch.object(cache, "set", side_effect=ConnectionError("redis down")),
            self.assertLogs("netbox_windows_dhcp_plugin.retention", level="WARNING"),
            self.assertRaisesMessage(RuntimeError, "database gone"),
        ):
            retention.prune_when_due()

    def test_full_batch_looks_again_next_minute(self):
        with mock.patch.object(retention, "prune_sync_runs", return_value=retention.PRUNE_BATCH):
            retention.prune_when_due()
        self.assertFalse(cache.get(retention.IDLE_CACHE_KEY))

    def test_failing_prune_does_not_fail_the_scheduler(self):
        self.server.import_interval = 60
        self.server.save()
        with (
            override_settings(PLUGINS_CONFIG=retention_config("ninety")),
            mock.patch("netbox_windows_dhcp_plugin.jobs.enqueue_import", return_value=object()) as queued,
            self.assertLogs("netbox.jobs.ScheduleImportsJob", level="INFO") as logs,
        ):
            self.scheduler().run()
        self.assertEqual([call.args[0] for call in queued.call_args_list], [self.server])
        self.assertTrue(any("Queued an import for dhcp1" in line for line in logs.output), logs.output)
        self.assertTrue(any("Could not delete old sync runs" in line for line in logs.output), logs.output)
