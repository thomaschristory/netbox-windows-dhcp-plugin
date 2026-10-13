"""
Optional integration with netbox-branching and netbox-change-control.

Everything that imports those plugins lives here, so the plugin keeps working when they are not installed.
"""

import logging
from contextlib import contextmanager

from django.apps import apps

log = logging.getLogger(__name__)

# Bookkeeping models that must always live on main (see models/sync.py).
NON_BRANCHABLE = {"windowsserver", "syncstate", "syncrun", "importproposal", "importproposalitem"}


def branching_installed():
    return apps.is_installed("netbox_branching")


def save_belongs_to_branching():
    """
    True when a model save is not an edit of main: it happens inside a branch (edits, imports, syncs from main), or
    while a branch is being merged or reverted (the merge then queues its own pushes). Direct edits of main still
    push, as without branching.
    """
    if not branching_installed():
        return False
    try:
        from netbox_branching.choices import BranchStatusChoices
        from netbox_branching.contextvars import active_branch
        from netbox_branching.models import Branch
    except ImportError:
        return True
    if active_branch.get() is not None:
        return True
    busy = (BranchStatusChoices.MERGING, BranchStatusChoices.REVERTING)
    return Branch.objects.filter(status__in=busy).exists()


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


def _affected_servers(branch):
    from ..signals import servers_for_change

    ids = set()
    for change in branch.get_merged_changes().select_related("changed_object_type"):
        ids |= servers_for_change(change)
    return ids


def _queue(ids, user, branch):
    from django.db import transaction

    from ..signals import queue_pushes

    if not ids:
        return
    name = branch.name
    transaction.on_commit(lambda: queue_pushes(ids, user, branch=name), robust=True)


def _on_merge(sender, branch, user=None, **kwargs):
    try:
        _queue(_affected_servers(branch), user, branch)
    except Exception:
        # Never break a merge that already happened; the push can be run by hand.
        log.exception("could not queue Windows DHCP pushes after merging %s", branch)


def _on_pre_revert(sender, branch, user=None, **kwargs):
    # A revert that fails, or has nothing to revert, sends no post_revert: do not let those entries pile up.
    if len(_reverting) > 50:
        _reverting.clear()
    try:
        _reverting[branch.pk] = _affected_servers(branch)
    except Exception:
        log.exception("could not work out the Windows DHCP servers touched by %s", branch)


def _on_revert(sender, branch, user=None, **kwargs):
    try:
        _queue(_reverting.pop(branch.pk, set()), user, branch)
    except Exception:
        log.exception("could not queue Windows DHCP pushes after reverting %s", branch)


def _connect_push_signals():
    try:
        from netbox_branching.signals import post_merge, post_revert, pre_revert
    except ImportError:
        log.warning("netbox-branching has no merge signals; Windows DHCP will not push after merges")
        return
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
    timeout, so use the one netbox-branching uses for its own jobs.
    """
    if not branching_installed():
        return {}
    from netbox.plugins.utils import get_plugin_config

    return {"job_timeout": get_plugin_config("netbox_branching", "job_timeout")}


def missing_import_permissions(user):
    """Permissions a user needs to start an import that will open a branch (and change request), and lacks."""
    if not branching_installed():
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
    return f"{prefix}/{windows_server.dhcp_server.name[: max(room, 1)]}/"


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


def _request(user):
    """A stand-in request: NetBox records change log entries (and the branch its diff) only during a request."""
    import uuid

    from django.test import RequestFactory
    from django.urls import reverse

    request = RequestFactory().get(reverse("home"))
    request.id = uuid.uuid4()
    request.user = user
    return request


@contextmanager
def reading(branch):
    """Read from the branch (no change tracking)."""
    from netbox_branching.utilities import activate_branch

    with activate_branch(branch):
        yield


@contextmanager
def writing(branch, user):
    """Write into the branch with change tracking, so the changes show in its diff. Needs a real user."""
    from netbox.context_managers import event_tracking
    from netbox_branching.utilities import activate_branch

    # The branch must still be active when event_tracking exits and flushes its events, so it goes outside.
    with activate_branch(branch), event_tracking(_request(user)):
        yield


# Advisory lock (two numbers) serialising branch-mode imports while they allocate subnet IDs.
_SUBNET_ID_LOCK = (0x57444843, 0)


@contextmanager
def subnet_id_allocation():
    """
    Yield an iterator of free netbox-plugin-dhcp subnet IDs, while holding a lock other imports wait on.

    sys4 numbers a new Subnet max(subnet_id) + 1 within the schema it is saved in. Two import branches would each
    start from main's maximum and both merge the same ID, which the unique constraint refuses at merge time. So
    count from the highest ID in main and in every open import branch instead.
    """
    import itertools

    from django.db import DatabaseError, connection
    from django.db.models import Max
    from netbox_branching.choices import BranchStatusChoices
    from netbox_branching.models import Branch
    from netbox_branching.utilities import activate_branch
    from netbox_dhcp.models import Subnet

    from ..conf import get_setting

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s, %s)", list(_SUBNET_ID_LOCK))
    try:
        highest = Subnet.objects.aggregate(m=Max("subnet_id"))["m"] or 0
        others = Branch.objects.filter(
            name__startswith=f"{get_setting('import_branch_prefix')}/", status=BranchStatusChoices.READY
        )
        for other in others:
            try:
                with activate_branch(other):
                    highest = max(highest, Subnet.objects.aggregate(m=Max("subnet_id"))["m"] or 0)
            except DatabaseError:
                log.warning("could not read subnet IDs from branch %s", other.name)
            finally:
                close_connection(other)
        yield itertools.count(highest + 1)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", list(_SUBNET_ID_LOCK))


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
