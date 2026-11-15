"""
Regression tests for the code quality audit, group g4 (import, review queue, NetBox state): findings F39 to F51.
Each test class names the finding it covers.
"""

import uuid
from unittest import mock, skipUnless

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import DataError, connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from ipam.models import VRF, IPAddress, IPRange, Prefix
from netaddr import IPNetwork
from utilities.testing import APITestCase
from utilities.testing import TestCase as NetBoxTestCase

from netbox_windows_dhcp_plugin import review
from netbox_windows_dhcp_plugin.choices import ProposalItemStatusChoices
from netbox_windows_dhcp_plugin.integrations import branching
from netbox_windows_dhcp_plugin.models import (
    FailoverRelationship,
    ImportProposalItem,
    SyncState,
    WindowsClass,
    WindowsPolicy,
)
from netbox_windows_dhcp_plugin.models import Reservation as NetBoxReservation
from netbox_windows_dhcp_plugin.models import Scope as NetBoxScope
from netbox_windows_dhcp_plugin.models import Superscope as NetBoxSuperscope
from netbox_windows_dhcp_plugin.sync import importer, netbox_state, windows_state
from netbox_windows_dhcp_plugin.sync.mapping import Writer
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import DhcpClass, Reservation, ReservationV6, Scope, ScopeV6
from netbox_windows_dhcp_plugin.windows.client import FailoverRelationship as WinFailover
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from . import fixtures
from .fixtures import PluginAPIMixin, windows_server
from .test_import import ImportTestMixin
from .test_remaining import seed_more
from .test_review import ITEM, ITEMS_APPLY, SCOPE_PERMISSIONS, created_prefixes, make_proposal
from .test_v6 import seed_v6

needs_branching = skipUnless(apps.is_installed("netbox_branching"), "netbox-branching is not installed")
IN_BRANCH = "netbox_windows_dhcp_plugin.integrations.branching.in_branch"
SCOPE_TABLE = 'FROM "netbox_windows_dhcp_plugin_scope"'


def one_scope(fake, start="10.1.0.10", end="10.1.0.200"):
    client = DhcpClient(fake)
    client.add_scope(Scope(name="LAN", start_range=start, end_range=end, subnet_mask="255.255.255.0"))
    return client


def import_and_apply(server, fake):
    """Import a fake server into a proposal and apply everything. Returns the proposal (None without differences)."""
    result = importer.import_to_proposal(server, windows_state.read(DhcpClient(fake)), None)
    if result.proposal is not None:
        importer.apply_items(result.proposal.items.all())
    return result.proposal


def errors(proposal):
    return dict(proposal.items.exclude(error="").values_list("key", "error"))


# -- F39 ---------------------------------------------------------------------------------------------------------


class F39EventQueueTest(TestCase):
    """The event queue is put back for any error, not only the ones the import expects."""

    def test_an_unexpected_error_restores_the_queue(self):
        from netbox.context import events_queue

        token = events_queue.set({"kept": {"snapshots": {"postchange": {"a": 1}}}})
        try:
            with self.assertRaises(RuntimeError), importer._one_change():
                events_queue.get()["rolled-back"] = {"snapshots": {}}
                events_queue.get()["kept"]["snapshots"]["postchange"] = {"a": 2}
                raise RuntimeError("deadlock detected")
            self.assertEqual(list(events_queue.get()), ["kept"])
            self.assertEqual(events_queue.get()["kept"]["snapshots"]["postchange"], {"a": 1})
        finally:
            events_queue.reset(token)

    def test_success_keeps_what_was_queued(self):
        from netbox.context import events_queue

        token = events_queue.set({})
        try:
            with importer._one_change():
                events_queue.get()["new"] = {"snapshots": {}}
            self.assertEqual(list(events_queue.get()), ["new"])
        finally:
            events_queue.reset(token)


def _create_a_prefix_then_crash(self, change):
    Prefix(prefix="203.0.113.0/24").save()
    raise RuntimeError("something nobody expected")


class F39ViewTest(NetBoxTestCase):
    def test_a_crashing_apply_sends_no_event_for_what_was_rolled_back(self):
        server, proposal = make_proposal()
        item = proposal.items.get(kind="scope")
        self.add_permissions(ITEM, *SCOPE_PERMISSIONS)
        self.client.raise_request_exception = False
        with (
            mock.patch("netbox.context_managers.flush_events") as flush,
            mock.patch.object(Writer, "apply", _create_a_prefix_then_crash),
        ):
            response = self.client.post(f"/plugins/windows-dhcp/importproposalitems/{item.pk}/apply/")
        self.assertEqual(response.status_code, 500)
        self.assertFalse(Prefix.objects.exists())
        self.assertEqual(created_prefixes(flush), [])


