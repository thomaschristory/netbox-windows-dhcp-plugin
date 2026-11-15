"""Security regression tests: allowed_hosts matching, failover partner check, redirects, key passphrases, repr."""

from __future__ import annotations

import http.server
import threading
from typing import Any

import pytest

from netbox_windows_dhcp_plugin import conf
from netbox_windows_dhcp_plugin.sync.diff import Change
from netbox_windows_dhcp_plugin.sync.windows_writer import WindowsWriter
from netbox_windows_dhcp_plugin.windows import ssh
from netbox_windows_dhcp_plugin.windows.client import DhcpClient
from netbox_windows_dhcp_plugin.windows.runner import RunnerError
from netbox_windows_dhcp_plugin.windows.ssh import SSHConfig
from netbox_windows_dhcp_plugin.windows.winrm import WinRMConfig, WinRMRunner

from ..sync.test_windows_writer import client, failover, fake, push, wanted  # noqa: F401 - client and fake are fixtures
from . import test_ssh as sshkit
from .test_ssh import ssh_server  # noqa: F401 - fixture
from .test_winrm import cfg, pypsrp  # noqa: F401 - pypsrp is a fixture

OK_ENVELOPE = b"eyJvayI6dHJ1ZSwiZGF0YSI6bnVsbH0="  # base64 of {"ok":true,"data":null}


# ---- allowed_hosts: address patterns and numeric host spellings ----


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


# ---- the writer asks before creating a failover relationship ----


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


# ---- configs keep secrets out of repr ----


def test_transport_configs_hide_secrets_in_repr() -> None:
    w = WinRMConfig("dhcp01.example.com", "svc", "S3cret-pw")
    assert w.password == "S3cret-pw" and w.port is None
    assert "S3cret-pw" not in repr(w) and "svc" in repr(w)
    s = SSHConfig("dhcp01.example.com", "svc", "S3cret-pw", "PRIVATE-KEY-TEXT", 2222)
    assert (s.password, s.private_key, s.port) == ("S3cret-pw", "PRIVATE-KEY-TEXT", 2222)
    assert "S3cret-pw" not in repr(s) and "PRIVATE-KEY-TEXT" not in repr(s) and "svc" in repr(s)


# ---- WinRM does not follow redirects ----


def test_winrm_session_is_built_with_redirects_forbidden(pypsrp) -> None:  # noqa: F811
    f = pypsrp(lambda argv, stdin: (OK_ENVELOPE, b"", 0))
    WinRMRunner(cfg()).run("# x", {})
    assert [s.max_redirects for s in f.sessions] == [0]


def test_winrm_stops_when_pypsrp_cannot_forbid_redirects(pypsrp) -> None:  # noqa: F811
    f = pypsrp(lambda argv, stdin: (OK_ENVELOPE, b"", 0))
    original = f.wsman_mod.WSMan.__init__

    def init(self, **kwargs: Any) -> None:
        original(self, **kwargs)
        del self.transport  # a pypsrp without the session builder the guard relies on

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


# ---- SSH: a key passphrase is not an account password ----


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
