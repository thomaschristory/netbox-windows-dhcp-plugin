"""
Where an import went, for the Import panel on the Windows server and sync run pages (issues #32 and #36).

With netbox-branching an import writes into an import branch unless the user chose main, so a finished import must
say which branch (and change request) holds the result, or that it wrote into main, and one still queued or running
must not look finished. Without branching the panel points at the import proposal instead and says nothing about
branches.

Everything is read as the viewing user: a run, job, branch or change request they may not view is left out.
"""

from dataclasses import dataclass

from core.choices import JobStatusChoices
from django.utils import timezone

from .choices import ImportDestinationChoices, ProposalStatusChoices, SyncKindChoices, SyncStatusChoices
from .integrations import branching
from .models import ImportProposal, SyncRun

QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

# netbox-branching Branch statuses in which the branch's changes may already be in main.
_MERGED_STATUSES = ("merging", "reverting", "merged", "archived")
# Statuses of a branch whose merge has finished (an archived branch is a merged one whose schema was dropped).
_DONE_STATUSES = ("merged", "archived")

# What became of an import's branch (issue #44), for the Import panel's wording.
OPEN = "open"
MERGING = "merging"
REVERTING = "reverting"
MERGED = "merged"
# The branch exists but the user may not view it, so its state is unknown to them.
HIDDEN = "hidden"
DELETED_MERGED = "deleted-merged"
DELETED_UNMERGED = "deleted-unmerged"
# Gone, and no deletion record the user may view says how it ended: never read this as merged.
GONE = "gone"


@dataclass
class BranchInfo:
    name: str
    branch: object = None
    change_request: object = None
    # The branch is still there, even if the user may not view it.
    exists: bool = False
    # For a deleted branch: its deletion in the change log, and its status and merge time just before it went.
    deletion: object = None
    deleted_status: str | None = None
    deleted_merged_time: object = None

    @property
    def main_unchanged(self):
        """True while the branch still exists unmerged, so its changes are not in main."""
        return self.branch is not None and self.branch.status not in _MERGED_STATUSES

    @property
    def lifecycle(self):
        if self.branch is not None:
            if self.branch.status in _DONE_STATUSES:
                return MERGED
            if self.branch.status in (MERGING, REVERTING):
                return self.branch.status
            return OPEN
        if self.exists:
            return HIDDEN
        if self.deletion is None or self.deleted_status in (MERGING, REVERTING, None):
            return GONE
        return DELETED_MERGED if self.deleted_status in _DONE_STATUSES else DELETED_UNMERGED

    @property
    def merged_time(self):
        if self.branch is not None:
            return getattr(self.branch, "merged_time", None) if self.lifecycle == MERGED else None
        return self.deleted_merged_time if self.lifecycle == DELETED_MERGED else None


@dataclass
class ImportStatus:
    # The state of ``run``, or None when there is no run yet.
    state: str | None = None
    run: SyncRun | None = None
    job: object = None
    branch: BranchInfo | None = None
    proposal: object = None
    # The run's proposal exists but the user may not view it. Only then does the panel say so: a run can also lose
    # its proposal because a later import took the open proposal over, or because someone deleted it.
    proposal_hidden: bool = False
    # When the run's own proposal is not linked any more: the server's open proposal, if the user may view it.
    open_proposal: object = None
    # Where the run wrote (ImportDestinationChoices), or None when there is no run yet.
    destination: str | None = None
    # A newer import job that has not started its run yet: QUEUED or RUNNING (the run is created a moment after the
    # worker picks the job up), or FAILED when the job ended without recording one (a timeout while queued, say).
    next_state: str | None = None
    next_job: object = None
    # Where that newer job will write, recorded when it was queued.
    next_destination: str | None = None
    # When the latest run wrote no branch (it failed, or found nothing new), the open branch of an earlier run.
    earlier_branch: BranchInfo | None = None

    # The branch that newer job will write into, when the user chose one.
    next_branch: str | None = None

    @property
    def uses_branching(self):
        return self.destination in (ImportDestinationChoices.BRANCH, ImportDestinationChoices.CURRENT)

    @property
    def chosen_branch(self):
        """The run wrote into a branch the user chose, not an import branch of its own."""
        return self.destination == ImportDestinationChoices.CURRENT

    @property
    def direct(self):
        return self.destination == ImportDestinationChoices.MAIN

    @property
    def applied(self):
        """How many changes the run wrote, or None when it did not record it (it stopped before finishing)."""
        return (self.run.summary or {}).get("applied") if self.run is not None else None

    @property
    def pending(self):
        """Queued or running: the server page panel refreshes itself until the import has finished."""
        return self.state in (QUEUED, RUNNING) or self.next_state in (QUEUED, RUNNING)

    @property
    def changes(self):
        return (self.run.summary or {}).get("changes") if self.run is not None else None

    @property
    def review(self):
        """
        Differences a proposal import sent to review, or None when that is not known. Runs from before issue #43
        did not record it: every difference went to review then unless a reviewer had dismissed it, so only a run
        still linked to a proposal (visible or not) can be read as "all of them".
        """
        if self.run is None:
            return None
        summary = self.run.summary or {}
        if "review" in summary:
            return summary["review"]
        if self.proposal is not None or self.proposal_hidden:
            return summary.get("changes")
        return None

    @property
    def dismissed(self):
        """Differences a proposal import left out because a reviewer had already dismissed the same change."""
        return ((self.run.summary or {}).get("dismissed") or 0) if self.run is not None else 0

    @property
    def branch_created(self):
        return bool((self.run.summary or {}).get("branch_created")) if self.run is not None else False


