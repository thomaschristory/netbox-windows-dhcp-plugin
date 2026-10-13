"""
Apply or dismiss import proposal items for a reviewing user, from the UI or the REST API.

Applying writes NetBox objects as that user, so on top of "change" on the items the user needs the add, change or
delete permission on the objects each item writes. The main object's permission is checked up front (items the user
may not apply are left pending); every object written, prefixes and addresses included, is then checked as it is
saved, constraints included, and an item that breaks a permission is rolled back and marked failed.
"""

from .choices import ProposalItemStatusChoices
from .sync import importer

__all__ = ("apply", "dismiss", "missing_permission", "open_items")

_PERMISSION = {"create": "add", "update": "change", "delete": "delete"}


def open_items(queryset):
    """Items still waiting for a decision (pending, or failed and worth retrying)."""
    return queryset.filter(
        status__in=(ProposalItemStatusChoices.PENDING, ProposalItemStatusChoices.FAILED)
    ).select_related("proposal__windows_server__dhcp_server")


def missing_permission(user, item):
    """The permission the user lacks to apply this item, or None."""
    model = importer.kind_model(item.kind, item.after or item.before)
    if model is None:
        return f"cannot apply {item.kind} items"
    action = _PERMISSION[item.action]
    perm = f"{model._meta.app_label}.{action}_{model._meta.model_name}"
    if not user.has_perm(perm):
        return perm
    # Object permissions can limit which objects a user may change or delete.
    if (
        item.object_id is not None
        and model is item.object_type.model_class()
        and not model.objects.restrict(user, action).filter(pk=item.object_id).exists()
    ):
        return perm
    return None


def apply(user, items):
    """Apply what the user may apply. Returns (applied, failed, denied permission names)."""
    allowed, denied = [], set()
    for item in items:
        missing = missing_permission(user, item)
        if missing:
            denied.add(missing)
        else:
            allowed.append(item)
    applied, failed = importer.apply_items(allowed, user=user)
    return applied, failed, sorted(denied)


def dismiss(user, items):
    return importer.dismiss_items(list(items))
