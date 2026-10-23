"""Run the real rendered scripts and bootstrap in a local pwsh against stub cmdlets.

Port of internal/dhcp/pwsh_test.go. testdata/stubs.ps1 is loaded as an in-memory
module before each script; every stub records its bound parameters as one JSON line
in $env:WD_CALLS. Skipped when pwsh is not installed (set $PWSH to point at one).

The stubs only check that the scripts run and bind parameters the way the stubs
declare them; parameter lists for the cmdlets the Go provider did not use are not
verified against Windows unless the stub cites a lab finding. The stubs also replace
ConvertTo-Json to print "nothing" as {} the way Windows PowerShell 5.1 does (#15).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from netbox_windows_dhcp_plugin.windows.client import (
    ALL_SCRIPTS,
    DHCP_MODULE,
    DhcpClass,
    DhcpClient,
    ExclusionRange,
    FailoverRelationship,
    MacFilter,
    OptionDefinition,
    OptionKey,
    OptionKeyV6,
    Policy,
    PolicyIPRange,
    Reservation,
    ReservationV6,
    Scope,
    ScopeV6,
    Superscope,
)
from netbox_windows_dhcp_plugin.windows.psscript import (
    NotFound,
    PSError,
    Script,
    command_argv,
    decode_output,
    parse,
    render,
)
from netbox_windows_dhcp_plugin.windows.runner import RunnerError, clean_stderr

from .conftest import TESTDATA

pytestmark = pytest.mark.pwsh

STUBS = (TESTDATA / "stubs.ps1").read_text(encoding="utf-8")

SEED_SCOPE = """
$global:Scopes['10.1.20.0'] = New-Scope @{ ScopeId = '10.1.20.0'; SubnetMask = '255.255.255.0'; Name = 'Users'
    State = 'Active'; StartRange = '10.1.20.10'; EndRange = '10.1.20.250'; LeaseDuration = '8.00:00:00'
    Description = ''; Type = 'Dhcp' }
"""


class PwshRunner:
    def __init__(self, pwsh: str, calls: Path, seed: str = "") -> None:
        self.pwsh = pwsh
        self.calls = calls
        self.seed = seed

    def run(self, script: str, params: Any = None) -> bytes:
        rendered = render(script, params)
        full = (
            "New-Module -Name DhcpServerStub -ScriptBlock {\n"
            + STUBS
            + "\n} | Import-Module\n$global:WdNoRecord = $true\n"
            + self.seed
            + "\n$global:WdNoRecord = $false\n"
            + rendered
        )
        args, stdin = command_argv(full)
        env = {**os.environ, "WD_CALLS": str(self.calls)}
        p = subprocess.run([self.pwsh, *args], input=stdin.encode(), capture_output=True, env=env, timeout=120)
        if p.returncode != 0:
            raise RunnerError(f"pwsh exited {p.returncode}: {clean_stderr(p.stderr.decode(errors='replace'))}")
        return decode_output(p.stdout)

    def last_calls(self) -> list[dict[str, Any]]:
        if not self.calls.exists():
            return []
        lines = self.calls.read_text(encoding="utf-8-sig").splitlines()
        self.calls.unlink()
        return [json.loads(line.lstrip("﻿")) for line in lines if line.strip()]


def find_call(calls: list[dict[str, Any]], cmd: str) -> dict[str, Any]:
    """The last call of ``cmd``."""
    for c in reversed(calls):
        if c["cmd"] == cmd:
            return c
    raise AssertionError(f"{cmd} was not called; calls: {calls}")


@pytest.fixture
def pw(pwsh_path: str, tmp_path: Path) -> PwshRunner:
    return PwshRunner(pwsh_path, tmp_path / "calls.jsonl")


def test_all_scripts_parse(pwsh_path: str) -> None:
    scripts = {s.op: render(s.with_requirement(DHCP_MODULE).wrap(), {"x": "it's ’"}) for s in ALL_SCRIPTS}
    check = r"""
