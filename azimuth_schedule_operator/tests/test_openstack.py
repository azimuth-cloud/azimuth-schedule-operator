import unittest

import httpx

from azimuth_schedule_operator import openstack


class TestAuth(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_uses_request_timeout(self):
        client_timeout = httpx.Timeout(10, connect=2, read=3, write=4, pool=5)
        overrides = [
            {},
            {"timeout": httpx.Timeout(20, connect=6, read=7, write=8, pool=9)},
            {"timeout": None},
        ]
        for override in overrides:
            with self.subTest(override=override):
                requests = []

                async def handle(request):
                    requests.append(request)
                    if request.url.path == "/v3/auth/tokens":
                        return httpx.Response(
                            201,
                            headers={"X-Subject-Token": "fake-token"},
                            json={"token": {"user": {"id": "fake-user"}}},
                        )
                    return httpx.Response(200, json={"catalog": []})

                auth = openstack.Auth(
                    "https://openstack.invalid/v3", "fake-id", "fake-secret"
                )
                async with httpx.AsyncClient(
                    auth=auth,
                    timeout=client_timeout,
                    transport=httpx.MockTransport(handle),
                ) as client:
                    await client.get(
                        "https://openstack.invalid/v3/auth/catalog", **override
                    )

                self.assertEqual(
                    [(request.method, request.url.path) for request in requests],
                    [("POST", "/v3/auth/tokens"), ("GET", "/v3/auth/catalog")],
                )
                expected = httpx.Timeout(
                    override.get("timeout", client_timeout)
                ).as_dict()
                for request in requests:
                    self.assertEqual(request.extensions.get("timeout"), expected)
                self.assertEqual(requests[1].headers["X-Auth-Token"], "fake-token")
