import re

from ipam.models import Prefix
from netbox_dhcp.models import DHCPServer, Subnet
from utilities.testing import TestCase, ViewTestCases

from netbox_windows_dhcp_plugin.models import (
    ExclusionRange,
    ImportProposal,
    MacFilter,
    MacFilterLists,
    WindowsScope,
    WindowsServer,
    WindowsVendorOption,
    WindowsVendorOptionDefinition,
)

from .fixtures import PluginViewMixin, dhcp_servers, subnets, vendor_classes, windows_server


class WindowsServerViewTest(PluginViewMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = WindowsServer

    @classmethod
    def setUpTestData(cls):
        servers = dhcp_servers(7)
        for server in servers[:3]:
            windows_server(server)
        cls.form_data = {
            "dhcp_server": servers[3].pk,
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
            "dhcp_server,host,credential_profile",
            f"{servers[4].name},a.example.com,lab",
            f"{servers[5].name},b.example.com,lab",
            f"{servers[6].name},c.example.com,lab",
        )
        ids = list(WindowsServer.objects.values_list("pk", flat=True))
        cls.csv_update_data = (
            "id,host",
            *(f"{pk},updated{pk}.example.com" for pk in ids),
        )
        cls.bulk_edit_data = {"credential_profile": "other"}


class ExclusionRangeViewTest(PluginViewMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = ExclusionRange

    @classmethod
    def setUpTestData(cls):
        (server,) = dhcp_servers(1)
        (subnet,) = subnets(server, 1)
        for i in range(1, 4):
            ExclusionRange.objects.create(subnet=subnet, start_address=f"10.1.0.{i}0", end_address=f"10.1.0.{i}5")
        cls.form_data = {"subnet": subnet.pk, "start_address": "10.1.0.200", "end_address": "10.1.0.210", "tags": []}
        cls.csv_data = (
            "subnet,start_address,end_address",
            f"{subnet.name},10.1.0.100,10.1.0.101",
            f"{subnet.name},10.1.0.110,10.1.0.111",
            f"{subnet.name},10.1.0.120,10.1.0.121",
        )
        ids = list(ExclusionRange.objects.values_list("pk", flat=True))
        cls.csv_update_data = ("id,description", *(f"{pk},updated" for pk in ids))
        cls.bulk_edit_data = {"description": "bulk"}


class MacFilterViewTest(PluginViewMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = MacFilter

    @classmethod
    def setUpTestData(cls):
        (server,) = dhcp_servers(1)
        for i in range(1, 4):
            MacFilter.objects.create(dhcp_server=server, mac_address=f"00-15-5d-00-00-0{i}", list="allow")
        cls.form_data = {"dhcp_server": server.pk, "mac_address": "00-15-5d-*", "list": "deny", "tags": []}
        cls.csv_data = (
            "dhcp_server,mac_address,list",
            f"{server.name},00-15-5d-00-01-01,deny",
            f"{server.name},00-15-5d-00-01-02,deny",
            f"{server.name},00-15-5d-00-01-03,deny",
        )
        ids = list(MacFilter.objects.values_list("pk", flat=True))
        cls.csv_update_data = ("id,description", *(f"{pk},updated" for pk in ids))
        cls.bulk_edit_data = {"list": "deny"}


class MacFilterListsViewTest(
    PluginViewMixin,
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkEditObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = MacFilterLists

    @classmethod
    def setUpTestData(cls):
        servers = dhcp_servers(4)
        for server in servers[:3]:
            MacFilterLists.objects.create(dhcp_server=server)
        cls.form_data = {
            "dhcp_server": servers[3].pk,
            "allow_list_enabled": True,
            "deny_list_enabled": False,
            "tags": [],
        }
        cls.bulk_edit_data = {"deny_list_enabled": True}


class WindowsVendorOptionDefinitionViewTest(PluginViewMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = WindowsVendorOptionDefinition
    # The form takes JSON text, the model stores the decoded list.
    validation_excluded_fields = ("default_value",)

    @classmethod
    def setUpTestData(cls):
        (server,) = dhcp_servers(1)
        (vendor_class,) = vendor_classes(server)
        for code in range(1, 4):
            WindowsVendorOptionDefinition.objects.create(vendor_class=vendor_class, code=code, name=f"option{code}")
        cls.form_data = {
            "vendor_class": vendor_class.pk,
            "code": 10,
            "name": "new",
            "windows_type": "Byte",
            "multi_valued": True,
            "default_value": '["0x1"]',
            "tags": [],
        }
        cls.csv_data = (
            "dhcp_server,vendor_class,code,name",
            f"{server.name},{vendor_class.name},20,a",
            f"{server.name},{vendor_class.name},21,b",
            f"{server.name},{vendor_class.name},22,c",
        )
        ids = list(WindowsVendorOptionDefinition.objects.values_list("pk", flat=True))
        cls.csv_update_data = ("id,description", *(f"{pk},updated" for pk in ids))
        cls.bulk_edit_data = {"windows_type": "Word"}


class WindowsVendorOptionViewTest(
    PluginViewMixin,
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = WindowsVendorOption
    # The form takes JSON text, the model stores the decoded list.
    validation_excluded_fields = ("value",)

    @classmethod
    def setUpTestData(cls):
        (server,) = dhcp_servers(1)
        (vendor_class,) = vendor_classes(server)
        first, second = (
            WindowsVendorOptionDefinition.objects.create(vendor_class=vendor_class, code=code, name=f"option{code}")
            for code in (1, 2)
        )
        nets = subnets(server, 3)
        for subnet in nets:
            WindowsVendorOption.objects.create(definition=first, assigned_object=subnet, value=["0x1"])
        cls.form_data = {"definition": second.pk, "subnet": nets[0].pk, "value": '["0x2"]', "tags": []}


class ImportProposalListViewTest(TestCase):
    def test_list_is_ordered(self):
        server = windows_server(dhcp_servers(1)[0])
        ImportProposal.objects.create(windows_server=server)
        ImportProposal.objects.create(windows_server=server)
        self.add_permissions("netbox_windows_dhcp_plugin.view_importproposal")
        response = self.client.get("/plugins/windows-dhcp/importproposals/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["table"].data.data.ordered)


class WindowsServerLinkTest(TestCase):
    """Issue #38: a Windows server's name opens the Windows server, wherever it is listed."""

    def setUp(self):
        super().setUp()
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_windowsserver",
            "netbox_windows_dhcp_plugin.view_syncrun",
            "netbox_dhcp.view_dhcpserver",
        )
        # Primary keys that differ between the Windows server and its DHCP server, so neither can stand in for the
        # other in a URL.
        servers = dhcp_servers(4)
        windows_server(servers[0]).delete()
        windows_server(servers[1]).delete()
        self.dhcp = servers[3]
        self.server = windows_server(self.dhcp)
        self.assertNotEqual(self.server.pk, self.dhcp.pk)

    def links(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return re.findall(r'<a href="([^"]+)"[^>]*>\s*([^<]+?)\s*</a>', response.content.decode())

    def test_name_in_the_server_list_opens_the_windows_server(self):
        links = self.links("/plugins/windows-dhcp/windowsservers/")
        self.assertIn((self.server.get_absolute_url(), self.dhcp.name), links)
        # The generic DHCP server stays one click away, under its own column.
        self.assertIn((self.dhcp.get_absolute_url(), self.dhcp.name), links)
        self.assertIn("Generic DHCP server", self.client.get("/plugins/windows-dhcp/windowsservers/").content.decode())

    def test_matches_the_sync_run_links(self):
        from netbox_windows_dhcp_plugin.choices import SyncKindChoices
        from netbox_windows_dhcp_plugin.models import SyncRun

        run = SyncRun.objects.create(windows_server=self.server, kind=SyncKindChoices.TEST)
        target = (self.server.get_absolute_url(), self.dhcp.name)
        self.assertIn(target, self.links("/plugins/windows-dhcp/windowsservers/"))
        self.assertIn(target, self.links("/plugins/windows-dhcp/syncruns/"))
        self.assertIn(target, self.links(run.get_absolute_url()))

    def test_list_sorts_by_name(self):
        other = windows_server(DHCPServer.objects.create(name="aaa-first"))
        for sort, first in (("dhcp_server", other), ("-dhcp_server", self.server)):
            html = self.client.get(f"/plugins/windows-dhcp/windowsservers/?sort={sort}").content.decode()
            names = [
                n for _, n in re.findall(r'<a href="(/plugins/windows-dhcp/windowsservers/\d+/)">([^<]+)</a>', html)
            ]
            self.assertEqual(names[0], first.dhcp_server.name, sort)

    def test_saved_layouts_keep_the_windows_server_link(self):
        # A layout saved before #38 lists "dhcp_server" only: that column now opens the Windows server.
        from users.models import UserConfig

        config, _ = UserConfig.objects.get_or_create(user=self.user)
        config.set("tables.WindowsServerTable.columns", ["dhcp_server", "host"], commit=True)
        links = self.links("/plugins/windows-dhcp/windowsservers/")
        self.assertIn((self.server.get_absolute_url(), self.dhcp.name), links)


class WindowsScopeListTest(TestCase):
    """Issue #47: the scope list shows each scope's network next to its name."""

    def setUp(self):
        super().setUp()
        self.add_permissions("netbox_windows_dhcp_plugin.view_windowsscope", "ipam.view_prefix")
        server = dhcp_servers(1)[0]
        windows_server(server)
        first, second = subnets(server, 2)
        # Two scopes with the same friendly name stay told apart by their prefixes.
        for subnet in (first, second):
            subnet.name = f"{server.name}/Office LAN {subnet.pk}"
            subnet.save()
        v6 = Subnet.objects.create(
            name=f"{server.name}/v6", dhcp_server=server, prefix=Prefix.objects.create(prefix="2001:db8:47::/64")
        )
        self.scopes = [WindowsScope.objects.create(subnet=s) for s in (first, second, v6)]

    def test_prefix_column_is_shown_by_default(self):
        response = self.client.get("/plugins/windows-dhcp/windowsscopes/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for scope in self.scopes:
            prefix = scope.subnet.prefix
            self.assertIn(f'<a href="{prefix.get_absolute_url()}">{prefix.prefix}</a>', html)
            self.assertIn(scope.subnet.name, html)

    def test_sorts_by_prefix(self):
        for sort, first in (("prefix", "10.1.0.0/24"), ("-prefix", "2001:db8:47::/64")):
            html = self.client.get(f"/plugins/windows-dhcp/windowsscopes/?sort={sort}").content.decode()
            prefixes = re.findall(r'<a href="/ipam/prefixes/\d+/">([^<]+)</a>', html)
            self.assertEqual(prefixes[0], first, sort)
