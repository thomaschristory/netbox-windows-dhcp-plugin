"""
Issue #103: a credential profile pins the transport, port, Kerberos realm and SSH host key its servers may use.
Issue #147: it can also require WinRM over HTTPS with certificate verification.
"""

import uuid
from unittest import mock

from core.choices import JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job
from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from utilities.testing import APITestCase

from netbox_windows_dhcp_plugin import conf
from netbox_windows_dhcp_plugin.choices import TransportChoices
from netbox_windows_dhcp_plugin.conf import CredentialError
from netbox_windows_dhcp_plugin.connection import (
    build_client,
    build_failover_client,
    build_runner,
    check_connection_allowed,
    failover_secret_lookup,
)
from netbox_windows_dhcp_plugin.jobs import TestConnectionJob
from netbox_windows_dhcp_plugin.models import FailoverRelationship, WindowsServer
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .fixtures import PluginAPIMixin, windows_server

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
            # Without a realm, the form's own message comes first.
            self.assert_field_error(
                "kerberos_realm", server(winrm_auth="kerberos"), "A realm is required for Kerberos authentication."
            )

    def test_ssh_host_key(self):
        with override_settings(PLUGINS_CONFIG=config(ssh_host_keys=[KEY])):
            server(transport="ssh", ssh_host_key=f"{KEY} dhcp1").full_clean()
            server(transport="ssh").full_clean()  # the connection trusts only the profile's keys
            self.assert_field_error("ssh_host_key", server(transport="ssh", ssh_host_key=OTHER_KEY), "does not trust")
            self.assert_field_error("ssh_host_key", server(transport="ssh", ssh_host_key="junk"), "not a valid")
        with override_settings(PLUGINS_CONFIG=config(ssh_host_keys=[])):
            self.assert_field_error("ssh_host_key", server(transport="ssh"), "trusts no SSH host key")

    def test_require_verified_https(self):
        # Issue #147: whatever the logon, WinRM with this profile must use HTTPS and check the certificate.
        with override_settings(PLUGINS_CONFIG=config(require_verified_https=True)):
            server().full_clean()
            server(winrm_auth="kerberos", kerberos_realm="CORP.EXAMPLE").full_clean()
            self.assert_field_error("winrm_https", server(winrm_https=False), "WinRM must use HTTPS, not HTTP")
            self.assert_field_error(
                "winrm_https",
                server(winrm_https=False, winrm_auth="kerberos", kerberos_realm="CORP.EXAMPLE"),
                "credential profile 'lab' sets require_verified_https",
            )
            self.assert_field_error("verify_tls", server(verify_tls=False), "must verify the server's certificate")
            # The failover logon uses the same connection settings.
            self.assert_field_error(
                "winrm_https", server(winrm_https=False, failover_auth="credssp"), "require_verified_https"
            )
            # SSH needs the profile's host keys, or an editor could pin a key they chose.
            self.assert_field_error(
                "transport",
                server(transport="ssh", ssh_host_key=KEY),
                "require_verified_https, which needs ssh_host_keys for SSH",
            )
        with override_settings(PLUGINS_CONFIG=config(require_verified_https=True, ssh_host_keys=[KEY])):
            server(transport="ssh", winrm_https=False).full_clean()
            self.assert_field_error("ssh_host_key", server(transport="ssh", ssh_host_key=OTHER_KEY), "does not trust")
        with override_settings(PLUGINS_CONFIG=config(require_verified_https=False)):
            server(winrm_https=False).full_clean()
            server(verify_tls=False).full_clean()
        with override_settings(PLUGINS_CONFIG=config(require_verified_https="yes")):
            self.assert_field_error("credential_profile", server(), "require_verified_https must be true or false")
            # Malformed is refused whatever the transport, like the other keys.
            self.assert_field_error("credential_profile", server(transport="ssh"), "true or false")

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
            # A record written straight to the database: Kerberos without a realm.
            (
                config(kerberos_realms=["CORP.EXAMPLE"], username="svc@CORP.EXAMPLE"),
                server(winrm_auth="kerberos"),
                "Kerberos realm (none)",
            ),
            (config(ports=["5986"]), server(), "invalid entry '5986'"),
            # Issue #147: a record saved before the profile asked for it, or written straight to the database.
            (config(require_verified_https=True), server(winrm_https=False), "WinRM must use HTTPS, not HTTP"),
            (
                config(require_verified_https=True),
                server(winrm_auth="kerberos", kerberos_realm="CORP.EXAMPLE", verify_tls=False),
                "WinRM must verify the server's certificate",
            ),
            (config(require_verified_https=1), server(), "require_verified_https must be true or false"),
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

    @override_settings(PLUGINS_CONFIG=config(kerberos_realms=["CORP.EXAMPLE"], username="admin"))
    def test_failover_client_is_checked_too(self):
        srv = server(winrm_auth="kerberos", kerberos_realm="EVIL.EXAMPLE", failover_auth="credssp")
        with self.assertRaisesMessage(CredentialError, "Kerberos realm EVIL.EXAMPLE"):
            build_failover_client(srv, runner=object())
        srv.kerberos_realm = " corp.example "
        runner = build_runner(srv, auth="credssp")
        # The CredSSP user name borrows the (allowed) realm, in the form the check read it.
        self.assertEqual(runner.cfg.username, "admin@CORP.EXAMPLE")
        self.assertEqual(runner.cfg.kerberos_realm, "CORP.EXAMPLE")
        self.assertIsNotNone(build_failover_client(srv, runner=object()))

    def test_require_verified_https_covers_the_failover_client(self):
        for fields, message in (
            ({"winrm_https": False}, "WinRM must use HTTPS, not HTTP"),
            ({"verify_tls": False}, "WinRM must verify the server's certificate"),
        ):
            srv = server(failover_auth="credssp", **fields)
            # The lab profile's own consent to insecure CredSSP does not lift require_verified_https.
            profile = config(require_verified_https=True, allow_insecure_credssp=True)
            with self.subTest(fields=fields), override_settings(PLUGINS_CONFIG=profile):
                with self.assertRaisesMessage(CredentialError, message):
                    build_failover_client(srv, runner=object())
                with self.assertRaisesMessage(CredentialError, message):
                    build_runner(srv, auth="credssp")
        with override_settings(PLUGINS_CONFIG=config(require_verified_https=True)):
            self.assertIsNotNone(build_failover_client(server(failover_auth="credssp"), runner=object()))

    @override_settings(PLUGINS_CONFIG=config(require_verified_https=True))
    def test_ssh_needs_the_profile_host_keys(self):
        # A key pinned on the server alone is the editor's choice, not the profile owner's.
        srv = server(transport=TransportChoices.SSH, winrm_https=False, ssh_host_key=KEY)
        message = "credential profile 'lab' sets require_verified_https, which needs ssh_host_keys for SSH"
        with self.assertRaisesMessage(CredentialError, message):
            check_connection_allowed(srv)
        with self.assertRaisesMessage(CredentialError, message):
            build_runner(srv)
        with override_settings(PLUGINS_CONFIG=config(require_verified_https=True, ssh_host_keys=[KEY])):
            check_connection_allowed(srv)
            self.assertEqual(build_runner(srv).cfg.trusted_host_keys, (KEY,))

    @override_settings(PLUGINS_CONFIG=config(require_verified_https=True))
    def test_allowed_hosts_is_checked_first(self):
        # The host check runs before the profile's other rules, and neither message holds a secret.
        srv = server(host="evil.example.org", winrm_https=False)
        with self.assertRaisesMessage(CredentialError, "may not be used with host 'evil.example.org'") as ctx:
            check_connection_allowed(srv)
        self.assertNotIn("require_verified_https", str(ctx.exception))
        with self.assertRaises(CredentialError) as ctx:
            check_connection_allowed(server(winrm_https=False))
        self.assertNotIn("lab\\admin", str(ctx.exception))
        self.assertNotIn("password", str(ctx.exception))

    @override_settings(PLUGINS_CONFIG=config(kerberos_realms=["CORP.EXAMPLE"]))
    def test_values_outside_the_choices_are_refused(self):
        # Records written straight to the database: the transport would read these its own way (WinRM lower-cases
        # the method, anything but "ssh" is WinRM) while exact comparisons in the checks would miss them.
        cases = [
            ({"transport": "WINRM"}, "unknown transport 'WINRM'"),
            ({"transport": "telnet"}, "unknown transport 'telnet'"),
            ({"winrm_auth": "KERBEROS", "kerberos_realm": "EVIL.EXAMPLE"}, "unknown winrm_auth 'KERBEROS'"),
            ({"winrm_auth": "Basic", "winrm_https": False}, "unknown winrm_auth 'Basic'"),
            ({"winrm_auth": "CREDSSP", "winrm_https": False}, "unknown winrm_auth 'CREDSSP'"),
            ({"failover_auth": "kerberos", "kerberos_realm": "EVIL.EXAMPLE"}, "unknown failover_auth 'kerberos'"),
            ({"failover_auth": "CredSSP", "winrm_https": False}, "unknown failover_auth 'CredSSP'"),
        ]
        for fields, message in cases:
            srv = server(**fields)
            with self.subTest(fields=fields):
                with self.assertRaisesMessage(CredentialError, message):
                    build_client(srv, runner=object())
                with self.assertRaisesMessage(CredentialError, message):
                    build_failover_client(srv, runner=object())
                with self.assertRaisesMessage(CredentialError, message):
                    build_runner(srv)

    def test_only_credssp_makes_a_second_client(self):
        self.assertEqual(server(failover_auth="kerberos").separate_failover_auth, "")
        self.assertEqual(server(failover_auth="CREDSSP").separate_failover_auth, "")
        self.assertEqual(server(failover_auth="credssp").separate_failover_auth, "credssp")

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

    def test_http_set_behind_the_form_never_connects(self):
        # Issue #147: a raw save that turns HTTPS off (behind the form's back) is still refused when connecting.
        WindowsServer.objects.filter(pk=self.server.pk).update(winrm_https=False)
        self.server.refresh_from_db()
        job = Job.objects.create(
            object=self.server, name="test", status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4()
        )
        profile = config(require_verified_https=True)
        with override_settings(PLUGINS_CONFIG=profile), self.assertRaises(JobFailed):
            TestConnectionJob(job).run()
        self.assertEqual(self.fake.calls, [])
        self.assertIn("sets require_verified_https", self.server.sync_runs.get().error)


