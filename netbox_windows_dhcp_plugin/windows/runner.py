"""Runner protocol, concurrency limiter and the shared execute path.

Port of ``internal/runner/runner.go``. A runner takes a wrapped script plus its
parameters, runs it on the Windows host and returns the decoded JSON envelope bytes.
Transports (``winrm.py``, ``ssh.py``) only provide an ``exec`` function; rendering,
encoding, exit code handling and stderr cleaning live here so they behave the same.

This module must not import Django or NetBox.
"""

from __future__ import annotations

import html
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from . import psscript

__all__ = [
    "DEFAULT_MAX_CONCURRENCY",
    "ExecResult",
    "Limited",
    "Runner",
    "RunnerError",
    "clean_stderr",
    "execute",
    "limit",
]

DEFAULT_MAX_CONCURRENCY = 4


@runtime_checkable
class Runner(Protocol):
    """Runs one wrapped script and returns the decoded envelope.

    Implementations must be safe to call from several threads at once.
    """

    def run(self, script: str, params: Mapping[str, Any] | None = None) -> bytes: ...


class RunnerError(psscript.WindowsLayerError):
    """The script could not be run: connection, authentication or a crashed powershell.exe."""


class Limited:
    """Caps the number of scripts in flight on the wrapped runner (a bounded semaphore)."""

    def __init__(self, runner: Runner, n: int = DEFAULT_MAX_CONCURRENCY, timeout: float | None = None) -> None:
        self.runner = runner
        self.n = max(1, int(n))
        self.timeout = timeout
        self._sem = threading.BoundedSemaphore(self.n)

    def run(self, script: str, params: Mapping[str, Any] | None = None) -> bytes:
        if not self._sem.acquire(timeout=self.timeout):
            raise RunnerError(f"timed out after {self.timeout}s waiting for a free connection slot")
        try:
            return self.runner.run(script, params)
        finally:
            self._sem.release()

    def close(self) -> None:
        close = getattr(self.runner, "close", None)
        if callable(close):
            close()


def limit(runner: Runner, n: int = DEFAULT_MAX_CONCURRENCY, timeout: float | None = None) -> Limited:
    return Limited(runner, n, timeout)


@dataclass
class ExecResult:
    stdout: bytes
    stderr: bytes
    exit_code: int


# exec(cmdline_argv, stdin) where argv is [executable, *args].
ExecFunc = Callable[[list[str], str], ExecResult]


def execute(transport: str, exec_fn: ExecFunc, script: str, params: Mapping[str, Any] | None) -> bytes:
    """Render, encode, run through ``exec_fn`` and decode. Shared by every transport."""
    try:
        rendered = psscript.render(script, params)
    except (TypeError, ValueError) as e:
        raise RunnerError(f"encoding script parameters: {e}") from e
    args, stdin = psscript.command_argv(rendered)
    try:
        res = exec_fn([psscript.POWERSHELL_EXE, *args], stdin)
    except RunnerError:
        raise
    except Exception as e:  # noqa: BLE001 - transport libraries raise many unrelated types
        raise RunnerError(f"{transport}: running PowerShell: {e}") from e
    if res.exit_code != 0 or not res.stdout.strip():
        raise RunnerError(
            f"{transport}: PowerShell exited with code {res.exit_code}: "
            f"{clean_stderr(res.stderr.decode('utf-8', errors='replace'))}"
        )
    try:
        return psscript.decode_output(res.stdout)
    except psscript.OutputError as e:
        raise RunnerError(f"{transport}: {e}") from e


_CLIXML_STRING = re.compile(r'<S S="Error">(.*?)</S>', re.S)


def clean_stderr(s: str) -> str:
    """Turn powershell.exe's CLIXML stderr into readable text.

    With redirected stdio, powershell.exe writes errors as ``#< CLIXML`` serialised
    objects. Extract the ``<S S="Error">`` strings, decode ``_x000D__x000A_`` line
    breaks and XML entities. Plain text is returned trimmed.
    """
    s = s.strip()
    if not s.startswith("#< CLIXML"):
        return s or "(no error output)"
    parts = []
    for m in _CLIXML_STRING.finditer(s):
        part = m.group(1).replace("_x000D__x000A_", "\n")
        parts.append(html.unescape(part))
    out = "".join(parts).strip()
    return out or "(no error output)"
