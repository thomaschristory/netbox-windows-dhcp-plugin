"""Failover authentication (issue #141): CredSSP for the failover writes and replication only."""

import uuid

from core.choices import JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from netbox_windows_dhcp_plugin.conf import CredentialError
from netbox_windows_dhcp_plugin.connection import build_failover_client, check_connection_allowed
from netbox_windows_dhcp_plugin.jobs import PushJob, TestConnectionJob
from netbox_windows_dhcp_plugin.models import FailoverRelationship, SyncRun, WindowsServer
from netbox_windows_dhcp_plugin.models import Scope as NetBoxScope
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import op_of

from .fixtures import windows_server
from .test_jobs import CREDENTIALS
from .test_push import PushTestMixin

INSECURE_CREDSSP = {
    **CREDENTIALS,
    "netbox_windows_dhcp_plugin": {
        "credentials": {"lab": {"username": "lab\\admin", "password": "x", "allow_insecure_credssp": True}}
    },
}


class Tagged:
    """A runner over a shared fake server that records each operation it sends."""

    def __init__(self, fake, log):
        self.fake, self.log = fake, log

    def run(self, script, params=None):
        self.log.append(op_of(script))
        return self.fake.run(script, params)


def kerberos(server, **fields):
    """Kerberos for everything, CredSSP for failover writes: the setup issue #141 asks for."""
    server.winrm_auth = "kerberos"
    server.kerberos_realm = "EXAMPLE.COM"
    server.failover_auth = "credssp"
    for name, value in fields.items():
        setattr(server, name, value)
    return server


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class FailoverAuthModelTest(TestCase):
    def setUp(self):
        self.server = windows_server()

    def test_credssp_for_failover_with_verified_https_is_accepted(self):
        kerberos(self.server).full_clean()
        self.assertEqual(self.server.separate_failover_auth, "credssp")

    def test_empty_or_same_as_the_server_means_no_second_client(self):
        self.assertEqual(self.server.separate_failover_auth, "")
        self.server.winrm_auth = self.server.failover_auth = "credssp"
        self.assertEqual(self.server.separate_failover_auth, "")
        self.assertIsNone(build_failover_client(self.server, runner=FakeDhcpServer()))

    def test_ssh_refuses_a_failover_authentication(self):
        self.server.transport = "ssh"
        self.server.failover_auth = "credssp"
        with self.assertRaises(ValidationError) as cm:
            self.server.full_clean()
        self.assertIn("failover_auth", cm.exception.error_dict)
        self.assertEqual(self.server.separate_failover_auth, "")

    def test_credssp_for_failover_needs_verified_https(self):
        kerberos(self.server, winrm_https=False)
        with self.assertRaises(ValidationError) as cm:
            self.server.full_clean()
        self.assertIn("winrm_https", cm.exception.error_dict)
        kerberos(self.server, winrm_https=True, verify_tls=False)
        with self.assertRaises(ValidationError) as cm:
            self.server.full_clean()
        self.assertIn("verify_tls", cm.exception.error_dict)

    @override_settings(PLUGINS_CONFIG=INSECURE_CREDSSP)
    def test_a_lab_profile_may_allow_insecure_credssp_for_failover(self):
        kerberos(self.server, winrm_https=False).full_clean()

    def test_the_connection_check_refuses_credssp_for_failover_without_verified_https(self):
        # A record that skipped validation (changed in the database) is still refused before any connection.
        WindowsServer.objects.filter(pk=self.server.pk).update(
            winrm_auth="kerberos", kerberos_realm="EXAMPLE.COM", failover_auth="credssp", winrm_https=False
        )
        server = WindowsServer.objects.get(pk=self.server.pk)
        with self.assertRaisesRegex(CredentialError, "CredSSP"):
            check_connection_allowed(server)
        with self.assertRaisesRegex(CredentialError, "CredSSP"):
            build_failover_client(server, runner=FakeDhcpServer())


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class FailoverAuthConnectionTestJobTest(TestCase):
    def setUp(self):
        self.server = kerberos(windows_server())
        self.server.save()
        self.fake = FakeDhcpServer()
        self.own, self.failover = [], []
        TestConnectionJob.runner_factory = staticmethod(lambda server: Tagged(self.fake, self.own))
        TestConnectionJob.failover_runner_factory = staticmethod(lambda server: Tagged(self.fake, self.failover))
        self.addCleanup(setattr, TestConnectionJob, "runner_factory", None)
        self.addCleanup(setattr, TestConnectionJob, "failover_runner_factory", None)

    def _run(self):
        job = Job.objects.create(
            object=self.server, name=TestConnectionJob.name, status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4()
        )
        TestConnectionJob(job).run()
        return SyncRun.objects.get(job=job)

    def test_the_failover_logon_is_tested_too(self):
        run = self._run()
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(run.summary["failover_auth"], "credssp")
        self.assertEqual(self.failover, ["dhcp.available"])
        self.assertIn("scope.list", self.own)

    def test_a_failing_failover_logon_fails_the_test(self):
        TestConnectionJob.failover_runner_factory = staticmethod(lambda server: _Refusing())
        with self.assertRaises(JobFailed):
            self._run()
        run = SyncRun.objects.get(windows_server=self.server)
        self.assertEqual(run.status, "failed")
        self.assertIn("CredSSP refused", run.error)


