"""
Regression tests for the code quality audit, group g7 (views, forms, filtersets, REST API): findings F07 to F10,
F17, F18, F66 to F68 and F70. Each test class names the finding it covers.
"""

from datetime import timedelta
from unittest import mock

from core.models import Job
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from utilities.testing import APITestCase, TestCase

from netbox_windows_dhcp_plugin import filtersets, forms
from netbox_windows_dhcp_plugin.choices import ClassTypeChoices, ProposalItemStatusChoices
from netbox_windows_dhcp_plugin.jobs import TestConnectionJob
from netbox_windows_dhcp_plugin.models import (
    ExclusionRange,
    FailoverRelationship,
    ImportProposalItem,
    OptionDefinition,
    OptionValue,
    SyncState,
    WindowsClass,
    WindowsPolicy,
    WindowsServer,
)
from netbox_windows_dhcp_plugin.models import Scope as NetBoxScope
from netbox_windows_dhcp_plugin.sync import importer, windows_state
from netbox_windows_dhcp_plugin.windows import DhcpClient
from netbox_windows_dhcp_plugin.windows.client import Scope
from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

from .fixtures import PluginAPIMixin, reservation, scope, windows_server

API = "/api/plugins/windows-dhcp"
IN_BRANCH = "netbox_windows_dhcp_plugin.integrations.branching.in_branch"
PENDING = ProposalItemStatusChoices.PENDING


def _superuser(test):
    test.user.is_superuser = True
    test.user.save()


def _messages(response):
    return [(m.level_tag, str(m)) for m in response.context["messages"]]


def make_proposal(name="dhcp1", network="10.1.0"):
    """A server with an open proposal holding one item: a scope to create."""
    server = windows_server(name)
    fake = FakeDhcpServer()
    DhcpClient(fake).add_scope(
        Scope(name=name, start_range=f"{network}.10", end_range=f"{network}.200", subnet_mask="255.255.255.0")
    )
    return server, importer.import_to_proposal(server, windows_state.read(DhcpClient(fake)), None).proposal


# -- F07 ---------------------------------------------------------------------------------------------------------


