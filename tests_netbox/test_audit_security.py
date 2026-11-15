"""Security regression tests: failover partner and allowed_hosts, and the event queue after an unexpected error."""

from unittest import mock

from core.exceptions import JobFailed
from django.test import TestCase, override_settings
from ipam.models import Prefix
from utilities.testing import TestCase as NetBoxTestCase

from netbox_windows_dhcp_plugin.choices import SyncStatusChoices
from netbox_windows_dhcp_plugin.connection import failover_partner_check
from netbox_windows_dhcp_plugin.models import FailoverRelationship, SyncRun, WindowsServer
from netbox_windows_dhcp_plugin.sync import importer
from netbox_windows_dhcp_plugin.sync.mapping import Writer
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .test_jobs import CREDENTIALS
from .test_remaining import RemainingObjectsMixin
from .test_review import ITEM, SCOPE_PERMISSIONS, created_prefixes, make_proposal


def config(**lab):
    profiles = {"lab": {"username": "lab\\admin", "password": "x", **lab}}
    return {**CREDENTIALS, "netbox_windows_dhcp_plugin": {"credentials": profiles}}


@override_settings(PLUGINS_CONFIG=config())
class FailoverPartnerAllowedHostsTest(RemainingObjectsMixin, TestCase):
    """
    Creating a failover relationship makes the primary talk to the partner with the account of its credential
    profile, so the partner must be inside that profile's allowed_hosts.
    """

    def test_create_is_refused_when_the_profile_does_not_cover_the_partner(self):
        self.target = FakeDhcpServer()
        primary_only = config(allowed_hosts=["dhcp1.example.com"])
        with override_settings(PLUGINS_CONFIG=primary_only), self.assertRaises(JobFailed):
            self._run()
        run = SyncRun.objects.latest("pk")
        self.assertEqual(run.status, SyncStatusChoices.FAILED)
        self.assertIn("may not be used with the partner host 'dhcp2.example.com'", str(run.summary) + run.error)
        self.assertEqual(self.target.failovers, {})
        self.assertNotIn("failover.add", self.target.ops())
        # Everything else was still pushed.
        self.assertIn("10.2.0.0", self.target.scopes)

    def test_create_goes_through_when_the_profile_covers_the_partner(self):
        self.target = FakeDhcpServer()
        with override_settings(PLUGINS_CONFIG=config(allowed_hosts=["dhcp?.example.com"])):
            run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertIn("ha", self.target.failovers)

    def test_profile_without_allowed_hosts_is_unaffected(self):
        self.target = FakeDhcpServer()
        with override_settings(PLUGINS_CONFIG=config()):
            run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertIn("ha", self.target.failovers)

    def test_existing_relationship_can_still_be_changed_and_deleted(self):
        # Only creation names a partner chosen in NetBox; a tightened profile must not block cleanup.
        with override_settings(PLUGINS_CONFIG=config()):
            self._run()
        with override_settings(PLUGINS_CONFIG=config(allowed_hosts=["dhcp1.example.com"])):
            relationship = FailoverRelationship.objects.get()
            relationship.snapshot()
            relationship.load_balance_percent = 70
            relationship.save()
            run = self._run()
            self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
            self.assertEqual(self.fake.failovers["ha"]["load_balance_percent"], 70)
            relationship.delete()
            run = self._run()
            self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
            self.assertNotIn("ha", self.fake.failovers)

    def test_the_dhcp_server_behind_a_management_host_is_what_counts(self):
        # The primary is told to contact computer_name; the partner's management host is never contacted by it.
        WindowsServer.objects.filter(pk=self.partner.pk).update(host="jump.other.test", computer_name="dhcp2")
        self.target = FakeDhcpServer()
        with override_settings(PLUGINS_CONFIG=config(allowed_hosts=["dhcp1.example.com", "jump.other.test"])):
            with self.assertRaises(JobFailed):
                self._run()
            run = SyncRun.objects.latest("pk")
            self.assertIn("may not be used with the partner host 'dhcp2'", str(run.summary) + run.error)
            self.assertEqual(self.target.failovers, {})
        with override_settings(PLUGINS_CONFIG=config(allowed_hosts=["dhcp1.example.com", "dhcp2"])):
            run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(self.target.failovers["ha"]["partner_server"], "dhcp2")

    def test_the_partner_about_to_be_sent_is_checked_whatever_the_database_holds(self):
        # The push plans first and writes later. A relationship renamed, repointed or deleted in between must not
        # let the planned partner through unchecked, so the check uses the host it is handed and reads no rows.
        check = failover_partner_check(self.server)
        tight = override_settings(PLUGINS_CONFIG=config(allowed_hosts=["dhcp1.example.com"]))
        refused = "failover HA: credential profile 'lab' may not be used with the partner host"
        with tight, self.assertNumQueries(0):
            check("HA", "dhcp1.example.com")
            with self.assertRaisesMessage(ValueError, refused):
                check("HA", "dhcp2.example.com")
        FailoverRelationship.objects.all().delete()
        with tight, self.assertNumQueries(0), self.assertRaisesMessage(ValueError, refused):
            check("HA", "dhcp2.example.com")

    def test_unknown_profile_or_broken_list(self):
        check = failover_partner_check(self.server)
        with override_settings(PLUGINS_CONFIG={**CREDENTIALS, "netbox_windows_dhcp_plugin": {"credentials": {}}}):
            check("HA", "dhcp2.example.com")
        with (
            override_settings(PLUGINS_CONFIG=config(allowed_hosts="dhcp2.example.com")),
            self.assertRaisesMessage(ValueError, "allowed_hosts must be a list"),
        ):
            check("HA", "dhcp2.example.com")


class EventQueueTest(TestCase):
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


class CrashingApplyViewTest(NetBoxTestCase):
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
