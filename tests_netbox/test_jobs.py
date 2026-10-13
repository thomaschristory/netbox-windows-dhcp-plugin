import uuid

from core.choices import JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job
from django.conf import settings
from django.test import TestCase, override_settings

from netbox_windows_dhcp_plugin.choices import SyncStatusChoices
from netbox_windows_dhcp_plugin.jobs import TestConnectionJob
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import Scope
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import PSError

from .fixtures import dhcp_servers, windows_server

CREDENTIALS = {
    **settings.PLUGINS_CONFIG,
    "netbox_windows_dhcp_plugin": {"credentials": {"lab": {"username": "lab\\admin", "password": "x"}}},
}


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class TestConnectionJobTest(TestCase):
    def setUp(self):
        (dhcp_server,) = dhcp_servers(1)
        self.server = windows_server(dhcp_server)
        self.fake = FakeDhcpServer()
        TestConnectionJob.runner_factory = staticmethod(lambda server: self.fake)
        self.addCleanup(setattr, TestConnectionJob, "runner_factory", None)

    def _run(self):
        job = Job.objects.create(
            object=self.server, name="test", status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4()
        )
        TestConnectionJob(job).run()
        return self.server.sync_runs.get()

    def test_success_counts_scopes(self):
        DhcpClient(self.fake).add_scope(
            Scope(name="lan", start_range="10.1.0.10", end_range="10.1.0.200", subnet_mask="255.255.255.0")
        )
        sync_run = self._run()
        self.assertEqual(sync_run.status, SyncStatusChoices.COMPLETED)
        self.assertEqual(sync_run.summary, {"ipv4_scopes": 1})

    def test_failure_is_recorded(self):
        self.fake.fail = lambda op, params: PSError(message="Access is denied", category="PermissionDenied")
        with self.assertRaises(JobFailed):
            self._run()
        sync_run = self.server.sync_runs.get()
        self.assertEqual(sync_run.status, SyncStatusChoices.FAILED)
        self.assertIn("Access is denied", sync_run.error)

    def test_rejected_settings_are_recorded(self):
        # The WinRM transport refuses a profile without a password; that must fail the run, not leave it running.
        TestConnectionJob.runner_factory = None
        profiles = {"lab": {"username": "lab\\admin"}}
        config = {**CREDENTIALS, "netbox_windows_dhcp_plugin": {"credentials": profiles}}
        with override_settings(PLUGINS_CONFIG=config), self.assertRaises(JobFailed):
            self._run()
        sync_run = self.server.sync_runs.get()
        self.assertEqual(sync_run.status, SyncStatusChoices.FAILED)
        self.assertIn("password", sync_run.error)

    def test_client_is_closed(self):
        closed = []
        self.fake.close = lambda: closed.append(True)
        self._run()
        self.assertEqual(closed, [True])

    def test_host_outside_profile_never_connects(self):
        # A host changed behind the form's back must still not receive the credentials.
        type(self.server).objects.filter(pk=self.server.pk).update(host="attacker.test")
        self.server.refresh_from_db()
        profiles = {"lab": {"username": "lab\\admin", "password": "x", "allowed_hosts": ["*.example.com"]}}
        config = {**CREDENTIALS, "netbox_windows_dhcp_plugin": {"credentials": profiles}}
        with override_settings(PLUGINS_CONFIG=config), self.assertRaises(JobFailed):
            self._run()
        self.assertEqual(self.fake.calls, [])
        self.assertIn("may not be used with host", self.server.sync_runs.get().error)


class TestConnectionViewTest(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        (dhcp_server,) = dhcp_servers(1)
        self.server = windows_server(dhcp_server)
        self.user = get_user_model().objects.create_superuser("admin", "admin@example.com", "admin")
        self.client.force_login(self.user)

    def test_button_is_shown(self):
        response = self.client.get(self.server.get_absolute_url())
        self.assertContains(response, "Test connection")

    def test_post_queues_a_job(self):
        from django.urls import reverse

        url = reverse("plugins:netbox_windows_dhcp_plugin:windowsserver_test", args=[self.server.pk])
        response = self.client.post(url)
        self.assertRedirects(response, self.server.get_absolute_url(), fetch_redirect_response=False)
        self.assertTrue(Job.objects.filter(object_id=self.server.pk, name=TestConnectionJob.name).exists())
