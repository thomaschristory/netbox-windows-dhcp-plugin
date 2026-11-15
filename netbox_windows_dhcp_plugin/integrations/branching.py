"""
Optional integration with netbox-branching and netbox-change-control.

Everything that imports those plugins lives here, so the plugin keeps working when they are not installed.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar

from django.apps import apps

log = logging.getLogger(__name__)

# Bookkeeping models that must always live on main (see models/sync.py).
NON_BRANCHABLE = {"windowsserver", "syncstate", "syncrun", "pushtrigger", "importproposal", "importproposalitem"}


def branching_installed():
    return apps.is_installed("netbox_branching")


def save_belongs_to_branching():
    """
    True when a save happens inside a branch (edits, imports, syncs from main). Merge/revert writes target main:
    their change-log provenance is checked after commit, not by whether any branch happens to be busy.
    """
    if not branching_installed():
        return False
    try:
        from netbox_branching.contextvars import active_branch
    except ImportError:
        # Fail safe, unlike active_branch_name(): when the branch cannot be read, queue no push rather than push
        # what may be a save inside a branch.
        return True
    return active_branch.get() is not None


def request_applied_by_branch(request_id):
    """After commit, whether this request's writes were replayed by a merge/revert that queues its own push."""
    if request_id is None or not branching_installed():
        return False
    from netbox_branching.models import AppliedChange

    return AppliedChange.objects.filter(change__request_id=request_id).exists()


def in_branch():
    """True while the request works inside a netbox-branching branch (not main)."""
    return active_branch_name() is not None


def active_branch_name():
    """The name of the branch the request works in, or None on main."""
    if not branching_installed():
        return None
    try:
        from netbox_branching.contextvars import active_branch
    except ImportError:
        return None
    branch = active_branch.get()
    return branch.name if branch is not None else None


def change_control_installed():
    return apps.is_installed("netbox_change_control")


def _resolver(model):
    if model._meta.app_label == "netbox_windows_dhcp_plugin" and model._meta.model_name in NON_BRANCHABLE:
        return False
    return None


def register():
    if not branching_installed():
        return
    _connect_push_signals()
    try:
        from netbox_branching.utilities import register_branching_resolver
    except ImportError:
        # A netbox-branching release without this hook: start anyway rather than take NetBox down.
        log.warning("netbox-branching has no register_branching_resolver; Windows DHCP bookkeeping will be branched")
        return
    register_branching_resolver(_resolver)


# -- pushes after a merge or a revert ------------------------------------------------------------------------------
#
# netbox-branching sends post_merge from the worker running the merge, after its transaction committed (an aborted
# merge sends nothing, and neither does a merge with no changes). Settings and credential profiles must therefore be
# available to netbox-worker too.

# Servers touched by a branch being reverted, captured in pre_revert: once reverted, its changes no longer count as
# merged and the objects it created are gone.
_reverting = {}


def _watched_content_types():
    """Content types of the models a push reads (signals.WATCHED). A label with no content type simply drops out."""
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Q

    from ..signals import WATCHED

    wanted = Q()
    for label in WATCHED:
        app_label, model = label.split(".")
        wanted |= Q(app_label=app_label, model=model)
    return ContentType.objects.filter(wanted)


def _affected_servers(branch):
    from ..models import WindowsServer
    from ..signals import servers_for_change

    # Only servers with sync enabled get a push (signals.queue_pushes), so once they are all found the rest of the
    # branch cannot add one: an import branch with thousands of reservations of one server stops at its first change.
    pushable = set(WindowsServer.objects.filter(sync_enabled=True).values_list("pk", flat=True))
    ids = set()
    if not pushable:
        return ids
    # This runs for every merge and revert in NetBox, DHCP related or not: let the database drop the changes a push
    # does not read, and do not keep the rest (with their before and after data) in memory.
    changes = branch.get_merged_changes().filter(changed_object_type__in=_watched_content_types())
    for change in changes.select_related("changed_object_type").iterator():
        ids |= servers_for_change(change)
        if ids >= pushable:
            break
    return ids


