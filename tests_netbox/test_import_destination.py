"""
Choosing where an import writes (issue #36): an import branch or main with netbox-branching, a proposal without it.

The first classes stub branching (what is installed, the active branch, change control's settings) to check the
choice, its checks and what the pages say. DirectImportTest runs real direct imports, with netbox-branching
installed, against the fake Windows server.
"""

import uuid
from types import SimpleNamespace
from unittest import mock, skipUnless

from core.choices import JobStatusChoices
from core.models import Job, ObjectType
from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from netbox_dhcp.models import HostReservation, Subnet
from utilities.testing import APITestCase, TestCase

from netbox_windows_dhcp_plugin import import_status
from netbox_windows_dhcp_plugin.choices import ImportDestinationChoices, SyncKindChoices, SyncStatusChoices
from netbox_windows_dhcp_plugin.integrations import branching
from netbox_windows_dhcp_plugin.jobs import ImportJob, ImportRefused, enqueue_import, import_refusal
from netbox_windows_dhcp_plugin.models import ImportProposal, SyncRun, SyncState

from .fixtures import PLUGIN, PluginAPIMixin, dhcp_servers, ensure_standard_option_definitions, windows_server
from .test_import import seed
from .test_import_status import panel
from .test_jobs import CREDENTIALS

BRANCHING = "netbox_windows_dhcp_plugin.integrations.branching.branching_installed"
CHANGE_CONTROL = "netbox_windows_dhcp_plugin.integrations.branching.change_control_installed"
ACTIVE_BRANCH = "netbox_windows_dhcp_plugin.integrations.branching.active_branch_name"
MISSING = "netbox_windows_dhcp_plugin.integrations.branching.missing_import_permissions"
CURRENT = ImportDestinationChoices.CURRENT
MAIN = ImportDestinationChoices.MAIN
BRANCH = ImportDestinationChoices.BRANCH
PROPOSAL = ImportDestinationChoices.PROPOSAL


# Tests that stub "branching installed" but reach netbox-branching's own code or settings run only where it is.
needs_branching = skipUnless(apps.is_installed("netbox_branching"), "netbox-branching is not installed")
needs_change_control = skipUnless(apps.is_installed("netbox_change_control"), "netbox-change-control is not installed")


def import_jobs():
    # Only import jobs: a fresh test database also holds NetBox's system jobs.
    return Job.objects.filter(name=ImportJob.name)


def plugins_config(**settings):
    """PLUGINS_CONFIG with these settings for this plugin (credentials kept) and change control's protect_main."""
    protect = settings.pop("protect_main", None)
    config = {**CREDENTIALS, PLUGIN: {**CREDENTIALS[PLUGIN], **settings}}
    if protect is not None:
        config["netbox_change_control"] = {"protect_main": protect[0], "protect_main_scope": protect[1]}
    return config


def patch(test, target, value):
    patcher = mock.patch(target, return_value=value)
    patcher.start()
    test.addCleanup(patcher.stop)


