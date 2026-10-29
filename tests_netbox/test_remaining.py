"""
Milestone 5 objects end to end against the fake server: superscopes, policies and their values, vendor class
definitions and values, failover relationships, MAC filters and option values containing a comma.
"""

from core.exceptions import JobFailed
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError, RestrictedError
from django.test import TestCase, override_settings

from netbox_windows_dhcp_plugin import signals
from netbox_windows_dhcp_plugin.choices import POLICY_CRITERIA_KEYS, SyncStatusChoices
from netbox_windows_dhcp_plugin.jobs import DryRunJob
from netbox_windows_dhcp_plugin.models import (
    FailoverRelationship,
    MacFilter,
    MacFilterLists,
    OptionDefinition,
    OptionValue,
    Scope,
    SyncRun,
    SyncState,
    WindowsClass,
    WindowsPolicy,
    WindowsServer,
)
from netbox_windows_dhcp_plugin.models import Superscope as NetBoxSuperscope
from netbox_windows_dhcp_plugin.sync import importer, netbox_state, pusher, windows_state
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import (
    POLICY_CRITERIA,
    OptionKey,
    Policy,
    PolicyIPRange,
    Superscope,
)
from netbox_windows_dhcp_plugin.windows.client import (
    FailoverRelationship as WinFailover,
)
from netbox_windows_dhcp_plugin.windows.client import (
    MacFilter as WinMacFilter,
)
from netbox_windows_dhcp_plugin.windows.client import (
    Scope as WinScope,
)
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .fixtures import reservation, scope, windows_server, windows_servers
from .test_jobs import CREDENTIALS
from .test_push import PushTestMixin

SECRET_PROFILES = {
    **CREDENTIALS,
    "netbox_windows_dhcp_plugin": {
        "credentials": {
            "lab": {"username": "lab\\admin", "password": "x"},
            "ha": {"secret": "s3cret", "allowed_hosts": ["*.example.com"]},
            "elsewhere": {"secret": "s3cret", "allowed_hosts": ["*.other.example"]},
            "primary-only": {"secret": "s3cret", "allowed_hosts": ["dhcp1.example.com"]},
        }
    },
}


