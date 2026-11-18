# Troubleshooting

Start from the **sync run** of the job (on the Windows server page) and its **job log** (**Operations > Jobs**). The
error names the step, the cmdlet and the PowerShell message.

## Connection

| Message | Cause and fix |
|---|---|
| `credential profile 'x' is not defined in PLUGINS_CONFIG` | The Windows server names a profile that does not exist, in the worker's configuration at least. Check the spelling, and that the worker reads the same configuration |
| `credential profile 'x': environment variable X is not set` | Set it for the worker as well as the web service, and restart them |
| `credential profile 'x': cannot read /path: ...` | The file is missing or not readable by the worker's user |
| `credential profile 'x': password is set to None (is an environment variable missing?)` | An inline value is `None`, usually `os.environ.get()` of a variable that is not set. Use `password_env` (or `_file`), and set the variable for the worker |
| `credential profile 'x' may not be used with host 'y'` | `allowed_hosts` does not cover the host (or `computer_name`). Fix the host or extend the list. An address pattern (`10.20.*.*`) only matches IP addresses and needs all four parts (`10.*` matches nothing), and a name pattern only matches names, see [Configuration](configuration.md#allowed_hosts) |
| `failover x: credential profile 'y' may not be used with the partner host 'z'` | A push would create a failover relationship whose partner the primary server's profile does not cover. Add the partner to that profile's `allowed_hosts` |
| `... is not a valid host name or IP address` | The host has a port, a user name, a path or spaces, or is a number a resolver would read as an IPv4 address (`2130706433`, `127.1`). Use a plain host name or IP address |
| `A connection test for x is already queued (job #N)` | A test for that server has not started yet, so no second one is queued. If it never starts, check that `netbox-worker` runs, or delete that job under **Operations > Jobs**; it stops blocking new tests after 15 minutes |
| `Basic authentication needs HTTPS with certificate verification` | Use NTLM or Kerberos, or HTTPS with verification |
| `winrm: host, username and password are required` | The profile has no username or password |
| `winrm: kerberos_realm is required with kerberos auth` | Set **Kerberos realm** on the Windows server (the form refuses Kerberos without one) |
| `...Failover...: Failed to verify if a failover relationship ... exists on server ... (PermissionDenied)` | The server could not reach its partner as your account, or could not resolve the partner's host as NetBox gives it. Use CredSSP (or SSH with a password) for failover servers, and make the partner's host resolvable on the primary DHCP server, see [Failover](failover.md#connecting-to-the-servers) |
| `CredSSP hands the password to the server, so it needs HTTPS with certificate verification` | Turn on **WinRM over HTTPS** and **Verify host** (the server needs an HTTPS listener with a certificate the worker trusts). In a lab only, set `allow_insecure_credssp` on the credential profile |
| `Failover relationship x on server y is not in NORMAL state` | The partner cannot be reached. The plugin never forces a removal, which would leave the partner serving the scope alone. Bring the partner back and push again |
| `failover partner 'x' matches no Windows server in NetBox` | Windows names the partner by its computer name on one side of a relationship. Run **Test connection** on the partner's Windows server so it records that name (or add the partner if NetBox does not have it), see [Failover](failover.md#how-it-syncs) |
| `failover partner 'x' matches more than one Windows server in NetBox` | Two Windows servers reported the same computer name. Give the partner's Windows server that name as host |
| `winrm: CredSSP needs the requests-credssp package` | Install the plugin with its dependencies (`pypsrp[credssp]`) where the worker runs |
| `winrm: ...` with `401`, `Unauthorized` or `credentials were rejected` | Wrong user name or password, or the account may not log in remotely: add it to **Remote Management Users** |
| `winrm: ...` with `certificate verify failed` | The worker does not trust the server's certificate. Set `REQUESTS_CA_BUNDLE`, or check that `host` matches the certificate name |
| `winrm: ...` with `Connection refused` or a timeout | No listener on that port, or a firewall. Check with `Test-WSMan` from another machine and `winrm enumerate winrm/config/listener` on the server. A refused connection is already tried three more times over a few seconds, so this error means the refusals lasted. If it only shows up during reads with `max_concurrency` above 1, something on the path (a NAT router, a proxy) refuses bursts of new connections: lower `max_concurrency` |
| `winrm: PowerShell did not finish within N s` | A command kept the connection alive without finishing for ten times `command_timeout` (at least 10 minutes). Check the DHCP service on the server; raise `command_timeout` only if the server is just slow |
| `winrm: ...` with `Exceeded 0 redirects` | The WinRM endpoint answered with an HTTP redirect, which the plugin never follows. Point **Host** and **Port** at the real listener |
| `PowerShell sent more than 256 MiB of output` | One command returned more than the plugin accepts. Please open an issue with the kind of object and how many the server holds |
| `ssh: host ... is not in ...known_hosts; add it` | Paste the host key into **SSH host key**, or add it to the worker's `known_hosts` |
| `ssh: host key mismatch ...` | The server's key changed, or another machine answers on that address. Check before updating the pinned key |
| `ssh: authentication failed: ...` | Wrong user name, password or key, or the key is not authorised on the server. The passphrase of an encrypted key is not tried as the account's password |
| `ssh: parsing private_key: ...` | The key cannot be read: it must be an Ed25519, ECDSA or RSA key in PEM or OpenSSH format, and an encrypted key needs its passphrase as the profile's `password` |
| `ssh: timed out waiting for the exit status` | The command stayed silent longer than `command_timeout`; raise it for very large servers |
| `The DhcpServer PowerShell module is not available on this host. ...` | The DHCP Server role or its management tools are missing: `Install-WindowsFeature RSAT-DHCP` |
| `Access is denied` from a DHCP cmdlet | The account is not in **DHCP Administrators** (or the service was not restarted after creating the group) |

## Import

**"imports into a branch need a user: set import_user for scheduled imports".** Scheduled imports with branching
need `import_user`; see [Import](import.md#scheduled-imports).

**"importing needs the permission: netbox_branching.add_branch".** The user (or `import_user`) needs to add branches,
and change requests when change control is installed.

**The import fails with a branch limit message.** netbox-branching's `max_working_branches` is reached. Merge or
archive old branches, or raise the limit.

**"another import or push for X is running".** Wait for it; see [Workers and jobs](workers.md#one-job-per-server).

**Objects I created in NetBox are proposed for deletion.** They were never pushed (sync disabled, or the push
failed). Dismiss those items, fix the push, and run it.

**A dismissed item came back.** Windows now shows different values for it; a dismissal only holds while Windows
proposes exactly the same values.

**An applied item failed with a "changed since" message.** Someone changed the NetBox object after the import.
Import again to get a fresh item.

**"import proposals are applied to main: leave the branch (switch to main) and apply again".** Proposal items
cannot be applied while you work in a netbox-branching branch; see
[Import](import.md#without-branching-import-proposals). Dismissing works anywhere.

**"IP range ... is shared with scope ... of Windows server ..., which is not a failover partner for this
scope".** Two servers hold the same subnet and were imported into the same VRF, so their scopes share one IP range
in NetBox. Pick the case that fits, then apply the failed item again (or run the import again):

- The servers are failover partners: apply the failover relationship item too (both in one go), or first.
- Both servers got the same range change on Windows: edit the shared IP range in NetBox to the new start and end.
- Otherwise: edit one of the two scopes in NetBox and point it at its own prefix and IP range in another VRF
  (NetBox refuses two overlapping ranges in one VRF).

Do not delete the scope in NetBox to import it afresh: the next push would delete it on the Windows server.
Setting a separate import VRF on a server does not help here, because a scope that exists keeps the VRF of its
prefix; it only keeps servers apart when set before their first import. See
[Limitations](limitations.md#operations).

**Some Windows objects never show up.** Check the import's notes in the sync run: DHCPv6 vendor classes, DHCPv6
exclusions and values of DHCPv6 codes that are not standard options are left out on purpose. A failover
relationship imports from one partner only: the first one imported, which becomes its primary. A scope deleted on
Windows while the server was being read counts as deleted on Windows in that run, and the notes name it.

## Push

**Nothing happens after a save.** Check, in order: **Sync enabled** on the Windows server; that the worker runs;
with branching, that you edited main or merged the branch (edits inside a branch push nothing); that the object
belongs to the server (through its scope, for reservations and exclusion ranges).

**"Sync is disabled for X: nothing pushed".** Turn **Sync enabled** on.

**A change is in `not_pushed`, or the push failed with a "cannot change on Windows" error.** The reason says why;
see [what a push will not do](push.md#what-a-push-will-not-do).

**The push failed on one change.** The sync run's **Error** names the change and the cmdlet's message, and the summary
lists it under `errors`; the other changes were applied. Here a new policy with an FQDN condition went to Windows,
but the option value set on it did not: Windows does not allow ordinary option values on such a policy (see
[Limitations](limitations.md#windows-rules-the-plugin-will-not-work-around)), and Windows Server 2025 only says
"Failed to set option values" (`InvalidOperation`) rather than naming DHCP 20136. Fix the policy or the value in
NetBox and the next push sends it.

![A failed push: policy Kiosks-FQDN created, its option 15 value refused with Set-DhcpServerv4OptionValue: Failed to
set option values on DHCP server dhcp01 (InvalidOperation)](images/troubleshooting-failed-push.webp)

The usual reasons a change is in `not_pushed`:

| Reason starts with | What to do |
|---|---|
| `changed on Windows since NetBox last pushed it` | Someone edited it on Windows. Import it back, or delete it on Windows by hand |
| `NetBox holds more than one object for this Windows object` | Delete the extra object in NetBox (for example a class or policy whose name differs only in case) |
| `option 1 is the subnet mask on Windows` / `option 51 is the lease time on Windows` | Remove the option value from NetBox; the scope's prefix length and lease duration set them |
| `scope(s) ... are already in failover relationship ... on Windows` | Windows holds those scopes in a relationship NetBox does not have (made by hand, or renamed on Windows). Import it, or remove the scopes from it on Windows |

**"unexpected error (<type>), see the worker log".** The push hit an error it did not expect on that change and
stopped: what it had already written is recorded, and the changes after it are `not_attempted`. The traceback is in
the NetBox worker's log (`netbox-worker`, or the `rqworker` service). Please open an issue with it.

**A scope deleted in NetBox stays on Windows, in its failover relationship.** It had leases when the push first
tried, so it was kept. A later push deletes it once the leases are gone. A scope left that way by 0.2.0 or an
earlier version is taken for one added by hand: remove it from the relationship on Windows once.

**DHCP 20007 when deleting a scope.** The scope still has leases. The plugin never forces the delete; it succeeds on a
later push once the leases are gone, or delete the scope on Windows by hand.

**A push keeps retrying.** It found the server busy (an import running). It retries every minute for up to two
hours, then fails.

### A dry run right after an import plans changes

An import followed by a dry run should plan nothing. If it plans updates, compare the `planned` values with Windows:
a value that Windows rewrites on save (casing, a number in hex) points at a normalisation gap. Please open an issue
with the kind, the key and both values (without anything that identifies your network).

## Leases

**There is no Leases tab.** You may not view the scope's Windows server.

**The Load leases button is missing.** You need `change` on the Windows server, and to be in main rather than a
branch.

**"The last read never finished".** The worker did not run the job within 15 minutes. Check that the worker runs, then
load again.

## Upgrade

**The netbox-docker container exits with `ProtectedError: Cannot delete some instances of model 'ContentType'`.** The
database was upgraded from 0.1.x and the image's automatic `remove_stale_contenttypes` step stops on old change log
entries. From 0.3.0 the plugin removes its own leftovers during `migrate`; see
[Installation](installation.md#with-netbox-docker) for what it deletes, what it keeps and how to start NetBox again.

## Getting more detail

The job log of each run lists every change it made or refused. To see what PowerShell returned for one cmdlet, run
the same `Get-DhcpServer...` cmdlet on the server as the plugin's account.
