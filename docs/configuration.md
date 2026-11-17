# Configuration

Plugin settings go in `PLUGINS_CONFIG["netbox_windows_dhcp_plugin"]` in NetBox's `configuration.py`. Settings that
belong to one Windows server (host, transport, schedule) are fields of the [Windows server](servers.md) in NetBox.

The NetBox worker reads the same configuration as the web service, and runs every job: make sure the settings, the
environment variables and the files they name exist for both.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `credentials` | `{}` | [Credential profiles](#credential-profiles), by name |
| `import_branch_prefix` | `'dhcp-import'` | Branches created by imports are named `<prefix>/<server>/<timestamp>` |
| `import_change_request` | `True` | Open a change request for each import branch when netbox-change-control is installed |
| `import_direct` | `True` | With netbox-branching, offer **Import directly** (an import written straight into main). `False` allows branch imports only. Change control's `protect_main` applies either way |
| `import_user` | `None` | User name [scheduled imports](import.md#scheduled-imports) run as. With branching, scheduled imports fail without it |
| `review_job_threshold` | `100` | Applying or dismissing more open [import proposal](import.md#without-branching-import-proposals) items than this at once (Apply all, Dismiss all, a bulk selection, or the REST API) runs as a background job instead of in the web request, so a large first import cannot time out the browser or a reverse proxy. `None` keeps every review in the request |
| `sync_run_retention_days` | `90` | Days to keep [sync runs](servers.md#sync-runs). The import scheduler deletes older runs, oldest first, except the ones the plugin still reads and runs with a journal entry (see [Workers](workers.md#sync-run-retention)). 1 to 36500; `0` or `None` keeps every run |
| `push_debounce` | `10` | Seconds to wait after a save before pushing, so a burst of edits becomes one push (saves outside branching) |
| `max_concurrency` | `4` | How many scopes one import, push or dry run reads at the same time, and the most PowerShell commands that job has running at once. Each command is its own WinRM connection (or SSH session). The cap is per job, so per Windows server record: jobs of other records, and lease views, add their own (see [Workers](workers.md#timeouts-and-concurrency)). 1 to 10 is sensible: above 10 a warning is logged, above 30 (the default WinRM `MaxShellsPerUser`) 30 is used. `1` reads one scope after another. A value that is not a whole number of at least 1 is logged and the default used. Changes are always sent one after another |
| `command_timeout` | `120` | Seconds: the WinRM connection and operation timeout (one wait for output), and the longest silence allowed while an SSH command runs (SSH connects within 30 seconds). A WinRM command that never finishes is abandoned after ten times this long, and never before 10 minutes |

## Credential profiles

Credentials are never stored in the database, the change log or the job logs. Each Windows server in NetBox names a
credential profile, and the profile says where the secrets come from. A profile is a dictionary of keys:

| Key | Used for |
|---|---|
| `username` | WinRM and SSH. `DOMAIN\user`, `user@REALM` or a local `user` |
| `password` | WinRM (all methods), SSH password login, or the passphrase of an encrypted SSH key (it is then used as a passphrase only, never offered to the server as a password) |
| `private_key` | SSH key login: an Ed25519, ECDSA or RSA key in PEM or OpenSSH format |
| `secret` | The shared secret of a [failover relationship](failover.md#shared-secret) |
| `allowed_hosts` | The hosts the profile's credentials may be sent to, see [below](#allowed_hosts) |
| `allow_insecure_credssp` | `True` lets CredSSP run over HTTP or without certificate verification. Labs only, see [Security](security.md#transport) |

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
and the variable or file, never the value. So does an inline value of `None`, which is what
`"password": os.environ.get("X")` gives when `X` is missing: use `password_env` instead. That holds for the keys the
job uses: `username` and `password`, and `secret` when a failover relationship reads it from the profile. A key that
is not used and set to `None` reads as not set.

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

An entry is either an address pattern or a name pattern, never both:

- four dot-separated parts made of digits, `*` and `?` (`10.20.*.*`), or anything containing a `:`, is an address
  pattern and matches IP addresses only: `10.20.*.*` does not allow the DNS name `10.20.evil.example`;
- a shorter entry made of digits and wildcards that holds a number (`10.*`, `192.168.*`) is read as an address
  pattern too, and matches nothing: write all four parts of an IPv4 pattern (`10.*.*.*`);
- anything else is a name pattern and matches host names only. A bare `*` allows any single-label name, not IP
  addresses: list IPv6 addresses or an IPv6 pattern of their own.

It is checked:

- when a Windows server is saved (the form and the API refuse a `host` or `computer_name` the profile does not
  cover);
- before every connection, so tightening the list later also stops servers saved earlier;
- when a push creates a failover relationship, for the partner: creating it makes the primary server contact the
  partner as the plugin's account, so the primary's profile must cover the partner's DHCP host too (its
  `computer_name` when set, else its `host`);
- for failover secrets, against the hosts of both partners when the relationship is saved and whenever a push sends
  the secret.

A Windows server's `host` and `computer_name` must be plain host names or IP addresses: a port, a user name, a path
or spaces are refused, so the check and the connection always see the same host. So are names a resolver would read
as an IPv4 address written another way (`2130706433`, `0x7f.1`, `127.1`, or any name whose last label is a number).

An SSH host key pinned on a Windows server is only as trustworthy as the people who may edit that server: restrict
`change` on Windows servers, or keep the keys in the worker's `known_hosts`.
