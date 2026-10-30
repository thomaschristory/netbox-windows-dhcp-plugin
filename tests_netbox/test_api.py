from utilities.testing import APITestCase, APIViewTestCases

from netbox_windows_dhcp_plugin.choices import ClassTypeChoices, MacFilterListChoices
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
    PluginAPIMixin,
    ip_address,
    prefix,
    reservation,
    scope,
    scopes,
    vendor_classes,
    windows_server,
    windows_servers,
)

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
SERVER_VIEW = ("netbox_windows_dhcp_plugin.view_windowsserver",)
SCOPE_VIEW = (*SERVER_VIEW, "netbox_windows_dhcp_plugin.view_scope", "ipam.view_prefix")


class WindowsServerAPITest(*STANDARD):
    model = WindowsServer
    brief_fields = ["description", "display", "id", "name", "url"]
    graphql_base_name = "windows_dhcp_server"
    bulk_update_data = {"credential_profile": "other"}

    @classmethod
    def setUpTestData(cls):
        windows_servers(3)
        cls.create_data = [
            {"name": f"new{i}", "host": f"new{i}.corp", "credential_profile": "lab"} for i in range(1, 4)
        ]

    def test_credentials_are_not_exposed(self):
        self.add_permissions("netbox_windows_dhcp_plugin.view_windowsserver")
        response = self.client.get(self._get_detail_url(WindowsServer.objects.first()), **self.header)
        self.assertNotIn("password", response.json())


class SuperscopeAPITest(*STANDARD):
    user_permissions = SERVER_VIEW
    model = Superscope
    brief_fields = ["description", "display", "id", "name", "url"]
    graphql_base_name = "windows_dhcp_superscope"
    bulk_update_data = {"description": "updated"}

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        for i in range(1, 4):
            Superscope.objects.create(server=server, name=f"campus{i}")
        cls.create_data = [{"server": server.pk, "name": f"new{i}"} for i in range(1, 4)]


class ScopeAPITest(*STANDARD):
    user_permissions = (*SERVER_VIEW, "ipam.view_prefix")
    model = Scope
    brief_fields = ["display", "id", "name", "scope_id", "state", "url"]
    graphql_base_name = "windows_dhcp_scope"
    bulk_update_data = {"state": "inactive"}
    # The first scope keeps its IP range, so the update must keep its prefix.
    update_data = {"name": "renamed", "delay_ms": 5, "description": "a long Windows description"}

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        scopes(server, 3)
        cls.create_data = [
            {"server": server.pk, "prefix": prefix("10.7.0.0/24").pk, "name": "a", "delay_ms": 100},
            {"server": server.pk, "prefix": prefix("10.8.0.0/24").pk, "name": "b", "lease_duration": 3600},
            {"server": server.pk, "prefix": prefix("2001:db8:9::/64").pk, "name": "c", "preference": 5},
        ]

    def test_scope_id_and_family_follow_the_prefix(self):
        self.add_permissions("netbox_windows_dhcp_plugin.view_scope")
        v6 = scope(Scope.objects.first().server, "2001:db8:1::/64")
        data = self.client.get(self._get_detail_url(v6), **self.header).json()
        self.assertEqual((data["scope_id"], data["family"]), ("2001:db8:1::", 6))

    def test_range_outside_the_prefix_is_rejected(self):
        from .fixtures import ip_range

        self.add_permissions("netbox_windows_dhcp_plugin.add_scope")
        response = self.client.post(
            self._get_list_url(),
            {
                "server": Scope.objects.first().server_id,
                "prefix": prefix("10.30.0.0/24").pk,
                "ip_range": ip_range("10.31.0.10/24", "10.31.0.20/24").pk,
                "name": "bad",
            },
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400)


