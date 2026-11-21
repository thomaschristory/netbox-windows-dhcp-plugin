"""
The DHCP configuration of a Windows server, as Windows holds it.

Every model here is branchable: it is reviewed, merged and pushed like any other NetBox data. Scopes and reservations
point at core NetBox objects (prefix, IP range, IP address, MAC address), so the addresses show up in IPAM too.
"""

import ipaddress
import re
from copy import copy, deepcopy

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
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


class _ParentMoveValidationMixin:
    """Validate existing children against changed parent identities, including saves made by branch merges."""

    def clean(self):
        super().clean()
        self._validate_parent_move()

    def save(self, *args, **kwargs):
        # NetBox's API validates a copy; branch merges and other callers can save without calling clean().
        self._validate_parent_move(update_fields=kwargs.get("update_fields"), using=kwargs.get("using"))
        return super().save(*args, **kwargs)

    def _validate_parent_move(self, update_fields=None, using=None):
        if self.pk is None:
            return
        fields = {name: self._meta.get_field(name).attname for name in self._parent_fields}
        written = (
            fields
            if update_fields is None
            else {
                name: attname for name, attname in fields.items() if name in update_fields or attname in update_fields
            }
        )
        if not written:
            return
        previous = type(self).objects.using(using).filter(pk=self.pk).values(*fields.values()).first()
        if previous is None:
            return
        changed = {name for name, attname in written.items() if previous[attname] != getattr(self, attname)}
        if not changed:
            return
        candidate = self
        if len(written) != len(fields):
            candidate = copy(self)
            for name, attname in fields.items():
                if name not in written:
                    setattr(candidate, attname, previous[attname])
        candidate._clean_parent_move(changed)


class Superscope(_ParentMoveValidationMixin, NetBoxModel):
    """A Windows superscope: IPv4 scopes of one server served together on the same network segment."""

    server = _server_fk("superscopes")
    name = models.CharField(verbose_name=_("name"), max_length=255)
    description = models.CharField(verbose_name=_("description"), max_length=200, blank=True)
    comments = models.TextField(verbose_name=_("comments"), blank=True)
    _parent_fields = ("server",)

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

    def _clean_parent_move(self, changed):
        if self.server_id and self.scopes.exclude(server_id=self.server_id).exists():
            raise ValidationError({"server": _("Remove or reassign this superscope's scopes before changing server.")})


