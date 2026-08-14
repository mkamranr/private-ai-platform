"""Shared test fixtures.

Tests run **inside the backend container** (`make test`), against the real
Postgres, Valkey, Qdrant and MinIO from the Compose stack. That is a deliberate
choice over SQLite plus mocks: this platform's correctness depends on
PostgreSQL-specific behaviour — JSONB columns, partial unique indexes, ON DELETE
semantics, transactional DDL — none of which SQLite reproduces. A test suite that
passes on SQLite and fails on Postgres is worse than no suite.

Isolation comes from a per-test transaction that is always rolled back, so tests
share one schema without leaking rows into each other.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_session
from app.config.settings import Settings, get_settings
from app.core.permissions import Permission as Perm
from app.core.security import PasswordHasherService, SecretCipher, TokenService
from app.db.clients import MinioClient, QdrantClientWrapper, RedisClient
from app.db.session import Database
from app.main import create_app
from app.models.auth import Permission, Role, User
from app.services.health import HealthService
from app.services.tool_executors import build_executors

TEST_PASSWORD = "test-password-must-be-long"


# ---------------------------------------------------------------------------
# Configuration and engine
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def settings() -> Settings:
    """Configuration is synchronous and immutable, so one instance serves the run."""
    return get_settings()


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    """A fresh engine per test, disposed at teardown.

    Function-scoped deliberately. asyncpg connections are bound to the event loop
    that created them, and pytest-asyncio gives each test its own loop — so a
    session-scoped engine hands out connections belonging to a loop that has already
    closed, and every database probe fails with a confusing "connection terminating"
    error. Engine construction is cheap; debugging loop affinity is not.
    """
    instance = Database(settings)

    # Bound lock waits so a future fixture-ordering mistake fails in seconds with
    # "canceling statement due to lock timeout" instead of hanging the whole suite
    # until someone notices. Costs nothing when there is no contention.
    @event.listens_for(instance.engine.sync_engine, "connect")
    def _set_timeouts(dbapi_conn: Any, _record: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("SET lock_timeout = '5s'")
        cursor.execute("SET idle_in_transaction_session_timeout = '60s'")

    try:
        yield instance
    finally:
        await instance.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    """A session whose transaction is always rolled back.

    The outer transaction is never committed, so anything a test writes — including
    audit rows — disappears at teardown. ``join_transaction_mode="create_savepoint"``
    lets application code call ``commit()`` normally: those commits release a
    savepoint inside this transaction rather than persisting, which means the code
    under test needs no awareness of being tested.
    """
    async with database.engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with factory() as db_session:
            try:
                yield db_session
            finally:
                await transaction.rollback()


# ---------------------------------------------------------------------------
# Application under test
# ---------------------------------------------------------------------------
@pytest.fixture
async def app(settings: Settings, database: Database, session: AsyncSession):
    """The FastAPI app with its session dependency bound to the test transaction.

    ``app.state`` is populated by hand rather than by running the lifespan: the
    lifespan would build a second engine with its own connection pool, and writes
    made through it would sit outside this test's transaction and survive rollback.
    """
    application = create_app(settings)

    application.state.database = database
    application.state.password_hasher = PasswordHasherService(settings.security)
    application.state.token_service = TokenService(settings.auth)
    # Node agent tokens are stored Fernet-encrypted (Phase 1), so the cipher must be
    # present for anything touching nodes.
    application.state.secret_cipher = SecretCipher(settings.security)
    # The tool executor table (M12), as the lifespan would build it. Tests that exercise
    # the §10 pipeline substitute their own executors; this is what everything else needs
    # simply to have the dependency resolve.
    application.state.tool_executors = build_executors(application.state.secret_cipher)

    # Real clients against the live Compose stack, so tests exercise the genuine code
    # paths rather than a stub. The gateway's rate limiter needs Redis specifically.
    redis_client = RedisClient(settings)
    application.state.redis = redis_client
    # Qdrant and MinIO, as the lifespan would build them. Knowledge and memory (Phase 5)
    # resolve them from app.state, so they must be present even for tests that never touch
    # a vector — the dependency is constructed before the route body runs.
    qdrant_client = QdrantClientWrapper(settings)
    application.state.qdrant = qdrant_client
    application.state.minio = MinioClient(settings)
    application.state.health_service = HealthService(
        settings=settings,
        engine=database.engine,
        redis=redis_client,
        qdrant=qdrant_client,
        minio=application.state.minio,
    )

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = _override_session
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    """HTTP client speaking directly to the ASGI app — no network, no live server."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"user-agent": "pytest"},
    ) as http_client:
        yield http_client


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
async def _make_user(
    session: AsyncSession,
    settings: Settings,
    *,
    username: str,
    permissions: list[str],
    is_superuser: bool = False,
    is_active: bool = True,
) -> User:
    """Create a user holding exactly ``permissions`` via a purpose-built role."""
    hasher = PasswordHasherService(settings.security)
    suffix = uuid.uuid4().hex[:8]

    granted: list[Permission] = []
    for name in permissions:
        resource, _, action = name.partition(".")
        permission = Permission(
            name=f"{name}.{suffix}",  # unique: the real catalogue may already hold it
            resource=f"{resource}{suffix}",
            action=action,
            description="test permission",
        )
        session.add(permission)
        granted.append(permission)

    role = Role(name=f"TEST_ROLE_{suffix}", description="test role", permissions=granted)
    session.add(role)

    user = User(
        username=f"{username}-{suffix}",
        email=f"{username}-{suffix}@test.local",
        hashed_password=hasher.hash(TEST_PASSWORD),
        is_active=is_active,
        is_superuser=is_superuser,
        roles=[role],
    )
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
async def superuser(session: AsyncSession, settings: Settings) -> User:
    return await _make_user(session, settings, username="root", permissions=[], is_superuser=True)


