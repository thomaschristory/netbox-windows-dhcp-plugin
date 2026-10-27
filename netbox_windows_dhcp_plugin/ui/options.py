"""How the UI names an option value's code: from the server's definition, else from the standard option list."""

from ..standard_options import standard_name


class OptionNames:
    """
    Option names for many values at once, one query per server, family and vendor class (a table page usually holds
    one or two), instead of one query per row.
    """

    def __init__(self):
        self._cache = {}

    def name(self, value):
        from ..models import OptionDefinition

        key = (value.server_id, value.family, value.vendor_class_id)
        if key not in self._cache:
            self._cache[key] = dict(
                OptionDefinition.objects.filter(
                    server_id=value.server_id, family=value.family, vendor_class_id=value.vendor_class_id
                ).values_list("code", "name")
            )
        defined = self._cache[key].get(value.code)
        if defined:
            return defined
        # A vendor class's codes are its own: the standard names only apply to options every client can get.
        return "" if value.vendor_class_id else standard_name(value.family, value.code)


def option_name(value):
    """The name of one option value's option, or an empty string when nothing names it."""
    return OptionNames().name(value)
