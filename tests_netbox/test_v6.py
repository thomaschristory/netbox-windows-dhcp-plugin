"""DHCPv6 scopes, reservations, user classes and option values end to end against the fake server."""

from django.test import TestCase, override_settings
from ipam.models import IPAddress, Prefix
from netbox_dhcp.models import HostReservation, Option, Pool, Subnet

from netbox_windows_dhcp_plugin import signals
from netbox_windows_dhcp_plugin.choices import SyncStatusChoices
from netbox_windows_dhcp_plugin.models import SyncState, WindowsClass, WindowsReservation, WindowsScope
from netbox_windows_dhcp_plugin.sync import importer, netbox_state
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import DhcpClass, OptionKeyV6, ReservationV6, ScopeV6
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .test_import import ImportTestMixin
from .test_jobs import CREDENTIALS
from .test_push import PushTestMixin

PREFIX = "2001:db8:1::"
RESERVED = "2001:db8:1::10"


def seed_v6(fake):
    c = DhcpClient(fake)
    c.add_v6_class(DhcpClass(name="Lab", type="User", data="LabComputers", description="lab machines"))
    # Windows ships a DHCPv6 vendor class; the plugin leaves vendor classes out.
    fake.add_v6_class("Microsoft Windows Options", data="MSFT 5.0")
    c.add_v6_scope(ScopeV6(prefix=PREFIX, name="v6 users", description="office", preference=5, t1="1.00:00:00"))
    c.add_v6_reservation(
        ReservationV6(prefix=PREFIX, ip_address=RESERVED, client_duid="00-01-00-01-aa-bb-cc-dd", iaid=7, name="pc")
    )
    c.set_v6_option_value(OptionKeyV6(23), ["2001:db8::53"])
    c.set_v6_option_value(OptionKeyV6(24, prefix=PREFIX), ["example.com"])
    c.set_v6_option_value(OptionKeyV6(23, prefix=PREFIX, user_class="Lab"), ["2001:db8::54"])
    c.set_v6_option_value(OptionKeyV6(23, prefix=PREFIX, reserved_ip=RESERVED), ["2001:db8::56"])
    return c


class V6Mixin:
    def apply_all(self):
        if not getattr(self, "_v6_seeded", False):
            self.v6 = seed_v6(self.fake)
            self._v6_seeded = True
        return super().apply_all()