def _queue(ids, user, branch, source):
    from django.db import transaction

    from ..signals import queue_pushes

    if not ids:
        return
    name = branch.name
    triggers = [{"source": source, "user": user, "branch": name}]
    transaction.on_commit(lambda: queue_pushes(ids, user, branch=name, triggers=triggers), robust=True)


def _on_merge(sender, branch, user=None, **kwargs):
    clear_applying()
    try:
        _queue(_affected_servers(branch), user, branch, "merge")
    except Exception:
        # Never break a merge that already happened; the push can be run by hand.
        log.exception("could not queue Windows DHCP pushes after merging %s", branch)


# The branch whose merge or revert is replaying changes in this context, with its name and the user merging or
# reverting it. netbox-branching sends no signal when a merge fails, so applying_branch() also checks that the branch
# is still merging or reverting, and that the save runs under one of the branch's request IDs, before trusting it.
_applying = ContextVar("netbox_windows_dhcp_plugin_applying_branch", default=None)


def _on_pre_apply(sender, branch, user=None, **kwargs):
    _applying.set((branch.pk, branch.name, user))


def clear_applying():
    """Forget the merge or revert mark: at its end, and when a new request starts (a failed one sends no signal)."""
    _applying.set(None)


def applying_branch(instance):
    """
    While netbox-branching merges or reverts a branch in this context and this save is one of its replayed changes:
    (branch name, merging or reverting user, request IDs of every change the branch holds for this object), else None.

    The mark alone is not enough: a failed, empty or dry-run merge sends no closing signal, so the mark can outlive it
    (until the next request or merge in this process). A replayed save runs under the request ID of one of the
    branch's own changes to this object (both strategies set it), which an ordinary save never has.
    """
    mark = _applying.get()
    if mark is None or not branching_installed():
        return None
    from netbox.context import current_request

    request_id = getattr(current_request.get(), "id", None)
    if request_id is None:
        return None
    from core.models import ObjectType
    from netbox_branching.choices import BranchStatusChoices
    from netbox_branching.models import Branch

    pk, name, user = mark
    branch = Branch.objects.filter(pk=pk).first()
    if branch is None:
        return None
    if branch.status not in (BranchStatusChoices.MERGING, BranchStatusChoices.REVERTING):
        return None
    # What Branch.merge() and Branch.revert() replay: every change in the branch schema (get_changes()). Not main's
    # applied changes: a branch change that was a no-op on main at merge left none there, yet its undo is replayed.
    changes = branch.get_changes()
    object_type = ObjectType.objects.get_for_model(instance)
    request_ids = set(
        changes.filter(changed_object_type=object_type, changed_object_id=instance.pk).values_list(
            "request_id", flat=True
        )
    )
    # request.id may be a UUID or its string form.
    if str(request_id) not in {str(r) for r in request_ids}:
        return None
    return name, user, request_ids


def _on_pre_revert(sender, branch, user=None, **kwargs):
    _on_pre_apply(sender, branch, user)
    # A revert that fails, or has nothing to revert, sends no post_revert: do not let those entries pile up.
    if len(_reverting) > 50:
        _reverting.clear()
    try:
        _reverting[branch.pk] = _affected_servers(branch)
    except Exception:
        log.exception("could not work out the Windows DHCP servers touched by %s", branch)


def _on_revert(sender, branch, user=None, **kwargs):
    clear_applying()
    try:
        _queue(_reverting.pop(branch.pk, set()), user, branch, "revert")
    except Exception:
        log.exception("could not queue Windows DHCP pushes after reverting %s", branch)


def _connect_push_signals():
    try:
        from netbox_branching.signals import post_merge, post_revert, pre_merge, pre_revert
    except ImportError:
        log.warning("netbox-branching has no merge signals; Windows DHCP will not push after merges")
        return
    pre_merge.connect(_on_pre_apply, dispatch_uid="netbox_windows_dhcp_plugin.guard.pre_merge")
    post_merge.connect(_on_merge, dispatch_uid="netbox_windows_dhcp_plugin.push.merge")
    pre_revert.connect(_on_pre_revert, dispatch_uid="netbox_windows_dhcp_plugin.push.pre_revert")
    post_revert.connect(_on_revert, dispatch_uid="netbox_windows_dhcp_plugin.push.revert")


