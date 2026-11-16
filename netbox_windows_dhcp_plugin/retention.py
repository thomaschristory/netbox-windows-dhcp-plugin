"""
Sync run retention (issue #106). Every import, push, dry run and connection test leaves a sync run; with scheduled
imports that is tens of thousands of rows a year per server. The import scheduler, which runs every minute, deletes
the runs older than ``sync_run_retention_days`` a batch at a time, oldest first.

Some old runs are still read by the plugin and are never deleted, whatever their age (see ``protected_run_ids``).
"""

import logging
from datetime import timedelta

from django.utils import timezone

from .choices import ProposalStatusChoices, SyncKindChoices, SyncStatusChoices
from .conf import get_setting

log = logging.getLogger(__name__)

__all__ = ("PRUNE_BATCH", "protected_run_ids", "prune_sync_runs", "prune_when_due", "retention_days")

# At most this many runs are deleted per scheduler tick. After an upgrade the first ticks may find a year of
# history: one huge delete would hold the scheduler, and locks on the table, for too long.
PRUNE_BATCH = 500
# A tick that deletes less than a full batch has caught up. Looking again every minute would only repeat the same
# lookups for nothing, so the next look is an hour later. Shared through NetBox's cache (Redis) by all workers.
IDLE_CACHE_KEY = "netbox_windows_dhcp_plugin:sync_run_prune_idle"
IDLE_SECONDS = 3600


def retention_days():
    """The ``sync_run_retention_days`` setting in days, or None when every run is kept (0 or None)."""
    value = get_setting("sync_run_retention_days")
    if value is None or value == 0:
        return None
    # A typo must not delete history it was meant to keep: refuse anything but a positive whole number of days.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"sync_run_retention_days must be a whole number of days (0 keeps every run), not {value!r}")
    return value


def protected_run_ids(server_ids):
    """
    The runs of these servers that must be kept however old they are, because the plugin still reads them:

    - the newest run of each kind (import, push, dry run, connection test). The import scheduler times the next
      import from the newest import, and the server page's Import panel describes it;
    - the newest push that reached Windows: the next push reads the scopes still to replicate to a failover partner
      from it (``pusher.replication_runs``);
    - the newest run of each of the two newest import branches: the Import panel still shows an earlier import
      branch waiting to be merged after a run that wrote none (``import_status.import_branch_runs``);
    - runs an open import proposal belongs to: the proposal and the panel link them;
    - runs whose job is still queued or running: the job finishes them.
    """
    from core.choices import JobStatusChoices

    from .import_status import import_branch_runs
    from .models import ImportProposal, SyncRun
    from .sync.pusher import replication_runs

    keep = set()
    # A few small lookups per server, each served by the (windows_server, kind, -created) index.
    for server_id in server_ids:
        runs = SyncRun.objects.filter(windows_server_id=server_id).order_by("-created", "-pk")
        for kind in SyncKindChoices.values():
            keep.add(runs.filter(kind=kind).values_list("pk", flat=True).first())
        keep.add(replication_runs(server_id).values_list("pk", flat=True).first())
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
    # A run left "running" by a job that ended without finishing it (a killed worker) is not kept: nothing will
    # ever finish it, and the Import panel already calls it failed.
    keep.update(
        SyncRun.objects.filter(
            windows_server_id__in=server_ids,
            status__in=(SyncStatusChoices.PENDING, SyncStatusChoices.RUNNING),
            job__status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES,
        ).values_list("pk", flat=True)
    )
    keep.discard(None)
    return keep


def prune_sync_runs(now=None, batch=PRUNE_BATCH):
    """Delete up to ``batch`` runs older than the retention period, oldest first. Returns how many were deleted."""
    from .models import SyncRun

    days = retention_days()
    if days is None:
        return 0
    now = now or timezone.now()
    old = SyncRun.objects.filter(created__lt=now - timedelta(days=days))
    # order_by() drops the model's ordering, which DISTINCT would otherwise apply to as well.
    server_ids = set(old.order_by().values_list("windows_server_id", flat=True).distinct())
    if not server_ids:
        return 0
    keep = protected_run_ids(server_ids)
    ids = list(old.exclude(pk__in=keep).order_by("created", "pk").values_list("pk", flat=True)[:batch])
    if ids:
        # The runs' push triggers go with them; import proposals (closed ones, open ones are kept) lose the link.
        SyncRun.objects.filter(pk__in=ids).delete()
    return len(ids)


def prune_when_due(now=None):
    """``prune_sync_runs``, unless an earlier tick within the last hour found nothing more to delete."""
    from django.core.cache import cache

    if cache.get(IDLE_CACHE_KEY):
        return 0
    deleted = prune_sync_runs(now)
    if deleted < PRUNE_BATCH:
        cache.set(IDLE_CACHE_KEY, True, IDLE_SECONDS)
    return deleted