# -- F40, F41 ----------------------------------------------------------------------------------------------------


class F41DuplicateTargetTest(TestCase):
    """An item that deletes the extra copy of a NetBox duplicate links that copy, not the original."""

    def setUp(self):
        self.server = windows_server()
        self.partner = windows_server("dhcp2")
        self.scope = fixtures.scope(self.server)

    def items(self):
        # Windows holds nothing: every NetBox object becomes a delete item.
        result = importer.import_to_proposal(self.server, windows_state.read(DhcpClient(FakeDhcpServer())), None)
        return {(i.kind, i.key): i for i in result.proposal.items.all()}

    def test_policy(self):
        original = WindowsPolicy.objects.create(server=self.server, scope=self.scope, name="Printers")
        extra = WindowsPolicy.objects.create(server=self.server, scope=self.scope, name="printers", processing_order=2)
        items = self.items()
        self.assertEqual(items["policy", f"10.1.0.0/printers#{extra.pk}"].object_id, extra.pk)
        self.assertEqual(items["policy", "10.1.0.0/printers"].object_id, original.pk)

    def test_superscope(self):
        original = NetBoxSuperscope.objects.create(server=self.server, name="Campus")
        extra = NetBoxSuperscope.objects.create(server=self.server, name="campus")
        items = self.items()
        self.assertEqual(items["superscope", f"campus#{extra.pk}"].object_id, extra.pk)
        self.assertEqual(items["superscope", "campus"].object_id, original.pk)

    def test_failover(self):
        original = FailoverRelationship.objects.create(
            name="HA", primary_server=self.server, partner_server=self.partner
        )
        extra = FailoverRelationship.objects.create(name="ha", primary_server=self.server, partner_server=self.partner)
        items = self.items()
        self.assertEqual(items["failover", f"ha#{extra.pk}"].object_id, extra.pk)
        self.assertEqual(items["failover", "ha"].object_id, original.pk)

    def test_applying_the_delete_removes_the_copy_it_links(self):
        original = WindowsPolicy.objects.create(server=self.server, scope=self.scope, name="Printers")
        extra = WindowsPolicy.objects.create(server=self.server, scope=self.scope, name="printers", processing_order=2)
        item = self.items()["policy", f"10.1.0.0/printers#{extra.pk}"]
        self.assertEqual(importer.apply_items([item]), (1, 0), item.error)
        self.assertEqual(list(WindowsPolicy.objects.values_list("pk", flat=True)), [original.pk])


# -- F42 ---------------------------------------------------------------------------------------------------------


class F42QueriesTest(ImportTestMixin, TestCase):
    def test_permission_checks_do_not_read_the_content_type_per_item(self):
        self.apply_all()
        for name in ("one", "two", "three"):
            self.client.set_scope(
                Scope(scope_id="10.1.0.0", name=name, start_range="10.1.0.10", end_range="10.1.0.200")
            )
            self.propose()
        user = get_user_model().objects.create_user(username="f42", is_superuser=True)
        items = list(review.open_items(ImportProposalItem.objects.filter(action="update")))
        self.assertTrue(items)
        self.assertIsNone(review.missing_permission(user, items[0]))  # warms Django's content type cache
        with CaptureQueriesContext(connection) as queries:
            for item in items * 3:
                self.assertIsNone(review.missing_permission(user, item))
        self.assertFalse([q["sql"] for q in queries if "django_content_type" in q["sql"]])

    def test_option_definitions_are_read_once(self):
        self.apply_all()
        with CaptureQueriesContext(connection) as queries:
            netbox_state.read(self.server, for_import=True)
        reads = [q for q in queries if 'FROM "netbox_windows_dhcp_plugin_optiondefinition"' in q["sql"]]
        self.assertEqual(len(reads), 1)

    def test_dismissed_history_still_hides_the_same_change(self):
        proposal = self.propose().proposal
        self.assertEqual(importer.dismiss_items(list(proposal.items.all())), proposal.items.count())
        result = self.propose()
        self.assertEqual((result.review, result.dismissed), (0, len(result.changes)))


# -- F43 ---------------------------------------------------------------------------------------------------------

LONG_NAME = "v" * 250  # "vendor/" + 250 characters: more than the 255 a key column held before it became text


