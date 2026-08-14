"""HTTP client for the node agent (M04).

The control plane's only route to a managed host. §M04 forbids exposing the Docker
socket directly to the central platform, so every host operation — container control,
GPU telemetry, system inventory — travels over this authenticated API instead.

Transport security scales with deployment:

* **Local Compose** — HTTP over the private `ai-platform` bridge, which publishes no
  host port. Bearer token only.
* **Remote node** — HTTPS with the platform's own CA (`scripts/gen_certs.sh`), since
  an air-gapped site has no reachable public CA. Optionally mutual TLS, so a stolen
  agent token alone is not enough.

Every call is bounded by a timeout. A node whose driver has wedged will accept a TCP
connection and never answer; without a deadline the metrics worker would block on it
and stop collecting from every *other* node in the fleet.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.logging import get_logger

log = get_logger(__name__)

# Deliberately different budgets. Health must fail fast so a dead node is detected
# promptly; GPU collection runs nvidia-smi or dcgmi on the host and legitimately takes
# longer; container creation can involve large image layers.
DEFAULT_TIMEOUT = 15.0
HEALTH_TIMEOUT = 5.0
CONTROL_TIMEOUT = 120.0


class NodeAgentError(RuntimeError):
    """A node agent call failed."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NodeAgentUnreachableError(NodeAgentError):
    """Connection failed or timed out — the node is down or unreachable."""


class NodeAgentAuthError(NodeAgentError):
    """The agent rejected the token, or TLS verification failed."""


class NodeAgentRefusedError(NodeAgentError):
    """The agent refused on policy grounds — typically the managed-label guard."""


@dataclass(frozen=True, slots=True)
class NodeAgentTarget:
    """Everything needed to reach one agent."""

    base_url: str
    token: str
    verify_tls: bool = True
    # Platform CA bundle; an air-gapped site's certificates chain to its own root.
    ca_cert_path: str | None = None
    # Client certificate for mutual TLS, as (certfile, keyfile).
    client_cert: tuple[str, str] | None = None