class ImportV6Test(V6Mixin, ImportTestMixin, TestCase):
    def test_import_writes_every_v6_kind(self):
        result = self.apply_all()
        self.assertIn("skipped 1 DHCPv6 vendor class(es): the plugin syncs DHCPv6 user classes only", result.notes)

        subnet = Subnet.objects.get(prefix__prefix__family=6)
        self.assertEqual(subnet.name, "dhcp1/2001:db8:1::")
        self.assertEqual(str(subnet.prefix.prefix), "2001:db8:1::/64")
        self.assertEqual((subnet.description, subnet.comments), ("v6 users", "office"))
        self.assertEqual((subnet.preferred_lifetime, subnet.valid_lifetime), (8 * 86400, 12 * 86400))
        # T2 is left to the fake, which uses the Windows default of 6.4 days (6 days 9:36), see #16.
        self.assertEqual((subnet.renew_timer, subnet.rebind_timer), (86400, 552960))
        self.assertEqual(subnet.dhcp_server, self.dhcp)
        self.assertFalse(Pool.objects.filter(subnet=subnet).exists())
        self.assertEqual(WindowsScope.objects.get(subnet=subnet).preference, 5)

        reservation = HostReservation.objects.get(subnet=subnet)
        self.assertEqual(reservation.name, "dhcp1/2001:db8:1::/2001:db8:1::10")
        self.assertEqual((reservation.duid, reservation.description), ("00-01-00-01-aa-bb-cc-dd", "pc"))
        self.assertEqual([str(a.address) for a in reservation.ipv6_addresses.all()], ["2001:db8:1::10/64"])
        self.assertIsNone(reservation.ipv4_address)
        self.assertEqual(WindowsReservation.objects.get(host_reservation=reservation).iaid, 7)

        lab = WindowsClass.objects.get(family=6)
        self.assertEqual((lab.name, lab.class_type, lab.data), ("Lab", "user", "LabComputers"))
        values = {
            (o.assigned_object_type.model, o.definition.code): o.data
            for o in Option.objects.filter(definition__family=6).select_related("definition")
        }
        self.assertEqual(
            values,
            {("dhcpserver", 23): "2001:db8::53", ("subnet", 24): "example.com", ("hostreservation", 23): "2001:db8::56"}
            | {("subnet", 23): "2001:db8::54"},
        )
        qualified = Option.objects.get(definition__code=23, assigned_object_id=subnet.pk)
        self.assertEqual(qualified.netbox_windows_dhcp_plugin_windows_qualifier.user_class, lab)

    def test_round_trip_leaves_nothing_to_import(self):
        self.apply_all()
        self.assertEqual(self.changes(), [])

    def test_v6_changes_on_windows_are_proposed_and_applied(self):
        self.apply_all()
        self.v6.set_v6_scope(ScopeV6(prefix=PREFIX, name="renamed", description="office", preference=5))
        self.v6.set_v6_reservation(ReservationV6(PREFIX, RESERVED, "00-01-02", 9, "pc"))
        self.v6.remove_v6_option_value(OptionKeyV6(24, prefix=PREFIX))
        changes = {(c.action, c.kind) for c in self.changes()}
        self.assertEqual(changes, {("update", "scope6"), ("update", "reservation6"), ("delete", "option6")})
        self.apply_all()
        self.assertEqual(self.changes(), [])
        self.assertEqual(Subnet.objects.get(prefix__prefix__family=6).description, "renamed")
        self.assertEqual(WindowsReservation.objects.get(iaid__isnull=False).iaid, 9)

    def test_scope_removed_on_windows_is_deleted_with_its_children(self):
        self.apply_all()
        self.fake.v6_scopes.pop(PREFIX)
        self.fake.v6_reservations.clear()
        self.fake.v6_options = {k: v for k, v in self.fake.v6_options.items() if not k[1] and not k[2]}
        self.apply_all()
        self.assertFalse(Subnet.objects.filter(prefix__prefix__family=6).exists())
        self.assertFalse(HostReservation.objects.filter(duid__isnull=False).exists())
        self.assertEqual(Option.objects.filter(definition__family=6).count(), 1)

    def test_values_of_codes_without_a_standard_definition_are_skipped(self):
        self.v6 = seed_v6(self.fake)
        self._v6_seeded = True
        self.v6.set_v6_option_value(OptionKeyV6(99, prefix=PREFIX), ["x"])
        _, notes = importer.compute(self.server, self.state())
        self.assertTrue(any("no standard definition" in n for n in notes), notes)


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class PushV6Test(V6Mixin, PushTestMixin, TestCase):
    def test_imported_v6_data_pushes_nothing_and_is_adopted(self):
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(run.summary["changes"], 0, run.summary.get("planned"))
        kinds = set(SyncState.objects.values_list("kind", flat=True))
        self.assertTrue({"class6", "scope6", "reservation6", "option6"} <= kinds, kinds)

    def test_push_to_an_empty_server_recreates_v6_then_changes_nothing(self):
        self.target = FakeDhcpServer()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(self.remaining(), [])
        self.assertEqual(self.target.v6_scopes[PREFIX]["preference"], 5)
        self.assertEqual(self.target.v6_reservations[RESERVED]["iaid"], 7)
        self.assertEqual(self._run().summary["changes"], 0)

    def test_netbox_edits_reach_windows(self):
        self._run()
        subnet = Subnet.objects.get(prefix__prefix__family=6)
        subnet.snapshot()
        subnet.description = "renamed in NetBox"
        subnet.valid_lifetime = 86400 * 2
        subnet.save()
        side = WindowsReservation.objects.get(iaid=7)
        side.snapshot()
        side.iaid = 11
        side.save()
        option = Option.objects.get(definition__code=24)
        option.snapshot()
        option.data = "example.org, example.net"
        option.save()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(self.fake.v6_scopes[PREFIX]["name"], "renamed in NetBox")
        self.assertEqual(self.fake.v6_scopes[PREFIX]["valid_lifetime"], "2.00:00:00")
        self.assertEqual(self.fake.v6_reservations[RESERVED]["iaid"], 11)
        self.assertEqual(self.fake.v6_options[(24, PREFIX, "", "", "")]["value"], ["example.org", "example.net"])
        self.assertEqual(self._run().summary["changes"], 0)

    def test_deleted_in_netbox_is_deleted_on_windows_once_managed(self):
        self._run()
        HostReservation.objects.get(subnet__prefix__prefix__family=6).delete()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertNotIn(RESERVED, self.fake.v6_reservations)
        self.assertFalse(SyncState.objects.filter(kind="reservation6").exists())

    def test_wrong_prefix_length_is_not_pushed_and_keeps_the_scope(self):
        self._run()
        subnet = Subnet.objects.get(prefix__prefix__family=6)
        subnet.snapshot()
        subnet.prefix = Prefix.objects.create(prefix="2001:db8:1::/48")
        subnet.save()
        run = self._run()
        self.assertIn(PREFIX, self.fake.v6_scopes)
        reasons = [s["reason"] for s in run.summary["not_pushed"] if s["kind"] == "scope6"]
        self.assertTrue(reasons and "/64" in reasons[0], run.summary)

    def test_v6_address_changes_queue_a_push(self):
        address = IPAddress.objects.get(address__net_host=RESERVED)
        self.assertEqual(signals.servers_for("ipam.ipaddress", address.pk, {"address": "x"}), {self.dhcp.pk})


