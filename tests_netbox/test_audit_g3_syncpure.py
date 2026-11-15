"""Audit g3-syncpure, the Django side of a push: replication after a failed change, and SyncState bookkeeping."""

from core.exceptions import JobFailed
from django.test import TestCase, override_settings

from netbox_windows_dhcp_plugin.choices import SyncKindChoices
from netbox_windows_dhcp_plugin.models import MacFilter, SyncRun, SyncState
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer
from netbox_windows_dhcp_plugin.windows.psscript import PSError
from netbox_windows_dhcp_plugin.windows.runner import RunnerError

from .test_jobs import CREDENTIALS
from .test_push import FailoverPushTest, PushTestMixin


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class FailedChangeReplicationTest(FailoverPushTest):
    def _failed_run(self):
        with self.assertRaises(JobFailed):
            self._run()
        return SyncRun.objects.filter(kind=SyncKindChoices.PUSH).order_by("-pk").first()

    def test_change_windows_refused_part_way_is_still_replicated(self):
        # F58: an error from Windows may come after part of the change was made, so the partner gets the scope.
        self.fake.fail = lambda op, p: (
            PSError("boom", "InvalidOperation", "", "Set-DhcpServerv4Scope") if op == "scope.set" else None
        )
        self._change_scope()
        run = self._failed_run()
        self.assertEqual(run.summary["failed"], 1)
        self.assertEqual(self.fake.replications, [{"name": "", "scope_ids": ["10.1.0.0"]}])
        self.assertNotIn("replication_pending", run.summary)

    def test_lost_connection_sends_nothing_more_and_leaves_the_scope_pending(self):
        # F58 and F61: the reply was lost, so the scope may have changed; replication waits for the next push
        # instead of waiting for one more timeout per relationship.
        def lost(op, p):
            if op == "scope.set":
                raise RunnerError("connection lost")

        self.fake.fail = lost
        self._change_scope()
        run = self._failed_run()
        ops = self.fake.ops()
        after = ops[len(ops) - ops[::-1].index("scope.set") :]
        self.assertEqual(after, [])
        self.assertEqual(self.fake.replications, [])
        self.assertEqual(run.summary["replication_pending"], {"fo": ["10.1.0.0"]})
        self.assertIn("the push stopped early", run.error)
        # The next push writes the scope and replicates it.
        self.fake.fail = None
        run = self._run()
        self.assertEqual(self.fake.replications, [{"name": "", "scope_ids": ["10.1.0.0"]}])
        self.assertNotIn("replication_pending", run.summary)

    def _refuse(self, failing):
        self.fake.fail = lambda op, p: PSError("boom", "InvalidOperation", "", "Some-Cmdlet") if op == failing else None

    def test_server_level_change_windows_keeps_refusing_replicates_nothing(self):
        # R04/R16: a refused MAC filter is planned again by every push. It must not send every scope of every
        # relationship to the partner each time.
        MacFilter.objects.create(server=self.server, mac_address="00-15-5d-01-02-03", list="allow")
        self._refuse("filter.add")
        for _ in range(2):
            run = self._failed_run()
            self.assertEqual(run.summary["failed"], 1)
            self.assertNotIn("replication_pending", run.summary)
            self.assertNotIn("failover replication", run.error)
        self.assertEqual(self.fake.replications, [])
        self.assertNotIn("failover.get", self.fake.ops())
        # Once Windows takes it, it is an applied server-level change and replicates as it always did.
        self.fake.fail = None
        self._run()
        self.assertEqual(self.fake.replications, [{"name": "", "scope_ids": ["10.1.0.0"]}])

    def test_failover_change_windows_keeps_refusing_replicates_nothing(self):
        # R04: the usual persistent failure (partner unreachable, CredSSP missing). Windows replicates a
        # relationship itself when it changes it, so a failed change gives the partner nothing to fetch.
        self.relationship.snapshot()
        self.relationship.load_balance_percent = 70
        self.relationship.save()
        self._refuse("failover.set")
        for _ in range(2):
            run = self._failed_run()
            self.assertEqual(run.summary["failed"], 1)
            self.assertNotIn("replication_pending", run.summary)
        self.assertEqual(self.fake.replications, [])

    def test_refused_server_level_change_does_not_hide_a_scope_that_may_have_changed(self):
        # F58 still holds next to a refused server-level change: only the scope goes to the partner.
        MacFilter.objects.create(server=self.server, mac_address="00-15-5d-01-02-03", list="allow")
        self.fake.fail = lambda op, p: (
            PSError("boom", "InvalidOperation", "", "Some-Cmdlet") if op in ("filter.add", "scope.set") else None
        )
        self._change_scope()
        run = self._failed_run()
        self.assertEqual(run.summary["failed"], 2)
        self.assertEqual(self.fake.replications, [{"name": "", "scope_ids": ["10.1.0.0"]}])

    def test_lost_reply_on_a_server_level_change_notes_the_scopes_once(self):
        # F58: the filter may be on Windows although the reply was lost. The next push then plans nothing for it,
        # so the scopes are noted now (no call to the server) and replicated by that push.
        MacFilter.objects.create(server=self.server, mac_address="00-15-5d-01-02-03", list="allow")

        def lost(op, p):
            if op == "filter.add":
                raise RunnerError("connection lost")

        self.fake.fail = lost
        run = self._failed_run()
        self.assertEqual(self.fake.replications, [])
        self.assertEqual(run.summary["replication_pending"], {"fo": ["10.1.0.0"]})
        self.fake.fail = None
        run = self._run()
        self.assertEqual(self.fake.replications, [{"name": "", "scope_ids": ["10.1.0.0"]}])
        self.assertNotIn("replication_pending", run.summary)

    def test_lost_reply_on_a_failover_change_notes_nothing(self):
        self.relationship.snapshot()
        self.relationship.load_balance_percent = 70
        self.relationship.save()

        def lost(op, p):
            if op == "failover.set":
                raise RunnerError("connection lost")

        self.fake.fail = lost
        run = self._failed_run()
        self.assertEqual(self.fake.replications, [])
        self.assertNotIn("replication_pending", run.summary)


