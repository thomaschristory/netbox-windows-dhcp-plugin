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
        pushes.assert_called_once_with(
            {self.server.pk},
            self.user,
            branch=self.branch.name,
            triggers=[{"source": "merge", "user": self.user, "branch": self.branch.name}],
        )
        self.branch.can_revert = True
        with mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes") as pushes:
            self.branch.revert(user=self.user)
        self.assertEqual(Scope.objects.get().name, "LAN")
        pushes.assert_called_once_with(
            {self.server.pk},
            self.user,
            branch=self.branch.name,
            triggers=[{"source": "revert", "user": self.user, "branch": self.branch.name}],
        )

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
    """The IPAM edit guard (#102) in a branch, and at merge and revert, where the merging user is checked."""

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

    def _merge(self, user):
        self.branch.can_merge = True
        with mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            self.branch.merge(user=user)

    def _address(self):
        return str(Reservation.objects.get().ip_address.address)

    def test_merge_checks_the_merging_user(self):
        from netbox_branching.choices import BranchStatusChoices
        from utilities.exceptions import AbortRequest

        before = self._address()
        self._edit_in_branch(self.editor)
        with self.assertRaises(AbortRequest):
            self._merge(self.ipam_only)
        self.assertEqual(self._address(), before)
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, BranchStatusChoices.READY)

    def test_refused_merge_leaves_nothing_applied(self):
        from netbox_branching.models import AppliedChange
        from utilities.exceptions import AbortRequest

        self._edit_in_branch(self.editor)
        with self.assertRaises(AbortRequest):
            self._merge(self.ipam_only)
        self.assertFalse(AppliedChange.objects.filter(branch=self.branch).exists())

    def test_squash_merge_checks_the_merging_user(self):
        from utilities.exceptions import AbortRequest

        before = self._address()
        self._edit_in_branch(self.editor)
        self.branch.merge_strategy = "squash"
        self.branch.save(update_fields=["merge_strategy"])
        with self.assertRaises(AbortRequest):
            self._merge(self.ipam_only)
        self.assertEqual(self._address(), before)

    def test_merge_by_a_user_with_the_permission(self):
        # The editor lost the permission since: the merging user's is what counts at merge time.
        address = self._edit_in_branch(self.editor)
        self.editor_dhcp.delete()
        merger = get_user_model().objects.create_user(username="guard-merger")
        _grant(merger, "netbox_windows_dhcp_plugin", "reservation")
        self._merge(merger)
        self.assertEqual(self._address(), address)

    def test_stale_branch_edit_is_checked_at_merge(self):
        # The address exists when the branch is created but gains its reservation on main only afterwards: the edit
        # in the branch sees no dependent and is allowed, the merge must check main's.
        from ipam.models import IPAddress
        from netaddr import IPNetwork
        from netbox_branching.models import Branch
        from utilities.exceptions import AbortRequest

        spare = IPAddress.objects.create(address=IPNetwork("10.1.0.77/24"))
        owner = get_user_model().objects.get(username="guard-owner")
        stale = Branch(name="guard-stale", owner=owner)
        stale.save(provision=False)
        self.addCleanup(self._drop, stale)
        stale.provision(user=owner)
        stale.refresh_from_db()
        Reservation.objects.create(scope=Reservation.objects.get().scope, ip_address=spare, name="late")
        with branching.writing(stale, self.ipam_only):
            ip = IPAddress.objects.get(pk=spare.pk)
            ip.snapshot()
            ip.address = IPNetwork("10.1.0.78/24")
            ip.save()
        stale.can_merge = True
        with self.assertRaises(AbortRequest), mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            stale.merge(user=self.ipam_only)
        self.assertEqual(str(IPAddress.objects.get(pk=spare.pk).address), "10.1.0.77/24")

    def _drop(self, branch):
        from django.db import connections

        connections[branch.connection_name].close()
        branch.delete()

    def test_revert_checks_the_reverting_user(self):
        from utilities.exceptions import AbortRequest

        address = self._edit_in_branch(self.editor)
        self._merge(self.editor)
        self.branch.can_revert = True
        with self.assertRaises(AbortRequest), mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            self.branch.revert(user=self.ipam_only)
        self.assertEqual(self._address(), address)
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, "merged")


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
            self.assertEqual(branching._applying.get()[0], 99)
            close()
            self.assertIsNone(branching._applying.get())


@skipUnless(apps.is_installed("netbox_branching"), "netbox-branching is not installed")
class ApplyingBranchStatusTest(TestCase):
    def test_mark_of_a_branch_no_longer_merging_is_ignored(self):
        # A failed merge sends no closing signal: its mark must not exempt later edits once the branch is READY.
        from netbox_branching.models import Branch

        branch = Branch(name="stale-mark")
        branch.save(provision=False)
        branching._on_pre_apply(None, branch=branch)
        self.addCleanup(branching.clear_applying)
        self.assertFalse(branching.applying_branch())
