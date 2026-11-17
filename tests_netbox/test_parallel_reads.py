"""Issue #99: the import, push and dry run jobs read Windows with max_concurrency scopes at a time."""

import logging
import uuid
from unittest import mock

from core.choices import JobStatusChoices
from core.models import Job
from django.test import TestCase, override_settings

from netbox_windows_dhcp_plugin import conf
from netbox_windows_dhcp_plugin.choices import SyncStatusChoices
from netbox_windows_dhcp_plugin.jobs import DryRunJob, ImportJob, PushJob
from netbox_windows_dhcp_plugin.sync import windows_state

from .test_jobs import CREDENTIALS
from .test_push import PushTestMixin

PLUGIN = "netbox_windows_dhcp_plugin"
THREE = {**CREDENTIALS, PLUGIN: {**CREDENTIALS[PLUGIN], "max_concurrency": 3}}
BROKEN = {**CREDENTIALS, PLUGIN: {**CREDENTIALS[PLUGIN], "max_concurrency": "lots"}}


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class ParallelReadJobTest(PushTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        ImportJob.runner_factory = staticmethod(lambda server: self.fake)
        self.addCleanup(setattr, ImportJob, "runner_factory", None)

    def _job(self, job_class, **kwargs):
        job = Job.objects.create(
            object=self.server, name=job_class.name, status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4()
        )
        with (
            mock.patch("netbox_windows_dhcp_plugin.integrations.branching.branching_installed", return_value=False),
            mock.patch.object(windows_state, "read", wraps=windows_state.read) as read,
        ):
            job_class(job).run(**kwargs)
        run = self.server.sync_runs.get(job=job)
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        return read

    def assert_workers(self, read, workers):
        self.assertEqual(read.call_count, 1)
        self.assertEqual(read.call_args.kwargs, {"workers": workers})

    @override_settings(PLUGINS_CONFIG=THREE)
    def test_import_reads_with_max_concurrency(self):
        self.assert_workers(self._job(ImportJob), 3)

    @override_settings(PLUGINS_CONFIG=THREE)
    def test_push_reads_with_max_concurrency(self):
        self.assert_workers(self._job(PushJob), 3)

    @override_settings(PLUGINS_CONFIG=THREE)
    def test_dry_run_reads_with_max_concurrency(self):
        self.assert_workers(self._job(DryRunJob), 3)

    @override_settings(PLUGINS_CONFIG=CREDENTIALS)
    def test_the_default_is_four(self):
        self.assert_workers(self._job(ImportJob), 4)

    @override_settings(PLUGINS_CONFIG=BROKEN)
    def test_a_bad_value_is_logged_and_the_default_used(self):
        # The log object itself, not assertLogs: a LOGGING setting may disable the module's logger.
        with mock.patch.object(conf, "log") as log, mock.patch.object(conf, "_concurrency_logged", set()):
            read = self._job(PushJob)
        self.assert_workers(read, 4)
        log.log.assert_called_once_with(
            logging.ERROR, "max_concurrency must be a whole number of at least 1, not 'lots': using 4"
        )