def seed_more(fake):
    """What the base seed lacks: a second scope, a superscope, policies, a failover relationship, MAC filters."""
    c = DhcpClient(fake)
    c.add_scope(WinScope(name="Lab", start_range="10.2.0.10", end_range="10.2.0.200", subnet_mask="255.255.255.0"))
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
        self.partner = windows_server("dhcp2")
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
        superscope = NetBoxSuperscope.objects.get()
        self.assertEqual((superscope.server, superscope.name), (self.server, "Campus"))
        self.assertEqual(sorted(s.name for s in superscope.scopes.all()), ["LAN", "Lab"])

        printers = WindowsPolicy.objects.get(name="Printers")
        self.assertEqual(printers.scope.name, "LAN")
        self.assertEqual(printers.lease_duration, 4 * 3600)
        self.assertEqual(printers.ip_ranges, [["10.1.0.100", "10.1.0.120"]])
        # As Windows stores the "00-15-5d*" it was given (#21).
        self.assertEqual(printers.criteria, {"mac_address": ["EQ", "00-15-5d-*"]})
        value = printers.option_values.get()
        self.assertEqual((value.scope, value.code, value.value), (printers.scope, 3, ["10.1.0.2"]))
        self.assertIsNone(WindowsPolicy.objects.get(name="Windows").scope)

        definition = OptionDefinition.objects.get(vendor_class__isnull=False)
        self.assertEqual((definition.vendor_class.name, definition.code), ("MSFT", 2))
        vendor_values = OptionValue.objects.filter(vendor_class__name="MSFT")
        values = {o.policy.name if o.policy else "": o.value for o in vendor_values}
        self.assertEqual(values, {"": ["1"], "Windows": ["0"]})

        relationship = FailoverRelationship.objects.get()
        self.assertEqual((relationship.primary_server, relationship.partner_server), (self.server, self.partner))
        self.assertEqual([s.name for s in relationship.scopes.all()], ["Lab"])
        self.assertEqual(MacFilter.objects.get().list, "deny")
        lists = MacFilterLists.objects.get()
        self.assertEqual((lists.allow_list_enabled, lists.deny_list_enabled), (False, True))

        # Values are lists, as Windows holds them: a comma inside one value stays one value.
        comma = OptionValue.objects.get(server=self.server, code=224, scope__isnull=True)
        self.assertEqual(comma.value, ["a,b"])

    def test_round_trip_leaves_nothing_to_import(self):
        self.assertEqual(self.changes(), [])

    def test_superscope_removed_on_windows_keeps_its_scopes(self):
        self.client.remove_superscope("Campus")
        self.apply_all()
        self.assertFalse(NetBoxSuperscope.objects.exists())
        self.assertEqual(Scope.objects.filter(server=self.server, superscope__isnull=True).count(), 2)
        self.assertEqual(self.changes(), [])

    def test_superscope_membership_change_moves_scopes(self):
        self.client.set_superscope(Superscope(name="Campus", scope_ids=["10.1.0.0"]))
        self.apply_all()
        self.assertEqual(Scope.objects.get(superscope__isnull=False).name, "LAN")
        self.assertEqual(Scope.objects.get(superscope__isnull=True).name, "Lab")
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
        self.assertEqual(FailoverRelationship.objects.get().partner_server, self.partner)
        self.assertEqual(self.changes(), [])

    def test_superscope_of_scopes_far_apart_imports(self):
        # A superscope needs no covering prefix.
        DhcpClient(self.fake).add_scope(
            WinScope(name="Far", start_range="172.16.0.10", end_range="172.16.0.20", subnet_mask="255.255.255.0")
        )
        DhcpClient(self.fake).add_superscope(Superscope(name="Campus", scope_ids=["172.16.0.0"]))
        self.apply_all()
        self.assertEqual(Scope.objects.get(name="Far").superscope.name, "Campus")
        self.assertEqual(self.changes(), [])

    def test_vendor_items_need_the_vendor_model_permissions(self):
        from users.models import User

        from netbox_windows_dhcp_plugin.models import ImportProposalItem
        from netbox_windows_dhcp_plugin.review import missing_permission

        user = User.objects.create(username="reviewer")
        item = ImportProposalItem(kind="option", action="create", after={"vendor_class": "MSFT"})
        self.assertEqual(missing_permission(user, item), "netbox_windows_dhcp_plugin.add_optionvalue")

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
        self.assertEqual(FailoverRelationship.objects.get(name="Other").partner_server, self.partner)

    def test_a_computer_name_two_servers_report_matches_neither(self):
        other = windows_server("dhcp3")
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
            server=self.server,
            scope=printers.scope,
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
        Scope.objects.get(name="Lab").delete()
        run = self._run()
        self.assertIn("10.2.0.0", self.fake.scopes)
        reasons = {s["key"]: s["reason"] for s in run.summary["not_pushed"]}
        self.assertIn("policy 10.2.0.0/phones", reasons["10.2.0.0"])

    def test_scope_leaving_a_relationship_is_deleted_in_one_push(self):
        self._run()
        relationship = FailoverRelationship.objects.get()
        lab = Scope.objects.get(name="Lab")
        relationship.snapshot()
        relationship.scopes.set([Scope.objects.get(name="LAN")])
        lab.delete()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertNotIn("10.2.0.0", self.fake.scopes)
        self.assertEqual(self.fake.failovers["ha"]["scope_ids"], ["10.1.0.0"])

    def test_authentication_without_a_profile_is_left_as_windows_has_it(self):
        # Found while taking the documentation screenshots: Windows has message authentication on, an import cannot
        # read the secret, so NetBox has no profile. Every push then planned "turn it off", which Windows refuses,
        # and failed. Without a profile NetBox does not manage authentication: nothing to import, nothing to push.
        self.fake.failovers["ha"]["enable_auth"] = True
        changes, _ = importer.compute(self.server, windows_state.read(DhcpClient(self.fake)))
        self.assertEqual([c for c in changes if c.kind == "failover"], [])
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(run.summary["changes"], 0, run.summary.get("planned"))
        # Other changes to the relationship still go through, and leave authentication alone.
        relationship = FailoverRelationship.objects.get()
        relationship.snapshot()
        relationship.load_balance_percent = 70
        relationship.save()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(self.fake.failovers["ha"]["load_balance_percent"], 70)
        self.assertTrue(self.fake.failovers["ha"]["enable_auth"])
        self.assertEqual(self.fake.failover_secrets, {})

    def test_a_relationship_pushed_without_a_profile_can_be_deleted_later(self):
        # Review of the change above: the fingerprint recorded after a push must hold what Windows has for
        # authentication, not "not managed", or a later delete is skipped as changed on Windows.
        self.target = FakeDhcpServer()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertIn("ha", self.target.failovers)
        FailoverRelationship.objects.get().delete()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(run.summary.get("not_pushed", []), [])
        self.assertNotIn("ha", self.target.failovers)

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

    def test_secret_is_not_sent_when_the_partner_host_is_outside_the_profile(self):
        # The secret goes to both partners: a profile set behind the form's back that covers the primary only stops
        # the push before the secret is read.
        self._run()
        FailoverRelationship.objects.update(shared_secret_profile="primary-only")
        with self.assertRaises(JobFailed):
            self._run()
        self.assertIn("may not be used with host", SyncRun.objects.latest("pk").error)
        self.assertEqual(self.fake.failover_secrets, {})
        with self.assertRaises(ValidationError):
            FailoverRelationship.objects.get().full_clean()

    def test_deleting_the_partner_server_is_refused(self):
        # Deleting the relationship with it would make the next push remove failover from the primary on Windows.
        self._run()
        with self.assertRaises(ProtectedError):
            self.partner.delete()
        self.assertTrue(FailoverRelationship.objects.exists())

    def test_partner_push_leaves_the_relationship_alone(self):
        self._run()
        # The partner holds the relationship too (Windows replicates it); NetBox has it under the primary only.
        outcome = pusher.run(self.partner, DhcpClient(self.fake), dry_run=True)
        self.assertFalse([c for c in outcome.plan.changes if c.kind == "failover"])

    def test_new_objects_map_to_their_server(self):
        objects = [
            *WindowsPolicy.objects.all(),
            *OptionDefinition.objects.all(),
            *OptionValue.objects.all(),
            *MacFilter.objects.all(),
            *MacFilterLists.objects.all(),
            *NetBoxSuperscope.objects.all(),
            *Scope.objects.all(),
        ]
        for obj in objects:
            with self.subTest(obj=obj):
                self.assertEqual(
                    signals.servers_for(obj._meta.label_lower, obj.pk, signals._data(obj)), {self.server.pk}
                )


