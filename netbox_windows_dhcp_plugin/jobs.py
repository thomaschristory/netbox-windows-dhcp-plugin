from contextlib import contextmanager
from datetime import timedelta

from core.choices import JobNotificationChoices
from core.exceptions import JobFailed
from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone
from netbox.jobs import JobRunner, system_job

from .choices import ImportDestinationChoices, SyncKindChoices, SyncStatusChoices
from .conf import CredentialError, get_setting
from .connection import build_client
from .integrations.branching import job_kwargs as branching_job_kwargs
from .models import SyncRun, WindowsServer
from .windows.psscript import WindowsLayerError


def record_names(server, client, logger):
    """
    Keep the names the DHCP server reports for itself: on one side of a failover relationship Windows names the
    partner by its computer name, and imports and pushes of its partners match on them (#57).
    """
    try:
        names = client.get_server_names()
    except WindowsLayerError as exc:
        # Only partner matching uses the names, so this does not fail the job that is reading the server.
        logger.warning(f"Could not read the names of {server.host}: {exc}")
        return
    if names and names != server.reported_names:
        # An update, not save(): the names are read from Windows, not an edit worth a change log entry.
        WindowsServer.objects.filter(pk=server.pk).update(reported_names=names)
        server.reported_names = names


class TestConnectionJob(JobRunner):
    """Connect to a Windows server, check the DhcpServer module and count its scopes."""

    class Meta:
        name = "Windows DHCP connection test"

    # Tests substitute a fake runner here.
    runner_factory = None

    def run(self, *args, **kwargs):
        server = self.job.object
        sync_run = SyncRun.objects.create(
            windows_server=server,
            kind=SyncKindChoices.TEST,
            status=SyncStatusChoices.RUNNING,
            job=self.job,
            started=timezone.now(),
        )
        runner = self.runner_factory(server) if self.runner_factory else None
        client = None
        try:
            client = build_client(server, runner=runner)
            client.check_available()
            scopes = client.list_scopes()
            record_names(server, client, self.logger)
        # ValueError covers settings the transports reject, such as a profile without a password or a malformed
        # SSH host key.
        except (CredentialError, WindowsLayerError, ValueError) as exc:
            sync_run.status = SyncStatusChoices.FAILED
            sync_run.error = str(exc)
            self.logger.error(f"Connection to {server.host} failed: {exc}")
        else:
            sync_run.status = SyncStatusChoices.COMPLETED
            sync_run.summary = {"ipv4_scopes": len(scopes), "reported_names": server.reported_names}
            self.logger.info(f"Connected to {server.host}: {len(scopes)} IPv4 scopes")
        finally:
            if sync_run.status == SyncStatusChoices.RUNNING:
                # Anything else is a bug; record the type only and let the traceback reach the job log.
                sync_run.status = SyncStatusChoices.FAILED
                sync_run.error = "unexpected error, see the job log"
            if client is not None:
                client.close()
            sync_run.completed = timezone.now()
            sync_run.save()
        if sync_run.status == SyncStatusChoices.FAILED:
            raise JobFailed(sync_run.error)


class LeaseJob(JobRunner):
    """
    Read the live leases of one scope into the job's data. The job belongs to the Windows server (netbox-plugin-dhcp
    subnets cannot hold jobs); ``data["subnet"]`` says which subnet it read, from the moment it is queued.
    """

    class Meta:
        name = "Windows DHCP leases"

    # Tests substitute a fake runner here.
    runner_factory = None

    subnet_id = None

    def run(self, *args, subnet_id=None, **kwargs):
        from netbox_dhcp.models import Subnet

        from . import leases
        from .windows.psscript import NotFound

        self.subnet_id = subnet_id
        server = self.job.object
        subnet = Subnet.objects.select_related("prefix").filter(pk=subnet_id).first()
        if subnet is None:
            self._fail("the subnet no longer exists")
        if server is None or leases.windows_server_for(subnet) != server:
            self._fail(f"{subnet} no longer belongs to this Windows server")
        family, scope = leases.scope_of(subnet)
        runner = self.runner_factory(server) if self.runner_factory else None
        client = None
        try:
            client = build_client(server, runner=runner)
            found = client.list_v6_leases(scope) if family == 6 else client.list_leases(scope)
        except NotFound:
            self._fail(f"scope {scope} is not on {server.host}")
        except (CredentialError, WindowsLayerError, ValueError) as exc:
            self._fail(f"reading leases from {server.host} failed: {exc}")
        finally:
            if client is not None:
                client.close()
        kept, total = leases.rows(found, family)
        self._forget_earlier_reads(server)
        self.job.data = {
            "subnet": subnet_id,
            "server": str(server),
            "scope": scope,
            "family": family,
            "total": total,
            "truncated": total > len(kept),
            "leases": kept,
        }
        self.logger.info(f"Read {total} lease(s) of scope {scope} from {server.host}")

    def _forget_earlier_reads(self, server):
        # Only the newest read is shown, so earlier ones (up to 5000 leases each) are not kept in the job table.
        from core.choices import JobStatusChoices

        earlier = LeaseJob.get_jobs(server).filter(data__subnet=self.subnet_id).exclude(pk=self.job.pk)
        earlier.filter(status__in=JobStatusChoices.TERMINAL_STATE_CHOICES).delete()

    def _fail(self, message):
        # Job.terminate saves the job, data included: the page shows the reason without reading the job log.
        if self.job.object is not None and self.subnet_id is not None:
            self._forget_earlier_reads(self.job.object)
        self.job.data = {"subnet": self.subnet_id, "error": message}
        self.logger.error(message)
        raise JobFailed(message)


