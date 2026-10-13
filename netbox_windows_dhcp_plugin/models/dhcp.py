"""
Windows DHCP concepts that netbox-plugin-dhcp (built around Kea) does not model.

Every model here is DHCP configuration and therefore branchable: it is reviewed, merged and pushed together with the
netbox-plugin-dhcp objects it extends.
"""

import ipaddress

from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from netbox.models import NetBoxModel, PrimaryModel

from ..choices import (
    ClassTypeChoices,
    FailoverModeChoices,
    FailoverServerRoleChoices,
    MacFilterListChoices,
    PolicyConditionChoices,
    ReservationTypeChoices,
    ScopeStateChoices,
    ScopeTypeChoices,
)

__all__ = (
    "ExclusionRange",
    "FailoverRelationship",
    "MacFilter",
    "WindowsClass",
    "WindowsOptionDefinition",
    "WindowsOptionQualifier",
    "WindowsPolicy",
    "WindowsReservation",
    "WindowsScope",
)


def _url(model_name, pk):
    return reverse(f"plugins:netbox_windows_dhcp_plugin:{model_name}", args=[pk])


class WindowsScope(NetBoxModel):
    """Windows-only scope settings for a netbox-plugin-dhcp Subnet (the scope itself)."""

    subnet = models.OneToOneField(
        to="netbox_dhcp.Subnet",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_windows_scope",
        verbose_name=_("subnet"),
    )
    state = models.CharField(
        verbose_name=_("state"),
        max_length=10,
        choices=ScopeStateChoices,
        default=ScopeStateChoices.ACTIVE,
    )
    scope_type = models.CharField(
        verbose_name=_("type"),
        max_length=10,
        choices=ScopeTypeChoices,
        default=ScopeTypeChoices.DHCP,
    )
    delay_ms = models.PositiveIntegerField(
        verbose_name=_("offer delay (ms)"),
        default=0,
    )
    max_bootp_clients = models.PositiveIntegerField(
        verbose_name=_("max BOOTP clients"),
        blank=True,
        null=True,
    )
    activate_policies = models.BooleanField(
        verbose_name=_("activate policies"),
        default=True,
    )
    preference = models.PositiveSmallIntegerField(
        verbose_name=_("preference"),
        blank=True,
        null=True,
        help_text=_("DHCPv6 scopes only"),
    )

    class Meta:
        ordering = ("subnet",)
        verbose_name = _("Windows scope")
        verbose_name_plural = _("Windows scopes")

    def __str__(self):
        return str(self.subnet)

    def get_absolute_url(self):
        return _url("windowsscope", self.pk)

    def get_state_color(self):
        return ScopeStateChoices.colors.get(self.state)


class WindowsReservation(NetBoxModel):
    """Windows-only settings for a netbox-plugin-dhcp HostReservation."""

    host_reservation = models.OneToOneField(
        to="netbox_dhcp.HostReservation",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_windows_reservation",
        verbose_name=_("host reservation"),
    )
    reservation_type = models.CharField(
        verbose_name=_("type"),
        max_length=10,
        choices=ReservationTypeChoices,
        default=ReservationTypeChoices.BOTH,
    )

    class Meta:
        ordering = ("host_reservation",)
        verbose_name = _("Windows reservation")
        verbose_name_plural = _("Windows reservations")

    def __str__(self):
        return str(self.host_reservation)

    def get_absolute_url(self):
        return _url("windowsreservation", self.pk)


class ExclusionRange(PrimaryModel):
    """An address range inside a scope that the server never hands out."""

    subnet = models.ForeignKey(
        to="netbox_dhcp.Subnet",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_windows_exclusion_ranges",
        verbose_name=_("subnet"),
    )
    start_address = models.GenericIPAddressField(verbose_name=_("start address"))
    end_address = models.GenericIPAddressField(verbose_name=_("end address"))

    class Meta:
        ordering = ("subnet", "start_address")
        verbose_name = _("exclusion range")
        verbose_name_plural = _("exclusion ranges")
        constraints = (
            models.UniqueConstraint(
                fields=("subnet", "start_address", "end_address"),
                name="%(app_label)s_%(class)s_unique_range",
            ),
        )

    def __str__(self):
        return f"{self.start_address}-{self.end_address}"

    def get_absolute_url(self):
        return _url("exclusionrange", self.pk)

    def clean(self):
        super().clean()
        if not (self.start_address and self.end_address):
            return
        start = ipaddress.ip_address(self.start_address)
        end = ipaddress.ip_address(self.end_address)
        if start.version != end.version:
            raise ValidationError(_("Start and end addresses must be of the same family."))
        if start > end:
            raise ValidationError({"end_address": _("The end address must not be lower than the start address.")})
        if self.subnet_id is not None:
            network = ipaddress.ip_network(str(self.subnet.prefix.prefix))
            if start not in network or end not in network:
                raise ValidationError(
                    _("The range must be inside the subnet's prefix {prefix}.").format(prefix=network)
                )


