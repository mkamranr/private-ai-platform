"""Signed workflow webhooks (expansion E36), through the endpoint.

The token (E6) still decides who may call. A signature proves the body arrived as sent
and, with the timestamp inside it, that the call is fresh; the platform also refuses a
signature it has already accepted.
"""

from __future__ import annotations

import json
import time
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workflows import Workflow
from app.services import webhook_signing as ws
from app.services.workflows import template_definition
from tests.api.test_workflows import builder, published, wf  # noqa: F401
from tests.conftest import auth_header


async def _hook(client, tokens, builder, *, require: bool | None = None):
    workflow = await published(client, tokens, builder, template_definition("blank"))
    body = {} if require is None else {"require_signature": require}
    rotated = await client.post(
        f"/api/v1/workflows/{workflow['id']}/webhook",
        headers=auth_header(tokens, builder),
        json=body,
    )
    assert rotated.status_code == 200, rotated.text
    return workflow, rotated.json()


def _signed(token: str, secret: str, body: bytes, *, at: int | None = None) -> dict[str, str]:
    stamp = int(time.time()) if at is None else at
    return {
        "X-Workflow-Token": token,
        "X-Workflow-Timestamp": str(stamp),
        "X-Workflow-Signature": ws.sign(secret, stamp, body),
        "Content-Type": "application/json",
    }


class TestIssuing:
    async def test_a_signing_secret_is_issued_once_and_stored_encrypted(
        self, client, tokens, builder, session: AsyncSession
    ) -> None:
        workflow, hook = await _hook(client, tokens, builder)
        secret = hook["signing_secret"]
        assert secret.startswith("wfs_") and hook["signature_required"] is False
        stored = await session.get(Workflow, uuid.UUID(workflow["id"]))
        assert stored is not None and stored.webhook_secret_encrypted
        assert secret not in stored.webhook_secret_encrypted

        detail = await client.get(
            f"/api/v1/workflows/{workflow['id']}", headers=auth_header(tokens, builder)
        )
        assert "signing_secret" not in detail.text and secret not in detail.text

    async def test_requiring_signatures_is_kept_across_rotation(
        self, client, tokens, builder
    ) -> None:
        workflow, hook = await _hook(client, tokens, builder, require=True)
        assert hook["signature_required"] is True
        again = await client.post(
            f"/api/v1/workflows/{workflow['id']}/webhook", headers=auth_header(tokens, builder)
        )
        assert again.json()["signature_required"] is True
        assert again.json()["signing_secret"] != hook["signing_secret"]


class TestCalls:
    async def test_a_signed_call_is_accepted_once(self, client, tokens, builder) -> None:
        workflow, hook = await _hook(client, tokens, builder, require=True)
        url = f"/api/v1/workflow-hooks/{workflow['id']}"
        body = json.dumps({"order": "SO-88140"}).encode()
        headers = _signed(hook["token"], hook["signing_secret"], body)

        accepted = await client.post(url, content=body, headers=headers)
        assert accepted.status_code == 202, accepted.text
        # The same call again: a replay.
        assert (await client.post(url, content=body, headers=headers)).status_code == 401

    async def test_when_required_an_unsigned_call_is_refused(self, client, tokens, builder) -> None:
        workflow, hook = await _hook(client, tokens, builder, require=True)
        response = await client.post(
            f"/api/v1/workflow-hooks/{workflow['id']}",
            json={"order": 1},
            headers={"X-Workflow-Token": hook["token"]},
        )
        assert response.status_code == 401
        # The same answer as a wrong token: nothing says which check failed.
        assert response.json()["error"]["message"] == "Invalid webhook token."

    async def test_a_changed_body_or_old_timestamp_is_refused(
        self, client, tokens, builder
    ) -> None:
        workflow, hook = await _hook(client, tokens, builder, require=True)
        url = f"/api/v1/workflow-hooks/{workflow['id']}"
        signed_for = json.dumps({"amount": 10}).encode()
        tampered = json.dumps({"amount": 10_000}).encode()
        headers = _signed(hook["token"], hook["signing_secret"], signed_for)
        assert (await client.post(url, content=tampered, headers=headers)).status_code == 401

        stale = _signed(
            hook["token"],
            hook["signing_secret"],
            signed_for,
            at=int(time.time()) - ws.TOLERANCE_SECONDS - 60,
        )
        assert (await client.post(url, content=signed_for, headers=stale)).status_code == 401

    async def test_not_required_unsigned_works_but_a_bad_signature_does_not(
        self, client, tokens, builder
    ) -> None:
        workflow, hook = await _hook(client, tokens, builder)
        url = f"/api/v1/workflow-hooks/{workflow['id']}"
        plain = await client.post(url, json={"a": 1}, headers={"X-Workflow-Token": hook["token"]})
        assert plain.status_code == 202, plain.text

        body = json.dumps({"a": 2}).encode()
        wrong = _signed(hook["token"], "wfs_not_the_secret", body)
        assert (await client.post(url, content=body, headers=wrong)).status_code == 401

    async def test_a_rotated_secret_stops_working(self, client, tokens, builder) -> None:
        workflow, hook = await _hook(client, tokens, builder, require=True)
        rotated = await client.post(
            f"/api/v1/workflows/{workflow['id']}/webhook", headers=auth_header(tokens, builder)
        )
        body = b"{}"
        old = _signed(rotated.json()["token"], hook["signing_secret"], body)
        response = await client.post(
            f"/api/v1/workflow-hooks/{workflow['id']}", content=body, headers=old
        )
        assert response.status_code == 401
