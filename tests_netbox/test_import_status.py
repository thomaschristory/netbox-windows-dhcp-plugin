"""
The Import panel on the Windows server and sync run pages (issue #32).

With branching an import goes into an import branch, so the page must say which one, link to it and its change
request, and say main has not changed; a queued or running import must not look finished. Without branching the
panel points at the import proposal and never mentions a branch.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock, skipUnless

from core.choices import JobStatusChoices
from core.models import Job
from django.apps import apps
from django.urls import reverse
from django.utils import timezone
from utilities.testing import TestCase

from netbox_windows_dhcp_plugin import import_status
from netbox_windows_dhcp_plugin.choices import ProposalStatusChoices, SyncKindChoices, SyncStatusChoices
from netbox_windows_dhcp_plugin.jobs import ImportJob
from netbox_windows_dhcp_plugin.models import ImportProposal, SyncRun

from .fixtures import dhcp_servers, windows_server

BRANCHING = "netbox_windows_dhcp_plugin.integrations.branching.branching_installed"
IMPORT_BRANCH = "netbox_windows_dhcp_plugin.integrations.branching.import_branch"
BRANCH_DELETION = "netbox_windows_dhcp_plugin.integrations.branching.branch_deletion"
MERGED_AT = datetime(2026, 10, 23, 12, 26, 23, tzinfo=UTC)
# How the panel words a known merge time (NetBox wraps the date in a tooltip).
MERGED_ON = "The branch was merged into main on <span"


def fake_branch(status="ready", merged_time=None):
    return SimpleNamespace(
        status=status,
        merged_time=merged_time,
        get_absolute_url=lambda: "/plugins/branching/branches/7/",
        get_status_color=lambda: "green",
        get_status_display=lambda: status.title(),
    )


class FakeChangeRequest:
    def get_absolute_url(self):
        return "/plugins/changes/change-requests/3/"

    def __str__(self):
        return "Import dhcp1"


def panel(html):
    """The Import panel's own HTML, up to the next card."""
    return html.split('id="windows-import-status"', 1)[1].split('<div class="card"', 1)[0]