class NodeAgentClient:
    """Calls one node agent.

    Constructed per operation rather than held long-term: node credentials and URLs
    change through the API, and a cached client would keep using stale ones.
    """

    def __init__(self, target: NodeAgentTarget) -> None:
        self._target = target

    def _verify(self) -> Any:
        if not self._target.verify_tls:
            return False
        return self._target.ca_cert_path or True

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._target.base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            headers={"Authorization": f"Bearer {self._target.token}"},
            verify=self._verify(),
            cert=self._target.client_cert,
            follow_redirects=False,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        # Named for what it is: the deadline handed to httpx for this call, not an
        # asyncio timeout wrapping the coroutine.
        request_timeout: float = DEFAULT_TIMEOUT,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            async with self._client(request_timeout) as client:
                response = await client.request(method, path, json=json, params=params)
        except httpx.ConnectError as exc:
            # httpx wraps a certificate failure inside ConnectError, so the cause chain
            # is the only way to tell "the node is down" from "the node's certificate
            # does not verify". Those send an operator to entirely different places.
            if _is_tls_failure(exc):
                raise NodeAgentAuthError(
                    f"TLS verification failed for {self._target.base_url}: {exc}. "
                    "Check that the agent's certificate chains to the CA configured "
                    "for this node and that its SAN covers the hostname used here."
                ) from exc
            raise NodeAgentUnreachableError(
                f"Cannot reach node agent at {self._target.base_url}: {type(exc).__name__}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise NodeAgentUnreachableError(
                f"Node agent at {self._target.base_url} did not respond within {request_timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise NodeAgentError(f"Node agent request failed: {type(exc).__name__}") from exc

        if response.status_code == 401:
            raise NodeAgentAuthError(
                "The node agent rejected the platform's token. Re-register the node "
                "with the token configured as NODE_AGENT_AUTH_TOKEN on that host.",
                status_code=401,
            )
        if response.status_code == 403:
            raise NodeAgentRefusedError(_detail(response), status_code=403)
        if response.status_code == 404:
            raise NodeAgentError(_detail(response) or "Not found on node.", status_code=404)
        if response.status_code >= 400:
            raise NodeAgentError(
                _detail(response) or f"Node agent returned {response.status_code}.",
                status_code=response.status_code,
            )

        if response.status_code == 204 or not response.content:
            return None
        parsed: Any = response.json()
        return parsed

    async def _request_dict(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """A request whose response must be a JSON object.

        The cast is honest about where the assumption lives — the agent's wire contract
        (`node-agent/app/schemas.py`) guarantees an object for these endpoints. Raising
        here rather than letting a list or a string flow onward means a protocol
        mismatch after an agent upgrade surfaces at the boundary, not three layers deep.
        """
        result = await self._request(method, path, **kwargs)
        if result is not None and not isinstance(result, dict):
            raise NodeAgentError(
                f"Node agent returned {type(result).__name__} for {path}, expected an object. "
                "This usually means the agent and control plane are on incompatible versions."
            )
        return result or {}

    # -- reads -------------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        """Agent health. Unauthenticated on the agent side, but the token is still
        sent so a misconfigured token surfaces during registration rather than later."""
        return await self._request_dict("GET", "/health", request_timeout=HEALTH_TIMEOUT)

    async def system(self) -> dict[str, Any]:
        return await self._request_dict("GET", "/system")

    async def disk(self) -> dict[str, Any]:
        return await self._request_dict("GET", "/disk")

    async def network(self) -> dict[str, Any]:
        return await self._request_dict("GET", "/network")

    async def gpus(self) -> dict[str, Any]:
        """Inventory, telemetry and occupancy in one call.

        Longer timeout than a plain read: this runs nvidia-smi or dcgmi on the host.
        """
        return await self._request_dict("GET", "/gpus", request_timeout=30.0)

    async def docker(self) -> dict[str, Any]:
        return await self._request_dict("GET", "/docker")

    async def containers(self, *, managed_only: bool = False) -> list[dict[str, Any]]:
        result = await self._request(
            "GET", "/containers", params={"managed_only": str(managed_only).lower()}
        )
        if result is None:
            return []
        if not isinstance(result, list):
            raise NodeAgentError("Node agent returned a non-list container inventory.")
        return result

    async def container(self, container_id: str) -> dict[str, Any]:
        return await self._request_dict("GET", f"/containers/{container_id}")

    async def container_logs(self, container_id: str, *, tail: int = 200) -> dict[str, Any]:
        return await self._request_dict(
            "GET", f"/containers/{container_id}/logs", params={"tail": tail}, request_timeout=30.0
        )

    async def container_stats(self, container_id: str) -> dict[str, Any]:
        return await self._request_dict("GET", f"/containers/{container_id}/stats")

    # -- writes ------------------------------------------------------------
    async def create_container(self, spec: dict[str, Any]) -> dict[str, Any]:
        return await self._request_dict(
            "POST", "/containers/create", json=spec, request_timeout=CONTROL_TIMEOUT
        )

    async def start_container(self, container_id: str) -> dict[str, Any]:
        return await self._request_dict(
            "POST", f"/containers/{container_id}/start", request_timeout=CONTROL_TIMEOUT
        )

    async def stop_container(
        self, container_id: str, *, timeout_seconds: int = 30
    ) -> dict[str, Any]:
        return await self._request_dict(
            "POST",
            f"/containers/{container_id}/stop",
            params={"timeout_seconds": timeout_seconds},
            # The agent waits up to `timeout_seconds` for a graceful stop, so the HTTP
            # deadline must exceed it or the platform gives up on its own request.
            request_timeout=CONTROL_TIMEOUT + timeout_seconds,
        )

    async def restart_container(
        self, container_id: str, *, timeout_seconds: int = 30
    ) -> dict[str, Any]:
        return await self._request_dict(
            "POST",
            f"/containers/{container_id}/restart",
            params={"timeout_seconds": timeout_seconds},
            request_timeout=CONTROL_TIMEOUT + timeout_seconds,
        )

    async def remove_container(self, container_id: str, *, force: bool = False) -> None:
        await self._request(
            "DELETE",
            f"/containers/{container_id}",
            params={"force": str(force).lower()},
            request_timeout=CONTROL_TIMEOUT,
        )


def _detail(response: httpx.Response) -> str:
    """Pull a message out of an agent error response, whatever shape it took."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail[:300]
        if isinstance(body.get("error"), dict):
            return str(body["error"].get("message", ""))[:300]
    return str(body)[:300]


def _is_tls_failure(exc: BaseException) -> bool:
    """Whether a ConnectError was really a certificate problem.

    Walks the cause chain because httpx reports a TLS handshake failure as a plain
    ConnectError with the ``ssl.SSLError`` underneath.
    """
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 5:
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return False
