"""
Import: bring what a Windows server holds into NetBox, as an import branch (netbox-branching) or as an
ImportProposal review queue (without it).

No Windows I/O here: the job reads the server and passes the ``WindowsState`` in.
"""

import json
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from netbox_dhcp.models import HostReservation, Option, OptionDefinition, Subnet

from ..choices import ProposalItemStatusChoices, ProposalStatusChoices
from ..conf import get_setting
from ..integrations import branching
from ..models import ExclusionRange, ImportProposal, ImportProposalItem, WindowsClass
from . import netbox_state, windows_state
from .diff import CREATE, DELETE, UPDATE, Change, Record, diff, index
from .mapping import ApplyError, Writer

__all__ = (
    "KIND_MODELS",
    "ImportResult",
    "apply_items",
    "compute",
    "dismiss_items",
    "import_to_branch",
    "import_to_proposal",
)

log = logging.getLogger(__name__)

# The NetBox model each kind of change creates, updates or deletes, for permission checks and links.
KIND_MODELS = {
    "class": WindowsClass,
    "optiondef": OptionDefinition,
    "scope": Subnet,
    "exclusion": ExclusionRange,
    "reservation": HostReservation,
    "option": Option,
}

# Errors one change can raise without the rest of the import being wrong.
APPLY_ERRORS = (ApplyError, ValidationError, IntegrityError, ValueError)


@dataclass
class ImportResult:
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    applied: int = 0
    failed: list[str] = field(default_factory=list)
    branch: object = None
    proposal: ImportProposal | None = None

    def summary(self):
        counts = {a: sum(1 for c in self.changes if c.action == a) for a in (CREATE, UPDATE, DELETE)}
        out = {"changes": len(self.changes), **counts}
        if self.branch is not None:
            out.update(applied=self.applied, failed=len(self.failed))
        if self.notes:
            out["notes"] = self.notes
        return out


def _message(exc):
    if isinstance(exc, ValidationError):
        return "; ".join(exc.messages)
    return str(exc)


def compute(windows_server, state):
    """Diff a Windows state against what NetBox (or the active branch) holds for the server."""
    records, notes = windows_state.to_records(state, standard_codes=netbox_state.standard_codes())
    changes = diff(records, netbox_state.read(windows_server), equal=windows_state.equal)
    return changes, notes


# -- without branching: a review queue -----------------------------------------------------------------------------


def _target(writer, change):
    """The NetBox object a change updates or deletes, for a link on the review page."""
    fields = change.before
    try:
        if change.kind == "scope":
            return writer.subnet(change.key, required=False)
        if change.kind == "reservation":
            return writer.reservation(fields["scope_id"], fields["ip_address"], required=False)
        if change.kind == "class":
            return writer.windows_class(fields["class_type"], fields["name"], required=False)
        if change.kind == "optiondef":
            return writer.definition(fields["option_id"], fields["vendor_class"], required=False)
        if change.kind == "option":
            return writer.option(change.key, fields)
    except ApplyError:
        return None
    return None


@transaction.atomic
def import_to_proposal(windows_server, state, sync_run):
    """
    Record the differences in the server's open ImportProposal, creating it when needed.

    Pending (and failed) items from an earlier run are replaced. An item someone dismissed stays dismissed as long as
    Windows still proposes exactly the same thing.
    """
    changes, notes = compute(windows_server, state)
    result = ImportResult(changes=changes, notes=notes)
    proposal = (
        ImportProposal.objects.select_for_update()
        .filter(windows_server=windows_server, status=ProposalStatusChoices.OPEN)
        .order_by("-created")
        .first()
    )
    if proposal is None and not changes:
        return result
    if proposal is None:
        proposal = ImportProposal.objects.create(windows_server=windows_server, sync_run=sync_run)
    else:
        proposal.items.filter(status__in=(ProposalItemStatusChoices.PENDING, ProposalItemStatusChoices.FAILED)).delete()
        proposal.sync_run = sync_run
        proposal.save()
    dismissed = {
        (i.action, i.kind, i.key, _frozen(i.after))
        for i in proposal.items.filter(status=ProposalItemStatusChoices.DISMISSED)
    }
    writer = Writer(windows_server)
    for sequence, change in enumerate(changes):
        if (change.action, change.kind, change.key, _frozen(change.after)) in dismissed:
            continue
        target = _target(writer, change) if change.action != CREATE else None
        ImportProposalItem.objects.create(
            proposal=proposal,
            sequence=sequence,
            action=change.action,
            kind=change.kind,
            key=change.key,
            before=change.before,
            after=change.after,
            object=target,
        )
    update_status(proposal)
    result.proposal = proposal
    return result


def _frozen(value):
    return json.dumps(value, sort_keys=True)


