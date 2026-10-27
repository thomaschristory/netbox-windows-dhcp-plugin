"""The validation rules of the plugin's models (clean()), which forms, CSV imports and the REST API all run."""

from dcim.models import MACAddress
from django.core.exceptions import ValidationError
from django.test import TestCase

from netbox_windows_dhcp_plugin.choices import ClassTypeChoices
from netbox_windows_dhcp_plugin.models import (
    OptionDefinition,
    OptionValue,
    Reservation,
    Scope,
    Superscope,
    WindowsClass,
    WindowsPolicy,
)

from .fixtures import ip_address, ip_range, prefix, reservation, scope, windows_server, windows_servers


def errors(obj):
    """The message dict of the ValidationError clean() raises (fails the test when it raises none)."""
    try:
        obj.clean()
    except ValidationError as exc:
        # An error not tied to one field is reported under "__all__", as forms do.
        return exc.message_dict if hasattr(exc, "error_dict") else {"__all__": exc.messages}
    raise AssertionError(f"{obj!r} validated")


class WindowsServerCleanTest(TestCase):
    def test_device_or_virtual_machine_not_both(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
        from virtualization.models import VirtualMachine

        site = Site.objects.create(name="s", slug="s")
        manufacturer = Manufacturer.objects.create(name="m", slug="m")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="t", slug="t")
        role = DeviceRole.objects.create(name="r", slug="r")
        device = Device.objects.create(name="d", site=site, device_type=device_type, role=role)
        vm = VirtualMachine.objects.create(name="vm", site=site)
        server = windows_server()
        server.device = device
        server.clean()
        server.virtual_machine = vm
        self.assertIn("__all__", errors(server))
        server.device = None
        server.clean()


class ScopeCleanTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server, cls.other = windows_servers(2)

    def test_range_inside_the_prefix(self):
        Scope(
            server=self.server, prefix=prefix("10.1.0.0/24"), ip_range=ip_range("10.1.0.10/24", "10.1.0.20/24")
        ).clean()

    def test_range_outside_the_prefix_is_rejected(self):
        obj = Scope(server=self.server, prefix=prefix("10.1.0.0/24"), ip_range=ip_range("10.1.0.10/16", "10.1.1.20/16"))
        self.assertIn("ip_range", errors(obj))

    def test_v6_scope_has_no_range(self):
        obj = Scope(
            server=self.server, prefix=prefix("2001:db8::/64"), ip_range=ip_range("10.1.0.10/24", "10.1.0.20/24")
        )
        self.assertIn("ip_range", errors(obj))

    def test_v6_scope_has_no_superscope(self):
        superscope = Superscope.objects.create(server=self.server, name="Campus")
        obj = Scope(server=self.server, prefix=prefix("2001:db8::/64"), superscope=superscope)
        self.assertIn("superscope", errors(obj))
        Scope(server=self.server, prefix=prefix("2001:db8:1::/64")).clean()

    def test_superscope_of_another_server_is_rejected(self):
        superscope = Superscope.objects.create(server=self.other, name="Campus")
        obj = Scope(server=self.server, prefix=prefix("10.2.0.0/24"), superscope=superscope)
        self.assertIn("superscope", errors(obj))

    def test_properties_follow_the_prefix(self):
        s = scope(self.server, "10.3.0.0/24")
        self.assertEqual((s.scope_id, s.family, str(s)), ("10.3.0.0", 4, "dhcp1/10.3.0.0"))
        s6 = scope(self.server, "2001:db8:5::/64")
        self.assertEqual((s6.scope_id, s6.family), ("2001:db8:5::", 6))
        self.assertIsNone(s6.ip_range)


class ReservationCleanTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = windows_server()
        cls.scope = scope(cls.server, "10.1.0.0/24")
        cls.scope6 = scope(cls.server, "2001:db8:1::/64")

    def test_address_inside_the_scope(self):
        Reservation(scope=self.scope, ip_address=ip_address("10.1.0.50/24"), client_id="aa").clean()

    def test_address_outside_the_scope_is_rejected(self):
        obj = Reservation(scope=self.scope, ip_address=ip_address("10.2.0.50/24"))
        self.assertIn("ip_address", errors(obj))

    def test_v4_reservation_has_no_duid(self):
        obj = Reservation(scope=self.scope, ip_address=ip_address("10.1.0.51/24"), duid="00-01", iaid=1)
        self.assertIn("__all__", errors(obj))

    def test_v4_reservation_has_a_mac_or_a_client_id(self):
        mac = MACAddress.objects.create(mac_address="00:15:5d:00:00:01")
        obj = Reservation(scope=self.scope, ip_address=ip_address("10.1.0.52/24"), mac_address=mac, client_id="x")
        self.assertIn("client_id", errors(obj))

    def test_v6_reservation_has_no_mac(self):
        mac = MACAddress.objects.create(mac_address="00:15:5d:00:00:02")
        obj = Reservation(scope=self.scope6, ip_address=ip_address("2001:db8:1::10/64"), mac_address=mac)
        self.assertIn("__all__", errors(obj))
        Reservation(scope=self.scope6, ip_address=ip_address("2001:db8:1::11/64"), duid="00-01-00-01", iaid=3).clean()