class F43LongKeyTest(TestCase):
    """A long Windows name is no obstacle (the key columns are text), and a database value error fails one object."""

    def setUp(self):
        self.server = windows_server()
        self.fake = FakeDhcpServer()
        client = one_scope(self.fake)
        client.add_class(DhcpClass(name=LONG_NAME, type="Vendor", data="long"))

    def test_a_key_longer_than_255_characters_is_imported_and_tracked(self):
        key = f"vendor/{LONG_NAME}"
        result = importer.import_to_proposal(self.server, windows_state.read(DhcpClient(self.fake)), None)
        self.assertEqual(result.review, 2)
        self.assertFalse([n for n in result.notes if "cannot be synced" in n])
        item = result.proposal.items.get(kind="class")
        self.assertEqual(item.key, key)
        self.assertEqual(importer.apply_items([item]), (1, 0))
        self.assertTrue(WindowsClass.objects.filter(name=LONG_NAME).exists())
        self.assertTrue(SyncState.objects.filter(windows_server=self.server, kind="class", key=key).exists())

    def test_a_database_value_error_fails_one_item(self):
        result = importer.import_to_proposal(self.server, windows_state.read(DhcpClient(self.fake)), None)
        item = result.proposal.items.get(kind="scope")
        with mock.patch.object(Writer, "apply", side_effect=DataError("value too long for type character varying")):
            self.assertEqual(importer.apply_items([item]), (0, 1))
        item.refresh_from_db()
        self.assertEqual(item.status, ProposalItemStatusChoices.FAILED)
        self.assertIn("value too long", item.error)


# -- F44 ---------------------------------------------------------------------------------------------------------


class F44ItemStatusTest(TestCase):
    """A decision is recorded on the row as it is now, not on the copy loaded when the request started."""

    def setUp(self):
        self.server, self.proposal = make_proposal()
        self.item = self.proposal.items.get(kind="scope")

    def test_an_item_replaced_meanwhile_is_not_brought_back(self):
        stale = ImportProposalItem.objects.get(pk=self.item.pk)
        ImportProposalItem.objects.filter(pk=self.item.pk).delete()
        self.assertFalse(importer._set_item(stale, ProposalItemStatusChoices.APPLIED))
        self.assertFalse(ImportProposalItem.objects.filter(pk=self.item.pk).exists())
        self.assertEqual(importer.dismiss_items([stale]), 0)
        self.assertFalse(ImportProposalItem.objects.filter(pk=self.item.pk).exists())

    def test_a_late_dismiss_does_not_undo_an_apply(self):
        late = ImportProposalItem.objects.select_related("proposal__windows_server").get(pk=self.item.pk)
        self.assertEqual(importer.apply_items([self.item]), (1, 0))
        self.assertEqual(importer.dismiss_items([late]), 0)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.APPLIED)

    def test_a_late_failure_does_not_undo_an_apply(self):
        late = ImportProposalItem.objects.select_related("proposal__windows_server").get(pk=self.item.pk)
        self.assertEqual(importer.apply_items([self.item]), (1, 0))
        with mock.patch.object(Writer, "apply", side_effect=importer.ApplyError("lost the race")):
            # The object is in NetBox by now, so the late copy has nothing left to do and decides nothing.
            self.assertEqual(importer.apply_items([late]), (0, 0))
        self.assertFalse(importer._set_item(late, ProposalItemStatusChoices.FAILED, "lost the race"))
        self.item.refresh_from_db()
        self.assertEqual((self.item.status, self.item.error), (ProposalItemStatusChoices.APPLIED, ""))

    def test_an_item_dismissed_meanwhile_is_not_written(self):
        # Another reviewer dismissed the item after this request loaded it: nothing may reach NetBox, and above all
        # no sync state, which would let a later push delete the scope on Windows.
        ImportProposalItem.objects.filter(pk=self.item.pk).update(status=ProposalItemStatusChoices.DISMISSED)
        self.assertEqual(self.item.status, ProposalItemStatusChoices.PENDING)
        self.assertEqual(importer.apply_items([self.item]), (0, 0))
        self.assertFalse(NetBoxScope.objects.exists())
        self.assertFalse(SyncState.objects.exists())
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.DISMISSED)

    def test_an_item_replaced_meanwhile_is_not_written(self):
        ImportProposalItem.objects.filter(pk=self.item.pk).delete()
        self.assertEqual(importer.apply_items([self.item]), (0, 0))
        self.assertFalse(NetBoxScope.objects.exists())
        self.assertFalse(SyncState.objects.exists())

    def test_a_decision_updates_the_copy_in_hand(self):
        self.assertTrue(importer._set_item(self.item, ProposalItemStatusChoices.FAILED, "why"))
        self.assertEqual((self.item.status, self.item.error), (ProposalItemStatusChoices.FAILED, "why"))
        stored = ImportProposalItem.objects.get(pk=self.item.pk)
        self.assertEqual((stored.status, stored.error), (ProposalItemStatusChoices.FAILED, "why"))
        # A failed item is still open: it can be applied again.
        self.assertEqual(importer.apply_items([stored]), (1, 0))


