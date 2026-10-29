import dataclasses
import uuid
from datetime import timedelta
from unittest import mock, skipUnless

from core.choices import JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job
from dcim.models import MACAddress
from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from ipam.models import IPAddress, IPRange, Prefix

from netbox_windows_dhcp_plugin.choices import (
    ProposalItemStatusChoices,
    ProposalStatusChoices,
    SyncKindChoices,
    SyncStatusChoices,
)
from netbox_windows_dhcp_plugin.jobs import ImportJob, due_servers
from netbox_windows_dhcp_plugin.models import (
    ExclusionRange,
    ImportProposal,
    OptionDefinition,
    OptionValue,
    SyncRun,
    WindowsClass,
)
from netbox_windows_dhcp_plugin.models import Reservation as NetBoxReservation
from netbox_windows_dhcp_plugin.models import Scope as NetBoxScope
from netbox_windows_dhcp_plugin.sync import importer, windows_state
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import (
    DhcpClass,
    OptionKey,
    Reservation,
    Scope,
)
from netbox_windows_dhcp_plugin.windows.client import (
    ExclusionRange as WinExclusion,
)
from netbox_windows_dhcp_plugin.windows.client import (
    OptionDefinition as WinOptionDefinition,
)
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .fixtures import windows_server
from .test_jobs import CREDENTIALS


def seed(fake):
    c = DhcpClient(fake)
    c.add_scope(
        Scope(
            name="LAN",
            description="office floor 1",
            start_range="10.1.0.10",
            end_range="10.1.0.200",
            subnet_mask="255.255.255.0",
            lease_duration="1.00:00:00",
        )
    )
    c.add_exclusion_range(WinExclusion(scope_id="10.1.0.0", start_range="10.1.0.10", end_range="10.1.0.19"))
    c.add_reservation(
        Reservation(scope_id="10.1.0.0", ip_address="10.1.0.50", client_id="AA-BB-CC-00-11-22", name="printer")
    )
    c.add_class(DhcpClass(name="MSFT", type="Vendor", data="MSFT 5.0"))
    c.add_class(DhcpClass(name="Labs", type="User", data="labs"))
    c.add_option_definition(WinOptionDefinition(option_id=224, name="Custom", type="String"))
    c.add_option_definition(WinOptionDefinition(option_id=2, name="Release", type="DWord", vendor_class="MSFT"))
    c.set_option_value(OptionKey(option_id=6), ["10.0.0.1", "10.0.0.2"])
    c.set_option_value(OptionKey(option_id=3, scope_id="10.1.0.0"), ["10.1.0.1"])
    c.set_option_value(OptionKey(option_id=224, scope_id="10.1.0.0"), ["hello"])
    c.set_option_value(OptionKey(option_id=2, vendor_class="MSFT"), ["1"])
    c.set_option_value(OptionKey(option_id=6, scope_id="10.1.0.0", user_class="Labs"), ["10.9.9.9"])
    c.set_option_value(OptionKey(option_id=3, scope_id="10.1.0.0", reserved_ip="10.1.0.50"), ["10.1.0.254"])
    return c


class ImportTestMixin:
    def setUp(self):
        self.server = windows_server()
        self.fake = FakeDhcpServer()
        self.client = seed(self.fake)

    def state(self):
        return windows_state.read(DhcpClient(self.fake))

    def changes(self):
        return importer.compute(self.server, self.state())[0]

    def propose(self):
        return importer.import_to_proposal(self.server, self.state(), None)

    def apply_all(self):
        result = self.propose()
        applied, failed = importer.apply_items(result.proposal.items.all())
        errors = list(result.proposal.items.exclude(error="").values_list("key", "error"))
        self.assertEqual(failed, 0, errors)
        return result


