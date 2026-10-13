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
  option values (server, scope, reservation and class-qualified) become netbox-plugin-dhcp objects plus the
  prefixes, IP ranges, IP addresses and MAC addresses they need, in the server's import VRF. Objects missing on
  Windows are proposed for deletion. Policy-qualified option values are skipped until policies are supported.
- With netbox-branching, each import writes into a branch `dhcp-import/<server>/<timestamp>` (reused while it is
  open) and opens a change request, submitted for review, when netbox-change-control is installed.
- Without branching, each import fills an import proposal. Items are applied or dismissed one by one, in bulk or
  for the whole proposal, from the UI or the REST API; applying writes as the reviewing user and refuses items whose
  NetBox object changed since the import.
- "Import now" button and `POST /api/plugins/windows-dhcp/windows-servers/<id>/import/`; scheduled imports every
  `import_interval` minutes through a system job; `import_user` setting for scheduled imports.
- `WindowsOptionDefinition` keeps the Windows data type and default value.
