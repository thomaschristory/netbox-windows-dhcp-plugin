"""
The Import panel on the Windows server and sync run pages (issue #32).

With branching an import goes into an import branch, so the page must say which one, link to it and its change
request, and say main has not changed; a queued or running import must not look finished. Without branching the
panel points at the import proposal and never mentions a branch.
"""

import uuid
from types import SimpleNamespace
from unittest import mock

from core.choices import JobStatusChoices
from core.models import Job
from django.urls import reverse
from utilities.testing import TestCase

from netbox_windows_dhcp_plugin import import_status
from netbox_windows_dhcp_plugin.choices import SyncKindChoices, SyncStatusChoices
from netbox_windows_dhcp_plugin.jobs import ImportJob
from netbox_windows_dhcp_plugin.models import ImportProposal, SyncRun

from .fixtures import dhcp_servers, windows_server

BRANCHING = "netbox_windows_dhcp_plugin.integrations.branching.branching_installed"
IMPORT_BRANCH = "netbox_windows_dhcp_plugin.integrations.branching.import_branch"


def fake_branch(status="ready"):
    return SimpleNamespace(
        status=status,
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
        self, job=None, status=SyncStatusChoices.COMPLETED, branch_name="", changes=2, kind=SyncKindChoices.IMPORT
    ):
        return SyncRun.objects.create(
            windows_server=self.server,
            kind=kind,
            status=status,
            job=job,
            branch_name=branch_name,
            summary={"changes": changes},
        )

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
        self.assertIsNone(import_status.for_server(self.server))
        self.assertNotIn('id="windows-import-status"', self.server_page())

    def test_queued_import_is_not_shown_as_done(self):
        job = self.job(JobStatusChoices.STATUS_PENDING)
        status = import_status.for_server(self.server)
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
        status = import_status.for_server(self.server)
        self.assertEqual((status.state, status.next_state), (import_status.RUNNING, None))
        self.assertIn("Import running. Nothing is final until it has finished.", self.server_page())

    def test_finished_import_links_the_proposal_and_never_a_branch(self):
        job = self.job(JobStatusChoices.STATUS_COMPLETED)
        run = self.make_run(job)
        proposal = ImportProposal.objects.create(windows_server=self.server, sync_run=run)
        status = import_status.for_server(self.server)
        self.assertEqual((status.state, status.proposal, status.pending), (import_status.COMPLETED, proposal, False))
        html = self.server_page()
        self.assertIn("2 differences wait for review in an import proposal.", html)
        self.assertIn(proposal.get_absolute_url(), html)
        self.assertNotIn("Main has not been changed", html)
        self.assertNotIn("hx-get", panel(html).split(">", 1)[0])
        self.assertNotIn("branch", panel(html).lower())

    def test_a_newer_queued_job_keeps_the_previous_result(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), changes=0)
        queued = self.job(JobStatusChoices.STATUS_PENDING)
        status = import_status.for_server(self.server)
        self.assertEqual((status.state, status.next_job), (import_status.COMPLETED, queued))
        html = self.server_page()
        self.assertIn("Import queued", html)
        self.assertIn("Previous import", html)
        self.assertIn("No differences found.", html)

    def test_a_job_that_failed_without_a_run(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED))
        failed = self.job(JobStatusChoices.STATUS_ERRORED)
        self.assertEqual(import_status.for_server(self.server).next_state, import_status.FAILED)
        self.assertIn(f"The latest import job (#{failed.pk}) ended without a result.", self.server_page())

    def test_failed_run_shows_its_error(self):
        run = self.make_run(self.job(JobStatusChoices.STATUS_FAILED), status=SyncStatusChoices.FAILED)
        run.error = "Access is denied"
        run.save()
        html = self.server_page()
        self.assertIn("The import failed.", html)
        self.assertIn("Access is denied", html)

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
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/20261022-101010")
        with mock.patch(IMPORT_BRANCH, return_value=(fake_branch(), FakeChangeRequest())):
            status = import_status.for_server(self.server)
            html = self.server_page()
        self.assertTrue(status.main_unchanged)
        self.assertIn("Imported 2 differences into a branch.", html)
        self.assertIn("Main has not been changed.", html)
        self.assertIn('href="/plugins/branching/branches/7/">dhcp-import/dhcp1/20261022-101010</a>', html)
        self.assertIn('href="/plugins/changes/change-requests/3/"', html)

    def test_reused_branch_with_nothing_new(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x", changes=0)
        with mock.patch(IMPORT_BRANCH, return_value=(fake_branch(), None)):
            html = self.server_page()
        self.assertIn("No new differences since the last import: the branch below already holds them.", html)
        self.assertIn("Main has not been changed.", html)

    def test_merged_branch_does_not_claim_main_is_unchanged(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x")
        with mock.patch(IMPORT_BRANCH, return_value=(fake_branch("merged"), None)):
            html = self.server_page()
        self.assertNotIn("Main has not been changed.", html)
        self.assertIn("Merged", html)

    def test_deleted_branch(self):
        self.make_run(self.job(JobStatusChoices.STATUS_COMPLETED), branch_name="dhcp-import/dhcp1/x")
        with mock.patch(IMPORT_BRANCH, return_value=(None, None)):
            html = self.server_page()
        self.assertIn("(no longer exists)", html)
        self.assertNotIn("Main has not been changed.", html)

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
