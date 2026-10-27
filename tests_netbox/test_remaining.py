"""
Milestone 5 objects end to end against the fake server: superscopes, policies and their values, vendor class
definitions and values, failover relationships, MAC filters and option values containing a comma.
"""

from core.exceptions import JobFailed
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from netbox_dhcp.models import HostReservation, Option, SharedNetwork, Subnet

from netbox_windows_dhcp_plugin import signals
from netbox_windows_dhcp_plugin.choices import POLICY_CRITERIA_KEYS, SyncStatusChoices
from netbox_windows_dhcp_plugin.jobs import DryRunJob
from netbox_windows_dhcp_plugin.models import (
    FailoverRelationship,
    MacFilter,
    MacFilterLists,
    SyncRun,
    SyncState,
    WindowsClass,
    WindowsPolicy,
    WindowsServer,
    WindowsVendorOption,
    WindowsVendorOptionDefinition,
)
from netbox_windows_dhcp_plugin.sync import importer, netbox_state, pusher, windows_state
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import (
    POLICY_CRITERIA,
    OptionKey,
    Policy,
    PolicyIPRange,
    Scope,
    Superscope,
)
from netbox_windows_dhcp_plugin.windows.client import (
    FailoverRelationship as WinFailover,
)
from netbox_windows_dhcp_plugin.windows.client import (
    MacFilter as WinMacFilter,
)
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .fixtures import dhcp_servers, subnets, windows_server
from .test_jobs import CREDENTIALS
from .test_push import PushTestMixin

SECRET_PROFILES = {
    **CREDENTIALS,
    "netbox_windows_dhcp_plugin": {
        "credentials": {
            "lab": {"username": "lab\\admin", "password": "x"},
            "ha": {"secret": "s3cret", "allowed_hosts": ["*.example.com"]},
            "elsewhere": {"secret": "s3cret", "allowed_hosts": ["*.other.example"]},
        }
    },
}


def seed_more(fake):
    """What the base seed lacks: a second scope, a superscope, policies, a failover relationship, MAC filters."""
    c = DhcpClient(fake)
    c.add_scope(Scope(name="Lab", start_range="10.2.0.10", end_range="10.2.0.200", subnet_mask="255.255.255.0"))
    c.add_superscope(Superscope(name="Campus", scope_ids=["10.1.0.0", "10.2.0.0"]))
    c.add_policy(Policy(name="Windows", criteria={"vendor_class": ["EQ", "MSFT"]}, processing_order=1))
    c.add_policy(
        Policy(
            name="Printers",
            scope_id="10.1.0.0",
            description="print",
            criteria={"mac_address": ["EQ", "00-15-5d*"]},
            lease_duration="0.04:00:00",
            processing_order=1,
        )
    )
    c.add_policy_ip_range(PolicyIPRange("10.1.0.0", "Printers", "10.1.0.100", "10.1.0.120"))
    c.set_option_value(OptionKey(option_id=3, scope_id="10.1.0.0", policy_name="Printers"), ["10.1.0.2"])
    c.set_option_value(OptionKey(option_id=2, vendor_class="MSFT", policy_name="Windows"), ["0"])
    c.set_option_value(OptionKey(option_id=224), ["a,b"])
    c.add_failover(WinFailover(name="HA", partner_server="dhcp2.example.com", scope_ids=["10.2.0.0"]))
    c.add_filter(WinMacFilter(mac_address="00-15-5d-*", list="Deny", description="lab VMs"))
    c.set_filter_lists(allow=False, deny=True)
    return c