def lease_jobs(subnet, server):
    """Lease reads of this subnet, newest first."""
    return LeaseJob.get_jobs(server).filter(data__subnet=subnet.pk).order_by("-created")


def lease_read_in_progress(job, now=None):
    """
    Whether a lease job is still waiting or running. One overdue by ORPHANED_JOB never reached a worker, or lost it:
    it no longer counts, so the page stops waiting for it and a new read can be queued.
    """
    from core.choices import JobStatusChoices

    if job is None or job.status not in JobStatusChoices.ENQUEUED_STATE_CHOICES:
        return False
    return job.created >= (now or timezone.now()) - ORPHANED_JOB


# Advisory lock (with the subnet's key) taken while deciding whether to queue a lease read.
_LEASE_LOCK_CLASS = 0x57444C53  # "WDLS"


def enqueue_leases(subnet, user):
    """Queue a lease read for the subnet unless one is already waiting or running. Returns the job, or None."""
    from django.db import transaction

    from . import leases

    server = leases.windows_server_for(subnet)
    if server is None:
        return None
    # RQ only gets the job once this transaction commits, so the subnet is recorded before the job can start; the
    # lock makes two clicks at the same moment queue one read.
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_LEASE_LOCK_CLASS, subnet.pk])
        if lease_read_in_progress(lease_jobs(subnet, server).first()):
            return None
        job = LeaseJob.enqueue(
            instance=server,
            user=user,
            # Never notify: the person who asked is watching the page, which updates itself.
            notifications=JobNotificationChoices.NOTIFICATION_NEVER,
            subnet_id=subnet.pk,
        )
        job.data = {"subnet": subnet.pk}
        job.save(update_fields=["data"])
    return job


