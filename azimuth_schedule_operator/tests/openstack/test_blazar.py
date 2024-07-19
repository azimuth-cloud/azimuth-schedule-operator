import datetime
import unittest
from unittest import mock

from azimuth_schedule_operator.openstack import blazar


class TestOperator(unittest.IsolatedAsyncioTestCase):
    @mock.patch.object(blazar, "_send_create_lease")
    async def test_create_lease(self, mock_send_create_lease):
        cloud = mock.Mock()
        name = None
        flavor_counts = {"flavor_id1": 1, "flavor_id2": 2}
        end_date = datetime.datetime(2024, 7, 19, 18, 0, 13)
        start_date = None
        mock_send_create_lease.return_value = {"id": "lease_id"}

        result = await blazar.create_lease(
            cloud, name, flavor_counts, end_date, start_date
        )

        self.assertEqual("lease_id", result)
        mock_send_create_lease.assert_awaited_once_with(
            cloud,
            {
                "name": name,
                "start_date": "now",
                "end_date": "2024-07-19 18:00",
                "reservations": [
                    {
                        "amount": 1,
                        "flavor_id": "flavor_id1",
                        "resource_type": "flavor:instance",
                        "affinity": "None",
                    },
                    {
                        "amount": 2,
                        "flavor_id": "flavor_id2",
                        "resource_type": "flavor:instance",
                        "affinity": "None",
                    },
                ],
                "events": [],
                "before_end_date": None,
            },
        )

    @mock.patch.object(blazar, "_fetch_lease")
    async def test_get_lease_flavors(self, mock_fetch_lease):
        cloud = mock.Mock()
        lease_id = "lease_id"
        mock_fetch_lease.return_value = {
            "status": "ACTIVE",
            "reservations": [
                {
                    "id": "new1",
                    "resource_properties": '{"id": "old1", "name": "old1"}',
                },
                {
                    "id": "new2",
                    "resource_properties": '{"id": "old2", "name": "old2"}',
                },
            ],
        }

        result = await blazar.get_lease_flavors(cloud, lease_id)

        mock_fetch_lease.assert_awaited_once_with(cloud, lease_id)
        self.assertEqual({"old1": "new1", "old2": "new2"}, result)

    @mock.patch.object(blazar, "_fetch_lease")
    async def test_get_lease_flavors_returns_None(self, mock_fetch_lease):
        cloud = mock.Mock()
        lease_id = "lease_id"
        mock_fetch_lease.return_value = {"status": "STARTING"}

        result = await blazar.get_lease_flavors(cloud, lease_id)

        self.assertIsNone(result)