class RemainingObjectsMixin(PushTestMixin):
    def setUp(self):
        # The partner must exist before the import reads the relationship.
        self.partner_dhcp = dhcp_servers(1, start=2)[0]
        self.partner = windows_server(self.partner_dhcp)
        self._seed_more = True
        super().setUp()

    def apply_all(self):
        if getattr(self, "_seed_more", False):
            seed_more(self.fake)
            self._seed_more = False
        return super().apply_all()


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class ImportRemainingTest(RemainingObjectsMixin, TestCase):
    def test_import_writes_every_kind(self):
        network = SharedNetwork.objects.get()
        self.assertEqual((network.name, network.description), ("dhcp1/Campus", "Campus"))
        self.assertEqual(str(network.prefix.prefix), "10.0.0.0/14")
        members = Subnet.objects.filter(shared_network=network)
        self.assertEqual(members.count(), 2)
        self.assertFalse(members.filter(dhcp_server__isnull=False).exists())

        printers = WindowsPolicy.objects.get(name="Printers")
        self.assertEqual(printers.subnet.description, "LAN")
        self.assertEqual(printers.lease_duration, 4 * 3600)
        self.assertEqual(printers.ip_ranges, [["10.1.0.100", "10.1.0.120"]])
        # As Windows stores the "00-15-5d*" it was given (#21).
        self.assertEqual(printers.criteria, {"mac_address": ["EQ", "00-15-5d-*"]})
        self.assertEqual(printers.options.get().option.data, "10.1.0.2")
        self.assertIsNone(WindowsPolicy.objects.get(name="Windows").subnet)

        definition = WindowsVendorOptionDefinition.objects.get()
        self.assertEqual((definition.vendor_class.name, definition.code), ("MSFT", 2))
        values = {o.policy.name if o.policy else "": o.value for o in WindowsVendorOption.objects.all()}
        self.assertEqual(values, {"": ["1"], "Windows": ["0"]})

        relationship = FailoverRelationship.objects.get()
        self.assertEqual((relationship.primary_server, relationship.partner_server), (self.dhcp, self.partner_dhcp))
        self.assertEqual([s.description for s in relationship.subnets.all()], ["Lab"])
        self.assertEqual(MacFilter.objects.get().list, "deny")
        lists = MacFilterLists.objects.get()
        self.assertEqual((lists.allow_list_enabled, lists.deny_list_enabled), (False, True))

        # sys4 validates a string option as one value, so the escaped comma passes its checks.
        comma = Option.objects.get(definition__code=224, assigned_object_id=self.dhcp.pk)
        self.assertEqual((comma.data, windows_state.split_values(comma.data)), ("a\\,b", ["a,b"]))

    def test_round_trip_leaves_nothing_to_import(self):
        self.assertEqual(self.changes(), [])

    def test_superscope_removed_on_windows_keeps_its_subnets(self):
        self.client.remove_superscope("Campus")
        self.apply_all()
        self.assertFalse(SharedNetwork.objects.exists())
        self.assertEqual(Subnet.objects.filter(dhcp_server=self.dhcp).count(), 2)
        self.assertEqual(self.changes(), [])

    def test_superscope_membership_change_moves_subnets(self):
        self.client.set_superscope(Superscope(name="Campus", scope_ids=["10.1.0.0"]))
        self.apply_all()
        self.assertEqual(Subnet.objects.get(shared_network__isnull=False).description, "LAN")
        self.assertEqual(Subnet.objects.get(dhcp_server=self.dhcp).description, "Lab")
        self.assertEqual(self.changes(), [])

    def test_partner_import_leaves_the_relationship_to_the_primary(self):
        state = windows_state.read(DhcpClient(self.fake))
        records, notes = windows_state.to_records(state, partner_failovers=netbox_state.partner_failovers(self.partner))
        self.assertNotIn("failover", {r.kind for r in records})
        self.assertTrue(any("failover" in n for n in notes))

    def test_partner_is_matched_on_its_computer_name(self):
        # The partner is reached through a management host; Windows knows it by its own name.
        self.partner.host = "mgmt.example.com"
        self.partner.computer_name = "dhcp2.example.com"
        self.partner.save()
        FailoverRelationship.objects.all().delete()
        self.apply_all()
        self.assertEqual(FailoverRelationship.objects.get().partner_server, self.partner_dhcp)
        self.assertEqual(self.changes(), [])

    def test_too_wide_superscope_is_refused_with_a_clear_message(self):
        DhcpClient(self.fake).add_scope(
            Scope(name="Far", start_range="172.16.0.10", end_range="172.16.0.20", subnet_mask="255.255.255.0")
        )
        DhcpClient(self.fake).add_superscope(Superscope(name="Campus", scope_ids=["172.16.0.0"]))
        result = self.propose()
        importer.apply_items(result.proposal.items.all())
        item = result.proposal.items.get(kind="superscope")
        self.assertIn("too far apart", item.error)

    def test_vendor_items_need_the_vendor_model_permissions(self):
        from users.models import User

        from netbox_windows_dhcp_plugin.models import ImportProposalItem
        from netbox_windows_dhcp_plugin.review import missing_permission

        user = User.objects.create(username="reviewer")
        item = ImportProposalItem(kind="option", action="create", after={"vendor_class": "MSFT"})
        self.assertEqual(missing_permission(user, item), "netbox_windows_dhcp_plugin.add_windowsvendoroption")

    def test_unknown_partner_fails_the_item_clearly(self):
        DhcpClient(self.fake).add_failover(WinFailover(name="Other", partner_server="nowhere", scope_ids=["10.1.0.0"]))
        result = self.propose()
        item = result.proposal.items.get(kind="failover")
        importer.apply_items([item])
        item.refresh_from_db()
        self.assertIn("matches no Windows server in NetBox", item.error)
        # Windows names the partner by its computer name on one side: say how to make it match (lab, 2026-10-26).
        self.assertIn("Test connection", item.error)

    def test_a_partner_named_by_its_reported_computer_name_matches(self):
        # #57 (lab, Windows Server 2022): on the partner's side of a relationship Windows names the other server by
        # its computer name, while NetBox may know that server by IP address.
        WindowsServer.objects.filter(pk=self.partner.pk).update(reported_names=["dhcp-b"])
        DhcpClient(self.fake).add_failover(WinFailover(name="Other", partner_server="DHCP-B", scope_ids=["10.1.0.0"]))
        item = self.propose().proposal.items.get(kind="failover")
        self.assertEqual(item.after["partner_server"], self.partner.host)
        importer.apply_items([item])
        item.refresh_from_db()
        self.assertEqual(item.error, "")
        self.assertEqual(FailoverRelationship.objects.get(name="Other").partner_server, self.partner_dhcp)

    def test_a_computer_name_two_servers_report_matches_neither(self):
        other = windows_server(dhcp_servers(1, start=3)[0])
        WindowsServer.objects.filter(pk__in=[self.partner.pk, other.pk]).update(reported_names=["dhcp-b"])
        DhcpClient(self.fake).add_failover(WinFailover(name="Other", partner_server="dhcp-b", scope_ids=["10.1.0.0"]))
        item = self.propose().proposal.items.get(kind="failover")
        importer.apply_items([item])
        item.refresh_from_db()
        self.assertIn("more than one Windows server", item.error)


