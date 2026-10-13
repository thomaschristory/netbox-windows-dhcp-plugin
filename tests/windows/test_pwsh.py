"""Run the real rendered scripts and bootstrap in a local pwsh against stub cmdlets.

Port of internal/dhcp/pwsh_test.go. testdata/stubs.ps1 is loaded as an in-memory
module before each script; every stub records its bound parameters as one JSON line
in $env:WD_CALLS. Skipped when pwsh is not installed (set $PWSH to point at one).

The stubs only check that the scripts run and bind parameters the way the stubs
declare them; parameter lists for the cmdlets the Go provider did not use are not
verified against Windows.
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
from netbox_windows_dhcp_plugin.windows.psscript import NotFound, PSError, Script, command_argv, decode_output, render
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
    pw2 = PwshRunner(pw.pwsh, tmp_path / "c2.jsonl")
    pw2.run = lambda script, params=None: _run_without_stubs(pw.pwsh, script, params)  # type: ignore[method-assign]
    with pytest.raises(PSError, match="DhcpServer PowerShell module is not available") as ei:
        DhcpClient(pw2).list_scopes()
    assert not ei.value.not_found


def _run_without_stubs(pwsh: str, script: str, params: Any) -> bytes:
    args, stdin = command_argv(render(script, params))
    p = subprocess.run([pwsh, *args], input=stdin.encode(), capture_output=True, timeout=120)
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
    c.remove_failover("lb")
    assert find_call(pw.last_calls(), "Remove-DhcpServerv4Failover")["Force"] is False
    assert [x.name for x in c.list_failovers()] == ["lb"]


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
    assert s == ScopeV6("2001:db8:1::", "v6", "", "Inactive", 0, "8.00:00:00", "12.00:00:00", "", "")
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
