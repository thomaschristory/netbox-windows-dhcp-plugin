"""
Import: bring what a Windows server holds into NetBox, as an import branch (netbox-branching) or as an
ImportProposal review queue (without it). With netbox-branching, a user may also choose to import straight into main
(issue #36).

No Windows I/O here: the job reads the server and passes the ``WindowsState`` in.
"""

import json
import logging
from contextlib import contextmanager, nullcontext
from copy import copy
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError, transaction
from utilities.exceptions import AbortRequest

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
    OptionDefinition,
    OptionValue,
    Reservation,
    Scope,
    Superscope,
    SyncState,
    WindowsClass,
    WindowsPolicy,
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
    "import_to_main",
    "import_to_proposal",
)

log = logging.getLogger(__name__)

# The NetBox model each kind of change creates, updates or deletes, for permission checks and links.
KIND_MODELS = {
    "class": WindowsClass,
    "optiondef": OptionDefinition,
    "scope": Scope,
    "exclusion": ExclusionRange,
    "reservation": Reservation,
    "option": OptionValue,
    "superscope": Superscope,
    "policy": WindowsPolicy,
    "failover": FailoverRelationship,
    "macfilter": MacFilter,
    "filterlists": MacFilterLists,
    "class6": WindowsClass,
    "scope6": Scope,
    "reservation6": Reservation,
    "option6": OptionValue,
}


def kind_model(kind):
    """The model a change of this kind writes, or None for a kind this version does not know."""
    return KIND_MODELS.get(kind)


# Errors one change can raise without the rest of the import being wrong. AbortRequest is how a plugin's pre_save
# receiver refuses a save, such as change control's protect_main. DataError is a value the database column cannot
# hold (too long, out of range): that one object fails, in its own transaction, instead of the whole run.
APPLY_ERRORS = (ApplyError, ValidationError, IntegrityError, DataError, ValueError, AbortRequest)


@dataclass
class ImportResult:
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    applied: int = 0
    failed: list[str] = field(default_factory=list)
    branch: object = None
    # Whether this run created its branch, rather than adding to the server's open import branch.
    branch_created: bool = False
    proposal: ImportProposal | None = None
    # Written straight into main (import_to_main).
    direct: bool = False
    # Proposal imports only: how many differences went to review, and how many were left out because a reviewer
    # had already dismissed exactly that change. None for branch and main imports, which have no dismissals.
    review: int | None = None
    dismissed: int = 0

    def summary(self):
        """
        Counts for the sync run. ``changes`` is every difference found. A proposal import also records ``review``
        and ``dismissed``, so "nothing waits for review" is never read as "no differences" (issue #43).
        """
        counts = {a: sum(1 for c in self.changes if c.action == a) for a in (CREATE, UPDATE, DELETE)}
        out = {"changes": len(self.changes), **counts}
        if self.direct:
            out.update(applied=self.applied, failed=len(self.failed))
        elif self.branch is not None:
            out.update(applied=self.applied, failed=len(self.failed), branch_created=self.branch_created)
        if self.review is not None:
            out.update(review=self.review, dismissed=self.dismissed)
        if self.notes:
            out["notes"] = self.notes
        return out


@contextmanager
def _one_change():
    """
    One change in its own transaction. If it fails, the change log events it queued are dropped too: the block that
    tracks events sends them when it ends, and event rules and webhooks must not hear of objects rolled back.

    The queue is put back whatever the error is, not only for the errors callers expect: an unexpected one in a web
    request becomes an error page, after which NetBox still sends what is queued (GHSA-5x5m-vqw4-wq85).
    """
    from netbox.context import events_queue

    # EventContext lazily serializes data: converting it to dict loses that behavior. Coalescing an event also
    # updates its snapshots in place, so those need their own copy.
    queued = {key: copy(event) for key, event in events_queue.get().items()}
    for event in queued.values():
        if "snapshots" in event:
            event["snapshots"] = dict(event["snapshots"])
    try:
        yield
    except BaseException:
        events_queue.set(queued)
        raise


