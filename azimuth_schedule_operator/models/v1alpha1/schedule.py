import datetime

import kube_custom_resource as crd
import pydantic
from kube_custom_resource import schema


class ScheduleStatus(schema.BaseModel):
    # updated to show operator found CRD
    ref_exists: bool = False
    # updated when delete has been triggered
    ref_delete_triggered: bool = False
    updated_at: schema.Optional[datetime.datetime] = None


class ScheduleRef(schema.BaseModel):
    api_version: str
    kind: str
    name: str


class ScheduleSpec(schema.BaseModel):
    ref: ScheduleRef
    not_after: datetime.datetime


class Schedule(
    crd.CustomResource,
    scope=crd.Scope.NAMESPACED,
    subresources={"status": {}},
):
    spec: ScheduleSpec
    status: ScheduleStatus = pydantic.Field(default_factory=ScheduleStatus)


def get_fake():
    return Schedule(**get_fake_dict())


def get_fake_dict():
    return dict(
        apiVersion="scheduling.azimuth.stackhpc.com/v1alpha1",
        kind="Schedule",
        metadata=dict(name="test1", uid="fakeuid1", namespace="ns1"),
        spec=dict(
            ref=dict(apiVersion="v1", kind="Pod", name="test1"),
            notAfter=datetime.datetime.now(datetime.timezone.utc),
        ),
    )