# -- import branches -----------------------------------------------------------------------------------------------
#
# These run in the import job (an RQ worker). They are only called after branching_installed() said yes.

# Branch names are limited to 100 characters; the timestamp suffix is 15 of them.
BRANCH_NAME_LENGTH = 100
_TIMESTAMP = r"\d{8}-\d{6}"


class BranchError(Exception):
    """An import branch could not be created or written. The message is meant for the job log."""


def job_kwargs():
    """
    Extra arguments for enqueueing an import job. Provisioning a branch can take longer than RQ's default 300 second
    timeout, so the import may run as long as netbox-branching lets its own jobs (jobs.enqueue_import takes the
    longer of this and the plugin's own timeout).
    """
    if not branching_installed():
        return {}
    from netbox.plugins.utils import get_plugin_config

    return {"job_timeout": get_plugin_config("netbox_branching", "job_timeout")}


def import_destinations():
    """
    Where an import may write here, the default (and safe) one first: an import branch, a branch the user names (the
    one they work in), or main when chosen on purpose, with netbox-branching; an import proposal without it.
    """
    from ..choices import ImportDestinationChoices as D

    if branching_installed():
        return [D.BRANCH, D.CURRENT, D.MAIN]
    return [D.PROPOSAL]


def branch_ready(branch):
    from netbox_branching.choices import BranchStatusChoices

    return branch.status == BranchStatusChoices.READY


def chosen_branch(name, user):
    """The ready branch called ``name`` that ``user`` may view, and None with the reason when there is none."""
    from netbox_branching.choices import BranchStatusChoices
    from netbox_branching.models import Branch

    if not name:
        return None, "name the branch to import into"
    branch = Branch.objects.restrict(user, "view").filter(name=name).first()
    if branch is None:
        return None, f"branch {name!r} does not exist or you may not view it"
    if branch.status != BranchStatusChoices.READY:
        return None, f"branch {name!r} is not ready ({branch.get_status_display()})"
    return branch, None


# Models an import writes, as protect_main_scope entries would name them: this plugin's own branchable models and the
# core objects its scopes and reservations point at.
_IMPORT_MODELS = (
    "ipam.prefix",
    "ipam.iprange",
    "ipam.ipaddress",
    "dcim.macaddress",
    "netbox_windows_dhcp_plugin.",
)


def _protect_main_covers_imports():
    from netbox.plugins.utils import get_plugin_config

    if not get_plugin_config("netbox_change_control", "protect_main"):
        return False
    scope = [str(entry).lower() for entry in get_plugin_config("netbox_change_control", "protect_main_scope") or []]
    if not scope:
        return True
    for entry in scope:
        label = entry[:-1] if entry.endswith("*") else entry
        if any(m.startswith(label) or label.startswith(m) for m in _IMPORT_MODELS):
            return True
    return False


def direct_import_refusal(user):
    """
    Why ``user`` may not import straight into main, or None. Direct imports skip the branch review, so they are only
    offered where a direct edit of main would be allowed too.
    """
    from ..conf import get_setting

    if not branching_installed():
        return "importing directly into main is only offered with netbox-branching"
    if not get_setting("import_direct"):
        return "direct imports are turned off (the import_direct setting)"
    if change_control_installed():
        try:
            covered = _protect_main_covers_imports()
        except Exception:
            # Change control's settings could not be read: assume main is protected rather than bypass it.
            log.exception("could not read netbox-change-control's protect_main settings")
            covered = True
        if covered and not user.has_perm("netbox_change_control.bypass_policy"):
            return "change control protects main (protect_main): import into a branch"
    return None


def missing_import_permissions(user, destination=None):
    """
    Permissions a user needs to start an import into ``destination`` (the default when None), and lacks. A branch
    import needs to open a branch (and change request); writing into main is checked object by object as it saves.
    """
    from ..choices import ImportDestinationChoices

    if not branching_installed() or destination in (ImportDestinationChoices.MAIN, ImportDestinationChoices.CURRENT):
        return []
    from ..conf import get_setting

    needed = ["netbox_branching.add_branch"]
    if change_control_installed() and get_setting("import_change_request"):
        needed.append("netbox_change_control.add_changerequest")
    return [p for p in needed if not user.has_perm(p)]


