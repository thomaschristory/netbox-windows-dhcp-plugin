"""Lab helpers shared by the pytest lab tier and dev/scripts/lab_e2e.py. No pytest or Django imports.

See tests/lab/conftest.py for the variables and the safety rules.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from netbox_windows_dhcp_plugin.windows import client as wc
from netbox_windows_dhcp_plugin.windows.psscript import Script
from netbox_windows_dhcp_plugin.windows.winrm import WinRMConfig, WinRMRunner

REPO = Path(__file__).resolve().parents[2]
# Older checkouts kept the lab settings in netbox.env, which is tracked: lab.env (gitignored) wins.
ENV_FILES = (REPO / "dev" / "env" / "netbox.env", REPO / "dev" / "env" / "lab.env")

PREFIX = "nbwd-test-"
LAB_V4 = ipaddress.ip_network("198.18.0.0/15")
LAB_V6 = ipaddress.ip_network("2001:db8:4e42::/64")
V6_PREFIX = "2001:db8:4e42::"
MAC_PREFIX = "02-4e-42-"
# Site-specific option codes (RFC 2132 224-254). A test takes one that the server does not define yet.
FREE_OPTION_IDS = range(240, 255)


def _load_env_file() -> dict[str, str]:
    """WINDOWS_DHCP_* keys from dev/env/netbox.env and dev/env/lab.env (KEY=VALUE lines; no shell expansion)."""
    out: dict[str, str] = {}
    lines = [line for f in ENV_FILES if f.is_file() for line in f.read_text().splitlines()]
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("WINDOWS_DHCP_"):
            out[key] = value.strip().strip('"').strip("'")
    return out


def lab_env() -> dict[str, str]:
    env = _load_env_file()
    env.update({k: v for k, v in os.environ.items() if k.startswith("WINDOWS_DHCP_")})
    return env


def skip_reason(env: dict[str, str]) -> str:
    if os.environ.get("WINDOWS_DHCP_LAB") != "1":
        return "lab tests are opt-in: run make test-lab (sets WINDOWS_DHCP_LAB=1)"
    missing = [k for k in ("WINDOWS_DHCP_LAB_HOST", "WINDOWS_DHCP_USERNAME", "WINDOWS_DHCP_PASSWORD") if not env.get(k)]
    if missing:
        return "lab variables not set: " + ", ".join(missing)
    return ""


# -- recording -------------------------------------------------------------------------------------------------------


class RecordingRunner:
    """Wraps a runner and saves every raw envelope, numbered, for turning into fixtures (after scrubbing)."""

    def __init__(self, inner: Any, directory: Path) -> None:
        self.inner = inner
        self.transport = getattr(inner, "transport", "winrm")
        self.directory = directory
        self.n = 0
        directory.mkdir(parents=True, exist_ok=True)

    def run(self, script: str, params: Any = None) -> bytes:
        out = self.inner.run(script, params)
        self.n += 1
        from netbox_windows_dhcp_plugin.windows.psscript import op_of

        name = f"{self.n:04d}-{op_of(script) or 'script'}.json"
        (self.directory / name).write_bytes(out)
        return out

    def close(self) -> None:
        self.inner.close()


# -- safety ----------------------------------------------------------------------------------------------------------


class UnsafeError(AssertionError):
    """A lab test was about to touch something it did not create."""


def in_v4(addr: str) -> bool:
    return bool(addr) and ipaddress.ip_address(addr) in LAB_V4


def in_v6(addr: str) -> bool:
    return bool(addr) and ipaddress.ip_address(addr) in LAB_V6


def ours(name: str) -> bool:
    return bool(name) and name.startswith(PREFIX)


class Lab:
    """The lab client plus guarded helpers. Every remove goes through a ``remove_*`` method here."""

    def __init__(self, client: wc.DhcpClient) -> None:
        self.client = client
        self._cleanups: list[tuple[str, Callable[[], None]]] = []
        self._next_v4 = 0

    # Raw PowerShell, for probing property names. The script text is a constant in the test; values still go
    # through $p only.
    def raw(self, op: str, body: str, params: dict[str, Any] | None = None) -> Any:
        present, data = self.client._run(Script(op, body), params)
        return data if present else None

    def dump(self, op: str, pipeline: str, params: dict[str, Any] | None = None) -> list[dict[str, str]]:
        """Every property of the objects ``pipeline`` emits, as {name: "value"} plus {name#type: "TypeName"}."""
        body = (
            "$out = @("
            + pipeline
            + r""" | ForEach-Object {
    $o = [ordered]@{}
    foreach ($pp in $_.PSObject.Properties) {
        if ($pp.Name -like 'Cim*' -or $pp.Name -eq 'PSComputerName') { continue }
        $o[$pp.Name] = "$($pp.Value)"
        $o[$pp.Name + '#type'] = if ($null -eq $pp.Value) { 'null' } else { $pp.Value.GetType().FullName }
    }
    $o
})"""
        )
        data = self.raw(op, body, params)
        if data is None:
            return []
        return [data] if isinstance(data, dict) else list(data)

    # -- cleanup registry --------------------------------------------------------------------------------------------

    def on_cleanup(self, label: str, fn: Callable[[], None]) -> None:
        self._cleanups.append((label, fn))

    def run_cleanups(self) -> list[str]:
        """Undo in reverse order. What fails is tried once more at the end: Windows may refuse to remove a class or
        option definition while a scope still holds values using it, and the scope goes last."""
        failed: list[tuple[str, Callable[[], None]]] = []
        while self._cleanups:
            label, fn = self._cleanups.pop()
            if not self._attempt(fn):
                failed.append((label, fn))
        errors = []
        for label, fn in failed:
            try:
                fn()
            except wc.NotFound:
                pass
            except Exception as exc:  # noqa: BLE001 - keep cleaning up, report at the end
                errors.append(f"{label}: {exc}")
        return errors

    @staticmethod
    def _attempt(fn: Callable[[], None]) -> bool:
        try:
            fn()
        except wc.NotFound:
            pass
        except Exception:  # noqa: BLE001 - retried once by run_cleanups
            return False
        return True

    # -- allocation --------------------------------------------------------------------------------------------------

    def new_v4_subnet(self) -> str:
        """A /24 in 198.19.0.0/16 that has no scope on the server. Returns the network address."""
        existing = {s.scope_id for s in self.client.list_scopes()}
        while self._next_v4 < 256:
            net = f"198.19.{self._next_v4}.0"
            self._next_v4 += 1
            if net not in existing:
                return net
        raise RuntimeError("no free /24 left in 198.19.0.0/16")

    def free_option_id(self, vendor_class: str = "") -> int:
        used = {d.option_id for d in self.client.list_option_definitions() if d.vendor_class == vendor_class}
        for i in FREE_OPTION_IDS:
            if i not in used:
                return i
        raise RuntimeError("no free option code in 240-254")

    # -- create with cleanup -----------------------------------------------------------------------------------------

    def scope(self, name: str, **kw: Any) -> wc.Scope:
        assert ours(name), name
        net = self.new_v4_subnet()
        base = net.rsplit(".", 1)[0]
        s = wc.Scope(
            name=name,
            start_range=f"{base}.10",
            end_range=f"{base}.200",
            subnet_mask="255.255.255.0",
            scope_id=net,
            **kw,
        )
        self.on_cleanup(f"scope {net}", lambda: self.remove_scope(net))
        return self.client.add_scope(s)

    def v6_scope(self, name: str, **kw: Any) -> wc.ScopeV6:
        assert ours(name), name
        if any(s.prefix == V6_PREFIX for s in self.client.list_v6_scopes()):
            # Left by an earlier run (the sweep failed) or someone else's: remove_v6_scope refuses the latter.
            self.remove_v6_scope(V6_PREFIX)
        self.on_cleanup(f"v6 scope {V6_PREFIX}", lambda: self.remove_v6_scope(V6_PREFIX))
        return self.client.add_v6_scope(wc.ScopeV6(prefix=V6_PREFIX, name=name, **kw))

    def user_class(self, name: str, data: str, description: str = "", v6: bool = False) -> wc.DhcpClass:
        assert ours(name), name
        c = wc.DhcpClass(name=name, type="User", data=data, description=description)
        if v6:
            self.on_cleanup(f"v6 class {name}", lambda: self.remove_v6_class(name, "User"))
            return self.client.add_v6_class(c)
        self.on_cleanup(f"class {name}", lambda: self.remove_class(name, "User"))
        return self.client.add_class(c)

    def vendor_class(self, name: str, data: str) -> wc.DhcpClass:
        assert ours(name), name
        self.on_cleanup(f"class {name}", lambda: self.remove_class(name, "Vendor"))
        return self.client.add_class(wc.DhcpClass(name=name, type="Vendor", data=data))

    def option_definition(self, d: wc.OptionDefinition) -> wc.OptionDefinition:
        assert ours(d.name), d.name
        oid, vc = d.option_id, d.vendor_class
        self.on_cleanup(f"option definition {oid}/{vc}", lambda: self.remove_option_definition(oid, vc))
        return self.client.add_option_definition(d)

    def policy(self, p: wc.Policy) -> wc.Policy:
        assert ours(p.name), p.name
        name, sid = p.name, p.scope_id
        self.on_cleanup(f"policy {sid}/{name}", lambda: self.remove_policy(name, sid))
        return self.client.add_policy(p)

    def superscope(self, s: wc.Superscope) -> wc.Superscope:
        assert ours(s.name), s.name
        name = s.name
        self.on_cleanup(f"superscope {name}", lambda: self.remove_superscope(name))
        return self.client.add_superscope(s)

    def mac_filter(self, f: wc.MacFilter) -> wc.MacFilter:
        assert f.mac_address.lower().startswith(MAC_PREFIX), f.mac_address
        mac = f.mac_address
        self.on_cleanup(f"filter {mac}", lambda: self.remove_filter(mac))
        return self.client.add_filter(f)

    def failover(self, f: wc.FailoverRelationship, shared_secret: str = "") -> wc.FailoverRelationship:
        assert ours(f.name) and all(in_v4(sid) for sid in f.scope_ids), f
        self.on_cleanup(f"failover {f.name}", lambda: self.remove_failover(f.name))
        return self.client.add_failover(f, shared_secret)

    # -- guarded removes ---------------------------------------------------------------------------------------------

    def remove_scope(self, scope_id: str) -> None:
        s = self.client.get_scope(scope_id)
        if not (in_v4(s.scope_id) and ours(s.name)):
            raise UnsafeError(f"refusing to remove scope {s.scope_id} ({s.name!r})")
        # Lab scopes never hold real leases (the range is not routed), so force only clears test leases.
        self.client.remove_scope(scope_id, force=True)

    def remove_v6_scope(self, prefix: str) -> None:
        s = self.client.get_v6_scope(prefix)
        if not (in_v6(s.prefix) and ours(s.name)):
            raise UnsafeError(f"refusing to remove DHCPv6 scope {s.prefix} ({s.name!r})")
        self.client.remove_v6_scope(prefix, force=True)

    def remove_class(self, name: str, type_: str) -> None:
        c = self.client.get_class(name, type_)
        if not ours(c.name):
            raise UnsafeError(f"refusing to remove class {c.name!r}")
        self.client.remove_class(name, type_)

    def remove_v6_class(self, name: str, type_: str) -> None:
        if not any(c.name == name for c in self.client.list_v6_classes()):
            raise wc.NotFound(name)
        if not ours(name):
            raise UnsafeError(f"refusing to remove DHCPv6 class {name!r}")
        self.client.remove_v6_class(name, type_)

    def remove_option_definition(self, option_id: int, vendor_class: str = "") -> None:
        d = self.client.get_option_definition(option_id, vendor_class)
        if not ours(d.name):
            raise UnsafeError(f"refusing to remove option definition {option_id} ({d.name!r})")
        self.client.remove_option_definition(option_id, vendor_class)

    def remove_policy(self, name: str, scope_id: str = "") -> None:
        p = self.client.get_policy(name, scope_id)
        if not ours(p.name) or (scope_id and not in_v4(scope_id)):
            raise UnsafeError(f"refusing to remove policy {scope_id}/{p.name!r}")
        self.client.remove_policy(name, scope_id)

    def remove_superscope(self, name: str) -> None:
        s = self.client.get_superscope(name)
        if not ours(s.name) or not all(in_v4(sid) for sid in s.scope_ids):
            raise UnsafeError(f"refusing to remove superscope {s.name!r}")
        self.client.remove_superscope(name)

    def remove_failover(self, name: str) -> None:
        """Removing a relationship also deletes its scopes on the partner (they stay here)."""
        f = self.client.get_failover(name)
        if not ours(f.name) or not all(in_v4(sid) for sid in f.scope_ids):
            raise UnsafeError(f"refusing to remove failover relationship {f.name!r}")
        self.client.remove_failover(name)

    def remove_filter(self, mac: str) -> None:
        f = self.client.get_filter(mac)
        if not f.mac_address.lower().startswith(MAC_PREFIX):
            raise UnsafeError(f"refusing to remove MAC filter {f.mac_address}")
        self.client.remove_filter(mac)

    # -- sweep -------------------------------------------------------------------------------------------------------

    def sweep(self) -> list[str]:
        """Remove what earlier runs left behind: only ``nbwd-test-`` objects inside the lab ranges."""
        c = self.client
        errors: list[str] = []

        def attempt(label: str, fn: Callable[[], None]) -> None:
            try:
                fn()
            except wc.NotFound:
                pass
            except Exception as exc:  # noqa: BLE001
                errors.append(f"sweep {label}: {exc}")

        for f in c.list_failovers():
            if ours(f.name):
                attempt(f"failover {f.name}", lambda n=f.name: self.remove_failover(n))
        for s in c.list_superscopes():
            if ours(s.name):
                attempt(f"superscope {s.name}", lambda n=s.name: self.remove_superscope(n))
        for p in c.list_policies():
            if ours(p.name):
                attempt(f"policy {p.name}", lambda n=p.name: self.remove_policy(n))
        for s in c.list_scopes():
            if in_v4(s.scope_id) and ours(s.name):
                attempt(f"scope {s.scope_id}", lambda sid=s.scope_id: self.remove_scope(sid))
        for s in c.list_v6_scopes():
            if in_v6(s.prefix) and ours(s.name):
                attempt(f"v6 scope {s.prefix}", lambda px=s.prefix: self.remove_v6_scope(px))
        for d in c.list_option_definitions():
            if ours(d.name):
                attempt(
                    f"option definition {d.option_id}",
                    lambda i=d.option_id, v=d.vendor_class: self.remove_option_definition(i, v),
                )
        for cl in c.list_classes():
            if ours(cl.name):
                attempt(f"class {cl.name}", lambda n=cl.name, t=cl.type: self.remove_class(n, t))
        for cl in c.list_v6_classes():
            if ours(cl.name):
                attempt(f"v6 class {cl.name}", lambda n=cl.name, t=cl.type: self.remove_v6_class(n, t))
        for f in c.list_filters():
            if f.mac_address.lower().startswith(MAC_PREFIX):
                attempt(f"filter {f.mac_address}", lambda m=f.mac_address: self.remove_filter(m))
        return errors


def make_runner(env: dict[str, str]) -> Any:
    cfg = WinRMConfig(
        host=env["WINDOWS_DHCP_LAB_HOST"],
        username=env["WINDOWS_DHCP_USERNAME"],
        password=env["WINDOWS_DHCP_PASSWORD"],
        port=int(env["WINDOWS_DHCP_LAB_PORT"]) if env.get("WINDOWS_DHCP_LAB_PORT") else None,
        auth=env.get("WINDOWS_DHCP_LAB_AUTH", "ntlm"),
        # Some labs only have the HTTP listener (5985). NTLM still encrypts the WinRM messages there.
        https=env.get("WINDOWS_DHCP_LAB_HTTPS", "1") != "0",
        kerberos_realm=env.get("WINDOWS_DHCP_LAB_REALM", ""),
        cert_validation=env.get("WINDOWS_DHCP_LAB_INSECURE") != "1",
        timeout=120,
    )
    runner: Any = WinRMRunner(cfg)
    if env.get("WINDOWS_DHCP_LAB_RECORD") == "1":
        # Raw server data: kept under the gitignored .lab/ only, and scrubbed by hand before becoming a fixture.
        runner = RecordingRunner(runner, REPO / ".lab" / "record")
    return runner