def _abandoned(created, now):
    """
    A job or run this old that still claims to be queued or running has lost its worker (or never reached one): the
    same cut-off after which jobs.enqueue_import queues a new import, so the panel stops waiting for it.
    """
    from .jobs import ABANDONED_JOB

    return created < now - ABANDONED_JOB


def _job_state(job, now):
    if job.status in JobStatusChoices.ENQUEUED_STATE_CHOICES:
        if _abandoned(job.created, now):
            return FAILED
        return RUNNING if job.status == JobStatusChoices.STATUS_RUNNING else QUEUED
    if job.status in (JobStatusChoices.STATUS_ERRORED, JobStatusChoices.STATUS_FAILED):
        return FAILED
    # Completed, so it did record a run, which someone has since deleted: nothing to say about it.
    return None


def _job_destination(job):
    """Where a queued job will write; jobs queued before #36 recorded nothing and use the default."""
    destination = (job.data or {}).get("destination") if isinstance(job.data, dict) else None
    return destination or branching.import_destinations()[0]


def _run_destination(run, has_proposal):
    """Where a run wrote. Runs from before #36 recorded nothing: a branch name or a proposal says which it was."""
    if run.destination:
        return run.destination
    if run.branch_name:
        return ImportDestinationChoices.BRANCH
    if has_proposal or not branching.branching_installed():
        return ImportDestinationChoices.PROPOSAL
    return ImportDestinationChoices.BRANCH


def _branch(name, user, since=None):
    """What became of the branch an import wrote into. ``since``: when the run started, so an older deleted branch of
    the same name is not taken for this one."""
    if not name:
        return None
    branch, change_request, exists = branching.import_branch(name, user)
    info = BranchInfo(name=name, branch=branch, change_request=change_request, exists=exists)
    if not exists:
        deletion = branching.branch_deletion(name, user, since=since)
        if deletion is not None:
            info.deletion, info.deleted_status, info.deleted_merged_time = deletion
    return info


def for_run(run, user, job=None, now=None):
    """The status of one import run. ``job`` defaults to the run's own job."""
    now = now or timezone.now()
    job = job if job is not None else run.job
    if run.status in (SyncStatusChoices.PENDING, SyncStatusChoices.RUNNING) and (
        job is None or job.status == JobStatusChoices.STATUS_RUNNING
    ):
        state = FAILED if _abandoned(run.created, now) else RUNNING
    elif run.status == SyncStatusChoices.COMPLETED:
        state = COMPLETED
    else:
        # Failed, or a run whose job ended without finishing it (the worker stopped, for example).
        state = FAILED
    proposal = None
    hidden = False
    open_proposal = None
    if not run.branch_name and run.destination != ImportDestinationChoices.MAIN:
        proposal = (
            ImportProposal.objects.restrict(user, "view")
            .filter(sync_run=run)
            .select_related("windows_server__dhcp_server")
            .order_by("-pk")
            .first()
        )
        if proposal is None and (run.summary or {}).get("changes"):
            # Nothing visible, yet the run found differences. Tell a proposal hidden by permissions apart from one
            # that is no longer this run's: a repeat import takes the open proposal over, and proposals can be
            # deleted. Only the first may be called "a proposal you may not view".
            hidden = ImportProposal.objects.filter(sync_run=run).exists()
            if not hidden:
                open_proposal = (
                    ImportProposal.objects.restrict(user, "view")
                    .filter(windows_server_id=run.windows_server_id, status=ProposalStatusChoices.OPEN)
                    .order_by("-pk")
                    .first()
                )
    return ImportStatus(
        state=state,
        run=run,
        job=job,
        branch=_branch(run.branch_name, user, since=run.created),
        proposal=proposal,
        proposal_hidden=hidden,
        open_proposal=open_proposal,
        destination=_run_destination(run, proposal is not None or hidden),
    )


def for_server(server, user, now=None):
    """The server's latest import run, and any newer import job still to start one. None if neither exists."""
    from core.models import Job
    from django.contrib.contenttypes.models import ContentType

    from .jobs import ImportJob

    now = now or timezone.now()
    runs = (
        SyncRun.objects.restrict(user, "view")
        .filter(windows_server=server, kind=SyncKindChoices.IMPORT)
        .select_related("job", "windows_server__dhcp_server")
        .order_by("-created")
    )
    run = runs.first()
    jobs = Job.objects.restrict(user, "view").filter(
        object_type=ContentType.objects.get_for_model(server), object_id=server.pk, name=ImportJob.name
    )
    if run is not None:
        jobs = jobs.filter(created__gt=run.job.created if run.job is not None else run.created)
        if run.job_id is not None:
            jobs = jobs.exclude(pk=run.job_id)
    next_job = jobs.order_by("-created").first()
    next_state = _job_state(next_job, now) if next_job is not None else None
    if next_state is None:
        next_job = None
    if run is None and next_job is None:
        return None
    status = for_run(run, user, now=now) if run is not None else ImportStatus()
    status.next_job, status.next_state = next_job, next_state
    if next_job is not None:
        status.next_destination = _job_destination(next_job)
        if isinstance(next_job.data, dict):
            status.next_branch = next_job.data.get("branch")
    if branching.branching_installed() and (status.branch is None or not status.branch.main_unchanged):
        # Keep an unmerged branch from an earlier run in sight after a run that failed or found nothing new.
        # Only import branches: a branch the user chose to import into is their own work, not an import to merge.
        earlier = runs.exclude(branch_name="").exclude(destination=ImportDestinationChoices.CURRENT)
        if status.branch is not None:
            earlier = earlier.exclude(branch_name=status.branch.name)
        earlier = earlier.values_list("branch_name", "created").first()
        info = _branch(earlier[0], user, since=earlier[1]) if earlier else None
        if info is not None and info.main_unchanged:
            status.earlier_branch = info
    return status
