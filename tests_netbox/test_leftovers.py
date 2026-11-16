"""Small sync and job leftovers from the 0.3.0 review (issue #109): one test class per item."""

import uuid
from types import SimpleNamespace
from unittest import mock, skipUnless

from core.choices import JobNotificationChoices, JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from extras.models import SavedFilter
from ipam.models import IPRange
from utilities.testing import TestCase as NetBoxTestCase

from netbox_windows_dhcp_plugin import jobs
from netbox_windows_dhcp_plugin.choices import ImportDestinationChoices, ProposalItemStatusChoices, SyncStatusChoices
from netbox_windows_dhcp_plugin.jobs import _LOCK_CLASS, IMPORT_ATTEMPTS, ImportJob, LeaseJob, due_servers
from netbox_windows_dhcp_plugin.models import FailoverRelationship, ImportProposalItem, SyncRun
from netbox_windows_dhcp_plugin.models import Scope as NetBoxScope
from netbox_windows_dhcp_plugin.sync import importer, windows_state
from netbox_windows_dhcp_plugin.sync.diff import CREATE, Change
from netbox_windows_dhcp_plugin.sync.mapping import Writer
from netbox_windows_dhcp_plugin.sync.push import PLANNED_LIMIT, Skip
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import FailoverRelationship as WinFailover
from netbox_windows_dhcp_plugin.windows.client import Scope
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from . import fixtures
from .fixtures import windows_server
from .test_audit_g4_syncnetbox import errors, import_and_apply, one_scope
from .test_audit_g7_web import _messages, _superuser, make_proposal
from .test_import import ImportTestMixin
from .test_jobs import CREDENTIALS

PROPOSAL = ImportDestinationChoices.PROPOSAL
PENDING = ProposalItemStatusChoices.PENDING
DISMISSED = ProposalItemStatusChoices.DISMISSED


# -- 1. one warning per refused change, capped ---------------------------------------------------------------------


class SkippedLogCapTest(TestCase):
    def _log(self, count):
        skipped = [Skip(Change(CREATE, "scope", f"10.{i // 256}.{i % 256}.0"), "no reason") for i in range(count)]
        logger = mock.Mock()
        jobs.log_skipped(logger, skipped)
        return [call.args[0] for call in logger.warning.call_args_list]

    def test_a_few_are_each_logged(self):
        lines = self._log(3)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "Not pushed: create scope 10.0.0.0: no reason")

    def test_exactly_the_limit_has_no_tail(self):
        self.assertEqual(len(self._log(PLANNED_LIMIT)), PLANNED_LIMIT)

    def test_more_stop_at_the_limit_and_say_how_many_more(self):
        lines = self._log(PLANNED_LIMIT + 25)
        self.assertEqual(len(lines), PLANNED_LIMIT + 1)
        self.assertEqual(lines[-1], "Not pushed: and 25 more, see the sync run summary")


