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
from netbox_dhcp.models import HostReservation, Option, OptionDefinition, SharedNetwork, Subnet

from ..choices import ProposalItemStatusChoices, ProposalStatusChoices
from ..conf import get_setting
from ..integrations import branching
from ..models import (
    ExclusionRange,
    FailoverRelationship,
    ImportProposal,
    ImportProposalItem,
    MacFilter,
    MacFilterLists,
    WindowsClass,
    WindowsPolicy,
    WindowsVendorOption,
    WindowsVendorOptionDefinition,
)
from . import netbox_state, windows_state
from .diff import CREATE, DELETE, UPDATE, Change, Record, diff, index
from .mapping import ApplyError, Writer

__all__ = (
    "KIND_MODELS",
    "kind_model",
    "ImportResult",
    "apply_items",
    "compute",
    "dismiss_items",
    "import_to_branch",
    "import_to_proposal",
)

log = logging.getLogger(__name__)

# The NetBox model each kind of change creates, updates or deletes, for permission checks and links. Vendor class
# definitions and values use their own models (see kind_model).
KIND_MODELS = {
    "class": WindowsClass,
    "optiondef": OptionDefinition,
    "scope": Subnet,
    "exclusion": ExclusionRange,
    "reservation": HostReservation,
    "option": Option,
    "superscope": SharedNetwork,
    "policy": WindowsPolicy,
    "failover": FailoverRelationship,
    "macfilter": MacFilter,
    "filterlists": MacFilterLists,
    "class6": WindowsClass,
    "scope6": Subnet,
    "reservation6": HostReservation,
    "option6": Option,
}

VENDOR_KIND_MODELS = {"optiondef": WindowsVendorOptionDefinition, "option": WindowsVendorOption}


def kind_model(kind, fields):
    """The model a change writes: vendor class definitions and values have models of their own."""
    if (fields or {}).get("vendor_class") and kind in VENDOR_KIND_MODELS:
        return VENDOR_KIND_MODELS[kind]
    return KIND_MODELS.get(kind)


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
    records, notes = windows_state.to_records(
        state,
        standard_codes=netbox_state.standard_codes(),
        partner_failovers=netbox_state.partner_failovers(windows_server),
        standard_codes6=netbox_state.standard_codes6(),
    )
    changes = diff(records, netbox_state.read(windows_server, for_import=True), equal=windows_state.equal)
    return changes, notes


# -- without branching: a review queue -----------------------------------------------------------------------------


def _target(writer, change):
    """The NetBox object a change updates or deletes, for a link on the review page."""
    fields = change.before
    try:
        if change.kind == "scope":
            return writer.subnet(change.key, required=False)
        if change.kind == "reservation":
            return writer.reservation(*change.key.split("/", 1), required=False)
        if change.kind == "class":
            return writer.windows_class(fields["class_type"], fields["name"], required=False)
        if change.kind == "optiondef":
            return writer.definition(fields["option_id"], fields["vendor_class"], required=False)
        if change.kind == "option":
            return writer.option(change.key, fields)
        if change.kind == "policy":
            return writer.windows_policy(fields["scope_id"], fields["name"], required=False)
        if change.kind == "superscope":
            return writer.shared_network(fields["name"])
        if change.kind == "failover":
            return writer.failover(fields["name"])
        if change.kind == "macfilter":
            return MacFilter.objects.filter(dhcp_server=writer.dhcp, mac_address__iexact=fields["mac_address"]).first()
        if change.kind == "filterlists":
            return MacFilterLists.objects.filter(dhcp_server=writer.dhcp).first()
        if change.kind == "class6":
            return writer.windows_class6(fields["name"], required=False)
        if change.kind == "scope6":
            return writer.subnet6(change.key.removeprefix("v6/"), required=False)
        if change.kind == "reservation6":
            return writer.reservation6(*change.key.removeprefix("v6/").split("/", 1), required=False)
        if change.kind == "option6":
            return writer.option6(change.key, fields)
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
    # Dismissals hold across proposals: closing a proposal must not bring back what a reviewer already turned down.
    dismissed = {
        (i.action, i.kind, i.key, _frozen(i.after))
        for i in ImportProposalItem.objects.filter(
            proposal__windows_server=windows_server, status=ProposalItemStatusChoices.DISMISSED
        )
    }
    to_review = [
        (sequence, change)
        for sequence, change in enumerate(changes)
        if (change.action, change.kind, change.key, _frozen(change.after)) not in dismissed
    ]
    proposal = (
        ImportProposal.objects.select_for_update()
        .filter(windows_server=windows_server, status=ProposalStatusChoices.OPEN)
        .order_by("-created")
        .first()
    )
    if proposal is None and not to_review:
        return result
    if proposal is None:
        proposal = ImportProposal.objects.create(windows_server=windows_server, sync_run=sync_run)
    else:
        proposal.items.filter(status__in=(ProposalItemStatusChoices.PENDING, ProposalItemStatusChoices.FAILED)).delete()
        proposal.sync_run = sync_run
        proposal.save()
    writer = Writer(windows_server)
    for sequence, change in to_review:
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
    if not ImportProposalItem.objects.filter(pk=item.pk).exists():
        return  # a new import replaced it meanwhile; saving would bring it back
    item.snapshot()
    item.status = status
    item.error = error
    item.save()


