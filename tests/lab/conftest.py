"""Lab test tier: the real DhcpClient against a real Windows DHCP server, over the real WinRM transport.

Opt-in only. Every test here is skipped unless ``WINDOWS_DHCP_LAB=1`` is set (``make test-lab`` sets it) and the
lab connection variables are present, either in the environment or in ``dev/env/lab.env`` (gitignored;
the dev stack loads it too):

    WINDOWS_DHCP_LAB_HOST        host name or address of the DHCP server
    WINDOWS_DHCP_USERNAME        DOMAIN\\user (or user@REALM)
    WINDOWS_DHCP_PASSWORD
    WINDOWS_DHCP_LAB_PORT        optional, default 5986 (HTTPS) or 5985 (HTTP)
    WINDOWS_DHCP_LAB_HTTPS       optional, 0 uses the HTTP listener (NTLM, Kerberos or CredSSP message encryption)
    WINDOWS_DHCP_LAB_AUTH        optional, ntlm (default), basic, kerberos or credssp (failover tests)
    WINDOWS_DHCP_LAB_INSECURE    optional, 1 skips TLS certificate checks (self-signed lab certificate)
    WINDOWS_DHCP_LAB_REALM       optional, Kerberos realm
    WINDOWS_DHCP_LAB_RECORD      optional, 1 saves every raw JSON envelope under .lab/record/ (gitignored), to
                                 turn into fixtures after scrubbing

Safety rules, enforced by ``Lab`` below:

* everything a test creates is named ``nbwd-test-...`` and lives in 198.18.0.0/15 (IPv4) or 2001:db8:4e42::/64
  (DHCPv6); MAC addresses are locally administered ``02-4e-42-...``;
* every delete re-reads the object from the server and checks name and range first;
* cleanup is registered before an object is created, so it runs even when the test fails;
* a sweep at the start and end of the session removes leftovers of earlier runs, under the same checks;
* nothing server-wide changes behaviour: no unqualified server-level option values; the one server-level policy is
  disabled and only matches 02-4e-42 MACs; the MAC filter list switches are only written back with the values they
  already have.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from netbox_windows_dhcp_plugin.windows import client as wc

from .labkit import REPO, Lab, lab_env, make_runner, skip_reason


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    reason = skip_reason(lab_env())
    if not reason:
        return
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents:
            item.add_marker(pytest.mark.skip(reason=reason))


# -- fixtures --------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def lab_session() -> Iterator[Lab]:
    env = lab_env()
    client = wc.DhcpClient(make_runner(env))
    client.check_available()
    lab = Lab(client)
    leftovers = lab.sweep()
    assert not leftovers, leftovers
    yield lab
    errors = lab.run_cleanups() + lab.sweep()
    assert not errors, errors


@pytest.fixture
def lab(lab_session: Lab) -> Iterator[Lab]:
    """Per test: whatever the test created is removed when it ends, pass or fail."""
    yield lab_session
    errors = lab_session.run_cleanups()
    assert not errors, errors


@pytest.fixture(scope="session")
def probe_out() -> Path:
    """Where property dumps go (gitignored). Read them to settle property names."""
    path = REPO / ".lab" / "probe"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_probe(probe_out: Path, name: str, data: Any) -> None:
    (probe_out / f"{name}.json").write_text(json.dumps(data, indent=2, sort_keys=True))
