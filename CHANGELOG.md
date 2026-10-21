# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

Pre-release 0.1.0rc1 (2026-10-21) is on PyPI for testing ahead of the lab run; 0.1.0 follows once the lab tests
pass. First release. Every v1 feature is in place and tested against the fake server and PowerShell stubs; the lab tests
against a real Windows server are written (`make test-lab`, `make lab-e2e`) but not run yet.

### Added

Connections and security

- Windows server connection settings linked to netbox-plugin-dhcp servers: WinRM (NTLM, Basic or Kerberos, HTTPS by
  default) or SSH, with an optional `computer_name` for a management host.
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
- `SECURITY.md`: report vulnerabilities through GitHub's private vulnerability reporting.

Development

- `dev/scripts/lab-server-setup.ps1` prepares a Windows Server VM as the lab server (DHCP role, a least-privilege
  account, WinRM over HTTPS).
- Opt-in lab tests against a real Windows DHCP server: `make test-lab` (the client over WinRM, one round trip per
  unverified cmdlet) and `make lab-e2e` (import, push and leases on the dev stack). They only touch `nbwd-test-`
  objects in `198.18.0.0/15` and `2001:db8:4e42::/64`.
