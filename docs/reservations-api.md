# Reservations through the API

This page creates a DHCP reservation with the NetBox REST API, gives it an option value, waits for the plugin to
push it to Windows, checks the result, and deletes it again. It is the API version of
[Reservations](reservations.md), which does the same in the web UI and explains each field.

The lab is the one used for the other pages: NetBox 4.7 without branching, two Windows Server 2025 DHCP servers
(`dhcp01.example.com` and `dhcp02.example.com`, known to NetBox as `dhcp01` and `dhcp02`) in a failover
relationship, sync enabled on both, and the scope `198.18.60.0/24` (`VLAN60-Office`) already pushed to `dhcp01`.

## What you need

- An API token that may write (a v2 token is sent as `Authorization: Bearer nbt_...`; a v1 token as
  `Authorization: Token ...`), for a user allowed to add IP addresses, MAC addresses, reservations and option
  values, and to change the Windows server (for the push action).
- The scope in NetBox, on a Windows server.
- **Sync enabled** on that Windows server. Then every save queues a push by itself, `push_debounce` seconds (10)
  later, and the push action works. With sync disabled, the push action answers `409 sync is disabled` and nothing
  reaches Windows.

## The calls

A reservation is three NetBox objects (an IP address, a MAC address and the reservation), plus its option values.
All plugin endpoints are under `/api/plugins/windows-dhcp/`:

| Step | Request | Body |
|---|---|---|
| Find the Windows server | `GET /api/plugins/windows-dhcp/windows-servers/?name=dhcp01` | |
| Find the scope | `GET /api/plugins/windows-dhcp/scopes/?server_id=<id>&name=VLAN60-Office` | |
| IP address | `POST /api/ipam/ip-addresses/` | `{"address": "198.18.60.23/24", "status": "active"}` |
| MAC address | `POST /api/dcim/mac-addresses/` | `{"mac_address": "02:4E:42:60:00:23"}` |
| Reservation | `POST /api/plugins/windows-dhcp/reservations/` | `{"scope": <id>, "ip_address": <id>, "mac_address": <id>, "name": "label-printer01", "reservation_type": "dhcp"}` |
| Option value on the reservation | `POST /api/plugins/windows-dhcp/option-values/` | `{"server": <id>, "reservation": <id>, "family": 4, "code": 15, "value": ["print.example.com"]}` |
| Push | `POST /api/plugins/windows-dhcp/windows-servers/<id>/push/` | |
| Follow the push | `GET /api/plugins/windows-dhcp/sync-runs/?windows_server_id=<id>&kind=push&created__gte=<time>` | |

How the fields reach Windows:

- **name**: the Windows reservation **name**. **description**: the Windows description.
- **mac_address**: the client ID. For a client identified by something else, leave it out and set **client_id**.
- **ip_address**: the reserved address, inside the scope's prefix.
- **reservation_type**: `dhcp`, `bootp` or `both`. Left out, it is `both`, the Windows default.
- An option value names its option by **code** and holds its **value** as a list of strings, one per Windows value
  (`["10.1.0.53", "10.1.0.54"]` for two DNS servers). Setting **reservation** makes it a reservation value; its scope
  is filled in from the reservation.

The same request with curl, for the reservation for example (the IDs come from the lookups and the two POSTs before
it):

```bash
curl -s -X POST "$NETBOX_URL/api/plugins/windows-dhcp/reservations/" \
  -H "Authorization: Bearer $NETBOX_TOKEN" -H "Content-Type: application/json" \
  -d '{"scope": 1, "ip_address": 9, "mac_address": 7, "name": "label-printer01", "reservation_type": "dhcp"}'
```

## End to end: create

The script below does every step and waits for the push. It needs Python 3.11 or later and `requests`, and reads
`NETBOX_URL` and `NETBOX_TOKEN` from the environment.

