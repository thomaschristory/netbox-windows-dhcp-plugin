"""Issue #103: a credential profile pins the transport, port, Kerberos realm and SSH host key its servers may use."""

import uuid
from unittest import mock

from core.choices import JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job
from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from netbox_windows_dhcp_plugin import conf
from netbox_windows_dhcp_plugin.choices import TransportChoices
from netbox_windows_dhcp_plugin.conf import CredentialError
from netbox_windows_dhcp_plugin.connection import (
    build_client,
    build_failover_client,
    build_runner,
    check_connection_allowed,
)
from netbox_windows_dhcp_plugin.jobs import TestConnectionJob
from netbox_windows_dhcp_plugin.models import WindowsServer
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .fixtures import windows_server

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGbM5L1hQdQHwPVzFXDyOcnRTMUXN0zXV0fRSC0c0b7w"
OTHER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINPId/9cRTqDqanPcCm3ESFRFp0vxBnfZ0+ZIkyc8tiF"


def config(**pins):
    profiles = {
        "lab": {"username": "lab\\admin", "password": "x", "allowed_hosts": ["*.example.com"], **pins},
    }
    return {**settings.PLUGINS_CONFIG, "netbox_windows_dhcp_plugin": {"credentials": profiles}}


def server(**kwargs):
    # Not saved: the checks only read the connection fields.
    kwargs.setdefault("host", "dhcp1.example.com")
    return WindowsServer(name="dhcp1", credential_profile="lab", **kwargs)


class ServerCleanTest(TestCase):
    """The form and the API refuse a server the profile does not allow, on the field that breaks it."""

    def assert_field_error(self, field, srv, message=""):
        with self.assertRaises(ValidationError) as ctx:
            srv.full_clean()
        self.assertIn(field, ctx.exception.message_dict)
        if message:
            self.assertIn(message, " ".join(ctx.exception.message_dict[field]))

    def test_no_pins_change_nothing(self):
        with override_settings(PLUGINS_CONFIG=config()):
            server(transport="ssh", port=2222, ssh_host_key=KEY).full_clean()
            server(winrm_auth="kerberos", kerberos_realm="ANY.EXAMPLE").full_clean()

    def test_transport(self):
        with override_settings(PLUGINS_CONFIG=config(transports=["winrm"])):
            server().full_clean()
            self.assert_field_error("transport", server(transport="ssh"), "may not be used over ssh")

    def test_port_including_the_default(self):
        with override_settings(PLUGINS_CONFIG=config(ports=[5986])):
            server().full_clean()  # no port: 5986 for WinRM over HTTPS
            server(port=5986).full_clean()
            self.assert_field_error("port", server(port=15986), "port 15986")
            self.assert_field_error("port", server(winrm_https=False), "port 5985")

    def test_kerberos_realm(self):
        with override_settings(PLUGINS_CONFIG=config(kerberos_realms=["corp.example"])):
            server(winrm_auth="kerberos", kerberos_realm="Corp.Example").full_clean()
            self.assert_field_error("kerberos_realm", server(winrm_auth="kerberos", kerberos_realm="EVIL.EXAMPLE"))
            # NTLM does not use the realm.
            server(kerberos_realm="EVIL.EXAMPLE").full_clean()

    def test_ssh_host_key(self):
        with override_settings(PLUGINS_CONFIG=config(ssh_host_keys=[KEY])):
            server(transport="ssh", ssh_host_key=f"{KEY} dhcp1").full_clean()
            server(transport="ssh").full_clean()  # the connection trusts only the profile's keys
            self.assert_field_error("ssh_host_key", server(transport="ssh", ssh_host_key=OTHER_KEY), "does not trust")
        with override_settings(PLUGINS_CONFIG=config(ssh_host_keys=[])):
            self.assert_field_error("ssh_host_key", server(transport="ssh"), "trusts no SSH host key")

    def test_malformed_profile_is_a_credential_profile_error(self):
        with override_settings(PLUGINS_CONFIG=config(transports="winrm")):
            self.assert_field_error("credential_profile", server(), "transports must be a list")

    def test_unknown_transport_is_not_a_crash(self):
        with override_settings(PLUGINS_CONFIG=config(ports=[5986])):
            self.assert_field_error("transport", server(transport="telnet"))