class ImportStatusTestBase(TestCase):
    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        (dhcp,) = dhcp_servers(1)
        self.server = windows_server(dhcp)

    def job(self, status):
        return Job.objects.create(object=self.server, name=ImportJob.name, status=status, job_id=uuid.uuid4())

    def make_run(
        self,
        job=None,
        status=SyncStatusChoices.COMPLETED,
        branch_name="",
        changes=2,
        kind=SyncKindChoices.IMPORT,
        created=False,
        **extra,
    ):
        summary = {"changes": changes, **extra}
        if branch_name:
            summary["branch_created"] = created
        return SyncRun.objects.create(
            windows_server=self.server, kind=kind, status=status, job=job, branch_name=branch_name, summary=summary
        )

    def age(self, obj, hours):
        type(obj).objects.filter(pk=obj.pk).update(created=timezone.now() - timedelta(hours=hours))
        obj.refresh_from_db()

    def server_page(self):
        response = self.client.get(self.server.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class StandaloneImportStatusTest(ImportStatusTestBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch(BRANCHING, return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_import_no_panel(self):
        self.assertIsNone(import_status.for_server(self.server, self.user))
        self.assertNotIn('id="windows-import-status"', self.server_page())

    def test_queued_import_is_not_shown_as_done(self):
        job = self.job(JobStatusChoices.STATUS_PENDING)
        status = import_status.for_server(self.server, self.user)
        self.assertEqual((status.state, status.next_state, status.next_job), (None, import_status.QUEUED, job))
        self.assertTrue(status.pending)
        html = self.server_page()
        self.assertIn(f"Import queued (job #{job.pk}). Nothing has been imported yet.", html)
        url = reverse("plugins:netbox_windows_dhcp_plugin:windowsserver_import_status", args=[self.server.pk])
        self.assertIn(f'hx-get="{url}"', html)
        self.assertNotIn("branch", panel(html).lower())

    def test_running_run(self):
        job = self.job(JobStatusChoices.STATUS_RUNNING)
        self.make_run(job, status=SyncStatusChoices.RUNNING)
        status = import_status.for_server(self.server, self.user)
        self.assertEqual((status.state, status.next_state), (import_status.RUNNING, None))
        self.assertIn("Import running. Nothing is final until it has finished.", self.server_page())

    def test_finished_import_links_the_proposal_and_never_a_branch(self):
        job = self.job(JobStatusChoices.STATUS_COMPLETED)
        run = self.make_run(job)
        proposal = ImportProposal.objects.create(windows_server=self.server, sync_run=run)
        status = import_status.for_server(self.server, self.user)
        self.assertEqual((status.state, status.proposal, status.pending), (import_status.COMPLETED, proposal, False))
        html = self.server_page()
        self.assertIn("2 differences wait for review in an import proposal.", html)
        self.assertIn(proposal.get_absolute_url(), html)
        self.assertNotIn("Main has not been changed", html)
        self.assertNotIn("hx-get", panel(html).split(">", 1)[0])
        self.assertNotIn("branch", panel(html).lower())

    def test_an_applied_proposal_no_longer_waits_for_review(self):
        run = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED))
        ImportProposal.objects.create(windows_server=self.server, sync_run=run, status=ProposalStatusChoices.APPLIED)
        html = self.server_page()
        self.assertIn("2 differences were sent for review in an import proposal, now closed.", html)
        self.assertNotIn("wait for review", html)

    def test_a_newer_queued_job_keeps_the_previous_result(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=0)
        queued = self.job(JobStatusChoices.STATUS_PENDING)
        status = import_status.for_server(self.server, self.user)
        self.assertEqual((status.state, status.next_job), (import_status.COMPLETED, queued))
        html = self.server_page()
        self.assertIn("Import queued", html)
        self.assertIn("Previous import", html)
        self.assertIn("No differences found.", html)

    def test_dismissed_differences_are_not_called_no_differences(self):
        # Issue #43: the repeat import found a difference a reviewer had dismissed, so it opened no proposal.
        run = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=1, update=1, review=0, dismissed=1)
        for html in (self.server_page(), self.client.get(run.get_absolute_url()).content.decode()):
            text = panel(html)
            self.assertNotIn("No differences found", text)
            self.assertIn("No new differences waiting for review.", text)
            self.assertIn("1 difference found earlier was dismissed", text)
            self.assertNotIn("branch", text.lower())

    def test_review_count_excludes_dismissed(self):
        run = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=3, review=1, dismissed=2)
        ImportProposal.objects.create(windows_server=self.server, sync_run=run)
        text = panel(self.server_page())
        self.assertIn("1 difference waits for review in an import proposal.", text)
        self.assertIn("2 differences dismissed earlier stay ignored", text)
        self.assertNotIn("3 differences", text)

    def test_closed_proposal_with_only_dismissed_differences(self):
        # The run closed the open proposal: its last pending item went away and only dismissed ones were left.
        run = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=1, review=0, dismissed=1)
        proposal = ImportProposal.objects.create(windows_server=self.server, sync_run=run, status="dismissed")
        text = panel(self.server_page())
        self.assertNotIn("waits for review", text)
        self.assertNotIn("No differences found", text)
        self.assertIn("No new differences waiting for review.", text)
        self.assertIn(proposal.get_absolute_url(), text)

    def test_hidden_proposal_is_not_called_no_differences(self):
        run = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=2, review=2, dismissed=0)
        ImportProposal.objects.create(windows_server=self.server, sync_run=run)
        self.user.is_superuser = False
        self.user.save()
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_windowsserver",
            "netbox_windows_dhcp_plugin.view_syncrun",
            "core.view_job",
        )
        status = import_status.for_server(self.server, self.user)
        self.assertIsNone(status.proposal)
        text = panel(self.server_page())
        self.assertNotIn("No differences found", text)
        self.assertIn("2 differences wait for review in an import proposal you may not view.", text)

    def test_earlier_run_whose_proposal_a_later_run_took_over(self):
        # A repeat import reuses the open proposal and points it at the newer run, so the earlier run has none.
        first = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=2, review=2, dismissed=0)
        second = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=2, review=2, dismissed=0)
        proposal = ImportProposal.objects.create(windows_server=self.server, sync_run=second)
        status = import_status.for_run(first, self.user)
        self.assertEqual((status.proposal, status.proposal_hidden, status.open_proposal), (None, False, proposal))
        text = panel(self.client.get(first.get_absolute_url()).content.decode())
        self.assertNotIn("may not view", text)
        self.assertNotIn("No differences found", text)
        self.assertIn("2 differences were sent for review. That import proposal now belongs to a later import", text)
        self.assertIn("Open import proposal", text)
        self.assertIn(proposal.get_absolute_url(), text)

    def test_deleted_proposal_is_not_called_hidden(self):
        run = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=1, review=1, dismissed=0)
        text = panel(self.server_page())
        self.assertNotIn("may not view", text)
        self.assertIn("1 difference was sent for review. That import proposal now belongs to a later import", text)
        self.assertNotIn("Open import proposal", text)
        self.assertIsNone(import_status.for_run(run, self.user).open_proposal)

    def test_legacy_run_without_review_count_or_proposal(self):
        # Recorded before #43 (no "review" key) in exactly the #43 case: the only difference had been dismissed.
        run = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=1, update=1)
        self.assertIsNone(import_status.for_run(run, self.user).review)
        for html in (self.server_page(), self.client.get(run.get_absolute_url()).content.decode()):
            text = panel(html)
            self.assertIn("1 difference found; no import proposal is linked to this run.", text)
            self.assertNotIn("may not view", text)
            self.assertNotIn("waits for review", text)
            self.assertNotIn("No differences found", text)

    def test_legacy_run_with_hidden_proposal(self):
        run = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=2)
        ImportProposal.objects.create(windows_server=self.server, sync_run=run)
        self.user.is_superuser = False
        self.user.save()
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_windowsserver",
            "netbox_windows_dhcp_plugin.view_syncrun",
            "core.view_job",
        )
        status = import_status.for_run(run, self.user)
        self.assertEqual((status.proposal, status.proposal_hidden, status.review), (None, True, 2))
        text = panel(self.server_page())
        self.assertIn("2 differences wait for review in an import proposal you may not view.", text)

    def test_a_job_that_failed_without_a_run(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED))
        failed = self.job(JobStatusChoices.STATUS_ERRORED)
        self.assertEqual(import_status.for_server(self.server, self.user).next_state, import_status.FAILED)
        self.assertIn(f"The latest import job (#{failed.pk}) ended without a result.", self.server_page())

    def test_failed_run_shows_its_error(self):
        run = self.make_run(self.job(JobStatusChoices.STATUS_FAILED), status=SyncStatusChoices.FAILED)
        run.error = "Access is denied"
        run.save()
        html = self.server_page()
        self.assertIn("The import failed.", html)
        self.assertIn("Access is denied", html)

    def test_stuck_job_stops_the_refresh(self):
        job = self.job(JobStatusChoices.STATUS_RUNNING)
        self.age(job, 3)
        status = import_status.for_server(self.server, self.user)
        self.assertEqual(status.next_state, import_status.FAILED)
        self.assertFalse(status.pending)

    def test_stuck_run_stops_the_refresh(self):
        job = self.job(JobStatusChoices.STATUS_RUNNING)
        run = self.make_run(job, status=SyncStatusChoices.RUNNING)
        self.age(job, 3)
        self.age(run, 3)
        status = import_status.for_server(self.server, self.user)
        self.assertEqual(status.state, import_status.FAILED)
        self.assertNotIn("hx-get", panel(self.server_page()).split(">", 1)[0])

    def test_proposal_run_stays_a_proposal_once_branching_is_installed(self):
        run = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED))
        ImportProposal.objects.create(windows_server=self.server, sync_run=run)
        with mock.patch(BRANCHING, return_value=True):
            status = import_status.for_server(self.server, self.user)
        self.assertFalse(status.uses_branching)

    def test_users_who_may_not_view_runs_or_jobs_see_no_panel(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=0)
        self.job(JobStatusChoices.STATUS_PENDING)
        self.user.is_superuser = False
        self.user.save()
        self.add_permissions("netbox_windows_dhcp_plugin.view_windowsserver")
        self.assertIsNone(import_status.for_server(self.server, self.user))
        self.assertNotIn('id="windows-import-status"', self.server_page())

    def test_refresh_endpoint_returns_the_panel(self):
        self.job(JobStatusChoices.STATUS_PENDING)
        url = reverse("plugins:netbox_windows_dhcp_plugin:windowsserver_import_status", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="windows-import-status"', response.content.decode())
        self.assertIn(f'hx-get="{url}"', response.content.decode())

    def test_refresh_endpoint_needs_view_permission(self):
        self.user.is_superuser = False
        self.user.save()
        url = reverse("plugins:netbox_windows_dhcp_plugin:windowsserver_import_status", args=[self.server.pk])
        self.assertIn(self.client.get(url).status_code, (403, 404))

    def test_sync_run_page_shows_the_result_for_imports_only(self):
        imported = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=0)
        self.assertIn("Import result", self.client.get(imported.get_absolute_url()).content.decode())
        pushed = self.make_run(kind=SyncKindChoices.PUSH)
        self.assertNotIn("Import result", self.client.get(pushed.get_absolute_url()).content.decode())

    def test_queue_message_mentions_the_proposal(self):
        url = reverse("plugins:netbox_windows_dhcp_plugin:windowsserver_import", args=[self.server.pk])
        with mock.patch("netbox_windows_dhcp_plugin.views.enqueue_import", return_value=SimpleNamespace(pk=42)):
            response = self.client.post(url, follow=True)
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("Queued import (job #42)", text)
        self.assertIn("import proposal", text)
        self.assertNotIn("branch", text)


