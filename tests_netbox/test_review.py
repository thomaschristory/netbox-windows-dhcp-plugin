"""The review queue from the UI and the REST API: permissions, and writes made as the reviewing user."""

from contextlib import nullcontext
from unittest import skipUnless
from unittest.mock import patch

from core.models import ObjectChange, ObjectType
from django.apps import apps
from ipam.models import Prefix
from utilities.testing import APITestCase, TestCase

from netbox_windows_dhcp_plugin.choices import ProposalItemStatusChoices, ProposalStatusChoices
from netbox_windows_dhcp_plugin.models import ImportProposalItem
from netbox_windows_dhcp_plugin.models import Scope as NetBoxScope
from netbox_windows_dhcp_plugin.sync import importer, windows_state
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import Scope
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .fixtures import PluginAPIMixin, windows_server

# Everything applying a scope writes.
SCOPE_PERMISSIONS = (
    "netbox_windows_dhcp_plugin.add_scope",
    "netbox_windows_dhcp_plugin.view_scope",
    "ipam.add_prefix",
    "ipam.add_iprange",
)
ITEM = "netbox_windows_dhcp_plugin.change_importproposalitem"
# What starting an import needs when it opens a branch and a change request.
BRANCH_PERMISSIONS = tuple(
    perm
    for perm, app in (
        ("netbox_branching.add_branch", "netbox_branching"),
        ("netbox_change_control.add_changerequest", "netbox_change_control"),
    )
    if apps.is_installed(app)
)
PROPOSAL = "netbox_windows_dhcp_plugin.change_importproposal"
ITEMS_APPLY = "/api/plugins/windows-dhcp/import-proposal-items/apply/"


def created_prefixes(flush):
    return [
        event["data"]["prefix"]
        for call in flush.call_args_list
        for event in call.args[0]
        if event["object_type"].app_label == "ipam"
        and event["object_type"].model == "prefix"
        and event["event_type"] == "object_created"
    ]


def make_proposal():
    server = windows_server()
    fake = FakeDhcpServer()
    DhcpClient(fake).add_scope(
        Scope(name="LAN", start_range="10.1.0.10", end_range="10.1.0.200", subnet_mask="255.255.255.0")
    )
    result = importer.import_to_proposal(server, windows_state.read(DhcpClient(fake)), None)
    return server, result.proposal


