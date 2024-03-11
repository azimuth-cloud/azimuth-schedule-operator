import datetime

import kube_custom_resource as crd
from kube_custom_resource import schema
import pydantic


class ScheduleStatus(schema.BaseModel):
    # updated to show operator found CRD
    ref_found: bool = False
    # updated when delete has been triggered
    delete_triggered: bool = False


class ScheduleRef(schema.BaseModel):
    apiVersion: str
    kind: str
    name: str


class ScheduleSpec(schema.BaseModel):
    ref: ScheduleRef
    notBefore: datetime.datetime
    notAfter: datetime.datetime


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
            notBefore=datetime.datetime.now(),
            notAfter=datetime.datetime.now() + datetime.timedelta(days=1),
        ),
        status=dict(ref_found=True, delete_triggered=False),
    )
