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
| Scope (v4/v6) | `Subnet` + one `Pool` | Pool's `IPRange` is the scope start/end. `valid_lifetime` is the lease duration |
| Scope state, type, delay, NAP, etc. | none | Our `WindowsScope` (OneToOne to Subnet) |
| Superscope | `SharedNetwork` | Needs a covering Prefix; import creates it when missing |
| Reservation | `HostReservation` | MAC to `hw_address`, IP to `ipv4_address`. Reservation type in our `WindowsReservation` |
| Exclusion range | none | Our `ExclusionRange` (FK Subnet, start, end) |
| Option definition | `OptionDefinition` | Server-scoped definitions. Vendor class on our side table. Type mapping table needed (Byte, Word, DWord, IPv4Address, String, BinaryData, ...) |
| Option value | `Option` | GFK to server, subnet or reservation. User/vendor class and policy qualifiers in our `WindowsOptionQualifier` |
| User and vendor class | none | Our `WindowsClass`. sys4 `ClientClass` is a Kea expression, so we do not reuse it |
| Policy | none | Our `WindowsPolicy` (server or scope level, conditions stored as structured JSON, ranges, processing order) |
| Failover relationship | none (`DHCPCluster` is only a group) | Our `FailoverRelationship` (partner servers, mode, MCLT, ratio or standby, shared secret reference, scopes) |
| MAC filter (allow/deny) | none | Our `MacFilter` |
| Lease | none | Read only. Shown live on the scope page, optionally cached in `LeaseSnapshot` |

**Naming.** sys4 names are unique per model across all servers, so two servers with a scope called "LAN" would collide. Imported objects are named `<server>/<scope id>` for subnets and `<server>/<scope id>/<ip>` for reservations, with the Windows name kept in `description` (configurable template).

**Core objects.** Import creates the NetBox `Prefix`, `IPRange`, `IPAddress` and `MACAddress` objects sys4 requires, in a VRF chosen per server (default global).

**Known sys4 quirks.** `Option` has no uniqueness rule, so the sync dedupes. Several models validate in `save()`, so the sync saves one object at a time and never uses bulk updates. `available_client_classes` on Subnet and SharedNetwork is broken upstream and must not be called.

## 4. Our models

Branchable (DHCP configuration, reviewed and merged like sys4 objects): `WindowsScope`, `WindowsReservation`, `ExclusionRange`, `WindowsClass`, `WindowsPolicy`, `FailoverRelationship`, `MacFilter`, `WindowsOptionQualifier`.

Not branchable (bookkeeping; listed in branching's `exempt_models` so change control's `protect_main` does not block them):

- `WindowsServer`: OneToOne to `DHCPServer`. Host, transport (winrm or ssh), port, WinRM auth (ntlm, basic, kerberos), HTTPS (default on), TLS verification, Kerberos realm and SPN, SSH host key, optional `computer_name` for remoting, VRF for imports, sync enabled, import schedule, and a credential profile name.
- `SyncState`: per managed Windows object, the last pushed fingerprint. This is what lets the push delete only objects NetBox created, and spot drift.
- `SyncRun`: one row per push or import job with its result, linked to the NetBox `Job`.
- `ImportProposal` and `ImportProposalItem`: the review queue used when branching is off.
- `LeaseSnapshot`: optional lease cache.

**Credentials** never go in the database, the change log or job logs. `WindowsServer` names a credential profile defined in `PLUGINS_CONFIG` (username, password or key, read from environment variables or files), the same way the terraform provider reads them from config and environment. Each profile can list `allowed_hosts` (wildcards allowed); the server form refuses a `host` or `computer_name` outside it, and the connection code checks again before every connection, so editing a server cannot redirect credentials to another machine. Profiles without the list allow any host and are logged as a warning at startup.

## 5. Windows layer (ported from terraform-provider-windowsddi)

Same layering as the provider, so the two stay easy to compare:

```
sync engine  ->  client (one function per cmdlet operation)  ->  psscript (envelope)  ->  runner (WinRM or SSH)
```

- **psscript.** Same contract: each script runs in try/catch and prints one JSON envelope `{ok, data}` or `{ok: false, error}`. User values only enter as a single-quoted JSON literal `$p` with quotes doubled; nothing from NetBox is ever interpolated into PowerShell. Same preamble, including `$PSDefaultParameterValues['*:ErrorAction']='Stop'`, and the same base64 stdin bootstrap.
- **Not found** uses the same rules: `ObjectNotFound`, `DHCP 20005`, `20010`, `20018`, or a null result. `DHCP 20007` (scope has leases) is a real error.
- **Normalisation** is ported as is: scope ID from start and mask, MAC canonical form, lease durations, `InActive`/`Inactive` casing, option values compared hex-or-decimal.
- **runner.** WinRM through pypsrp with a new connection per call, as the Go code learned the hard way with NTLM. SSH through paramiko with pinned host key or known_hosts. Concurrency is capped per job (`max_concurrency`); a cap shared by all workers per server comes with push (milestone 4).
- **New cmdlets** beyond the provider: DHCPv6 scopes, reservations and options, superscopes, option definitions, classes, policies, failover, MAC filters, server settings. These follow the same patterns and get the same fixture tests.

## 6. Push: NetBox to Windows

