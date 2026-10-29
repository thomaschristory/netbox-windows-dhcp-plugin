from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from netbox.ui import attrs, panels


def days(seconds):
    """A duration in seconds as days, the unit the Windows DHCP console uses: "(8 days)", "(0.33 days)"."""
    value = round(seconds / 86400, 2)
    if seconds and not value:
        # Under about seven minutes: two decimals would read "0 days".
        return f"(< {0.01:g} {ngettext('day', 'days', 2)})"
    count = int(value) if float(value).is_integer() else 2
    return f"({ngettext('%(n)s day', '%(n)s days', count) % {'n': f'{value:g}'}})"


class DurationAttr(attrs.NumericAttr):
    """Seconds, followed by the same duration in days."""

    def get_context(self, obj, attr, value, context):
        return {**super().get_context(obj, attr, value, context), "unit": days(value)}


class WindowsServerPanel(panels.ObjectAttributesPanel):
    title = _("Windows server")
    name = attrs.TextAttr("name")
    device = attrs.RelatedObjectAttr("device", linkify=True)
    virtual_machine = attrs.RelatedObjectAttr("virtual_machine", linkify=True, label=_("Virtual machine"))
    description = attrs.TextAttr("description")


class WindowsServerConnectionPanel(panels.ObjectAttributesPanel):
    title = _("Connection")
    host = attrs.TextAttr("host", style="font-monospace", copy_button=True)
    transport = attrs.ChoiceAttr("transport")
    effective_port = attrs.NumericAttr("effective_port", label=_("Port"))
    credential_profile = attrs.TextAttr("credential_profile", style="font-monospace")
    computer_name = attrs.TextAttr("computer_name", label=_("Remote DHCP server"))
    reported_names = attrs.TextAttr("reported_names_display", label=_("Reported names"))


class WindowsServerSecurityPanel(panels.ObjectAttributesPanel):
    title = _("Authentication")
    winrm_auth = attrs.ChoiceAttr("winrm_auth", label=_("WinRM authentication"))
    winrm_https = attrs.BooleanAttr("winrm_https", label=_("WinRM over HTTPS"))
    verify_tls = attrs.BooleanAttr("verify_tls", label=_("Verify host"))
    kerberos_realm = attrs.TextAttr("kerberos_realm", label=_("Kerberos realm"))
    kerberos_spn = attrs.TextAttr("kerberos_spn", label=_("Kerberos SPN"))
    ssh_host_key = attrs.TextAttr("ssh_host_key", label=_("SSH host key"), style="font-monospace")


class WindowsServerSyncPanel(panels.ObjectAttributesPanel):
    title = _("Sync")
    sync_enabled = attrs.BooleanAttr("sync_enabled")
    import_interval = attrs.NumericAttr("import_interval", label=_("Import interval (minutes)"))
    import_vrf = attrs.RelatedObjectAttr("import_vrf", linkify=True, label=_("Import VRF"))


class SuperscopePanel(panels.ObjectAttributesPanel):
    title = _("Superscope")
    server = attrs.RelatedObjectAttr("server", linkify=True)
    name = attrs.TextAttr("name")
    description = attrs.TextAttr("description")


class ScopePanel(panels.ObjectAttributesPanel):
    title = _("Scope")
    server = attrs.RelatedObjectAttr("server", linkify=True)
    name = attrs.TextAttr("name")
    scope_id = attrs.TextAttr("scope_id", label=_("Scope ID"), style="font-monospace", copy_button=True)
    prefix = attrs.RelatedObjectAttr("prefix", linkify=True, grouped_by="vrf")
    ip_range = attrs.RelatedObjectAttr("ip_range", linkify=True, label=_("IP range"))
    superscope = attrs.RelatedObjectAttr("superscope", linkify=True)
    state = attrs.ChoiceAttr("state")
    description = attrs.TextAttr("description")


class _FamilyPanel(panels.ObjectAttributesPanel):
    """Settings that only apply to one address family: the panel shows on scopes of that family only."""

    family = None

    def should_render(self, context):
        return getattr(context["object"], "family", None) == self.family


class ScopeIPv4Panel(_FamilyPanel):
    title = _("IPv4 settings")
    family = 4
    scope_type = attrs.ChoiceAttr("scope_type", label=_("Type"))
    lease_duration = DurationAttr("lease_duration", label=_("Lease duration (s)"))
    delay_ms = attrs.NumericAttr("delay_ms", label=_("Offer delay (ms)"))
    max_bootp_clients = attrs.NumericAttr("max_bootp_clients", label=_("Max BOOTP clients"))
    activate_policies = attrs.BooleanAttr("activate_policies")


