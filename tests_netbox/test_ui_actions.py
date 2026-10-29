"""
The buttons on the plugin's pages, checked in the rendered HTML rather than only through the HTTP status.

Two bugs got past tests that only looked for a 200: the plugin's POST buttons (Sync now, Dry run, Import now, Test
connection, Apply, Dismiss) rendered without a CSRF token, so the browser got a 403 (issue #19), and read-only pages
such as a sync run showed Edit, Clone or Add buttons whose link was "None" (issue #23).
"""

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from django.test import Client
from django.urls import Resolver404, resolve, reverse
from netbox.registry import registry
from utilities.testing import TestCase
from utilities.views import get_viewname

from netbox_windows_dhcp_plugin.choices import ProposalStatusChoices, SyncKindChoices
from netbox_windows_dhcp_plugin.models import ImportProposal, ImportProposalItem, SyncRun

from .fixtures import PLUGIN
from .test_review import make_proposal

PLUGIN_URL = "/plugins/windows-dhcp/"
# The plugin's own POST actions, by the last part of their URL.
POST_ACTION = re.compile(r"^/plugins/windows-dhcp/[^?]+/(test|push|dry-run|import|apply|dismiss)/(\?.*)?$")
# Attributes that hold a URL the user can follow or submit to.
URL_ATTRIBUTES = ("href", "action", "formaction", "hx-get", "hx-post")


class Page(HTMLParser):
    """The forms of a rendered page (action, method, inputs, submit buttons) and every URL in it."""

    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.urls = []
        self._open = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.urls.extend(attrs[name] for name in URL_ATTRIBUTES if attrs.get(name))
        if tag == "form":
            form = {
                "action": attrs.get("action") or "",
                "method": (attrs.get("method") or "get").lower(),
                "inputs": {},
                "formactions": [],
            }
            self.forms.append(form)
            self._open.append(form)
        elif self._open and tag == "input" and attrs.get("name"):
            self._open[-1]["inputs"][attrs["name"]] = attrs.get("value")
        elif self._open and tag == "button" and attrs.get("formaction"):
            self._open[-1]["formactions"].append(attrs["formaction"])

    def handle_endtag(self, tag):
        if tag == "form" and self._open:
            self._open.pop()

    def post_forms(self):
        """Forms that POST to one of the plugin's actions, directly or through a button's formaction."""
        return [
            form
            for form in self.forms
            if form["method"] == "post"
            and any(POST_ACTION.match(url) for url in (form["action"], *form["formactions"]))
        ]

    def form_for(self, suffix):
        matches = [form for form in self.forms if form["action"].endswith(suffix)]
        if len(matches) != 1:
            raise AssertionError(f"expected one form posting to *{suffix}, found {len(matches)}")
        return matches[0]


class UIActionsTestBase(TestCase):
    def setUp(self):
        super().setUp()
        # Every button a user may see: what is checked is how they render, not who may see them.
        self.user.is_superuser = True
        self.user.save()
        self.server, self.proposal = make_proposal()
        self.item = self.proposal.items.get(kind="scope")
        self.sync_run = SyncRun.objects.create(windows_server=self.server, kind=SyncKindChoices.IMPORT)

    def page(self, url, client=None):
        response = (client or self.client).get(url)
        self.assertEqual(response.status_code, 200, url)
        return Page(response.content.decode())


class CsrfTokenTest(UIActionsTestBase):
    """Issue #19: every plugin POST button carries a CSRF token, and a browser-like POST with it is accepted."""

    def assert_tokens(self, url, expected):
        page = self.page(url)
        forms = page.post_forms()
        found = {
            match.group(1)
            for form in forms
            for u in (form["action"], *form["formactions"])
            if (match := POST_ACTION.match(u))
        }
        self.assertEqual(found, set(expected), url)
        for form in forms:
            token = form["inputs"].get("csrfmiddlewaretoken")
            self.assertTrue(token and token != "NOTPROVIDED", f"no CSRF token in the form posting to {form}")

    def test_server_actions_carry_a_token(self):
        self.assert_tokens(self.server.get_absolute_url(), {"push", "dry-run", "import", "test"})

    def test_proposal_actions_carry_a_token(self):
        self.assert_tokens(self.proposal.get_absolute_url(), {"apply", "dismiss"})

    def test_item_actions_carry_a_token(self):
        self.assert_tokens(self.item.get_absolute_url(), {"apply", "dismiss"})

    def test_bulk_review_actions_carry_a_token(self):
        # The bulk buttons submit the list's own form, which must hold the token.
        self.assert_tokens(reverse(f"plugins:{PLUGIN}:importproposalitem_list"), {"apply", "dismiss"})

    def test_lease_button_carries_a_token(self):
        from .fixtures import scope

        lan = scope(self.server, "10.5.0.0/24")
        url = reverse(f"plugins:{PLUGIN}:scope_leases", kwargs={"pk": lan.pk})
        form = self.page(url).form_for(f"/scopes/{lan.pk}/leases/")
        self.assertEqual(form["method"], "post")
        token = form["inputs"].get("csrfmiddlewaretoken")
        self.assertTrue(token and token != "NOTPROVIDED")

    def test_browser_post_with_the_rendered_token_is_accepted(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        page = self.page(self.server.get_absolute_url(), client)
        form = page.form_for(f"/windowsservers/{self.server.pk}/test/")
        # Without the token Django refuses the POST, which is what users saw.
        self.assertEqual(client.post(form["action"]).status_code, 403)
        response = client.post(form["action"], {"csrfmiddlewaretoken": form["inputs"].get("csrfmiddlewaretoken")})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.server.jobs.exists())

        page = self.page(self.proposal.get_absolute_url(), client)
        form = page.form_for(f"/importproposals/{self.proposal.pk}/dismiss/")
        response = client.post(form["action"], {"csrfmiddlewaretoken": form["inputs"].get("csrfmiddlewaretoken")})
        self.assertEqual(response.status_code, 302)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ProposalStatusChoices.DISMISSED)