class Scope(_ParentMoveValidationMixin, NetBoxModel):
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
    _parent_fields = ("server", "prefix")

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
            # The push refuses any other length too (the prefix can still change in IPAM later); saying it here
            # saves a merge and a failed push. Windows only reports /64 scopes, so imports are not affected.
            if network.prefixlen != 64:
                raise ValidationError(
                    {
                        "prefix": _("Windows DHCPv6 scopes are /64 prefixes; this one is /{length}.").format(
                            length=network.prefixlen
                        )
                    }
                )
            self._clean_v6_timers()
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

    def _clean_v6_timers(self):
        """
        Windows wants T1 < T2 < preferred lifetime <= valid lifetime. Only pairs set in NetBox are judged: an empty
        field keeps what Windows has, which is not known here, so that case is still left to Windows at push time.
        """
        rules = (
            ("preferred_lifetime", "valid_lifetime", True, _("The preferred lifetime cannot exceed the valid one.")),
            ("renew_timer", "rebind_timer", False, _("T1 must be lower than T2.")),
            ("rebind_timer", "preferred_lifetime", False, _("T2 must be lower than the preferred lifetime.")),
            ("renew_timer", "preferred_lifetime", False, _("T1 must be lower than the preferred lifetime.")),
        )
        # 0 in T1 or T2 means "let the server work it out": the sync reads it as empty, so it is not judged either.
        # A lifetime of 0 is sent to Windows as a real value and stays judged.
        timers = {"renew_timer", "rebind_timer"}
        for low_field, high_field, may_be_equal, message in rules:
            low, high = getattr(self, low_field), getattr(self, high_field)
            if low_field in timers:
                low = low or None
            if high_field in timers:
                high = high or None
            if low is None or high is None:
                continue
            if low > high or (low == high and not may_be_equal):
                raise ValidationError({low_field: message})

    def _clean_parent_move(self, changed):
        values = OptionValue.objects.filter(Q(scope_id=self.pk) | Q(reservation__scope_id=self.pk))
        if "server" in changed and self.server_id:
            if (
                self.policies.exclude(server_id=self.server_id).exists()
                or values.exclude(server_id=self.server_id).exists()
            ):
                raise ValidationError(
                    {"server": _("Remove or reassign this scope's policies and option values before changing server.")}
                )
            if self.failover_relationships.exclude(primary_server_id=self.server_id).exists():
                raise ValidationError({"server": _("Remove this scope from failover before changing server.")})
        if "prefix" not in changed or not self.prefix_id:
            return
        network = self.network
        if values.exclude(family=network.version).exists():
            raise ValidationError({"prefix": _("The new prefix would invalidate this scope's option values.")})
        if network.version == 6 and (self.policies.exists() or self.failover_relationships.exists()):
            raise ValidationError({"prefix": _("Remove policies and failover membership before changing to DHCPv6.")})
        for reservation in self.reservations.select_related("ip_address"):
            if ipaddress.ip_address(str(reservation.ip_address.address.ip)) not in network:
                raise ValidationError({"prefix": _("The new prefix must contain all existing reservation addresses.")})
        for exclusion in self.exclusion_ranges.all():
            if (
                network.version != 4
                or ipaddress.ip_address(exclusion.start_address) not in network
                or ipaddress.ip_address(exclusion.end_address) not in network
            ):
                raise ValidationError({"prefix": _("The new prefix must contain all existing exclusion ranges.")})
        for policy in self.policies.all():
            try:
                outside = any(
                    ipaddress.ip_address(start) not in network or ipaddress.ip_address(end) not in network
                    for start, end in policy.ip_ranges or []
                )
            except (TypeError, ValueError):
                # Only a raw save (a branch merge, a script, nbshell) can store ranges that do not parse.
                raise ValidationError(
                    {
                        "prefix": _(
                            "The IP ranges stored on policy {policy} are not a list of [start, end] addresses; fix "
                            "them before changing the prefix."
                        ).format(policy=policy.name)
                    }
                ) from None
            if outside:
                raise ValidationError({"prefix": _("The new prefix must contain all existing policy IP ranges.")})


class Reservation(_ParentMoveValidationMixin, NetBoxModel):
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
    _parent_fields = ("scope",)

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
            self._clean_identity("client_id")
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
            self._clean_identity("duid")

    def _clean_identity(self, field):
        """A client ID or DUID is a string of bytes: refuse anything else here, the push could never send it."""
        value = getattr(self, field)
        if not value:
            return
        canonical = _canonical_hex(value)
        if canonical is None:
            raise ValidationError(
                {field: _("Use pairs of hex digits, optionally separated by '-', ':' or '.', for example 01-aa-bb-cc.")}
            )
        setattr(self, field, canonical)

    def save(self, *args, **kwargs):
        # The REST API saves without running clean() on this instance; keep the one spelling Windows reports, so
        # filters find the reservation however the identity was typed. A value that is not hex is left as it is.
        self.client_id = _canonical_hex(self.client_id) or self.client_id
        self.duid = _canonical_hex(self.duid) or self.duid
        return super().save(*args, **kwargs)

    def _clean_parent_move(self, changed):
        if (
            self.scope_id
            and self.option_values.exclude(
                scope_id=self.scope_id, server_id=self.scope.server_id, family=self.scope.family
            ).exists()
        ):
            raise ValidationError(
                {"scope": _("Remove or reassign this reservation's option values before changing scope.")}
            )


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