class ReservationAPITest(*STANDARD):
    user_permissions = (*SCOPE_VIEW, "ipam.view_ipaddress")
    model = Reservation
    brief_fields = ["display", "id", "ip_address", "name", "url"]
    graphql_base_name = "windows_dhcp_reservation"
    bulk_update_data = {"description": "updated"}

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        lan = scope(server, "10.1.0.0/24")
        v6 = scope(server, "2001:db8:1::/64")
        for i in range(1, 4):
            reservation(lan, f"10.1.0.{i}/24", client_id=f"aa-{i}")
        cls.create_data = [
            {"scope": lan.pk, "ip_address": ip_address("10.1.0.50/24").pk, "client_id": "aa-50", "name": "a"},
            {
                "scope": lan.pk,
                "ip_address": ip_address("10.1.0.51/24").pk,
                "client_id": "aa-51",
                "reservation_type": "dhcp",
            },
            {"scope": v6.pk, "ip_address": ip_address("2001:db8:1::10/64").pk, "duid": "00-01-00-01", "iaid": 7},
        ]

    def test_address_outside_the_scope_is_rejected(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_reservation")
        response = self.client.post(
            self._get_list_url(),
            {"scope": Scope.objects.first().pk, "ip_address": ip_address("10.9.0.5/24").pk, "client_id": "x"},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400)


class ParentMoveAPITest(PluginAPIMixin, APITestCase):
    user_permissions = (*SCOPE_VIEW, "netbox_windows_dhcp_plugin.view_reservation", "ipam.view_ipaddress")

    @classmethod
    def setUpTestData(cls):
        cls.server, cls.other = windows_servers(2)
        cls.lan = scope(cls.server, "10.1.0.0/24")
        cls.destination = scope(cls.other, "10.1.0.0/24")
        cls.printer = reservation(cls.lan, "10.1.0.50/24", client_id="aa")

    def patch(self, endpoint, obj, data):
        return self.client.patch(f"/api/plugins/windows-dhcp/{endpoint}/{obj.pk}/", data, format="json", **self.header)

    def test_scope_move_with_each_dependent_kind_is_rejected(self):
        self.add_permissions("netbox_windows_dhcp_plugin.change_scope")
        for kind in ("scope_option", "reservation_option", "policy"):
            with self.subTest(kind=kind):
                if kind == "policy":
                    child = WindowsPolicy.objects.create(server=self.server, scope=self.lan, name="phones")
                else:
                    child = OptionValue.objects.create(
                        server=self.server,
                        scope=self.lan,
                        reservation=self.printer if kind == "reservation_option" else None,
                        code=3,
                        value=["10.1.0.1"],
                    )
                response = self.patch("scopes", self.lan, {"server": self.other.pk})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("server", response.data)
                self.lan.refresh_from_db()
                child.refresh_from_db()
                self.assertEqual(self.lan.server_id, self.server.pk)
                self.assertEqual(child.server_id, self.server.pk)
                child.delete()

    def test_bulk_scope_move_is_atomic_when_one_scope_has_dependencies(self):
        self.add_permissions("netbox_windows_dhcp_plugin.change_scope")
        free = scope(self.server, "10.2.0.0/24")
        OptionValue.objects.create(server=self.server, scope=self.lan, code=3, value=["10.1.0.1"])
        response = self.client.patch(
            "/api/plugins/windows-dhcp/scopes/",
            [{"id": free.pk, "server": self.other.pk}, {"id": self.lan.pk, "server": self.other.pk}],
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.lan.refresh_from_db()
        free.refresh_from_db()
        self.assertEqual((self.lan.server_id, free.server_id), (self.server.pk, self.server.pk))

    def test_scope_move_without_server_bound_dependencies_is_allowed(self):
        self.add_permissions("netbox_windows_dhcp_plugin.change_scope")
        response = self.patch("scopes", self.lan, {"server": self.other.pk})
        self.assertEqual(response.status_code, 200, response.content)
        self.lan.refresh_from_db()
        self.printer.refresh_from_db()
        self.assertEqual(self.lan.server_id, self.other.pk)
        self.assertEqual(self.printer.server.pk, self.other.pk)

    def test_reservation_move_with_option_is_rejected(self):
        self.add_permissions("netbox_windows_dhcp_plugin.change_reservation")
        value = OptionValue.objects.create(
            server=self.server, scope=self.lan, reservation=self.printer, code=3, value=["10.1.0.1"]
        )
        same_server = scope(self.server, "10.1.0.0/23")
        for destination in (same_server, self.destination):
            with self.subTest(server=destination.server_id):
                response = self.patch("reservations", self.printer, {"scope": destination.pk})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("scope", response.data)
        self.printer.refresh_from_db()
        value.refresh_from_db()
        self.assertEqual(self.printer.scope_id, self.lan.pk)
        self.assertEqual((value.server_id, value.scope_id), (self.server.pk, self.lan.pk))

    def test_reservation_move_without_options_is_allowed(self):
        self.add_permissions("netbox_windows_dhcp_plugin.change_reservation")
        response = self.patch("reservations", self.printer, {"scope": self.destination.pk})
        self.assertEqual(response.status_code, 200, response.content)
        self.printer.refresh_from_db()
        self.assertEqual(self.printer.scope_id, self.destination.pk)

    def test_policy_move_with_option_is_rejected(self):
        self.add_permissions("netbox_windows_dhcp_plugin.change_windowspolicy")
        policy = WindowsPolicy.objects.create(server=self.server, scope=self.lan, name="phones")
        OptionValue.objects.create(server=self.server, scope=self.lan, policy=policy, code=3, value=["10.1.0.1"])
        response = self.patch("windows-policies", policy, {"scope": None})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("scope", response.data)
        policy.refresh_from_db()
        self.assertEqual(policy.scope_id, self.lan.pk)

    def test_class_move_with_option_is_rejected(self):
        self.add_permissions("netbox_windows_dhcp_plugin.change_windowsclass")
        vendor = WindowsClass.objects.create(
            server=self.server, name="phones", class_type=ClassTypeChoices.VENDOR, data="phones"
        )
        OptionValue.objects.create(server=self.server, vendor_class=vendor, code=3, value=["10.1.0.1"])
        response = self.patch("windows-classes", vendor, {"server": self.other.pk})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("server", response.data)
        vendor.refresh_from_db()
        self.assertEqual(vendor.server_id, self.server.pk)


class ExclusionRangeAPITest(*STANDARD):
    user_permissions = SCOPE_VIEW
    model = ExclusionRange
    brief_fields = ["description", "display", "end_address", "id", "start_address", "url"]
    graphql_base_name = "windows_dhcp_exclusion_range"
    bulk_update_data = {"description": "updated"}

    @classmethod
    def setUpTestData(cls):
        lan = scope(windows_server(), "10.1.0.0/24")
        for i in range(1, 4):
            ExclusionRange.objects.create(scope=lan, start_address=f"10.1.0.{i}0", end_address=f"10.1.0.{i}5")
        cls.create_data = [
            {"scope": lan.pk, "start_address": f"10.1.0.{i}0", "end_address": f"10.1.0.{i}9"} for i in range(5, 8)
        ]

    def test_range_outside_prefix_is_rejected(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_exclusionrange")
        response = self.client.post(
            self._get_list_url(),
            {"scope": ExclusionRange.objects.first().scope_id, "start_address": "10.9.0.1", "end_address": "10.9.0.5"},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400)


class WindowsClassAPITest(*STANDARD):
    user_permissions = SERVER_VIEW
    model = WindowsClass
    brief_fields = ["class_type", "description", "display", "id", "name", "url"]
    graphql_base_name = "windows_dhcp_class"
    bulk_update_data = {"description": "updated"}

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        for i in range(1, 4):
            WindowsClass.objects.create(server=server, name=f"class{i}", class_type=ClassTypeChoices.USER, data=f"d{i}")
        cls.create_data = [
            {"server": server.pk, "name": f"vendor{i}", "class_type": ClassTypeChoices.VENDOR, "data": "MSFT"}
            for i in range(1, 4)
        ]


# Vendor option definitions and values also need their vendor class to be visible.
CLASS_VIEW = (*SERVER_VIEW, "netbox_windows_dhcp_plugin.view_windowsclass")


class OptionDefinitionAPITest(*STANDARD):
    user_permissions = CLASS_VIEW
    model = OptionDefinition
    brief_fields = ["code", "display", "id", "name", "url"]
    graphql_base_name = "windows_dhcp_option_definition"
    bulk_update_data = {"description": "updated"}

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        vendor_class = vendor_classes(server)[0]
        for code in range(1, 4):
            OptionDefinition.objects.create(server=server, vendor_class=vendor_class, code=code, name=f"option{code}")
        cls.create_data = [
            {"server": server.pk, "vendor_class": vendor_class.pk, "code": 4, "name": "a", "default_value": ["0x0"]},
            {"server": server.pk, "code": 224, "name": "b", "windows_type": "IPv4Address", "multi_valued": True},
            {"server": server.pk, "family": 6, "code": 99, "name": "c"},
        ]

    def test_user_class_is_rejected(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_optiondefinition")
        server = OptionDefinition.objects.first().server
        user_class = WindowsClass.objects.create(
            server=server, name="users", class_type=ClassTypeChoices.USER, data="u"
        )
        response = self.client.post(
            self._get_list_url(),
            {"server": server.pk, "vendor_class": user_class.pk, "code": 9, "name": "bad"},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400)


class WindowsPolicyAPITest(*STANDARD):
    user_permissions = SCOPE_VIEW
    model = WindowsPolicy
    brief_fields = ["description", "display", "id", "name", "url"]
    graphql_base_name = "windows_dhcp_policy"
    bulk_update_data = {"enabled": False}

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        lan = scope(server, "10.1.0.0/24")
        for i in range(1, 4):
            WindowsPolicy.objects.create(server=server, name=f"p{i}", processing_order=i)
        cls.create_data = [
            {"server": server.pk, "name": "a", "criteria": {"vendor_class": ["EQ", "MSFT"]}},
            {"server": server.pk, "scope": lan.pk, "name": "b", "ip_ranges": [["10.1.0.100", "10.1.0.120"]]},
            {"server": server.pk, "scope": lan.pk, "name": "c", "condition": "and", "lease_duration": 600},
        ]

    def test_invalid_criteria_are_rejected(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_windowspolicy")
        response = self.client.post(
            self._get_list_url(),
            {"server": WindowsPolicy.objects.first().server_id, "name": "bad", "criteria": {"colour": ["EQ", "x"]}},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400)


class OptionValueAPITest(*STANDARD):
    user_permissions = (*SCOPE_VIEW, "netbox_windows_dhcp_plugin.view_reservation", "ipam.view_ipaddress")
    model = OptionValue
    brief_fields = ["code", "display", "id", "url", "value"]
    graphql_base_name = "windows_dhcp_option_value"
    bulk_update_data = {"value": ["10.0.0.9"]}

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        lan = scope(server, "10.1.0.0/24")
        printer = reservation(lan, "10.1.0.50/24", client_id="aa")
        for code in (3, 6, 42):
            OptionValue.objects.create(server=server, code=code, value=["10.0.0.1"])
        cls.create_data = [
            {"server": server.pk, "code": 3, "scope": lan.pk, "value": ["10.1.0.1"]},
            {"server": server.pk, "code": 3, "reservation": printer.pk, "value": ["10.1.0.254"]},
            {"server": server.pk, "code": 15, "value": ["corp, example"]},
        ]

    def test_reservation_value_belongs_to_its_scope(self):
        self.add_permissions(
            "netbox_windows_dhcp_plugin.add_optionvalue", "netbox_windows_dhcp_plugin.view_optionvalue"
        )
        printer = Reservation.objects.get()
        response = self.client.post(
            self._get_list_url(),
            {"server": printer.scope.server_id, "code": 44, "reservation": printer.pk, "value": ["10.0.0.5"]},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["level"], "reservation")
        self.assertEqual(OptionValue.objects.get(pk=response.json()["id"]).scope_id, printer.scope_id)

    def test_option_name_comes_from_the_standard_list_or_the_definition(self):
        self.add_permissions("netbox_windows_dhcp_plugin.view_optionvalue")
        server = WindowsServer.objects.get()
        OptionDefinition.objects.create(server=server, code=224, name="Custom")
        custom = OptionValue.objects.create(server=server, code=224, value=["x"])
        router = OptionValue.objects.get(code=3, scope__isnull=True)
        self.assertEqual(self.client.get(self._get_detail_url(router), **self.header).json()["option_name"], "routers")
        self.assertEqual(self.client.get(self._get_detail_url(custom), **self.header).json()["option_name"], "Custom")

    def test_value_on_another_server_is_rejected(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_optionvalue")
        (other,) = windows_servers(1, start=9)
        response = self.client.post(
            self._get_list_url(),
            {"server": other.pk, "code": 3, "scope": Scope.objects.get().pk, "value": ["10.1.0.1"]},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400)

    def test_value_must_be_a_list(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_optionvalue")
        response = self.client.post(
            self._get_list_url(),
            {"server": WindowsServer.objects.get().pk, "code": 44, "value": "10.0.0.1"},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400)


class FailoverRelationshipAPITest(*STANDARD):
    user_permissions = SCOPE_VIEW
    model = FailoverRelationship
    brief_fields = ["description", "display", "id", "mode", "name", "url"]
    graphql_base_name = "windows_dhcp_failover_relationship"
    bulk_update_data = {"description": "updated"}

    @classmethod
    def setUpTestData(cls):
        primary, partner = windows_servers(2)
        nets = scopes(primary, 3)
        for i in range(1, 4):
            FailoverRelationship.objects.create(name=f"ha{i}", primary_server=primary, partner_server=partner)
        cls.create_data = [
            {"name": "a", "primary_server": primary.pk, "partner_server": partner.pk, "scopes": [nets[0].pk]},
            {
                "name": "b",
                "primary_server": primary.pk,
                "partner_server": partner.pk,
                "mode": "hotstandby",
                "reserve_percent": 5,
                "server_role": "standby",
            },
            {"name": "c", "primary_server": partner.pk, "partner_server": primary.pk, "load_balance_percent": 60},
        ]

    def test_partner_must_differ(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_failoverrelationship")
        server = FailoverRelationship.objects.first().primary_server_id
        response = self.client.post(
            self._get_list_url(),
            {"name": "self", "primary_server": server, "partner_server": server},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400)

    def test_scopes_must_be_ipv4_scopes_of_the_primary(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_failoverrelationship")
        relationship = FailoverRelationship.objects.first()
        primary, partner = relationship.primary_server, relationship.partner_server
        foreign = scope(partner, "10.50.0.0/24")
        v6 = scope(primary, "2001:db8:50::/64")
        for bad in (foreign, v6):
            with self.subTest(scope=str(bad)):
                response = self.client.post(
                    self._get_list_url(),
                    {"name": "bad", "primary_server": primary.pk, "partner_server": partner.pk, "scopes": [bad.pk]},
                    format="json",
                    **self.header,
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("scopes", response.data)

    def test_new_primary_must_hold_the_existing_scopes(self):
        self.add_permissions("netbox_windows_dhcp_plugin.change_failoverrelationship")
        relationship = FailoverRelationship.objects.first()
        relationship.scopes.add(scope(relationship.primary_server, "10.60.0.0/24"))
        response = self.client.patch(
            self._get_detail_url(relationship),
            {"primary_server": relationship.partner_server_id, "partner_server": relationship.primary_server_id},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("scopes", response.data)


class MacFilterAPITest(*STANDARD):
    user_permissions = SERVER_VIEW
    model = MacFilter
    brief_fields = ["description", "display", "id", "list", "mac_address", "url"]
    graphql_base_name = "windows_dhcp_mac_filter"
    bulk_update_data = {"list": MacFilterListChoices.DENY}

    @classmethod
    def setUpTestData(cls):
        server = windows_server()
        for i in range(1, 4):
            MacFilter.objects.create(server=server, mac_address=f"00-15-5d-00-00-0{i}", list="allow")
        cls.create_data = [
            {"server": server.pk, "mac_address": f"00-15-5d-00-01-0{i}", "list": "deny"} for i in range(1, 4)
        ]

    def test_mac_address_is_stored_in_one_spelling(self):
        # Import and push look filters up by this spelling; one typed with colons or capitals would be duplicated.
        self.add_permissions("netbox_windows_dhcp_plugin.add_macfilter")
        server = MacFilter.objects.first().server_id
        response = self.client.post(
            self._get_list_url(),
            {"server": server, "mac_address": "00:15:5D:AA:BB:CC", "list": "deny"},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(MacFilter.objects.get(pk=response.data["id"]).mac_address, "00-15-5d-aa-bb-cc")


class MacFilterListsAPITest(*STANDARD):
    user_permissions = SERVER_VIEW
    model = MacFilterLists
    brief_fields = ["allow_list_enabled", "deny_list_enabled", "display", "id", "url"]
    graphql_base_name = "windows_dhcp_mac_filter_lists"
    bulk_update_data = {"deny_list_enabled": True}

    @classmethod
    def setUpTestData(cls):
        servers = windows_servers(6)
        for server in servers[:3]:
            MacFilterLists.objects.create(server=server, allow_list_enabled=True)
        cls.create_data = [{"server": server.pk, "deny_list_enabled": True} for server in servers[3:]]


class ReadOnlyAPITest(PluginAPIMixin, APITestCase):
    def test_sync_runs_are_read_only(self):
        self.add_permissions("netbox_windows_dhcp_plugin.add_syncrun")
        response = self.client.post("/api/plugins/windows-dhcp/sync-runs/", {}, format="json", **self.header)
        self.assertEqual(response.status_code, 405)

    def test_import_proposals_list(self):
        # The list annotates an item count; an aggregate drops the model's default ordering, which the
        # paginator refuses.
        server = windows_server()
        ImportProposal.objects.create(windows_server=server)
        ImportProposal.objects.create(windows_server=server)
        self.add_permissions("netbox_windows_dhcp_plugin.view_importproposal")
        response = self.client.get("/api/plugins/windows-dhcp/import-proposals/", **self.header)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)


class TestConnectionActionTest(PluginAPIMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.server = windows_server()
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
