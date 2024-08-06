import asyncio
import base64
import datetime
import json
import logging
import os
import sys

import kopf
import httpx

import yaml

from azimuth_schedule_operator.models import registry
from azimuth_schedule_operator.models.v1alpha1 import (
    lease as lease_crd,
    schedule as schedule_crd
)
from azimuth_schedule_operator import openstack
from azimuth_schedule_operator.utils import k8s

LOG = logging.getLogger(__name__)
K8S_CLIENT = None

CHECK_INTERVAL_SECONDS = int(
    os.environ.get("AZIMUTH_SCHEDULE_CHECK_INTERVAL_SECONDS", "60")
)


@kopf.on.startup()
async def startup(settings, **kwargs):
    # Apply kopf setting to force watches to restart periodically
    settings.watching.client_timeout = int(os.environ.get("KOPF_WATCH_TIMEOUT", "600"))
    global K8S_CLIENT
    K8S_CLIENT = k8s.get_k8s_client()
    # Create or update the CRDs
    for crd in registry.get_crd_resources():
        try:
            await K8S_CLIENT.apply_object(crd, force=True)
        except Exception:
            LOG.exception("error applying CRD %s - exiting", crd["metadata"]["name"])
            sys.exit(1)
    LOG.info("All CRDs updated.")
    # Give Kubernetes a chance to create the APIs for the CRDs
    await asyncio.sleep(0.5)
    # Check to see if the APIs for the CRDs are up
    # If they are not, the kopf watches will not start properly
    for crd in registry.get_crd_resources():
        api_group = crd["spec"]["group"]
        preferred_version = next(
            v["name"] for v in crd["spec"]["versions"] if v["storage"]
        )
        api_version = f"{api_group}/{preferred_version}"
        plural_name = crd["spec"]["names"]["plural"]
        try:
            _ = await K8S_CLIENT.get(f"/apis/{api_version}/{plural_name}")
        except Exception:
            LOG.exception("api for %s not available - exiting", crd["metadata"]["name"])
            sys.exit(1)


@kopf.on.cleanup()
async def cleanup(**_):
    if K8S_CLIENT:
        await K8S_CLIENT.aclose()
    LOG.info("Cleanup complete.")


async def ekresource_for_model(model, subresource = None):
    """
    Returns an easykube resource for the given model.
    """
    api = K8S_CLIENT.api(f"{registry.API_GROUP}/{model._meta.version}")
    resource = model._meta.plural_name
    if subresource:
        resource = f"{resource}/{subresource}"
    return await api.resource(resource)


async def save_instance_status(instance):
    """
    Save the status of the given instance.
    """
    ekresource = await ekresource_for_model(instance.__class__, "status")
    data = await ekresource.replace(
        instance.metadata.name,
        {
            # Include the resource version for optimistic concurrency
            "metadata": { "resourceVersion": instance.metadata.resource_version },
            "status": instance.status.model_dump(exclude_defaults = True),
        },
        namespace = instance.metadata.namespace
    )
    # Store the new resource version
    instance.metadata.resource_version = data["metadata"]["resourceVersion"]


async def get_reference(namespace: str, ref: schedule_crd.ScheduleRef):
    resource = await K8S_CLIENT.api(ref.api_version).resource(ref.kind)
    object = await resource.fetch(ref.name, namespace=namespace)
    return object


async def delete_reference(namespace: str, ref: schedule_crd.ScheduleRef):
    resource = await K8S_CLIENT.api(ref.api_version).resource(ref.kind)
    await resource.delete(ref.name, namespace=namespace)


async def update_schedule_status(namespace: str, name: str, status_updates: dict):
    status_resource = await K8S_CLIENT.api(registry.API_VERSION).resource(
        "schedules/status"
    )
    await status_resource.patch(
        name,
        dict(status=status_updates),
        namespace=namespace,
    )


async def check_for_delete(namespace: str, schedule: schedule_crd.Schedule):
    now = datetime.datetime.now(datetime.timezone.utc)
    if now >= schedule.spec.not_after:
        LOG.info(f"Attempting delete for {namespace} and {schedule.metadata.name}.")
        await delete_reference(namespace, schedule.spec.ref)
        await update_schedule(
            namespace, schedule.metadata.name, ref_delete_triggered=True
        )
    else:
        LOG.info(f"No delete for {namespace} and {schedule.metadata.name}.")


async def update_schedule(
    namespace: str,
    name: str,
    ref_exists: bool = None,
    ref_delete_triggered: bool = None,
):
    now = datetime.datetime.now(datetime.timezone.utc)
    now_string = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    status_updates = dict(updatedAt=now_string)

    if ref_exists is not None:
        status_updates["refExists"] = ref_exists
    if ref_delete_triggered is not None:
        status_updates["refDeleteTriggered"] = ref_delete_triggered

    LOG.info(f"Updating status for {name} in {namespace} with: {status_updates}")
    await update_schedule_status(namespace, name, status_updates)


@kopf.timer(registry.API_GROUP, "schedule", interval=CHECK_INTERVAL_SECONDS)
async def schedule_check(body, namespace, **_):
    schedule = schedule_crd.Schedule(**body)

    if not schedule.status.ref_exists:
        await get_reference(namespace, schedule.spec.ref)
        await update_schedule(namespace, schedule.metadata.name, ref_exists=True)

    if not schedule.status.ref_delete_triggered:
        await check_for_delete(namespace, schedule)