class DestinationRulesTest(TestCase):
    """What import_refusal allows and why it refuses, without touching any page."""

    def setUp(self):
        super().setUp()
        patch(self, BRANCHING, True)
        patch(self, CHANGE_CONTROL, False)

    def test_branching_offers_branch_first_then_main(self):
        self.assertEqual(branching.import_destinations(), [BRANCH, CURRENT, MAIN])
        self.assertIsNone(import_refusal(BRANCH, self.user))
        self.assertIsNone(import_refusal(MAIN, self.user))

    def test_without_branching_only_a_proposal(self):
        with mock.patch(BRANCHING, return_value=False):
            self.assertEqual(branching.import_destinations(), [PROPOSAL])
            self.assertIn("cannot import into 'main'", import_refusal(MAIN, self.user))
            self.assertIn("cannot import into 'branch'", import_refusal(BRANCH, self.user))
            self.assertIsNone(import_refusal(PROPOSAL, None))

    def test_unknown_destination_is_refused_not_replaced(self):
        self.assertIn("choose one of: branch, current, main", import_refusal("elsewhere", self.user))
        self.assertIn("cannot import into 'proposal'", import_refusal(PROPOSAL, self.user))

    def test_main_needs_a_user(self):
        self.assertEqual(import_refusal(MAIN, None), "an import into main needs a user")
        self.assertEqual(import_refusal(CURRENT, None, "x"), "an import into a chosen branch needs a user")

    @needs_branching
    def test_chosen_branch_must_be_named_and_visible(self):
        self.assertEqual(import_refusal(CURRENT, self.user), "name the branch to import into")
        with mock.patch(
            "netbox_windows_dhcp_plugin.integrations.branching.chosen_branch", return_value=(None, "nope")
        ) as lookup:
            self.assertEqual(import_refusal(CURRENT, self.user, "feature-x"), "nope")
        lookup.assert_called_once_with("feature-x", self.user)

    @override_settings(PLUGINS_CONFIG=plugins_config(import_direct=False))
    def test_setting_turns_direct_imports_off(self):
        self.assertIn("import_direct", import_refusal(MAIN, self.user))
        self.assertIsNone(import_refusal(BRANCH, self.user))

    @needs_change_control
    @override_settings(PLUGINS_CONFIG=plugins_config(protect_main=(True, [])))
    def test_protect_main_refuses_without_bypass(self):
        with mock.patch(CHANGE_CONTROL, return_value=True):
            self.assertIn("protect_main", import_refusal(MAIN, self.user))
            self.add_permissions("netbox_change_control.bypass_policy")
            self.assertIsNone(import_refusal(MAIN, get_user_model().objects.get(pk=self.user.pk)))

    def test_protect_main_scope_decides(self):
        cases = [
            ([], True),
            (["netbox_dhcp.*"], True),
            (["netbox_dhcp.subnet"], True),
            (["ipam.*"], True),
            (["IPAM.Prefix"], True),
            (["dcim.macaddress"], True),
            (["netbox_windows_dhcp_plugin.windowsscope"], True),
            (["dcim.site", "circuits.*"], False),
            (["dcim.*"], True),
        ]
        for scope, refused in cases:
            with (
                self.subTest(scope=scope),
                mock.patch(CHANGE_CONTROL, return_value=True),
                override_settings(PLUGINS_CONFIG=plugins_config(protect_main=(True, scope))),
            ):
                self.assertEqual(import_refusal(MAIN, self.user) is not None, refused)

    @override_settings(PLUGINS_CONFIG=plugins_config(protect_main=(False, [])))
    def test_protect_main_off_allows_direct(self):
        with mock.patch(CHANGE_CONTROL, return_value=True):
            self.assertIsNone(import_refusal(MAIN, self.user))

    def test_main_needs_no_branch_permissions(self):
        self.assertEqual(branching.missing_import_permissions(self.user, MAIN), [])
        self.assertEqual(branching.missing_import_permissions(self.user, CURRENT), [])
        self.assertIn("netbox_branching.add_branch", branching.missing_import_permissions(self.user, BRANCH))
        self.assertIn("netbox_branching.add_branch", branching.missing_import_permissions(self.user))


@needs_branching
class EnqueueTest(TestCase):
    def setUp(self):
        super().setUp()
        patch(self, BRANCHING, True)
        patch(self, CHANGE_CONTROL, False)
        self.server = windows_server(dhcp_servers(1)[0])

    def test_destination_travels_with_the_job(self):
        with mock.patch.object(ImportJob, "enqueue", wraps=ImportJob.enqueue) as enqueue:
            job = enqueue_import(self.server, self.user, MAIN)
        self.assertEqual(enqueue.call_args.kwargs["destination"], MAIN)
        job.refresh_from_db()
        self.assertEqual(job.data, {"destination": MAIN})

    def test_default_is_the_branch(self):
        job = enqueue_import(self.server, self.user)
        job.refresh_from_db()
        self.assertEqual(job.data, {"destination": BRANCH})

    def test_chosen_branch_travels_with_the_job(self):
        chosen = "netbox_windows_dhcp_plugin.integrations.branching.chosen_branch"
        with (
            mock.patch(chosen, return_value=(object(), None)),
            mock.patch.object(ImportJob, "enqueue", wraps=ImportJob.enqueue) as enqueue,
        ):
            job = enqueue_import(self.server, self.user, CURRENT, "feature-x")
        self.assertEqual(enqueue.call_args.kwargs["branch"], "feature-x")
        job.refresh_from_db()
        self.assertEqual(job.data, {"destination": CURRENT, "branch": "feature-x"})

    def test_refused_destination_queues_nothing(self):
        with self.assertRaises(ImportRefused):
            enqueue_import(self.server, self.user, "elsewhere")
        with self.assertRaises(ImportRefused):
            enqueue_import(self.server, None, MAIN)
        self.assertFalse(import_jobs().exists())


