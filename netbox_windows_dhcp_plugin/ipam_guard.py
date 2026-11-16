"""
Edits of the core NetBox objects that scopes and reservations point at (issue #102).

A scope reads its prefix and IP range, a reservation its IP address and MAC address. Changing those values in IPAM
changes what the next push writes to Windows, so such an edit is a DHCP change: it needs the plugin's permission on
the scope or reservation that uses the object. This module knows which fields a push reads and which plugin objects
depend on an object; the refusal itself is `check_edit`, a pre_save receiver.
"""

import hmac
import os
import uuid

from netaddr import EUI, IPNetwork

PLUGIN = "netbox_windows_dhcp_plugin"

# Model label -> the fields a push reads from it (see sync/netbox_state.py). Nothing else on these objects (status,
# VRF, tenant, description, tags, DNS name, interface) ever reaches Windows.
WATCHED = {
    "ipam.prefix": ("prefix",),
    "ipam.iprange": ("start_address", "end_address"),
    "ipam.ipaddress": ("address",),
    "dcim.macaddress": ("mac_address",),
}


def push_value(label, field, value):
    """The part of a field's value that a push sends, so that edits Windows never sees compare equal."""
    if value is None or value == "":
        return None
    if label == "dcim.macaddress":
        # Compare the 48-bit number: the stored value and the edited one can carry different display formats.
        return EUI(str(value)).value
    network = IPNetwork(value)
    if label == "ipam.prefix":
        # A prefix is the scope ID and subnet mask: network and length both count.
        return str(network.cidr)
    # Ranges and addresses: Windows gets the address alone, not the mask written next to it in NetBox.
    return str(network.ip)


def changed_fields(instance, update_fields=None, using=None):
    """
    The watched fields whose pushed value differs from the stored row; () for new or unwatched objects. With
    ``update_fields`` (a save of some fields only), the other fields are not written, so they cannot change. ``using``
    is the database the save writes to (the stored row is read there, not wherever the router would read).
    """
    label = instance._meta.label_lower
    fields = WATCHED.get(label)
    if fields and update_fields is not None:
        fields = tuple(f for f in fields if f in update_fields)
    # Not instance._state.adding: an instance built with the primary key of an existing row saves as an UPDATE, and
    # the stored row below is what tells a new object from an edit.
    if not fields or instance.pk is None:
        return ()
    stored = type(instance).objects.using(using).filter(pk=instance.pk).values(*fields).first()
    if stored is None:
        return ()
    # Kept for recheck_edit, which may have to put these values back.
    instance._nbwd_stored = stored
    return tuple(f for f in fields if push_value(label, f, getattr(instance, f)) != push_value(label, f, stored[f]))


def dependents(instance, using=None):
    """
    The plugin objects whose Windows state depends on this core object, as [(permission codename, queryset)], with
    only the non-empty querysets. ``using``: the database the core object is saved to, where its dependents live.
    """
    from .models import Reservation, Scope

    scopes = Scope.objects.using(using).select_related("server")
    reservations = Reservation.objects.using(using).select_related("scope__server", "ip_address")
    label = instance._meta.label_lower
    if label == "ipam.prefix":
        found = [("change_scope", scopes.filter(prefix_id=instance.pk))]
    elif label == "ipam.iprange":
        found = [("change_scope", scopes.filter(ip_range_id=instance.pk))]
    elif label == "ipam.ipaddress":
        found = [("change_reservation", reservations.filter(ip_address_id=instance.pk))]
    elif label == "dcim.macaddress":
        found = [("change_reservation", reservations.filter(mac_address_id=instance.pk))]
    else:
        return []
    return [(codename, queryset) for codename, queryset in found if queryset.exists()]


# -- the import's own changes ---------------------------------------------------------------------------------------
#
# An import into a branch writes what Windows already holds. When that branch is merged, its changes replay on main
# under their original request ID (netbox-branching's iterative strategy; squash uses the last change's), and they
# need no DHCP permission: they do not change what Windows has. The import's request IDs carry a keyed hash of
# NetBox's SECRET_KEY, so they can be recognised without storing them and cannot be made up. An edit someone adds to
# an import branch has its own request ID and is checked at merge like any other, and so is every change of an object
# that such an edit touched (squash collapses them under one request ID).

_IMPORT_SALT = "netbox_windows_dhcp_plugin.import-request"


