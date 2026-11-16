"""
Sync run retention (issue #106). Every import, push, dry run and connection test leaves a sync run; with scheduled
imports that is tens of thousands of rows a year per server. The import scheduler, which runs every minute, deletes
the runs older than ``sync_run_retention_days`` a batch at a time, oldest first.

Some old runs are still read by the plugin, or carry a journal someone wrote, and are never deleted, whatever their
age (see ``protected_run_ids`` and ``prune_sync_runs``).
"""

import logging
from datetime import timedelta

from django.utils import timezone

from .choices import ProposalStatusChoices, SyncKindChoices, SyncStatusChoices
from .conf import get_setting

log = logging.getLogger(__name__)

__all__ = (
    "PRUNE_BATCH",
    "check_setting",
    "protected_run_ids",
    "prune_sync_runs",
    "prune_when_due",
    "retention_days",
)

# At most this many runs are deleted per scheduler tick. After an upgrade the first ticks may find a year of
# history: one huge delete would hold the scheduler, and locks on the table, for too long.
PRUNE_BATCH = 500
# A tick that deletes less than a full batch has caught up. Looking again every minute would only repeat the same
# lookups for nothing, so the next look is an hour later. Shared through NetBox's cache (Redis) by all workers.
IDLE_CACHE_KEY = "netbox_windows_dhcp_plugin:sync_run_prune_idle"
IDLE_SECONDS = 3600
# After a failed tick (a bad setting, a protection rule, a database error) the next try waits this long, so an error
# that persists is logged four times an hour, not every minute.
FAILURE_SECONDS = 900
# A hundred years. Far beyond any useful history, and far below the dates Python can count back to: a larger value
# would fail with an OverflowError on every tick instead of a clear message.
MAX_DAYS = 36500


def retention_days():
    """The ``sync_run_retention_days`` setting in days, or None when every run is kept (0 or None)."""
    value = get_setting("sync_run_retention_days")
    if value is None or value == 0:
        return None
    # A typo must not delete history it was meant to keep: refuse anything but a positive whole number of days.
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_DAYS:
        raise ValueError(
            f"sync_run_retention_days must be a whole number of days from 1 to {MAX_DAYS} (0 keeps every run), "
            f"not {value!r}"
        )
    return value


def check_setting():
    """At startup: say clearly when the setting is invalid, rather than only in the scheduler's job log."""
    try:
        retention_days()
    except ValueError as exc:
        log.warning("%s; no sync run is deleted until it is fixed", exc)


def protected_run_ids(server_ids, now=None):
    """
    The runs of these servers that must be kept however old they are, because the plugin still reads them:

    - the newest run of each kind (import, push, dry run, connection test). The import scheduler times the next
      import from the newest import, and the server page's Import panel describes it;
    - the newest push that reached Windows: the next push reads the scopes still to replicate to a failover partner
      from it (``pusher.replication_runs``);
    - with netbox-branching, the newest run of each of the two newest import branches: the Import panel still shows
      an earlier import branch waiting to be merged after a run that wrote none (``import_status.import_branch_runs``);
    - runs an open import proposal belongs to: the proposal and the panel link them;
    - runs whose job is still queued or running, and not abandoned: the job finishes them.
    """
    from core.choices import JobStatusChoices

    from .import_status import import_branch_runs
    from .integrations.branching import branching_installed
    from .jobs import ABANDONED_JOB
    from .models import ImportProposal, SyncRun
    from .sync.pusher import replication_runs

    now = now or timezone.now()
    # Without netbox-branching no import writes a branch, and the panel never looks for one.
    branching = branching_installed()
    keep = set()
    # A few small lookups per server. Each reads the newest row of a (server, kind) pair, from the
    # (windows_server, kind, -created) index or, for the replication run, the (windows_server, kind, -completed) one.
    # The second import branch lookup may walk the server's runs of the newest branch to find a different name.
    for server_id in server_ids:
        runs = SyncRun.objects.filter(windows_server_id=server_id).order_by("-created", "-pk")
        for kind in SyncKindChoices.values():
            keep.add(runs.filter(kind=kind).values_list("pk", flat=True).first())
        keep.add(replication_runs(server_id).values_list("pk", flat=True).first())
        if branching:
            branch_runs = import_branch_runs(runs.filter(kind=SyncKindChoices.IMPORT))
            newest = branch_runs.values_list("pk", "branch_name").first()
            if newest is not None:
                keep.add(newest[0])
                keep.add(branch_runs.exclude(branch_name=newest[1]).values_list("pk", flat=True).first())
    keep.update(
        ImportProposal.objects.filter(
            windows_server_id__in=server_ids, status=ProposalStatusChoices.OPEN, sync_run__isnull=False
        ).values_list("sync_run_id", flat=True)
    )
    # The same cut-off as the import queue (jobs._busy_jobs): a job still "running" long after it started was
    # abandoned (its worker was killed), and nothing will ever finish its run. The Import panel calls it failed too.
    keep.update(
        SyncRun.objects.filter(
            windows_server_id__in=server_ids,
            status__in=(SyncStatusChoices.PENDING, SyncStatusChoices.RUNNING),
            job__status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES,
            job__created__gte=now - ABANDONED_JOB,
        ).values_list("pk", flat=True)
    )
    keep.discard(None)
    return keep


def prune_sync_runs(now=None, batch=PRUNE_BATCH):
    """Delete up to ``batch`` runs older than the retention period, oldest first. Returns how many were deleted."""
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Exists, OuterRef
    from extras.models import JournalEntry

    from .models import SyncRun, WindowsServer

    days = retention_days()
    if days is None:
        return 0
    now = now or timezone.now()
    old = SyncRun.objects.filter(created__lt=now - timedelta(days=days))
    # One indexed look first: when nothing is old enough, skip the per-server lookups.
    if not old.exists():
        return 0
    keep = protected_run_ids(list(WindowsServer.objects.values_list("pk", flat=True)), now=now)
    # A journal entry is something a person chose to keep. Deleting the run would delete it with no trace (a job
    # writes no change log), so such runs stay until someone deletes the entry or the run.
    journaled = JournalEntry.objects.filter(
        assigned_object_type=ContentType.objects.get_for_model(SyncRun), assigned_object_id=OuterRef("pk")
    )
    ids = list(
        old.exclude(pk__in=keep)
        .exclude(Exists(journaled))
        .order_by("created", "pk")
        .values_list("pk", flat=True)[:batch]
    )
    if ids:
        # The runs' push triggers, tags and bookmarks go with them; closed import proposals lose the link.
        SyncRun.objects.filter(pk__in=ids).delete()
    return len(ids)


def _skip_for(cache, seconds):
    try:
        cache.set(IDLE_CACHE_KEY, True, seconds)
    except Exception:
        # Without the cache the next tick simply looks again: slower, never wrong.
        log.warning("could not store the sync run retention pause in the cache", exc_info=True)


def prune_when_due(now=None):
    """
    ``prune_sync_runs``, unless an earlier tick within the last hour found nothing more to delete, or one within the
    last 15 minutes failed. Errors are raised again for the caller to log.
    """
    from django.core.cache import cache

    try:
        paused = cache.get(IDLE_CACHE_KEY)
    except Exception:
        log.warning("could not read the sync run retention pause from the cache", exc_info=True)
        paused = False
    if paused:
        return 0
    try:
        deleted = prune_sync_runs(now)
    except Exception:
        _skip_for(cache, FAILURE_SECONDS)
        raise
    if deleted < PRUNE_BATCH:
        _skip_for(cache, IDLE_SECONDS)
    return deleted
