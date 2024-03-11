import kube_custom_resource as crd

from azimuth_schedule_operator.models.v1alpha1 import schedule

API_GROUP = "scheduling.azimuth.stackhpc.com"
API_VERSION = API_GROUP + "/v1alpha1"
CATEGORIES = ["azimuth"]


def get_registry():
    registry = crd.CustomResourceRegistry(API_GROUP, CATEGORIES)
    registry.discover_models(schedule)
    return registry


def get_crd_resources():
    reg = get_registry()
    for resource in reg:
        yield resource.kubernetes_resource()