# -- 2. a scheduled import that finds its server busy tries again shortly ---------------------------------------------


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class ScheduledImportBusyTest(ImportTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.server.import_interval = 60
        self.server.save()
        ImportJob.runner_factory = staticmethod(lambda server: self.fake)
        self.addCleanup(setattr, ImportJob, "runner_factory", None)
        # Another worker holds the server: advisory locks belong to a database session, so take it from a second one.
        self.other = connection.copy()
        self.addCleanup(self.other.close)
        with self.other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s, %s)", [_LOCK_CLASS, self.server.pk])

    def _job(self):
        return Job.objects.create(
            object=self.server,
            name=ImportJob.name,
            status=JobStatusChoices.STATUS_RUNNING,
            job_id=uuid.uuid4(),
            notifications=JobNotificationChoices.NOTIFICATION_ON_FAILURE,
        )

    def _run(self, job, **kwargs):
        with mock.patch("netbox_windows_dhcp_plugin.integrations.branching.branching_installed", return_value=False):
            ImportJob(job).run(destination=PROPOSAL, **kwargs)

    def _queued(self, job):
        return Job.objects.filter(object_id=self.server.pk, name=ImportJob.name).exclude(pk=job.pk)

    def test_a_scheduled_import_is_queued_again_and_leaves_no_failed_run(self):
        job = self._job()
        with mock.patch.object(ImportJob, "enqueue", wraps=ImportJob.enqueue) as enqueue:
            self._run(job, scheduled=True)
        kwargs = enqueue.call_args.kwargs
        self.assertEqual((kwargs["scheduled"], kwargs["attempt"], kwargs["destination"]), (True, 2, PROPOSAL))
        self.assertIsNotNone(kwargs["schedule_at"])
        (retry,) = self._queued(job)
        self.assertEqual(retry.status, JobStatusChoices.STATUS_SCHEDULED)
        self.assertEqual(retry.data, {"destination": PROPOSAL})
        self.assertFalse(SyncRun.objects.filter(job=job).exists())
        # Nothing was read: the attempt that runs tells the user, not this one.
        self.assertEqual(job.notifications, JobNotificationChoices.NOTIFICATION_NEVER)

    def test_the_scheduler_does_not_queue_another_while_the_retry_waits(self):
        self._run(self._job(), scheduled=True)
        self.assertEqual(list(due_servers()), [])

    def test_the_last_attempt_fails_and_keeps_its_run(self):
        job = self._job()
        with self.assertRaises(JobFailed):
            self._run(job, scheduled=True, attempt=IMPORT_ATTEMPTS)
        self.assertFalse(self._queued(job).exists())
        self.assertIn("another import", SyncRun.objects.get(job=job).error)

    def test_an_import_started_by_hand_fails_at_once(self):
        job = self._job()
        with self.assertRaises(JobFailed):
            self._run(job)
        self.assertFalse(self._queued(job).exists())
        self.assertEqual(SyncRun.objects.get(job=job).status, SyncStatusChoices.FAILED)

    def test_the_scheduler_marks_its_imports(self):
        with mock.patch.object(jobs, "enqueue_import", return_value=None) as enqueue:
            scheduler = Job.objects.create(
                name=jobs.ScheduleImportsJob.name, status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4()
            )
            jobs.ScheduleImportsJob(scheduler).run()
        enqueue.assert_called_once_with(self.server, None, scheduled=True)


# -- 3. "select all matching" with a filter name the list does not know ---------------------------------------------


class SelectAllUnknownFilterTest(NetBoxTestCase):
    def setUp(self):
        super().setUp()
        _superuser(self)
        _, self.first = make_proposal("dhcp1", "10.1.0")
        _, self.second = make_proposal("dhcp2", "10.2.0")

    def _post(self, verb, query):
        url = reverse(f"plugins:netbox_windows_dhcp_plugin:importproposalitem_bulk_{verb}")
        return self.client.post(f"{url}?{query}", {"_all": "1"}, follow=True)

    def _statuses(self):
        return set(ImportProposalItem.objects.values_list("status", flat=True))

    def test_a_mistyped_filter_selects_nothing(self):
        for verb in ("apply", "dismiss"):
            response = self._post(verb, f"proposal={self.first.pk}")
            messages = _messages(response)
            self.assertIn("danger", [level for level, _ in messages], verb)
            self.assertTrue(any("proposal" in text and "Unknown" in text for _, text in messages), messages)
            self.assertEqual(self._statuses(), {PENDING}, verb)
        self.assertFalse(NetBoxScope.objects.exists())

    def test_a_refused_selection_reports_no_success(self):
        for query in (f"proposal={self.first.pk}", "proposal_id=999999", "filter_id=999999"):
            for verb in ("apply", "dismiss"):
                levels = [level for level, _ in _messages(self._post(verb, query))]
                self.assertEqual(levels, ["danger"], (query, verb))

    def test_list_parameters_and_lookups_are_not_filters_to_refuse(self):
        saved = self._saved_filter({"status": ["pending"]})
        query = (
            f"proposal_id={self.first.pk}&status__n=dismissed&per_page=50&sort=key&page=1&return_url=/x/"
            f"&q=&filter_id={saved.pk}&tableconfig_id=1&export=table"
        )
        response = self._post("dismiss", query)
        self.assertEqual([level for level, _ in _messages(response)], ["success"])
        self.assertEqual(set(self.first.items.values_list("status", flat=True)), {DISMISSED})
        self.assertEqual(set(self.second.items.values_list("status", flat=True)), {PENDING})

    def _saved_filter(self, parameters, **kwargs):
        name = f"f-{uuid.uuid4().hex[:6]}"
        return SavedFilter.objects.create(name=name, slug=name, parameters=parameters, **kwargs)

    def test_a_saved_filter_with_an_unknown_name_selects_nothing(self):
        # NetBox replaces filter_id by the saved filter's own parameters: those are checked too.
        saved = self._saved_filter({"proposal": [self.first.pk]}, shared=True)
        for query in (f"filter_id={saved.pk}", f"filter={saved.slug}"):
            messages = _messages(self._post("dismiss", query))
            self.assertEqual([level for level, _ in messages], ["danger"], query)
            self.assertIn("proposal", messages[0][1])
            self.assertEqual(self._statuses(), {PENDING}, query)

    def test_a_valid_saved_filter_selects_what_it_says(self):
        saved = self._saved_filter({"proposal_id": [self.first.pk]}, shared=True)
        self._post("dismiss", f"filter={saved.slug}")
        self.assertEqual(set(self.first.items.values_list("status", flat=True)), {DISMISSED})
        self.assertEqual(set(self.second.items.values_list("status", flat=True)), {PENDING})

    def test_a_saved_filter_that_is_gone_or_hidden_selects_nothing(self):
        # NetBox drops a saved filter it cannot find, or that the user may not see: the list would be unfiltered.
        other = get_user_model().objects.create_user(username=f"other-{uuid.uuid4().hex[:6]}")
        hidden = self._saved_filter({"proposal_id": [self.first.pk]}, shared=False, user=other)
        self.user.is_superuser = False
        self.user.save()
        self.add_permissions(
            "netbox_windows_dhcp_plugin.view_importproposalitem", "netbox_windows_dhcp_plugin.change_importproposalitem"
        )
        for query in (
            f"filter_id={hidden.pk}",
            f"filter={hidden.slug}",
            "filter_id=999999",
            "filter=gone",
            "filter_id=x",
        ):
            messages = _messages(self._post("dismiss", query))
            self.assertEqual([level for level, _ in messages], ["danger"], query)
            self.assertIn("saved filter", messages[0][1])
            self.assertEqual(self._statuses(), {PENDING}, query)

    def test_listed_ids_are_not_checked_against_the_query(self):
        # Without "_all" the rows are named one by one: the query string plays no part.
        item = self.first.items.get()
        url = reverse("plugins:netbox_windows_dhcp_plugin:importproposalitem_bulk_dismiss")
        self.client.post(f"{url}?nonsense=1", {"pk": [str(item.pk)]})
        self.assertEqual(set(self.first.items.values_list("status", flat=True)), {DISMISSED})