# The inherited FailoverPushTest tests are not collected a second time.
for _name in [n for n in dir(FailoverPushTest) if n.startswith("test_")]:
    setattr(FailedChangeReplicationTest, _name, None)


@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class SyncStateBookkeepingTest(PushTestMixin, TestCase):
    def test_first_push_records_every_object_with_its_push_time(self):
        # F59/F60: new rows are written in bulk; each still gets its fingerprint and last_pushed.
        self.target = FakeDhcpServer()
        SyncState.objects.filter(windows_server=self.server).delete()
        run = self._run()
        rows = SyncState.objects.filter(windows_server=self.server)
        self.assertEqual(rows.count(), run.summary["create"])
        self.assertFalse(rows.filter(last_pushed__isnull=True).exists())
        self.assertFalse(rows.filter(fingerprint="").exists())
        # Nothing to record twice: a second push finds every row and adds none.
        before = set(rows.values_list("pk", flat=True))
        self._run()
        self.assertEqual(set(rows.values_list("pk", flat=True)), before)

    def test_removed_object_loses_its_row_and_a_changed_one_gets_a_new_fingerprint(self):
        from netbox_windows_dhcp_plugin.models import Reservation, Scope

        self._run()
        scope_row = SyncState.objects.get(windows_server=self.server, kind="scope")
        old = (scope_row.fingerprint, scope_row.last_pushed)
        reservations = SyncState.objects.filter(windows_server=self.server, kind="reservation")
        count = reservations.count()
        self.assertGreater(count, 0)
        Reservation.objects.first().delete()
        scope = Scope.objects.get()
        scope.snapshot()
        scope.name = "changed"
        scope.save()
        run = self._run()
        self.assertEqual(run.summary["failed"], 0, run.error)
        self.assertEqual(reservations.count(), count - 1)
        scope_row.refresh_from_db()
        self.assertNotEqual(scope_row.fingerprint, old[0])
        self.assertGreater(scope_row.last_pushed, old[1])
