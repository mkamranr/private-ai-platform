"""E76: inference backends report their load, and Ollama loads and unloads models."""

# ruff: noqa: F811  (pytest fixtures imported from the E1 tests)
from __future__ import annotations

import datetime as dt
import json
import uuid

import httpx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import _backend_service
from app.models.audit import AuditLog
from app.models.operations import BackendModelPull, PullStatus
from app.services.inference_backends import parse_prometheus
from tests.api.test_backends import (  # noqa: F401  (fixtures)
    _create,
    backend_admin,
    backend_viewer,
    fake_backend,
)
from tests.conftest import auth_header

VLLM_METRICS = """# HELP vllm:num_requests_running Number of requests running.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="qwen"} 3.0
vllm:num_requests_waiting{model_name="qwen"} 1.0
vllm:gpu_cache_usage_perc{model_name="qwen"} 0.42
vllm:prompt_tokens_total{model_name="qwen"} 1200.0
vllm:generation_tokens_total{model_name="qwen"} 800.0
python_gc_objects_collected_total{generation="0"} 99.0
"""


def vllm(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/metrics":
        return httpx.Response(200, text=VLLM_METRICS)
    if request.url.path == "/v1/models":
        return httpx.Response(200, json={"data": [{"id": "qwen"}]})
    return httpx.Response(404)


class TestParse:
    def test_vllm_series(self) -> None:
        totals, per_model = parse_prometheus(VLLM_METRICS)
        assert totals == {
            "requests_running": 3.0,
            "requests_waiting": 1.0,
            "kv_cache_usage_percent": 42.0,
            "prompt_tokens_total": 1200.0,
            "generation_tokens_total": 800.0,
        }
        assert per_model["qwen"]["requests_running"] == 3.0


class TestMetrics:
    async def test_vllm(self, client: AsyncClient, tokens, backend_admin, fake_backend) -> None:
        headers = auth_header(tokens, backend_admin)
        created = await _create(
            client, headers, backend_type="vllm", base_url="http://vllm-host:8000"
        )
        assert "metrics" in created["capabilities"]
        fake_backend["vllm-host"] = vllm
        response = await client.get(f"/api/v1/backends/{created['id']}/metrics", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["values"]["kv_cache_usage_percent"] == 42.0
        assert body["models"] == [
            {
                "name": "qwen",
                "requests_running": 3.0,
                "requests_waiting": 1.0,
                "kv_cache_usage_percent": 42.0,
                "prompt_tokens_total": 1200.0,
                "generation_tokens_total": 800.0,
            }
        ]

    async def test_ollama_models_in_memory(
        self, client: AsyncClient, tokens, backend_viewer, backend_admin, fake_backend
    ) -> None:
        created = await _create(client, auth_header(tokens, backend_admin))

        def ollama(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/ps"
            return httpx.Response(
                200,
                json={"models": [{"name": "llama3.2:latest", "size": 10, "size_vram": 8}]},
            )

        fake_backend["ollama-host"] = ollama
        response = await client.get(
            f"/api/v1/backends/{created['id']}/metrics", headers=auth_header(tokens, backend_viewer)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["values"] == {"models_loaded": 1.0, "vram_bytes": 8.0}
        assert body["models"][0]["name"] == "llama3.2:latest"

    async def test_unsupported_or_unreachable(
        self, client: AsyncClient, tokens, backend_admin, fake_backend
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        plain = await _create(
            client, headers, backend_type="openai_compatible", base_url="http://plain-host:8000"
        )
        response = await client.get(f"/api/v1/backends/{plain['id']}/metrics", headers=headers)
        assert response.status_code == 422 and "reports no metrics" in response.text
        down = await _create(client, headers)
        response = await client.get(f"/api/v1/backends/{down['id']}/metrics", headers=headers)
        assert response.status_code == 422 and "Could not read metrics" in response.text


class TestLoadUnload:
    async def test_ollama(
        self, client: AsyncClient, tokens, backend_admin, fake_backend, session: AsyncSession
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        created = await _create(client, headers)
        sent: list[dict] = []

        def ollama(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/generate"
            body = json.loads(request.content)
            sent.append(body)
            if body["model"] == "missing":
                return httpx.Response(404, json={"error": "model not found"})
            return httpx.Response(200, json={"done": True})

        fake_backend["ollama-host"] = ollama
        url = f"/api/v1/backends/{created['id']}/models"
        response = await client.post(f"{url}/load", json={"model": "llama3.2"}, headers=headers)
        assert response.status_code == 200, response.text
        response = await client.post(f"{url}/unload", json={"model": "llama3.2"}, headers=headers)
        assert response.status_code == 200, response.text
        assert sent == [
            {"model": "llama3.2", "keep_alive": -1},
            {"model": "llama3.2", "keep_alive": 0},
        ]
        response = await client.post(f"{url}/load", json={"model": "missing"}, headers=headers)
        assert response.status_code == 422 and "pull it there first" in response.text

        actions = (
            (
                await session.execute(
                    select(AuditLog.action).where(AuditLog.resource_id == created["id"])
                )
            )
            .scalars()
            .all()
        )
        assert {"BACKEND_MODEL_LOADED", "BACKEND_MODEL_UNLOADED"} <= set(actions)

    async def test_vllm_cannot_and_viewers_may_not(
        self, client: AsyncClient, tokens, backend_admin, backend_viewer, fake_backend
    ) -> None:
        created = await _create(
            client,
            auth_header(tokens, backend_admin),
            backend_type="vllm",
            base_url="http://vllm-host:8000",
        )
        url = f"/api/v1/backends/{created['id']}/models/load"
        response = await client.post(
            url, json={"model": "qwen"}, headers=auth_header(tokens, backend_admin)
        )
        assert response.status_code == 422 and "can't load models" in response.text
        response = await client.post(
            url, json={"model": "qwen"}, headers=auth_header(tokens, backend_viewer)
        )
        assert response.status_code == 403


class TestPull:
    """E90: fetching a model onto an Ollama backend. E97: queued, run by the worker,
    with its progress."""

    async def test_queued_once(
        self,
        client: AsyncClient,
        tokens,
        backend_admin,
        backend_viewer,
        fake_backend,
        session: AsyncSession,
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        created = await _create(client, headers)
        url = f"/api/v1/backends/{created['id']}/models/pull"
        body = {"model": "registry.internal/library/llama3.2:3b", "insecure": True}
        response = await client.post(url, json=body, headers=headers)
        assert response.status_code == 202, response.text
        pull = response.json()
        assert (pull["status"], pull["model"], pull["insecure"]) == (
            "QUEUED",
            "registry.internal/library/llama3.2:3b",
            True,
        )
        again = await client.post(url, json=body, headers=headers)
        assert again.status_code == 202 and again.json()["id"] == pull["id"]  # not twice

        listed = await client.get(
            f"/api/v1/backends/{created['id']}/pulls", headers=auth_header(tokens, backend_viewer)
        )
        assert listed.status_code == 200 and [p["id"] for p in listed.json()] == [pull["id"]]

        response = await client.post(url, json={"model": "bad name; rm"}, headers=headers)
        assert response.status_code == 422
        response = await client.post(
            url, json={"model": "llama3.2"}, headers=auth_header(tokens, backend_viewer)
        )
        assert response.status_code == 403
        actions = (
            (
                await session.execute(
                    select(AuditLog.action).where(AuditLog.resource_id == created["id"])
                )
            )
            .scalars()
            .all()
        )
        assert actions.count("BACKEND_MODEL_PULLED") == 1

    async def test_vllm_cannot(
        self, client: AsyncClient, tokens, backend_admin, fake_backend
    ) -> None:
        created = await _create(
            client,
            auth_header(tokens, backend_admin),
            backend_type="vllm",
            base_url="http://vllm-host:8000",
        )
        response = await client.post(
            f"/api/v1/backends/{created['id']}/models/pull",
            json={"model": "qwen"},
            headers=auth_header(tokens, backend_admin),
        )
        assert response.status_code == 422 and "can't fetch models" in response.text

    async def _queued(self, client, tokens, backend_admin, model: str) -> dict:
        headers = auth_header(tokens, backend_admin)
        created = await _create(client, headers)
        response = await client.post(
            f"/api/v1/backends/{created['id']}/models/pull", json={"model": model}, headers=headers
        )
        assert response.status_code == 202, response.text
        return response.json()

    async def test_the_worker_records_progress(
        self, app, client: AsyncClient, tokens, backend_admin, fake_backend, session
    ) -> None:
        pull = await self._queued(client, tokens, backend_admin, "llama3.2:3b")
        sent: list[dict] = []

        def ollama(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/pull"
            sent.append(json.loads(request.content))
            lines = [
                {"status": "pulling manifest"},
                {"status": "pulling 6a0746a1ec1a", "total": 2000, "completed": 500},
                {"status": "pulling 6a0746a1ec1a", "total": 2000, "completed": 2000},
                {"status": "verifying sha256 digest"},
                {"status": "success"},
            ]
            return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines) + "\n")

        fake_backend["ollama-host"] = ollama
        service = _backend_service(session, app.state.secret_cipher)
        saved: list[tuple] = []

        async def save() -> None:
            saved.append((claimed.stage, claimed.completed_bytes))
            await session.flush()

        claimed = await service.claim_pull()
        assert claimed is not None and str(claimed.id) == pull["id"]
        assert claimed.status == PullStatus.RUNNING
        assert await service.claim_pull() is None  # running and beating: not claimed twice
        done = await service.run_pull(claimed, save, every_seconds=0)
        assert sent == [{"model": "llama3.2:3b", "stream": True, "insecure": False}]
        assert done.status == PullStatus.SUCCEEDED and done.finished_at is not None
        assert (done.completed_bytes, done.total_bytes, done.stage) == (2000, 2000, "success")
        assert ("pulling 6a0746a1ec1a", 500) in saved  # progress saved along the way

    async def test_a_failed_pull_says_why(
        self, app, client: AsyncClient, tokens, backend_admin, fake_backend, session
    ) -> None:
        await self._queued(client, tokens, backend_admin, "nowhere/x:1")
        service = _backend_service(session, app.state.secret_cipher)
        replies = iter(
            [
                httpx.Response(500, json={"error": "pull model manifest: file does not exist"}),
                httpx.Response(
                    200,
                    content='{"status": "pulling manifest"}\n{"error": "max retries exceeded"}\n',
                ),
                httpx.Response(200, content='{"status": "pulling manifest"}\n'),
            ]
        )
        fake_backend["ollama-host"] = lambda request: next(replies)
        for words in ("file does not exist", "max retries exceeded", "before it finished"):
            claimed = await service.claim_pull()
            assert claimed is not None
            done = await service.run_pull(claimed, session.flush)
            assert done.status == PullStatus.FAILED and words in (done.error or ""), done.error
            # Queued again to try the next reply.
            done.status = PullStatus.QUEUED
            await session.flush()

    async def test_a_stalled_pull_is_taken_up_again(
        self, app, client: AsyncClient, tokens, backend_admin, fake_backend, session
    ) -> None:
        await self._queued(client, tokens, backend_admin, "llama3.2:3b")
        service = _backend_service(session, app.state.secret_cipher)
        claimed = await service.claim_pull()
        assert claimed is not None
        claimed.heartbeat_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
        await session.flush()
        again = await service.claim_pull()
        assert again is not None and again.id == claimed.id

    async def test_old_finished_pulls_are_pruned(
        self, app, client: AsyncClient, tokens, backend_admin, fake_backend, session
    ) -> None:
        pull = await self._queued(client, tokens, backend_admin, "llama3.2:3b")
        service = _backend_service(session, app.state.secret_cipher)
        row = await session.get(BackendModelPull, uuid.UUID(pull["id"]))
        assert row is not None
        assert await service.prune_pulls() == 0  # queued: kept
        row.status = PullStatus.SUCCEEDED
        row.finished_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=8)
        await session.flush()
        assert await service.prune_pulls() == 1