# -- F46 (and the cheap part of F45) -----------------------------------------------------------------------------


class F46PartialReadTest(ImportTestMixin, TestCase):
    """Reading some kinds only gives exactly the records a full read gives for those kinds."""

    def setUp(self):
        self.partner = windows_server("dhcp2")
        super().setUp()
        seed_more(self.fake)
        seed_v6(self.fake)
        self.apply_all()
        # What Windows cannot hold: the same thing twice. These get "#<pk>" keys.
        scope = NetBoxScope.objects.get(name="LAN")
        vrf = VRF.objects.create(name="elsewhere")
        NetBoxScope.objects.create(
            server=self.server, name="dup", prefix=Prefix.objects.create(prefix="10.1.0.0/24", vrf=vrf)
        )
        NetBoxReservation.objects.create(
            scope=scope, ip_address=IPAddress.objects.create(address="10.1.0.50/24", vrf=vrf), client_id="aa"
        )
        WindowsPolicy.objects.create(server=self.server, scope=scope, name="PRINTERS", processing_order=9)
        NetBoxSuperscope.objects.create(server=self.server, name="CAMPUS")

    def test_every_kind_alone_matches_the_full_read(self):
        for for_import in (True, False):
            full = netbox_state.read(self.server, for_import=for_import)
            kinds = {r.kind for r in full}
            # The fixture must cover every kind, or this test proves nothing for the missing ones.
            self.assertEqual(kinds, set(importer.KIND_MODELS) - ({"filterlists"} - kinds))
            for kind in sorted(importer.KIND_MODELS):
                with self.subTest(kind=kind, for_import=for_import):
                    part = netbox_state.read(self.server, for_import=for_import, kinds=frozenset({kind}))
                    self.assertEqual(part, [r for r in full if r.kind == kind])

    def test_pairs_of_kinds_match_the_full_read(self):
        full = netbox_state.read(self.server, for_import=True)
        for kinds in (
            {"scope", "option"},
            {"reservation", "policy"},
            {"option", "option6"},
            {"superscope", "failover", "macfilter"},
            {"exclusion", "reservation6", "class"},
        ):
            with self.subTest(kinds=kinds):
                part = netbox_state.read(self.server, for_import=True, kinds=frozenset(kinds))
                self.assertEqual(part, [r for r in full if r.kind in kinds])

    def test_no_kinds_reads_nothing(self):
        self.assertEqual(netbox_state.read(self.server, for_import=True, kinds=frozenset()), [])

    def test_applying_a_scope_item_does_not_load_the_reservations(self):
        self.client.set_scope(
            Scope(scope_id="10.2.0.0", name="renamed", start_range="10.2.0.10", end_range="10.2.0.200")
        )
        item = self.propose().proposal.items.get(kind="scope", action="update")
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(importer.apply_items([item]), (1, 0), item.error)
        tables = ("reservation", "optionvalue", "windowspolicy", "exclusionrange", "macfilter")
        loaded = [
            q["sql"] for q in queries if any(f'FROM "netbox_windows_dhcp_plugin_{t}"' in q["sql"] for t in tables)
        ]
        self.assertEqual(loaded, [])
        self.assertEqual(NetBoxScope.objects.get(prefix__prefix="10.2.0.0/24").name, "renamed")

    def test_staleness_is_still_seen_with_a_partial_read(self):
        self.client.set_reservation(
            Reservation(scope_id="10.1.0.0", ip_address="10.1.0.50", client_id="AA-BB-CC-00-11-22", name="from windows")
        )
        item = self.propose().proposal.items.get(kind="reservation", action="update")
        reservation = NetBoxReservation.objects.get(pk=item.object_id)
        reservation.name = "edited in netbox"
        reservation.save()
        self.assertEqual(importer.apply_items([item]), (0, 1))
        self.assertIn("changed in NetBox since the import", item.error)


# -- F47 ---------------------------------------------------------------------------------------------------------


