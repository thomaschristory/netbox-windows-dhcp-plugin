from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from netbox.models import PrimaryModel
from netbox.models.features import JobsMixin

from ..choices import TransportChoices, WinRMAuthChoices
from ..conf import CredentialError, get_setting, host_allowed, normalise_host

__all__ = ("WindowsServer",)

DEFAULT_PORTS = {
    (TransportChoices.SSH, False): 22,
    (TransportChoices.SSH, True): 22,
    (TransportChoices.WINRM, True): 5986,
    (TransportChoices.WINRM, False): 5985,
}


class WindowsServer(JobsMixin, PrimaryModel):
    """
    How to reach the Windows host behind a netbox-plugin-dhcp DHCPServer.

    Credentials are never stored here: `credential_profile` names an entry of the plugin's `credentials` setting,
    which reads them from the environment or files. This model is not branchable (see integrations.branching).
    """

    dhcp_server = models.OneToOneField(
        to="netbox_dhcp.DHCPServer",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_windows_server",
        verbose_name=_("DHCP server"),
    )
    host = models.CharField(
        verbose_name=_("host"),
        max_length=255,
        help_text=_("Hostname or IP address to connect to"),
    )
    transport = models.CharField(
        verbose_name=_("transport"),
        max_length=10,
        choices=TransportChoices,
        default=TransportChoices.WINRM,
    )
    port = models.PositiveIntegerField(
        verbose_name=_("port"),
        blank=True,
        null=True,
        help_text=_("Defaults to 5986 (WinRM over HTTPS), 5985 (WinRM over HTTP) or 22 (SSH)"),
    )
    credential_profile = models.CharField(
        verbose_name=_("credential profile"),
        max_length=100,
        help_text=_("Name of a credential profile from the plugin configuration"),
    )
    winrm_auth = models.CharField(
        verbose_name=_("WinRM authentication"),
        max_length=10,
        choices=WinRMAuthChoices,
        default=WinRMAuthChoices.NTLM,
    )
    winrm_https = models.BooleanField(
        verbose_name=_("WinRM over HTTPS"),
        default=True,
        help_text=_("Over HTTP, NTLM and Kerberos messages are still encrypted"),
    )
    verify_tls = models.BooleanField(
        verbose_name=_("verify host"),
        default=True,
        help_text=_("Verify the TLS certificate (WinRM) or the host key (SSH)"),
    )
    kerberos_realm = models.CharField(
        verbose_name=_("Kerberos realm"),
        max_length=255,
        blank=True,
    )
    kerberos_spn = models.CharField(
        verbose_name=_("Kerberos SPN"),
        max_length=255,
        blank=True,
        help_text=_("Defaults to WSMAN/<host>"),
    )
    ssh_host_key = models.TextField(
        verbose_name=_("SSH host key"),
        blank=True,
        help_text=_("Expected host key in authorized_keys format, for example ssh-ed25519 AAAA..."),
    )
    computer_name = models.CharField(
        verbose_name=_("remote DHCP server"),
        max_length=255,
        blank=True,
        help_text=_(
            "Run the cmdlets against this server through the host (-ComputerName). Credentials may not reach "
            "it (double hop), so connecting directly is recommended."
        ),
    )
    import_vrf = models.ForeignKey(
        to="ipam.VRF",
        on_delete=models.SET_NULL,
        related_name="+",
        blank=True,
        null=True,
        verbose_name=_("import VRF"),
        help_text=_("VRF for prefixes, ranges and addresses created by imports (global when empty)"),
    )
    sync_enabled = models.BooleanField(
        verbose_name=_("sync enabled"),
        default=True,
        help_text=_("Push NetBox changes to this server"),
    )
    import_interval = models.PositiveIntegerField(
        verbose_name=_("import interval"),
        blank=True,
        null=True,
        help_text=_("Minutes between scheduled imports (no schedule when empty)"),
    )

    clone_fields = (
        "transport",
        "port",
        "credential_profile",
        "winrm_auth",
        "winrm_https",
        "verify_tls",
        "kerberos_realm",
        "import_vrf",
        "sync_enabled",
        "import_interval",
    )

    class Meta:
        ordering = ("dhcp_server",)
        verbose_name = _("Windows server")
        verbose_name_plural = _("Windows servers")

    def __str__(self):
        return str(self.dhcp_server)

    def get_absolute_url(self):
        return reverse("plugins:netbox_windows_dhcp_plugin:windowsserver", args=[self.pk])

    def get_transport_color(self):
        return TransportChoices.colors.get(self.transport)

    @property
    def effective_port(self):
        if self.port:
            return self.port
        return DEFAULT_PORTS[(self.transport, self.winrm_https)]

    def clean(self):
        super().clean()
        self._clean_allowed_hosts()
        if self.transport == TransportChoices.SSH and not self.verify_tls:
            raise ValidationError(
                {
                    "verify_tls": _(
                        "SSH always checks the host key; set an SSH host key or use the worker's known_hosts."
                    )
                }
            )
        if self.transport != TransportChoices.WINRM:
            return
        if self.winrm_auth == WinRMAuthChoices.KERBEROS and not self.kerberos_realm:
            raise ValidationError({"kerberos_realm": _("A realm is required for Kerberos authentication.")})
        if self.winrm_auth == WinRMAuthChoices.BASIC and not self.winrm_https:
            raise ValidationError({"winrm_https": _("Basic authentication requires HTTPS.")})
        if self.winrm_auth == WinRMAuthChoices.BASIC and not self.verify_tls:
            # Basic sends the password itself; without verification anyone in the path can read it.
            raise ValidationError({"verify_tls": _("Basic authentication requires certificate verification.")})

    @property
    def dhcp_host(self):
        """The DHCP server itself: computer_name when the connection goes through a management host."""
        return self.computer_name or self.host

    def _clean_allowed_hosts(self):
        # Stop a profile's credentials from being pointed at a host it does not cover. The connection code checks
        # again before connecting. An unknown profile is reported when a job runs, as before.
        errors = {}
        for field in ("host", "computer_name"):
            value = getattr(self, field)
            if value and normalise_host(value) is None:
                errors[field] = _("Enter a host name or IP address only (no port, user, path or spaces).")
        if errors:
            raise ValidationError(errors)
        profile = get_setting("credentials").get(self.credential_profile)
        if profile is None:
            return
        for field in ("host", "computer_name"):
            value = getattr(self, field)
            try:
                allowed = not value or host_allowed(self.credential_profile, profile, value)
            except CredentialError as exc:
                raise ValidationError({"credential_profile": str(exc)}) from exc
            if not allowed:
                errors[field] = _("Credential profile {profile} may not be used with this host.").format(
                    profile=self.credential_profile
                )
        if errors:
            raise ValidationError(errors)
