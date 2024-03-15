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

    @mock.patch.object(operator, "update_schedule")
    @mock.patch.object(operator, "check_for_delete")
    @mock.patch.object(operator, "get_reference")
    async def test_schedule_check(
        self, mock_get_reference, mock_check_for_delete, mock_update_schedule
    ):
        body = schedule_crd.get_fake_dict()
        fake = schedule_crd.Schedule(**body)
        namespace = "ns1"

        await operator.schedule_check(body, namespace)

        # Assert the expected behavior
        mock_get_reference.assert_awaited_once_with(namespace, fake.spec.ref)
        mock_check_for_delete.assert_awaited_once_with(namespace, fake)
        mock_update_schedule.assert_awaited_once_with(
            fake.metadata.name, namespace, ref_found=True
        )

    @mock.patch.object(operator, "update_schedule")
    @mock.patch.object(operator, "delete_reference")
    async def test_check_for_delete(self, mock_delete_reference, mock_update_schedule):
        namespace = "ns1"
        schedule = schedule_crd.get_fake()

        await operator.check_for_delete(namespace, schedule)

        mock_delete_reference.assert_awaited_once_with(namespace, schedule.spec.ref)
        mock_update_schedule.assert_awaited_once_with(
            schedule.metadata.name, namespace, delete_triggered=True
        )