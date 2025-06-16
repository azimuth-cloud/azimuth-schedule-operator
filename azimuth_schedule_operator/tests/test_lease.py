import json
import unittest
from unittest import mock

import freezegun
import kopf
from easykube.rest.util import PropertyDict

from azimuth_schedule_operator import openstack, operator
from azimuth_schedule_operator.models.v1alpha1 import lease as lease_crd

from . import util

API_VERSION = "scheduling.azimuth.stackhpc.com/v1alpha1"


def fake_credential():
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "fake-credential",
            "namespace": "fake-ns",
        },
        "data": {
            "clouds.yaml": "NOT A REAL CREDENTIAL",
        },
    }


def fake_lease(start=True, end=True, phase=None):
    lease = {
        "apiVersion": API_VERSION,
        "kind": "Lease",
        "metadata": {
            "name": "fake-lease",
            "namespace": "fake-ns",
            "resourceVersion": "currentversion",
            "ownerReferences": [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "name": "fake-lease-owner",
                    "uid": "fake-uid",
                    "blockOwnerDeletion": True,
                },
            ],
            "finalizers": [
                "scheduling.azimuth.stackhpc.com",
            ],
        },
        "spec": {
            "cloudCredentialsSecretName": "fake-credential",
            "resources": {
                "machines": [
                    {
                        "sizeId": "id1",
                        "count": 3,
                    },
                    {
                        "sizeId": "id2",
                        "count": 5,
                    },
                ],
            },
        },
    }
    if start:
        lease["spec"]["startsAt"] = "2024-08-21T15:00:00Z"
    if end:
        lease["spec"]["endsAt"] = "2024-08-21T16:00:00Z"
    if phase:
        lease.setdefault("status", {})["phase"] = str(phase)
        if phase == lease_crd.LeasePhase.ACTIVE:
            lease["status"]["sizeMap"] = {"id1": "newid1", "id2": "newid2"}
            lease["status"]["sizeNameMap"] = {
                "flavor1": "newflavor1",
                "flavor2": "newflavor2",
            }
    return lease


def fake_blazar_lease_request(start=True):
    return {
        "name": "az-fake-lease",
        "start_date": "2024-08-21 15:00" if start else "now",
        "end_date": "2024-08-21 16:00",
        "reservations": [
            {
                "amount": 3,
                "flavor_id": "id1",
                "resource_type": "flavor:instance",
                "affinity": "None",
            },
            {
                "amount": 5,
                "flavor_id": "id2",
                "resource_type": "flavor:instance",
                "affinity": "None",
            },
        ],
        "events": [],
        "before_end_date": None,
    }


def fake_blazar_lease(status="PENDING"):
    lease = {
        "id": "blazarleaseid",
        "name": "az-fake-lease",
        "status": status,
    }
    if status == "ACTIVE":
        lease["reservations"] = [
            {
                "id": "newid1",
                "resource_type": "flavor:instance",
                "resource_properties": json.dumps({"id": "id1", "foo": "bar", "x": 1}),
            },
            {
                "id": "newid2",
                "resource_type": "flavor:instance",
                "resource_properties": json.dumps({"id": "id2", "foo": "baz", "y": 2}),
            },
            {
                "id": "notused",
                "resource_type": "physical:host",
                "resource_properties": "",
            },
        ]
    return lease


