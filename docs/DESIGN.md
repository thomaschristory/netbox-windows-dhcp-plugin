# Windows DHCP sync plugin for NetBox: design

Status: approved 2026-10-13. Package `netbox-windows-dhcp-plugin`, module and `PLUGINS` entry `netbox_windows_dhcp_plugin` (`netbox_windows_dhcp` is taken by another plugin).

## 1. Goal

A NetBox 4.7+ plugin that keeps Windows DHCP servers and the [sys4 netbox-plugin-dhcp](https://github.com/sys4/netbox-plugin-dhcp) data in step, in both directions, over WinRM or SSH.

- **NetBox to Windows (push).** Changes to DHCP objects in NetBox are applied to the Windows servers. With netbox-branching, this happens when a branch is merged (and change control has approved it). Without branching, it happens after each save.
- **Windows to NetBox (import).** Changes found on a Windows server are brought back for review. With branching, each import creates a new branch. Without branching, they go into a review queue.
- **v1 covers everything:** scopes (v4 and v6), superscopes, reservations, exclusions, option definitions and values (server, scope, reservation, class), user and vendor classes, policies, failover relationships, MAC filters and leases (read only).

Out of scope: DNS (the terraform provider handles it), and managing Windows server roles or features.

## 2. Dependencies

| Package | Version | Why |
|---|---|---|
| NetBox | >= 4.7.0 | sys4 0.2 and netbox-branching 1.2 both require 4.7 |
| netbox-plugin-dhcp (sys4) | >= 0.2, < 0.3 | Required. Source of servers, scopes, reservations and options. Beta, pin tightly |
| netbox-branching | >= 1.2.2, optional | Push on merge, import as branch |
| netbox-change-control | >= 0.6, optional | Approval gate before merge. Requires branching |
| pypsrp | required | WinRM (NTLM, Basic, Kerberos), message encryption over HTTP |
| paramiko | required | SSH transport |
| pypsrp[kerberos] | optional extra | Kerberos needs system GSSAPI libs, so it stays an extra |

Branching and change control are detected at runtime with `apps.is_installed(...)`. Their imports live in one lazily loaded module (`integrations/branching.py`) so the plugin works without them.

## 3. How sys4 maps to Windows

sys4 is modelled on Kea, so several Windows concepts have no model. We add our own models for those, linked to sys4 objects with OneToOne or ForeignKey relations. We never change sys4 tables.

| Windows | sys4 model | Notes |
|---|---|---|
| DHCP server | `DHCPServer` | Plus our `WindowsServer` (connection settings) |
| Scope (v4) | `Subnet` + one `Pool` | Pool's `IPRange` is the scope start/end. `valid_lifetime` is the lease duration |
| Scope (v6) | `Subnet` (a /64 prefix, no pool) | `preferred_lifetime`, `valid_lifetime`, `renew_timer` (T1) and `rebind_timer` (T2); state and preference in our `WindowsScope` |
| Scope state, type, delay, NAP, etc. | none | Our `WindowsScope` (OneToOne to Subnet) |
| Superscope | `SharedNetwork` | Needs a covering Prefix; import creates it when missing |
| Reservation | `HostReservation` | v4: MAC to `hw_address`, IP to `ipv4_address`, reservation type in our `WindowsReservation`. v6: DUID to `duid`, IP in `ipv6_addresses`, IAID in our `WindowsReservation` (sys4 has no field for it) |
| Exclusion range | none | Our `ExclusionRange` (FK Subnet, start, end) |
| Option definition | `OptionDefinition` | Server-scoped definitions, with the Windows type and default on our `WindowsOptionDefinition`. Vendor class definitions are our `WindowsVendorOptionDefinition`: sys4 allows one definition per code and server, and Windows ships several vendor classes defining the same codes |
| Option value | `Option` | GFK to server, subnet or reservation. User class and policy qualifiers in our `WindowsOptionQualifier`. Vendor class values are our `WindowsVendorOption` (same GFK, value kept as a list) |
| User and vendor class | none | Our `WindowsClass`. sys4 `ClientClass` is a Kea expression, so we do not reuse it |
| Policy | none | Our `WindowsPolicy` (server or scope level, conditions stored as structured JSON, ranges, processing order) |
| Failover relationship | none (`DHCPCluster` is only a group) | Our `FailoverRelationship` (partner servers, mode, MCLT, ratio or standby, shared secret reference, scopes) |
| MAC filter (allow/deny) | none | Our `MacFilter`, and `MacFilterLists` for the allow and deny list switches |
| Lease | none | Read only. Read live by a job and shown on a Leases tab of the subnet page; not stored |

**Naming.** sys4 names are unique per model across all servers, so two servers with a scope called "LAN" would collide. Imported objects are named `<server>/<scope id>` for subnets and `<server>/<scope id>/<ip>` for reservations, with the Windows name kept in `description` (configurable template).

**Core objects.** Import creates the NetBox `Prefix`, `IPRange`, `IPAddress` and `MACAddress` objects sys4 requires, in a VRF chosen per server (default global).

**Known sys4 quirks.** `Option` has no uniqueness rule, so the sync dedupes. Several models validate in `save()`, so the sync saves one object at a time and never uses bulk updates. `available_client_classes` on Subnet and SharedNetwork is broken upstream and must not be called.

## 4. Our models

Branchable (DHCP configuration, reviewed and merged like sys4 objects): `WindowsScope`, `WindowsReservation`, `ExclusionRange`, `WindowsClass`, `WindowsOptionDefinition`, `WindowsVendorOptionDefinition`, `WindowsVendorOption`, `WindowsPolicy`, `FailoverRelationship`, `MacFilter`, `MacFilterLists`, `WindowsOptionQualifier`.

Not branchable (bookkeeping; listed in branching's `exempt_models` so change control's `protect_main` does not block them):

- `WindowsServer`: OneToOne to `DHCPServer`. Host, transport (winrm or ssh), port, WinRM auth (ntlm, basic, kerberos), HTTPS (default on), TLS verification, Kerberos realm and SPN, SSH host key, optional `computer_name` for remoting, VRF for imports, sync enabled, import schedule, and a credential profile name.
- `SyncState`: per managed Windows object, the last pushed fingerprint. This is what lets the push delete only objects NetBox manages, and spot drift. A push records every object it creates or updates, and adopts every object it finds identical on both sides (which is how objects merged from an import branch become managed). Applying an import proposal item records it too, since NetBox and Windows then agree.
- `SyncRun`: one row per push or import job with its result, linked to the NetBox `Job`.
- `ImportProposal` and `ImportProposalItem`: the review queue used when branching is off.

Leases have no model: a lease read is kept in its job's data (see section 8).

**Credentials** never go in the database, the change log or job logs. `WindowsServer` names a credential profile defined in `PLUGINS_CONFIG` (username, password or key, read from environment variables or files), the same way the terraform provider reads them from config and environment. Each profile can list `allowed_hosts` (wildcards allowed); the server form refuses a `host` or `computer_name` outside it, and the connection code checks again before every connection, so editing a server cannot redirect credentials to another machine. Profiles without the list allow any host and are logged as a warning at startup.

## 5. Windows layer (ported from terraform-provider-windowsddi)

Same layering as the provider, so the two stay easy to compare:

```
sync engine  ->  client (one function per cmdlet operation)  ->  psscript (envelope)  ->  runner (WinRM or SSH)
```

- **psscript.** Same contract: each script runs in try/catch and prints one JSON envelope `{ok, data}` or `{ok: false, error}`. User values only enter as a single-quoted JSON literal `$p` with quotes doubled; nothing from NetBox is ever interpolated into PowerShell. Same preamble, including `$PSDefaultParameterValues['*:ErrorAction']='Stop'`, and the same base64 stdin bootstrap.
- **Not found** uses the same rules: `ObjectNotFound`, `DHCP 20005`, `20010`, `20018`, or a null result. `DHCP 20007` (scope has leases) is a real error. A getter that finds nothing leaves `$out` holding AutomationNull, which Windows PowerShell 5.1 serialises as `{}` (pwsh 7 as `null`); the envelope turns it into `$null` before serialising, and the client also reads an empty object as not found (issue #15).
- **Cmdlet parameters checked on Windows Server 2025** (lab run, issues #14 to #22): an option value on a reservation is named by `-ReservedIP` alone, since Set and Remove refuse it next to `-ScopeId` (IPv4) or `-Prefix` (DHCPv6); the client keeps the scope or prefix as context in what it returns. `Rename-DhcpServerv4Superscope` takes `-Name` and `-NewName`, unlike Add and Remove (`-SuperscopeName`). `Get-DhcpServerv6Lease` has no `AddressState`, so a DHCPv6 lease has no state. A policy condition takes a wildcard only at its start or end, and a policy with an FQDN condition cannot hold IP ranges (DHCP 20135) or ordinary option values (DHCP 20136). `Set-DhcpServerv4Scope` refuses a range that moves one end out and the other in (DHCP 20023), so the client first narrows to the overlap of the old and new pools (or widens over both when they do not overlap), then sets the final range, and tries to put the old range back if that fails (#30; the start-widens direction is unverified). `Set-DhcpServerv4Failover` has no `-ServerRole` (only Add has it), so a hot standby role cannot change in place and the push refuses that change (#33). A `Set-DhcpServerv4Reservation` or `Set-DhcpServerv6Reservation` that resent the client ID and type (DHCPv6: DUID and IAID) for a description change reported success but dropped the reservation's option values (#42). The client now sends those only when they change, and the push writer reads the reservation's option values before and after every reservation update (two extra list calls) and writes back any that went missing; the plan and dry run do not change. Whether a real client ID, type, DUID or IAID change drops the values on its own is unverified.
- **Normalisation** is ported as is: scope ID from start and mask, MAC canonical form, lease durations, `InActive`/`Inactive` casing, option values compared hex-or-decimal.
- **runner.** WinRM through pypsrp with a new connection per call, as the Go code learned the hard way with NTLM. SSH through paramiko with pinned host key or known_hosts. Concurrency is capped per job (`max_concurrency`), and a PostgreSQL advisory lock per server lets only one import or push talk to a server at a time, across all workers. The lock is held by the database session, so the workers' database connection must not go through a transaction-pooling proxy (pgbouncer in transaction mode).
- **New cmdlets** beyond the provider: DHCPv6 scopes, reservations and options, superscopes, option definitions, classes, policies, failover, MAC filters, server settings. These follow the same patterns and get the same fixture tests.

## 6. Push: NetBox to Windows

The push is a **reconcile per server**, not a replay of the change log. For each affected server it reads the Windows state, compares it with NetBox, and applies the difference in dependency order (classes and definitions, then scopes, exclusions, reservations, options, policies, failover). It only deletes Windows objects listed in `SyncState`, so objects created by hand on Windows are left alone (they come back through import instead).

**With branching.** A `post_merge` receiver reads `branch.get_merged_changes()`, keeps the `netbox_dhcp` and plugin changes, works out which servers are affected, and queues one push job per server. Because `post_merge` fires in the RQ worker after commit, nothing is pushed for an aborted merge. `post_revert` does the same, with affected IDs captured in `pre_revert`. Model saves made inside a branch, or while a branch is merging or reverting, are ignored (the merge queues its own pushes); direct edits of main still push, as without branching.

**With change control.** Nothing extra: it already refuses merges without an approved change request, so a push only ever follows an approved change.

**Without branching.** `post_save`/`post_delete` queue a push job through `transaction.on_commit`, debounced per server.

**Always available.** A "Sync now" button and a dry-run that shows the planned changes without applying them.

**Failover.** Writes go to one partner, then `Invoke-DhcpServerv4FailoverReplication` copies them to the other, as the provider's design notes describe.

**How it is built (milestone 4).**

- **Plan.** `sync/push.py` is pure Python. It diffs NetBox records (source) against Windows records (target) with
  the import's diff engine, then applies the safety rules: delete only what `SyncState` lists; never delete a managed
  object whose Windows fingerprint changed since the last push (someone edited it by hand); never delete an object
  whose removal would take unmanaged objects with it (a scope holding a hand-made reservation, a vendor class or
  policy option value, a value with a comma, a scope policy: `windows_state.hidden_records` and the scope's policies
  count as children). NetBox data Windows cannot hold is reported, not pushed, and its Windows counterpart is kept:
  duplicates (`#<pk>` keys), a scope without a pool, a reservation without a MAC or client ID, an option without a
  value, options and definitions 1 and 51 (the mask and lease time come from the prefix and the subnet), and values
  of codes Windows has no definition for. Class names compare without case, as on Windows. A lease time left empty in NetBox keeps the Windows one, and a reservation
  without a name gets its IP, as Windows does, so neither shows as a change on every push.
- **Write.** `sync/windows_writer.py` turns one change into client calls, back in Windows spelling (`InActive`,
  `d.hh:mm:ss`, 2^32-1 for no BOOTP limit). An exclusion update is a remove and an add. Changing a scope subnet mask
  or an option definition type or array flag is refused: Windows only allows it by removing the object with
  everything under it. A scope with leases is never force-removed (DHCP 20007 fails that change).
- **Run.** Changes run one at a time. A failure skips the children of an object that was not created and keeps the
  parents of an object that was not deleted; other changes go ahead and the run ends failed with one line per
  error. A lost connection stops the run. Fingerprints are sha256 of the record, with option numbers in decimal.
- **Jobs.** `PushJob` and `DryRunJob` take the per-server lock shared with import. A push that finds the server busy
  queues itself again a minute later, so a merge's push is never lost. A new push is not queued while one for the
  same server is waiting to start (it will read the latest data anyway). `sync_enabled = False` stops pushes, not
  dry runs.
- **Triggers.** `signals.servers_for` maps a changed object (from its saved fields or a change log entry) to the
  DHCP servers it affects: sys4 subnets, pools, reservations, options and definitions, our models, and the prefixes,
  IP ranges, IP addresses and MAC addresses sys4 points at. With branching, `post_merge` reads the branch's merged
  changes (before and after values, so moving a subnet pushes both servers); `pre_revert` captures the servers and
  `post_revert` queues them. Other saves (`post_save`, and `pre_delete` so related rows still exist) use the object's
  values and NetBox's pre-change snapshot, collect servers per request and queue after commit with `push_debounce`;
  applying import items suppresses this. A queued job that never started within 15 minutes of being due counts as
  lost and no longer blocks new pushes; a push that keeps finding its server busy gives up after two hours.
- **Failover.** After a push to a relationship's primary, the touched scopes (all of the relationship's scopes when
  a server-level object changed) are replicated. A push to a partner leaves the relationship's scopes to the primary.

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
- **Policy criteria (#21).** Windows Server 2025 stores a MAC prefix as whole bytes with a separator before the
  wildcard (`02-4e-42-ab*` reads back `02-4e-42-ab-*`) and host names in lowercase. `normalize
  .canonical_policy_criterion` gives that form; `windows_state.equal` and `push.fingerprint` compare and hash criteria
  in it (`windows_state.canonical_criteria`), while pushes still send what NetBox holds. Other criteria compare
  exactly until a real server shows Windows rewriting them. The fake server stores criteria the same way.
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
- **Superscopes.** A sys4 `SharedNetwork` named `<server>/<superscope>` with the Windows name as description. Its
  prefix is the smallest supernet covering the member scopes (at most a /8; wider is refused), created in the import
  VRF when missing; sys4 checks
  that member subnets sit inside it. A subnet has one parent (server or shared network), so import moves subnets in
  and out, and moves them back to the server before deleting a shared network, which sys4 would otherwise delete
  with its subnets. On Windows, removing a superscope keeps its scopes. Deleting a shared network by hand in NetBox
  still deletes its subnets (sys4 behaviour), and the next push removes those scopes from Windows.
- **Failover.** Pushed from the primary only: on the partner, the relationship is neither imported nor pushed
  (`partner_failovers`). The partner is the DHCP server itself, so its Windows server's `computer_name` when it
  connects through a management host, else its `host`; short and fully qualified names match. A partner without a
  Windows server in NetBox is not pushed (there is no host to check the secret's profile against), and import
  refuses a relationship already held with the other server as primary. The shared secret comes from the
  credential profile in `shared_secret_profile` (key `secret`), is checked against both partners' hosts when saved
  and before each push, and only travels from the profile to the cmdlet; records only say whether authentication is
  on, so a rotated secret is not noticed. Changing the partner or turning authentication off is refused. A scope a
  managed relationship no longer lists may be deleted: it leaves the relationship first in the same push (after a
  lease check, so a refused delete changes nothing; a scope whose delete fails stays in the relationship). A scope
  only leaves a relationship that NetBox manages and that is unchanged on Windows since the last push, and never
  when NetBox does not manage the scope: leaving removes it from the partner. Removing a relationship removes its
  scopes from the partner, as Windows documents.
- **MAC filters.** An update is a remove and an add (no Set cmdlet). The list switches are only pushed when NetBox
  holds a `MacFilterLists` for the server; deleting it stops managing them.
- **Vendor class definitions and values.** Our own models, linked to the vendor class; values keep their list, so
  commas and spaces survive. Their option code 1 and 51 are ordinary vendor options, not the mask and lease time.
- **Comma values.** sys4 keeps option values as one string; a comma inside a value is stored as `\,` (Kea's own
  escape) and split back on commas without a backslash before them. Other backslashes stay as typed (boot file
  paths), so strings stored before this change read the same. Values with surrounding spaces, or ending in a
  backslash, are still skipped. sys4 validates typed options (addresses, FQDNs) by splitting on commas, so escaped
  commas only work in string options, which is where Windows values have them.
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
  keep) and vendor class values, values of codes netbox-plugin-dhcp has no standard dhcp6 definition for (DHCPv6
  option definitions are not synced), values with surrounding spaces, and exclusion ranges. Each import notes how
  many it left out. A push never touches them, and they count as children of their scope or reservation, so a scope
  holding any of them is never deleted. NetBox values of other DHCPv6 definitions and DHCPv6 vendor classes held in
  NetBox are left out the same way, so neither side proposes or pushes them.
- **Scopes.** Windows DHCPv6 scopes are /64: a NetBox subnet with another prefix length is not pushed, and keeps its
  Windows counterpart. Lifetimes left empty in NetBox keep the Windows values (a new scope gets Windows' defaults:
  preferred 8 days, valid 12 days, T1 4 days, T2 6.4 days = 552960 s, read back from Windows Server 2025 in #16 and
  matching the cmdlet documentation). T1 and T2 left empty follow the preferred lifetime (`push.v6_timers`: half and
  80 %, which gives exactly those defaults for 8 days): a new scope gets them, and an existing scope keeps its
  Windows timers unless they would break T1 < T2 < preferred lifetime, which the cmdlet documentation asks for. When
  NetBox sets only one of them, the empty one is worked out against it (`push._derive_timers`): the default if it
  still falls on the right side, otherwise T1 = 5/8 of T2 or T2 halfway between T1 and the preferred lifetime. A timer
  NetBox sets is never changed; one that leaves no room is sent as is and Windows refuses it. What
  Windows itself picks for a short preferred lifetime without timers is unverified (lab test
  `test_v6_scope_timers_with_a_short_preferred_lifetime`); the push sends explicit values either way. The fake server
  refuses timers outside T1 < T2 < preferred (unverified that Windows does), so tests catch a push that would send
  them; the pwsh stub does not check them;
  a scope without a name is named after its prefix. Deletes are never forced, so a scope with leases fails with
  DHCP 20007; as for IPv4, its managed reservations and values NetBox deleted with it are removed first, and the
  scope itself on a later push once its leases are gone. Windows has no DHCPv6 failover, so DHCPv6 changes neither replicate nor wait for a primary.
- **Reservations.** Need a DUID and an IAID (set on the Windows reservation in NetBox); the first IPv6 address of the
  reservation (by primary key) is the reserved one. An address change is a new reservation (it is in the key).
- **Option values** use sys4's standard dhcp6 definitions, with the user class in `WindowsOptionQualifier` as for
  IPv4 (the qualifier checks the class is a DHCPv6 one). No policies: Windows has none for DHCPv6, so the model
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
`push_suppressed` and `SyncState` recorded as for proposal applies; subnet IDs from the same allocator as branch
imports, which also counts branches named by `destination=current` sync runs). Each change runs in its own
transaction, and a failed one drops the change log events it queued, so webhooks only see what stayed. Main is offered when `import_direct` is on and `protect_main` does not cover the imported models for a
user without `bypass_policy`. `jobs.import_refusal()` checks the destination when it is queued and again when the job
starts; a refused destination fails, never falls back. The destination is a job kwarg (plus `job.data` for the panel
while queued) and `SyncRun.destination` (migration 0005). Without branching the only destination is `proposal`.

**Repeated runs.** If an open import branch or proposal already exists for a server, the job updates it instead of opening another.

**Where the result is (issue #32).** The Windows server page has an Import panel (`import_status.py`) built from the latest import `SyncRun` and any newer import job that has not started its run yet. It tells queued and running apart from finished, and refreshes itself over HTMX until the job ends. With branching it names and links the branch (`SyncRun.branch_name`, looked up in `integrations/branching.import_branch()`) and its change request, and says main has not changed while the branch is unmerged, telling a new branch from one the run added to (`branch_created` in the run summary); after a run that wrote no branch it still shows an earlier run's open branch. Without branching it links the import proposal and never mentions a branch. Everything is read as the viewing user (runs, jobs, proposals, branches and change requests they may not view are left out; change requests are read from main even inside a branch), and a job or run still claiming to run after `ABANDONED_JOB` counts as failed, so the panel stops polling. The sync run page shows the same panel for its own run.

**How it is built (milestone 3).**

- **Diff engine.** `sync/diff.py` is pure Python. Both sides become records (kind, Windows key, flat fields) and the
  engine returns create, update and delete changes in dependency order: classes, option definitions, scopes,
  exclusions, reservations, options; deletes in reverse. It does not know which side is Windows, so push reuses it.
  `sync/windows_state.py` (pure) and `sync/netbox_state.py` (Django) produce the records; `sync/mapping.py` writes a
  change into NetBox, one object at a time, with `snapshot()` before each update or delete.
- **What is imported.** IPv4 scopes, exclusion ranges, reservations, user and vendor classes, option definitions and
  option values at server, scope and reservation level, including user class qualifiers; since milestone 5 also
  superscopes, policies and policy values, vendor class definitions and values, failover relationships, MAC filters
  and the list switches (see section 6). Option values with surrounding spaces are skipped: sys4 stores a list as one
  comma-separated string, which drops them. A failover relationship with message authentication imports without a
  secret profile; the difference stays visible until a reviewer sets one.
- **Mapping details.** The Windows scope or reservation name goes in `description`, the Windows description in
  `comments`. Options 1 (subnet mask) and 51 (lease time) are not imported as options, since the prefix and
  `valid_lifetime` already hold them. Windows definitions of codes sys4 defines itself are left to sys4; others become
  server-scoped `OptionDefinition`s, with the exact Windows type and default value on `WindowsOptionDefinition`.
  Existing prefixes, IP ranges, IP addresses and MAC addresses in the import VRF are reused, so failover partners
  share one IP range, which moves when the scope range changes. Text going into 200-character description fields is
  cut on the Windows side too, so both sides compare equal. Text option values compare exactly; numbers compare
  equal in hex or decimal. "No BOOTP limit" (2^32-1) is stored as empty.
- **Deletes.** Any object linked to the server that Windows no longer has is proposed for deletion, including what
  Windows cannot hold: extra copies of an option, two subnets on one prefix, two reservations for one address, a
  reservation without an address. Those get a `#<pk>` key suffix, and the writer only follows it to an object of the
  same server and parent. Objects created in NetBox that no push has written yet (sync disabled, or a failed push)
  show up as deletes; the reviewer dismisses them.
- **Branch mode.** The diff is taken against the open import branch when there is one (matched on the full name, and
  never a stale one), so a repeated run only adds what changed. A new branch is created and provisioned
  synchronously in the job, with netbox-branching's `job_timeout`, only when there are differences; one that fails
  to provision is deleted, and `max_working_branches` refusals fail the run. New subnets get IDs above those in main
  and every open import branch, under a lock, so two import branches never merge the same `subnet_id`. The change
  request is created and submitted for review; if that fails, the branch is still recorded on the sync run.
  Branch writes need a real user (NetBox's change log records one).
- **Proposal mode.** A repeated run replaces pending and failed items. A dismissal holds, across proposals, while
  Windows proposes exactly the same values; a run with nothing new to review opens no proposal. Applying checks the
  object still matches the item's "before" values and fails the item otherwise. Every object an apply writes
  (prefixes and addresses included) is checked against the reviewer's permissions and constraints, and a refusal
  rolls the item back.
- **Who imports.** Manual imports run as the user who asked, who needs change on the server, plus add on branches
  and change requests when those are installed. Scheduled imports, queued every minute by a system job for servers
  whose `import_interval` has passed, run as the `import_user` setting; with branching they fail without it.
- **One at a time.** A PostgreSQL advisory lock allows one import per server across all workers, and an import is
  not queued while another is queued or running (jobs older than two hours count as abandoned).

## 8. UI and API

- Tabs on sys4 server and subnet pages: connection status, last sync, drift, live leases.
- **Live leases (milestone 5).** A Leases tab on netbox-plugin-dhcp's subnet page, for subnets of a Windows server,
  shown to users who can view both the subnet and the Windows server (object permissions included). "Load leases"
  connects to Windows, so like the connection test it needs permission to view and change that Windows server. It
  queues a job (`LeaseJob`, one waiting or running per subnet under an advisory lock, no notification) that reads
  the scope's IPv4 or DHCPv6 leases (Windows reports a state for IPv4 leases only; the panel shows "n/a" for DHCPv6)
  and keeps at most 5000 of them, in address order, in the job's data with the
  total, the scope read and the time of the read. The panel asks for itself again every few seconds (HTMX) while the
  job runs; a job still waiting or running after 15 minutes counts as lost, so the page stops waiting and a new read
  can be queued. Only the latest read of a subnet is kept: each read deletes the earlier finished ones. A failed read
  shows its reason. The job belongs to the Windows server (sys4 subnets cannot hold jobs) and records the subnet in
  its data, so users who may view jobs see the latest lease read too. Reads are refused inside a branch, since the
  job reads the subnet as it is in main. The tab view is registered when the plugin starts, because
  netbox-plugin-dhcp builds its URLs before ours are loaded.
- Pages for our models, the review queue and sync runs, under a "Windows DHCP" menu.
- Pages only show buttons whose views the plugin registers: sync runs, import proposals and their items are written
  by jobs, so their pages offer Delete and the review actions but no Add, Edit, Clone, Import or bulk edit. The
  plugin's own POST buttons pass the CSRF token to their template themselves, because NetBox renders action buttons
  without the page's request context. Tests check the rendered links and tokens, not just the status code (#19, #23).
- REST and GraphQL for every model, plus actions to trigger push, dry-run and import.
- Built per the netboxlabs plugin skill: `@register_model_view`, `get_model_urls`, Strawberry GraphQL, `@register_filterset`, `PluginMenu`.

## 9. Testing

- **Unit** (`tests/`): psscript rendering and envelope parsing, normalisation, mapping, diff engine, all against recorded JSON fixtures and an in-memory fake server, as in the provider.
- **PowerShell** tests: run the real scripts in `pwsh` against stub cmdlets, reusing the provider's approach.
- **NetBox** (`tests_netbox/`): models, views, API, push and import against the fake server, with and without branching.
- **Lab** acceptance tests against a real Windows server, opt-in through environment variables (`make test-lab` for the client over WinRM, `make lab-e2e` for import, push and leases on the dev stack). They only create `nbwd-test-` objects in `198.18.0.0/15` and `2001:db8:4e42::/64`, check name and range before every delete, and clean up even when a test fails. Not run in CI.

## 10. Repo setup

Matches `netbox-ipam-treeview-plugin` and `netbox-opentelemetry-plugin`: hatchling `pyproject.toml` with uv, ruff (line length 120), `Makefile` (test, test-netbox, lint, format, dev, seed, dist), `dev/` docker compose stack with seed scripts, `tests/` and `tests_netbox/`, `ci.yml` and `release.yml` with SHA-pinned actions, grouped weekly Dependabot, issue and PR templates, CODEOWNERS, CHANGELOG, Apache-2.0 and `docs/`, built into a documentation site with Zensical (`make docs`, checked in CI, published to GitHub Pages from release tags). The dev stack includes branching and change control so both modes are tested.

## 11. Milestones

1. Repo scaffold, models, connection test button.
2. Windows layer ported, with fixtures and fake server.
3. Import (branch mode and review queue).
4. Push (merge-driven and save-driven), dry-run.
5. Remaining objects: policies, failover, MAC filters, DHCPv6.
6. Lab acceptance tests and first release.

## 12. Decisions on the former open questions

1. **Credentials.** Credential profiles in `PLUGINS_CONFIG`, each secret inline, from an environment variable or a
   file, limited to `allowed_hosts`. Nothing secret is stored in the database, the change log or job logs.
2. **Unmanaged objects.** Left alone. A push only deletes Windows objects recorded in `SyncState` (created by a push
   or adopted by an import); objects made by hand on Windows come into NetBox through import.
3. **Leases.** Live view only (a background job and an HTMX panel on the subnet page). NetBox IP addresses are not
   updated from leases, and there is no lease cache.
4. **sys4 is beta.** Pinned to `>=0.2,<0.3`, following its releases. Upstream bugs are worked around here (for
   example `available_client_classes` is never called) and can be reported upstream separately.
