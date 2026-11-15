"""The push trail (#102): each push records the edits, Sync now clicks and merges that queued it."""

import uuid
from types import SimpleNamespace
from unittest import mock

from core.choices import JobStatusChoices, ObjectChangeActionChoices
from core.models import Job
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from netbox.context_managers import event_tracking
from utilities.testing import TestCase as ViewTestCase

from netbox_windows_dhcp_plugin import signals
from netbox_windows_dhcp_plugin.choices import PushTriggerSourceChoices
from netbox_windows_dhcp_plugin.integrations import branching
from netbox_windows_dhcp_plugin.jobs import _LOCK_CLASS, DryRunJob, PushJob
from netbox_windows_dhcp_plugin.models import PushTrigger, SyncRun
from netbox_windows_dhcp_plugin.models import Scope as NetBoxScope

from .test_import import ImportTestMixin
from .test_jobs import CREDENTIALS
from .test_push import STANDALONE, PushTestMixin, _Changes


def _request(user):
    request = RequestFactory().get("/")
    request.id = uuid.uuid4()
    request.user = user
    return request


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class PushTrailTest(PushTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        users = get_user_model().objects
        self.alice = users.create_user(username="trail-alice", is_superuser=True)
        self.bob = users.create_user(username="trail-bob", is_superuser=True)
        self.addCleanup(signals._forget_pending)

    def edit(self, user, name):
        request = _request(user)
        with (
            STANDALONE,
            mock.patch("netbox.context_managers.flush_events"),
            self.captureOnCommitCallbacks(execute=True),
            event_tracking(request),
        ):
            scope = NetBoxScope.objects.get()
            scope.snapshot()
            scope.name = name
            scope.save()
        return request

    def test_edit_records_trigger(self):
        request = self.edit(self.alice, "renamed")
        run = self._run()
        trigger = run.triggers.get()
        self.assertEqual(trigger.source, PushTriggerSourceChoices.EDIT)
        self.assertEqual(trigger.user, self.alice)
        self.assertEqual(trigger.request_id, request.id)
        self.assertFalse(PushTrigger.objects.filter(sync_run=None).exists())

    def test_debounced_edits_are_merged(self):
        first = self.edit(self.alice, "first")
        second = self.edit(self.bob, "second")
        self.assertEqual(PushJob.get_jobs(self.server).count(), 1)
        run = self._run()
        self.assertEqual(
            set(run.triggers.values_list("user__username", "request_id")),
            {("trail-alice", first.id), ("trail-bob", second.id)},
        )

    def test_edit_after_a_push_started_goes_to_the_next_push(self):
        self.edit(self.alice, "first")
        first = self._run()
        self.edit(self.bob, "second")
        self.assertEqual(first.triggers.count(), 1)
        self.assertEqual(PushTrigger.objects.filter(sync_run=None).get().user, self.bob)

    def test_dry_run_leaves_triggers_to_the_push(self):
        self.edit(self.alice, "renamed")
        self._run(DryRunJob)
        self.assertEqual(PushTrigger.objects.filter(sync_run=None).count(), 1)

    def test_busy_push_leaves_triggers_for_its_retry(self):
        self.edit(self.alice, "renamed")
        job = Job.objects.create(
            object=self.server, name=PushJob.name, status=JobStatusChoices.STATUS_PENDING, job_id=uuid.uuid4()
        )
        other = connection.copy()
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s, %s)", [_LOCK_CLASS, self.server.pk])
            PushJob.handle(job)
        finally:
            other.close()
        self.assertEqual(PushTrigger.objects.filter(sync_run=None).count(), 1)

    def test_triggers_dropped_when_sync_was_turned_off(self):
        # Sync turned off after the edit: the push does nothing, and its triggers must not attach to a push weeks later.
        self.edit(self.alice, "renamed")
        self.server.sync_enabled = False
        self.server.save()
        self._run()
        self.assertFalse(PushTrigger.objects.exists())

    def test_sync_disabled_records_nothing(self):
        self.server.sync_enabled = False
        self.server.save()
        self.edit(self.alice, "renamed")
        self.assertFalse(PushTrigger.objects.exists())


class MergeTrailTest(ImportTestMixin, TestCase):
    def test_merge_records_its_trigger(self):
        self.apply_all()
        scope = NetBoxScope.objects.get()
        scope.snapshot()
        scope.name = "merged"
        branch = SimpleNamespace(
            pk=1,
            name="feature",
            get_merged_changes=lambda: _Changes([scope.to_objectchange(ObjectChangeActionChoices.ACTION_UPDATE)]),
        )
        with self.captureOnCommitCallbacks(execute=True):
            branching._on_merge(None, branch=branch, user=None)
        trigger = PushTrigger.objects.get()
        self.assertEqual((trigger.source, trigger.branch), (PushTriggerSourceChoices.MERGE, "feature"))


class SyncNowTrailTest(ViewTestCase):
    def setUp(self):
        super().setUp()
        from .fixtures import windows_server

        self.server = windows_server()
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_windowsserver",
            "netbox_windows_dhcp_plugin.change_windowsserver",
            "netbox_windows_dhcp_plugin.view_syncrun",
        )

    def test_sync_now_trigger(self):
        self.client.post(f"/plugins/windows-dhcp/windowsservers/{self.server.pk}/push/")
        trigger = PushTrigger.objects.get()
        self.assertEqual(trigger.source, PushTriggerSourceChoices.SYNC_NOW)
        self.assertEqual(trigger.user, self.user)

    def test_dry_run_records_nothing(self):
        self.client.post(f"/plugins/windows-dhcp/windowsservers/{self.server.pk}/dry-run/")
        self.assertFalse(PushTrigger.objects.exists())

    def test_sync_run_page_links_changelog(self):
        run = SyncRun.objects.create(windows_server=self.server, kind="push", status="completed")
        request_id = uuid.uuid4()
        PushTrigger.objects.create(
            windows_server=self.server, sync_run=run, source="edit", user=self.user, request_id=request_id
        )
        response = self.client.get(run.get_absolute_url())
        self.assertContains(response, f"request_id={request_id}")
        self.assertContains(response, self.user.username)

    def test_sync_run_api_has_triggers(self):
        run = SyncRun.objects.create(windows_server=self.server, kind="push", status="completed")
        request_id = uuid.uuid4()
        PushTrigger.objects.create(
            windows_server=self.server, sync_run=run, source="edit", user=self.user, request_id=request_id
        )
        # The browser session is enough for NetBox's REST API.
        response = self.client.get(f"/api/plugins/windows-dhcp/sync-runs/{run.pk}/")
        self.assertEqual(response.status_code, 200)
        (trigger,) = response.json()["triggers"]
        self.assertEqual(trigger["source"], "edit")
        self.assertEqual(trigger["user"], self.user.username)
        self.assertEqual(trigger["request_id"], str(request_id))
