"""
End-to-end lab run against the dev stack: Windows -> NetBox -> Windows, for every kind, IPv4 and DHCPv6.

Run with ``make lab-e2e`` (inside the netbox container, through ``manage.py shell -c``; exits non-zero on
failure). Needs the lab variables in dev/env/netbox.env (see tests/lab/conftest.py) and the ``lab`` credential
profile of dev/configuration/plugins.py.

Steps:

1. Create one ``nbwd-test-`` object of every kind on the lab server (198.19.x.0/24 and 2001:db8:4e42::/64 only).
2. Import the server into NetBox (review queue path, every item applied).
3. Check: a second import and a dry-run push change nothing.
4. Remove the test objects from Windows, then push: NetBox recreates them (create path).
5. Change the description of every test object that has one in NetBox, then push (update path).
6. Read live leases for a test scope through the Leases tab (LeaseJob in the worker).
7. Delete every test object in NetBox, then push (delete path).
After every push: a second dry-run push and an import change nothing.

Safety: before every real push, a dry run must only touch test objects, decided per kind from the record key
(``is_test``: scope IDs in 198.19.0.0/16, the DHCPv6 test prefix, ``nbwd-test-`` names, ``02-4e-42-`` MACs), and the
changes the push applied are checked again afterwards. Anything else aborts the run before Windows is written.
Objects the lab server already had are imported into NetBox (read only) and never pushed. The test objects are
swept from Windows at the end, pass or fail. The NetBox server record is created with sync_enabled off, so saves
never queue pushes of their own.
"""

import ipaddress
import sys
import time
import traceback

sys.path.insert(0, "/plugin")

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402
from netbox_dhcp.models import DHCPServer  # noqa: E402

from netbox_windows_dhcp_plugin.connection import build_client  # noqa: E402
from netbox_windows_dhcp_plugin.jobs import enqueue_leases  # noqa: E402
from netbox_windows_dhcp_plugin.models import FailoverRelationship, SyncRun, WindowsServer  # noqa: E402
from netbox_windows_dhcp_plugin.sync import importer, netbox_state, pusher, windows_state  # noqa: E402
from netbox_windows_dhcp_plugin.sync.diff import DELETE, UPDATE, Change  # noqa: E402
from netbox_windows_dhcp_plugin.sync.mapping import Writer  # noqa: E402
from netbox_windows_dhcp_plugin.windows import client as wc  # noqa: E402
from tests.lab.labkit import MAC_PREFIX, V6_PREFIX, Lab, lab_env, make_runner  # noqa: E402

SERVER_NAME = "nbwd-lab"
TEST_V4 = ipaddress.ip_network("198.19.0.0/16")
DUID = "00-01-00-01-4e-42-00-00-02-4e-42-00-00-01"


class Abort(Exception):
    pass


def log(msg):
    print(f"[lab-e2e] {msg}", flush=True)


def _in_test_v4(text):
    try:
        return ipaddress.ip_address(text) in TEST_V4
    except ValueError:
        return False


def _ours(name):
    return name.startswith("nbwd-test-")


def is_test(kind, key):
    """
    True only for objects this run creates, decided from the record key alone (never from field values, which can
    mention anything). Used both to pick what to edit or delete in NetBox and to check every planned push.
    """
    key = key.lower()
    head, _, rest = key.partition("/")
    if kind in ("scope", "exclusion", "reservation", "option"):
        # Keys start with the scope ID: 198.19.0.0, 198.19.0.0/..., never "server/..." for these runs.
        return _in_test_v4(head)
    if kind == "policy":
        return _in_test_v4(head) and _ours(rest)
    if kind == "class":
        return head in ("user", "vendor") and _ours(rest)
    if kind == "optiondef":
        # Only vendor class definitions ("<class>/<code>"): plain codes name no test object.
        return bool(rest) and _ours(head)
    if kind == "superscope":
        return _ours(key)
    if kind == "macfilter":
        return key.startswith(MAC_PREFIX)
    if kind in ("scope6", "reservation6", "option6"):
        # v6/2001:db8:4e42::, v6/2001:db8:4e42::/..., never v6/server/...
        return head == "v6" and rest.partition("/")[0] == V6_PREFIX
    if kind == "class6":
        return key.startswith("v6/user/") and _ours(key.removeprefix("v6/user/"))
    # failover, filterlists and anything new: never ours.
    return False


# -- Windows side ----------------------------------------------------------------------------------------------------