def _message(exc):
    if isinstance(exc, ValidationError):
        return "; ".join(exc.messages)
    if isinstance(exc, AbortRequest):
        return exc.message
    return str(exc)


def compute(windows_server, state):
    """Diff a Windows state against what NetBox (or the active branch) holds for the server."""
    records, notes = windows_state.to_records(
        state,
        standard_codes=netbox_state.standard_codes(),
        partner_failovers=netbox_state.partner_failovers(windows_server),
        standard_codes6=netbox_state.standard_codes6(),
        partner_servers=netbox_state.partner_servers(windows_server),
    )
    changes = diff(
        records,
        netbox_state.read(windows_server, for_import=True),
        equal_records=windows_state.records_equal,
    )
    return changes, notes


# -- without branching: a review queue -----------------------------------------------------------------------------


def _target(writer, change):
    """The NetBox object a change updates or deletes, for a link on the review page."""
    fields = change.before
    try:
        if change.kind == "scope":
            return writer.scope(change.key, required=False)
        if change.kind == "reservation":
            return writer.reservation(*change.key.split("/", 1), required=False)
        if change.kind == "class":
            return writer.windows_class(fields["class_type"], fields["name"], required=False)
        if change.kind == "optiondef":
            return writer.definition(fields["option_id"], fields["vendor_class"], required=False)
        if change.kind == "option":
            return writer.option(change.key, fields)
        # These four can exist twice in NetBox under names differing only in case: the writer's own lookups reach
        # the copy a delete names, so the link and the permission check are for the object that will be written.
        if change.kind == "policy":
            return writer.find_policy(change)
        if change.kind == "superscope":
            return writer.find_superscope(change)
        if change.kind == "failover":
            return writer.find_failover(change)
        if change.kind == "macfilter":
            return writer.find_macfilter(change)
        if change.kind == "filterlists":
            return writer.filter_lists()
        if change.kind == "class6":
            return writer.windows_class6(fields["name"], required=False)
        if change.kind == "scope6":
            return writer.scope6(change.key.removeprefix("v6/"), required=False)
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
    # Only the four fields compared: the history of a server can hold many dismissed items.
    dismissed = {
        (action, kind, key, _frozen(after))
        for action, kind, key, after in ImportProposalItem.objects.filter(
            proposal__windows_server=windows_server, status=ProposalItemStatusChoices.DISMISSED
        ).values_list("action", "kind", "key", "after")
    }
    to_review = []
    for sequence, change in enumerate(changes):
        if (change.action, change.kind, change.key, _frozen(change.after)) in dismissed:
            result.dismissed += 1
        else:
            to_review.append((sequence, change))
    result.review = len(to_review)
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


_OPEN = (ProposalItemStatusChoices.PENDING, ProposalItemStatusChoices.FAILED)


def _set_item(item, status, error=""):
    """
    Record the decision on an item that is still open, and say whether it was. The row is locked and read again:
    the copy in hand was loaded when the request started, and since then a new import may have replaced the item
    (saving the copy would bring it back) or another reviewer may have applied or dismissed it (their decision
    stands).
    """
    with transaction.atomic():
        locked = ImportProposalItem.objects.select_for_update().filter(pk=item.pk, status__in=_OPEN).first()
        if locked is None:
            return False
        locked.snapshot()
        locked.status = status
        locked.error = error
        # Never an INSERT: if the row went away after all, this fails instead of bringing it back.
        locked.save(force_update=True)
    item.status = status
    item.error = error
    return True


# Said to a reviewer who tries to apply items while working in a netbox-branching branch.
IN_BRANCH = "import proposals are applied to main: leave the branch (switch to main) and apply again"


