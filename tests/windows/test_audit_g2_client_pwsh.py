"""Audit fixes checked by running the real scripts in pwsh against the stub cmdlets (skipped without pwsh)."""

from __future__ import annotations

from pathlib import Path

import pytest

from netbox_windows_dhcp_plugin.windows.client import DhcpClient, MacFilter, Scope
from netbox_windows_dhcp_plugin.windows.psscript import NotFound, PSError

from .test_pwsh import SEED_SCOPE, PwshRunner

pytestmark = pytest.mark.pwsh

SEED_FILTER = "Add-DhcpServerv4Filter -List Deny -MacAddress aa-bb-cc-dd-ee-ff -Description stolen\n"


@pytest.fixture
def pw(pwsh_path: str, tmp_path: Path) -> PwshRunner:
    return PwshRunner(pwsh_path, tmp_path / "calls.jsonl")


def set_scope_calls(pw: PwshRunner) -> list[tuple[str, str]]:
    return [(c["StartRange"], c["EndRange"]) for c in pw.last_calls() if c["cmd"] == "Set-DhcpServerv4Scope"]


def moved(pw: PwshRunner, start: str, end: str) -> Scope:
    pw.seed = SEED_SCOPE
    got = DhcpClient(pw).get_scope("10.1.20.0")
    pw.last_calls()
    return Scope(**{**got.to_dict(), "start_range": start, "end_range": end})


def test_scope_set_reports_a_failed_range_rollback(pw: PwshRunner) -> None:
    """F71: step one moves the range, the update and the restore both fail: the error says where the range is."""
    target = moved(pw, "10.1.20.251", "10.1.20.254")  # no overlap: step one widens to 10.1.20.10-10.1.20.254
    pw.seed = SEED_SCOPE + "$global:WdFailSetScopeFrom = 2\n"
    with pytest.raises(PSError) as ei:
        DhcpClient(pw).set_scope(target)
    e = ei.value
    assert e.message.count("The RPC server is unavailable.") == 2
    assert "The scope range was left at 10.1.20.10-10.1.20.254" in e.message
    assert "putting the old range 10.1.20.10-10.1.20.250 back failed" in e.message
    assert e.message.startswith("Set-DhcpServerv4Scope: ")  # the cmdlet is still named
    # The original error's ID and category survive, so the not-found rules still apply to it.
    assert (e.id, e.category) == ("DHCP 1722,Set-DhcpServerv4Scope", "NotSpecified")
    assert set_scope_calls(pw) == [
        ("10.1.20.10", "10.1.20.254"),
        ("10.1.20.251", "10.1.20.254"),
        ("10.1.20.10", "10.1.20.250"),
    ]


def test_scope_set_failed_rollback_keeps_not_found(pw: PwshRunner) -> None:
    """F71: a scope removed between the steps is still reported as not found."""
    target = moved(pw, "10.1.20.251", "10.1.20.254")
    pw.seed = SEED_SCOPE + "$global:WdForgetScopeFrom = 2\n"
    with pytest.raises(NotFound) as ei:
        DhcpClient(pw).set_scope(target)
    assert "The scope range was left at 10.1.20.10-10.1.20.254" in str(ei.value)


def test_scope_set_rollback_that_works_reports_the_original_error_only(pw: PwshRunner) -> None:
    target = moved(pw, "10.1.20.251", "10.1.20.254")
    pw.seed = SEED_SCOPE
    target.state = "Bogus"  # refused by the second call only; the restore sends no state and succeeds
    with pytest.raises(PSError, match="Bogus") as ei:
        DhcpClient(pw).set_scope(target)
    assert "left at" not in str(ei.value)
    pw.last_calls()
    after = DhcpClient(pw).get_scope("10.1.20.0")
    assert (after.start_range, after.end_range) == ("10.1.20.10", "10.1.20.250")


def test_filter_set_puts_the_old_entry_back_when_the_add_fails(pw: PwshRunner) -> None:
    """F74: the script reads the filters at the end, so the read-back shows what is left on the server."""
    pw.seed = SEED_FILTER + "$global:WdFailAddFilter = 1\n"
    c = DhcpClient(pw)
    with pytest.raises(PSError, match="Failed to add MAC address filter") as ei:
        c.set_filter(MacFilter("aa-bb-cc-dd-ee-ff", "Allow", "found"))
    assert ei.value.id.startswith("DHCP 1722")
    calls = pw.last_calls()
    assert [x["cmd"] for x in calls] == [
        "Get-DhcpServerv4Filter",
        "Remove-DhcpServerv4Filter",
        "Add-DhcpServerv4Filter",
        "Add-DhcpServerv4Filter",
    ]
    assert (calls[2]["List"], calls[2]["Description"]) == ("Allow", "found")
    assert {k: v for k, v in calls[3].items() if k != "cmd"} == {
        "List": "Deny",
        "MacAddress": ["aa-bb-cc-dd-ee-ff"],
        "Description": "stolen",
    }


def test_filter_set_rollback_leaves_out_an_empty_description(pw: PwshRunner) -> None:
    pw.seed = "Add-DhcpServerv4Filter -List Deny -MacAddress aa-bb-cc-dd-ee-ff\n$global:WdFailAddFilter = 1\n"
    with pytest.raises(PSError, match="Failed to add MAC address filter"):
        DhcpClient(pw).set_filter(MacFilter("aa-bb-cc-dd-ee-ff", "Allow"))
    back = pw.last_calls()[-1]
    assert back["cmd"] == "Add-DhcpServerv4Filter" and back["List"] == "Deny" and "Description" not in back


def test_filter_set_reports_the_add_error_when_the_rollback_fails_too(pw: PwshRunner) -> None:
    """R03: the MAC address is now in neither list, so the error has to say so (as scope.set does for its range)."""
    pw.seed = SEED_FILTER + "$global:WdFailAddFilter = 2\n"
    with pytest.raises(PSError, match="Failed to add MAC address filter") as ei:
        DhcpClient(pw).set_filter(MacFilter("aa-bb-cc-dd-ee-ff", "Allow"))
    e = ei.value
    assert "The MAC address was left out of the Deny list" in e.message
    assert "putting the old entry back failed" in e.message
    assert e.message.count("Failed to add MAC address filter") == 2  # the Add error, then the rollback error
    assert e.message.startswith("Add-DhcpServerv4Filter: ")  # the cmdlet is still named
    # The original error's ID and category survive the rebuilt error.
    assert e.id.startswith("DHCP 1722")
    assert e.category == "NotSpecified"
    assert [x["cmd"] for x in pw.last_calls()].count("Add-DhcpServerv4Filter") == 2


def test_filter_set_still_works(pw: PwshRunner) -> None:
    pw.seed = SEED_FILTER
    assert DhcpClient(pw).set_filter(MacFilter("AA:BB:CC:DD:EE:FF", "Allow", "ok")) == MacFilter(
        "aa-bb-cc-dd-ee-ff", "Allow", "ok"
    )
    assert [x["cmd"] for x in pw.last_calls()].count("Add-DhcpServerv4Filter") == 1