class WindowsClass(PrimaryModel):
    """A Windows user or vendor class, matched on the class data the client sends."""

    dhcp_server = models.ForeignKey(
        to="netbox_dhcp.DHCPServer",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_windows_classes",
        verbose_name=_("DHCP server"),
    )
    name = models.CharField(verbose_name=_("name"), max_length=255)
    class_type = models.CharField(
        verbose_name=_("type"),
        max_length=10,
        choices=ClassTypeChoices,
    )
    data = models.CharField(
        verbose_name=_("data"),
        max_length=255,
        help_text=_("ASCII class data sent by the client"),
    )
    family = models.PositiveSmallIntegerField(
        verbose_name=_("address family"),
        choices=((4, "IPv4"), (6, "IPv6")),
        default=4,
    )

    class Meta:
        ordering = ("dhcp_server", "class_type", "name")
        verbose_name = _("Windows class")
        verbose_name_plural = _("Windows classes")
        constraints = (
            models.UniqueConstraint(
                fields=("dhcp_server", "family", "class_type", "name"),
                name="%(app_label)s_%(class)s_unique_name",
            ),
        )

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return _url("windowsclass", self.pk)

    def get_class_type_color(self):
        return ClassTypeChoices.colors.get(self.class_type)


class WindowsOptionDefinition(NetBoxModel):
    """Windows-only settings for a server-scoped netbox-plugin-dhcp OptionDefinition."""

    option_definition = models.OneToOneField(
        to="netbox_dhcp.OptionDefinition",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_windows_option_definition",
        verbose_name=_("option definition"),
    )
    vendor_class = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.WindowsClass",
        on_delete=models.PROTECT,
        related_name="option_definitions",
        blank=True,
        null=True,
        limit_choices_to={"class_type": ClassTypeChoices.VENDOR},
        verbose_name=_("vendor class"),
    )
    # netbox-plugin-dhcp has fewer data types than Windows (BinaryData and EncapsulatedData are both "binary"), so
    # keep the Windows type to push back exactly what was imported.
    windows_type = models.CharField(
        verbose_name=_("Windows type"),
        max_length=50,
        blank=True,
        help_text=_("Windows data type, for example DWord or EncapsulatedData"),
    )
    default_value = models.JSONField(
        verbose_name=_("default value"),
        default=list,
        blank=True,
        help_text=_("Default value list, as Windows stores it"),
    )

    class Meta:
        ordering = ("option_definition",)
        verbose_name = _("Windows option definition")
        verbose_name_plural = _("Windows option definitions")

    def __str__(self):
        return str(self.option_definition)

    def get_absolute_url(self):
        return _url("windowsoptiondefinition", self.pk)


