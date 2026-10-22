# Installation

## Requirements

- NetBox 4.7.
- [netbox-plugin-dhcp](https://github.com/sys4/netbox-plugin-dhcp) 0.2.x. It is installed as a dependency, but it
  is a NetBox plugin too and must be enabled.
- Python 3.12, 3.13 or 3.14.
- A NetBox background worker (`rqworker`). Every connection to Windows runs in a job.
- PostgreSQL connections from the worker that do not go through a transaction-pooling proxy (pgbouncer in
  transaction mode): the plugin holds a PostgreSQL advisory lock per Windows server while an import or push runs,
  and that lock belongs to the database session.

Optional:

- [netbox-branching](https://github.com/netboxlabs/netbox-branching) 1.2.2 or later: imports become branches and
  pushes follow merges.
- [netbox-change-control](https://github.com/antoinekh/netbox-change-control) 0.6 or later (needs branching): each
  import branch gets a change request, and merges need approval.
- Kerberos: system GSSAPI libraries (for example `libkrb5-dev` and `krb5-user` on Debian or Ubuntu) and a
  `krb5.conf` on the NetBox hosts.

## Install the package

Install it into NetBox's virtual environment, and add it to `local_requirements.txt` so NetBox upgrades keep it:

!!! note "Release candidate"
    Until 0.1.0 is out, PyPI only has release candidates (the latest is 0.1.0rc2), which pip skips by default. Ask for it by
    version (`pip install netbox-windows-dhcp-plugin==0.1.0rc2`, and the same line in `local_requirements.txt`) or
    add `--pre`.

```bash
source /opt/netbox/venv/bin/activate
pip install netbox-windows-dhcp-plugin
echo netbox-windows-dhcp-plugin >> /opt/netbox/local_requirements.txt
```

With Kerberos support:

```bash
pip install "netbox-windows-dhcp-plugin[kerberos]"
```

With netbox-branching and netbox-change-control:

```bash
pip install "netbox-windows-dhcp-plugin[branching]"
```

For the [netbox-docker](https://github.com/netbox-community/netbox-docker) image, add the same names to
`plugin_requirements.txt` and rebuild the image, as its documentation describes.

## Enable it

In `configuration.py`, enable netbox-plugin-dhcp first, then this plugin:

```python
PLUGINS = ["netbox_dhcp", "netbox_windows_dhcp_plugin"]
```

With branching and change control, `netbox_branching` must come last, and NetBox needs its schema-aware database
settings and router:

```python
from netbox_branching.utilities import DynamicSchemaDict

PLUGINS = ["netbox_dhcp", "netbox_windows_dhcp_plugin", "netbox_change_control", "netbox_branching"]
DATABASES = DynamicSchemaDict({"default": {...}})  # your existing database settings
DATABASE_ROUTERS = ["netbox_branching.database.BranchAwareRouter"]
```

The plugin detects branching and change control when NetBox starts. It works the same way without them, with
an [import proposal](import.md#without-branching-import-proposals) queue instead of branches and a push after each save.

Then add at least one credential profile, as described in [Configuration](configuration.md).

## Migrate and restart

```bash
cd /opt/netbox/netbox
python manage.py migrate
sudo systemctl restart netbox netbox-rq
```

Restart the worker as well as the web service: the worker runs every job, and it reads the same configuration.

## Check it

- The navigation menu has a **Windows DHCP** entry.
- The NetBox log has no line saying `credential profiles without allowed_hosts can be sent to any host`. If it has
  one, add `allowed_hosts` to the profiles it lists (see [Configuration](configuration.md#allowed_hosts)).
- **Operations > Jobs** lists a job called "Windows DHCP import scheduler", which NetBox runs every minute and which
  queues the [scheduled imports](import.md#scheduled-imports).

## Upgrading

Install the new version, run `migrate`, and restart NetBox and the worker. Read the [changelog](changelog.md) first:
while the plugin is below 1.0, a minor version can change behaviour.

## Removing it

1. Remove `netbox_windows_dhcp_plugin` from `PLUGINS` and restart NetBox and the worker. Nothing is pushed to or
   removed from Windows.
2. To drop its tables, follow NetBox's guide to removing a plugin. The sys4 objects that imports created (subnets,
   reservations, options) belong to netbox-plugin-dhcp and stay.
