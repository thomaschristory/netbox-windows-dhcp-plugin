from __future__ import annotations

import base64
import threading
import time

import pytest

from netbox_windows_dhcp_plugin.windows import psscript
from netbox_windows_dhcp_plugin.windows.runner import (
    ExecResult,
    Limited,
    Runner,
    RunnerError,
    clean_stderr,
    execute,
    limit,
)


def _b64(s: str) -> bytes:
    return base64.b64encode(s.encode()).decode().encode()


def test_execute_success() -> None:
    seen: dict = {}

    def ex(argv: list[str], stdin: str) -> ExecResult:
        seen["argv"], seen["stdin"] = argv, stdin
        return ExecResult(stdout=_b64('{"ok":true,"data":1}') + b"\r\n", stderr=b"", exit_code=0)

    out = execute("test", ex, "# body", {"a": "b'c"})
    assert out == b'{"ok":true,"data":1}'
    assert seen["argv"][0] == "powershell.exe"
    assert seen["argv"][-2] == "-EncodedCommand"
    rendered = base64.b64decode(seen["stdin"]).decode()
    assert rendered == psscript.render("# body", {"a": "b'c"})


@pytest.mark.parametrize(
    ("res", "match"),
    [
        (ExecResult(b"", b"boom", 0), "exited with code 0: boom"),
        (ExecResult(b"  \r\n", b"", 0), r"\(no error output\)"),
        (ExecResult(_b64("{}"), b'#< CLIXML\n<Objs><S S="Error">bad &amp; worse</S></Objs>', 1), "code 1: bad & worse"),
        (ExecResult(b"Banner noise", b"", 0), "not base64"),
    ],
)
def test_execute_failures(res: ExecResult, match: str) -> None:
    with pytest.raises(RunnerError, match=match):
        execute("test", lambda a, s: res, "# body", None)


def test_execute_transport_exception_wrapped() -> None:
    def ex(argv: list[str], stdin: str) -> ExecResult:
        raise ConnectionRefusedError("refused")

    with pytest.raises(RunnerError, match="test: running PowerShell: refused"):
        execute("test", ex, "# body", None)


def test_execute_bad_params() -> None:
    with pytest.raises(RunnerError, match="encoding script parameters"):
        execute("test", lambda a, s: ExecResult(b"", b"", 0), "# body", {"x": object()})


def test_clean_stderr() -> None:
    clixml = (
        "#< CLIXML\n"
        '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
        '<S S="Error">Get-Foo : bad &amp; worse_x000D__x000A_</S><S S="Error">at line 1_x000D__x000A_</S>'
        '<S S="progress">ignored</S></Objs>'
    )
    assert clean_stderr(clixml) == "Get-Foo : bad & worse\nat line 1"
    assert clean_stderr("  plain  ") == "plain"
    assert clean_stderr("") == "(no error output)"
    assert clean_stderr("#< CLIXML\n<Objs></Objs>") == "(no error output)"


class _Counting:
    def __init__(self) -> None:
        self.cur = 0
        self.max = 0
        self.lock = threading.Lock()

    def run(self, script: str, params=None) -> bytes:
        with self.lock:
            self.cur += 1
            self.max = max(self.max, self.cur)
        time.sleep(0.01)
        with self.lock:
            self.cur -= 1
        return b"{}"


def test_limit_caps_concurrency() -> None:
    c = _Counting()
    r = limit(c, 2)
    assert isinstance(r, Runner)
    threads = [threading.Thread(target=r.run, args=("x",)) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 1 <= c.max <= 2


def test_limit_timeout_and_minimum() -> None:
    gate = threading.Event()
    started = threading.Event()

    class Blocking:
        def run(self, script: str, params=None) -> bytes:
            started.set()
            gate.wait(5)
            return b"{}"

    r = Limited(Blocking(), 0, timeout=0.05)
    assert r.n == 1
    t = threading.Thread(target=r.run, args=("x",))
    t.start()
    started.wait(5)
    with pytest.raises(RunnerError, match="timed out"):
        r.run("y")
    gate.set()
    t.join()
    assert r.run("z") == b"{}"


def test_limit_close_delegates() -> None:
    closed = []

    class C:
        def run(self, script: str, params=None) -> bytes:
            return b""

        def close(self) -> None:
            closed.append(True)

    limit(C()).close()
    assert closed == [True]
