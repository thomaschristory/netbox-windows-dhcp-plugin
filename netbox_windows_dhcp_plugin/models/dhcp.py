"""
The DHCP configuration of a Windows server, as Windows holds it.

Every model here is branchable: it is reviewed, merged and pushed like any other NetBox data. Scopes and reservations
point at core NetBox objects (prefix, IP range, IP address, MAC address), so the addresses show up in IPAM too.
"""

import ipaddress

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from netbox.models import NetBoxModel, PrimaryModel

from ..choices import (
    POLICY_CRITERIA_KEYS,
    ClassTypeChoices,
    FailoverModeChoices,
    FailoverServerRoleChoices,
    FamilyChoices,
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
    "MacFilterLists",
    "OptionDefinition",
    "OptionValue",
    "Reservation",
    "Scope",
    "Superscope",
    "WindowsClass",
    "WindowsPolicy",
)

SERVER = "netbox_windows_dhcp_plugin.WindowsServer"


def _url(model_name, pk):
    return reverse(f"plugins:netbox_windows_dhcp_plugin:{model_name}", args=[pk])


def _server_fk(related_name, **kwargs):
    return models.ForeignKey(
        to=SERVER,
        on_delete=models.CASCADE,
        related_name=related_name,
        verbose_name=_("server"),
        **kwargs,
    )


class Superscope(NetBoxModel):
    """A Windows superscope: IPv4 scopes of one server served together on the same network segment."""

    server = _server_fk("superscopes")
    name = models.CharField(verbose_name=_("name"), max_length=255)
    description = models.CharField(verbose_name=_("description"), max_length=200, blank=True)
    comments = models.TextField(verbose_name=_("comments"), blank=True)

    class Meta:
        ordering = ("server", "name")
        verbose_name = _("superscope")
        verbose_name_plural = _("superscopes")
        constraints = (
            models.UniqueConstraint(
                fields=("server", "name"),
                name="%(app_label)s_%(class)s_unique_name",
            ),
        )

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return _url("superscope", self.pk)