class ProposalImportTest(ImportTestMixin, TestCase):
    def test_first_import_proposes_every_object(self):
        result = self.propose()
        kinds = sorted(result.proposal.items.values_list("kind", flat=True))
        self.assertEqual(
            kinds,
            ["class", "class", "exclusion", "option", "option", "option", "option", "option", "option"]
            + ["optiondef", "optiondef", "reservation", "scope"],
        )
        self.assertTrue(all(i.action == "create" for i in result.proposal.items.all()))
        # Proposing writes nothing but the queue.
        self.assertFalse(NetBoxScope.objects.exists())

    def test_items_follow_dependency_order(self):
        items = list(self.propose().proposal.items.all())
        kinds = [i.kind for i in items]
        self.assertLess(kinds.index("scope"), kinds.index("reservation"))
        self.assertLess(kinds.index("class"), kinds.index("optiondef"))
        self.assertLess(kinds.index("reservation"), kinds.index("option"))

    def test_apply_writes_dhcp_and_core_objects(self):
        self.apply_all()
        scope = NetBoxScope.objects.get()
        self.assertEqual(scope.name, "LAN")
        self.assertEqual(scope.description, "office floor 1")
        self.assertEqual(scope.lease_duration, 86400)
        self.assertEqual(str(scope.prefix.prefix), "10.1.0.0/24")
        self.assertEqual(scope.server, self.server)
        self.assertEqual(
            (str(scope.ip_range.start_address), str(scope.ip_range.end_address)), ("10.1.0.10/24", "10.1.0.200/24")
        )
        self.assertEqual(IPRange.objects.get(), scope.ip_range)
        exclusion = ExclusionRange.objects.get()
        self.assertEqual((exclusion.scope, exclusion.start_address), (scope, "10.1.0.10"))

        reservation = NetBoxReservation.objects.get()
        self.assertEqual(reservation.scope, scope)
        self.assertEqual(reservation.name, "printer")
        self.assertEqual(str(reservation.ip_address.address), "10.1.0.50/24")
        self.assertEqual(str(reservation.mac_address.mac_address), "AA:BB:CC:00:11:22")
        self.assertEqual(reservation.client_id, "")
        self.assertEqual(reservation.reservation_type, "both")

        custom = OptionDefinition.objects.get(code=224, server=self.server)
        self.assertEqual((custom.name, custom.windows_type, custom.vendor_class), ("Custom", "String", None))
        vendor_definition = OptionDefinition.objects.get(code=2, server=self.server)
        self.assertEqual((vendor_definition.vendor_class.name, vendor_definition.windows_type), ("MSFT", "DWord"))
        self.assertEqual(OptionDefinition.objects.count(), 2)  # standard definitions are not copied
        self.assertEqual(WindowsClass.objects.count(), 2)

        server_dns = OptionValue.objects.get(code=6, scope=None, user_class=None)
        self.assertEqual(server_dns.value, ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(server_dns.level, "server")
        qualified = OptionValue.objects.get(user_class__name="Labs")
        self.assertEqual((qualified.scope, qualified.value), (scope, ["10.9.9.9"]))
        vendor_value = OptionValue.objects.get(vendor_class__name="MSFT")
        self.assertEqual((vendor_value.code, vendor_value.level, vendor_value.value), (2, "server", ["1"]))
        on_reservation = OptionValue.objects.get(reservation=reservation)
        self.assertEqual((on_reservation.scope, on_reservation.code, on_reservation.value), (scope, 3, ["10.1.0.254"]))
        self.assertEqual(result_status(self.server), ProposalStatusChoices.APPLIED)

    def test_round_trip_leaves_nothing_to_import(self):
        self.apply_all()
        self.assertEqual(self.changes(), [])

    def test_option_values_keep_commas_and_spaces(self):
        """Values are lists, as on Windows: a comma or a space inside one value is not a separator."""
        self.client.set_option_value(OptionKey(option_id=224, scope_id="10.1.0.0"), [" a, b ", "c,d"])
        self.apply_all()
        self.assertEqual(OptionValue.objects.get(code=224).value, [" a, b ", "c,d"])
        self.assertEqual(self.changes(), [])

    def test_existing_core_objects_are_reused(self):
        prefix = Prefix.objects.create(prefix="10.1.0.0/24")
        ip = IPAddress.objects.create(address="10.1.0.50/24")
        mac = MACAddress.objects.create(mac_address="AA:BB:CC:00:11:22")
        self.apply_all()
        self.assertEqual(NetBoxScope.objects.get().prefix, prefix)
        self.assertEqual(NetBoxReservation.objects.get().ip_address, ip)
        self.assertEqual(NetBoxReservation.objects.get().mac_address, mac)
        self.assertEqual(Prefix.objects.count(), 1)

    def test_update_and_delete_after_windows_changes(self):
        self.apply_all()
        self.client.set_scope(Scope(scope_id="10.1.0.0", name="LAN 2", start_range="10.1.0.10", end_range="10.1.0.100"))
        self.client.remove_reservation("10.1.0.50")
        changes = {(c.action, c.kind, c.key) for c in self.changes()}
        self.assertIn(("update", "scope", "10.1.0.0"), changes)
        self.assertIn(("delete", "reservation", "10.1.0.0/10.1.0.50"), changes)
        self.apply_all()
        self.assertEqual(NetBoxScope.objects.get().name, "LAN 2")
        self.assertEqual(str(IPRange.objects.get().end_address), "10.1.0.100/24")
        self.assertFalse(NetBoxReservation.objects.exists())
        self.assertEqual(self.changes(), [])

    def test_objects_missing_on_windows_are_proposed_for_deletion(self):
        self.apply_all()
        self.client.remove_scope("10.1.0.0", force=True)
        result = self.propose()
        deletes = set(result.proposal.items.filter(action="delete").values_list("kind", flat=True))
        self.assertEqual(deletes, {"scope", "exclusion", "reservation", "option"})
        importer.apply_items(result.proposal.items.all())
        self.assertFalse(NetBoxScope.objects.exists())
        self.assertFalse(OptionValue.objects.filter(scope__isnull=False).exists())

    def test_duplicate_options_are_removed(self):
        from netbox_windows_dhcp_plugin.models import WindowsPolicy

        self.apply_all()
        # Policy names are not case sensitive on Windows but are in NetBox: these two values land on one Windows key.
        criteria = {"mac_address": ["EQ", "00-15-5d*"]}
        night, other = (
            WindowsPolicy.objects.create(server=self.server, name=name, criteria=criteria)
            for name in ("Night", "night")
        )
        OptionValue.objects.create(server=self.server, code=6, policy=night, value=["10.0.0.1"])
        duplicate = OptionValue.objects.create(server=self.server, code=6, policy=other, value=["10.0.0.1"])
        changes = {(c.action, c.kind, c.key) for c in self.changes()}
        self.assertIn(("delete", "option", f"server/6;policy=night#{duplicate.pk}"), changes)
        self.assertIn(("delete", "option", "server/6;policy=night"), changes)
        self.apply_all()
        self.assertFalse(OptionValue.objects.filter(policy__isnull=False).exists())
        self.assertEqual(self.changes(), [])

    def test_class_names_cannot_point_at_other_options(self):
        # A "#<pk>" key suffix marks a duplicate option. A Windows class name ending in "#<pk>" must not make the
        # import touch that option, here one belonging to another server.
        other = windows_server("dhcp2")
        victim = OptionValue.objects.create(server=other, code=6, value=["192.0.2.1"])
        self.client.add_class(DhcpClass(name=f"evil#{victim.pk}", type="User", data="evil"))
        self.client.set_option_value(OptionKey(option_id=6, user_class=f"evil#{victim.pk}"), ["10.6.6.6"])
        self.apply_all()
        victim.refresh_from_db()
        self.assertEqual((victim.server, victim.value), (other, ["192.0.2.1"]))
        evil = OptionValue.objects.get(user_class__name=f"evil#{victim.pk}")
        self.assertEqual((evil.server, evil.value), (self.server, ["10.6.6.6"]))
        self.assertEqual(self.changes(), [])

    def test_dismissals_survive_the_proposal_closing(self):
        self.apply_all()
        self.client.remove_reservation("10.1.0.50")
        proposal = self.propose().proposal
        importer.dismiss_items(list(proposal.items.all()))
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatusChoices.DISMISSED)
        # The next run finds the same difference: nothing new to review, so no new proposal.
        result = self.propose()
        self.assertIsNone(result.proposal)
        self.assertEqual(ImportProposal.objects.filter(status=ProposalStatusChoices.OPEN).count(), 0)
        # The summary still says there is a difference, and that a dismissal is why nothing waits for review
        # (issue #43: the panel must not read this as "no differences").
        dismissed = proposal.items.count()
        self.assertEqual((result.review, result.dismissed), (0, dismissed))
        summary = result.summary()
        self.assertEqual((summary["changes"], summary["review"], summary["dismissed"]), (dismissed, 0, dismissed))

    def test_summary_counts_new_and_dismissed_differences(self):
        self.apply_all()
        self.client.remove_reservation("10.1.0.50")
        proposal = self.propose().proposal
        dismissed = proposal.items.count()
        importer.dismiss_items(list(proposal.items.all()))
        scope = self.client.get_scope("10.1.0.0")
        self.client.set_scope(dataclasses.replace(scope, description="moved upstairs"))
        result = self.propose()
        self.assertEqual(list(result.proposal.items.values_list("kind", flat=True)), ["scope"])
        summary = result.summary()
        self.assertEqual((summary["changes"], summary["review"], summary["dismissed"]), (dismissed + 1, 1, dismissed))

    def test_run_that_only_finds_dismissed_differences_closes_the_open_proposal(self):
        self.apply_all()
        scope = self.client.get_scope("10.1.0.0")
        self.client.remove_reservation("10.1.0.50")
        self.client.set_scope(dataclasses.replace(scope, description="moved upstairs"))
        proposal = self.propose().proposal
        dismissed = proposal.items.exclude(kind="scope").count()
        importer.dismiss_items(list(proposal.items.exclude(kind="scope")))
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatusChoices.OPEN)
        # Windows goes back to what NetBox holds for the scope: only the dismissed differences remain.
        self.client.set_scope(scope)
        result = self.propose()
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatusChoices.DISMISSED)
        self.assertEqual((result.review, result.dismissed), (0, dismissed))

    def test_netbox_duplicates_do_not_break_the_import(self):
        self.apply_all()
        from ipam.models import VRF

        # NetBox can hold what Windows cannot: the same network or address twice, in two VRFs.
        vrf = VRF.objects.create(name="elsewhere")
        scope = NetBoxScope.objects.get()
        extra = NetBoxScope.objects.create(
            server=self.server, name="dup", prefix=Prefix.objects.create(prefix="10.1.0.0/24", vrf=vrf)
        )
        twin = NetBoxReservation.objects.create(
            scope=scope, ip_address=IPAddress.objects.create(address="10.1.0.50/24", vrf=vrf), client_id="aa"
        )
        changes = {(c.action, c.key) for c in self.changes()}
        self.assertEqual(changes, {("delete", f"10.1.0.0#{extra.pk}"), ("delete", f"10.1.0.0/10.1.0.50#{twin.pk}")})
        self.apply_all()
        self.assertFalse(NetBoxScope.objects.filter(pk=extra.pk).exists())
        self.assertFalse(NetBoxReservation.objects.filter(pk=twin.pk).exists())
        self.assertTrue(NetBoxScope.objects.filter(pk=scope.pk).exists())
        self.assertEqual(NetBoxReservation.objects.count(), 1)
        self.assertEqual(self.changes(), [])

    def test_failover_partners_share_and_move_one_ip_range(self):
        self.apply_all()
        partner = windows_server("dhcp2")
        state = windows_state.read(DhcpClient(self.fake))
        result = importer.import_to_proposal(partner, state, None)
        importer.apply_items(result.proposal.items.all())
        self.assertEqual(IPRange.objects.count(), 1)
        self.client.set_scope(Scope(scope_id="10.1.0.0", name="LAN", start_range="10.1.0.10", end_range="10.1.0.99"))
        self.apply_all()
        self.assertEqual(IPRange.objects.count(), 1)
        self.assertEqual(str(IPRange.objects.get().end_address), "10.1.0.99/24")

    def test_rerun_replaces_pending_items_and_keeps_one_proposal(self):
        first = self.propose().proposal
        self.client.remove_reservation("10.1.0.50")
        second = self.propose().proposal
        self.assertEqual(first.pk, second.pk)
        self.assertFalse(second.items.filter(kind="reservation").exists())
        self.assertEqual(ImportProposal.objects.count(), 1)

    def test_dismissed_items_stay_dismissed_while_unchanged(self):
        proposal = self.propose().proposal
        item = proposal.items.get(kind="reservation")
        importer.dismiss_items([item])
        self.propose()
        self.assertEqual(proposal.items.filter(kind="reservation").count(), 1)
        self.assertEqual(proposal.items.get(kind="reservation").status, ProposalItemStatusChoices.DISMISSED)
        # A different proposal for the same object comes back for review.
        self.client.set_reservation(
            Reservation(scope_id="10.1.0.0", ip_address="10.1.0.50", client_id="AA-BB-CC-00-11-22", name="new")
        )
        self.propose()
        self.assertEqual(proposal.items.filter(kind="reservation", status=ProposalItemStatusChoices.PENDING).count(), 1)

    def test_apply_refuses_an_item_changed_in_netbox_since(self):
        self.apply_all()
        self.client.set_scope(
            Scope(scope_id="10.1.0.0", name="from windows", start_range="10.1.0.10", end_range="10.1.0.200")
        )
        proposal = self.propose().proposal
        scope = NetBoxScope.objects.get()
        scope.name = "edited in netbox"
        scope.save()
        importer.apply_items(proposal.items.all())
        item = proposal.items.get(kind="scope")
        self.assertEqual(item.status, ProposalItemStatusChoices.FAILED)
        self.assertIn("changed in NetBox", item.error)
        self.assertEqual(NetBoxScope.objects.get().name, "edited in netbox")

    def test_child_without_parent_fails_with_a_clear_message(self):
        proposal = self.propose().proposal
        importer.apply_items(proposal.items.filter(kind="reservation"))
        item = proposal.items.get(kind="reservation")
        self.assertEqual(item.status, ProposalItemStatusChoices.FAILED)
        self.assertIn("scope 10.1.0.0 is not in NetBox yet", item.error)
        self.assertFalse(NetBoxReservation.objects.exists())

    def test_no_differences_open_no_proposal(self):
        self.apply_all()
        ImportProposal.objects.all().delete()
        self.assertIsNone(self.propose().proposal)
        self.assertFalse(ImportProposal.objects.exists())

    def test_vrf_is_used_for_core_objects(self):
        from ipam.models import VRF

        vrf = VRF.objects.create(name="dhcp")
        self.server.import_vrf = vrf
        self.server.save()
        self.apply_all()
        self.assertEqual(Prefix.objects.get().vrf, vrf)
        self.assertEqual(IPRange.objects.get().vrf, vrf)
        self.assertEqual(IPAddress.objects.get().vrf, vrf)


