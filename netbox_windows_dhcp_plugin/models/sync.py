"""
Bookkeeping for pushes and imports. None of these models is branchable: they record what happened on main and on the
Windows servers, and must stay writable when netbox-change-control protects main.
"""

from django.contrib.contenttypes.fields import GenericForeignKey
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from netbox.models import NetBoxModel
from utilities.querysets import RestrictedQuerySet

from ..choices import (
    ImportDestinationChoices,
    ProposalItemActionChoices,
    ProposalItemStatusChoices,
    ProposalStatusChoices,
    SyncKindChoices,
    SyncStatusChoices,
)

__all__ = (
    "ImportProposal",
    "ImportProposalItem",
    "SyncRun",
    "SyncState",
)


class SyncState(models.Model):
    """
    One Windows object this plugin manages, with the fingerprint of what was last pushed.

    Pushes only ever delete Windows objects listed here, so objects created by hand on Windows are left alone until
    they are imported.
    """

    windows_server = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.WindowsServer",
        on_delete=models.CASCADE,
        related_name="sync_states",
    )
    kind = models.CharField(max_length=50, help_text=_("Windows object kind, for example scope or reservation"))
    key = models.CharField(max_length=255, help_text=_("Windows identity, for example 10.0.0.0 or 10.0.0.0/10.0.0.5"))
    object_type = models.ForeignKey(
        to="contenttypes.ContentType",
        on_delete=models.CASCADE,
        related_name="+",
        blank=True,
        null=True,
    )
    object_id = models.PositiveBigIntegerField(blank=True, null=True)
    object = GenericForeignKey("object_type", "object_id")
    fingerprint = models.CharField(max_length=64)
    last_pushed = models.DateTimeField(auto_now=True)

    objects = RestrictedQuerySet.as_manager()

    class Meta:
        ordering = ("windows_server", "kind", "key")
        constraints = (
            models.UniqueConstraint(
                fields=("windows_server", "kind", "key"),
                name="%(app_label)s_%(class)s_unique_key",
            ),
        )

    def __str__(self):
        return f"{self.kind} {self.key}"


class SyncRun(NetBoxModel):
    """One push, dry run, import or connection test against a Windows server."""

    windows_server = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.WindowsServer",
        on_delete=models.CASCADE,
        related_name="sync_runs",
        verbose_name=_("Windows server"),
    )
    kind = models.CharField(verbose_name=_("kind"), max_length=10, choices=SyncKindChoices)
    status = models.CharField(
        verbose_name=_("status"),
        max_length=10,
        choices=SyncStatusChoices,
        default=SyncStatusChoices.PENDING,
    )
    job = models.ForeignKey(
        to="core.Job",
        on_delete=models.SET_NULL,
        related_name="+",
        blank=True,
        null=True,
        verbose_name=_("job"),
    )
    started = models.DateTimeField(verbose_name=_("started"), blank=True, null=True)
    completed = models.DateTimeField(verbose_name=_("completed"), blank=True, null=True)
    branch_name = models.CharField(
        verbose_name=_("branch"),
        max_length=255,
        blank=True,
        help_text=_("Branch that triggered the push, or that the import created"),
    )
    destination = models.CharField(
        verbose_name=_("destination"),
        max_length=10,
        choices=ImportDestinationChoices,
        blank=True,
        help_text=_("Where an import wrote: a new import branch, a chosen branch, main, or an import proposal"),
    )
    summary = models.JSONField(verbose_name=_("summary"), default=dict, blank=True)
    error = models.TextField(verbose_name=_("error"), blank=True)

    class Meta:
        ordering = ("-created",)
        verbose_name = _("sync run")
        verbose_name_plural = _("sync runs")

    def __str__(self):
        return f"{self.get_kind_display()} {self.windows_server} #{self.pk}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_windows_dhcp_plugin:syncrun", args=[self.pk])

    def get_kind_color(self):
        return SyncKindChoices.colors.get(self.kind)

    def get_status_color(self):
        return SyncStatusChoices.colors.get(self.status)

    def get_destination_color(self):
        return ImportDestinationChoices.colors.get(self.destination)


class ImportProposal(NetBoxModel):
    """Differences found on a Windows server, waiting for review. Used when netbox-branching is not installed."""

    windows_server = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.WindowsServer",
        on_delete=models.CASCADE,
        related_name="import_proposals",
        verbose_name=_("Windows server"),
    )
    sync_run = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.SyncRun",
        on_delete=models.SET_NULL,
        related_name="import_proposals",
        blank=True,
        null=True,
        verbose_name=_("sync run"),
    )
    status = models.CharField(
        verbose_name=_("status"),
        max_length=10,
        choices=ProposalStatusChoices,
        default=ProposalStatusChoices.OPEN,
    )

    class Meta:
        ordering = ("-created",)
        verbose_name = _("import proposal")
        verbose_name_plural = _("import proposals")

    def __str__(self):
        return f"{self.windows_server} import #{self.pk}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_windows_dhcp_plugin:importproposal", args=[self.pk])

    def get_status_color(self):
        return ProposalStatusChoices.colors.get(self.status)


class ImportProposalItem(NetBoxModel):
    """One difference: an object to create, update or delete in NetBox so it matches Windows."""

    proposal = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.ImportProposal",
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("proposal"),
    )
    # Dependency order from the diff engine (parents before children), so bulk apply can follow it.
    sequence = models.PositiveIntegerField(verbose_name=_("sequence"), default=0)
    action = models.CharField(verbose_name=_("action"), max_length=10, choices=ProposalItemActionChoices)
    status = models.CharField(
        verbose_name=_("status"),
        max_length=10,
        choices=ProposalItemStatusChoices,
        default=ProposalItemStatusChoices.PENDING,
    )
    kind = models.CharField(verbose_name=_("kind"), max_length=50)
    key = models.CharField(verbose_name=_("Windows key"), max_length=255)
    object_type = models.ForeignKey(
        to="contenttypes.ContentType",
        on_delete=models.CASCADE,
        related_name="+",
        blank=True,
        null=True,
        verbose_name=_("object type"),
    )
    object_id = models.PositiveBigIntegerField(verbose_name=_("object ID"), blank=True, null=True)
    object = GenericForeignKey("object_type", "object_id")
    before = models.JSONField(verbose_name=_("before"), blank=True, null=True)
    after = models.JSONField(verbose_name=_("after"), blank=True, null=True)
    error = models.TextField(verbose_name=_("error"), blank=True)

    class Meta:
        ordering = ("proposal", "sequence", "pk")
        verbose_name = _("import proposal item")
        verbose_name_plural = _("import proposal items")

    def __str__(self):
        return f"{self.get_action_display()} {self.kind} {self.key}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_windows_dhcp_plugin:importproposalitem", args=[self.pk])

    def get_action_color(self):
        return ProposalItemActionChoices.colors.get(self.action)

    def get_status_color(self):
        return ProposalItemStatusChoices.colors.get(self.status)