class ImportViewTest(TestCase):
    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        patch(self, BRANCHING, True)
        patch(self, CHANGE_CONTROL, False)
        patch(self, MISSING, [])
        self.server = windows_server(dhcp_servers(1)[0])
        self.url = reverse(f"plugins:{PLUGIN}:windowsserver_import", args=[self.server.pk])

    def post(self, data):
        with mock.patch("netbox_windows_dhcp_plugin.views.enqueue_import", return_value=SimpleNamespace(pk=42)) as q:
            response = self.client.post(self.url, data, follow=True)
        return q, response, " ".join(str(m) for m in response.context["messages"])

    def test_dropdown_offers_both_destinations(self):
        html = self.client.get(self.server.get_absolute_url()).content.decode()
        self.assertIn("Import into branch", html)
        self.assertIn("Stage changes for review", html)
        self.assertIn("Import directly", html)
        self.assertIn("Apply imported configuration to main", html)
        self.assertIn('name="destination" value="branch"', html)
        # Main is a link to the confirmation page, never a one-click POST.
        self.assertIn(f'href="{self.url}?destination=main"', html)
        self.assertNotIn('value="main"', html)
        # On main there is no branch to import into.
        self.assertNotIn("Import into this branch", html)

    def test_in_a_branch_the_dropdown_offers_this_branch(self):
        with mock.patch(ACTIVE_BRANCH, return_value="feature-x"):
            html = self.client.get(self.server.get_absolute_url()).content.decode()
        self.assertIn("Import into this branch", html)
        self.assertIn("Write into feature-x, no new branch", html)
        self.assertIn('name="destination" value="current"', html)
        self.assertIn('name="branch" value="feature-x"', html)
        self.assertIn("Import into branch", html)
        self.assertIn("Import directly", html)

    def test_this_branch_is_queued_with_its_name(self):
        with (
            mock.patch(ACTIVE_BRANCH, return_value="feature-x"),
            mock.patch("netbox_windows_dhcp_plugin.views.import_refusal", return_value=None),
        ):
            q, _, text = self.post({"destination": CURRENT, "branch": "feature-x"})
        q.assert_called_once_with(self.server, self.user, CURRENT, "feature-x")
        self.assertIn("into branch feature-x, not into main. No new branch is created", text)

    def test_this_branch_must_still_be_the_active_one(self):
        with mock.patch(ACTIVE_BRANCH, return_value="feature-y"):
            q, _, text = self.post({"destination": CURRENT, "branch": "feature-x"})
        q.assert_not_called()
        self.assertIn("you are no longer working in branch feature-x", text)
        q, _, text = self.post({"destination": CURRENT, "branch": "feature-x"})  # now on main
        q.assert_not_called()

    @override_settings(PLUGINS_CONFIG=plugins_config(import_direct=False))
    def test_dropdown_disables_a_refused_direct_import_and_says_why(self):
        html = self.client.get(self.server.get_absolute_url()).content.decode()
        self.assertIn("Import into branch", html)
        self.assertIn('class="dropdown-item disabled"', html)
        self.assertIn("Direct imports are turned off", html)
        self.assertNotIn(f"{self.url}?destination=main", html)

    def test_without_branching_one_button_for_a_proposal(self):
        with mock.patch(BRANCHING, return_value=False):
            html = self.client.get(self.server.get_absolute_url()).content.decode()
            q, _, text = self.post({})
        self.assertIn("Import now", html)
        self.assertNotIn("Import directly", html)
        self.assertNotIn("Import into branch", html)
        q.assert_called_once_with(self.server, self.user, PROPOSAL, "")
        self.assertIn("import proposal", text)

    def test_branch_is_queued_into_a_branch(self):
        q, _, text = self.post({"destination": BRANCH})
        q.assert_called_once_with(self.server, self.user, BRANCH, "")
        self.assertIn("It writes into an import branch, not into main", text)

    def test_main_without_confirmation_queues_nothing(self):
        q, response, _ = self.post({"destination": MAIN})
        q.assert_not_called()
        self.assertTemplateUsed(response, "netbox_windows_dhcp_plugin/import_main_confirm.html")

    def test_confirmation_page_names_main(self):
        response = self.client.get(f"{self.url}?destination=main")
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("straight into main", html)
        self.assertIn('name="destination" value="main"', html)
        self.assertIn("csrfmiddlewaretoken", html)
        self.assertNotIn("You are working in branch", html)

    def test_confirmation_page_warns_about_the_active_branch(self):
        with mock.patch(ACTIVE_BRANCH, return_value="feature-x"):
            html = self.client.get(f"{self.url}?destination=main").content.decode()
        self.assertIn("You are working in branch <strong>feature-x</strong>", html)
        self.assertIn("it writes into main, not into feature-x", html)

    def test_confirmation_page_warns_about_an_open_import_branch(self):
        with mock.patch(
            "netbox_windows_dhcp_plugin.integrations.branching.open_import_branch_name",
            return_value="dhcp-import/dhcp1/20261022-101010",
        ):
            html = self.client.get(f"{self.url}?destination=main").content.decode()
        self.assertIn("Import branch <strong>dhcp-import/dhcp1/20261022-101010</strong> is still open", html)

    def test_confirmed_main_is_queued_into_main(self):
        q, _, text = self.post({"destination": MAIN, "_confirm": ""})
        q.assert_called_once_with(self.server, self.user, MAIN, "")
        self.assertIn("directly into main. Nothing has been imported yet", text)

    @override_settings(PLUGINS_CONFIG=plugins_config(import_direct=False))
    def test_refused_main_is_explained(self):
        response = self.client.get(f"{self.url}?destination=main", follow=True)
        self.assertIn("Cannot import directly into main", " ".join(str(m) for m in response.context["messages"]))
        response = self.client.post(self.url, {"destination": MAIN, "_confirm": ""}, follow=True)
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("Import not queued: direct imports are turned off", text)
        # Refused between the check and the queue: a fixed message, never the exception's text.
        with (
            mock.patch("netbox_windows_dhcp_plugin.views.import_refusal", return_value=None),
            mock.patch("netbox_windows_dhcp_plugin.views.enqueue_import", side_effect=ImportRefused("internal")),
        ):
            response = self.client.post(self.url, {"destination": MAIN, "_confirm": ""}, follow=True)
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("Import not queued: it was refused, try again", text)
        self.assertNotIn("internal", text)

    def test_unknown_destination_queues_nothing(self):
        response = self.client.post(self.url, {"destination": "elsewhere"}, follow=True)
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("Import not queued", text)
        self.assertFalse(import_jobs().exists())

    def test_needs_change_on_the_server(self):
        self.user.is_superuser = False
        self.user.save()
        self.add_permissions(f"{PLUGIN}.view_windowsserver")
        response = self.client.post(self.url, {"destination": MAIN, "_confirm": ""})
        self.assertIn(response.status_code, (403, 404))
        self.assertFalse(import_jobs().exists())


