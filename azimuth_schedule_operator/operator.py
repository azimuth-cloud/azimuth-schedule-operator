import asyncio
import collections
import datetime
import json
import logging
import os
import sys

import easykube
import httpx
import kopf

from azimuth_schedule_operator import openstack
from azimuth_schedule_operator.models import registry
from azimuth_schedule_operator.models.v1alpha1 import (
    lease as lease_crd,
)
from azimuth_schedule_operator.models.v1alpha1 import (
    schedule as schedule_crd,
)
from azimuth_schedule_operator.utils import k8s

LOG = logging.getLogger(__name__)
K8S_CLIENT = None

CHECK_INTERVAL_SECONDS = int(
    os.environ.get(
        "AZIMUTH_SCHEDULE_CHECK_INTERVAL_SECONDS",
        # By default, check schedules and leases every 60s
        "60",
    )
)
LEASE_CHECK_INTERVAL_SECONDS = int(
    os.environ.get("AZIMUTH_LEASE_CHECK_INTERVAL_SECONDS", CHECK_INTERVAL_SECONDS)
)
LEASE_DEFAULT_GRACE_PERIOD_SECONDS = int(
    os.environ.get(
        "AZIMUTH_LEASE_DEFAULT_GRACE_PERIOD_SECONDS",
        # Give platforms 10 minutes to delete by default
        "600",
    )
)
# Indicates whether leases should use Blazar
# Valid values are "yes", "no" and "auto"
# The default is "auto", which means Blazar will be used iff it is available
LEASE_BLAZAR_ENABLED = os.environ.get("AZIMUTH_LEASE_BLAZAR_ENABLED", "auto")


@kopf.on.startup()
async def startup(settings, **kwargs):
    # Use a scheduler-specific format for the finalizer
    settings.persistence.finalizer = registry.API_GROUP
    # Use the annotation-based storage only (not status)
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(
        prefix=registry.API_GROUP
    )
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(
        prefix=registry.API_GROUP,
        key="last-handled-configuration",
    )
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


async def ekresource_for_model(model, subresource=None):
    """Returns an easykube resource for the given model."""
    api = K8S_CLIENT.api(f"{registry.API_GROUP}/{model._meta.version}")
    resource = model._meta.plural_name
    if subresource:
        resource = f"{resource}/{subresource}"
    return await api.resource(resource)


async def save_instance_status(instance):
    """Save the status of the given instance."""
    ekresource = await ekresource_for_model(instance.__class__, "status")
    try:
        data = await ekresource.replace(
            instance.metadata.name,
            {
                # Include the resource version for optimistic concurrency
                "metadata": {"resourceVersion": instance.metadata.resource_version},
                "status": instance.status.model_dump(exclude_defaults=True),
            },
            namespace=instance.metadata.namespace,
        )
    except easykube.ApiError as exc:
        # Retry as soon as possible after a 409
        if exc.status_code == 409:
            raise kopf.TemporaryError("conflict updating status", delay=1)
        else:
            raise
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
    ref_exists: bool | None = None,
    ref_delete_triggered: bool | None = None,
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


async def find_blazar_lease(blazar_client, lease_name):
    return await anext(
        (
            lease
            async for lease in blazar_client.resource("leases").list()
            if lease["name"] == lease_name
        ),
        None,
    )


def blazar_enabled(cloud):
    """Returns True if Blazar should be used, False otherwise."""
    if LEASE_BLAZAR_ENABLED == "yes":
        return True
    elif LEASE_BLAZAR_ENABLED == "auto":
        try:
            _ = cloud.api_client("reservation")
        except openstack.ApiNotSupportedError:
            return False
        else:
            return True
    else:
        return False


class BlazarLeaseCreateError(Exception):
    """Raised when there is a permanent error creating a Blazar lease."""


