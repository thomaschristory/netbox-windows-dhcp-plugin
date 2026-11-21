"""Issue #105: WinRM ignores the worker's proxy variables unless the credential profile sets "proxy": "env"."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from netbox_windows_dhcp_plugin.conf import CredentialError
from netbox_windows_dhcp_plugin.connection import (
    build_client,
    build_runner,
    check_connection_allowed,
    failover_secret_lookup,
)
from netbox_windows_dhcp_plugin.models import FailoverRelationship, WindowsServer

from .fixtures import windows_server


def config(lab=None, ha=None):
    profiles = {
        "lab": {"username": "lab\\admin", "password": "x", "allowed_hosts": ["*.example.com"], **(lab or {})},
        "ha": {"secret": "s3", "allowed_hosts": ["*.example.com"], **(ha or {})},
    }
    return override_settings(
        PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, "netbox_windows_dhcp_plugin": {"credentials": profiles}}
    )


def server(**kwargs):
    # Not saved: the checks only read the connection fields.
    kwargs.setdefault("host", "dhcp1.example.com")
    return WindowsServer(name="dhcp1", credential_profile="lab", **kwargs)


class ServerCleanTest(TestCase):
    def test_accepted_values(self):
        for lab in ({}, {"proxy": "none"}, {"proxy": "env"}):
            with self.subTest(lab=lab), config(lab=lab):
                server().full_clean()
                server(transport="ssh", port=22).full_clean()

    def test_malformed_value_is_a_credential_profile_error(self):
        for value in ("ENV", True, None, "http://proxy.example.com:3128"):
            with self.subTest(value=value), config(lab={"proxy": value}):
                with self.assertRaises(ValidationError) as ctx:
                    server().full_clean()
                self.assertIn("proxy must be", " ".join(ctx.exception.message_dict["credential_profile"]))


class ConnectionTest(SimpleTestCase):
    def test_runner_connects_directly_by_default(self):
        for lab in ({}, {"proxy": "none"}):
            with self.subTest(lab=lab), config(lab=lab):
                self.assertEqual(build_runner(server()).cfg.proxy, "none")

    def test_env_reaches_the_winrm_runner(self):
        with config(lab={"proxy": "env"}):
            self.assertEqual(build_runner(server()).cfg.proxy, "env")

    def test_failover_client_follows_the_server_profile(self):
        # The failover logon is the same connection with another method, so the server's own profile decides.
        srv = server(failover_auth="credssp")
        with config(lab={"proxy": "env"}):
            self.assertEqual(build_runner(srv, auth="credssp").cfg.proxy, "env")
        with config():
            self.assertEqual(build_runner(srv, auth="credssp").cfg.proxy, "none")

    def test_malformed_value_is_refused_before_connecting(self):
        with config(lab={"proxy": "yes"}):
            for call in (check_connection_allowed, lambda s: build_client(s, runner=object()), build_runner):
                with self.assertRaisesMessage(CredentialError, "credential profile 'lab': proxy must be"):
                    call(server())
            # SSH never uses a proxy, but a typo is still a typo.
            with self.assertRaisesMessage(CredentialError, "proxy must be"):
                build_runner(server(transport="ssh", port=22))


class FailoverSecretTest(TestCase):
    """The secret travels over the primary's connection: through the proxy only with the secret profile's consent."""

    def setUp(self):
        self.server = windows_server()
        self.partner = windows_server("dhcp2")
        self.relationship = FailoverRelationship.objects.create(
            name="ha", primary_server=self.server, partner_server=self.partner, shared_secret_profile="ha"
        )
        self.lookup = failover_secret_lookup(self.server)

    def test_secret_profile_must_agree_to_the_proxy(self):
        refused = "failover ha: credential profile 'ha' does not set proxy \"env\""
        with config(lab={"proxy": "env"}), self.assertRaisesMessage(ValueError, refused):
            self.lookup("ha")
        with config(lab={"proxy": "env"}, ha={"proxy": "env"}):
            self.assertEqual(self.lookup("ha"), "s3")
        # A direct server connection needs nothing from the secret profile.
        with config():
            self.assertEqual(self.lookup("ha"), "s3")
        with config(ha={"proxy": "env"}):
            self.assertEqual(self.lookup("ha"), "s3")

    def test_malformed_secret_profile_value(self):
        with config(ha={"proxy": "ENV"}), self.assertRaisesMessage(ValueError, "'ha': proxy must be"):
            self.lookup("ha")

    def test_checked_when_the_relationship_is_created(self):
        new = FailoverRelationship(
            name="ha2", primary_server=self.server, partner_server=windows_server("dhcp3"), shared_secret_profile="ha"
        )
        with config(lab={"proxy": "env"}), self.assertRaises(ValidationError) as ctx:
            new.full_clean()
        self.assertIn('does not set proxy "env"', " ".join(ctx.exception.message_dict["shared_secret_profile"]))
        with config(lab={"proxy": "env"}, ha={"proxy": "env"}):
            new.full_clean()

    def test_ssh_server_ignores_the_proxy_key(self):
        WindowsServer.objects.filter(pk=self.server.pk).update(transport="ssh", port=22)
        self.server.refresh_from_db()
        with config(lab={"proxy": "env"}):
            self.assertEqual(self.lookup("ha"), "s3")


class ProxyMessageTest(TestCase):
    """A proxy URL often holds a password: it never appears in an error, and the error lands on the right field."""

    SECRET_URL = "http://svc:S3cret-pw@proxy.example.com:3128"

    def setUp(self):
        self.server = windows_server()
        FailoverRelationship.objects.create(
            name="ha", primary_server=self.server, partner_server=windows_server("dhcp2"), shared_secret_profile="ha"
        )
        self.lookup = failover_secret_lookup(self.server)

    def test_server_form_never_shows_a_proxy_url(self):
        with config(lab={"proxy": self.SECRET_URL}), self.assertRaises(ValidationError) as ctx:
            server().full_clean()
        self.assertNotIn("S3cret-pw", str(ctx.exception))
        self.assertIn("a proxy URL is not accepted", str(ctx.exception))

    def test_failover_lookup_never_shows_a_proxy_url(self):
        with config(lab={"proxy": "env"}, ha={"proxy": self.SECRET_URL}), self.assertRaises(ValueError) as ctx:
            self.lookup("ha")
        self.assertNotIn("S3cret-pw", str(ctx.exception))

    def test_malformed_server_profile_is_reported_on_the_primary_server(self):
        new = FailoverRelationship(
            name="ha2", primary_server=self.server, partner_server=windows_server("dhcp3"), shared_secret_profile="ha"
        )
        with config(lab={"proxy": "ENV"}), self.assertRaises(ValidationError) as ctx:
            new.full_clean()
        errors = ctx.exception.message_dict
        self.assertIn("credential profile 'lab'", " ".join(errors["primary_server"]))
        self.assertNotIn("shared_secret_profile", errors)
