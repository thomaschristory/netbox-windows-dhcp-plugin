from utilities.testing import TestCase, ViewTestCases

from netbox_windows_dhcp_plugin.models import ExclusionRange, ImportProposal, MacFilter, WindowsServer

from .fixtures import PluginViewMixin, dhcp_servers, subnets, windows_server


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


class ImportProposalListViewTest(TestCase):
    def test_list_is_ordered(self):
        server = windows_server(dhcp_servers(1)[0])
        ImportProposal.objects.create(windows_server=server)
        ImportProposal.objects.create(windows_server=server)
        self.add_permissions("netbox_windows_dhcp_plugin.view_importproposal")
        response = self.client.get("/plugins/windows-dhcp/importproposals/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["table"].data.data.ordered)
