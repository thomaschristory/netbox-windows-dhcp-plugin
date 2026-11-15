"""
Remove what NetBox still remembers about the 0.1.x models that 0.2.0 dropped.

0.2.0 (migration 0001_standalone) dropped the tables of six 0.1.x models (WindowsScope, WindowsReservation,
WindowsOptionDefinition, WindowsOptionQualifier, WindowsVendorOptionDefinition, WindowsVendorOption). NetBox still
keeps a content type (and an object type) for each of them, and the change log entries written while 0.1.x ran
still point at those types. Django's `remove_stale_contenttypes` command then stops with a ProtectedError, because
NetBox refuses to delete a type that change log entries use. The netbox-docker image runs that command whenever a
migration was applied, so its NetBox container exited on the first start after every new migration.

This migration deletes the change log entries of those dropped objects, then the stale types. Deleting a type also
removes what NetBox attached to it, exactly as `remove_stale_contenttypes` would: its permissions, search index
rows, jobs, and its place in lists of object types that keep another type. The objects themselves were already
gone since 0.2.0; only their history goes now.

What it never touches:
- the types of other apps (netbox-plugin-dhcp's included) and of this plugin's current models;
- a type that something configured by an administrator still needs: when deleting it would leave a permission, tag,
  custom field, change control policy or similar with no object type at all, or when another protected reference
  (a bookmark, a notification, an open branch's diff) uses it. Such a type is left as it was, change log
  included, and the migration says why, so the administrator can decide.

On a fresh 0.2 install there is nothing to remove and the migration does nothing. Running it again changes nothing.
"""

from django.db import migrations, transaction
from django.db.models import ProtectedError, RestrictedError

APP_LABEL = "netbox_windows_dhcp_plugin"

# Every model name of 0.1.x (migrations 0001_initial to 0006). The ones that still exist in the current model are
# left out at run time, so only the names 0.2.0 really dropped are candidates. A fixed list (rather than "any type
# of this app without a model") keeps the migration from touching the type of a model added by a later version,
# for example after migrating backwards and forwards again.
OLD_MODELS = (
    "exclusionrange",
    "failoverrelationship",
    "importproposal",
    "importproposalitem",
    "macfilter",
    "macfilterlists",
    "syncrun",
    "syncstate",
    "windowsclass",
    "windowsoptiondefinition",
    "windowsoptionqualifier",
    "windowspolicy",
    "windowsreservation",
    "windowsscope",
    "windowsserver",
    "windowsvendoroption",
    "windowsvendoroptiondefinition",
)


def _say(message):
    # migrate prints each migration on its own line; indent ours under it.
    print(f"\n  {message}", end="")


def _emptied_holders(type_models, ct_id, stale_ids):
    """
    Describe the configuration objects that would lose their last object type if `ct_id` were deleted.

    Permissions, tags, custom fields, event rules, saved filters, change control policies and the like hold a
    list of object types. Deleting a type removes it from those lists. That changes nothing when the list keeps
    another type, but an empty list can mean something else entirely (a tag or a change control policy with no
    type applies to every type), so such a type is not deleted.
    """
    found = []
    for model in type_models:
        for rel in model._meta.get_fields(include_hidden=True):
            if not (rel.many_to_many and rel.auto_created):
                continue
            through = rel.through
            holder_field = rel.field.m2m_field_name()
            type_field = rel.field.m2m_reverse_field_name()
            holders = through.objects.filter(**{f"{type_field}_id": ct_id}).values_list(f"{holder_field}_id", flat=True)
            for holder_id in holders:
                others = (
                    through.objects.filter(**{f"{holder_field}_id": holder_id})
                    .exclude(**{f"{type_field}_id__in": stale_ids})
                    .exists()
                )
                if not others:
                    found.append(f"{rel.related_model._meta.label} #{holder_id}")
    return found


def remove_old_content_types(apps, schema_editor):
    # Historical models (apps.get_model) describe the database as it is at this point of the migrations, whatever
    # later versions of NetBox or the plugin change in their Python code. They also know every table that points
    # at a content type, including those of other installed plugins, so the delete below sees all of them.
    db = schema_editor.connection.alias
    ContentType = apps.get_model("contenttypes", "ContentType")
    ObjectType = apps.get_model("core", "ObjectType")
    ObjectChange = apps.get_model("core", "ObjectChange")

    live = {model._meta.model_name for model in apps.get_app_config(APP_LABEL).get_models(include_auto_created=True)}
    candidates = [name for name in OLD_MODELS if name not in live]
    stale = list(ContentType.objects.using(db).filter(app_label=APP_LABEL, model__in=candidates).order_by("model"))
    if not stale:
        return
    stale_ids = [ct.pk for ct in stale]

    removed_types = removed_changes = cleared_links = 0
    for ct in stale:
        label = f"{APP_LABEL}.{ct.model}"
        emptied = _emptied_holders((ContentType, ObjectType), ct.pk, stale_ids)
        if emptied:
            _say(f"Kept the old type {label}: removing it would leave no object type on {', '.join(emptied)}.")
            continue
        try:
            # One savepoint per type: if something we do not handle protects it, its change log comes back too
            # and the other types still go.
            with transaction.atomic(using=db):
                changes, _ = ObjectChange.objects.using(db).filter(changed_object_type_id=ct.pk).delete()
                # A change of a current object can name an old object as its related object (shown on that
                # object's change log). Keep the entry and only forget the link.
                links = (
                    ObjectChange.objects.using(db)
                    .filter(related_object_type_id=ct.pk)
                    .update(related_object_type=None, related_object_id=None)
                )
                ContentType.objects.using(db).filter(pk=ct.pk).delete()
        except (ProtectedError, RestrictedError) as exc:
            blockers = sorted({obj._meta.label for obj in getattr(exc, "protected_objects", None) or ()})
            blockers = blockers or sorted({obj._meta.label for obj in getattr(exc, "restricted_objects", None) or ()})
            _say(f"Kept the old type {label}: still used by {', '.join(blockers) or 'another object'}.")
            continue
        removed_types += 1
        removed_changes += changes
        cleared_links += links

    if removed_types:
        _say(
            f"Removed {removed_types} content type(s) of 0.1.x models dropped in 0.2.0 and {removed_changes} change "
            f"log entries of their objects; cleared {cleared_links} related-object link(s) on other entries."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_windows_dhcp_plugin", "0002_sync_keys_port_and_run_index"),
    ]

    operations = [
        migrations.RunPython(remove_old_content_types, migrations.RunPython.noop),
    ]


# netbox-branching applies new migrations to each branch schema too. Content types and object types exist only in
# main (a branch schema has no copy of them), so on a branch this code would reach through to main and delete
# there. Main is cleaned by the normal `migrate`; the branch is marked as migrated without running the code. Old
# change log rows a branch schema may hold have no database constraint there, so they cannot block anything.
fake_on_branch = True
