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

    def _edit_in_branch(self, user, request_id=None):
        from ipam.models import IPAddress
        from netaddr import IPNetwork

        with branching.writing(self.branch, user, request_id=request_id):
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

    def test_squash_does_not_pass_a_hand_edit_off_as_the_import(self):
        # Squash replays an object's changes under its last change's request ID. A hand edit of the address followed
        # by an import write to the same object must still be checked at merge.
        from ipam.models import IPAddress
        from utilities.exceptions import AbortRequest

        from netbox_windows_dhcp_plugin.ipam_guard import import_request_id

        before = self._address()
        self._edit_in_branch(self.editor)
        with branching.writing(self.branch, self.editor, request_id=import_request_id()):
            ip = IPAddress.objects.get(pk=Reservation.objects.get().ip_address_id)
            ip.snapshot()
            ip.description = "written by an import"
            ip.save()
        self.branch.merge_strategy = "squash"
        self.branch.save(update_fields=["merge_strategy"])
        with self.assertRaises(AbortRequest):
            self._merge(self.ipam_only)
        self.assertEqual(self._address(), before)

    def _import_in_branch(self):
        """Change the reserved address in the branch as an import does; returns the new address."""
        from netbox_windows_dhcp_plugin.ipam_guard import import_request_id

        return self._edit_in_branch(self.editor, request_id=import_request_id())

    def _describe_on_main_and_sync(self):
        """Edit only the description of the reserved address on main, then sync the branch (#115)."""
        from ipam.models import IPAddress

        request = RequestFactory().get("/")
        request.id = uuid.uuid4()
        request.user = self.ipam_only
        ip = IPAddress.objects.get(pk=Reservation.objects.get().ip_address_id)
        ip.snapshot()
        ip.description = "edited on main"
        with event_tracking(request), mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            ip.save()
        self.branch.can_sync = True
        self.branch.sync(user=get_user_model().objects.get(username="guard-owner"))
        self.branch.refresh_from_db()

    def _describe_in_branch(self, user):
        from ipam.models import IPAddress

        with branching.writing(self.branch, user):
            ip = IPAddress.objects.get(pk=Reservation.objects.get().ip_address_id)
            ip.snapshot()
            ip.description = "edited in the branch"
            ip.save()

    def _set_strategy(self, strategy):
        self.branch.merge_strategy = strategy
        self.branch.save(update_fields=["merge_strategy"])

    def test_import_changes_merge_without_the_permission(self):
        address = self._import_in_branch()
        self._merge(self.ipam_only)
        self.assertEqual(self._address(), address)

    def _import_merge_after_sync(self, strategy):
        # #115: the synced description edit has its own request ID; it used to take the exemption away.
        address = self._import_in_branch()
        self._describe_on_main_and_sync()
        self._set_strategy(strategy)
        self._merge(self.ipam_only)
        self.assertEqual(self._address(), address)
        self.assertEqual(Reservation.objects.get().ip_address.description, "edited on main")

    def test_import_changes_merge_after_a_sync_of_a_main_description_edit(self):
        self._import_merge_after_sync("iterative")

    def test_squash_import_changes_merge_after_a_sync_of_a_main_description_edit(self):
        self._import_merge_after_sync("squash")

    def _revert_import(self, strategy, user):
        before = self._before = self._address()
        address = self._import_in_branch()
        self._describe_on_main_and_sync()
        self._set_strategy(strategy)
        self._merge(self.ipam_only)
        self.assertEqual(self._address(), address)
        self.branch.can_revert = True
        with mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            self.branch.revert(user=user)
        return before, address

    def _refused_revert_of_import(self, strategy):
        # Undoing the import puts back an address Windows no longer has: the next push would change Windows.
        from utilities.exceptions import AbortRequest

        with self.assertRaisesMessage(AbortRequest, "Branch guard cannot be merged or reverted by guard-ipam-only"):
            self._revert_import(strategy, self.ipam_only)
        self.assertNotEqual(self._address(), self._before)
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, "merged")

    def test_revert_of_import_changes_is_checked(self):
        self._refused_revert_of_import("iterative")

    def test_squash_revert_of_import_changes_is_checked(self):
        self._refused_revert_of_import("squash")

    def test_revert_of_import_changes_by_a_user_with_the_permission(self):
        before, _ = self._revert_import("iterative", self.editor)
        self.assertEqual(self._address(), before)

    def test_revert_of_import_changes_without_a_user_is_refused(self):
        from utilities.exceptions import AbortRequest

        with self.assertRaisesMessage(AbortRequest, "cannot be merged or reverted without a user"):
            self._revert_import("iterative", None)

    def test_import_merge_after_a_sync_of_a_main_address_edit_is_checked(self):
        # Stricter than #115 asked: a synced change of the address itself is not the import's, so it is checked.
        from ipam.models import IPAddress
        from netaddr import IPNetwork
        from utilities.exceptions import AbortRequest

        self._import_in_branch()
        ip = IPAddress.objects.get(pk=Reservation.objects.get().ip_address_id)
        ip.snapshot()
        ip.address = IPNetwork("10.1.0.55/24")
        request = RequestFactory().get("/")
        request.id = uuid.uuid4()
        request.user = self.editor
        with event_tracking(request), mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            ip.save()
        self.branch.can_sync = True
        self.branch.sync(user=get_user_model().objects.get(username="guard-owner"))
        self.branch.refresh_from_db()
        with self.assertRaises(AbortRequest):
            self._merge(self.ipam_only)
        self.assertEqual(self._address(), "10.1.0.55/24")

    def test_squash_hand_edit_of_the_description_keeps_the_import_exemption(self):
        # Squash replays the address under the hand edit's request ID (the last change): the hand edit left the address
        # alone, so the address is still the import's alone.
        address = self._import_in_branch()
        self._describe_in_branch(self.editor)
        self._set_strategy("squash")
        self._merge(self.ipam_only)
        self.assertEqual(self._address(), address)

    def _import_and_hand_edit_of_the_address(self, strategy):
        # The import moves the address, then someone moves it again by hand: still checked after a sync.
        from utilities.exceptions import AbortRequest

        before = self._address()
        self._import_in_branch()
        self._edit_in_branch(self.editor)
        self._describe_on_main_and_sync()
        self._set_strategy(strategy)
        with self.assertRaises(AbortRequest):
            self._merge(self.ipam_only)
        self.assertEqual(self._address(), before)

    def test_import_merge_with_a_hand_edit_of_the_address_is_checked(self):
        self._import_and_hand_edit_of_the_address("iterative")

    def test_squash_import_merge_with_a_hand_edit_of_the_address_is_checked(self):
        self._import_and_hand_edit_of_the_address("squash")

    def _range_mask_edit_replays_a_stale_end(self, strategy):
        # The import shifts the range start in the branch; main's end moves afterwards (by someone allowed to). A user
        # without the scope permission changes only the masks in the branch: the merge replays the stored end, address
        # included, which would put the stale end back on main. It must be checked, not taken for the import's.
        from ipam.models import IPRange
        from netaddr import IPNetwork
        from utilities.exceptions import AbortRequest

        from netbox_windows_dhcp_plugin.ipam_guard import import_request_id

        for user in (self.editor, self.ipam_only):
            _grant(user, "ipam", "iprange", "view")
            _grant(user, "ipam", "iprange")
        _grant(self.editor, "netbox_windows_dhcp_plugin", "scope")
        range_pk = Scope.objects.get().ip_range_id
        with branching.writing(self.branch, self.editor, request_id=import_request_id()):
            ip_range = IPRange.objects.get(pk=range_pk)
            ip_range.snapshot()
            ip_range.start_address = IPNetwork("10.1.0.11/24")
            ip_range.save()
        ip_range = IPRange.objects.get(pk=range_pk)
        ip_range.end_address = IPNetwork("10.1.0.199/24")
        with mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            ip_range.save()
        with branching.writing(self.branch, self.ipam_only):
            ip_range = IPRange.objects.get(pk=range_pk)
            ip_range.snapshot()
            ip_range.start_address = IPNetwork("10.1.0.11/25")
            ip_range.end_address = IPNetwork("10.1.0.200/25")
            ip_range.save()
        self._set_strategy(strategy)
        with self.assertRaises(AbortRequest):
            self._merge(self.ipam_only)
        self.assertEqual(str(IPRange.objects.get(pk=range_pk).end_address), "10.1.0.199/24")

    def test_range_mask_edit_does_not_ride_on_the_import(self):
        self._range_mask_edit_replays_a_stale_end("iterative")

    def test_squash_range_mask_edit_does_not_ride_on_the_import(self):
        self._range_mask_edit_replays_a_stale_end("squash")

    def _import_creates_a_reservation(self, hand_edit):
        """In the branch, an import creates an address and a reservation on it; then a hand edit may move it."""
        from ipam.models import IPAddress
        from netaddr import IPNetwork

        from netbox_windows_dhcp_plugin.ipam_guard import import_request_id

        with branching.writing(self.branch, self.editor, request_id=import_request_id()):
            ip = IPAddress.objects.create(address=IPNetwork("10.1.0.120/24"))
            Reservation.objects.create(
                scope=Reservation.objects.first().scope,
                ip_address=ip,
                name="imported",
                client_id="01-aa-bb-cc-dd-ee-ff",
            )
        if hand_edit:
            with branching.writing(self.branch, self.editor):
                ip = IPAddress.objects.get(pk=ip.pk)
                ip.snapshot()
                ip.address = IPNetwork("10.1.0.121/24")
                ip.save()
        return ip.pk

    def _created_address(self, pk):
        from ipam.models import IPAddress

        found = IPAddress.objects.filter(pk=pk).first()
        return str(found.address) if found else None

    def test_squash_create_with_a_hand_edit_of_the_address_is_checked(self):
        # Squash folds the import's create and the hand edit into one create on main, which the guard did not see.
        from utilities.exceptions import AbortRequest

        pk = self._import_creates_a_reservation(hand_edit=True)
        self._set_strategy("squash")
        with self.assertRaisesMessage(AbortRequest, "Branch guard cannot be merged or reverted by guard-ipam-only"):
            self._merge(self.ipam_only)
        self.assertIsNone(self._created_address(pk))

    def test_iterative_create_with_a_hand_edit_of_the_address_is_checked(self):
        from utilities.exceptions import AbortRequest

        pk = self._import_creates_a_reservation(hand_edit=True)
        with self.assertRaises(AbortRequest):
            self._merge(self.ipam_only)
        self.assertIsNone(self._created_address(pk))

    def test_squash_create_without_a_user_is_refused(self):
        from utilities.exceptions import AbortRequest

        pk = self._import_creates_a_reservation(hand_edit=True)
        self._set_strategy("squash")
        with self.assertRaisesMessage(AbortRequest, "cannot be merged or reverted without a user"):
            self._merge(None)
        self.assertIsNone(self._created_address(pk))

    def test_squash_create_with_a_hand_edit_by_a_user_with_the_permission(self):
        pk = self._import_creates_a_reservation(hand_edit=True)
        self._set_strategy("squash")
        self._merge(self.editor)
        self.assertEqual(self._created_address(pk), "10.1.0.121/24")

    def test_squash_create_by_the_import_alone_needs_no_permission(self):
        pk = self._import_creates_a_reservation(hand_edit=False)
        self._set_strategy("squash")
        self._merge(self.ipam_only)
        self.assertEqual(self._created_address(pk), "10.1.0.120/24")

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

    def test_squash_revert_checks_the_reverting_user(self):
        from utilities.exceptions import AbortRequest

        address = self._edit_in_branch(self.editor)
        self.branch.merge_strategy = "squash"
        self.branch.save(update_fields=["merge_strategy"])
        self._merge(self.editor)
        self.branch.can_revert = True
        refused = self.assertRaisesMessage(AbortRequest, "Branch guard cannot be merged or reverted by guard-ipam-only")
        with refused, mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            self.branch.revert(user=self.ipam_only)
        self.assertEqual(self._address(), address)

    def test_revert_of_a_change_that_was_a_no_op_at_merge_checks_the_reverting_user(self):
        # Main already held the branch's new address, so the merge recorded nothing for it on main; the revert still
        # undoes the branch change and puts the old address back.
        from ipam.models import IPAddress
        from netaddr import IPNetwork
        from utilities.exceptions import AbortRequest

        address = self._edit_in_branch(self.editor)
        ip = IPAddress.objects.get(pk=Reservation.objects.get().ip_address_id)
        ip.address = IPNetwork(address)
        with mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            ip.save()
        self._merge(self.editor)
        from netbox_branching.models import AppliedChange

        self.assertFalse(AppliedChange.objects.filter(branch=self.branch, change__changed_object_id=ip.pk).exists())
        self.branch.can_revert = True
        refused = self.assertRaisesMessage(AbortRequest, "Branch guard cannot be merged or reverted by guard-ipam-only")
        with refused, mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            self.branch.revert(user=self.ipam_only)
        self.assertEqual(self._address(), address)

    def test_squash_revert_does_not_pass_a_hand_edit_off_as_the_import(self):
        # Squash records one change per object on main, under the last (import) request ID: the revert must still see
        # the hand edit among the branch's changes.
        from ipam.models import IPAddress
        from utilities.exceptions import AbortRequest

        from netbox_windows_dhcp_plugin.ipam_guard import import_request_id

        address = self._edit_in_branch(self.editor)
        with branching.writing(self.branch, self.editor, request_id=import_request_id()):
            ip = IPAddress.objects.get(pk=Reservation.objects.get().ip_address_id)
            ip.snapshot()
            ip.description = "written by an import"
            ip.save()
        self.branch.merge_strategy = "squash"
        self.branch.save(update_fields=["merge_strategy"])
        self._merge(self.editor)
        self.branch.can_revert = True
        with self.assertRaises(AbortRequest), mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            self.branch.revert(user=self.ipam_only)
        self.assertEqual(self._address(), address)

    def test_merge_refused_by_the_check_after_the_save(self):
        # The merging user may change reservations in the lower half of the subnet only: moving one to the upper half
        # passes the check on the stored value and must be refused on the saved one.
        from core.models import ObjectType
        from ipam.models import IPAddress
        from netaddr import IPNetwork
        from users.models import ObjectPermission
        from utilities.exceptions import AbortRequest

        before = self._address()
        with branching.writing(self.branch, self.editor):
            ip = IPAddress.objects.get(pk=Reservation.objects.get().ip_address_id)
            ip.snapshot()
            ip.address = IPNetwork("10.1.0.200/24")
            ip.save()
        permission = ObjectPermission.objects.create(
            name="lower-half",
            actions=["change"],
            constraints={"ip_address__address__net_host_contained": "10.1.0.0/25"},
        )
        permission.object_types.add(ObjectType.objects.get(app_label="netbox_windows_dhcp_plugin", model="reservation"))
        permission.users.add(self.ipam_only)
        with self.assertRaises(AbortRequest):
            self._merge(self.ipam_only)
        self.assertEqual(self._address(), before)

    NO_USER = (
        "Branch guard cannot be merged or reverted without a user: changing address would change one or more "
        'reservations on Windows. Merge or revert it as a user with the "change reservation" permission on them.'
    )

    def test_merge_without_a_user_is_refused(self):
        # A merge job whose user was deleted: nobody to check, even though the change's author has the permission.
        before = self._address()
        self._edit_in_branch(self.editor)
        from utilities.exceptions import AbortRequest

        with self.assertRaisesMessage(AbortRequest, self.NO_USER):
            self._merge(None)
        self.assertEqual(self._address(), before)

    def test_merge_as_an_anonymous_user_is_refused_like_no_user(self):
        from django.contrib.auth.models import AnonymousUser
        from utilities.exceptions import AbortRequest

        before = self._address()
        self._edit_in_branch(self.editor)
        with self.assertRaisesMessage(AbortRequest, self.NO_USER):
            self._merge(AnonymousUser())
        self.assertEqual(self._address(), before)

    def test_squash_merge_without_a_user_is_refused(self):
        # Squash replays as the merging user, here nobody: before #116 the change was not checked at all.
        from utilities.exceptions import AbortRequest

        before = self._address()
        self._edit_in_branch(self.editor)
        self.branch.merge_strategy = "squash"
        self.branch.save(update_fields=["merge_strategy"])
        with self.assertRaisesMessage(AbortRequest, self.NO_USER):
            self._merge(None)
        self.assertEqual(self._address(), before)

    def test_revert_without_a_user_is_refused(self):
        from utilities.exceptions import AbortRequest

        address = self._edit_in_branch(self.editor)
        self._merge(self.editor)
        self.branch.can_revert = True
        with (
            self.assertRaisesMessage(AbortRequest, self.NO_USER),
            mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"),
        ):
            self.branch.revert(user=None)
        self.assertEqual(self._address(), address)

    def test_merge_without_a_user_of_an_address_nothing_uses(self):
        # An address no scope or reservation uses: nothing to refuse.
        from ipam.models import IPAddress
        from netaddr import IPNetwork
        from netbox_branching.models import Branch

        spare = IPAddress.objects.create(address=IPNetwork("10.1.0.77/24"))
        owner = get_user_model().objects.get(username="guard-owner")
        branch = Branch(name="guard-spare", owner=owner)
        branch.save(provision=False)
        self.addCleanup(self._drop, branch)
        branch.provision(user=owner)
        branch.refresh_from_db()
        with branching.writing(branch, self.editor):
            ip = IPAddress.objects.get(pk=spare.pk)
            ip.snapshot()
            ip.address = IPNetwork("10.1.0.78/24")
            ip.save()
        branch.can_merge = True
        with mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
            branch.merge(user=None)
        self.assertEqual(str(IPAddress.objects.get(pk=spare.pk).address), "10.1.0.78/24")

    def test_mark_of_a_branch_no_longer_merging_is_ignored_even_under_its_request_id(self):
        # A failed merge resets the branch to ready but sends no closing signal: even a save under one of the branch's
        # request IDs must not be taken for a replay.
        from core.models import ObjectChange
        from ipam.models import IPAddress

        self._edit_in_branch(self.editor)
        change = ObjectChange.objects.using(self.branch.connection_name).filter(
            changed_object_id=Reservation.objects.get().ip_address_id
        )
        request = RequestFactory().get("/")
        request.id = change.values_list("request_id", flat=True).first()
        request.user = self.editor
        branching._on_pre_apply(None, branch=self.branch, user=self.editor)
        self.addCleanup(branching.clear_applying)
        ip = IPAddress.objects.get(pk=Reservation.objects.get().ip_address_id)
        with event_tracking(request):
            self.assertIsNone(branching.applying_branch(ip))
        # The same save while the branch is merging is a replay: only the status told them apart.
        from netbox_branching.choices import BranchStatusChoices
        from netbox_branching.models import Branch

        Branch.objects.filter(pk=self.branch.pk).update(status=BranchStatusChoices.MERGING)
        self.addCleanup(Branch.objects.filter(pk=self.branch.pk).update, status=BranchStatusChoices.READY)
        with event_tracking(request):
            self.assertEqual(branching.applying_branch(ip)[:2], ("guard", self.editor))

    def test_stale_mark_does_not_check_ordinary_saves_as_the_merging_user(self):
        # A mark left by a failed merge in this context while the same branch merges elsewhere (status MERGING): an
        # ordinary save here is checked as its own user, not as the stale merging user.
        from ipam.models import IPAddress
        from netaddr import IPNetwork
        from netbox_branching.choices import BranchStatusChoices
        from netbox_branching.models import Branch

        # The address has changes in the branch, so only the request ID tells this save from a replay.
        self._edit_in_branch(self.editor)
        merger = get_user_model().objects.create_user(username="guard-stale-merger")
        _grant(merger, "netbox_windows_dhcp_plugin", "reservation")
        branching._on_pre_apply(None, branch=self.branch, user=merger)
        self.addCleanup(branching.clear_applying)
        Branch.objects.filter(pk=self.branch.pk).update(status=BranchStatusChoices.MERGING)
        self.addCleanup(Branch.objects.filter(pk=self.branch.pk).update, status=BranchStatusChoices.READY)
        request = RequestFactory().get("/")
        request.id = uuid.uuid4()
        request.user = self.ipam_only
        from utilities.exceptions import AbortRequest

        ip = IPAddress.objects.get(pk=Reservation.objects.get().ip_address_id)
        ip.address = IPNetwork(f"{ip.address.ip + 1}/{ip.address.prefixlen}")
        with self.assertRaises(AbortRequest) as caught, event_tracking(request):
            ip.save()
        # Refused as an ordinary save by ipam_only, not checked as a replay (that refusal would name the branch).
        self.assertIn("Changing address would change", str(caught.exception))
        self.assertNotIn("Branch guard", str(caught.exception))

    def test_save_to_main_with_a_branch_active_checks_main(self):
        # The guard reads where the save writes: here the branch has no reservation on the address any more, main
        # still has one.
        from ipam.models import IPAddress
        from netaddr import IPNetwork
        from netbox_branching.utilities import activate_branch
        from utilities.exceptions import AbortRequest

        with branching.writing(self.branch, self.editor):
            reservation = Reservation.objects.get()
            reservation.snapshot()
            reservation.delete()
        ip_pk = Reservation.objects.get().ip_address_id
        request = RequestFactory().get("/")
        request.id = uuid.uuid4()
        request.user = self.ipam_only
        with activate_branch(self.branch):
            ip = IPAddress.objects.using("default").get(pk=ip_pk)
            ip.address = IPNetwork(f"{ip.address.ip + 1}/{ip.address.prefixlen}")
            with (
                self.assertRaisesMessage(AbortRequest, "Changing address would change"),
                event_tracking(request),
                mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"),
            ):
                ip.save(using="default")

    def _main_save_with_branch_active(self, address, user):
        from ipam.models import IPAddress
        from netaddr import IPNetwork
        from netbox_branching.utilities import activate_branch

        request = RequestFactory().get("/")
        request.id = uuid.uuid4()
        request.user = user
        with activate_branch(self.branch):
            ip = IPAddress.objects.using("default").get(pk=Reservation.objects.using("default").get().ip_address_id)
            ip.address = IPNetwork(address)
            with event_tracking(request), mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes"):
                ip.save(using="default")

    def test_main_save_with_a_branch_active_compares_main_values(self):
        # The branch already holds the new address, main does not: the change is read on main, where it is one.
        from utilities.exceptions import AbortRequest

        before = self._address()
        address = self._edit_in_branch(self.editor)
        with self.assertRaisesMessage(AbortRequest, "Changing address would change"):
            self._main_save_with_branch_active(address, self.ipam_only)
        self.assertEqual(self._address(), before)

    def test_check_after_the_save_puts_main_back_with_a_branch_active(self):
        # No transaction around the save here, so only the write-back undoes it, and it must write main, not the
        # active branch.
        from core.models import ObjectType
        from users.models import ObjectPermission
        from utilities.exceptions import AbortRequest

        before = self._address()
        permission = ObjectPermission.objects.create(
            name="lower-half-main",
            actions=["change"],
            constraints={"ip_address__address__net_host_contained": "10.1.0.0/25"},
        )
        permission.object_types.add(ObjectType.objects.get(app_label="netbox_windows_dhcp_plugin", model="reservation"))
        permission.users.add(self.ipam_only)
        with self.assertRaisesMessage(AbortRequest, "Changing address would change"):
            self._main_save_with_branch_active("10.1.0.200/24", self.ipam_only)
        self.assertEqual(self._address(), before)


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
