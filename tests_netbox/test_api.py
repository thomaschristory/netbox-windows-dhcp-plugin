from utilities.testing import APITestCase, APIViewTestCases

from netbox_windows_dhcp_plugin.choices import ClassTypeChoices, MacFilterListChoices
from netbox_windows_dhcp_plugin.models import (
    ExclusionRange,
    ImportProposal,
    MacFilter,
    MacFilterLists,
    WindowsClass,
    WindowsScope,
    WindowsServer,
    WindowsVendorOption,
    WindowsVendorOptionDefinition,
)

from .fixtures import PluginAPIMixin, dhcp_servers, subnets, vendor_classes, windows_server

STANDARD = (
    PluginAPIMixin,
    APIViewTestCases.GetObjectViewTestCase,
    APIViewTestCases.ListObjectsViewTestCase,
    APIViewTestCases.CreateObjectViewTestCase,
    APIViewTestCases.UpdateObjectViewTestCase,
    APIViewTestCases.DeleteObjectViewTestCase,
    APIViewTestCases.GraphQLTestCase,
)
# GraphQL hides related objects the user cannot view, and these relations are required.
DHCP_VIEW = ("netbox_dhcp.view_dhcpserver", "netbox_dhcp.view_subnet", "ipam.view_prefix")


class WindowsServerAPITest(*STANDARD):
    user_permissions = DHCP_VIEW
    model = WindowsServer
    brief_fields = ["description", "display", "host", "id", "url"]
    graphql_base_name = "windows_dhcp_server"
    bulk_update_data = {"credential_profile": "other"}

    @classmethod
    def setUpTestData(cls):
        servers = dhcp_servers(6)
        for server in servers[:3]:
            windows_server(server)
        cls.create_data = [
            {"dhcp_server": server.pk, "host": f"{server.name}.corp", "credential_profile": "lab"}
            for server in servers[3:]
        ]

    def test_credentials_are_not_exposed(self):
        self.add_permissions("netbox_windows_dhcp_plugin.view_windowsserver")
        response = self.client.get(self._get_detail_url(WindowsServer.objects.first()), **self.header)
        self.assertNotIn("password", response.json())


class WindowsScopeAPITest(*STANDARD):
    user_permissions = DHCP_VIEW
    model = WindowsScope
    brief_fields = ["display", "id", "state", "url"]
    graphql_base_name = "windows_dhcp_scope"
    bulk_update_data = {"state": "inactive"}

    @classmethod
    def setUpTestData(cls):
        (server,) = dhcp_servers(1)
        nets = subnets(server, 6)
        for subnet in nets[:3]:
            WindowsScope.objects.create(subnet=subnet)
        cls.create_data = [{"subnet": subnet.pk, "delay_ms": 100} for subnet in nets[3:]]


