import datetime as dt
import typing as t

from pydantic import Field

from kube_custom_resource import CustomResource, schema


class VirtualMachine(schema.BaseModel):
    """
    Represents a reservation for a virtual machine.
    """
    flavor_id: schema.constr(min_length = 1) = Field(
        ...,
        description = "The ID of the flavor for the virtual machine."
    )
    count: schema.conint(gt = 0) = Field(
        ...,
        description = "The number of virtual machines of this flavor to reserve."
    )


class ResourcesSpec(schema.BaseModel):
    """
    The resources that a lease is reserving.
    """
    virtual_machines: t.List[VirtualMachine] = Field(
        default_factory = list,
        description = "Virtual machine resources that should be reserved by the lease."
    )


class LeaseSpec(schema.BaseModel):
    """
    The spec of a lease.
    """
    cloud_credentials_secret_name: schema.constr(min_length = 1) = Field(
        ...,
        description = "The name of the secret containing the cloud credentials."
    )
    starts_at: schema.Optional[dt.datetime] = Field(
        None,
        description = (
            "The start time for the lease. "
            "If no start time is given, it is assumed to start immediately."
        )
    )
    ends_at: dt.datetime = Field(
        ...,
        description = "The end time for the lease."
    )
    resources: ResourcesSpec = Field(
        ...,
        description = "The resources that the lease is reserving."
    )


class LeasePhase(str, schema.Enum):
    """
    The phase of the lease.
    """
    # Stable phases
    PENDING     = "Pending"
    ACTIVE      = "Active"
    TERMINATED  = "Terminated"
    ERROR       = "Error"
    # Transitional phases
    CREATING    = "Creating"
    STARTING    = "Starting"
    UPDATING    = "Updating"
    TERMINATING = "Terminating"
    DELETING    = "Deleting"
    UNKNOWN     = "Unknown"


class LeaseStatus(schema.BaseModel, extra = "allow"):
    """
    The status of a lease.
    """
    phase: LeasePhase = Field(
        LeasePhase.UNKNOWN.value,
        description = "The phase of the lease."
    )
    flavor_map: schema.Dict[str, str] = Field(
        default_factory = dict,
        description = "Mapping of original flavor ID to reserved flavor ID."
    )


class Lease(
    CustomResource,
    subresources = {"status": {}},
    printer_columns = [
        {
            "name": "Starts At",
            "type": "string",
            "format": "date-time",
            "jsonPath": ".spec.startsAt",
        },
        {
            "name": "Ends At",
            "type": "string",
            "format": "date-time",
            "jsonPath": ".spec.endsAt",
        },
        {
            "name": "phase",
            "type": "string",
            "jsonPath": ".status.phase",
        },
    ]
):
    """
    A lease consisting of one or more reserved resources.
    """
    spec: LeaseSpec
    status: LeaseStatus = Field(default_factory = LeaseStatus)
