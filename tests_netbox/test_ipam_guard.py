"""
Edits of the core objects scopes and reservations point at (issue #102): which fields reach Windows, which plugin
objects depend on them, and the refusal for users without the DHCP permission.
"""

from dcim.models import MACAddress
from django.test import TestCase
from ipam.models import IPAddress
from netaddr import IPNetwork

from netbox_windows_dhcp_plugin import ipam_guard

from . import fixtures


class ChangedFieldsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = fixtures.windows_server()
        cls.scope = fixtures.scope(cls.server, "198.18.10.0/24")
        cls.mac = MACAddress.objects.create(mac_address="aa:bb:cc:00:00:01")
        cls.reservation = fixtures.reservation(cls.scope, "198.18.10.25/24", mac_address=cls.mac)

    def _ip(self):
        return IPAddress.objects.get(pk=self.reservation.ip_address_id)

    def test_address_change_detected(self):
        ip = self._ip()
        ip.address = IPNetwork("198.18.10.26/24")
        self.assertEqual(ipam_guard.changed_fields(ip), ("address",))

    def test_mask_only_change_ignored(self):
        ip = self._ip()
        ip.address = IPNetwork("198.18.10.25/32")
        self.assertEqual(ipam_guard.changed_fields(ip), ())

    def test_description_change_ignored(self):
        ip = self._ip()
        ip.description = "printer"
        self.assertEqual(ipam_guard.changed_fields(ip), ())

    def test_range_start_detected(self):
        rng = self.scope.ip_range
        rng.start_address = IPNetwork("198.18.10.20/24")
        self.assertEqual(ipam_guard.changed_fields(rng), ("start_address",))

    def test_prefix_length_detected(self):
        prefix = self.scope.prefix
        prefix.prefix = IPNetwork("198.18.10.0/23")
        self.assertEqual(ipam_guard.changed_fields(prefix), ("prefix",))

    def test_mac_detected(self):
        mac = MACAddress.objects.get(pk=self.mac.pk)
        mac.mac_address = "aa:bb:cc:00:00:02"
        self.assertEqual(ipam_guard.changed_fields(mac), ("mac_address",))

    def test_mac_case_only_change_ignored(self):
        mac = MACAddress.objects.get(pk=self.mac.pk)
        mac.mac_address = "AA:BB:CC:00:00:01"
        self.assertEqual(ipam_guard.changed_fields(mac), ())

    def test_new_object_ignored(self):
        self.assertEqual(ipam_guard.changed_fields(IPAddress(address=IPNetwork("198.18.10.99/24"))), ())

    def test_unwatched_model_ignored(self):
        self.assertEqual(ipam_guard.changed_fields(self.server), ())


class DependentsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server1, cls.server2 = fixtures.windows_servers(2)
        cls.scope1 = fixtures.scope(cls.server1, "198.18.20.0/24")
        # A second scope on another server sharing the first scope's prefix.
        cls.scope2 = fixtures.scope(cls.server2, "198.18.21.0/24")
        cls.scope2.prefix = cls.scope1.prefix
        cls.scope2.save()

    def test_dependents_of_prefix(self):
        [(codename, queryset)] = ipam_guard.dependents(self.scope1.prefix)
        self.assertEqual(codename, "change_scope")
        self.assertEqual(set(queryset), {self.scope1, self.scope2})

    def test_unused_ip_has_no_dependents(self):
        ip = fixtures.ip_address("198.18.20.77/24")
        self.assertEqual(ipam_guard.dependents(ip), [])