@needs_branching
class ImportActionAPITest(PluginAPIMixin, APITestCase):
    def setUp(self):
        super().setUp()
        patch(self, BRANCHING, True)
        patch(self, CHANGE_CONTROL, False)
        self.server = windows_server(dhcp_servers(1)[0])
        self.url = f"/api/plugins/windows-dhcp/windows-servers/{self.server.pk}/import/"
        self.add_permissions(f"{PLUGIN}.view_windowsserver", f"{PLUGIN}.change_windowsserver")

    def test_main_is_queued_with_its_destination(self):
        response = self.client.post(self.url, {"destination": MAIN}, format="json", **self.header)
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(import_jobs().get().data, {"destination": MAIN})

    def test_branch_still_needs_branch_permissions(self):
        response = self.client.post(self.url, {"destination": BRANCH}, format="json", **self.header)
        self.assertEqual(response.status_code, 403)
        self.assertIn("add_branch", response.content.decode())

    def test_current_needs_a_branch(self):
        response = self.client.post(self.url, {"destination": CURRENT}, format="json", **self.header)
        self.assertEqual(response.status_code, 400)
        self.assertIn("branch", response.json())

    def test_current_with_a_branch_name(self):
        chosen = "netbox_windows_dhcp_plugin.integrations.branching.chosen_branch"
        with mock.patch(chosen, return_value=(object(), None)):
            response = self.client.post(
                self.url, {"destination": CURRENT, "branch": "feature-x"}, format="json", **self.header
            )
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(import_jobs().get().data, {"destination": CURRENT, "branch": "feature-x"})

    def test_current_with_an_unknown_branch_is_a_403(self):
        response = self.client.post(self.url, {"destination": CURRENT, "branch": "nope"}, format="json", **self.header)
        self.assertEqual(response.status_code, 403)
        self.assertIn("does not exist", response.content.decode())

    def test_unknown_destination_is_a_400(self):
        response = self.client.post(self.url, {"destination": "elsewhere"}, format="json", **self.header)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(import_jobs().exists())

    @override_settings(PLUGINS_CONFIG=plugins_config(import_direct=False))
    def test_refused_main_is_a_403(self):
        response = self.client.post(self.url, {"destination": MAIN}, format="json", **self.header)
        self.assertEqual(response.status_code, 403)
        self.assertIn("import_direct", response.content.decode())
        self.assertFalse(import_jobs().exists())