class ScopeIPv6Panel(_FamilyPanel):
    title = _("DHCPv6 settings")
    family = 6
    preference = attrs.NumericAttr("preference")
    preferred_lifetime = DurationAttr("preferred_lifetime", label=_("Preferred lifetime (s)"))
    valid_lifetime = DurationAttr("valid_lifetime", label=_("Valid lifetime (s)"))
    renew_timer = DurationAttr("renew_timer", label=_("T1 (s)"))
    rebind_timer = DurationAttr("rebind_timer", label=_("T2 (s)"))


class ReservationPanel(panels.ObjectAttributesPanel):
    title = _("Reservation")
    scope = attrs.RelatedObjectAttr("scope", linkify=True, grouped_by="server")
    ip_address = attrs.RelatedObjectAttr("ip_address", linkify=True, label=_("IP address"))
    name = attrs.TextAttr("name")
    description = attrs.TextAttr("description")


class ReservationClientPanel(panels.ObjectAttributesPanel):
    title = _("Client")
    mac_address = attrs.RelatedObjectAttr("mac_address", linkify=True, label=_("MAC address"))
    client_id = attrs.TextAttr("client_id", label=_("Client ID"), style="font-monospace")
    reservation_type = attrs.ChoiceAttr("reservation_type", label=_("Type"))
    duid = attrs.TextAttr("duid", label=_("DUID"), style="font-monospace")
    iaid = attrs.NumericAttr("iaid", label=_("IAID"))


class ExclusionRangePanel(panels.ObjectAttributesPanel):
    title = _("Exclusion range")
    scope = attrs.RelatedObjectAttr("scope", linkify=True, grouped_by="server")
    start_address = attrs.TextAttr("start_address", style="font-monospace")
    end_address = attrs.TextAttr("end_address", style="font-monospace")
    description = attrs.TextAttr("description")


class WindowsClassPanel(panels.ObjectAttributesPanel):
    title = _("Windows class")
    server = attrs.RelatedObjectAttr("server", linkify=True)
    name = attrs.TextAttr("name")
    class_type = attrs.ChoiceAttr("class_type", label=_("Type"))
    family = attrs.ChoiceAttr("family", label=_("Address family"))
    data = attrs.TextAttr("data", style="font-monospace")
    description = attrs.TextAttr("description")


class OptionDefinitionPanel(panels.ObjectAttributesPanel):
    title = _("Option definition")
    server = attrs.RelatedObjectAttr("server", linkify=True)
    family = attrs.ChoiceAttr("family", label=_("Address family"))
    vendor_class = attrs.RelatedObjectAttr("vendor_class", linkify=True)
    code = attrs.NumericAttr("code")
    name = attrs.TextAttr("name")
    windows_type = attrs.TextAttr("windows_type", label=_("Windows type"))
    multi_valued = attrs.BooleanAttr("multi_valued", label=_("Multi-valued"))
    default_value = attrs.ArrayAttr("default_value", label=_("Default value"), style="font-monospace")
    description = attrs.TextAttr("description")


class WindowsPolicyPanel(panels.ObjectAttributesPanel):
    title = _("Policy")
    server = attrs.RelatedObjectAttr("server", linkify=True)
    scope = attrs.RelatedObjectAttr("scope", linkify=True)
    name = attrs.TextAttr("name")
    processing_order = attrs.NumericAttr("processing_order")
    enabled = attrs.BooleanAttr("enabled")
    condition = attrs.ChoiceAttr("condition")
    lease_duration = DurationAttr("lease_duration", label=_("Lease duration (s)"))
    description = attrs.TextAttr("description")


class OptionNameAttr(attrs.TextAttr):
    """The option's name, from the server's definition or the standard option list."""

    def get_value(self, obj):
        from .options import option_name

        return option_name(obj)


class OptionValuePanel(panels.ObjectAttributesPanel):
    title = _("Option value")
    server = attrs.RelatedObjectAttr("server", linkify=True)
    scope = attrs.RelatedObjectAttr("scope", linkify=True)
    reservation = attrs.RelatedObjectAttr("reservation", linkify=True)
    family = attrs.ChoiceAttr("family", label=_("Address family"))
    code = attrs.NumericAttr("code")
    option = OptionNameAttr("code", label=_("Option"))
    definition = attrs.RelatedObjectAttr("definition", linkify=True)
    value = attrs.ArrayAttr("value", style="font-monospace", copy_button=True)


