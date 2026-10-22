from django.utils.translation import gettext_lazy as _
from netbox.ui import attrs, panels


class WindowsServerPanel(panels.ObjectAttributesPanel):
    title = _("Connection")
    dhcp_server = attrs.RelatedObjectAttr("dhcp_server", linkify=True, label=_("DHCP server"))
    host = attrs.TextAttr("host", style="font-monospace", copy_button=True)
    transport = attrs.ChoiceAttr("transport")
    effective_port = attrs.NumericAttr("effective_port", label=_("Port"))
    credential_profile = attrs.TextAttr("credential_profile", style="font-monospace")
    computer_name = attrs.TextAttr("computer_name", label=_("Remote DHCP server"))
    description = attrs.TextAttr("description")


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


class WindowsScopePanel(panels.ObjectAttributesPanel):
    title = _("Windows scope")
    subnet = attrs.RelatedObjectAttr("subnet", linkify=True)
    state = attrs.ChoiceAttr("state")
    scope_type = attrs.ChoiceAttr("scope_type", label=_("Type"))
    delay_ms = attrs.NumericAttr("delay_ms", label=_("Offer delay (ms)"))
    max_bootp_clients = attrs.NumericAttr("max_bootp_clients", label=_("Max BOOTP clients"))
    activate_policies = attrs.BooleanAttr("activate_policies")
    preference = attrs.NumericAttr("preference")


class WindowsReservationPanel(panels.ObjectAttributesPanel):
    title = _("Windows reservation")
    host_reservation = attrs.RelatedObjectAttr("host_reservation", linkify=True)
    reservation_type = attrs.ChoiceAttr("reservation_type", label=_("Type"))
    iaid = attrs.NumericAttr("iaid", label=_("IAID"))


class ExclusionRangePanel(panels.ObjectAttributesPanel):
    title = _("Exclusion range")
    subnet = attrs.RelatedObjectAttr("subnet", linkify=True)
    start_address = attrs.TextAttr("start_address", style="font-monospace")
    end_address = attrs.TextAttr("end_address", style="font-monospace")
    description = attrs.TextAttr("description")


class WindowsClassPanel(panels.ObjectAttributesPanel):
    title = _("Windows class")
    dhcp_server = attrs.RelatedObjectAttr("dhcp_server", linkify=True, label=_("DHCP server"))
    name = attrs.TextAttr("name")
    class_type = attrs.ChoiceAttr("class_type", label=_("Type"))
    family = attrs.ChoiceAttr("family", label=_("Address family"))
    data = attrs.TextAttr("data", style="font-monospace")
    description = attrs.TextAttr("description")


class WindowsOptionDefinitionPanel(panels.ObjectAttributesPanel):
    title = _("Windows option definition")
    option_definition = attrs.RelatedObjectAttr("option_definition", linkify=True)
    windows_type = attrs.TextAttr("windows_type", label=_("Windows type"))


class WindowsPolicyPanel(panels.ObjectAttributesPanel):
    title = _("Policy")
    dhcp_server = attrs.RelatedObjectAttr("dhcp_server", linkify=True, label=_("DHCP server"))
    subnet = attrs.RelatedObjectAttr("subnet", linkify=True)
    name = attrs.TextAttr("name")
    processing_order = attrs.NumericAttr("processing_order")
    enabled = attrs.BooleanAttr("enabled")
    condition = attrs.ChoiceAttr("condition")
    lease_duration = attrs.NumericAttr("lease_duration", label=_("Lease duration (s)"))
    description = attrs.TextAttr("description")


class WindowsOptionQualifierPanel(panels.ObjectAttributesPanel):
    title = _("Option qualifier")
    option = attrs.RelatedObjectAttr("option", linkify=True)
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
    subnets = attrs.RelatedObjectListAttr("subnets", linkify=True)
    description = attrs.TextAttr("description")


class MacFilterPanel(panels.ObjectAttributesPanel):
    title = _("MAC filter")
    dhcp_server = attrs.RelatedObjectAttr("dhcp_server", linkify=True, label=_("DHCP server"))
    mac_address = attrs.TextAttr("mac_address", style="font-monospace", label=_("MAC address"))
    list = attrs.ChoiceAttr("list")
    description = attrs.TextAttr("description")


class MacFilterListsPanel(panels.ObjectAttributesPanel):
    title = _("MAC filter lists")
    dhcp_server = attrs.RelatedObjectAttr("dhcp_server", linkify=True, label=_("DHCP server"))
    allow_list_enabled = attrs.BooleanAttr("allow_list_enabled", label=_("Allow list enabled"))
    deny_list_enabled = attrs.BooleanAttr("deny_list_enabled", label=_("Deny list enabled"))


class WindowsVendorOptionDefinitionPanel(panels.ObjectAttributesPanel):
    title = _("Vendor option definition")
    vendor_class = attrs.RelatedObjectAttr("vendor_class", linkify=True)
    code = attrs.NumericAttr("code")
    name = attrs.TextAttr("name")
    windows_type = attrs.TextAttr("windows_type", label=_("Windows type"))
    multi_valued = attrs.BooleanAttr("multi_valued", label=_("Multi-valued"))
    description = attrs.TextAttr("description")


class WindowsVendorOptionPanel(panels.ObjectAttributesPanel):
    title = _("Vendor option value")
    definition = attrs.RelatedObjectAttr("definition", linkify=True)
    assigned_object = attrs.GenericForeignKeyAttr("assigned_object", linkify=True, label=_("Set on"))
    user_class = attrs.RelatedObjectAttr("user_class", linkify=True)
    policy = attrs.RelatedObjectAttr("policy", linkify=True)


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