class FailoverSecretPinsTest(TestCase):
    """The shared secret travels over the primary server's own connection: the secret profile's pins apply to it."""

    def setUp(self):
        self.server = windows_server()
        partner = windows_server("dhcp2")
        self.relationship = FailoverRelationship.objects.create(
            name="ha", primary_server=self.server, partner_server=partner, shared_secret_profile="ha"
        )
        self.lookup = failover_secret_lookup(self.server)

    def settings(self, lab=None, **pins):
        profiles = {
            "lab": {"username": "lab\\admin", "password": "x", "secret": "lab-s3", **(lab or {})},
            "ha": {"secret": "s3", "allowed_hosts": ["*.example.com"], **pins},
        }
        return override_settings(
            PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, "netbox_windows_dhcp_plugin": {"credentials": profiles}}
        )

    def assert_refused(self, message, **pins):
        with self.settings(**pins), self.assertRaisesMessage(ValueError, f"failover ha: {message}"):
            self.lookup("ha")

    def test_pins_apply_to_the_primary_connection(self):
        with self.settings(transports=["winrm"], ports=[5986]):
            self.assertEqual(self.lookup("ha"), "s3")
        self.assert_refused("credential profile 'ha' may not be used over winrm", transports=["ssh"])
        self.assert_refused("credential profile 'ha' may not be used with port 5986", ports=[5985])

    def test_require_verified_https_applies_to_the_primary_connection(self):
        # Issue #147: the secret profile's require_verified_https judges the primary's connection, even when the
        # server's own profile does not ask for it.
        with self.settings(require_verified_https=True):
            self.assertEqual(self.lookup("ha"), "s3")
        self.server.winrm_https = False
        self.assert_refused(
            "credential profile 'ha' sets require_verified_https: WinRM must use HTTPS, not HTTP",
            require_verified_https=True,
        )
        self.server.winrm_https, self.server.verify_tls = True, False
        self.assert_refused(
            "credential profile 'ha' sets require_verified_https: WinRM must verify", require_verified_https=True
        )
        # Over CredSSP, allow_insecure_credssp on the secret profile does not lift it.
        self.server.failover_auth = "credssp"
        self.assert_refused(
            "credential profile 'ha' sets require_verified_https",
            require_verified_https=True,
            allow_insecure_credssp=True,
        )
        self.assert_refused(
            "credential profile 'ha': require_verified_https must be true or false", require_verified_https="true"
        )
        # Over SSH the secret profile must list the host keys it trusts, and the server must pin one of them.
        self.server.transport, self.server.failover_auth = "ssh", ""
        self.server.ssh_host_key = KEY
        self.assert_refused(
            "credential profile 'ha' sets require_verified_https, which needs ssh_host_keys for SSH",
            require_verified_https=True,
        )
        with self.settings(require_verified_https=True, ssh_host_keys=[KEY]):
            self.assertEqual(self.lookup("ha"), "s3")

    def test_server_profile_require_verified_https_blocks_the_secret_too(self):
        # The secret goes out over the server's own connection, which its own profile refuses before anything.
        FailoverRelationship.objects.filter(pk=self.relationship.pk).update(shared_secret_profile="lab")
        self.server.winrm_https = False
        with (
            self.settings(lab={"require_verified_https": True}),
            self.assertRaisesMessage(ValueError, "failover ha: credential profile 'lab' sets require_verified_https"),
        ):
            self.lookup("ha")

    def test_malformed_secret_profile(self):
        self.assert_refused("credential profile 'ha': ports must be a list", ports="5986")
        self.assert_refused("credential profile 'ha': ssh_host_keys must be a list", ssh_host_keys=KEY)
        self.assert_refused("credential profile 'ha': kerberos_realms holds an invalid entry ''", kerberos_realms=[""])

    def test_kerberos_realm(self):
        self.server.winrm_auth = "kerberos"
        self.server.kerberos_realm = "EVIL.EXAMPLE"
        refused = "credential profile 'ha' may not be used with Kerberos realm"
        self.assert_refused(f"{refused} EVIL.EXAMPLE", kerberos_realms=["CORP.EXAMPLE"])
        # A record written straight to the database with no realm.
        self.server.kerberos_realm = ""
        self.assert_refused(f"{refused} (none)", kerberos_realms=["CORP.EXAMPLE"])
        self.server.kerberos_realm = "corp.example"
        with self.settings(kerberos_realms=["CORP.EXAMPLE"]):
            self.assertEqual(self.lookup("ha"), "s3")

    def test_ssh_needs_a_pinned_key_from_the_secret_profile(self):
        self.server.transport = "ssh"
        # Without a key of its own the connection trusts the server profile's keys or known_hosts, not ha's.
        self.assert_refused(
            "pin the server's SSH host key to send credential profile 'ha' over SSH", ssh_host_keys=[KEY]
        )
        self.server.ssh_host_key = OTHER_KEY
        self.assert_refused("credential profile 'ha' does not trust this SSH host key", ssh_host_keys=[KEY])
        self.server.ssh_host_key = f"{KEY} dhcp1"
        with self.settings(ssh_host_keys=[KEY]):
            self.assertEqual(self.lookup("ha"), "s3")

    def test_same_profile_as_the_server_is_not_refused_twice(self):
        FailoverRelationship.objects.filter(pk=self.relationship.pk).update(shared_secret_profile="lab")
        self.server.transport = "ssh"
        pins = {"ssh_host_keys": [KEY], "transports": ["ssh"], "ports": [22], "allowed_hosts": ["*.example.com"]}
        # The connection trusts this profile's keys already, so no pinned key is needed on the server.
        with self.settings(lab=pins):
            self.assertEqual(self.lookup("ha"), "lab-s3")

    def test_insecure_credssp_needs_the_secret_profile_consent_too(self):
        insecure = [
            {"failover_auth": "credssp", "winrm_https": False},
            {"winrm_auth": "credssp", "winrm_https": False},
            {"failover_auth": "credssp", "verify_tls": False},
            {"winrm_auth": "credssp", "verify_tls": False},
        ]
        for fields in insecure:
            with self.subTest(fields=fields):
                server = windows_server("dhcp9", **fields)
                lookup = failover_secret_lookup(server)
                FailoverRelationship.objects.filter(pk=self.relationship.pk).update(primary_server=server)
                with (
                    self.settings(),
                    self.assertRaisesMessage(ValueError, "'ha' does not set allow_insecure_credssp"),
                ):
                    lookup("ha")
                with self.settings(allow_insecure_credssp=True):
                    self.assertEqual(lookup("ha"), "s3")
                FailoverRelationship.objects.filter(pk=self.relationship.pk).update(primary_server=self.server)
                server.delete()
        # Verified HTTPS needs no consent.
        self.server.failover_auth = "credssp"
        with self.settings():
            self.assertEqual(self.lookup("ha"), "s3")

    def insecure_primary(self):
        WindowsServer.objects.filter(pk=self.server.pk).update(winrm_https=False, failover_auth="credssp")
        self.server.refresh_from_db()

    def assert_secret_refused(self, relationship, message):
        with self.settings(), self.assertRaises(ValidationError) as ctx:
            relationship.full_clean()
        self.assertIn(message, " ".join(ctx.exception.message_dict["shared_secret_profile"]))

    def test_checked_when_the_relationship_is_created(self):
        self.insecure_primary()
        partner = windows_server("dhcp3")
        new = FailoverRelationship(name="ha2", primary_server=self.server, partner_server=partner)
        new.shared_secret_profile = "ha"
        self.assert_secret_refused(new, "allow_insecure_credssp")
        with self.settings(allow_insecure_credssp=True):
            new.full_clean()
        with self.settings(transports=["ssh"]), self.assertRaises(ValidationError):
            new.full_clean()

    def test_checked_when_the_secret_profile_or_primary_changes(self):
        FailoverRelationship.objects.filter(pk=self.relationship.pk).update(shared_secret_profile="")
        self.insecure_primary()
        relationship = FailoverRelationship.objects.get(pk=self.relationship.pk)
        relationship.shared_secret_profile = "ha"
        self.assert_secret_refused(relationship, "allow_insecure_credssp")
        # Moving an existing relationship onto a server that the secret's profile does not allow.
        FailoverRelationship.objects.filter(pk=self.relationship.pk).update(
            shared_secret_profile="ha", primary_server=windows_server("dhcp3")
        )
        relationship = FailoverRelationship.objects.get(pk=self.relationship.pk)
        relationship.primary_server = WindowsServer.objects.get(pk=self.server.pk)
        self.assert_secret_refused(relationship, "allow_insecure_credssp")

    def test_a_tightened_profile_does_not_block_editing(self):
        # The relationship already exists (on Windows too): a description edit or bulk edit still saves, and the
        # push reports the refusal when it would send the secret.
        self.insecure_primary()
        relationship = FailoverRelationship.objects.get(pk=self.relationship.pk)
        relationship.description = "edited"
        with self.settings():
            relationship.full_clean()
            relationship.save()

    def test_a_relationship_read_without_its_secret_profile_behaves_as_a_full_read(self):
        # Django loads a deferred field through refresh_from_db, which remembers the stored value: unchanged, it
        # saves like a fully read relationship, and a new choice is still checked.
        self.insecure_primary()
        only = ("pk", "name", "primary_server", "partner_server")
        relationship = FailoverRelationship.objects.only(*only).get(pk=self.relationship.pk)
        with self.settings():
            relationship.full_clean()
        relationship = FailoverRelationship.objects.only(*only).get(pk=self.relationship.pk)
        relationship.shared_secret_profile = "ha"  # set without reading the stored value first
        self.assert_secret_refused(relationship, "allow_insecure_credssp")

    def test_refresh_from_db_forgets_an_edit(self):
        FailoverRelationship.objects.filter(pk=self.relationship.pk).update(shared_secret_profile="")
        relationship = FailoverRelationship.objects.get(pk=self.relationship.pk)
        FailoverRelationship.objects.filter(pk=self.relationship.pk).update(shared_secret_profile="ha")
        self.insecure_primary()
        # Read again: "ha" is now the stored value, not a choice made here.
        relationship.refresh_from_db(fields=["shared_secret_profile"])
        with self.settings():
            relationship.full_clean()
        relationship.shared_secret_profile = ""
        relationship.refresh_from_db()
        with self.settings():
            relationship.full_clean()

    def test_same_profile_as_the_server_saves(self):
        self.server.transport = "ssh"
        self.server.save()
        relationship = FailoverRelationship.objects.get(pk=self.relationship.pk)
        relationship.shared_secret_profile = "lab"
        pins = {"ssh_host_keys": [KEY], "allowed_hosts": ["*.example.com"]}
        with self.settings(lab=pins):
            relationship.full_clean()

    def test_no_primary_is_a_field_error(self):
        relationship = FailoverRelationship(name="ha2", partner_server=windows_server("dhcp3"))
        relationship.shared_secret_profile = "ha"
        with self.settings(transports=["ssh"]), self.assertRaises(ValidationError) as ctx:
            relationship.full_clean()
        self.assertIn("primary_server", ctx.exception.message_dict)
        self.assertNotIn("shared_secret_profile", ctx.exception.message_dict)

    def test_checked_when_the_relationship_is_saved(self):
        FailoverRelationship.objects.filter(pk=self.relationship.pk).update(shared_secret_profile="")
        relationship = FailoverRelationship.objects.get(pk=self.relationship.pk)
        relationship.shared_secret_profile = "ha"
        with self.settings(transports=["winrm"]):
            relationship.full_clean()
        with self.settings(transports=["ssh"]), self.assertRaises(ValidationError) as ctx:
            relationship.full_clean()
        self.assertIn("may not be used over winrm", " ".join(ctx.exception.message_dict["shared_secret_profile"]))

    def test_require_verified_https_checked_when_the_relationship_is_saved(self):
        FailoverRelationship.objects.filter(pk=self.relationship.pk).update(shared_secret_profile="")
        WindowsServer.objects.filter(pk=self.server.pk).update(verify_tls=False)
        relationship = FailoverRelationship.objects.get(pk=self.relationship.pk)
        relationship.shared_secret_profile = "ha"
        with self.settings():
            relationship.full_clean()
        with self.settings(require_verified_https=True), self.assertRaises(ValidationError) as ctx:
            relationship.full_clean()
        self.assertIn("'ha' sets require_verified_https", " ".join(ctx.exception.message_dict["shared_secret_profile"]))

    def test_relationship_saved_with_a_require_verified_https_secret_profile(self):
        # The model's save path (models/dhcp.py): a new relationship whose secret profile requires verified HTTPS.
        partner = windows_server("dhcp3")
        with self.settings(require_verified_https=True):
            new = FailoverRelationship(
                name="ha2", primary_server=self.server, partner_server=partner, shared_secret_profile="ha"
            )
            new.full_clean()
            new.save()
        self.assertEqual(FailoverRelationship.objects.get(pk=new.pk).shared_secret_profile, "ha")
        WindowsServer.objects.filter(pk=self.server.pk).update(winrm_https=False)
        refused = FailoverRelationship(
            name="ha3",
            primary_server=WindowsServer.objects.get(pk=self.server.pk),
            partner_server=partner,
            shared_secret_profile="ha",
        )
        with self.settings(require_verified_https=True), self.assertRaises(ValidationError) as ctx:
            refused.full_clean()
        self.assertIn("WinRM must use HTTPS, not HTTP", " ".join(ctx.exception.message_dict["shared_secret_profile"]))