$in = [Console]::In.ReadToEnd() | ConvertFrom-Json
foreach ($prop in $in.PSObject.Properties) {
    $errs = $null; $tok = $null
    [void][System.Management.Automation.Language.Parser]::ParseInput($prop.Value, [ref]$tok, [ref]$errs)
    if ($errs) { "$($prop.Name): $($errs | ForEach-Object { $_.Message + ' line ' + $_.Extent.StartLineNumber })" }
}
"""
    p = subprocess.run(
        [pwsh_path, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", check],
        input=json.dumps(scripts),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "", p.stdout


def test_scope(pw: PwshRunner) -> None:
    c = DhcpClient(pw)
    name = 'Café\'s "scope" ‘x’'
    s = c.add_scope(
        Scope(
            name=name,
            start_range="198.18.0.10",
            end_range="198.18.0.200",
            subnet_mask="255.255.255.0",
            state="InActive",
            type="Both",
            lease_duration="08:00:00",
        )
    )
    assert s == Scope(
        scope_id="198.18.0.0",
        name=name,
        start_range="198.18.0.10",
        end_range="198.18.0.200",
        subnet_mask="255.255.255.0",
        state="Inactive",
        type="Both",
        lease_duration="0.08:00:00",
        delay=0,
        max_bootp_clients=0,
        activate_policies=False,
        superscope_name="",
    )
    add = find_call(pw.last_calls(), "Add-DhcpServerv4Scope")
    assert add["LeaseDuration"] == 28800 and add["Name"] == name and add["SubnetMask"] == "255.255.255.0"
    assert "Description" not in add and "ComputerName" not in add and "Delay" not in add

    pw.seed = SEED_SCOPE
    got = c.get_scope("10.1.20.0")
    assert got.lease_duration == "8.00:00:00" and got.name == "Users"
    # An empty lease duration on set keeps the scope's own (for example an unlimited lease).
    c.set_scope(Scope(**{**got.to_dict(), "lease_duration": ""}))
    assert "LeaseDuration" not in find_call(pw.last_calls(), "Set-DhcpServerv4Scope")
    with pytest.raises(NotFound) as ei:
        c.get_scope("10.9.9.0")  # non-terminating stub error becomes terminating via ErrorAction Stop
    assert str(ei.value).startswith("Get-DhcpServerv4Scope: Failed to get information for scope 10.9.9.0")
    assert [x.scope_id for x in c.list_scopes()] == ["10.1.20.0"]
    pw.seed = ""
    assert c.list_scopes() == []

    pw.seed = SEED_SCOPE
    pw.last_calls()
    upd = Scope(
        scope_id="10.1.20.0",
        name=name,
        start_range="10.1.20.20",
        end_range="10.1.20.240",
        subnet_mask="255.255.255.0",
        state="InActive",
        type="Both",
        lease_duration="1.00:00:00",
        description="new",
        delay=200,
    )
    s = c.set_scope(upd)
    assert s.start_range == "10.1.20.20" and s.lease_duration == "1.00:00:00" and s.description == "new"
    st = find_call(pw.last_calls(), "Set-DhcpServerv4Scope")
    assert st["ScopeId"] == "10.1.20.0" and st["State"] == "InActive" and st["LeaseDuration"] == 86400
    assert st["Delay"] == 200

    c.remove_scope("10.1.20.0", force=True)
    rm = find_call(pw.last_calls(), "Remove-DhcpServerv4Scope")
    assert rm["Force"] is True and rm["Confirm"] is False
    c.remove_scope("10.1.20.0")
    assert find_call(pw.last_calls(), "Remove-DhcpServerv4Scope")["Force"] is False


@pytest.mark.parametrize(
    ("start", "end", "first"),
    [
        ("10.1.20.20", "10.1.20.252", ("10.1.20.20", "10.1.20.250")),  # start narrows, end widens: overlap first
        ("10.1.20.5", "10.1.20.240", ("10.1.20.10", "10.1.20.240")),  # start widens, end narrows: overlap first
        ("10.1.20.100", "10.1.20.254", ("10.1.20.100", "10.1.20.250")),  # shifted up, still overlapping
        ("10.1.20.251", "10.1.20.254", ("10.1.20.10", "10.1.20.254")),  # no overlap: widen over both pools
    ],
)
def test_scope_range_shift_takes_two_steps(pw: PwshRunner, start: str, end: str, first: tuple[str, str]) -> None:
    """Issue #30: Windows refuses (DHCP 20023) a range that widens one end and narrows the other in one call."""
    pw.seed = SEED_SCOPE
    c = DhcpClient(pw)
    got = c.get_scope("10.1.20.0")
    with pytest.raises(PSError, match="Failed to set IP address range"):
        raw = Script(
            "raw",
            "Set-DhcpServerv4Scope -ScopeId 10.1.20.0 -StartRange $p.s -EndRange $p.e -Description x\n$out = 1",
        )
        c._run(raw, {"s": start, "e": end})
    pw.last_calls()
    s = c.set_scope(Scope(**{**got.to_dict(), "start_range": start, "end_range": end, "description": "moved"}))
    assert (s.start_range, s.end_range, s.description) == (start, end, "moved")
    sets = [x for x in pw.last_calls() if x["cmd"] == "Set-DhcpServerv4Scope"]
    assert len(sets) == 2
    assert {k: v for k, v in sets[0].items() if k != "cmd"} == {
        "ScopeId": "10.1.20.0",
        "StartRange": first[0],
        "EndRange": first[1],
    }
    assert (sets[1]["StartRange"], sets[1]["EndRange"]) == (start, end)


def test_stub_refuses_a_mixed_range_change_but_applies_the_rest(pw: PwshRunner) -> None:
    pw.seed = SEED_SCOPE
    script = Script(
        "raw",
        "try { Set-DhcpServerv4Scope -ScopeId 10.1.20.0 -StartRange 10.1.20.5 -EndRange 10.1.20.240 -Description x }"
        " catch { }\n$s = Get-DhcpServerv4Scope -ScopeId 10.1.20.0\n"
        '$out = @("$($s.StartRange)", "$($s.EndRange)", $s.Description)',
    )
    assert DhcpClient(pw)._run(script)[1] == ["10.1.20.10", "10.1.20.250", "x"]


def test_scope_range_shift_rolls_back_when_the_update_fails(pw: PwshRunner) -> None:
    pw.seed = SEED_SCOPE
    c = DhcpClient(pw)
    got = c.get_scope("10.1.20.0")
    # The stub refuses the state on the second call, after the first step has moved the range.
    bad = Scope(**{**got.to_dict(), "start_range": "10.1.20.20", "end_range": "10.1.20.252", "state": "Bogus"})
    with pytest.raises((PSError, RunnerError), match="Bogus"):
        c.set_scope(bad)
    after = c.get_scope("10.1.20.0")
    assert (after.start_range, after.end_range) == ("10.1.20.10", "10.1.20.250")


@pytest.mark.parametrize(("start", "end"), [("10.1.20.5", "10.1.20.254"), ("10.1.20.20", "10.1.20.200")])
def test_scope_range_one_direction_is_one_call(pw: PwshRunner, start: str, end: str) -> None:
    pw.seed = SEED_SCOPE
    c = DhcpClient(pw)
    got = c.get_scope("10.1.20.0")
    pw.last_calls()
    s = c.set_scope(Scope(**{**got.to_dict(), "start_range": start, "end_range": end}))
    assert (s.start_range, s.end_range) == (start, end)
    assert len([x for x in pw.last_calls() if x["cmd"] == "Set-DhcpServerv4Scope"]) == 1


def test_computer_name(pw: PwshRunner) -> None:
    pw.seed = SEED_SCOPE
    DhcpClient(pw, "dhcp02.example.local").get_scope("10.1.20.0")
    assert find_call(pw.last_calls(), "Get-DhcpServerv4Scope")["ComputerName"] == "dhcp02.example.local"


def test_reservation(pw: PwshRunner) -> None:
    pw.seed = SEED_SCOPE
    c = DhcpClient(pw)
    with pytest.raises(NotFound):
        c.get_reservation("10.1.20.0", "10.1.20.5")
    with pytest.raises(NotFound):
        c.get_reservation("10.9.9.0", "10.9.9.5")
    r = c.add_reservation(Reservation(scope_id="10.1.20.0", ip_address="10.1.20.5", client_id="AA:BB:CC:DD:EE:FF"))
    assert r.name == "10.1.20.5" and r.client_id == "aa-bb-cc-dd-ee-ff" and r.type == "Both"
    assert "Name" not in find_call(pw.last_calls(), "Add-DhcpServerv4Reservation")
    pw.seed = (
        SEED_SCOPE
        + "Add-DhcpServerv4Reservation -ScopeId 10.1.20.0 -IPAddress 10.1.20.5 "
        + "-ClientId aa-bb-cc-dd-ee-ff -Name printer"
    )
    pw.last_calls()
    lst = c.list_reservations("10.1.20.0")
    assert len(lst) == 1 and lst[0].name == "printer"
    r = c.set_reservation(Reservation(scope_id="10.1.20.0", ip_address="10.1.20.5", client_id="aa-bb-cc-dd-ee-00"))
    assert r.client_id == "aa-bb-cc-dd-ee-00" and r.name == "printer"
    st = find_call(pw.last_calls(), "Set-DhcpServerv4Reservation")
    assert st["Description"] == "" and st["IPAddress"] == "10.1.20.5" and "ScopeId" not in st
    c.remove_reservation("10.1.20.5")
    find_call(pw.last_calls(), "Remove-DhcpServerv4Reservation")