class StandaloneImportActionAPITest(PluginAPIMixin, APITestCase):
    def setUp(self):
        super().setUp()
        patch(self, BRANCHING, False)
        self.server = windows_server(dhcp_servers(1)[0])
        self.url = f"/api/plugins/windows-dhcp/windows-servers/{self.server.pk}/import/"
        self.add_permissions(f"{PLUGIN}.view_windowsserver", f"{PLUGIN}.change_windowsserver")

    def test_without_branching_branch_and_main_are_400s(self):
        for destination in (BRANCH, MAIN):
            response = self.client.post(self.url, {"destination": destination}, format="json", **self.header)
            self.assertEqual(response.status_code, 400, destination)
        self.assertFalse(import_jobs().exists())


@needs_branching
class DirectImportStatusTest(TestCase):
    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        patch(self, BRANCHING, True)
        patch(self, CHANGE_CONTROL, False)
        self.server = windows_server(dhcp_servers(1)[0])

    def job(self, status, destination=None):
        return Job.objects.create(
            object=self.server,
            name=ImportJob.name,
            status=status,
            job_id=uuid.uuid4(),
            data={"destination": destination} if destination else None,
        )

    def run_(self, status=SyncStatusChoices.COMPLETED, **summary):
        return SyncRun.objects.create(
            windows_server=self.server,
            kind=SyncKindChoices.IMPORT,
            status=status,
            job=self.job(JobStatusChoices.STATUS_COMPLETED),
            destination=MAIN,
            summary=summary,
            error="update scope 10.1.0.0: no" if status == SyncStatusChoices.FAILED else "",
        )

    def page(self):
        return panel(self.client.get(self.server.get_absolute_url()).content.decode())

    def test_queued_direct_import_says_main(self):
        self.job(JobStatusChoices.STATUS_PENDING, MAIN)
        status = import_status.for_server(self.server, self.user)
        self.assertEqual(status.next_destination, MAIN)
        html = self.page()
        self.assertIn("Nothing has been imported yet.", html)
        self.assertIn("It will write directly into main.", html)
        self.assertNotIn("import branch", html)

    def test_queued_import_into_a_chosen_branch_names_it(self):
        job = self.job(JobStatusChoices.STATUS_PENDING)
        job.data = {"destination": CURRENT, "branch": "feature-x"}
        job.save()
        self.assertIn("It will write into branch feature-x, not into main.", self.page())

    def test_completed_import_into_a_chosen_branch(self):
        run = self.run_(changes=2)
        run.destination, run.branch_name = CURRENT, "feature-x"
        run.save()
        branch = SimpleNamespace(
            status="ready",
            get_absolute_url=lambda: "/plugins/branching/branches/9/",
            get_status_color=lambda: "green",
            get_status_display=lambda: "Ready",
        )
        with mock.patch(
            "netbox_windows_dhcp_plugin.integrations.branching.import_branch", return_value=(branch, None, True)
        ):
            html = self.page()
        self.assertIn("Imported 2 differences into the branch you chose.", html)
        self.assertIn("Main has not been changed.", html)
        self.assertIn('href="/plugins/branching/branches/9/">feature-x</a>', html)
        self.assertNotIn("new branch", html)

    def test_running_import_into_a_chosen_branch_names_it(self):
        run = self.run_(status=SyncStatusChoices.RUNNING)
        run.destination, run.branch_name, run.job.status = CURRENT, "feature-x", JobStatusChoices.STATUS_RUNNING
        run.job.save()
        run.save()
        html = self.page()
        self.assertIn("It writes into branch feature-x, not into main.", html)
        self.assertNotIn("import branch", html)

    def test_a_chosen_branch_is_not_called_an_earlier_import_branch(self):
        run = self.run_(changes=1)
        run.destination, run.branch_name = CURRENT, "feature-x"
        run.save()
        self.run_(status=SyncStatusChoices.FAILED)
        lookup = "netbox_windows_dhcp_plugin.integrations.branching.import_branch"
        with mock.patch(lookup, return_value=(SimpleNamespace(status="ready"), None, True)) as found:
            status = import_status.for_server(self.server, self.user)
        self.assertIsNone(status.earlier_branch)
        found.assert_not_called()

    def test_queued_job_without_a_destination_is_a_branch_import(self):
        self.job(JobStatusChoices.STATUS_PENDING)
        self.assertIn("It will write into an import branch, not into main.", self.page())

    def test_completed_direct_import(self):
        self.run_(changes=3, applied=3, failed=0)
        html = self.page()
        self.assertIn("Imported 3 differences directly into main.", html)
        self.assertIn("No branch or change request was used.", html)
        self.assertNotIn("Main has not been changed", html)
        self.assertIn("Destination", html)

    def test_direct_import_with_nothing_to_do(self):
        self.run_(changes=0)
        self.assertIn("No differences found: main matched Windows when this import ran.", self.page())

    def test_partly_failed_direct_import_says_what_reached_main(self):
        self.run_(status=SyncStatusChoices.FAILED, changes=3, applied=2, failed=1)
        html = self.page()
        self.assertIn("The import failed.", html)
        self.assertIn("It wrote 2 changes into main; the changes below failed.", html)

    def test_direct_import_that_wrote_nothing(self):
        self.run_(status=SyncStatusChoices.FAILED, changes=1, applied=0, failed=1)
        self.assertIn("Nothing was written into main.", self.page())

    def test_direct_import_that_stopped_early(self):
        self.run_(status=SyncStatusChoices.FAILED)
        self.assertIn("Anything it wrote before failing stays in main", self.page())

    def test_sync_run_list_shows_and_filters_the_destination(self):
        run = self.run_(changes=0)
        link = f'href="{run.get_absolute_url()}"'
        html = self.client.get(reverse(f"plugins:{PLUGIN}:syncrun_list")).content.decode()
        self.assertIn("Destination", html)
        self.assertIn(link, html)
        url = reverse(f"plugins:{PLUGIN}:syncrun_list")
        self.assertIn(link, self.client.get(f"{url}?destination=main").content.decode())
        self.assertNotIn(link, self.client.get(f"{url}?destination=branch").content.decode())