class ImportJob(JobRunner):
    """
    Read a Windows server and bring its differences into NetBox: an import branch, main (chosen on purpose, issue
    #36), or an import proposal.
    """

    class Meta:
        name = "Windows DHCP import"

    # Tests substitute a fake runner here.
    runner_factory = None

    def run(self, *args, destination=None, branch="", **kwargs):
        from .integrations import branching
        from .sync import importer, windows_state

        server = self.job.object
        # The destination travels with the job from the moment it is queued; the worker's own branch context (or
        # what is installed by now) never picks it. A job queued before #36 carries none: use the default.
        destination = destination or branching.import_destinations()[0]
        sync_run = SyncRun.objects.create(
            windows_server=server,
            kind=SyncKindChoices.IMPORT,
            status=SyncStatusChoices.RUNNING,
            job=self.job,
            started=timezone.now(),
            destination=destination,
            branch_name=branch[:255] if destination == ImportDestinationChoices.CURRENT else "",
        )
        runner = self.runner_factory(server) if self.runner_factory else None
        client = None

        def record_branch(branch):
            # Saved straight away, so the run points at the branch even if a later step fails.
            sync_run.branch_name = branch.name
            sync_run.save()

        try:
            user = self.job.user or _import_user()
            # Checked again here: the setting, protect_main or the user's permissions may have changed since queuing.
            refusal = import_refusal(destination, user, branch)
            if refusal:
                raise ImportRefused(refusal)
            target = None
            if destination == ImportDestinationChoices.CURRENT:
                target, _ = branching.chosen_branch(branch, user)
            with server_lock(server, ImportBusy):
                client = build_client(server, runner=runner)
                state = windows_state.read(client)
                record_names(server, client, self.logger)
                client.close()
                client = None
                if destination in (ImportDestinationChoices.BRANCH, ImportDestinationChoices.CURRENT):
                    result = importer.import_to_branch(
                        server, state, user, logger=self.logger, on_branch=record_branch, target=target
                    )
                elif destination == ImportDestinationChoices.MAIN:
                    result = importer.import_to_main(server, state, user, logger=self.logger)
                else:
                    result = importer.import_to_proposal(server, state, sync_run)
        except (
            CredentialError,
            WindowsLayerError,
            ValueError,
            ImportBusy,
            ImportRefused,
            branching.BranchError,
        ) as exc:
            sync_run.status = SyncStatusChoices.FAILED
            sync_run.error = str(exc)
            self._nothing_written(sync_run, destination)
            self.logger.error(f"Import from {server.host} failed: {exc}")
        except ValidationError as exc:
            # For example netbox-branching refusing a new branch once max_working_branches is reached.
            sync_run.status = SyncStatusChoices.FAILED
            sync_run.error = "; ".join(exc.messages)
            self._nothing_written(sync_run, destination)
            self.logger.error(f"Import from {server.host} failed: {sync_run.error}")
        else:
            sync_run.summary = result.summary()
            if result.branch is not None:
                sync_run.branch_name = result.branch.name
            for note in result.notes:
                self.logger.info(note)
            if result.failed:
                sync_run.status = SyncStatusChoices.FAILED
                sync_run.error = "\n".join(result.failed)
            else:
                sync_run.status = SyncStatusChoices.COMPLETED
            message = f"Import from {server.host}: {len(result.changes)} difference(s)"
            if result.dismissed:
                # Still differences, only not proposed again: say so rather than look like fewer differences.
                message += f", {result.dismissed} already dismissed (not proposed again)"
            self.logger.info(message)
        finally:
            if sync_run.status == SyncStatusChoices.RUNNING:
                sync_run.status = SyncStatusChoices.FAILED
                sync_run.error = "unexpected error, see the job log"
            if client is not None:
                client.close()
            sync_run.completed = timezone.now()
            sync_run.save()
        if sync_run.status == SyncStatusChoices.FAILED:
            raise JobFailed(sync_run.error)

    @staticmethod
    def _nothing_written(sync_run, destination):
        # These errors all come before an import into main writes anything (each change's own errors are caught
        # and counted), so the Import panel can say main was not touched. Anything else leaves it unsaid.
        if destination == ImportDestinationChoices.MAIN:
            sync_run.summary = {"applied": 0}


def push_log_line(host, outcome, dry_run):
    """The job log's last line: what a dry run plans, or what a push really changed (not what it planned)."""
    planned = len(outcome.plan.changes)
    if dry_run:
        return f"Push to {host}: would change {planned} object(s)"
    result = outcome.result
    line = f"Push to {host}: changed {len(result.applied)} of {planned} object(s)" if planned else f"Push to {host}"
    if result.failed:
        line += f", {len(result.failed)} failed"
    if result.skipped:
        line += f", {len(result.skipped)} not attempted"
    if outcome.replication_errors:
        line += f", failover replication failed ({len(outcome.replication_errors)})"
    if not planned and not outcome.replication_errors:
        line += ": nothing to change"
    return line


