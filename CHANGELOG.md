# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Windows server connection settings linked to netbox-plugin-dhcp servers, with WinRM (NTLM, Basic, Kerberos) and
  SSH transports and credential profiles in `PLUGINS_CONFIG`.
- `allowed_hosts` on credential profiles limits which hosts their credentials can be sent to. Checked when a Windows
  server is saved and again before every connection. Basic authentication requires HTTPS with certificate checks.
- SSH always verifies the host key (pinned key or the worker's known_hosts).
- Models for Windows scope and reservation settings, exclusion ranges, classes, option definitions and qualifiers,
  policies, failover relationships and MAC filters.
- Sync bookkeeping models: sync state, sync runs and import proposals.
- Connection test button, background job and REST action.
- REST API and GraphQL for every model.
- netbox-branching and netbox-change-control integration: bookkeeping models are not branched.
- PowerShell layer ported from terraform-provider-windowsddi, with a fake server for tests.
- Import from Windows: IPv4 scopes, exclusion ranges, reservations, user and vendor classes, option definitions and
  option values (server, scope, reservation and user-class qualified) become netbox-plugin-dhcp objects plus the
  prefixes, IP ranges, IP addresses and MAC addresses they need, in the server's import VRF. Objects missing on
  Windows are proposed for deletion. Policy and vendor class option values, vendor class option definitions and
  option values containing commas are skipped (and logged) for now.
- With netbox-branching, each import writes into a branch `dhcp-import/<server>/<timestamp>` (reused while it is
  open) and opens a change request, submitted for review, when netbox-change-control is installed.
- Without branching, each import fills an import proposal. Items are applied or dismissed one by one, in bulk or
  for the whole proposal, from the UI or the REST API; applying writes as the reviewing user and refuses items whose
  NetBox object changed since the import.
- "Import now" button and `POST /api/plugins/windows-dhcp/windows-servers/<id>/import/`; scheduled imports every
  `import_interval` minutes through a system job; `import_user` setting for scheduled imports. One import per server
  runs at a time.
- `WindowsOptionDefinition` keeps the Windows data type and default value.
- Windows class names are matched without case, in import and push alike.
- Push to Windows: a reconcile per server that creates, updates and deletes classes, option definitions, IPv4
  scopes, exclusion ranges, reservations and option values (server, scope, reservation and user-class qualified) so
  Windows matches NetBox, in dependency order. Only objects recorded in sync state are deleted: objects created by
  hand on Windows are left alone, and so are managed objects changed on Windows since the last push and objects
  whose removal would take unmanaged ones with them (including vendor class and policy option values and scope
  policies the push does not handle yet). Objects already identical on both sides are recorded as managed by the
  next push, and applying an import proposal item records it straight away. NetBox data Windows cannot hold (a scope
  without a pool, a reservation without a MAC address, options 1 and 51, values of codes Windows does not define) is
  reported in the sync run instead of pushed. A scope subnet mask or an option definition type is never changed
  in place (that would need a remove and re-add), and a scope with leases is never force-removed.
- With netbox-branching, merging or reverting a branch queues a push for each Windows server it touched. Other saves
  and deletes (all of them without branching, direct edits of main with it) queue one push per server after the
  transaction commits, `push_debounce` seconds later; moving an object between servers pushes both. Applying an
  import proposal queues no push.
- "Sync now" and "Dry run" buttons, and `POST .../windows-servers/<id>/push/` and `.../dry-run/`. A dry run records
  the planned changes in its sync run summary. `sync_enabled = False` stops pushes (dry runs still work).
- One import or push per server runs at a time, across all workers; a push that finds its server busy tries again a
  minute later.
- After a push to the primary of a failover relationship, the changed scopes are replicated to the partner. Pushes
  to a partner leave the relationship's scopes to the primary.