def _name_prefix(windows_server):
    """``dhcp-import/<server>/``, with the server name shortened so a timestamp still fits."""
    from ..conf import get_setting

    prefix = get_setting("import_branch_prefix")
    room = BRANCH_NAME_LENGTH - len(prefix) - 2 - 15
    return f"{prefix}/{windows_server.name[: max(room, 1)]}/"


def open_import_branch(windows_server, logger=log):
    """The newest ready import branch for this server whose change request (if any) is still open, or None."""
    import re

    from netbox_branching.choices import BranchStatusChoices
    from netbox_branching.models import Branch

    # Match the whole name: a server called "dc" must not pick up the branches of a server called "dc/east".
    pattern = f"^{re.escape(_name_prefix(windows_server))}{_TIMESTAMP}$"
    branches = Branch.objects.filter(name__regex=pattern, status=BranchStatusChoices.READY).order_by("-created")
    if change_control_installed():
        try:
            from netbox_change_control.choices import ChangeRequestStatusChoices
        except ImportError:
            logger.warning("netbox-change-control has no ChangeRequestStatusChoices; finished requests not excluded")
        else:
            # A branch whose change request was completed or abandoned is finished, even if it still exists.
            branches = branches.exclude(change_request__status__in=ChangeRequestStatusChoices.TERMINAL)
    for branch in branches:
        # A stale branch can no longer be synced with main, so it cannot be merged cleanly: start a new one.
        if branch.is_stale:
            logger.warning(f"Not reusing stale branch {branch.name}")
            continue
        if branch.get_unsynced_changes().exists():
            logger.warning(f"Branch {branch.name} is behind main; sync it before merging")
        return branch
    return None


def open_import_branch_name(windows_server):
    """The name of the server's open import branch, or None. For warnings only, so a failed lookup says nothing."""
    if not branching_installed():
        return None
    try:
        branch = open_import_branch(windows_server)
    except Exception:
        log.exception("could not look for an open import branch of %s", windows_server)
        return None
    return branch.name if branch is not None else None


def import_branch(name, user):
    """
    The branch an import wrote into, and its change request, to link to: ``(branch, change_request, exists)``.

    ``branch`` and ``change_request`` are None when they are gone (a merged branch may have been deleted), when
    ``user`` may not view them, or when the plugin behind them is not installed. ``exists`` says whether the branch is
    still there, visible to the user or not.
    """
    if not name or not branching_installed():
        return None, None, False
    from django.db import DEFAULT_DB_ALIAS
    from netbox_branching.models import Branch

    branch = Branch.objects.restrict(user, "view").filter(name=name).first()
    exists = branch is not None or Branch.objects.filter(name=name).exists()
    change_request = None
    if change_control_installed():
        try:
            from netbox_change_control.models import ChangeRequest
        except ImportError:
            return branch, None, exists
        # Read from main even while the user has a branch active: change requests are created after the branch was
        # provisioned, so a branch's own copy of the table (when they are not in exempt_models) never has them.
        requests = ChangeRequest.objects.using(DEFAULT_DB_ALIAS).restrict(user, "view")
        if branch is not None:
            change_request = requests.filter(branch_id=branch.pk).first()
        elif not exists and any(f.name == "branch_name" for f in ChangeRequest._meta.get_fields()):
            # Change control keeps the branch name once the branch is deleted, so the review stays reachable.
            change_request = requests.filter(branch_name=name).order_by("-pk").first()
    return branch, change_request, exists


def branch_deletion(name, user, since=None):
    """
    The change log record of a deleted branch, so the Import panel can say whether it was merged before it went
    (issue #44): ``(change, status, merged_time)`` or None.

    ``status`` and ``merged_time`` are the branch's own values just before it was deleted. None when no deletion of a
    branch of that name since ``since`` is recorded, when ``user`` may not view the change log, or when netbox-branching
    is not installed; a missing branch alone says nothing about whether it was merged.
    """
    if not name or not branching_installed():
        return None
    from core.models import ObjectChange
    from django.contrib.contenttypes.models import ContentType
    from django.db import DEFAULT_DB_ALIAS
    from django.utils.dateparse import parse_datetime
    from netbox_branching.models import Branch

    changes = (
        ObjectChange.objects.using(DEFAULT_DB_ALIAS)
        .restrict(user, "view")
        .filter(
            changed_object_type=ContentType.objects.get_for_model(Branch),
            action="delete",
            prechange_data__name=name,
        )
    )
    if since is not None:
        changes = changes.filter(time__gte=since)
    # The first deletion since: the run's branch existed then, so a later one of the same name is a newer branch.
    change = changes.order_by("time").first()
    if change is None:
        return None
    data = change.prechange_data or {}
    merged_time = data.get("merged_time")
    return change, data.get("status"), parse_datetime(merged_time) if isinstance(merged_time, str) else None