class V6ReviewTest(V6Mixin, ImportTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.apply_all()
        self.subnet = Subnet.objects.get(prefix__prefix__family=6)

    def test_values_of_custom_definitions_are_left_alone_on_both_sides(self):
        from netbox_dhcp.models import OptionDefinition

        custom = OptionDefinition.objects.create(
            name="custom-v6", code=100, family=6, space="dhcp6", type="string", dhcp_server=self.dhcp
        )
        Option.objects.create(definition=custom, assigned_object=self.subnet, data="x")
        self.v6.set_v6_option_value(OptionKeyV6(100, prefix=PREFIX), ["x"])
        keys = {r.key for r in netbox_state.read(self.server) if r.kind == "option6"}
        self.assertNotIn("v6/2001:db8:1::/100", keys)
        # Import proposes nothing for it: Windows' value is skipped, and so is NetBox's.
        self.assertEqual(self.changes(), [])

    def test_vendor_classes_in_netbox_are_not_read_as_user_classes(self):
        WindowsClass.objects.create(dhcp_server=self.dhcp, family=6, class_type="vendor", name="Lab", data="x")
        classes = [r for r in netbox_state.read(self.server) if r.kind == "class6"]
        self.assertEqual([(r.key, r.fields["class_type"]) for r in classes], [("v6/user/lab", "user")])

    def test_qualifiers_on_dhcpv6_values_are_validated(self):
        from django.core.exceptions import ValidationError

        from netbox_windows_dhcp_plugin.models import WindowsOptionQualifier, WindowsPolicy

        option = Option.objects.get(definition__code=24)
        policy = WindowsPolicy.objects.create(
            dhcp_server=self.dhcp, name="p", criteria={"mac_address": ["EQ", "00-15-5d*"]}
        )
        with self.assertRaisesMessage(ValidationError, "DHCPv6"):
            WindowsOptionQualifier(
                option=Option.objects.get(definition__code=23, assigned_object_id=self.dhcp.pk), policy=policy
            ).full_clean()
        v4_class = WindowsClass.objects.get(family=4, name="Labs")
        with self.assertRaisesMessage(ValidationError, "use an IPv6 user class"):
            WindowsOptionQualifier(option=option, user_class=v4_class).full_clean()