def _tag(nonce):
    from django.utils.crypto import salted_hmac

    return salted_hmac(_IMPORT_SALT, nonce).digest()[:8]


def import_request_id():
    """A request ID for the change log entries an import writes into a branch."""
    nonce = os.urandom(8)
    return uuid.UUID(bytes=nonce + _tag(nonce))


def is_import_request(value):
    """Whether this request ID was made by import_request_id()."""
    try:
        raw = (value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))).bytes
    except (TypeError, ValueError, AttributeError):
        return False
    return hmac.compare_digest(raw[8:], _tag(raw[:8]))


def _alters_watched(label, prechange, postchange):
    """
    Whether a change log entry (its pre- and postchange data, None for a create or delete) writes a watched field.
    The stored values are compared as they are, not by what a push sends: netbox-branching replays a field whose stored
    value differs, mask and all, so a change of the mask alone also writes the address it carries. Counting it as
    the import's would let a mask edit of a range's end replay a stale end address under the import's exemption.
    """
    prechange, postchange = prechange or {}, postchange or {}
    return any(prechange.get(field) != postchange.get(field) for field in WATCHED[label])


def import_only(label, changes):
    """
    Whether the branch changes of one object, [(request ID, prechange data, postchange data)], come from an import as
    far as Windows is concerned: every change that writes a watched field is the import's (and there is one). Changes
    that leave the watched fields alone, such as main's description edit brought in by a sync (#115) or a hand edit of
    the tags, cannot pass anything off as the import's, so they do not count. A hand edit of a watched field does,
    even of the mask alone: squash replays all of an object's changes under one request ID, which may be the
    import's.
    """
    watched = [
        request_id for request_id, prechange, postchange in changes if _alters_watched(label, prechange, postchange)
    ]
    return bool(watched) and all(is_import_request(r) for r in watched)


# The refusal names at most this many scopes or reservations, then counts the rest.
MESSAGE_LIMIT = 5

_KIND = {"change_scope": "scope", "change_reservation": "reservation"}


def _describe(codename, obj):
    if codename == "change_scope":
        return f"{obj.server.name}/{obj.scope_id}"
    return f"{obj.scope.server.name}/{obj.scope.scope_id}/{obj.ip_address.address.ip if obj.ip_address_id else obj}"


def _refusal(fields, missing, user, branch=None):
    """
    The message: each scope or reservation the user may not change, named only when the user may view it (the
    message must not reveal objects hidden from them, not even how many there are).
    """
    names = ", ".join(fields)
    visible, hidden = [], []
    for codename, obj, viewable in missing:
        if viewable:
            visible.append((codename, obj))
        elif codename not in hidden:
            hidden.append(codename)
    lines = []
    for codename, obj in visible[:MESSAGE_LIMIT]:
        kind = _KIND[codename]
        lines.append(
            f'Changing {names} would change {kind} {_describe(codename, obj)} on Windows. You need the "change {kind}" '
            "permission on it."
        )
    if len(visible) > MESSAGE_LIMIT:
        lines.append(f"And {len(visible) - MESSAGE_LIMIT} more.")
    for codename in hidden:
        # Not how many: the number alone would tell the user how many hidden objects use this one.
        kind = _KIND[codename]
        lines.append(
            f"Changing {names} would change one or more {kind}s on Windows that you may not view. You need the "
            f'"change {kind}" permission on them.'
        )
    if branch:
        # Shown in the failed merge (or revert) job: say which branch and whose permission was missing.
        lines.insert(0, f"Branch {branch} cannot be merged or reverted by {user}:")
    return " ".join(lines)


def _no_user_refusal(fields, branch, codenames):
    # Names no scope or reservation, nor how many: there is no user whose view permission would allow it.
    kinds = [_KIND[codename] for codename in codenames]
    permissions = " and ".join(f'"change {kind}"' for kind in kinds)
    return (
        f"Branch {branch} cannot be merged or reverted without a user: changing {', '.join(fields)} would change "
        f"one or more {' and '.join(kind + 's' for kind in kinds)} on Windows. Merge or revert it as a user with the "
        f"{permissions} permission on them."
    )


def _request_user():
    from netbox.context import current_request

    request = current_request.get()
    user = getattr(request, "user", None) if request is not None else None
    return user if user is not None and user.is_authenticated else None


