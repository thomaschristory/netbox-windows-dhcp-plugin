"""
Regression tests for the code quality audit, group g6 (models, choices, migrations): findings F29 to F36.

Each class names the finding it covers. F30 only pins today's behaviour (a design decision left to the owner).
"""

from dcim.models import Device, DeviceRole, DeviceType, Interface, MACAddress, Manufacturer, Site
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import TestCase, override_settings
from ipam.models import IPAddress
from netaddr import IPNetwork

from netbox_windows_dhcp_plugin.choices import ClassTypeChoices, SyncStatusChoices
from netbox_windows_dhcp_plugin.models import (
    ImportProposalItem,
    OptionDefinition,
    Reservation,
    Scope,
    SyncRun,
    SyncState,
    WindowsClass,
    WindowsPolicy,
    WindowsServer,
)
from netbox_windows_dhcp_plugin.sync import importer, netbox_state, windows_state
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import DhcpClass
from netbox_windows_dhcp_plugin.windows.client import OptionDefinition as WinOptionDefinition
from netbox_windows_dhcp_plugin.windows.client import Scope as WinScope
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .fixtures import ip_address, prefix, reservation, scope, windows_server
from .test_jobs import CREDENTIALS
from .test_models import errors
from .test_push import PushTestMixin

# -- F29 ---------------------------------------------------------------------------------------------------------


class F29ScopeRulesTest(TestCase):
    """Rules Windows always enforces on a DHCPv6 scope are reported when the scope is saved."""

    @classmethod
    def setUpTestData(cls):
        cls.server = windows_server()

    def _v6(self, **timers):
        return Scope(server=self.server, prefix=prefix("2001:db8:29::/64"), **timers)

    def test_a_v6_scope_is_a_64(self):
        errs = errors(Scope(server=self.server, prefix=prefix("2001:db8::/48")))
        self.assertIn("/48", errs["prefix"][0])
        self._v6().clean()

    def test_any_v4_length_is_fine(self):
        for network in ("10.29.0.0/16", "10.30.0.0/27"):
            Scope(server=self.server, prefix=prefix(network)).clean()

    def test_timers_in_order_are_accepted(self):
        self._v6(renew_timer=100, rebind_timer=200, preferred_lifetime=300, valid_lifetime=300).clean()

    def test_preferred_lifetime_past_the_valid_one(self):
        self.assertIn("preferred_lifetime", errors(self._v6(preferred_lifetime=86400, valid_lifetime=3600)))

    def test_t1_not_below_t2(self):
        self.assertIn("renew_timer", errors(self._v6(renew_timer=200, rebind_timer=200)))
        self.assertIn("renew_timer", errors(self._v6(renew_timer=300, rebind_timer=200)))

    def test_t2_not_below_the_preferred_lifetime(self):
        self.assertIn("rebind_timer", errors(self._v6(rebind_timer=300, preferred_lifetime=300)))

    def test_t1_not_below_the_preferred_lifetime(self):
        self.assertIn("renew_timer", errors(self._v6(renew_timer=400, preferred_lifetime=300)))

    def test_a_value_left_empty_is_not_judged(self):
        # An empty field keeps what Windows has, which NetBox does not know.
        for timers in (
            {"preferred_lifetime": 86400},
            {"valid_lifetime": 60},
            {"renew_timer": 10**6},
            {"rebind_timer": 10**6, "valid_lifetime": 60},
            {"renew_timer": 10**6, "valid_lifetime": 60},
        ):
            self._v6(**timers).clean()

    def test_a_timer_of_zero_is_empty_as_the_sync_reads_it(self):
        # 0.2.0 stored 0 in T1 or T2 and pushed it as "let the server work it out": such a scope stays editable.
        for timers in (
            {"renew_timer": 0, "rebind_timer": 0},
            {"renew_timer": 3600, "rebind_timer": 0},
            {"renew_timer": 0, "rebind_timer": 0, "preferred_lifetime": 300},
            {"renew_timer": 0, "rebind_timer": 3600, "preferred_lifetime": 7200},
        ):
            with self.subTest(timers=timers):
                self._v6(**timers).clean()

    def test_a_lifetime_of_zero_is_still_judged(self):
        # Only the two timers read 0 as empty; a lifetime of 0 is a real value sent to Windows.
        self.assertIn("preferred_lifetime", errors(self._v6(preferred_lifetime=300, valid_lifetime=0)))
        self.assertIn("rebind_timer", errors(self._v6(rebind_timer=300, preferred_lifetime=0)))

    def test_what_windows_reports_still_imports(self):
        # The fake's default DHCPv6 scope (8 and 12 days, T1 4 days, T2 6.4 days) passes the same rules.
        from .test_v6 import seed_v6

        fake = FakeDhcpServer()
        seed_v6(fake)
        result = importer.import_to_proposal(self.server, windows_state.read(DhcpClient(fake)), None)
        self.assertEqual(importer.apply_items(result.proposal.items.all())[1], 0)
        self.assertTrue(Scope.objects.filter(prefix__prefix__family=6).exists())