class ServerSecretProfileCleanTest(TestCase):
    """Editing a primary server: the failover secret profile's limits are checked on the form, not only at push."""

    def setUp(self):
        self.server = windows_server()
        self.partner = windows_server("dhcp2")
        FailoverRelationship.objects.create(
            name="ha", primary_server=self.server, partner_server=self.partner, shared_secret_profile="ha"
        )

    def settings(self, **pins):
        profiles = {
            "lab": {"username": "lab\\admin", "password": "x"},
            "ha": {"secret": "s3", "allowed_hosts": ["*.example.com"], **pins},
        }
        return override_settings(
            PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, "netbox_windows_dhcp_plugin": {"credentials": profiles}}
        )

    def test_turning_https_off_on_the_primary_is_refused(self):
        with self.settings(require_verified_https=True):
            self.server.full_clean()
            self.server.winrm_https = False
            with self.assertRaises(ValidationError) as ctx:
                self.server.full_clean()
        message = " ".join(ctx.exception.message_dict["winrm_https"])
        self.assertIn("Failover shared secret: credential profile 'ha' sets require_verified_https", message)
        self.assertNotIn("s3", message)
        self.server.winrm_https, self.server.verify_tls = True, False
        with self.settings(require_verified_https=True), self.assertRaises(ValidationError) as ctx:
            self.server.full_clean()
        self.assertIn("verify_tls", ctx.exception.message_dict)
        # Without the key, nothing changes.
        with self.settings():
            self.server.full_clean()

    def test_switching_the_primary_to_ssh_is_refused(self):
        self.server.transport, self.server.ssh_host_key = "ssh", KEY
        with self.settings(require_verified_https=True), self.assertRaises(ValidationError) as ctx:
            self.server.full_clean()
        self.assertIn("needs ssh_host_keys for SSH", " ".join(ctx.exception.message_dict["transport"]))
        with self.settings(require_verified_https=True, ssh_host_keys=[KEY]):
            self.server.full_clean()

    def test_secret_only_rules_are_checked_too(self):
        # Insecure CredSSP needs the secret profile's own consent (issue #103): a form error without a field.
        self.server.failover_auth, self.server.winrm_https = "credssp", False
        with (
            override_settings(
                PLUGINS_CONFIG={
                    **settings.PLUGINS_CONFIG,
                    "netbox_windows_dhcp_plugin": {
                        "credentials": {
                            "lab": {"username": "a", "password": "x", "allow_insecure_credssp": True},
                            "ha": {"secret": "s3", "allowed_hosts": ["*.example.com"]},
                        }
                    },
                }
            ),
            self.assertRaises(ValidationError) as ctx,
        ):
            self.server.full_clean()
        self.assertIn("'ha' does not set allow_insecure_credssp", " ".join(ctx.exception.messages))

    def test_only_connection_edits_are_judged(self):
        # A secret profile tightened (or broken) after the server was saved must not block a description edit; the
        # push still refuses to send the secret.
        WindowsServer.objects.filter(pk=self.server.pk).update(winrm_https=False)
        server = WindowsServer.objects.get(pk=self.server.pk)
        server.description = "edited"
        with self.settings(require_verified_https=True):
            server.full_clean()
        with self.settings(require_verified_https="yes"):
            server.full_clean()
        # A connection field changed now is judged, even back and forth on another field.
        server.port = 5985
        with self.settings(require_verified_https=True), self.assertRaises(ValidationError) as ctx:
            server.full_clean()
        self.assertIn("winrm_https", ctx.exception.message_dict)
        # After a save, the saved values are the stored ones.
        server.save()
        server.description = "again"
        with self.settings(require_verified_https=True):
            server.full_clean()
        # A deferred field is loaded through refresh_from_db, which remembers it: like a full read, an unchanged server
        # is not judged, and a field set before reading the stored value is.
        deferred = WindowsServer.objects.defer("verify_tls").get(pk=self.server.pk)
        with self.settings(require_verified_https=True):
            deferred.full_clean()
        deferred = WindowsServer.objects.defer("verify_tls").get(pk=self.server.pk)
        deferred.verify_tls = False
        with self.settings(require_verified_https=True), self.assertRaises(ValidationError):
            deferred.full_clean()
        # refresh_from_db forgets an edit.
        server.winrm_https = True
        server.refresh_from_db()
        with self.settings(require_verified_https=True):
            server.full_clean()

    def test_partner_and_undefined_profiles_are_not_judged(self):
        # The secret rides the primary's connection only; an undefined profile is reported by the push.
        self.partner.winrm_https = False
        with self.settings(require_verified_https=True):
            self.partner.full_clean()
        FailoverRelationship.objects.update(shared_secret_profile="gone")
        self.server.winrm_https = False
        with self.settings(require_verified_https=True):
            self.server.full_clean()


