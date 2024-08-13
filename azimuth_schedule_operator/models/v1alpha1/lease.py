import datetime as dt
import typing as t

from pydantic import Field

from kube_custom_resource import CustomResource, schema


class Machine(schema.BaseModel):
    """
    Represents a reservation for a machine.
    """
    size_id: schema.constr(min_length = 1) = Field(
        ...,
        description = "The ID of the size for the machine."
    )
    count: schema.conint(gt = 0) = Field(
        ...,
        description = "The number of machines of this size to reserve."
    )


class ResourcesSpec(schema.BaseModel):
    """
    The resources that a lease is reserving.
    """
    machines: t.List[Machine] = Field(
        default_factory = list,
        description = "Machines that should be reserved by the lease."
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
    ends_at: schema.Optional[dt.datetime] = Field(
        None,
        description = (
            "The end time for the lease. "
            "If no end time is given, the lease is assumed to be infinite."
        )
    )
    grace_period: schema.Optional[schema.conint(ge = 0)] = Field(
        None,
        description = (
            "The grace period before the end of the lease that the platform "
            "will be given to shut down gracefully. "
            "If not given, the operator default grace period will be used."
        )
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
    error_message: str = Field(
        "",
        description = "The error message for the lease, if known."
    )
    size_map: schema.Dict[str, str] = Field(
        default_factory = dict,
        description = "Mapping of original size ID to reserved size ID."
    )

    def set_phase(self, phase: LeasePhase, error_message: t.Optional[str] = None):
        """
        Set the phase of the lease, along with an optional error message.
        """
        self.phase = phase
        self.error_message = error_message if phase == LeasePhase.ERROR else ""


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