def create_windows_objects(lab):
    c = lab.client
    a = lab.scope("nbwd-test-e2e-a", description="e2e")
    b = lab.scope("nbwd-test-e2e-b")
    base = a.scope_id.rsplit(".", 1)[0]
    c.add_reservation(
        wc.Reservation(a.scope_id, f"{base}.20", MAC_PREFIX + "00-00-20", "nbwd-test-e2e-host", "e2e", "Dhcp")
    )
    c.add_exclusion_range(wc.ExclusionRange(a.scope_id, f"{base}.100", f"{base}.110"))
    user = lab.user_class("nbwd-test-e2e-user", "nbwd-e2e-user", "e2e")
    vendor = lab.vendor_class("nbwd-test-e2e-vendor", "nbwd-e2e-vendor")
    oid = lab.free_option_id(vendor.name)
    lab.option_definition(
        wc.OptionDefinition(option_id=oid, name="nbwd-test-e2e-vopt", type="String", vendor_class=vendor.name)
    )
    c.set_option_value(wc.OptionKey(15, a.scope_id), ["e2e.example"])
    c.set_option_value(wc.OptionKey(15, a.scope_id, user_class=user.name), ["user.e2e.example"])
    c.set_option_value(wc.OptionKey(oid, a.scope_id, vendor_class=vendor.name), ["vendor-e2e"])
    c.set_option_value(wc.OptionKey(15, a.scope_id, reserved_ip=f"{base}.20"), ["host.e2e.example"])
    pol = lab.policy(
        wc.Policy(
            name="nbwd-test-e2e-policy",
            scope_id=a.scope_id,
            description="e2e",
            criteria={"mac_address": ["EQ", "02-4e-42-ab*"]},
        )
    )
    c.add_policy_ip_range(wc.PolicyIPRange(a.scope_id, pol.name, f"{base}.50", f"{base}.60"))
    c.set_option_value(wc.OptionKey(15, a.scope_id, policy_name=pol.name), ["policy.e2e.example"])
    lab.superscope(wc.Superscope(name="nbwd-test-e2e-super", scope_ids=[a.scope_id, b.scope_id]))
    lab.mac_filter(wc.MacFilter(mac_address=MAC_PREFIX + "00-00-99", list="Deny", description="nbwd-test-e2e"))
    lab.raw(
        "e2e.lease",
        "Add-DhcpServerv4Lease -ScopeId $p.s -IPAddress $p.ip -ClientId $p.mac -HostName 'nbwd-test-e2e-lease' "
        "-LeaseExpiryTime ((Get-Date).AddDays(1))",
        {"s": a.scope_id, "ip": f"{base}.30", "mac": MAC_PREFIX + "00-00-30"},
    )

    lab.v6_scope("nbwd-test-e2e-v6", description="e2e")
    c.add_v6_reservation(
        wc.ReservationV6(V6_PREFIX, "2001:db8:4e42::20", DUID, 7, "nbwd-test-e2e-v6host", "e2e"),
    )
    v6user = lab.user_class("nbwd-test-e2e-v6user", "nbwd-e2e-v6", "e2e", v6=True)
    c.set_v6_option_value(wc.OptionKeyV6(24, prefix=V6_PREFIX), ["e2e.example"])
    c.set_v6_option_value(wc.OptionKeyV6(24, prefix=V6_PREFIX, user_class=v6user.name), ["user.e2e.example"])
    c.set_v6_option_value(wc.OptionKeyV6(24, prefix=V6_PREFIX, reserved_ip="2001:db8:4e42::20"), ["host.e2e.example"])
    return a


# -- NetBox side -----------------------------------------------------------------------------------------------------


def netbox_server(env):
    dhcp, _ = DHCPServer.objects.get_or_create(name=SERVER_NAME)
    server = WindowsServer.objects.filter(dhcp_server=dhcp).first()
    if server is None:
        server = WindowsServer(dhcp_server=dhcp)
    server.host = env["WINDOWS_DHCP_LAB_HOST"]
    server.port = int(env["WINDOWS_DHCP_LAB_PORT"]) if env.get("WINDOWS_DHCP_LAB_PORT") else None
    server.credential_profile = "lab"
    server.verify_tls = env.get("WINDOWS_DHCP_LAB_INSECURE") != "1"
    server.winrm_auth = env.get("WINDOWS_DHCP_LAB_AUTH", "ntlm").lower()
    server.kerberos_realm = env.get("WINDOWS_DHCP_LAB_REALM", "")
    server.sync_enabled = False
    server.full_clean()
    server.save()
    return server