class F07ReservationValueScopeTest(PluginAPIMixin, APITestCase):
    """A PATCH cannot detach a reservation's option value from the reservation's scope."""

    def setUp(self):
        super().setUp()
        _superuser(self)
        server = windows_server()
        self.lan = scope(server, "10.1.0.0/24")
        self.other = scope(server, "10.2.0.0/24")
        printer = reservation(self.lan, "10.1.0.50/24", client_id="aa")
        self.value = OptionValue.objects.create(
            server=server, scope=self.lan, reservation=printer, code=3, value=["10.1.0.1"]
        )
        self.url = f"{API}/option-values/{self.value.pk}/"

    def _patch(self, data):
        response = self.client.patch(self.url, data, format="json", **self.header)
        self.value.refresh_from_db()
        return response

    def test_null_scope_keeps_the_reservations_scope(self):
        response = self._patch({"scope": None})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.value.scope_id, self.lan.pk)
        self.assertEqual(response.json()["scope"]["id"], self.lan.pk)

    def test_a_patch_of_the_value_alone_leaves_the_scope(self):
        response = self._patch({"value": ["10.1.0.254"]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((self.value.scope_id, self.value.value), (self.lan.pk, ["10.1.0.254"]))

    def test_another_scope_is_still_rejected(self):
        self.assertEqual(self._patch({"scope": self.other.pk}).status_code, 400)
        self.assertEqual(self.value.scope_id, self.lan.pk)

    def test_removing_the_reservation_makes_it_a_scope_value(self):
        response = self._patch({"reservation": None})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((self.value.reservation_id, self.value.scope_id), (None, self.lan.pk))


# -- F08 and F66 -------------------------------------------------------------------------------------------------


class _TestJobsMixin:
    def _jobs(self):
        # A fresh test database also holds NetBox's own system jobs: count ours by name.
        return Job.objects.filter(object_id=self.server.pk, name=TestConnectionJob.name)


class F08ConnectionTestAPITest(_TestJobsMixin, PluginAPIMixin, APITestCase):
    """A second connection test is refused while one is waiting to start."""

    def setUp(self):
        super().setUp()
        _superuser(self)
        self.server = windows_server()
        self.url = f"{API}/windows-servers/{self.server.pk}/test-connection/"

    def _post(self):
        return self.client.post(self.url, {}, format="json", **self.header)

    def test_second_post_is_a_409(self):
        self.assertEqual(self._post().status_code, 202)
        response = self._post()
        self.assertEqual(response.status_code, 409)
        self.assertIn("already queued", response.data["detail"])
        # R13: the answer names the waiting job, so a stuck one can be found and deleted.
        self.assertEqual(response.data["job"], self._jobs().get().pk)
        self.assertEqual(self._jobs().count(), 1)

    def test_409_without_a_job_when_it_just_started(self):
        # The waiting job may start between the refusal and the lookup that names it.
        with mock.patch("netbox_windows_dhcp_plugin.jobs.enqueue_test", return_value=None):
            response = self._post()
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(response.data["job"])

    def test_a_job_that_never_started_stops_blocking(self):
        self.assertEqual(self._post().status_code, 202)
        self._jobs().update(created=timezone.now() - timedelta(minutes=20))
        self.assertEqual(self._post().status_code, 202)
        self.assertEqual(self._jobs().count(), 2)

    def test_another_server_is_not_blocked(self):
        self.assertEqual(self._post().status_code, 202)
        other = windows_server("dhcp2")
        response = self.client.post(
            f"{API}/windows-servers/{other.pk}/test-connection/", {}, format="json", **self.header
        )
        self.assertEqual(response.status_code, 202)


class F66ConnectionTestViewTest(_TestJobsMixin, TestCase):
    def setUp(self):
        super().setUp()
        _superuser(self)
        self.server = windows_server()
        self.url = reverse("plugins:netbox_windows_dhcp_plugin:windowsserver_test", args=[self.server.pk])

    def test_second_click_warns_and_queues_nothing(self):
        first = self.client.post(self.url, follow=True)
        self.assertEqual([level for level, _ in _messages(first)], ["success"])
        second = self.client.post(self.url, follow=True)
        (message,) = _messages(second)
        self.assertEqual(message[0], "warning")
        self.assertIn("already queued", message[1])
        # R13: the warning names the waiting job and says what to do when no worker picks it up.
        self.assertIn(f"job #{self._jobs().get().pk}", message[1])
        self.assertIn("delete that job", message[1])
        self.assertEqual(self._jobs().count(), 1)

    def test_warning_stays_plain_when_the_waiting_job_just_started(self):
        # The waiting job may start between the refusal and the lookup that names it.
        with mock.patch("netbox_windows_dhcp_plugin.views.enqueue_test", return_value=None):
            response = self.client.post(self.url, follow=True)
        (message,) = _messages(response)
        self.assertEqual(message[0], "warning")
        self.assertIn("already queued", message[1])
        self.assertNotIn("job #", message[1])

    def test_a_new_test_can_be_queued_once_the_first_is_done(self):
        self.client.post(self.url)
        self._jobs().update(status="completed")
        self.client.post(self.url)
        self.assertEqual(self._jobs().count(), 2)


# -- F09 and F67 -------------------------------------------------------------------------------------------------


def _rows(server, partner, start, count):
    """One exclusion, policy, reservation, two option values and a failover relationship on each of ``count`` scopes."""
    for i in range(start, start + count):
        net = scope(server, f"10.{i}.0.0/24")
        ExclusionRange.objects.create(scope=net, start_address=f"10.{i}.0.20", end_address=f"10.{i}.0.25")
        WindowsPolicy.objects.create(server=server, scope=net, name=f"p{i}", processing_order=1)
        host = reservation(net, f"10.{i}.0.50/24", client_id=f"aa{i:02x}")
        OptionValue.objects.create(server=server, scope=net, code=3, value=[f"10.{i}.0.1"])
        OptionValue.objects.create(server=server, scope=net, reservation=host, code=6, value=["10.0.0.53"])
        relationship = FailoverRelationship.objects.create(name=f"ha{i}", primary_server=server, partner_server=partner)
        relationship.scopes.add(net)


class _ScalingMixin:
    """The number of queries of a list must not grow with the number of rows (each on a scope of its own)."""

    urls = ()

    def setUp(self):
        super().setUp()
        _superuser(self)
        self.server = windows_server()
        self.partner = windows_server("dhcp2")
        _rows(self.server, self.partner, 1, 2)

    def _count(self, url):
        with CaptureQueriesContext(connection) as queries:
            response = self._get(url)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_query_count_does_not_grow_with_rows(self):
        for url in self.urls:
            self._count(url)  # warm caches (content types, permissions)
        before = {url: self._count(url) for url in self.urls}
        _rows(self.server, self.partner, 3, 4)
        after = {url: self._count(url) for url in self.urls}
        self.assertEqual(after, before)


class F09NestedScopeAPITest(_ScalingMixin, PluginAPIMixin, APITestCase):
    urls = tuple(
        f"{API}/{name}/?limit=100"
        for name in ("exclusion-ranges", "reservations", "windows-policies", "option-values", "failover-relationships")
    )

    def _get(self, url):
        return self.client.get(url, **self.header)

    def test_rows_show_their_scope(self):
        response = self._get(f"{API}/option-values/?limit=100")
        self.assertEqual(
            sorted({row["scope"]["display"] for row in response.json()["results"]}),
            ["dhcp1/10.1.0.0", "dhcp1/10.2.0.0"],
        )


class F67ScopeColumnViewTest(_ScalingMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.urls = tuple(
            reverse(f"plugins:netbox_windows_dhcp_plugin:{name}_list") + "?per_page=100"
            for name in ("optionvalue", "windowspolicy", "failoverrelationship")
        )

    def _get(self, url):
        return self.client.get(url)

    def test_the_scope_column_is_shown(self):
        self.assertContains(self._get(self.urls[0]), "dhcp1/10.2.0.0")


# -- F17 ---------------------------------------------------------------------------------------------------------


class F17SearchDigitTest(PluginAPIMixin, APITestCase):
    """A character that looks like a digit but is not a number ("²") is searched as text, not turned into a code."""

    def setUp(self):
        super().setUp()
        _superuser(self)
        server = windows_server()
        OptionValue.objects.create(server=server, code=3, value=["10.0.0.1"])
        OptionValue.objects.create(server=server, code=6, value=["10.0.0.5"])
        OptionDefinition.objects.create(server=server, code=3, name="gateway")
        OptionDefinition.objects.create(server=server, code=224, name="custom")

    def test_filtersets_do_not_raise(self):
        for filterset, model in (
            (filtersets.OptionValueFilterSet, OptionValue),
            (filtersets.OptionDefinitionFilterSet, OptionDefinition),
        ):
            for text in ("²", "①", " ² "):
                self.assertEqual(list(filterset({"q": text}, model.objects.all()).qs), [])
            self.assertEqual([o.code for o in filterset({"q": "3"}, model.objects.all()).qs], [3])
            self.assertEqual([o.code for o in filterset({"q": " 3 "}, model.objects.all()).qs], [3])

    def test_api_and_ui_answer(self):
        for name in ("option-values", "option-definitions"):
            response = self.client.get(f"{API}/{name}/?q=%C2%B2", **self.header)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()["count"], 0)

    def test_forms_narrow_only_by_real_numbers(self):
        # The guards live in the CSV import forms, which narrow their querysets while being built: with "²" taken
        # for a number, building the form already raised.
        server = WindowsServer.objects.get()
        for form in (
            forms.OptionDefinitionImportForm({"server": server.name, "family": "²", "code": "224", "name": "x"}),
            forms.OptionValueImportForm(
                {"server": server.name, "family": "²", "scope": "²", "code": "3", "value": '["10.0.0.1"]'}
            ),
        ):
            self.assertFalse(form.is_valid())
            self.assertIn("family", form.errors)

    def test_import_forms_narrow_by_a_real_family(self):
        server = WindowsServer.objects.get()
        v4 = WindowsClass.objects.create(
            server=server, name="v4", class_type=ClassTypeChoices.VENDOR, family=4, data="a"
        )
        WindowsClass.objects.create(server=server, name="v6", class_type=ClassTypeChoices.VENDOR, family=6, data="b")
        form = forms.OptionDefinitionImportForm({"server": server.name, "family": "4", "code": "224", "name": "x"})
        self.assertEqual(list(form.fields["vendor_class"].queryset), [v4])


# -- F18 ---------------------------------------------------------------------------------------------------------


class F18HostKeyImportTest(TestCase):
    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGbM5L1hQdQHwPVzFXDyOcnRTMUXN0zXV0fRSC0c0b7w"

    def test_csv_import_sets_the_ssh_host_key(self):
        self.assertIn("ssh_host_key", forms.WindowsServerImportForm.Meta.fields)
        _superuser(self)
        csv = "\n".join(
            (
                "name,host,credential_profile,transport,verify_tls,ssh_host_key",
                f"ssh1,ssh1.example.com,lab,ssh,true,{self.KEY}",
            )
        )
        response = self.client.post(
            reverse("plugins:netbox_windows_dhcp_plugin:windowsserver_bulk_import"),
            {"data": csv, "format": "csv", "csv_delimiter": ","},
        )
        errors = response.context["form"].errors if response.context else None
        self.assertEqual(response.status_code, 302, errors)
        self.assertEqual(WindowsServer.objects.get(name="ssh1").ssh_host_key, self.KEY)


# -- F68 ---------------------------------------------------------------------------------------------------------


class _ProposalMixin:
    def setUp(self):
        super().setUp()
        _superuser(self)
        self.server, self.proposal = make_proposal()
        self.item = self.proposal.items.get(kind="scope")

    def assert_nothing_applied(self):
        self.assertFalse(NetBoxScope.objects.exists())
        self.assertFalse(SyncState.objects.exists())
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, PENDING)


class F68ApplyInBranchViewTest(_ProposalMixin, TestCase):
    """Apply is refused inside a branch, with the reason alone; dismiss still works."""

    def _url(self, name, *args):
        return reverse(f"plugins:netbox_windows_dhcp_plugin:{name}", args=args)

    def test_every_apply_view_refuses_with_one_error(self):
        for url, data in (
            (self._url("importproposalitem_apply", self.item.pk), {}),
            (self._url("importproposal_apply", self.proposal.pk), {}),
            (self._url("importproposalitem_bulk_apply"), {"pk": [self.item.pk]}),
        ):
            with mock.patch(IN_BRANCH, return_value=True):
                response = self.client.post(url, data, follow=True)
            self.assertEqual(_messages(response), [("danger", importer.IN_BRANCH)], url)
            self.assert_nothing_applied()

    def test_dismiss_still_works(self):
        with mock.patch(IN_BRANCH, return_value=True):
            response = self.client.post(self._url("importproposalitem_dismiss", self.item.pk), follow=True)
        self.assertEqual([level for level, _ in _messages(response)], ["success"])
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ProposalItemStatusChoices.DISMISSED)

    def test_on_main_it_applies(self):
        response = self.client.post(self._url("importproposalitem_apply", self.item.pk), follow=True)
        self.assertEqual([level for level, _ in _messages(response)], ["success"])
        self.assertTrue(NetBoxScope.objects.exists())


