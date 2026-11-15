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
from netbox_windows_dhcp_plugin.models import Reservation, Scope

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

    def test_rolled_back_save_does_not_shadow_a_later_edit(self):
        # No request_started in between, as in an RQ worker after a dry-run or failed merge.
        with self.assertRaises(ValueError), transaction.atomic():
            scope = Scope.objects.get()
            scope.name = "rolled back"
            scope.save()
            raise ValueError("abort")
        with (
            mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes") as pushes,
            mock.patch("netbox.context_managers.flush_events"),
            self.captureOnCommitCallbacks(execute=True),
            event_tracking(self._request()),
        ):
            scope = Scope.objects.get()
            scope.snapshot()
            scope.name = "main edit"
            scope.save()
        pushes.assert_called_once_with(
            {self.server.pk}, self.user, delay=mock.ANY, notify_always=False, triggers=mock.ANY
        )


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
        pushes.assert_called_once_with({self.server.pk}, self.user, branch=self.branch.name, triggers=mock.ANY)
        self.branch.can_revert = True
        with mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes") as pushes:
            self.branch.revert(user=self.user)
        self.assertEqual(Scope.objects.get().name, "LAN")
        pushes.assert_called_once_with({self.server.pk}, self.user, branch=self.branch.name, triggers=mock.ANY)

    def test_aborted_merge_queues_nothing(self):
        from utilities.exceptions import AbortTransaction

        with (
            mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes") as pushes,
            self.assertRaises(AbortTransaction),
        ):
            self.branch.merge(user=self.user, commit=False)
        self.assertEqual(Scope.objects.get().name, "LAN")
        pushes.assert_not_called()
        # The worker's next commit must not push the dry run's servers under the merge's user.
        self.assertFalse(getattr(signals._local, "pending", None) and signals._pending_is_live())


def _grant(user, app_label, model, action="change"):
    from core.models import ObjectType
    from users.models import ObjectPermission

    permission = ObjectPermission.objects.create(name=f"{user}-{action}-{model}", actions=[action])
    permission.object_types.add(ObjectType.objects.get(app_label=app_label, model=model))
    permission.users.add(user)
    return permission


@skipUnless(apps.is_installed("netbox_branching"), "netbox-branching is not installed")
class BranchGuardTest(ImportTestMixin, TransactionTestCase):
    """The IPAM edit guard (#102) in a branch, and its absence when a merge or revert replays the change."""

    serialized_rollback = True

    def setUp(self):
        from netbox_branching.models import Branch

        super().setUp()
        self.apply_all()
        users = get_user_model().objects
        self.editor = users.create_user(username="guard-editor")
        self.ipam_only = users.create_user(username="guard-ipam-only")
        for user in (self.editor, self.ipam_only):
            _grant(user, "ipam", "ipaddress", "view")
            _grant(user, "ipam", "ipaddress")
        self.editor_dhcp = _grant(self.editor, "netbox_windows_dhcp_plugin", "reservation")
        owner = users.create_user(username="guard-owner", is_superuser=True)
        self.branch = Branch(name="guard", owner=owner)
        self.branch.save(provision=False)
        self.addCleanup(self._drop_branch)
        self.addCleanup(signals._forget_pending)
        self.branch.provision(user=owner)
        self.branch.refresh_from_db()

    def _drop_branch(self):
        from django.db import connections

        connections[self.branch.connection_name].close()
        self.branch.delete()

    def _edit_in_branch(self, user):
        from ipam.models import IPAddress
        from netaddr import IPNetwork

        with branching.writing(self.branch, user):
            ip = IPAddress.objects.get(pk=Reservation.objects.get().ip_address_id)
            ip.snapshot()
            ip.address = IPNetwork(f"{ip.address.ip + 1}/{ip.address.prefixlen}")
            ip.save()
            return str(ip.address)

    def test_guard_applies_in_branch(self):
        from utilities.exceptions import AbortRequest

        with self.assertRaises(AbortRequest):
            self._edit_in_branch(self.ipam_only)

    def test_merge_and_revert_not_checked(self):
        address = self._edit_in_branch(self.editor)
        # The editor lost the DHCP permission since, and the merging user never had it: the edit was checked when it
        # was made, and who may merge is branching's and change control's business.
        self.editor_dhcp.delete()
        self.branch.can_merge = True
        with mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            self.branch.merge(user=self.ipam_only)
        self.assertEqual(str(Reservation.objects.get().ip_address.address), address)
        self.branch.can_revert = True
        with mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            self.branch.revert(user=self.ipam_only)
        self.assertNotEqual(str(Reservation.objects.get().ip_address.address), address)


class ApplyingBranchMarkTest(TestCase):
    """The merge/revert mark must not outlive the merge or revert that set it (a failed one sends no closing signal)."""

    def test_cleared_after_merge_revert_and_request_start(self):
        from types import SimpleNamespace

        fake = SimpleNamespace(pk=99, name="x", get_merged_changes=lambda: [])
        for close in (
            lambda: branching._on_merge(None, branch=fake),
            lambda: branching._on_revert(None, branch=fake),
            signals._forget_pending,
        ):
            branching._on_pre_apply(None, branch=fake)
            self.assertEqual(branching._applying.get(), 99)
            close()
            self.assertIsNone(branching._applying.get())