def create_import_branch(windows_server, user, description):
    """
    Create and provision an import branch, synchronously (we are already in a background job).

    Raises django.core.exceptions.ValidationError when netbox-branching refuses a new branch (max_working_branches),
    and BranchError when provisioning fails; a branch that failed to provision is deleted rather than left behind.
    """
    from django.utils import timezone
    from netbox_branching.models import Branch

    branch = Branch(
        name=f"{_name_prefix(windows_server)}{timezone.now():%Y%m%d-%H%M%S}", owner=user, description=description[:200]
    )
    branch.full_clean()
    # provision() runs the schema copy here and now; save() would queue it as another job instead.
    branch.save(provision=False)
    try:
        branch.provision(user=user)
    except Exception as exc:
        try:
            branch.delete()
        except Exception:
            log.exception("could not delete branch %s after a failed provision", branch.name)
        raise BranchError(f"provisioning branch {branch.name} failed: {exc}") from exc
    branch.refresh_from_db()
    return branch


def _request(user, request_id=None):
    """A stand-in request: NetBox records change log entries (and the branch its diff) only during a request."""
    import uuid

    from django.test import RequestFactory
    from django.urls import reverse

    request = RequestFactory().get(reverse("home"))
    request.id = request_id or uuid.uuid4()
    request.user = user
    return request


@contextmanager
def reading(branch):
    """Read from the branch (no change tracking)."""
    from netbox_branching.utilities import activate_branch

    with activate_branch(branch):
        yield


@contextmanager
def writing(branch, user, request_id=None):
    """
    Write into the branch with change tracking, so the changes show in its diff. Needs a real user. ``request_id``
    sets the request ID the change log records (imports use ipam_guard.import_request_id()).
    """
    from netbox.context_managers import event_tracking
    from netbox_branching.utilities import activate_branch

    # The branch must still be active when event_tracking exits and flushes its events, so it goes outside.
    with activate_branch(branch), event_tracking(_request(user, request_id)):
        yield


@contextmanager
def on_main():
    """Read and write main, whatever branch is active: a direct import must never land in a branch by accident."""
    from netbox_branching.utilities import deactivate_branch

    with deactivate_branch():
        yield


@contextmanager
def writing_main(user):
    """
    Write into main with change tracking, as ``user``: the change log shows who imported, and change control's
    protect_main judges the writes against that user, as it would their own edits.
    """
    from netbox.context_managers import event_tracking

    with on_main(), event_tracking(_request(user)):
        yield


def close_connection(branch):
    """
    Close the branch's database connection. netbox-branching closes them when a request ends; a job sends no request
    signals, so without this every import would keep a Postgres connection open for the life of the worker.
    """
    from django.db import connections

    connections[branch.connection_name].close()


def atomic(branch):
    """A transaction on the branch's database connection, for one change."""
    from django.db import transaction

    return transaction.atomic(using=branch.connection_name)


def open_change_request(branch, user, title, description):
    """Open a change request for the branch and submit it for review. Returns it, or None if not possible."""
    if not change_control_installed():
        return None
    try:
        from netbox_change_control.models import ChangeRequest
    except ImportError:
        log.warning("netbox-change-control has no ChangeRequest model; no change request opened")
        return None
    existing = ChangeRequest.objects.filter(branch=branch).first()
    if existing is not None:
        return existing
    cr = ChangeRequest(branch=branch, title=title[:200], description=description[:200], requester=user)
    cr.full_clean()
    cr.save()
    cr.submit()
    return cr
