# ruff: noqa: F811 - the shared `rag` fixture is imported, then requested by name
"""Per-document access (E56): restricted documents are retrieved only by their readers.

Reuses the E5 retrieval harness: the real knowledge service on the test database with an
in-memory vector store that honours the ``access`` label filter the way Qdrant does.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.interfaces.vector import AnyOrMissing
from app.core.permissions import Permission as Perm
from app.models.audit import AuditAction, AuditLog
from app.models.knowledge import Document, DocumentChunk
from app.models.teams import Team, TeamMembership
from app.services.document_access import DocumentReader, access_labels
from tests.api.conftest import _user_with
from tests.api.test_rag_depth import (  # noqa: F401 - the rag fixture is used by name
    HOWTO,
    TRAVEL,
    add_document,
    make_base,
    rag,
)
from tests.conftest import auth_header

SECRET = "Password reset codes for the executive accounts are kept in the red binder."


async def _team(session: AsyncSession, *members: Any) -> Team:
    suffix = uuid.uuid4().hex[:8]
    team = Team(name=f"Team {suffix}", slug=f"team-{suffix}")
    session.add(team)
    await session.flush()
    for member in members:
        session.add(TeamMembership(team_id=team.id, user_id=member.id))
    await session.flush()
    return team


async def _viewer(session: AsyncSession, settings, name: str = "viewer") -> Any:
    return await _user_with(session, settings, [Perm.KNOWLEDGE_VIEW], name=name)


async def _manager(session: AsyncSession, settings) -> Any:
    return await _user_with(
        session, settings, [Perm.KNOWLEDGE_VIEW, Perm.KNOWLEDGE_MANAGE], name="kbmanager"
    )


async def _found(rag: SimpleNamespace, base: Any, reader: DocumentReader | None) -> set:
    trace = await rag.service.retrieve([base], "password reset codes", top_k=10, reader=reader)
    return {r.document_id for r in trace.results}


async def _corpus(rag: SimpleNamespace, actor: Any) -> tuple[Any, Document, Document]:
    base = await make_base(rag.session)
    public = await add_document(rag, base, "howto.txt", HOWTO, actor)
    secret = await add_document(rag, base, "secret.txt", SECRET, actor)
    return base, public, secret


# ---------------------------------------------------------------------------
# Labels (pure)
# ---------------------------------------------------------------------------
class TestLabels:
    def test_open_documents_are_public(self) -> None:
        assert access_labels(False, [("user", uuid.uuid4())]) == ["public"]

    def test_restricted_always_carry_the_marker(self) -> None:
        """Never an empty list, which the filter would read as "no label, so open"."""
        assert access_labels(True, []) == ["restricted"]

    def test_reader_labels(self) -> None:
        user, team = uuid.uuid4(), uuid.uuid4()
        reader = DocumentReader(user_id=user, team_ids=frozenset({team}))
        assert reader.labels() == ("public", f"user:{user}", f"team:{team}")
        assert DocumentReader.open_only().labels() == ("public",)

    def test_may_read(self) -> None:
        user, team = uuid.uuid4(), uuid.uuid4()
        assert DocumentReader(user_id=user).may_read(True, [("user", user)])
        assert DocumentReader(team_ids=frozenset({team})).may_read(True, [("team", team)])
        assert not DocumentReader(user_id=user).may_read(True, [("team", user)])
        assert DocumentReader.open_only().may_read(False, [])
        assert DocumentReader(unrestricted=True).may_read(True, [])


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
class TestRetrieval:
    async def test_open_documents_are_read_by_everyone(self, rag, superuser) -> None:
        base, public, secret = await _corpus(rag, superuser)
        assert await _found(rag, base, None) == {public.id, secret.id}

    async def test_restricted_to_a_user(self, rag, superuser, settings) -> None:
        base, public, secret = await _corpus(rag, superuser)
        alice = await _viewer(rag.session, settings, "alice")
        bob = await _viewer(rag.session, settings, "bob")
        await rag.service.set_access(
            secret.id, restricted=True, user_ids=[alice.id], team_ids=[], actor=superuser
        )
        assert secret.id in await _found(rag, base, await rag.service.reader_for(alice))
        assert await _found(rag, base, await rag.service.reader_for(bob)) == {public.id}
        # No reader at all (a scheduled workflow run): open documents only.
        assert await _found(rag, base, None) == {public.id}
        assert await _found(rag, base, DocumentReader.open_only()) == {public.id}

    async def test_restricted_to_a_team(self, rag, superuser, settings) -> None:
        base, _public, secret = await _corpus(rag, superuser)
        member = await _viewer(rag.session, settings, "member")
        outsider = await _viewer(rag.session, settings, "outsider")
        team = await _team(rag.session, member)
        await rag.service.set_access(
            secret.id, restricted=True, user_ids=[], team_ids=[team.id], actor=superuser
        )
        assert secret.id in await _found(rag, base, await rag.service.reader_for(member.id))
        assert secret.id not in await _found(rag, base, await rag.service.reader_for(outsider))

    async def test_superusers_read_everything(self, rag, superuser) -> None:
        base, _, secret = await _corpus(rag, superuser)
        await rag.service.set_access(
            secret.id, restricted=True, user_ids=[], team_ids=[], actor=superuser
        )
        assert secret.id in await _found(rag, base, await rag.service.reader_for(superuser))

    async def test_restricted_chunks_are_labelled(self, rag, superuser, settings) -> None:
        base, _, secret = await _corpus(rag, superuser)
        alice = await _viewer(rag.session, settings, "alice")
        await rag.service.set_access(
            secret.id, restricted=True, user_ids=[alice.id], team_ids=[], actor=superuser
        )
        points = rag.vectors.collections[base.collection].values()
        labels = {
            tuple(p.payload["access"]) for p in points if p.payload["document_id"] == str(secret.id)
        }
        assert labels == {("restricted", f"user:{alice.id}")}
        await rag.service.set_access(
            secret.id, restricted=False, user_ids=[], team_ids=[], actor=superuser
        )
        labels = {
            tuple(p.payload["access"]) for p in points if p.payload["document_id"] == str(secret.id)
        }
        assert labels == {("public",)}

    async def test_unlabelled_points_count_as_open(self, rag, superuser) -> None:
        """Indexed before E56: no re-index needed for them to stay readable."""
        base, public, secret = await _corpus(rag, superuser)
        for point in rag.vectors.collections[base.collection].values():
            point.payload.pop("access", None)
        assert await _found(rag, base, DocumentReader.open_only()) == {public.id, secret.id}

    async def test_a_stale_label_cannot_leak(self, rag, superuser, settings) -> None:
        """PostgreSQL decides: a label the vector store failed to update is not trusted."""
        base, public, secret = await _corpus(rag, superuser)
        bob = await _viewer(rag.session, settings, "bob")

        async def broken(*_: Any, **__: Any) -> None:
            raise RuntimeError("vector store down")

        rag.vectors.set_payload = broken
        await rag.service.set_access(
            secret.id, restricted=True, user_ids=[], team_ids=[], actor=superuser
        )
        assert await _found(rag, base, await rag.service.reader_for(bob)) == {public.id}

    async def test_new_versions_inherit_access(self, rag, superuser, settings) -> None:
        base, _, secret = await _corpus(rag, superuser)
        alice = await _viewer(rag.session, settings, "alice")
        bob = await _viewer(rag.session, settings, "bob")
        await rag.service.set_access(
            secret.id, restricted=True, user_ids=[alice.id], team_ids=[], actor=superuser
        )
        second = await add_document(
            rag, base, "secret.txt", SECRET + " Updated.", superuser, version_of=secret.id
        )
        assert second.restricted is True
        assert second.id in await _found(rag, base, await rag.service.reader_for(alice))
        assert second.id not in await _found(rag, base, await rag.service.reader_for(bob))

    async def test_restricted_from_upload(self, rag, superuser, settings) -> None:
        base = await make_base(rag.session)
        alice = await _viewer(rag.session, settings, "alice")
        document = await rag.service.accept_upload(
            base.id,
            filename="secret.txt",
            content_type="text/plain",
            content=SECRET.encode(),
            actor=superuser,
            restricted=True,
            user_ids=[alice.id],
        )
        from tests.api.test_rag_depth import ingest

        await ingest(rag, document.id)
        chunks = (
            (
                await rag.session.execute(
                    select(DocumentChunk).where(DocumentChunk.document_id == document.id)
                )
            )
            .scalars()
            .all()
        )
        assert chunks
        payloads = [rag.vectors.collections[base.collection][c.vector_id].payload for c in chunks]
        assert all(p["access"] == ["restricted", f"user:{alice.id}"] for p in payloads)

    async def test_the_filter_is_sent_to_the_vector_store(self, rag, superuser, settings) -> None:
        base, *_ = await _corpus(rag, superuser)
        alice = await _viewer(rag.session, settings, "alice")
        seen: list[Any] = []
        original = rag.vectors.search

        async def spy(*args: Any, **kwargs: Any):
            seen.append(kwargs.get("filters"))
            return await original(*args, **kwargs)

        rag.vectors.search = spy
        await _found(rag, base, await rag.service.reader_for(alice))
        assert isinstance(seen[0]["access"], AnyOrMissing)
        seen.clear()
        await _found(rag, base, await rag.service.reader_for(superuser))
        assert seen[0]["access"] is None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class TestApi:
    async def test_set_and_read_access(self, client, tokens, rag, superuser, settings) -> None:
        _base, _, secret = await _corpus(rag, superuser)
        alice = await _viewer(rag.session, settings, "alice")
        team = await _team(rag.session)
        response = await client.put(
            f"/api/v1/documents/{secret.id}/access",
            headers=auth_header(tokens, superuser),
            json={"restricted": True, "users": [str(alice.id)], "teams": [str(team.id)]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["restricted"] is True
        assert body["users"] == [{"id": str(alice.id), "name": alice.username}]
        assert body["teams"] == [{"id": str(team.id), "name": team.name}]

        got = await client.get(
            f"/api/v1/documents/{secret.id}/access", headers=auth_header(tokens, superuser)
        )
        assert got.json() == body

        audit = (
            (
                await rag.session.execute(
                    select(AuditLog).where(
                        AuditLog.action == AuditAction.DOCUMENT_ACCESS_CHANGED,
                        AuditLog.resource_id == str(secret.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audit) == 1

    async def test_readers_need_a_restriction(self, client, tokens, rag, superuser, settings):
        _, _, secret = await _corpus(rag, superuser)
        alice = await _viewer(rag.session, settings, "alice")
        response = await client.put(
            f"/api/v1/documents/{secret.id}/access",
            headers=auth_header(tokens, superuser),
            json={"restricted": False, "users": [str(alice.id)]},
        )
        assert response.status_code == 422

    async def test_unknown_reader_is_refused(self, client, tokens, rag, superuser) -> None:
        _, _, secret = await _corpus(rag, superuser)
        response = await client.put(
            f"/api/v1/documents/{secret.id}/access",
            headers=auth_header(tokens, superuser),
            json={"restricted": True, "users": [str(uuid.uuid4())]},
        )
        assert response.status_code == 422

    async def test_changing_access_needs_manage(self, client, tokens, rag, superuser, settings):
        _, _, secret = await _corpus(rag, superuser)
        viewer = await _viewer(rag.session, settings)
        response = await client.put(
            f"/api/v1/documents/{secret.id}/access",
            headers=auth_header(tokens, viewer),
            json={"restricted": True},
        )
        assert response.status_code == 403

    async def test_hidden_documents_are_not_listed_or_fetched(
        self, client, tokens, rag, superuser, settings
    ) -> None:
        base, public, secret = await _corpus(rag, superuser)
        viewer = await _viewer(rag.session, settings)
        manager = await _manager(rag.session, settings)
        await rag.service.set_access(
            secret.id, restricted=True, user_ids=[], team_ids=[], actor=superuser
        )

        listed = await client.get(
            f"/api/v1/knowledge-bases/{base.id}/documents", headers=auth_header(tokens, viewer)
        )
        assert listed.status_code == 200, listed.text
        assert [d["id"] for d in listed.json()] == [str(public.id)]
        fetched = await client.get(
            f"/api/v1/documents/{secret.id}", headers=auth_header(tokens, viewer)
        )
        assert fetched.status_code == 404

        # A manager sees it listed (to manage it), flagged as restricted...
        listed = await client.get(
            f"/api/v1/knowledge-bases/{base.id}/documents", headers=auth_header(tokens, manager)
        )
        rows = {d["id"]: d for d in listed.json()}
        assert rows[str(secret.id)]["restricted"] is True
        # ...but does not retrieve it.
        searched = await client.post(
            f"/api/v1/knowledge-bases/{base.id}/search",
            headers=auth_header(tokens, manager),
            json={"query": "password reset codes", "limit": 10},
        )
        assert searched.status_code == 200, searched.text
        assert str(secret.id) not in {r["document_id"] for r in searched.json()["results"]}

    async def test_search_endpoint_applies_access(
        self, client, tokens, rag, superuser, settings
    ) -> None:
        base, _, secret = await _corpus(rag, superuser)
        alice = await _viewer(rag.session, settings, "alice")
        bob = await _viewer(rag.session, settings, "bob")
        await rag.service.set_access(
            secret.id, restricted=True, user_ids=[alice.id], team_ids=[], actor=superuser
        )

        async def search(user: Any) -> set[str]:
            response = await client.post(
                f"/api/v1/knowledge-bases/{base.id}/search",
                headers=auth_header(tokens, user),
                json={"query": "password reset codes", "limit": 10},
            )
            assert response.status_code == 200, response.text
            return {r["document_id"] for r in response.json()["results"]}

        assert str(secret.id) in await search(alice)
        assert str(secret.id) not in await search(bob)

    async def test_restricted_upload_form(self, client, tokens, rag, superuser, settings) -> None:
        base = await make_base(rag.session)
        alice = await _viewer(rag.session, settings, "alice")
        response = await client.post(
            f"/api/v1/knowledge-bases/{base.id}/documents",
            headers=auth_header(tokens, superuser),
            files={"file": ("travel.txt", TRAVEL.encode(), "text/plain")},
            data={"restricted": "true", "users": str(alice.id)},
        )
        assert response.status_code == 202, response.text
        document_id = uuid.UUID(response.json()["document_id"])
        document = await rag.session.get(Document, document_id)
        assert document is not None and document.restricted is True
        access = await rag.service.describe_access(document)
        assert [u["id"] for u in access["users"]] == [alice.id]
