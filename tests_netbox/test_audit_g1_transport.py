"""Regression tests for the transport findings of the code quality audit (group g1-transport)."""

from core.exceptions import JobFailed
from django.test import TestCase, override_settings

from netbox_windows_dhcp_plugin.choices import SyncStatusChoices
from netbox_windows_dhcp_plugin.connection import failover_partner_check
from netbox_windows_dhcp_plugin.models import FailoverRelationship, SyncRun, WindowsServer
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .test_jobs import CREDENTIALS
from .test_remaining import RemainingObjectsMixin


def config(**lab):
    profiles = {"lab": {"username": "lab\\admin", "password": "x", **lab}}
    return {**CREDENTIALS, "netbox_windows_dhcp_plugin": {"credentials": profiles}}


@override_settings(PLUGINS_CONFIG=config())
class FailoverPartnerAllowedHostsTest(RemainingObjectsMixin, TestCase):
    """
    Creating a failover relationship makes the primary talk to the partner with the account of its credential
    profile, so the partner must be inside that profile's allowed_hosts (finding F13).
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


@override_settings(PLUGINS_CONFIG=config())
class FailoverSecretInlineNoneTest(RemainingObjectsMixin, TestCase):
    """An inline None stops a job only for the key that job needs (finding R18 on F11)."""

    def test_a_none_secret_is_reported_when_a_relationship_needs_it(self):
        from netbox_windows_dhcp_plugin.connection import failover_secret_lookup

        FailoverRelationship.objects.filter(primary_server=self.server).update(shared_secret_profile="lab")
        lookup = failover_secret_lookup(self.server)
        with (
            override_settings(PLUGINS_CONFIG=config(secret=None)),
            self.assertRaisesMessage(ValueError, "failover HA: credential profile 'lab': secret is set to None"),
        ):
            lookup("HA")
        # A profile read only for its secret is not stopped by a None it does not use.
        with override_settings(PLUGINS_CONFIG=config(secret="s3", private_key=None)):
            self.assertEqual(lookup("HA"), "s3")

    def test_a_none_secret_does_not_stop_a_connection_that_never_uses_it(self):
        from netbox_windows_dhcp_plugin.conf import get_credentials

        with override_settings(PLUGINS_CONFIG=config(secret=None, private_key=None)):
            creds = get_credentials("lab")
        self.assertEqual((creds.password, creds.secret, creds.private_key), ("x", "", ""))
