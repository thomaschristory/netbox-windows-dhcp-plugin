"""Large reviews in the background (#101): over review_job_threshold, apply and dismiss run as a job of the proposal."""

from contextlib import contextmanager
from unittest.mock import patch

from core.choices import JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job, ObjectChange
from django.conf import settings
from django.test import override_settings
from utilities.testing import APITestCase, TestCase

from netbox_windows_dhcp_plugin import jobs
from netbox_windows_dhcp_plugin.choices import ProposalItemStatusChoices, ProposalStatusChoices
from netbox_windows_dhcp_plugin.jobs import ReviewJob
from netbox_windows_dhcp_plugin.models import ImportProposalItem
from netbox_windows_dhcp_plugin.models import Scope as NetBoxScope

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


def review_jobs(proposal):
    return Job.objects.filter(name=ReviewJob.name, object_id=proposal.pk)


class _ReviewJobMixin:
    def setUp(self):
        super().setUp()
        self.server, self.proposal = make_proposal()
        self.item = self.proposal.items.get(kind="scope")

    def _queue(self, dismiss=False, whole_proposal=True):
        """Queue a review of the scope item and return the job and the arguments the worker would receive."""
        with patch.object(ReviewJob, "enqueue", wraps=ReviewJob.enqueue) as enqueue:
            (job,) = jobs.enqueue_review(
                self.user, {self.proposal: [self.item.pk]}, dismiss=dismiss, whole_proposal=whole_proposal
            )
        kwargs = {k: v for k, v in enqueue.call_args.kwargs.items() if k in ("item_ids", "dismiss", "whole_proposal")}
        return job, kwargs

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
        self.add_permissions(PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
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
            {"action": "apply", "items": 1, "applied": 1, "failed": 0, "missing_permissions": [], "remaining": 0},
        )

    def test_permissions_are_checked_again_when_it_runs(self):
        self.add_permissions(PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
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
        self.add_permissions(PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        job, kwargs = self._queue()
        self.remove_permissions(PROPOSAL)
        with self.assertRaises(JobFailed):
            self._run(job, **kwargs)
        self.assertIn("may no longer change", self.ran.data["error"])
        self.assertFalse(NetBoxScope.objects.exists())

    def test_inactive_user_applies_nothing(self):
        self.add_permissions(PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        job, kwargs = self._queue()
        self.user.is_active = False
        self.user.save()
        with self.assertRaises(JobFailed):
            self._run(job, **kwargs)
        self.assertFalse(NetBoxScope.objects.exists())

    def test_only_the_items_picked_when_queued(self):
        self.add_permissions(PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
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
        self.add_permissions(PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
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
        self.add_permissions(PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
        job, kwargs = self._queue()
        with patch.object(jobs, "server_lock", wraps=jobs.server_lock) as lock:
            self._run(job, **kwargs)
        self.assertEqual(lock.call_args.args[0], self.server)

    def test_dismiss(self):
        self.add_permissions(PROPOSAL, ITEM)
        job, kwargs = self._queue(dismiss=True)
        job = self._run(job, **kwargs)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.DISMISSED)
        self.assertEqual(job.data, {"action": "dismiss", "items": 1, "dismissed": 1, "remaining": 0})
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
        self.add_permissions(VIEW_PROPOSAL, PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
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
        self.add_permissions(PROPOSAL, ITEM, *SCOPE_PERMISSIONS)
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