class _Refusing:
    def run(self, script, params=None):
        from netbox_windows_dhcp_plugin.windows.runner import RunnerError

        raise RunnerError("winrm: CredSSP refused")


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class FailoverAuthPushTest(PushTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        kerberos(self.server).save()
        self.partner = windows_server("dhcp2")
        relationship = FailoverRelationship.objects.create(
            name="fo", primary_server=self.server, partner_server=self.partner
        )
        relationship.scopes.add(NetBoxScope.objects.get())
        self.fake.failovers["fo"] = {
            "name": "fo",
            "partner_server": "dhcp2.example.com",
            "scope_ids": ["10.1.0.0"],
            "max_client_lead_time": 3600,
        }
        self.own, self.failover = [], []
        PushJob.runner_factory = staticmethod(lambda server: Tagged(self.target, self.own))
        PushJob.failover_runner_factory = staticmethod(lambda server: Tagged(self.target, self.failover))
        self.addCleanup(setattr, PushJob, "failover_runner_factory", None)

    def _change_scope(self, **fields):
        scope = NetBoxScope.objects.get()
        scope.snapshot()
        for name, value in fields.items():
            setattr(scope, name, value)
        scope.save()

    def test_replication_goes_through_the_failover_client(self):
        self._change_scope(name="changed")
        run = self._run()
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(self.failover, ["failover.replicate"])
        self.assertIn("scope.set", self.own)
        self.assertNotIn("failover.replicate", self.own)

    def test_relationship_writes_go_through_the_failover_client(self):
        relationship = FailoverRelationship.objects.get(name="fo")
        relationship.snapshot()
        relationship.load_balance_percent = 70
        relationship.save()
        run = self._run()
        self.assertEqual(run.status, "completed", run.error)
        self.assertIn("failover.set", self.failover)
        self.assertNotIn("failover.set", self.own)
        self.assertEqual(self.fake.failovers["fo"]["load_balance_percent"], 70)

    def test_a_dry_run_opens_no_failover_client(self):
        from netbox_windows_dhcp_plugin.jobs import DryRunJob

        # DryRunJob inherits both factories from PushJob (set in setUp); setting its own would outlive the test.
        self._change_scope(name="changed")
        self._run(DryRunJob)
        self.assertEqual(self.failover, [])

    def test_without_a_failover_authentication_everything_uses_one_client(self):
        self.server.failover_auth = ""
        self.server.save()
        self._change_scope(name="changed")
        run = self._run()
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(self.failover, [])
        self.assertIn("failover.replicate", self.own)


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class FailoverRefusedPushTest(FailoverAuthPushTest):
    """A failover logon that fails (CredSSP not enabled on the server) fails the failover change only."""

    def setUp(self):
        super().setUp()
        PushJob.failover_runner_factory = staticmethod(lambda server: _Refusing())

    def test_the_rest_of_the_push_still_goes_through(self):
        relationship = FailoverRelationship.objects.get(name="fo")
        relationship.snapshot()
        relationship.load_balance_percent = 70
        relationship.save()
        self._change_scope(name="changed")
        with self.assertRaises(JobFailed):
            self._run()
        run = SyncRun.objects.filter(kind="push").latest("pk")
        self.assertIn("failover authentication (credssp): winrm: CredSSP refused", run.error)
        # Not stopped early: replication was tried, through the failover client, and waits for the next push.
        self.assertNotIn("stopped early", run.error)
        self.assertIn("failover replication fo: failover authentication (credssp)", run.error)
        self.assertEqual(run.summary["replication_pending"], {"fo": ["10.1.0.0"]})
        self.assertEqual(self.fake.scopes["10.1.0.0"]["name"], "changed")
        self.assertNotEqual(self.fake.failovers["fo"].get("load_balance_percent"), 70)

    # The parent's tests expect a working failover logon.
    test_replication_goes_through_the_failover_client = None
    test_relationship_writes_go_through_the_failover_client = None
    test_a_dry_run_opens_no_failover_client = None
    test_without_a_failover_authentication_everything_uses_one_client = None


@override_settings(
    PLUGINS_CONFIG={
        **CREDENTIALS,
        "netbox_windows_dhcp_plugin": {
            "credentials": {
                "bare": {"username": "admin", "password": "x"},
                "lab": {"username": "LAB\\admin", "password": "x"},
            }
        },
    }
)
class FailoverRunnerUserTest(TestCase):
    def test_credssp_gets_the_realm_for_a_bare_user_name(self):
        from netbox_windows_dhcp_plugin.connection import build_runner

        server = kerberos(windows_server(), credential_profile="bare")
        self.assertEqual(build_runner(server, auth="credssp").cfg.username, "admin@EXAMPLE.COM")
        self.assertEqual(build_runner(server).cfg.username, "admin")
        server.credential_profile = "lab"
        self.assertEqual(build_runner(server, auth="credssp").cfg.username, "LAB\\admin")
