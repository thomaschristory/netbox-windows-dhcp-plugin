# Windows DHCP sync plugin for NetBox: design

Status: approved 2026-10-13; data model revised for 0.2.0 (standalone) on 2026-10-27. Package `netbox-windows-dhcp-plugin`, module and `PLUGINS` entry `netbox_windows_dhcp_plugin` (`netbox_windows_dhcp` is taken by another plugin).

## 1. Goal

A NetBox 4.7+ plugin that models Windows DHCP servers in NetBox and keeps NetBox and the servers in step, in both directions, over WinRM or SSH. It is standalone: it owns the whole Windows DHCP data model (0.1.x was a sub-plugin of netbox-plugin-dhcp; 0.2.0 dropped that dependency, see section 3). `main` is the 0.2 line; the `release/0.1` branch keeps bug fixes, and no new features, for users who stay on netbox-plugin-dhcp.

- **NetBox to Windows (push).** Changes to DHCP objects in NetBox are applied to the Windows servers. With netbox-branching, this happens when a branch is merged (and change control has approved it). Without branching, it happens after each save.
- **Windows to NetBox (import).** Changes found on a Windows server are brought back for review. With branching, each import creates a new branch. Without branching, they go into a review queue.
- **v1 covers everything:** scopes (v4 and v6), superscopes, reservations, exclusions, option definitions and values (server, scope, reservation, class), user and vendor classes, policies, failover relationships, MAC filters and leases (read only).

Out of scope: DNS (the terraform provider handles it), and managing Windows server roles or features.

## 2. Dependencies

| Package | Version | Why |
|---|---|---|
| NetBox | >= 4.7.0 | netbox-branching 1.2 requires 4.7 |
| netbox-branching | >= 1.2.2, optional | Push on merge, import as branch |
| netbox-change-control | >= 0.6, optional | Approval gate before merge. Requires branching |
| pypsrp[credssp] | required | WinRM (NTLM, Basic, Kerberos, CredSSP), message encryption over HTTP. CredSSP (requests-credssp, pure Python) is what failover writes need: the server calls its partner as the caller |
| paramiko | required | SSH transport |
| pypsrp[kerberos] | optional extra | Kerberos needs system GSSAPI libs, so it stays an extra |

Branching and change control are detected at runtime with `apps.is_installed(...)`. Their imports live in one lazily loaded module (`integrations/branching.py`) so the plugin works without them.

## 3. Data model

One plugin model per Windows concept, named and shaped the way Windows sees it. Scopes and reservations point at core NetBox objects (`ipam.Prefix`, `ipam.IPRange`, `ipam.IPAddress`, `dcim.MACAddress`) so DHCP addresses appear in IPAM; those FKs are `PROTECT`, so IPAM cannot delete an address a scope or reservation uses.

| Windows | Model | Notes |
|---|---|---|
| DHCP server | `WindowsServer` | Own unique `name`; optional `device` (dcim.Device) or `virtual_machine`, for reference only (the connection uses `host`); connection settings (section 4) |
| Scope (v4 and v6) | `Scope` | `server`, `prefix` (unique per server; the family comes from it), `ip_range` (v4 only, the scope start and end, inside the prefix), `superscope`, `name` and `description` (the Windows ones), `state`. v4: `scope_type`, `lease_duration`, `delay_ms`, `max_bootp_clients`, `activate_policies`. v6: `preference`, `preferred_lifetime`, `valid_lifetime`, `renew_timer` (T1), `rebind_timer` (T2). Properties `network`, `family`, `scope_id` |
| Superscope | `Superscope` | `server`, `name`. No prefix. `Scope.superscope` is `SET_NULL`: removing a superscope keeps its scopes, as on Windows. No DHCPv6 superscopes |
| Reservation | `Reservation` | `scope`, `ip_address` (both families, unique per scope), v4: `mac_address` or `client_id` (not both; a reservation with neither is not pushed), `reservation_type`; v6: `duid`, `iaid`. `name` and `description` are the Windows ones |
| Exclusion range | `ExclusionRange` | `scope`, start, end |
| User and vendor class | `WindowsClass` | `server`, name, type, data, family |
| Option definition | `OptionDefinition` | `server`, `family`, `vendor_class` (nullable), `code`, `name`, `windows_type`, `multi_valued`, `default_value` (list). Unique per (server, family, vendor class, code). Windows' standard v4 definitions are not stored |
| Option value | `OptionValue` | `server`, `scope` and `reservation` (both empty: server level; `clean()` sets the scope from the reservation), `family`, `code` (an integer, no FK: standard options need no definition), `vendor_class`, `user_class`, `policy`, `value` (JSON list of strings, so commas and spaces survive). Unique per (server, family, scope, reservation, code, vendor class, user class, policy). Properties `level` and `definition` |
| Policy | `WindowsPolicy` | `server`, `scope` (empty for a server policy), conditions stored as structured JSON, ranges, processing order |
| Failover relationship | `FailoverRelationship` | `primary_server` and `partner_server` (WindowsServer), mode, MCLT, ratio or standby, shared secret reference, `scopes` (M2M to Scope) |
| MAC filter (allow/deny) | `MacFilter`, `MacFilterLists` | `server`; `MacFilterLists` holds the allow and deny list switches (one per server) |
| Lease | none | Read only. Read live by a job and shown on a Leases tab of the scope page; not stored |

`netbox_windows_dhcp_plugin/standard_options.py` lists the standard options (`STANDARD_V4`, `STANDARD_V6`: code to name, type and multi-valued flag; `standard_name()`). It labels values in the UI and decides what the sync leaves alone: definitions of standard v4 codes are not imported (Windows ships them, and a push must never remove one), and DHCPv6 values of other codes are left out (DHCPv6 definitions are not synced).

**Naming.** Objects keep their Windows names. Uniqueness follows Windows, per server (classes, policies per level, superscopes, failover relationships). A scope's `__str__` is `<server>/<scope id>` so lists tell servers apart.

**Core objects.** Import creates or reuses the NetBox `Prefix`, `IPRange`, `IPAddress` and `MACAddress` objects in a VRF chosen per server (`import_vrf`, default global). Failover partners therefore share one IP range per scope. A scope's first IP range and its reservations' addresses follow the VRF of the scope's prefix, which differs from the import VRF only for a scope made by hand elsewhere. An import never moves an IP range that a scope of another server uses unless the two are failover partners for it in NetBox: that scope change fails and names the other scope.