def test_reservation_set_sends_identity_only_on_change(pw: PwshRunner) -> None:
    """Issue #42: a description change must not resend the client ID and type, which lost the option values."""
    pw.seed = (
        SEED_SCOPE
        + "Add-DhcpServerv4Reservation -ScopeId 10.1.20.0 -IPAddress 10.1.20.5 "
        + "-ClientId aa-bb-cc-dd-ee-ff -Name printer -Type Dhcp"
    )
    c = DhcpClient(pw)

    def set_call(client_id: str, type_: str) -> dict[str, Any]:
        r = Reservation("10.1.20.0", "10.1.20.5", client_id, "printer", "changed", type_)
        assert c.set_reservation(r).description == "changed"
        return find_call(pw.last_calls(), "Set-DhcpServerv4Reservation")

    st = set_call("aa-bb-cc-dd-ee-ff", "Dhcp")
    assert st["Description"] == "changed" and st["IPAddress"] == "10.1.20.5"
    assert "ClientId" not in st and "Type" not in st and "ScopeId" not in st
    # The same MAC in another case or separator is not a change.
    st = set_call("AA:BB:CC:DD:EE:FF", "DHCP")
    assert "ClientId" not in st and "Type" not in st
    st = set_call("aa-bb-cc-dd-ee-00", "Dhcp")
    assert st["ClientId"] == "aa-bb-cc-dd-ee-00" and "Type" not in st
    st = set_call("aa-bb-cc-dd-ee-ff", "Both")
    assert st["Type"] == "Both" and "ClientId" not in st


def test_v6_reservation_set_sends_identity_only_on_change(pw: PwshRunner) -> None:
    pw.seed = (
        "Add-DhcpServerv6Scope -Prefix 2001:db8:1:: -Name v6\n"
        + "Add-DhcpServerv6Reservation -Prefix 2001:db8:1:: -IPAddress 2001:db8:1::10 -ClientDuid 00-01-ab -Iaid 7\n"
    )
    c = DhcpClient(pw)

    def set_call(duid: str, iaid: int) -> dict[str, Any]:
        r = c.set_v6_reservation(ReservationV6("2001:db8:1::", "2001:db8:1::10", duid, iaid, "pc", "changed"))
        assert r.description == "changed" and r.name == "pc"
        return find_call(pw.last_calls(), "Set-DhcpServerv6Reservation")

    st = set_call("00:01:AB", 7)
    assert st["Description"] == "changed" and "ClientDuid" not in st and "Iaid" not in st and "Prefix" not in st
    st = set_call("00-01-ac", 7)
    assert st["ClientDuid"] == "00-01-ac" and "Iaid" not in st
    st = set_call("00-01-ab", 8)
    assert st["Iaid"] == 8 and "ClientDuid" not in st


def test_exclusion(pw: PwshRunner) -> None:
    pw.seed = SEED_SCOPE
    c = DhcpClient(pw)
    e = ExclusionRange("10.1.20.0", "10.1.20.1", "10.1.20.9")
    with pytest.raises(NotFound):
        c.get_exclusion_range(e)
    assert c.add_exclusion_range(e) == e
    c.remove_exclusion_range(e)
    rm = find_call(pw.last_calls(), "Remove-DhcpServerv4ExclusionRange")
    assert rm["StartRange"] == "10.1.20.1" and rm["EndRange"] == "10.1.20.9"  # never the whole scope


def test_option(pw: PwshRunner) -> None:
    pw.seed = SEED_SCOPE
    c = DhcpClient(pw)
    k = OptionKey(3, scope_id="10.1.20.0")
    with pytest.raises(NotFound):
        c.get_option_value(k)
    v = c.set_option_value(k, ["10.1.20.1"])
    assert v.value == ["10.1.20.1"] and v.option_id == 3 and v.name == "Router" and v.scope_id == "10.1.20.0"
    st = find_call(pw.last_calls(), "Set-DhcpServerv4OptionValue")
    assert st["Value"] == ["10.1.20.1"] and st["ScopeId"] == "10.1.20.0" and "ReservedIP" not in st
    v = c.set_option_value(OptionKey(6, vendor_class="Vendor A"), ["10.0.0.53", "10.0.1.53"])
    assert len(v.value) == 2 and v.vendor_class == "Vendor A"
    st = find_call(pw.last_calls(), "Set-DhcpServerv4OptionValue")
    assert "ScopeId" not in st and st["VendorClass"] == "Vendor A"
    res = OptionKey(12, reserved_ip="10.1.20.5", policy_name="printers")
    c.set_option_value(res, ["printer"])
    st = find_call(pw.last_calls(), "Set-DhcpServerv4OptionValue")
    assert st["ReservedIP"] == "10.1.20.5" and st["PolicyName"] == "printers"
    c.remove_option_value(res)
    rm = find_call(pw.last_calls(), "Remove-DhcpServerv4OptionValue")
    assert rm["OptionId"] == ["12"] and rm["ReservedIP"] == "10.1.20.5" and rm["Confirm"] is False
    pw.seed = SEED_SCOPE + "Set-DhcpServerv4OptionValue -ScopeId 10.1.20.0 -OptionId 3 -Value 10.1.20.1"
    lst = c.list_option_values(scope_id="10.1.20.0", all=True)
    assert [o.option_id for o in lst] == [3]
    assert find_call(pw.last_calls(), "Get-DhcpServerv4OptionValue")["All"] is True