async def create_blazar_lease(blazar_client, lease_name, lease):
    # Sum the requested machine counts by flavor ID
    flavor_counts = collections.defaultdict(int)
    for machine in lease.spec.resources.machines:
        flavor_counts[machine.size_id] += machine.count
    try:
        return await blazar_client.resource("leases").create(
            {
                "name": lease_name,
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
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in [400, 500]:
            try:
                message = exc.response.json()["error_message"]
            except (json.JSONDecodeError, TypeError, KeyError):
                message = exc.response.text
            raise BlazarLeaseCreateError(f"error creating blazar lease - {message}")
        else:
            raise


def get_size_map(blazar_lease):
    """Produce a map from requested size ID to reservation size ID."""
    size_map = {}
    for reservation in blazar_lease.get("reservations", []):
        if (
            reservation["resource_type"] == "flavor:instance"
            and "resource_properties" in reservation
        ):
            properties = json.loads(reservation["resource_properties"])
            size_map[properties["id"]] = reservation["id"]
    return size_map


async def get_size_name_map(cloud, size_map):
    """Produce a size name map for the given size map."""
    compute_client = cloud.api_client("compute")
    flavor_names = {
        flavor.id: flavor.name
        async for flavor in compute_client.resource("flavors").list()
    }
    size_name_map = {}
    for original_id, new_id in size_map.items():
        try:
            size_name_map[flavor_names[original_id]] = flavor_names[new_id]
        except KeyError:
            pass
    return size_name_map


async def update_lease_status_no_blazar(cloud, lease):
    """Updates the lease status when Blazar is not used for the lease."""
    if lease.spec.starts_at:
        now = datetime.datetime.now(datetime.timezone.utc)
        lease_started = now >= lease.spec.starts_at
    else:
        # No start date means start now
        lease_started = True
    if lease_started:
        lease.status.set_phase(lease_crd.LeasePhase.ACTIVE)
        lease.status.size_map = {
            m.size_id: m.size_id for m in lease.spec.resources.machines
        }
        lease.status.size_name_map = await get_size_name_map(
            cloud, lease.status.size_map
        )
    else:
        lease.status.set_phase(lease_crd.LeasePhase.PENDING)


@kopf.on.create(registry.API_GROUP, "lease")
@kopf.on.resume(registry.API_GROUP, "lease")
async def reconcile_lease(body, logger, **_):
    lease = lease_crd.Lease.model_validate(body)

    # Put the lease into a pending state as soon as possible
    if lease.status.phase == lease_crd.LeasePhase.UNKNOWN:
        lease.status.set_phase(lease_crd.LeasePhase.PENDING)
        await save_instance_status(lease)

    # Create a cloud instance from the referenced credential secret
    secrets = await K8S_CLIENT.api("v1").resource("secrets")
    cloud_creds = await secrets.fetch(
        lease.spec.cloud_credentials_secret_name, namespace=lease.metadata.namespace
    )
    async with openstack.from_secret_data(cloud_creds.data) as cloud:
        # If the lease has no end date, we don't attempt to use Blazar
        if not lease.spec.ends_at:
            logger.info("lease has no end date")
            await update_lease_status_no_blazar(cloud, lease)
            await save_instance_status(lease)
            return

        # If the lease has an end date, we might need to do some Blazar stuff
        if blazar_enabled(cloud):
            blazar_client = cloud.api_client("reservation", timeout=30)
            logger.info("checking if blazar lease exists")
            blazar_lease_name = f"az-{lease.metadata.name}"
            blazar_lease = await find_blazar_lease(blazar_client, blazar_lease_name)
            if not blazar_lease:
                # NOTE(mkjpryor)
                #
                # We only create the Blazar lease if we are in the PENDING phase
                #
                # If we are in any other phase and the lease does not exist, then we
                # leave the status as-is but log it. This can happen in one of three
                # ways:
                #
                #  1. The lease was not created due to an unrecoverable error
                #  2. The lease we created was deleted by someone else
                #  3. Blazar has been enabled on a cloud after a lease has already been
                #     processed using the non-Blazar code path
                if lease.status.phase == lease_crd.LeasePhase.PENDING:
                    logger.info("creating blazar lease")
                    try:
                        blazar_lease = await create_blazar_lease(
                            blazar_client, blazar_lease_name, lease
                        )
                    except BlazarLeaseCreateError as exc:
                        logger.error(str(exc))
                        lease.status.set_phase(lease_crd.LeasePhase.ERROR, str(exc))
                        await save_instance_status(lease)
                        return
                else:
                    phase = lease.status.phase.name
                    logger.warn(f"phase is {phase} but blazar lease does not exist")
            # Set the status from the created lease
            if blazar_lease:
                blazar_lease_status = blazar_lease["status"]
                logger.info(f"blazar lease has status '{blazar_lease_status}'")
                lease.status.set_phase(lease_crd.LeasePhase[blazar_lease_status])
                if lease.status.phase == lease_crd.LeasePhase.ACTIVE:
                    lease.status.size_map = get_size_map(blazar_lease)
                    lease.status.size_name_map = await get_size_name_map(
                        cloud, lease.status.size_map
                    )
                # Save the current status of the lease
                await save_instance_status(lease)
        else:
            # We are not using Blazar
            # We just control the phase based on the start and end times
            logger.info("not attempting to use blazar")
            await update_lease_status_no_blazar(cloud, lease)
            await save_instance_status(lease)
            return


@kopf.timer(
    registry.API_GROUP,
    "lease",
    interval=LEASE_CHECK_INTERVAL_SECONDS,
    # This means that the timer will not run while we are modifying the resource
    idle=LEASE_CHECK_INTERVAL_SECONDS,
)
async def check_lease(body, logger, **_):
    lease = lease_crd.Lease.model_validate(body)

    # Create a cloud instance from the referenced credential secret
    secrets = await K8S_CLIENT.api("v1").resource("secrets")
    cloud_creds = await secrets.fetch(
        lease.spec.cloud_credentials_secret_name, namespace=lease.metadata.namespace
    )
    async with openstack.from_secret_data(cloud_creds.data) as cloud:
        if not lease.spec.ends_at:
            await update_lease_status_no_blazar(cloud, lease)
            await save_instance_status(lease)
            return

        # If the lease has an end date, we may need to contact Blazar
        if blazar_enabled(cloud):
            blazar_client = cloud.api_client("reservation", timeout=30)
            logger.info("checking if blazar lease exists")
            blazar_lease_name = f"az-{lease.metadata.name}"
            blazar_lease = await find_blazar_lease(blazar_client, blazar_lease_name)
            if blazar_lease:
                blazar_lease_status = blazar_lease["status"]
                logger.info(f"blazar lease has status '{blazar_lease_status}'")
                # Set the phase from the Blazar lease status
                lease.status.set_phase(lease_crd.LeasePhase[blazar_lease_status])
                # If the lease is active, report the size map
                if lease.status.phase == lease_crd.LeasePhase.ACTIVE:
                    lease.status.size_map = get_size_map(blazar_lease)
                    lease.status.size_name_map = await get_size_name_map(
                        cloud, lease.status.size_map
                    )
                await save_instance_status(lease)
            else:
                phase = lease.status.phase.name
                logger.warn(f"phase is {phase} but blazar lease does not exist")
        else:
            logger.info("not attempting to use blazar")
            await update_lease_status_no_blazar(cloud, lease)
            await save_instance_status(lease)

    # Calculate the grace period before the end of the lease that we want to use
    grace_period = (
        lease.spec.grace_period
        if lease.spec.grace_period is not None
        else LEASE_DEFAULT_GRACE_PERIOD_SECONDS
    )
    # Calculate the threshold time at which we want to issue a delete
    threshold = lease.spec.ends_at - datetime.timedelta(seconds=grace_period)
    # Issue the delete if the threshold time has passed
    if threshold < datetime.datetime.now(datetime.timezone.utc):
        logger.info("lease is ending within grace period - deleting owners")
        for owner in lease.metadata.owner_references:
            resource = await K8S_CLIENT.api(owner.api_version).resource(owner.kind)
            await resource.delete(
                owner.name,
                # Make sure that we block the owner from deleting, if configured
                propagation_policy="Foreground",
                namespace=lease.metadata.namespace,
            )
    else:
        logger.info("lease is not within the grace period of ending")


@kopf.on.delete(registry.API_GROUP, "lease")
async def delete_lease(body, logger, **_):
    lease = lease_crd.Lease.model_validate(body)

    # Wait until our finalizer is the only finalizer
    if any(f != registry.API_GROUP for f in lease.metadata.finalizers):
        raise kopf.TemporaryError("waiting for finalizers to be removed", delay=15)

    # Put the lease into a deleting state once we are able to start deleting
    if lease.status.phase != lease_crd.LeasePhase.DELETING:
        lease.status.set_phase(lease_crd.LeasePhase.DELETING)
        await save_instance_status(lease)

    # Once all other finalizers have been removed, we can do our teardown
    # This involves deleting the Blazar lease, if one exists, and the app cred
    secrets = await K8S_CLIENT.api("v1").resource("secrets")
    try:
        cloud_creds = await secrets.fetch(
            lease.spec.cloud_credentials_secret_name, namespace=lease.metadata.namespace
        )
    except easykube.ApiError as exc:
        if exc.status_code == 404:
            # If we can't find the cloud credential, there isn't much we can do
            logger.warn("cloud credential missing - no action taken")
            return
        else:
            raise
    async with openstack.from_secret_data(cloud_creds.data) as cloud:
        # It is possible that the app cred was deleted but the secret wasn't
        # In that case, the cloud will report as unauthenticated
        if cloud.is_authenticated:
            # Check if there is any work to do to delete a Blazar lease
            if lease.spec.ends_at and blazar_enabled(cloud):
                logger.info("checking for blazar lease")
                blazar_client = cloud.api_client("reservation", timeout=30)
                blazar_lease_name = f"az-{lease.metadata.name}"
                blazar_lease = await find_blazar_lease(blazar_client, blazar_lease_name)
                if blazar_lease:
                    logger.info("deleting blazar lease")
                    await blazar_client.resource("leases").delete(blazar_lease["id"])
                    raise kopf.TemporaryError(
                        "waiting for blazar lease to delete", delay=15
                    )
                else:
                    logger.warn("blazar lease does not exist")
            else:
                logger.info("blazar is not used for this lease")

            # Delete the application credential
            identityapi = cloud.api_client("identity", "v3")
            appcreds = identityapi.resource(
                "application_credentials",
                # appcreds are user-namespaced
                prefix=f"users/{cloud.current_user_id}",
            )
            try:
                await appcreds.delete(cloud.application_credential_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 403:
                    logger.warn("unable to delete application credential for cluster")
                else:
                    raise
            logger.info("deleted application credential for cluster")

    # Now the appcred is gone, we can delete the secret
    await secrets.delete(
        lease.spec.cloud_credentials_secret_name, namespace=lease.metadata.namespace
    )
    logger.info("cloud credential secret deleted")