def apply_items(items, user=None):
    """
    Apply proposal items in dependency order, one transaction each. Call from a request: NetBox then records the
    reviewing user in the change log, and every object written is checked against ``user``'s permissions (a
    refusal rolls the item back). Returns (applied, failed) counts.

    Each item is checked against what NetBox holds now. If someone changed the object since the import, the item
    fails instead of overwriting their edit; running the import again proposes a fresh change.

    Saves made here queue no push: the objects then match Windows, so pushing them back would do nothing.
    """
    from ..signals import push_suppressed

    with push_suppressed():
        return _apply_items(items, user)


def _apply_items(items, user):
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
            current_by_server[server.pk] = index(netbox_state.read(server, for_import=True))
        current = current_by_server[server.pk].get((item.kind, item.key))
        change = _item_change(item)
        error = _stale(change, current)
        if error is None:
            try:
                with transaction.atomic():
                    if not _already_done(change, current):
                        Writer(server, user=user).apply(change)
                    _record_managed(server, change)
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


def _record_managed(server, change):
    """
    NetBox and Windows now agree on this object, so later pushes manage it: deleting it in NetBox deletes it on
    Windows. The fingerprint is of the Windows values, as a push would record them.
    """
    from ..models import SyncState
    from .push import fingerprint

    if change.action == DELETE:
        SyncState.objects.filter(windows_server=server, kind=change.kind, key=change.key).delete()
        return
    SyncState.objects.update_or_create(
        windows_server=server,
        kind=change.kind,
        key=change.key,
        defaults={"fingerprint": fingerprint(Record(change.kind, change.key, change.after or {}))},
    )


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


def import_to_branch(windows_server, state, user, logger=log, on_branch=None):
    """
    Write the differences into the server's open import branch, or into a new one.

    The diff is taken against the branch itself, so a repeated run only adds what changed since. A new branch is
    created only when there is something to write. ``on_branch(branch)`` is called as soon as the branch is known, so
    the caller can record it even if a later step fails.
    """
    if user is None:
        # NetBox's change log, which is what puts changes into a branch, needs a user to record.
        raise branching.BranchError("imports into a branch need a user: set import_user for scheduled imports")
    branch = branching.open_import_branch(windows_server, logger=logger)
    try:
        return _write_branch(windows_server, state, user, branch, logger, on_branch)
    finally:
        if branch is not None:
            branching.close_connection(branch)


def _write_branch(windows_server, state, user, branch, logger, on_branch):
    with branching.reading(branch) if branch else nullcontext():
        changes, notes = compute(windows_server, state)
    result = ImportResult(changes=changes, notes=notes, branch=branch)
    if not changes:
        return result

    if branch is None:
        branch = branching.create_import_branch(
            windows_server, user, description=f"Changes found on Windows DHCP server {windows_server}"
        )
        logger.info(f"Created branch {branch.name}")
    else:
        logger.info(f"Updating branch {branch.name}")
    result.branch = branch
    try:
        return _fill_branch(windows_server, user, branch, result, logger, on_branch)
    finally:
        branching.close_connection(branch)


def _fill_branch(windows_server, user, branch, result, logger, on_branch):
    changes = result.changes
    if on_branch is not None:
        on_branch(branch)

    with branching.subnet_id_allocation() as subnet_ids, branching.writing(branch, user):
        writer = Writer(windows_server, subnet_ids=subnet_ids)
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

    if get_setting("import_change_request") and branching.change_control_installed():
        try:
            branching.open_change_request(
                branch,
                user,
                title=f"DHCP import from {windows_server}",
                description=f"{len(changes)} change(s) found on Windows DHCP server {windows_server}",
            )
        except Exception as exc:
            # The changes are in the branch already; a missing change request must not hide that.
            message = f"Could not open a change request for {branch.name}: {_message(exc)}"
            result.notes.append(message)
            logger.warning(message)
    return result
