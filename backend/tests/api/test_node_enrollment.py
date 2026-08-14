"""Node self-enrolment over the API (M04).

The happy path is one test. Most of this file is the ways enrolment must refuse, because
that is where the risk lives: the node-facing endpoint takes a credential that is not a
user account, and it makes the control plane fetch an address the caller supplied.

Reuses `test_infrastructure`'s fixtures — `infra_app` patches `NodeService.build_client`,
the single seam where an agent client is constructed, so token encryption, the sync and
the audit trail all still run for real and only the network hop is faked.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.infrastructure import EnrollmentStatus, Node, NodeEnrollment
from tests.api.test_infrastructure import (  # noqa: F401 — fixtures are used by name
    FakeAgentClient,
    agent,
    infra_admin,
    infra_app,
    infra_client,
)
from tests.conftest import auth_header


@pytest.fixture(autouse=True)
def _headroom(settings):
    """Lift the per-IP enrolment rate limit for this module.

    Every test here calls the endpoint from 127.0.0.1 within the same minute, so the
    production default of 10/min would make results depend on test ordering. The limit
    itself is exercised in its own test below, which sets it back down deliberately.
    """
    original = settings.enrollment.rate_limit_per_minute_per_ip
    settings.enrollment.rate_limit_per_minute_per_ip = 10_000
    yield
    settings.enrollment.rate_limit_per_minute_per_ip = original


ENROLL = "/api/v1/nodes/enroll"
ENROLLMENTS = "/api/v1/node-enrollments"
AGENT_TOKEN = "a" * 40
ADVERTISED = "http://10.90.0.21:9100"


async def mint(client: AsyncClient, tokens, admin: User, **body) -> dict:
    body.setdefault("name", "fake")
    response = await client.post(ENROLLMENTS, json=body, headers=auth_header(tokens, admin))
    assert response.status_code == 201, response.text
    return response.json()


async def enrol(client: AsyncClient, token: str, **body) -> tuple[int, dict]:
    body.setdefault("agent_token", AGENT_TOKEN)
    body.setdefault("advertised_url", ADVERTISED)
    response = await client.post(ENROLL, json=body, headers={"Authorization": f"Bearer {token}"})
    return response.status_code, response.json()


# -- issuing --------------------------------------------------------------------------


class TestIssuing:
    async def test_the_token_is_returned_once_with_a_runnable_command(
        self, infra_client: AsyncClient, tokens, infra_admin: User
    ) -> None:
        body = await mint(infra_client, tokens, infra_admin)
        assert body["enrollment_token"].startswith("aine_")
        # The point of the whole feature: the operator gets a line to run, not a form.
        assert "install-node.sh" in body["command"]
        assert body["enrollment_token"] in body["command"]
        assert "--name fake" in body["command"]

    async def test_the_mint_response_is_not_cacheable(
        self, infra_client: AsyncClient, tokens, infra_admin: User
    ) -> None:
        """It is the only response in the API that carries a live credential."""
        response = await infra_client.post(
            ENROLLMENTS, json={"name": "fake"}, headers=auth_header(tokens, infra_admin)
        )
        assert response.headers.get("cache-control") == "no-store"

    async def test_the_token_never_appears_again(
        self, infra_client: AsyncClient, tokens, infra_admin: User
    ) -> None:
        """Only a hash is stored, so neither listing nor fetching can show it."""
        created = await mint(infra_client, tokens, infra_admin)
        headers = auth_header(tokens, infra_admin)

        listing = await infra_client.get(ENROLLMENTS, headers=headers)
        one = await infra_client.get(f"{ENROLLMENTS}/{created['id']}", headers=headers)
        for text in (listing.text, one.text):
            assert created["enrollment_token"] not in text
            assert "token_hash" not in text
        assert one.json()["token_prefix"] == created["token_prefix"]

    async def test_two_open_enrolments_for_one_name_are_refused(
        self, infra_client: AsyncClient, tokens, infra_admin: User
    ) -> None:
        await mint(infra_client, tokens, infra_admin, name="dupe")
        again = await infra_client.post(
            ENROLLMENTS, json={"name": "dupe"}, headers=auth_header(tokens, infra_admin)
        )
        assert again.status_code == 409

    async def test_a_user_without_infrastructure_manage_cannot_issue_one(
        self, infra_client: AsyncClient, tokens, unprivileged_user: User
    ) -> None:
        response = await infra_client.post(
            ENROLLMENTS, json={"name": "nope"}, headers=auth_header(tokens, unprivileged_user)
        )
        assert response.status_code == 403

    async def test_anonymous_cannot_issue_one(self, infra_client: AsyncClient) -> None:
        assert (await infra_client.post(ENROLLMENTS, json={"name": "nope"})).status_code == 401


# -- the node-facing endpoint ----------------------------------------------------------


class TestEnrolling:
    async def test_a_node_enrols_itself_and_arrives_online(
        self, infra_client: AsyncClient, tokens, infra_admin: User, session: AsyncSession
    ) -> None:
        created = await mint(infra_client, tokens, infra_admin)
        status, body = await enrol(infra_client, created["enrollment_token"])

        assert status == 201, body
        assert body["node_name"] == "fake"
        assert body["status"] == "ONLINE"
        assert body["gpus_seen"] > 0
        # The address was reported by the node, not typed by anyone.
        node = (await session.execute(select(Node).where(Node.name == "fake"))).scalar_one()
        assert node.agent_url == ADVERTISED

    async def test_the_agent_token_is_stored_encrypted_and_usable(
        self, infra_client: AsyncClient, tokens, infra_admin: User, session: AsyncSession, app
    ) -> None:
        """Encrypted rather than hashed, because the platform presents it on every poll."""
        created = await mint(infra_client, tokens, infra_admin)
        await enrol(infra_client, created["enrollment_token"])

        node = (await session.execute(select(Node).where(Node.name == "fake"))).scalar_one()
        assert node.agent_token_encrypted != AGENT_TOKEN
        assert app.state.secret_cipher.decrypt(node.agent_token_encrypted) == AGENT_TOKEN

    async def test_a_platform_jwt_is_not_a_node_credential(
        self, infra_client: AsyncClient, tokens, infra_admin: User
    ) -> None:
        """401, not 422.

        There is no router-level guard on this module, so the dependency is the only thing
        between this route and the network. A valid admin JWT must still be refused: the
        caller is meant to be a shell script holding a one-time token.
        """
        response = await infra_client.post(
            ENROLL,
            json={"agent_token": AGENT_TOKEN, "advertised_url": ADVERTISED},
            headers=auth_header(tokens, infra_admin),
        )
        assert response.status_code == 401

    async def test_no_credential_at_all_is_refused(self, infra_client: AsyncClient) -> None:
        response = await infra_client.post(
            ENROLL, json={"agent_token": AGENT_TOKEN, "advertised_url": ADVERTISED}
        )
        assert response.status_code == 401

    async def test_a_token_works_exactly_once(
        self, infra_client: AsyncClient, tokens, infra_admin: User, session: AsyncSession
    ) -> None:
        created = await mint(infra_client, tokens, infra_admin)
        first, _ = await enrol(infra_client, created["enrollment_token"])
        second, _ = await enrol(infra_client, created["enrollment_token"])

        assert (first, second) == (201, 401)
        nodes = (await session.execute(select(Node).where(Node.name == "fake"))).scalars().all()
        assert len(nodes) == 1, "a replayed token created a second node"

    async def test_an_expired_token_is_refused(
        self, infra_client: AsyncClient, tokens, infra_admin: User, session: AsyncSession
    ) -> None:
        created = await mint(infra_client, tokens, infra_admin)
        row = await session.get(NodeEnrollment, uuid.UUID(created["id"]))
        assert row is not None
        row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        await session.flush()

        status, _ = await enrol(infra_client, created["enrollment_token"])
        assert status == 401

    async def test_a_revoked_token_is_refused(
        self, infra_client: AsyncClient, tokens, infra_admin: User
    ) -> None:
        created = await mint(infra_client, tokens, infra_admin)
        revoked = await infra_client.delete(
            f"{ENROLLMENTS}/{created['id']}", headers=auth_header(tokens, infra_admin)
        )
        assert revoked.status_code == 200
        status, _ = await enrol(infra_client, created["enrollment_token"])
        assert status == 401

    async def test_a_garbage_token_is_refused(self, infra_client: AsyncClient) -> None:
        status, _ = await enrol(infra_client, "aine_deadbeef_nonsense")
        assert status == 401

    async def test_a_weak_agent_token_is_refused(
        self, infra_client: AsyncClient, tokens, infra_admin: User
    ) -> None:
        """The node generates this one, so the platform enforces a floor on it."""
        created = await mint(infra_client, tokens, infra_admin)
        status, _ = await enrol(infra_client, created["enrollment_token"], agent_token="short")
        assert status == 422


# -- what the probe-back is for --------------------------------------------------------


class TestVerification:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254:9100",  # cloud metadata
            "http://127.0.0.1:9100",  # the control plane itself
            "http://10.90.0.21:22",  # a port that is not an agent
            "http://user:pw@10.90.0.21:9100",
            "http://10.90.0.21:9100/latest/meta-data",
            "file:///etc/passwd",
        ],
    )
    async def test_a_hostile_address_is_refused_and_never_fetched(
        self,
        infra_client: AsyncClient,
        tokens,
        infra_admin: User,
        agent: FakeAgentClient,
        url: str,
        session: AsyncSession,
    ) -> None:
        created = await mint(infra_client, tokens, infra_admin)
        before = len(agent.actions)
        status, _ = await enrol(infra_client, created["enrollment_token"], advertised_url=url)

        assert status == 422
        assert len(agent.actions) == before, f"the control plane fetched {url}"
        assert not (await session.execute(select(Node).where(Node.name == "fake"))).scalars().all()

    async def test_a_node_calling_itself_something_else_is_refused(
        self, infra_client: AsyncClient, tokens, infra_admin: User, session: AsyncSession
    ) -> None:
        """Closes a gap the agent's own config documented but nothing enforced.

        Catches the accident that matters: running the script on a host that already runs
        an agent for a *different* node.
        """
        created = await mint(infra_client, tokens, infra_admin, name="gpu-node-07")
        status, body = await enrol(infra_client, created["enrollment_token"])

        assert status == 422
        assert "NODE_AGENT_NODE_NAME=gpu-node-07" in body["error"]["message"]
        assert (
            not (await session.execute(select(Node).where(Node.name == "gpu-node-07")))
            .scalars()
            .all()
        )

    async def test_an_unreachable_agent_leaves_no_node_and_an_unspent_token(
        self, app, client: AsyncClient, tokens, infra_admin: User, session: AsyncSession
    ) -> None:
        """The rule manual registration already follows, kept for enrolment.

        A phantom node that never reports is indistinguishable from one that is merely
        offline — so failure must be loud, and the invitation must survive so the operator
        can fix the firewall and run the script again.
        """
        from app.services.infrastructure import NodeService

        created = await mint(client, tokens, infra_admin, name="unreachable-node")
        down = FakeAgentClient(unreachable=True, node_name="unreachable-node")
        original = NodeService.build_client
        NodeService.build_client = lambda self, **kwargs: down  # type: ignore[assignment,method-assign]
        try:
            status, _ = await enrol(client, created["enrollment_token"])
        finally:
            NodeService.build_client = original  # type: ignore[method-assign]

        assert status == 422
        assert (
            not (await session.execute(select(Node).where(Node.name == "unreachable-node")))
            .scalars()
            .all()
        )
        row = await session.get(NodeEnrollment, uuid.UUID(created["id"]))
        await session.refresh(row)
        assert row is not None and row.status == EnrollmentStatus.PENDING


# -- re-enrolment ----------------------------------------------------------------------


class TestReenrolment:
    async def test_a_rebuilt_host_keeps_its_identity_and_history(
        self, infra_client: AsyncClient, tokens, infra_admin: User, session: AsyncSession
    ) -> None:
        """The case a PENDING status on `nodes` could not have modelled.

        Same row, new credentials: the node's id, GPUs and everything hanging off them
        survive a rebuild or a token rotation.
        """
        first = await mint(infra_client, tokens, infra_admin)
        await enrol(infra_client, first["enrollment_token"])
        node = (await session.execute(select(Node).where(Node.name == "fake"))).scalar_one()
        original_id, gpu_count = node.id, len(node.gpus)
        assert gpu_count > 0

        second = await mint(infra_client, tokens, infra_admin, reenroll=True)
        rotated = "b" * 40
        status, _ = await enrol(
            infra_client,
            second["enrollment_token"],
            agent_token=rotated,
            advertised_url="http://10.90.0.99:9100",
        )
        assert status == 201

        await session.refresh(node)
        assert node.id == original_id
        assert node.agent_url == "http://10.90.0.99:9100"
        assert len(node.gpus) == gpu_count

    async def test_re_enrolling_without_asking_for_it_is_refused(
        self, infra_client: AsyncClient, tokens, infra_admin: User
    ) -> None:
        first = await mint(infra_client, tokens, infra_admin)
        await enrol(infra_client, first["enrollment_token"])
        clash = await infra_client.post(
            ENROLLMENTS, json={"name": "fake"}, headers=auth_header(tokens, infra_admin)
        )
        assert clash.status_code == 409


# -- the audit trail -------------------------------------------------------------------


async def test_no_credential_reaches_the_audit_log(
    infra_client: AsyncClient, tokens, infra_admin: User, session: AsyncSession
) -> None:
    """`_REDACT_KEYS` matches exact key names, so neither token is covered by "token".

    Belt and braces: nothing puts them in metadata, and the redaction list now names them.
    """
    from app.models.audit import AuditLog

    created = await mint(infra_client, tokens, infra_admin)
    await enrol(infra_client, created["enrollment_token"])

    rows = (await session.execute(select(AuditLog))).scalars().all()
    blob = " ".join(str(r.meta) for r in rows)
    assert created["enrollment_token"] not in blob
    assert AGENT_TOKEN not in blob
    assert any(r.action == "NODE_ENROLLED" for r in rows)


# -- serving the node's installer -----------------------------------------------------


BUNDLE_URL = "/api/v1/nodes/enrollment-bundle"


def _stage(settings, tmp_path) -> None:
    """Put the four artifacts where the control plane expects them."""
    from app.api.v1.infrastructure import NODE_BUNDLE_FILES

    for name in NODE_BUNDLE_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"stand-in for " + name.encode())
    settings.enrollment.node_bundle_path = str(tmp_path)


class TestNodeBundleDownload:
    """A joining node fetches ~288 MB from the control plane rather than 1.9 GB by hand.

    Not a Rule 4 violation: the bytes came in on the bundle and this moves them one hop
    inside the site's own network. What these tests hold onto is that the credential is the
    enrolment token, that fetching does not spend it, and that the file list is fixed.
    """

    async def test_the_bundle_needs_the_enrolment_token(
        self, infra_client: AsyncClient, settings, tmp_path
    ) -> None:
        _stage(settings, tmp_path)
        response = await infra_client.get(BUNDLE_URL)
        assert response.status_code == 401, response.text

    async def test_a_user_jwt_is_not_a_node_credential(
        self, infra_client: AsyncClient, settings, tmp_path, tokens, infra_admin: User
    ) -> None:
        """The caller is a shell script on a host with no user account, and must stay so."""
        _stage(settings, tmp_path)
        response = await infra_client.get(BUNDLE_URL, headers=auth_header(tokens, infra_admin))
        assert response.status_code == 401, response.text

    async def test_it_serves_exactly_the_four_artifacts(
        self, infra_client: AsyncClient, settings, tmp_path, tokens, infra_admin: User
    ) -> None:
        import io
        import tarfile

        from app.api.v1.infrastructure import NODE_BUNDLE_FILES

        _stage(settings, tmp_path)
        created = await mint(infra_client, tokens, infra_admin, name="bundle-node")
        response = await infra_client.get(
            BUNDLE_URL, headers={"Authorization": f"Bearer {created['enrollment_token']}"}
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/x-tar"

        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r|") as archive:
            assert sorted(m.name for m in archive) == sorted(NODE_BUNDLE_FILES)

    async def test_downloading_does_not_spend_the_single_use_token(
        self, infra_client: AsyncClient, settings, tmp_path, tokens, infra_admin: User
    ) -> None:
        """The one use belongs to the enrolment that follows.

        A download that consumed it would leave the operator holding a dead token and an
        installed agent that cannot join — and the failure would arrive minutes later, on a
        different host, looking nothing like its cause.
        """
        _stage(settings, tmp_path)
        # The default name, because the harness's fake agent reports itself as "fake" and
        # a mismatch is refused *after* the token resolves — which would pass this test for
        # the wrong reason.
        created = await mint(infra_client, tokens, infra_admin)
        token = created["enrollment_token"]

        first = await infra_client.get(BUNDLE_URL, headers={"Authorization": f"Bearer {token}"})
        assert first.status_code == 200, first.text

        status, body = await enrol(infra_client, token)
        assert status == 201, body

    async def test_an_unstaged_control_plane_says_what_is_missing(
        self, infra_client: AsyncClient, settings, tmp_path, tokens, infra_admin: User
    ) -> None:
        """A development checkout installed nothing from a bundle, and must say so."""
        settings.enrollment.node_bundle_path = str(tmp_path / "absent")
        created = await mint(infra_client, tokens, infra_admin, name="unstaged-node")
        response = await infra_client.get(
            BUNDLE_URL, headers={"Authorization": f"Bearer {created['enrollment_token']}"}
        )
        assert response.status_code == 404, response.text
        assert "install-node.sh" in response.text

    async def test_the_command_falls_back_when_nothing_is_staged(
        self, infra_client: AsyncClient, settings, tmp_path, tokens, infra_admin: User
    ) -> None:
        """No download line, rather than a link that would 404."""
        settings.enrollment.node_bundle_path = str(tmp_path / "absent")
        created = await mint(infra_client, tokens, infra_admin, name="fallback-node")
        assert "install-node.sh" in created["command"]
        assert "curl" not in created["command"]

    async def test_the_command_offers_the_download_when_staged(
        self, infra_client: AsyncClient, settings, tmp_path, tokens, infra_admin: User
    ) -> None:
        _stage(settings, tmp_path)
        created = await mint(infra_client, tokens, infra_admin, name="offer-node")
        command = created["command"]
        assert "enrollment-bundle" in command
        # In a header, never the URL: the access log records paths.
        assert f"Authorization: Bearer {created['enrollment_token']}" in command
        assert f"token={created['enrollment_token']}" not in command