def test_leases(pw: PwshRunner) -> None:
    pw.seed = (
        SEED_SCOPE
        + """
[void]$global:Leases.Add([pscustomobject]@{ IPAddress = [ipaddress]'10.1.20.11'; ScopeId = [ipaddress]'10.1.20.0'
    ClientId = '00-11-22-33-44-55'; HostName = 'pc01'; AddressState = 'Active'
    LeaseExpiryTime = [datetime]::new(2026, 10, 9, 8, 0, 0, [DateTimeKind]::Utc)
    Description = $null; ClientType = 'Dhcp' })
[void]$global:Leases.Add([pscustomobject]@{ IPAddress = [ipaddress]'10.1.20.5'; ScopeId = [ipaddress]'10.1.20.0'
    ClientId = 'aa-bb-cc-dd-ee-ff'; HostName = ''; AddressState = 'InactiveReservation'
    LeaseExpiryTime = $null; Description = ''; ClientType = 'None' })
"""
    )
    got = DhcpClient(pw).list_leases("10.1.20.0", all_leases=True)
    assert len(got) == 2
    assert got[0].lease_expiry_time == "2026-10-09T08:00:00Z" and got[0].host_name == "pc01"
    assert got[1].lease_expiry_time == ""
    assert find_call(pw.last_calls(), "Get-DhcpServerv4Lease")["AllLeases"] is True


def test_lease_expiry_ignores_server_culture(pw: PwshRunner) -> None:
    # Thai uses the Buddhist calendar (year 2569); the expiry must still be an ISO date.
    pw.seed = (
        SEED_SCOPE
        + """
[cultureinfo]::CurrentCulture = 'th-TH'
[void]$global:Leases.Add([pscustomobject]@{ IPAddress = [ipaddress]'10.1.20.11'; ScopeId = [ipaddress]'10.1.20.0'
    ClientId = '00-11-22-33-44-55'; HostName = 'pc01'; AddressState = 'Active'
    LeaseExpiryTime = [datetime]::new(2026, 10, 9, 8, 0, 0, [DateTimeKind]::Utc)
    Description = $null; ClientType = 'Dhcp' })
"""
    )
    (got,) = DhcpClient(pw).list_leases("10.1.20.0")
    assert got.lease_expiry_time == "2026-10-09T08:00:00Z"


def test_single_item_list_stays_a_list(pw: PwshRunner) -> None:
    pw.seed = SEED_SCOPE
    got = DhcpClient(pw).list_scopes()
    assert len(got) == 1


def test_broken_script(pw: PwshRunner) -> None:
    with pytest.raises((PSError, RunnerError)):
        DhcpClient(pw)._run(Script("broken", "$out = @("))


def test_missing_module_is_not_not_found(pw: PwshRunner, tmp_path: Path) -> None:
    # A host with the DHCP RSAT tools would find the real module (issue #31): search an empty module directory
    # instead, so the module is missing whatever is installed.
    empty = tmp_path / "no-modules"
    empty.mkdir()
    env = {**os.environ, "PSModulePath": str(empty)}
    probe = subprocess.run(
        [pw.pwsh, "-NoProfile", "-NonInteractive", "-Command", "[bool](Get-Command Get-DhcpServerv4Scope -EA 0)"],
        capture_output=True,
        env=env,
        timeout=120,
    )
    assert probe.returncode == 0, probe.stderr.decode(errors="replace")
    if probe.stdout.decode(errors="replace").strip() == "True":
        pytest.skip("the DhcpServer module is still found with an empty PSModulePath")
    pw2 = PwshRunner(pw.pwsh, tmp_path / "c2.jsonl")
    pw2.run = lambda script, params=None: _run_without_stubs(pw.pwsh, script, params, env)  # type: ignore[method-assign]
    with pytest.raises(PSError, match="DhcpServer PowerShell module is not available") as ei:
        DhcpClient(pw2).list_scopes()
    assert not ei.value.not_found


def _run_without_stubs(pwsh: str, script: str, params: Any, env: dict[str, str]) -> bytes:
    args, stdin = command_argv(render(script, params))
    p = subprocess.run([pwsh, *args], input=stdin.encode(), capture_output=True, env=env, timeout=120)
    return decode_output(p.stdout)


# ---- objects added by the plugin ----


def test_classes(pw: PwshRunner) -> None:
    c = DhcpClient(pw)
    pw.seed = "Add-DhcpServerv4Class -Name phones -Type User -Data voip -Description 'IP phones'"
    cl = c.get_class("phones", "user")
    assert cl == DhcpClass("phones", "User", "voip", "IP phones")  # ASCII data, not the hex form
    with pytest.raises(NotFound):
        c.get_class("phones", "Vendor")
    pw.seed = ""
    cl = c.add_class(DhcpClass("Contoso", "Vendor", "CONTOSO 1.0"))
    assert cl.data == "CONTOSO 1.0" and cl.type == "Vendor"
    add = find_call(pw.last_calls(), "Add-DhcpServerv4Class")
    assert add["Type"] == "Vendor" and "Description" not in add
    pw.seed = "Add-DhcpServerv4Class -Name phones -Type User -Data voip -Description 'IP phones'"
    assert c.set_class(DhcpClass("phones", "User", "voip2", "")).data == "voip2"
    c.remove_class("phones", "User")
    rm = find_call(pw.last_calls(), "Remove-DhcpServerv4Class")
    assert rm["Name"] == "phones" and rm["Confirm"] is False
    assert [x.name for x in c.list_classes("User")] == ["phones"]


