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


def change_control_installed():
    return apps.is_installed("netbox_change_control")


def _resolver(model):
    if model._meta.app_label == "netbox_windows_dhcp_plugin" and model._meta.model_name in NON_BRANCHABLE:
        return False
    return None


def register():
    if not branching_installed():
        return
    try:
        from netbox_branching.utilities import register_branching_resolver
    except ImportError:
        # A netbox-branching release without this hook: start anyway rather than take NetBox down.
        log.warning("netbox-branching has no register_branching_resolver; Windows DHCP bookkeeping will be branched")
        return
    register_branching_resolver(_resolver)


# -- import branches -----------------------------------------------------------------------------------------------
#
# These run in the import job (an RQ worker). They are only called after branching_installed() said yes.


def import_branch_prefix(windows_server):
    from ..conf import get_setting

    return f"{get_setting('import_branch_prefix')}/{windows_server.dhcp_server.name}/"


def open_import_branch(windows_server):
    """The newest ready import branch for this server whose change request (if any) is still open, or None."""
    from netbox_branching.choices import BranchStatusChoices
    from netbox_branching.models import Branch

    branches = Branch.objects.filter(
        name__startswith=import_branch_prefix(windows_server), status=BranchStatusChoices.READY
    ).order_by("-created")
    if change_control_installed():
        try:
            from netbox_change_control.choices import ChangeRequestStatusChoices
        except ImportError:
            pass
        else:
            # A branch whose change request was completed or abandoned is finished, even if it still exists.
            branches = branches.exclude(change_request__status__in=ChangeRequestStatusChoices.TERMINAL)
    return branches.first()


def create_import_branch(windows_server, user, description):
    """
    Create and provision an import branch, synchronously (we are already in a background job).

    Raises django.core.exceptions.ValidationError when max_working_branches is reached, since Branch.clean() is
    where netbox-branching enforces it.
    """
    from django.utils import timezone
    from netbox_branching.models import Branch

    # Branch names are limited to 100 characters.
    name = f"{import_branch_prefix(windows_server)}{timezone.now():%Y%m%d-%H%M%S}"[-100:]
    branch = Branch(name=name, owner=user, description=description[:200])
    branch.full_clean()
    # provision() runs the schema copy here and now; save() would queue it as another job instead.
    branch.save(provision=False)
    branch.provision(user=user)
    branch.refresh_from_db()
    return branch


def _request(user):
    """A stand-in request: NetBox records change log entries (and the branch its diff) only during a request."""
    import uuid

    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory
    from django.urls import reverse

    request = RequestFactory().get(reverse("home"))
    request.id = uuid.uuid4()
    request.user = user or AnonymousUser()
    return request


@contextmanager
def reading(branch):
    """Read from the branch (no change tracking)."""
    from netbox_branching.utilities import activate_branch

    with activate_branch(branch):
        yield


@contextmanager
def writing(branch, user):
    """Write into the branch with change tracking, so the changes show in its diff."""
    from netbox.context_managers import event_tracking
    from netbox_branching.utilities import activate_branch

    # The branch must still be active when event_tracking exits and flushes its events, so it goes outside.
    with activate_branch(branch), event_tracking(_request(user)):
        yield


def atomic(branch):
    """A transaction on the branch's database connection, for one change."""
    from django.db import transaction

    return transaction.atomic(using=branch.connection_name)


def open_change_request(branch, user, title, description):
    """Open a change request for the branch and submit it for review. Returns it, or None if not possible."""
    if not change_control_installed() or user is None or not user.is_authenticated:
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