# -- F30 ---------------------------------------------------------------------------------------------------------


class F30ProtectedAddressTest(TestCase):
    """Pins today's behaviour: a device whose interface holds a reserved address or MAC cannot be deleted."""

    @classmethod
    def setUpTestData(cls):
        site = Site.objects.create(name="f30", slug="f30")
        manufacturer = Manufacturer.objects.create(name="f30", slug="f30")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="f30", slug="f30")
        role = DeviceRole.objects.create(name="f30", slug="f30")
        cls.device = Device.objects.create(name="printer", site=site, device_type=device_type, role=role)
        cls.interface = Interface.objects.create(device=cls.device, name="eth0", type="1000base-t")
        cls.scope = scope(windows_server(), "10.30.0.0/24")

    def test_a_reserved_ip_address_blocks_the_device_delete(self):
        address = IPAddress.objects.create(address=IPNetwork("10.30.0.50/24"), assigned_object=self.interface)
        held = Reservation.objects.create(scope=self.scope, ip_address=address, client_id="aa-bb-cc-00-00-30")
        with self.assertRaises(ProtectedError):
            self.device.delete()
        held.delete()
        self.device.delete()

    def test_a_reserved_mac_address_blocks_the_device_delete(self):
        mac = MACAddress.objects.create(mac_address="aa:bb:cc:00:00:31", assigned_object=self.interface)
        held = Reservation.objects.create(scope=self.scope, ip_address=ip_address("10.30.0.51/24"), mac_address=mac)
        with self.assertRaises(ProtectedError):
            self.device.delete()
        held.delete()
        self.device.delete()


# -- F31 ---------------------------------------------------------------------------------------------------------


