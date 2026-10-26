# netbox-windows-dhcp-plugin

A NetBox plugin that keeps Microsoft Windows DHCP servers in step with the data modelled by
[netbox-plugin-dhcp](https://github.com/sys4/netbox-plugin-dhcp), in both directions, over WinRM or SSH.

**Documentation: https://thomaschristory.github.io/netbox-windows-dhcp-plugin/**

> **Status: pre-release.** Every v1 feature is built and tested against an in-memory fake server and PowerShell
> stubs, and the lab tests have run in full against two Windows Server 2025 DHCP servers (import, push, leases and
> failover). A few details are still marked `# unverified:` in the code (see the
> [limitations](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/limitations/)). Try it on a lab server
> first.

## Features

- **Import** from Windows, on demand or every `import_interval` minutes. With
  [netbox-branching](https://github.com/netboxlabs/netbox-branching) each import lands in a branch (plus a change
  request with [netbox-change-control](https://github.com/antoinekh/netbox-change-control)); without it, in an import
  proposal whose items you apply or dismiss.
- **Push** to Windows: a reconcile per server when a branch is merged, or a few seconds after a save without
  branching, plus "Sync now" and "Dry run". A push only deletes Windows objects NetBox manages; objects made by hand
  on Windows are left alone until imported.
- IPv4 scopes, superscopes, exclusion ranges, reservations, option definitions and values, user and vendor classes,
  vendor class options, policies, failover relationships (with replication to the partner) and MAC filters.
- DHCPv6 scopes, reservations (DUID and IAID), user classes and option values.
- A **Leases** tab on the subnet page, read live from Windows in a background job.
- WinRM (NTLM, Kerberos, Basic, CredSSP for failover; HTTPS by default) or SSH, as in
  [terraform-provider-windowsddi](https://github.com/thomaschristory/terraform-provider-windowsddi). Credentials
  live in named profiles in the plugin configuration, limited to `allowed_hosts`, never in the database.
- REST API and GraphQL for every model; all Windows I/O in background jobs.

## Compatibility

| Plugin | NetBox | netbox-plugin-dhcp | netbox-branching | Python |
|---|---|---|---|---|
| 0.1.x | 4.7 | 0.2.x | 1.2.2+ (optional) | 3.12 to 3.14 |

Windows Server 2016 or later with the DHCP Server role.

## Quick start

```bash
pip install --pre netbox-windows-dhcp-plugin   # or ==0.1.0rc5; --pre is needed until 0.1.0 is out
# extras: [kerberos] for Kerberos (needs system GSSAPI libraries), [branching] for branching and change control
```

```python
PLUGINS = ["netbox_dhcp", "netbox_windows_dhcp_plugin"]

PLUGINS_CONFIG = {
    "netbox_windows_dhcp_plugin": {
        "credentials": {
            "dhcp-admin": {
                "username": "CORP\\svc-netbox-dhcp",
                "password_env": "DHCP_ADMIN_PASSWORD",
                "allowed_hosts": ["dhcp*.corp.example"],
            },
        },
    },
}
```

Run `python manage.py migrate`, restart NetBox and its worker, then add a Windows server under **Windows DHCP >
Windows servers**, click **Test connection** and **Import now** (**Import** with netbox-branching). The
[first sync](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/quickstart/) walkthrough covers the rest.

## Documentation

- [Installation](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/installation/) and
  [preparing Windows](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/windows-setup/)
- [Configuration](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/configuration/) and
  [security](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/security/)
- [How objects map](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/mapping/),
  [import](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/import/) and
  [push](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/push/)
- [Permissions](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/permissions/),
  [REST API and GraphQL](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/api/),
  [troubleshooting](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/troubleshooting/)
- [Design](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/blob/main/docs/DESIGN.md) and
  [changelog](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/blob/main/CHANGELOG.md)

The same pages are in the [`docs/`](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/tree/main/docs)
folder of the repository.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
make lint        # ruff check and format check
make test        # unit tests, no NetBox needed
make dev         # NetBox, netbox-plugin-dhcp, branching and change control in docker
make test-netbox # NetBox integration tests inside the dev stack
make docs        # build the documentation site
make dist        # build and check the wheel and sdist
```

See [Development](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/development/) for the lab tests
against a real Windows server.

## License

Apache-2.0, see [LICENSE](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/blob/main/LICENSE).