@kopf.timer(registry.API_GROUP, "lease", interval=CHECK_INTERVAL_SECONDS)
@kopf.on.create(registry.API_GROUP, "lease")
@kopf.on.update(registry.API_GROUP, "lease", field = "spec")
@kopf.on.resume(registry.API_GROUP, "lease")
async def reconcile_lease(body, **_):
    lease = lease_crd.Lease.model_validate(body)
    # Put the lease into a pending state as soon as possible
    if lease.status.phase == lease_crd.LeasePhase.UNKNOWN:
        lease.status.phase = lease_crd.LeasePhase.PENDING
        await save_instance_status(lease)
    # Get the cloud credentials to use to create the lease
    secrets = await K8S_CLIENT.api("v1").resource("secrets")
    cloud_creds = await secrets.fetch(
        lease.spec.cloud_credentials_secret_name,
        namespace = lease.metadata.namespace
    )
    clouds = yaml.safe_load(base64.b64decode(cloud_creds.data["clouds.yaml"]))
    if "cacert" in cloud_creds.data:
        cacert = base64.b64decode(cloud_creds.data["cacert"]).decode()
    else:
        cacert = None
    async with openstack.Cloud.from_clouds(clouds, "openstack", cacert) as cloud:
        blazar_leases = cloud.api_client("reservation", timeout = 30).resource("leases")
        blazar_lease_name = f"az-{lease.metadata.name}"
        # First, try to find the Blazar release by name
        blazar_lease = await anext(
            (l async for l in blazar_leases.list() if l["name"] == blazar_lease_name),
            None
        )
        # If it doesn't exist, create it
        if not blazar_lease:
            # Aggregate the virtual machine resource counts by name
            flavor_counts = {}
            for vm in lease.spec.resources.virtual_machines:
                flavor_counts[vm.flavor_id] = flavor_counts.get(vm.flavor_id, 0) + vm.count
            try:
                blazar_lease = await blazar_leases.create(
                    {
                        "name": blazar_lease_name,
                        "start_date": (
                            lease.spec.starts_at.strftime("%Y-%m-%d %H:%M")
                            if lease.spec.starts_at
                            else "now"
                        ),
                        "end_date": lease.spec.ends_at.strftime("%Y-%m-%d %H:%M"),
                        "reservations": [
                            {
                                "amount": int(count),
                                "flavor_id": flavor_id,
                                "resource_type": "flavor:instance",
                                "affinity": "None",
                            }
                            for flavor_id, count in flavor_counts.items()
                        ],
                        "events": [],
                        "before_end_date": None,
                    }
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400:
                    # TODO(johngarbutt): check for problems with the lease
                    LOG.error(f"error creating lease: {e.response.json()}")
                    lease.status.phase = lease_crd.LeasePhase.ERROR
                    await save_instance_status(lease)
                raise
    # Save the status of the lease
    lease.status.phase = lease_crd.LeasePhase[blazar_lease["status"]]
    # If the lease is active, report the flavor map
    # This is a map from original flavor ID to reserved flavor ID
    if lease.status.phase == lease_crd.LeasePhase.ACTIVE:
        flavor_map = {}
        for reservation in blazar_lease.get("reservations", []):
            if reservation["resource_type"] != "flavor:instance":
                continue
            if "resource_properties" in reservation:
                properties = json.loads(reservation["resource_properties"])
                flavor_map[properties["id"]] = reservation["id"]
        lease.status.flavor_map = flavor_map
    else:
        LOG.info(f"lease {lease.metadata.name} is in phase {lease.status.phase}")
    await save_instance_status(lease)


@kopf.on.delete(registry.API_GROUP, "lease")
async def delete_lease(body, **_):
    lease = lease_crd.Lease.model_validate(body)
    # Put the lease into a deleting state as soon as possible
    if lease.status.phase != lease_crd.LeasePhase.DELETING:
        lease.status.phase = lease_crd.LeasePhase.DELETING
        await save_instance_status(lease)
    # Get the cloud credentials to use to create the lease
    secrets = await K8S_CLIENT.api("v1").resource("secrets")
    cloud_creds = await secrets.fetch(
        lease.spec.cloud_credentials_secret_name,
        namespace = lease.metadata.namespace
    )
    clouds = yaml.safe_load(base64.b64decode(cloud_creds.data["clouds.yaml"]))
    if "cacert" in cloud_creds.data:
        cacert = base64.b64decode(cloud_creds.data["cacert"]).decode()
    else:
        cacert = None
    async with openstack.Cloud.from_clouds(clouds, "openstack", cacert) as cloud:
        blazar_leases = cloud.api_client("reservation", timeout = 30).resource("leases")
        blazar_lease_name = f"az-{lease.metadata.name}"
        # First, try to find the Blazar release by name
        blazar_lease = await anext(
            (l async for l in blazar_leases.list() if l["name"] == blazar_lease_name),
            None
        )
        # If there is no such lease, we are done
        # If there is, delete it and check again in a few seconds
        if blazar_lease:
            await blazar_leases.delete(blazar_lease["id"])
            raise kopf.TemporaryError("waiting for blazar lease to delete", delay = 15)