class F68ApplyInBranchAPITest(_ProposalMixin, PluginAPIMixin, APITestCase):
    def test_both_apply_actions_are_a_409(self):
        for url, data in (
            (f"{API}/import-proposal-items/apply/", {"ids": [self.item.pk]}),
            (f"{API}/import-proposals/{self.proposal.pk}/apply/", {}),
        ):
            with mock.patch(IN_BRANCH, return_value=True):
                response = self.client.post(url, data, format="json", **self.header)
            self.assertEqual(response.status_code, 409, url)
            self.assertEqual(response.data, {"detail": importer.IN_BRANCH})
            self.assert_nothing_applied()

    def test_dismiss_still_works(self):
        with mock.patch(IN_BRANCH, return_value=True):
            response = self.client.post(
                f"{API}/import-proposals/{self.proposal.pk}/dismiss/", {}, format="json", **self.header
            )
        self.assertEqual((response.status_code, response.data), (200, {"dismissed": 1}))

    def test_on_main_the_answer_is_unchanged(self):
        response = self.client.post(
            f"{API}/import-proposal-items/apply/", {"ids": [self.item.pk]}, format="json", **self.header
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"applied": 1, "failed": 0, "missing_permissions": []})


# -- F70 ---------------------------------------------------------------------------------------------------------