def update_status(proposal):
    """An open proposal with nothing left to review is closed: applied if anything was, else dismissed."""
    items = proposal.items.all()
    if proposal.status != ProposalStatusChoices.OPEN or items.filter(status=ProposalItemStatusChoices.PENDING).exists():
        return
    if items.filter(status=ProposalItemStatusChoices.FAILED).exists():
        return
    applied = items.filter(status=ProposalItemStatusChoices.APPLIED).exists()
    proposal.snapshot()
    proposal.status = ProposalStatusChoices.APPLIED if applied else ProposalStatusChoices.DISMISSED
    proposal.save()


def _item_change(item):
    return Change(item.action, item.kind, item.key, item.before, item.after)


def _set_item(item, status, error=""):
    item.snapshot()
    item.status = status
    item.error = error
    item.save()


def apply_items(items):
    """
    Apply proposal items in dependency order, one transaction each. Call from a request: NetBox then records the
    reviewing user in the change log. Returns (applied, failed) counts.

    Each item is checked against what NetBox holds now. If someone changed the object since the import, the item
    fails instead of overwriting their edit; running the import again proposes a fresh change.
    """
    items = sorted(items, key=lambda i: (i.proposal_id, i.sequence, i.pk))
    applied = failed = 0
    proposals = {}
    current_by_server = {}
    for item in items:
        if item.status not in (ProposalItemStatusChoices.PENDING, ProposalItemStatusChoices.FAILED):
            continue
        proposal = item.proposal
        proposals[proposal.pk] = proposal
        server = proposal.windows_server
        if server.pk not in current_by_server:
            current_by_server[server.pk] = index(netbox_state.read(server))
        current = current_by_server[server.pk].get((item.kind, item.key))
        change = _item_change(item)
        error = _stale(change, current)
        if error is None:
            try:
                with transaction.atomic():
                    if not _already_done(change, current):
                        Writer(server).apply(change)
            except APPLY_ERRORS as exc:
                error = _message(exc)
        if error:
            failed += 1
            _set_item(item, ProposalItemStatusChoices.FAILED, error)
        else:
            applied += 1
            _set_item(item, ProposalItemStatusChoices.APPLIED)
    for proposal in proposals.values():
        update_status(proposal)
    return applied, failed


def _same(a, b):
    return not diff([Record("x", "x", a)], [Record("x", "x", b)], equal=windows_state.equal)


def _already_done(change, current):
    if change.action == DELETE:
        return current is None
    return current is not None and _same(current.fields, change.after)


def _stale(change, current):
    """Why an item can no longer be applied as reviewed, or None."""
    if _already_done(change, current):
        return None
    if change.action == CREATE and current is not None:
        return "already in NetBox with different values: run the import again"
    if change.action in (UPDATE, DELETE):
        if current is None:
            return "no longer in NetBox: run the import again"
        if not _same(current.fields, change.before):
            return "changed in NetBox since the import: run the import again"
    return None


def dismiss_items(items):
    count = 0
    proposals = {}
    for item in items:
        if item.status in (ProposalItemStatusChoices.PENDING, ProposalItemStatusChoices.FAILED):
            _set_item(item, ProposalItemStatusChoices.DISMISSED)
            proposals[item.proposal_id] = item.proposal
            count += 1
    for proposal in proposals.values():
        update_status(proposal)
    return count


# -- with branching: an import branch ------------------------------------------------------------------------------


def import_to_branch(windows_server, state, user, logger=log):
    """
    Write the differences into the server's open import branch, or into a new one.

    The diff is taken against the branch itself, so a repeated run only adds what changed since. A new branch is
    created only when there is something to write.
    """
    branch = branching.open_import_branch(windows_server)
    with branching.reading(branch) if branch else nullcontext():
        changes, notes = compute(windows_server, state)
    result = ImportResult(changes=changes, notes=notes, branch=branch)
    if not changes:
        return result

    created = branch is None
    if created:
        branch = branching.create_import_branch(
            windows_server, user, description=f"Changes found on Windows DHCP server {windows_server}"
        )
        result.branch = branch
        logger.info(f"Created branch {branch.name}")
    else:
        logger.info(f"Updating branch {branch.name}")

    with branching.writing(branch, user):
        writer = Writer(windows_server)
        for change in changes:
            try:
                with branching.atomic(branch):
                    writer.apply(change)
            except APPLY_ERRORS as exc:
                message = f"{change.action} {change.kind} {change.key}: {_message(exc)}"
                result.failed.append(message)
                logger.warning(message)
            else:
                result.applied += 1

    if get_setting("import_change_request"):
        cr = branching.open_change_request(
            branch,
            user,
            title=f"DHCP import from {windows_server}",
            description=f"{len(changes)} change(s) found on Windows DHCP server {windows_server}",
        )
        if cr is None and branching.change_control_installed():
            logger.warning("No change request opened: scheduled imports need the import_user setting")
    return result