class WindowsClass(_ParentMoveValidationMixin, PrimaryModel):
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
    _parent_fields = ("server", "family", "class_type")

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

    def _clean_parent_move(self, changed):
        references = (
            (self.option_definitions, ClassTypeChoices.VENDOR),
            (self.vendor_class_option_values, ClassTypeChoices.VENDOR),
            (self.user_class_option_values, ClassTypeChoices.USER),
        )
        for related, expected_type in references:
            invalid = related.all()
            if self.class_type == expected_type:
                invalid = invalid.exclude(server_id=self.server_id, family=self.family)
            if invalid.exists():
                field = next(name for name in self._parent_fields if name in changed)
                raise ValidationError(
                    {field: _("Remove or reassign this class's option definitions and values before changing it.")}
                )


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
        self.windows_type = _canonical_option_type(self.windows_type)
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

    def save(self, *args, **kwargs):
        # The REST API saves without running clean() on this instance: store the spelling Windows reports.
        self.windows_type = _canonical_option_type(self.windows_type)
        return super().save(*args, **kwargs)


class WindowsPolicy(_ParentMoveValidationMixin, PrimaryModel):
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
    _parent_fields = ("server", "scope")

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
        network = self.scope.network if self.scope_id is not None else None
        for pair in self.ip_ranges:
            # Strings only: ip_address() also reads numbers (1 is 0.0.0.1), and the sync cannot read those back, so
            # one such policy would stop every push and import of its server.
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and all(isinstance(a, str) for a in pair)):
                raise ValidationError({"ip_ranges": _("Must be a list of [start, end] pairs.")})
            try:
                start, end = (ipaddress.ip_address(a) for a in pair)
            except ValueError as exc:
                raise ValidationError({"ip_ranges": _("Must be a list of [start, end] pairs.")}) from exc
            if start.version != end.version:
                raise ValidationError({"ip_ranges": _("Start and end addresses must be of the same family.")})
            if start.version != 4:
                raise ValidationError({"ip_ranges": _("Policy IP ranges are IPv4 addresses.")})
            if start > end:
                raise ValidationError({"ip_ranges": _("Range {start}-{end} is reversed.").format(start=start, end=end)})
            if network is not None and (start not in network or end not in network):
                raise ValidationError(
                    {
                        "ip_ranges": _("Range {start}-{end} is outside the scope {prefix}.").format(
                            start=start, end=end, prefix=network
                        )
                    }
                )

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
        stored = getattr(self, "_stored_criteria", None)
        if not self._state.adding and stored is not None and criteria == stored:
            # Conditions saved before this check (0.6.0 and earlier) must not block editing another field of the
            # policy: the push names them as "not pushed" until they are fixed (sync/push.py _unpushable).
            return
        from ..integrations.branching import applying_branch

        if applying_branch(self):
            # A branch merge or revert replays what the branch (or main before it) held, and cannot be fixed half-way:
            # reverting a fix, or a deleted policy, puts an older condition back. The push names it instead.
            return
        for value in (criteria.get("mac_address") or [])[1:]:
            mac_error = _mac_condition_error(value)
            if mac_error:
                raise ValidationError(
                    {"criteria": _("MAC address {value!r}: {error}").format(value=value, error=mac_error)}
                )

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        # The conditions as stored, so clean() only judges conditions written now. A deferred field is not
        # remembered, and clean() then judges the conditions as new.
        if "criteria" in instance.__dict__:
            instance._stored_criteria = deepcopy(instance.criteria)
        return instance

    def save(self, *args, **kwargs):
        result = super().save(*args, **kwargs)
        # What was just written is the stored value now (not when the save left the conditions out).
        update_fields = kwargs.get("update_fields")
        if (update_fields is None or "criteria" in update_fields) and "criteria" in self.__dict__:
            self._stored_criteria = deepcopy(self.criteria)
        return result

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        # Only a field that was reread is the stored value again.
        if (fields is None or "criteria" in fields) and "criteria" in self.__dict__:
            self._stored_criteria = deepcopy(self.criteria)

    def _clean_parent_move(self, changed):
        if self.option_values.exclude(server_id=self.server_id, scope_id=self.scope_id).exists():
            field = "server" if "server" in changed else "scope"
            raise ValidationError(
                {field: _("Remove or reassign this policy's option values before changing its server or scope.")}
            )


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

    # Whether clean() checks the partner against the primary's credential profile (see _clean_partner_host).
    check_partner_host = True
    # Whether clean() checks the primary's connection against the secret profile's limits (see
    # _clean_secret_profile). An import turns both off: it records what Windows already has.
    check_secret_profile = True

    # The fields clean() compares with their stored values.
    _REMEMBERED = ("primary_server_id", "partner_server_id", "shared_secret_profile")

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._remember_stored()
        return instance

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        # What was just read is the stored value again; a field not reread keeps what was remembered for it.
        names = None
        if fields is not None:
            # A foreign key may be named with or without its "_id".
            names = {f"{f}_id" if f in ("primary_server", "partner_server") else f for f in fields}
        self._remember_stored(names)

    def _remember_stored(self, names=None):
        # The servers and secret profile as stored, so clean() can tell a choice made now from one already on Windows.
        # A deferred field is not remembered, and clean() then checks as for a new choice.
        stored = dict(getattr(self, "_stored", {}))
        for attr in self._REMEMBERED:
            if attr in self.__dict__ and (names is None or attr in names):
                stored[attr] = self.__dict__[attr]
        self._stored = stored

    def _unchanged(self, *attrs):
        """Whether these fields hold the values last read from the database (False for a new relationship)."""
        stored = getattr(self, "_stored", {})
        return not self._state.adding and all(a in stored and stored[a] == getattr(self, a) for a in attrs)

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
        self._clean_partner_host()
        self._clean_secret_profile()

    def _clean_partner_host(self):
        # Creating the relationship makes the primary talk to the partner as the account of the primary's credential
        # profile, so the partner is one more host those credentials reach: the push refuses a partner outside the
        # profile's allowed_hosts (connection.failover_partner_check). Saying it here saves a merge and a failed push.
        # Only a pair chosen in NetBox is judged (a new relationship, or a new primary or partner, which the push
        # creates on the primary): a relationship Windows already has is never created again, so tightening a profile
        # must not block editing it. An import records what Windows already has and turns this off. An unknown
        # profile is reported when a job runs, as for the server itself.
        if not self.check_partner_host or self.primary_server_id is None or self.partner_server_id is None:
            return
        if self._unchanged("primary_server_id", "partner_server_id"):
            return
        from ..conf import CredentialError, get_setting, host_allowed

        primary = self.primary_server
        profile = get_setting("credentials").get(primary.credential_profile)
        if profile is None:
            return
        partner = self.partner_server.dhcp_host
        try:
            allowed = host_allowed(primary.credential_profile, profile, partner)
        except CredentialError as exc:
            raise ValidationError({"partner_server": str(exc)}) from exc
        if not allowed:
            raise ValidationError(
                {
                    "partner_server": _(
                        "Credential profile {profile} of the primary server may not be used with the partner host "
                        "{host}; add it to the profile's allowed_hosts."
                    ).format(profile=primary.credential_profile, host=partner)
                }
            )

    def _clean_secret_profile(self):
        # The secret goes to both partners, so the profile must allow both hosts, as for connection credentials. The
        # push checks again before sending it.
        if not self.shared_secret_profile:
            return
        from ..conf import CredentialError, check_host_allowed, check_secret_connection

        hosts = []
        # Read the ids first: a missing server is reported as a field error, not a crash.
        for field in ("primary_server", "partner_server"):
            if getattr(self, f"{field}_id") is not None:
                server = getattr(self, field)
                hosts += [server.host, server.computer_name]
        try:
            check_host_allowed(self.shared_secret_profile, *hosts)
            # The secret travels over the primary server's connection: the secret profile's limits (transports, ports,
            # realms, SSH host keys, CredSSP consent) apply to it (issue #103). Judged only for a secret profile or
            # primary chosen now, like the partner check: tightening those limits must not block editing a
            # relationship, and an import records what Windows has. (The allowed_hosts check above still runs on every
            # save.) The push checks again before sending the secret, which catches a later change to the server.
            chosen = not self._unchanged("primary_server_id", "shared_secret_profile")
            if self.check_secret_profile and chosen and self.primary_server_id is not None:
                check_secret_connection(self.shared_secret_profile, self.primary_server)
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


