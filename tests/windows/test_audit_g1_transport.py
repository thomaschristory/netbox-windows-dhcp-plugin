"""Regression tests for the transport findings of the code quality audit (group g1-transport)."""

from __future__ import annotations

import http.server
import json
import threading
import time
import types
from typing import Any

import pytest

from netbox_windows_dhcp_plugin import conf
from netbox_windows_dhcp_plugin.sync.diff import Change
from netbox_windows_dhcp_plugin.sync.windows_writer import WindowsWriter
from netbox_windows_dhcp_plugin.windows import psscript, ssh, winrm
from netbox_windows_dhcp_plugin.windows.client import DhcpClient
from netbox_windows_dhcp_plugin.windows.psscript import NotFound, PSError, Script
from netbox_windows_dhcp_plugin.windows.runner import RunnerError, clean_stderr
from netbox_windows_dhcp_plugin.windows.ssh import SSHConfig
from netbox_windows_dhcp_plugin.windows.winrm import WinRMConfig, WinRMRunner

from ..sync.test_windows_writer import client, failover, fake, push, wanted  # noqa: F401 - client and fake are fixtures
from .test_winrm import cfg, pypsrp  # noqa: F401 - pypsrp is a fixture

OK_ENVELOPE = b"eyJvayI6dHJ1ZSwiZGF0YSI6bnVsbH0="  # base64 of {"ok":true,"data":null}


# ---- F11: an inline credential set to None ----


@pytest.mark.parametrize("key", ["username", "password"])
def test_inline_none_is_an_error_not_the_word_none(key: str) -> None:
    profiles = {"p": {"username": "u", "password": "x", key: None}}
    with pytest.raises(conf.CredentialError, match=f"{key} is set to None"):
        conf.get_credentials("p", profiles, environ={})


@pytest.mark.parametrize("key", ["private_key", "secret"])
def test_inline_none_for_a_key_that_is_not_used_reads_as_not_set(key: str) -> None:
    # A working 0.2.0 profile such as {"secret": os.environ.get("X")} with X unset must keep connecting.
    profiles = {"p": {"username": "u", "password": "x", key: None}}
    creds = conf.get_credentials("p", profiles, environ={})
    assert getattr(creds, key) == "" and creds.password == "x"


def test_inline_none_is_an_error_for_the_key_the_caller_needs() -> None:
    profiles = {"p": {"username": None, "password": None, "secret": None}, "q": {"username": None, "secret": "s"}}
    with pytest.raises(conf.CredentialError, match="'p': secret is set to None"):
        conf.get_credentials("p", profiles, environ={}, needed=("secret",))
    # A profile read only for its secret is not stopped by the keys it does not use.
    assert conf.get_credentials("q", profiles, environ={}, needed=("secret",)).secret == "s"


def test_inline_values_that_are_not_strings_still_work() -> None:
    creds = conf.get_credentials("p", {"p": {"username": "u", "password": 123456}}, environ={})
    assert creds.password == "123456" and creds.secret == ""


# ---- F12: allowed_hosts address patterns and numeric host spellings ----


@pytest.mark.parametrize(
    ("patterns", "host", "want"),
    [
        (["10.20.*.*"], "10.20.evil.com", False),
        (["10.20.*.*"], "10.20.3.4", True),
        (["10.20.0.*"], "10.20.0.xyz", False),
        (["10.20.0.*"], "10.20.0.7", True),
        (["192.168.1.?"], "192.168.1.x", False),
        (["192.168.1.?"], "192.168.1.9", True),
        (["10.0.0.5"], "10.0.0.5", True),
        (["dhcp*.corp.example"], "dhcp01.corp.example", True),
        (["dhcp*.corp.example"], "DHCP01.corp.example.", True),
        (["*"], "dhcp01", True),
        (["*.*.*"], "dhcp01.corp.example", True),
        # A name wildcard no longer covers IPv6 addresses (they hold no dot).
        (["*"], "2001:db8::1", False),
        (["*"], "::1", False),
        # A short pattern holding a number was meant as an address range: it allows no name (and no address).
        (["10.*"], "10.com", False),
        (["10.20.*"], "10.20.evil", False),
        (["192.168.*"], "192.168.example", False),
        (["10.*"], "10.1.2.3", False),
        # Wildcards alone, or mixed with letters, are still name patterns.
        (["1*"], "10", False),
        (["1*.example"], "10.example", True),
        (["*.*"], "dhcp01.corp", True),
        (["2001:db8::*"], "2001:db8::1", True),
        (["2001:DB8::1"], "2001:db8:0::1", True),
        (["2001:db8::*"], "dhcp01", False),
    ],
)
def test_address_patterns_match_addresses_and_name_patterns_match_names(patterns, host, want) -> None:
    assert conf.host_allowed("p", {"allowed_hosts": patterns}, host) is want