class PushJob(JobRunner):
    """Make a Windows server match NetBox: create, update and delete what differs, in dependency order."""

    class Meta:
        name = "Windows DHCP push"

    # Tests substitute a fake runner here.
    runner_factory = None
    dry_run = False

    def run(self, *args, branch="", attempt=1, **kwargs):
        server = self.job.object
        if not self.dry_run and not server.sync_enabled:
            self.logger.info(f"Sync is disabled for {server}: nothing pushed")
            return
        try:
            with server_lock(server):
                self._run(server, branch)
        except ServerBusy as exc:
            if self.dry_run or attempt >= PUSH_ATTEMPTS:
                raise JobFailed(f"{exc}; giving up after {attempt} attempt(s)") from None
            # An import (or another push) is running. Try again shortly rather than lose a merge's changes.
            enqueue_push(
                server,
                self.job.user,
                delay=PUSH_RETRY,
                branch=branch,
                notify_always=self.job.notifications != JobNotificationChoices.NOTIFICATION_ON_FAILURE,
                attempt=attempt + 1,
            )
            self.logger.info(f"{exc}; push queued again in {PUSH_RETRY} seconds")

    def _run(self, server, branch):
        from .sync import pusher

        sync_run = SyncRun.objects.create(
            windows_server=server,
            kind=SyncKindChoices.DRY_RUN if self.dry_run else SyncKindChoices.PUSH,
            status=SyncStatusChoices.RUNNING,
            job=self.job,
            started=timezone.now(),
            branch_name=branch[:255],
        )
        runner = self.runner_factory(server) if self.runner_factory else None
        client = None
        try:
            client = build_client(server, runner=runner)
            record_names(server, client, self.logger)
            outcome = pusher.run(server, client, dry_run=self.dry_run, logger=self.logger)
        except (CredentialError, WindowsLayerError, ValueError) as exc:
            sync_run.status = SyncStatusChoices.FAILED
            sync_run.error = str(exc)
            self.logger.error(f"Push to {server.host} failed: {exc}")
        else:
            sync_run.summary = outcome.summary()
            for note in outcome.notes:
                self.logger.info(note)
            for skip in outcome.plan.skipped:
                c = skip.change
                self.logger.warning(f"Not pushed: {c.action} {c.kind} {c.key}: {skip.reason}")
            if outcome.failed:
                sync_run.status = SyncStatusChoices.FAILED
                sync_run.error = "\n".join(outcome.errors())
            else:
                sync_run.status = SyncStatusChoices.COMPLETED
            self.logger.info(push_log_line(server.host, outcome, self.dry_run))
        finally:
            if sync_run.status == SyncStatusChoices.RUNNING:
                sync_run.status = SyncStatusChoices.FAILED
                sync_run.error = "unexpected error, see the job log"
            if client is not None:
                client.close()
            sync_run.completed = timezone.now()
            sync_run.save()
        if sync_run.status == SyncStatusChoices.FAILED:
            raise JobFailed(sync_run.error)


class DryRunJob(PushJob):
    """Plan a push without writing anything; the planned changes go in the sync run summary."""

    class Meta:
        name = "Windows DHCP dry run"

    dry_run = True


# Seconds before a push that found its server busy tries again, and how many times it tries (two hours: longer than
# an import provisioning a branch should take).
PUSH_RETRY = 60
PUSH_ATTEMPTS = 120

# A job still waiting this long after it was due never reached a worker (Redis lost it, or the worker died before
# starting it). It no longer stops new pushes from being queued.
ORPHANED_JOB = timedelta(minutes=15)

# RQ's default timeout is 300 seconds; reading a large server takes one PowerShell call per scope and per
# reservation, so allow more.
PUSH_TIMEOUT = 3600

# Advisory lock (with the server's key) taken while deciding whether to queue a push, so two saves at the same
# moment do not both queue one.
_QUEUE_LOCK_CLASS = 0x57444851  # "WDHQ"


def _waiting(server, job_class, now=None):
    """Jobs of this class for the server that have not started yet, and are not overdue enough to be orphans."""
    from core.choices import JobStatusChoices
    from django.db.models import Q

    cutoff = (now or timezone.now()) - ORPHANED_JOB
    return job_class.get_jobs(server).filter(
        Q(status=JobStatusChoices.STATUS_PENDING, created__gte=cutoff)
        | Q(status=JobStatusChoices.STATUS_SCHEDULED, scheduled__gte=cutoff)
    )


def enqueue_push(server, user=None, delay=0, branch="", dry_run=False, notify_always=True, attempt=1):
    """
    Queue a push (or a dry run) for the server, ``delay`` seconds from now, unless one is already waiting to start:
    that one will see the latest NetBox data anyway. Returns the job, or None.
    """
    from django.db import transaction

    job_class = DryRunJob if dry_run else PushJob
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_QUEUE_LOCK_CLASS, server.pk])
        if _waiting(server, job_class).exists():
            return None
        return job_class.enqueue(
            instance=server,
            user=user,
            schedule_at=timezone.now() + timedelta(seconds=delay) if delay else None,
            notifications=(
                JobNotificationChoices.NOTIFICATION_ALWAYS
                if notify_always
                else JobNotificationChoices.NOTIFICATION_ON_FAILURE
            ),
            job_timeout=PUSH_TIMEOUT,
            branch=branch,
            attempt=attempt,
        )


class ServerBusy(Exception):
    pass


class ImportBusy(ServerBusy):
    pass


# Two numbers identify a PostgreSQL advisory lock: this plugin's, and the server's primary key.
_LOCK_CLASS = 0x57444843  # "WDHC"


