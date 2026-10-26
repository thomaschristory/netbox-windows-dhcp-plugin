# Roadmap

Milestones from [DESIGN.md](DESIGN.md#11-milestones).

1. **Done.** Repo scaffold, models, REST and GraphQL API, connection test.
2. **Done.** PowerShell layer ported from terraform-provider-windowsddi, with fake server and pwsh stub tests.
   Cmdlets the provider did not use are marked `# unverified:` in `windows/client.py` until checked on a real server.
3. **Done.** Import from Windows: a branch per import with branching (reused while open, change request submitted
   when change control is installed), an import proposal review queue without it. IPv4 scopes, exclusions,
   reservations, user and vendor classes, option definitions and values. A pure diff engine (`sync/diff.py`) that
   push will reuse. Scheduled by a system job from `import_interval`.
4. **Done.** Push to Windows: a reconcile per server (`sync/push.py` plans, `sync/windows_writer.py` writes) on
   branch merge or revert, or after saves without branching. Dry-run and "Sync now". Deletes only what sync state
   records as managed. One import or push per server at a time across all workers (a PostgreSQL advisory lock, not
   Redis: import already used one). Failover: replication after pushing to a primary.
5. **Done.** Remaining objects end to end, in two PRs.
   - **PR A (#4, merged):** superscopes, policies (and policy-qualified option values), vendor class option definitions and values
     (`WindowsVendorOptionDefinition`, `WindowsVendorOption`), failover relationships (secret from a credential
     profile), MAC filters and list switches (`MacFilterLists`), option values containing commas (Kea's `\,`).
   - **PR B (#5, merged):** DHCPv6 scopes, reservations, user classes and option values against sys4's standard dhcp6
     definitions (v6 custom definitions, vendor classes and values, and exclusions are skipped, logged and kept);
     IAID on `WindowsReservation`; live leases on a Leases tab of the subnet page through a job and an HTMX panel,
     without a cache model.
6. **In progress.** Lab acceptance tests against a real Windows server, then the first release.
   - **Built:** the opt-in lab tier. `make test-lab` (`tests/lab/`) runs the real client over WinRM with one round
     trip test per `# unverified:` marker and known question; `make lab-e2e` (`dev/scripts/lab_e2e.py`) imports the
     lab server into the dev stack, pushes creates, updates and deletes back for every kind, checks that a second
     push and import change nothing, and reads live leases. Both only touch `nbwd-test-` objects in
     `198.18.0.0/15` and `2001:db8:4e42::/64`, check before every delete and clean up on failure.
   - **First partial lab run (Windows Server 2025, after 0.1.0rc1):** reported as issues #14 to #24. Fixed with
     fake, fixture and strict stub regression tests: the superscope rename parameter (#14), missing objects read as
     empty ones because Windows PowerShell 5.1 prints "nothing" as `{}` (#15), reservation option values sent with
     both the scope (or prefix) and the reserved IP (#20, #17), a DHCPv6 lease state Windows does not have (#22),
     the policy lab test (#18), the DHCPv6 T2 default of 6.4 days (#16), policy criteria compared as Windows
     stores them (#21) and policies pushed in processing order (#24). The markers that run settled are gone.
   - **rc2 acceptance (after 0.1.0rc2):** issues #29 to #33. Fixed: a scope range that moves one end out and the
     other in is set in two steps (narrow to the overlap, then widen; old range restored on failure), since Windows refuses it in one call (DHCP 20023, #30); the policy lab test
     allows option 51 (#29); the missing-module unit test no longer depends on RSAT being absent (#31); an Import
     panel on the server and sync run pages says whether an import is queued, running or done and, with branching,
     links its branch and change request and says main has not changed (#32). Then #36: with branching the Import
button is a menu of import branch, the branch you work in, or main (confirmed, policy and permission checked). And
     #38: the Windows servers list links server names to the Windows server page (saved layouts included). Also #33: hot standby updates no
     longer send `-ServerRole`, which `Set-DhcpServerv4Failover` lacks; a role change is refused.
   - **rc3 acceptance (after 0.1.0rc3, for rc4):** #42, a reservation description update dropped the reservation's
     option values on Windows Server 2025. The client sends the client ID and type (DHCPv6: DUID and IAID) only when
     they change, and the push writer puts back any reservation option value an update removed. Lab tests added for
     both families; whether an identity change alone drops the values stays unverified. #47: the Windows scopes list
     shows each scope's prefix in its own column. #44: the Import panel no longer calls a merged or deleted branch
     open; it says what became of it, reading a deleted branch's last status from the change log, and says "not
     known" rather than "merged" when there is no record.
   - **Full lab run (2026-10-26):** two Windows Server 2025 DHCP servers in a workgroup, WinRM over HTTP. `make
     test-lab` (now with failover tests against a second server) and `make lab-e2e` (now with a failover phase: a
     relationship made in NetBox, pushed, replicated and deleted) pass. Findings fixed: failover writes asked for a
     confirmation (every failover write now sends `-Force`), and failover writes need CredSSP (new WinRM
     authentication choice; NTLM gets `PermissionDenied` on the double hop). The fake now keeps an empty
     relationship when its last scope leaves, as Windows does. Every other marker is settled; three details stay
     unverified (client type or DUID changes alone, policy Set without conditions; Kerberos, Basic, SSH and HTTPS
     were not in the lab).
   - **Release prep:** README, DESIGN section 12 and CHANGELOG made ready for 0.1.0. A documentation site (Zensical,
     `make docs`, published to GitHub Pages from release tags) with installation, Windows preparation, configuration,
     a first-sync walkthrough, import, push, DHCPv6, failover, leases, permissions, API, security, workers,
     troubleshooting and limitations. Version bump, tag and PyPI publishing (trusted publisher and `pypi` environment
     ready) wait for the owner's go.
   - **Fixes from testing 0.1.0rc1:** CSRF tokens on the plugin's buttons (#19); no Edit, Clone or Add buttons
     leading to "None" on read-only pages (#23).