class F31ClientIdentityTest(TestCase):
    """A client ID or DUID is a string of bytes, stored in the one spelling Windows reports."""

    @classmethod
    def setUpTestData(cls):
        cls.server = windows_server()
        cls.scope = scope(cls.server, "10.31.0.0/24")
        cls.scope6 = scope(cls.server, "2001:db8:31::/64")

    def _v4(self, client_id, last=50):
        return Reservation(scope=self.scope, ip_address=ip_address(f"10.31.0.{last}/24"), client_id=client_id)

    def _v6(self, duid):
        return Reservation(scope=self.scope6, ip_address=ip_address("2001:db8:31::10/64"), duid=duid, iaid=1)

    def test_a_client_id_that_is_not_hex_is_rejected(self):
        for value in ("printer-01", "x", "abc", "aa-bbb", "zz"):
            with self.subTest(value=value):
                self.assertIn("client_id", errors(self._v4(value)))

    def test_a_duid_that_is_not_hex_is_rejected(self):
        for value in ("my-laptop", "0001000", "00-01-0g"):
            with self.subTest(value=value):
                self.assertIn("duid", errors(self._v6(value)))

    def test_every_spelling_is_stored_the_way_windows_reports_it(self):
        for typed in (
            "AA:BB:CC:DD:EE:FF",
            "aa-bb-cc-dd-ee-ff",
            "aabb.ccdd.eeff",
            "AABBCCDDEEFF",
            " aa bb cc dd ee ff ",
        ):
            with self.subTest(typed=typed):
                obj = self._v4(typed)
                obj.clean()
                self.assertEqual(obj.client_id, "aa-bb-cc-dd-ee-ff")

    def test_single_digits_are_read_as_bytes_like_the_push_reads_them(self):
        obj = self._v4("0:1b:21:a:b:c")
        obj.clean()
        self.assertEqual(obj.client_id, "00-1b-21-0a-0b-0c")

    def test_a_duid_is_normalised_too(self):
        obj = self._v6("00:01:00:01:AA:BB:CC:DD")
        obj.clean()
        self.assertEqual(obj.duid, "00-01-00-01-aa-bb-cc-dd")

    def test_save_normalises_without_clean(self):
        # The REST API validates a copy and saves another instance, so clean() alone would not store the spelling.
        saved = reservation(self.scope, "10.31.0.60/24", client_id="AA:BB:CC:00:00:60")
        saved.refresh_from_db()
        self.assertEqual(saved.client_id, "aa-bb-cc-00-00-60")
        self.assertTrue(Reservation.objects.filter(client_id="aa-bb-cc-00-00-60").exists())

    def test_save_leaves_an_unreadable_stored_value_alone(self):
        saved = reservation(self.scope, "10.31.0.61/24", client_id="printer-01")
        saved.refresh_from_db()
        self.assertEqual(saved.client_id, "printer-01")

    def test_the_same_identity_reaches_the_push_whatever_was_typed(self):
        reservation(self.scope, "10.31.0.62/24", client_id="AABB.CCDD.0062")
        (record,) = [r for r in netbox_state.read(self.server) if r.kind == "reservation"]
        self.assertEqual(record.fields["client_id"], "aa-bb-cc-dd-00-62")

    def test_a_mac_address_needs_no_client_id(self):
        mac = MACAddress.objects.create(mac_address="aa:bb:cc:00:00:63")
        Reservation(scope=self.scope, ip_address=ip_address("10.31.0.63/24"), mac_address=mac).clean()


# -- F32 ---------------------------------------------------------------------------------------------------------