@override_settings(PLUGINS_CONFIG=SECRET_PROFILES)
class PushRemainingTest(RemainingObjectsMixin, TestCase):
    def test_imported_data_pushes_nothing_and_is_adopted(self):
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(run.summary["changes"], 0, run.summary.get("planned"))
        kinds = set(SyncState.objects.values_list("kind", flat=True))
        for kind in ("superscope", "policy", "failover", "macfilter", "filterlists"):
            self.assertIn(kind, kinds)
        self.assertTrue(SyncState.objects.filter(kind="optiondef", key="msft/2").exists())

    def test_push_to_an_empty_server_recreates_everything_then_changes_nothing(self):
        self.target = FakeDhcpServer()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(self.remaining(), [])
        self.assertEqual(self.target.superscopes, {"Campus": ["10.1.0.0", "10.2.0.0"]})
        self.assertEqual(self.target.options[(224, "", "", "", "", "")]["value"], ["a,b"])
        second = self._run()
        self.assertEqual(second.summary["changes"], 0, second.summary.get("planned"))

    def test_a_partner_windows_names_by_its_computer_name_is_not_drift(self):
        # #57: without the partner's reported names, its computer name reads as another partner, which Windows
        # cannot change: every push of the relationship would fail.
        self._run()
        self.fake.failovers["ha"]["partner_server"] = "DHCP-B"
        self.assertEqual(self._run(DryRunJob).summary["changes"], 1)
        WindowsServer.objects.filter(pk=self.partner.pk).update(reported_names=["dhcp-b", "dhcp-b.corp.example"])
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(run.summary["changes"], 0, run.summary.get("planned"))

    def test_netbox_edits_of_new_kinds_reach_windows(self):
        self._run()
        policy = WindowsPolicy.objects.get(name="Printers")
        policy.snapshot()
        policy.enabled = False
        policy.ip_ranges = [["10.1.0.150", "10.1.0.160"]]
        policy.save()
        mac = MacFilter.objects.get()
        mac.snapshot()
        mac.list = "allow"
        mac.save()
        relationship = FailoverRelationship.objects.get()
        relationship.snapshot()
        relationship.load_balance_percent = 70
        relationship.save()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertFalse(self.fake.policies[("10.1.0.0", "printers")]["enabled"])
        self.assertEqual(self.fake.policy_ranges[("10.1.0.0", "printers")][0]["start_range"], "10.1.0.150")
        self.assertEqual(self.fake.filters["00-15-5d-*"]["list"], "Allow")
        self.assertEqual(self.fake.failovers["ha"]["load_balance_percent"], 70)
        self.assertEqual(self._run().summary["changes"], 0)

    def test_policy_conditions_in_netbox_spelling_push_once(self):
        """#21: Windows stores a MAC prefix and a host name in its own spelling; the next push changes nothing."""
        self._run()
        policy = WindowsPolicy.objects.get(name="Printers")
        policy.snapshot()
        policy.criteria = {"mac_address": ["EQ", "02-4e-42-ab*"], "fqdn": ["EQ", "*.Example.Test"]}
        policy.save()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(
            self.fake.policies[("10.1.0.0", "printers")]["criteria"],
            {"mac_address": ["EQ", "02-4e-42-ab-*"], "fqdn": ["EQ", "*.example.test"]},
        )
        second = self._run()
        self.assertEqual(second.summary["changes"], 0, second.summary.get("planned"))
        self.assertEqual(self.changes(), [])

    def test_policies_are_created_in_processing_order(self):
        """#24: names sorting opposite to processing orders; Windows refuses order 2 before order 1 exists."""
        printers = WindowsPolicy.objects.get(name="Printers")
        WindowsPolicy.objects.create(
            dhcp_server=self.dhcp,
            subnet=printers.subnet,
            name="Accounting",
            processing_order=2,
            criteria={"mac_address": ["EQ", "02-4e-42-cd*"]},
        )
        self.target = FakeDhcpServer()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(run.summary["failed"], 0, run.summary.get("errors"))
        level = {name: p["processing_order"] for (sid, name), p in self.target.policies.items() if sid == "10.1.0.0"}
        self.assertEqual(level, {"printers": 1, "accounting": 2})
        second = self._run()
        self.assertEqual(second.summary["changes"], 0, second.summary.get("planned"))

    def test_deleting_a_policy_deletes_it_and_its_values(self):
        self._run()
        WindowsPolicy.objects.get(name="Printers").delete()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertNotIn(("10.1.0.0", "printers"), self.fake.policies)
        self.assertFalse(any(k[5] == "Printers" for k in self.fake.options))

    def test_unmanaged_scope_policy_keeps_its_scope(self):
        self._run()
        DhcpClient(self.fake).add_policy(
            Policy(name="phones", scope_id="10.2.0.0", criteria={"vendor_class": ["EQ", "MSFT"]})
        )
        FailoverRelationship.objects.all().delete()
        Subnet.objects.get(description="Lab").delete()
        run = self._run()
        self.assertIn("10.2.0.0", self.fake.scopes)
        reasons = {s["key"]: s["reason"] for s in run.summary["not_pushed"]}
        self.assertIn("policy 10.2.0.0/phones", reasons["10.2.0.0"])

    def test_scope_leaving_a_relationship_is_deleted_in_one_push(self):
        self._run()
        relationship = FailoverRelationship.objects.get()
        subnet = Subnet.objects.get(description="Lab")
        relationship.snapshot()
        relationship.subnets.set([Subnet.objects.get(description="LAN")])
        subnet.delete()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertNotIn("10.2.0.0", self.fake.scopes)
        self.assertEqual(self.fake.failovers["ha"]["scope_ids"], ["10.1.0.0"])

    def test_shared_secret_comes_from_the_profile_and_is_never_recorded(self):
        self._run()
        relationship = FailoverRelationship.objects.get()
        relationship.snapshot()
        relationship.shared_secret_profile = "ha"
        relationship.save()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(self.fake.failover_secrets, {"ha": "s3cret"})
        self.assertNotIn("s3cret", str(run.summary) + str(run.error))
        self.assertFalse(SyncState.objects.filter(fingerprint__contains="s3cret").exists())

    def test_shared_secret_profile_must_allow_both_servers(self):
        self._run()
        relationship = FailoverRelationship.objects.get()
        FailoverRelationship.objects.filter(pk=relationship.pk).update(shared_secret_profile="elsewhere")
        with self.assertRaises(JobFailed):
            self._run()
        run = SyncRun.objects.latest("pk")
        self.assertEqual(run.status, SyncStatusChoices.FAILED)
        self.assertIn("may not be used with host", run.error)
        self.assertEqual(self.fake.failover_secrets, {})

    def test_secret_needs_the_partner_windows_server(self):
        self._run()
        relationship = FailoverRelationship.objects.get()
        FailoverRelationship.objects.filter(pk=relationship.pk).update(shared_secret_profile="ha")
        self.partner.delete()
        run = self._run()
        # Reported, not pushed, and the secret is never read.
        reasons = {s["key"]: s["reason"] for s in run.summary["not_pushed"]}
        self.assertIn("no Windows server", reasons["ha"])
        self.assertEqual(self.fake.failover_secrets, {})
        relationship.refresh_from_db()
        with self.assertRaises(ValidationError):
            relationship.full_clean()

    def test_partner_push_leaves_the_relationship_alone(self):
        self._run()
        # The partner holds the relationship too (Windows replicates it); NetBox has it under the primary only.
        outcome = pusher.run(self.partner, DhcpClient(self.fake), dry_run=True)
        self.assertFalse([c for c in outcome.plan.changes if c.kind == "failover"])

    def test_new_objects_map_to_their_server(self):
        objects = [
            *WindowsPolicy.objects.all(),
            *WindowsVendorOptionDefinition.objects.all(),
            *WindowsVendorOption.objects.all(),
            *MacFilter.objects.all(),
            *MacFilterLists.objects.all(),
            *SharedNetwork.objects.all(),
        ]
        for obj in objects:
            with self.subTest(obj=obj):
                self.assertEqual(signals.servers_for(obj._meta.label_lower, obj.pk, signals._data(obj)), {self.dhcp.pk})


