import asyncio
import logging
import os
import sys

import kopf

from azimuth_schedule_operator.models import registry
from azimuth_schedule_operator.models.v1alpha1 import schedule as schedule_crd
from azimuth_schedule_operator.utils import k8s

LOG = logging.getLogger(__name__)
K8S_CLIENT = None


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
async def cleanup(**kwargs):
    if K8S_CLIENT:
        await K8S_CLIENT.aclose()
    LOG.info("Cleanup complete.")


@kopf.on.create(registry.API_GROUP, "schedule")
@kopf.on.update(registry.API_GROUP, "schedule")
@kopf.on.resume(registry.API_GROUP, "schedule")
async def schedule_changed(body, name, namespace, labels, **kwargs):
    schedule = schedule_crd.Schedule(**body)
    LOG.error(f"seen schedule changed {schedule.spec}")