def test_failover(pw: PwshRunner) -> None:
    c = DhcpClient(pw)
    pw.seed = SEED_SCOPE
    f = c.add_failover(
        FailoverRelationship(
            name="fo",
            partner_server="dhcp02",
            mode="HotStandby",
            server_role="Standby",
            scope_ids=["10.1.20.0"],
            max_client_lead_time=1800,
            auto_state_transition=True,
        ),
        shared_secret="s3'cret",
    )
    assert f.mode == "HotStandby" and f.server_role == "Standby" and f.reserve_percent == 5
    assert f.load_balance_percent is None and f.max_client_lead_time == 1800 and f.state_switch_interval is None
    assert f.enable_auth and f.scope_ids == ["10.1.20.0"]
    add = find_call(pw.last_calls(), "Add-DhcpServerv4Failover")
    assert add["SharedSecret"] == "s3'cret" and add["MaxClientLeadTime"] == 1800 and add["ServerRole"] == "Standby"
    assert "LoadBalancePercent" not in add

    seed_fo = (
        SEED_SCOPE
        + "Add-DhcpServerv4Failover -Name lb -PartnerServer dhcp02 -ScopeId 10.1.20.0 -LoadBalancePercent 60\n"
    )
    pw.seed = seed_fo
    f = c.get_failover("lb")
    assert f.mode == "LoadBalance" and f.load_balance_percent == 60 and f.reserve_percent is None
    with pytest.raises(NotFound):
        c.get_failover("nope")
    f = c.set_failover(
        FailoverRelationship(name="lb", mode="LoadBalance", load_balance_percent=70, state_switch_interval=600)
    )
    assert f.load_balance_percent == 70 and f.state_switch_interval == 600
    st = find_call(pw.last_calls(), "Set-DhcpServerv4Failover")
    assert st["Force"] is True and "SharedSecret" not in st and "ServerRole" not in st
    assert c.add_failover_scopes("lb", ["10.1.30.0"]).scope_ids == ["10.1.20.0", "10.1.30.0"]
    assert c.remove_failover_scopes("lb", ["10.1.20.0"]).scope_ids == []
    c.replicate_failover("lb")  # the cmdlet's own output must not break the envelope
    rep = find_call(pw.last_calls(), "Invoke-DhcpServerv4FailoverReplication")
    assert rep["Name"] == ["lb"] and rep["Force"] is True and "ScopeId" not in rep
    # Both given: the scoped parameter set, never both (#51); scope ids alone work the same way.
    for name in ("lb", ""):
        c.replicate_failover(name, ["10.1.30.0"])
        rep = find_call(pw.last_calls(), "Invoke-DhcpServerv4FailoverReplication")
        assert rep["ScopeId"] == ["10.1.30.0"] and "Name" not in rep
    c.remove_failover("lb")
    assert find_call(pw.last_calls(), "Remove-DhcpServerv4Failover")["Force"] is False
    assert [x.name for x in c.list_failovers()] == ["lb"]


def test_hot_standby_update_sends_no_server_role(pw: PwshRunner) -> None:
    """Issue #33: Set-DhcpServerv4Failover has no -ServerRole, so sending it failed parameter binding."""
    pw.seed = (
        SEED_SCOPE
        + "Add-DhcpServerv4Failover -Name hs -PartnerServer dhcp02 -ScopeId 10.1.20.0 -ServerRole Active\n"
        + "Add-DhcpServerv4Failover -Name lb -PartnerServer dhcp02 -ScopeId 10.1.20.0\n"
    )
    c = DhcpClient(pw)
    f = c.set_failover(
        FailoverRelationship(
            name="hs",
            partner_server="dhcp02",
            mode="HotStandby",
            server_role="Active",
            reserve_percent=10,
            max_client_lead_time=120,
        )
    )
    assert (f.server_role, f.reserve_percent, f.max_client_lead_time) == ("Active", 10, 120)
    st = find_call(pw.last_calls(), "Set-DhcpServerv4Failover")
    assert "ServerRole" not in st and st["Mode"] == "HotStandby" and st["Confirm"] is False
    # The stub refuses the parameter, as Windows does.
    with pytest.raises(PSError, match="ServerRole"):
        c._run(Script("raw", "Set-DhcpServerv4Failover -Name hs -ServerRole Standby"))
    f = c.set_failover(FailoverRelationship(name="lb", mode="HotStandby", server_role="Standby"))
    assert f.mode == "HotStandby" and f.server_role == "Active"  # the role is Windows' choice, not what was asked


def test_superscopes(pw: PwshRunner) -> None:
    c = DhcpClient(pw)
    assert c.list_superscopes() == []  # scopes without a superscope are ignored
    ss = c.add_superscope(Superscope("A", ["10.1.20.0", "10.1.30.0"]))
    assert ss == Superscope("A", ["10.1.20.0", "10.1.30.0"])
    seed = "Add-DhcpServerv4Superscope -SuperscopeName A -ScopeId 10.1.20.0,10.1.30.0\n"
    pw.seed = seed
    pw.last_calls()
    assert c.set_superscope(Superscope("A", ["10.1.20.0", "10.1.30.0", "10.1.40.0"])).scope_ids == [
        "10.1.20.0",
        "10.1.30.0",
        "10.1.40.0",
    ]
    calls = pw.last_calls()
    assert not [x for x in calls if x["cmd"] == "Remove-DhcpServerv4Superscope"]
    assert find_call(calls, "Add-DhcpServerv4Superscope")["ScopeId"] == ["10.1.40.0"]
    assert c.set_superscope(Superscope("A", ["10.1.30.0"])).scope_ids == ["10.1.30.0"]
    calls = pw.last_calls()
    # Only the departing scope is removed; the superscope itself is never dropped.
    assert find_call(calls, "Remove-DhcpServerv4Superscope")["ScopeId"] == ["10.1.20.0"]
    assert not [x for x in calls if x["cmd"] == "Add-DhcpServerv4Superscope"]
    with pytest.raises(ValueError, match="remove_superscope"):
        c.set_superscope(Superscope("A", []))
    assert c.rename_superscope("A", "B") == Superscope("B", ["10.1.20.0", "10.1.30.0"])
    # Rename takes -Name and -NewName, not -SuperscopeName like Add and Remove (issue #14).
    rn = find_call(pw.last_calls(), "Rename-DhcpServerv4Superscope")
    assert (rn["Name"], rn["NewName"]) == ("A", "B") and "SuperscopeName" not in rn
    with pytest.raises(NotFound):
        c.get_superscope("nope")
    c.remove_superscope("A")
    assert find_call(pw.last_calls(), "Remove-DhcpServerv4Superscope")["Confirm"] is False


def test_filters(pw: PwshRunner) -> None:
    c = DhcpClient(pw)
    f = c.add_filter(MacFilter("AA:BB:CC:DD:EE:FF", "Deny", "stolen"))
    assert f == MacFilter("aa-bb-cc-dd-ee-ff", "Deny", "stolen")
    pw.seed = "Add-DhcpServerv4Filter -List Deny -MacAddress aa-bb-cc-dd-ee-ff -Description stolen\n"
    pw.last_calls()
    assert c.set_filter(MacFilter("aa-bb-cc-dd-ee-ff", "Allow")).list == "Allow"
    calls = [x["cmd"] for x in pw.last_calls()]
    assert calls.index("Remove-DhcpServerv4Filter") < calls.index("Add-DhcpServerv4Filter")
    assert [x.mac_address for x in c.list_filters("Deny")] == ["aa-bb-cc-dd-ee-ff"]
    assert find_call(pw.last_calls(), "Get-DhcpServerv4Filter")["List"] == "Deny"
    st = c.set_filter_lists(allow=False, deny=True)
    assert st.deny and not st.allow
    assert c.get_filter_lists().deny is False  # fresh process, stub state reset


