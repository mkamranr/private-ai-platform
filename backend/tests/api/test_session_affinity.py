"""Session affinity (E93): a conversation's calls go to one copy of a model, so the
copy's prefix cache holds its earlier turns."""

# ruff: noqa: F811  (pytest fixtures imported from the E58 tests)
from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from app.services.gateway import GatewayContext, session_key
from app.services.replica_balancer import ReplicaBalancer
from tests.api.test_replica_balancing import (  # noqa: F401  (fixtures)
    CopyProvider,
    MemoryRedis,
    _Dep,
    _model_with_copies,
    gateway,
)


def chat(system: str, user: str, *later: str) -> list[dict[str, Any]]:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for i, text in enumerate(later):
        messages.append({"role": "assistant" if i % 2 == 0 else "user", "content": text})
    return messages


class TestKey:
    def test_the_header_wins(self) -> None:
        body = {"model": "m", "messages": chat("s", "hi")}
        assert session_key(body, "abc") == "id:abc"
        assert len(session_key(body, "x" * 1000)) == len("id:") + 32  # hashed when long

    def test_later_turns_keep_the_key(self) -> None:
        first = session_key({"model": "m", "messages": chat("s", "hi")}, None)
        later = session_key({"model": "m", "messages": chat("s", "hi", "hello", "more")}, None)
        assert first == later and first.startswith("chat:")
        other = session_key({"model": "m", "messages": chat("s", "something else")}, None)
        assert other != first
        assert session_key({"model": "m2", "messages": chat("s", "hi")}, None) != first

    def test_no_user_message_no_key(self) -> None:
        assert session_key({"messages": [{"role": "system", "content": "s"}]}, None) is None
        assert session_key({"messages": "nonsense"}, None) is None


class TestBalancer:
    async def test_same_conversation_same_copy(self) -> None:
        balancer = ReplicaBalancer(MemoryRedis(), 600, "least_busy")
        copies = [_Dep(), _Dep(), _Dep()]
        model = uuid.uuid4()
        for key in ("chat:a", "chat:b", "id:c"):
            firsts = {(await balancer.order(model, copies, affinity=key))[0].id for _ in range(6)}
            assert len(firsts) == 1

    async def test_conversations_spread_by_share(self) -> None:
        balancer = ReplicaBalancer(MemoryRedis(), 600, "least_busy")
        a, b = _Dep(), _Dep()
        a.traffic_weight, b.traffic_weight = 90, 10
        model = uuid.uuid4()
        firsts = Counter(
            [(await balancer.order(model, [a, b], affinity=f"chat:{i}"))[0].id for i in range(2000)]
        )
        assert 0.06 < firsts[b.id] / 2000 < 0.14  # about its 10% share

    async def test_a_new_copy_moves_only_its_share(self) -> None:
        balancer = ReplicaBalancer(MemoryRedis(), 600, "least_busy")
        copies = [_Dep(), _Dep(), _Dep()]
        model = uuid.uuid4()
        keys = [f"chat:{i}" for i in range(600)]
        before = {k: (await balancer.order(model, copies, affinity=k))[0].id for k in keys}
        after = {
            k: (await balancer.order(model, [*copies, _Dep()], affinity=k))[0].id for k in keys
        }
        moved = sum(before[k] != after[k] for k in keys)
        assert 0.15 < moved / len(keys) < 0.35  # about a quarter, all to the new copy
        assert all(after[k] not in {c.id for c in copies} for k in keys if before[k] != after[k])

    async def test_outcomes_are_counted(self) -> None:
        """E96: how often a conversation stays on its copy, and how often it moves."""
        from prometheus_client import REGISTRY

        def count(outcome: str) -> float:
            name = "ai_platform_gateway_session_affinity_total"
            return REGISTRY.get_sample_value(name, {"outcome": outcome}) or 0.0

        redis = MemoryRedis()
        balancer = ReplicaBalancer(redis, 600, "least_busy", affinity_max_extra=0)
        copies = [_Dep(), _Dep()]
        model = uuid.uuid4()
        pinned, overflow = count("pinned"), count("overflow")
        first = (await balancer.order(model, copies, affinity="chat:m"))[0]
        assert count("pinned") == pinned + 1
        await balancer.acquire(first.id)
        await balancer.order(model, copies, affinity="chat:m")
        assert count("overflow") == overflow + 1

    async def test_a_busy_copy_is_passed_over(self) -> None:
        redis = MemoryRedis()
        balancer = ReplicaBalancer(redis, 600, "least_busy", affinity_max_extra=2)
        copies = [_Dep(), _Dep()]
        model = uuid.uuid4()
        pinned = (await balancer.order(model, copies, affinity="chat:x"))[0]
        other = next(c for c in copies if c is not pinned)
        for _ in range(2):
            await balancer.acquire(pinned.id)
        assert (await balancer.order(model, copies, affinity="chat:x"))[0] is pinned
        await balancer.acquire(pinned.id)  # three more than the other now
        assert (await balancer.order(model, copies, affinity="chat:x"))[0] is other

    async def test_weight_zero_is_failover_only(self) -> None:
        balancer = ReplicaBalancer(MemoryRedis(), 600, "least_busy")
        a, b = _Dep(), _Dep()
        b.traffic_weight = 0
        for i in range(50):
            ordered = await balancer.order(uuid.uuid4(), [a, b], affinity=f"chat:{i}")
            assert [d.id for d in ordered] == [a.id, b.id]


class TestGateway:
    async def test_a_chat_stays_on_its_copy(self, gateway, session) -> None:
        build, providers = gateway
        model, urls = await _model_with_copies(session, 3)
        served: list[str] = []
        for i, url in enumerate(urls):
            providers[url] = CopyProvider(f"copy{i}", served)
        service = build()
        service._settings.gateway.session_affinity = True
        turns = [chat("Be brief.", "Plan a trip"), chat("Be brief.", "Plan a trip", "Ok", "And?")]
        for messages in turns * 3:
            await service.chat_completion(
                {"model": model.name, "messages": messages}, GatewayContext()
            )
        assert len(set(served)) == 1

        served.clear()
        for i in range(30):
            await service.chat_completion(
                {"model": model.name, "messages": chat("Be brief.", "hi")},
                GatewayContext(session_id=f"conversation-{i}"),
            )
        assert len(set(served)) == 3  # different conversations still spread

    async def test_off_by_default(self, gateway, session) -> None:
        build, providers = gateway
        model, urls = await _model_with_copies(session, 3)
        served: list[str] = []
        for i, url in enumerate(urls):
            providers[url] = CopyProvider(f"copy{i}", served)
        service = build()
        for _ in range(6):
            await service.chat_completion(
                {"model": model.name, "messages": chat("s", "same chat")}, GatewayContext()
            )
        assert sorted(Counter(served).values()) == [2, 2, 2]