class ModelValidationTest(TestCase):
    def test_policy_criteria_keys_match_the_client(self):
        self.assertEqual(set(POLICY_CRITERIA_KEYS), set(POLICY_CRITERIA))

    def test_policy_criteria_are_validated(self):
        server = windows_server()
        for criteria in ({"colour": ["EQ", "x"]}, {"fqdn": "x"}, {"fqdn": ["LIKE", "x"]}, ["EQ"]):
            with self.subTest(criteria=criteria), self.assertRaises(ValidationError):
                WindowsPolicy(server=server, name="p", criteria=criteria).full_clean()

    def test_class_option_values_do_not_block_deleting_the_server(self):
        server = windows_server()
        vendor = WindowsClass.objects.create(server=server, name="MSFT", class_type="vendor", data="MSFT 5.0")
        phones = WindowsClass.objects.create(server=server, name="Phones", class_type="user", data="p")
        OptionDefinition.objects.create(server=server, vendor_class=vendor, code=1, name="x")
        OptionValue.objects.create(server=server, vendor_class=vendor, code=1, value=["1"])
        OptionValue.objects.create(server=server, user_class=phones, code=6, value=["10.0.0.1"])
        # A class in use still cannot be deleted on its own.
        with self.assertRaises(RestrictedError):
            phones.delete()
        server.delete()
        self.assertFalse(WindowsClass.objects.exists())
        self.assertFalse(OptionValue.objects.exists())

    def test_vendor_value_needs_its_server(self):
        one, two = windows_servers(2)
        vendor = WindowsClass.objects.create(server=one, name="MSFT", class_type="vendor", data="MSFT 5.0")
        OptionDefinition.objects.create(server=one, vendor_class=vendor, code=1, name="x")
        with self.assertRaises(ValidationError):
            OptionValue(server=two, vendor_class=vendor, code=1, value=["1"]).full_clean()
        OptionValue(server=one, vendor_class=vendor, code=1, value=["1"]).full_clean()
        other_user = WindowsClass.objects.create(server=two, name="Phones", class_type="user", data="p")
        with self.assertRaises(ValidationError):
            OptionValue(server=one, vendor_class=vendor, user_class=other_user, code=1, value=["1"]).full_clean()
        lan = scope(one, "10.1.0.0/24")
        scope_policy = WindowsPolicy.objects.create(server=one, scope=lan, name="p")
        with self.assertRaises(ValidationError):
            OptionValue(server=one, vendor_class=vendor, policy=scope_policy, code=1, value=["1"]).full_clean()
        OptionValue(server=one, scope=lan, vendor_class=vendor, policy=scope_policy, code=1, value=["1"]).full_clean()
        printer = reservation(scope(two, "10.2.0.0/24"), "10.2.0.5/24", client_id="aa")
        with self.assertRaises(ValidationError):
            OptionValue(server=one, reservation=printer, vendor_class=vendor, code=1, value=["1"]).full_clean()