_HEX_SEPARATORS = re.compile(r"[:\-. ]+")
_MAC_CONDITION_BODY = re.compile(r"[0-9A-Fa-f-]*")


def _mac_condition_problem(value):
    """
    Which rule a policy's MAC address condition breaks, or None. A copy of windows/normalize.mac_condition_problem,
    which models must not import; tests_netbox check that the two agree.
    """
    text = str(value)
    if text != text.strip():
        return "spaces"
    if "*" in text[:-1]:
        return None  # a wildcard elsewhere is Windows' own rule (#18)
    wildcard = text.endswith("*")
    body = text[:-1].removesuffix("-") if wildcard else text
    if not _MAC_CONDITION_BODY.fullmatch(body):
        return "characters"
    groups = body.split("-")
    digits = "".join(groups)
    if not wildcard and len(digits) != 12:
        return "length"
    # A prefix whose first group alone has one digit is read as meant: Windows pads it on the left (2-4e-42-ab* is
    # stored as 02-4e-42-ab-*).
    lead = groups[1:] if wildcard and len(groups) > 1 and len(groups[0]) == 1 else groups
    if (len(groups) > 1 and any(len(g) != 2 for g in lead)) or (wildcard and text != "*" and not digits):
        return "groups"
    if wildcard and len(digits) % 2 and lead is groups:
        return "half"
    return None


