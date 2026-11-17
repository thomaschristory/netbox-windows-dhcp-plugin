"""Issue #108: rules for servers and failover partners, and a dry run showing the push-side reasons."""

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from ipam.models import IPAddress
from netaddr import IPNetwork

from netbox_windows_dhcp_plugin.choices import SyncStatusChoices, TransportChoices
from netbox_windows_dhcp_plugin.jobs import DryRunJob
from netbox_windows_dhcp_plugin.models import (
    ExclusionRange,
    FailoverRelationship,
    Reservation,
    Scope,
    WindowsPolicy,
    WindowsServer,
)
from netbox_windows_dhcp_plugin.sync import importer, netbox_state
from netbox_windows_dhcp_plugin.sync.windows_state import policy_key

from .fixtures import ip_address, windows_server
from .test_jobs import CREDENTIALS
from .test_remaining import RemainingObjectsMixin


def config(**lab):
    profiles = {"lab": {"username": "lab\\admin", "password": "x", **lab}}
    return {**CREDENTIALS, "netbox_windows_dhcp_plugin": {"credentials": profiles}}


def field_errors(obj):
    try:
        obj.clean()
    except ValidationError as exc:
        return exc.message_dict
    return {}


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class SameServerTwiceTest(TestCase):
    def setUp(self):
        self.first = windows_server("dhcp1")

    def test_same_host_and_port_is_refused_whatever_the_case_or_trailing_dot(self):
        for host in ("dhcp1.example.com", "DHCP1.Example.com", "dhcp1.example.com."):
            with self.subTest(host=host):
                other = WindowsServer(name="copy", host=host, credential_profile="lab")
                self.assertIn("Windows server dhcp1 already connects", field_errors(other)["host"][0])

    def test_explicit_default_port_is_the_same_port(self):
        other = WindowsServer(name="copy", host="dhcp1.example.com", port=5986, credential_profile="lab")
        self.assertIn("host", field_errors(other))

    def test_other_port_transport_or_remote_server_is_another_server(self):
        cases = {
            "port": {"port": 15986},
            "transport": {"transport": TransportChoices.SSH},
            "computer_name": {"computer_name": "dhcp9"},
        }
        for name, extra in cases.items():
            with self.subTest(name):
                other = WindowsServer(name="other", host="dhcp1.example.com", credential_profile="lab", **extra)
                self.assertNotIn("host", field_errors(other))

    def test_remote_servers_compare_without_case(self):
        WindowsServer.objects.filter(pk=self.first.pk).update(computer_name="DHCP9.example.com")
        other = WindowsServer(
            name="other", host="dhcp1.example.com", computer_name="dhcp9.example.com.", credential_profile="lab"
        )
        self.assertIn("host", field_errors(other))

    def test_a_server_does_not_clash_with_itself(self):
        server = WindowsServer.objects.get(pk=self.first.pk)
        server.description = "edited"
        self.assertEqual(field_errors(server), {})