def _missing(instance, user, using=None):
    """[(codename, object, viewable)] for every dependent scope or reservation the user may not change."""
    missing = []
    for codename, queryset in dependents(instance, using):
        allowed = set(queryset.restrict(user, "change").values_list("pk", flat=True))
        refused = [obj for obj in queryset if obj.pk not in allowed]
        if refused:
            viewable = set(queryset.restrict(user, "view").values_list("pk", flat=True))
            missing.extend((codename, obj, obj.pk in viewable) for obj in refused)
    return missing


def _merge_dependents(instance, using, connection):
    """
    The dependents a merge must check: main's, plus the branch's (``connection``, None for a revert or an ordinary
    save). The squash strategy replays every update before any create, so a reservation that the branch puts on an
    address main already has is not on main yet when the address edit is replayed.
    """
    found = dependents(instance, using)
    if connection:
        codenames = {codename for codename, _ in found}
        found += [(c, q) for c, q in dependents(instance, connection) if c not in codenames]
    return found


def _merge_missing(instance, user, using, connection):
    """_missing() for main, plus the branch's dependents (see _merge_dependents) not already listed."""
    missing = _missing(instance, user, using)
    if connection:
        seen = {(codename, obj.pk) for codename, obj, _ in missing}
        missing += [m for m in _missing(instance, user, connection) if (m[0], m[1].pk) not in seen]
    return missing


def check_edit(sender, instance, raw=False, **kwargs):
    """
    pre_save receiver for the four watched models: refuse (AbortRequest) an edit that changes what a push writes to
    Windows when the user may not change every scope or reservation affected. NetBox's views and REST API turn the
    exception into an error message (or HTTP 400) and roll the request back.
    """
    instance._nbwd_guard_user = None
    # The database the save writes to. A merge writes to main even while a branch is active in its context, and the
    # router would read that branch's rows instead.
    using = kwargs.get("using")
    # Reset first: a mark left by an earlier save of this instance must not decide this one.
    instance._nbwd_push_fields = None
    if raw:
        # Fixture loading (not an edit by anyone), or a merge replaying a create: netbox-branching saves new objects
        # raw.
        _check_replayed_create(instance, using)
        return
    fields = changed_fields(instance, kwargs.get("update_fields"), using)
    # Read by signals._changed after the save: no push for edits Windows never sees.
    instance._nbwd_push_fields = fields
    if not fields:
        return
    from .integrations.branching import applying_branch
    from .signals import pushes_suppressed

    # Imports write what Windows already holds.
    if pushes_suppressed():
        return
    if applying := applying_branch(instance):
        # A merge or revert replays the branch's changes on main as their authors (iterative strategy) or as the
        # merging user (squash): check the merging or reverting user, against main's scopes and reservations, which
        # may differ from what the branch held when the edit was made there. An import's own changes are not checked:
        # only when every change the branch holds for this object that alters a watched field is the import's, as
        # squash replays an object's changes under its last change's request ID, which must not pass a hand edit off
        # as the import's (see import_only).
        # Not for a revert: undoing the import puts back values Windows no longer holds, and the next push writes
        # them there.
        branch, user, changes = applying.branch, applying.user, applying.changes
        if not applying.reverting and import_only(instance._meta.label_lower, changes):
            return
        instance._nbwd_branch = branch
        # A revert puts main back as it was before the merge: main's dependents are the ones it affects.
        branch_connection = None if applying.reverting else applying.connection
        if user is None or not user.is_authenticated:
            # No merging user (a merge job whose user was deleted, or code calling merge(user=None)): nobody whose
            # permission could be checked, so a change Windows would see is refused. Not the user the replay runs as:
            # squash runs it as the (missing) merging user, iterative as each change's author, who may have made the
            # edit long ago. Someone with the permission can run the merge again.
            if found := _merge_dependents(instance, using, branch_connection):
                from utilities.exceptions import AbortRequest

                raise AbortRequest(_no_user_refusal(fields, branch, [codename for codename, _ in found]))
            return
    else:
        instance._nbwd_branch = None
        branch_connection = None
        user = _request_user()
    # nbshell and management commands have no user: nothing to check (NetBox writes no change log entry either).
    if user is None or not user.is_authenticated:
        return
    from utilities.exceptions import AbortRequest

    if missing := _merge_missing(instance, user, using, branch_connection):
        raise AbortRequest(_refusal(fields, missing, user, instance._nbwd_branch))
    # Checked again after the save (recheck_edit): a permission constraint can depend on the edited value itself, for
    # example reservations allowed in part of a subnet only.
    instance._nbwd_guard_user = user
    instance._nbwd_using = using


