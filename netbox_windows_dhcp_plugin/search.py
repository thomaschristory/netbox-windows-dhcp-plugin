from netbox.search import SearchIndex

from . import models


class WindowsServerIndex(SearchIndex):
    model = models.WindowsServer
    fields = (("host", 100), ("computer_name", 300), ("description", 500), ("comments", 5000))
    display_attrs = ("dhcp_server", "transport", "description")


class WindowsClassIndex(SearchIndex):
    model = models.WindowsClass
    fields = (("name", 100), ("data", 300), ("description", 500), ("comments", 5000))
    display_attrs = ("dhcp_server", "class_type", "description")


class WindowsPolicyIndex(SearchIndex):
    model = models.WindowsPolicy
    fields = (("name", 100), ("description", 500), ("comments", 5000))
    display_attrs = ("dhcp_server", "subnet", "description")


class FailoverRelationshipIndex(SearchIndex):
    model = models.FailoverRelationship
    fields = (("name", 100), ("description", 500), ("comments", 5000))
    display_attrs = ("primary_server", "partner_server", "mode", "description")


class MacFilterIndex(SearchIndex):
    model = models.MacFilter
    fields = (("mac_address", 100), ("description", 500), ("comments", 5000))
    display_attrs = ("dhcp_server", "list", "description")


class ExclusionRangeIndex(SearchIndex):
    model = models.ExclusionRange
    fields = (("start_address", 100), ("end_address", 100), ("description", 500), ("comments", 5000))
    display_attrs = ("subnet", "description")


class WindowsVendorOptionDefinitionIndex(SearchIndex):
    model = models.WindowsVendorOptionDefinition
    fields = (("name", 100), ("description", 500), ("comments", 5000))
    display_attrs = ("vendor_class", "code", "description")


indexes = (
    WindowsServerIndex,
    WindowsClassIndex,
    WindowsPolicyIndex,
    FailoverRelationshipIndex,
    MacFilterIndex,
    ExclusionRangeIndex,
    WindowsVendorOptionDefinitionIndex,
)
