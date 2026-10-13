from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from netbox_windows_dhcp_plugin.conf import (
    CredentialError,
    check_host_allowed,
    get_credentials,
    profiles_without_allowed_hosts,
)

from .fixtures import dhcp_servers, windows_server


class CredentialProfileTest(SimpleTestCase):
    profiles = {
        "inline": {"username": "svc", "password": "secret"},
        "env": {"username": "svc", "password_env": "DHCP_TEST_PASSWORD"},
    }

    def test_inline(self):
        creds = get_credentials("inline", self.profiles, environ={})
        self.assertEqual((creds.username, creds.password), ("svc", "secret"))

    def test_env(self):
        creds = get_credentials("env", self.profiles, environ={"DHCP_TEST_PASSWORD": "from-env"})
        self.assertEqual(creds.password, "from-env")

    def test_missing_env_var(self):
        with self.assertRaises(CredentialError):
            get_credentials("env", self.profiles, environ={})

    def test_unknown_profile(self):
        with self.assertRaises(CredentialError):
            get_credentials("nope", self.profiles, environ={})

    def test_repr_hides_secrets(self):
        creds = get_credentials("inline", self.profiles, environ={})
        self.assertNotIn("secret", repr(creds))


class AllowedHostsTest(SimpleTestCase):
    profiles = {
        "open": {"username": "svc"},
        "corp": {"username": "svc", "allowed_hosts": ["dhcp*.corp.example", "10.0.0.5"]},
        "none": {"username": "svc", "allowed_hosts": []},
        "typo": {"username": "svc", "allowed_hosts": "dhcp01.corp.example"},
    }

    def test_profile_without_list_allows_any_host(self):
        check_host_allowed("open", "anything.example", profiles=self.profiles)

    def test_matching_hosts_are_allowed(self):
        check_host_allowed("corp", "DHCP01.corp.example.", "10.0.0.5", "", profiles=self.profiles)

    def test_other_host_is_refused(self):
        with self.assertRaisesMessage(CredentialError, "may not be used with host 'evil.example'"):
            check_host_allowed("corp", "dhcp01.corp.example", "evil.example", profiles=self.profiles)

    def test_wildcard_stays_within_one_label(self):
        with self.assertRaises(CredentialError):
            check_host_allowed("corp", "dhcp.evil.example.corp.example", profiles=self.profiles)

    def test_values_a_url_parser_reads_differently_are_refused(self):
        # Each would match "dhcp*.corp.example" character by character, but a URL parser connects to evil.example.
        for host in (
            "dhcp.evil.example#.corp.example",
            "dhcp.evil.example/.corp.example",
            "dhcp@evil.example?.corp.example",
            "dhcp01.corp.example:5986",
            " dhcp01.corp.example",
        ):
            with self.subTest(host=host), self.assertRaisesMessage(CredentialError, "not a valid host"):
                check_host_allowed("corp", host, profiles=self.profiles)

    def test_ip_addresses_are_compared_canonically(self):
        profiles = {"v6": {"allowed_hosts": ["2001:DB8::1"]}}
        check_host_allowed("v6", "2001:db8:0::1", profiles=profiles)

    def test_empty_list_allows_nothing(self):
        with self.assertRaises(CredentialError):
            check_host_allowed("none", "dhcp01.corp.example", profiles=self.profiles)

    def test_string_is_rejected(self):
        with self.assertRaisesMessage(CredentialError, "must be a list"):
            check_host_allowed("typo", "d", profiles=self.profiles)

    def test_unrestricted_profiles_are_listed(self):
        self.assertEqual(profiles_without_allowed_hosts(self.profiles), ["open"])


@override_settings(
    PLUGINS_CONFIG={
        **settings.PLUGINS_CONFIG,
        "netbox_windows_dhcp_plugin": {"credentials": {"lab": {"allowed_hosts": ["*.example.com"]}}},
    }
)
class ServerValidationTest(TestCase):
    def setUp(self):
        self.server = windows_server(dhcp_servers(1)[0])

    def test_allowed_host_validates(self):
        self.server.full_clean()

    def test_host_outside_profile_is_rejected(self):
        self.server.host = "attacker.test"
        with self.assertRaises(ValidationError) as ctx:
            self.server.full_clean()
        self.assertIn("host", ctx.exception.message_dict)

    def test_host_with_url_parts_is_rejected(self):
        self.server.host = "dhcp1.example.com:5986"
        with self.assertRaises(ValidationError) as ctx:
            self.server.full_clean()
        self.assertIn("host", ctx.exception.message_dict)

    def test_computer_name_outside_profile_is_rejected(self):
        self.server.computer_name = "attacker.test"
        with self.assertRaises(ValidationError) as ctx:
            self.server.full_clean()
        self.assertIn("computer_name", ctx.exception.message_dict)

    def test_ssh_cannot_skip_host_key_check(self):
        self.server.transport = "ssh"
        self.server.verify_tls = False
        with self.assertRaises(ValidationError) as ctx:
            self.server.full_clean()
        self.assertIn("verify_tls", ctx.exception.message_dict)

    def test_basic_auth_needs_tls_verification(self):
        self.server.winrm_auth = "basic"
        self.server.verify_tls = False
        with self.assertRaises(ValidationError) as ctx:
            self.server.full_clean()
        self.assertIn("verify_tls", ctx.exception.message_dict)