def test_option_definitions(pw: PwshRunner) -> None:
    c = DhcpClient(pw)
    d = c.add_option_definition(
        OptionDefinition(252, "WPAD", "String", description="proxy", default_value=["http://wpad/wpad.dat"])
    )
    assert d == OptionDefinition(252, "WPAD", "String", "proxy", "", False, ["http://wpad/wpad.dat"])
    add = find_call(pw.last_calls(), "Add-DhcpServerv4OptionDefinition")
    assert "MultiValued" not in add and "VendorClass" not in add
    d = c.add_option_definition(OptionDefinition(1, "Thing", "IPv4Address", vendor_class="Contoso", multi_valued=True))
    assert d.multi_valued and d.vendor_class == "Contoso" and d.default_value == []
    pw.seed = "Add-DhcpServerv4OptionDefinition -Name WPAD -OptionId 252 -Type String\n"
    assert c.set_option_definition(OptionDefinition(252, "WPAD2", "String", "d")).name == "WPAD2"
    assert [x.option_id for x in c.list_option_definitions()] == [252]
    assert find_call(pw.last_calls(), "Get-DhcpServerv4OptionDefinition")["All"] is True
    with pytest.raises(NotFound):
        c.get_option_definition(252, vendor_class="Contoso")
    c.remove_option_definition(252)
    assert find_call(pw.last_calls(), "Remove-DhcpServerv4OptionDefinition")["OptionId"] == 252


def test_policies(pw: PwshRunner) -> None:
    c = DhcpClient(pw)
    pw.seed = SEED_SCOPE
    pol = c.add_policy(
        Policy(
            name="printers",
            scope_id="10.1.20.0",
            description="HP",
            condition="or",
            criteria={"mac_address": ["EQ", "3c-d9-2b*"], "fqdn": ["EQ", "*.print.lab"]},
            lease_duration="1.00:00:00",
        )
    )
    assert pol.criteria == {"mac_address": ["EQ", "3c-d9-2b*"], "fqdn": ["EQ", "*.print.lab"]}
    assert pol.lease_duration == "1.00:00:00" and pol.enabled and pol.condition == "OR" and pol.scope_id == "10.1.20.0"
    add = find_call(pw.last_calls(), "Add-DhcpServerv4Policy")
    assert add["MacAddress"] == ["EQ", "3c-d9-2b*"] and add["ScopeId"] == "10.1.20.0" and add["LeaseDuration"] == 86400
    assert "UserClass" not in add and "ProcessingOrder" not in add
    # A processing order past the end of the level's list is refused with DHCP 20110 (#24).
    with pytest.raises(PSError) as err:
        c.add_policy(Policy(name="srv", criteria={"user_class": ["EQ", "phones"]}, processing_order=2))
    assert err.value.id.startswith("DHCP 20110") and err.value.command == "Add-DhcpServerv4Policy"
    pw.seed = SEED_SCOPE + "Add-DhcpServerv4Policy -Name first -Condition OR -UserClass EQ,x\n"
    srv = c.add_policy(Policy(name="srv", criteria={"user_class": ["EQ", "phones"]}, enabled=False, processing_order=2))
    assert srv.scope_id == "" and not srv.enabled and srv.processing_order == 2
    assert "ScopeId" not in find_call(pw.last_calls(), "Add-DhcpServerv4Policy")

    pw.seed = SEED_SCOPE + "Add-DhcpServerv4Policy -Name printers -Condition OR -ScopeId 10.1.20.0 -MacAddress EQ,aa*\n"
    pw.last_calls()
    pol = c.set_policy(Policy(name="printers", scope_id="10.1.20.0", description="", condition="AND"))
    assert pol.condition == "AND" and pol.criteria == {"mac_address": ["EQ", "aa*"]}
    with pytest.raises(NotFound):
        c.get_policy("printers")  # server level
    assert [p.name for p in c.list_policies("10.1.20.0")] == ["printers"]
    r = c.add_policy_ip_range(PolicyIPRange("10.1.20.0", "printers", "10.1.20.100", "10.1.20.120"))
    assert r == PolicyIPRange("10.1.20.0", "printers", "10.1.20.100", "10.1.20.120")
    c.remove_policy_ip_range(r)
    assert find_call(pw.last_calls(), "Remove-DhcpServerv4PolicyIPRange")["StartRange"] == "10.1.20.100"
    c.remove_policy("printers", "10.1.20.0")
    assert find_call(pw.last_calls(), "Remove-DhcpServerv4Policy")["ScopeId"] == "10.1.20.0"


