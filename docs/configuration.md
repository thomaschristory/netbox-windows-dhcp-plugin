# Configuration

Plugin settings go in `PLUGINS_CONFIG["netbox_windows_dhcp_plugin"]` in NetBox's `configuration.py`. Settings that
belong to one Windows server (host, transport, schedule) are fields of the [Windows server](servers.md) in NetBox.

The NetBox worker reads the same configuration as the web service, and runs every job: make sure the settings, the
environment variables and the files they name exist for both.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `credentials` | `{}` | [Credential profiles](#credential-profiles), by name |
| `subnet_name_template` | `'{server}/{scope_id}'` | Name of subnets created by imports. sys4 names are unique across all servers, so the default includes the server |
| `reservation_name_template` | `'{server}/{scope_id}/{ip_address}'` | Name of reservations created by imports |
| `import_branch_prefix` | `'dhcp-import'` | Branches created by imports are named `<prefix>/<server>/<timestamp>` |
| `import_change_request` | `True` | Open a change request for each import branch when netbox-change-control is installed |
| `import_user` | `None` | User name [scheduled imports](import.md#scheduled-imports) run as. With branching, scheduled imports fail without it |
| `push_debounce` | `10` | Seconds to wait after a save before pushing, so a burst of edits becomes one push (saves outside branching) |
| `max_concurrency` | `4` | PowerShell commands one job runs at the same time against a server |
| `command_timeout` | `120` | Seconds: the WinRM connection and operation timeout, and the longest silence allowed while an SSH command runs (SSH connects within 30 seconds) |

In the templates, `{server}` is the netbox-plugin-dhcp server name, `{scope_id}` the Windows scope ID (for example
`10.1.0.0`, or the prefix of a DHCPv6 scope) and `{ip_address}` the reserved address. Changing a template only
affects objects created afterwards.

## Credential profiles

Credentials are never stored in the database, the change log or the job logs. Each Windows server in NetBox names a
credential profile, and the profile says where the secrets come from. A profile is a dictionary of keys:

| Key | Used for |
|---|---|
| `username` | WinRM and SSH. `DOMAIN\user`, `user@REALM` or a local `user` |
| `password` | WinRM (all methods), SSH password login, or the passphrase of an encrypted SSH key |
| `private_key` | SSH key login: an Ed25519, ECDSA or RSA key in PEM or OpenSSH format |
| `secret` | The shared secret of a [failover relationship](failover.md#shared-secret) |
| `allowed_hosts` | The hosts the profile's credentials may be sent to, see [below](#allowed_hosts) |

Every value except `allowed_hosts` can be given in three ways:

- inline: `"password": "..."` (avoid it outside a lab: `configuration.py` is often readable by many people);
- from an environment variable: `"password_env": "DHCP_ADMIN_PASSWORD"`;
- from a file: `"password_file": "/run/secrets/dhcp_admin_password"`. Surrounding white space is removed, except
  for `private_key_file`, which is read as is.

The value is read each time a job connects, so a rotated password or a changed file takes effect without a restart
(an environment variable needs a restart of the process that reads it).

```python
PLUGINS_CONFIG = {
    "netbox_windows_dhcp_plugin": {
        "credentials": {
            # WinRM with a domain account, password from the environment.
            "dhcp-admin": {
                "username": "CORP\\svc-netbox-dhcp",
                "password_env": "DHCP_ADMIN_PASSWORD",
                "allowed_hosts": ["dhcp*.corp.example"],
            },
            # SSH with a key read from a file.
            "dhcp-ssh": {
                "username": "svc-netbox-dhcp",
                "private_key_file": "/run/secrets/dhcp_ssh_key",
                "allowed_hosts": ["10.0.0.5"],
            },
            # Failover message authentication: the secret only, for both partners.
            "failover-secret": {
                "secret_env": "DHCP_FAILOVER_SECRET",
                "allowed_hosts": ["dhcp1.corp.example", "dhcp2.corp.example"],
            },
        },
        "import_user": "dhcp-import-bot",
    },
}
```

A missing environment variable or unreadable file fails the job that needed it, with a message naming the profile
and the variable or file, never the value.

### allowed_hosts

Set `allowed_hosts` on every profile. Without it, anyone who may edit a Windows server in NetBox can point it at a
machine they control and receive the profile's credentials there. NetBox logs a warning at startup for each profile
without it:

```
credential profiles without allowed_hosts can be sent to any host: dhcp-admin
```

It is a list of host names and IP addresses. `*` and `?` match within one dot-separated label, as in TLS
certificates: `dhcp*.corp.example` matches `dhcp01.corp.example` but not `dhcp.evil.example.corp.example`. Names
compare without case and without a trailing dot; IPv6 addresses compare in any spelling.

It is checked:

- when a Windows server is saved (the form and the API refuse a `host` or `computer_name` the profile does not
  cover);
- before every connection, so tightening the list later also stops servers saved earlier;
- for failover secrets, against the hosts of both partners when the relationship is saved and whenever a push sends
  the secret.

A Windows server's `host` and `computer_name` must be plain host names or IP addresses: a port, a user name, a path
or spaces are refused, so the check and the connection always see the same host.