class Scope(NetBoxModel):
    """
    A Windows DHCP scope, IPv4 or DHCPv6 (the prefix decides). An IPv4 scope hands out its IP range; a DHCPv6 scope
    is a /64 and has no range.
    """

    server = _server_fk("scopes")
    prefix = models.ForeignKey(
        to="ipam.Prefix",
        # Deleting the prefix in IPAM must not silently delete the scope (and with it the scope on Windows).
        on_delete=models.PROTECT,
        related_name="%(app_label)s_scopes",
        verbose_name=_("prefix"),
    )
    ip_range = models.ForeignKey(
        to="ipam.IPRange",
        on_delete=models.PROTECT,
        related_name="%(app_label)s_scopes",
        blank=True,
        null=True,
        verbose_name=_("IP range"),
        help_text=_("IPv4 scopes: the addresses the scope hands out (start and end of the scope)"),
    )
    superscope = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.Superscope",
        # On Windows, removing a superscope keeps its scopes.
        on_delete=models.SET_NULL,
        related_name="scopes",
        blank=True,
        null=True,
        verbose_name=_("superscope"),
    )
    name = models.CharField(verbose_name=_("name"), max_length=255)
    # Windows descriptions have no practical length limit, unlike NetBox's usual 200-character description.
    description = models.TextField(verbose_name=_("description"), blank=True)
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
        help_text=_("IPv4 scopes only"),
    )
    lease_duration = models.PositiveIntegerField(
        verbose_name=_("lease duration (s)"),
        blank=True,
        null=True,
        help_text=_("IPv4 scopes only. Empty keeps what Windows has"),
    )
    delay_ms = models.PositiveIntegerField(
        verbose_name=_("offer delay (ms)"),
        default=0,
        help_text=_("IPv4 scopes only"),
    )
    max_bootp_clients = models.PositiveIntegerField(
        verbose_name=_("max BOOTP clients"),
        blank=True,
        null=True,
        help_text=_("IPv4 scopes only. Empty means no limit"),
    )
    activate_policies = models.BooleanField(
        verbose_name=_("activate policies"),
        default=True,
        help_text=_("IPv4 scopes only"),
    )
    preference = models.PositiveSmallIntegerField(
        verbose_name=_("preference"),
        blank=True,
        null=True,
        help_text=_("DHCPv6 scopes only"),
    )
    preferred_lifetime = models.PositiveIntegerField(
        verbose_name=_("preferred lifetime (s)"),
        blank=True,
        null=True,
        help_text=_("DHCPv6 scopes only. Empty keeps what Windows has"),
    )
    valid_lifetime = models.PositiveIntegerField(
        verbose_name=_("valid lifetime (s)"),
        blank=True,
        null=True,
        help_text=_("DHCPv6 scopes only. Empty keeps what Windows has"),
    )
    renew_timer = models.PositiveIntegerField(
        verbose_name=_("T1 (s)"),
        blank=True,
        null=True,
        help_text=_("DHCPv6 scopes only: renewal time. Empty follows the preferred lifetime"),
    )
    rebind_timer = models.PositiveIntegerField(
        verbose_name=_("T2 (s)"),
        blank=True,
        null=True,
        help_text=_("DHCPv6 scopes only: rebinding time. Empty follows the preferred lifetime"),
    )
    comments = models.TextField(verbose_name=_("comments"), blank=True)

    clone_fields = (
        "server",
        "superscope",
        "state",
        "scope_type",
        "lease_duration",
        "delay_ms",
        "max_bootp_clients",
        "activate_policies",
        "preference",
        "preferred_lifetime",
        "valid_lifetime",
        "renew_timer",
        "rebind_timer",
    )

    class Meta:
        ordering = ("server", "prefix")
        verbose_name = _("scope")
        verbose_name_plural = _("scopes")
        constraints = (
            models.UniqueConstraint(
                fields=("server", "prefix"),
                name="%(app_label)s_%(class)s_unique_prefix",
            ),
        )

    def __str__(self):
        return f"{self.server}/{self.scope_id}" if self.prefix_id else self.name

    def get_absolute_url(self):
        return _url("scope", self.pk)

    def get_state_color(self):
        return ScopeStateChoices.colors.get(self.state)

    @property
    def network(self):
        return ipaddress.ip_network(str(self.prefix.prefix)) if self.prefix_id else None

    @property
    def family(self):
        return self.network.version if self.prefix_id else None

    @property
    def scope_id(self):
        """How Windows names the scope: its network address (IPv4 scope ID, DHCPv6 prefix)."""
        return str(self.network.network_address) if self.prefix_id else ""

    def clean(self):
        super().clean()
        if not self.prefix_id:
            return
        network = self.network
        if network.version == 6:
            if self.ip_range_id is not None:
                raise ValidationError({"ip_range": _("DHCPv6 scopes have no IP range.")})
            if self.superscope_id is not None:
                raise ValidationError({"superscope": _("Windows has no DHCPv6 superscopes.")})
        if self.ip_range_id is not None:
            # NetBox holds netaddr objects; compare in the standard library's types.
            start = ipaddress.ip_address(str(self.ip_range.start_address.ip))
            end = ipaddress.ip_address(str(self.ip_range.end_address.ip))
            if start not in network or end not in network:
                raise ValidationError(
                    {"ip_range": _("The range must be inside the prefix {prefix}.").format(prefix=network)}
                )
        if self.superscope_id is not None and self.server_id and self.superscope.server_id != self.server_id:
            raise ValidationError({"superscope": _("The superscope belongs to another server.")})