class F70SelectAllMatchingTest(TestCase):
    """With a filter that is not valid, "select all matching" acts on nothing: the list it came from shows nothing."""

    def setUp(self):
        super().setUp()
        _superuser(self)
        _, self.first = make_proposal("dhcp1", "10.1.0")
        _, self.second = make_proposal("dhcp2", "10.2.0")

    def _post(self, verb, query):
        url = reverse(f"plugins:netbox_windows_dhcp_plugin:importproposalitem_bulk_{verb}")
        return self.client.post(f"{url}?{query}", {"_all": "1"}, follow=True)

    def _statuses(self, proposal):
        return set(ImportProposalItem.objects.filter(proposal=proposal).values_list("status", flat=True))

    def test_a_deleted_proposal_selects_nothing(self):
        gone = self.second.pk + 1000
        for verb in ("apply", "dismiss"):
            response = self._post(verb, f"proposal_id={gone}")
            self.assertIn("danger", [level for level, _ in _messages(response)], verb)
            self.assertEqual(self._statuses(self.first) | self._statuses(self.second), {PENDING}, verb)
        self.assertFalse(NetBoxScope.objects.exists())
        self.assertFalse(SyncState.objects.exists())

    def test_an_invalid_status_selects_nothing(self):
        self._post("dismiss", f"proposal_id={self.first.pk}&status=nonsense")
        self.assertEqual(self._statuses(self.first) | self._statuses(self.second), {PENDING})

    def test_a_valid_filter_touches_only_that_proposal(self):
        response = self._post("dismiss", f"proposal_id={self.first.pk}")
        self.assertEqual([level for level, _ in _messages(response)], ["success"])
        self.assertEqual(self._statuses(self.first), {ProposalItemStatusChoices.DISMISSED})
        self.assertEqual(self._statuses(self.second), {PENDING})

    def test_no_filter_still_means_every_open_item(self):
        self._post("dismiss", "per_page=50&sort=key")
        self.assertEqual(
            self._statuses(self.first) | self._statuses(self.second), {ProposalItemStatusChoices.DISMISSED}
        )

    def test_listed_ids_ignore_anything_that_is_not_a_number(self):
        item = self.first.items.get()
        url = reverse("plugins:netbox_windows_dhcp_plugin:importproposalitem_bulk_dismiss")
        self.client.post(url, {"pk": [str(item.pk), "²", "x"]})
        self.assertEqual(self._statuses(self.first), {ProposalItemStatusChoices.DISMISSED})
        self.assertEqual(self._statuses(self.second), {PENDING})
