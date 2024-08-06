import asyncio
import re

from azimuth_schedule_operator.openstack import openstack


async def _send_create_lease(cloud, json):
    blazar_client = cloud.api_client("reservation")
    leases = blazar_client.resource("leases")
    return await leases.create(json)


async def create_lease(cloud, name, flavor_counts, end_date, start_date=None):
    """Create a lease in Blazar."""
    reservations = [
        {
            "amount": int(count),
            "flavor_id": flavor_id,
            "resource_type": "flavor:instance",
            "affinity": "None",
        }
        for flavor_id, count in flavor_counts.items()
    ]
    json = {
        "name": name,
        "start_date": (
            "now" if not start_date else start_date.strftime("%Y-%m-%d %H:%M")
        ),
        "end_date": end_date.strftime("%Y-%m-%d %H:%M"),
        "reservations": reservations,
        "events": [],
        "before_end_date": None,
    }
    result = await _send_create_lease(cloud, json)
    return result["id"]


async def _fetch_lease(cloud, lease_id):
    blazar_client = cloud.api_client("reservation")
    leases = blazar_client.resource("leases")
    return await leases.fetch(lease_id)


async def get_lease_flavors(cloud, lease_id):
    """Get a lease in Blazar."""
    lease_details = await _fetch_lease(cloud, lease_id)

    status = lease_details["status"]
    if status != "ACTIVE":
        print(f"Lease {lease_id} is {status} and not active.")
        return None
    print(lease_details)
    # flavor fetching seems racey, so add a sleep :/
    await asyncio.sleep(0.1)

    flavor_mapping = {}
    for reservation in lease_details["reservations"]:
        id = reservation["id"]
        # TODO(johngarbutt) this seems a little racey somehow!
        # nova_client = cloud.api_client("compute")
        # flavor = nova_client.resource("flavors")
        # await flavor.fetch(id)
        search = re.search('.*"id": "([^"]*)",.*', reservation["resource_properties"])
        original_flavor_id = None
        if search:
            original_flavor_id = search.group(1)
        else:
            raise Exception(f"Failed to find flavor id in {reservation}")
        flavor_mapping[original_flavor_id] = id

    return flavor_mapping


async def delete_lease(cloud, lease_id):
    """Delete a lease in Blazar."""
    blazar_client = cloud.api_client("reservation")
    leases = blazar_client.resource("leases")
    # TODO(johngarbutt): this seems to timeout quite a lot!
    await leases.delete(lease_id)


async def main():
    import datetime
    from datetime import timezone
    import yaml

    clouds = yaml.safe_load(open("/home/johng/.arcus_staging_clouds.yaml"))
    async with openstack.Cloud.from_clouds(clouds, "openstack", None) as cloud:
        end = datetime.datetime.now(tz=timezone.utc) + datetime.timedelta(hours=1)
        lease_id = await create_lease(
            cloud, "testlease1", {"3091f837-cf3e-4bde-a994-ec01544c3a74": 1}, end
        )
        print(lease_id)

        # wait for active
        details = None
        while details is None:
            details = await get_lease_flavors(cloud, lease_id)
            await asyncio.sleep(1)
        print(details)
        await delete_lease(cloud, lease_id)


if __name__ == "__main__":
    asyncio.run(main())