_MAC_CONDITION_MESSAGES = {
    "spaces": _("remove the spaces or line breaks around it."),
    "characters": _("use hex digits, with dashes or no separator (02-4e-42-ab-cd-ef); Windows refuses colons."),
    "groups": _(
        "write each byte as two hex digits, separated by dashes or with no separator; Windows ignores the dashes, "
        "and pads a prefix with an odd number of digits on the left, so 02-4-42-* would match 00-24-42-*."
    ),
    "length": _("a MAC address has 12 hex digits; end it with * to match a prefix (02-4e-42-*)."),
    "half": _("a prefix must be whole bytes; Windows would read 024e4* as 00-24-e4-*."),
}


def _mac_condition_error(value):
    """
    Why a policy's MAC address condition would not reach Windows as meant, or None.

    Seen on Windows Server 2025 (#110): Windows refuses colons and a full address without its 12 digits, and for a
    prefix it ignores the dashes and joins the digits, padding an odd count with a zero on the left. So
    ``02-4e-42-a*`` would match ``00-24-e4-2a-*`` and ``02-4-42-ab*`` would match ``00-24-42-ab-*``: other devices
    than the ones meant. Each byte must therefore be two hex digits.
    """
    problem = _mac_condition_problem(value)
    return _MAC_CONDITION_MESSAGES[problem] if problem else None


def _canonical_hex(value):
    """
    A client ID or DUID as Windows reports it (``aa-bb-cc``), or None when the text is not a string of bytes.

    Reads what the push reads (windows/normalize.canonical_mac, which models must not import): ':', '-', '.' or
    spaces between groups, and single digits padded when every group is one byte (``0:1b:21:a:b:c``).
    """
    groups = [g for g in _HEX_SEPARATORS.split(str(value).strip()) if g]
    if len(groups) > 1 and any(len(g) % 2 for g in groups):
        if any(len(g) > 2 for g in groups):
            return None
        groups = [g.zfill(2) for g in groups]
    digits = "".join(groups)
    if not digits or len(digits) % 2:
        return None
    try:
        raw = bytes.fromhex(digits)
    except ValueError:
        return None
    return "-".join(f"{b:02x}" for b in raw)


# The spellings Windows reports for its option data types. The 64-bit type is left out on purpose: the code and
# the documents disagree on its spelling.
# unverified: how Windows spells the 64-bit option type (DWordDword or DWordDWord) when it reads one back.
_OPTION_TYPES = {
    name.lower(): name
    for name in ("Byte", "Word", "DWord", "IPv4Address", "IPv6Address", "String", "BinaryData", "EncapsulatedData")
}


def _canonical_option_type(value):
    """
    A Windows option data type in the spelling Windows reports (``string`` becomes ``String``). Windows takes the
    type without case and reads it back in its own spelling. A type not known here is kept as typed, so a type a
    newer Windows adds can still be imported.
    """
    text = str(value or "").strip()
    return _OPTION_TYPES.get(text.lower(), text)
