"""
Push one Windows server: read Windows and NetBox, plan with ``push.plan``, apply with ``WindowsWriter``, then record
what NetBox now manages in ``SyncState``. Runs in the push job (an RQ worker), never in a request.
"""

import logging
from dataclasses import dataclass, field

from django.db import transaction
from netbox_dhcp.models import Subnet

from ..models import FailoverRelationship, SyncState
from ..windows.psscript import WindowsLayerError
from . import netbox_state, push, windows_state
from .windows_writer import WindowsWriter, option_target

log = logging.getLogger(__name__)

__all__ = ("PushOutcome", "run")

REPLICATED = "this scope is in failover relationship {name}, whose primary is {primary}: push to that server instead"


@dataclass
class PushOutcome:
    plan: push.Plan
    result: push.Result | None = None
    notes: list[str] = field(default_factory=list)
    replication_errors: list[str] = field(default_factory=list)

    @property
    def failed(self):
        return bool(self.replication_errors) or bool(self.result and self.result.failed)

    def summary(self):
        out = self.plan.summary()
        if self.result is not None:
            out.update(self.result.summary())
        if self.notes:
            out["notes"] = self.notes
        if self.replication_errors:
            out["replication_errors"] = self.replication_errors
        return out

    def errors(self):
        failed = self.result.failed if self.result else []
        lines = [f"{c.action} {c.kind} {c.key}: {msg}" for c, msg in failed]
        return lines + self.replication_errors


def managed(windows_server):
    return {(s.kind, s.key): s.fingerprint for s in SyncState.objects.filter(windows_server=windows_server)}


def _scope_of(change):
    """The Windows scope a change lives in, or "" for server-level objects."""
    fields = change.after or change.before or {}
    if change.kind == "scope":
        return change.key
    if change.kind in ("exclusion", "reservation"):
        return fields.get("scope_id", "")
    if change.kind == "option":
        return option_target(fields.get("parent", "server"))[0]
    return ""


def _relationship_scopes(relationship):
    subnets = Subnet.objects.filter(pk__in=relationship.subnets.values("pk")).select_related("prefix")
    return {netbox_state.scope_id_of(s) for s in subnets}


def _hold_partner_scopes(windows_server, plan, logger):
    """
    A failover partner gets its scopes from the primary through replication. Writing them on the partner as well
    would be overwritten at the next replication, so leave them to the primary.
    """
    dhcp = windows_server.dhcp_server
    for relationship in FailoverRelationship.objects.filter(partner_server=dhcp).select_related("primary_server"):
        scopes = _relationship_scopes(relationship)
        reason = REPLICATED.format(name=relationship.name, primary=relationship.primary_server)
        kept = []
        for change in plan.changes:
            if _scope_of(change) in scopes:
                plan.skipped.append(push.Skip(change, reason))
            else:
                kept.append(change)
        if len(kept) != len(plan.changes):
            logger.info(
                f"Left {len(plan.changes) - len(kept)} change(s) in failover relationship {relationship.name} to "
                f"its primary, {relationship.primary_server}"
            )
        plan.changes = kept


def _replicate(windows_server, client, applied, logger):
    """After writing to a failover primary, copy the changed scopes to the partner. Returns error lines."""
    if not applied:
        return []
    touched = {_scope_of(c) for c in applied}
    server_level = "" in touched
    errors = []
    for relationship in FailoverRelationship.objects.filter(primary_server=windows_server.dhcp_server):
        scopes = _relationship_scopes(relationship)
        changed = sorted(scopes if server_level else touched & scopes)
        if not changed:
            continue
        try:
            # unverified: replication of the listed scopes copies their exclusions, reservations and options.
            client.replicate_failover(name=relationship.name, scope_ids=changed)
        except WindowsLayerError as exc:
            errors.append(f"failover replication {relationship.name}: {exc}")
        else:
            logger.info(f"Replicated {len(changed)} scope(s) to the partner of {relationship.name}")
    return errors


@transaction.atomic
def _save_state(windows_server, result):
    if result.removals:
        for kind, key in result.removals:
            SyncState.objects.filter(windows_server=windows_server, kind=kind, key=key).delete()
    existing = {(s.kind, s.key): s for s in SyncState.objects.filter(windows_server=windows_server)}
    for (kind, key), fp in result.upserts.items():
        row = existing.get((kind, key))
        if row is None:
            SyncState.objects.create(windows_server=windows_server, kind=kind, key=key, fingerprint=fp)
        elif row.fingerprint != fp:
            row.fingerprint = fp
            row.save()


def run(windows_server, client, dry_run=False, logger=log):
    """Push NetBox to the server, or only plan it with ``dry_run``. Errors reading Windows raise."""
    state = windows_state.read(client)
    target, notes = windows_state.to_records(state, standard_codes=netbox_state.standard_codes())
    source = netbox_state.read(windows_server)
    plan = push.plan(source, target, managed(windows_server))
    _hold_partner_scopes(windows_server, plan, logger)
    outcome = PushOutcome(plan=plan, notes=notes)
    if dry_run:
        return outcome
    outcome.result = push.execute(plan, WindowsWriter(client))
    _save_state(windows_server, outcome.result)
    outcome.replication_errors = _replicate(windows_server, client, outcome.result.applied, logger)
    return outcome
