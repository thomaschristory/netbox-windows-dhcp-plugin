"""Failover lab tests: two Windows DHCP servers, writes through the first one.

Needs, on top of the usual lab variables (see conftest.py):

    WINDOWS_DHCP_LAB_PARTNER          the second server, as the first one reaches it (name or address)
    WINDOWS_DHCP_LAB_FAILOVER_SECRET  optional shared secret for the test relationship
    WINDOWS_DHCP_LAB_AUTH=credssp     failover cmdlets make the first server call its partner as us, which needs
                                      our credentials there (CredSSP); NTLM gets PermissionDenied

The partner is read with the same credentials. Only ``nbwd-test-`` relationships and scopes are written; removing
a relationship also deletes its scopes on the partner.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from netbox_windows_dhcp_plugin.sync import windows_state
from netbox_windows_dhcp_plugin.windows import client as wc
from netbox_windows_dhcp_plugin.windows.psscript import PSError

from .conftest import save_probe
from .labkit import MAC_PREFIX, Lab, lab_env, make_runner

ENV = lab_env()
PARTNER = ENV.get("WINDOWS_DHCP_LAB_PARTNER", "")
SECRET = ENV.get("WINDOWS_DHCP_LAB_FAILOVER_SECRET", "")
NAME = "nbwd-test-fo"

pytestmark = pytest.mark.skipif(
    not PARTNER or ENV.get("WINDOWS_DHCP_LAB_AUTH", "").lower() != "credssp",
    reason="failover lab tests need WINDOWS_DHCP_LAB_PARTNER and WINDOWS_DHCP_LAB_AUTH=credssp",
)


@pytest.fixture(scope="module")
def partner(lab_session: Lab) -> Iterator[Lab]:
    """The second server, read only apart from the sweep of what a failed run left there."""
    client = wc.DhcpClient(make_runner(dict(ENV, WINDOWS_DHCP_LAB_HOST=PARTNER)))
    side = Lab(client)
    # The first server's sweep has already removed our relationships; scopes they left on the partner go now.
    assert not side.sweep()
    yield side
    assert not side.sweep()


def lab_host() -> str:
    return ENV["WINDOWS_DHCP_LAB_HOST"]


def partner_scope_ids(partner: Lab) -> list[str]:
    return [s.scope_id for s in partner.client.list_scopes()]


def test_failover_lifecycle(lab: Lab, partner: Lab, probe_out) -> None:
    """client.py: failover output names and settings; creation and replication copying scope configuration;
    pusher.py: replication of listed scopes; fake.py: refusals and the role a mode switch gives."""
    s = lab.scope("nbwd-test-fo-a")
    base = s.scope_id.rsplit(".", 1)[0]
    lab.client.add_exclusion_range(wc.ExclusionRange(s.scope_id, f"{base}.100", f"{base}.110"))
    lab.client.add_reservation(
        wc.Reservation(s.scope_id, f"{base}.20", MAC_PREFIX + "00-0f-20", "nbwd-test-fo-host", "lab", "Dhcp")
    )
    lab.client.set_option_value(wc.OptionKey(15, s.scope_id), ["fo.example"])

    f = lab.failover(
        wc.FailoverRelationship(
            name=NAME,
            partner_server=PARTNER,
            scope_ids=[s.scope_id],
            load_balance_percent=60,
            max_client_lead_time=1800,
        ),
        SECRET,
    )
    save_probe(probe_out, "v4-failover-created", f.to_dict())
    save_probe(probe_out, "v4-failover", lab.dump("probe.fo", "Get-DhcpServerv4Failover -Name $p.n", {"n": NAME}))
    assert f.name == NAME and f.mode == "LoadBalance"
    assert f.scope_ids == [s.scope_id]
    assert f.load_balance_percent == 60 and f.max_client_lead_time == 1800
    assert f.enable_auth is bool(SECRET)

    # Creating the relationship copies the scope with its configuration to the partner.
    other = partner.client.get_failover(NAME)
    save_probe(probe_out, "v4-failover-partner", other.to_dict())
    assert other.scope_ids == [s.scope_id]
    assert [r.ip_address for r in partner.client.list_reservations(s.scope_id)] == [f"{base}.20"]
    assert [(e.start_range, e.end_range) for e in partner.client.list_exclusion_ranges(s.scope_id)] == [
        (f"{base}.100", f"{base}.110")
    ]
    assert partner.client.get_option_value(wc.OptionKey(15, s.scope_id)).value == ["fo.example"]

    # windows_state.py (#57): the partner names this server by a name this server reports for itself, so a server
    # NetBox knows by IP address still matches.
    names = lab.client.get_server_names()
    save_probe(probe_out, "server-names", {"first": names, "partner": partner.client.get_server_names()})
    assert names
    assert windows_state.resolve_partner(other.partner_server, [(lab_host(), names)]) == windows_state.host_name(
        lab_host()
    )

    # pusher.py: later changes reach the partner only through replication of the listed scopes.
    lab.client.add_reservation(
        wc.Reservation(s.scope_id, f"{base}.21", MAC_PREFIX + "00-0f-21", "nbwd-test-fo-host2", "", "Dhcp")
    )
    lab.client.add_exclusion_range(wc.ExclusionRange(s.scope_id, f"{base}.120", f"{base}.125"))
    lab.client.set_option_value(wc.OptionKey(15, s.scope_id), ["fo2.example"])
    lab.client.replicate_failover(name=NAME, scope_ids=[s.scope_id])
    assert sorted(r.ip_address for r in partner.client.list_reservations(s.scope_id)) == [f"{base}.20", f"{base}.21"]
    assert len(partner.client.list_exclusion_ranges(s.scope_id)) == 2
    assert partner.client.get_option_value(wc.OptionKey(15, s.scope_id)).value == ["fo2.example"]

    # fake.py: one scope outside any relationship makes Windows refuse the whole replication.
    loose = lab.scope("nbwd-test-fo-loose")
    seen: dict[str, str] = {}
    try:
        lab.client.replicate_failover(scope_ids=[s.scope_id, loose.scope_id])
        seen["replicate_with_loose_scope"] = "accepted"
    except PSError as exc:
        seen["replicate_with_loose_scope"] = f"refused: {exc.id}: {exc}"

    # A second scope joins, then windows_writer.py: does Remove-DhcpServerv4Scope refuse a scope still in it?
    b = lab.scope("nbwd-test-fo-b")
    assert sorted(lab.client.add_failover_scopes(NAME, [b.scope_id]).scope_ids) == sorted([s.scope_id, b.scope_id])
    assert b.scope_id in partner_scope_ids(partner)
    try:
        lab.client.remove_scope(b.scope_id)
        seen["remove_scope_in_relationship"] = "accepted"
    except PSError as exc:
        seen["remove_scope_in_relationship"] = f"refused: {exc.id}: {exc}"
    seen["after_remove_scope_relationship"] = ",".join(lab.client.get_failover(NAME).scope_ids)
    seen["after_remove_scope_partner_has_b"] = str(b.scope_id in partner_scope_ids(partner))

    # Leaving the relationship deletes the scope on the partner and keeps it here.
    if b.scope_id in lab.client.get_failover(NAME).scope_ids:
        assert lab.client.remove_failover_scopes(NAME, [b.scope_id]).scope_ids == [s.scope_id]
        assert b.scope_id not in partner_scope_ids(partner)
        assert lab.client.get_scope(b.scope_id).scope_id == b.scope_id
    # A scope that is not in the relationship (DHCP 20116 in the fake) and the last scope.
    try:
        lab.client.remove_failover_scopes(NAME, [b.scope_id])
        seen["remove_scope_not_in_it"] = "accepted"
    except PSError as exc:
        seen["remove_scope_not_in_it"] = f"refused: {exc.id}: {exc}"

    # fake.py and client.py: the role this server gets when Set switches the mode to HotStandby.
    f.mode = "HotStandby"
    f.reserve_percent = 10
    f.load_balance_percent = None
    try:
        hot = lab.client.set_failover(f)
        seen["hot_standby_role"] = hot.server_role
        seen["hot_standby_partner_role"] = partner.client.get_failover(NAME).server_role
        dump = lab.dump("probe.fo", "Get-DhcpServerv4Failover -Name $p.n", {"n": NAME})
        save_probe(probe_out, "v4-failover-hot", dump)
    except PSError as exc:
        seen["hot_standby_role"] = f"refused: {exc.id}: {exc}"
    save_probe(probe_out, "v4-failover-behaviour", seen)

    assert seen["replicate_with_loose_scope"].startswith("refused: DHCP 20116"), seen
    assert seen["remove_scope_not_in_it"].startswith("refused: DHCP 20116"), seen
    assert seen["remove_scope_in_relationship"].startswith("refused: DHCP 20113"), seen
    assert seen["hot_standby_role"] == "Active" and seen["hot_standby_partner_role"] == "Standby", seen

    # Removing the relationship deletes its scopes on the partner and keeps them here.
    lab.remove_failover(NAME)
    assert s.scope_id not in partner_scope_ids(partner)
    assert lab.client.get_scope(s.scope_id).scope_id == s.scope_id


def test_failover_last_scope_leaving(lab: Lab, partner: Lab, probe_out) -> None:
    """fake.py: what Windows does when the last scope leaves a relationship (removes it, or keeps it empty)."""
    s = lab.scope("nbwd-test-fo-last")
    lab.failover(wc.FailoverRelationship(name=NAME, partner_server=PARTNER, scope_ids=[s.scope_id]), SECRET)
    seen: dict[str, object] = {}
    try:
        after = lab.client.remove_failover_scopes(NAME, [s.scope_id])
        seen["result"] = after.to_dict()
    except wc.NotFound:
        seen["result"] = "relationship gone (the read after the removal found nothing)"
    except PSError as exc:
        seen["result"] = f"refused: {exc.id}: {exc}"
    seen["here"] = [f.to_dict() for f in lab.client.list_failovers() if f.name == NAME]
    seen["partner"] = [f.to_dict() for f in partner.client.list_failovers() if f.name == NAME]
    seen["partner_has_scope"] = s.scope_id in partner_scope_ids(partner)
    save_probe(probe_out, "v4-failover-last-scope", seen)
    # Windows keeps the relationship, empty, on both partners, and the scope leaves the partner.
    assert isinstance(seen["result"], dict) and seen["result"]["scope_ids"] == [], seen
    assert [f["scope_ids"] for f in seen["here"]] == [[]] and [f["scope_ids"] for f in seen["partner"]] == [[]], seen
    assert seen["partner_has_scope"] is False, seen