class FailoverSecretAPITest(PluginAPIMixin, APITestCase):
    """Through the REST API: an edit that keeps the secret profile saves, choosing a refused one does not."""

    user_permissions = (
        "netbox_windows_dhcp_plugin.view_windowsserver",
        "netbox_windows_dhcp_plugin.change_failoverrelationship",
    )

    def setUp(self):
        super().setUp()
        self.server = windows_server(winrm_https=False, failover_auth="credssp")
        self.relationship = FailoverRelationship.objects.create(
            name="ha", primary_server=self.server, partner_server=windows_server("dhcp2"), shared_secret_profile="ha"
        )
        profiles = {
            "lab": {"username": "lab\\admin", "password": "x", "allow_insecure_credssp": True},
            "ha": {"secret": "s3", "allowed_hosts": ["*.example.com"]},
            "ha-lab": {"secret": "s3", "allowed_hosts": ["*.example.com"], "allow_insecure_credssp": True},
        }
        override = override_settings(
            PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, "netbox_windows_dhcp_plugin": {"credentials": profiles}}
        )
        override.enable()
        self.addCleanup(override.disable)

    def patch(self, data):
        url = f"/api/plugins/windows-dhcp/failover-relationships/{self.relationship.pk}/"
        return self.client.patch(url, data, format="json", **self.header)

    def test_description_edit_saves(self):
        response = self.patch({"description": "edited"})
        self.assertEqual(response.status_code, 200, response.content)

    def test_choosing_a_refusing_secret_profile_is_refused(self):
        FailoverRelationship.objects.filter(pk=self.relationship.pk).update(shared_secret_profile="ha-lab")
        response = self.patch({"shared_secret_profile": "ha"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("allow_insecure_credssp", str(response.data))
        self.assertEqual(self.patch({"shared_secret_profile": "ha-lab"}).status_code, 200)
