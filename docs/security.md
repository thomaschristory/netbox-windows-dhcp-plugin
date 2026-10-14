# Security

The plugin holds credentials that can change DHCP on your Windows servers, and anyone who can change DHCP can
redirect clients. These are the guarantees it gives, and what is left to you.

## Credentials

- Credentials live only in the plugin configuration (`PLUGINS_CONFIG`), read from the configuration itself, an
  environment variable or a file. They are never written to the database, the change log, job logs, sync runs or
  API responses. A Windows server only stores the **name** of its credential profile.
- Secrets are read when a job connects and kept in memory for that job only. Their text representation in Python
  never includes them, so a traceback cannot print them.
- Errors name the cmdlet and the PowerShell message, nothing more.
- The failover shared secret is read when a push needs it and goes straight to the cmdlet; NetBox records only
  whether authentication is on.

Prefer `*_env` or `*_file` over inline secrets, and keep `configuration.py` readable only by the NetBox service.

## Where credentials can be sent

- `allowed_hosts` on each profile limits the hosts its credentials may reach. It is checked when a Windows server is
  saved and again before every connection, for `host` and `computer_name`; for a failover secret, against both
  partners when the relationship is saved and whenever a push sends the secret. Without it, a user who may edit a Windows server could point it at their own machine and receive the
  password; NetBox warns at startup about such profiles. See [Configuration](configuration.md#allowed_hosts).
- `host` and `computer_name` must be plain host names or IP addresses, so the check and the transport always agree on
  the host.

## Transport

- **WinRM** uses HTTPS with certificate verification by default. Over HTTP, NTLM and Kerberos still encrypt every
  message. Turning verification off is possible for labs; the Windows server page shows it.
- **Basic** authentication sends the password itself, so the plugin only allows it over HTTPS with verification on.
- **SSH** always checks the host key, from the pinned key on the Windows server or the worker's `known_hosts`; an
  unknown or changed key fails the connection. There is no way to turn this off.
- A new WinRM connection is made per command, as the terraform provider the plugin is ported from does: NTLM
  sessions shared between concurrent calls proved unreliable.

## PowerShell

- Nothing typed in NetBox is ever pasted into a PowerShell command. Every value reaches the script as one
  single-quoted JSON literal (`$p`), decoded on the Windows side. A scope name with quotes, semicolons or `$(...)` is
  just a name.
- Every script stops on the first error (`ErrorAction Stop`) and answers one JSON envelope, so a failed cmdlet is
  never mistaken for success.

## Who can do what in NetBox

- Every action that connects to Windows (test, import, push, dry run, leases) needs `change` on the Windows server,
  not just `view`. See [Permissions](permissions.md).
- Applying import proposal items writes as the reviewing user, with their permissions.
- With netbox-branching and netbox-change-control, pushes only follow merges, and merges only follow approved change
  requests. Edits made directly in main still push (except while a branch is being merged or reverted); restrict who
  may edit main if every change must be approved.

## Safety of the Windows data

- A push only deletes objects NetBox manages, and refuses deletes that would take hand-made objects with them. See
  [Push](push.md#what-a-push-will-not-do).
- A scope with leases is never force-removed.
- Turning off **Sync enabled** on a Windows server stops all pushes to it at once.

## What is left to you

- Give the plugin's account only the rights it needs: DHCP Administrators and remote login, not domain admin.
- Restrict the WinRM or SSH port on the DHCP servers to the NetBox hosts.
- Protect the NetBox hosts, the worker's environment and `known_hosts`, and the database backups (they hold the DHCP
  configuration, though not the credentials).

## Reporting a vulnerability

Do not open a public issue. Report it privately through GitHub's
[private vulnerability reporting](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/security/advisories/new)
(**Security > Report a vulnerability** on the repository). Only the maintainer sees the report; the fix and an
advisory are published together.
