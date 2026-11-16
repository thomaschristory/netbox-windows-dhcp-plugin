"""Large reviews in the background (#101): over review_job_threshold, apply and dismiss run as a job of the proposal."""

import uuid
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from core.choices import JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job, ObjectChange
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from utilities.testing import APITestCase, TestCase

from netbox_windows_dhcp_plugin import jobs, review
from netbox_windows_dhcp_plugin.choices import ProposalItemStatusChoices, ProposalStatusChoices
from netbox_windows_dhcp_plugin.jobs import ReviewJob
from netbox_windows_dhcp_plugin.models import ImportProposalItem
from netbox_windows_dhcp_plugin.models import Scope as NetBoxScope
from netbox_windows_dhcp_plugin.sync import importer, windows_state
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import Scope
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .fixtures import PluginAPIMixin
from .test_review import ITEM, PROPOSAL, SCOPE_PERMISSIONS, make_proposal

DEFAULT_CONFIG = settings.PLUGINS_CONFIG
# Every review goes to a job: the fake server's proposal holds only a handful of items.
IN_BACKGROUND = {
    **settings.PLUGINS_CONFIG,
    "netbox_windows_dhcp_plugin": {
        **settings.PLUGINS_CONFIG.get("netbox_windows_dhcp_plugin", {}),
        "review_job_threshold": 0,
    },
}
VIEW_PROPOSAL = "netbox_windows_dhcp_plugin.view_importproposal"
VIEW_JOB = "core.view_job"


def review_jobs(proposal):
    return Job.objects.filter(name=ReviewJob.name, object_id=proposal.pk)


class _ReviewJobMixin:
    def setUp(self):
        super().setUp()
        self.server, self.proposal = make_proposal()
        self.item = self.proposal.items.get(kind="scope")

    def _queue(self, dismiss=False, whole_proposal=True, ids=None):
        """Queue a review of the scope item and return the job and the arguments the worker would receive."""
        with patch.object(ReviewJob, "enqueue", wraps=ReviewJob.enqueue) as enqueue:
            (job,) = jobs.enqueue_review(
                self.user, {self.proposal: ids or [self.item.pk]}, dismiss=dismiss, whole_proposal=whole_proposal
            )
        kwargs = {k: v for k, v in enqueue.call_args.kwargs.items() if k in ("item_ids", "dismiss", "whole_proposal")}
        return job, kwargs

    def _second_item(self):
        return ImportProposalItem.objects.create(
            proposal=self.proposal,
            sequence=self.item.sequence + 1,
            action="create",
            kind="scope",
            key="10.2.0.0",
            after={**self.item.after, "start_range": "10.2.0.10", "end_range": "10.2.0.200"},
        )

    def _run(self, job, **kwargs):
        # Loaded again, as the worker does: the user's permissions are read afresh.
        job = Job.objects.get(pk=job.pk)
        job.status = JobStatusChoices.STATUS_RUNNING
        job.save()
        self.ran = job
        ReviewJob(job).run(**kwargs)
        return job