@pytest.mark.parametrize("host", ["2130706433", "0x7f.1", "010.1.1.1", "127.1", "10.20.3", "dhcp.0x10", "0X7F.1"])
def test_names_the_resolver_reads_as_addresses_are_not_host_names(host: str) -> None:
    assert conf.normalise_host(host) is None
    with pytest.raises(conf.CredentialError, match="not a valid host"):
        conf.check_host_allowed("p", host, profiles={"p": {}})


@pytest.mark.parametrize("host", ["10.20.evil.com", "dhcp01", "dhcp-2.corp.example", "srv1.x0", "10.0.0.5", "::1"])
def test_ordinary_names_and_addresses_are_still_host_names(host: str) -> None:
    assert conf.normalise_host(host) == host


# ---- F13: the writer asks before creating a failover relationship ----


def test_failover_create_is_refused_by_the_partner_check(client, fake) -> None:  # noqa: F811
    push(client, [r for r in wanted() if r.kind != "failover"])
    asked: list[tuple[str, str]] = []

    def refuse(name: str, partner: str) -> None:
        asked.append((name, partner))
        raise ValueError(f"failover {name}: partner not allowed")

    writer = WindowsWriter(client, failover_partner_check=refuse)
    with pytest.raises(ValueError, match="partner not allowed"):
        writer.apply(Change("create", "failover", "ha", after=failover().fields))
    # The check sees the very partner the cmdlet would be given, not one looked up again later.
    assert asked == [("HA", failover().fields["partner_server"])]
    assert fake.failovers == {} and "failover.add" not in fake.ops()


def test_failover_changes_and_deletes_do_not_ask_the_partner_check(client, fake) -> None:  # noqa: F811
    push(client, wanted())

    def refuse(name: str, partner: str) -> None:
        raise AssertionError("only creation names a partner chosen in NetBox")

    writer = WindowsWriter(client, failover_partner_check=refuse)
    before = failover().fields
    writer.apply(Change("update", "failover", "ha", before=before, after=failover(load_balance_percent=70).fields))
    assert fake.failovers["ha"]["load_balance_percent"] == 70
    writer.apply(Change("delete", "failover", "ha", before=before))
    assert fake.failovers == {}


# ---- F80: a missing cmdlet is not a missing object ----


@pytest.mark.parametrize(
    ("err_id", "want"),
    [
        ("CommandNotFoundException", False),
        ("CommandNotFoundException,Microsoft.PowerShell.Commands.GetCommandCommand", False),
        ("CommandNotFoundExceptionLookalike", True),
        ("DHCP 20049,Get-DhcpServerv4Policy", True),
    ],
)
def test_command_not_found_is_not_not_found(err_id: str, want: bool) -> None:
    raw = json.dumps({"ok": False, "error": {"message": "m", "category": "ObjectNotFound", "id": err_id}})
    with pytest.raises(PSError) as ei:
        psscript.parse(raw)
    assert ei.value.not_found is want
    assert isinstance(ei.value, NotFound) is want


@pytest.mark.pwsh
def test_pwsh_script_calling_an_unknown_cmdlet_is_a_real_error(pwsh_path: str, tmp_path) -> None:
    from .test_pwsh import PwshRunner

    runner = PwshRunner(pwsh_path, tmp_path / "calls.jsonl")
    with pytest.raises(PSError, match="Get-NoSuchAuditCmdlet") as ei:
        DhcpClient(runner)._run(Script("audit.missing", "$out = Get-NoSuchAuditCmdlet"))
    assert not ei.value.not_found and not isinstance(ei.value, NotFound)


# ---- F83: output from the host is bounded ----


def test_clean_stderr_is_fast_on_unterminated_tags() -> None:
    hostile = "#< CLIXML\n" + '<S S="Error">' * 200_000  # about 2.6 MB; the old regular expression took minutes
    started = time.monotonic()
    assert clean_stderr(hostile) == "(no error output)"
    assert time.monotonic() - started < 5


def test_clean_stderr_skips_an_unterminated_string_and_bounds_the_text() -> None:
    text = '#< CLIXML\n<Objs><S S="Error">first_x000D__x000A_</S><S S="progress">no</S><S S="Error">cut off'
    assert clean_stderr(text) == "first"
    assert len(clean_stderr("x" * 5_000_000)) == 64 * 1024
    assert len(clean_stderr('#< CLIXML\n<S S="Error">' + "y" * 500_000 + "</S>")) == 64 * 1024


