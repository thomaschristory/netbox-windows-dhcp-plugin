"""
Migration 0003 removes the content types (and their change log) of the 0.1.x models that 0.2.0 dropped.

The leftovers of a 0.1.x upgrade are rebuilt by hand: object types for dropped model names, change log entries that
point at them, and controls that must survive (another app's stale type, a current model's type). The migration
function runs with the historical models of its own migration state, as `migrate` runs it.
"""

import importlib
import io
import uuid
from contextlib import redirect_stdout
from types import SimpleNamespace

from core.models import ObjectChange, ObjectType
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.models import ProtectedError
from django.test import TestCase
from extras.models import Bookmark
from users.models import ObjectPermission

from netbox_windows_dhcp_plugin.models import Scope

APP = "netbox_windows_dhcp_plugin"
MIGRATION = "0003_remove_old_content_types"
migration = importlib.import_module(f"{APP}.migrations.{MIGRATION}")


def stale_type(app_label, model):
    # ObjectType is a content type with NetBox's extra columns: creating one writes both rows, as NetBox did when
    # the 0.1.x models were installed.
    return ObjectType.objects.create(app_label=app_label, model=model, public=True, features=["change_logging"])


def change(object_type, related_type=None):
    return ObjectChange.objects.create(
        user_name="admin",
        request_id=uuid.uuid4(),
        action="create",
        changed_object_type_id=object_type.pk,
        changed_object_id=1,
        related_object_type_id=related_type.pk if related_type else None,
        related_object_id=1 if related_type else None,
        object_repr="old object",
    )


class OldContentTypesTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The models as they are just before 0003 runs (what `migrate` hands to the function).
        loader = MigrationLoader(connection)
        cls.historical = loader.project_state((APP, MIGRATION), at_end=False).apps

    def run_migration(self):
        out = io.StringIO()
        with redirect_stdout(out):
            migration.remove_old_content_types(self.historical, SimpleNamespace(connection=connection))
        ContentType.objects.clear_cache()
        return out.getvalue()

    def setUp(self):
        self.scope_old = stale_type(APP, "windowsscope")
        self.option_old = stale_type(APP, "windowsoptiondefinition")
        self.other_app = stale_type("someoldplugin", "widget")
        self.live = ObjectType.objects.get_for_model(Scope)
        self.scope_changes = [change(self.scope_old), change(self.scope_old)]
        self.option_change = change(self.option_old)
        self.other_change = change(self.other_app)
        # A change of a current object that names an old object as related: kept, link cleared.
        self.live_change = change(self.live, related_type=self.scope_old)

    def test_removes_old_types_and_their_change_log(self):
        output = self.run_migration()

        old_ids = (self.scope_old.pk, self.option_old.pk)
        self.assertFalse(ContentType.objects.filter(pk__in=old_ids).exists())
        self.assertFalse(ObjectType.objects.filter(pk__in=old_ids).exists())
        self.assertFalse(ObjectChange.objects.filter(changed_object_type_id__in=old_ids).exists())
        self.assertIn("Removed 2 content type(s)", output)
        self.assertIn("3 change log entr", output)

        # Controls: another app's stale type, a current model's type, and their change log stay.
        self.assertTrue(ObjectType.objects.filter(pk=self.other_app.pk).exists())
        self.assertTrue(ObjectChange.objects.filter(pk=self.other_change.pk).exists())
        self.assertTrue(ObjectType.objects.filter(pk=self.live.pk).exists())
        live_change = ObjectChange.objects.get(pk=self.live_change.pk)
        self.assertEqual(live_change.changed_object_type_id, self.live.pk)
        self.assertIsNone(live_change.related_object_type_id)
        self.assertIsNone(live_change.related_object_id)

    def test_second_run_is_a_no_op(self):
        self.run_migration()
        types, changes = ContentType.objects.count(), ObjectChange.objects.count()
        self.assertEqual(self.run_migration(), "")
        self.assertEqual((ContentType.objects.count(), ObjectChange.objects.count()), (types, changes))

    def test_fresh_install_is_a_no_op(self):
        ObjectChange.objects.filter(changed_object_type__app_label=APP).delete()
        ObjectChange.objects.filter(related_object_type__app_label=APP).delete()
        ObjectType.objects.filter(pk__in=(self.scope_old.pk, self.option_old.pk)).delete()
        types, changes = ContentType.objects.count(), ObjectChange.objects.count()
        self.assertEqual(self.run_migration(), "")
        self.assertEqual((ContentType.objects.count(), ObjectChange.objects.count()), (types, changes))

    def test_remove_stale_contenttypes_then_succeeds(self):
        # Before the migration, Django's command stops on the protected change log (what netbox-docker hit).
        with self.assertRaises(ProtectedError):
            call_command("remove_stale_contenttypes", interactive=False, verbosity=0)
        self.run_migration()
        # Not installed apps (someoldplugin) are skipped by the command unless --include-stale-apps is given.
        call_command("remove_stale_contenttypes", interactive=False, verbosity=0)
        self.assertFalse(ContentType.objects.filter(app_label=APP, model="windowsscope").exists())

    def test_keeps_a_type_that_is_the_last_one_of_a_permission(self):
        user = get_user_model().objects.create(username="perm-test")
        only_old = ObjectPermission.objects.create(name="only old", actions=["view"])
        only_old.object_types.set([self.scope_old.pk])
        mixed = ObjectPermission.objects.create(name="mixed", actions=["view"])
        mixed.object_types.set([self.option_old.pk, self.live.pk])
        only_old.users.add(user)

        output = self.run_migration()

        # The permission would otherwise be left with no type at all: keep the type and its change log.
        self.assertIn(f"Kept the old type {APP}.windowsscope", output)
        self.assertTrue(ObjectType.objects.filter(pk=self.scope_old.pk).exists())
        self.assertEqual(ObjectChange.objects.filter(changed_object_type_id=self.scope_old.pk).count(), 2)
        self.assertEqual(list(only_old.object_types.values_list("pk", flat=True)), [self.scope_old.pk])
        # The mixed permission keeps its current type; the old one goes.
        self.assertFalse(ObjectType.objects.filter(pk=self.option_old.pk).exists())
        self.assertEqual(list(mixed.object_types.values_list("pk", flat=True)), [self.live.pk])

    def test_keeps_a_type_another_protected_row_uses(self):
        user = get_user_model().objects.create(username="bookmark-test")
        Bookmark.objects.create(user=user, object_type_id=self.scope_old.pk, object_id=1)

        output = self.run_migration()

        self.assertIn(f"Kept the old type {APP}.windowsscope: still used by extras.Bookmark", output)
        self.assertTrue(ObjectType.objects.filter(pk=self.scope_old.pk).exists())
        # Its change log is restored with it (one savepoint per type), the other type still goes.
        self.assertEqual(ObjectChange.objects.filter(changed_object_type_id=self.scope_old.pk).count(), 2)
        self.assertEqual(ObjectChange.objects.get(pk=self.live_change.pk).related_object_type_id, self.scope_old.pk)
        self.assertFalse(ObjectType.objects.filter(pk=self.option_old.pk).exists())

    def test_never_touches_current_models(self):
        # A current model name in the old list (windowsserver existed in 0.1.x and still does) is never a candidate.
        server = ObjectType.objects.get_for_model(django_apps.get_model(APP, "windowsserver"))
        server_change = change(server)
        self.run_migration()
        self.assertTrue(ObjectType.objects.filter(pk=server.pk).exists())
        self.assertTrue(ObjectChange.objects.filter(pk=server_change.pk).exists())

    def test_marked_fake_on_branch(self):
        self.assertIs(migration.fake_on_branch, True)