```python
"""Create a DHCP reservation with an option value through the NetBox REST API and wait until Windows has it."""

import os
import sys
import time
from datetime import UTC, datetime

import requests

NETBOX = os.environ["NETBOX_URL"].rstrip("/") + "/api/"
PLUGIN = "plugins/windows-dhcp/"
api = requests.Session()
api.headers["Authorization"] = f"Bearer {os.environ['NETBOX_TOKEN']}"
api.headers["Accept"] = "application/json"

# What to reserve, and where.
SERVER = "dhcp01"
SCOPE = "VLAN60-Office"
IP = "198.18.60.23/24"
MAC = "02:4E:42:60:00:23"
NAME = "label-printer01"


def call(method, path, **kwargs):
    r = api.request(method, NETBOX + path, **kwargs)
    if r.status_code >= 400 and r.status_code != 409:
        sys.exit(f"{method} {path}: {r.status_code} {r.text}")
    return r


def one(path, **params):
    results = call("GET", path, params=params).json()["results"]
    if len(results) != 1:
        sys.exit(f"{path} {params}: expected one result, got {len(results)}")
    return results[0]


# 1. Look up the Windows server and the scope.
server = one(PLUGIN + "windows-servers/", name=SERVER)
scope = one(PLUGIN + "scopes/", server_id=server["id"], name=SCOPE)
started = datetime.now(UTC).isoformat()

# 2. The IP address and the MAC address the reservation points at.
ip = call("POST", "ipam/ip-addresses/", json={"address": IP, "status": "active", "description": NAME}).json()
mac = call("POST", "dcim/mac-addresses/", json={"mac_address": MAC, "description": NAME}).json()

# 3. The reservation. Its name is the Windows reservation name; the type is optional (both when left out).
reservation = call(
    "POST",
    PLUGIN + "reservations/",
    json={
        "scope": scope["id"],
        "ip_address": ip["id"],
        "mac_address": mac["id"],
        "name": NAME,
        "reservation_type": "dhcp",
    },
).json()
print(f"reservation {reservation['id']}: {reservation['display']}")

# 4. An option value on the reservation: option 15 (DNS domain name). Values are lists of strings.
call(
    "POST",
    PLUGIN + "option-values/",
    json={
        "server": server["id"],
        "reservation": reservation["id"],
        "family": 4,
        "code": 15,
        "value": ["print.example.com"],
    },
)

# 5. Push. With sync enabled the saves above already queued one (409 here just means it is waiting).
r = call("POST", PLUGIN + f"windows-servers/{server['id']}/push/")
print("push:", r.status_code, r.json().get("detail", "queued"))
if r.status_code == 409 and "disabled" in r.json()["detail"]:
    sys.exit("sync is disabled for this server: enable it, or push from the server page later")

# 6. Wait for the push to finish and show what it did.
for _ in range(60):
    runs = call(
        "GET",
        PLUGIN + "sync-runs/",
        params={"windows_server_id": server["id"], "kind": "push", "created__gte": started, "ordering": "-id"},
    ).json()["results"]
    done = [run for run in runs if run["status"]["value"] in ("completed", "failed")]
    if done:
        break
    time.sleep(5)
else:
    sys.exit("no push finished within 5 minutes")

run = done[0]
print(f"sync run {run['id']}: {run['status']['value']}")
for change in run["summary"].get("planned", []):
    print(f"  {change['action']} {change['kind']} {change['key']}")
if run["error"]:
    print(run["error"])
```

What it prints:

```text
$ python reserve.py
reservation 6: 198.18.60.23
push: 409 a push for this server is already queued
sync run 17: completed
  create reservation 198.18.60.0/198.18.60.23
  create option 198.18.60.0/198.18.60.23/15
```

The push action answered `409` because the saves had already queued a push, which is what sync enabled does; the
script then waited for that push. One push carried the reservation and its option value, since both were saved
before it ran.

On Windows:

```text
PS> Get-DhcpServerv4Reservation -IPAddress 198.18.60.23 | Format-List

IPAddress   : 198.18.60.23
ClientId    : 02-4e-42-60-00-23
ScopeId     : 198.18.60.0
Name        : label-printer01
Type        : Dhcp
Description :

PS> Get-DhcpServerv4OptionValue -ReservedIP 198.18.60.23

OptionId   Name            Type       Value                VendorClass     UserClass       PolicyName
--------   ----            ----       -----                -----------     ---------       ----------
15         DNS Domain Name String     {print.example.com}
```