class Reservation(NetBoxModel):
    """
    A reserved address in a scope. IPv4: for a MAC address (or another client ID). DHCPv6: for a DUID and IAID.
    """

    scope = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.Scope",
        on_delete=models.CASCADE,
        related_name="reservations",
        verbose_name=_("scope"),
    )
    ip_address = models.ForeignKey(
        to="ipam.IPAddress",
        on_delete=models.PROTECT,
        related_name="%(app_label)s_reservations",
        verbose_name=_("IP address"),
    )
    mac_address = models.ForeignKey(
        to="dcim.MACAddress",
        on_delete=models.PROTECT,
        related_name="%(app_label)s_reservations",
        blank=True,
        null=True,
        verbose_name=_("MAC address"),
        help_text=_("IPv4 reservations: the client's MAC address"),
    )
    client_id = models.CharField(
        verbose_name=_("client ID"),
        max_length=255,
        blank=True,
        help_text=_("IPv4 reservations of clients identified by something else than a MAC address"),
    )
    duid = models.CharField(
        verbose_name=_("DUID"),
        max_length=255,
        blank=True,
        help_text=_("DHCPv6 reservations: the client's DUID, for example 00-01-00-01-2a-..."),
    )
    iaid = models.PositiveBigIntegerField(
        verbose_name=_("IAID"),
        blank=True,
        null=True,
        validators=[MaxValueValidator(0xFFFFFFFF)],
        help_text=_("DHCPv6 reservations: the identity association ID the client sends with its DUID"),
    )
    name = models.CharField(verbose_name=_("name"), max_length=255, blank=True)
    description = models.TextField(verbose_name=_("description"), blank=True)
    reservation_type = models.CharField(
        verbose_name=_("type"),
        max_length=10,
        choices=ReservationTypeChoices,
        default=ReservationTypeChoices.BOTH,
        help_text=_("IPv4 reservations only"),
    )
    comments = models.TextField(verbose_name=_("comments"), blank=True)

    clone_fields = ("scope", "reservation_type")

    class Meta:
        ordering = ("scope", "ip_address")
        verbose_name = _("reservation")
        verbose_name_plural = _("reservations")
        constraints = (
            models.UniqueConstraint(
                fields=("scope", "ip_address"),
                name="%(app_label)s_%(class)s_unique_address",
            ),
        )

    def __str__(self):
        if self.ip_address_id:
            return str(self.ip_address.address.ip)
        return self.name or super().__str__()

    def get_absolute_url(self):
        return _url("reservation", self.pk)

    @property
    def server(self):
        return self.scope.server if self.scope_id else None

    @property
    def client(self):
        """The client identity Windows matches: MAC address or client ID (IPv4), DUID (DHCPv6)."""
        if self.mac_address_id:
            return str(self.mac_address.mac_address)
        return self.client_id or self.duid

    def clean(self):
        super().clean()
        if not (self.scope_id and self.ip_address_id):
            return
        network = self.scope.network
        ip = ipaddress.ip_address(str(self.ip_address.address.ip))
        if ip not in network:
            raise ValidationError(
                {"ip_address": _("The address must be inside the scope {prefix}.").format(prefix=network)}
            )
        if network.version == 4:
            if self.duid or self.iaid is not None:
                raise ValidationError(_("DUID and IAID are for DHCPv6 reservations."))
            if self.mac_address_id and self.client_id:
                raise ValidationError({"client_id": _("Set a MAC address or a client ID, not both.")})
            # Windows matches an IPv4 reservation on its client ID: without one the push cannot create it.
            if not (self.mac_address_id or self.client_id):
                raise ValidationError({"mac_address": _("Set the client's MAC address, or a client ID.")})
        elif self.mac_address_id or self.client_id:
            raise ValidationError(
                _("DHCPv6 reservations are matched on DUID and IAID, not a MAC address or client ID.")
            )
        else:
            # Windows matches a DHCPv6 reservation on both: without them the push cannot create it.
            missing = {}
            if not self.duid:
                missing["duid"] = _("A DHCPv6 reservation needs the client's DUID.")
            if self.iaid is None:
                missing["iaid"] = _("A DHCPv6 reservation needs the client's IAID.")
            if missing:
                raise ValidationError(missing)


