# Reservations through the API

This page creates a DHCP reservation with the NetBox REST API, waits for the plugin to push it to Windows, checks
the result, and deletes it again. It is the API version of [Reservations](reservations.md), which does the same in
the web UI and explains each field.

Everything below was run as shown against the lab used for the other pages: NetBox 4.7 without branching, two
Windows Server 2025 DHCP servers (`dhcp01.example.com` and `dhcp02.example.com`) in a failover relationship, sync
enabled on both, and the scope `198.18.60.0/24` already pushed to `dhcp01`.

## What you need

- An API token that may write (a v2 token is sent as `Authorization: Bearer nbt_...`; a v1 token as
  `Authorization: Token ...`), for a user allowed to add IP addresses, MAC addresses, host reservations and
  Windows reservation settings, and to change the Windows server (for the push action).
- The scope in NetBox: a sys4 subnet with a pool on the DHCP server of a Windows server.
- **Sync enabled** on that Windows server. Then every save queues a push by itself, `push_debounce` seconds (10)
  later, and the push action works. With sync disabled, the push action answers `409 sync is disabled` and nothing
  reaches Windows.

## The calls

A reservation is three NetBox objects, plus one optional object of this plugin:

| Step | Request | Body |
|---|---|---|
| Find the Windows server | `GET /api/plugins/windows-dhcp/windows-servers/?host=dhcp01.example.com` | |
| Find the subnet (the scope) | `GET /api/plugins/netbox-dhcp/subnets/?name=dhcp01/198.18.60.0` | |
| IP address | `POST /api/ipam/ip-addresses/` | `{"address": "198.18.60.23/24", "status": "active"}` |
| MAC address | `POST /api/dcim/mac-addresses/` | `{"mac_address": "02:4E:42:60:00:23"}` |
| Host reservation | `POST /api/plugins/netbox-dhcp/hostreservations/` | `{"name": ..., "description": ..., "subnet": <id>, "hw_address": <id>, "ipv4_address": <id>}` |
| Reservation type (optional) | `POST /api/plugins/windows-dhcp/windows-reservations/` | `{"host_reservation": <id>, "reservation_type": "dhcp"}` |
| Push | `POST /api/plugins/windows-dhcp/windows-servers/<id>/push/` | |
| Follow the push | `GET /api/plugins/windows-dhcp/sync-runs/?windows_server_id=<id>&kind=push&created__gte=<time>` | |

How the fields reach Windows:

- **name**: unique among all host reservations in NetBox, never sent to Windows. Imports name reservations
  `<server>/<scope>/<ip>`; using the same pattern keeps API-made and imported reservations alike.
- **description**: the Windows reservation **name**. **comments**: the Windows description.
- **hw_address**: the client ID. **ipv4_address**: the reserved address, inside the subnet.
- **reservation_type**: `dhcp`, `bootp` or `both`. Without reservation settings, the reservation is pushed as
  `both`, the Windows default.

The same requests with curl, for the host reservation for example (the IDs come from the lookups and the two
POSTs before it):

```bash
curl -s -X POST "$NETBOX_URL/api/plugins/netbox-dhcp/hostreservations/" \
  -H "Authorization: Bearer $NETBOX_TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "dhcp01/198.18.60.0/198.18.60.23", "description": "label-printer01",
       "subnet": 1, "hw_address": 7, "ipv4_address": 9}'
```

## End to end: create

The script below does every step and waits for the push. It needs Python 3.11 or later and `requests`, and reads
`NETBOX_URL` and `NETBOX_TOKEN` from the environment.

```python
"""Create a DHCP reservation through the NetBox REST API and wait until Windows has it."""

import os
import sys
import time
from datetime import UTC, datetime

import requests

NETBOX = os.environ["NETBOX_URL"].rstrip("/") + "/api/"
api = requests.Session()
api.headers["Authorization"] = f"Bearer {os.environ['NETBOX_TOKEN']}"
api.headers["Accept"] = "application/json"

# What to reserve, and where.
WINDOWS_HOST = "dhcp01.example.com"
SUBNET = "dhcp01/198.18.60.0"
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


# 1. Look up the Windows server and the subnet (the Windows scope).
server = one("plugins/windows-dhcp/windows-servers/", host=WINDOWS_HOST)
subnet = one("plugins/netbox-dhcp/subnets/", name=SUBNET)
started = datetime.now(UTC).isoformat()

# 2. The IP address and the MAC address the reservation points at.
ip = call("POST", "ipam/ip-addresses/", json={"address": IP, "status": "active", "description": NAME}).json()
mac = call("POST", "dcim/mac-addresses/", json={"mac_address": MAC, "description": NAME}).json()

# 3. The reservation. Its description becomes the Windows reservation name.
reservation = call(
    "POST",
    "plugins/netbox-dhcp/hostreservations/",
    json={
        "name": f"{SUBNET}/{IP.split('/')[0]}",
        "description": NAME,
        "subnet": subnet["id"],
        "hw_address": mac["id"],
        "ipv4_address": ip["id"],
    },
).json()
print(f"host reservation {reservation['id']}: {reservation['name']}")

# 4. Optional: Windows reservation type (DHCP, BOOTP or both; both when left out).
call(
    "POST",
    "plugins/windows-dhcp/windows-reservations/",
    json={"host_reservation": reservation["id"], "reservation_type": "dhcp"},
)

# 5. Push. With sync enabled the saves above already queued one (409 here just means it is waiting).
r = call("POST", f"plugins/windows-dhcp/windows-servers/{server['id']}/push/")
print("push:", r.status_code, r.json().get("detail", "queued"))
if r.status_code == 409 and "disabled" in r.json()["detail"]:
    sys.exit("sync is disabled for this server: enable it, or push from the server page later")

# 6. Wait for the push to finish and show what it did.
for _ in range(60):
    runs = call(
        "GET",
        "plugins/windows-dhcp/sync-runs/",
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

What it printed:

```text
$ python reserve.py
host reservation 6: dhcp01/198.18.60.0/198.18.60.23
push: 409 a push for this server is already queued
sync run 17: completed
  create reservation 198.18.60.0/198.18.60.23