The scope is in a failover relationship with `dhcp01` as primary, so the push also replicated it: `dhcp02` shows
the same reservation without NetBox writing to it (see [Failover](failover.md#5-changes-replicate)).

## Option values at other levels

The same endpoint sets server and scope values: leave out `reservation` and `scope` for a server value, set `scope`
for a scope value. A value can also be restricted to a user class (`user_class`), a vendor class (`vendor_class`, for
an option that class defines) or, for IPv4 server and scope values, a policy (`policy`). For example the routers of
the scope:

```json
{"server": 1, "scope": 1, "family": 4, "code": 3, "value": ["198.18.60.1"]}
```

List a reservation's values with `GET /api/plugins/windows-dhcp/option-values/?reservation_id=<id>`, or a scope's
with `?scope_id=<id>` (which includes the values of its reservations; add `&level=scope` for the scope's own).

## Check without writing: dry run

A dry run plans a push and writes nothing to Windows. It works with sync disabled too, so it is the way to see
what a push would do before turning sync on:

```text
POST /api/plugins/windows-dhcp/windows-servers/1/dry-run/
202 {"id": 201, "name": "Windows DHCP dry run", "status": {"value": "pending", ...}, ...}

GET /api/core/jobs/201/                 (until status is completed)
GET /api/plugins/windows-dhcp/sync-runs/?windows_server_id=1&kind=dry_run&ordering=-id&limit=1
{"id": 15, "kind": {"value": "dry_run"}, "status": {"value": "completed"},
 "job": {"url": ".../api/core/jobs/201/", ...},
 "summary": {"changes": 0, "planned": [], "unmanaged": 15, ...}, ...}
```

The sync run's `job` is the job the action returned (match it by URL). `changes: 0` means NetBox and Windows
agree; otherwise `planned` lists each create, update and delete the push would make, with before and after values.

## End to end: delete

Deleting the reservation also deletes its option values. The IP and MAC address objects stay, unless you delete
them as well (after the reservation: they cannot be deleted while it points at them):

```python
"""Delete a DHCP reservation through the NetBox REST API, and its IP and MAC address objects."""

import os
import sys
import time
from datetime import UTC, datetime

import requests

NETBOX = os.environ["NETBOX_URL"].rstrip("/") + "/api/"
PLUGIN = "plugins/windows-dhcp/"
api = requests.Session()
api.headers["Authorization"] = f"Bearer {os.environ['NETBOX_TOKEN']}"
api.headers["Accept"] = "application/json"

SERVER = "dhcp01"
SCOPE = "VLAN60-Office"
NAME = "label-printer01"


def one(path, **params):
    r = api.get(NETBOX + path, params=params)
    r.raise_for_status()
    results = r.json()["results"]
    if len(results) != 1:
        sys.exit(f"{path} {params}: expected one result, got {len(results)}")
    return results[0]


server = one(PLUGIN + "windows-servers/", name=SERVER)
scope = one(PLUGIN + "scopes/", server_id=server["id"], name=SCOPE)
reservation = one(PLUGIN + "reservations/", scope_id=scope["id"], name=NAME)
started = datetime.now(UTC).isoformat()

# Deleting the reservation also deletes its option values.
api.delete(NETBOX + PLUGIN + f"reservations/{reservation['id']}/").raise_for_status()
# The IP and MAC address objects are not part of the reservation: delete them too if nothing else uses them.
api.delete(NETBOX + f"ipam/ip-addresses/{reservation['ip_address']['id']}/").raise_for_status()
api.delete(NETBOX + f"dcim/mac-addresses/{reservation['mac_address']['id']}/").raise_for_status()

# With sync enabled the delete queued a push; wait for it.
for _ in range(60):
    runs = api.get(
        NETBOX + PLUGIN + "sync-runs/",
        params={"windows_server_id": server["id"], "kind": "push", "created__gte": started, "ordering": "-id"},
    ).json()["results"]
    done = [run for run in runs if run["status"]["value"] in ("completed", "failed")]
    if done:
        break
    time.sleep(5)
else:
    sys.exit("no push finished within 5 minutes")

run = done[0]
print(f"sync run {run['id']}: {run['status']['value']}")
for change in run["summary"].get("planned", []):
    print(f"  {change['action']} {change['kind']} {change['key']}")
```

```text
$ python release.py
sync run 18: completed
  delete option 198.18.60.0/198.18.60.23/15
  delete reservation 198.18.60.0/198.18.60.23
```

Only reservations NetBox manages are deleted from Windows; one made by hand on Windows stays (see
[Reservations](reservations.md#delete-a-reservation)).

## With netbox-branching

Send the same requests in a branch by adding the header `X-NetBox-Branch: <branch schema ID>`. Saves in a branch do
not push; the push runs when the branch is merged (with change control, after approval), and its sync run then
records the branch name. Follow it the same way, with `created__gte` set to the merge time. See
[Push to Windows](push.md#when-a-push-runs).

## Errors you may meet

| Answer | Meaning |
|---|---|
| `400` on the reservation, `ip_address` | The address is outside the scope's prefix |
| `400` on the reservation | The scope already has a reservation for that address, a MAC address and a client ID together, or DUID and IAID on an IPv4 reservation |
| `400` on the option value | The value is not a non-empty list of strings, the scope, reservation or class belongs to another server, the family does not match the scope or the class, the reservation is in another scope than the one given, the policy does not fit (with a user class, on a reservation, or from another server or scope), or the server already holds a value for that code at that level (with the same classes and policy) |
| `409` from `push/`, `already queued` | A push is waiting to run (sync enabled queued it): wait for its sync run |
| `409` from `push/`, `sync is disabled` | Enable sync on the Windows server, or use a dry run |
| `404` from `push/` or `dry-run/` | The token's user may not change this Windows server |
| Sync run `failed` | Its `error` and `summary` say which change Windows refused; see [Troubleshooting](troubleshooting.md#push) |