class WindowsPolicy(PrimaryModel):
    """A Windows DHCP policy, at server level or (with a subnet) at scope level."""

    dhcp_server = models.ForeignKey(
        to="netbox_dhcp.DHCPServer",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_windows_policies",
        verbose_name=_("DHCP server"),
    )
    subnet = models.ForeignKey(
        to="netbox_dhcp.Subnet",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_windows_policies",
        blank=True,
        null=True,
        verbose_name=_("subnet"),
        help_text=_("Leave empty for a server-level policy"),
    )
    name = models.CharField(verbose_name=_("name"), max_length=255)
    processing_order = models.PositiveSmallIntegerField(verbose_name=_("processing order"), default=1)
    enabled = models.BooleanField(verbose_name=_("enabled"), default=True)
    condition = models.CharField(
        verbose_name=_("condition"),
        max_length=5,
        choices=PolicyConditionChoices,
        default=PolicyConditionChoices.OR,
    )
    criteria = models.JSONField(
        verbose_name=_("criteria"),
        default=dict,
        blank=True,
        help_text=_(
            'Match criteria by kind, for example {"vendor_class": ["EQ", "MSFT"], "mac_address": ["EQ", "00-15-5d*"]}'
        ),
    )
    lease_duration = models.PositiveIntegerField(
        verbose_name=_("lease duration (s)"),
        blank=True,
        null=True,
    )
    ip_ranges = models.JSONField(
        verbose_name=_("IP ranges"),
        default=list,
        blank=True,
        help_text=_('Scope-level policies only, for example [["10.0.0.10", "10.0.0.50"]]'),
    )

    class Meta:
        ordering = ("dhcp_server", "subnet", "processing_order", "name")
        verbose_name = _("Windows policy")
        verbose_name_plural = _("Windows policies")
        constraints = (
            models.UniqueConstraint(
                fields=("dhcp_server", "subnet", "name"),
                name="%(app_label)s_%(class)s_unique_name",
                nulls_distinct=False,
            ),
        )

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return _url("windowspolicy", self.pk)

    def clean(self):
        super().clean()
        if self.ip_ranges and self.subnet_id is None:
            raise ValidationError({"ip_ranges": _("Only scope-level policies can have IP ranges.")})
        # dhcp_server is required; when it is missing the form reports that, so skip the comparison here.
        if self.subnet_id is not None and self.dhcp_server_id and self.subnet.parent_dhcp_server != self.dhcp_server:
            raise ValidationError({"subnet": _("The subnet belongs to another DHCP server.")})
        if not isinstance(self.ip_ranges, list):
            raise ValidationError({"ip_ranges": _("Must be a list of [start, end] pairs.")})
        for pair in self.ip_ranges:
            try:
                start, end = (ipaddress.ip_address(a) for a in pair)
            except (TypeError, ValueError) as exc:
                raise ValidationError({"ip_ranges": _("Must be a list of [start, end] pairs.")}) from exc
            if start.version != end.version:
                raise ValidationError({"ip_ranges": _("Start and end addresses must be of the same family.")})
            if start > end:
                raise ValidationError({"ip_ranges": _("Range {start}-{end} is reversed.").format(start=start, end=end)})


class WindowsOptionQualifier(NetBoxModel):
    """Restricts a netbox-plugin-dhcp Option value to a user class, vendor class or policy, as Windows allows."""

    option = models.OneToOneField(
        to="netbox_dhcp.Option",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_windows_qualifier",
        verbose_name=_("option"),
    )
    user_class = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.WindowsClass",
        on_delete=models.PROTECT,
        related_name="user_class_options",
        blank=True,
        null=True,
        limit_choices_to={"class_type": ClassTypeChoices.USER},
        verbose_name=_("user class"),
    )
    vendor_class = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.WindowsClass",
        on_delete=models.PROTECT,
        related_name="vendor_class_options",
        blank=True,
        null=True,
        limit_choices_to={"class_type": ClassTypeChoices.VENDOR},
        verbose_name=_("vendor class"),
    )
    policy = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.WindowsPolicy",
        on_delete=models.CASCADE,
        related_name="options",
        blank=True,
        null=True,
        verbose_name=_("policy"),
    )

    class Meta:
        ordering = ("option",)
        verbose_name = _("Windows option qualifier")
        verbose_name_plural = _("Windows option qualifiers")

    def __str__(self):
        return str(self.option)

    def get_absolute_url(self):
        return _url("windowsoptionqualifier", self.pk)

    def clean(self):
        super().clean()
        # Set-DhcpServerv4OptionValue accepts -PolicyName with -VendorClass, but not with -UserClass.
        if self.policy_id is not None and self.user_class_id is not None:
            raise ValidationError({"user_class": _("A policy option cannot also have a user class.")})