class F32OptionTypeTest(TestCase):
    """The Windows data type is stored in Windows' spelling; a type not known here is kept, so imports never fail."""

    @classmethod
    def setUpTestData(cls):
        cls.server = windows_server()

    def _definition(self, windows_type, code=200):
        return OptionDefinition(server=self.server, code=code, name="Custom", windows_type=windows_type)

    def test_clean_stores_the_windows_spelling(self):
        for typed, stored in (
            ("string", "String"),
            ("Dword", "DWord"),
            ("IPV4ADDRESS", "IPv4Address"),
            (" binarydata ", "BinaryData"),
            ("encapsulateddata", "EncapsulatedData"),
            # Windows Server 2025 reads the 64-bit type back as DWordDWord (lab, 2026-11-22).
            ("DWordDword", "DWordDWord"),
            ("dworddword", "DWordDWord"),
        ):
            with self.subTest(typed=typed):
                obj = self._definition(typed)
                obj.clean()
                self.assertEqual(obj.windows_type, stored)

    def test_an_unknown_type_is_kept_as_typed(self):
        for typed in ("SomethingNew", "DWordDWordDWord"):
            with self.subTest(typed=typed):
                obj = self._definition(typed)
                obj.full_clean()
                self.assertEqual(obj.windows_type, typed)

    def test_save_normalises_without_clean(self):
        obj = self._definition("word", code=201)
        obj.save()
        obj.refresh_from_db()
        self.assertEqual(obj.windows_type, "Word")

    def test_every_type_windows_reports_is_imported_as_reported(self):
        fake = FakeDhcpServer()
        client = DhcpClient(fake)
        client.add_scope(
            WinScope(name="LAN", start_range="10.32.0.10", end_range="10.32.0.20", subnet_mask="255.255.255.0")
        )
        types = ("Byte", "Word", "DWord", "IPv4Address", "String", "BinaryData", "EncapsulatedData")
        for code, windows_type in enumerate(types, start=200):
            client.add_option_definition(WinOptionDefinition(option_id=code, name=f"t{code}", type=windows_type))
        result = importer.import_to_proposal(self.server, windows_state.read(client), None)
        self.assertEqual(importer.apply_items(result.proposal.items.all())[1], 0)
        stored = dict(OptionDefinition.objects.filter(code__gte=200).values_list("code", "windows_type"))
        self.assertEqual(stored, dict(enumerate(types, start=200)))
        # And nothing is left to import: the stored spelling compares equal to what Windows reports.
        self.assertEqual(importer.compute(self.server, windows_state.read(client))[0], [])

    def test_a_definition_stored_in_another_case_is_not_a_difference(self):
        # A row written before the model normalised the type (a raw update, as a branch merge would do it).
        fake = FakeDhcpServer()
        client = DhcpClient(fake)
        client.add_option_definition(WinOptionDefinition(option_id=210, name="Custom", type="String"))
        result = importer.import_to_proposal(self.server, windows_state.read(client), None)
        importer.apply_items(result.proposal.items.all())
        OptionDefinition.objects.filter(code=210).update(windows_type="string")
        changes = importer.compute(self.server, windows_state.read(client))[0]
        self.assertEqual([c for c in changes if c.kind == "optiondef"], [])


# -- F33 ---------------------------------------------------------------------------------------------------------


class F33PolicyRangesTest(TestCase):
    """Policy IP ranges are pairs of IPv4 address strings inside the policy's scope: nothing else can be synced."""

    @classmethod
    def setUpTestData(cls):
        cls.server = windows_server()
        cls.scope = scope(cls.server, "10.33.0.0/24")

    def _policy(self, ip_ranges):
        return WindowsPolicy(name="p", server=self.server, scope=self.scope, ip_ranges=ip_ranges)

    def test_valid_ranges(self):
        self._policy([["10.33.0.10", "10.33.0.20"], ["10.33.0.30", "10.33.0.30"]]).clean()
        self._policy([]).clean()

    def test_entries_that_are_not_two_strings_are_rejected(self):
        for ip_ranges in (
            [[1, 2]],
            [[True, 2]],
            [["10.33.0.10", 169938964]],
            [["10.33.0.10", "10.33.0.20", "10.33.0.30"]],
            [["10.33.0.10"]],
            ["10.33.0.10-10.33.0.20"],
            [{"start": "10.33.0.10", "end": "10.33.0.20"}],
            [None],
            [["10.33.0.10", None]],
            [["10.33.0.10", "not an address"]],
        ):
            with self.subTest(ip_ranges=ip_ranges):
                self.assertIn("ip_ranges", errors(self._policy(ip_ranges)))

    def test_an_ipv6_pair_is_rejected(self):
        self.assertIn("ip_ranges", errors(self._policy([["2001:db8::1", "2001:db8::2"]])))

    def test_a_range_outside_the_scope_is_rejected(self):
        for pair in (["192.168.9.1", "192.168.9.5"], ["10.33.0.250", "10.33.1.5"]):
            with self.subTest(pair=pair):
                errs = errors(self._policy([pair]))
                self.assertIn("outside the scope 10.33.0.0/24", errs["ip_ranges"][0])

    def test_what_passes_clean_can_be_read_by_the_sync(self):
        policy = self._policy([["10.33.0.10", "10.33.0.20"]])
        policy.full_clean()
        policy.save()
        (record,) = [r for r in netbox_state.read(self.server) if r.kind == "policy"]
        self.assertEqual(record.fields["ip_ranges"], [["10.33.0.10", "10.33.0.20"]])


