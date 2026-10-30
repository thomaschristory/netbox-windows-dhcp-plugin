"""
Queue a push when DHCP data changes in NetBox.

Every save or delete of an object a push reads queues a push of the servers it belongs to (before and after the
change, so moving a reservation between servers' scopes pushes both), after the transaction commits and
``push_debounce`` seconds later, so a burst of edits becomes one push. With netbox-branching, saves inside a branch
or during a merge or revert are ignored; merges and reverts queue their own pushes (see integrations/branching.py)
with the same ``servers_for`` mapping.
"""

import threading
from contextlib import contextmanager

from django.core.signals import request_started
from django.db import transaction
from django.db.models.signals import post_save, pre_delete

__all__ = ("connect", "push_suppressed", "queue_pushes", "servers_for", "servers_for_change")

PLUGIN = "netbox_windows_dhcp_plugin"

_local = threading.local()


@contextmanager
def push_suppressed():
    """
    Do not queue pushes for saves made inside this block. Used when applying an import: the objects then match
    Windows, so pushing them back would be a round trip for nothing.
    """
    previous = getattr(_local, "suppressed", False)
    _local.suppressed = True
    try:
        yield
    finally:
        _local.suppressed = previous


# -- which servers does a change affect? ---------------------------------------------------------------------------


def _scope_servers(**lookup):
    from .models import Scope

    return set(Scope.objects.filter(**lookup).values_list("server_id", flat=True))


def _reservation_servers(**lookup):
    from .models import Reservation

    return set(Reservation.objects.filter(**lookup).values_list("scope__server_id", flat=True))


def _server(pk, d):
    return {d.get("server")}


# Model label -> function(primary key, field values) returning WindowsServer primary keys. Field values are what the
# change log records: foreign keys as primary keys, under the field name.
_RESOLVERS = {
    f"{PLUGIN}.scope": _server,
    f"{PLUGIN}.superscope": _server,
    f"{PLUGIN}.reservation": lambda pk, d: _scope_servers(pk=d.get("scope")),
    f"{PLUGIN}.exclusionrange": lambda pk, d: _scope_servers(pk=d.get("scope")),
    f"{PLUGIN}.windowsclass": _server,
    f"{PLUGIN}.optiondefinition": _server,
    f"{PLUGIN}.optionvalue": _server,
    f"{PLUGIN}.windowspolicy": _server,
    f"{PLUGIN}.macfilter": _server,
    f"{PLUGIN}.macfilterlists": _server,
    # The primary pushes the relationship; the partner's push decides which scopes it leaves to the primary.
    f"{PLUGIN}.failoverrelationship": lambda pk, d: {d.get("primary_server"), d.get("partner_server")},
    # Core objects scopes and reservations point at: a scope's prefix and range, a reservation's address and MAC.
    # These records hold no server, so the scopes and reservations using the object are looked up.
    "ipam.prefix": lambda pk, d: _scope_servers(prefix_id=pk),
    "ipam.iprange": lambda pk, d: _scope_servers(ip_range_id=pk),
    "ipam.ipaddress": lambda pk, d: _reservation_servers(ip_address_id=pk),
    "dcim.macaddress": lambda pk, d: _reservation_servers(mac_address_id=pk),
}

WATCHED = frozenset(_RESOLVERS)


def servers_for(label, pk, data):
    """Primary keys of the WindowsServers a change to this object affects."""
    resolver = _RESOLVERS.get(label)
    if resolver is None or not data:
        return set()
    return {s for s in resolver(pk, data) if s}


def servers_for_change(change):
    """The same, for an ObjectChange: before and after, so moving a reservation to another scope pushes both."""
    ct = change.changed_object_type
    label = f"{ct.app_label}.{ct.model}"
    if label not in WATCHED:
        return set()
    out = set()
    for data in (change.prechange_data, change.postchange_data):
        out |= servers_for(label, change.changed_object_id, data)
    return out


def _data(instance):
    return {f.name: f.value_from_object(instance) for f in instance._meta.concrete_fields}


# -- queueing ------------------------------------------------------------------------------------------------------


def queue_pushes(server_ids, user=None, delay=0, branch="", notify_always=True):
    """Queue one push per Windows server (with sync enabled) of these primary keys. Returns the jobs queued."""
    from .jobs import enqueue_push
    from .models import WindowsServer

    jobs = []
    for server in WindowsServer.objects.filter(pk__in=server_ids, sync_enabled=True):
        job = enqueue_push(server, user, delay=delay, branch=branch, notify_always=notify_always)
        if job is not None:
            jobs.append(job)
    return jobs


def _flush():
    pending = getattr(_local, "pending", None)
    _local.pending = None
    if not pending:
        return
    from .conf import get_setting
    from .integrations.branching import request_applied_by_branch

    # AppliedChange is recorded after the model save; only after commit can its request be identified as a replay.
    replayed = {
        request_id: request_applied_by_branch(request_id) for request_id in {rid for _, rid in pending.values()}
    }
    by_user = {}
    for server_id, (user, request_id) in pending.items():
        if replayed[request_id]:
            continue
        by_user.setdefault(user, set()).add(server_id)
    for user, ids in by_user.items():
        queue_pushes(ids, user, delay=get_setting("push_debounce"), notify_always=False)


def _forget_pending(**kwargs):
    # Servers left pending by a rolled-back transaction belong to an earlier request: do not push them under this one.
    _local.pending = None


def _changed(sender, instance, **kwargs):
    # Connected for every model, so leave quickly for the ones a push does not read.
    label = sender._meta.label_lower
    if label not in WATCHED or getattr(_local, "suppressed", False):
        return
    from .integrations.branching import save_belongs_to_branching

    if save_belongs_to_branching():
        return
    ids = servers_for(label, instance.pk, _data(instance))
    # NetBox snapshots an object before editing it: the servers it belonged to before count too.
    snapshot = getattr(instance, "_prechange_snapshot", None)
    if snapshot:
        ids |= servers_for(label, instance.pk, snapshot)
    if not ids:
        return
    from netbox.context import current_request

    request = current_request.get()
    user = getattr(request, "user", None) if request is not None else None
    if user is not None and not user.is_authenticated:
        user = None
    if getattr(_local, "pending", None) is None:
        _local.pending = {}
    for server_id in ids:
        _local.pending.setdefault(server_id, (user, getattr(request, "id", None)))
    # One callback per change; the first to run takes everything pending. robust: failing to queue a push must not
    # turn a save that already committed into an error page.
    transaction.on_commit(_flush, robust=True)


def connect():
    post_save.connect(_changed, dispatch_uid=f"{PLUGIN}.push.save")
    # Before the delete, while the rows the object points at (and that point at it) are still there.
    pre_delete.connect(_changed, dispatch_uid=f"{PLUGIN}.push.delete")
    request_started.connect(_forget_pending, dispatch_uid=f"{PLUGIN}.push.request")
