from datetime import timedelta

from core.exceptions import JobFailed
from django.core.exceptions import ValidationError
from django.utils import timezone
from netbox.jobs import JobRunner, system_job

from .choices import SyncKindChoices, SyncStatusChoices
from .conf import CredentialError, get_setting
from .connection import build_client
from .models import SyncRun
from .windows.psscript import WindowsLayerError


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
        # ValueError covers settings the transports reject, such as a profile without a password or a malformed
        # SSH host key.
        except (CredentialError, WindowsLayerError, ValueError) as exc:
            sync_run.status = SyncStatusChoices.FAILED
            sync_run.error = str(exc)
            self.logger.error(f"Connection to {server.host} failed: {exc}")
        else:
            sync_run.status = SyncStatusChoices.COMPLETED
            sync_run.summary = {"ipv4_scopes": len(scopes)}
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


class ImportJob(JobRunner):
    """Read a Windows server and bring its differences into NetBox: an import branch, or an import proposal."""

    class Meta:
        name = "Windows DHCP import"

    # Tests substitute a fake runner here.
    runner_factory = None

    def run(self, *args, **kwargs):
        from .integrations import branching
        from .sync import importer, windows_state

        server = self.job.object
        sync_run = SyncRun.objects.create(
            windows_server=server,
            kind=SyncKindChoices.IMPORT,
            status=SyncStatusChoices.RUNNING,
            job=self.job,
            started=timezone.now(),
        )
        runner = self.runner_factory(server) if self.runner_factory else None
        client = None
        try:
            client = build_client(server, runner=runner)
            state = windows_state.read(client)
            client.close()
            client = None
            if branching.branching_installed():
                result = importer.import_to_branch(server, state, self.job.user or _import_user(), logger=self.logger)
            else:
                result = importer.import_to_proposal(server, state, sync_run)
        except (CredentialError, WindowsLayerError, ValueError) as exc:
            sync_run.status = SyncStatusChoices.FAILED
            sync_run.error = str(exc)
            self.logger.error(f"Import from {server.host} failed: {exc}")
        except ValidationError as exc:
            # Raised by netbox-branching when max_working_branches is reached.
            sync_run.status = SyncStatusChoices.FAILED
            sync_run.error = "; ".join(exc.messages)
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
            self.logger.info(f"Import from {server.host}: {len(result.changes)} difference(s)")
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
        for server in due_servers():
            ImportJob.enqueue(instance=server, user=_import_user())
            self.logger.info(f"Queued an import for {server}")


def due_servers(now=None):
    """Servers with an import interval whose last import started at least that long ago, and none queued."""
    from core.choices import JobStatusChoices
    from core.models import Job
    from django.contrib.contenttypes.models import ContentType

    from .models import WindowsServer

    now = now or timezone.now()
    ct = ContentType.objects.get_for_model(WindowsServer)
    busy = set(
        Job.objects.filter(
            object_type=ct, name=ImportJob.name, status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES
        ).values_list("object_id", flat=True)
    )
    for server in WindowsServer.objects.filter(import_interval__isnull=False).exclude(import_interval=0):
        if server.pk in busy:
            continue
        last = server.sync_runs.filter(kind=SyncKindChoices.IMPORT).order_by("-created").first()
        if last is None or last.created <= now - timedelta(minutes=server.import_interval):
            yield server
