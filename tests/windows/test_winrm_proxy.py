"""WinRM and the worker's proxy environment variables (issue #105): ignored unless the profile says "env"."""

from __future__ import annotations

import base64
import http.server
import threading
from typing import Any

import pytest

from netbox_windows_dhcp_plugin.windows import winrm
from netbox_windows_dhcp_plugin.windows.runner import RunnerError
from netbox_windows_dhcp_plugin.windows.winrm import PROXY_ENV, PROXY_NONE, WinRMConfig, WinRMRunner

from .test_winrm import cfg, pypsrp  # noqa: F401 - pypsrp is a fixture

OK_ENVELOPE = base64.b64encode(b'{"ok":true,"data":null}')
PROXY = "http://proxy.example.com:3128"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
NO_PROXY_VARS = ("NO_PROXY", "no_proxy")


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch):
    """Every proxy variable requests reads set to PROXY, upper and lower case, and no NO_PROXY."""
    for var in NO_PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    for var in PROXY_VARS:
        monkeypatch.setenv(var, PROXY)


def test_default_is_no_proxy() -> None:
    assert WinRMConfig("h", "u", "p").proxy == PROXY_NONE
    assert winrm.PROXIES == (PROXY_NONE, PROXY_ENV)


@pytest.mark.parametrize("value", ["ENV", "", "system", "http://proxy:3128"])
def test_unknown_proxy_values_are_refused(value: str) -> None:
    with pytest.raises(ValueError, match="unsupported proxy"):
        WinRMRunner(cfg(proxy=value))


def test_wsman_kwargs_ask_pypsrp_for_no_proxy_by_default() -> None:
    assert WinRMRunner(cfg()).wsman_kwargs()["no_proxy"] is True
    assert "no_proxy" not in WinRMRunner(cfg(proxy="env")).wsman_kwargs()
    # The plugin never names a proxy itself: with "env" the environment decides.
    assert "proxy" not in WinRMRunner(cfg(proxy="env")).wsman_kwargs()


def test_session_stops_reading_the_environment_by_default(pypsrp) -> None:  # noqa: F811
    f = pypsrp(lambda argv, stdin: (OK_ENVELOPE, b"", 0))
    WinRMRunner(cfg()).run("# x", {})
    WinRMRunner(cfg(proxy="env")).run("# x", {})
    direct, env = f.sessions
    assert direct.trust_env is False and direct.proxies == {}
    assert env.trust_env is True and env.proxies == {"https": PROXY}


def test_every_connect_retry_gets_a_direct_session(pypsrp, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    f = pypsrp(lambda argv, stdin: (OK_ENVELOPE, b"", 0))
    monkeypatch.setattr(winrm.time, "sleep", lambda s: None)
    f.open_errors = [ConnectionRefusedError(), ConnectionRefusedError()]
    WinRMRunner(cfg()).run("# x", {})
    assert len(f.sessions) == 3
    assert all(s.trust_env is False and s.proxies == {} for s in f.sessions)


def _real_session(proxy: str, https: bool):
    """The requests session the real pypsrp would use for one call, after the plugin prepared it (no network)."""
    pytest.importorskip("pypsrp")
    pytest.importorskip("spnego")
    from pypsrp.wsman import WSMan

    runner = WinRMRunner(cfg(https=https, proxy=proxy))
    wsman = WSMan(**runner.wsman_kwargs())
    winrm._prepare_session(wsman, proxy)
    return wsman.transport.session, wsman.transport.endpoint


def _proxy_for(session: Any, url: str) -> str | None:
    """The proxy requests would send `url` through: what Session.send resolves, then what the adapter picks."""
    import requests
    from requests.utils import select_proxy

    prepared = session.prepare_request(requests.Request("POST", url))
    return select_proxy(url, session.rebuild_proxies(prepared, session.proxies))


@pytest.mark.parametrize("https", [True, False])
def test_real_pypsrp_session_ignores_every_proxy_variable_by_default(proxy_env, https: bool) -> None:
    session, endpoint = _real_session(PROXY_NONE, https)
    assert session.trust_env is False
    assert session.max_redirects == 0
    # The endpoint, and the other scheme too (pypsrp's own no_proxy only clears the scheme in use).
    for url in (endpoint, "http://dhcp01.example.local:5985/wsman", "https://dhcp01.example.local:5986/wsman"):
        assert _proxy_for(session, url) is None, url


@pytest.mark.parametrize("https", [True, False])
@pytest.mark.parametrize("var", ["HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"])
def test_real_pypsrp_session_follows_each_variable_with_env(monkeypatch: pytest.MonkeyPatch, https: bool, var: str):
    for name in (*PROXY_VARS, *NO_PROXY_VARS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(var, PROXY)
    session, endpoint = _real_session(PROXY_ENV, https)
    scheme = "https" if https else "http"
    applies = var.lower() in (f"{scheme}_proxy", "all_proxy")
    assert _proxy_for(session, endpoint) == (PROXY if applies else None)


def test_real_pypsrp_session_with_env_honours_no_proxy(proxy_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", ".example.local")
    session, endpoint = _real_session(PROXY_ENV, True)
    assert _proxy_for(session, endpoint) is None


class _Recorder(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append(self.path)  # type: ignore[attr-defined]
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


def _http_server() -> http.server.HTTPServer:
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    srv.seen = []  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("proxy", [PROXY_NONE, PROXY_ENV])
def test_real_pypsrp_over_the_network(monkeypatch: pytest.MonkeyPatch, proxy: str) -> None:
    """With the real pypsrp: the DHCP server gets the request directly, and the proxy only with "env"."""
    pytest.importorskip("pypsrp")
    target, fake_proxy = _http_server(), _http_server()
    for var in NO_PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    for var in PROXY_VARS:
        monkeypatch.setenv(var, f"http://127.0.0.1:{fake_proxy.server_address[1]}")
    try:
        runner = WinRMRunner(
            WinRMConfig(
                "127.0.0.1",
                "svc",
                "pw",
                port=target.server_address[1],
                https=False,
                auth="basic",
                timeout=5,
                proxy=proxy,
            )
        )
        with pytest.raises(RunnerError):
            runner.run("# x", {})
        if proxy == PROXY_NONE:
            assert target.seen == ["/wsman"] and fake_proxy.seen == []
        else:
            # A proxy receives the full URL of the server it is asked to reach.
            assert target.seen == [] and fake_proxy.seen == [f"http://127.0.0.1:{target.server_address[1]}/wsman"]
    finally:
        for srv in (target, fake_proxy):
            srv.shutdown()
            srv.server_close()


def test_a_proxy_url_never_appears_in_the_runner_error() -> None:
    with pytest.raises(ValueError) as ctx:
        WinRMRunner(cfg(proxy="http://svc:S3cret-pw@proxy.example.com:3128"))
    assert "S3cret-pw" not in str(ctx.value) and "proxy.example.com" not in str(ctx.value)
    assert "a proxy URL is not accepted" in str(ctx.value)
