# Development

Requires [uv](https://docs.astral.sh/uv/) and Docker (Compose 2.24 or later, for the optional `dev/env/lab.env`).
PowerShell (`pwsh`) is optional: with it, the unit tests also run the real scripts against stub cmdlets.

```bash
git clone https://github.com/thomaschristory/netbox-windows-dhcp-plugin
cd netbox-windows-dhcp-plugin
uv sync
```

## Make targets

| Target | Does |
|---|---|
| `make lint` | `ruff check` and `ruff format --check` (line length 120) |
| `make format` | Fix what ruff can and format |
| `make test` | Unit tests (`tests/`): the Windows layer, the diff engine, the docs. No NetBox needed |
| `make dev` | Start the dev stack: NetBox 4.7 with netbox-branching and netbox-change-control, at http://localhost:8000 |
| `make test-netbox` | NetBox integration tests (`tests_netbox/`) inside the dev stack |
| `make restart` | Restart NetBox and the worker after Python changes (the source is mounted into the containers) |
| `make logs` | Follow the NetBox log |
| `make down` | Stop the dev stack |
| `make dist` | Build the wheel and sdist and check them |
| `make docs` | Build this site into `site/` with Zensical (strict) |
| `make docs-serve` | Serve the site with live reload |
| `make test-lab` | Opt-in lab tests against a real Windows server, see [below](#lab-tests) |
| `make lab-e2e` | Opt-in end-to-end run on the dev stack against that server |
| `make lab-seed`, `make lab-wipe` | Opt-in: fill two lab servers with a configuration to import, or remove it, see [below](#seed-data-to-import) |

The dev stack runs in branching mode. Its first start migrates a fresh database and takes several minutes. Log in as
`admin` with the `SUPERUSER_PASSWORD` from `dev/env/netbox.env`.

## Code layout

```
netbox_windows_dhcp_plugin/
  windows/      pure Python: PowerShell scripts, transports (WinRM, SSH), the client (one function per cmdlet),
                normalisation, and an in-memory fake server for tests. No Django imports
  sync/         diff engine, Windows and NetBox records, push planning and writing, import
  models/ api/ graphql/ views.py forms.py ...   the NetBox side
  connection.py builds a client from a Windows server and its credential profile
  jobs.py       every job; all Windows I/O happens here
  integrations/branching.py   the only module that imports netbox-branching and netbox-change-control
```

The [design](DESIGN.md) explains the decisions; the [roadmap](ROADMAP.md) what is done.

Rules worth knowing before a change:

- Values reach PowerShell only through the `$p` JSON literal built by `windows/psscript.py`; never format a value
  into a script.
- Never log or store credentials.
- A new cmdlet gets an implementation in `windows/fake.py`, a JSON fixture under `tests/windows/testdata/` and a
  `pwsh` stub test; mark it `# unverified:` until it has run against a real server.
- Model changes need a migration and refreshed query-count baselines in both standalone and branching modes.
- Update `CHANGELOG.md` under Unreleased with every user-visible change, and these pages when behaviour changes.

## Testing tiers

1. **Unit** (`make test`): script rendering and envelope parsing, normalisation, the client against recorded fixtures
   and the fake server, the diff engine, and checks on these docs.
2. **PowerShell** (part of `make test`, skipped without `pwsh`; the `PWSH` variable picks the binary): the real
   scripts run against stub cmdlets in `tests/windows/testdata/stubs.ps1`.
3. **NetBox** (`make test-netbox`): models, views, API, import, push and leases against the fake server. CI runs it
   in standalone and branching modes.
4. **Lab** (opt-in): a real Windows DHCP server, see below.

CI runs lint, unit tests on Python 3.12 to 3.14, the build, the docs build, and the NetBox tests in both modes.

## Lab tests

The lab tier checks the client and the whole sync against a real Windows DHCP server. CI never runs it.

!!! danger "Use a lab server"
    The tests only create objects named `nbwd-test-...` in `198.18.0.0/15` and `2001:db8:4e42::/64` (MAC addresses
    `02-4e-42-...`), check name and range before every delete, and clean up even when a test fails. Still, never
    point them at a production server.

### Preparing a lab VM

Any Windows Server 2016 or later VM will do; an evaluation edition on a host-only network is enough, since the tests
never serve real clients. In an elevated PowerShell on the VM, run the setup script from the repository:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\dev\scripts\lab-server-setup.ps1 -AllowFrom 192.168.56.0/24
```

It installs the DHCP role, creates a local account `nbwd-lab` in DHCP Administrators and Remote Management Users
(asking for its password), sets up WinRM over HTTPS on 5986 with a self-signed certificate, opens the firewall
(optionally only to the given network), and prints the settings for the next step. It is safe to run again.

### Running them

Put the connection settings in `dev/env/lab.env` (gitignored; the dev stack loads it too, so run `make dev` after
changing it: `make restart` does not reload it, nor install new dependencies) or the environment:

| Variable | Meaning |
|---|---|
| `WINDOWS_DHCP_LAB_HOST` | Host name or address of the lab server |
| `WINDOWS_DHCP_USERNAME`, `WINDOWS_DHCP_PASSWORD` | The account |
| `WINDOWS_DHCP_LAB_PORT` | Optional, default 5986 (5985 with `WINDOWS_DHCP_LAB_HTTPS=0`) |
| `WINDOWS_DHCP_LAB_HTTPS` | `0` uses the HTTP listener |
| `WINDOWS_DHCP_LAB_AUTH` | Optional: `ntlm` (default), `basic`, `kerberos` or `credssp` (needed by the failover tests) |
| `WINDOWS_DHCP_LAB_PARTNER` | Optional: a second server, as the first reaches it; runs the failover tests (with `credssp`) |
| `WINDOWS_DHCP_LAB_FAILOVER_SECRET` | Optional shared secret for the test relationship |
| `WINDOWS_DHCP_LAB_REALM` | Optional, Kerberos realm |
| `WINDOWS_DHCP_LAB_INSECURE` | `1` skips the certificate check (self-signed lab certificate); or keep the check and point `REQUESTS_CA_BUNDLE` at the certificate |
| `WINDOWS_DHCP_LAB_TRANSPORT` | Optional: `winrm` (default) or `ssh` (OpenSSH Server on the lab server, password authentication) |
| `WINDOWS_DHCP_LAB_SSH_HOST_KEY` | Optional with `ssh`: the server's host key (`ssh-ed25519 AAAA...`), else `~/.ssh/known_hosts` |
| `WINDOWS_DHCP_LAB_PARTNER_SSH_HOST_KEY` | Optional with `ssh`: the same for the failover partner |
| `WINDOWS_DHCP_LAB_RECORD` | `1` saves every raw PowerShell answer under `.lab/record/`, to turn into fixtures after scrubbing |

Then:

```bash
make test-lab        # the client over WinRM, one round trip per unverified cmdlet
make dev             # recreate the dev stack once, so it mounts the lab helpers
make lab-e2e         # import, push creates, updates and deletes, leases, on the dev stack
```

`make test-lab` writes property dumps to `.lab/probe/`. `make lab-e2e` prints `PASSED` at the end and exits non-zero
on failure. Both leave the server as they found it. Everything under `.lab/` can identify the lab and is ignored by
git: scrub host names and addresses before turning a recording into a fixture.

### Seed data to import

To try the import on a realistic configuration, `make lab-seed` fills the two lab servers (`WINDOWS_DHCP_LAB_HOST`
and `WINDOWS_DHCP_LAB_PARTNER`, with `WINDOWS_DHCP_LAB_AUTH=credssp` and `WINDOWS_DHCP_LAB_FAILOVER_SECRET`) with
every kind the plugin imports:

| Where | What |
|---|---|
| First server | Server options (6, 15, 42 and custom 200) and a server policy on a user class; MAC filters (deny list on) |
| Both servers | User classes `Printers` and `Lab-Kiosks`, vendor class `Polycom` with a vendor option definition, option definition 200 |
| `VLAN10-Users`, `VLAN10-Users-B` | Superscope `Campus-VLAN10`, exclusion, reservations; failover `campus-lb` (load balance) |
| `VLAN20-Voice` | Vendor class option, scope policy `Polycom-Phones` with its own range and option; failover `voice-hs` |
| `VLAN30-Printers` | DHCP and BOOTP, user class option, reservations of each type, a reservation option; failover `voice-hs` (hot standby, first server active) |
| `VLAN40-Guests`, `VLAN50-Servers` | An inactive scope; a scope of server reservations |
| `v6-Users` | DHCPv6 scope `2001:db8:4e43::/64` with options and a DUID reservation |
| Second server only | Server option 15 and scope `VLAN90-Lab` |

Then add both servers in NetBox and import the first one, then the second. `make lab-wipe` removes what the seed
created and nothing else; seeding again needs a wipe first. The seed stays out of the lab tests' ranges, so
`make test-lab` and `make lab-e2e` can run next to it.

## Documentation

These pages live in `docs/` and are built with [Zensical](https://zensical.org/) from `mkdocs.yml`:

```bash
make docs-serve      # http://localhost:8001, reloads on save
make docs            # strict build into site/
```

`make test` checks the docs too: no em dashes, the configuration table matches the plugin's defaults, every page is
in the navigation, and the `PLUGINS_CONFIG` examples are valid. The site is published to GitHub Pages when a release
is tagged.

## Releasing

1. Update `netbox_windows_dhcp_plugin/version.py` and move the Unreleased section of `CHANGELOG.md` under the new
   version and date.
2. `make dist`, merge to main.
3. Tag `vX.Y.Z` on main and push the tag. The release workflow checks the tag is on main, builds, and publishes to
   PyPI through trusted publishing (the `pypi` environment only accepts `v*` tags, with no manual approval); the docs
   workflow publishes the site.

`main` is the 0.2 line. The `release/0.1` branch keeps bug fixes, and no new features, for those who stay on 0.1.x
with netbox-plugin-dhcp: fixes reach it through pull requests into `release/0.1`, and a `v0.1.Z` tag there publishes
to PyPI without touching the documentation site, which describes main.