class F47RefusalTest(TestCase):
    """Nothing is applied from inside a branch: the sync state would go to main and the objects to the branch."""

    def setUp(self):
        self.server, self.proposal = make_proposal()
        self.item = self.proposal.items.get(kind="scope")
        self.user = get_user_model().objects.create_user(username="f47", is_superuser=True)

    def assert_nothing_applied(self):
        self.assertFalse(NetBoxScope.objects.exists())
        self.assertFalse(SyncState.objects.exists())
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.PENDING)

    def test_review_apply_refuses_in_a_branch(self):
        self.assertIsNone(review.apply_refusal())
        with mock.patch(IN_BRANCH, return_value=True):
            self.assertEqual(review.apply_refusal(), importer.IN_BRANCH)
            result = review.apply(self.user, review.open_items(self.proposal.items.all()))
        self.assertEqual(result, (0, 0, [importer.IN_BRANCH]))
        self.assert_nothing_applied()

    def test_the_importer_refuses_too(self):
        with mock.patch(IN_BRANCH, return_value=True), self.assertRaises(branching.BranchError):
            importer.apply_items([self.item], user=self.user)
        self.assert_nothing_applied()

    def test_dismissing_stays_possible(self):
        with mock.patch(IN_BRANCH, return_value=True):
            self.assertEqual(review.dismiss(self.user, review.open_items(self.proposal.items.all())), 1)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.DISMISSED)

    def test_on_main_the_same_call_applies(self):
        self.assertEqual(review.apply(self.user, review.open_items(self.proposal.items.all())), (1, 0, []))
        self.assertTrue(SyncState.objects.filter(kind="scope", key="10.1.0.0").exists())


class F47ViewTest(NetBoxTestCase):
    def test_the_ui_says_why(self):
        server, proposal = make_proposal()
        item = proposal.items.get(kind="scope")
        self.add_permissions(ITEM, *SCOPE_PERMISSIONS)
        with mock.patch(IN_BRANCH, return_value=True):
            response = self.client.post(f"/plugins/windows-dhcp/importproposalitems/{item.pk}/apply/", follow=True)
        self.assertIn("leave the branch", " ".join(str(m) for m in response.context["messages"]))
        self.assertFalse(NetBoxScope.objects.exists())
        self.assertFalse(SyncState.objects.exists())


class F47APITest(PluginAPIMixin, APITestCase):
    def test_the_api_applies_nothing_and_says_why(self):
        server, proposal = make_proposal()
        item = proposal.items.get(kind="scope")
        self.add_permissions(ITEM, *SCOPE_PERMISSIONS)
        with mock.patch(IN_BRANCH, return_value=True):
            response = self.client.post(ITEMS_APPLY, {"ids": [item.pk]}, format="json", **self.header)
        # 409 with the reason since the g7 audit fix (F68); it used to be a 200 with the reason listed as a
        # missing permission.
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {"detail": importer.IN_BRANCH})
        self.assertFalse(NetBoxScope.objects.exists())
        self.assertFalse(SyncState.objects.exists())


@needs_branching
class F47RealBranchTest(TransactionTestCase):
    """The same refusal with a real, provisioned branch active."""

    serialized_rollback = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(username=f"f47-{uuid.uuid4().hex[:6]}", is_superuser=True)
        self.addCleanup(self._drop_branches)

    def _drop_branches(self):
        from django.db import connections
        from netbox_branching.models import Branch

        for branch in Branch.objects.all():
            connections[branch.connection_name].close()
            branch.delete()

    def test_apply_in_a_branch_writes_nothing_anywhere(self):
        from netbox_branching.models import Branch
        from netbox_branching.utilities import activate_branch

        server, proposal = make_proposal()
        item = proposal.items.get(kind="scope")
        branch = Branch(name="feature-x", owner=self.user)
        branch.save(provision=False)
        branch.provision(user=self.user)
        branch.refresh_from_db()
        with activate_branch(branch):
            result = review.apply(self.user, review.open_items(proposal.items.all()))
            with self.assertRaises(branching.BranchError):
                importer.apply_items([item], user=self.user)
            self.assertFalse(NetBoxScope.objects.exists())
        self.assertEqual(result, (0, 0, [importer.IN_BRANCH]))
        self.assertFalse(NetBoxScope.objects.exists())
        self.assertFalse(SyncState.objects.exists())
        item.refresh_from_db()
        self.assertEqual(item.status, ProposalItemStatusChoices.PENDING)


# -- F49 ---------------------------------------------------------------------------------------------------------


