"""Round-trip regressions across the actual NetBox and Windows state readers."""

from django.test import TestCase, override_settings

from netbox_windows_dhcp_plugin.choices import SyncStatusChoices
from netbox_windows_dhcp_plugin.models import OptionValue, Reservation, Scope
from netbox_windows_dhcp_plugin.windows.client import OptionDefinition, OptionKey

from .test_jobs import CREDENTIALS
from .test_push import PushTestMixin


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class TypedOptionRoundTripTest(PushTestMixin, TestCase):
    def _import_string(self, code):
        key = OptionKey(option_id=code)
        self.client.set_option_value(key, ["0010"])
        self.apply_all()
        return key, OptionValue.objects.get(server=self.server, code=code, scope__isnull=True)

    def test_numeric_looking_strings_push_for_standard_and_custom_types(self):
        for code in (67, 224):
            with self.subTest(code=code):
                key, value = self._import_string(code)
                value.value = ["10"]
                value.full_clean()
                value.save()
                run = self._run()
                self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
                self.assertEqual(self.client.get_option_value(key).value, ["10"])
                self.assertEqual(self.remaining(), [])

    def test_numeric_looking_string_import_is_not_mistaken_for_already_applied(self):
        key, value = self._import_string(67)
        self.client.set_option_value(key, ["10"])
        self.apply_all()
        value.refresh_from_db()
        self.assertEqual(value.value, ["10"])
        self.assertEqual(self.changes(), [])

    def test_numeric_looking_string_drift_prevents_deletion(self):
        key, value = self._import_string(67)
        self.client.set_option_value(key, ["10"])
        value.delete()
        run = self._run()
        self.assertEqual(self.client.get_option_value(key).value, ["10"])
        refused = [item for item in run.summary["not_pushed"] if item["key"] == "server/67"]
        self.assertEqual(len(refused), 1)
        self.assertIn("changed on Windows", refused[0]["reason"])

    def test_numeric_value_without_a_local_definition_converges_and_is_deletable(self):
        self.client.add_option_definition(OptionDefinition(option_id=225, name="Windows only", type="DWord"))
        value = OptionValue.objects.create(server=self.server, code=225, value=["0x10"])
        for expected in (1, 0, 0):
            run = self._run()
            self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
            self.assertEqual(run.summary["changes"], expected)
        value.delete()
        run = self._run()
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual(run.summary["delete"], 1)

    def test_long_names_and_suffix_edits_round_trip(self):
        scope, reservation = Scope.objects.get(), Reservation.objects.get()
        for length in (200, 201, 255):
            for suffix in ("a", "b"):
                with self.subTest(length=length, suffix=suffix):
                    name = "n" * (length - 1) + suffix
                    scope.name = reservation.name = name
                    scope.full_clean()
                    reservation.full_clean()
                    scope.save()
                    reservation.save()
                    run = self._run()
                    self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
                    self.assertEqual(self.client.get_scope("10.1.0.0").name, name)
                    self.assertEqual(self.client.get_reservation("10.1.0.0", "10.1.0.50").name, name)
                    self.assertEqual(self.remaining(), [])
                    self.assertEqual(self.changes(), [])