class ExclusionRangeCleanTest(TestCase):
    def test_range_rules(self):
        from netbox_windows_dhcp_plugin.models import ExclusionRange

        s = scope(windows_server(), "10.1.0.0/24")
        ExclusionRange(scope=s, start_address="10.1.0.10", end_address="10.1.0.20").clean()
        for start, end in (("10.1.0.20", "10.1.0.10"), ("10.1.0.10", "2001:db8::1"), ("10.9.0.1", "10.9.0.5")):
            with self.subTest(start=start, end=end), self.assertRaises(ValidationError):
                ExclusionRange(scope=s, start_address=start, end_address=end).clean()

    def test_dhcpv6_ranges_are_refused(self):
        # They would never be synced: DHCPv6 exclusions are left out on both sides.
        from netbox_windows_dhcp_plugin.models import ExclusionRange

        s = scope(windows_server(), "2001:db8:1::/64")
        exclusion = ExclusionRange(scope=s, start_address="2001:db8:1::10", end_address="2001:db8:1::20")
        self.assertIn("scope", errors(exclusion))


class WindowsClassConstraintTest(TestCase):
    def test_names_differing_only_in_case_are_one_class(self):
        # Windows names are not case sensitive; two such classes would make every sync of the server fail.
        server = windows_server()
        WindowsClass.objects.create(server=server, name="Phones", class_type=ClassTypeChoices.USER, data="p")
        duplicate = WindowsClass(server=server, name="phones", class_type=ClassTypeChoices.USER, data="q")
        with self.assertRaises(ValidationError):
            duplicate.full_clean()


class FailoverCleanTest(TestCase):
    def test_missing_server_is_a_field_error(self):
        from netbox_windows_dhcp_plugin.models import FailoverRelationship

        relationship = FailoverRelationship(
            name="ha", primary_server=windows_server(), shared_secret_profile="lab-failover"
        )
        with self.assertRaises(ValidationError) as ctx:
            relationship.full_clean()
        self.assertIn("partner_server", ctx.exception.message_dict)


