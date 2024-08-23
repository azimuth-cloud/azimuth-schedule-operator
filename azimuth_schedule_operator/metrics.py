import asyncio
import datetime
import functools

from aiohttp import web

import easykube

from .models import registry


class Metric:
    # The prefix for the metric
    prefix = None
    # The suffix for the metric
    suffix = None
    # The type of the metric - info or guage
    type = "info"
    # The description of the metric
    description = None

    def __init__(self):
        self._objs = []

    def add_obj(self, obj):
        self._objs.append(obj)

    @property
    def name(self):
        return f"{self.prefix}_{self.suffix}"

    def labels(self, obj):
        """The labels for the given object."""
        return {}

    def value(self, obj):
        """The value for the given object."""
        return 1

    def records(self):
        """Returns the records for the metric, i.e. a list of (labels, value) tuples."""
        for obj in self._objs:
            yield self.labels(obj), self.value(obj)


class ScheduleMetric(Metric):
    prefix = "azimuth_schedule"

    def labels(self, obj):
        return {
            "schedule_namespace": obj.metadata.namespace,
            "schedule_name": obj.metadata.name,
            "ref_kind": obj.spec.ref.kind,
            "ref_name": obj.spec.ref.name,
        }


class ScheduleRefFound(ScheduleMetric):
    suffix = "ref_found"
    type = "gauge"
    description = "Indicates whether the ref has been found"

    def value(self, obj):
        return 1 if obj.get("status", {}).get("refExists", False) else 0


class ScheduleDeleteTriggered(ScheduleMetric):
    suffix = "delete_triggered"
    type = "gauge"
    description = "Indicates whether the schedule has triggered a delete"

    def value(self, obj):
        return 1 if obj.get("status", {}).get("refDeleteTriggered", False) else 0


class LeaseMetric(Metric):
    prefix = "azimuth_lease"

    def labels(self, obj):
        return {
            "lease_namespace": obj.metadata.namespace,
            "lease_name": obj.metadata.name,
        }


class LeasePhase(LeaseMetric):
    suffix = "phase"
    description = "The phase of the lease"

    def labels(self, obj):
        return {
            **super().labels(obj),
            "phase": obj.get("status", {}).get("phase", "Unknown"),
        }


class LeaseStartsAt(LeaseMetric):
    suffix = "starts_at"
    type = "gauge"
    description = "The start time of the lease"

    def value(self, obj):
        created_at = obj.metadata["creationTimestamp"]
        starts_at = obj.get("spec", {}).get("startsAt", created_at)
        return datetime.datetime.fromisoformat(starts_at).timestamp()


class LeaseEndsAt(LeaseMetric):
    suffix = "ends_at"
    type = "gauge"
    description = "The end time of the lease"

    def value(self, obj):
        ends_at = obj.get("spec", {}).get("endsAt")
        if ends_at:
            return datetime.datetime.fromisoformat(ends_at).timestamp()
        else:
            return -1


def escape(content):
    """Escape the given content for use in metric output."""
    return content.replace("\\", r"\\").replace("\n", r"\n").replace('"', r"\"")


def format_value(value):
    """Formats a value for output, e.g. using Go formatting."""
    formatted = repr(value)
    dot = formatted.find(".")
    if value > 0 and dot > 6:
        mantissa = f"{formatted[0]}.{formatted[1:dot]}{formatted[dot + 1:]}".rstrip(
            "0."
        )
        return f"{mantissa}e+0{dot - 1}"
    else:
        return formatted


def render_openmetrics(*metrics):
    """Renders the metrics using OpenMetrics text format."""
    output = []
    for metric in metrics:
        if metric.description:
            output.append(f"# HELP {metric.name} {escape(metric.description)}\n")
        output.append(f"# TYPE {metric.name} {metric.type}\n")

        for labels, value in metric.records():
            if labels:
                labelstr = "{{{0}}}".format(
                    ",".join([f'{k}="{escape(v)}"' for k, v in sorted(labels.items())])
                )
            else:
                labelstr = ""
            output.append(f"{metric.name}{labelstr} {format_value(value)}\n")
    output.append("# EOF\n")

    return (
        "application/openmetrics-text; version=1.0.0; charset=utf-8",
        "".join(output).encode("utf-8"),
    )


METRICS = {
    registry.API_GROUP: {
        "leases": [
            LeasePhase,
            LeaseEndsAt,
        ],
        "schedules": [
            ScheduleRefFound,
            ScheduleDeleteTriggered,
        ],
    },
}


async def metrics_handler(ekclient, request):
    """Produce metrics for the operator."""
    metrics = []
    for api_group, resources in METRICS.items():
        ekapi = await ekclient.api_preferred_version(api_group)
        for resource, metric_classes in resources.items():
            ekresource = await ekapi.resource(resource)
            resource_metrics = [klass() for klass in metric_classes]
            async for obj in ekresource.list(all_namespaces=True):
                for metric in resource_metrics:
                    metric.add_obj(obj)
            metrics.extend(resource_metrics)

    content_type, content = render_openmetrics(*metrics)
    return web.Response(headers={"Content-Type": content_type}, body=content)


async def metrics_server():
    """Launch a lightweight HTTP server to serve the metrics endpoint."""
    ekclient = easykube.Configuration.from_environment().async_client()

    app = web.Application()
    app.add_routes([web.get("/metrics", functools.partial(metrics_handler, ekclient))])

    runner = web.AppRunner(app, handle_signals=False)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", "8080", shutdown_timeout=1.0)
    await site.start()

    # Sleep until we need to clean up
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.shield(runner.cleanup())