```

The push action answered `409` because the saves had already queued a push, which is what sync enabled does; the
script then waited for that push. One push carried both the reservation and its type, since the reservation
settings were saved before it ran.

On Windows:

```text
PS> Get-DhcpServerv4Reservation -IPAddress 198.18.60.23 | Format-List

IPAddress   : 198.18.60.23
ClientId    : 02-4e-42-60-00-23
ScopeId     : 198.18.60.0
Name        : label-printer01
Type        : Dhcp
Description :
```

The scope is in a failover relationship with `dhcp01` as primary, so the push also replicated it: `dhcp02` showed
the same reservation without NetBox writing to it (see [Failover](failover.md#5-changes-replicate)).

## Option values on a reservation

!!! warning "Not through the REST API with netbox-plugin-dhcp 0.2.x"
    sys4's option endpoint (`/api/plugins/netbox-dhcp/options/`) refuses `"assigned_object_type":
    "netbox_dhcp.hostreservation"` with `Invalid content type`: its list of allowed parents misspells the host
    reservation model (`hostresevration`,
    [sys4/netbox-plugin-dhcp#69](https://github.com/sys4/netbox-plugin-dhcp/issues/69)). Options on servers and subnets work through the API; for a reservation,
    add the option in the web UI (**Add Object > Option** on the host reservation, see
    [Reservations](reservations.md#reservation-options)) until sys4 fixes it. The plugin pushes reservation options
    however they were created.

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

Deleting the host reservation also deletes its reservation settings and option values. The IP and MAC address
objects stay, unless you delete them as well:

```python
"""Delete a DHCP reservation through the NetBox REST API, and its IP and MAC address objects."""

import os
import sys
import time
from datetime import UTC, datetime

import requests

NETBOX = os.environ["NETBOX_URL"].rstrip("/") + "/api/"
api = requests.Session()
api.headers["Authorization"] = f"Bearer {os.environ['NETBOX_TOKEN']}"
api.headers["Accept"] = "application/json"

WINDOWS_HOST = "dhcp01.example.com"
RESERVATION = "dhcp01/198.18.60.0/198.18.60.23"


def one(path, **params):
    r = api.get(NETBOX + path, params=params)
    r.raise_for_status()
    results = r.json()["results"]
    if len(results) != 1:
        sys.exit(f"{path} {params}: expected one result, got {len(results)}")
    return results[0]


server = one("plugins/windows-dhcp/windows-servers/", host=WINDOWS_HOST)
reservation = one("plugins/netbox-dhcp/hostreservations/", name=RESERVATION)
started = datetime.now(UTC).isoformat()

# Deleting the reservation also deletes its reservation settings and option values.
api.delete(NETBOX + f"plugins/netbox-dhcp/hostreservations/{reservation['id']}/").raise_for_status()
# The IP and MAC address objects are not part of the reservation: delete them too if nothing else uses them.
api.delete(NETBOX + f"ipam/ip-addresses/{reservation['ipv4_address']['id']}/").raise_for_status()
api.delete(NETBOX + f"dcim/mac-addresses/{reservation['hw_address']['id']}/").raise_for_status()

# With sync enabled the delete queued a push; wait for it.
for _ in range(60):
    runs = api.get(
        NETBOX + "plugins/windows-dhcp/sync-runs/",
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
| `400` on the host reservation, `name` | The name is already used by another host reservation |
| `201` for an address outside the subnet | netbox-plugin-dhcp 0.2.x does not check that the IPv4 address is inside the subnet: check it yourself before creating the reservation |
| `409` from `push/`, `already queued` | A push is waiting to run (sync enabled queued it): wait for its sync run |
| `409` from `push/`, `sync is disabled` | Enable sync on the Windows server, or use a dry run |
| `404` from `push/` or `dry-run/` | The token's user may not change this Windows server |
| Sync run `failed` | Its `error` and `summary` say which change Windows refused; see [Troubleshooting](troubleshooting.md#push) |