def test_v6(pw: PwshRunner) -> None:
    c = DhcpClient(pw)
    s = c.add_v6_scope(ScopeV6(prefix="2001:DB8:1::", name="v6", state="inactive", t1="0.00:00:00"))
    # T1 and T2 not sent: Windows' own defaults, 4 and 6.4 days (#16).
    assert s == ScopeV6(
        "2001:db8:1::", "v6", "", "Inactive", 0, "8.00:00:00", "12.00:00:00", "4.00:00:00", "6.09:36:00"
    )
    add = find_call(pw.last_calls(), "Add-DhcpServerv6Scope")
    assert add["State"] == "InActive" and add["PreferredLifeTime"] == 8 * 86400 and "T1" not in add
    seed = "Add-DhcpServerv6Scope -Prefix 2001:db8:1:: -Name v6\n"
    pw.seed = seed
    assert c.set_v6_scope(ScopeV6(prefix="2001:db8:1::", name="v6b", preference=3)).preference == 3
    c.remove_v6_scope("2001:db8:1::", force=True)
    assert find_call(pw.last_calls(), "Remove-DhcpServerv6Scope")["Force"] is True
    with pytest.raises(NotFound):
        c.get_v6_scope("2001:db8:2::")

    r = c.add_v6_reservation(
        ReservationV6(prefix="2001:db8:1::", ip_address="2001:db8:1::10", client_duid="00:01:00:01:AB", iaid=7)
    )
    assert r == ReservationV6("2001:db8:1::", "2001:db8:1::10", "00-01-00-01-ab", 7, "2001:db8:1::10", "")
    pw.seed = (
        seed + "Add-DhcpServerv6Reservation -Prefix 2001:db8:1:: -IPAddress 2001:db8:1::10 -ClientDuid 01 -Iaid 1\n"
    )
    assert len(c.list_v6_reservations("2001:db8:1::")) == 1
    c.set_v6_reservation(ReservationV6("2001:db8:1::", "2001:db8:1::10", "0102", 2))
    st = find_call(pw.last_calls(), "Set-DhcpServerv6Reservation")
    assert st["Iaid"] == 2 and st["ClientDuid"] == "01-02" and st["Description"] == ""
    c.remove_v6_reservation("2001:db8:1::10")

    pw.seed = seed
    v = c.set_v6_option_value(OptionKeyV6(23, prefix="2001:db8:1::"), ["2001:db8::53"])
    assert v.value == ["2001:db8::53"] and v.prefix == "2001:db8:1::"
    assert find_call(pw.last_calls(), "Set-DhcpServerv6OptionValue")["Prefix"] == "2001:db8:1::"
    with pytest.raises(NotFound):
        c.get_v6_option_value(OptionKeyV6(23, prefix="2001:db8:1::"))
    c.remove_v6_option_value(OptionKeyV6(23))
    assert "Prefix" not in find_call(pw.last_calls(), "Remove-DhcpServerv6OptionValue")


def test_v6_classes(pw: PwshRunner) -> None:
    c = DhcpClient(pw)
    got = c.add_v6_class(DhcpClass(name="lab", type="user", data="LabComputers"))
    assert got == DhcpClass("lab", "User", "LabComputers", "")
    add = find_call(pw.last_calls(), "Add-DhcpServerv6Class")
    assert add["Type"] == "User" and "VendorId" not in add and "Description" not in add
    pw.seed = "Add-DhcpServerv6Class -Name lab -Type User -Data LabComputers\n"
    assert c.set_v6_class(DhcpClass("lab", "User", "Lab2", "d")).data == "Lab2"
    assert find_call(pw.last_calls(), "Set-DhcpServerv6Class")["Description"] == "d"
    assert [x.name for x in c.list_v6_classes()] == ["lab"]
    c.remove_v6_class("lab", "User")
    assert find_call(pw.last_calls(), "Remove-DhcpServerv6Class")["Confirm"] is False


def test_v6_exclusions_leases_and_all_options(pw: PwshRunner) -> None:
    pw.seed = """
Add-DhcpServerv6Scope -Prefix 2001:db8:1:: -Name v6
[void]$global:V6Exclusions.Add([pscustomobject]@{ Prefix = [ipaddress]'2001:db8:1::'
    StartRange = [ipaddress]'2001:db8:1::1'; EndRange = [ipaddress]'2001:db8:1::ff' })
[void]$global:V6Leases.Add([pscustomobject]@{ IPAddress = [ipaddress]'2001:db8:1::1:10'
    Prefix = [ipaddress]'2001:db8:1::'; ClientDuid = '00-01-00-01-aa-bb'; Iaid = [uint32]7; HostName = 'pc01'
    AddressType = 'IANA'; LeaseExpiryTime = [datetime]::new(2026, 10, 20, 8, 0, 0, [DateTimeKind]::Utc)
    Description = $null })
"""
    c = DhcpClient(pw)
    (e,) = c.list_v6_exclusion_ranges("2001:db8:1::")
    assert (e.prefix, e.start_range, e.end_range) == ("2001:db8:1::", "2001:db8:1::1", "2001:db8:1::ff")
    (lease,) = c.list_v6_leases("2001:db8:1::")
    assert lease.iaid == 7 and lease.host_name == "pc01" and lease.lease_expiry_time == "2026-10-20T08:00:00Z"
    assert lease.description == ""
    # Windows has no DHCPv6 lease state (issue #22); AddressType is not one, so nothing stands in for it.
    assert "address_state" not in lease.to_dict()
    c.list_v6_option_values(prefix="2001:db8:1::", all=True)
    assert find_call(pw.last_calls(), "Get-DhcpServerv6OptionValue")["All"] is True


# ---- issue #15: a getter for a missing object ----

EMPTY_PIPELINE = "1 | Where-Object { $false } | Select-Object -First 1"


def test_stubs_reproduce_the_windows_powershell_empty_object(pw: PwshRunner) -> None:
    # Without the envelope's own handling, Windows PowerShell 5.1 prints {} for "nothing" (AutomationNull).
    raw = pw.run("$out = " + EMPTY_PIPELINE + "\nConvertTo-Json -InputObject @{ ok = $true; data = $out } -Compress")
    assert parse(raw) == (True, {})


def test_envelope_turns_an_empty_result_into_null(pw: PwshRunner) -> None:
    assert parse(pw.run(Script("t.empty", "$out = " + EMPTY_PIPELINE).wrap())) == (False, None)
    # An empty list stays an empty list, and a real value is untouched.
    assert parse(pw.run(Script("t.list", "$out = @(" + EMPTY_PIPELINE + ")").wrap())) == (True, [])
    assert parse(pw.run(Script("t.value", "$out = [ordered]@{ a = 1 }").wrap())) == (True, {"a": 1})


SEED_V6 = "Add-DhcpServerv6Scope -Prefix 2001:db8:1:: -Name v6\n"

MISSING = {
    "filter": lambda c: c.get_filter("aa-bb-cc-dd-ee-01"),
    "superscope": lambda c: c.get_superscope("nope"),
    "policy": lambda c: c.get_policy("nope", "10.1.20.0"),
    "server policy": lambda c: c.get_policy("nope"),
    "v6 scope": lambda c: c.get_v6_scope("2001:db8:9::"),
    "class": lambda c: c.get_class("nope", "User"),
    "reservation": lambda c: c.get_reservation("10.1.20.0", "10.1.20.77"),
    "exclusion": lambda c: c.get_exclusion_range(ExclusionRange("10.1.20.0", "10.1.20.1", "10.1.20.2")),
    "option": lambda c: c.get_option_value(OptionKey(15, scope_id="10.1.20.0")),
    "reservation option": lambda c: c.get_option_value(OptionKey(15, "10.1.20.0", reserved_ip="10.1.20.5")),
    "server option": lambda c: c.get_option_value(OptionKey(15)),
    "option definition": lambda c: c.get_option_definition(252),
    "failover": lambda c: c.get_failover("nope"),
    "v6 reservation": lambda c: c.get_v6_reservation("2001:db8:1::", "2001:db8:1::99"),
    "v6 option": lambda c: c.get_v6_option_value(OptionKeyV6(23, prefix="2001:db8:1::")),
    "v6 reservation option": lambda c: c.get_v6_option_value(
        OptionKeyV6(23, prefix="2001:db8:1::", reserved_ip="2001:db8:1::10")
    ),
}