@skipUnless(apps.is_installed("netbox_branching"), "netbox-branching is not installed")
@override_settings(PLUGINS_CONFIG=CREDENTIALS)
class DirectImportTest(TransactionTestCase):
    """Real imports into main with netbox-branching installed."""

    serialized_rollback = True

    def setUp(self):
        from netbox_windows_dhcp_plugin.windows.fake import FakeDhcpServer

        ensure_standard_option_definitions()
        self.server = windows_server(dhcp_servers(1)[0])
        self.fake = FakeDhcpServer()
        seed(self.fake)
        self.user = get_user_model().objects.create_user(username=f"direct-{uuid.uuid4().hex[:6]}", is_superuser=True)
        ImportJob.runner_factory = staticmethod(lambda server: self.fake)
        self.addCleanup(setattr, ImportJob, "runner_factory", None)
        self.addCleanup(self._drop_branches)

    def _drop_branches(self):
        from django.db import connections
        from netbox_branching.models import Branch

        for branch in Branch.objects.all():
            connections[branch.connection_name].close()
            branch.delete()

    def _user_branch(self, name="feature-x"):
        from netbox_branching.models import Branch

        branch = Branch(name=name, owner=self.user)
        branch.save(provision=False)
        branch.provision(user=self.user)
        branch.refresh_from_db()
        return branch

    def _run(self, destination, user=None, branch=""):
        job = Job.objects.create(
            object=self.server,
            name=ImportJob.name,
            status=JobStatusChoices.STATUS_RUNNING,
            job_id=uuid.uuid4(),
            user=user or self.user,
        )
        try:
            ImportJob(job).run(destination=destination, branch=branch)
        except Exception as exc:
            from core.exceptions import JobFailed

            if not isinstance(exc, JobFailed):
                raise
        return self.server.sync_runs.get(job=job)

    def test_direct_import_writes_main_without_a_branch_or_a_push(self):
        from core.models import ObjectChange
        from netbox_branching.models import Branch

        with mock.patch("netbox_windows_dhcp_plugin.signals.queue_pushes") as pushes:
            run = self._run(MAIN)
        self.assertEqual((run.status, run.destination, run.branch_name), (SyncStatusChoices.COMPLETED, MAIN, ""))
        self.assertEqual(run.summary["failed"], 0)
        self.assertEqual(run.summary["applied"], run.summary["changes"])
        # Only proposal imports count dismissals (issue #43).
        self.assertNotIn("review", run.summary)
        self.assertEqual(Subnet.objects.get().name, "dhcp1/10.1.0.0")
        self.assertEqual(HostReservation.objects.count(), 1)
        self.assertFalse(Branch.objects.exists())
        self.assertFalse(ImportProposal.objects.exists())
        pushes.assert_not_called()
        # The objects are managed from now on, and the change log says who imported them.
        self.assertTrue(SyncState.objects.filter(windows_server=self.server, kind="scope", key="10.1.0.0").exists())
        self.assertTrue(ObjectChange.objects.filter(user=self.user, changed_object_type__model="subnet").exists())
        # Nothing left to import.
        again = self._run(MAIN)
        self.assertEqual(again.summary["changes"], 0)

    def test_direct_import_ignores_the_active_branch(self):
        from netbox_branching.models import Branch
        from netbox_branching.utilities import activate_branch

        from netbox_windows_dhcp_plugin.integrations.branching import create_import_branch

        branch = create_import_branch(self.server, self.user, description="someone's work")
        with activate_branch(branch):
            run = self._run(MAIN)
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED)
        self.assertEqual(Subnet.objects.count(), 1)  # in main
        with activate_branch(branch):
            self.assertFalse(Subnet.objects.exists())  # not in the branch
        self.assertEqual(Branch.objects.count(), 1)

    def test_branch_import_still_leaves_main_alone(self):
        run = self._run(BRANCH)
        self.assertEqual((run.status, run.destination), (SyncStatusChoices.COMPLETED, BRANCH))
        self.assertTrue(run.branch_name)
        self.assertFalse(Subnet.objects.exists())

    def test_import_into_the_users_branch_creates_no_other(self):
        from django.db import connections
        from netbox_branching.models import Branch
        from netbox_branching.utilities import activate_branch

        branch = self._user_branch()
        run = self._run(CURRENT, branch="feature-x")
        self.assertEqual(run.status, SyncStatusChoices.COMPLETED, run.error)
        self.assertEqual((run.destination, run.branch_name), (CURRENT, "feature-x"))
        self.assertFalse(run.summary["branch_created"])
        self.assertNotIn("review", run.summary)
        self.assertEqual(list(Branch.objects.values_list("name", flat=True)), ["feature-x"])
        self.assertFalse(Subnet.objects.exists())  # main is untouched
        with activate_branch(branch):
            self.assertEqual(Subnet.objects.get().name, "dhcp1/10.1.0.0")
        connections[branch.connection_name].close()
        if apps.is_installed("netbox_change_control"):
            from netbox_change_control.models import ChangeRequest

            self.assertFalse(ChangeRequest.objects.exists())
        # Running again finds the branch already matches Windows.
        again = self._run(CURRENT, branch="feature-x")
        self.assertEqual((again.status, again.summary["changes"]), (SyncStatusChoices.COMPLETED, 0))

    def test_import_into_a_branch_the_user_may_not_write_fails_each_change(self):
        from django.db import connections
        from netbox_branching.utilities import activate_branch
        from users.models import ObjectPermission

        branch = self._user_branch()
        user = get_user_model().objects.create_user(username=f"viewer-{uuid.uuid4().hex[:6]}")
        perm = ObjectPermission.objects.create(name=f"view branches {user.username}", actions=["view"])
        perm.object_types.add(ObjectType.objects.get_by_natural_key("netbox_branching", "branch"))
        perm.users.add(user)
        run = self._run(CURRENT, user=user, branch="feature-x")
        self.assertEqual(run.status, SyncStatusChoices.FAILED)
        self.assertIn("permission", run.error)
        with activate_branch(branch):
            self.assertFalse(Subnet.objects.exists())
        connections[branch.connection_name].close()

    def test_import_into_a_missing_branch_fails(self):
        run = self._run(CURRENT, branch="gone")
        self.assertEqual(run.status, SyncStatusChoices.FAILED)
        self.assertIn("does not exist", run.error)

    def test_objects_the_user_may_not_write_are_skipped(self):
        user = get_user_model().objects.create_user(username=f"limited-{uuid.uuid4().hex[:6]}")
        run = self._run(MAIN, user=user)
        self.assertEqual(run.status, SyncStatusChoices.FAILED)
        self.assertEqual(run.summary["applied"], 0)
        self.assertIn("permission", run.error)
        self.assertFalse(Subnet.objects.exists())
        self.assertFalse(SyncState.objects.exists())

    def test_refused_direct_import_says_nothing_was_written(self):
        with override_settings(PLUGINS_CONFIG=plugins_config(import_direct=False)):
            run = self._run(MAIN)
        self.assertEqual((run.status, run.summary), (SyncStatusChoices.FAILED, {"applied": 0}))
        status = import_status.for_run(run, self.user)
        self.assertEqual(status.applied, 0)

    def test_rolled_back_changes_send_no_events(self):
        user = get_user_model().objects.create_user(username=f"limited-{uuid.uuid4().hex[:6]}")
        with mock.patch("netbox.context_managers.flush_events") as flush:
            run = self._run(MAIN, user=user)
        self.assertEqual(run.summary["applied"], 0)
        sent = [event for call in flush.call_args_list for event in call.args[0]]
        self.assertEqual(sent, [])

    def test_subnet_ids_count_branches_imported_into_by_choice(self):
        from django.db import connections
        from netbox_branching.utilities import activate_branch

        branch = self._user_branch()
        self.assertEqual(self._run(CURRENT, branch="feature-x").status, SyncStatusChoices.COMPLETED)
        with activate_branch(branch):
            in_branch = Subnet.objects.get().subnet_id
        connections[branch.connection_name].close()
        # Another server imported straight into main afterwards must not reuse that ID.
        self.server = windows_server(dhcp_servers(1, start=2)[0])
        self.assertEqual(self._run(MAIN).status, SyncStatusChoices.COMPLETED)
        self.assertGreater(Subnet.objects.get().subnet_id, in_branch)

    def test_protect_main_refuses_at_run_time(self):
        config = plugins_config(protect_main=(True, []))
        with (
            override_settings(PLUGINS_CONFIG=config),
            mock.patch(CHANGE_CONTROL, return_value=True),
        ):
            user = get_user_model().objects.create_user(username=f"guarded-{uuid.uuid4().hex[:6]}")
            run = self._run(MAIN, user=user)
        self.assertEqual(run.status, SyncStatusChoices.FAILED)
        self.assertIn("protect_main", run.error)
        self.assertFalse(Subnet.objects.exists())

    @skipUnless(apps.is_installed("netbox_change_control"), "netbox-change-control is not installed")
    def test_protect_main_receiver_refuses_each_save(self):
        # Change control's own receiver judges every save as the importing user (without bypass_policy here). With the
        # up-front check skipped, as if the settings changed mid-run, each refused save fails its change, not the job.
        user = get_user_model().objects.create_user(username=f"nobypass-{uuid.uuid4().hex[:6]}")
        config = plugins_config(protect_main=(True, []))
        with (
            override_settings(PLUGINS_CONFIG=config),
            mock.patch("netbox_windows_dhcp_plugin.jobs.import_refusal", return_value=None),
            mock.patch("netbox_windows_dhcp_plugin.sync.mapping.Writer._check"),
        ):
            run = self._run(MAIN, user=user)
        self.assertEqual(run.status, SyncStatusChoices.FAILED)
        self.assertIn("Direct changes to main are disabled", run.error)
        self.assertFalse(Subnet.objects.exists())
