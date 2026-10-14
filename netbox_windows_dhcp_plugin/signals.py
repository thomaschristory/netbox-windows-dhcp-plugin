"""
Queue a push when DHCP data changes in NetBox.

Every save or delete of an object a push reads queues a push of the servers it belongs to (before and after the
change, so moving a subnet pushes both), after the transaction commits and ``push_debounce`` seconds later, so a
burst of edits becomes one push. With netbox-branching, saves inside a branch or during a merge or revert are
ignored; merges and reverts queue their own pushes (see integrations/branching.py) with the same ``servers_for``
mapping.
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


def _subnet_servers(ids):
    from netbox_dhcp.models import Subnet

    rows = Subnet.objects.filter(pk__in=ids).values_list("dhcp_server_id", "shared_network__dhcp_server_id")
    return {pk for row in rows for pk in row}


def _reservation_servers(ids):
    from netbox_dhcp.models import HostReservation

    rows = HostReservation.objects.filter(pk__in=ids).values_list(
        "dhcp_server_id", "subnet__dhcp_server_id", "subnet__shared_network__dhcp_server_id"
    )
    return {pk for row in rows for pk in row}


def _assigned_servers(type_id, object_id):
    """Servers of the object an Option is attached to (a server, a subnet or a reservation)."""
    from django.contrib.contenttypes.models import ContentType
    from netbox_dhcp.models import DHCPServer, HostReservation, Subnet

    if not type_id or not object_id:
        return set()
    try:
        model = ContentType.objects.get_for_id(type_id).model_class()
    except ContentType.DoesNotExist:
        return set()
    if model is DHCPServer:
        return {object_id}
    if model is Subnet:
        return _subnet_servers([object_id])
    if model is HostReservation:
        return _reservation_servers([object_id])
    return set()


def _option_servers(ids):
    from netbox_dhcp.models import Option

    out = set()
    for type_id, object_id in Option.objects.filter(pk__in=ids).values_list(
        "assigned_object_type_id", "assigned_object_id"
    ):
        out |= _assigned_servers(type_id, object_id)
    return out


def _definition_servers(ids):
    from netbox_dhcp.models import OptionDefinition

    return set(OptionDefinition.objects.filter(pk__in=ids).values_list("dhcp_server_id", flat=True))


def _class_servers(ids):
    from .models import WindowsClass

    return set(WindowsClass.objects.filter(pk__in=ids).values_list("dhcp_server_id", flat=True))


def _vendor_definition_servers(ids):
    from .models import WindowsVendorOptionDefinition

    return set(
        WindowsVendorOptionDefinition.objects.filter(pk__in=ids).values_list("vendor_class__dhcp_server_id", flat=True)
    )


def _pool_servers(ip_range_id):
    from netbox_dhcp.models import Pool

    return _subnet_servers(Pool.objects.filter(ip_range_id=ip_range_id).values("subnet_id"))


def _prefix_servers(prefix_id):
    from netbox_dhcp.models import Subnet

    return _subnet_servers(Subnet.objects.filter(prefix_id=prefix_id).values("pk"))


def _address_servers(**lookup):
    from netbox_dhcp.models import HostReservation

    return _reservation_servers(HostReservation.objects.filter(**lookup).values("pk"))


def _shared_network_servers(ids):
    from netbox_dhcp.models import SharedNetwork

    return set(SharedNetwork.objects.filter(pk__in=ids).values_list("dhcp_server_id", flat=True))


def _shared_network_subnet_servers(shared_network_id):
    from netbox_dhcp.models import Subnet

    return _subnet_servers(Subnet.objects.filter(shared_network_id=shared_network_id).values("pk"))


# Model label -> function(primary key, field values) returning DHCPServer primary keys. Field values are what the
# change log records: foreign keys as primary keys, under the field name.
_RESOLVERS = {
    "netbox_dhcp.subnet": lambda pk, d: {d.get("dhcp_server")} | _shared_network_servers([d.get("shared_network")]),
    "netbox_dhcp.pool": lambda pk, d: _subnet_servers([d.get("subnet")]),
    "netbox_dhcp.hostreservation": lambda pk, d: {d.get("dhcp_server")} | _subnet_servers([d.get("subnet")]),
    "netbox_dhcp.option": lambda pk, d: _assigned_servers(d.get("assigned_object_type"), d.get("assigned_object_id")),
    "netbox_dhcp.optiondefinition": lambda pk, d: {d.get("dhcp_server")},
    # A shared network's server owns the subnets in it (see netbox_state.server_subnets).
    "netbox_dhcp.sharednetwork": lambda pk, d: {d.get("dhcp_server")} | _shared_network_subnet_servers(pk),
    f"{PLUGIN}.windowsscope": lambda pk, d: _subnet_servers([d.get("subnet")]),
    f"{PLUGIN}.windowsreservation": lambda pk, d: _reservation_servers([d.get("host_reservation")]),
    f"{PLUGIN}.exclusionrange": lambda pk, d: _subnet_servers([d.get("subnet")]),
    f"{PLUGIN}.windowsclass": lambda pk, d: {d.get("dhcp_server")},
    f"{PLUGIN}.windowsoptiondefinition": lambda pk, d: _definition_servers([d.get("option_definition")]),
    f"{PLUGIN}.windowsoptionqualifier": lambda pk, d: _option_servers([d.get("option")]),
    f"{PLUGIN}.windowspolicy": lambda pk, d: {d.get("dhcp_server")},
    f"{PLUGIN}.windowsvendoroptiondefinition": lambda pk, d: _class_servers([d.get("vendor_class")]),
    f"{PLUGIN}.windowsvendoroption": lambda pk, d: (
        _assigned_servers(d.get("assigned_object_type"), d.get("assigned_object_id"))
        | _vendor_definition_servers([d.get("definition")])
    ),
    f"{PLUGIN}.macfilter": lambda pk, d: {d.get("dhcp_server")},
    f"{PLUGIN}.macfilterlists": lambda pk, d: {d.get("dhcp_server")},
    # The primary pushes the relationship; the partner's push decides which scopes it leaves to the primary.
    f"{PLUGIN}.failoverrelationship": lambda pk, d: {d.get("primary_server"), d.get("partner_server")},
    # Core objects netbox-plugin-dhcp points at: the scope range, the prefix, a reservation's address and MAC.
    "ipam.iprange": lambda pk, d: _pool_servers(pk),
    "ipam.prefix": lambda pk, d: _prefix_servers(pk),
    # An IPv4 reservation points at its address; a DHCPv6 one lists its addresses.
    "ipam.ipaddress": lambda pk, d: _address_servers(ipv4_address_id=pk) | _address_servers(ipv6_addresses=pk),
    "dcim.macaddress": lambda pk, d: _address_servers(hw_address_id=pk),
}

WATCHED = frozenset(_RESOLVERS)


def servers_for(label, pk, data):
    """Primary keys of the DHCPServers a change to this object affects."""
    resolver = _RESOLVERS.get(label)
    if resolver is None or not data:
        return set()
    return {s for s in resolver(pk, data) if s}


def servers_for_change(change):
    """The same, for an ObjectChange: before and after, so moving a subnet to another server pushes both."""
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


def queue_pushes(dhcp_server_ids, user=None, delay=0, branch="", notify_always=True):
    """Queue one push per Windows server (with sync enabled) of these DHCP servers. Returns the jobs queued."""
    from .jobs import enqueue_push
    from .models import WindowsServer

    jobs = []
    for server in WindowsServer.objects.filter(dhcp_server_id__in=dhcp_server_ids, sync_enabled=True):
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

    by_user = {}
    for server_id, user in pending.items():
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
        _local.pending.setdefault(server_id, user)
    # One callback per change; the first to run takes everything pending. robust: failing to queue a push must not
    # turn a save that already committed into an error page.
    transaction.on_commit(_flush, robust=True)


def connect():
    post_save.connect(_changed, dispatch_uid=f"{PLUGIN}.push.save")
    # Before the delete, while the rows the object points at (and that point at it) are still there.
    pre_delete.connect(_changed, dispatch_uid=f"{PLUGIN}.push.delete")
    request_started.connect(_forget_pending, dispatch_uid=f"{PLUGIN}.push.request")
