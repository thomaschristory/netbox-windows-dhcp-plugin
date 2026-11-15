"""
Edits of the core objects scopes and reservations point at (issue #102): which fields reach Windows, which plugin
objects depend on them, and the refusal for users without the DHCP permission.
"""

import uuid
from unittest import mock

from core.models import ObjectType
from dcim.models import MACAddress
from django.test import TestCase
from django.urls import reverse
from ipam.models import IPAddress
from netaddr import IPNetwork
from netbox.context_managers import event_tracking
from utilities import testing as utilities_testing
from utilities.exceptions import AbortRequest
from utilities.request import NetBoxFakeRequest

from netbox_windows_dhcp_plugin import ipam_guard
from netbox_windows_dhcp_plugin.signals import push_suppressed

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


class _GuardMixin:
    """A reservation on 198.18.10.25 (server dhcp1) and a user allowed to edit IPAM and DCIM but not the plugin."""

    def setUp(self):
        super().setUp()
        self.server = fixtures.windows_server()
        self.scope = fixtures.scope(self.server, "198.18.10.0/24")
        self.mac = MACAddress.objects.create(mac_address="aa:bb:cc:00:00:01")
        self.reservation = fixtures.reservation(self.scope, "198.18.10.25/24", mac_address=self.mac)
        self.ip = self.reservation.ip_address
        self.add_permissions(
            "ipam.view_ipaddress",
            "ipam.change_ipaddress",
            "ipam.add_ipaddress",
            "ipam.view_prefix",
            "ipam.change_prefix",
            "dcim.view_macaddress",
            "dcim.change_macaddress",
            "ipam.view_vrf",
            "ipam.view_role",
        )

    def grant(self, codename, constraints=None):
        from users.models import ObjectPermission

        action = codename.split("_", 1)[0]
        model = codename.split("_", 1)[1]
        permission = ObjectPermission.objects.create(name=codename, actions=[action], constraints=constraints)
        permission.object_types.add(ObjectType.objects.get(app_label=fixtures.PLUGIN, model=model))
        permission.users.add(self.user)

    def stored_address(self):
        return str(IPAddress.objects.get(pk=self.ip.pk).address)


class GuardViewTest(_GuardMixin, utilities_testing.TestCase):
    def edit(self, address, description=""):
        url = reverse("ipam:ipaddress_edit", args=[self.ip.pk])
        return self.client.post(url, {"address": address, "status": "active", "description": description})

    def test_ui_edit_refused(self):
        self.grant("view_reservation")
        response = self.edit("198.18.10.26/24")
        self.assertEqual(response.status_code, 200)
        self.assertIn("dhcp1/198.18.10.0/198.18.10.25", response.content.decode())
        self.assertIn("change reservation", response.content.decode())
        self.assertEqual(self.stored_address(), "198.18.10.25/24")

    def test_ui_description_edit_allowed(self):
        response = self.edit("198.18.10.25/24", description="printer")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(IPAddress.objects.get(pk=self.ip.pk).description, "printer")

    def test_ui_edit_allowed_with_permission(self):
        self.grant("change_reservation")
        self.assertEqual(self.edit("198.18.10.26/24").status_code, 302)
        self.assertEqual(self.stored_address(), "198.18.10.26/24")

    def test_allowed_with_matching_constraint(self):
        self.grant("change_reservation", {"scope__server__name": "dhcp1"})
        self.assertEqual(self.edit("198.18.10.26/24").status_code, 302)

    def test_refused_with_other_constraint(self):
        self.grant("change_reservation", {"scope__server__name": "dhcp2"})
        self.assertEqual(self.edit("198.18.10.26/24").status_code, 200)
        self.assertEqual(self.stored_address(), "198.18.10.25/24")

    def test_csv_import_update_refused(self):
        url = reverse("ipam:ipaddress_bulk_import")
        data = f"id,address,status\n{self.ip.pk},198.18.10.26/24,active"
        self.client.post(url, {"data": data, "format": "csv", "csv_delimiter": ","})
        self.assertEqual(self.stored_address(), "198.18.10.25/24")

    def test_bulk_edit_mask_only_allowed(self):
        # Bulk edit cannot change an address, only its mask, which Windows never sees.
        url = reverse("ipam:ipaddress_bulk_edit")
        response = self.client.post(url, {"pk": [self.ip.pk], "mask_length": 32, "_apply": True})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored_address(), "198.18.10.25/32")

    def test_shared_prefix_needs_every_scope(self):
        other = fixtures.scope(fixtures.windows_server("dhcp2"), "198.18.11.0/24")
        other.prefix = self.scope.prefix
        other.save()
        self.grant("change_scope", {"server__name": "dhcp1"})
        self.grant("view_scope")
        url = reverse("ipam:prefix_edit", args=[self.scope.prefix_id])
        response = self.client.post(url, {"prefix": "198.18.10.0/23", "status": "active", "scope_type": ""})
        self.assertEqual(response.status_code, 200)
        self.assertIn("dhcp2/", response.content.decode())
        self.scope.prefix.refresh_from_db()
        self.assertEqual(str(self.scope.prefix.prefix), "198.18.10.0/24")

    def test_message_hides_what_the_user_cannot_view(self):
        other = fixtures.scope(fixtures.windows_server("hidden-dhcp"), "198.18.12.0/24")
        other.prefix = self.scope.prefix
        other.save()
        self.grant("change_scope", {"server__name": "dhcp1"})
        self.grant("view_scope", {"server__name": "dhcp1"})
        url = reverse("ipam:prefix_edit", args=[self.scope.prefix_id])
        response = self.client.post(url, {"prefix": "198.18.10.0/23", "status": "active", "scope_type": ""})
        content = response.content.decode()
        self.assertNotIn("hidden-dhcp", content)
        self.assertIn("1 scope you cannot view", content)
        self.assertIn("change scope", content)

    def test_constraint_checked_after_the_change_too(self):
        # Allowed on reservations in the lower half of the subnet only: moving one out of it is refused.
        self.grant("change_reservation", {"ip_address__address__net_host_contained": "198.18.10.0/25"})
        self.assertEqual(self.edit("198.18.10.200/24").status_code, 200)
        self.assertEqual(self.stored_address(), "198.18.10.25/24")
        self.assertEqual(self.edit("198.18.10.30/24").status_code, 302)


