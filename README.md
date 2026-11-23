# netbox-windows-dhcp-plugin

A NetBox plugin that models Microsoft Windows DHCP servers in NetBox and keeps both in step, in both directions,
over WinRM or SSH.

**Documentation: https://thomaschristory.github.io/netbox-windows-dhcp-plugin/**

> **Status: 0.7.2.** The plugin no longer needs netbox-plugin-dhcp: it holds the whole Windows
> DHCP configuration in its own models. 0.2.0 is a fresh start from 0.1.x (see the [upgrade
> notes](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/installation/#upgrading-from-01x)). 0.4.0
> reads faster, guards IPAM edits that change Windows, and holds deletes back while a push leaves something out; it
> changes a few defaults and API answers, so read the [upgrade
> notes](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/installation/#upgrading-from-030-to-040) and the
> [changelog](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/changelog/) first. 0.7.0
> stops WinRM from following the worker's proxy variables unless a credential profile opts in (see the [upgrade
> notes](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/installation/#upgrading-from-06x-to-070)). Every v1
> feature is built and tested against an in-memory fake server and PowerShell stubs. The Windows side (scripts,
> transports, sync rules) is that of 0.1.x, apart from option values now being lists; 0.1.0's lab tests ran in full
> against two Windows Server 2022 DHCP servers (import, push, leases and failover, over WinRM HTTP and HTTPS and over
> SSH). 0.2.0 passed the full lab tests (`make test-lab`, `make lab-e2e`) on two Windows Server 2025 servers over WinRM
> HTTP with CredSSP, and 0.4.x's lab tests (`make test-lab`, and `make lab-e2e` on 0.4.1) passed there too. In a
> domain lab (0.5.0), Kerberos, SSH key logons and `-ComputerName` (with CredSSP) passed, and so did a push with
> Kerberos for normal calls and CredSSP only for failover writes (and again with 0.6.0, over HTTPS and HTTP);
> failover writes need CredSSP or SSH with a password
> (see the [limitations](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/limitations/)). 0.7.1 passed
> the domain lab's tests (`make test-lab`, 66 tests, Windows Server 2025); the long SSH script file, the SSH session wait
> and the policy MAC condition rules are not yet checked on Windows Server 2022. Try it on a lab server first.

## Features

- **Import** from Windows, on demand or every `import_interval` minutes. With
  [netbox-branching](https://github.com/netboxlabs/netbox-branching) each import lands in a branch (plus a change
  request with [netbox-change-control](https://github.com/antoinekh/netbox-change-control)); without it, in an import
  proposal whose items you apply or dismiss.
- **Push** to Windows: a reconcile per server when a branch is merged, or a few seconds after a save without
  branching, plus "Sync now" and "Dry run". A push only deletes Windows objects NetBox manages; objects made by hand
  on Windows are left alone until imported.
- IPv4 scopes, superscopes, exclusion ranges, reservations, option definitions and values, user and vendor classes,
  vendor class options, policies, failover relationships (with replication to the partner) and MAC filters, each a
  plugin model. Scopes and reservations point at NetBox prefixes, IP ranges, IP addresses and MAC addresses, so the
  addresses show up in IPAM too. A Windows server can be linked to its device or virtual machine.
- DHCPv6 scopes, reservations (DUID and IAID), user classes and option values.
- A **Leases** tab on the scope page, read live from Windows in a background job.
- WinRM (NTLM, Kerberos, Basic, CredSSP for failover; HTTPS by default) or SSH, as in
  [terraform-provider-windowsddi](https://github.com/thomaschristory/terraform-provider-windowsddi). Credentials
  live in named profiles in the plugin configuration, limited to `allowed_hosts` (and optionally to set transports,
  ports, Kerberos realms, SSH host keys or verified HTTPS), never in the database.
- REST API and GraphQL for every model; all Windows I/O in background jobs.

## Compatibility

| Plugin | NetBox | netbox-plugin-dhcp | netbox-branching | Python |
|---|---|---|---|---|
| 0.2.x to 0.7.x | 4.7 | not used | 1.2.2+ (optional) | 3.12 to 3.14 |
| 0.1.x | 4.7 | 0.2.x (required) | 1.2.2+ (optional) | 3.12 to 3.14 |

`main` is the standalone line (0.2 and later). The 0.1.x line (latest 0.1.4, branch `release/0.1`) keeps getting
bug fixes, but no new features, for installs that stay on netbox-plugin-dhcp: pin `netbox-windows-dhcp-plugin<0.2`
to stay on it.

Windows Server 2016 or later with the DHCP Server role.

## Quick start

```bash
pip install netbox-windows-dhcp-plugin   # pin "<0.2" to stay on the 0.1.x line (netbox-plugin-dhcp)
# extras: [kerberos] for Kerberos (needs system GSSAPI libraries), [branching] for branching and change control
```

```python
PLUGINS = ["netbox_windows_dhcp_plugin"]

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
make dev         # NetBox, branching and change control in docker
make test-netbox # NetBox integration tests inside the dev stack
make docs        # build the documentation site
make dist        # build and check the wheel and sdist
```

See [Development](https://thomaschristory.github.io/netbox-windows-dhcp-plugin/development/) for the lab tests
against a real Windows server.

## License

Apache-2.0, see [LICENSE](https://github.com/thomaschristory/netbox-windows-dhcp-plugin/blob/main/LICENSE).