# -- F34 ---------------------------------------------------------------------------------------------------------


class F34ServerPortTest(TestCase):
    def _port_errors(self, port):
        server = WindowsServer(name="f34", host="f34.example.com", credential_profile="lab", port=port)
        try:
            server.clean_fields()
        except ValidationError as exc:
            return exc.message_dict.get("port", [])
        return []

    def test_ports_outside_the_tcp_range_are_rejected(self):
        for port in (0, 65536, 70000):
            with self.subTest(port=port):
                self.assertTrue(self._port_errors(port))

    def test_real_ports_and_the_default_are_accepted(self):
        for port in (None, 1, 22, 5986, 65535):
            with self.subTest(port=port):
                self.assertEqual(self._port_errors(port), [])

    def test_a_stored_zero_still_means_the_default(self):
        # Rows saved before the check keep working in jobs: only the next edit is refused.
        server = windows_server("f34", port=0)
        self.assertEqual(server.effective_port, 5986)


# -- F35 ---------------------------------------------------------------------------------------------------------

LONG_NAME = "v" * 250  # "vendor/" + 250 characters did not fit the varchar(255) key columns


class F35LongKeyTest(TestCase):
    """Sync keys string together Windows names of up to 255 characters each; the key columns hold them."""

    def setUp(self):
        self.server = windows_server()

    def test_the_key_columns_have_no_length_limit(self):
        for model in (SyncState, ImportProposalItem):
            self.assertIsNone(model._meta.get_field("key").max_length)
        key = "10.1.0.0/10.1.0.50/43;vendor=" + "v" * 255 + ";user=" + "u" * 255 + ";policy=" + "p" * 255
        SyncState.objects.create(windows_server=self.server, kind="option", key=key, fingerprint="0" * 64)
        self.assertEqual(SyncState.objects.get().key, key)
        # The unique constraint still holds on the text column.
        with self.assertRaises(IntegrityError), transaction.atomic():
            SyncState.objects.create(windows_server=self.server, kind="option", key=key, fingerprint="1" * 64)

    def test_an_import_of_a_long_class_name_is_proposed_and_recorded(self):
        fake = FakeDhcpServer()
        client = DhcpClient(fake)
        client.add_class(DhcpClass(name=LONG_NAME, type="Vendor", data="long"))
        result = importer.import_to_proposal(self.server, windows_state.read(client), None)
        item = result.proposal.items.get(kind="class")
        self.assertEqual(item.key, f"vendor/{LONG_NAME}")
        self.assertEqual(importer.apply_items([item]), (1, 0))
        self.assertTrue(WindowsClass.objects.filter(name=LONG_NAME).exists())
        self.assertTrue(SyncState.objects.filter(kind="class", key=f"vendor/{LONG_NAME}").exists())


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class F35LongKeyPushTest(PushTestMixin, TestCase):
    def test_a_push_of_a_long_class_name_records_its_state(self):
        WindowsClass.objects.create(
            server=self.server, name=LONG_NAME, class_type=ClassTypeChoices.VENDOR, data="long vendor"
        )
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(run.summary["failed"], 0)
        self.assertTrue(SyncState.objects.filter(kind="class", key=f"vendor/{LONG_NAME}").exists())
        self.assertEqual(self.remaining(), [])


# -- F36 ---------------------------------------------------------------------------------------------------------


class F36SyncRunIndexTest(TestCase):
    def test_the_latest_run_lookup_has_an_index(self):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, SyncRun._meta.db_table)
        index = constraints["nbwd_syncrun_latest"]
        self.assertTrue(index["index"])
        self.assertEqual(index["columns"], ["windows_server_id", "kind", "created"])
        self.assertEqual(index["orders"], ["ASC", "ASC", "DESC"])