def read_state(server):
    client = build_client(server)
    try:
        return windows_state.read(client)
    finally:
        client.close()


def import_into_netbox(server):
    state = read_state(server)
    run = SyncRun.objects.create(windows_server=server, kind="import", status="running")
    result = importer.import_to_proposal(server, state, run)
    if result.proposal is None:
        log("import: nothing to apply")
        return 0
    applied, failed = importer.apply_items(list(result.proposal.items.all()))
    failures = [f"{i.action} {i.kind} {i.key}: {i.error}" for i in result.proposal.items.filter(status="failed")]
    log(f"import: {applied} applied, {failed} failed")
    if failures:
        raise Abort("import failures:\n  " + "\n  ".join(failures))
    return applied


def expect_import_noop(server, step):
    changes, _ = importer.compute(server, read_state(server))
    if changes:
        lines = [f"{c.action} {c.kind} {c.key} {c.changed_fields}" for c in changes]
        raise Abort(f"{step}: import still sees {len(changes)} difference(s):\n  " + "\n  ".join(lines))
    log(f"{step}: import sees no difference")


def dry_run(server):
    client = build_client(server)
    try:
        return pusher.run(server, client, dry_run=True)
    finally:
        client.close()


def expect_push_noop(server, step):
    plan = dry_run(server).plan
    if plan.changes:
        lines = [f"{c.action} {c.kind} {c.key} {c.changed_fields}" for c in plan.changes]
        raise Abort(f"{step}: push still plans {len(plan.changes)} change(s):\n  " + "\n  ".join(lines))
    log(f"{step}: push plans nothing")


def guarded_push(server, step, expect_actions):
    plan = dry_run(server).plan
    foreign = [c for c in plan.changes if not is_test(c.kind, c.key)]
    if foreign:
        lines = [f"{c.action} {c.kind} {c.key}" for c in foreign]
        raise Abort(f"{step}: the push would touch objects the lab run did not create:\n  " + "\n  ".join(lines))
    wrong = [c for c in plan.changes if c.action not in expect_actions]
    if wrong:
        raise Abort(f"{step}: unexpected actions: " + ", ".join(f"{c.action} {c.kind} {c.key}" for c in wrong))
    if not plan.changes:
        raise Abort(f"{step}: the push plans nothing")
    for s in plan.skipped:
        log(f"{step}: not pushed: {s.change.action} {s.change.kind} {s.change.key}: {s.reason}")
    client = build_client(server)
    try:
        outcome = pusher.run(server, client)
    finally:
        client.close()
    # The push plans again: check what it really did as well (nothing else edits the dev stack meanwhile).
    done = outcome.result.applied if outcome.result else []
    foreign = [c for c in done if not is_test(c.kind, c.key)]
    if foreign:
        raise Abort(
            f"{step}: the push changed objects outside the test set: " + ", ".join(f"{c.kind} {c.key}" for c in foreign)
        )
    kinds = sorted({c.kind for c in plan.changes})
    log(f"{step}: pushed {len(plan.changes)} change(s) over kinds {kinds}")
    if outcome.failed:
        raise Abort(f"{step}: push errors:\n  " + "\n  ".join(outcome.errors()))
    return kinds


def test_records(server):
    return [r for r in netbox_state.read(server) if is_test(r.kind, r.key)]


def edit_descriptions(server):
    writer = Writer(server)
    n = 0
    for r in test_records(server):
        if "description" not in r.fields:
            continue
        after = dict(r.fields, description="e2e changed")
        writer.apply(Change(UPDATE, r.kind, r.key, before=r.fields, after=after))
        n += 1
    log(f"edit: {n} description(s) changed in NetBox")


def delete_test_objects(server):
    # Children first: the reverse of the order netbox_state reads them in is close, and parents() in push decides
    # the Windows order anyway. Retry a few rounds for objects whose parent went first.
    for _ in range(3):
        records = list(reversed(test_records(server)))
        if not records:
            break
        writer = Writer(server)
        for r in records:
            try:
                writer.apply(Change(DELETE, r.kind, r.key, before=r.fields))
            except Exception as exc:  # noqa: BLE001
                log(f"delete {r.kind} {r.key}: {exc} (retrying)")
    left = test_records(server)
    if left:
        raise Abort("could not delete in NetBox: " + ", ".join(f"{r.kind} {r.key}" for r in left))