class FailoverPartnerHostTest(TestCase):
    def setUp(self):
        self.primary = windows_server("dhcp1")
        self.partner = windows_server("dhcp2")

    def relationship(self, **kwargs):
        return FailoverRelationship(primary_server=self.primary, partner_server=self.partner, name="HA", **kwargs)

    def test_partner_outside_the_primary_profile_is_refused(self):
        with override_settings(PLUGINS_CONFIG=config(allowed_hosts=["dhcp1.example.com"])):
            errors = field_errors(self.relationship())
        self.assertIn("may not be used with the partner host dhcp2.example.com", errors["partner_server"][0])

    def test_partner_inside_the_profile_or_profile_without_allowed_hosts_is_fine(self):
        for profile in (config(allowed_hosts=["dhcp?.example.com"]), config()):
            with self.subTest(profile=profile), override_settings(PLUGINS_CONFIG=profile):
                self.assertEqual(field_errors(self.relationship()), {})

    def test_unknown_primary_profile_is_left_to_the_job(self):
        WindowsServer.objects.filter(pk=self.primary.pk).update(credential_profile="missing")
        self.primary.refresh_from_db()
        with override_settings(PLUGINS_CONFIG=config(allowed_hosts=["dhcp1.example.com"])):
            self.assertEqual(field_errors(self.relationship()), {})

    def test_the_dhcp_server_behind_a_management_host_is_what_counts(self):
        WindowsServer.objects.filter(pk=self.partner.pk).update(host="jump.other.test", computer_name="dhcp2")
        self.partner.refresh_from_db()
        with override_settings(PLUGINS_CONFIG=config(allowed_hosts=["dhcp1.example.com", "jump.other.test"])):
            self.assertIn("partner host dhcp2", field_errors(self.relationship())["partner_server"][0])

    def test_existing_relationship_keeps_its_partner_but_a_new_partner_is_checked(self):
        with override_settings(PLUGINS_CONFIG=config()):
            self.relationship().save()
        third = windows_server("dhcp3")
        with override_settings(PLUGINS_CONFIG=config(allowed_hosts=["dhcp1.example.com"])):
            stored = FailoverRelationship.objects.get()
            stored.description = "edited"
            # The push never names the partner again for a relationship Windows already has.
            self.assertEqual(field_errors(stored), {})
            stored.partner_server = third
            self.assertIn("partner_server", field_errors(stored))

    def test_import_records_the_partner_windows_already_has(self):
        relationship = self.relationship()
        relationship.check_partner_host = False
        with override_settings(PLUGINS_CONFIG=config(allowed_hosts=["dhcp1.example.com"])):
            self.assertEqual(field_errors(relationship), {})


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class DryRunReasonsTest(RemainingObjectsMixin, TestCase):
    """What Windows would refuse shows up in the dry run, and the push leaves Windows as it was."""

    def setUp(self):
        super().setUp()
        self._run()  # adopts everything

    def test_dry_run_lists_the_reasons_and_the_push_deletes_nothing(self):
        lan = Scope.objects.get(name="LAN")
        printer = Reservation.objects.get(scope=lan)
        # Edits made outside the plugin's forms: an IPAM edit of the address, raw saves of the rest.
        IPAddress.objects.filter(pk=printer.ip_address_id).update(address=IPNetwork("10.1.0.5/24"))
        Reservation.objects.create(scope=lan, ip_address=ip_address("10.1.0.60/24"), client_id="aa-bb-cc-00-11-22")
        ExclusionRange.objects.create(scope=lan, start_address="10.1.0.15", end_address="10.1.0.25")
        WindowsPolicy.objects.filter(name="Printers").update(ip_ranges=[["10.1.0.100"]])

        dry = self._run(DryRunJob)
        self.assertEqual(dry.status, SyncStatusChoices.COMPLETED, dry.error)
        reasons = {(e["kind"], e["key"]): e["reason"] for e in dry.summary["not_pushed"]}
        self.assertIn("outside the scope's IP range", reasons["reservation", "10.1.0.0/10.1.0.5"])
        self.assertIn("is for the same client", reasons["reservation", "10.1.0.0/10.1.0.60"])
        self.assertIn("cannot be pushed", reasons["reservation", "10.1.0.0/10.1.0.50"])
        self.assertIn("shares addresses with exclusion range", reasons["exclusion", "10.1.0.0/10.1.0.15-10.1.0.25"])
        self.assertIn("policy Printers: its stored IP ranges", reasons["policy", policy_key("10.1.0.0", "Printers")])
        self.assertNotIn("delete", [c["action"] for c in dry.summary["planned"]])

        ranges = dict(self.fake.policy_ranges)
        self._run()
        self.assertIn("10.1.0.50", self.fake.reservations)
        self.assertEqual(len(self.fake.exclusions), 1)
        self.assertEqual(self.fake.policy_ranges, ranges)

    def test_import_still_reads_a_server_with_unreadable_policy_ranges(self):
        WindowsPolicy.objects.filter(name="Printers").update(ip_ranges=[["10.1.0.100"]])
        record = next(r for r in netbox_state.read(self.server) if r.kind == "policy" and r.fields["scope_id"])
        self.assertEqual(record.fields["ip_ranges"], [["10.1.0.100"]])
        # An import proposes Windows' ranges, which puts the stored ones right.
        changes = importer.compute(self.server, self.state())[0]
        update = next(c for c in changes if c.kind == "policy" and c.key == record.key)
        self.assertEqual(update.after["ip_ranges"], [["10.1.0.100", "10.1.0.120"]])
