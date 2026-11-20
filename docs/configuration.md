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
| `transports`, `ports`, `kerberos_realms`, `ssh_host_keys` | Optional limits on how the profile's servers connect, see [below](#pinning-transport-port-realm-and-host-key) |

Every secret (`username`, `password`, `private_key`, `secret`) can be given in three ways:

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
`change` on Windows servers, keep the keys in the worker's `known_hosts`, or list them in the profile's
`ssh_host_keys` (below).

### Pinning transport, port, realm and host key

Within the hosts `allowed_hosts` covers, whoever may edit a Windows server still chooses how the profile's credentials
travel: the transport, the port, the Kerberos realm whose KDC the logon goes to (it receives a value derived from the
password, which can be attacked offline), and the SSH host key that is trusted. When the people who write
`configuration.py` are not the people who edit servers, a profile can pin these too. Each key is optional: without it,
nothing changes.

| Key | Holds | A server using the profile must |
|---|---|---|
| `transports` | `"winrm"`, `"ssh"` | use one of these transports |
| `ports` | port numbers (1 to 65535) | connect on one of these ports. A server without a port uses the default (5986 for WinRM over HTTPS, 5985 over HTTP, 22 for SSH), and that default must be listed. A port does not imply TLS: **WinRM over HTTPS** and **Verify host** stay the server editor's choice (Basic and CredSSP always need both) |
| `kerberos_realms` | realm names, compared without case | with WinRM and Kerberos, name one of these realms. Other logons do not use the server's realm |
| `ssh_host_keys` | host keys, written as the server's SSH host key field takes them (`ssh-ed25519 AAAA...`; `ssh-ed25519`, `ssh-rsa` and `ecdsa-sha2-nistp256/384/521`) | with SSH, either pin one of these keys, or pin none: the connection then trusts only these keys, never the worker's `known_hosts` |

```python
PLUGINS_CONFIG = {
    "netbox_windows_dhcp_plugin": {
        "credentials": {
            "dhcp-admin": {
                "username": "svc-netbox-dhcp",
                "password_env": "DHCP_ADMIN_PASSWORD",
                "allowed_hosts": ["dhcp*.corp.example"],
                "transports": ["winrm"],
                "ports": [5986],
                "kerberos_realms": ["CORP.EXAMPLE"],
            },
            "dhcp-ssh": {
                "username": "svc-netbox-dhcp",
                "private_key_file": "/run/secrets/dhcp_ssh_key",
                "allowed_hosts": ["10.0.0.5", "10.0.0.6"],
                "transports": ["ssh"],
                "ssh_host_keys": [
                    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGbM5L1hQdQHwPVzFXDyOcnRTMUXN0zXV0fRSC0c0b7w dhcp01",
                    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINPId/9cRTqDqanPcCm3ESFRFp0vxBnfZ0+ZIkyc8tiF dhcp02",
                ],
            },
        },
    },
}
```

Each value must be a list (a tuple works too). A bare string, `None`, a port given as text or `True`, an unknown
transport name or a key that does not parse (or is of another type) is refused, naming the profile and the key: no
server can use the profile until it is fixed. An empty list allows nothing it applies to: an empty `transports` or
`ports` refuses every server using the profile, an empty `ssh_host_keys` every SSH server, and an empty
`kerberos_realms` every WinRM server with Kerberos (as does a Kerberos server without a realm, whatever the list).
Keys compare by their key material, so the comment and spacing do not matter. A host pattern written before a key
(as in a `known_hosts` line) is ignored: every key listed is trusted for every SSH server of the profile, so give
servers that must not share keys profiles of their own.

The Kerberos SPN (**Kerberos SPN** on the server) is not pinned. The plugin asks for no delegation, so a ticket for
another service name gives that service neither the password nor a ticket it can pass on. But an editor who sets the
SPN to an account they control, on a server using WinRM over HTTP or HTTPS without verification, and who can also
intercept the traffic, defeats Kerberos mutual authentication: they can then read what the plugin sends, including
failover shared secrets, though not the password. WinRM over HTTPS with verification avoids it; a profile-level
requirement for it is planned
([#147](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/issues/147)).

Connections also refuse a transport or authentication value outside the known choices (a record written straight to
the database with `KERBEROS` or `Basic`, say), so the checks and the transport always read the same value.

The limits are checked like `allowed_hosts`: when a Windows server is saved (the form and the API name the field
that breaks the rule) and again before every connection, including the connection test, live leases and the
failover logon, so a server changed in the database or a profile tightened later is refused before anything is
sent. A failover shared secret travels over the primary server's own connection, so the limits of the secret's
profile (`shared_secret_profile`) apply to that connection too, as well as those of the server's own profile. A
failover partner is only checked against `allowed_hosts`.
