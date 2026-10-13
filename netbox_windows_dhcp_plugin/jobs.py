from core.exceptions import JobFailed
from django.utils import timezone
from netbox.jobs import JobRunner

from .choices import SyncKindChoices, SyncStatusChoices
from .conf import CredentialError
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