**From 0.1.x.** 0.1.x stored servers, scopes, reservations, definitions and options in netbox-plugin-dhcp (its DHCP server, subnet and pool, shared network, host reservation, option definition and option models) with our Windows-only settings beside them (`WindowsScope`, `WindowsReservation`, `WindowsOptionDefinition`, `WindowsOptionQualifier`, `WindowsVendorOptionDefinition`, `WindowsVendorOption`). netbox-plugin-dhcp is modelled on Kea, which forced workarounds: names unique across servers (`<server>/<scope id>` naming templates), option values as one comma-separated string (`\,` escape, values with surrounding spaces skipped), a covering prefix for each superscope, subnet ID allocation across branches, no uniqueness on option values, `save()` side effects lost on branch merges, and upstream bugs (netbox-plugin-dhcp issues #68 and #69). 0.2.0 replaced all of it with the models above. There is no data migration: the single 0.2.0 migration (`0001_standalone`) drops the 0.1.x tables and users re-import from Windows (see `installation.md`). Migration `0003` (0.3.0) then deletes the content types of the six dropped 0.1.x models and the change log entries of their objects, which made `remove_stale_contenttypes` (run by netbox-docker after every migration) fail; it keeps a type that is the last of a permission, tag or policy, or that another protected row uses, never touches other apps, and is faked on branch schemas because content types live only in main. The record kinds, keys and fields the sync works on did not change, so the diff engine, the push planner and the Windows layer kept their logic; only the comma-escaping workaround went.

## 4. Branchable and bookkeeping models

Branchable (DHCP configuration, reviewed and merged like any NetBox data): `Scope`, `Superscope`, `Reservation`, `ExclusionRange`, `WindowsClass`, `OptionDefinition`, `OptionValue`, `WindowsPolicy`, `FailoverRelationship`, `MacFilter`, `MacFilterLists`.

Not branchable (bookkeeping, always written in main): `integrations/branching.NON_BRANCHABLE`, handed to netbox-branching through `register_branching_resolver` at startup.

- `WindowsServer`: name, optional device or virtual machine, host, transport (winrm or ssh), port, WinRM auth (ntlm, basic, kerberos, credssp), failover auth (empty or credssp: a second client for failover writes and replication, #141), HTTPS (default on), TLS verification, Kerberos realm and SPN, SSH host key, optional `computer_name` for remoting, the names it reports for itself, VRF for imports, sync enabled, import schedule, and a credential profile name. Deleting it deletes its DHCP objects in NetBox (not on Windows; IPAM objects stay). A server that is the partner of another server's failover relationship cannot be deleted while that relationship exists.
- `SyncState`: per managed Windows object, the last pushed fingerprint. This is what lets the push delete only objects NetBox manages, and spot drift. A push records every object it creates or updates, and adopts every object it finds identical on both sides (which is how objects merged from an import branch become managed). Applying an import proposal item records it too, since NetBox and Windows then agree.
- `SyncRun`: one row per push, dry run, import or connection test job with its result, linked to the NetBox `Job`. Kept for `sync_run_retention_days` (90 by default, 0 keeps them all; issue #106): the import scheduler deletes older runs, oldest first, at most `retention.PRUNE_BATCH` (500) per tick, and once caught up looks again an hour later (a key in NetBox's cache; 15 minutes after a failed tick). It never deletes what the plugin still reads (`retention.protected_run_ids`), per server: the newest run of each kind (`due_servers`, the Import panel), the newest push that reached Windows (`pusher.replication_runs`, read for `replication_pending`), with branching the newest run of each of the two newest import branches (`import_status.import_branch_runs`, the panel's earlier branch), runs of open import proposals, and runs whose job is still queued or running and younger than `ABANDONED_JOB`. Runs with a journal entry are never deleted either: a job writes no change log, so the entry would vanish without a trace. Protection is per server, not per viewer: a user whose view permission is limited by run attributes can see their newest visible run deleted. Migration `0005` adds an index on `created` (the oldest-first lookup) and one on `(windows_server, kind, -completed)` (`replication_runs`). The delete cascades to the run's `PushTrigger` rows, tags and bookmarks, and unlinks closed proposals; it is one atomic delete per batch, so a NetBox protection rule on sync runs that refuses one run stops retention. `sync_run_retention_days` is checked at startup too (a warning), and must be 1 to 36500 days.
- `ImportProposal` and `ImportProposalItem`: the review queue used when branching is off.

Leases have no model: a lease read is kept in its job's data (see section 8).

**Credentials** never go in the database, the change log or job logs. `WindowsServer` names a credential profile defined in `PLUGINS_CONFIG` (username, password or key, read from environment variables or files), the same way the terraform provider reads them from config and environment. Each profile can list `allowed_hosts` (wildcards allowed); the server form refuses a `host` or `computer_name` outside it, and the connection code checks again before every connection, so editing a server cannot redirect credentials to another machine. An address pattern matches IP addresses only and a name pattern matches names only, and hosts a resolver reads as numeric IPv4 spellings are refused. A failover partner is one more host the primary's credentials reach, so a push refuses to create a relationship whose partner (its `computer_name` when set) the primary's profile does not cover. Profiles without the list allow any host and are logged as a warning at startup. A profile can also pin `transports`, `ports` (the server's effective port), `kerberos_realms` (WinRM with Kerberos, compared upper-case) and `ssh_host_keys` (#103): each optional and strictly a list or tuple (an empty one allows nothing it applies to; SSH key types limited to what paramiko loads; a Kerberos server without a realm is refused when `kerberos_realms` is set, the form reporting "A realm is required" first), checked in `WindowsServer.clean()` on the field that breaks it and again in `connection.check_connection_allowed`. With `ssh_host_keys`, a server's pinned key must be one of them, and a server without one trusts only those keys (`SSHConfig.trusted_host_keys`), not the worker's `known_hosts`. Before any of this, `check_connection_allowed` refuses `transport`, `winrm_auth` or `failover_auth` values outside the choices (a record written straight to the database), and `separate_failover_auth` is only ever `credssp`. Ports do not imply TLS; HTTPS and verification are server settings unless the profile sets `require_verified_https` (#147): strictly `True` or `False` (anything else is a `CredentialError` whatever the transport), it makes WinRM with the profile use HTTPS with verification for every logon (an error on `winrm_https` for HTTP, else on `verify_tls`), checked in three places through `conf.connection_pin_errors`: `WindowsServer.clean()`, `connection.check_connection_allowed` (which also covers the failover client, same record) and `conf.check_secret_connection` (a failover secret profile); `allow_insecure_credssp` does not lift it. With SSH it requires the profile's `ssh_host_keys` (error on `transport`), since a key pinned only on the server is the editor's choice. It covers the plugin's own connection only, not the DHCP server's RPC to its failover partner nor the `-ComputerName` hop. `WindowsServer.clean()` also runs the secret profile checks of the relationships where the server is primary, only when a connection field (`CONNECTION_FIELDS`) changes (never reading the secret; an undefined secret profile is left to the push, which checks every time). `conf.PROFILE_KEYS` is the one list of known profile keys; `ready()` warns about any other key (`profiles_with_unknown_keys`). `requests>=2.32.4` is a runtime floor (older versions could read an empty `CURL_CA_BUNDLE` as verification off; CVE-2024-47081). WinRM ignores the worker's proxy environment variables by default (#105): `WinRMRunner` passes pypsrp `no_proxy=True` and `_prepare_session` sets the requests session's `trust_env` to False with no proxies, because pypsrp's `no_proxy` only clears the scheme in use and requests resolves environment proxies again on every send (the logon rounds of NTLM, Kerberos and CredSSP reuse those settings). A profile's `proxy` key (strictly `"none"`, the default, or `"env"`, checked with the pins) lets WinRM follow the environment again; the server's own profile governs the failover client too (same connection, another logon), and a failover secret profile other than the server's must also say `"env"` when the server's does. SSH reads no proxy variables.

## 5. Windows layer (ported from terraform-provider-windowsddi)

Same layering as the provider, so the two stay easy to compare:

```
sync engine  ->  client (one function per cmdlet operation)  ->  psscript (envelope)  ->  runner (WinRM or SSH)
```

- **psscript.** Same contract: each script runs in try/catch and prints one JSON envelope `{ok, data}` or `{ok: false, error}`. User values only enter as a single-quoted JSON literal `$p` with quotes doubled; nothing from NetBox is ever interpolated into PowerShell. Secrets (the failover shared secret) never enter the script text: they follow the script on the same stdin line (`<script>.<name>:<value>...<count>`, each part base64, the count of pairs last so a line cut short is refused), the bootstrap reads them into `$wdSecrets` with .NET calls only, and the preamble copies them onto `$p`, so script block logging (event 4104) does not record them (checked with pwsh 7, and with Windows PowerShell 5.1 on Windows Server 2025 by the lab test `test_secret_stays_out_of_the_script_block`) (issue #104). Same preamble, including `$PSDefaultParameterValues['*:ErrorAction']='Stop'`, and the same base64 stdin bootstrap, which reads the script as one line (Windows OpenSSH sometimes loses the end of stdin; over SSH, input above 64 KB is refused, as Windows OpenSSH stalls on it).
- **Not found** uses the same rules: `ObjectNotFound`, `DHCP 20005`, `20010`, `20018`, `20111` (policy not found, also filed under `ObjectNotFound`), or a null result. The reservation option reads alone (`list_option_values` with a reserved IP and `list_reservation_option_values`) also take `DHCP 20016` (InvalidType), Windows' answer for an IP that is not reserved, as not found (`client.RESERVATION_GONE_IDS`); everywhere else it stays a real error. DHCPv6 answers a missing reservation with `DHCP 20013` (a database error code), which stays a real error (issue #100). `DHCP 20007` (scope has leases) is a real error. A getter that finds nothing leaves `$out` holding AutomationNull, which Windows PowerShell 5.1 serialises as `{}` (pwsh 7 as `null`); the envelope turns it into `$null` before serialising, and the client also reads an empty object as not found (issue #15).
- **Batched reads (issue #100).** A full read (`windows_state.read`, used by import, push and dry run) used to start
  PowerShell once per reservation (its option values) and twice per policy (IP ranges, option values). Three batched
  operations replace those loops: `option.list_reserved` and `v6option.list_reserved` (client
  `list_reservation_option_values`, `list_v6_reservation_option_values`) and `policy.list_contents`
  (`list_policy_contents`, scope or server-level policies). The client sends the IPs or names it has just listed,
  at most 64 and 12 KB of escaped `$p` literal per script (`READ_BATCH`, `READ_BATCH_BYTES`; a longer single name is
  refused), so the input stays under 24 KB, below the 32 KB that SSH sends on stdin (a longer script goes as a file, #63). The script answers `[{ip, values: [...]}]`
  (policies: `{name, ranges, values}`; values without their option name, scope or IP, to keep large scopes' output
  small; the client leaves the name empty) and stops at its first error, which reaches the envelope as with the
  one-at-a-time reads: a reservation or policy deleted mid-read is not found, so `_gone_or_read` reads the scope
  again, and a DHCP service that goes away fails the read at once rather than item by item. Server-level policies are
  read outside `_gone_or_read`, so one deleted during the read fails the read, as before. A key with no answer, or an
  answer without its list of values, is an `OutputError`, never "no options". Over SSH `command_timeout` is a silence
  timeout and the script prints only at its end, so a batch must finish within it: the option reads use `-Brief`
  (no option name lookup), and with `computer_name` the script opens one CIM session (`New-CimSession`, swapped into
  `$cn` as `-CimSession`, removed in a `finally`) instead of one per cmdlet call. Per scope the read is now
  exclusions, reservations, scope options, policies, plus one batch per 64 reservations and one per 64 policies when
  there are any. The single-target reads stay for the push writer (`_keep_reservation_options`). Checked on Windows
  Server 2025 (lab, Windows PowerShell 5.1): output shapes (an empty `values` stays `[]`), `-Brief` with the same
  values and class names as without it, 6.8 ms per reservation with `-Brief` against 15 ms without (64 items about
  0.4 s, while a PowerShell start costs about 2.2 s), and the not-found codes above (`test_batched_reads_match_single_reads`,
  `test_batched_read_time`, `test_v6_batched_reservation_option_values`). The CIM session path was checked later
  through a management host (domain lab, Windows Server 2025, #62).
- **Cmdlet parameters checked on Windows Server 2025** (lab run, issues #14 to #22): an option value on a reservation is named by `-ReservedIP` alone, since Set and Remove refuse it next to `-ScopeId` (IPv4) or `-Prefix` (DHCPv6); the client keeps the scope or prefix as context in what it returns. `Rename-DhcpServerv4Superscope` takes `-Name` and `-NewName`, unlike Add and Remove (`-SuperscopeName`). `Get-DhcpServerv6Lease` has no `AddressState`, so a DHCPv6 lease has no state. A policy condition takes a wildcard only at its start or end, and a policy with an FQDN condition cannot hold IP ranges (DHCP 20135) or ordinary option values (DHCP 20136). `Set-DhcpServerv4Scope` refuses a range that moves one end out and the other in (DHCP 20023), so the client first narrows to the overlap of the old and new pools (or widens over both when they do not overlap), then sets the final range, and tries to put the old range back if that fails (#30; both directions checked in the lab, Windows Server 2022). `Set-DhcpServerv4Failover` has no `-ServerRole` (only Add has it), so a hot standby role cannot change in place and the push refuses that change (#33). A `Set-DhcpServerv4Reservation` or `Set-DhcpServerv6Reservation` that resent the client ID and type (DHCPv6: DUID and IAID) for a description change reported success but dropped the reservation's option values (#42). The client now sends those only when they change, and the push writer reads the reservation's option values before and after every reservation update (two extra list calls) and writes back any that went missing; the plan and dry run do not change. A failed write-back does not stop the others; the error names each lost value and whether NetBox holds it (the next push recreates it) or not (DHCPv6 vendor class values, unsynced codes, values added on Windows since the last import: put back by hand). A new client ID, type, DUID or IAID on its own drops them too (lab, Windows Server 2022, #56). `Set-DhcpServerv4OptionValue` and `Set-DhcpServerv6OptionValue` check that every DNS server address (option 6, DHCPv6 23) answers before saving and refuse one they cannot query ("is not a valid DNS server"); NetBox cannot know what the DHCP server reaches, so the client sends `-Force`, which skips the check as the DHCP console allows. `Set-DhcpServerv4Failover` refuses `-Mode` when it names the mode the relationship already has (Windows Server 2025), so the writer sends the mode only with a switch.
- **Failover writes** (checked on two Windows Server 2022 servers, 2026-10-26): Add, Set, Remove, the scope add and
  remove, and replication make the server call its partner as the caller, so they need CredSSP (as the server's WinRM authentication, or as its failover authentication, #141) or SSH with a password
  (an NTLM or Kerberos logon cannot be passed on: `PermissionDenied`). Resource-based constrained delegation (the only kind tried)
  does not help: the DHCP calls to the partner never used Kerberos, they went out as an anonymous NTLM logon (domain
  lab, Windows Server 2025, #62). Neither does an SSH key logon. Add, Set and replication ask for confirmation unless given
  `-Force` (`-Confirm:$false` does not help, ConfirmImpact is Medium), so they send it. The removes never do: they do
  not ask in the Normal state, and when the partner is unreachable `-Force` removes on this server only (split
  relationship), while without it Windows refuses ("not in NORMAL state"). Windows keeps a relationship whose last scope left, empty, on both partners.
- **Normalisation:** scope ID from start and mask, MAC canonical form, lease durations, `InActive`/`Inactive` casing. Numeric option types compare hex-or-decimal; string and binary values compare exactly.
- **runner.** WinRM through pypsrp with a new connection per call, as the Go code learned the hard way with NTLM. A WinRM call whose TCP connection is refused before its command is sent (during the logon or the shell creation) is tried again after 0.5, 1 and 2 s, then fails as before (issue #132): a refused connection carries nothing, so this is safe for writes too, and nothing is tried again once the command has been sent. SSH through paramiko with pinned host key or known_hosts. A read of a whole server reads the server-wide lists (IPv4 and DHCPv6) one after another, then up to `max_concurrency` scopes, IPv4 and DHCPv6 in one pool, at the same time on a thread pool (issue #99). Each scope is still one unit, so a scope deleted mid-read is still detected; results are merged in list order so the records match a one-at-a-time read. When a scope fails, the scopes after it are cancelled or stop at their next command, the ones before it finish, and the first failure in list order is raised, as one at a time; a job interrupted in its main thread (a timeout) stops every scope at its next command and waits at most `INTERRUPT_WAIT` (30 s) for the commands in flight, so the job can record its failure within RQ's grace period. The stop is a thread-local check in `windows/runner.py` (`set_stop_check`, `Stopped`) that `Limited` and the SSH session retry also honour, so `windows/` stays free of sync logic. `runner.Limited` caps the commands in flight per job (per WindowsServer record, not per Windows host). SSH never drops a live shared connection for a failed session open (only a dead one is reopened); a refused session while others of the runner are open joins a first-come, first-served line (one place per open, kept across a reconnect; a dead thread's place is dropped) and is tried again each time one of ours closes (every 0.1 s just after a close, otherwise after a short back-off, then every second) for as long as those run, up to `command_timeout` and at most `SESSION_WAIT_MAX` (300 s); ten PowerShell commands at once took about 10 s on a small Windows Server 2025, so a few seconds were not enough, and polling let a command that had just finished take the freed session ahead of one that had waited. A refusal with none open fails at once; a wait that reaches the deadline says so (another command is more likely stuck). Removing a leftover script file waits 5 s at most. Windows OpenSSH refuses a session past `MaxSessions` with a channel open failure, as OpenSSH does. Writes are sent one after another, in order, a WinRM command that never finishes is abandoned after ten times `command_timeout` (at least 10 minutes), and a PostgreSQL advisory lock per server lets only one import or push talk to a server at a time, across all workers. The lock is held by the database session, so the workers' database connection must not go through a transaction-pooling proxy (pgbouncer in transaction mode).
- **New cmdlets** beyond the provider: DHCPv6 scopes, reservations and options, superscopes, option definitions, classes, policies, failover, MAC filters, server settings. These follow the same patterns and get the same fixture tests.

## 6. Push: NetBox to Windows

The push is a **reconcile per server**, not a replay of the change log. For each affected server it reads the Windows state, compares it with NetBox, and applies the difference in dependency order (classes and definitions, then scopes, superscopes, exclusions, reservations, policies, options, failover, MAC filters). It only deletes Windows objects listed in `SyncState`, so objects created by hand on Windows are left alone (they come back through import instead).

**With branching.** A `post_merge` receiver reads `branch.get_merged_changes()`, keeps the plugin and IPAM changes, works out which servers are affected, and queues one push job per server. Because `post_merge` fires in the RQ worker after commit, nothing is pushed for an aborted merge. `post_revert` does the same, with affected IDs captured in `pre_revert`. Model saves made inside a branch are ignored. Main saves collect their request IDs; after commit, `AppliedChange` identifies merge/revert replays whose completion receiver queues the push. Unrelated main edits still push even while another branch is merging or reverting.

**With change control.** Nothing extra: it already refuses merges without an approved change request, so a push only ever follows an approved change.

**Without branching.** `post_save`/`post_delete` queue a push job through `transaction.on_commit`, debounced per server.

**Always available.** A "Sync now" button and a dry-run that shows the planned changes without applying them.

**Failover.** Writes go to one partner, then `Invoke-DhcpServerv4FailoverReplication` copies them to the other, as the provider's design notes describe.

**How it is built (milestone 4).**

- **Plan.** `sync/push.py` is pure Python. It diffs NetBox records (source) against Windows records (target) with
  the import's diff engine, then applies the safety rules: delete only what `SyncState` lists; never delete a managed
  object whose Windows fingerprint changed since the last push (someone edited it by hand); never delete an object
  whose removal would take unmanaged objects with it (a scope holding a hand-made reservation, a vendor class or
  policy option value, a scope policy: `windows_state.hidden_records` and the scope's policies count as children). NetBox data Windows cannot hold is reported, not pushed, and its Windows counterpart is kept:
  duplicates (`#<pk>` keys), an IPv4 scope without an IP range, a reservation without a MAC or client ID, options
  and definitions 1 and 51 (the mask and lease time come from the prefix and the scope's `lease_duration`), and values
  of codes Windows has no definition for. Class names compare without case, as on Windows. A lease time left empty in NetBox keeps the Windows one, and a reservation
  without a name gets its IP, as Windows does, so neither shows as a change on every push.
- **Write.** `sync/windows_writer.py` turns one change into client calls, back in Windows spelling (`InActive`,
  `d.hh:mm:ss`, 2^32-1 for no BOOTP limit). An exclusion update is a remove and an add. Changing a scope subnet mask
  (refused by the plan first, see the delete pause) or an option definition type or array flag is refused: Windows only allows it by removing the object with
  everything under it. A scope with leases is never force-removed (DHCP 20007 fails that change).
- **Run.** Changes run one at a time. A failure skips the children of an object that was not created and keeps the
  parents of an object that was not deleted; other changes go ahead and the run ends failed with one line per
  error. A lost connection stops the run. Fingerprints are sha256 of the record, with option numbers in decimal.
- **Jobs.** `PushJob` and `DryRunJob` take the per-server lock shared with import. A push that finds the server busy
  queues itself again a minute later, so a merge's push is never lost. A new push is not queued while one for the
  same server is waiting to start (it will read the latest data anyway). `sync_enabled = False` stops pushes, not
  dry runs. A scheduled import (`scheduled=True` job kwarg) that finds the server busy queues itself again
  `PUSH_RETRY` seconds later, up to `IMPORT_ATTEMPTS`, and deletes its own busy run so `due_servers` does not count it
  as the latest import (the queued job keeps the scheduler from queueing another); manual imports fail and keep the
  run.
- **IPAM edits need the DHCP permission (#102).** A `pre_save` receiver (`ipam_guard.check_edit`) on Prefix,
  IPRange, IPAddress and MACAddress compares the fields a push reads (prefix; range start and end, address part;
  address, address part; MAC) with the stored row. When one changed and a scope or reservation uses the object, the
  request user needs `change` on every such scope or reservation (object permissions and constraints count), or
  NetBox's `AbortRequest` refuses the save with a message naming the ones the user may view (others are neither
  named nor counted). The same check runs again in `post_save` (`recheck_edit`) against the saved values, so a
  constraint on the edited value cannot be escaped; a refusal there writes the stored values back with `update()`
  before raising, so a caller catching the exception (a script) or a save outside a transaction does not keep the
  edit. `update_fields` saves compare only the fields written; an instance built with an existing primary key is
  checked like an edit. Covered: edit view, bulk edit (prefix length), bulk import, REST API, scripts and background
  requests, which carry their user. Not checked: saves with no user (nbshell, management commands; NetBox writes no
  change log entry for them either), imports (proposal apply, into a branch, into main: all under
  `push_suppressed`, they write what Windows has). netbox-branching merges and reverts are checked as the merging
  or reverting user (not each change's author, so the rule does not depend on the merge strategy), against main's
  scopes and reservations, which may include ones created after the branch: `integrations.branching.applying_branch()`
  returns the branch name, that user and the branch's changes to the object (request ID, pre- and postchange data)
  from a context variable
  set by `pre_merge`/`pre_revert`, trusted only while the branch is still merging or reverting (a failed, empty or
  dry-run merge sends no closing signal, so the mark can outlive it) and when the save runs under one of those request
  IDs (a replayed save always carries one, an ordinary save never does: this covers a stale mark in a long-lived
  process while the same branch merges elsewhere); the import's own branch changes are exempt (their request IDs come
  from `ipam_guard.import_request_id()`, a keyed hash of `SECRET_KEY`, and replays keep the original request ID), but
  only when every change the branch holds for that object that writes a watched field (stored values compared as
  they are, mask included, as the replay writes them) is the import's (squash replays an object's changes under its
  last request ID; `ipam_guard.import_only()`). Changes that leave the watched fields alone, such as a sync of
  main's description edit or a hand edit of the tags, do not count (#115). Reverts get no import exemption: undoing
  an import puts back values Windows no longer holds. netbox-branching replays a create as a raw save, which the guard
  otherwise skips; during a merge (`_check_replayed_create`), a create is checked when the branch holds a non-import
  update of a watched field for the object and the folded value differs, as a push sees it, from the value the
  import last wrote to that field (else the created value; squash folds the create and later edits into one
  create), as the merging user against the scopes and reservations using it in the branch schema (they are
  created on main after it). Replayed updates in a merge are checked against main's dependents and the branch's
  (`_merge_dependents`): squash replays every update before any create, so a reservation the branch puts on an
  existing address is not on main yet. The guard reads the stored row and the dependents from the database the save writes to (the `using` of
  the save), not wherever the router would read, so a merge run with a branch active (custom code such as a
  script running in a branch; the merge job itself runs with none) is checked against main. A merge or revert with no
  user (a merge job whose user was deleted, a script calling `merge(user=None)`) is refused when a replayed change
  alters a watched field that a scope or reservation uses, with a message naming no object (#116); the import's own
  changes (in a merge, not a revert) and changes no plugin object depends on still merge. A refusal aborts
  the merge, which netbox-branching rolls back and leaves the branch ready (owner's decision after the adversarial
  review of #112). It is NetBox's documented plugin mechanism (`docs/plugins/development/exceptions.md`);
  `CUSTOM_VALIDATORS` belong to the admin's configuration.
- **Push trail.** `enqueue_push(triggers=...)` records `PushTrigger` rows (source: edit, sync_now, merge, revert;
  user; request ID; branch) for the server, whether it queues a job or a waiting one will carry the change. A push
  claims every unclaimed row of its server with one UPDATE once it holds the server lock (and, for that UPDATE, the
  queue lock enqueue_push takes, so a row being recorded is claimed or gets its own push), so edits made during the
  debounce land on that push and busy retries need to pass nothing on; dry runs claim nothing. Not `job.data`:
  NetBox's `Job.start()` saves the whole row from the copy RQ pickled at enqueue time. `PushTrigger` is not
  branchable (`NON_BRANCHABLE`). For the four core models, `_changed` queues only when the guard saw a watched field
  change.
- **Triggers.** `signals.servers_for` maps a changed object (from its saved fields or a change log entry) to the
  Windows servers it affects: our models, and the prefixes, IP ranges, IP addresses and MAC addresses scopes and
  reservations point at. With branching, `post_merge` reads the branch's merged changes (before and after values, so
  moving a scope pushes both servers); `pre_revert` captures the servers and
  `post_revert` queues them. Other saves (`post_save`, and `pre_delete` so related rows still exist) use the object's
  values and NetBox's pre-change snapshot, collect servers per request and queue after commit with `push_debounce`;
  applying import items suppresses this. A queued job that never started within 15 minutes of being due counts as
  lost and no longer blocks new pushes; a push that keeps finding its server busy gives up after two hours.
- **Failover.** After a push to a relationship's primary, the touched scopes (all of the relationship's scopes when
  a server-level object changed) are replicated, by `-ScopeId` only (it and `-Name` are separate parameter sets,
  #51), limited to scopes in the relationship on Windows (one missing there would make Windows refuse the whole call;
  it waits instead). Scopes whose replication failed or waits are kept in the push's
  `SyncRun.summary["replication_pending"]`; the next push to the primary (skipping runs that never reached Windows)
  replicates them again, even with no changes, restricted to scopes still in the relationship. A failed change that may have reached Windows has its scopes replicated too. A push that stopped early (lost connection, command timeout, unexpected error) replicates nothing in that run: its scopes go to `replication_pending`. A push to a partner leaves the relationship's scopes to the primary.

**Remaining objects (milestone 5).** Every kind goes through the same records, diff, plan and writers. Dependency
order: classes, option definitions, scopes, superscopes, exclusions, reservations, policies, option values, failover
relationships, MAC filters, list switches.

- **Keys.** Windows names are not case sensitive, so keys use them lowercased and fields compare without case:
  `vendor/msft`, `msft 5.0/1` (vendor definition), `server/printers` or `10.1.0.0/printers` (policy),
  `...;policy=printers` (policy option value), `campus` (superscope), `ha` (failover), `00-15-5d-*` (MAC filter),
  `server` (list switches). Only policy values carry a `policy` field, so other records keep their fingerprints.
- **Policies.** A scope policy is a child of its scope, and a policy naming a class in its conditions a child of that
  class, so an unmanaged policy keeps its scope (the special case from milestone 4 is gone). IP ranges are diffed and
  added or removed one by one. Set cannot clear a condition, so that change is refused; a lease time left empty in
  NetBox keeps the Windows one. A policy without conditions is not pushed (Windows requires one), nor are policies
  sharing a processing order. A policy value is only pushed when NetBox holds its policy at the same level, since
  Windows names the policy by name only; the models check the policy's server and level too.
- **Containment and clashes (#108).** IPAM edits bypass the plugin's `clean()` checks, and cross-object rules in
  model validation would make proposal items apply in an order-dependent way, so these are push-side reasons instead
  (`_unpushable` per record, `_conflicts` across records; pure, in the dry run, never blocking an import): an IPv4
  scope range outside its prefix, reversed, or holding the network or broadcast address (prefixes up to /30); a
  reservation outside its scope's prefix (inside the prefix but outside the range is accepted by Windows Server
  2025, lab, despite Microsoft KB 2005980); an exclusion outside the prefix or reversed (past the range inside the
  prefix is accepted, lab); IPv4 scopes of one server sharing addresses, Windows scopes NetBox neither holds nor
  manages included; exclusions of one scope sharing even one address; two reservations of one scope for one client
  (client ID compared without separators or case; DHCPv6 DUID and IAID); children of a second scope on one network
  (`#<pk>` key); a scope policy whose stored
  `ip_ranges` do not parse (`netbox_state._policy_ranges` passes the stored value through instead of failing the
  whole read; `windows_state.ip_ranges` stays strict). For a clash, an object already on Windows as NetBox has it
  (same key, same mask or client) keeps being pushed and only the others are left out; with none, all are. Children of
  a refused object are refused too, transitively over `parents()` (`_waiting_children`), so nothing is created under
  it. Scope policy IP ranges must parse (`_unpushable`), run upwards and sit in the scope's prefix (`_conflicts`);
  `windows_writer` checks them again before any write, since a policy update removes old ranges before adding new
  ones.
- **Delete pause (#108).** While anything in a family cannot be pushed, nothing in that family is deleted; the dry run
  lists what is waiting and why. Families (`push.family`): v4 = scope, superscope, reservation, exclusion, failover,
  scope policy, option values under a scope or reservation; v6 = scope6, reservation6, option6 under a prefix; policy =
  server-level policies and the server-level option values set through them. Classes, option definitions, plain server
  option values and MAC filters are in none. `_pause_deletes` runs last in `plan`. The blocking set is the objects
  refused for what they are: plan reasons (a scope mask change included, since Set-DhcpServerv4Scope cannot change it),
  their children (`_waiting_children`, transitively over `parents()`), a scope held by a relationship this push does not
  release (`_scopes_in_other_relationships`), and a new policy (a new name, a rename's new policy included) refused
  for its processing order (`Plan.order_refusals`): one of the deletes may be what it replaces. Such a new policy is
  left out only when its number fails to fit just because the planned deletes close the gaps before it, and fits
  among the policies NetBox holds at the level. A policy already on Windows refused for its number never blocks: it
  stays on Windows whatever happens, so no delete replaces it, and holding the deletes back would keep a NetBox that
  did not renumber after deleting order 1 of 3 stuck for good (that delete goes ahead, and the others wait for the
  renumbering with a reason, as before the pause existed). Changes that still go
  ahead in another form (`_protect_memberships`) do not block either. Every delete of a blocked family, and every
  failover update that drops scopes, becomes a `not_pushed` entry (`waiting: ...`, first three blockers named, then "and
  N more"); a delete outside the families that still holds a paused child waits with it; a create that would clash on
  Windows with a waiting delete (the same client's reservation in the scope, an exclusion or a scope over its addresses,
  a relationship taking a scope a waiting relationship holds) waits too, instead of failing at Windows on every push,
  and so does whatever would be created under a waiting create (a reservation update giving the client of a waiting
  delete included). A reservation create, or an update giving another client, is left out (`_clients_held_on_windows`,
  #155) when Windows would still hold that client in the scope at another address: under a reservation this push does
  not delete (unmanaged, drifted, holding unmanaged children) or whose own change is left out, repeated until nothing
  more is left out. Add and Set both refuse it (lab, Windows Server 2025: ResourceExists). An update freeing a client
  runs before the change taking it (a dependency order, `Plan.client_needs`), and in `_pause_deletes` the change waits
  when that update waits; a cycle of client swaps is left out (Windows cannot apply any of it first). A left-out
  create is a refusal that pauses its family's deletes; a left-out update is not (the reservation stays on Windows as
  it is, so no move is half done). The job log's last line counts what waits.
  Waiting entries come first in `not_pushed` and the job log, and `waiting_count` counts them whatever the 1000-entry
  cap. Paused objects stay in `SyncState`, so the first push with nothing refused deletes them; nothing churns (a second
  plan is empty). This replaced an address-matching heuristic (`_replaced`), which missed a DHCPv6 prefix move, a move
  onto a destination refused for another reason, and a /23 split.
- **Failover moves (#108).** A simple move of a scope from one relationship to another in NetBox: the relationship
  letting it go releases it in the same push (its update runs before the other relationship creates and updates), so the
  taker is not refused and the move converges in one push. Only an update that is not refused releases anything
  (`_scopes_in_other_relationships` repeats until nothing more is refused). Moves that go round in a circle (two
  relationships swapping scopes) are refused, asking for one scope at a time; a scope held by a relationship NetBox does
  not manage, or one this push keeps, still refuses the taker. Both block the IPv4 deletes. What the writer refuses on a
  relationship update (a new partner, a new hot standby role, turning authentication off) is refused in the plan
  (`_failover_update_problem`), so such a taker never lets the other relationship release a scope for nothing. When the
  taker fails at Windows anyway (partner unreachable, secret missing, profile refusing the partner), `execute` puts the
  scopes back into the relationship that let them go (`WindowsWriter.restore_failover_scopes`, best effort, said in the
  error) and records the relationship as Windows now holds it (as this push wrote it, plus the scopes put back), so
  its other changes stay recorded and the next push finds it in step and tries the move again. Without a usable
  connection (lost, or the push stopped on an unexpected error) nothing more is sent; the error says the scopes are in
  no relationship and the next push adds them to the taker. Relationship creates and updates are ordered by the moves
  (a topological sort of who takes from whom), not by name. A child of a policy refused as a create waits with it; a
  policy already on Windows refused for its order does not stop what hangs off it.
- **Lab results (Windows Server 2025).** Range with the network or broadcast address refused; overlapping and nested
  exclusions refused (InvalidData); duplicate IPv4 client refused (ResourceExists); same DHCPv6 DUID and IAID refused,
  another IAID accepted; two scopes sharing addresses refused (ResourceExists, a /25 inside a /24). The fake server
  refuses the same, with Windows' messages. No `pre_save` veto on core IPAM models: the guard in `ipam_guard.py` is
  about permissions only.
- **Model checks (#108).** `WindowsServer.clean()` refuses a second row for one DHCP server (normalised host, port
  with its default, normalised remote DHCP server; hosts compared in Python, so IPv6 spellings match), one query,
  without naming the other server. `FailoverRelationship.clean()` refuses a partner whose DHCP host the primary's
  credential profile does not allow (`conf.host_allowed`), only for a new relationship or a new primary or partner,
  as the push only names the partner on create; skipped for an unknown profile and for imports
  (`check_partner_host = False` in `mapping`), which record what Windows already has.
- **Policy criteria (#21).** Windows Server 2025 stores a MAC prefix as whole bytes with a separator before the
  wildcard (`02-4e-42-ab*` reads back `02-4e-42-ab-*`) and host names in lowercase. `normalize
  .canonical_policy_criterion` gives that form; `windows_state.equal` and `push.fingerprint` compare and hash criteria
  in it (`windows_state.canonical_criteria`), while pushes still send what NetBox holds. Other criteria compare
  exactly until a real server shows Windows rewriting them. The fake server stores criteria the same way.
- **Policy MAC conditions (#110).** Seen on Windows Server 2025: colons, dots and spaces (around the value too), a
  full address without 12 digits, and a prefix of 6 bytes or more (a vaguer "Failed to create policy") are
  refused; a full address keeps its 12 digits whatever the dashes; for a prefix Windows joins the digits (dashes ignored) and pads an odd count on the left, so
  `02-4e-42-a*` reads back `00-24-e4-2a-*`. `normalize.canonical_mac_pattern` and `mac_pattern_refused` model that,
  the fake refuses and stores the same way. `normalize.mac_condition_problem` (copied in `models/dhcp.py`, which
  must not import it) names what Windows refuses or reads as other devices (a prefix group that is not two digits,
  half a byte before the wildcard, a prefix of 6 bytes or more, a doubled or stray dash in a full address): `WindowsPolicy` refuses it when the conditions change, and `push._unpushable` reports a
  stored one as not pushed (older data, raw saves), which pauses the deletes of its family like any refusal. An
  exception for a policy Windows already holds was tried and dropped in review: it could not tell the object Windows
  holds from a renamed one (rename chains, option values moved to another code), and 0.6.0's fingerprints of such
  policies no longer match anyway. Imports are unaffected: Windows only reports whole bytes.
  `tests/lab/test_lab_v4.py::test_policy_mac_condition_storage` records each case.
- **Policy processing order (#24).** Windows numbers the policies of a level (the server, or one scope) 1, 2, 3...
  without gaps: Add with a number past the end fails with DHCP 20110 (seen on Windows Server 2025), and a used number
  moves the others down. `diff` stays generic (same-kind changes sorted by key); `push._order_policies` reorders the
  policy creates and updates of each level by ascending processing order and simulates Windows' list as it goes:
  starting from the Windows policies of the level (hand-made ones included) minus the planned deletes (deletes run
  first; Windows is assumed to close the gap, unverified), each policy is inserted or moved to its number. A policy
  NetBox did not change that the simulation shows moved down gets a processing-order-only update in the same push;
  a number past the end is refused at plan time with a reason (and its policy values are then not attempted). When
  that policy is already on Windows (NetBox does not renumber after a delete, Windows closes the gap), every create
  and update of the level is refused with it: applying the rest alone would reverse the order clients are matched
  in (delete order 1 of 3, and setting 2 back to 2 puts it below 3). A new policy past the end is only left out. The
  fake server enforces the numbering on Add, Set and Remove, including DHCP 20110; the pwsh stub only on Add (its
  Set copies the number without moving the others, and its Remove leaves the gap). Whether Set and Remove renumber
  the same way on Windows (`# unverified:` in `push._order_level` and `fake._ordered`) is settled by the lab test
  `test_policy_processing_order`. The policy fingerprint leaves the processing order out: Windows renumbers a level
  by itself on every add, move or remove, so a policy it moved (one held back with the rest of its level, say) is
  not taken for one edited by hand when NetBox later deletes it. Fingerprints recorded with the order (0.1.0rc1)
  still match while the policy keeps that number (`push.unchanged`).
- **Superscopes.** A `Superscope` with the Windows name; members are the scopes whose `superscope` points at it.
  Import sets and clears `Scope.superscope`. On Windows, removing a superscope keeps its scopes, and in NetBox
  deleting a `Superscope` sets its scopes' `superscope` to null, so the two agree. (0.1.x used a netbox-plugin-dhcp
  shared network with a covering prefix, which deleted its scopes with it.)
- **Failover.** Pushed from the primary only: on the partner, the relationship is neither imported nor pushed
  (`partner_failovers`). The partner is the DHCP server itself, so its Windows server's `computer_name` when it
  connects through a management host, else its `host`; short and fully qualified names match. A partner without a
  Windows server in NetBox is not pushed (there is no host to check the secret's profile against), and import
  refuses a relationship already held with the other server as primary. The shared secret comes from the credential
  profile in `shared_secret_profile` (key `secret`), is checked against both partners' hosts when saved and before each
  push, and only travels from the profile to the cmdlet. It rides the primary server's connection, so
  `conf.check_secret_connection` (on save when the relationship is new or its secret profile or primary changes, skipped
  for imports through `check_secret_profile`, and before each push) applies the secret profile's pins
  (#103, and `require_verified_https`, #147) to that connection; unless the secret profile is the server's own, over SSH the server must pin a host key from
  the secret profile's `ssh_host_keys`, and CredSSP without verified HTTPS also needs the secret profile's
  `allow_insecure_credssp`, and a server profile with `"proxy": "env"` needs the same on the secret profile (#105); records only say whether authentication is on, so a rotated secret is not noticed. Changing
  the partner or turning authentication off is refused (when the push is planned). A scope a
  managed relationship no longer lists may be deleted: it leaves the relationship first in the same push (after a
  lease check, so a refused delete changes nothing; a scope whose delete fails stays in the relationship). A scope
  only leaves a relationship that NetBox manages and that is unchanged on Windows since the last push, and never
  when NetBox does not manage the scope: leaving removes it from the partner. Removing a relationship removes its
  scopes from the partner, as Windows documents.
- **MAC filters.** An update is a remove and an add (no Set cmdlet). The list switches are only pushed when NetBox
  holds a `MacFilterLists` for the server; deleting it stops managing them.
- **Vendor class definitions and values.** `OptionDefinition` and `OptionValue` rows with `vendor_class` set. Their
  option code 1 and 51 are ordinary vendor options, not the mask and lease time.
- **Option values are lists.** `OptionValue.value` is a JSON list of strings, one per Windows value, so commas,
  backslashes and surrounding spaces are stored and compared as they are. (0.1.x stored netbox-plugin-dhcp's
  comma-separated string with a `\,` escape and skipped values with surrounding spaces.)
- **Duplicates.** Extra NetBox copies of one Windows object carry a `_duplicate` field (their primary key) as well
  as the `#<pk>` key suffix, so a Windows name ending in `#<digits>` is not mistaken for one, and import deletes
  exactly the marked copy.

**DHCPv6 (milestone 5).** Core objects only: scopes, reservations, option values at server, scope and reservation
level, and the user classes their qualifiers name. Windows keeps DHCPv6 classes and options apart from IPv4 ones, so
these are kinds of their own, keyed under `v6/` so they never meet an IPv4 key: `class6` (`v6/user/lab`), `scope6`
(`v6/2001:db8:1::`), `reservation6` (`v6/2001:db8:1::/2001:db8:1::10`) and `option6` (`v6/server/23`,
`v6/2001:db8:1::/24;user=lab`). Addresses are compared and keyed in one spelling (lowercase, compressed), and option
values holding IPv6 addresses compare the same way.

- **Not synced, logged and kept.** DHCPv6 vendor classes (writing one needs an enterprise number the plugin does not
  keep) and vendor class values, values of codes outside `standard_options.STANDARD_V6` (DHCPv6 option definitions
  are not synced), and exclusion ranges. Each import notes how
  many it left out. A push never touches them, and they count as children of their scope or reservation, so a scope
  holding any of them is never deleted. NetBox values of other DHCPv6 definitions and DHCPv6 vendor classes held in
  NetBox are left out the same way, so neither side proposes or pushes them.
- **Scopes.** Windows DHCPv6 scopes are /64: NetBox refuses to save a DHCPv6 scope with another prefix length, and
  one whose prefix length changed in IPAM afterwards is not pushed, and keeps its Windows counterpart. Lifetimes
  left empty in NetBox keep the Windows values (a new scope gets Windows' defaults:
  preferred 8 days, valid 12 days, T1 4 days, T2 6.4 days = 552960 s, read back from Windows Server 2025 in #16 and
  matching the cmdlet documentation). T1 and T2 left empty follow the preferred lifetime (`push.v6_timers`: half and
  80 %, which gives exactly those defaults for 8 days): a new scope gets them, and an existing scope keeps its
  Windows timers unless they would break T1 < T2 < preferred lifetime, which the cmdlet documentation asks for. When
  NetBox sets only one of them, the empty one is worked out against it (`push._derive_timers`): the default if it
  still falls on the right side, otherwise T1 = 5/8 of T2 or T2 halfway between T1 and the preferred lifetime. A timer
  NetBox sets is never changed. Two values both set in NetBox and out of order (T1 < T2 < preferred <= valid) are
  refused on save; a half-set case that leaves no room is sent as is and Windows refuses it. What
  Windows itself picks for a short preferred lifetime without timers is unverified (lab test
  `test_v6_scope_timers_with_a_short_preferred_lifetime`); the push sends explicit values either way. The fake server
  refuses timers outside T1 < T2 < preferred (as Windows does, with WIN32 87), so tests catch a push that would send
  them; the pwsh stub does not check them;
  a scope without a name is named after its prefix. Deletes are never forced, so a scope with leases fails with
  DHCP 20007; as for IPv4, its managed reservations and values NetBox deleted with it are removed first, and the
  scope itself on a later push once its leases are gone. Windows has no DHCPv6 failover, so DHCPv6 changes neither replicate nor wait for a primary.
- **Reservations.** Need a DUID and an IAID; `Reservation.ip_address` is the reserved address. An address change is
  a new reservation (it is in the key).
- **Option values** are `OptionValue` rows with family 6 and a standard DHCPv6 code, optionally with a user class
  as for IPv4 (the model checks the class is a DHCPv6 one). No policies: Windows has none for DHCPv6, so the model
  refuses a policy on a DHCPv6 value and the push refuses one set anyway.

## 7. Import: Windows to NetBox

A scheduled job per server (and a manual button) reads the full Windows state and compares it with NetBox.

**With branching.** When differences are found, the job creates and provisions a new branch named `dhcp-import/<server>/<timestamp>` and writes the differences into it under `activate_branch` with change tracking, taking a snapshot of each object before editing it so conflict detection works. With change control installed it also opens a change request. Merging that branch triggers a push, which finds nothing to do because NetBox now matches Windows.

**Without branching (proposal).** The job writes an `ImportProposal` with one item per difference (create, update or delete, with before and after values). A review page shows the diff; reviewers apply or dismiss items one by one or in bulk. Applying writes to NetBox as the reviewing user, so the change log shows who accepted it. Applying does not trigger a push back, since the objects then match Windows.

**Choosing the destination (issue #36).** With branching, the user picks where an import writes: an import branch
(the default and the only choice for scheduled imports), the branch they work in (`current`, named explicitly and
checked against the active branch when posted; written with the user's object permissions, no change request), or
main (`main`, behind a confirmation page that also warns about an open import branch of the server; written under `deactivate_branch` and `event_tracking` as the user, so
NetBox records them and change control's `protect_main` judges every save; per-object permission checks;
`push_suppressed` and `SyncState` recorded as for proposal applies). Each change runs in its own
transaction, and a failed one drops the change log events it queued, so webhooks only see what stayed. Main is offered when `import_direct` is on and `protect_main` does not cover the imported models for a
user without `bypass_policy`. `jobs.import_refusal()` checks the destination when it is queued and again when the job
starts; a refused destination fails, never falls back. The destination is a job kwarg (plus `job.data` for the panel
while queued) and `SyncRun.destination`. Without branching the only destination is `proposal`.

**Repeated runs.** If an open import branch or proposal already exists for a server, the job updates it instead of opening another.

**Where the result is (issue #32).** The Windows server page has an Import panel (`import_status.py`) built from the latest import `SyncRun` and any newer import job that has not started its run yet. It tells queued and running apart from finished, and refreshes itself over HTMX until the job ends. With branching it names and links the branch (`SyncRun.branch_name`, looked up in `integrations/branching.import_branch()`) and its change request, and says main has not changed while the branch is unmerged, telling a new branch from one the run added to (`branch_created` in the run summary); after a run that wrote no branch it still shows an earlier run's open branch. A branch that is no longer open is described by what became of it (issue #44, `BranchInfo.lifecycle`): merging, merged (or archived) with its merge time, or deleted; a deleted branch's last status and merge time come from its first deletion in the change log after the run started (`integrations/branching.branch_deletion()`, read as the viewing user), a branch of the same name created after the run finished is a different branch, and with no deletion record the panel says the outcome is unknown rather than inferring a merge. Without branching it links the import proposal and never mentions a branch; proposal runs record `review` (items sent to the proposal) and `dismissed` (differences left out because the same change was dismissed before) in the run summary, so the panel says "No differences found" only when `changes` is 0 and otherwise says how many wait for review and how many stay dismissed (issue #43). A repeat import moves the open proposal to the newest run, so a run can lose its proposal: it says "a proposal you may not view" only when an unrestricted lookup finds one for the run, otherwise it says the proposal now belongs to a later import or no longer exists and links the server's open proposal. Runs recorded before #43 (no `review`) and with no proposal linked only say how many differences they found. Everything is read as the viewing user (runs, jobs, proposals, branches and change requests they may not view are left out; change requests are read from main even inside a branch), and a job or run still claiming to run after `ABANDONED_JOB` counts as failed, so the panel stops polling. The sync run page shows the same panel for its own run.

**How it is built (milestone 3).**

- **Diff engine.** `sync/diff.py` is pure Python. Both sides become records (kind, Windows key, flat fields) and the
  engine returns create, update and delete changes in dependency order (`diff.KIND_ORDER`, see section 6);
  deletes in reverse. It does not know which side is Windows, so push reuses it.
  `sync/windows_state.py` (pure) and `sync/netbox_state.py` (Django) produce the records; `sync/mapping.py` writes a
  change into NetBox, one object at a time, with `snapshot()` before each update or delete.
- **What is imported.** IPv4 scopes, exclusion ranges, reservations, user and vendor classes, option definitions and
  option values at server, scope and reservation level, including user class qualifiers; since milestone 5 also
  superscopes, policies and policy values, vendor class definitions and values, failover relationships, MAC filters
  and the list switches (see section 6). A failover relationship with message authentication imports without a
  secret profile; the difference stays visible until a reviewer sets one.
- **Mapping details.** The Windows scope or reservation name goes in `name`, the Windows description in
  `description`. Options 1 (subnet mask) and 51 (lease time) are not imported as options, since the prefix and
  `Scope.lease_duration` already hold them. Windows definitions of standard v4 codes are not imported; others become
  `OptionDefinition`s of the server, with the exact Windows type and default value. Existing prefixes, IP ranges, IP addresses and MAC addresses in the import VRF are reused, so failover partners
  share one IP range, which moves when the scope range changes. Descriptions going into 200-character fields are
  cut on the Windows side too, so both sides compare equal; scope and reservation names keep all 255 supported
  characters. Text and binary option values compare exactly; numeric option types compare equal in hex or decimal.
  "No BOOTP limit" (2^32-1) is stored as empty.
- **Deletes.** Any object linked to the server that Windows no longer has is proposed for deletion, including what
  Windows cannot hold: extra copies of one Windows object (names that differ only in case). Those get a `#<pk>` key suffix, and the writer only follows it to an object of the
  same server and parent. Objects created in NetBox that no push has written yet (sync disabled, or a failed push)
  show up as deletes; the reviewer dismisses them.
- **Branch mode.** The diff is taken against the open import branch when there is one (matched on the full name, and
  never a stale one), so a repeated run only adds what changed. A new branch is created and provisioned
  synchronously in the job, only when there are differences (an import job may run for an hour like a push, or
  as long as netbox-branching's `job_timeout` when that is longer); one that fails
  to provision is deleted, and `max_working_branches` refusals fail the run. The change request is created and submitted for review; if that fails, the branch is still recorded on the sync run.
  Branch writes need a real user (NetBox's change log records one).
- **Proposal mode.** A repeated run replaces pending and failed items. A dismissal holds, across proposals, while
  Windows proposes exactly the same values; a run with nothing new to review opens no proposal, and the run summary
  counts the differences a dismissal kept out (`dismissed`) next to those sent to review (`review`). Applying checks the
  object still matches the item's "before" values and fails the item otherwise. Items are applied to main only (an
  apply sent in a branch is refused: the objects would go to the branch and the sync state to main), and an item's
  status is decided on its locked row, so concurrent reviewers or a newer import do not overwrite each other.
  Every object an apply writes
  (prefixes and addresses included) is checked against the reviewer's permissions and constraints, and a refusal
  rolls the item back.
  A review picking more open items than `review_job_threshold` (default 100) runs as `jobs.ReviewJob`, one per
  proposal (the job's object; `ImportProposal` has `JobsMixin`), queued as the requesting user with the item IDs
  frozen at enqueue (issue #101). The job checks again, as that user: active, `change` on the proposal for Apply all
  and Dismiss all, `change` on each item, then the same per-item checks as the request. It takes the server's advisory
  lock (fails if an import or push holds it), writes on main under `event_tracking` with the job's `job_id` as the
  request ID (`integrations/branching.acting_as`), and records `action`, `items`, the counts and `remaining` in
  `job.data`, with `superseded` (picked items no longer open when it starts, read under the server lock: an import
  replaces open items with new IDs; none left fails the job; at the end it also counts items someone else decided
  during the run, so superseded + applied/dismissed + remaining = items) and, when it dies halfway, `interrupted`
  plus counts read from the database (skipped, and logged, if that read fails too). `acting_as` flushes the queued
  events even on an exception, so every write the job's code path makes goes through `importer._one_change` (item
  applies and `_set_item`), keeping events of rolled-back writes out of the queue. Permissions are read once, at
  start. The proposal serializer's `review_job` gives the newest job's status and data without `core.view_job`;
  job IDs, users and errors (`data["error"]` and the job's own `error`) are shown only to the requester and viewers
  of the job, on the page and in the API. A non-integer or negative `review_job_threshold` is logged and treated as None. Queuing
  takes a per-proposal transaction lock and refuses a second job while one waits or runs (409 in
  the API). The API answers 202 with the job (proposal actions) or `{"jobs": [...]}` (item actions); below the
  threshold the request does the work and answers as before. Each item's change log events go into a queue of their
  own (`importer._one_change`), merged into the request's queue with NetBox's coalescing rules only when the item
  succeeds, so applying N items no longer copies the whole queue N times.
- **Who imports.** Manual imports run as the user who asked, who needs change on the server, plus add on branches
  and change requests when those are installed. Scheduled imports, queued every minute by a system job for servers
  whose `import_interval` has passed, run as the `import_user` setting; with branching they fail without it.
- **One at a time.** A PostgreSQL advisory lock allows one import per server across all workers, and an import is
  not queued while another is queued or running (jobs older than two hours count as abandoned).

## 8. UI and API

- The Windows server page: name, linked device or virtual machine, connection and authentication settings, reported
  names, sync settings, the Import panel, and tables of the server's scopes, superscopes, classes, policies, option
  definitions, server option values, MAC filters, failover relationships and sync runs. Buttons: Sync now, Dry run,
  Import now (a menu of destinations with branching) and Test connection.
- A device or virtual machine page lists the Windows servers linked to it, for users who may view them.
- **Reservations on core pages (issue #107).** The reservation FKs to `ipam.IPAddress` and `dcim.MACAddress` stay
  `PROTECT`: cascading would delete the reservation from Windows on the next push as a side effect of a device
  delete, and `SET_NULL` on the MAC would strip a client identity silently. Instead, a **DHCP reservations** panel
  (`template_content.py`) on device, virtual machine, interface (dcim and virtualization), IP address and MAC
  address pages lists the reservations (v4 and v6) using the object: by IP address or MAC address for those two
  pages; for an interface, its assigned IP and MAC addresses; for a device or virtual machine, those of all its
  interfaces. Read as the viewing user (`restrict(user, "view")` on the reservations and on the interfaces
  searched), hidden when empty, one query per page whatever the number of reservations (the generic assignment is
  matched by object type and an interface ID subquery, so a reservation with both addresses on the device is one
  row). NetBox's own protected error already names and links the reservation; the plugin does not change it.
  Modules and FHRP groups (which cascade to interfaces and a virtual IP) get no panel; the docs point to the
  interface or IP address page.
- The scope form can create its prefix and IP range, and the reservation form its IP address and MAC address, in a
  pop-up ("+" next to the field, NetBox's `quick_add`).
- **Live leases (milestone 5).** A Leases tab on the scope page, shown to users who can view both the scope and its
  Windows server (object permissions included). "Load leases"
  connects to Windows, so like the connection test it needs permission to view and change that Windows server. It
  queues a job (`LeaseJob`, one waiting or running per scope under an advisory lock, no notification) that reads
  the scope's IPv4 or DHCPv6 leases (Windows reports a state for IPv4 leases only; the panel shows "n/a" for DHCPv6)
  and keeps at most 5000 of them, in address order, in the job's data with the
  total, the scope read and the time of the read. The panel asks for itself again every few seconds (HTMX) while the
  job runs; a job still waiting or running after 15 minutes counts as lost, so the page stops waiting and a new read
  can be queued. Only the latest read of a scope is kept: each read deletes the earlier finished ones. A failed read
  shows its reason. The job belongs to the Windows server (so one server's reads are listed together) and records the
  scope in its data, so users who may view jobs see the latest lease read too. Reads are refused
  inside a branch, since the job reads the scope as it is in main.
- Pages for our models, the review queue and sync runs, under a "Windows DHCP" menu.
- Pages only show buttons whose views the plugin registers: sync runs, import proposals and their items are written
  by jobs, so their pages offer Delete and the review actions but no Add, Edit, Clone, Import or bulk edit. The
  plugin's own POST buttons pass the CSRF token to their template themselves, because NetBox renders action buttons
  without the page's request context. Tests check the rendered links and tokens, not just the status code (#19, #23).
- REST and GraphQL for every model, plus actions on a Windows server to test the connection, push, dry-run and import,
  and on import proposals and their items to apply or dismiss. Lease reads have no API.
- Built per the netboxlabs plugin skill: `@register_model_view`, `get_model_urls`, Strawberry GraphQL, `@register_filterset`, `PluginMenu`.

## 9. Testing

- **Unit** (`tests/`): psscript rendering and envelope parsing, normalisation, mapping, diff engine, all against recorded JSON fixtures and an in-memory fake server, as in the provider.
- **PowerShell** tests: run the real scripts in `pwsh` against stub cmdlets, reusing the provider's approach.
- **NetBox** (`tests_netbox/`): models, views, API, push and import against the fake server, with and without branching.
- **Lab** acceptance tests against a real Windows server, opt-in through environment variables (`make test-lab` for the client over WinRM or SSH, `make lab-e2e` for import, push and leases on the dev stack). They only create `nbwd-test-` objects, with scopes in `198.19.0.0/16` (inside the `198.18.0.0/15` lab range) and `2001:db8:4e42::/64`, check name and range before every delete, and clean up even when a test fails. Not run in CI. `make lab-seed` (and `make lab-wipe`) fill two lab servers with a realistic configuration to import, in `198.18.0.0/15` outside `198.19.0.0/16` and in `2001:db8:4e43::/64`, so it never meets the test objects (`dev/scripts/lab_seed.py`).

## 10. Repo setup

Matches `netbox-ipam-treeview-plugin` and `netbox-opentelemetry-plugin`: hatchling `pyproject.toml` with uv, ruff (line length 120), `Makefile` (test, test-lab, lab-e2e, lab-seed, test-netbox, lint, format, dev, dist, docs), `dev/` docker compose stack with lab scripts, `tests/` and `tests_netbox/`, `ci.yml` and `release.yml` with SHA-pinned actions, grouped weekly Dependabot, issue and PR templates, CODEOWNERS, CHANGELOG, Apache-2.0 and `docs/`, built into a documentation site with Zensical (`make docs`, checked in CI, published to GitHub Pages from release tags). The dev stack includes branching and change control so both modes are tested.

## 11. Milestones

1. Repo scaffold, models, connection test button.
2. Windows layer ported, with fixtures and fake server.
3. Import (branch mode and review queue).
4. Push (merge-driven and save-driven), dry-run.
5. Remaining objects: policies, failover, MAC filters, DHCPv6.
6. Lab acceptance tests and first release.
7. Standalone (0.2.0): drop netbox-plugin-dhcp, own the whole data model (section 3).

## 12. Decisions on the former open questions

1. **Credentials.** Credential profiles in `PLUGINS_CONFIG`, each secret inline, from an environment variable or a
   file, limited to `allowed_hosts`. Nothing secret is stored in the database, the change log or job logs.
2. **Unmanaged objects.** Left alone. A push only deletes Windows objects recorded in `SyncState` (created by a push
   or adopted by an import); objects made by hand on Windows come into NetBox through import.
3. **Leases.** Live view only (a background job and an HTMX panel on the scope page). NetBox IP addresses are not
   updated from leases, and there is no lease cache.
4. **netbox-plugin-dhcp.** 0.1.x was a sub-plugin of netbox-plugin-dhcp (pinned `>=0.2,<0.3`). Its Kea-shaped model
   needed workarounds for every Windows concept it lacked (section 3), so it was decided on 2026-10-27 that 0.2.0
   makes the plugin standalone, with a fresh start instead of a data migration: Windows is the source of truth, and
   an import rebuilds NetBox from it. Release lines (2026-10-29): `main` carries 0.2 and later; `release/0.1`
   (0.1.1 to 0.1.4 so far) keeps bug fixes, and no new features, for users who stay on netbox-plugin-dhcp.
5. **Device link.** A `WindowsServer` may point at the `dcim.Device` or `virtualization.VirtualMachine` it runs on,
   for reference. The connection always uses `host`, so the link cannot redirect credentials.
