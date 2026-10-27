# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Documentation: a Reservations page (create, type, options, change, delete) and a step-by-step failover
  walkthrough, each with screenshots of NetBox and of the Windows DHCP console and PowerShell output, taken on two
  Windows Server 2025 servers.

## [0.1.0] - 2026-10-27

Release candidates 0.1.0rc1 to 0.1.0rc5 (2026-10-21 to 2026-10-23) went to PyPI for testing; what testing them found
is listed under Fixed.

First release. Every v1 feature is in place and tested against the fake server and PowerShell stubs. The lab tests
against real Windows servers (`make test-lab`, `make lab-e2e`) ran in full on two Windows Server 2022 DHCP servers,
failover included; what they found is listed under Fixed, and the few details they could not reach are listed in
the documentation's limitations page.

### Added

Connections and security

- Windows server connection settings linked to netbox-plugin-dhcp servers: WinRM (NTLM, Basic, Kerberos or CredSSP, HTTPS by
  default; CredSSP is what failover writes need) or SSH, with an optional `computer_name` for a management host.
- Credential profiles in `PLUGINS_CONFIG`, each secret inline, from an environment variable or a file. Nothing secret
  is stored in the database, the change log or job logs.
- `allowed_hosts` on credential profiles limits which hosts their credentials can be sent to, checked when a Windows
  server is saved and before every connection. Basic authentication requires HTTPS with certificate checks. SSH
  always verifies the host key (pinned key or the worker's known_hosts).
- "Test connection" button and REST action, run as a background job.

Models and API

- Models for what netbox-plugin-dhcp does not hold: Windows scope and reservation settings (including the DHCPv6
  IAID), exclusion ranges, user and vendor classes, option definitions (Windows data type and default value),
  option qualifiers, vendor class option definitions and values, policies, failover relationships, MAC filters and
  the allow and deny list switches.
- Sync bookkeeping: sync state, sync runs and import proposals.
- REST API and GraphQL for every model. Names of classes, policies, superscopes and failover relationships compare
  without case, as on Windows.

Import (Windows to NetBox)

- IPv4 scopes, superscopes, exclusion ranges, reservations, user and vendor classes, option definitions, vendor class
  options, policies (conditions, IP ranges, processing order, lease time), failover relationships, MAC filters and
  option values at server, scope, reservation, user class and policy level. They become netbox-plugin-dhcp objects
  plus the prefixes, IP ranges, IP addresses and MAC addresses they need, in the server's import VRF. Objects missing
  on Windows are proposed for deletion.
- DHCPv6 scopes (lifetimes, T1 and T2, state, preference), reservations (DUID, IAID), user classes and option values
  against netbox-plugin-dhcp's standard DHCPv6 definitions. DHCPv6 vendor classes and values, other option codes and
  exclusion ranges are noted and left alone.
- Option values containing a comma, stored as `\,` (Kea's escape).
- With netbox-branching, the **Import** button is a menu: **Import into branch** (an import branch, the default),
  **Import into this branch** (the branch you work in, no new branch) and **Import directly** (into main, after a
  confirmation page, with your permissions checked object by object and change control's `protect_main` respected;
  the new `import_direct` setting turns it off). The REST import action takes the same choice as `destination`. Sync
  runs record where each import wrote (new **Destination** column and filter), and the Import panel says so while
  the import is queued, running and done (#36).
- With netbox-branching, each import writes into a branch `dhcp-import/<server>/<timestamp>` (reused while open) and,
  with netbox-change-control, opens a change request submitted for review.
- Without branching, each import fills an import proposal whose items are applied or dismissed one by one, in bulk
  or for the whole proposal, from the UI or the REST API. Applying writes as the reviewing user, checks their
  permissions, and refuses items whose NetBox object changed since the import.
- "Import now" button and REST action, and scheduled imports every `import_interval` minutes (`import_user` for
  scheduled imports with branching).

Push (NetBox to Windows)

- A reconcile per server that creates, updates and deletes every kind above, in dependency order, so Windows matches
  NetBox.
- Only objects recorded in sync state are deleted. Objects created by hand on Windows are left alone, and so are
  managed objects changed on Windows since the last push and objects whose removal would take unmanaged ones with
  them. Identical objects are adopted as managed; applying an import item adopts it straight away.
- NetBox data Windows cannot hold (a scope without a pool, a reservation without a MAC address, options 1 and 51,
  values of codes Windows does not define) is reported in the sync run instead of pushed. A scope subnet mask or an
  option definition type is never changed in place, and a scope with leases is never force-removed.
- With netbox-branching, merging or reverting a branch queues a push for each Windows server it touched. Other saves
  (all of them without branching, direct edits of main with it) queue one push per server after commit,
  `push_debounce` seconds later. `sync_enabled = False` stops pushes.
- "Sync now" and "Dry run" buttons and REST actions. A dry run records the planned changes in its sync run.
- Failover: relationships are written on the primary, with the shared secret read from the credential profile named
  in `shared_secret_profile`; the changed scopes are then replicated to the partner. A scope leaving a relationship
  leaves it before it is deleted, and a scope with leases is refused before anything changes.
- One import or push per server at a time across all workers (a PostgreSQL advisory lock); a busy push retries a
  minute later.

Leases

- A Leases tab on the subnet page of Windows scopes (IPv4 and DHCPv6) reads the live leases in a background job.
  Viewing needs the subnet and its Windows server, loading needs permission to change the server, and is refused
  inside a branch. Leases are not stored.

Documentation

- A documentation site (https://thomaschristory.github.io/netbox-windows-dhcp-plugin/): installation, preparing
  Windows, configuration, a first-sync walkthrough, how objects map, import, push, DHCPv6, failover, leases,
  permissions, REST API and GraphQL, security, workers and jobs, troubleshooting and limitations.
- Screenshots from a lab run on real Windows servers: the first-sync walkthrough step by step, import proposals and
  import branches with change requests, a new scope built in NetBox and pushed, a failover relationship, the Leases
  tab and a failed push.
- Installation: netbox-change-control needs at least one policy, or change requests (an import's included) can never be
  approved. How objects map: netbox-plugin-dhcp 0.2.0 does not save comments from its edit forms
  (sys4/netbox-plugin-dhcp#68).
- `SECURITY.md`: report vulnerabilities through GitHub's private vulnerability reporting.

Development

- `dev/scripts/lab-server-setup.ps1` prepares a Windows Server VM as the lab server (DHCP role, a least-privilege
  account, WinRM over HTTPS).
- Opt-in lab tests against a real Windows DHCP server: `make test-lab` (the client over WinRM, one round trip per
  unverified cmdlet) and `make lab-e2e` (import, push and leases on the dev stack). They only touch `nbwd-test-`
  objects in `198.18.0.0/15` and `2001:db8:4e42::/64`.

### Fixed

- The Windows servers list links each server's name to its Windows server page (actions, Import panel, sync runs),
  as the sync run pages do, saved table layouts included; the generic netbox-plugin-dhcp server stays linked in a
  new **Generic DHCP server** column (#38).
- The plugin's buttons (Test connection, Sync now, Dry run, Import now, and Apply all, Dismiss all, Apply and Dismiss
  on the review queue) were rendered without a CSRF token, so clicking them failed with "CSRF verification failed"
  (403). They now carry the token (#19).
- Read-only pages (sync runs, import proposals and their items) showed Edit, Clone, Add, Import or Edit Selected
  buttons that led to a "None" address and a 404. Pages now show only the buttons whose views exist; Delete and the
  review actions stay. Lists without CSV import or bulk edit no longer offer those buttons either (#23).

Found by a first, partial lab run against Windows Server 2025:

- A Windows object that does not exist is reported as missing again, instead of as an object with every field empty.
  Windows PowerShell 5.1 printed "nothing found" as `{}`; the script envelope now turns it into `null`, and the
  client also reads `{}` as missing. This affected MAC filters, superscopes, policies, DHCPv6 scopes, classes,
  option values and every other lookup (#15).
- Renaming a superscope uses the cmdlet's real `-Name` parameter instead of `-SuperscopeName`, which Windows rejected
  (#14).
- Option values on an IPv4 reservation are written and removed with `-ReservedIP` alone: Windows refuses `-ScopeId`
  next to it, so a push of a reservation option failed (#20).
- Option values on a DHCPv6 reservation are written and removed with `-ReservedIP` alone, for the same reason with
  `-Prefix` (#17).
- DHCPv6 leases no longer read an `AddressState` property Windows does not have; the Leases tab shows their state as
  n/a, since Windows reports none for DHCPv6 (#22).
- The policy lab test uses a condition Windows accepts (`*.Example.Test`), checks FQDN policies apart from policy IP
  ranges and option values (which Windows refuses on FQDN policies, DHCP 20135 and 20136), and the fake server now
  refuses a wildcard in the middle of a condition, as Windows does (#18).
- A new DHCPv6 scope pushed without renewal and rebinding times now gets Windows' own default rebinding time (T2),
  6.4 days (6 days 9:36), instead of 6 days 10 hours, so it matches a scope made on Windows without them (#16).
  Renewal and rebinding times left empty in NetBox now follow the scope's preferred lifetime (half and 80 % of it,
  which gives Windows' 4 and 6.4 days for the default 8 days), so a scope with a preferred lifetime shorter than
  6.4 days no longer gets timers past it, which Windows documents as invalid. An existing scope keeps its Windows
  timers while they still fit below the preferred lifetime. When only one of T1 and T2 is set in NetBox, the empty
  one is now worked out against it (T1 at 5/8 of T2, or T2 halfway between T1 and the preferred lifetime, when the
  default would not fit), instead of a default that could land on the wrong side and be refused by Windows.
- Policy conditions Windows stores in its own spelling no longer show as changed after every push or import: a MAC
  prefix such as `02-4e-42-ab*` (read back as `02-4e-42-ab-*`) and a host name in mixed case (read back in
  lowercase) now compare equal, so a second push is a no-op (#21). Upgrading from 0.1.0rc1: a policy whose condition
  NetBox spells differently from Windows was recorded under its old spelling, so until the next push records it
  again, deleting it in NetBox is reported as changed on Windows and not deleted. Push once after upgrading.
- A push now creates and updates the policies of each level in ascending processing order instead of by name, so
  recreating policies whose names sort the other way no longer fails with DHCP 20110. Policies Windows moves down
  when another takes their number are set back in the same push, and a processing order that would leave a gap is
  reported in the plan instead of failing on Windows (#24). After a policy is deleted in NetBox without renumbering
  the ones after it, the push no longer moves the others into the wrong order: it leaves that level as Windows holds
  it and asks for the renumbering. Policies Windows renumbers by itself (after a delete, for example) are no longer taken for
  policies changed on Windows when NetBox later deletes them: the processing order is no longer part of what the
  push compares to notice hand edits.

Found by testing 0.1.0rc2:
- The Windows server page has an **Import** panel saying whether the latest import is queued, running or finished
  and where it went. With branching it names and links the import branch and its change request and says main has
  not changed; without branching it links the import proposal and does not mention branches. It refreshes itself
  while the job runs, and the sync run page shows the same for its own run. The message after **Import now** also
  says where the import will go (#32).
- Changing a scope's pool so that one end grows and the other shrinks (for example `.10-.200` to `.11-.210`) no
  longer fails with DHCP 20023, which Windows returns for that change in one call. The push changes the range in
  two steps: it first narrows to the addresses both pools share (or, when they do not overlap, widens over both),
  then sets the final range. If the second step fails, it tries to put the old range back (#30).
- Updating a hot standby failover relationship no longer fails with "A parameter cannot be found that matches
  parameter name 'ServerRole'": `Set-DhcpServerv4Failover` has no such parameter, so the push no longer sends it.
  Windows cannot change the role (active or standby) in place, so a push that changes it is refused with a message
  saying to recreate the relationship. After switching a relationship to hot standby, the push checks the role
  Windows chose against NetBox. Mode changes no longer wait for a confirmation nobody can answer (#33).
- The policy lab test allows the lease time option (51) Windows lists next to a policy's own option values (#29).
- The missing-module unit test no longer fails on a host with the Windows DHCP management tools (RSAT) installed: it
  runs PowerShell with an empty module path (#31).

Found by testing 0.1.0rc3:
- A reservation description or name change no longer drops its option values on Windows (IPv4 and DHCPv6, #42).
  The push only sends the client ID, type, DUID or IAID when they change, and puts back any reservation option value
  Windows removed during the update. The dry run is unchanged: it still lists only the reservation update. If a value
  cannot be put back, the reservation change is reported as failed, naming each lost option and saying whether it
  comes back by itself: the next push recreates a value NetBox holds, but values NetBox does not hold (DHCPv6 vendor
  class values, option codes the plugin does not sync, values added on Windows since the last import) must be put
  back by hand.
- Without branching, an import that finds only differences a reviewer already dismissed no longer reports "No
  differences found." The Import panel and the sync run now say that nothing new waits for review and how many
  dismissed differences stay ignored while Windows shows the same values. The count of differences waiting for review
  no longer includes dismissed ones, a proposal you may not view is no longer read as "no differences", and the sync
  run summary and job log record how many differences were dismissed earlier (#43). An earlier run whose proposal a
  later import took over (or that was deleted) says so and links the server's open proposal instead of claiming a
  proposal you may not view, and a run recorded before this fix with no proposal linked says only how many
  differences it found.
- The Windows scopes list shows each scope's prefix in its own column, next to the subnet name, as the
  netbox-plugin-dhcp subnet list does, so scopes with similar or repeated names are told apart without opening them.
  The column links to the prefix and sorts by network (#47).
- With branching, the Import panel no longer calls a merged or deleted branch "the open import branch", on the
  Windows server page or on the sync run of an earlier import. It says what became of the branch: being merged,
  merged into main (with the time), merged and since deleted, or deleted without being merged, read from the branch
  or from its deletion in the change log. With no record you may view, it says the outcome is not known rather than
  guessing a merge. A merged import no longer reads as "main matches Windows", the deleted branch row links its
  deletion record, and the server panel links the server's import history (#44).

Found by testing 0.1.0rc4:
- Failover replication after a push to the primary no longer fails with `AmbiguousParameterSet`. The plugin passed
  both `-Name` and `-ScopeId` to `Invoke-DhcpServerv4FailoverReplication`, which takes one or the other; it now
  replicates the changed scopes with `-ScopeId` only, never widening the copy to the whole relationship (#51).
- A failed failover replication is retried by the next push to the primary, even one with nothing else to write, so
  the partner no longer stays behind until the next change. The push's sync run lists the scopes still to replicate.
  A scope that is not yet in the relationship on Windows waits for a later push instead of blocking the others.

Found by the full lab run (two Windows Server 2022 servers; earlier notes said 2025, which is what the rc testers
ran):
- Over SSH, about one PowerShell run in fifteen hung until the timeout: Windows OpenSSH sometimes loses the end of
  stdin, and the script loader read stdin to its end. It now reads the one line the script is sent as. Each hung
  run also kept its SSH session open, so after a few the server refused new ones (#56). Windows OpenSSH also stalls
  on more than about 70 KB of input: the SSH transport refuses a script above 64 KB at once, saying to use WinRM, and
  a run that stays silent past the command timeout now says so instead of giving an empty error.
- Failover relationships could not be created over WinRM: `Add-DhcpServerv4Failover` asked for a confirmation nobody
  can answer ("PowerShell is in NonInteractive mode"). Add, Set and replication now send `-Force`. The removes never
  do: they do not ask while the relationship is healthy, and with the partner unreachable `-Force` would remove the
  relationship or scope on one server only. Such a push now fails with "not in NORMAL state" and changes nothing.
- Failover writes need the DHCP server to reach its partner as you, which NTLM and Kerberos cannot pass on
  (`PermissionDenied`). The new **CredSSP** WinRM authentication makes them work; see the failover page. CredSSP
  hands the password to the server, so it requires HTTPS with certificate verification, unless the credential
  profile sets `allow_insecure_credssp` (for labs).
- An import or push no longer fails when a scope disappears while the server is being read (for example the partner's
  copy, when a failover relationship is removed at that moment): that scope counts as gone, the rest is read. The
  sync run's notes and the job log name each scope left out this way (#58).
- A failover relationship imported with message authentication on made every later push to that server fail: the
  import cannot read the secret, so NetBox had no shared secret profile and each push tried to turn authentication
  off, which Windows refuses. Without a profile NetBox now leaves authentication as Windows has it; a profile still
  turns it on with that secret. Found while taking the documentation screenshots.
- A sync run's page listed every import proposal of every server instead of its own (the filter it used did not
  exist).
- The Kerberos SPN help text read "Defaults to WSMAN/": the host placeholder was dropped as an HTML tag.
- A failover partner Windows names by its computer name now matches a server NetBox knows by IP address, and one it
  names by address matches a server NetBox knows by name (#57). Windows names the partner by its computer name on one
  side of a relationship, so that side's import failed and its pushes saw a partner change Windows cannot make. Test
  connection, imports and pushes now record the DHCP server's computer name, DNS name and addresses (**Reported
  names**, migration 0006, cleared when the host or computer name changes), and partners match on them as well as on
  the host. A name two servers report matches neither, and the error says so.