class FailoverRelationship(PrimaryModel):
    """A Windows DHCPv4 failover relationship between two servers and the scopes it covers."""

    name = models.CharField(verbose_name=_("name"), max_length=126)
    primary_server = models.ForeignKey(
        to="netbox_dhcp.DHCPServer",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_windows_failover_primary",
        verbose_name=_("primary server"),
        help_text=_("Changes are written here, then replicated to the partner"),
    )
    partner_server = models.ForeignKey(
        to="netbox_dhcp.DHCPServer",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_windows_failover_partner",
        verbose_name=_("partner server"),
    )
    mode = models.CharField(
        verbose_name=_("mode"),
        max_length=15,
        choices=FailoverModeChoices,
        default=FailoverModeChoices.LOAD_BALANCE,
    )
    max_client_lead_time = models.PositiveIntegerField(
        verbose_name=_("maximum client lead time (s)"),
        default=3600,
    )
    load_balance_percent = models.PositiveSmallIntegerField(
        verbose_name=_("load balance (%)"),
        blank=True,
        null=True,
        help_text=_("Load balance mode only"),
    )
    reserve_percent = models.PositiveSmallIntegerField(
        verbose_name=_("reserve (%)"),
        blank=True,
        null=True,
        help_text=_("Hot standby mode only"),
    )
    server_role = models.CharField(
        verbose_name=_("primary server role"),
        max_length=10,
        choices=FailoverServerRoleChoices,
        blank=True,
        help_text=_("Hot standby mode only"),
    )
    state_switch_interval = models.PositiveIntegerField(
        verbose_name=_("state switchover interval (s)"),
        blank=True,
        null=True,
        help_text=_("Automatic switch to partner-down after this delay; disabled when empty"),
    )
    shared_secret_profile = models.CharField(
        verbose_name=_("shared secret profile"),
        max_length=100,
        blank=True,
        help_text=_("Credential profile holding the message authentication secret; the secret is never stored"),
    )
    subnets = models.ManyToManyField(
        to="netbox_dhcp.Subnet",
        related_name="%(app_label)s_windows_failover_relationships",
        blank=True,
        verbose_name=_("subnets"),
    )

    class Meta:
        ordering = ("name",)
        verbose_name = _("failover relationship")
        verbose_name_plural = _("failover relationships")
        constraints = (
            models.UniqueConstraint(
                fields=("primary_server", "name"),
                name="%(app_label)s_%(class)s_unique_name",
            ),
        )

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return _url("failoverrelationship", self.pk)

    def get_mode_color(self):
        return FailoverModeChoices.colors.get(self.mode)

    def clean(self):
        super().clean()
        if self.primary_server_id and self.primary_server_id == self.partner_server_id:
            raise ValidationError({"partner_server": _("The partner must be a different server.")})
        if self.mode == FailoverModeChoices.LOAD_BALANCE:
            if self.reserve_percent is not None or self.server_role:
                raise ValidationError(_("Reserve and server role only apply to hot standby."))
            if self.load_balance_percent is not None and self.load_balance_percent > 100:
                raise ValidationError({"load_balance_percent": _("Must be between 0 and 100.")})
        else:
            if self.load_balance_percent is not None:
                raise ValidationError({"load_balance_percent": _("Load balance only applies to load balance mode.")})
            if self.reserve_percent is not None and self.reserve_percent > 100:
                raise ValidationError({"reserve_percent": _("Must be between 0 and 100.")})


class MacFilter(PrimaryModel):
    """A server-wide allow or deny entry, matched on the client MAC address (wildcards allowed)."""

    dhcp_server = models.ForeignKey(
        to="netbox_dhcp.DHCPServer",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_windows_mac_filters",
        verbose_name=_("DHCP server"),
    )
    mac_address = models.CharField(
        verbose_name=_("MAC address"),
        max_length=32,
        help_text=_("For example 00-15-5d-01-02-03 or 00-15-5d-*"),
    )
    list = models.CharField(
        verbose_name=_("list"),
        max_length=10,
        choices=MacFilterListChoices,
    )

    class Meta:
        ordering = ("dhcp_server", "list", "mac_address")
        verbose_name = _("MAC filter")
        verbose_name_plural = _("MAC filters")
        constraints = (
            models.UniqueConstraint(
                fields=("dhcp_server", "mac_address"),
                name="%(app_label)s_%(class)s_unique_mac",
            ),
        )

    def __str__(self):
        return self.mac_address

    def get_absolute_url(self):
        return _url("macfilter", self.pk)

    def get_list_color(self):
        return MacFilterListChoices.colors.get(self.list)

    def clean(self):
        super().clean()
        mac = self.mac_address.strip().lower().replace(":", "-")
        groups = mac.split("-")
        if not mac or len(groups) > 6 or any(g != "*" and (len(g) != 2 or not _is_hex(g)) for g in groups):
            raise ValidationError({"mac_address": _("Use pairs of hex digits separated by '-' or ':', or '*'.")})
        self.mac_address = mac


def _is_hex(value):
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
