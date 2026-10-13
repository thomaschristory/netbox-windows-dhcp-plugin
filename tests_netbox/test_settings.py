from django.test import SimpleTestCase

from netbox_windows_dhcp_plugin.conf import CredentialError, get_credentials


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