class ExclusionRangeAPITest(*STANDARD):
    user_permissions = DHCP_VIEW
    model = ExclusionRange
    brief_fields = ["description", "display", "end_address", "id", "start_address", "url"]
    graphql_base_name = "windows_dhcp_exclusion_range"
    bulk_update_data = {"description": "updated"}

    @classmethod
    def setUpTestData(cls):
        (server,) = dhcp_servers(1)
        (subnet,) = subnets(server, 1)
        for i in range(1, 4):
            ExclusionRange.objects.create(subnet=subnet, start_address=f"10.1.0.{i}0", end_address=f"10.1.0.{i}5")
        cls.create_data = [
            {"subnet": subnet.pk, "start_address": f"10.1.0.{i}0", "end_address": f"10.1.0.{i}9"} for i in range(5, 8)
        ]

    def test_range_outside_prefix_is_rejected(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_exclusionrange")
        subnet = ExclusionRange.objects.first().subnet
        response = self.client.post(
            self._get_list_url(),
            {"subnet": subnet.pk, "start_address": "10.9.0.1", "end_address": "10.9.0.5"},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400)


class WindowsClassAPITest(*STANDARD):
    user_permissions = DHCP_VIEW
    model = WindowsClass
    brief_fields = ["class_type", "description", "display", "id", "name", "url"]
    graphql_base_name = "windows_dhcp_class"
    bulk_update_data = {"description": "updated"}

    @classmethod
    def setUpTestData(cls):
        (server,) = dhcp_servers(1)
        for i in range(1, 4):
            WindowsClass.objects.create(
                dhcp_server=server, name=f"class{i}", class_type=ClassTypeChoices.USER, data=f"data{i}"
            )
        cls.create_data = [
            {"dhcp_server": server.pk, "name": f"vendor{i}", "class_type": ClassTypeChoices.VENDOR, "data": "MSFT"}
            for i in range(1, 4)
        ]


class MacFilterAPITest(*STANDARD):
    user_permissions = DHCP_VIEW
    model = MacFilter
    brief_fields = ["description", "display", "id", "list", "mac_address", "url"]
    graphql_base_name = "windows_dhcp_mac_filter"
    bulk_update_data = {"list": MacFilterListChoices.DENY}

    @classmethod
    def setUpTestData(cls):
        (server,) = dhcp_servers(1)
        for i in range(1, 4):
            MacFilter.objects.create(dhcp_server=server, mac_address=f"00-15-5d-00-00-0{i}", list="allow")
        cls.create_data = [
            {"dhcp_server": server.pk, "mac_address": f"00:15:5D:00:01:0{i}", "list": "deny"} for i in range(1, 4)
        ]


class MacFilterListsAPITest(*STANDARD):
    user_permissions = DHCP_VIEW
    model = MacFilterLists
    brief_fields = ["allow_list_enabled", "deny_list_enabled", "display", "id", "url"]
    graphql_base_name = "windows_dhcp_mac_filter_lists"
    bulk_update_data = {"deny_list_enabled": True}

    @classmethod
    def setUpTestData(cls):
        servers = dhcp_servers(6)
        for server in servers[:3]:
            MacFilterLists.objects.create(dhcp_server=server, allow_list_enabled=True)
        cls.create_data = [{"dhcp_server": server.pk, "deny_list_enabled": True} for server in servers[3:]]


# Vendor option definitions also need their vendor class to be visible, and values their definition.
VENDOR_CLASS_VIEW = (*DHCP_VIEW, "netbox_windows_dhcp_plugin.view_windowsclass")


class WindowsVendorOptionDefinitionAPITest(*STANDARD):
    user_permissions = VENDOR_CLASS_VIEW
    model = WindowsVendorOptionDefinition
    brief_fields = ["code", "description", "display", "id", "name", "url"]
    graphql_base_name = "windows_dhcp_vendor_option_definition"
    bulk_update_data = {"description": "updated"}

    @classmethod
    def setUpTestData(cls):
        vendor_class = vendor_classes(dhcp_servers(1)[0])[0]
        for code in range(1, 4):
            WindowsVendorOptionDefinition.objects.create(vendor_class=vendor_class, code=code, name=f"option{code}")
        cls.create_data = [
            {"vendor_class": vendor_class.pk, "code": code, "name": f"new{code}", "default_value": ["0x0"]}
            for code in range(4, 7)
        ]

    def test_user_class_is_rejected(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_windowsvendoroptiondefinition")
        user_class = WindowsClass.objects.create(
            dhcp_server=WindowsClass.objects.first().dhcp_server,
            name="users",
            class_type=ClassTypeChoices.USER,
            data="u",
        )
        response = self.client.post(
            self._get_list_url(),
            {"vendor_class": user_class.pk, "code": 9, "name": "bad"},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400)


class WindowsVendorOptionAPITest(*STANDARD):
    user_permissions = (*VENDOR_CLASS_VIEW, "netbox_windows_dhcp_plugin.view_windowsvendoroptiondefinition")
    model = WindowsVendorOption
    brief_fields = ["display", "id", "url", "value"]
    graphql_base_name = "windows_dhcp_vendor_option"
    bulk_update_data = {"value": ["0x2"]}

    @classmethod
    def setUpTestData(cls):
        (server,) = dhcp_servers(1)
        definition = WindowsVendorOptionDefinition.objects.create(
            vendor_class=vendor_classes(server)[0], code=1, name="option1"
        )
        nets = subnets(server, 6)
        for subnet in nets[:3]:
            WindowsVendorOption.objects.create(definition=definition, assigned_object=subnet, value=["0x1"])
        cls.create_data = [
            {
                "definition": definition.pk,
                "assigned_object_type": "netbox_dhcp.subnet",
                "assigned_object_id": subnet.pk,
                "value": ["0x1", "0x2"],
            }
            for subnet in nets[3:]
        ]

    def test_value_on_another_server_is_rejected(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_windowsvendoroption")
        (other,) = dhcp_servers(1, start=9)
        response = self.client.post(
            self._get_list_url(),
            {
                "definition": WindowsVendorOptionDefinition.objects.first().pk,
                "assigned_object_type": "netbox_dhcp.dhcpserver",
                "assigned_object_id": other.pk,
                "value": ["0x1"],
            },
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400)


class ReadOnlyAPITest(PluginAPIMixin, APITestCase):
    def test_sync_runs_are_read_only(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_syncrun")
        response = self.client.post("/api/plugins/windows-dhcp/sync-runs/", {}, format="json", **self.header)
        self.assertEqual(response.status_code, 405)

    def test_import_proposals_list(self):
        # The list annotates an item count; an aggregate drops the model's default ordering, which the
        # paginator refuses.
        server = windows_server(dhcp_servers(1)[0])
        ImportProposal.objects.create(windows_server=server)
        ImportProposal.objects.create(windows_server=server)
        self.add_permissions("netbox_windows_dhcp_plugin.view_importproposal")
        response = self.client.get("/api/plugins/windows-dhcp/import-proposals/", **self.header)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)


class TestConnectionActionTest(PluginAPIMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.server = windows_server(dhcp_servers(1)[0])
        self.url = f"/api/plugins/windows-dhcp/windows-servers/{self.server.pk}/test-connection/"

    def test_change_permission_is_enough(self):
        # POST normally means "add"; running a test only needs change on the server.
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_windowsserver", "netbox_windows_dhcp_plugin.change_windowsserver"
        )
        response = self.client.post(self.url, {}, format="json", **self.header)
        self.assertEqual(response.status_code, 202)

    def test_view_only_is_refused(self):
        self.add_permissions("netbox_windows_dhcp_plugin.view_windowsserver")
        response = self.client.post(self.url, {}, format="json", **self.header)
        self.assertEqual(response.status_code, 404)