class EventQueueTest(TestCase):
    """_one_change: a change's events reach the request's queue only if it succeeds, coalesced as NetBox would."""

    def setUp(self):
        from django.test import RequestFactory

        super().setUp()
        self.request = RequestFactory().get("/")
        self.request.user = self.user

    def _enqueue(self, instance, event_type):
        from extras.events import enqueue_event
        from netbox.context import events_queue

        instance.snapshot()
        enqueue_event(events_queue.get(), instance, self.request, event_type)

    def _tracked(self, queue):
        from netbox.context import events_queue

        token = events_queue.set(queue)
        self.addCleanup(events_queue.reset, token)
        return queue

    def test_rollback_leaves_earlier_events_untouched(self):
        queue = self._tracked({})
        prefix = Prefix.objects.create(prefix="192.0.2.0/24")
        self._enqueue(prefix, "object_created")
        first = queue[f"ipam.prefix:{prefix.pk}"]
        with self.assertRaises(importer.ApplyError), importer._one_change():
            # Its own copy, as a writer loads it: the rolled back edit must not show in the earlier event.
            edited = Prefix.objects.get(pk=prefix.pk)
            edited.prefix = "198.51.100.0/24"
            edited.save()
            self._enqueue(edited, "object_updated")
            other = Prefix.objects.create(prefix="203.0.113.0/24")
            self._enqueue(other, "object_created")
            raise importer.ApplyError("refused")
        self.assertEqual(list(queue), [f"ipam.prefix:{prefix.pk}"])
        self.assertIs(queue[f"ipam.prefix:{prefix.pk}"], first)
        self.assertEqual(first["snapshots"]["postchange"]["prefix"], "192.0.2.0/24")
        self.assertEqual(first["data"]["prefix"], "192.0.2.0/24")
        self.assertIs(first["object"], prefix)

    def test_success_coalesces_like_netbox(self):
        # The same saves, once queued straight into one queue (what NetBox does) and once through _one_change.
        def saves(through_changes):
            queue = {}
            from netbox.context import events_queue

            token = events_queue.set(queue)
            try:
                kept = Prefix.objects.create(prefix="192.0.2.0/24")
                gone = Prefix.objects.create(prefix="198.51.100.0/24")
                self._enqueue(kept, "object_created")
                self._enqueue(gone, "object_created")
                with importer._one_change() if through_changes else nullcontext():
                    kept.description = "edited"
                    kept.save()
                    self._enqueue(kept, "object_updated")
                with importer._one_change() if through_changes else nullcontext():
                    self._enqueue(gone, "object_deleted")
                    gone.delete()
            finally:
                events_queue.reset(token)

            def fields(snapshot):
                # Timestamps and IDs differ between the two runs.
                return None if snapshot is None else (snapshot["prefix"], snapshot["description"])

            return [
                (
                    e["event_type"],
                    fields(e["snapshots"]["prechange"]),
                    fields(e["snapshots"]["postchange"]),
                    e["data"].get("prefix"),
                    e["data"].get("description"),
                )
                for e in queue.values()
            ]

        direct = saves(False)
        Prefix.objects.all().delete()
        self.assertEqual(saves(True), direct)
        self.assertEqual([row[0] for row in direct], ["object_created", "object_deleted"])
        self.assertEqual(direct[0][4], "edited")

    def test_cost_does_not_grow_with_the_queue(self):
        # #101: the request's queue used to be copied for every change, so applying N items cost N x (events so far).
        # Now a change only adds its own events: nothing ever walks the request's queue.
        class Watched(dict):
            walks = 0

            def __iter__(self):
                Watched.walks += 1
                return super().__iter__()

            def items(self):
                Watched.walks += 1
                return super().items()

            def values(self):
                Watched.walks += 1
                return super().values()

            def copy(self):
                Watched.walks += 1
                return super().copy()

        queue = self._tracked(Watched())
        for n in range(50):
            with importer._one_change():
                self._enqueue(Prefix.objects.create(prefix=f"10.{n}.0.0/24"), "object_created")
        self.assertEqual(len(queue), 50)
        self.assertEqual(Watched.walks, 0)


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
        scope = NetBoxScope.objects.get()
        change = ObjectChange.objects.get(changed_object_id=scope.pk, changed_object_type__model="scope")
        self.assertEqual(change.user, self.user)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ProposalStatusChoices.APPLIED)

    def test_apply_without_target_permission_leaves_the_item_pending(self):
        self.add_permissions(ITEM)
        self.client.post(f"/plugins/windows-dhcp/importproposalitems/{self.item.pk}/apply/")
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.PENDING)
        self.assertFalse(NetBoxScope.objects.exists())

    def test_apply_needs_permission_on_every_object_written(self):
        # add_scope alone must not let a reviewer create prefixes and IP ranges through an import.
        self.add_permissions(ITEM, *(p for p in SCOPE_PERMISSIONS if p != "ipam.add_prefix"))
        with patch("netbox.context_managers.flush_events") as flush:
            self.client.post(f"/plugins/windows-dhcp/importproposalitems/{self.item.pk}/apply/")
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.FAILED)
        self.assertIn("ipam.add_prefix", self.item.error)
        self.assertFalse(NetBoxScope.objects.exists())
        self.assertFalse(Prefix.objects.exists())
        self.assertEqual(created_prefixes(flush), [])

    def test_apply_respects_permission_constraints(self):
        from users.models import ObjectPermission

        self.add_permissions(ITEM, *(p for p in SCOPE_PERMISSIONS if p != "ipam.add_prefix"))
        permission = ObjectPermission.objects.create(
            name="prefixes in 192.0.2.0/24 only", actions=["add"], constraints={"prefix": "192.0.2.0/24"}
        )
        permission.object_types.add(ObjectType.objects.get_for_model(Prefix))
        permission.users.add(self.user)
        with patch("netbox.context_managers.flush_events") as flush:
            self.client.post(f"/plugins/windows-dhcp/importproposalitems/{self.item.pk}/apply/")
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.FAILED)
        self.assertFalse(Prefix.objects.exists())
        self.assertEqual(created_prefixes(flush), [])

    def test_apply_needs_change_on_items(self):
        self.add_permissions(*SCOPE_PERMISSIONS)
        response = self.client.post(f"/plugins/windows-dhcp/importproposalitems/{self.item.pk}/apply/")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(NetBoxScope.objects.exists())

    def test_dismiss_closes_the_proposal(self):
        self.add_permissions(PROPOSAL, ITEM)
        self.client.post(f"/plugins/windows-dhcp/importproposals/{self.proposal.pk}/dismiss/")
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ProposalStatusChoices.DISMISSED)
        self.assertFalse(NetBoxScope.objects.exists())

    def test_bulk_apply_selected(self):
        self.add_permissions(ITEM, *SCOPE_PERMISSIONS)
        self.client.post("/plugins/windows-dhcp/importproposalitems/apply/", {"pk": [self.item.pk]})
        self.assertTrue(NetBoxScope.objects.exists())

    def test_object_permissions_limit_which_server_can_be_imported(self):
        from users.models import ObjectPermission

        permission = ObjectPermission.objects.create(
            name="other servers only", actions=["view", "change"], constraints={"host": "elsewhere.example.com"}
        )
        permission.object_types.add(ObjectType.objects.get_for_model(type(self.server)))
        permission.users.add(self.user)
        self.add_permissions(*BRANCH_PERMISSIONS)
        response = self.client.post(f"/plugins/windows-dhcp/windowsservers/{self.server.pk}/import/")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.server.jobs.exists())

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
            "netbox_windows_dhcp_plugin.view_windowsserver",
            "netbox_windows_dhcp_plugin.change_windowsserver",
            *BRANCH_PERMISSIONS,
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
        self.assertTrue(NetBoxScope.objects.exists())

    def test_apply_reports_missing_permissions(self):
        self.add_permissions(ITEM)
        response = self.client.post(
            "/api/plugins/windows-dhcp/import-proposal-items/apply/",
            {"ids": [self.item.pk]},
            format="json",
            **self.header,
        )
        self.assertEqual(response.data["missing_permissions"], ["netbox_windows_dhcp_plugin.add_scope"])
        self.assertFalse(NetBoxScope.objects.exists())

    def test_denied_supporting_write_dispatches_no_creation_event(self):
        self.add_permissions(ITEM, *(p for p in SCOPE_PERMISSIONS if p != "ipam.add_prefix"))
        with patch("netbox.context_managers.flush_events") as flush:
            response = self.client.post(ITEMS_APPLY, {"ids": [self.item.pk]}, format="json", **self.header)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data["applied"], response.data["failed"]), (0, 1))
        self.assertFalse(Prefix.objects.exists())
        self.assertEqual(created_prefixes(flush), [])

    def test_failed_item_keeps_successful_item_events(self):
        from users.models import ObjectPermission

        self.add_permissions(ITEM, *(p for p in SCOPE_PERMISSIONS if p != "ipam.add_prefix"))
        permission = ObjectPermission.objects.create(
            name="first prefix only", actions=["add"], constraints={"prefix": "10.1.0.0/24"}
        )
        permission.object_types.add(ObjectType.objects.get_for_model(Prefix))
        permission.users.add(self.user)
        denied = ImportProposalItem.objects.create(
            proposal=self.proposal,
            sequence=self.item.sequence + 1,
            action="create",
            kind="scope",
            key="10.2.0.0",
            after={**self.item.after, "start_range": "10.2.0.10", "end_range": "10.2.0.200"},
        )
        with patch("netbox.context_managers.flush_events") as flush:
            response = self.client.post(ITEMS_APPLY, {"ids": [self.item.pk, denied.pk]}, format="json", **self.header)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data["applied"], response.data["failed"]), (1, 1))
        self.assertEqual([str(prefix) for prefix in Prefix.objects.values_list("prefix", flat=True)], ["10.1.0.0/24"])
        self.assertEqual(created_prefixes(flush), ["10.1.0.0/24"])

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
            "netbox_windows_dhcp_plugin.view_windowsserver",
            "netbox_windows_dhcp_plugin.change_windowsserver",
            *BRANCH_PERMISSIONS,
        )
        url = f"/api/plugins/windows-dhcp/windows-servers/{self.server.pk}/import/"
        response = self.client.post(url, {}, format="json", **self.header)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["name"], "Windows DHCP import")
        # A second request while the first is queued is refused.
        response = self.client.post(url, {}, format="json", **self.header)
        self.assertEqual(response.status_code, 409)

    @skipUnless(apps.is_installed("netbox_branching"), "netbox-branching is not installed")
    def test_import_action_needs_branch_permission_with_branching(self):
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_windowsserver", "netbox_windows_dhcp_plugin.change_windowsserver"
        )
        url = f"/api/plugins/windows-dhcp/windows-servers/{self.server.pk}/import/"
        response = self.client.post(url, {}, format="json", **self.header)
        self.assertEqual(response.status_code, 403)

    def test_import_action_needs_change(self):
        self.add_permissions("netbox_windows_dhcp_plugin.view_windowsserver")
        url = f"/api/plugins/windows-dhcp/windows-servers/{self.server.pk}/import/"
        response = self.client.post(url, {}, format="json", **self.header)
        self.assertEqual(response.status_code, 404)