class F49ScopeLookupTest(ImportTestMixin, TestCase):
    """A child change looks its scope up once."""

    def lookups(self, change):
        with CaptureQueriesContext(connection) as queries:
            Writer(self.server).apply(change)
        return [q["sql"] for q in queries if SCOPE_TABLE in q["sql"] and ">>=" in q["sql"]]

    def changes_of(self, kind):
        return [c for c in self.changes() if c.kind == kind]

    def test_reservation_and_its_option_value(self):
        for change in self.changes():
            if change.kind in ("reservation", "option"):
                break
            Writer(self.server).apply(change)
        (reservation,) = self.changes_of("reservation")
        self.assertEqual(len(self.lookups(reservation)), 1)
        self.assertEqual(NetBoxReservation.objects.count(), 1)
        for option in self.changes_of("option"):
            expected = 0 if option.after["parent"] == "server" else 1
            with self.subTest(key=option.key):
                self.assertEqual(len(self.lookups(option)), expected)
        self.assertEqual(self.changes(), [])

    def test_a_missing_scope_is_still_named(self):
        (reservation,) = self.changes_of("reservation")
        with self.assertRaisesMessage(importer.ApplyError, "scope 10.1.0.0 is not in NetBox yet: apply it first"):
            Writer(self.server).apply(reservation)
        scoped = next(c for c in self.changes_of("option") if c.after["parent"] == "10.1.0.0")
        with self.assertRaisesMessage(importer.ApplyError, "scope 10.1.0.0 is not in NetBox yet: apply it first"):
            Writer(self.server).apply(scoped)


# -- F50 ---------------------------------------------------------------------------------------------------------


