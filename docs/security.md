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
  whether authentication is on. It is sent next to the script, not inside it, so PowerShell script block logging
  (event 4104) on the DHCP server does not record it (checked with PowerShell 7 and with Windows PowerShell 5.1 on
  Windows Server 2025 in the lab). Module logging (event 4103, and event 800 in the classic "Windows PowerShell"
  log) and transcription can record the failover cmdlet's parameters, `-SharedSecret` included: if they are on,
  restrict who may read those logs.

Prefer `*_env` or `*_file` over inline secrets, and keep `configuration.py` readable only by the NetBox service.

## Where credentials can be sent

- `allowed_hosts` on each profile limits the hosts its credentials may reach. It is checked when a Windows server is
  saved and again before every connection, for `host` and `computer_name`; for a failover secret, against both
  partners when the relationship is saved and whenever a push sends the secret. Without it, a user who may edit a Windows server could point it at their own machine and receive the
  password; NetBox warns at startup about such profiles. See [Configuration](configuration.md#allowed_hosts).
- An address pattern in `allowed_hosts` matches IP addresses only and a name pattern matches names only, so
  `10.20.*.*` cannot be satisfied by a DNS name.
- A failover partner is one more host the primary server's credentials reach: creating a relationship makes the
  primary contact the partner as the plugin's account. A push refuses to create one whose partner the primary's
  credential profile does not cover.
- `host` and `computer_name` must be plain host names or IP addresses, so the check and the transport always agree on
  the host. Names a resolver reads as numeric IPv4 spellings (`2130706433`, `127.1`) are refused.
- An SSH host key pinned on a Windows server is only as trustworthy as the people who may edit that server.
- Within the allowed hosts, a server's editors choose its transport, port, Kerberos realm and SSH host key, and
  whether WinRM uses HTTPS and checks the certificate. A profile's `transports`, `ports`, `kerberos_realms`,
  `ssh_host_keys` and `require_verified_https` take those choices back for the people who write
  `configuration.py`: the realm decides which KDC the logon goes to (it receives a value derived from the password,
  which can be attacked offline), and the host key which machine SSH trusts with the credentials. With `ssh_host_keys`,
  a server without a pinned key trusts only the profile's keys, never the worker's `known_hosts`. See
  [Configuration](configuration.md#pinning-transport-port-realm-and-host-key).
- `ports` does not require TLS. `require_verified_https: True` does: WinRM with the profile must then use HTTPS with
  certificate verification whatever the logon, so NTLM or Kerberos over HTTP, or over HTTPS without verification,
  are refused, and `allow_insecure_credssp` no longer lifts it for CredSSP. Without it, that stays the server
  editor's choice: Basic always needs HTTPS with verification, and so does CredSSP unless the profile sets
  `allow_insecure_credssp`. It does not apply to SSH, which checks host keys instead (`ssh_host_keys`).
- A failover shared secret travels over the primary server's own connection, so the secret profile's limits
  (`transports`, `ports`, `kerberos_realms`, `ssh_host_keys`, `require_verified_https`) and its
  `allow_insecure_credssp` govern that connection
  too, not only the server's own profile. Unless the secret profile is the server's own, a server reached over SSH
  must then pin its own host key, one of the secret profile's `ssh_host_keys`.
- A connection refuses a transport or authentication value outside the known choices, so a record written straight
  to the database cannot slip past these checks with another spelling.

## Transport

- **WinRM** uses HTTPS with certificate verification by default. Over HTTP, NTLM, Kerberos and CredSSP still encrypt
  every message. Turning verification off is possible for labs; the Windows server page shows it. HTTP redirects
  are refused, so the logon only goes to the host that was checked. A proxy set in the worker's environment
  (`HTTPS_PROXY`, `HTTP_PROXY`) still applies to WinRM: set `NO_PROXY` for the DHCP servers unless the proxy is
  meant to carry it.
- **CredSSP** hands the account's password to the DHCP server, which can then act as that account towards other
  servers: that is what failover needs. Use it only for servers that are part of a failover pair, with an account
  that can do nothing but manage DHCP. **Failover authentication: CredSSP** limits it further: the password then
  goes to the server only for the failover writes and replication, and every other call uses the server's own
  authentication (Kerberos, say). Because the password goes to whoever answers, the plugin only allows CredSSP
  over HTTPS with certificate verification, as for Basic: over HTTP, or without verification, the NTLM exchange
  inside CredSSP does not prove who the server is, and `allowed_hosts` only checks the name configured, not who
  answers at that name. A credential profile can set `allow_insecure_credssp` to lift this in a lab.
- **Basic** authentication sends the password itself, so the plugin only allows it over HTTPS with verification on.
- **SSH** always checks the host key, from the pinned key on the Windows server, the credential profile's
  `ssh_host_keys` or the worker's `known_hosts`; an unknown or changed key fails the connection. There is no way to turn
  this off. The passphrase of an encrypted private key is never offered to the server as a password. A script over 32 KB
  is uploaded over SFTP to the account's home folder under a random name (created only if it does not exist), with a
  checksum sent on stdin: PowerShell refuses a file that changed before it was read, and deletes it. The file holds the
  script and its parameters, never a secret (those stay on stdin).
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
  requests. Edits made directly in main still push, including during unrelated branch merges or reverts; restrict who
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
