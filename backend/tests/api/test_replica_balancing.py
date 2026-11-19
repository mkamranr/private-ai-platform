"""Spreading calls across a model's running copies (E58).

The balancer is tested against an in-memory stand-in for Valkey (and one that is down);
the gateway against a model with three running copies, each answered by its own fake
runtime, so which copy served a call is visible.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DependencyUnavailableError
from app.models.models_registry import DeploymentState, Model, ModelDeployment, ModelStatus
from app.services.gateway import GatewayContext, GatewayService, StreamAccounting
from app.services.gateway_policy import BREAKERS
from app.services.llm_provider import ProviderError
from app.services.replica_balancer import ReplicaBalancer
from tests.api.conftest import FakeProvider

MESSAGES = [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# A Valkey stand-in: the handful of commands the balancer uses
# ---------------------------------------------------------------------------
class MemoryRedis:
    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.counters: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> _Pipe:
        return _Pipe(self)

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def zrem(self, key: str, member: str) -> None:
        self.zsets.get(key, {}).pop(member, None)

    def in_flight(self, deployment_id: uuid.UUID) -> int:
        return len(self.zsets.get(f"gw:inflight:{deployment_id}", {}))


class _Pipe:
    def __init__(self, redis: MemoryRedis) -> None:
        self.redis, self.ops = redis, []

    async def __aenter__(self) -> _Pipe:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def zremrangebyscore(self, key: str, low: Any, high: float) -> None:
        def run() -> None:
            members = self.redis.zsets.setdefault(key, {})
            for member, score in list(members.items()):
                if score <= high:
                    del members[member]

        self.ops.append(run)

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.ops.append(lambda: self.redis.zsets.setdefault(key, {}).update(mapping))

    def expire(self, key: str, seconds: int) -> None:
        self.ops.append(lambda: None)

    def zcount(self, key: str, low: float, high: Any) -> None:
        self.ops.append(lambda: sum(1 for s in self.redis.zsets.get(key, {}).values() if s >= low))

    async def execute(self) -> list[Any]:
        return [op() for op in self.ops]


class DownRedis:
    def pipeline(self, transaction: bool = True) -> Any:
        raise ConnectionError("valkey down")

    async def incr(self, key: str) -> int:
        raise ConnectionError("valkey down")

    async def zrem(self, key: str, member: str) -> None:
        raise ConnectionError("valkey down")


class _Dep:
    def __init__(self) -> None:
        self.id = uuid.uuid4()


# ---------------------------------------------------------------------------
# The balancer
# ---------------------------------------------------------------------------
class TestBalancer:
    async def test_equally_busy_copies_take_turns(self) -> None:
        balancer = ReplicaBalancer(MemoryRedis(), 600, "least_busy")
        copies = [_Dep(), _Dep(), _Dep()]
        model = uuid.uuid4()
        firsts = Counter([(await balancer.order(model, copies))[0].id for _ in range(9)])
        assert sorted(firsts.values()) == [3, 3, 3]

    async def test_least_busy_wins(self) -> None:
        redis = MemoryRedis()
        balancer = ReplicaBalancer(redis, 600, "least_busy")
        a, b, c = _Dep(), _Dep(), _Dep()
        for _ in range(3):
            await balancer.acquire(a.id)
        await balancer.acquire(b.id)
        for _ in range(5):
            assert (await balancer.order(uuid.uuid4(), [a, b, c]))[0] is c
        ordered = await balancer.order(uuid.uuid4(), [a, b, c])
        assert [d.id for d in ordered] == [c.id, b.id, a.id]

    async def test_release_frees_the_copy(self) -> None:
        redis = MemoryRedis()
        balancer = ReplicaBalancer(redis, 600, "least_busy")
        a = _Dep()
        lease = await balancer.acquire(a.id)
        assert redis.in_flight(a.id) == 1
        await balancer.release(lease)
        assert redis.in_flight(a.id) == 0

    async def test_stale_leases_lapse(self) -> None:
        """A control-plane process that died mid-call cannot keep a copy busy forever."""
        redis = MemoryRedis()
        balancer = ReplicaBalancer(redis, 60, "least_busy")
        a, b = _Dep(), _Dep()
        redis.zsets[f"gw:inflight:{a.id}"] = {"dead": time.time() - 3600}
        model = uuid.uuid4()
        firsts = Counter([(await balancer.order(model, [a, b]))[0].id for _ in range(4)])
        assert firsts[a.id] == 2  # counted as idle, so it takes its turns

    async def test_first_keeps_the_old_behaviour(self) -> None:
        balancer = ReplicaBalancer(MemoryRedis(), 600, "first")
        copies = [_Dep(), _Dep()]
        for _ in range(3):
            assert (await balancer.order(uuid.uuid4(), copies))[0] is copies[0]

    async def test_valkey_down_still_rotates_and_never_raises(self) -> None:
        balancer = ReplicaBalancer(DownRedis(), 600, "least_busy")
        copies = [_Dep(), _Dep()]
        firsts = {(await balancer.order(uuid.uuid4(), copies))[0].id for _ in range(4)}
        assert len(firsts) == 2
        lease = await balancer.acquire(copies[0].id)
        assert lease is None
        await balancer.release(lease)


# ---------------------------------------------------------------------------
# The gateway
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_breakers() -> Iterator[None]:
    BREAKERS.reset()
    yield
    BREAKERS.reset()


class CopyProvider(FakeProvider):
    """One running copy. Records each call; can fail."""

    def __init__(self, name: str, log: list[str], fail: bool = False) -> None:
        super().__init__(fail=fail, chunks=[f"from {name}"])
        self.name, self.log = name, log

    async def chat(self, messages, **kwargs):
        self.log.append(self.name)
        return await super().chat(messages, **kwargs)

    async def chat_stream(self, messages, **kwargs):
        self.log.append(self.name)
        async for chunk in super().chat_stream(messages, **kwargs):
            yield chunk


async def _model_with_copies(session: AsyncSession, n: int) -> tuple[Model, list[str]]:
    model = Model(
        name=f"replicated-{uuid.uuid4().hex[:8]}",
        display_name="Replicated",
        type="LLM",
        storage_path="/x",
        runtime="mock",
        status=ModelStatus.AVAILABLE,
    )
    session.add(model)
    await session.flush()
    urls = []
    for i in range(n):
        url = f"http://{model.name}-{i}:8000"
        urls.append(url)
        session.add(
            ModelDeployment(
                model_id=model.id,
                node_id=None,
                state=DeploymentState.RUNNING,
                runtime="mock",
                image="",
                internal_port=8000,
                internal_url=url,
                gpu_indices=[],
            )
        )
    await session.flush()
    return model, urls


@pytest.fixture
def gateway(app, session: AsyncSession, settings, monkeypatch):
    from app.repositories.models_registry import (
        ApiKeyRepository,
        ModelAliasRepository,
        ModelDeploymentRepository,
        ModelRepository,
        UsageRepository,
    )

    providers: dict[str, CopyProvider] = {}

    def build(strategy: str = "least_busy", retries: int = 1, redis: Any = None) -> GatewayService:
        local = settings.model_copy(deep=True)
        local.gateway.load_balancing = strategy
        local.gateway.replica_retries = retries
        service = GatewayService(
            local,
            ModelRepository(session),
            ModelAliasRepository(session),
            ModelDeploymentRepository(session),
            ApiKeyRepository(session),
            UsageRepository(session),
            redis or MemoryRedis(),
            app.state.database.sessionmaker,
        )

        async def no_record(*_: Any, **__: Any) -> None:
            return None

        service._record = no_record  # type: ignore[method-assign]
        return service

    monkeypatch.setattr(
        GatewayService, "_provider", lambda self, target: providers[target.internal_url]
    )
    return build, providers


class TestGateway:
    async def test_calls_are_spread_across_copies(self, gateway, session) -> None:
        build, providers = gateway
        model, urls = await _model_with_copies(session, 3)
        served: list[str] = []
        for i, url in enumerate(urls):
            providers[url] = CopyProvider(f"copy{i}", served)
        service = build()
        for _ in range(6):
            reply = await service.chat_completion(
                {"model": model.name, "messages": MESSAGES}, GatewayContext()
            )
            assert reply["model"] == model.name
        assert sorted(Counter(served).values()) == [2, 2, 2]

    async def test_a_failed_call_is_retried_on_another_copy(self, gateway, session) -> None:
        build, providers = gateway
        model, urls = await _model_with_copies(session, 2)
        served: list[str] = []
        providers[urls[0]] = CopyProvider("bad", served, fail=True)
        providers[urls[1]] = CopyProvider("good", served)
        service = build(strategy="first")
        # Whichever copy "first" picks, the call succeeds; when it is the bad one, the
        # good one answers.
        for _ in range(4):
            reply = await service.chat_completion(
                {"model": model.name, "messages": MESSAGES}, GatewayContext()
            )
            assert reply["choices"][0]["message"]["content"] == "from good"

    async def test_no_retries_means_the_failure_is_reported(self, gateway, session) -> None:
        build, providers = gateway
        model, urls = await _model_with_copies(session, 2)
        served: list[str] = []
        providers[urls[0]] = CopyProvider("bad0", served, fail=True)
        providers[urls[1]] = CopyProvider("bad1", served, fail=True)
        service = build(retries=1)
        with pytest.raises(DependencyUnavailableError):
            await service.chat_completion(
                {"model": model.name, "messages": MESSAGES}, GatewayContext()
            )
        assert sorted(served) == ["bad0", "bad1"]  # both tried, once each

        served.clear()
        with pytest.raises(DependencyUnavailableError):
            await build(retries=0).chat_completion(
                {"model": model.name, "messages": MESSAGES}, GatewayContext()
            )
        assert len(served) == 1

    async def test_a_stream_moves_before_it_has_sent_anything(self, gateway, session) -> None:
        build, providers = gateway
        model, urls = await _model_with_copies(session, 2)
        served: list[str] = []
        providers[urls[0]] = CopyProvider("bad", served, fail=True)
        providers[urls[1]] = CopyProvider("good", served)
        redis = MemoryRedis()  # shared, so the copies take turns being first
        for _ in range(2):  # so the bad copy is first at least once
            service = build(redis=redis)
            context = GatewayContext()
            body = {"model": model.name, "messages": MESSAGES, "stream": True}
            target = await service.prepare_stream(body, context)
            accounting = StreamAccounting()
            text = "".join([c async for c in service.stream_chunks(target, body, accounting)])
            assert "from good" in text and "upstream_error" not in text
            assert target.internal_url == urls[1]  # accounting sees the copy that served
            assert accounting.status_code != 502
            await service.finalise_stream(target, context, accounting)
        assert "bad" in served

    async def test_a_stream_that_already_sent_is_not_replayed(self, gateway, session) -> None:
        build, providers = gateway
        model, urls = await _model_with_copies(session, 2)
        served: list[str] = []

        class Breaks(CopyProvider):
            async def chat_stream(self, messages, **kwargs):
                from app.core.interfaces.llm import ChatCompletionChunk

                self.log.append(self.name)
                yield ChatCompletionChunk(id="s", model="m", delta="half an ans")
                raise ProviderError("connection reset")

        providers[urls[0]] = Breaks("breaks0", served)
        providers[urls[1]] = Breaks("breaks1", served)
        service = build()
        body = {"model": model.name, "messages": MESSAGES, "stream": True}
        target = await service.prepare_stream(body, GatewayContext())
        accounting = StreamAccounting()
        text = "".join([c async for c in service.stream_chunks(target, body, accounting)])
        assert text.count("half an ans") == 1 and "upstream_error" in text
        assert len(served) == 1
        assert accounting.status_code == 502

    async def test_stream_load_is_counted_until_finalised(self, gateway, session) -> None:
        build, providers = gateway
        model, urls = await _model_with_copies(session, 1)
        providers[urls[0]] = CopyProvider("only", [])
        redis = MemoryRedis()
        service = build(redis=redis)
        context = GatewayContext()
        body = {"model": model.name, "messages": MESSAGES, "stream": True}
        target = await service.prepare_stream(body, context)
        assert redis.in_flight(target.deployment.id) == 1
        accounting = StreamAccounting()
        [c async for c in service.stream_chunks(target, body, accounting)]
        await service.finalise_stream(target, context, accounting)
        assert redis.in_flight(target.deployment.id) == 0
