import re

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from users.models import ObjectPermission
from utilities.testing import TestCase, ViewTestCases
from virtualization.models import VirtualMachine

from netbox_windows_dhcp_plugin.choices import ClassTypeChoices
from netbox_windows_dhcp_plugin.models import (
    ExclusionRange,
    FailoverRelationship,
    ImportProposal,
    MacFilter,
    MacFilterLists,
    OptionDefinition,
    OptionValue,
    Reservation,
    Scope,
    Superscope,
    WindowsClass,
    WindowsPolicy,
    WindowsServer,
)

from .fixtures import (
    PluginViewMixin,
    ip_address,
    ip_range,
    prefix,
    reservation,
    scope,
    scopes,
    vendor_classes,
    windows_server,
    windows_servers,
)

# Models without a CSV import form: every standard view test but bulk import.
NO_IMPORT = (
    PluginViewMixin,
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkEditObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
)


def _ids(model):
    return list(model.objects.values_list("pk", flat=True))


class WindowsServerViewTest(PluginViewMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = WindowsServer

    @classmethod
    def setUpTestData(cls):
        windows_servers(3)
        cls.form_data = {
            "name": "new",
            "host": "new.example.com",
            "transport": "winrm",
            "credential_profile": "lab",
            "winrm_auth": "ntlm",
            "winrm_https": True,
            "verify_tls": True,
            "sync_enabled": True,
            "tags": [],
        }
        cls.csv_data = (
            "name,host,credential_profile",
            "a,a.example.com,lab",
            "b,b.example.com,lab",
            "c,c.example.com,lab",
        )
        cls.csv_update_data = ("id,host", *(f"{pk},updated{pk}.example.com" for pk in _ids(WindowsServer)))
        cls.bulk_edit_data = {"credential_profile": "other"}


class SuperscopeViewTest(PluginViewMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = Superscope

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        for i in range(1, 4):
            Superscope.objects.create(server=server, name=f"campus{i}")
        cls.form_data = {"server": server.pk, "name": "new", "description": "d", "tags": []}
        cls.csv_data = ("server,name", "dhcp1,a", "dhcp1,b", "dhcp1,c")
        cls.csv_update_data = ("id,description", *(f"{pk},updated" for pk in _ids(Superscope)))
        cls.bulk_edit_data = {"description": "bulk"}


class ScopeViewTest(PluginViewMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = Scope

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        scopes(server, 3)
        superscope = Superscope.objects.create(server=server, name="Campus")
        cls.form_data = {
            "server": server.pk,
            "prefix": prefix("10.9.0.0/24").pk,
            "ip_range": ip_range("10.9.0.10/24", "10.9.0.99/24").pk,
            "superscope": superscope.pk,
            "name": "new",
            "description": "a long Windows description",
            "state": "inactive",
            "scope_type": "both",
            "lease_duration": 3600,
            "delay_ms": 10,
            "activate_policies": False,
            "tags": [],
        }
        for value in ("10.20.0.0/24", "10.21.0.0/24", "2001:db8:22::/64"):
            prefix(value)
        cls.csv_data = (
            "server,prefix,name,superscope",
            "dhcp1,10.20.0.0/24,a,Campus",
            "dhcp1,10.21.0.0/24,b,",
            "dhcp1,2001:db8:22::/64,c,",
        )
        cls.csv_update_data = ("id,description", *(f"{pk},updated" for pk in _ids(Scope)))
        cls.bulk_edit_data = {"state": "inactive"}

    def test_add_form_creates_the_prefix_and_ip_range_in_place(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_scope")
        response = self.client.get("/plugins/windows-dhcp/scopes/add/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/ipam/prefixes/add/?_quickadd=True&target=id_prefix")
        self.assertContains(response, "/ipam/ip-ranges/add/?_quickadd=True&target=id_ip_range")


class ReservationViewTest(PluginViewMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = Reservation

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        lan = scope(server, "10.1.0.0/24")
        for i in range(1, 4):
            reservation(lan, f"10.1.0.{i}/24", client_id=f"aa-bb-cc-00-00-0{i}", name=f"r{i}")
        cls.form_data = {
            "scope": lan.pk,
            "ip_address": ip_address("10.1.0.50/24").pk,
            "name": "printer",
            "description": "floor 1",
            "client_id": "aa-bb-cc-00-11-22",
            "reservation_type": "dhcp",
            "tags": [],
        }
        for i in range(60, 63):
            ip_address(f"10.1.0.{i}/24")
        cls.csv_data = (
            "scope,ip_address,name,client_id",
            *(f"{lan.pk},10.1.0.{i}/24,c{i},aa-{i}" for i in range(60, 63)),
        )
        cls.csv_update_data = ("id,description", *(f"{pk},updated" for pk in _ids(Reservation)))
        cls.bulk_edit_data = {"reservation_type": "bootp"}

    def test_add_form_creates_the_ip_and_mac_address_in_place(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_reservation")
        response = self.client.get("/plugins/windows-dhcp/reservations/add/")
        self.assertEqual(response.status_code, 200)
        # The quick-add buttons open NetBox's own add forms in a pop-up.
        self.assertContains(response, "/ipam/ip-addresses/add/?_quickadd=True&target=id_ip_address")
        self.assertContains(response, "/dcim/mac-addresses/add/?_quickadd=True&target=id_mac_address")


class ExclusionRangeViewTest(PluginViewMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = ExclusionRange

    @classmethod
    def setUpTestData(cls):
        lan = scope(windows_server(), "10.1.0.0/24")
        for i in range(1, 4):
            ExclusionRange.objects.create(scope=lan, start_address=f"10.1.0.{i}0", end_address=f"10.1.0.{i}5")
        cls.form_data = {"scope": lan.pk, "start_address": "10.1.0.200", "end_address": "10.1.0.210", "tags": []}
        cls.csv_data = (
            "scope,start_address,end_address",
            f"{lan.pk},10.1.0.100,10.1.0.101",
            f"{lan.pk},10.1.0.110,10.1.0.111",
            f"{lan.pk},10.1.0.120,10.1.0.121",
        )
        cls.csv_update_data = ("id,description", *(f"{pk},updated" for pk in _ids(ExclusionRange)))
        cls.bulk_edit_data = {"description": "bulk"}


class WindowsClassViewTest(PluginViewMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = WindowsClass

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        vendor_classes(server, 3)
        cls.form_data = {
            "server": server.pk,
            "name": "Labs",
            "class_type": ClassTypeChoices.USER,
            "family": 6,
            "data": "labs",
            "tags": [],
        }
        cls.csv_data = (
            # No family column: IPv4, the default.
            "server,name,class_type,data",
            "dhcp1,u1,user,u1",
            "dhcp1,u2,user,u2",
            "dhcp1,v9,vendor,v9",
        )
        cls.csv_update_data = ("id,description", *(f"{pk},updated" for pk in _ids(WindowsClass)))
        cls.bulk_edit_data = {"description": "bulk"}


class OptionDefinitionViewTest(PluginViewMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = OptionDefinition

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        (vendor_class,) = vendor_classes(server)
        for code in range(1, 4):
            OptionDefinition.objects.create(server=server, vendor_class=vendor_class, code=code, name=f"option{code}")
        cls.form_data = {
            "server": server.pk,
            "family": 4,
            "vendor_class": vendor_class.pk,
            "code": 10,
            "name": "new",
            "windows_type": "Byte",
            "multi_valued": True,
            "default_value": '["0x1"]',
            "tags": [],
        }
        cls.csv_data = (
            "server,family,vendor_class,code,name",
            f"dhcp1,4,{vendor_class.name},20,a",
            "dhcp1,4,,224,b",
            "dhcp1,6,,99,c",
        )
        cls.csv_update_data = ("id,description", *(f"{pk},updated" for pk in _ids(OptionDefinition)))
        cls.bulk_edit_data = {"windows_type": "Word"}


class WindowsPolicyViewTest(*NO_IMPORT):
    model = WindowsPolicy

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        lan = scope(server, "10.1.0.0/24")
        for i in range(1, 4):
            WindowsPolicy.objects.create(server=server, name=f"p{i}", processing_order=i)
        cls.form_data = {
            "server": server.pk,
            "scope": lan.pk,
            "name": "Printers",
            "processing_order": 1,
            "enabled": True,
            "condition": "and",
            "criteria": '{"mac_address": ["EQ", "00-15-5d-*"]}',
            "ip_ranges": '[["10.1.0.100", "10.1.0.120"]]',
            "lease_duration": 3600,
            "tags": [],
        }
        cls.bulk_edit_data = {"enabled": False}


class OptionValueViewTest(PluginViewMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = OptionValue

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        lan = scope(server, "10.1.0.0/24")
        printer = reservation(lan, "10.1.0.50/24", client_id="aa")
        OptionValue.objects.create(server=server, code=6, value=["10.0.0.1"])
        OptionValue.objects.create(server=server, scope=lan, code=3, value=["10.1.0.1"])
        OptionValue.objects.create(server=server, scope=lan, reservation=printer, code=3, value=["10.1.0.254"])
        cls.form_data = {
            "server": server.pk,
            "family": 4,
            "code": 15,
            "scope": lan.pk,
            "reservation": printer.pk,
            # Commas and spaces survive in a value, unlike in a comma-separated string.
            "value": '["corp, example"]',
            "tags": [],
        }
        cls.csv_data = (
            "server,code,scope,value",
            f'dhcp1,15,{lan.pk},"[""example.com""]"',
            'dhcp1,42,,"[""10.0.0.5"", ""10.0.0.6""]"',
            'dhcp1,44,,"[""a,b""]"',
        )
        cls.csv_update_data = ("id,value", *(f'{pk},"[""10.0.0.{pk % 250}""]"' for pk in _ids(OptionValue)))
        cls.bulk_edit_data = {"value": '["10.0.0.9"]'}


class FailoverRelationshipViewTest(*NO_IMPORT):
    model = FailoverRelationship

    @classmethod
    def setUpTestData(cls):
        primary, partner = windows_servers(2)
        nets = scopes(primary, 2)
        for i in range(1, 4):
            relationship = FailoverRelationship.objects.create(
                name=f"ha{i}", primary_server=primary, partner_server=partner
            )
            relationship.scopes.set(nets[:1])
        cls.form_data = {
            "name": "new",
            "primary_server": primary.pk,
            "partner_server": partner.pk,
            "mode": "hotstandby",
            "max_client_lead_time": 600,
            "reserve_percent": 10,
            "server_role": "active",
            "scopes": [nets[1].pk],
            "tags": [],
        }
        cls.bulk_edit_data = {"description": "bulk"}


class MacFilterViewTest(PluginViewMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = MacFilter

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        for i in range(1, 4):
            MacFilter.objects.create(server=server, mac_address=f"00-15-5d-00-00-0{i}", list="allow")
        cls.form_data = {"server": server.pk, "mac_address": "00-15-5d-*", "list": "deny", "tags": []}
        cls.csv_data = (
            "server,mac_address,list",
            "dhcp1,00-15-5d-00-01-01,deny",
            "dhcp1,00-15-5d-00-01-02,deny",
            "dhcp1,00-15-5d-00-01-03,deny",
        )
        cls.csv_update_data = ("id,description", *(f"{pk},updated" for pk in _ids(MacFilter)))
        cls.bulk_edit_data = {"list": "deny"}


class MacFilterListsViewTest(*NO_IMPORT):
    model = MacFilterLists

    @classmethod
    def setUpTestData(cls):
        servers = windows_servers(4)
        for server in servers[:3]:
            MacFilterLists.objects.create(server=server)
        cls.form_data = {
            "server": servers[3].pk,
            "allow_list_enabled": True,
            "deny_list_enabled": False,
            "tags": [],
        }
        cls.bulk_edit_data = {"deny_list_enabled": True}


class ImportProposalListViewTest(TestCase):
    def test_list_is_ordered(self):
        server = windows_server()
        ImportProposal.objects.create(windows_server=server)
        ImportProposal.objects.create(windows_server=server)
        self.add_permissions("netbox_windows_dhcp_plugin.view_importproposal")
        response = self.client.get("/plugins/windows-dhcp/importproposals/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["table"].data.data.ordered)


class SyncRunProposalsTest(TestCase):
    """A sync run page lists the proposals of that run only (it listed every proposal of every server)."""

    def test_proposals_are_filtered_by_sync_run(self):
        from netbox_windows_dhcp_plugin.choices import SyncKindChoices
        from netbox_windows_dhcp_plugin.filtersets import ImportProposalFilterSet
        from netbox_windows_dhcp_plugin.models import SyncRun

        server = windows_server()
        imported = SyncRun.objects.create(windows_server=server, kind=SyncKindChoices.IMPORT)
        dry_run = SyncRun.objects.create(windows_server=server, kind=SyncKindChoices.DRY_RUN)
        proposal = ImportProposal.objects.create(windows_server=server, sync_run=imported)
        qs = ImportProposalFilterSet({"sync_run_id": [imported.pk]}, ImportProposal.objects.all()).qs
        self.assertEqual(list(qs), [proposal])
        self.assertFalse(ImportProposalFilterSet({"sync_run_id": [dry_run.pk]}, ImportProposal.objects.all()).qs)

        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_syncrun", "netbox_windows_dhcp_plugin.view_importproposal"
        )
        link = f'href="{proposal.get_absolute_url()}"'
        self.assertIn(link, self.client.get(imported.get_absolute_url()).content.decode())
        self.assertNotIn(link, self.client.get(dry_run.get_absolute_url()).content.decode())


def _links(test, url):
    response = test.client.get(url)
    test.assertEqual(response.status_code, 200)
    return re.findall(r'<a href="([^"]+)"[^>]*>\s*([^<]+?)\s*</a>', response.content.decode())


class WindowsServerLinkTest(TestCase):
    """Issue #38: a Windows server's name opens the Windows server, wherever it is listed."""

    def setUp(self):
        super().setUp()
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_windowsserver",
            "netbox_windows_dhcp_plugin.view_syncrun",
        )
        self.server = windows_server("dhcp-b")

    def test_name_in_the_server_list_opens_the_windows_server(self):
        links = _links(self, "/plugins/windows-dhcp/windowsservers/")
        self.assertIn((self.server.get_absolute_url(), self.server.name), links)

    def test_matches_the_sync_run_links(self):
        from netbox_windows_dhcp_plugin.choices import SyncKindChoices
        from netbox_windows_dhcp_plugin.models import SyncRun

        run = SyncRun.objects.create(windows_server=self.server, kind=SyncKindChoices.TEST)
        target = (self.server.get_absolute_url(), self.server.name)
        self.assertIn(target, _links(self, "/plugins/windows-dhcp/windowsservers/"))
        self.assertIn(target, _links(self, "/plugins/windows-dhcp/syncruns/"))
        self.assertIn(target, _links(self, run.get_absolute_url()))

    def test_list_sorts_by_name(self):
        other = windows_server("aaa-first")
        for sort, first in (("name", other), ("-name", self.server)):
            html = self.client.get(f"/plugins/windows-dhcp/windowsservers/?sort={sort}").content.decode()
            names = [
                n for _, n in re.findall(r'<a href="(/plugins/windows-dhcp/windowsservers/\d+/)">([^<]+)</a>', html)
            ]
            self.assertEqual(names[0], first.name, sort)


class ScopeListTest(TestCase):
    """Issue #47: the scope list shows each scope's network next to its name."""

    def setUp(self):
        super().setUp()
        self.add_permissions("netbox_windows_dhcp_plugin.view_scope", "ipam.view_prefix")
        server = windows_server()
        # Two scopes with the same friendly name stay told apart by their prefixes.
        self.scopes = [
            scope(server, "10.1.0.0/24", name="Office LAN"),
            scope(server, "10.2.0.0/24", name="Office LAN"),
            scope(server, "2001:db8:47::/64", name="v6"),
        ]

    def test_prefix_column_is_shown_by_default(self):
        response = self.client.get("/plugins/windows-dhcp/scopes/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for s in self.scopes:
            self.assertIn(f'<a href="{s.prefix.get_absolute_url()}">{s.prefix.prefix}</a>', html)
            self.assertIn(s.name, html)

    def test_sorts_by_prefix(self):
        for sort, first in (("prefix", "10.1.0.0/24"), ("-prefix", "2001:db8:47::/64")):
            html = self.client.get(f"/plugins/windows-dhcp/scopes/?sort={sort}").content.decode()
            prefixes = re.findall(r'<a href="/ipam/prefixes/\d+/">([^<]+)</a>', html)
            self.assertEqual(prefixes[0], first, sort)


class DevicePanelTest(TestCase):
    """The Windows DHCP servers running on a device or virtual machine show on its page, if the user may see them."""

    def setUp(self):
        super().setUp()
        site = Site.objects.create(name="site", slug="site")
        manufacturer = Manufacturer.objects.create(name="m", slug="m")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="t", slug="t")
        role = DeviceRole.objects.create(name="r", slug="r")
        self.device = Device.objects.create(name="dc01", site=site, device_type=device_type, role=role)
        self.vm = VirtualMachine.objects.create(name="vm01", site=site)
        self.add_permissions("dcim.view_device", "virtualization.view_virtualmachine")

    def test_no_panel_without_a_linked_server(self):
        windows_server()
        self.add_permissions("netbox_windows_dhcp_plugin.view_windowsserver")
        self.assertNotContains(self.client.get(self.device.get_absolute_url()), "Windows DHCP servers")

    def test_panel_lists_the_linked_servers(self):
        server = windows_server("dhcp-dc01", device=self.device)
        other = windows_server("dhcp-vm01", virtual_machine=self.vm)
        self.add_permissions("netbox_windows_dhcp_plugin.view_windowsserver")
        response = self.client.get(self.device.get_absolute_url())
        self.assertContains(response, "Windows DHCP servers")
        self.assertContains(response, f'<a href="{server.get_absolute_url()}">dhcp-dc01</a>')
        self.assertNotContains(response, "dhcp-vm01")
        response = self.client.get(self.vm.get_absolute_url())
        self.assertContains(response, f'<a href="{other.get_absolute_url()}">dhcp-vm01</a>')
        self.assertNotContains(response, "dhcp-dc01")

    def test_panel_needs_the_view_permission(self):
        windows_server("dhcp-dc01", device=self.device)
        self.assertNotContains(self.client.get(self.device.get_absolute_url()), "dhcp-dc01")

    def test_object_permissions_are_respected(self):
        server = windows_server("dhcp-dc01", device=self.device)
        perm = ObjectPermission.objects.create(name="other", actions=["view"], constraints={"pk": server.pk + 1000})
        from core.models import ObjectType

        perm.object_types.add(ObjectType.objects.get_for_model(WindowsServer))
        perm.users.add(self.user)
        self.assertNotContains(self.client.get(self.device.get_absolute_url()), "dhcp-dc01")

    def test_form_refuses_both_a_device_and_a_virtual_machine(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_windowsserver")
        response = self.client.post(
            "/plugins/windows-dhcp/windowsservers/add/",
            {
                "name": "both",
                "host": "both.example.com",
                "transport": "winrm",
                "credential_profile": "lab",
                "winrm_auth": "ntlm",
                "winrm_https": True,
                "verify_tls": True,
                "device": self.device.pk,
                "virtual_machine": self.vm.pk,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not both")
        self.assertFalse(WindowsServer.objects.filter(name="both").exists())
