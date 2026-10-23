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

from .choices import ImportDestinationChoices, SyncKindChoices, SyncStatusChoices
from .integrations import branching
from .models import ImportProposal, SyncRun

QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

# netbox-branching Branch statuses in which the branch's changes may already be in main.
_MERGED_STATUSES = ("merging", "reverting", "merged", "archived")


@dataclass
class BranchInfo:
    name: str
    branch: object = None
    change_request: object = None
    # The branch is still there, even if the user may not view it.
    exists: bool = False

    @property
    def main_unchanged(self):
        """True while the branch still exists unmerged, so its changes are not in main."""
        return self.branch is not None and self.branch.status not in _MERGED_STATUSES


@dataclass
class ImportStatus:
    # The state of ``run``, or None when there is no run yet.
    state: str | None = None
    run: SyncRun | None = None
    job: object = None
    branch: BranchInfo | None = None
    proposal: object = None
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
        Differences a proposal import sent to review. Runs from before issue #43 did not record it: every
        difference went to review then, unless a dismissal hid it, which those runs cannot tell.
        """
        if self.run is None:
            return None
        summary = self.run.summary or {}
        return summary.get("review", summary.get("changes"))

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


def _run_destination(run, proposal):
    """Where a run wrote. Runs from before #36 recorded nothing: a branch name or a proposal says which it was."""
    if run.destination:
        return run.destination
    if run.branch_name:
        return ImportDestinationChoices.BRANCH
    if proposal is not None or not branching.branching_installed():
        return ImportDestinationChoices.PROPOSAL
    return ImportDestinationChoices.BRANCH


def _branch(name, user):
    if not name:
        return None
    branch, change_request, exists = branching.import_branch(name, user)
    return BranchInfo(name=name, branch=branch, change_request=change_request, exists=exists)


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
    if not run.branch_name and run.destination != ImportDestinationChoices.MAIN:
        proposal = (
            ImportProposal.objects.restrict(user, "view")
            .filter(sync_run=run)
            .select_related("windows_server__dhcp_server")
            .order_by("-pk")
            .first()
        )
    return ImportStatus(
        state=state,
        run=run,
        job=job,
        branch=_branch(run.branch_name, user),
        proposal=proposal,
        destination=_run_destination(run, proposal),
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
        earlier = earlier.values_list("branch_name", flat=True).first()
        info = _branch(earlier, user)
        if info is not None and info.main_unchanged:
            status.earlier_branch = info
    return status