The push is a **reconcile per server**, not a replay of the change log. For each affected server it reads the Windows state, compares it with NetBox, and applies the difference in dependency order (classes and definitions, then scopes, exclusions, reservations, options, policies, failover). It only deletes Windows objects listed in `SyncState`, so objects created by hand on Windows are left alone (they come back through import instead).

**With branching.** A `post_merge` receiver reads `branch.get_merged_changes()`, keeps the `netbox_dhcp` and plugin changes, works out which servers are affected, and queues one push job per server. Because `post_merge` fires in the RQ worker after commit, nothing is pushed for an aborted merge. `post_revert` does the same, with affected IDs captured in `pre_revert`. Model `post_save` signals are ignored while branching is installed, since they also fire during merges.

**With change control.** Nothing extra: it already refuses merges without an approved change request, so a push only ever follows an approved change.

**Without branching.** `post_save`/`post_delete` queue a push job through `transaction.on_commit`, debounced per server.

**Always available.** A "Sync now" button and a dry-run that shows the planned changes without applying them.

**Failover.** Writes go to one partner, then `Invoke-DhcpServerv4FailoverReplication` copies them to the other, as the provider's design notes describe.

## 7. Import: Windows to NetBox

A scheduled job per server (and a manual button) reads the full Windows state and compares it with NetBox.

**With branching.** When differences are found, the job creates and provisions a new branch named `dhcp-import/<server>/<timestamp>` and writes the differences into it under `activate_branch` with change tracking, taking a snapshot of each object before editing it so conflict detection works. With change control installed it also opens a change request. Merging that branch triggers a push, which finds nothing to do because NetBox now matches Windows.

**Without branching (proposal).** The job writes an `ImportProposal` with one item per difference (create, update or delete, with before and after values). A review page shows the diff; reviewers apply or dismiss items one by one or in bulk. Applying writes to NetBox as the reviewing user, so the change log shows who accepted it. Applying does not trigger a push back, since the objects then match Windows.

**Repeated runs.** If an open import branch or proposal already exists for a server, the job updates it instead of opening another.

**How it is built (milestone 3).**

- **Diff engine.** `sync/diff.py` is pure Python. Both sides become records (kind, Windows key, flat fields) and the
  engine returns create, update and delete changes in dependency order: classes, option definitions, scopes,
  exclusions, reservations, options; deletes in reverse. It does not know which side is Windows, so push reuses it.
  `sync/windows_state.py` (pure) and `sync/netbox_state.py` (Django) produce the records; `sync/mapping.py` writes a
  change into NetBox, one object at a time, with `snapshot()` before each update or delete.
- **What is imported.** IPv4 scopes, exclusion ranges, reservations, user and vendor classes, option definitions and
  option values at server, scope and reservation level, including user class qualifiers. Skipped and logged until
  milestone 5: superscopes, policy-qualified values, and vendor class option definitions and values (sys4 allows one
  `OptionDefinition` per code and server, and Windows ships several vendor classes defining the same codes, so they
  need their own model). Option values containing a comma or surrounding spaces are skipped too: sys4 stores a list
  as one comma-separated string, so they would never compare equal again.
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
  same server and parent. Until push exists (milestone 4), objects created in NetBox show up
  as deletes and are dismissed by the reviewer.
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
- Pages for our models, the review queue and sync runs, under a "Windows DHCP" menu.
- REST and GraphQL for every model, plus actions to trigger push, dry-run and import.
- Built per the netboxlabs plugin skill: `@register_model_view`, `get_model_urls`, Strawberry GraphQL, `@register_filterset`, `PluginMenu`.

## 9. Testing

- **Unit** (`tests/`): psscript rendering and envelope parsing, normalisation, mapping, diff engine, all against recorded JSON fixtures and an in-memory fake server, as in the provider.
- **PowerShell** tests: run the real scripts in `pwsh` against stub cmdlets, reusing the provider's approach.
- **NetBox** (`tests_netbox/`): models, views, API, push and import against the fake server, with and without branching.
- **Lab** acceptance tests against a real Windows server, opt-in through environment variables, scopes in `198.18.0.0/15`.

## 10. Repo setup

Matches `netbox-ipam-treeview-plugin` and `netbox-opentelemetry-plugin`: hatchling `pyproject.toml` with uv, ruff (line length 120), `Makefile` (test, test-netbox, lint, format, dev, seed, dist), `dev/` docker compose stack with seed scripts, `tests/` and `tests_netbox/`, `ci.yml` and `release.yml` with SHA-pinned actions, grouped weekly Dependabot, issue and PR templates, CODEOWNERS, CHANGELOG, Apache-2.0 and `docs/`. The dev stack includes branching and change control so both modes are tested.

## 11. Milestones

1. Repo scaffold, models, connection test button.
2. Windows layer ported, with fixtures and fake server.
3. Import (branch mode and review queue).
4. Push (merge-driven and save-driven), dry-run.
5. Remaining objects: policies, failover, MAC filters, DHCPv6.
6. Lab acceptance tests and first release.

## 12. Open questions

1. **Credentials.** Credential profiles in `PLUGINS_CONFIG` read from environment variables (proposed), or encrypted in the database?
2. **Unmanaged objects.** Leave objects created by hand on Windows alone until imported (proposed), or let push delete anything NetBox does not have?
3. **Leases.** Live view only (proposed), or also keep IP addresses in NetBox updated from leases?
4. **sys4 is beta.** Accept pinning to 0.2.x and following its releases, or also contribute fixes upstream (for example the `available_client_classes` bug)?