class ExclusionRange(NetBoxModel):
    """An address range inside a scope that the server never hands out."""

    scope = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.Scope",
        on_delete=models.CASCADE,
        related_name="exclusion_ranges",
        verbose_name=_("scope"),
    )
    start_address = models.GenericIPAddressField(verbose_name=_("start address"))
    end_address = models.GenericIPAddressField(verbose_name=_("end address"))
    description = models.CharField(verbose_name=_("description"), max_length=200, blank=True)
    comments = models.TextField(verbose_name=_("comments"), blank=True)

    class Meta:
        ordering = ("scope", "start_address")
        verbose_name = _("exclusion range")
        verbose_name_plural = _("exclusion ranges")
        constraints = (
            models.UniqueConstraint(
                fields=("scope", "start_address", "end_address"),
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
        if self.scope_id is not None:
            network = self.scope.network
            if network.version == 6:
                raise ValidationError({"scope": _("DHCPv6 exclusion ranges are not synced: add them on Windows.")})
            if start not in network or end not in network:
                raise ValidationError(_("The range must be inside the scope {prefix}.").format(prefix=network))


class WindowsClass(PrimaryModel):
    """A Windows user or vendor class, matched on the class data the client sends."""

    server = _server_fk("classes")
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
        choices=FamilyChoices,
        default=FamilyChoices.IPV4,
    )

    class Meta:
        ordering = ("server", "family", "class_type", "name")
        verbose_name = _("class")
        verbose_name_plural = _("classes")
        constraints = (
            # Windows class names are not case sensitive: "Phones" and "phones" are the same class.
            models.UniqueConstraint(
                "server",
                "family",
                "class_type",
                Lower("name"),
                name="%(app_label)s_%(class)s_unique_name",
                violation_error_message=_("This server already has a class of that type with this name."),
            ),
        )

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return _url("windowsclass", self.pk)

    def get_class_type_color(self):
        return ClassTypeChoices.colors.get(self.class_type)


class OptionDefinition(NetBoxModel):
    """
    An option a server defines, either for every client or (with a vendor class) for one vendor class. Standard
    options (router, DNS servers...) need no definition here: their values name the code directly.
    """

    server = _server_fk("option_definitions")
    family = models.PositiveSmallIntegerField(
        verbose_name=_("address family"),
        choices=FamilyChoices,
        default=FamilyChoices.IPV4,
    )
    vendor_class = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.WindowsClass",
        on_delete=models.CASCADE,
        related_name="option_definitions",
        blank=True,
        null=True,
        limit_choices_to={"class_type": ClassTypeChoices.VENDOR},
        verbose_name=_("vendor class"),
        help_text=_("Leave empty for an option every client can get"),
    )
    code = models.PositiveSmallIntegerField(
        verbose_name=_("code"),
        validators=[MinValueValidator(1), MaxValueValidator(65535)],
    )
    name = models.CharField(verbose_name=_("name"), max_length=255)
    windows_type = models.CharField(
        verbose_name=_("type"),
        max_length=50,
        default="String",
        help_text=_("Windows data type, for example Byte, Word, DWord, IPv4Address, String or BinaryData"),
    )
    multi_valued = models.BooleanField(verbose_name=_("multi-valued"), default=False)
    default_value = models.JSONField(
        verbose_name=_("default value"),
        default=list,
        blank=True,
        help_text=_("Default value list, as Windows stores it"),
    )
    description = models.TextField(verbose_name=_("description"), blank=True)

    class Meta:
        ordering = ("server", "family", "vendor_class", "code")
        verbose_name = _("option definition")
        verbose_name_plural = _("option definitions")
        constraints = (
            models.UniqueConstraint(
                fields=("server", "family", "vendor_class", "code"),
                name="%(app_label)s_%(class)s_unique_code",
                nulls_distinct=False,
            ),
        )

    def __str__(self):
        prefix = f"{self.vendor_class} " if self.vendor_class_id else ""
        return f"{prefix}{self.code} ({self.name})"

    def get_absolute_url(self):
        return _url("optiondefinition", self.pk)

    def clean(self):
        super().clean()
        if not _is_string_list(self.default_value):
            raise ValidationError({"default_value": _("Must be a list of strings.")})
        if self.vendor_class_id is None:
            return
        if self.vendor_class.class_type != ClassTypeChoices.VENDOR:
            raise ValidationError({"vendor_class": _("Must be a vendor class.")})
        if self.server_id and self.vendor_class.server_id != self.server_id:
            raise ValidationError({"vendor_class": _("The vendor class belongs to another server.")})
        if self.vendor_class.family != self.family:
            raise ValidationError({"vendor_class": _("The vendor class is of another address family.")})