def apply_items(items, user=None):
    """
    Apply proposal items in dependency order, one transaction each. Call from a request: NetBox then records the
    reviewing user in the change log, and every object written is checked against ``user``'s permissions (a
    refusal rolls the item back). Returns (applied, failed) counts.

    Each item is checked against what NetBox holds now. If someone changed the object since the import, the item
    fails instead of overwriting their edit; running the import again proposes a fresh change.

    Saves made here queue no push: the objects then match Windows, so pushing them back would do nothing.

    Refused inside a netbox-branching branch: the objects would be written to the branch while the record that
    NetBox now manages them goes to main, and the next push from main would delete them on Windows.
    """
    from ..signals import push_suppressed

    if branching.in_branch():
        raise branching.BranchError(IN_BRANCH)
    with push_suppressed():
        return _apply_items(items, user)


def _apply_items(items, user):
    items = sorted(
        (i for i in items if i.status in _OPEN),
        key=lambda i: (i.proposal_id, i.sequence, i.pk),
    )
    applied = failed = 0
    proposals = {}
    # What NetBox holds now, per server, for the kinds of object the items are about: applying one scope must not
    # load every reservation of the server into the request.
    kinds_by_server = {}
    for item in items:
        kinds_by_server.setdefault(item.proposal.windows_server_id, set()).add(item.kind)
    current_by_server = {}
    # One writer per server: each item carries its own copy of the server, and a writer reads the server's VRF.
    writers = {}
    for item in items:
        proposal = item.proposal
        proposals[proposal.pk] = proposal
        server = proposal.windows_server
        if server.pk not in current_by_server:
            kinds = frozenset(kinds_by_server[server.pk])
            current_by_server[server.pk] = index(netbox_state.read(server, for_import=True, kinds=kinds))
            writers[server.pk] = Writer(server, user=user)
        current = current_by_server[server.pk].get((item.kind, item.key))
        change = _item_change(item)
        error = _stale(change, current)
        if error is None:
            try:
                with _one_change(), transaction.atomic():
                    # The item is locked before anything is written, and stays locked until its new status is
                    # committed with the objects. The copy in hand was loaded when the request started: if another
                    # reviewer dismissed the item since, or a new import replaced it, writing it now would put an
                    # object in NetBox (and under push management) that the review queue says was turned down.
                    if not ImportProposalItem.objects.select_for_update().filter(pk=item.pk, status__in=_OPEN).exists():
                        continue
                    if not _already_done(change, current):
                        writers[server.pk].apply(change)
                    _record_managed(server, change)
                    applied += _set_item(item, ProposalItemStatusChoices.APPLIED)
                continue
            except APPLY_ERRORS as exc:
                error = _message(exc)
        # Counted only when this call made the decision: an item replaced by a new import, or decided by another
        # reviewer meanwhile, keeps what was recorded there.
        failed += _set_item(item, ProposalItemStatusChoices.FAILED, error)
    for proposal in proposals.values():
        update_status(proposal)
    return applied, failed


def _record_managed(server, change):
    """
    NetBox and Windows now agree on this object, so later pushes manage it: deleting it in NetBox deletes it on
    Windows. The fingerprint is of the Windows values, as a push would record them.
    """
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


def _same(kind, a, b):
    return windows_state.records_equal(kind, a, b)


def _already_done(change, current):
    if change.action == DELETE:
        return current is None
    return current is not None and _same(change.kind, current.fields, change.after)


def _stale(change, current):
    """Why an item can no longer be applied as reviewed, or None."""
    if _already_done(change, current):
        return None
    if change.action == CREATE and current is not None:
        return "already in NetBox with different values: run the import again"
    if change.action in (UPDATE, DELETE):
        if current is None:
            return "no longer in NetBox: run the import again"
        if not _same(change.kind, current.fields, change.before):
            return "changed in NetBox since the import: run the import again"
    return None


def dismiss_items(items):
    count = 0
    proposals = {}
    for item in items:
        # Counted only when this call made the decision: another reviewer may have applied the item meanwhile.
        if item.status in _OPEN and _set_item(item, ProposalItemStatusChoices.DISMISSED):
            proposals[item.proposal_id] = item.proposal
            count += 1
    for proposal in proposals.values():
        update_status(proposal)
    return count


# -- with branching: an import branch ------------------------------------------------------------------------------


