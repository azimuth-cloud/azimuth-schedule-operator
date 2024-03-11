import unittest
from unittest import mock

from azimuth_schedule_operator.models.v1alpha1 import schedule as schedule_crd
from azimuth_schedule_operator import operator


class TestOperator(unittest.IsolatedAsyncioTestCase):
    def _generate_fake_crd(self, name):
        plural_name, api_group = name.split(".", maxsplit=1)
        return {
            "metadata": {
                "name": name,
            },
            "spec": {
                "group": api_group,
                "names": {
                    "plural": plural_name,
                },
                "versions": [
                    {
                        "name": "v1alpha1",
                        "storage": True,
                    },
                ],
            },
        }

    @mock.patch("azimuth_schedule_operator.utils.k8s.get_k8s_client")
    async def test_startup_register_crds(self, mock_get):
        mock_client = mock.AsyncMock()
        mock_get.return_value = mock_client
        mock_settings = mock.Mock()

        await operator.startup(mock_settings)

        # Test that the CRDs were applied
        mock_client.apply_object.assert_has_awaits([mock.call(mock.ANY, force=True)])
        # Test that the APIs were checked
        mock_client.get.assert_has_awaits(
            [
                mock.call("/apis/scheduling.azimuth.stackhpc.com/v1alpha1/schedules"),
            ]
        )

    @mock.patch.object(operator, "K8S_CLIENT", new_callable=mock.AsyncMock)
    async def test_cleanup_calls_aclose(self, mock_client):
        await operator.cleanup()
        mock_client.aclose.assert_awaited_once_with()

    async def test_cluster_type_create_success(self):
        await operator.schedule_changed(schedule_crd.get_fake_dict(), "type1", "ns", {})
