import collections
from unittest import mock

import easykube
import httpx


class LeaseStatusMatcher:
    def __init__(self, phase, size_map=None, size_name_map=None, error_message=None):
        self.phase = phase
        self.size_map = size_map
        self.size_name_map = size_name_map
        self.error_message = error_message

    def __eq__(self, data):
        if not isinstance(data, dict):
            return False
        if "status" not in data:
            return False
        status = data["status"]
        if "phase" not in status or self.phase != status["phase"]:
            return False
        if self.size_map is not None:
            if "sizeMap" not in status or self.size_map != status["sizeMap"]:
                return False
        if self.size_name_map is not None:
            if (
                "sizeNameMap" not in status
                or self.size_name_map != status["sizeNameMap"]
            ):
                return False
        if self.error_message is not None:
            if (
                "errorMessage" not in status
                or self.error_message != status["errorMessage"]
            ):
                return False
        return True

    def __repr__(self):
        props = [f"phase={repr(self.phase)}"]
        if self.size_map is not None:
            props.append(f"size_map={repr(self.size_map)}")
        if self.size_name_map is not None:
            props.append(f"size_name_map={repr(self.size_name_map)}")
        return f"LeaseStatusMatcher<{', '.join(props)}>"


async def empty_async_iterator():
    if False:
        yield 1


async def as_async_iterable(sync_iterable):
    for elem in sync_iterable:
        yield elem


def side_effect_with_return_value(mock_, default_side_effect):
    # This function allows the default side effect to be overridden by setting a
    # return value (setting side_effect continues to work as normal)
    def side_effect(*args, **kwargs):
        ret = mock_._mock_return_value
        if mock_._mock_delegate is not None:
            ret = mock_._mock_delegate.return_value
        if ret is not mock.DEFAULT:
            return ret
        else:
            return default_side_effect(*args, **kwargs)

    mock_.side_effect = side_effect


def mock_k8s_client():
    client = mock.AsyncMock()
    client.__aenter__.return_value = client

    apis = client.apis = collections.defaultdict(mock_k8s_api)
    client.api = api_method = mock.Mock()
    side_effect_with_return_value(api_method, lambda api, *_, **__: apis[api])

    return client


def mock_k8s_api():
    api = mock.AsyncMock()
    resources = api.resources = collections.defaultdict(mock_k8s_resource)
    side_effect_with_return_value(api.resource, lambda name, *_, **__: resources[name])
    return api


def mock_k8s_resource():
    resource = mock.AsyncMock()

    # list is a synchronous method that returns an async iterator
    resource.list = list_method = mock.Mock()
    side_effect_with_return_value(list_method, empty_async_iterator)

    return resource


def k8s_api_error(status_code, message=None, text=None, json=None):
    return easykube.ApiError(httpx_status_error(status_code, message, text, json))


def httpx_status_error(status_code, message=None, text=None, json=None):
    request = httpx.Request("GET", "fakeurl")
    response = httpx.Response(status_code, text=text, json=json)
    message = message or f"error with status {status_code}"
    return httpx.HTTPStatusError(message, request=request, response=response)


def mock_openstack_cloud():
    cloud = mock.AsyncMock()
    cloud.__aenter__.return_value = cloud

    clients = cloud.clients = collections.defaultdict(mock_openstack_client)
    cloud.api_client = api_client_method = mock.Mock()
    side_effect_with_return_value(api_client_method, lambda api, *_, **__: clients[api])

    return cloud


def mock_openstack_client():
    client = mock.AsyncMock()

    resources = client.resources = collections.defaultdict(mock_openstack_resource)
    client.resource = resource_method = mock.Mock()
    side_effect_with_return_value(
        resource_method, lambda name, *_, **__: resources[name]
    )

    return client


def mock_openstack_resource():
    resource = mock.AsyncMock()

    # list is a synchronous method that returns an async iterator
    resource.list = list_method = mock.Mock()
    side_effect_with_return_value(list_method, empty_async_iterator)

    return resource
