from netbox.search import SearchIndex

from . import models


class WindowsServerIndex(SearchIndex):
    model = models.WindowsServer
    fields = (("name", 100), ("host", 110), ("computer_name", 300), ("description", 500), ("comments", 5000))
    display_attrs = ("host", "transport", "device", "virtual_machine", "description")


class SuperscopeIndex(SearchIndex):
    model = models.Superscope
    fields = (("name", 100), ("description", 500), ("comments", 5000))
    display_attrs = ("server", "description")


class ScopeIndex(SearchIndex):
    model = models.Scope
    fields = (("name", 100), ("description", 500), ("comments", 5000))
    display_attrs = ("server", "prefix", "state", "superscope")


class ReservationIndex(SearchIndex):
    model = models.Reservation
    fields = (
        ("name", 100),
        ("client_id", 300),
        ("duid", 300),
        ("description", 500),
        ("comments", 5000),
    )
    display_attrs = ("scope", "ip_address", "mac_address")


class ExclusionRangeIndex(SearchIndex):
    model = models.ExclusionRange
    fields = (("start_address", 100), ("end_address", 100), ("description", 500), ("comments", 5000))
    display_attrs = ("scope", "description")


class WindowsClassIndex(SearchIndex):
    model = models.WindowsClass
    fields = (("name", 100), ("data", 300), ("description", 500), ("comments", 5000))
    display_attrs = ("server", "class_type", "family", "description")


class OptionDefinitionIndex(SearchIndex):
    model = models.OptionDefinition
    fields = (("name", 100), ("code", 200), ("description", 500))
    display_attrs = ("server", "family", "vendor_class", "code")


class WindowsPolicyIndex(SearchIndex):
    model = models.WindowsPolicy
    fields = (("name", 100), ("description", 500), ("comments", 5000))
    display_attrs = ("server", "scope", "description")


class FailoverRelationshipIndex(SearchIndex):
    model = models.FailoverRelationship
    fields = (("name", 100), ("description", 500), ("comments", 5000))
    display_attrs = ("primary_server", "partner_server", "mode", "description")


class MacFilterIndex(SearchIndex):
    model = models.MacFilter
    fields = (("mac_address", 100), ("description", 500), ("comments", 5000))
    display_attrs = ("server", "list", "description")


indexes = (
    WindowsServerIndex,
    SuperscopeIndex,
    ScopeIndex,
    ReservationIndex,
    ExclusionRangeIndex,
    WindowsClassIndex,
    OptionDefinitionIndex,
    WindowsPolicyIndex,
    FailoverRelationshipIndex,
    MacFilterIndex,
)
