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
    "failoverrelationship",
    "macfilter",
    "syncrun",
    "importproposal",
    "importproposalitem",
)

urlpatterns = []
for model_name in MODELS:
    urlpatterns += [
        path(f"{model_name}s/", include(get_model_urls("netbox_windows_dhcp_plugin", model_name, detail=False))),
        path(f"{model_name}s/<int:pk>/", include(get_model_urls("netbox_windows_dhcp_plugin", model_name))),
    ]
