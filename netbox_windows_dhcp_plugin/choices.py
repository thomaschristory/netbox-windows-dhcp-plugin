from django.utils.translation import gettext_lazy as _
from utilities.choices import ChoiceSet

__all__ = (
    "POLICY_CRITERIA_KEYS",
    "ClassTypeChoices",
    "FailoverModeChoices",
    "FailoverServerRoleChoices",
    "MacFilterListChoices",
    "PolicyConditionChoices",
    "ProposalItemActionChoices",
    "ProposalItemStatusChoices",
    "ProposalStatusChoices",
    "ReservationTypeChoices",
    "ScopeStateChoices",
    "ScopeTypeChoices",
    "SyncKindChoices",
    "SyncStatusChoices",
    "TransportChoices",
    "WinRMAuthChoices",
)


class TransportChoices(ChoiceSet):
    WINRM = "winrm"
    SSH = "ssh"

    CHOICES = [
        (WINRM, _("WinRM"), "blue"),
        (SSH, _("SSH"), "purple"),
    ]


class WinRMAuthChoices(ChoiceSet):
    NTLM = "ntlm"
    BASIC = "basic"
    KERBEROS = "kerberos"

    CHOICES = [
        (NTLM, _("NTLM")),
        (BASIC, _("Basic")),
        (KERBEROS, _("Kerberos")),
    ]


class ScopeStateChoices(ChoiceSet):
    ACTIVE = "active"
    INACTIVE = "inactive"

    CHOICES = [
        (ACTIVE, _("Active"), "green"),
        (INACTIVE, _("Inactive"), "red"),
    ]


class ScopeTypeChoices(ChoiceSet):
    DHCP = "dhcp"
    BOOTP = "bootp"
    BOTH = "both"

    CHOICES = [
        (DHCP, _("DHCP")),
        (BOOTP, _("BOOTP")),
        (BOTH, _("Both")),
    ]


# Windows uses the same three values for scopes and reservations.
ReservationTypeChoices = ScopeTypeChoices


class ClassTypeChoices(ChoiceSet):
    USER = "user"
    VENDOR = "vendor"

    CHOICES = [
        (USER, _("User"), "blue"),
        (VENDOR, _("Vendor"), "orange"),
    ]


class FailoverModeChoices(ChoiceSet):
    LOAD_BALANCE = "loadbalance"
    HOT_STANDBY = "hotstandby"

    CHOICES = [
        (LOAD_BALANCE, _("Load balance"), "blue"),
        (HOT_STANDBY, _("Hot standby"), "purple"),
    ]


class FailoverServerRoleChoices(ChoiceSet):
    ACTIVE = "active"
    STANDBY = "standby"

    CHOICES = [
        (ACTIVE, _("Active"), "green"),
        (STANDBY, _("Standby"), "gray"),
    ]


class MacFilterListChoices(ChoiceSet):
    ALLOW = "allow"
    DENY = "deny"

    CHOICES = [
        (ALLOW, _("Allow"), "green"),
        (DENY, _("Deny"), "red"),
    ]


# The conditions a Windows policy can match on, as keys of WindowsPolicy.criteria. Same keys as the client's
# POLICY_CRITERIA (models do not import the Windows layer; a test keeps the two in step).
POLICY_CRITERIA_KEYS = (
    "vendor_class",
    "user_class",
    "mac_address",
    "client_id",
    "relay_agent",
    "circuit_id",
    "remote_id",
    "subscriber_id",
    "fqdn",
)


class PolicyConditionChoices(ChoiceSet):
    AND = "and"
    OR = "or"

    CHOICES = [
        (AND, _("AND")),
        (OR, _("OR")),
    ]


class SyncKindChoices(ChoiceSet):
    PUSH = "push"
    DRY_RUN = "dry_run"
    IMPORT = "import"
    TEST = "test"

    CHOICES = [
        (PUSH, _("Push"), "blue"),
        (DRY_RUN, _("Dry run"), "gray"),
        (IMPORT, _("Import"), "purple"),
        (TEST, _("Connection test"), "cyan"),
    ]


class SyncStatusChoices(ChoiceSet):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

    CHOICES = [
        (PENDING, _("Pending"), "gray"),
        (RUNNING, _("Running"), "blue"),
        (COMPLETED, _("Completed"), "green"),
        (FAILED, _("Failed"), "red"),
    ]


class ProposalStatusChoices(ChoiceSet):
    OPEN = "open"
    APPLIED = "applied"
    DISMISSED = "dismissed"

    CHOICES = [
        (OPEN, _("Open"), "blue"),
        (APPLIED, _("Applied"), "green"),
        (DISMISSED, _("Dismissed"), "gray"),
    ]


class ProposalItemActionChoices(ChoiceSet):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"

    CHOICES = [
        (CREATE, _("Create"), "green"),
        (UPDATE, _("Update"), "blue"),
        (DELETE, _("Delete"), "red"),
    ]


class ProposalItemStatusChoices(ChoiceSet):
    PENDING = "pending"
    APPLIED = "applied"
    DISMISSED = "dismissed"
    FAILED = "failed"

    CHOICES = [
        (PENDING, _("Pending"), "blue"),
        (APPLIED, _("Applied"), "green"),
        (DISMISSED, _("Dismissed"), "gray"),
        (FAILED, _("Failed"), "red"),
    ]
