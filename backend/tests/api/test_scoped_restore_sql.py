"""E64: the scoped restore's SQL, run against a real database.

``backup.py`` loads the backup's rows from CSV into staging tables with psql. Here the
staging tables are filled straight from the live tables instead ("taking the backup"),
the live rows are then changed, and ``apply_sql`` has to put back exactly what was staged.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Document, DocumentAccess, DocumentChunk, KnowledgeBase
from app.models.teams import Team
from tests.unit.test_release_tooling import load

pytestmark = pytest.mark.asyncio

backup = load("backup")


async def make_base(session: AsyncSession, *, team_id: uuid.UUID | None = None) -> KnowledgeBase:
    ident = uuid.uuid4()
    base = KnowledgeBase(
        id=ident,
        name=f"e64-{ident.hex[:10]}",
        display_name="Policies",
        tenant_id="default",
        collection=f"kb_{ident.hex}",
        embedding_model="fake-embed",
        embedding_dimensions=8,
        team_id=team_id,
    )
    session.add(base)
    await session.flush()
    return base


async def add_document(
    session: AsyncSession, base: KnowledgeBase, name: str, *, restricted: bool = False
) -> Document:
    ident = uuid.uuid4()
    document = Document(
        id=ident,
        knowledge_base_id=base.id,
        filename=name,
        content_type="text/plain",
        size_bytes=10,
        sha256=uuid.uuid4().hex,
        lineage_id=ident,
        status="INDEXED",
        restricted=restricted,
    )
    session.add(document)
    await session.flush()
    for ordinal in range(2):
        session.add(DocumentChunk(document_id=ident, ordinal=ordinal, text=f"{name} {ordinal}"))
    if restricted:
        session.add(
            DocumentAccess(lineage_id=ident, principal_type="team", principal_id=uuid.uuid4())
        )
    await session.flush()
    return document


async def columns(session: AsyncSession) -> dict[str, list[str]]:
    names = ", ".join(backup._literal(t) for t in backup.SCOPED_NAMES)
    rows = (await session.execute(text(backup.COLUMNS_SQL.format(names=names)))).all()
    return {row[0]: row[1].split(",") for row in rows}


async def take_backup(session: AsyncSession, ids: list[uuid.UUID]) -> dict[str, list[str]]:
    """Fill the staging tables with these bases' rows as they are now."""
    live = await columns(session)
    common = backup.common_columns(live, live)
    for statement in backup.stage_sql(common):
        await session.execute(text(statement))
    id_sql = backup._ids_sql([str(i) for i in ids])
    for table, where in backup.SCOPED_TABLES:
        if table in common:
            cols = ", ".join(backup._ident(c) for c in common[table])
            await session.execute(
                text(
                    f"INSERT INTO stage_{table} ({cols}) SELECT {cols} FROM public.{table} "
                    f"WHERE {where.format(ids=id_sql)}"
                )
            )
    return common


async def apply(session: AsyncSession, ids: list[uuid.UUID], common: dict[str, Any]) -> None:
    names = ", ".join(backup._literal(t) for t in backup.SCOPED_NAMES)
    fks = [
        (r[0], r[1], r[2], r[3], r[4] == "true")
        for r in (await session.execute(text(backup.FOREIGN_KEYS_SQL.format(names=names)))).all()
    ]
    for statement in backup.apply_sql([str(i) for i in ids], common, fks, set(common)):
        await session.execute(text(statement))
    session.expunge_all()  # later reads see the database, not the identity map


async def documents_of(session: AsyncSession, base_id: uuid.UUID) -> list[str]:
    rows = await session.execute(
        select(Document.filename)
        .where(Document.knowledge_base_id == base_id)
        .order_by(Document.filename)
    )
    return list(rows.scalars())


async def test_a_changed_base_is_put_back(session: AsyncSession):
    base = await make_base(session)
    other = await make_base(session)
    kept = await add_document(session, base, "a.txt", restricted=True)
    gone = await add_document(session, base, "b.txt")
    await add_document(session, other, "other.txt")
    chunk_ids = set((await session.execute(select(DocumentChunk.id))).scalars())
    common = await take_backup(session, [base.id])

    # Since the backup: renamed, a document deleted, another added, the other base edited.
    await session.execute(
        text(f"UPDATE knowledge_bases SET display_name = 'Renamed' WHERE id = '{base.id}'")
    )
    await session.delete(gone)
    await add_document(session, base, "c.txt")
    await add_document(session, other, "other-2.txt")
    await session.flush()

    await apply(session, [base.id], common)

    restored = await session.get(KnowledgeBase, base.id)
    assert restored is not None and restored.display_name == "Policies"
    assert await documents_of(session, base.id) == ["a.txt", "b.txt"]
    # Every other base is as it was left.
    assert await documents_of(session, other.id) == ["other-2.txt", "other.txt"]
    chunks = (
        (await session.execute(select(DocumentChunk).where(DocumentChunk.document_id == gone.id)))
        .scalars()
        .all()
    )
    assert sorted(c.ordinal for c in chunks) == [0, 1]
    assert not {c.id for c in chunks} & chunk_ids  # fresh ids, never a clash
    access = (
        (
            await session.execute(
                select(DocumentAccess).where(DocumentAccess.lineage_id == kept.lineage_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(access) == 1


async def test_a_deleted_base_comes_back_with_its_id(session: AsyncSession):
    base = await make_base(session)
    await add_document(session, base, "a.txt")
    common = await take_backup(session, [base.id])
    await session.delete(base)
    await session.flush()
    assert await session.get(KnowledgeBase, base.id) is None

    await apply(session, [base.id], common)
    assert await documents_of(session, base.id) == ["a.txt"]


async def test_a_deleted_uploader_is_cleared(session: AsyncSession, superuser):
    base = await make_base(session)
    document = await add_document(session, base, "a.txt")
    document.uploaded_by = superuser.id
    await session.flush()
    common = await take_backup(session, [base.id])
    await session.execute(text(f"UPDATE stage_documents SET uploaded_by = '{uuid.uuid4()}'"))

    await apply(session, [base.id], common)
    restored = await session.get(Document, document.id)
    assert restored is not None and restored.uploaded_by is None


async def test_a_missing_team_refuses_everything(session: AsyncSession):
    team = Team(name=f"t-{uuid.uuid4().hex[:8]}", slug=f"t-{uuid.uuid4().hex[:8]}")
    session.add(team)
    await session.flush()
    base = await make_base(session, team_id=team.id)
    await add_document(session, base, "a.txt")
    common = await take_backup(session, [base.id])
    await session.execute(text(f"UPDATE stage_knowledge_bases SET team_id = '{uuid.uuid4()}'"))
    await add_document(session, base, "since.txt")

    with pytest.raises(DBAPIError, match="SCOPED-REFUSED"):
        async with session.begin_nested():
            await apply(session, [base.id], common)
    assert await documents_of(session, base.id) == ["a.txt", "since.txt"]


async def test_a_name_taken_since_refuses(session: AsyncSession):
    base = await make_base(session)
    common = await take_backup(session, [base.id])
    name = base.name
    await session.delete(base)
    await session.flush()
    squatter = await make_base(session)
    squatter.name = name
    await session.flush()

    with pytest.raises(DBAPIError, match="another knowledge base now has the name"):
        async with session.begin_nested():
            await apply(session, [base.id], common)