class ModelValidationTest(TestCase):
    def test_policy_criteria_keys_match_the_client(self):
        self.assertEqual(set(POLICY_CRITERIA_KEYS), set(POLICY_CRITERIA))

    def test_policy_criteria_are_validated(self):
        (server,) = dhcp_servers(1)
        for criteria in ({"colour": ["EQ", "x"]}, {"fqdn": "x"}, {"fqdn": ["LIKE", "x"]}, ["EQ"]):
            with self.subTest(criteria=criteria), self.assertRaises(ValidationError):
                WindowsPolicy(dhcp_server=server, name="p", criteria=criteria).full_clean()

    def test_vendor_value_needs_its_definition_server(self):
        one, two = dhcp_servers(2)
        vendor = WindowsClass.objects.create(dhcp_server=one, name="MSFT", class_type="vendor", data="MSFT 5.0")
        definition = WindowsVendorOptionDefinition.objects.create(vendor_class=vendor, code=1, name="x")
        with self.assertRaises(ValidationError):
            WindowsVendorOption(definition=definition, assigned_object=two, value=["1"]).full_clean()
        WindowsVendorOption(definition=definition, assigned_object=one, value=["1"]).full_clean()
        other_user = WindowsClass.objects.create(dhcp_server=two, name="Phones", class_type="user", data="p")
        with self.assertRaises(ValidationError):
            WindowsVendorOption(
                definition=definition, assigned_object=one, user_class=other_user, value=["1"]
            ).full_clean()
        (subnet,) = subnets(one, 1)
        scope_policy = WindowsPolicy.objects.create(dhcp_server=one, subnet=subnet, name="p")
        with self.assertRaises(ValidationError):
            WindowsVendorOption(
                definition=definition, assigned_object=one, policy=scope_policy, value=["1"]
            ).full_clean()
        WindowsVendorOption(
            definition=definition, assigned_object=subnet, policy=scope_policy, value=["1"]
        ).full_clean()
        reservation = HostReservation.objects.create(name="r", dhcp_server=two)
        with self.assertRaises(ValidationError):
            WindowsVendorOption(definition=definition, assigned_object=reservation, value=["1"]).full_clean()