# -- 4. a range change and a new failover relationship in one import -------------------------------------------------


class RangeChangeWithNewFailoverTest(TestCase):
    """The scope is written before the relationship: it counts as a failover pair already (it used to fail once)."""

    def setUp(self):
        self.first, self.second = windows_server("dhcp1"), windows_server("dhcp2")
        self.fake = FakeDhcpServer()
        self.client = one_scope(self.fake)
        import_and_apply(self.first, self.fake)
        import_and_apply(self.second, self.fake)
        self.assertEqual(IPRange.objects.count(), 1)
        # On Windows: the pair becomes failover partners and the range narrows, before the next import.
        self.client.add_failover(WinFailover(name="ha", partner_server="dhcp2.example.com", scope_ids=["10.1.0.0"]))
        self.client.set_scope(Scope(scope_id="10.1.0.0", name="LAN", start_range="10.1.0.10", end_range="10.1.0.99"))

    @staticmethod
    def lan(server):
        return NetBoxScope.objects.get(server=server, prefix__prefix="10.1.0.0/24")

    def bounds(self, server):
        ip_range = self.lan(server).ip_range
        return str(ip_range.start_address), str(ip_range.end_address)

    def assert_moved_together(self):
        # One range for the pair (a scope created by the same run has its own).
        first, second = self.lan(self.first), self.lan(self.second)
        self.assertEqual(first.ip_range_id, second.ip_range_id)
        self.assertEqual(self.bounds(self.first), ("10.1.0.10/24", "10.1.0.99/24"))
        self.assertEqual(self.bounds(self.second), ("10.1.0.10/24", "10.1.0.99/24"))
        self.assertTrue(FailoverRelationship.objects.filter(primary_server=self.first, name="ha").exists())

    def test_applying_the_whole_proposal_works_the_first_time(self):
        proposal = import_and_apply(self.first, self.fake)
        self.assertEqual(errors(proposal), {})
        self.assert_moved_together()

    def test_the_scope_alone_still_waits_for_the_relationship(self):
        # A relationship the reviewer did not apply (or dismissed) is no reason to move the partner's range.
        result = importer.import_to_proposal(self.first, windows_state.read(DhcpClient(self.fake)), None)
        scope_item = result.proposal.items.get(kind="scope")
        self.assertEqual(importer.apply_items([scope_item]), (0, 1))
        scope_item.refresh_from_db()
        self.assertIn("is shared with scope", scope_item.error)
        self.assertEqual(self.bounds(self.second), ("10.1.0.10/24", "10.1.0.200/24"))

    def test_a_partner_netbox_does_not_know_gives_no_exception(self):
        self.client.remove_failover("ha")
        self.client.add_failover(WinFailover(name="ha", partner_server="elsewhere.example.com", scope_ids=["10.1.0.0"]))
        proposal = import_and_apply(self.first, self.fake)
        self.assertIn("is shared with scope", errors(proposal)["10.1.0.0"])
        self.assertEqual(self.bounds(self.second), ("10.1.0.10/24", "10.1.0.200/24"))

    def test_a_relationship_with_a_scope_not_in_netbox_gives_no_exception(self):
        # Its other scope is neither in NetBox nor applied now, so the relationship would fail after the scope change.
        self.client.add_scope(
            Scope(name="Other", start_range="10.3.0.10", end_range="10.3.0.200", subnet_mask="255.255.255.0")
        )
        self.client.add_failover_scopes("ha", ["10.3.0.0"])
        result = importer.import_to_proposal(self.first, windows_state.read(DhcpClient(self.fake)), None)
        items = list(result.proposal.items.exclude(kind="scope", key="10.3.0.0"))
        importer.apply_items(items)
        self.assertIn("is shared with scope", errors(result.proposal)["10.1.0.0"])
        self.assertEqual(self.bounds(self.second), ("10.1.0.10/24", "10.1.0.200/24"))

    def test_a_relationship_with_a_scope_created_in_the_same_run_counts(self):
        self.client.add_scope(
            Scope(name="Other", start_range="10.3.0.10", end_range="10.3.0.200", subnet_mask="255.255.255.0")
        )
        self.client.add_failover_scopes("ha", ["10.3.0.0"])
        proposal = import_and_apply(self.first, self.fake)
        self.assertEqual(errors(proposal), {})
        self.assert_moved_together()

    def test_a_reviewer_who_may_not_add_the_relationship_gets_no_exception(self):
        fields = {"name": "ha", "partner_server": "dhcp2.example.com", "scope_ids": ["10.1.0.0"]}
        scope, other = self.lan(self.first), self.lan(self.second)
        reviewer = get_user_model().objects.create_user(username=f"reviewer-{uuid.uuid4().hex[:6]}")
        self.assertFalse(Writer(self.first, user=reviewer, failovers=[fields])._failover_twin(scope, other))
        boss = get_user_model().objects.create_user(username=f"boss-{uuid.uuid4().hex[:6]}", is_superuser=True)
        self.assertTrue(Writer(self.first, user=boss, failovers=[fields])._failover_twin(scope, other))

    @skipUnless(apps.is_installed("netbox_branching"), "imports into main need netbox-branching")
    def test_an_import_into_main_works_the_first_time(self):
        user = get_user_model().objects.create_user(username=f"importer-{uuid.uuid4().hex[:6]}", is_superuser=True)
        result = importer.import_to_main(self.first, windows_state.read(DhcpClient(self.fake)), user)
        self.assertEqual(result.failed, [])
        self.assert_moved_together()