class FailoverFormTest(TestCase):
    def test_posted_scope_of_another_server_is_refused(self):
        # The form's scope picker only filters what it offers; a crafted POST may still name any scope.
        from netbox_windows_dhcp_plugin.forms import FailoverRelationshipForm

        primary, partner = windows_servers(2)
        foreign = scope(partner, "10.70.0.0/24")
        form = FailoverRelationshipForm(
            data={
                "name": "ha",
                "primary_server": primary.pk,
                "partner_server": partner.pk,
                "mode": "loadbalance",
                "max_client_lead_time": 3600,
                "scopes": [foreign.pk],
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("scopes", form.errors)


class OptionDefinitionCleanTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server, cls.other = windows_servers(2)
        cls.vendor = WindowsClass.objects.create(
            server=cls.server, name="MSFT", class_type=ClassTypeChoices.VENDOR, data="MSFT 5.0"
        )

    def test_vendor_definition(self):
        OptionDefinition(server=self.server, vendor_class=self.vendor, code=1, name="x").clean()

    def test_user_class_is_rejected(self):
        user = WindowsClass.objects.create(server=self.server, name="u", class_type=ClassTypeChoices.USER, data="u")
        self.assertIn("vendor_class", errors(OptionDefinition(server=self.server, vendor_class=user, code=1, name="x")))

    def test_vendor_class_of_another_server_is_rejected(self):
        obj = OptionDefinition(server=self.other, vendor_class=self.vendor, code=1, name="x")
        self.assertIn("vendor_class", errors(obj))

    def test_vendor_class_of_another_family_is_rejected(self):
        obj = OptionDefinition(server=self.server, family=6, vendor_class=self.vendor, code=1, name="x")
        self.assertIn("vendor_class", errors(obj))

    def test_default_value_is_a_string_list(self):
        obj = OptionDefinition(server=self.server, code=224, name="x", default_value="0")
        self.assertIn("default_value", errors(obj))


class WindowsPolicyCleanTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server, cls.other = windows_servers(2)
        cls.scope = scope(cls.server, "10.1.0.0/24")

    def _errors(self, **kwargs):
        return errors(WindowsPolicy(name="p", **kwargs))

    def test_valid_scope_policy(self):
        WindowsPolicy(name="p", server=self.server, scope=self.scope, ip_ranges=[["10.1.0.10", "10.1.0.20"]]).clean()

    def test_scope_without_server_is_not_a_crash(self):
        # The form still runs clean() when the required server is missing; that must not crash.
        WindowsPolicy(name="p", scope=self.scope).clean()

    def test_scope_of_another_server_is_rejected(self):
        self.assertIn("scope", self._errors(server=self.other, scope=self.scope))

    def test_v6_scope_is_rejected(self):
        self.assertIn("scope", self._errors(server=self.server, scope=scope(self.server, "2001:db8::/64")))

    def test_server_policy_has_no_ip_ranges(self):
        self.assertIn("ip_ranges", self._errors(server=self.server, ip_ranges=[["10.1.0.10", "10.1.0.20"]]))

    def test_mixed_families_are_rejected(self):
        errs = self._errors(server=self.server, scope=self.scope, ip_ranges=[["10.1.0.10", "2001:db8::1"]])
        self.assertIn("ip_ranges", errs)

    def test_reversed_range_is_rejected(self):
        errs = self._errors(server=self.server, scope=self.scope, ip_ranges=[["10.1.0.20", "10.1.0.10"]])
        self.assertIn("ip_ranges", errs)


class OptionValueCleanTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server, cls.other = windows_servers(2)
        cls.scope = scope(cls.server, "10.1.0.0/24")
        cls.scope2 = scope(cls.server, "10.2.0.0/24")
        cls.scope6 = scope(cls.server, "2001:db8:1::/64")
        cls.reservation = reservation(cls.scope, "10.1.0.50/24", client_id="aa")
        cls.vendor = WindowsClass.objects.create(
            server=cls.server, name="MSFT", class_type=ClassTypeChoices.VENDOR, data="MSFT 5.0"
        )
        cls.user = WindowsClass.objects.create(
            server=cls.server, name="Labs", class_type=ClassTypeChoices.USER, data="labs"
        )

    def value(self, **kwargs):
        kwargs.setdefault("server", self.server)
        kwargs.setdefault("code", 3)
        kwargs.setdefault("value", ["10.1.0.1"])
        return OptionValue(**kwargs)

    def test_levels(self):
        self.assertEqual(self.value().level, "server")
        self.assertEqual(self.value(scope=self.scope).level, "scope")
        self.assertEqual(self.value(reservation=self.reservation).level, "reservation")

    def test_reservation_sets_its_scope(self):
        obj = self.value(reservation=self.reservation)
        obj.clean()
        self.assertEqual(obj.scope_id, self.scope.pk)
        self.assertIn("scope", errors(self.value(reservation=self.reservation, scope=self.scope2)))

    def test_value_is_a_non_empty_string_list(self):
        for value in ([], "10.1.0.1", [1]):
            with self.subTest(value=value):
                self.assertIn("value", errors(self.value(value=value)))

    def test_scope_of_another_server_or_family_is_rejected(self):
        self.assertIn("scope", errors(self.value(server=self.other, scope=self.scope)))
        self.assertIn("family", errors(self.value(scope=self.scope6)))
        self.value(scope=self.scope6, family=6, code=23, value=["2001:db8::53"]).clean()

    def test_class_rules(self):
        self.value(vendor_class=self.vendor, user_class=self.user).clean()
        self.assertIn("vendor_class", errors(self.value(vendor_class=self.user)))
        self.assertIn("user_class", errors(self.value(user_class=self.vendor)))
        self.assertIn("user_class", errors(self.value(server=self.other, user_class=self.user)))
        # Windows keeps IPv4 and DHCPv6 classes apart.
        self.assertIn("vendor_class", errors(self.value(family=6, vendor_class=self.vendor)))

    def test_policy_rules(self):
        server_policy = WindowsPolicy.objects.create(server=self.server, name="srv")
        scope_policy = WindowsPolicy.objects.create(server=self.server, scope=self.scope, name="p")
        self.value(policy=server_policy).clean()
        self.value(policy=scope_policy, scope=self.scope, vendor_class=self.vendor).clean()
        # A scope policy's values live on its scope, a server policy's on the server.
        self.assertIn("policy", errors(self.value(policy=scope_policy)))
        self.assertIn("policy", errors(self.value(policy=server_policy, scope=self.scope)))
        self.assertIn("user_class", errors(self.value(policy=server_policy, user_class=self.user)))
        self.assertIn("policy", errors(self.value(policy=scope_policy, reservation=self.reservation)))
        self.assertIn("policy", errors(self.value(policy=server_policy, family=6)))
        self.assertIn("policy", errors(self.value(server=self.other, policy=server_policy)))
