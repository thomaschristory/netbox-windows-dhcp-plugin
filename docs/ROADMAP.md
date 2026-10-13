# Roadmap

Milestones from [DESIGN.md](DESIGN.md#11-milestones).

1. **Done.** Repo scaffold, models, REST and GraphQL API, connection test.
2. **Done.** PowerShell layer ported from terraform-provider-windowsddi, with fake server and pwsh stub tests.
   Cmdlets the provider did not use are marked `# unverified:` in `windows/client.py` until checked on a real server.
3. **Done.** Import from Windows: a branch per import with branching (reused while open, change request submitted
   when change control is installed), an import proposal review queue without it. IPv4 scopes, exclusions,
   reservations, user and vendor classes, option definitions and values. A pure diff engine (`sync/diff.py`) that
   push will reuse. Scheduled by a system job from `import_interval`.
4. Push to Windows: reconcile per server on branch merge, or after saves without branching. Dry-run and "Sync now".
   Cap concurrent PowerShell sessions per server across all workers (a Redis lock), not only per job.
5. Remaining objects end to end: superscopes, policies (and policy-qualified option values), failover, MAC filters,
   DHCPv6, live leases.
6. Lab acceptance tests against a real Windows server, then the first release.
