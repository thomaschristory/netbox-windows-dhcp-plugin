# netbox-windows-dhcp-plugin

A NetBox plugin that keeps Microsoft Windows DHCP servers in step with the data modelled by
[netbox-plugin-dhcp](https://github.com/sys4/netbox-plugin-dhcp), over WinRM or SSH.

> **Status: pre-release.** Every v1 feature is built and tested against an in-memory fake server and PowerShell
> stubs. Some cmdlets the plugin uses have not been checked against a real Windows server yet; they are marked
> `# unverified:` in the code, and the lab tests that settle them are waiting for a lab server (see
> [docs/ROADMAP.md](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/blob/main/docs/ROADMAP.md)). The
> design is in [docs/DESIGN.md](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/blob/main/docs/DESIGN.md).

## Features

- Connection settings per DHCP server: WinRM (NTLM, Basic, Kerberos; HTTPS by default) or SSH, as in
  [terraform-provider-windowsddi](https://github.com/thomaschristory/terraform-provider-windowsddi). Credentials
  live in named profiles in the plugin configuration, never in the database.
- Windows concepts that netbox-plugin-dhcp does not model: scope settings, reservation type, exclusion ranges,
  user and vendor classes, option definitions and qualifiers, vendor class options, policies, superscopes,
  failover relationships and MAC filters.
- IPv4 and DHCPv6: scopes, reservations (with IAID for DHCPv6), user classes and option values. DHCPv6 custom
  option definitions, vendor classes and exclusions are read but left alone.
- **Import** from Windows, on demand ("Import now" button and API action) or every `import_interval` minutes. With
  netbox-branching each import lands in a branch named `dhcp-import/<server>/<timestamp>` (plus a change request
  when change control is installed); without it, in an import proposal where each difference is applied or
  dismissed.
- **Push** to Windows: a reconcile per server when a branch is merged or reverted, or a few seconds after a save
  without branching. "Sync now" and "Dry run" buttons and API actions. A push only deletes Windows objects it
  created or imported itself; objects made by hand on Windows are left alone until imported.
- Failover: after pushing to a primary server, the changed scopes are replicated to the partner.
- **Leases**: a Leases tab on the netbox-plugin-dhcp subnet page reads the live leases of the scope in a background
  job. Leases are not stored.
- "Test connection" button and API action.
- Full REST API and GraphQL for every model.
- Works with [netbox-branching](https://github.com/netboxlabs/netbox-branching) and
  [netbox-change-control](https://github.com/antoinekh/netbox-change-control) when they are installed.

## Compatibility

| Plugin | NetBox | netbox-plugin-dhcp | netbox-branching | Python |
|---|---|---|---|---|
| 0.1.x | 4.7 | 0.2.x | 1.2.2+ (optional) | 3.12+ |

## Installation

```bash
pip install netbox-windows-dhcp-plugin
# Kerberos support needs system GSSAPI libraries:
pip install "netbox-windows-dhcp-plugin[kerberos]"
```

Enable it after `netbox_dhcp` in `configuration.py`:

```python
PLUGINS = ["netbox_dhcp", "netbox_windows_dhcp_plugin"]
```

Then run `python manage.py migrate`.

### With netbox-branching and change control

Install the extra and keep `netbox_branching` last in `PLUGINS`, as branching requires:

```bash
pip install "netbox-windows-dhcp-plugin[branching]"
```

```python
from netbox_branching.utilities import DynamicSchemaDict

PLUGINS = ["netbox_dhcp", "netbox_windows_dhcp_plugin", "netbox_change_control", "netbox_branching"]
DATABASES = DynamicSchemaDict({"default": {...}})
DATABASE_ROUTERS = ["netbox_branching.database.BranchAwareRouter"]
```

Connection settings, sync state and import proposals are not branched; DHCP configuration objects are.

## Configuration

Credentials are never stored in the database. Each Windows server names a credential profile, defined in
`PLUGINS_CONFIG`. Every value can be given inline, from an environment variable (`<key>_env`) or from a file
(`<key>_file`).

```python
PLUGINS_CONFIG = {
    "netbox_windows_dhcp_plugin": {
        "credentials": {
            "dhcp-admin": {
                "username": "CORP\\svc-dhcp",
                "password_env": "DHCP_ADMIN_PASSWORD",
                "allowed_hosts": ["dhcp*.corp.example"],
            },
            "ssh-dhcp": {
                "username": "svc-dhcp",
                "private_key_file": "/run/secrets/dhcp_key",
                "allowed_hosts": ["10.0.0.5"],
            },
        },
    },
}
```

Set `allowed_hosts` on every profile. It lists the host names or IP addresses the profile's credentials may be sent
to; `*` and `?` match within one dot-separated label, as in TLS certificates. It applies to a server's
`computer_name` as well. Without it, anyone who can edit a Windows server in NetBox can point it at a machine they
control and capture the credentials; NetBox logs a warning at startup for each such profile.

| Setting | Default | Meaning |
|---|---|---|
| `credentials` | `{}` | Credential profiles, see above |
| `subnet_name_template` | `{server}/{scope_id}` | Name of imported subnets (sys4 names are unique across servers) |
| `reservation_name_template` | `{server}/{scope_id}/{ip_address}` | Name of imported reservations |
| `import_branch_prefix` | `dhcp-import` | Prefix of import branches |
| `import_change_request` | `True` | Open a change request per import branch when change control is installed |
| `import_user` | `None` | Username scheduled imports run as (required for scheduled imports with branching) |
| `push_debounce` | `10` | Seconds to group saves into one push (without branching) |
| `max_concurrency` | `4` | PowerShell commands one job runs at once against a server |
| `command_timeout` | `120` | Seconds before a PowerShell command is abandoned |

Each Windows server also has `sync_enabled` (push NetBox changes to it, on by default) and `import_interval`
(minutes between scheduled imports, none when empty).

### Failover shared secret

A failover relationship with message authentication names a credential profile in `shared_secret_profile`. The
secret is read from that profile's `secret` key when a push needs it and goes straight to both servers, so the
profile's `allowed_hosts` must cover both partners:

```python
"failover-secret": {"secret_env": "DHCP_FAILOVER_SECRET", "allowed_hosts": ["dhcp1.corp.example", "dhcp2.corp.example"]},
```

### Workers

Every Windows command runs in a NetBox background job, never in a web request. The credential profiles, their
environment variables and files must therefore be available to `netbox-worker` (`rqworker`) as well as to the web
process. With branching, pushes start from the merge job in the worker. Only one import or push runs per server at a
time, across all workers.

### Windows side

The account needs membership of the `DHCP Administrators` group and WinRM (or OpenSSH server) enabled.
Basic authentication is only allowed over HTTPS with certificate verification on.

## Permissions

Each model has the usual NetBox `view`, `add`, `change` and `delete` permissions. In addition:

- Test connection, Import now, Sync now, Dry run and loading leases need `change` on the Windows server (object
  permissions can limit which servers). Seeing the Leases tab needs `view` on it.
- With branching, starting an import also needs `netbox_branching.add_branch`, and
  `netbox_change_control.add_changerequest` when change control opens a change request.
- Applying an import proposal item needs `change` on the item, plus the permission for the NetBox object it writes
  (for example `netbox_dhcp.add_subnet`). Items the user may not apply are reported, not applied.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
make lint        # ruff check and format check
make test        # Windows layer unit tests, no NetBox needed
make dev         # NetBox, netbox-plugin-dhcp, branching and change control in docker
make test-netbox # NetBox integration tests inside the dev stack
make dist        # build and check the wheel and sdist
make test-lab    # opt-in: the client against a real Windows DHCP server
make lab-e2e     # opt-in: import, push and leases on the dev stack against that server
```

The lab targets read `WINDOWS_DHCP_LAB_HOST`, `WINDOWS_DHCP_USERNAME` and `WINDOWS_DHCP_PASSWORD` (plus the optional
variables listed in `tests/lab/conftest.py`) from the environment or the uncommitted `dev/env/netbox.env`. They only
create objects named `nbwd-test-...` in `198.18.0.0/15` and `2001:db8:4e42::/64` and remove them afterwards, but use a
lab server, never a production one. CI does not run them.

The PowerShell tests in `tests/windows` run the real scripts in `pwsh` against stub cmdlets when `pwsh` is on the
path, and are skipped otherwise.

## License

Apache-2.0, see [LICENSE](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/blob/main/LICENSE).
