"""
Where an import went, for the Import panel on the Windows server and sync run pages (issue #32).

With netbox-branching an import writes into an import branch, never into main, so a finished import must say which
branch (and change request) holds the result, and one still queued or running must not look finished. Without
branching the panel points at the import proposal instead and says nothing about branches.
"""

from dataclasses import dataclass

from core.choices import JobStatusChoices

from .choices import SyncKindChoices, SyncStatusChoices
from .integrations import branching
from .models import SyncRun

QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"


@dataclass
class ImportStatus:
    # The state of ``run``, or None when there is no run yet.
    state: str | None = None
    run: SyncRun | None = None
    job: object = None
    branch_name: str = ""
    branch: object = None
    change_request: object = None
    proposal: object = None
    uses_branching: bool = False
    # A newer import job that has not started its run yet: QUEUED or RUNNING (the run is created a moment after the
    # worker picks the job up), or FAILED when the job ended without recording one (a timeout while queued, say).
    next_state: str | None = None
    next_job: object = None

    @property
    def pending(self):
        """Queued or running: the server page panel refreshes itself until the import has finished."""
        return self.state in (QUEUED, RUNNING) or self.next_state in (QUEUED, RUNNING)

    @property
    def main_unchanged(self):
        """True while the import branch still exists unmerged, so its changes are not in main."""
        # The status values of netbox-branching's Branch: merging, merged and archived ones may have reached main.
        return self.branch is not None and self.branch.status not in ("merging", "reverting", "merged", "archived")

    @property
    def changes(self):
        return (self.run.summary or {}).get("changes") if self.run is not None else None


def _job_state(job):
    if job.status in (JobStatusChoices.STATUS_PENDING, JobStatusChoices.STATUS_SCHEDULED):
        return QUEUED
    if job.status == JobStatusChoices.STATUS_RUNNING:
        return RUNNING
    if job.status in (JobStatusChoices.STATUS_ERRORED, JobStatusChoices.STATUS_FAILED):
        return FAILED
    # Completed, so it did record a run, which someone has since deleted: nothing to say about it.
    return None


def for_run(run, job=None):
    """The status of one import run. ``job`` defaults to the run's own job."""
    job = job if job is not None else run.job
    if run.status in (SyncStatusChoices.PENDING, SyncStatusChoices.RUNNING) and (
        job is None or job.status == JobStatusChoices.STATUS_RUNNING
    ):
        state = RUNNING
    elif run.status == SyncStatusChoices.COMPLETED:
        state = COMPLETED
    else:
        # Failed, or a run whose job ended without finishing it (the worker stopped, for example).
        state = FAILED
    branch, change_request = branching.import_branch(run.branch_name)
    return ImportStatus(
        state=state,
        run=run,
        job=job,
        branch_name=run.branch_name,
        branch=branch,
        change_request=change_request,
        proposal=None if run.branch_name else run.import_proposals.order_by("-pk").first(),
        # A run recorded with a branch went into one, whatever is installed now.
        uses_branching=bool(run.branch_name) or branching.branching_installed(),
    )


def for_server(server):
    """The server's latest import run, and any newer import job still to start one. None if neither exists."""
    from core.models import Job
    from django.contrib.contenttypes.models import ContentType

    from .jobs import ImportJob

    run = (
        SyncRun.objects.filter(windows_server=server, kind=SyncKindChoices.IMPORT)
        .select_related("job")
        .order_by("-created")
        .first()
    )
    jobs = Job.objects.filter(
        object_type=ContentType.objects.get_for_model(server), object_id=server.pk, name=ImportJob.name
    )
    if run is not None:
        jobs = jobs.filter(created__gt=run.job.created if run.job is not None else run.created)
        if run.job_id is not None:
            jobs = jobs.exclude(pk=run.job_id)
    next_job = jobs.order_by("-created").first()
    next_state = _job_state(next_job) if next_job is not None else None
    if next_state is None:
        next_job = None
    if run is None and next_job is None:
        return None
    status = for_run(run) if run is not None else ImportStatus(uses_branching=branching.branching_installed())
    status.next_job, status.next_state = next_job, next_state
    return status