class WindowsPolicy(PrimaryModel):
    """A Windows DHCP policy, at server level or (with a scope) at scope level. IPv4 only."""

    server = _server_fk("policies")
    scope = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.Scope",
        on_delete=models.CASCADE,
        related_name="policies",
        blank=True,
        null=True,
        verbose_name=_("scope"),
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
        ordering = ("server", "scope", "processing_order", "name")
        verbose_name = _("policy")
        verbose_name_plural = _("policies")
        constraints = (
            models.UniqueConstraint(
                fields=("server", "scope", "name"),
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
        if self.ip_ranges and self.scope_id is None:
            raise ValidationError({"ip_ranges": _("Only scope-level policies can have IP ranges.")})
        if self.scope_id is not None:
            # server is required; when it is missing the form reports that, so skip the comparison here.
            if self.server_id and self.scope.server_id != self.server_id:
                raise ValidationError({"scope": _("The scope belongs to another server.")})
            if self.scope.family == 6:
                raise ValidationError({"scope": _("Windows has no DHCPv6 policies.")})
        self._clean_criteria()
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

    def _clean_criteria(self):
        criteria = self.criteria
        error = _(
            'Must map a condition ({names}) to a list starting with EQ or NE, for example {{"mac_address": '
            '["EQ", "00-15-5d*"]}}.'
        ).format(names=", ".join(POLICY_CRITERIA_KEYS))
        if not isinstance(criteria, dict):
            raise ValidationError({"criteria": error})
        for name, values in criteria.items():
            if (
                name not in POLICY_CRITERIA_KEYS
                or not _is_string_list(values)
                or len(values) < 2
                or values[0].upper() not in ("EQ", "NE")
            ):
                raise ValidationError({"criteria": error})


class OptionValue(NetBoxModel):
    """
    An option value at server level, on a scope, or on a reservation, optionally for a vendor class, a user class or
    a policy, as Windows allows. The value is a list, as Windows holds it, so commas and spaces survive.
    """

    server = _server_fk("option_values")
    scope = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.Scope",
        on_delete=models.CASCADE,
        related_name="option_values",
        blank=True,
        null=True,
        verbose_name=_("scope"),
        help_text=_("Leave empty (with no reservation) for a server-level value"),
    )
    reservation = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.Reservation",
        on_delete=models.CASCADE,
        related_name="option_values",
        blank=True,
        null=True,
        verbose_name=_("reservation"),
    )
    family = models.PositiveSmallIntegerField(
        verbose_name=_("address family"),
        choices=FamilyChoices,
        default=FamilyChoices.IPV4,
    )
    code = models.PositiveSmallIntegerField(
        verbose_name=_("code"),
        validators=[MinValueValidator(1), MaxValueValidator(65535)],
    )
    # RESTRICT, not PROTECT: a class still in use cannot be deleted on its own, but deleting the server removes the
    # class and its option values together.
    vendor_class = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.WindowsClass",
        on_delete=models.RESTRICT,
        related_name="vendor_class_option_values",
        blank=True,
        null=True,
        limit_choices_to={"class_type": ClassTypeChoices.VENDOR},
        verbose_name=_("vendor class"),
        help_text=_("For an option the vendor class defines"),
    )
    user_class = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.WindowsClass",
        on_delete=models.RESTRICT,
        related_name="user_class_option_values",
        blank=True,
        null=True,
        limit_choices_to={"class_type": ClassTypeChoices.USER},
        verbose_name=_("user class"),
        help_text=_("Only clients of this user class get the value"),
    )
    policy = models.ForeignKey(
        to="netbox_windows_dhcp_plugin.WindowsPolicy",
        on_delete=models.CASCADE,
        related_name="option_values",
        blank=True,
        null=True,
        verbose_name=_("policy"),
        help_text=_("Only clients matching this policy get the value"),
    )
    value = models.JSONField(verbose_name=_("value"), default=list, help_text=_('For example ["10.0.0.1"]'))

    class Meta:
        ordering = ("server", "family", "scope", "reservation", "code")
        verbose_name = _("option value")
        verbose_name_plural = _("option values")
        constraints = (
            models.UniqueConstraint(
                fields=("server", "family", "scope", "reservation", "code", "vendor_class", "user_class", "policy"),
                name="%(app_label)s_%(class)s_unique_value",
                nulls_distinct=False,
            ),
        )

    def __str__(self):
        value = ", ".join(self.value) if _is_string_list(self.value) else self.value
        return f"{self.code} = {value}"

    def get_absolute_url(self):
        return _url("optionvalue", self.pk)

    @property
    def level(self):
        if self.reservation_id:
            return "reservation"
        return "scope" if self.scope_id else "server"

    @property
    def definition(self):
        """This server's definition of the option, if it has one (standard options have none)."""
        return (
            OptionDefinition.objects.filter(
                server_id=self.server_id, family=self.family, vendor_class_id=self.vendor_class_id, code=self.code
            ).first()
            if self.server_id
            else None
        )

    def clean(self):
        super().clean()
        if not _is_string_list(self.value) or not self.value:
            raise ValidationError({"value": _("Must be a non-empty list of strings.")})
        if self.reservation_id is not None:
            # A reservation's value belongs to its scope too, so filters by scope find it.
            if self.scope_id is not None and self.scope_id != self.reservation.scope_id:
                raise ValidationError({"scope": _("The reservation is in another scope.")})
            self.scope_id = self.reservation.scope_id
        if self.scope_id is not None:
            if self.server_id and self.scope.server_id != self.server_id:
                raise ValidationError({"scope": _("The scope belongs to another server.")})
            if self.scope.family != self.family:
                raise ValidationError({"family": _("The scope is IPv{family}.").format(family=self.scope.family)})
        self._clean_classes()
        self._clean_policy()

    def _clean_classes(self):
        for field, class_type in (("vendor_class", ClassTypeChoices.VENDOR), ("user_class", ClassTypeChoices.USER)):
            cls = getattr(self, field) if getattr(self, f"{field}_id") else None
            if cls is None:
                continue
            if cls.class_type != class_type:
                raise ValidationError({field: _("Must be a {type} class.").format(type=class_type)})
            if self.server_id and cls.server_id != self.server_id:
                raise ValidationError({field: _("The class belongs to another server.")})
            if cls.family != self.family:
                # Windows keeps IPv4 and DHCPv6 classes apart.
                raise ValidationError(
                    {field: _("This is an IPv{family} value: use an IPv{family} class.").format(family=self.family)}
                )

    def _clean_policy(self):
        if self.policy_id is None:
            return
        # Set-DhcpServerv4OptionValue accepts -PolicyName with -VendorClass, but not with -UserClass.
        if self.user_class_id is not None:
            raise ValidationError({"user_class": _("A policy value cannot also have a user class.")})
        if self.family == 6:
            raise ValidationError({"policy": _("Windows has no DHCPv6 policies.")})
        if self.reservation_id is not None:
            raise ValidationError({"policy": _("Windows has no policy values on reservations.")})
        if self.server_id and self.policy.server_id != self.server_id:
            raise ValidationError({"policy": _("The policy belongs to another server.")})
        # Windows names the policy of a value by name only: a scope policy's values live on its scope, a server
        # policy's on the server.
        if self.policy.scope_id != self.scope_id:
            raise ValidationError({"policy": _("The policy belongs to another scope (or level) than the value.")})