# -- 5. the lease job closes its connection without hiding its own outcome -----------------------------------------


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class LeaseJobCloseTest(TestCase):
    def setUp(self):
        self.server = windows_server()
        self.scope = fixtures.scope(self.server, "10.1.0.0/24")
        self.job = Job.objects.create(
            object=self.server, name=LeaseJob.name, status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4()
        )

    def _run(self, client):
        with mock.patch.object(jobs, "build_client", return_value=client):
            LeaseJob(self.job).run(scope_pk=self.scope.pk)

    def _client(self, **kwargs):
        return SimpleNamespace(close=mock.Mock(side_effect=OSError("transport gone")), **kwargs)

    def test_a_failing_close_keeps_the_leases(self):
        client = self._client(list_leases=lambda scope: [])
        with self.assertLogs("netbox.jobs.LeaseJob", level="WARNING") as logs:
            self._run(client)
        client.close.assert_called_once()
        self.assertEqual(self.job.data["total"], 0)
        self.assertTrue(any("Could not close the connection: OSError" in line for line in logs.output), logs.output)

    def test_a_failing_close_does_not_hide_the_read_error(self):
        from netbox_windows_dhcp_plugin.windows.psscript import PSError

        def fail(scope):
            raise PSError(message="Access is denied", category="PermissionDenied")

        client = self._client(list_leases=fail)
        with self.assertRaises(JobFailed) as raised:
            self._run(client)
        self.assertIn("Access is denied", str(raised.exception))
        self.assertIn("Access is denied", self.job.data["error"])