def result_status(server):
    return ImportProposal.objects.get(windows_server=server).status


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class ImportJobTest(ImportTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        ImportJob.runner_factory = staticmethod(lambda server: self.fake)
        self.addCleanup(setattr, ImportJob, "runner_factory", None)

    def _run(self):
        job = Job.objects.create(
            object=self.server, name=ImportJob.name, status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4()
        )
        with mock.patch("netbox_windows_dhcp_plugin.integrations.branching.branching_installed", return_value=False):
            ImportJob(job).run()
        return self.server.sync_runs.get(job=job)

    def test_import_writes_a_proposal_and_a_sync_run(self):
        run = self._run()
        self.assertEqual(run.kind, SyncKindChoices.IMPORT)
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED)
        self.assertEqual(run.summary["create"], 13)
        self.assertNotIn("notes", run.summary)  # nothing skipped
        self.assertEqual(ImportProposal.objects.get().sync_run, run)
        self.assertEqual((run.summary["review"], run.summary["dismissed"]), (13, 0))

    def test_rerun_after_dismissing_everything_says_why_nothing_waits(self):
        # Issue #43: the repeat run still finds the differences, it only proposes none of them again.
        importer.dismiss_items(list(importer.import_to_proposal(self.server, self.state(), None).proposal.items.all()))
        with self.assertLogs("netbox.jobs.ImportJob", level="INFO") as logs:
            run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED)
        self.assertEqual((run.summary["changes"], run.summary["review"], run.summary["dismissed"]), (13, 0, 13))
        self.assertTrue(any("13 already dismissed" in line for line in logs.output), logs.output)

    def test_one_import_per_server_at_a_time(self):
        from django.db import connection

        from netbox_windows_dhcp_plugin.jobs import _LOCK_CLASS

        # Another worker. Advisory locks belong to a database session (one session may take its own lock twice),
        # so hold it from a second connection.
        other = connection.copy()
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s, %s)", [_LOCK_CLASS, self.server.pk])
            with self.assertRaises(JobFailed):
                self._run()
        finally:
            other.close()
        self.assertIn("another import", SyncRun.objects.get().error)
        self.assertEqual(self._run().status, SyncStatusChoices.COMPLETED)

    def test_duplicate_imports_are_not_queued(self):
        from netbox_windows_dhcp_plugin.jobs import enqueue_import

        self.assertIsNotNone(enqueue_import(self.server, None))
        self.assertIsNone(enqueue_import(self.server, None))

    def test_windows_errors_fail_the_run(self):
        from netbox_windows_dhcp_plugin.windows.psscript import PSError

        self.fake.fail = lambda op, params: PSError(message="Access is denied", category="PermissionDenied")
        with self.assertRaises(JobFailed):
            self._run()
        self.assertEqual(SyncRun.objects.get().status, SyncStatusChoices.FAILED)
        self.assertFalse(ImportProposal.objects.exists())


