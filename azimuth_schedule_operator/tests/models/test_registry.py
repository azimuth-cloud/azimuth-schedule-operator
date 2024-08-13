from azimuth_schedule_operator.models import registry
from azimuth_schedule_operator.tests import base


class TestRegistry(base.TestCase):
    def test_registry_size(self):
        reg = registry.get_registry()
        self.assertEqual(2, len(list(reg)))

    def test_get_crd_resources(self):
        crds = registry.get_crd_resources()
        self.assertEqual(2, len(list(crds)))
