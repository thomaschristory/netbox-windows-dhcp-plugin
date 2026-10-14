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
5. **In progress.** Remaining objects end to end, in two PRs.
   - **PR A:** superscopes, policies (and policy-qualified option values), vendor class option definitions and values
     (`WindowsVendorOptionDefinition`, `WindowsVendorOption`), failover relationships (secret from a credential
     profile), MAC filters and list switches (`MacFilterLists`), option values containing commas (Kea's `\,`).
   - **PR B (next):** DHCPv6 scopes, reservations and option values against sys4's standard dhcp6 definitions (v6
     custom definitions, vendor values and exclusions are skipped and logged); live leases on the scope page through
     a job and an HTMX panel, without a cache model.
6. Lab acceptance tests against a real Windows server, then the first release.