class ScheduleTest(TestCase):
    def setUp(self):
        self.server = windows_server(import_interval=60)

    def test_server_without_import_is_due(self):
        self.assertEqual(list(due_servers()), [self.server])

    def test_recent_import_is_not_due(self):
        SyncRun.objects.create(windows_server=self.server, kind=SyncKindChoices.IMPORT)
        self.assertEqual(list(due_servers()), [])
        self.assertEqual(list(due_servers(now=timezone.now() + timedelta(minutes=61))), [self.server])

    def test_queued_import_is_not_due(self):
        Job.objects.create(
            object=self.server, name=ImportJob.name, status=JobStatusChoices.STATUS_PENDING, job_id=uuid.uuid4()
        )
        self.assertEqual(list(due_servers()), [])

    def test_abandoned_jobs_do_not_block_the_schedule(self):
        job = Job.objects.create(
            object=self.server, name=ImportJob.name, status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4()
        )
        Job.objects.filter(pk=job.pk).update(created=timezone.now() - timedelta(hours=3))
        self.assertEqual(list(due_servers()), [self.server])

    def test_servers_without_interval_are_ignored(self):
        self.server.import_interval = None
        self.server.save()
        self.assertEqual(list(due_servers()), [])


@skipUnless(apps.is_installed("netbox_branching"), "netbox-branching is not installed")
class BranchImportTest(ImportTestMixin, TransactionTestCase):
    # Provisioning a branch copies the schema and commits, which needs real transactions. Restore what the data
    # migrations seed (object types, for one) after each test's flush, for the test cases that run next.
    serialized_rollback = True

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username=f"importer-{uuid.uuid4().hex[:6]}")
        self.addCleanup(self._drop_branches)

    def _drop_branches(self):
        from django.db import connections
        from netbox_branching.models import Branch

        for branch in Branch.objects.all():
            # Reading a branch (activate_branch) opens a connection to it; an open one stops the test database
            # from being dropped at the end of the run.
            connections[branch.connection_name].close()
            branch.delete()

    def test_import_creates_a_branch_with_the_changes(self):
        from netbox_branching.utilities import activate_branch

        result = importer.import_to_branch(self.server, self.state(), self.user)
        self.assertEqual(result.failed, [])
        self.assertTrue(result.branch.name.startswith("dhcp-import/dhcp1/"))
        self.assertFalse(NetBoxScope.objects.exists())  # main is untouched
        with activate_branch(result.branch):
            self.assertEqual(NetBoxScope.objects.get().name, "LAN")
            self.assertEqual(NetBoxReservation.objects.count(), 1)
        self.assertGreater(result.branch.get_unmerged_changes().count(), 0)

    def test_repeated_import_reuses_the_branch(self):
        first = importer.import_to_branch(self.server, self.state(), self.user)
        self.client.remove_reservation("10.1.0.50")
        second = importer.import_to_branch(self.server, self.state(), self.user)
        self.assertEqual(first.branch.pk, second.branch.pk)
        self.assertEqual(
            [(c.action, c.kind) for c in second.changes if c.kind == "reservation"], [("delete", "reservation")]
        )
        third = importer.import_to_branch(self.server, self.state(), self.user)
        self.assertEqual(third.changes, [])

    def test_import_panel_names_the_real_branch(self):
        from netbox_windows_dhcp_plugin import import_status
        from netbox_windows_dhcp_plugin.conf import get_setting
        from netbox_windows_dhcp_plugin.integrations import branching

        result = importer.import_to_branch(self.server, self.state(), self.user)
        SyncRun.objects.create(
            windows_server=self.server,
            kind=SyncKindChoices.IMPORT,
            status=SyncStatusChoices.COMPLETED,
            branch_name=result.branch.name,
            summary=result.summary(),
        )
        self.user.is_superuser = True
        self.user.save()
        self.assertTrue(result.summary()["branch_created"])
        status = import_status.for_server(self.server, self.user)
        self.assertEqual(status.branch.branch.pk, result.branch.pk)
        self.assertTrue(status.branch.main_unchanged)
        if branching.change_control_installed() and get_setting("import_change_request"):
            self.assertEqual(status.branch.change_request.branch_id, result.branch.pk)
        # Someone who may view the server but not branches sees the name, not a link, and no "no longer exists".
        nobody = get_user_model().objects.create_user(username=f"viewer-{uuid.uuid4().hex[:6]}")
        hidden = import_status.for_server(self.server, nobody)
        self.assertIsNone(hidden)  # nor the run itself
        self.assertEqual(branching.import_branch(result.branch.name, nobody), (None, None, True))
        again = importer.import_to_branch(self.server, self.state(), self.user)
        self.assertFalse(again.summary().get("branch_created"))  # nothing new: the open branch is only read

    def test_branch_import_needs_a_user(self):
        from netbox_windows_dhcp_plugin.integrations.branching import BranchError

        with self.assertRaises(BranchError):
            importer.import_to_branch(self.server, self.state(), None)

    def test_branches_of_other_servers_are_not_reused(self):
        # "dhcp1" must not pick up the branch of a server whose name starts with "dhcp1/".
        other = windows_server("dhcp1/east", host="east.example.com")
        first = importer.import_to_branch(other, self.state(), self.user)
        mine = importer.import_to_branch(self.server, self.state(), self.user)
        self.assertNotEqual(first.branch.pk, mine.branch.pk)

    def test_no_differences_create_no_branch(self):
        from netbox_branching.models import Branch

        self.fake = FakeDhcpServer()
        result = importer.import_to_branch(self.server, self.state(), self.user)
        self.assertEqual(result.changes, [])
        self.assertIsNone(result.branch)
        self.assertFalse(Branch.objects.exists())