class GuardAPITest(_GuardMixin, utilities_testing.APITestCase):
    def test_api_patch_refused(self):
        url = f"/api/ipam/ip-addresses/{self.ip.pk}/"
        response = self.client.patch(url, {"address": "198.18.10.26/24"}, format="json", **self.header)
        self.assertEqual(response.status_code, 400)
        self.assertIn("change reservation", response.content.decode())
        self.assertEqual(self.stored_address(), "198.18.10.25/24")

    def test_api_mac_patch_refused(self):
        url = f"/api/dcim/mac-addresses/{self.mac.pk}/"
        response = self.client.patch(url, {"mac_address": "aa:bb:cc:00:00:02"}, format="json", **self.header)
        self.assertEqual(response.status_code, 400)

    def test_api_patch_allowed_with_permission(self):
        self.grant("change_reservation")
        url = f"/api/ipam/ip-addresses/{self.ip.pk}/"
        response = self.client.patch(url, {"address": "198.18.10.26/24"}, format="json", **self.header)
        self.assertEqual(response.status_code, 200)


class GuardContextTest(_GuardMixin, utilities_testing.TestCase):
    def _save(self):
        ip = IPAddress.objects.get(pk=self.ip.pk)
        ip.address = IPNetwork("198.18.10.26/24")
        ip.save()

    def test_no_user_allowed(self):
        # nbshell, management commands: nobody to check, the change log records the edit.
        self._save()
        self.assertEqual(self.stored_address(), "198.18.10.26/24")

    def test_script_user_checked(self):
        # Custom scripts and background requests run under event_tracking with the user who started them.
        request = NetBoxFakeRequest({"user": self.user, "id": uuid.uuid4(), "META": {}, "COOKIES": {}, "POST": {}})
        with (
            mock.patch("netbox.context_managers.flush_events"),
            event_tracking(request),
            self.assertRaises(AbortRequest),
        ):
            self._save()

    def test_push_suppressed_allowed(self):
        request = NetBoxFakeRequest({"user": self.user, "id": uuid.uuid4(), "META": {}, "COOKIES": {}, "POST": {}})
        with mock.patch("netbox.context_managers.flush_events"), event_tracking(request), push_suppressed():
            self._save()
        self.assertEqual(self.stored_address(), "198.18.10.26/24")
