import datetime
import unittest
from unittest import mock

from azimuth_schedule_operator import operator
from azimuth_schedule_operator.models.v1alpha1 import schedule as schedule_crd


class TestSchedule(unittest.IsolatedAsyncioTestCase):
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

        mock_get_reference.assert_awaited_once_with(namespace, fake.spec.ref)
        mock_check_for_delete.assert_awaited_once_with(namespace, fake)
        mock_update_schedule.assert_awaited_once_with(
            namespace,
            fake.metadata.name,
            ref_exists=True,
        )

    @mock.patch.object(operator, "update_schedule")
    @mock.patch.object(operator, "check_for_delete")
    @mock.patch.object(operator, "get_reference")
    async def test_schedule_check_skip(
        self, mock_get_reference, mock_check_for_delete, mock_update_schedule
    ):
        body = schedule_crd.get_fake_dict()
        body["status"] = {"refExists": True, "refDeleteTriggered": True}
        namespace = "ns1"

        await operator.schedule_check(body, namespace)

        mock_get_reference.assert_not_called()
        mock_check_for_delete.assert_not_called()
        mock_update_schedule.assert_not_called()

    @mock.patch.object(operator, "update_schedule")
    @mock.patch.object(operator, "delete_reference")
    async def test_check_for_delete(self, mock_delete_reference, mock_update_schedule):
        namespace = "ns1"
        schedule = schedule_crd.get_fake()

        await operator.check_for_delete(namespace, schedule)

        mock_delete_reference.assert_awaited_once_with(namespace, schedule.spec.ref)
        mock_update_schedule.assert_awaited_once_with(
            namespace, schedule.metadata.name, ref_delete_triggered=True
        )

    @mock.patch.object(operator, "update_schedule")
    @mock.patch.object(operator, "delete_reference")
    async def test_check_for_delete_skip(
        self, mock_delete_reference, mock_update_schedule
    ):
        namespace = "ns1"
        schedule = schedule_crd.get_fake()
        now = datetime.datetime.now(datetime.timezone.utc)
        schedule.spec.not_after = now + datetime.timedelta(seconds=5)

        await operator.check_for_delete(namespace, schedule)

        mock_delete_reference.assert_not_called()
        mock_update_schedule.assert_not_called()

    @mock.patch.object(operator, "update_schedule_status")
    async def test_update_schedule(self, mock_update_schedule_status):
        name = "schedule1"
        namespace = "ns1"

        await operator.update_schedule(
            namespace, name, ref_exists=True, ref_delete_triggered=False
        )

        mock_update_schedule_status.assert_awaited_once_with(
            namespace,
            name,
            {"updatedAt": mock.ANY, "refExists": True, "refDeleteTriggered": False},
        )

    @mock.patch.object(operator, "K8S_CLIENT", new_callable=mock.Mock)
    async def test_get_reference(self, mock_client):
        mock_resource = mock.AsyncMock()
        mock_api = mock.AsyncMock()
        mock_api.resource.return_value = mock_resource
        mock_client.api.return_value = mock_api
        mock_resource.fetch.return_value = "result"
        ref = schedule_crd.ScheduleRef(api_version="v1", kind="Pod", name="pod1")

        result = await operator.get_reference("ns1", ref)

        self.assertEqual(result, "result")
        mock_client.api.assert_called_once_with("v1")
        mock_api.resource.assert_awaited_once_with("Pod")
        mock_resource.fetch.assert_awaited_once_with("pod1", namespace="ns1")

    @mock.patch.object(operator, "K8S_CLIENT", new_callable=mock.Mock)
    async def test_delete_reference(self, mock_client):
        mock_resource = mock.AsyncMock()
        mock_api = mock.AsyncMock()
        mock_api.resource.return_value = mock_resource
        mock_client.api.return_value = mock_api
        ref = schedule_crd.ScheduleRef(api_version="v1", kind="Pod", name="pod1")

        await operator.delete_reference("ns1", ref)

        mock_client.api.assert_called_once_with("v1")
        mock_api.resource.assert_awaited_once_with("Pod")
        mock_resource.delete.assert_awaited_once_with("pod1", namespace="ns1")

    @mock.patch.object(operator, "K8S_CLIENT", new_callable=mock.Mock)
    async def test_update_schedule_status(self, mock_client):
        mock_resource = mock.AsyncMock()
        mock_api = mock.AsyncMock()
        mock_api.resource.return_value = mock_resource
        mock_client.api.return_value = mock_api

        await operator.update_schedule_status("ns1", "test1", {"a": "asdf"})

        mock_client.api.assert_called_once_with(
            "scheduling.azimuth.stackhpc.com/v1alpha1"
        )
        mock_api.resource.assert_awaited_once_with("schedules/status")
        mock_resource.patch.assert_awaited_once_with(
            "test1", {"status": {"a": "asdf"}}, namespace="ns1"
        )