@override_settings(PLUGINS_CONFIG=IN_BACKGROUND)
class ReviewJobTest(_ReviewJobMixin, TestCase):
    def test_applies_as_the_user_with_the_job_as_request(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        job, kwargs = self._queue()
        self.assertEqual(kwargs, {"item_ids": [self.item.pk], "dismiss": False, "whole_proposal": True})
        self.assertEqual(job.data, {"action": "apply", "items": 1})
        job = self._run(job, **kwargs)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.APPLIED, self.item.error)
        scope = NetBoxScope.objects.get()
        change = ObjectChange.objects.get(changed_object_id=scope.pk, changed_object_type__model="scope")
        self.assertEqual(change.user, self.user)
        self.assertEqual(change.request_id, job.job_id)
        self.assertEqual(
            job.data,
            {
                "action": "apply",
                "items": 1,
                "superseded": 0,
                "applied": 1,
                "failed": 0,
                "missing_permissions": [],
                "remaining": 0,
            },
        )

    def test_permissions_are_checked_again_when_it_runs(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        job, kwargs = self._queue(whole_proposal=False)
        # The scope permission is taken away while the job waits.
        self.remove_permissions("netbox_windows_dhcp_plugin.add_scope")
        job = self._run(job, **kwargs)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.PENDING)
        self.assertFalse(NetBoxScope.objects.exists())
        self.assertEqual(job.data["missing_permissions"], ["netbox_windows_dhcp_plugin.add_scope"])
        self.assertEqual(job.data["remaining"], 1)

    def test_apply_all_needs_change_on_the_proposal_when_it_runs(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        job, kwargs = self._queue()
        self.remove_permissions(PROPOSAL)
        with self.assertRaises(JobFailed):
            self._run(job, **kwargs)
        self.assertIn("may no longer view and change", self.ran.data["error"])
        self.assertFalse(NetBoxScope.objects.exists())

    def test_inactive_user_applies_nothing(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        job, kwargs = self._queue()
        self.user.is_active = False
        self.user.save()
        with self.assertRaises(JobFailed):
            self._run(job, **kwargs)
        self.assertFalse(NetBoxScope.objects.exists())

    def test_only_the_items_picked_when_queued(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        job, kwargs = self._queue()
        # A new import proposes something after the user clicked: it waits for its own review.
        later = ImportProposalItem.objects.create(
            proposal=self.proposal,
            sequence=self.item.sequence + 1,
            action="create",
            kind="scope",
            key="10.2.0.0",
            after={**self.item.after, "start_range": "10.2.0.10", "end_range": "10.2.0.200"},
        )
        self._run(job, **kwargs)
        later.refresh_from_db()
        self.assertEqual(later.status, ProposalItemStatusChoices.PENDING)
        self.assertEqual(NetBoxScope.objects.count(), 1)

    def test_busy_server_fails_and_applies_nothing(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        job, kwargs = self._queue()

        @contextmanager
        def busy(server, exc=jobs.ServerBusy):
            raise exc(f"another import or push for {server} is running")
            yield

        with patch.object(jobs, "server_lock", busy), self.assertRaises(JobFailed):
            self._run(job, **kwargs)
        self.assertIn("try again", self.ran.data["error"])
        self.assertFalse(NetBoxScope.objects.exists())

    def test_takes_the_server_lock(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        job, kwargs = self._queue()
        with patch.object(jobs, "server_lock", wraps=jobs.server_lock) as lock:
            self._run(job, **kwargs)
        self.assertEqual(lock.call_args.args[0], self.server)

    def test_dismiss(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM)
        job, kwargs = self._queue(dismiss=True)
        job = self._run(job, **kwargs)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.DISMISSED)
        self.assertEqual(job.data, {"action": "dismiss", "items": 1, "superseded": 0, "dismissed": 1, "remaining": 0})
        change = ObjectChange.objects.get(
            changed_object_id=self.item.pk, changed_object_type__model="importproposalitem"
        )
        self.assertEqual((change.user, change.request_id), (self.user, job.job_id))

    def test_a_second_review_of_the_proposal_is_refused(self):
        job, _ = self._queue()
        with self.assertRaises(jobs.ReviewBusy) as busy:
            self._queue()
        self.assertEqual(busy.exception.job, job)
        self.assertEqual(review_jobs(self.proposal).count(), 1)
        # Finished jobs no longer count.
        job.status = JobStatusChoices.STATUS_COMPLETED
        job.save()
        self._queue()
        self.assertEqual(review_jobs(self.proposal).count(), 2)


@override_settings(PLUGINS_CONFIG=IN_BACKGROUND)
class ReviewJobViewTest(_ReviewJobMixin, TestCase):
    def test_apply_all_queues_a_job_and_links_it(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        response = self.client.post(f"/plugins/windows-dhcp/importproposals/{self.proposal.pk}/apply/", follow=True)
        job = review_jobs(self.proposal).get()
        self.assertEqual(job.user, self.user)
        self.assertContains(response, job.get_absolute_url())
        self.assertFalse(NetBoxScope.objects.exists())
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ProposalStatusChoices.OPEN)

    def test_bulk_dismiss_queues_a_job(self):
        self.add_permissions(ITEM)
        self.client.post("/plugins/windows-dhcp/importproposalitems/dismiss/", {"pk": [self.item.pk]})
        self.assertEqual(review_jobs(self.proposal).get().data, {"action": "dismiss", "items": 1})
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.PENDING)

    def test_second_click_says_a_job_is_running(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        url = f"/plugins/windows-dhcp/importproposals/{self.proposal.pk}/apply/"
        self.client.post(url)
        response = self.client.post(url, follow=True)
        self.assertContains(response, "already queued or running")
        self.assertEqual(review_jobs(self.proposal).count(), 1)

    def test_proposal_page_shows_the_review_job(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, VIEW_JOB, *SCOPE_PERMISSIONS)
        response = self.client.get(self.proposal.get_absolute_url())
        self.assertNotContains(response, "Background review")
        job, kwargs = self._queue()
        response = self.client.get(self.proposal.get_absolute_url())
        self.assertContains(response, "Background review")
        self.assertContains(response, job.get_absolute_url())
        self.assertContains(response, "reload the page")
        job = self._run(job, **kwargs)
        job.status = JobStatusChoices.STATUS_COMPLETED
        job.save()
        response = self.client.get(self.proposal.get_absolute_url())
        self.assertNotContains(response, "reload the page")
        self.assertContains(response, "Applied")

    def test_below_the_threshold_nothing_is_queued(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        with override_settings(PLUGINS_CONFIG=DEFAULT_CONFIG):
            self.client.post(f"/plugins/windows-dhcp/importproposals/{self.proposal.pk}/apply/")
        self.assertFalse(review_jobs(self.proposal).exists())
        self.assertTrue(NetBoxScope.objects.exists())


@override_settings(PLUGINS_CONFIG=IN_BACKGROUND)
class ReviewJobAPITest(PluginAPIMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.server, self.proposal = make_proposal()
        self.item = self.proposal.items.get(kind="scope")

    def test_apply_all_answers_202_with_the_job(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        response = self.client.post(
            f"/api/plugins/windows-dhcp/import-proposals/{self.proposal.pk}/apply/", format="json", **self.header
        )
        self.assertEqual(response.status_code, 202, response.data)
        job = review_jobs(self.proposal).get()
        self.assertEqual(response.data["id"], job.pk)
        self.assertTrue(response.data["url"].endswith(f"/api/core/jobs/{job.pk}/"))
        self.assertFalse(NetBoxScope.objects.exists())
        # A second request while it waits: 409 naming it.
        response = self.client.post(
            f"/api/plugins/windows-dhcp/import-proposals/{self.proposal.pk}/apply/", format="json", **self.header
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["job"], job.pk)

    def test_items_answer_202_with_a_list_of_jobs(self):
        self.add_permissions(ITEM)
        response = self.client.post(
            "/api/plugins/windows-dhcp/import-proposal-items/dismiss/",
            {"ids": [self.item.pk]},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual([j["id"] for j in response.data["jobs"]], [review_jobs(self.proposal).get().pk])
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.PENDING)


@override_settings(PLUGINS_CONFIG=IN_BACKGROUND)
class ReviewJobReviewFindingsTest(_ReviewJobMixin, TestCase):
    """Fixes from the adversarial review of #124."""

    def _reimport(self):
        # The same Windows server read again: the import replaces every open item of the proposal with a new one.
        fake = FakeDhcpServer()
        DhcpClient(fake).add_scope(
            Scope(name="LAN", start_range="10.1.0.10", end_range="10.1.0.200", subnet_mask="255.255.255.0")
        )
        importer.import_to_proposal(self.server, windows_state.read(DhcpClient(fake)), None)

    def test_an_import_between_queuing_and_start_fails_the_job(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        job, kwargs = self._queue()
        self._reimport()
        self.assertFalse(ImportProposalItem.objects.filter(pk=self.item.pk).exists())
        with self.assertRaises(JobFailed):
            self._run(job, **kwargs)
        self.assertEqual(self.ran.data["superseded"], 1)
        self.assertIn("replaced by a newer import", self.ran.data["error"])
        self.assertFalse(NetBoxScope.objects.exists())
        # The new item waits for a new review.
        self.assertTrue(self.proposal.items.filter(kind="scope", status=ProposalItemStatusChoices.PENDING).exists())

    def test_items_decided_meanwhile_are_counted_as_superseded(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        second = self._second_item()
        job, kwargs = self._queue(ids=[self.item.pk, second.pk])
        importer.dismiss_items([second])
        job = self._run(job, **kwargs)
        self.assertEqual((job.data["superseded"], job.data["applied"], job.data["remaining"]), (1, 1, 0))

    def test_a_job_that_dies_halfway_keeps_its_counts_and_sends_committed_events(self):
        from netbox_windows_dhcp_plugin.sync.mapping import Writer

        from .test_review import created_prefixes

        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        second = self._second_item()
        job, kwargs = self._queue(ids=[self.item.pk, second.pk])
        real_apply = Writer.apply
        calls = []

        def apply(writer, change):
            calls.append(change)
            if len(calls) > 1:
                raise RuntimeError("worker killed")
            return real_apply(writer, change)

        with (
            patch.object(Writer, "apply", apply),
            patch("netbox.context_managers.flush_events") as flush,
        ):
            ReviewJob.handle(Job.objects.get(pk=job.pk), **kwargs)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_ERRORED)
        self.assertIn("worker killed", job.error)
        self.assertEqual(
            {k: job.data[k] for k in ("interrupted", "applied", "remaining", "superseded")},
            {"interrupted": True, "applied": 1, "remaining": 1, "superseded": 0},
        )
        self.assertEqual(created_prefixes(flush), ["10.1.0.0/24"])

    def test_threshold_must_be_a_whole_number(self):
        for value, expected in ((None, None), (0, 0), (5, 5), (-1, None), ("100", None), (True, None), (2.5, None)):
            config = {**IN_BACKGROUND, "netbox_windows_dhcp_plugin": {"review_job_threshold": value}}
            with self.subTest(value=value), override_settings(PLUGINS_CONFIG=config):
                self.assertEqual(review.review_job_threshold(), expected)
                # Never an error page: a bad value keeps reviews in the request.
                self.assertEqual(
                    review.in_background(ImportProposalItem.objects.filter(pk=self.item.pk)), expected == 0
                )

    def test_apply_all_needs_view_on_the_proposal_when_it_runs(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        job, kwargs = self._queue()
        self.remove_permissions(VIEW_PROPOSAL)
        with self.assertRaises(JobFailed):
            self._run(job, **kwargs)
        self.assertFalse(NetBoxScope.objects.exists())

    def test_panel_hides_the_job_from_users_who_may_not_view_it(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM)
        other = get_user_model().objects.create_user(username="other-reviewer")
        (job,) = jobs.enqueue_review(other, {self.proposal: [self.item.pk]}, whole_proposal=True)
        Job.objects.filter(pk=job.pk).update(
            status=JobStatusChoices.STATUS_ERRORED, error="RuntimeError('secret detail')"
        )
        response = self.client.get(self.proposal.get_absolute_url())
        self.assertContains(response, "Background review")
        self.assertNotContains(response, job.get_absolute_url())
        self.assertNotContains(response, "secret detail")
        self.assertContains(response, "The job did not finish")
        self.add_permissions(VIEW_JOB)
        response = self.client.get(self.proposal.get_absolute_url())
        self.assertContains(response, job.get_absolute_url())
        # Errored by NetBox (a crash): the reason is on the job, not in its data.
        self.assertContains(response, "secret detail")

    def test_an_abandoned_job_stops_spinning_and_blocking(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM)
        job, _ = self._queue()
        Job.objects.filter(pk=job.pk).update(created=timezone.now() - jobs.ABANDONED_JOB - timedelta(minutes=1))
        response = self.client.get(self.proposal.get_absolute_url())
        self.assertNotContains(response, "reload the page")
        self.assertContains(response, "never finished")
        self._queue()
        self.assertEqual(review_jobs(self.proposal).count(), 2)

    def test_busy_warning_names_only_a_job_the_user_may_see(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        other = get_user_model().objects.create_user(username="other-reviewer")
        (theirs,) = jobs.enqueue_review(other, {self.proposal: [self.item.pk]}, whole_proposal=True)
        url = f"/plugins/windows-dhcp/importproposals/{self.proposal.pk}/apply/"
        response = self.client.post(url, follow=True)
        self.assertContains(response, "already queued or running")
        self.assertNotContains(response, theirs.get_absolute_url())


@override_settings(PLUGINS_CONFIG=IN_BACKGROUND)
class ReviewJobAPIFindingsTest(PluginAPIMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.server, self.proposal = make_proposal()
        self.item = self.proposal.items.get(kind="scope")

    def test_409_hides_another_users_job(self):
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        other = get_user_model().objects.create_user(username="other-reviewer")
        jobs.enqueue_review(other, {self.proposal: [self.item.pk]}, whole_proposal=True)
        response = self.client.post(
            f"/api/plugins/windows-dhcp/import-proposals/{self.proposal.pk}/apply/", format="json", **self.header
        )
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(response.data["job"])

    def test_proposal_shows_its_newest_review_job(self):
        self.add_permissions(VIEW_PROPOSAL)
        url = f"/api/plugins/windows-dhcp/import-proposals/{self.proposal.pk}/"
        self.assertIsNone(self.client.get(url, **self.header).data["review_job"])
        (job,) = jobs.enqueue_review(self.user, {self.proposal: [self.item.pk]}, whole_proposal=True)
        review_job = self.client.get(url, **self.header).data["review_job"]
        self.assertEqual((review_job["status"], review_job["data"]), (job.status, {"action": "apply", "items": 1}))
        self.assertNotIn("id", review_job)
        listed = self.client.get("/api/plugins/windows-dhcp/import-proposals/", **self.header).data["results"]
        self.assertEqual(listed[0]["review_job"]["status"], job.status)


@override_settings(PLUGINS_CONFIG=IN_BACKGROUND)
class ReviewJobFinalRoundTest(_ReviewJobMixin, TestCase):
    """Fixes from the check of the #124 review fixes."""

    def _dying(self, after=1):
        """Writer.apply that works ``after`` times, then fails as a killed worker would."""
        from netbox_windows_dhcp_plugin.sync.mapping import Writer

        real_apply = Writer.apply
        calls = []

        def apply(writer, change):
            calls.append(change)
            if len(calls) > after:
                raise RuntimeError("worker killed")
            return real_apply(writer, change)

        return patch.object(Writer, "apply", apply)

    def test_a_rolled_back_status_write_leaves_no_event(self):
        from django.db import IntegrityError
        from django.test import RequestFactory
        from netbox.context import events_queue
        from netbox.context_managers import event_tracking

        request = RequestFactory().get("/")
        request.id = uuid.uuid4()
        request.user = self.user
        real_save = ImportProposalItem.save

        def save(item, *args, **kwargs):
            real_save(item, *args, **kwargs)
            raise IntegrityError("lost the connection after the save")

        with (
            patch("netbox.context_managers.flush_events"),
            event_tracking(request),
            patch.object(ImportProposalItem, "save", save),
        ):
            with self.assertRaises(IntegrityError):
                importer._set_item(self.item, ProposalItemStatusChoices.DISMISSED)
            self.assertEqual(events_queue.get(), {})

    def test_counts_survive_a_database_that_is_gone(self):
        from django.db import OperationalError

        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        second = self._second_item()
        job, kwargs = self._queue(ids=[self.item.pk, second.pk])
        with (
            self._dying(),
            patch.object(ReviewJob, "_progress", side_effect=OperationalError("server closed the connection")),
            patch("netbox.context_managers.flush_events"),
        ):
            ReviewJob.handle(Job.objects.get(pk=job.pk), **kwargs)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_ERRORED)
        # The original error, not the one from counting.
        self.assertIn("worker killed", job.error)
        self.assertTrue(job.data["interrupted"])
        self.assertNotIn("applied", job.data)

    def test_items_decided_during_the_run_add_up(self):
        from netbox_windows_dhcp_plugin.sync.mapping import Writer

        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        second = self._second_item()
        job, kwargs = self._queue(ids=[self.item.pk, second.pk])
        real_apply = Writer.apply

        def apply(writer, change):
            # Another reviewer dismisses the second item while the job applies the first.
            importer.dismiss_items([ImportProposalItem.objects.get(pk=second.pk)])
            return real_apply(writer, change)

        with patch.object(Writer, "apply", apply):
            job = self._run(job, **kwargs)
        counts = {k: job.data[k] for k in ("items", "superseded", "applied", "remaining")}
        self.assertEqual(counts, {"items": 2, "superseded": 1, "applied": 1, "remaining": 0})


@override_settings(PLUGINS_CONFIG=IN_BACKGROUND)
class ReviewJobErrorVisibilityAPITest(PluginAPIMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.server, self.proposal = make_proposal()
        self.item = self.proposal.items.get(kind="scope")

    def test_review_job_error_only_for_the_requester_or_viewers(self):
        self.add_permissions(VIEW_PROPOSAL)
        other = get_user_model().objects.create_user(username="other-reviewer")
        (job,) = jobs.enqueue_review(other, {self.proposal: [self.item.pk]}, whole_proposal=True)
        Job.objects.filter(pk=job.pk).update(
            status=JobStatusChoices.STATUS_FAILED,
            data={"action": "apply", "items": 1, "error": "other-reviewer may not"},
        )
        url = f"/api/plugins/windows-dhcp/import-proposals/{self.proposal.pk}/"
        self.assertEqual(
            self.client.get(url, **self.header).data["review_job"]["data"], {"action": "apply", "items": 1}
        )
        self.add_permissions(VIEW_JOB)
        self.assertEqual(
            self.client.get(url, **self.header).data["review_job"]["data"]["error"], "other-reviewer may not"
        )