def test_winrm_output_over_the_cap_is_refused(pypsrp, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(winrm, "MAX_OUTPUT", 1000)
    f = pypsrp(lambda argv, stdin: (b"A" * 5000, b"", 0))
    with pytest.raises(RunnerError, match="winrm: PowerShell sent more than"):
        WinRMRunner(cfg()).run("# x", {})
    assert f.signals and f.shells_closed == 1 and f.closed == 1


# ---- F84: configs keep secrets out of repr ----


def test_transport_configs_hide_secrets_in_repr() -> None:
    w = WinRMConfig("dhcp01.example.com", "svc", "S3cret-pw")
    assert w.password == "S3cret-pw" and w.port is None
    assert "S3cret-pw" not in repr(w) and "svc" in repr(w)
    s = SSHConfig("dhcp01.example.com", "svc", "S3cret-pw", "PRIVATE-KEY-TEXT", 2222)
    assert (s.password, s.private_key, s.port) == ("S3cret-pw", "PRIVATE-KEY-TEXT", 2222)
    assert "S3cret-pw" not in repr(s) and "PRIVATE-KEY-TEXT" not in repr(s) and "svc" in repr(s)


# ---- F85: WinRM does not follow redirects ----


def test_winrm_session_is_built_with_redirects_forbidden(pypsrp) -> None:  # noqa: F811
    f = pypsrp(lambda argv, stdin: (OK_ENVELOPE, b"", 0))
    WinRMRunner(cfg()).run("# x", {})
    assert [s.max_redirects for s in f.sessions] == [0]


def test_winrm_stops_when_pypsrp_cannot_forbid_redirects(pypsrp) -> None:  # noqa: F811
    f = pypsrp(lambda argv, stdin: (OK_ENVELOPE, b"", 0))
    f.wsman_mod.WSMan.transport = None  # a pypsrp without the session builder the guard relies on
    original = f.wsman_mod.WSMan.__init__

    def init(self, **kwargs: Any) -> None:
        original(self, **kwargs)
        del self.transport

    f.wsman_mod.WSMan.__init__ = init
    with pytest.raises(RunnerError, match="forbid HTTP redirects"):
        WinRMRunner(cfg()).run("# x", {})
    assert f.sent == [] and f.closed == 1


class _Recorder(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append((self.path, self.headers.get("Authorization")))  # type: ignore[attr-defined]
        target = self.server.redirect_to  # type: ignore[attr-defined]
        if target:
            self.send_response(307)
            self.send_header("Location", target)
        else:
            self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


def _http_server(redirect_to: str = "") -> http.server.HTTPServer:
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    srv.seen = []  # type: ignore[attr-defined]
    srv.redirect_to = redirect_to  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_winrm_redirect_is_refused_and_the_other_host_sees_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the real pypsrp: the approved host answers 307, and nothing (credentials included) goes any further."""
    pytest.importorskip("pypsrp")
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    other = _http_server()
    approved = _http_server(f"http://127.0.0.1:{other.server_address[1]}/wsman")
    try:
        runner = WinRMRunner(
            WinRMConfig("127.0.0.1", "svc", "pw", port=approved.server_address[1], https=False, auth="basic", timeout=5)
        )
        with pytest.raises(RunnerError, match="winrm: running PowerShell: .*redirect"):
            runner.run("# x", {})
        assert approved.seen and approved.seen[0][1]  # the approved host was asked, with the credentials
        assert other.seen == []
    finally:
        for srv in (approved, other):
            srv.shutdown()
            srv.server_close()


# ---- F86: a WinRM command that never finishes is abandoned ----


def _clock(monkeypatch: pytest.MonkeyPatch, step: float) -> None:
    now = [0.0]

    def monotonic() -> float:
        now[0] += step
        return now[0]

    monkeypatch.setattr(winrm, "time", types.SimpleNamespace(monotonic=monotonic))


def test_winrm_total_timeout_defaults_are_generous() -> None:
    assert cfg().effective_total_timeout == 600  # 10 x 60 s
    assert cfg(timeout=120).effective_total_timeout == 1200
    assert cfg(timeout=5).effective_total_timeout == 600  # never under ten minutes
    assert cfg(timeout=120, total_timeout=30).effective_total_timeout == 30


def test_winrm_command_that_never_finishes_is_abandoned(pypsrp, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    f = pypsrp(lambda argv, stdin: (b"", b"", 0))
    polls: list[int] = []
    f.process_cls.poll_invoke = lambda self: polls.append(1)  # the operation timeout, swallowed: never done
    _clock(monkeypatch, 100)
    with pytest.raises(RunnerError, match=r"^winrm: PowerShell did not finish within 600 s$"):
        WinRMRunner(cfg()).run("# x", {"shared_secret": "hunter2"})
    assert 4 <= len(polls) <= 6
    assert f.signals == ["http://schemas.microsoft.com/wbem/wsman/1/windows/shell/signal/Terminate"]
    assert f.shells_closed == 1 and f.closed == 1


def test_winrm_slow_command_within_the_total_still_succeeds(pypsrp, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    f = pypsrp(lambda argv, stdin: (OK_ENVELOPE, b"", 0))
    finish = f.process_cls.poll_invoke
    polls: list[int] = []

    def poll(self) -> None:
        polls.append(1)
        if len(polls) == 4:  # 400 s of silence: longer than the 60 s wait, shorter than the total
            finish(self)

    f.process_cls.poll_invoke = poll
    _clock(monkeypatch, 100)
    assert WinRMRunner(cfg()).run("# x", {}) == b'{"ok":true,"data":null}'
    assert len(polls) == 4


# ---- F82, F83: SSH ----

paramiko = pytest.importorskip("paramiko")

from . import test_ssh as sshkit  # noqa: E402
from .test_ssh import ssh_server  # noqa: E402, F401 - fixture


@pytest.fixture
def password_attempts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What the test SSH server was offered as a password or as a keyboard-interactive answer."""
    seen: list[str] = []
    check_password = sshkit._Iface.check_auth_password
    check_answers = sshkit._Iface.check_auth_interactive_response

    def password(self, username: str, password: str) -> int:
        seen.append(password)
        return check_password(self, username, password)

    def answers(self, responses) -> int:
        seen.extend(responses)
        return check_answers(self, responses)

    monkeypatch.setattr(sshkit._Iface, "check_auth_password", password)
    monkeypatch.setattr(sshkit._Iface, "check_auth_interactive_response", answers)
    return seen


def test_key_passphrase_is_never_offered_as_a_password(ssh_server, password_attempts) -> None:  # noqa: F811
    # The server refuses this key but would accept the passphrase as the account password.
    srv = ssh_server(behaviour=sshkit.Behaviour(client_key=sshkit.key("ecdsa-other-client")))
    pem = sshkit.private_pem(sshkit.key("ecdsa-client"), sshkit.PASSWORD)
    with pytest.raises(RunnerError, match="ssh: authentication failed: publickey") as ei:
        sshkit.runner(srv, private_key=pem).run("# x", {})
    assert password_attempts == []
    assert "password:" not in str(ei.value) and "keyboard-interactive" not in str(ei.value)


def test_plain_key_with_an_account_password_still_falls_back(ssh_server, password_attempts) -> None:  # noqa: F811
    srv = ssh_server(behaviour=sshkit.Behaviour(client_key=sshkit.key("ecdsa-other-client")))
    r = sshkit.runner(srv, private_key=sshkit.private_pem(sshkit.key("ecdsa-client")))
    try:
        DhcpClient(r).list_scopes()
    finally:
        r.close()
    assert password_attempts == [sshkit.PASSWORD]


def test_private_key_parsing_reports_whether_the_password_opened_it() -> None:
    k = sshkit.key("ecdsa-client")
    assert ssh._parse_private_key(sshkit.private_pem(k), "")[1] is False
    assert ssh._parse_private_key(sshkit.private_pem(k), "account-password")[1] is False
    assert ssh._parse_private_key(sshkit.private_pem(k, "passphrase"), "passphrase")[1] is True
    with pytest.raises(ValueError, match="ssh: parsing private_key"):
        ssh._parse_private_key(sshkit.private_pem(k, "passphrase"), "wrong")


def test_ssh_output_over_the_cap_is_refused(ssh_server, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(ssh, "MAX_OUTPUT", 1000)
    srv = ssh_server(behaviour=sshkit.Behaviour(respond=lambda cmd, stdin: (b"A" * 5000, b"", 0)))
    r = sshkit.runner(srv)
    try:
        with pytest.raises(RunnerError, match="ssh: PowerShell sent more than"):
            r.run("# x", {})
    finally:
        r.close()


def test_ssh_error_output_is_bounded_but_still_drained(ssh_server, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(ssh, "MAX_STDERR", 1000)
    srv = ssh_server(behaviour=sshkit.Behaviour(respond=lambda cmd, stdin: (b"", b"boom " * 100_000, 3)))
    r = sshkit.runner(srv)
    try:
        with pytest.raises(RunnerError, match="ssh: PowerShell exited with code 3: boom boom") as ei:
            r.run("# x", {})
    finally:
        r.close()
    assert len(str(ei.value)) < 40_000
