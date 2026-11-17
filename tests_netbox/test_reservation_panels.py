"""
Issue #107: the pages of a device, virtual machine, interface, IP address and MAC address list the reservations that
use them, since NetBox refuses to delete those objects while a reservation does (PROTECT stays).
"""

from core.models import ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Interface, MACAddress, Manufacturer, Site
from django.db import connection
from django.test.utils import CaptureQueriesContext
from ipam.models import IPAddress
from netaddr import IPNetwork
from users.models import ObjectPermission
from utilities.testing import TestCase
from virtualization.models import VirtualMachine, VMInterface

from netbox_windows_dhcp_plugin.models import Reservation

from .fixtures import ip_address, scope, windows_server

PANEL = "DHCP reservations"


class ReservationPanelTest(TestCase):
    def setUp(self):
        super().setUp()
        site = Site.objects.create(name="site", slug="site")
        manufacturer = Manufacturer.objects.create(name="m", slug="m")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="t", slug="t")
        role = DeviceRole.objects.create(name="r", slug="r")
        self.device = Device.objects.create(name="printer", site=site, device_type=device_type, role=role)
        self.other_device = Device.objects.create(name="other", site=site, device_type=device_type, role=role)
        self.eth0 = Interface.objects.create(device=self.device, name="eth0", type="1000base-t")
        self.eth1 = Interface.objects.create(device=self.device, name="eth1", type="1000base-t")
        self.other_eth = Interface.objects.create(device=self.other_device, name="eth0", type="1000base-t")
        self.vm = VirtualMachine.objects.create(name="vm01", site=site)
        self.vm_eth = VMInterface.objects.create(virtual_machine=self.vm, name="eth0")

        self.server = windows_server("dhcp01")
        self.server2 = windows_server("dhcp02")
        self.scope = scope(self.server, "10.107.0.0/24")
        self.scope2 = scope(self.server2, "10.108.0.0/24")
        self.scope6 = scope(self.server, "2001:db8:107::/64")
        self.add_permissions(
            "dcim.view_device",
            "dcim.view_interface",
            "dcim.view_macaddress",
            "ipam.view_ipaddress",
            "virtualization.view_virtualmachine",
            "virtualization.view_vminterface",
        )

    def _ip(self, address, on=None):
        return IPAddress.objects.create(address=IPNetwork(address), assigned_object=on)

    def _mac(self, value, on=None):
        return MACAddress.objects.create(mac_address=value, assigned_object=on)

    def _page(self, obj):
        response = self.client.get(obj.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        return response

    def _allow_reservations(self):
        self.add_permissions("netbox_windows_dhcp_plugin.view_reservation")

    # -- shown and hidden --------------------------------------------------------------------------------------------

    def test_no_panel_without_a_reservation(self):
        self._allow_reservations()
        self._ip("10.107.0.50/24", on=self.eth0)
        self._mac("aa:bb:cc:00:01:07", on=self.eth0)
        for obj in (self.device, self.eth0, self.vm, self.vm_eth):
            self.assertNotContains(self._page(obj), PANEL)

    def test_device_lists_reservations_through_its_interfaces(self):
        self._allow_reservations()
        # By IP address on eth0, by MAC address only on eth1 (its IP address is not on the device), DHCPv6 on eth0.
        by_ip = Reservation.objects.create(
            scope=self.scope, ip_address=self._ip("10.107.0.50/24", on=self.eth0), client_id="01-02", name="by-ip"
        )
        by_mac = Reservation.objects.create(
            scope=self.scope2,
            ip_address=ip_address("10.108.0.51/24"),
            mac_address=self._mac("aa:bb:cc:00:01:07", on=self.eth1),
            name="by-mac",
        )
        v6 = Reservation.objects.create(
            scope=self.scope6, ip_address=self._ip("2001:db8:107::10/64", on=self.eth0), duid="00-01", iaid=1, name="v6"
        )
        Reservation.objects.create(
            scope=self.scope, ip_address=self._ip("10.107.0.60/24", on=self.other_eth), client_id="03", name="elsewhere"
        )
        response = self._page(self.device)
        self.assertContains(response, PANEL)
        for held in (by_ip, by_mac, v6):
            self.assertContains(response, f'<a href="{held.get_absolute_url()}">{held.name}</a>')
        self.assertContains(response, f'<a href="{self.server2.get_absolute_url()}">dhcp02</a>')
        self.assertContains(response, "aa:bb:cc:00:01:07".upper())
        self.assertNotContains(response, "elsewhere")
        # A reservation whose IP and MAC are both on the device is listed once.
        by_ip.mac_address = self._mac("aa:bb:cc:00:01:08", on=self.eth0)
        by_ip.save()
        self.assertContains(self._page(self.device), f'<a href="{by_ip.get_absolute_url()}">', count=1)

    def test_virtual_machine_and_its_interface(self):
        self._allow_reservations()
        held = Reservation.objects.create(
            scope=self.scope,
            ip_address=ip_address("10.107.0.70/24"),
            mac_address=self._mac("aa:bb:cc:00:01:70", on=self.vm_eth),
            name="vm-res",
        )
        for obj in (self.vm, self.vm_eth):
            self.assertContains(self._page(obj), f'<a href="{held.get_absolute_url()}">vm-res</a>')
        self.assertNotContains(self._page(self.device), PANEL)

    def test_interface_ip_address_and_mac_address_pages(self):
        self._allow_reservations()
        address = self._ip("10.107.0.80/24", on=self.eth0)
        mac = self._mac("aa:bb:cc:00:01:80", on=self.eth1)
        held = Reservation.objects.create(scope=self.scope, ip_address=address, mac_address=mac, name="iface-res")
        for obj in (self.eth0, self.eth1, address, mac):
            self.assertContains(self._page(obj), f'<a href="{held.get_absolute_url()}">iface-res</a>')
        self.assertNotContains(self._page(self.other_eth), PANEL)
        self.assertNotContains(self._page(self._ip("10.107.0.81/24")), PANEL)

    # -- permissions -------------------------------------------------------------------------------------------------

    def test_panel_needs_the_view_permission(self):
        Reservation.objects.create(
            scope=self.scope, ip_address=self._ip("10.107.0.90/24", on=self.eth0), client_id="09", name="out-of-sight"
        )
        response = self._page(self.device)
        self.assertNotContains(response, PANEL)
        self.assertNotContains(response, "out-of-sight")

    def test_object_permissions_are_respected(self):
        Reservation.objects.create(
            scope=self.scope, ip_address=self._ip("10.107.0.91/24", on=self.eth0), client_id="10", name="mine"
        )
        Reservation.objects.create(
            scope=self.scope2, ip_address=self._ip("10.108.0.92/24", on=self.eth0), client_id="11", name="theirs"
        )
        perm = ObjectPermission.objects.create(
            name="dhcp01 only", actions=["view"], constraints={"scope__server__name": "dhcp01"}
        )
        perm.object_types.add(ObjectType.objects.get_for_model(Reservation))
        perm.users.add(self.user)
        response = self._page(self.device)
        self.assertContains(response, ">mine</a>")
        self.assertNotContains(response, "theirs")

    # -- cost --------------------------------------------------------------------------------------------------------

    def _count(self, obj):
        with CaptureQueriesContext(connection) as queries:
            self._page(obj)
        return len(queries)

    def test_query_count_does_not_grow_with_reservations(self):
        self._allow_reservations()
        # Every address exists from the start, so only the number of reservations changes between the two counts.
        addresses = [self._ip(f"10.107.0.{100 + i}/24", on=(self.eth0, self.eth1)[i % 2]) for i in range(6)]
        macs = [self._mac(f"aa:bb:cc:00:02:{i:02x}", on=self.eth1) for i in range(6)]

        def reserve(i):
            # Alternate servers, and reservations found by IP address and MAC address or by MAC address alone.
            if i % 2 == 0:
                scope_, address = self.scope, addresses[i]
            else:
                scope_, address = self.scope2, ip_address(f"10.108.0.{100 + i}/24")
            Reservation.objects.create(scope=scope_, ip_address=address, mac_address=macs[i], name=f"res{i}")

        reserve(0)
        self._page(self.device)  # warm caches (content types, permissions) the way any later view would find them
        pages = (self.device, self.eth1, macs[0])
        one = [self._count(obj) for obj in pages]
        for i in range(1, 6):
            reserve(i)
        many = [self._count(obj) for obj in pages]
        self.assertContains(self._page(self.device), ">res5</a>")
        self.assertEqual(one, many)

    # -- delete still refused ----------------------------------------------------------------------------------------

    def test_device_delete_is_still_refused_and_names_the_reservation(self):
        self._allow_reservations()
        self.add_permissions("dcim.delete_device")
        held = Reservation.objects.create(
            scope=self.scope, ip_address=self._ip("10.107.0.95/24", on=self.eth0), client_id="12", name="keeper"
        )
        response = self.client.post(f"{self.device.get_absolute_url()}delete/", {"confirm": True}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Device.objects.filter(pk=self.device.pk).exists())
        self.assertTrue(Reservation.objects.filter(pk=held.pk).exists())
        # NetBox's protected error links the reservation that holds the address.
        self.assertContains(response, held.get_absolute_url())