class TestLease(unittest.IsolatedAsyncioTestCase):
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    async def test_ekresource_for_model(self, k8s_client):
        resource = await operator.ekresource_for_model(lease_crd.Lease)
        # Check that the client was interacted with as expected
        k8s_client.api.assert_called_once_with(API_VERSION)
        k8s_client.apis[API_VERSION].resource.assert_awaited_once_with("leases")
        # Check that the resource is the one returned by the client
        self.assertIs(resource, k8s_client.apis[API_VERSION].resources["leases"])

    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    async def test_ekresource_for_model_subresource(self, k8s_client):
        resource = await operator.ekresource_for_model(
            lease_crd.Lease, subresource="status"
        )
        # Check that the client was interacted with as expected
        k8s_client.api.assert_called_once_with(API_VERSION)
        k8s_client.apis[API_VERSION].resource.assert_awaited_once_with("leases/status")
        # Check that the resource is the one returned by the client
        self.assertIs(resource, k8s_client.apis[API_VERSION].resources["leases/status"])

    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    async def test_save_instance_status(self, k8s_client):
        lease_data = fake_lease(phase=lease_crd.LeasePhase.ACTIVE)

        k8s_client.apis[API_VERSION].resources["leases/status"].replace.return_value = {
            **lease_data,
            "metadata": {**lease_data["metadata"], "resourceVersion": "nextversion"},
        }

        lease = lease_crd.Lease.model_validate(lease_data)
        await operator.save_instance_status(lease)

        self.assertEqual(lease.metadata.resource_version, "nextversion")
        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_awaited_once_with(
            lease_data["metadata"]["name"],
            {
                "metadata": {
                    "resourceVersion": lease_data["metadata"]["resourceVersion"]
                },
                "status": lease_data["status"],
            },
            namespace=lease_data["metadata"]["namespace"],
        )

    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    async def test_save_instance_status_conflict(self, k8s_client):
        lease_data = fake_lease(phase=lease_crd.LeasePhase.PENDING)

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.side_effect = util.k8s_api_error(409)

        lease = lease_crd.Lease.model_validate(lease_data)
        with self.assertRaises(kopf.TemporaryError):
            await operator.save_instance_status(lease)

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_awaited_once_with(
            lease_data["metadata"]["name"],
            {
                "metadata": {
                    "resourceVersion": lease_data["metadata"]["resourceVersion"]
                },
                "status": lease_data["status"],
            },
            namespace=lease_data["metadata"]["namespace"],
        )

    async def test_find_blazar_lease(self):
        fake_lease = {"name": "az-fake-lease"}
        blazar_client = util.mock_openstack_client()
        blazar_client.resources["leases"].list.return_value = util.as_async_iterable(
            [fake_lease]
        )

        lease = await operator.find_blazar_lease(blazar_client, fake_lease["name"])

        self.assertEqual(lease, fake_lease)
        blazar_client.resources["leases"].list.assert_called_once_with()

    async def test_find_blazar_lease_not_present(self):
        fake_lease = {"name": "az-fake-lease"}
        blazar_client = util.mock_openstack_cloud().clients["reservation"]
        blazar_client.resources["leases"].list.side_effect = (
            lambda: util.as_async_iterable([fake_lease])
        )

        lease = await operator.find_blazar_lease(blazar_client, "doesnotexist")

        self.assertIsNone(lease)
        blazar_client.resources["leases"].list.assert_called_once_with()

    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    def test_blazar_enabled_yes(self):
        cloud = util.mock_openstack_cloud()

        self.assertTrue(operator.blazar_enabled(cloud))

        cloud.api_client.assert_not_called()

    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    def test_blazar_enabled_no(self):
        cloud = util.mock_openstack_cloud()

        self.assertFalse(operator.blazar_enabled(cloud))

        cloud.api_client.assert_not_called()

    def test_blazar_enabled_auto_blazar_available(self):
        cloud = util.mock_openstack_cloud()

        self.assertTrue(operator.blazar_enabled(cloud))

        cloud.api_client.assert_called_once_with("reservation")

    def test_blazar_enabled_auto_blazar_not_available(self):
        cloud = util.mock_openstack_cloud()
        cloud.api_client.side_effect = openstack.ApiNotSupportedError("reservation")

        self.assertFalse(operator.blazar_enabled(cloud))

        cloud.api_client.assert_called_once_with("reservation")

    async def test_create_blazar_lease_no_start(self):
        blazar_client = util.mock_openstack_client()
        blazar_lease_data = {"name": "az-fake-lease"}
        blazar_client.resources["leases"].create.return_value = blazar_lease_data

        lease = lease_crd.Lease.model_validate(fake_lease(start=False))
        created = await operator.create_blazar_lease(
            blazar_client, f"az-{lease.metadata.name}", lease
        )

        self.assertEqual(created, blazar_lease_data)
        blazar_client.resources["leases"].create.assert_awaited_once_with(
            fake_blazar_lease_request(start=False)
        )

    async def test_create_blazar_lease_with_start(self):
        blazar_client = util.mock_openstack_client()
        blazar_lease_data = {"name": "az-fake-lease"}
        blazar_client.resources["leases"].create.return_value = blazar_lease_data

        lease = lease_crd.Lease.model_validate(fake_lease())
        created = await operator.create_blazar_lease(
            blazar_client, f"az-{lease.metadata.name}", lease
        )

        self.assertEqual(created, blazar_lease_data)
        blazar_client.resources["leases"].create.assert_awaited_once_with(
            fake_blazar_lease_request()
        )

    async def test_create_blazar_lease_error_json_message(self):
        blazar_client = util.mock_openstack_client()
        blazar_client.resources["leases"].create.side_effect = util.httpx_status_error(
            400, json={"error_message": "this is an error from blazar"}
        )

        lease = lease_crd.Lease.model_validate(fake_lease())
        with self.assertRaises(operator.BlazarLeaseCreateError) as ctx:
            _ = await operator.create_blazar_lease(
                blazar_client, f"az-{lease.metadata.name}", lease
            )

        self.assertEqual(
            str(ctx.exception),
            "error creating blazar lease - this is an error from blazar",
        )
        blazar_client.resources["leases"].create.assert_awaited_once()

    async def test_create_blazar_lease_error_json_message_500(self):
        blazar_client = util.mock_openstack_client()
        blazar_client.resources["leases"].create.side_effect = util.httpx_status_error(
            500, json={"error_message": "this is an error from blazar 2"}
        )

        lease = lease_crd.Lease.model_validate(fake_lease())
        with self.assertRaises(operator.BlazarLeaseCreateError) as ctx:
            _ = await operator.create_blazar_lease(
                blazar_client, f"az-{lease.metadata.name}", lease
            )

        self.assertEqual(
            str(ctx.exception),
            "error creating blazar lease - this is an error from blazar 2",
        )
        blazar_client.resources["leases"].create.assert_awaited_once()

    async def test_create_blazar_lease_error_not_valid_json(self):
        blazar_client = util.mock_openstack_client()
        blazar_client.resources["leases"].create.side_effect = util.httpx_status_error(
            400, text="this is not valid json"
        )

        lease = lease_crd.Lease.model_validate(fake_lease())
        with self.assertRaises(operator.BlazarLeaseCreateError) as ctx:
            _ = await operator.create_blazar_lease(
                blazar_client, f"az-{lease.metadata.name}", lease
            )

        self.assertEqual(
            str(ctx.exception), "error creating blazar lease - this is not valid json"
        )
        blazar_client.resources["leases"].create.assert_awaited_once()

    def test_get_size_map(self):
        blazar_lease = fake_blazar_lease(status="ACTIVE")
        size_map = operator.get_size_map(blazar_lease)
        self.assertEqual(size_map, {"id1": "newid1", "id2": "newid2"})

    async def test_get_size_name_map(self):
        cloud = util.mock_openstack_cloud()
        cloud.clients["compute"].resources[
            "flavors"
        ].list.return_value = util.as_async_iterable(
            [
                PropertyDict({"id": "id1", "name": "flavor1"}),
                PropertyDict({"id": "id2", "name": "flavor2"}),
                PropertyDict({"id": "newid1", "name": "newflavor1"}),
                PropertyDict({"id": "newid2", "name": "newflavor2"}),
            ]
        )

        size_map = {"id1": "newid1", "id2": "newid2", "id3": "newid3"}
        size_name_map = await operator.get_size_name_map(cloud, size_map)

        self.assertEqual(
            size_name_map, {"flavor1": "newflavor1", "flavor2": "newflavor2"}
        )
        cloud.clients["compute"].resources["flavors"].list.assert_called_once_with()

    @mock.patch.object(operator, "get_size_name_map")
    async def test_update_lease_status_no_blazar_no_start(self, get_size_name_map):
        cloud = util.mock_openstack_cloud()

        size_name_map = {"flavor1": "flavor1", "flavor2": "flavor2"}
        get_size_name_map.return_value = size_name_map

        lease = lease_crd.Lease.model_validate(fake_lease(start=False))
        await operator.update_lease_status_no_blazar(cloud, lease)

        self.assertEqual(lease.status.phase, lease_crd.LeasePhase.ACTIVE)
        self.assertEqual(lease.status.size_map, {"id1": "id1", "id2": "id2"})
        self.assertEqual(lease.status.size_name_map, size_name_map)

    @mock.patch.object(operator, "get_size_name_map")
    async def test_update_lease_status_no_blazar_started(self, get_size_name_map):
        cloud = util.mock_openstack_cloud()

        size_name_map = {"flavor1": "flavor1", "flavor2": "flavor2"}
        get_size_name_map.return_value = size_name_map

        lease = lease_crd.Lease.model_validate(fake_lease())
        with freezegun.freeze_time("2024-08-21T15:30:00Z"):
            await operator.update_lease_status_no_blazar(cloud, lease)

        self.assertEqual(lease.status.phase, lease_crd.LeasePhase.ACTIVE)
        self.assertEqual(lease.status.size_map, {"id1": "id1", "id2": "id2"})
        self.assertEqual(lease.status.size_name_map, size_name_map)

    @mock.patch.object(operator, "get_size_name_map")
    async def test_update_lease_status_no_blazar_not_started(self, get_size_name_map):
        cloud = util.mock_openstack_cloud()

        lease = lease_crd.Lease.model_validate(fake_lease())
        with freezegun.freeze_time("2024-08-21T14:30:00Z"):
            await operator.update_lease_status_no_blazar(cloud, lease)

        get_size_name_map.assert_not_called()
        self.assertEqual(lease.status.phase, lease_crd.LeasePhase.PENDING)
        self.assertEqual(lease.status.size_map, {})
        self.assertEqual(lease.status.size_name_map, {})

    def k8s_client_config_common(self, k8s_client):
        k8s_secrets = k8s_client.apis["v1"].resources["secrets"]
        k8s_secrets.fetch.return_value = PropertyDict(fake_credential())
        k8s_leases_status = k8s_client.apis[API_VERSION].resources["leases/status"]
        k8s_leases_status.replace.return_value = {
            "metadata": {"resourceVersion": "nextversion"}
        }

    def os_cloud_config_common(self, os_cloud):
        type(os_cloud).is_authenticated = mock.PropertyMock(return_value=True)
        type(os_cloud).application_credential_id = mock.PropertyMock(
            return_value="appcredid"
        )
        os_flavors = os_cloud.clients["compute"].resources["flavors"]
        os_flavors.list.return_value = util.as_async_iterable(
            [
                PropertyDict({"id": "id1", "name": "flavor1"}),
                PropertyDict({"id": "id2", "name": "flavor2"}),
                PropertyDict({"id": "newid1", "name": "newflavor1"}),
                PropertyDict({"id": "newid2", "name": "newflavor2"}),
            ]
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_reconcile_lease_no_blazar_no_end_no_start(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(start=False, end=False)
        await operator.reconcile_lease(lease_data, mock.Mock())

        # Check that the lease was patched as expected given the setup
        lease_status_replace = (
            k8s_client.apis[API_VERSION].resources["leases/status"].replace
        )
        self.assertEqual(lease_status_replace.call_count, 2)
        lease_status_replace.assert_has_calls(
            [
                # The unknown phase should be updated to pending
                mock.call(
                    "fake-lease",
                    util.LeaseStatusMatcher(lease_crd.LeasePhase.PENDING),
                    namespace="fake-ns",
                ),
                # The lease should be made active
                mock.call(
                    "fake-lease",
                    util.LeaseStatusMatcher(
                        lease_crd.LeasePhase.ACTIVE,
                        {"id1": "id1", "id2": "id2"},
                        {"flavor1": "flavor1", "flavor2": "flavor2"},
                    ),
                    namespace="fake-ns",
                ),
            ]
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_reconcile_lease_no_blazar_no_end_start_in_past(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(
            start=True, end=False, phase=lease_crd.LeasePhase.PENDING
        )
        with freezegun.freeze_time("2024-08-21T15:30:00Z"):
            await operator.reconcile_lease(lease_data, mock.Mock())

        # Check that the lease was patched as expected
        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ACTIVE,
                {"id1": "id1", "id2": "id2"},
                {"flavor1": "flavor1", "flavor2": "flavor2"},
            ),
            namespace="fake-ns",
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_reconcile_lease_no_blazar_no_end_start_in_future(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(
            start=True, end=False, phase=lease_crd.LeasePhase.PENDING
        )
        with freezegun.freeze_time("2024-08-21T14:30:00Z"):
            await operator.reconcile_lease(lease_data, mock.Mock())

        # Check that the lease was patched as expected
        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(lease_crd.LeasePhase.PENDING),
            namespace="fake-ns",
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_reconcile_lease_no_blazar_end_no_start(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(
            start=False, end=True, phase=lease_crd.LeasePhase.PENDING
        )
        with freezegun.freeze_time("2024-08-21T15:30:00Z"):
            await operator.reconcile_lease(lease_data, mock.Mock())

        # Check that the lease was patched as expected
        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ACTIVE,
                {"id1": "id1", "id2": "id2"},
                {"flavor1": "flavor1", "flavor2": "flavor2"},
            ),
            namespace="fake-ns",
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_reconcile_lease_no_blazar_end_start_in_past(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(
            start=True, end=True, phase=lease_crd.LeasePhase.PENDING
        )
        with freezegun.freeze_time("2024-08-21T15:30:00Z"):
            await operator.reconcile_lease(lease_data, mock.Mock())

        # Check that the lease was patched as expected
        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ACTIVE,
                {"id1": "id1", "id2": "id2"},
                {"flavor1": "flavor1", "flavor2": "flavor2"},
            ),
            namespace="fake-ns",
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_reconcile_lease_no_blazar_end_start_in_future(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(
            start=True, end=True, phase=lease_crd.LeasePhase.PENDING
        )
        with freezegun.freeze_time("2024-08-21T14:30:00Z"):
            await operator.reconcile_lease(lease_data, mock.Mock())

        # Check that the lease was patched as expected
        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(lease_crd.LeasePhase.PENDING),
            namespace="fake-ns",
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_reconcile_lease_blazar_lease_created(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)
        os_leases = os_cloud.clients["reservation"].resources["leases"]
        os_leases.create.return_value = fake_blazar_lease(status="CREATING")

        lease_data = fake_lease(phase=lease_crd.LeasePhase.PENDING)
        await operator.reconcile_lease(lease_data, mock.Mock())

        # Check that the Blazar lease was requested as expected
        os_leases.create.assert_called_once_with(fake_blazar_lease_request())

        # Check that the lease was patched as expected
        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(lease_crd.LeasePhase.CREATING),
            namespace="fake-ns",
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_reconcile_lease_blazar_lease_create_error(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)
        os_leases = os_cloud.clients["reservation"].resources["leases"]
        os_leases.create.side_effect = util.httpx_status_error(
            400, json={"error_message": "from blazar"}
        )

        lease_data = fake_lease(phase=lease_crd.LeasePhase.PENDING)
        await operator.reconcile_lease(lease_data, mock.Mock())

        # Check that the Blazar lease was requested as expected
        os_leases.create.assert_called_once_with(fake_blazar_lease_request())

        # Check that the lease was patched as expected
        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ERROR,
                error_message="error creating blazar lease - from blazar",
            ),
            namespace="fake-ns",
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_reconcile_lease_blazar_no_lease_not_pending(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)
        os_leases = os_cloud.clients["reservation"].resources["leases"]
        os_leases.create.side_effect = util.httpx_status_error(
            400, json={"error_message": "from blazar"}
        )

        lease_data = fake_lease(phase=lease_crd.LeasePhase.ERROR)
        await operator.reconcile_lease(lease_data, mock.Mock())

        # If the lease in in any state other than Pending, we will not attempt to
        # create the lease and the lease status should be left unchanged
        os_leases.create.assert_not_called()
        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_not_called()

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_reconcile_lease_blazar_existing_lease(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)
        os_leases = os_cloud.clients["reservation"].resources["leases"]
        os_leases.list.return_value = util.as_async_iterable(
            [fake_blazar_lease("ACTIVE")]
        )

        lease_data = fake_lease(phase=lease_crd.LeasePhase.PENDING)
        await operator.reconcile_lease(lease_data, mock.Mock())

        # We should not have attempted to create a lease
        os_leases.create.assert_not_called()

        # Check that the lease was patched as expected
        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ACTIVE,
                {"id1": "newid1", "id2": "newid2"},
                {"flavor1": "newflavor1", "flavor2": "newflavor2"},
            ),
            namespace="fake-ns",
        )

    def assert_lease_owner_deleted(self, k8s_client):
        k8s_client.apis["v1"].resources["ConfigMap"].delete.assert_called_once_with(
            "fake-lease-owner", propagation_policy="Foreground", namespace="fake-ns"
        )

    def assert_lease_owner_not_deleted(self, k8s_client):
        k8s_client.apis["v1"].resources["ConfigMap"].delete.assert_not_called()

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_check_lease_no_blazar_no_end_no_start(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(start=False, end=False)
        await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ACTIVE,
                {"id1": "id1", "id2": "id2"},
                {"flavor1": "flavor1", "flavor2": "flavor2"},
            ),
            namespace="fake-ns",
        )

        self.assert_lease_owner_not_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_check_lease_no_blazar_no_end_start_in_past(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(start=True, end=False)
        with freezegun.freeze_time("2024-08-21T15:30:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ACTIVE,
                {"id1": "id1", "id2": "id2"},
                {"flavor1": "flavor1", "flavor2": "flavor2"},
            ),
            namespace="fake-ns",
        )

        self.assert_lease_owner_not_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_check_lease_no_blazar_no_end_start_in_future(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(start=True, end=False)
        with freezegun.freeze_time("2024-08-21T14:30:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(lease_crd.LeasePhase.PENDING),
            namespace="fake-ns",
        )

        self.assert_lease_owner_not_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_check_lease_no_blazar_end_in_future_no_start(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(start=False, end=True)
        with freezegun.freeze_time("2024-08-21T15:30:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ACTIVE,
                {"id1": "id1", "id2": "id2"},
                {"flavor1": "flavor1", "flavor2": "flavor2"},
            ),
            namespace="fake-ns",
        )

        self.assert_lease_owner_not_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_check_lease_no_blazar_end_in_future_start_in_past(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(start=True, end=True)
        with freezegun.freeze_time("2024-08-21T15:30:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ACTIVE,
                {"id1": "id1", "id2": "id2"},
                {"flavor1": "flavor1", "flavor2": "flavor2"},
            ),
            namespace="fake-ns",
        )

        self.assert_lease_owner_not_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_check_lease_no_blazar_end_and_start_in_future(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(start=True, end=True)
        with freezegun.freeze_time("2024-08-21T14:30:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(lease_crd.LeasePhase.PENDING),
            namespace="fake-ns",
        )

        self.assert_lease_owner_not_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_check_lease_no_blazar_end_in_past_no_start(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(start=False, end=True)
        with freezegun.freeze_time("2024-08-21T16:30:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ACTIVE,
                {"id1": "id1", "id2": "id2"},
                {"flavor1": "flavor1", "flavor2": "flavor2"},
            ),
            namespace="fake-ns",
        )

        self.assert_lease_owner_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_check_lease_no_blazar_end_and_start_in_past(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(start=True, end=True)
        with freezegun.freeze_time("2024-08-21T16:30:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ACTIVE,
                {"id1": "id1", "id2": "id2"},
                {"flavor1": "flavor1", "flavor2": "flavor2"},
            ),
            namespace="fake-ns",
        )

        self.assert_lease_owner_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_check_lease_blazar_lease_active_end_in_future(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)
        os_leases = os_cloud.clients["reservation"].resources["leases"]
        os_leases.list.return_value = util.as_async_iterable(
            [fake_blazar_lease("ACTIVE")]
        )

        lease_data = fake_lease(start=True, end=True)
        with freezegun.freeze_time("2024-08-21T15:30:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ACTIVE,
                {"id1": "newid1", "id2": "newid2"},
                {"flavor1": "newflavor1", "flavor2": "newflavor2"},
            ),
            namespace="fake-ns",
        )

        self.assert_lease_owner_not_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_check_lease_blazar_lease_active_end_in_grace_period(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)
        os_leases = os_cloud.clients["reservation"].resources["leases"]
        os_leases.list.return_value = util.as_async_iterable(
            [fake_blazar_lease("ACTIVE")]
        )

        lease_data = fake_lease(start=True, end=True)
        with freezegun.freeze_time("2024-08-21T15:55:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ACTIVE,
                {"id1": "newid1", "id2": "newid2"},
                {"flavor1": "newflavor1", "flavor2": "newflavor2"},
            ),
            namespace="fake-ns",
        )

        self.assert_lease_owner_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_check_lease_blazar_lease_active_end_in_past(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)
        os_leases = os_cloud.clients["reservation"].resources["leases"]
        os_leases.list.return_value = util.as_async_iterable(
            [fake_blazar_lease("ACTIVE")]
        )

        lease_data = fake_lease(start=True, end=True)
        with freezegun.freeze_time("2024-08-21T16:30:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(
                lease_crd.LeasePhase.ACTIVE,
                {"id1": "newid1", "id2": "newid2"},
                {"flavor1": "newflavor1", "flavor2": "newflavor2"},
            ),
            namespace="fake-ns",
        )

        self.assert_lease_owner_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_check_lease_blazar_lease_not_active_end_in_future(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)
        os_leases = os_cloud.clients["reservation"].resources["leases"]
        os_leases.list.return_value = util.as_async_iterable(
            [fake_blazar_lease("STARTING")]
        )

        lease_data = fake_lease(start=True, end=True)
        with freezegun.freeze_time("2024-08-21T15:30:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(lease_crd.LeasePhase.STARTING),
            namespace="fake-ns",
        )

        self.assert_lease_owner_not_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_check_lease_blazar_lease_not_active_end_in_grace_period(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)
        os_leases = os_cloud.clients["reservation"].resources["leases"]
        os_leases.list.return_value = util.as_async_iterable(
            [fake_blazar_lease("STARTING")]
        )

        lease_data = fake_lease(start=True, end=True)
        with freezegun.freeze_time("2024-08-21T15:55:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(lease_crd.LeasePhase.STARTING),
            namespace="fake-ns",
        )

        self.assert_lease_owner_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_check_lease_blazar_lease_not_active_end_in_past(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)
        os_leases = os_cloud.clients["reservation"].resources["leases"]
        os_leases.list.return_value = util.as_async_iterable(
            [fake_blazar_lease("STARTING")]
        )

        lease_data = fake_lease(start=True, end=True)
        with freezegun.freeze_time("2024-08-21T16:30:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(lease_crd.LeasePhase.STARTING),
            namespace="fake-ns",
        )

        self.assert_lease_owner_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_check_lease_blazar_no_lease_end_in_future(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(start=True, end=True)
        with freezegun.freeze_time("2024-08-21T15:30:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_not_called()

        self.assert_lease_owner_not_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_check_lease_blazar_no_lease_end_in_grace_period(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(start=True, end=True)
        with freezegun.freeze_time("2024-08-21T15:55:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_not_called()

        self.assert_lease_owner_deleted(k8s_client)

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_check_lease_blazar_no_lease_end_in_past(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease_data = fake_lease(start=True, end=True)
        with freezegun.freeze_time("2024-08-21T16:30:00Z"):
            await operator.check_lease(lease_data, mock.Mock())

        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_not_called()

        self.assert_lease_owner_deleted(k8s_client)

    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_delete_lease_finalizer_present(self, k8s_client):
        lease = fake_lease()
        lease["metadata"]["finalizers"].append("anotherfinalizer")

        with self.assertRaises(kopf.TemporaryError):
            await operator.delete_lease(lease, mock.Mock())

        # Check that the lease status was not updated
        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_not_called()

    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_delete_lease_secret_already_deleted(self, k8s_client):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)
        k8s_client.apis["v1"].resources["secrets"].fetch.side_effect = (
            util.k8s_api_error(404)
        )

        lease = fake_lease()
        await operator.delete_lease(lease, mock.Mock())

        # Check that the phase was updated to deleting
        k8s_client.apis[API_VERSION].resources[
            "leases/status"
        ].replace.assert_called_once_with(
            "fake-lease",
            util.LeaseStatusMatcher(lease_crd.LeasePhase.DELETING),
            namespace="fake-ns",
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_delete_lease_appcred_already_deleted(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)
        type(os_cloud).is_authenticated = mock.PropertyMock(return_value=False)

        lease = fake_lease()
        await operator.delete_lease(lease, mock.Mock())

        # Assert that no openstack clients were used
        os_cloud.api_client.assert_not_called()

        # Assert that the credential secret was still deleted
        k8s_client.apis["v1"].resources["secrets"].delete.assert_called_once_with(
            "fake-credential", namespace="fake-ns"
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_delete_lease_no_end(self, k8s_client, openstack_from_secret_data):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease = fake_lease(end=False)
        await operator.delete_lease(lease, mock.Mock())

        # Assert that there was no attempt to find a lease
        os_cloud.clients["reservation"].resources["leases"].list.assert_not_called()

        # Assert that the app cred was deleted
        os_cloud.clients["identity"].resources[
            "application_credentials"
        ].delete.assert_called_once_with("appcredid")

        # Assert that the credential secret was deleted
        k8s_client.apis["v1"].resources["secrets"].delete.assert_called_once_with(
            "fake-credential", namespace="fake-ns"
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_delete_lease_no_blazar(self, k8s_client, openstack_from_secret_data):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease = fake_lease()
        await operator.delete_lease(lease, mock.Mock())

        # Assert that there was no attempt to find a lease
        os_cloud.clients["reservation"].resources["leases"].list.assert_not_called()

        # Assert that the app cred was deleted
        os_cloud.clients["identity"].resources[
            "application_credentials"
        ].delete.assert_called_once_with("appcredid")

        # Assert that the credential secret was deleted
        k8s_client.apis["v1"].resources["secrets"].delete.assert_called_once_with(
            "fake-credential", namespace="fake-ns"
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_delete_lease_blazar_no_lease(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)

        lease = fake_lease()
        await operator.delete_lease(lease, mock.Mock())

        # Assert that there was an attempt to find the lease but no delete
        os_leases = os_cloud.clients["reservation"].resources["leases"]
        os_leases.list.assert_called_once_with()
        os_leases.delete.assert_not_called()

        # Assert that the app cred was deleted
        os_cloud.clients["identity"].resources[
            "application_credentials"
        ].delete.assert_called_once_with("appcredid")

        # Assert that the credential secret was deleted
        k8s_client.apis["v1"].resources["secrets"].delete.assert_called_once_with(
            "fake-credential", namespace="fake-ns"
        )

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "yes")
    async def test_delete_lease_blazar_lease_exists(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)
        os_leases = os_cloud.clients["reservation"].resources["leases"]
        os_leases.list.return_value = util.as_async_iterable([fake_blazar_lease()])

        lease = fake_lease()
        # This should raise a temporary error after the lease has been deleted to
        # force a requeue
        with self.assertRaises(kopf.TemporaryError):
            await operator.delete_lease(lease, mock.Mock())

        # Assert that the lease was deleted
        os_leases.delete.assert_called_once_with("blazarleaseid")

        # Assert that the appcred was retained
        os_cloud.clients["identity"].resources[
            "application_credentials"
        ].delete.assert_not_called()
        k8s_client.apis["v1"].resources["secrets"].delete.assert_not_called()

    @mock.patch.object(openstack, "from_secret_data")
    @mock.patch.object(operator, "K8S_CLIENT", new_callable=util.mock_k8s_client)
    @mock.patch.object(operator, "LEASE_BLAZAR_ENABLED", "no")
    async def test_delete_lease_appcred_not_dangerous(
        self, k8s_client, openstack_from_secret_data
    ):
        # Configure the Kubernetes client
        self.k8s_client_config_common(k8s_client)

        # Configure the OpenStack cloud
        os_cloud = openstack_from_secret_data.return_value = util.mock_openstack_cloud()
        self.os_cloud_config_common(os_cloud)
        os_appcreds = os_cloud.clients["identity"].resources["application_credentials"]
        os_appcreds.delete.side_effect = util.httpx_status_error(403)

        logger = mock.Mock()

        lease = fake_lease()
        await operator.delete_lease(lease, logger)

        # Assert that an attempt was made to delete the appcred
        os_appcreds.delete.assert_called_once_with("appcredid")

        # Assert that a warning was logged that the appcred couldn't be deleted
        logger.warn.assert_called_with(
            "unable to delete application credential for cluster"
        )

        # Assert that the credential secret was still deleted
        k8s_client.apis["v1"].resources["secrets"].delete.assert_called_once_with(
            "fake-credential", namespace="fake-ns"
        )
