import asyncio
import base64
import contextlib
import urllib.parse

import httpx
import yaml
from easykube import rest


class UnsupportedAuthenticationError(Exception):
    """Raised when an unsupported authentication method is used."""

    def __init__(self, auth_type):
        super().__init__(f"unsupported authentication type: {auth_type}")


class ApiNotSupportedError(Exception):
    """Raised when the requested API is not supported."""

    def __init__(self, api):
        super().__init__(f"api '{api}' is not supported")


class Auth(httpx.Auth):
    """Authenticator class for OpenStack connections."""

    def __init__(
        self, auth_url, application_credential_id, application_credential_secret
    ):
        self.url = auth_url.rstrip("/").removesuffix("/v3")
        self._application_credential_id = application_credential_id
        self._application_credential_secret = application_credential_secret
        self._token = None
        self._user_id = None
        self._lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def _refresh_token(self):
        """
        Context manager to ensure only one request at a time triggers a token refresh.
        """
        token = self._token
        async with self._lock:
            # Only yield to the wrapped block if the token has not changed
            # in the time it took to acquire the lock
            if token == self._token:
                yield

    def _build_token_request(self):
        return httpx.Request(
            "POST",
            f"{self.url}/v3/auth/tokens",
            json={
                "auth": {
                    "identity": {
                        "methods": ["application_credential"],
                        "application_credential": {
                            "id": self._application_credential_id,
                            "secret": self._application_credential_secret,
                        },
                    },
                },
            },
        )

    def _handle_token_response(self, response):
        response.raise_for_status()
        self._token = response.headers["X-Subject-Token"]
        self._user_id = response.json()["token"]["user"]["id"]

    async def async_auth_flow(self, request):
        if self._token is None:
            async with self._refresh_token():
                response = yield self._build_token_request()
                await response.aread()
                self._handle_token_response(response)
        request.headers["X-Auth-Token"] = self._token
        # TODO(johngarbutt): this is needed for blazar
        request.headers["Content-Type"] = "application/json"
        # TODO(johngarbutt): this is needed for nova flavor extra spec info
        request.headers["X-OpenStack-Nova-API-Version"] = "2.61"
        response = yield request


class Resource(rest.Resource):
    """Base resource for OpenStack APIs."""

    def __init__(self, client, name, prefix=None, plural_name=None, singular_name=None):
        super().__init__(client, name, prefix)
        # Some resources support a /detail endpoint
        # In this case, we just want to use the name up to the slash as the plural name
        self._plural_name = plural_name or self._name.split("/")[0]
        # If no singular name is given, assume the name ends in 's'
        self._singular_name = singular_name or self._plural_name[:-1]

    @property
    def singular_name(self):
        return self._singular_name

    def _extract_list(self, response):
        # Some resources support a /detail endpoint
        # In this case, we just want to use the name up to the slash
        return response.json()[self._plural_name]

    def _extract_next_page(self, response):
        next_url = next(
            (
                link["href"]
                for link in response.json().get(f"{self._plural_name}_links", [])
                if link["rel"] == "next"
            ),
            None,
        )
        # Sometimes, the returned URLs have http where they should have https
        # To mitigate this, we split the URL and return the path and params separately
        url = urllib.parse.urlsplit(next_url)
        params = urllib.parse.parse_qs(url.query)
        return url.path, params

    def _extract_one(self, response):
        content_type = response.headers.get("content-type")
        if content_type == "application/json":
            return response.json()[self._singular_name]
        else:
            return super()._extract_one(response)


