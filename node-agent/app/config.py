"""Node agent configuration (M02 conventions, M04 service).

Deliberately independent of the control plane's settings module. The agent is a
separate deployable that runs on every managed host and is upgraded on its own
cadence; sharing a configuration model would couple those lifecycles for no gain.

All values come from environment variables prefixed ``NODE_AGENT_``.
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GpuProbeKind = Literal["nvidia_smi", "dcgm", "fake", "auto"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NODE_AGENT_",
        env_file=(".env",),
        extra="ignore",
        case_sensitive=False,
    )

    # --- identity ---
    node_name: str = Field(
        default="local",
        description="Stable name this agent reports. Must match the node registered "
        "in the control plane.",
    )

    # --- server ---
    host: str = "0.0.0.0"
    port: int = 9100
    log_level: str = "INFO"
    log_json: bool = True

    # --- authentication ---
    # Required. The agent can create containers and read host telemetry; an
    # unauthenticated agent on any reachable network is a remote-execution primitive.
    auth_token: SecretStr

    # --- TLS (§M04: the control plane talks to agents over HTTPS) ---
    # Left empty for local Compose, where the agent is reachable only from the
    # internal bridge network and never published to the host. A remote node must
    # set all three; see docs/security.md.
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    tls_ca_certs: str | None = None
    tls_require_client_cert: bool = True

    # --- Docker ---
    docker_socket: str = "unix:///var/run/docker.sock"
    docker_timeout_seconds: int = 60

    # Containers the platform created carry this label. Control operations are
    # refused on anything without it — see runtime/docker.py. Without that guard
    # the agent could stop the very Postgres holding the platform's own state.
    managed_label: str = "ai-platform.managed"
    # Escape hatch for an operator adopting pre-existing containers. Off by default.
    allow_unmanaged_control: bool = False

    # --- GPU ---
    # `auto` picks dcgm -> nvidia_smi -> fake, in that order, by what actually works
    # on this host.
    gpu_probe: GpuProbeKind = "auto"
    gpu_probe_timeout_seconds: float = 10.0

    # FakeGpuProbe shape. This is what makes the platform developable and
    # end-to-end testable on a machine with no NVIDIA hardware.
    fake_device_count: int = 4
    fake_device_model: str = "NVIDIA A100-SXM4-80GB"
    fake_memory_total_mib: int = 81920
    fake_driver_version: str = "550.90.07"
    fake_cuda_version: str = "12.4"

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        upper = v.upper()
        if upper not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError(f"Invalid log level: {v!r}")
        return upper

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_certfile and self.tls_keyfile)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    # auth_token has no default and is supplied by the environment.
    return Settings()
