"""Main edits must not be mistaken for another request's merge or revert."""

import uuid
from unittest import mock, skipUnless

from core.choices import ObjectChangeActionChoices
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import RequestFactory, TestCase, TransactionTestCase
from netbox.context_managers import event_tracking

from netbox_windows_dhcp_plugin import signals
from netbox_windows_dhcp_plugin.integrations import branching
from netbox_windows_dhcp_plugin.jobs import PushJob
from netbox_windows_dhcp_plugin.models import Scope

from .test_import import ImportTestMixin


@skipUnless(apps.is_installed("netbox_branching"), "netbox-branching is not installed")
class BranchSaveTest(ImportTestMixin, TestCase):
    def setUp(self):
        from netbox_branching.models import Branch

        super().setUp()
        self.apply_all()
        self.branch = Branch(name="unrelated")
        self.branch.save(provision=False)
        self.user = get_user_model().objects.create_user(username="branch-save-reviewer", is_superuser=True)
        self.addCleanup(signals._forget_pending)

    def _request(self):
        request = RequestFactory().get("/")
        request.id = uuid.uuid4()
        request.user = self.user
        return request

    def test_main_edits_push_while_an_unrelated_branch_is_busy(self):
        from netbox_branching.choices import BranchStatusChoices
        from netbox_branching.models import Branch

        for status in (BranchStatusChoices.MERGING, BranchStatusChoices.REVERTING):
            with self.subTest(status=status):
                Branch.objects.filter(pk=self.branch.pk).update(status=status)
                with (
                    mock.patch("netbox.context_managers.flush_events"),
                    self.captureOnCommitCallbacks(execute=True),
                    event_tracking(self._request()),
                ):
                    scope = Scope.objects.get()
                    scope.snapshot()
                    scope.name = status
                    scope.save()
                self.assertEqual(PushJob.get_jobs(self.server).count(), 1)
                PushJob.get_jobs(self.server).delete()

    def test_replayed_request_leaves_push_to_branch_completion(self):
        from netbox_branching.choices import BranchStatusChoices
        from netbox_branching.models import AppliedChange

        request = self._request()
        scope = Scope.objects.get()
        scope.snapshot()
        with (
            mock.patch("netbox.context_managers.flush_events"),
            self.captureOnCommitCallbacks(execute=True),
            event_tracking(request),
        ):
            scope.name = "merged"
            scope.save()
            change = scope.to_objectchange(ObjectChangeActionChoices.ACTION_UPDATE)
            change.request_id = request.id
            change.user = self.user
            change.save()
            AppliedChange.objects.create(branch=self.branch, change=change)
        self.assertFalse(PushJob.get_jobs(self.server).exists())
        self.branch.status = BranchStatusChoices.MERGED
        with self.captureOnCommitCallbacks(execute=True):
            branching._on_merge(None, branch=self.branch, user=self.user)
        self.assertEqual(PushJob.get_jobs(self.server).count(), 1)

    def test_rolled_back_main_edit_queues_no_push(self):
        with self.captureOnCommitCallbacks(execute=True), self.assertRaises(ValueError), transaction.atomic():
            scope = Scope.objects.get()
            scope.name = "rolled back"
            scope.save()
            raise ValueError("abort")
        self.assertFalse(PushJob.get_jobs(self.server).exists())


@skipUnless(apps.is_installed("netbox_branching"), "netbox-branching is not installed")
class BranchMergeIntegrationTest(ImportTestMixin, TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        from netbox_branching.models import Branch

        super().setUp()
        self.apply_all()
        self.user = get_user_model().objects.create_user(username="branch-merge-reviewer", is_superuser=True)
        self.branch = Branch(name="real-merge", owner=self.user)
        self.branch.save(provision=False)
        self.addCleanup(self._drop_branch)
        self.addCleanup(signals._forget_pending)
        self.branch.provision(user=self.user)
        self.branch.refresh_from_db()
        with branching.writing(self.branch, self.user):
            scope = Scope.objects.get()
            scope.snapshot()
            scope.name = "merged"
            scope.save()

    def _drop_branch(self):
        from django.db import connections

        connections[self.branch.connection_name].close()
        self.branch.delete()

    def test_merge_and_revert_queue_only_the_attributed_completion_push(self):
        # This test exercises replay/commit attribution, not change-control's approval policy.
        self.branch.can_merge = True
        with mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes") as pushes:
            self.branch.merge(user=self.user)
        self.assertEqual(Scope.objects.get().name, "merged")
        pushes.assert_called_once_with({self.server.pk}, self.user, branch=self.branch.name)
        self.branch.can_revert = True
        with mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes") as pushes:
            self.branch.revert(user=self.user)
        self.assertEqual(Scope.objects.get().name, "LAN")
        pushes.assert_called_once_with({self.server.pk}, self.user, branch=self.branch.name)

    def test_aborted_merge_queues_nothing(self):
        from utilities.exceptions import AbortTransaction

        with (
            mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes") as pushes,
            self.assertRaises(AbortTransaction),
        ):
            self.branch.merge(user=self.user, commit=False)
        self.assertEqual(Scope.objects.get().name, "LAN")
        pushes.assert_not_called()