class Client(rest.AsyncClient):
    """Client for OpenStack APIs."""

    def __init__(self, /, base_url, prefix=None, **kwargs):
        # Extract the path part of the base_url
        url = urllib.parse.urlsplit(base_url)
        # Initialise the client with the scheme/host
        super().__init__(base_url=f"{url.scheme}://{url.netloc}", **kwargs)
        # Add the path to the given prefix to use as the full prefix
        # Not having this on the base URL ensures pagination works without
        # duplicating the prefix
        self._prefix = "/".join([url.path.rstrip("/"), (prefix or "").lstrip("/")])

    def __aenter__(self):
        # Prevent individual clients from being used in a context manager
        raise RuntimeError("clients must be used via a cloud object")

    def resource(self, name, prefix=None, plural_name=None, singular_name=None):
        # If an additional prefix is given, combine it with the existing prefix
        if prefix:
            prefix = "/".join([self._prefix.rstrip("/"), prefix.lstrip("/")])
        else:
            prefix = self._prefix
        return Resource(self, name, prefix, plural_name, singular_name)


class Cloud:
    """Object for interacting with OpenStack clouds."""

    def __init__(self, auth, transport, interface, region):
        self._auth = auth
        self._transport = transport
        self._interface = interface
        self._region = region
        self._endpoints = {}
        # A map of api name to client
        self._clients = {}

    async def __aenter__(self):
        await self._transport.__aenter__()
        # Once the transport has been initialised, we can initialise the endpoints
        client = Client(
            base_url=self._auth.url, auth=self._auth, transport=self._transport
        )
        # We have to slightly artificially create the catalog URL as we don't
        # benefit from the prefix handling that the resources use
        catalog_url = self._auth.url.rstrip("/") + "/v3/auth/catalog"
        try:
            response = await client.get(catalog_url)
        except httpx.HTTPStatusError as exc:
            # If the auth fails, we just have an empty app catalog
            if exc.response.status_code == 404:
                return self
            else:
                raise
        self._endpoints = {
            entry["type"]: next(
                ep["url"]
                for ep in entry["endpoints"]
                if (
                    ep["interface"] == self._interface
                    and (not self._region or ep["region"] == self._region)
                )
            )
            for entry in response.json()["catalog"]
            if len(entry["endpoints"]) > 0
        }
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self._transport.__aexit__(exc_type, exc_value, traceback)

    @property
    def is_authenticated(self):
        """True if the cloud is authenticated, False otherwise."""
        return bool(self._endpoints)

    @property
    def application_credential_id(self):
        """The ID of the application credential used to authenticate."""
        return self._auth._application_credential_id

    @property
    def current_user_id(self):
        """The ID of the current user."""
        return self._auth._user_id

    def api_client(self, name, prefix=None, **kwargs):
        """Returns a client for the named API."""
        if name not in self._clients:
            if name not in self._endpoints:
                raise ApiNotSupportedError(name)
            self._clients[name] = Client(
                base_url=self._endpoints[name],
                prefix=prefix,
                auth=self._auth,
                transport=self._transport,
                **kwargs,
            )
        return self._clients[name]


def from_clouds(clouds, cloud, cacert):
    """Returns an OpenStack cloud object from the content of a clouds file."""
    config = clouds["clouds"][cloud]
    if config["auth_type"] != "v3applicationcredential":
        raise UnsupportedAuthenticationError(config["auth_type"])
    auth = Auth(
        config["auth"]["auth_url"],
        config["auth"]["application_credential_id"],
        config["auth"]["application_credential_secret"],
    )
    # Create a default context using the verification from the config
    context = httpx.create_ssl_context(verify=config.get("verify", True))
    # If a cacert was given, load it into the context
    if cacert is not None:
        context.load_verify_locations(cadata=cacert)
    transport = httpx.AsyncHTTPTransport(verify=context)
    return Cloud(
        auth, transport, config.get("interface", "public"), config.get("region_name")
    )


def from_secret_data(secret_data):
    """Returns an OpenStack cloud object from the given secret data."""
    clouds = yaml.safe_load(base64.b64decode(secret_data["clouds.yaml"]))
    if "cacert" in secret_data:
        cacert = base64.b64decode(secret_data["cacert"]).decode()
    else:
        cacert = None
    return from_clouds(clouds, next(c for c in clouds["clouds"]), cacert)
