# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Windows server connection settings linked to netbox-plugin-dhcp servers, with WinRM (NTLM, Basic, Kerberos) and
  SSH transports and credential profiles in `PLUGINS_CONFIG`.
- `allowed_hosts` on credential profiles limits which hosts their credentials can be sent to. Checked when a Windows
  server is saved and again before every connection. Basic authentication requires HTTPS with certificate checks.
- Models for Windows scope and reservation settings, exclusion ranges, classes, option definitions and qualifiers,
  policies, failover relationships and MAC filters.
- Sync bookkeeping models: sync state, sync runs and import proposals.
- Connection test button, background job and REST action.
- REST API and GraphQL for every model.
- netbox-branching and netbox-change-control integration: bookkeeping models are not branched.
- PowerShell layer ported from terraform-provider-windowsddi, with a fake server for tests.
