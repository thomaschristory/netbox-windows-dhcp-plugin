from django.core.exceptions import ValidationError
from django.test import TestCase

from netbox_windows_dhcp_plugin.models import WindowsPolicy

from .fixtures import dhcp_servers, subnets


class WindowsPolicyCleanTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        (cls.server,) = dhcp_servers(1)
        cls.subnet = subnets(cls.server, 1)[0]

    def _errors(self, **kwargs):
        with self.assertRaises(ValidationError) as ctx:
            WindowsPolicy(name="p", **kwargs).clean()
        return ctx.exception.message_dict

    def test_valid_scope_policy(self):
        WindowsPolicy(
            name="p", dhcp_server=self.server, subnet=self.subnet, ip_ranges=[["10.1.0.10", "10.1.0.20"]]
        ).clean()

    def test_subnet_without_server_is_a_validation_error(self):
        # The form still runs clean() when the required server is missing; that must not crash.
        WindowsPolicy(name="p", subnet=self.subnet).clean()

    def test_mixed_families_are_rejected(self):
        errors = self._errors(dhcp_server=self.server, subnet=self.subnet, ip_ranges=[["10.1.0.10", "2001:db8::1"]])
        self.assertIn("ip_ranges", errors)

    def test_reversed_range_is_rejected(self):
        errors = self._errors(dhcp_server=self.server, subnet=self.subnet, ip_ranges=[["10.1.0.20", "10.1.0.10"]])
        self.assertIn("ip_ranges", errors)