class F50SharedRangeTest(TestCase):
    """An import of one server never moves an IP range that another, unrelated server's scope uses."""

    def setUp(self):
        self.first, self.second = windows_server("dhcp1"), windows_server("dhcp2")
        self.fake = FakeDhcpServer()
        self.client = one_scope(self.fake)
        import_and_apply(self.first, self.fake)
        import_and_apply(self.second, self.fake)
        self.assertEqual(IPRange.objects.count(), 1)

    def scope_of(self, server):
        return NetBoxScope.objects.get(server=server)

    def bounds(self, server):
        ip_range = self.scope_of(server).ip_range
        return str(ip_range.start_address), str(ip_range.end_address)

    def narrow(self, start="10.1.0.10", end="10.1.0.99"):
        self.client.set_scope(Scope(scope_id="10.1.0.0", name="LAN", start_range=start, end_range=end))

    def test_an_overlapping_change_is_refused_and_the_other_server_keeps_its_pool(self):
        self.narrow()
        proposal = import_and_apply(self.first, self.fake)
        error = errors(proposal)["10.1.0.0"]
        self.assertIn("is shared with scope", error)
        self.assertIn("dhcp2", error)
        # The ways out it names must work for a scope that already shares the range.
        self.assertIn("edit the shared IP range", error)
        self.assertIn("in another VRF", error)
        self.assertNotIn("give each server its own import VRF", error)
        self.assertEqual(self.bounds(self.second), ("10.1.0.10/24", "10.1.0.200/24"))
        self.assertEqual(self.bounds(self.first), ("10.1.0.10/24", "10.1.0.200/24"))
        (record,) = [r for r in netbox_state.read(self.second) if r.kind == "scope"]
        self.assertEqual((record.fields["start_range"], record.fields["end_range"]), ("10.1.0.10", "10.1.0.200"))

    def test_a_range_elsewhere_gets_its_own_ip_range(self):
        self.narrow("10.1.0.210", "10.1.0.250")
        proposal = import_and_apply(self.first, self.fake)
        self.assertEqual(errors(proposal), {})
        self.assertEqual(self.bounds(self.first), ("10.1.0.210/24", "10.1.0.250/24"))
        self.assertEqual(self.bounds(self.second), ("10.1.0.10/24", "10.1.0.200/24"))
        self.assertEqual(IPRange.objects.count(), 2)

    def test_editing_the_shared_range_by_hand_unblocks_both_servers(self):
        # Both servers got the same change on Windows, so neither import may go first. The remedy the error names:
        # set the shared range to the new bounds in NetBox, then apply the failed items again. No scope is deleted.
        self.narrow()
        first = import_and_apply(self.first, self.fake)
        second = import_and_apply(self.second, self.fake)
        self.assertIn("is shared with scope", errors(first)["10.1.0.0"])
        self.assertIn("is shared with scope", errors(second)["10.1.0.0"])
        shared = IPRange.objects.get()
        shared.snapshot()
        shared.end_address = IPNetwork("10.1.0.99/24")
        shared.save()
        for proposal in (first, second):
            failed = list(proposal.items.filter(status=ProposalItemStatusChoices.FAILED))
            self.assertEqual(importer.apply_items(failed), (1, 0))
        self.assertEqual(IPRange.objects.count(), 1)
        self.assertEqual(self.bounds(self.first), ("10.1.0.10/24", "10.1.0.99/24"))
        self.assertEqual(self.bounds(self.second), ("10.1.0.10/24", "10.1.0.99/24"))
        for server in (self.first, self.second):
            changes, _notes = importer.compute(server, windows_state.read(DhcpClient(self.fake)))
            self.assertEqual(changes, [])

    def test_an_own_range_in_another_vrf_unblocks_the_scope(self):
        # The other remedy, for servers that did not change alike: one scope gets its own prefix and IP range in
        # another VRF (NetBox refuses overlapping ranges in one VRF), then the failed item is applied again.
        self.narrow()
        proposal = import_and_apply(self.first, self.fake)
        self.assertIn("is shared with scope", errors(proposal)["10.1.0.0"])
        vrf = VRF.objects.create(name="dhcp1")
        scope = self.scope_of(self.first)
        scope.snapshot()
        scope.prefix = fixtures.prefix("10.1.0.0/24", vrf=vrf)
        scope.ip_range = fixtures.ip_range("10.1.0.10/24", "10.1.0.200/24", vrf=vrf)
        scope.save()
        failed = list(proposal.items.filter(status=ProposalItemStatusChoices.FAILED))
        self.assertEqual(importer.apply_items(failed), (1, 0))
        self.assertEqual(self.bounds(self.first), ("10.1.0.10/24", "10.1.0.99/24"))
        self.assertEqual(self.scope_of(self.first).ip_range.vrf, vrf)
        self.assertEqual(self.bounds(self.second), ("10.1.0.10/24", "10.1.0.200/24"))

    def test_a_later_import_vrf_does_not_move_a_scope_that_shares_a_range(self):
        # Why the error does not name the import VRF as a way out: an existing scope keeps its prefix's VRF.
        self.first.import_vrf = VRF.objects.create(name="dhcp1")
        self.first.save()
        self.narrow()
        proposal = import_and_apply(self.first, self.fake)
        self.assertIn("is shared with scope", errors(proposal)["10.1.0.0"])

    def test_an_import_vrf_set_before_the_first_import_keeps_a_server_apart(self):
        other = windows_server("dhcp3", import_vrf=VRF.objects.create(name="dhcp3"))
        import_and_apply(other, self.fake)
        self.assertEqual(IPRange.objects.count(), 2)
        self.narrow()
        proposal = import_and_apply(other, self.fake)
        self.assertEqual(errors(proposal), {})
        self.assertEqual(self.bounds(other), ("10.1.0.10/24", "10.1.0.99/24"))
        self.assertEqual(self.bounds(self.first), ("10.1.0.10/24", "10.1.0.200/24"))

    def test_failover_partners_still_move_the_range_together(self):
        relationship = FailoverRelationship.objects.create(
            name="ha", primary_server=self.first, partner_server=self.second
        )
        relationship.scopes.add(self.scope_of(self.first))
        self.client.add_failover(WinFailover(name="ha", partner_server="dhcp2.example.com", scope_ids=["10.1.0.0"]))
        self.narrow()
        proposal = import_and_apply(self.first, self.fake)
        self.assertEqual(errors(proposal), {})
        self.assertEqual(IPRange.objects.count(), 1)
        self.assertEqual(self.bounds(self.second), ("10.1.0.10/24", "10.1.0.99/24"))

    def test_the_partner_side_moves_it_too(self):
        # The relationship lists the primary's scope only; the import of the partner must still see the pair.
        relationship = FailoverRelationship.objects.create(
            name="ha", primary_server=self.first, partner_server=self.second
        )
        relationship.scopes.add(self.scope_of(self.first))
        self.narrow()
        change = next(c for c in importer.compute(self.second, windows_state.read(DhcpClient(self.fake)))[0])
        self.assertEqual((change.kind, change.action), ("scope", "update"))
        Writer(self.second).apply(change)
        self.assertEqual(IPRange.objects.count(), 1)
        self.assertEqual(self.bounds(self.first), ("10.1.0.10/24", "10.1.0.99/24"))

    def test_a_relationship_for_another_scope_does_not_count(self):
        elsewhere = fixtures.scope(self.first, "10.9.0.0/24")
        relationship = FailoverRelationship.objects.create(
            name="ha", primary_server=self.first, partner_server=self.second
        )
        relationship.scopes.add(elsewhere)
        self.narrow()
        change = next(
            c for c in importer.compute(self.first, windows_state.read(DhcpClient(self.fake)))[0] if c.key == "10.1.0.0"
        )
        with self.assertRaisesMessage(importer.ApplyError, "is shared with scope"):
            Writer(self.first).apply(change)
        self.assertEqual(self.bounds(self.second), ("10.1.0.10/24", "10.1.0.200/24"))

    def test_a_server_alone_on_its_range_still_moves_it(self):
        NetBoxScope.objects.filter(server=self.second).delete()
        self.narrow()
        proposal = import_and_apply(self.first, self.fake)
        self.assertEqual(errors(proposal), {})
        self.assertEqual(IPRange.objects.count(), 1)
        self.assertEqual(self.bounds(self.first), ("10.1.0.10/24", "10.1.0.99/24"))


