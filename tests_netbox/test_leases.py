"""Live leases: the job that reads them, and the Leases tab on the scope page."""

import uuid
from datetime import timedelta
from unittest import mock

from core.choices import JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job, ObjectType
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from users.models import ObjectPermission
from utilities.testing import TestCase

from netbox_windows_dhcp_plugin import leases
from netbox_windows_dhcp_plugin.jobs import ORPHANED_JOB, LeaseJob, enqueue_leases, lease_jobs
from netbox_windows_dhcp_plugin.models import WindowsServer
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import Scope, ScopeV6
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import PSError

from .fixtures import scope, windows_server
from .test_jobs import CREDENTIALS


class LeaseMixin:
    def setUp(self):
        super().setUp()
        self.server = windows_server()
        self.scope = scope(self.server, "10.1.0.0/24")
        self.scope6 = scope(self.server, "2001:db8:1::/64")
        self.fake = FakeDhcpServer()
        c = DhcpClient(self.fake)
        c.add_scope(Scope(name="lan", start_range="10.1.0.10", end_range="10.1.0.200", subnet_mask="255.255.255.0"))
        c.add_v6_scope(ScopeV6(prefix="2001:db8:1::", name="v6"))
        self.fake.add_lease("10.1.0.0", "10.1.0.20", host_name="pc20")
        self.fake.add_lease("10.1.0.0", "10.1.0.3", host_name="<script>alert(1)</script>")
        self.fake.add_v6_lease("2001:db8:1::", "2001:db8:1::1:10", iaid=9, host_name="pc6")
        LeaseJob.runner_factory = staticmethod(lambda server: self.fake)
        self.addCleanup(setattr, LeaseJob, "runner_factory", None)

    def run_job(self, scope_, server=None):
        job = Job.objects.create(
            object=server or self.server,
            name=LeaseJob.name,
            status=JobStatusChoices.STATUS_RUNNING,
            job_id=uuid.uuid4(),
            data={"scope": scope_.pk},
        )
        try:
            LeaseJob(job).run(scope_pk=scope_.pk)
        finally:
            job.terminate(status=JobStatusChoices.STATUS_COMPLETED)
        return job


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class LeaseJobTest(LeaseMixin, TestCase):
    def test_ipv4_leases_in_address_order(self):
        job = self.run_job(self.scope)
        job.refresh_from_db()
        self.assertEqual(
            (job.data["family"], job.data["scope"], job.data["scope_id"], job.data["total"]),
            (4, self.scope.pk, "10.1.0.0", 2),
        )
        self.assertEqual([r["ip_address"] for r in job.data["leases"]], ["10.1.0.3", "10.1.0.20"])
        self.assertEqual(job.data["leases"][1]["client"], "00-11-22-33-44-55")
        self.assertFalse(job.data["truncated"])

    def test_ipv6_leases(self):
        job = self.run_job(self.scope6)
        job.refresh_from_db()
        (row,) = job.data["leases"]
        self.assertEqual((row["ip_address"], row["iaid"], row["host_name"]), ("2001:db8:1::1:10", 9, "pc6"))
        self.assertEqual(job.data["family"], 6)
        # Windows has no DHCPv6 lease state (issue #22): none is kept, rather than an empty or borrowed one.
        self.assertNotIn("address_state", row)

    def test_scope_missing_on_windows_fails_with_a_reason(self):
        self.fake.scopes.clear()
        with self.assertRaises(JobFailed):
            self.run_job(self.scope)
        job = lease_jobs(self.scope, self.server).get()
        self.assertEqual(job.data, {"scope": self.scope.pk, "error": "scope 10.1.0.0 is not on dhcp1.example.com"})

    def test_windows_error_is_kept_without_secrets(self):
        self.fake.fail = lambda op, params: PSError(message="Access is denied", category="PermissionDenied")
        with self.assertRaises(JobFailed):
            self.run_job(self.scope)
        error = lease_jobs(self.scope, self.server).get().data["error"]
        self.assertIn("Access is denied", error)
        self.assertNotIn("password", error.lower())

    def test_scope_of_another_server_fails(self):
        # The scope moved to another server after the read was queued: the job must not read it from this one.
        other = scope(windows_server("dhcp5"), "10.9.0.0/24")
        with self.assertRaises(JobFailed):
            self.run_job(other)
        self.assertIn("no longer belongs", lease_jobs(other, self.server).get().data["error"])
        self.assertEqual(enqueue_leases(other, None).object, other.server)

    def test_large_scopes_are_cut(self):
        for i in range(leases.LEASE_LIMIT + 3):
            self.fake.add_lease("10.1.0.0", f"10.1.{i // 250}.{i % 250}")
        job = self.run_job(self.scope)
        job.refresh_from_db()
        self.assertEqual(len(job.data["leases"]), leases.LEASE_LIMIT)
        self.assertTrue(job.data["truncated"])
        self.assertEqual(job.data["total"], leases.LEASE_LIMIT + 5)

    def test_one_read_at_a_time_per_scope(self):
        job = enqueue_leases(self.scope, None)
        self.assertEqual((job.object, job.data), (self.server, {"scope": self.scope.pk}))
        self.assertIsNone(enqueue_leases(self.scope, None))
        self.assertIsNotNone(enqueue_leases(self.scope6, None))


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class LeaseTabTest(LeaseMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_scope",
            "netbox_windows_dhcp_plugin.view_windowsserver",
            "netbox_windows_dhcp_plugin.change_windowsserver",
        )
        self.url = reverse("plugins:netbox_windows_dhcp_plugin:scope_leases", kwargs={"pk": self.scope.pk})

    def test_tab_shows_with_the_windows_server_view_permission(self):
        response = self.client.get(self.scope.get_absolute_url())
        self.assertContains(response, self.url)
        ObjectPermission.objects.all().delete()
        self.add_permissions("netbox_windows_dhcp_plugin.view_scope")
        response = self.client.get(self.scope.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.url)

    def test_post_queues_one_job_and_the_panel_polls_until_done(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertEqual(lease_jobs(self.scope, self.server).count(), 1)
        self.client.post(self.url)
        self.assertEqual(lease_jobs(self.scope, self.server).count(), 1)
        page = self.client.get(self.url)
        self.assertContains(page, 'hx-trigger="every 3s"')
        partial = self.client.get(self.url, headers={"HX-Request": "true"})
        self.assertContains(partial, "Reading leases from")
        self.assertNotContains(partial, "<html")

    def test_result_is_shown_escaped(self):
        self.run_job(self.scope)
        response = self.client.get(self.url)
        self.assertNotContains(response, 'hx-trigger="every 3s"')
        self.assertContains(response, "2 leases in scope 10.1.0.0 on")
        self.assertContains(response, "pc20")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")

    def test_ipv6_lease_state_is_shown_as_unavailable(self):
        self.run_job(self.scope)
        response = self.client.get(self.url)
        self.assertContains(response, "<td>Active</td>")
        self.assertNotContains(response, "Windows does not report a state for DHCPv6 leases")
        self.run_job(self.scope6)
        url6 = reverse("plugins:netbox_windows_dhcp_plugin:scope_leases", kwargs={"pk": self.scope6.pk})
        response = self.client.get(url6)
        self.assertContains(response, "pc6")
        self.assertContains(response, "Windows does not report a state for DHCPv6 leases")
        self.assertContains(response, "n/a")

    def test_failure_is_shown(self):
        self.fake.scopes.clear()
        with self.assertRaises(JobFailed):
            self.run_job(self.scope)
        Job.objects.update(status=JobStatusChoices.STATUS_FAILED)
        self.assertContains(self.client.get(self.url), "is not on dhcp1.example.com")

    def test_loading_needs_the_change_permission_on_the_server(self):
        ObjectPermission.objects.all().delete()
        self.add_permissions("netbox_windows_dhcp_plugin.view_scope", "netbox_windows_dhcp_plugin.view_windowsserver")
        self.run_job(self.scope)
        page = self.client.get(self.url)
        # The last read is still shown, without the button.
        self.assertContains(page, "pc20")
        self.assertNotContains(page, "Load leases")
        before = lease_jobs(self.scope, self.server).count()
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.assertEqual(lease_jobs(self.scope, self.server).count(), before)

    def test_needs_the_windows_server_view_permission(self):
        self.user.object_permissions.all().delete()
        ObjectPermission.objects.all().delete()
        self.add_permissions("netbox_windows_dhcp_plugin.view_scope")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.post(self.url)
        self.assertFalse(lease_jobs(self.scope, self.server).exists())

    def test_object_permissions_on_the_server_are_respected(self):
        ObjectPermission.objects.all().delete()
        self.add_permissions("netbox_windows_dhcp_plugin.view_scope")
        perm = ObjectPermission.objects.create(
            name="other servers", actions=["view"], constraints={"pk": self.server.pk + 1000}
        )
        perm.object_types.add(ObjectType.objects.get_for_model(WindowsServer))
        perm.users.add(self.user)
        self.assertFalse(WindowsServer.objects.restrict(self.user, "view").exists())
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(lease_jobs(self.scope, self.server).exists())
        self.assertContains(self.client.get(self.url), "You cannot view the Windows server of this scope")

    def test_change_permission_limited_to_other_servers_does_not_load(self):
        ObjectPermission.objects.all().delete()
        self.add_permissions("netbox_windows_dhcp_plugin.view_scope", "netbox_windows_dhcp_plugin.view_windowsserver")
        perm = ObjectPermission.objects.create(
            name="change other servers", actions=["change"], constraints={"pk": self.server.pk + 1000}
        )
        perm.object_types.add(ObjectType.objects.get_for_model(WindowsServer))
        perm.users.add(self.user)
        self.assertNotContains(self.client.get(self.url), "Load leases")
        self.client.post(self.url)
        self.assertFalse(lease_jobs(self.scope, self.server).exists())

    def test_a_lost_job_stops_blocking_the_page(self):
        job = enqueue_leases(self.scope, None)
        Job.objects.filter(pk=job.pk).update(created=timezone.now() - ORPHANED_JOB - timedelta(minutes=1))
        page = self.client.get(self.url)
        self.assertNotContains(page, 'hx-trigger="every 3s"')
        self.assertContains(page, "never finished")
        self.assertIsNotNone(enqueue_leases(self.scope, None))

    def test_only_the_latest_read_is_kept(self):
        self.run_job(self.scope)
        self.run_job(self.scope6)
        latest = self.run_job(self.scope)
        self.assertEqual(list(lease_jobs(self.scope, self.server)), [latest])
        self.assertEqual(lease_jobs(self.scope6, self.server).count(), 1)

    def test_no_reads_from_inside_a_branch(self):
        with mock.patch("netbox_windows_dhcp_plugin.integrations.branching.in_branch", return_value=True):
            self.assertNotContains(self.client.get(self.url), "Load leases")
            self.client.post(self.url)
        self.assertFalse(lease_jobs(self.scope, self.server).exists())