def read_leases_tab(server, scope_id, expect_ip):
    # Through the superscope too: a scope in one sits in a shared network, not directly under the server.
    subnet = netbox_state.server_subnets(server.dhcp_server).get(prefix__prefix=f"{scope_id}/24")
    admin = get_user_model().objects.filter(is_superuser=True).first()
    job = enqueue_leases(subnet, admin)
    if job is None:
        raise Abort("leases: no job queued")
    for _ in range(120):
        job.refresh_from_db()
        if job.status in ("completed", "failed", "errored"):
            break
        time.sleep(1)
    if job.status != "completed":
        raise Abort(f"leases: job {job.pk} ended {job.status}: {job.data}")
    ips = [row.get("ip_address") for row in job.data.get("leases", [])]
    if expect_ip not in ips:
        raise Abort(f"leases: {expect_ip} not in {ips}")
    web = Client()
    web.force_login(admin)
    page = web.get(reverse("plugins:netbox_dhcp:subnet_windows_leases", kwargs={"pk": subnet.pk}))
    if page.status_code != 200 or expect_ip not in page.content.decode():
        raise Abort(f"leases: tab returned {page.status_code} without {expect_ip}")
    log(f"leases: {len(ips)} lease(s) read, {expect_ip} shown on the Leases tab")


# -- main ------------------------------------------------------------------------------------------------------------


def main():
    env = lab_env()
    for key in ("WINDOWS_DHCP_LAB_HOST", "WINDOWS_DHCP_USERNAME", "WINDOWS_DHCP_PASSWORD"):
        if not env.get(key):
            raise Abort(f"{key} is not set (dev/env/netbox.env)")
    lab = Lab(wc.DhcpClient(make_runner(env)))
    leftovers = lab.sweep()
    if leftovers:
        raise Abort(f"sweep: {leftovers}")
    server = netbox_server(env)
    if FailoverRelationship.objects.filter(primary_server=server.dhcp_server).exists():
        # A push to a failover primary replicates whole relationships to the partner, outside the plan the guard
        # checks. The lab is a single server; refuse rather than guess.
        raise Abort("the lab server is a failover primary in NetBox: not supported by this script")
    ok = False
    try:
        scope_a = create_windows_objects(lab)
        lab._cleanups.clear()  # from here on the push owns the objects; the final sweep removes what is left
        log("1. created the test objects on Windows")

        import_into_netbox(server)
        expect_import_noop(server, "2. after import")
        expect_push_noop(server, "2. after import")

        errors = lab.sweep()
        if errors:
            raise Abort(f"sweep: {errors}")
        log("3. removed the test objects from Windows")
        guarded_push(server, "3. push creates", {"create"})
        expect_push_noop(server, "3. after create")
        expect_import_noop(server, "3. after create")

        edit_descriptions(server)
        guarded_push(server, "4. push updates", {"update"})
        expect_push_noop(server, "4. after update")
        expect_import_noop(server, "4. after update")

        # The sweep removed the lease with its scope; add it again for the Leases tab.
        base = scope_a.scope_id.rsplit(".", 1)[0]
        lab.raw(
            "e2e.lease",
            "Add-DhcpServerv4Lease -ScopeId $p.s -IPAddress $p.ip -ClientId $p.mac -HostName 'nbwd-test-e2e-lease' "
            "-LeaseExpiryTime ((Get-Date).AddDays(1))",
            {"s": scope_a.scope_id, "ip": f"{base}.30", "mac": MAC_PREFIX + "00-00-30"},
        )
        read_leases_tab(server, scope_a.scope_id, f"{base}.30")

        delete_test_objects(server)
        # The lease keeps the scope from being deleted without force (DHCP 20007): drop it first.
        lab.raw("e2e.unlease", "Remove-DhcpServerv4Lease -IPAddress $p.ip", {"ip": f"{base}.30"})
        guarded_push(server, "5. push deletes", {"delete"})
        expect_push_noop(server, "5. after delete")
        expect_import_noop(server, "5. after delete")
        left = [s.scope_id for s in lab.client.list_scopes() if s.name.startswith("nbwd-test-")]
        if left:
            raise Abort(f"5. scopes still on Windows after the delete push: {left}")
        ok = True
    finally:
        errors = lab.sweep()
        if errors:
            log(f"final sweep errors: {errors}")
        log("PASSED" if ok and not errors else "FAILED")
    return ok and not errors


try:
    passed = main()
except Abort as exc:
    log(f"ABORTED: {exc}")
    log("FAILED")
    passed = False
except Exception:
    traceback.print_exc()
    log("FAILED")
    passed = False
sys.exit(0 if passed else 1)