def _hand_updates(label, changes, instance):
    """
    The watched fields that the branch's non-import updates of a new object changed, as a push sees them.
    ``changes`` are oldest first. A field counts when a hand update wrote it and the value the object is created with
    on main differs from the baseline: the value the import last wrote to that field (what Windows holds), or, with
    no import write, the value the object was created with (creating alone is not checked). A mask edit alone, or an
    edit undone later, changes nothing Windows receives.
    """
    edited, baseline = set(), {}
    for request_id, prechange, postchange in changes:
        if postchange is None:
            continue
        if prechange is None or is_import_request(request_id):
            # A create sets every field; an import update sets the ones it changed.
            baseline.update(
                (f, postchange.get(f))
                for f in WATCHED[label]
                if prechange is None or prechange.get(f) != postchange.get(f)
            )
            continue
        edited.update(f for f in WATCHED[label] if prechange.get(f) != postchange.get(f))
    return tuple(
        f
        for f in WATCHED[label]
        if f in edited
        and (f not in baseline or push_value(label, f, baseline[f]) != push_value(label, f, getattr(instance, f)))
    )


def _check_replayed_create(instance, using):
    """
    A merge that creates an object on main: squash folds the object's create and later updates into one create, so
    a hand edit of the address made in the branch after the object was created there is not replayed as an update,
    which the iterative strategy would check. Check it here, as the merging user, against the scopes and reservations
    that use the object in the branch: they do not exist on main yet (squash creates them after the object). Creating
    the object alone is not checked, as everywhere else.
    """
    label = instance._meta.label_lower
    if label not in WATCHED or instance.pk is None:
        return
    from .integrations.branching import applying_branch

    applying = applying_branch(instance)
    if applying is None or applying.reverting:
        return
    if not (fields := _hand_updates(label, applying.changes, instance)):
        return
    if type(instance).objects.using(using).filter(pk=instance.pk).exists():
        # The row is already on main, so this is not a create (netbox-branching replays updates through a normal save,
        # which the usual path of check_edit checks).
        return
    from utilities.exceptions import AbortRequest

    user, branch = applying.user, applying.branch
    if user is None or not user.is_authenticated:
        if found := dependents(instance, applying.connection):
            raise AbortRequest(_no_user_refusal(fields, branch, [codename for codename, _ in found]))
        return
    if missing := _missing(instance, user, applying.connection):
        raise AbortRequest(_refusal(fields, missing, user, branch))


def recheck_edit(sender, instance, raw=False, **kwargs):
    """
    post_save receiver: the same check against the saved values. Raising here rolls the save back, as NetBox's views,
    REST API and scripts save inside a transaction (NetBox checks its own object permissions the same way).
    """
    user = getattr(instance, "_nbwd_guard_user", None)
    if raw or user is None:
        return
    instance._nbwd_guard_user = None
    using = getattr(instance, "_nbwd_using", None)
    from utilities.exceptions import AbortRequest

    if missing := _missing(instance, user, using):
        # The row is already written. Put the stored values back (without signals) before raising: outside a
        # transaction the exception rolls nothing back, and inside one a caller catching it (a script's try/except)
        # would commit the edit.
        stored = {f: v for f, v in instance._nbwd_stored.items() if f in instance._nbwd_push_fields}
        type(instance).objects.using(using).filter(pk=instance.pk).update(**stored)
        raise AbortRequest(_refusal(instance._nbwd_push_fields, missing, user, getattr(instance, "_nbwd_branch", None)))


def connect():
    from dcim.models import MACAddress
    from django.db.models.signals import post_save, pre_save
    from ipam.models import IPAddress, IPRange, Prefix

    for model in (Prefix, IPRange, IPAddress, MACAddress):
        label = model._meta.label_lower
        pre_save.connect(check_edit, sender=model, dispatch_uid=f"{PLUGIN}.guard.{label}")
        post_save.connect(recheck_edit, sender=model, dispatch_uid=f"{PLUGIN}.guard.post.{label}")