class FailoverRelationship(PrimaryModel):
    """A Windows DHCPv4 failover relationship between two servers and the scopes it covers."""

    name = models.CharField(verbose_name=_("name"), max_length=126)
    primary_server = models.ForeignKey(
        to=SERVER,
        on_delete=models.CASCADE,
        related_name="failover_primary",
        verbose_name=_("primary server"),
        help_text=_("Changes are written here, then replicated to the partner"),
    )
    partner_server = models.ForeignKey(
        to=SERVER,
        # Deleting the partner must not delete the relationship: the next push would remove failover on Windows.
        on_delete=models.PROTECT,
        related_name="failover_partner",
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
    scopes = models.ManyToManyField(
        to="netbox_windows_dhcp_plugin.Scope",
        related_name="failover_relationships",
        blank=True,
        verbose_name=_("scopes"),
        help_text=_("IPv4 scopes of the primary server"),
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

    @staticmethod
    def scope_error(primary_server, scopes):
        """
        Why these scopes cannot be in a relationship of this primary server, or None. clean() cannot check a
        many-to-many field, so forms and the API call this: a scope of another server (or a DHCPv6 scope) would be
        left out of the relationship by the sync, and the next push would take it out of the relationship on Windows.
        """
        for scope in scopes:
            if primary_server is not None and scope.server_id != primary_server.pk:
                return _("Scope {scope} is not on the primary server.").format(scope=scope)
            if scope.family != 4:
                return _("Windows has no DHCPv6 failover: {scope}.").format(scope=scope)
        return None

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
        self._clean_secret_profile()

    def _clean_secret_profile(self):
        # The secret goes to both partners, so the profile must allow both hosts, as for connection credentials. The
        # push checks again before sending it.
        if not self.shared_secret_profile:
            return
        from ..conf import CredentialError, check_host_allowed

        hosts = []
        # Read the ids first: a missing server is reported as a field error, not a crash.
        for field in ("primary_server", "partner_server"):
            if getattr(self, f"{field}_id") is not None:
                server = getattr(self, field)
                hosts += [server.host, server.computer_name]
        try:
            check_host_allowed(self.shared_secret_profile, *hosts)
        except CredentialError as exc:
            raise ValidationError({"shared_secret_profile": str(exc)}) from exc


class MacFilter(PrimaryModel):
    """A server-wide allow or deny entry, matched on the client MAC address (wildcards allowed)."""

    server = _server_fk("mac_filters")
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
        ordering = ("server", "list", "mac_address")
        verbose_name = _("MAC filter")
        verbose_name_plural = _("MAC filters")
        constraints = (
            models.UniqueConstraint(
                fields=("server", "mac_address"),
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

    def save(self, *args, **kwargs):
        # The REST API saves without running clean() on this instance; keep the one spelling the sync looks up.
        self.mac_address = self.mac_address.strip().lower().replace(":", "-")
        super().save(*args, **kwargs)


class MacFilterLists(NetBoxModel):
    """Whether a server applies its allow and deny lists. Without this object a push leaves the switches alone."""

    server = models.OneToOneField(
        to=SERVER,
        on_delete=models.CASCADE,
        related_name="mac_filter_lists",
        verbose_name=_("server"),
    )
    allow_list_enabled = models.BooleanField(
        verbose_name=_("allow list enabled"),
        default=False,
        help_text=_("Only clients on the allow list get an address"),
    )
    deny_list_enabled = models.BooleanField(
        verbose_name=_("deny list enabled"),
        default=False,
        help_text=_("Clients on the deny list get no address"),
    )

    class Meta:
        ordering = ("server",)
        verbose_name = _("MAC filter lists")
        verbose_name_plural = _("MAC filter lists")

    def __str__(self):
        return str(self.server)

    def get_absolute_url(self):
        return _url("macfilterlists", self.pk)


def _is_string_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _is_hex(value):
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