class BranchImportStatusTest(ImportStatusTestBase):
    """The branch itself is stubbed here; tests_netbox/test_import.py checks a real branch import end to end."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch(BRANCHING, return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_completed_import_names_the_branch_and_change_request(self):
        self.make_run(
            self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/20261022-101010", created=True
        )
        with mock.patch(IMPORT_BRANCH, return_value=(fake_branch(), FakeChangeRequest(), True)):
            status = import_status.for_server(self.server, self.user)
            html = self.server_page()
        self.assertTrue(status.branch.main_unchanged)
        self.assertIn("Imported 2 differences into a new branch.", html)
        self.assertIn("Main has not been changed.", html)
        self.assertIn('href="/plugins/branching/branches/7/">dhcp-import/dhcp1/20261022-101010</a>', html)
        self.assertIn('href="/plugins/changes/change-requests/3/"', html)

    def test_reused_branch_with_new_differences(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", changes=1)
        with mock.patch(IMPORT_BRANCH, return_value=(fake_branch(), None, True)):
            html = self.server_page()
        self.assertIn("Added 1 difference to the open import branch.", html)

    def test_failed_run_keeps_the_earlier_open_branch_in_sight(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", created=True)
        self.make_run(self.job(JobStatusChoices.STATUS_FAILED), status=SyncStatusChoices.FAILED)
        with mock.patch(IMPORT_BRANCH, return_value=(fake_branch(), None, True)) as lookup:
            status = import_status.for_server(self.server, self.user)
            html = self.server_page()
        lookup.assert_called_with("dhcp-import/dhcp1/x", self.user)
        self.assertEqual(status.state, import_status.FAILED)
        self.assertEqual(status.earlier_branch.name, "dhcp-import/dhcp1/x")
        self.assertIn("An earlier import's branch is still open.", html)
        self.assertIn('href="/plugins/branching/branches/7/">dhcp-import/dhcp1/x</a>', html)

    def test_hidden_branch_is_not_called_gone(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x")
        with mock.patch(IMPORT_BRANCH, return_value=(None, None, True)):
            html = self.server_page()
        self.assertIn("dhcp-import/dhcp1/x", html)
        self.assertNotIn("(no longer exists)", html)

    def test_reused_branch_with_nothing_new(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", changes=0)
        with mock.patch(IMPORT_BRANCH, return_value=(fake_branch(), None, True)):
            html = self.server_page()
        self.assertIn("No new differences since the last import: the branch below already holds them.", html)
        self.assertIn("Main has not been changed.", html)

    def test_merged_branch_does_not_claim_main_is_unchanged(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x")
        with mock.patch(IMPORT_BRANCH, return_value=(fake_branch("merged"), None, True)):
            html = self.server_page()
        self.assertNotIn("Main has not been changed.", html)
        self.assertNotIn("then merge it", html)
        self.assertIn("Merged", html)

    def test_deleted_branch(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x")
        with (
            mock.patch(IMPORT_BRANCH, return_value=(None, None, False)),
            mock.patch(BRANCH_DELETION, return_value=None),
        ):
            html = self.server_page()
        self.assertIn("(no longer exists)", html)
        self.assertNotIn("Main has not been changed.", html)

    # Issue #44: a branch that was merged or deleted is no longer called open.

    def deletion(self, status, merged_time=None):
        change = SimpleNamespace(get_absolute_url=lambda: "/core/changelog/9/")
        return mock.patch(BRANCH_DELETION, return_value=(change, status, merged_time))

    def test_merged_branch_is_not_called_open(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", changes=1)
        with mock.patch(IMPORT_BRANCH, return_value=(fake_branch("merged", MERGED_AT), None, True)):
            html = panel(self.server_page())
        self.assertNotIn("open import branch", html)
        self.assertIn("Added 1 difference to an import branch that was already open.", html)
        self.assertIn(MERGED_ON, html)
        self.assertIn("Main may have changed since", html)
        self.assertNotIn("Main has not been changed.", html)

    def test_archived_branch_reads_as_merged(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", created=True)
        with mock.patch(IMPORT_BRANCH, return_value=(fake_branch("archived", MERGED_AT), None, True)):
            status = import_status.for_server(self.server, self.user)
            html = panel(self.server_page())
        self.assertEqual(status.branch.lifecycle, import_status.MERGED)
        self.assertIn("Imported 2 differences into a new branch.", html)
        self.assertIn(MERGED_ON, html)

    def test_merging_branch(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", changes=1)
        with mock.patch(IMPORT_BRANCH, return_value=(fake_branch("merging"), None, True)):
            html = panel(self.server_page())
        self.assertIn("The branch is being merged into main.", html)
        self.assertNotIn("open import branch", html)

    def test_branch_merged_then_deleted(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", changes=1)
        with mock.patch(IMPORT_BRANCH, return_value=(None, None, False)), self.deletion("merged", MERGED_AT):
            status = import_status.for_server(self.server, self.user)
            html = panel(self.server_page())
        self.assertEqual(status.branch.lifecycle, import_status.DELETED_MERGED)
        self.assertNotIn("open import branch", html)
        self.assertIn(MERGED_ON, html)
        self.assertIn("and has since been deleted.", html)
        self.assertIn("Main may have changed since", html)
        # The deletion record stays one click away; "no longer exists" is for a branch nothing more is known of.
        self.assertIn('(<a href="/core/changelog/9/">deleted</a>)', html)
        self.assertNotIn("(no longer exists)", html)
        self.assertIn('<th scope="row">Merged</th>', html)

    def test_branch_merged_then_deleted_without_a_merge_time(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", changes=1)
        with mock.patch(IMPORT_BRANCH, return_value=(None, None, False)), self.deletion("archived"):
            html = panel(self.server_page())
        self.assertIn("The branch was merged into main and has since been deleted.", html)

    def test_branch_deleted_without_being_merged(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", changes=1)
        with mock.patch(IMPORT_BRANCH, return_value=(None, None, False)), self.deletion("ready"):
            html = panel(self.server_page())
        self.assertIn("deleted without being merged into main", html)
        self.assertNotIn("was merged into main on", html)
        self.assertNotIn("open import branch", html)

    def test_missing_branch_is_not_read_as_merged(self):
        # No deletion record the user may view: the outcome is unknown, and the panel says so.
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", changes=1)
        with (
            mock.patch(IMPORT_BRANCH, return_value=(None, None, False)),
            mock.patch(BRANCH_DELETION, return_value=None),
        ):
            status = import_status.for_server(self.server, self.user)
            html = panel(self.server_page())
        self.assertEqual(status.branch.lifecycle, import_status.GONE)
        self.assertIn("whether it was merged into main is not known here", html)
        self.assertNotIn("open import branch", html)
        self.assertNotIn("The branch was merged", html)

    def test_hidden_branch_has_no_lifecycle_claim(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", changes=1)
        with mock.patch(IMPORT_BRANCH, return_value=(None, None, True)), mock.patch(BRANCH_DELETION) as lookup:
            html = panel(self.server_page())
        lookup.assert_not_called()
        self.assertNotIn("open import branch", html)
        self.assertNotIn("merged into main", html)
        self.assertNotIn("not known here", html)

    def test_deleted_branch_lookup_starts_at_the_run(self):
        run = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x")
        with mock.patch(IMPORT_BRANCH, return_value=(None, None, False)), self.deletion("merged") as lookup:
            import_status.for_run(run, self.user)
        lookup.assert_called_once_with("dhcp-import/dhcp1/x", self.user, since=run.created)

    def test_recreated_branch_of_the_same_name_is_not_the_runs(self):
        # A branch the user chose, deleted after the run and created again under the same name: the new branch says
        # nothing about the run's, so the panel reads the old one's deletion instead.
        run = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="mywork", changes=1)
        SyncRun.objects.filter(pk=run.pk).update(destination="current", completed=timezone.now() - timedelta(hours=2))
        newer = fake_branch()
        newer.created = timezone.now()
        with (
            mock.patch(IMPORT_BRANCH, return_value=(newer, FakeChangeRequest(), True)),
            self.deletion("merged", MERGED_AT),
        ):
            status = import_status.for_server(self.server, self.user)
            html = panel(self.server_page())
        self.assertEqual(status.branch.lifecycle, import_status.DELETED_MERGED)
        self.assertIsNone(status.branch.change_request)
        self.assertIn("and has since been deleted.", html)
        self.assertNotIn("Main has not been changed.", html)
        self.assertNotIn("/plugins/branching/branches/7/", html)

    def test_branch_the_run_created_is_its_own(self):
        # An import branch is created after its run starts, but before the run finishes.
        run = self.make_run(
            self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", created=True
        )
        SyncRun.objects.filter(pk=run.pk).update(completed=run.created + timedelta(minutes=1))
        own = fake_branch()
        own.created = run.created + timedelta(seconds=1)
        with mock.patch(IMPORT_BRANCH, return_value=(own, None, True)):
            status = import_status.for_server(self.server, self.user)
        self.assertEqual(status.branch.lifecycle, import_status.OPEN)

    def test_legacy_run_without_branch_created(self):
        run = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", changes=1)
        SyncRun.objects.filter(pk=run.pk).update(summary={"changes": 1})
        with mock.patch(IMPORT_BRANCH, return_value=(fake_branch("merged", MERGED_AT), None, True)):
            html = panel(self.server_page())
        self.assertIn("Imported 1 difference into the import branch below.", html)
        self.assertNotIn("already open", html)

    def test_failed_run_into_a_merged_branch(self):
        self.make_run(
            self.job(JobStatusChoices.STATUS_FAILED), status=SyncStatusChoices.FAILED, branch_name="dhcp-import/dhcp1/x"
        )
        with mock.patch(IMPORT_BRANCH, return_value=(None, None, False)), self.deletion("merged", MERGED_AT):
            html = panel(self.server_page())
        self.assertIn("The import failed.", html)
        self.assertIn("and has since been deleted.", html)
        self.assertNotIn("Main has not been changed.", html)

    def test_merged_and_deleted_then_nothing_new(self):
        # The follow-up on #44: a later import with nothing new gives the server page a truthful latest result, and
        # the earlier run's own page still says what became of its branch instead of calling it open.
        old = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", changes=1)
        self.age(old, 1)
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=0)
        with mock.patch(IMPORT_BRANCH, return_value=(None, None, False)), self.deletion("merged", MERGED_AT):
            latest = panel(self.server_page())
            response = self.client.get(old.get_absolute_url())
        self.assertIn("nothing was imported and no branch was created", latest)
        self.assertNotIn("dhcp-import/dhcp1/x", latest)
        self.assertEqual(response.status_code, 200)
        history = panel(response.content.decode())
        self.assertNotIn("open import branch", history)
        self.assertIn(MERGED_ON, history)

    def test_server_panel_links_the_import_history(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=0)
        html = panel(self.server_page())
        self.assertIn(f"/plugins/windows-dhcp/syncruns/?windows_server_id={self.server.pk}&amp;kind=import", html)

    def test_direct_import_does_not_claim_main_still_matches(self):
        run = self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=0)
        SyncRun.objects.filter(pk=run.pk).update(destination="main")
        html = panel(self.server_page())
        self.assertIn("main matched Windows when this import ran", html)
        self.assertNotIn("already matches", html)

    def test_no_differences_no_branch(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=0)
        self.assertIn("nothing was imported and no branch was created", self.server_page())

    def test_queued_import_says_where_it_will_go(self):
        self.job(JobStatusChoices.STATUS_PENDING)
        self.assertIn("It will write into an import branch, not into main.", self.server_page())

    def test_queue_message_mentions_the_branch(self):
        url = reverse("plugins:netbox_windows_dhcp_plugin:windowsserver_import", args=[self.server.pk])
        with (
            mock.patch("netbox_windows_dhcp_plugin.views.enqueue_import", return_value=SimpleNamespace(pk=42)),
            mock.patch("netbox_windows_dhcp_plugin.integrations.branching.missing_import_permissions", return_value=[]),
        ):
            response = self.client.post(url, follow=True)
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("It writes into an import branch, not into main", text)


@skipUnless(apps.is_installed("netbox_branching"), "needs netbox-branching")
class BranchDeletionLookupTest(TestCase):
    """branching.branch_deletion reads a deleted branch's last status from the change log (issue #44)."""

    def setUp(self):
        super().setUp()
        from django.contrib.contenttypes.models import ContentType
        from netbox_branching.models import Branch

        self.branch_type = ContentType.objects.get_for_model(Branch)

    def record(self, name, status, merged_time=None, time=None):
        from core.models import ObjectChange

        change = ObjectChange.objects.create(
            user=self.user,
            user_name=self.user.username,
            request_id=uuid.uuid4(),
            action="delete",
            changed_object_type=self.branch_type,
            changed_object_id=1,
            object_repr=name,
            prechange_data={"name": name, "status": status, "merged_time": merged_time},
        )
        if time is not None:
            ObjectChange.objects.filter(pk=change.pk).update(time=time)
        return change

    def test_merged_then_deleted(self):
        from netbox_windows_dhcp_plugin.integrations import branching

        self.add_permissions("core.view_objectchange")
        change = self.record("dhcp-import/dhcp1/x", "merged", "2026-10-23T12:26:23.328Z")
        self.record("dhcp-import/dhcp1/other", "ready")
        found, status, merged_time = branching.branch_deletion("dhcp-import/dhcp1/x", self.user)
        self.assertEqual((found.pk, status), (change.pk, "merged"))
        self.assertEqual(merged_time, datetime(2026, 10, 23, 12, 26, 23, 328000, tzinfo=UTC))

    def test_needs_permission_to_view_the_change_log(self):
        from netbox_windows_dhcp_plugin.integrations import branching

        self.record("dhcp-import/dhcp1/x", "merged")
        self.assertIsNone(branching.branch_deletion("dhcp-import/dhcp1/x", self.user))

    def test_ignores_a_same_named_branch_deleted_before_the_run(self):
        from netbox_windows_dhcp_plugin.integrations import branching

        self.add_permissions("core.view_objectchange")
        now = timezone.now()
        self.record("mywork", "merged", time=now - timedelta(days=2))
        self.assertIsNone(branching.branch_deletion("mywork", self.user, since=now - timedelta(days=1)))
        first = self.record("mywork", "merged", time=now - timedelta(hours=2))
        self.assertEqual(branching.branch_deletion("mywork", self.user, since=now - timedelta(days=1))[0].pk, first.pk)

    def test_takes_the_first_deletion_after_the_run(self):
        # The run's branch was deleted first; a later branch of the same name was created and deleted after it.
        from netbox_windows_dhcp_plugin.integrations import branching

        self.add_permissions("core.view_objectchange")
        now = timezone.now()
        first = self.record("mywork", "merged", time=now - timedelta(hours=2))
        self.record("mywork", "ready", time=now - timedelta(hours=1))
        found, status, _ = branching.branch_deletion("mywork", self.user, since=now - timedelta(days=1))
        self.assertEqual((found.pk, status), (first.pk, "merged"))
