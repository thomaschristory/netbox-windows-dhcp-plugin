"""The review queue from the UI and the REST API: permissions, and writes made as the reviewing user."""

from core.models import ObjectChange
from netbox_dhcp.models import Subnet
from utilities.testing import APITestCase, TestCase

from netbox_windows_dhcp_plugin.choices import ProposalItemStatusChoices, ProposalStatusChoices
from netbox_windows_dhcp_plugin.sync import importer, windows_state
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import Scope
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .fixtures import PluginAPIMixin, dhcp_servers, ensure_standard_option_definitions, windows_server

# Everything applying a scope writes.
SCOPE_PERMISSIONS = (
    "netbox_dhcp.add_subnet",
    "netbox_dhcp.view_subnet",
)
ITEM = "netbox_windows_dhcp_plugin.change_importproposalitem"
PROPOSAL = "netbox_windows_dhcp_plugin.change_importproposal"
ITEMS_APPLY = "/api/plugins/windows-dhcp/import-proposal-items/apply/"


def make_proposal():
    ensure_standard_option_definitions()
    server = windows_server(dhcp_servers(1)[0])
    fake = FakeDhcpServer()
    DhcpClient(fake).add_scope(
        Scope(name="LAN", start_range="10.1.0.10", end_range="10.1.0.200", subnet_mask="255.255.255.0")
    )
    result = importer.import_to_proposal(server, windows_state.read(DhcpClient(fake)), None)
    return server, result.proposal


class ReviewViewTest(TestCase):
    def setUp(self):
        super().setUp()
        self.server, self.proposal = make_proposal()
        self.item = self.proposal.items.get(kind="scope")

    def test_apply_writes_as_the_reviewing_user(self):
        self.add_permissions(ITEM, *SCOPE_PERMISSIONS)
        response = self.client.post(f"/plugins/windows-dhcp/importproposalitems/{self.item.pk}/apply/")
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.APPLIED, self.item.error)
        subnet = Subnet.objects.get()
        change = ObjectChange.objects.get(changed_object_id=subnet.pk, changed_object_type__model="subnet")
        self.assertEqual(change.user, self.user)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ProposalStatusChoices.APPLIED)

    def test_apply_without_target_permission_leaves_the_item_pending(self):
        self.add_permissions(ITEM)
        self.client.post(f"/plugins/windows-dhcp/importproposalitems/{self.item.pk}/apply/")
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.PENDING)
        self.assertFalse(Subnet.objects.exists())

    def test_apply_needs_change_on_items(self):
        self.add_permissions(*SCOPE_PERMISSIONS)
        response = self.client.post(f"/plugins/windows-dhcp/importproposalitems/{self.item.pk}/apply/")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Subnet.objects.exists())

    def test_dismiss_closes_the_proposal(self):
        self.add_permissions(PROPOSAL, ITEM)
        self.client.post(f"/plugins/windows-dhcp/importproposals/{self.proposal.pk}/dismiss/")
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ProposalStatusChoices.DISMISSED)
        self.assertFalse(Subnet.objects.exists())

    def test_bulk_apply_selected(self):
        self.add_permissions(ITEM, *SCOPE_PERMISSIONS)
        self.client.post("/plugins/windows-dhcp/importproposalitems/apply/", {"pk": [self.item.pk]})
        self.assertTrue(Subnet.objects.exists())

    def test_detail_pages_show_review_buttons(self):
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_importproposal",
            "netbox_windows_dhcp_plugin.view_importproposalitem",
            PROPOSAL,
            ITEM,
        )
        response = self.client.get(self.proposal.get_absolute_url())
        self.assertContains(response, f"/importproposals/{self.proposal.pk}/apply/")
        response = self.client.get(self.item.get_absolute_url())
        self.assertContains(response, f"/importproposalitems/{self.item.pk}/dismiss/")
        response = self.client.get("/plugins/windows-dhcp/importproposalitems/")
        self.assertContains(response, "/importproposalitems/apply/")

    def test_import_now_button_and_view(self):
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_windowsserver", "netbox_windows_dhcp_plugin.change_windowsserver"
        )
        response = self.client.get(self.server.get_absolute_url())
        self.assertContains(response, f"/windowsservers/{self.server.pk}/import/")
        response = self.client.post(f"/plugins/windows-dhcp/windowsservers/{self.server.pk}/import/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.server.jobs.get().name, "Windows DHCP import")


class ReviewAPITest(PluginAPIMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.server, self.proposal = make_proposal()
        self.item = self.proposal.items.get(kind="scope")

    def test_apply_items(self):
        self.add_permissions(ITEM, *SCOPE_PERMISSIONS)
        response = self.client.post(
            "/api/plugins/windows-dhcp/import-proposal-items/apply/",
            {"ids": [self.item.pk]},
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {"applied": 1, "failed": 0, "missing_permissions": []})
        self.assertTrue(Subnet.objects.exists())

    def test_apply_reports_missing_permissions(self):
        self.add_permissions(ITEM)
        response = self.client.post(
            "/api/plugins/windows-dhcp/import-proposal-items/apply/",
            {"ids": [self.item.pk]},
            format="json",
            **self.header,
        )
        self.assertEqual(response.data["missing_permissions"], ["netbox_dhcp.add_subnet"])
        self.assertFalse(Subnet.objects.exists())

    def test_ids_are_required(self):
        self.add_permissions(ITEM)
        response = self.client.post(
            "/api/plugins/windows-dhcp/import-proposal-items/dismiss/", {}, format="json", **self.header
        )
        self.assertEqual(response.status_code, 400)

    def test_items_the_user_cannot_change_are_ignored(self):
        self.add_permissions(*SCOPE_PERMISSIONS, "netbox_windows_dhcp_plugin.view_importproposalitem")
        response = self.client.post(
            "/api/plugins/windows-dhcp/import-proposal-items/apply/",
            {"ids": [self.item.pk]},
            format="json",
            **self.header,
        )
        self.assertEqual(response.data["applied"], 0)

    def test_dismiss_proposal(self):
        self.add_permissions(PROPOSAL, ITEM, "netbox_windows_dhcp_plugin.view_importproposal")
        response = self.client.post(
            f"/api/plugins/windows-dhcp/import-proposals/{self.proposal.pk}/dismiss/", {}, format="json", **self.header
        )
        self.assertEqual(response.data, {"dismissed": 1})

    def test_import_action(self):
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_windowsserver", "netbox_windows_dhcp_plugin.change_windowsserver"
        )
        url = f"/api/plugins/windows-dhcp/windows-servers/{self.server.pk}/import/"
        response = self.client.post(url, {}, format="json", **self.header)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["name"], "Windows DHCP import")

    def test_import_action_needs_change(self):
        self.add_permissions("netbox_windows_dhcp_plugin.view_windowsserver")
        url = f"/api/plugins/windows-dhcp/windows-servers/{self.server.pk}/import/"
        response = self.client.post(url, {}, format="json", **self.header)
        self.assertEqual(response.status_code, 404)
