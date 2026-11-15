"""Regression tests for the transport findings of the code quality audit (group g1-transport)."""

from __future__ import annotations

import json
import time
import types

import pytest

from netbox_windows_dhcp_plugin import conf
from netbox_windows_dhcp_plugin.windows import psscript, ssh, winrm
from netbox_windows_dhcp_plugin.windows.client import DhcpClient
from netbox_windows_dhcp_plugin.windows.psscript import NotFound, PSError, Script
from netbox_windows_dhcp_plugin.windows.runner import RunnerError, clean_stderr
from netbox_windows_dhcp_plugin.windows.winrm import WinRMRunner

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