@contextmanager
def server_lock(server, busy=ServerBusy):
    """
    Allow one import or push per server at a time, across all workers: two imports would open two branches or
    proposals, and an import reading Windows while a push writes it would propose half-pushed changes back.
    A session lock, so it holds across the many transactions a job runs, and is released even if the job dies.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [_LOCK_CLASS, server.pk])
        if not cursor.fetchone()[0]:
            raise busy(f"another import or push for {server} is running")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [_LOCK_CLASS, server.pk])


class ImportRefused(Exception):
    """An import may not go where it was asked to. The message says why, for the user and the job log."""


def import_refusal(destination, user, branch=""):
    """
    Why an import into ``destination`` (``branch`` names the branch for CURRENT) may not run for ``user``, or None.
    Checked when it is queued and again when it runs. Never falls back to another destination: a refused import
    fails and says so.
    """
    from .integrations import branching

    allowed = branching.import_destinations()
    if destination not in allowed:
        return f"cannot import into {destination!r} here; choose one of: {', '.join(allowed)}"
    if destination == ImportDestinationChoices.MAIN and user is None:
        return "an import into main needs a user"
    if destination == ImportDestinationChoices.CURRENT and user is None:
        return "an import into a chosen branch needs a user"
    if destination == ImportDestinationChoices.MAIN:
        return branching.direct_import_refusal(user)
    if destination == ImportDestinationChoices.CURRENT:
        return branching.chosen_branch(branch, user)[1]
    return None


def enqueue_import(server, user, destination=None, branch=""):
    """
    Queue an import into ``destination`` (the default one when None; ``branch`` names the branch for CURRENT) unless
    one is already queued or running for the server. Returns the job, or None. Raises ImportRefused for a destination
    that may not be used.
    """
    from django.db import transaction

    from .integrations import branching

    destination = destination or branching.import_destinations()[0]
    if destination != ImportDestinationChoices.CURRENT:
        branch = ""
    refusal = import_refusal(destination, user, branch)
    if refusal:
        raise ImportRefused(refusal)
    if _busy_jobs().filter(object_id=server.pk).exists():
        return None
    # RQ gets the job when this transaction commits, with its data: the Import panel can say where a queued import
    # will write before the worker has picked it up.
    with transaction.atomic():
        job = ImportJob.enqueue(
            instance=server, user=user, destination=destination, branch=branch, **branching_job_kwargs()
        )
        job.data = {"destination": destination, **({"branch": branch} if branch else {})}
        job.save(update_fields=["data"])
    return job


def _import_user():
    """The user scheduled imports run as (the import_user setting), or None."""
    from django.contrib.auth import get_user_model

    username = get_setting("import_user")
    if not username:
        return None
    return get_user_model().objects.filter(username=username, is_active=True).first()


@system_job(interval=1)
class ScheduleImportsJob(JobRunner):
    """Every minute, queue an import for each server whose import interval has passed."""

    class Meta:
        name = "Windows DHCP import scheduler"

    def run(self, *args, **kwargs):
        user = _import_user()
        for server in due_servers():
            enqueue_import(server, user)
            self.logger.info(f"Queued an import for {server}")


# A job still "running" after this long was abandoned (for example its worker was killed), so it no longer counts as
# busy. Longer than netbox-branching's default job timeout, which an import can use for provisioning.
ABANDONED_JOB = timedelta(hours=2)


def _busy_jobs(now=None, name=None):
    """Import jobs (or jobs called ``name``) queued or running, ignoring ones abandoned long ago."""
    from core.choices import JobStatusChoices
    from core.models import Job
    from django.contrib.contenttypes.models import ContentType

    from .models import WindowsServer

    now = now or timezone.now()
    return Job.objects.filter(
        object_type=ContentType.objects.get_for_model(WindowsServer),
        name=name or ImportJob.name,
        status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES,
        created__gte=now - ABANDONED_JOB,
    )


def due_servers(now=None):
    """Servers with an import interval whose last import started at least that long ago, and none queued."""
    from .models import WindowsServer

    now = now or timezone.now()
    busy = set(_busy_jobs(now).values_list("object_id", flat=True))
    for server in WindowsServer.objects.filter(import_interval__isnull=False).exclude(import_interval=0):
        if server.pk in busy:
            continue
        last = server.sync_runs.filter(kind=SyncKindChoices.IMPORT).order_by("-created").first()
        if last is None or last.created <= now - timedelta(minutes=server.import_interval):
            yield server