@pytest.fixture
async def privileged_user(session: AsyncSession, settings: Settings) -> User:
    """Holds ``user.view`` — enough to read the user list, nothing more."""
    user = await _make_user(session, settings, username="viewer", permissions=[])
    # Reuse the seeded catalogue row rather than inserting one. `make seed` has
    # already created `user.view`, and permissions carry UNIQUE(name) plus
    # UNIQUE(resource, action) — inserting a second copy raises IntegrityError.
    real = (
        await session.execute(select(Permission).where(Permission.name == Perm.USER_VIEW))
    ).scalar_one_or_none()
    if real is None:  # pragma: no cover — only when the database is unseeded
        real = Permission(
            name=Perm.USER_VIEW, resource="user", action="view", description="View users"
        )
        session.add(real)
    user.roles[0].permissions.append(real)
    await session.flush()
    return user


@pytest.fixture
async def unprivileged_user(session: AsyncSession, settings: Settings) -> User:
    """Authenticates successfully but holds no permissions at all."""
    return await _make_user(session, settings, username="nobody", permissions=[])


@pytest.fixture
async def disabled_user(session: AsyncSession, settings: Settings) -> User:
    return await _make_user(session, settings, username="disabled", permissions=[], is_active=False)


#: Prefix marking rows that live outside a test transaction and need sweeping.
COMMITTED_FIXTURE_PREFIX = "committed-"


@pytest.fixture
async def committed_user(
    database: Database, settings: Settings
) -> tuple[User, async_sessionmaker[AsyncSession]]:
    """A user that really exists in the database, plus a factory to verify with.

    Needed by the audit-durability tests. ``AuditService.record_independent``
    commits in its own transaction, and the row it writes carries a foreign key to
    ``users.id``. A user created inside the test's rolled-back transaction is
    invisible to that separate transaction, so the insert would fail the FK check —
    and ``record_independent`` deliberately swallows its own errors, so the row
    would simply be missing and the test would misreport a product bug.

    Cleanup is deferred to the session-scoped :func:`_sweep_committed_fixtures`
    rather than done here. Per-test teardown deadlocks: a login updates
    ``users.last_login_at`` inside the still-open test transaction, holding a row
    lock, and fixture teardown order runs this cleanup *before* that transaction
    rolls back — so a ``DELETE FROM users`` blocks until the statement timeout.
    """
    hasher = PasswordHasherService(settings.security)
    suffix = uuid.uuid4().hex[:8]

    async with database.sessionmaker() as setup:
        user = User(
            username=f"{COMMITTED_FIXTURE_PREFIX}{suffix}",
            email=f"{COMMITTED_FIXTURE_PREFIX}{suffix}@test.local",
            hashed_password=hasher.hash(TEST_PASSWORD),
            is_active=True,
            is_superuser=False,
        )
        setup.add(user)
        await setup.commit()
        await setup.refresh(user)

    return user, database.sessionmaker


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def _sweep_committed_fixtures(settings: Settings) -> AsyncIterator[None]:
    """Remove committed fixture rows once, after the whole session.

    ``loop_scope="session"`` is required: a session-scoped async fixture running on
    the default function-scoped loop would be torn down on a loop that has already
    closed. It builds and disposes its own engine, so it shares nothing with the
    per-test engines.

    Runs after every test transaction has rolled back, so there are no row locks
    left to contend with. Sweeps before as well as after, so a previously
    interrupted run cannot leave rows that break the next one.
    """
    from app.models.audit import AuditLog
    from app.models.infrastructure import Node
    from app.models.models_registry import (
        ApiClient,
        ApiKey,
        Model,
        ModelDeployment,
        UsageRecord,
    )

    committed = f"{COMMITTED_FIXTURE_PREFIX}%"

    async def sweep() -> None:
        db = Database(settings)
        try:
            async with db.sessionmaker() as session:
                await session.execute(
                    AuditLog.__table__.delete().where(AuditLog.username.like(committed))
                )
                await session.execute(
                    AuditLog.__table__.delete().where(AuditLog.username.like("ghost-probe-%"))
                )
                # Child rows first: these carry real foreign keys, and unlike the ORM
                # a bulk DELETE performs no cascade of its own.
                await session.execute(
                    UsageRecord.__table__.delete().where(
                        UsageRecord.api_key_id.in_(
                            select(ApiKey.id).join(ApiClient).where(ApiClient.name.like(committed))
                        )
                    )
                )
                await session.execute(
                    ApiKey.__table__.delete().where(
                        ApiKey.client_id.in_(
                            select(ApiClient.id).where(ApiClient.name.like(committed))
                        )
                    )
                )
                await session.execute(
                    ApiClient.__table__.delete().where(ApiClient.name.like(committed))
                )
                await session.execute(
                    ModelDeployment.__table__.delete().where(
                        ModelDeployment.model_id.in_(
                            select(Model.id).where(Model.name.like(committed))
                        )
                    )
                )
                await session.execute(Model.__table__.delete().where(Model.name.like(committed)))
                await session.execute(Node.__table__.delete().where(Node.name.like(committed)))
                await session.execute(User.__table__.delete().where(User.username.like(committed)))
                await session.commit()
        finally:
            await db.dispose()

    await sweep()
    yield
    await sweep()


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
@pytest.fixture
def tokens(settings: Settings) -> TokenService:
    return TokenService(settings.auth)


def auth_header(tokens: TokenService, user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens.create_access_token(str(user.id))}"}