# -- F51 ---------------------------------------------------------------------------------------------------------


class F51VrfTest(TestCase):
    """Addresses and first ranges of a scope made by hand go in the VRF of its prefix, not the import VRF."""

    def setUp(self):
        self.server = windows_server()
        self.blue = VRF.objects.create(name="blue")
        self.fake = FakeDhcpServer()
        self.client = one_scope(self.fake)

    def test_a_reservation_address_follows_the_prefix_vrf(self):
        scope = NetBoxScope.objects.create(
            server=self.server,
            name="LAN",
            prefix=fixtures.prefix("10.1.0.0/24", vrf=self.blue),
            ip_range=fixtures.ip_range("10.1.0.10/24", "10.1.0.200/24", vrf=self.blue),
        )
        # The same address elsewhere, in the global table the server imports into: another site's host.
        unrelated = fixtures.ip_address("10.1.0.50/24")
        self.client.add_reservation(
            Reservation(scope_id="10.1.0.0", ip_address="10.1.0.50", client_id="AA-BB-CC-00-11-22", name="printer")
        )
        proposal = import_and_apply(self.server, self.fake)
        self.assertEqual(errors(proposal), {})
        reservation = NetBoxReservation.objects.get(scope=scope)
        self.assertEqual(reservation.ip_address.vrf, self.blue)
        self.assertNotEqual(reservation.ip_address.pk, unrelated.pk)
        self.assertFalse(NetBoxReservation.objects.filter(ip_address=unrelated).exists())
        self.assertEqual(IPAddress.objects.filter(vrf=None).count(), 1)

    def test_a_first_range_follows_the_prefix_vrf(self):
        scope = NetBoxScope.objects.create(
            server=self.server, name="LAN", prefix=fixtures.prefix("10.1.0.0/24", vrf=self.blue)
        )
        # A range with the same bounds in the global table must not be taken for this scope's.
        unrelated = fixtures.ip_range("10.1.0.10/24", "10.1.0.200/24")
        proposal = import_and_apply(self.server, self.fake)
        self.assertEqual(errors(proposal), {})
        scope.refresh_from_db()
        self.assertEqual(scope.ip_range.vrf, self.blue)
        self.assertNotEqual(scope.ip_range.pk, unrelated.pk)

    def test_a_dhcpv6_reservation_address_follows_the_prefix_vrf(self):
        scope = NetBoxScope.objects.create(
            server=self.server, name="v6", prefix=fixtures.prefix("2001:db8:1::/64", vrf=self.blue)
        )
        client = DhcpClient(self.fake)
        client.add_v6_scope(ScopeV6(prefix="2001:db8:1::", name="v6"))
        client.add_v6_reservation(
            ReservationV6(
                prefix="2001:db8:1::", ip_address="2001:db8:1::10", client_duid="00-01-00-01-aa-bb-cc-dd", iaid=7
            )
        )
        proposal = import_and_apply(self.server, self.fake)
        self.assertEqual(errors(proposal), {})
        self.assertEqual(NetBoxReservation.objects.get(scope=scope).ip_address.vrf, self.blue)

    def test_new_scopes_still_go_in_the_import_vrf(self):
        server = windows_server("dhcp2", import_vrf=self.blue)
        self.client.add_reservation(
            Reservation(scope_id="10.1.0.0", ip_address="10.1.0.50", client_id="AA-BB-CC-00-11-22", name="printer")
        )
        proposal = import_and_apply(server, self.fake)
        self.assertEqual(errors(proposal), {})
        scope = NetBoxScope.objects.get(server=server)
        self.assertEqual((scope.prefix.vrf, scope.ip_range.vrf), (self.blue, self.blue))
        self.assertEqual(NetBoxReservation.objects.get(scope=scope).ip_address.vrf, self.blue)
