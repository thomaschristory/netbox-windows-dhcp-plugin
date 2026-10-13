from django.urls import include, path
from utilities.urls import get_model_urls

from . import views  # noqa: F401  (registers the model views)

MODELS = (
    "windowsserver",
    "windowsscope",
    "windowsreservation",
    "exclusionrange",
    "windowsclass",
    "windowsoptiondefinition",
    "windowspolicy",
    "windowsoptionqualifier",
    "windowsvendoroptiondefinition",
    "windowsvendoroption",
    "failoverrelationship",
    "macfilter",
    "macfilterlists",
    "syncrun",
    "importproposal",
    "importproposalitem",
)

urlpatterns = []
for model_name in MODELS:
    # "macfilterlists" is already plural.
    plural = model_name if model_name.endswith("s") else f"{model_name}s"
    urlpatterns += [
        path(f"{plural}/", include(get_model_urls("netbox_windows_dhcp_plugin", model_name, detail=False))),
        path(f"{plural}/<int:pk>/", include(get_model_urls("netbox_windows_dhcp_plugin", model_name))),
    ]