class ConnectionCheckTest(SimpleTestCase):
    """Checked again before every connection: a record changed in the database, or a profile tightened later."""

    def test_each_pin_is_enforced(self):
        cases = [
            (config(transports=["winrm"]), server(transport="ssh"), "may not be used over ssh"),
            (config(ports=[5986]), server(port=443), "may not be used with port 443"),
            (
                config(kerberos_realms=["CORP.EXAMPLE"]),
                server(winrm_auth="kerberos", kerberos_realm="EVIL.EXAMPLE"),
                "Kerberos realm EVIL.EXAMPLE",
            ),
            (config(ssh_host_keys=[KEY]), server(transport="ssh", ssh_host_key=OTHER_KEY), "does not trust"),
            (config(ports=["5986"]), server(), "invalid entry '5986'"),
        ]
        for profile, srv, message in cases:
            with self.subTest(message=message), override_settings(PLUGINS_CONFIG=profile):
                with self.assertRaisesMessage(CredentialError, message):
                    check_connection_allowed(srv)
                # Every client goes through the same check, even with a substituted runner.
                with self.assertRaisesMessage(CredentialError, message):
                    build_client(srv, runner=object())
                with self.assertRaisesMessage(CredentialError, message):
                    build_runner(srv)

    @override_settings(PLUGINS_CONFIG=config(kerberos_realms=["CORP.EXAMPLE"]))
    def test_failover_client_is_checked_too(self):
        srv = server(winrm_auth="kerberos", kerberos_realm="EVIL.EXAMPLE", failover_auth="credssp")
        with self.assertRaisesMessage(CredentialError, "Kerberos realm EVIL.EXAMPLE"):
            build_failover_client(srv, runner=object())
        srv.kerberos_realm = "corp.example"
        runner = build_runner(srv, auth="credssp")
        # The CredSSP user name borrows the (allowed) realm.
        self.assertEqual(runner.cfg.username, "lab\\admin")
        self.assertIsNotNone(build_failover_client(srv, runner=object()))

    @override_settings(PLUGINS_CONFIG=config(ssh_host_keys=[KEY, OTHER_KEY]))
    def test_ssh_runner_trusts_only_the_profile_keys(self):
        runner = build_runner(server(transport=TransportChoices.SSH))
        self.assertEqual(runner.cfg.trusted_host_keys, (KEY, OTHER_KEY))
        self.assertEqual(runner.cfg.host_key, "")
        runner = build_runner(server(transport=TransportChoices.SSH, ssh_host_key=OTHER_KEY))
        self.assertEqual(runner.cfg.host_key, OTHER_KEY)

    @override_settings(PLUGINS_CONFIG=config())
    def test_without_the_key_the_ssh_runner_keeps_known_hosts(self):
        with mock.patch("netbox_windows_dhcp_plugin.windows.ssh.SSHRunner._load_known_hosts") as load:
            runner = build_runner(server(transport=TransportChoices.SSH))
        self.assertIsNone(runner.cfg.trusted_host_keys)
        load.assert_called_once()

    def test_transport_names_match_the_choices(self):
        self.assertEqual(set(conf.TRANSPORTS), set(TransportChoices.values()))


class ConnectionJobTest(TestCase):
    def setUp(self):
        self.server = windows_server()
        self.fake = FakeDhcpServer()
        TestConnectionJob.runner_factory = staticmethod(lambda server: self.fake)
        self.addCleanup(setattr, TestConnectionJob, "runner_factory", None)

    def test_port_changed_behind_the_form_never_connects(self):
        WindowsServer.objects.filter(pk=self.server.pk).update(port=8080)
        self.server.refresh_from_db()
        job = Job.objects.create(
            object=self.server, name="test", status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4()
        )
        with override_settings(PLUGINS_CONFIG=config(ports=[5986])), self.assertRaises(JobFailed):
            TestConnectionJob(job).run()
        self.assertEqual(self.fake.calls, [])
        self.assertIn("may not be used with port 8080", self.server.sync_runs.get().error)
