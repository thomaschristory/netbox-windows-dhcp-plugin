# netbox-windows-dhcp-plugin

A NetBox plugin that keeps Microsoft Windows DHCP servers in step with the data modelled by
[netbox-plugin-dhcp](https://github.com/sys4/netbox-plugin-dhcp), over WinRM or SSH.

> **Status: early development.** The models, REST and GraphQL API, connection test and the PowerShell layer are in
> place. Import from Windows and push to Windows are being built next (see [docs/ROADMAP.md](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/blob/main/docs/ROADMAP.md)).
> The design is in [docs/DESIGN.md](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/blob/main/docs/DESIGN.md).

## Features

- Connection settings per DHCP server: WinRM (NTLM, Basic, Kerberos; HTTPS by default) or SSH, as in
  [terraform-provider-windowsddi](https://github.com/thomaschristory/terraform-provider-windowsddi).
- Windows concepts that netbox-plugin-dhcp does not model: scope settings, reservation type, exclusion ranges,
  user and vendor classes, option definitions and qualifiers, policies, failover relationships and MAC filters.
- A "Test connection" button and API action that runs a background job against the server.
- Full REST API and GraphQL for every model.
- Works with [netbox-branching](https://github.com/netboxlabs/netbox-branching) and
  [netbox-change-control](https://github.com/antoinekh/netbox-change-control) when they are installed.

Planned for v1: import from Windows as a new branch (or a review queue without branching), push to Windows on
branch merge (or on save without branching), dry-run, live leases, DHCPv6.

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
| `push_debounce` | `10` | Seconds to group saves into one push (without branching) |
| `max_concurrency` | `4` | PowerShell commands one job runs at once against a server |
| `command_timeout` | `120` | Seconds before a PowerShell command is abandoned |

### Windows side

The account needs membership of the `DHCP Administrators` group and WinRM (or OpenSSH server) enabled.
Basic authentication is only allowed over HTTPS with certificate verification on.

## Permissions

Each model has the usual NetBox `view`, `add`, `change` and `delete` permissions. Running a connection test needs
`change` on the Windows server.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
make lint        # ruff check and format check
make test        # Windows layer unit tests, no NetBox needed
make dev         # NetBox, netbox-plugin-dhcp, branching and change control in docker
make test-netbox # NetBox integration tests inside the dev stack
make dist        # build and check the wheel and sdist
```

The PowerShell tests in `tests/windows` run the real scripts in `pwsh` against stub cmdlets when `pwsh` is on the
path, and are skipped otherwise.

## License

Apache-2.0, see [LICENSE](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/blob/main/LICENSE).
