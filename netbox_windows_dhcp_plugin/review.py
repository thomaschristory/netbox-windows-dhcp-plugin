"""
Apply or dismiss import proposal items for a reviewing user, from the UI or the REST API.

Applying writes NetBox objects as that user, so on top of "change" on the items the user needs the add, change or
delete permission on the objects each item writes. The main object's permission is checked up front (items the user
may not apply are left pending); every object written, prefixes and addresses included, is then checked as it is
saved, constraints included, and an item that breaks a permission is rolled back and marked failed.

Nothing is applied while the request works in a netbox-branching branch (see ``apply_refusal``).
"""

from django.contrib.contenttypes.models import ContentType

from .choices import ProposalItemStatusChoices
from .integrations import branching
from .sync import importer

__all__ = ("apply", "apply_refusal", "dismiss", "missing_permission", "open_items")

_PERMISSION = {"create": "add", "update": "change", "delete": "delete"}


def open_items(queryset):
    """Items still waiting for a decision (pending, or failed and worth retrying)."""
    return queryset.filter(
        status__in=(ProposalItemStatusChoices.PENDING, ProposalItemStatusChoices.FAILED)
    ).select_related("proposal__windows_server")


def missing_permission(user, item):
    """The permission the user lacks to apply this item, or None."""
    model = importer.kind_model(item.kind)
    if model is None:
        return f"cannot apply {item.kind} items"
    action = _PERMISSION[item.action]
    perm = f"{model._meta.app_label}.{action}_{model._meta.model_name}"
    if not user.has_perm(perm):
        return perm
    # Object permissions can limit which objects a user may change or delete. The content type comes from Django's
    # cache, not from a query per item.
    if (
        item.object_id is not None
        and item.object_type_id is not None
        and model is ContentType.objects.get_for_id(item.object_type_id).model_class()
        and not model.objects.restrict(user, action).filter(pk=item.object_id).exists()
    ):
        return perm
    return None


def apply_refusal():
    """
    Why nothing can be applied in this request, or None. Inside a netbox-branching branch the objects would be
    written to the branch, while the items and the record that NetBox now manages those objects are kept in main:
    the next push from main would then delete them on the Windows server. Dismissing only touches main, so it stays
    possible.
    """
    return importer.IN_BRANCH if branching.in_branch() else None


def apply(user, items):
    """
    Apply what the user may apply. Returns (applied, failed, denied permission names). While ``apply_refusal()``
    gives a reason, nothing is applied and that reason is returned in place of the permission names.
    """
    if refusal := apply_refusal():
        return 0, 0, [refusal]
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
