"""
Seed the two lab Windows DHCP servers with a realistic configuration to import into NetBox, or remove it again.

    make lab-seed     # uv run python dev/scripts/lab_seed.py seed
    make lab-wipe     # uv run python dev/scripts/lab_seed.py wipe

The seed covers every kind the plugin imports: server options and a server policy, user and vendor classes, custom
and vendor option definitions, a superscope of two scopes, exclusions, reservations of each type with options, user
class, vendor class and policy option values, an inactive scope, MAC filters, a DHCPv6 scope with a reservation, and
two failover relationships (load balance, and hot standby with the first server active). The second server gets the
failover scopes from the first, plus one scope of its own. The scripts are in dev/scripts/lab-seed/.

Settings come from dev/env/lab.env, as for make test-lab: WINDOWS_DHCP_LAB_HOST (first server),
WINDOWS_DHCP_LAB_PARTNER (second server, as the first one reaches it), WINDOWS_DHCP_LAB_AUTH=credssp (failover writes
need it) and WINDOWS_DHCP_LAB_FAILOVER_SECRET. Values reach PowerShell only through $p, like the plugin's own scripts.

Use lab servers only. It writes in 198.18.0.0/15 outside 198.19.0.0/16 and in 2001:db8:4e43::/64, so it does not
meet the lab tests' objects (198.19.0.0/16, 2001:db8:4e42::/64). Seeding refuses when the seed's scopes already exist;
wipe removes only what the seed creates.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from netbox_windows_dhcp_plugin.windows import client as wc  # noqa: E402
from netbox_windows_dhcp_plugin.windows.psscript import PSError, Script  # noqa: E402
from tests.lab.labkit import lab_env, make_runner  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent / "lab-seed"


def run(env: dict[str, str], host: str, name: str, params: dict[str, str] | None = None) -> None:
    """Run lab-seed/<name>.ps1 on host and print what it did."""
    print(f"{name} on {host}")
    client = wc.DhcpClient(make_runner(dict(env, WINDOWS_DHCP_LAB_HOST=host)))
    _, done = client._run(Script(f"lab-seed.{name}", (SCRIPTS / f"{name}.ps1").read_text()), params)
    for line in done or ["nothing to do"]:
        print(f"  {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the lab DHCP servers, or remove the seed.")
    parser.add_argument("action", choices=("seed", "wipe"))
    action = parser.parse_args().action

    env = lab_env()
    needed = ["WINDOWS_DHCP_LAB_HOST", "WINDOWS_DHCP_LAB_PARTNER", "WINDOWS_DHCP_USERNAME", "WINDOWS_DHCP_PASSWORD"]
    if action == "seed":
        needed.append("WINDOWS_DHCP_LAB_FAILOVER_SECRET")
    missing = [k for k in needed if not env.get(k)]
    if missing:
        print("missing in dev/env/lab.env: " + ", ".join(missing), file=sys.stderr)
        return 2
    if env.get("WINDOWS_DHCP_LAB_AUTH", "").lower() != "credssp":
        print("failover writes need WINDOWS_DHCP_LAB_AUTH=credssp", file=sys.stderr)
        return 2
    primary, partner = env["WINDOWS_DHCP_LAB_HOST"], env["WINDOWS_DHCP_LAB_PARTNER"]

    try:
        if action == "seed":
            for host in (primary, partner):
                run(env, host, "common")
            run(env, partner, "partner")
            run(
                env,
                primary,
                "primary",
                {"partner": partner, "shared_secret": env["WINDOWS_DHCP_LAB_FAILOVER_SECRET"]},
            )
        else:
            # The first server first: removing a relationship there also removes it and its scopes on the partner.
            for host in (primary, partner):
                run(env, host, "wipe")
    except PSError as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
