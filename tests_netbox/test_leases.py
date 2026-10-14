"""Live leases: the job that reads them, and the Leases tab on netbox-plugin-dhcp's subnet page."""

import uuid

from core.choices import JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job, ObjectType
from django.test import override_settings
from django.urls import reverse
from ipam.models import Prefix
from netbox_dhcp.models import Subnet
from users.models import ObjectPermission
from utilities.testing import TestCase

from netbox_windows_dhcp_plugin import leases
from netbox_windows_dhcp_plugin.jobs import LeaseJob, enqueue_leases, lease_jobs
from netbox_windows_dhcp_plugin.models import WindowsServer
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import Scope, ScopeV6
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import PSError

from .fixtures import dhcp_servers, windows_server
from .test_jobs import CREDENTIALS


class LeaseMixin:
    def setUp(self):
        super().setUp()
        (self.dhcp,) = dhcp_servers(1)
        self.server = windows_server(self.dhcp)
        self.subnet = Subnet.objects.create(
            name="dhcp1/10.1.0.0", dhcp_server=self.dhcp, prefix=Prefix.objects.create(prefix="10.1.0.0/24")
        )
        self.subnet6 = Subnet.objects.create(
            name="dhcp1/2001:db8:1::", dhcp_server=self.dhcp, prefix=Prefix.objects.create(prefix="2001:db8:1::/64")
        )
        self.fake = FakeDhcpServer()
        c = DhcpClient(self.fake)
        c.add_scope(Scope(name="lan", start_range="10.1.0.10", end_range="10.1.0.200", subnet_mask="255.255.255.0"))
        c.add_v6_scope(ScopeV6(prefix="2001:db8:1::", name="v6"))
        self.fake.add_lease("10.1.0.0", "10.1.0.20", host_name="pc20")
        self.fake.add_lease("10.1.0.0", "10.1.0.3", host_name="<script>alert(1)</script>")
        self.fake.add_v6_lease("2001:db8:1::", "2001:db8:1::1:10", iaid=9, host_name="pc6")
        LeaseJob.runner_factory = staticmethod(lambda server: self.fake)
        self.addCleanup(setattr, LeaseJob, "runner_factory", None)

    def run_job(self, subnet, server=None):
        job = Job.objects.create(
            object=server or self.server,
            name=LeaseJob.name,
            status=JobStatusChoices.STATUS_RUNNING,
            job_id=uuid.uuid4(),
            data={"subnet": subnet.pk},
        )
        try:
            LeaseJob(job).run(subnet_id=subnet.pk)
        finally:
            job.terminate(status=JobStatusChoices.STATUS_COMPLETED)
        return job


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class LeaseJobTest(LeaseMixin, TestCase):
    def test_ipv4_leases_in_address_order(self):
        job = self.run_job(self.subnet)
        job.refresh_from_db()
        self.assertEqual((job.data["family"], job.data["scope"], job.data["total"]), (4, "10.1.0.0", 2))
        self.assertEqual([r["ip_address"] for r in job.data["leases"]], ["10.1.0.3", "10.1.0.20"])
        self.assertEqual(job.data["leases"][1]["client"], "00-11-22-33-44-55")
        self.assertFalse(job.data["truncated"])

    def test_ipv6_leases(self):
        job = self.run_job(self.subnet6)
        job.refresh_from_db()
        (row,) = job.data["leases"]
        self.assertEqual((row["ip_address"], row["iaid"], row["host_name"]), ("2001:db8:1::1:10", 9, "pc6"))
        self.assertEqual(job.data["family"], 6)

    def test_scope_missing_on_windows_fails_with_a_reason(self):
        self.fake.scopes.clear()
        with self.assertRaises(JobFailed):
            self.run_job(self.subnet)
        job = lease_jobs(self.subnet, self.server).get()
        self.assertEqual(job.data, {"subnet": self.subnet.pk, "error": "scope 10.1.0.0 is not on dhcp1.example.com"})

    def test_windows_error_is_kept_without_secrets(self):
        self.fake.fail = lambda op, params: PSError(message="Access is denied", category="PermissionDenied")
        with self.assertRaises(JobFailed):
            self.run_job(self.subnet)
        error = lease_jobs(self.subnet, self.server).get().data["error"]
        self.assertIn("Access is denied", error)
        self.assertNotIn("password", error.lower())

    def test_subnet_of_another_server_fails(self):
        other = Subnet.objects.create(
            name="other", dhcp_server=dhcp_servers(1, start=5)[0], prefix=Prefix.objects.create(prefix="10.9.0.0/24")
        )
        with self.assertRaises(JobFailed):
            self.run_job(other)
        self.assertIsNone(enqueue_leases(other, None))

    def test_large_scopes_are_cut(self):
        for i in range(leases.LEASE_LIMIT + 3):
            self.fake.add_lease("10.1.0.0", f"10.1.{i // 250}.{i % 250}")
        job = self.run_job(self.subnet)
        job.refresh_from_db()
        self.assertEqual(len(job.data["leases"]), leases.LEASE_LIMIT)
        self.assertTrue(job.data["truncated"])
        self.assertEqual(job.data["total"], leases.LEASE_LIMIT + 5)

    def test_one_read_at_a_time_per_subnet(self):
        job = enqueue_leases(self.subnet, None)
        self.assertEqual((job.object, job.data), (self.server, {"subnet": self.subnet.pk}))
        self.assertIsNone(enqueue_leases(self.subnet, None))
        self.assertIsNotNone(enqueue_leases(self.subnet6, None))


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class LeaseTabTest(LeaseMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.add_permissions("netbox_dhcp.view_subnet", "netbox_windows_dhcp_plugin.view_windowsserver")
        self.url = reverse("plugins:netbox_dhcp:subnet_windows_leases", kwargs={"pk": self.subnet.pk})

    def test_tab_shows_on_windows_subnets_only(self):
        response = self.client.get(self.subnet.get_absolute_url())
        self.assertContains(response, self.url)
        other = Subnet.objects.create(
            name="other", dhcp_server=dhcp_servers(1, start=5)[0], prefix=Prefix.objects.create(prefix="10.9.0.0/24")
        )
        response = self.client.get(other.get_absolute_url())
        self.assertNotContains(response, "windows-leases")

    def test_post_queues_one_job_and_the_panel_polls_until_done(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertEqual(lease_jobs(self.subnet, self.server).count(), 1)
        self.client.post(self.url)
        self.assertEqual(lease_jobs(self.subnet, self.server).count(), 1)
        page = self.client.get(self.url)
        self.assertContains(page, 'hx-trigger="every 3s"')
        partial = self.client.get(self.url, headers={"HX-Request": "true"})
        self.assertContains(partial, "Reading leases from")
        self.assertNotContains(partial, "<html")

    def test_result_is_shown_escaped(self):
        self.run_job(self.subnet)
        response = self.client.get(self.url)
        self.assertNotContains(response, 'hx-trigger="every 3s"')
        self.assertContains(response, "2 leases on")
        self.assertContains(response, "pc20")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")

    def test_failure_is_shown(self):
        self.fake.scopes.clear()
        with self.assertRaises(JobFailed):
            self.run_job(self.subnet)
        Job.objects.update(status=JobStatusChoices.STATUS_FAILED)
        self.assertContains(self.client.get(self.url), "is not on dhcp1.example.com")

    def test_needs_the_windows_server_view_permission(self):
        self.user.object_permissions.all().delete()
        ObjectPermission.objects.all().delete()
        self.add_permissions("netbox_dhcp.view_subnet")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.post(self.url)
        self.assertFalse(lease_jobs(self.subnet, self.server).exists())

    def test_object_permissions_on_the_server_are_respected(self):
        ObjectPermission.objects.all().delete()
        self.add_permissions("netbox_dhcp.view_subnet")
        perm = ObjectPermission.objects.create(
            name="other servers", actions=["view"], constraints={"pk": self.server.pk + 1000}
        )
        perm.object_types.add(ObjectType.objects.get_for_model(WindowsServer))
        perm.users.add(self.user)
        self.assertFalse(WindowsServer.objects.restrict(self.user, "view").exists())
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(lease_jobs(self.subnet, self.server).exists())
        self.assertContains(self.client.get(self.url), "does not belong to a Windows server you can view")
