"""Parent identity edits must not leave configuration invisible to either server's sync."""

from django.core.exceptions import ValidationError
from django.test import TestCase

from netbox_windows_dhcp_plugin.choices import ClassTypeChoices
from netbox_windows_dhcp_plugin.models import (
    ExclusionRange,
    FailoverRelationship,
    OptionDefinition,
    OptionValue,
    Reservation,
    Scope,
    Superscope,
    WindowsClass,
    WindowsPolicy,
)

from .fixtures import ip_address, prefix, reservation, scope, windows_servers


class ParentMoveTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server, cls.other = windows_servers(2)
        cls.lan = scope(cls.server, "10.1.0.0/24")
        cls.destination = scope(cls.other, "10.1.0.0/24")
        cls.printer = reservation(cls.lan, "10.1.0.50/24", client_id="aa")

    def assert_rejected(self, obj, field):
        for validate in (obj.clean, obj.save):
            with self.subTest(path=validate.__name__), self.assertRaises(ValidationError) as ctx:
                validate()
            self.assertIn(field, ctx.exception.message_dict)

    def option(self, **kwargs):
        return OptionValue.objects.create(server=self.server, code=3, value=["10.1.0.1"], **kwargs)

    def test_scope_server_move_with_direct_option_is_rejected(self):
        value = self.option(scope=self.lan)
        self.lan.server = self.other
        self.assert_rejected(self.lan, "server")
        self.lan.refresh_from_db()
        value.refresh_from_db()
        self.assertEqual(self.lan.server_id, self.server.pk)
        self.assertEqual((value.server_id, value.scope_id), (self.server.pk, self.lan.pk))

    def test_scope_server_move_with_reservation_option_is_rejected(self):
        value = self.option(scope=self.lan, reservation=self.printer)
        self.lan.server = self.other
        self.assert_rejected(self.lan, "server")
        value.refresh_from_db()
        self.assertEqual((value.server_id, value.scope_id), (self.server.pk, self.lan.pk))

    def test_scope_move_also_checks_legacy_reservation_values_without_scope(self):
        self.option(reservation=self.printer)
        self.lan.server = self.other
        self.assert_rejected(self.lan, "server")

    def test_scope_server_move_with_policy_is_rejected(self):
        policy = WindowsPolicy.objects.create(server=self.server, scope=self.lan, name="phones")
        self.lan.server = self.other
        self.assert_rejected(self.lan, "server")
        policy.refresh_from_db()
        self.assertEqual(policy.server_id, self.server.pk)

    def test_scope_server_move_with_failover_is_rejected(self):
        relationship = FailoverRelationship.objects.create(
            name="ha", primary_server=self.server, partner_server=self.other
        )
        relationship.scopes.add(self.lan)
        self.lan.server = self.other
        self.assert_rejected(self.lan, "server")

    def test_scope_server_move_without_invalidated_children_is_allowed(self):
        ExclusionRange.objects.create(scope=self.lan, start_address="10.1.0.10", end_address="10.1.0.20")
        self.lan.server = self.other
        self.lan.full_clean()
        self.lan.save()
        self.printer.refresh_from_db()
        self.assertEqual(self.printer.server.pk, self.other.pk)
        self.assertEqual(self.lan.exclusion_ranges.count(), 1)

    def test_regular_scope_update_with_dependencies_is_allowed(self):
        self.option(scope=self.lan, reservation=self.printer)
        WindowsPolicy.objects.create(server=self.server, scope=self.lan, name="phones")
        self.lan.description = "Imported description"
        self.lan.full_clean()
        self.lan.save()
        self.lan.refresh_from_db()
        self.assertEqual(self.lan.description, "Imported description")

    def test_save_ignores_parent_fields_not_in_update_fields(self):
        self.option(scope=self.lan)
        self.lan.server = self.other
        self.lan.description = "Only this field is written"
        self.lan.save(update_fields=["description"])
        self.lan.refresh_from_db()
        self.assertEqual(self.lan.server_id, self.server.pk)
        self.assertEqual(self.lan.description, "Only this field is written")

    def test_partial_save_uses_persisted_other_parent_fields(self):
        self.option(scope=self.lan)
        self.lan.server = self.other
        self.lan.prefix = prefix("10.1.0.0/23")
        self.lan.save(update_fields=["prefix"])
        self.lan.refresh_from_db()
        self.assertEqual(self.lan.server_id, self.server.pk)
        self.assertEqual(str(self.lan.prefix.prefix), "10.1.0.0/23")

    def test_reservation_move_with_option_is_rejected_on_either_server(self):
        self.option(scope=self.lan, reservation=self.printer)
        same_server = scope(self.server, "10.1.0.0/23")
        for destination in (same_server, self.destination):
            with self.subTest(server=destination.server_id):
                self.printer.scope = destination
                self.assert_rejected(self.printer, "scope")
        self.printer.refresh_from_db()
        self.assertEqual(self.printer.scope_id, self.lan.pk)

    def test_reservation_move_without_options_is_allowed(self):
        self.printer.scope = self.destination
        self.printer.full_clean()
        self.printer.save()
        self.printer.refresh_from_db()
        self.assertEqual(self.printer.scope_id, self.destination.pk)

    def test_reservation_address_edit_with_options_is_allowed(self):
        value = self.option(scope=self.lan, reservation=self.printer)
        self.printer.ip_address = ip_address("10.1.0.51/24")
        self.printer.full_clean()
        self.printer.save()
        value.refresh_from_db()
        self.assertEqual(value.reservation_id, self.printer.pk)

    def test_prefix_move_must_keep_reservation_addresses(self):
        self.lan.prefix = prefix("10.2.0.0/24")
        self.lan.ip_range = None
        self.assert_rejected(self.lan, "prefix")

    def test_prefix_move_must_keep_exclusions(self):
        self.printer.delete()
        ExclusionRange.objects.create(scope=self.lan, start_address="10.1.0.10", end_address="10.1.0.20")
        self.lan.prefix = prefix("10.2.0.0/24")
        self.lan.ip_range = None
        self.assert_rejected(self.lan, "prefix")

    def test_prefix_move_must_keep_policy_ranges(self):
        self.printer.delete()
        WindowsPolicy.objects.create(
            server=self.server, scope=self.lan, name="phones", ip_ranges=[["10.1.0.10", "10.1.0.20"]]
        )
        self.lan.prefix = prefix("10.2.0.0/24")
        self.lan.ip_range = None
        self.assert_rejected(self.lan, "prefix")

    def test_prefix_widening_without_invalidated_children_is_allowed(self):
        self.option(scope=self.lan, reservation=self.printer)
        self.lan.prefix = prefix("10.1.0.0/23")
        self.lan.full_clean()
        self.lan.save()
        self.assertEqual(Scope.objects.get(pk=self.lan.pk).family, 4)

    def test_family_change_with_option_is_rejected(self):
        self.printer.delete()
        self.option(scope=self.lan)
        self.lan.prefix = prefix("2001:db8:1::/64")
        self.lan.ip_range = None
        self.assert_rejected(self.lan, "prefix")

    def test_family_change_with_policy_is_rejected(self):
        self.printer.delete()
        WindowsPolicy.objects.create(server=self.server, scope=self.lan, name="phones")
        self.lan.prefix = prefix("2001:db8:1::/64")
        self.lan.ip_range = None
        self.assert_rejected(self.lan, "prefix")

    def test_family_change_with_failover_is_rejected(self):
        self.printer.delete()
        relationship = FailoverRelationship.objects.create(
            name="ha", primary_server=self.server, partner_server=self.other
        )
        relationship.scopes.add(self.lan)
        self.lan.prefix = prefix("2001:db8:1::/64")
        self.lan.ip_range = None
        self.assert_rejected(self.lan, "prefix")

    def test_family_change_without_dependencies_is_allowed(self):
        self.printer.delete()
        self.lan.prefix = prefix("2001:db8:1::/64")
        self.lan.ip_range = None
        self.lan.full_clean()
        self.lan.save()
        self.assertEqual(Scope.objects.get(pk=self.lan.pk).family, 6)

    def test_policy_move_with_option_is_rejected(self):
        policy = WindowsPolicy.objects.create(server=self.server, scope=self.lan, name="phones")
        self.option(scope=self.lan, policy=policy)
        policy.scope = None
        self.assert_rejected(policy, "scope")
        policy.server = self.other
        policy.scope = self.destination
        self.assert_rejected(policy, "server")

    def test_policy_move_without_option_is_allowed(self):
        policy = WindowsPolicy.objects.create(server=self.server, scope=self.lan, name="phones")
        policy.scope, policy.server = self.destination, self.other
        policy.full_clean()
        policy.save()
        policy.refresh_from_db()
        self.assertEqual((policy.server_id, policy.scope_id), (self.other.pk, self.destination.pk))

    def test_class_identity_change_with_definition_is_rejected(self):
        vendor = WindowsClass.objects.create(
            server=self.server, class_type=ClassTypeChoices.VENDOR, name="phones", data="phones"
        )
        OptionDefinition.objects.create(server=self.server, vendor_class=vendor, code=1, name="address")
        for field, value in (("server", self.other), ("family", 6), ("class_type", ClassTypeChoices.USER)):
            with self.subTest(field=field):
                vendor.refresh_from_db()
                setattr(vendor, field, value)
                self.assert_rejected(vendor, field)

    def test_class_identity_change_with_value_is_rejected(self):
        for class_type, relation in (
            (ClassTypeChoices.VENDOR, "vendor_class"),
            (ClassTypeChoices.USER, "user_class"),
        ):
            cls = WindowsClass.objects.create(server=self.server, class_type=class_type, name="phones", data="phones")
            self.option(**{relation: cls})
            for field, value in (
                ("server", self.other),
                ("family", 6),
                (
                    "class_type",
                    ClassTypeChoices.USER if class_type == ClassTypeChoices.VENDOR else ClassTypeChoices.VENDOR,
                ),
            ):
                with self.subTest(relation=relation, field=field):
                    cls.refresh_from_db()
                    setattr(cls, field, value)
                    self.assert_rejected(cls, field)

    def test_unreferenced_class_move_and_referenced_class_data_edit_are_allowed(self):
        cls = WindowsClass.objects.create(
            server=self.server, class_type=ClassTypeChoices.USER, name="phones", data="phones"
        )
        cls.server = self.other
        cls.full_clean()
        cls.save()
        OptionValue.objects.create(server=self.other, user_class=cls, code=3, value=["10.1.0.1"])
        cls.data = "new client data"
        cls.full_clean()
        cls.save()
        cls.refresh_from_db()
        self.assertEqual((cls.server_id, cls.data), (self.other.pk, "new client data"))

    def test_superscope_move_with_scope_is_rejected(self):
        parent = Superscope.objects.create(server=self.server, name="campus")
        self.lan.superscope = parent
        self.lan.save()
        parent.server = self.other
        self.assert_rejected(parent, "server")

    def test_new_parents_have_no_dependent_validation(self):
        for obj in (
            Scope(server=self.server, prefix=prefix("10.5.0.0/24"), name="new"),
            Reservation(scope=self.lan, ip_address=ip_address("10.1.0.60/24"), client_id="bb"),
            WindowsPolicy(server=self.server, scope=self.lan, name="new"),
            WindowsClass(server=self.server, class_type=ClassTypeChoices.USER, name="new", data="new"),
        ):
            with self.subTest(model=type(obj).__name__):
                obj.full_clean()
                obj.save()

    def test_scope_edit_form_reports_the_dependency_error(self):
        from netbox_windows_dhcp_plugin.forms import ScopeForm

        self.option(scope=self.lan)
        form = ScopeForm(
            instance=self.lan,
            data={
                "server": self.other.pk,
                "prefix": self.lan.prefix_id,
                "ip_range": self.lan.ip_range_id,
                "name": self.lan.name,
                "state": "active",
                "scope_type": "dhcp",
                "delay_ms": 0,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("server", form.errors)

    def test_reservation_edit_form_reports_the_dependency_error(self):
        from netbox_windows_dhcp_plugin.forms import ReservationForm

        self.option(scope=self.lan, reservation=self.printer)
        form = ReservationForm(
            instance=self.printer,
            data={
                "scope": self.destination.pk,
                "ip_address": self.printer.ip_address_id,
                "client_id": self.printer.client_id,
                "reservation_type": "both",
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("scope", form.errors)