@pytest.mark.parametrize("getter", MISSING.values(), ids=MISSING.keys())
def test_missing_objects_raise_not_found(pw: PwshRunner, getter) -> None:
    pw.seed = SEED_SCOPE + SEED_V6
    with pytest.raises(NotFound):
        getter(DhcpClient(pw))


# ---- issues #20 and #17: options on a reservation ----


def test_stub_option_cmdlets_refuse_scope_and_reservation_together(pw: PwshRunner) -> None:
    for body, msg in (
        (
            "Set-DhcpServerv4OptionValue @cn -ScopeId 10.1.20.0 -ReservedIP 10.1.20.5 -OptionId 15 -Value x",
            "ScopeId and ReservedIP cannot be specified together",
        ),
        (
            "Remove-DhcpServerv4OptionValue @cn -ScopeId 10.1.20.0 -ReservedIP 10.1.20.5 -OptionId 15",
            "ScopeId and ReservedIP cannot be specified together",
        ),
        (
            "Set-DhcpServerv6OptionValue @cn -Prefix 2001:db8:1:: -ReservedIP 2001:db8:1::10 -OptionId 23 -Value x",
            "Prefix and ReservedIP cannot be specified together",
        ),
        (
            "Remove-DhcpServerv6OptionValue @cn -Prefix 2001:db8:1:: -ReservedIP 2001:db8:1::10 -OptionId 23",
            "Prefix and ReservedIP cannot be specified together",
        ),
    ):
        with pytest.raises(PSError, match=msg) as ei:
            parse(pw.run(Script("t.both", body).wrap()))
        assert ei.value.category == "InvalidArgument" and not ei.value.not_found


def test_reservation_option_is_named_by_its_ip_alone(pw: PwshRunner) -> None:
    pw.seed = SEED_SCOPE
    c = DhcpClient(pw)
    k = OptionKey(15, scope_id="10.1.20.0", reserved_ip="10.1.20.5")
    v = c.set_option_value(k, ["lab.example"])
    # The scope stays in the result as context, although Windows is never sent it.
    assert (v.value, v.scope_id, v.reserved_ip) == (["lab.example"], "10.1.20.0", "10.1.20.5")
    calls = pw.last_calls()
    st = find_call(calls, "Set-DhcpServerv4OptionValue")
    assert st["ReservedIP"] == "10.1.20.5" and "ScopeId" not in st
    assert "ScopeId" not in find_call(calls, "Get-DhcpServerv4OptionValue")

    pw.seed = SEED_SCOPE + "Set-DhcpServerv4OptionValue -ReservedIP 10.1.20.5 -OptionId 15 -Value lab.example\n"
    got = c.get_option_value(k)
    assert (got.value, got.scope_id, got.reserved_ip) == (["lab.example"], "10.1.20.0", "10.1.20.5")
    assert "ScopeId" not in find_call(pw.last_calls(), "Get-DhcpServerv4OptionValue")
    (listed,) = c.list_option_values(scope_id="10.1.20.0", reserved_ip="10.1.20.5", all=True)
    assert (listed.option_id, listed.scope_id, listed.reserved_ip) == (15, "10.1.20.0", "10.1.20.5")
    assert "ScopeId" not in find_call(pw.last_calls(), "Get-DhcpServerv4OptionValue")
    c.remove_option_value(k)
    rm = find_call(pw.last_calls(), "Remove-DhcpServerv4OptionValue")
    assert rm["ReservedIP"] == "10.1.20.5" and "ScopeId" not in rm

    # Scope values still name their scope.
    c.set_option_value(OptionKey(15, scope_id="10.1.20.0"), ["scope.example"])
    st = find_call(pw.last_calls(), "Set-DhcpServerv4OptionValue")
    assert st["ScopeId"] == "10.1.20.0" and "ReservedIP" not in st


def test_v6_reservation_option_is_named_by_its_ip_alone(pw: PwshRunner) -> None:
    pw.seed = SEED_V6
    c = DhcpClient(pw)
    k = OptionKeyV6(24, prefix="2001:db8:1::", reserved_ip="2001:db8:1::10")
    v = c.set_v6_option_value(k, ["lab.example"])
    assert (v.value, v.prefix, v.reserved_ip) == (["lab.example"], "2001:db8:1::", "2001:db8:1::10")
    calls = pw.last_calls()
    st = find_call(calls, "Set-DhcpServerv6OptionValue")
    assert st["ReservedIP"] == "2001:db8:1::10" and "Prefix" not in st
    assert "Prefix" not in find_call(calls, "Get-DhcpServerv6OptionValue")

    pw.seed = SEED_V6 + "Set-DhcpServerv6OptionValue -ReservedIP 2001:db8:1::10 -OptionId 24 -Value lab.example\n"
    got = c.get_v6_option_value(k)
    assert (got.value, got.prefix, got.reserved_ip) == (["lab.example"], "2001:db8:1::", "2001:db8:1::10")
    (listed,) = c.list_v6_option_values(prefix="2001:db8:1::", reserved_ip="2001:db8:1::10", all=True)
    assert (listed.prefix, listed.reserved_ip) == ("2001:db8:1::", "2001:db8:1::10")
    assert "Prefix" not in find_call(pw.last_calls(), "Get-DhcpServerv6OptionValue")
    c.remove_v6_option_value(k)
    rm = find_call(pw.last_calls(), "Remove-DhcpServerv6OptionValue")
    assert rm["ReservedIP"] == "2001:db8:1::10" and "Prefix" not in rm

    c.set_v6_option_value(OptionKeyV6(24, prefix="2001:db8:1::"), ["scope.example"])
    st = find_call(pw.last_calls(), "Set-DhcpServerv6OptionValue")
    assert st["Prefix"] == "2001:db8:1::" and "ReservedIP" not in st