class ActionLinksTest(UIActionsTestBase):
    """Issue #23: no button links to "None", and every plugin link on a page resolves to a registered view."""

    def assert_links_resolve(self, url):
        page = self.page(url)
        for link in page.urls:
            self.assertFalse(link == "None" or link.startswith("None?") or "/None" in link, f"{url} links to {link!r}")
            if link.startswith(PLUGIN_URL):
                try:
                    resolve(urlsplit(link).path)
                except Resolver404:
                    self.fail(f"{url} links to {link!r}, which does not resolve")
        return page

    def test_read_only_detail_pages(self):
        for obj in (self.sync_run, self.proposal, self.item):
            with self.subTest(model=obj._meta.model_name):
                page = self.assert_links_resolve(obj.get_absolute_url())
                base = obj.get_absolute_url()
                # Delete stays: old runs and proposals can be cleaned up from the UI.
                self.assertIn(f"{base}delete/", page.urls)
                self.assertNotIn(f"{base}edit/", page.urls)

    def test_review_actions_stay_on_read_only_pages(self):
        for obj in (self.proposal, self.item):
            with self.subTest(model=obj._meta.model_name):
                page = self.assert_links_resolve(obj.get_absolute_url())
                base = obj.get_absolute_url()
                self.assertIn(f"{base}apply/", page.urls)
                self.assertIn(f"{base}dismiss/", page.urls)

    def test_server_detail_page(self):
        self.assert_links_resolve(self.server.get_absolute_url())

    def test_scope_pages(self):
        from .fixtures import reservation, scope

        lan = scope(self.server, "10.5.0.0/24")
        reservation(lan, "10.5.0.50/24", client_id="aa")
        page = self.assert_links_resolve(lan.get_absolute_url())
        # The Leases tab sits on the scope page.
        self.assertIn(reverse(f"plugins:{PLUGIN}:scope_leases", kwargs={"pk": lan.pk}), page.urls)
        self.assert_links_resolve(reverse(f"plugins:{PLUGIN}:scope_leases", kwargs={"pk": lan.pk}))

    def test_durations_show_in_days_too(self):
        from .fixtures import scope

        office = scope(self.server, "10.6.0.0/24", lease_duration=691200)
        guests = scope(self.server, "10.7.0.0/24", lease_duration=28800)
        self.assertIn("(8 days)", self.client.get(office.get_absolute_url()).content.decode())
        self.assertIn("(0.33 days)", self.client.get(guests.get_absolute_url()).content.decode())

    def test_days(self):
        from netbox_windows_dhcp_plugin.ui.panels import days

        for seconds, text in ((0, "(0 days)"), (300, "(< 0.01 days)"), (86400, "(1 day)"), (691200, "(8 days)")):
            with self.subTest(seconds=seconds):
                self.assertEqual(days(seconds), text)

    def test_every_list_page(self):
        for model_name, views in registry["views"][PLUGIN].items():
            if not any(view["name"] == "list" for view in views):
                continue
            url = reverse(f"plugins:{PLUGIN}:{model_name}_list")
            with self.subTest(model=model_name):
                self.assert_links_resolve(url)
        # The read-only lists offer no Add, Import or Edit Selected (the navigation menu links elsewhere).
        for model in (SyncRun, ImportProposal, ImportProposalItem):
            url = reverse(get_viewname(model, "list"))
            page = self.page(url)
            for action in ("add/", "import/", "edit/"):
                self.assertFalse(any(link.startswith(url + action) for link in page.urls), f"{url}{action}")