class OptionQualifiersPanel(panels.ObjectAttributesPanel):
    title = _("Qualifiers")
    vendor_class = attrs.RelatedObjectAttr("vendor_class", linkify=True)
    user_class = attrs.RelatedObjectAttr("user_class", linkify=True)
    policy = attrs.RelatedObjectAttr("policy", linkify=True)


class FailoverRelationshipPanel(panels.ObjectAttributesPanel):
    title = _("Failover relationship")
    name = attrs.TextAttr("name")
    primary_server = attrs.RelatedObjectAttr("primary_server", linkify=True)
    partner_server = attrs.RelatedObjectAttr("partner_server", linkify=True)
    mode = attrs.ChoiceAttr("mode")
    max_client_lead_time = attrs.NumericAttr("max_client_lead_time", label=_("Maximum client lead time (s)"))
    load_balance_percent = attrs.NumericAttr("load_balance_percent", label=_("Load balance (%)"))
    reserve_percent = attrs.NumericAttr("reserve_percent", label=_("Reserve (%)"))
    server_role = attrs.ChoiceAttr("server_role", label=_("Primary server role"))
    state_switch_interval = attrs.NumericAttr("state_switch_interval", label=_("State switchover interval (s)"))
    shared_secret_profile = attrs.TextAttr("shared_secret_profile", style="font-monospace")
    scopes = attrs.RelatedObjectListAttr("scopes", linkify=True)
    description = attrs.TextAttr("description")


class MacFilterPanel(panels.ObjectAttributesPanel):
    title = _("MAC filter")
    server = attrs.RelatedObjectAttr("server", linkify=True)
    mac_address = attrs.TextAttr("mac_address", style="font-monospace", label=_("MAC address"))
    list = attrs.ChoiceAttr("list")
    description = attrs.TextAttr("description")


class MacFilterListsPanel(panels.ObjectAttributesPanel):
    title = _("MAC filter lists")
    server = attrs.RelatedObjectAttr("server", linkify=True)
    allow_list_enabled = attrs.BooleanAttr("allow_list_enabled", label=_("Allow list enabled"))
    deny_list_enabled = attrs.BooleanAttr("deny_list_enabled", label=_("Deny list enabled"))


class SyncRunPanel(panels.ObjectAttributesPanel):
    title = _("Sync run")
    windows_server = attrs.RelatedObjectAttr("windows_server", linkify=True)
    kind = attrs.ChoiceAttr("kind")
    status = attrs.ChoiceAttr("status")
    branch_name = attrs.TextAttr("branch_name", label=_("Branch"))
    job = attrs.RelatedObjectAttr("job", linkify=True)
    started = attrs.DateTimeAttr("started")
    completed = attrs.DateTimeAttr("completed")
    error = attrs.TextAttr("error", style="font-monospace")


class ImportProposalPanel(panels.ObjectAttributesPanel):
    title = _("Import proposal")
    windows_server = attrs.RelatedObjectAttr("windows_server", linkify=True)
    status = attrs.ChoiceAttr("status")
    sync_run = attrs.RelatedObjectAttr("sync_run", linkify=True)


class ImportProposalItemPanel(panels.ObjectAttributesPanel):
    title = _("Item")
    proposal = attrs.RelatedObjectAttr("proposal", linkify=True)
    action = attrs.ChoiceAttr("action")
    status = attrs.ChoiceAttr("status")
    kind = attrs.TextAttr("kind")
    key = attrs.TextAttr("key", label=_("Windows key"), style="font-monospace")
    object = attrs.GenericForeignKeyAttr("object", linkify=True)
    error = attrs.TextAttr("error", style="font-monospace")


class ImportStatusPanel(panels.ObjectPanel):
    """Where the latest import went (server page) or where this one went (sync run page), see import_status.py."""

    title = _("Import")
    template_name = "netbox_windows_dhcp_plugin/inc/import_status.html"

    def get_context(self, context):
        from django.urls import reverse

        from .. import import_status
        from ..choices import SyncKindChoices
        from ..models import SyncRun

        ctx = super().get_context(context)
        obj = ctx["object"]
        user = ctx["request"].user
        if isinstance(obj, SyncRun):
            ctx["status"] = import_status.for_run(obj, user) if obj.kind == SyncKindChoices.IMPORT else None
            ctx["refresh_url"] = None
        else:
            ctx["status"] = import_status.for_server(obj, user)
            ctx["refresh_url"] = reverse(
                "plugins:netbox_windows_dhcp_plugin:windowsserver_import_status", args=[obj.pk]
            )
        return ctx

    def should_render(self, context):
        return context["status"] is not None