def import_to_branch(windows_server, state, user, logger=log, on_branch=None, target=None):
    """
    Write the differences into the server's open import branch, or into a new one; or into ``target``, a branch the
    user chose (the one they work in, issue #36), which is never replaced by a new one.

    The diff is taken against the branch itself, so a repeated run only adds what changed since. A new branch is
    created only when there is something to write. ``on_branch(branch)`` is called as soon as the branch is known, so
    the caller can record it even if a later step fails.

    Writing into a chosen branch is the user's own edit of it: every object written is checked against their
    permissions, and no change request is opened (the branch has its own review, if any).
    """
    if user is None:
        # NetBox's change log, which is what puts changes into a branch, needs a user to record.
        raise branching.BranchError("imports into a branch need a user: set import_user for scheduled imports")
    branch = target if target is not None else branching.open_import_branch(windows_server, logger=logger)
    try:
        return _write_branch(windows_server, state, user, branch, logger, on_branch, chosen=target is not None)
    finally:
        if branch is not None:
            branching.close_connection(branch)


def _write_branch(windows_server, state, user, branch, logger, on_branch, chosen=False):
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
        result.branch_created = True
    else:
        logger.info(f"Updating branch {branch.name}")
    result.branch = branch
    try:
        return _fill_branch(windows_server, user, branch, result, logger, on_branch, chosen)
    finally:
        branching.close_connection(branch)


def _fill_branch(windows_server, user, branch, result, logger, on_branch, chosen=False):
    changes = result.changes
    if on_branch is not None:
        on_branch(branch)
    if chosen:
        # Reading Windows takes a while: the user's branch may have started merging, or been archived, since the job
        # checked it.
        branch.refresh_from_db()
        if not branching.branch_ready(branch):
            raise branching.BranchError(f"branch {branch.name} is no longer ready: nothing was written")

    from ..ipam_guard import import_request_id
    from ..signals import push_suppressed

    # push_suppressed: an import writes what Windows already holds, so the IPAM edit guard does not apply (and saves
    # in a branch queue no push anyway). The request ID marks these changes as the import's, so merging the branch
    # does not check them either.
    with push_suppressed(), branching.writing(branch, user, request_id=import_request_id()):
        writer = Writer(windows_server, user=user if chosen else None)
        for change in changes:
            try:
                with _one_change(), branching.atomic(branch):
                    writer.apply(change)
            except APPLY_ERRORS as exc:
                message = f"{change.action} {change.kind} {change.key}: {_message(exc)}"
                result.failed.append(message)
                logger.warning(message)
            else:
                result.applied += 1

    if not chosen and get_setting("import_change_request") and branching.change_control_installed():
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


# -- with branching, chosen on purpose: straight into main ---------------------------------------------------------


def import_to_main(windows_server, state, user, logger=log):
    """
    Write the differences straight into main, as ``user``, without a branch or a proposal (issue #36).

    Main is used whatever branch is active. Every object written is checked against the user's permissions, and
    change control's protect_main judges each save as it would the user's own edit; a refused change is rolled back
    and reported, the others stay. The saves queue no push (NetBox then matches Windows) and the written objects are
    recorded as managed, as when applying a proposal.
    """
    from ..signals import push_suppressed

    if user is None:
        raise branching.BranchError("imports into main need a user: the change log records who imported")
    with branching.on_main():
        changes, notes = compute(windows_server, state)
    result = ImportResult(changes=changes, notes=notes, direct=True)
    if not changes:
        return result

    logger.info(f"Writing {len(changes)} difference(s) into main as {user}")
    with push_suppressed(), branching.writing_main(user):
        writer = Writer(windows_server, user=user)
        for change in changes:
            try:
                with _one_change(), transaction.atomic():
                    writer.apply(change)
                    _record_managed(windows_server, change)
            except APPLY_ERRORS as exc:
                message = f"{change.action} {change.kind} {change.key}: {_message(exc)}"
                result.failed.append(message)
                logger.warning(message)
            else:
                result.applied += 1
    return result
