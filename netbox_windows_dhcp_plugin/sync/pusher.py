"""
Push one Windows server: read Windows and NetBox, plan with ``push.plan``, apply with ``WindowsWriter``, then record
what NetBox now manages in ``SyncState``. Runs in the push job (an RQ worker), never in a request.
"""

import logging
from dataclasses import dataclass, field

from django.db import transaction
from netbox_dhcp.models import Subnet

from ..choices import SyncKindChoices
from ..connection import failover_secret_lookup
from ..models import FailoverRelationship, SyncRun, SyncState
from ..windows.psscript import NotFound, WindowsLayerError
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
    # Scopes whose replication failed, by relationship name: the next push replicates them again.
    replication_pending: dict[str, list[str]] = field(default_factory=dict)

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
        if self.replication_pending:
            out["replication_pending"] = self.replication_pending
        return out

    def errors(self):
        failed = self.result.failed if self.result else []
        lines = [f"{c.action} {c.kind} {c.key}: {msg}" for c, msg in failed]
        return lines + self.replication_errors


def managed(windows_server):
    return {(s.kind, s.key): s.fingerprint for s in SyncState.objects.filter(windows_server=windows_server)}


def _scope_of(change):
    """The Windows IPv4 scope a change lives in, "" for server-level objects, None for DHCPv6 ones."""
    if change.kind.endswith("6"):
        # Windows has no DHCPv6 failover: nothing to replicate or to leave to a primary.
        return None
    fields = change.after or change.before or {}
    if change.kind == "scope":
        return change.key
    if change.kind in ("exclusion", "reservation", "policy"):
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


def _pending_replication(windows_server):
    """
    The scopes an earlier push wrote to Windows but could not replicate. A push that wrote nothing has nothing new to
    replicate, so without this the partner would stay behind until the next change. Pushes that stopped before
    writing (no summary: connection or credential errors) are skipped, so what they inherited is not lost.
    """
    last = (
        SyncRun.objects.filter(windows_server=windows_server, kind=SyncKindChoices.PUSH, completed__isnull=False)
        .exclude(summary={})
        .order_by("-completed", "-pk")
        .first()
    )
    pending = (last.summary.get("replication_pending") if last else None) or {}
    return {name: set(ids) for name, ids in pending.items()}


def _replicate(windows_server, client, applied, logger):
    """
    After writing to a failover primary, copy the changed scopes to the partner, plus those an earlier push failed
    to copy. Returns the error lines and the scopes still to copy, by relationship name.
    """
    pending = _pending_replication(windows_server)
    if not applied and not pending:
        return [], {}
    touched = {_scope_of(c) for c in applied} - {None}
    server_level = "" in touched
    errors, still_pending = [], {}
    for relationship in FailoverRelationship.objects.filter(primary_server=windows_server.dhcp_server):
        scopes = _relationship_scopes(relationship)
        # Scopes since dropped from the relationship are no longer the partner's business.
        changed = sorted(
            (scopes if server_level else touched & scopes) | (pending.get(relationship.name, set()) & scopes)
        )
        if not changed:
            continue
        try:
            # Only scopes Windows already has in the relationship can be replicated: one that is not (its create or
            # its addition to the relationship failed in this push) would make Windows refuse the whole call. Such
            # scopes wait for a later push, which adds them first.
            on_windows = set(client.get_failover(relationship.name).scope_ids)
        except NotFound:
            on_windows = set()
        except (WindowsLayerError, ValueError) as exc:
            errors.append(f"failover replication {relationship.name}: {exc}")
            still_pending[relationship.name] = changed
            continue
        ready = [s for s in changed if s in on_windows]
        waiting = [s for s in changed if s not in on_windows]
        if waiting:
            still_pending[relationship.name] = waiting
            logger.warning(
                f"Not in {relationship.name} on Windows yet, replication waits for the next push: {', '.join(waiting)}"
            )
        if not ready:
            continue
        try:
            # Replication of the listed scopes copies their exclusions, reservations and options (lab, Windows Server
            # 2022).
            client.replicate_failover(name=relationship.name, scope_ids=ready)
        except (WindowsLayerError, ValueError) as exc:
            errors.append(f"failover replication {relationship.name}: {exc}")
            still_pending[relationship.name] = changed
        else:
            logger.info(f"Replicated {len(ready)} scope(s) to the partner of {relationship.name}")
    return errors, still_pending


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
    partners = netbox_state.partner_failovers(windows_server)
    codes6 = netbox_state.standard_codes6()
    target, notes = windows_state.to_records(
        state,
        standard_codes=netbox_state.standard_codes(),
        partner_failovers=partners,
        standard_codes6=codes6,
        partner_servers=netbox_state.partner_servers(windows_server),
    )
    source = netbox_state.read(windows_server)
    current = managed(windows_server)
    hidden = windows_state.hidden_records(state, standard_codes6=codes6, partner_failovers=partners)
    defined = {windows_state.optiondef_key(d.option_id, d.vendor_class) for d in state.option_definitions}
    plan = push.plan(source, target, current, hidden=hidden, defined_options=defined)
    _hold_partner_scopes(windows_server, plan, logger)
    outcome = PushOutcome(plan=plan, notes=notes)
    if dry_run:
        return outcome
    # The option values the push writes: a reservation update that loses one of these gets it back on the next push.
    skipped = {(s.change.kind, s.change.key) for s in plan.skipped}
    held = {r.key for r in source if r.kind in ("option", "option6") and (r.kind, r.key) not in skipped}
    writer = WindowsWriter(client, failover_secret=failover_secret_lookup(windows_server), held_options=held)
    outcome.result = push.execute(plan, writer)
    _save_state(windows_server, outcome.result)
    outcome.replication_errors, outcome.replication_pending = _replicate(
        windows_server, client, outcome.result.applied, logger
    )
    return outcome
