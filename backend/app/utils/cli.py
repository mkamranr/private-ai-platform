"""Platform CLI (M02, M25).

Operational entrypoints that must work without the HTTP API — an air-gapped
installer runs these before anyone can reach a browser.

    python -m app.utils.cli seed        # roles, permissions, bootstrap admin, bucket
    python -m app.utils.cli chat-key    # provision Open WebUI's gateway credentials
    python -m app.utils.cli reconcile   # find model containers no deployment claims
    python -m app.utils.cli mcp-import  # register MCP servers from mcp/manifests/
    python -m app.utils.cli definitions-import  # register the shipped agents/skills/tools
    python -m app.utils.cli ollama-import  # register models a running Ollama serves
    python -m app.utils.cli external-import  # register an OpenAI-compatible endpoint
                                            #   (hosted provider, or --endpoint for a
                                            #    local engine such as llama.cpp)
    python -m app.utils.cli check       # probe every dependency, non-zero on failure
    python -m app.utils.cli config      # print effective config with secrets masked

Extended in Phase 6 with ``backup create|verify|restore`` (M25).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import secrets
import sys

import asyncpg

from app.config.settings import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.security import PasswordHasherService, SecretCipher, generate_api_key
from app.db.clients import MinioClient, QdrantClientWrapper, RedisClient
from app.db.session import Database
from app.models.models_registry import ApiClient, ApiKey
from app.repositories.models_registry import ApiClientRepository, ApiKeyRepository
from app.repositories.user import PermissionRepository, RoleRepository, UserRepository
from app.services.health import HealthService
from app.services.seed import SeedService

log = get_logger("app.cli")

_EXIT_OK = 0
_EXIT_FAILURE = 1
_EXIT_USAGE = 2


async def _seed() -> int:
    """Seed RBAC and the bootstrap admin, and ensure the MinIO bucket exists."""
    settings = get_settings()
    database = Database(settings)
    minio = MinioClient(settings)
    try:
        async with database.sessionmaker() as session:
            service = SeedService(
                UserRepository(session),
                RoleRepository(session),
                PermissionRepository(session),
                PasswordHasherService(settings.security),
            )
            password = settings.auth.bootstrap_admin_password
            result = await service.run(
                admin_username=settings.auth.bootstrap_admin_username,
                admin_email=settings.auth.bootstrap_admin_email,
                admin_password=password.get_secret_value() if password else None,
            )
            await session.commit()

        # Idempotent, and needs MinIO reachable — so it happens here rather than at
        # application startup, where a MinIO outage would block the whole platform.
        try:
            created = await minio.ensure_bucket()
        except Exception as exc:
            log.warning(
                "minio_bucket_skipped",
                reason=type(exc).__name__,
                note="Re-run `make seed` once MinIO is reachable.",
            )
            created = False

        created_databases = await _ensure_companion_databases(settings)

        print(
            f"Seed complete: "
            f"{result.permissions_created} permissions created, "
            f"{result.roles_created} roles created, "
            f"{result.role_grants_updated} role grants reconciled, "
            f"admin_created={result.admin_created}, "
            f"bucket_created={created}, "
            f"companion_databases={created_databases or 'none needed'}"
        )
        if result.admin_created:
            print(
                f"\n  Bootstrap admin '{result.admin_username}' created with the password "
                f"from AUTH__BOOTSTRAP_ADMIN_PASSWORD.\n"
                f"  Change it immediately after first login.\n"
            )
        return _EXIT_OK
    finally:
        await database.dispose()
        await minio.close()


async def _ensure_companion_databases(settings: Settings) -> list[str]:
    """Create the databases companion components keep their own schema in (M17, M19).

    Idempotent, and deliberately here rather than in a Postgres init script: those run
    only when the data directory is first initialised, so an existing install would never
    get a database added in a later release — and the failure would surface as a
    component that will not start, a long way from its cause.

    A failure is logged, not raised. Seeding the platform's own RBAC is what `make seed`
    is for, and it must not be undone because an optional component's database could not
    be created.
    """
    created: list[str] = []
    wanted = settings.database.companion_databases
    if not wanted:
        return created

    try:
        # CREATE DATABASE cannot run inside a transaction, so this connects with the raw
        # driver rather than through the ORM session.
        connection = await asyncpg.connect(
            host=settings.database.host,
            port=settings.database.port,
            user=settings.database.user,
            password=settings.database.password.get_secret_value(),
            database=settings.database.name,
            timeout=settings.database.connect_timeout_seconds,
        )
    except Exception as exc:
        log.warning("companion_databases_skipped", reason=type(exc).__name__)
        return created

    try:
        for name in wanted:
            exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
            if exists:
                continue
            # The name comes from configuration, not a request, and CREATE DATABASE takes
            # no parameters — so it is quoted as an identifier rather than interpolated.
            await connection.execute(f'CREATE DATABASE "{name}"')
            created.append(name)
            log.info("companion_database_created", name=name)
    except Exception as exc:
        log.warning("companion_database_failed", reason=type(exc).__name__, error=str(exc)[:200])
    finally:
        await connection.close()

    return created


async def _chat_key() -> int:
    """Provision the credentials Open WebUI needs to consume the gateway (M17).

    Emits shell-assignable lines on stdout so `make chat-key` can merge them into .env;
    everything else goes to the log, on stderr.

    Re-runnable. The signing secret is Fernet-encrypted rather than hashed, so an
    existing one is returned as-is and Open WebUI keeps working across a re-run. The API
    key cannot be — only its hash is stored — so a re-run mints a fresh key and revokes
    the client's previous ones, which makes this the rotation command as well as the
    provisioning one.
    """
    settings = get_settings()
    database = Database(settings)
    try:
        async with database.sessionmaker() as session:
            cipher = SecretCipher(settings.security)
            clients = ApiClientRepository(session)
            keys = ApiKeyRepository(session)

            admin = await UserRepository(session).get_by_username(
                settings.auth.bootstrap_admin_username
            )
            if admin is None:
                log.error("chat_key_no_admin", note="Run `make seed` first.")
                return _EXIT_FAILURE

            name = settings.gateway.chat_client_name
            client = await clients.get_by_name(name)
            if client is None:
                client = ApiClient(
                    name=name,
                    description="Open WebUI — the platform's own chat frontend (M17).",
                    owner_id=admin.id,
                    trusted_identity_headers=True,
                )
                session.add(client)
                await session.flush()
                log.info("chat_client_created", name=name)

            # Repaired rather than assumed: a client created through the API without
            # these would forward identities that the gateway then silently ignored.
            client.trusted_identity_headers = True
            if not client.identity_jwt_secret_encrypted:
                client.identity_jwt_secret_encrypted = cipher.encrypt(secrets.token_urlsafe(48))
            jwt_secret = cipher.decrypt(client.identity_jwt_secret_encrypted)

            revoked = 0
            for existing in await keys.list_for_client(client.id):
                if existing.revoked_at is None:
                    existing.revoked_at = dt.datetime.now(dt.UTC)
                    revoked += 1

            full_key, prefix, key_hash = generate_api_key()
            session.add(
                ApiKey(
                    client_id=client.id,
                    name="open-webui",
                    prefix=prefix,
                    key_hash=key_hash,
                    # Generous: this one key carries every person's chat traffic, so the
                    # per-key limit is a fleet limit here, not a per-user one.
                    rate_limit_per_minute=settings.gateway.chat_rate_limit_per_minute,
                )
            )
            await session.commit()

        log.info("chat_key_provisioned", client=name, previous_keys_revoked=revoked)
        print(f"OPEN_WEBUI__GATEWAY_API_KEY={full_key}")
        print(f"OPEN_WEBUI__IDENTITY_JWT_SECRET={jwt_secret}")
        return _EXIT_OK
    finally:
        await database.dispose()


async def _definitions_import() -> int:
    """Import the shipped agent, skill and tool definitions (M10-M12).

    A CLI command as well as an endpoint for the same reason `mcp-import` is one: this
    runs during installation, before anyone has opened a browser. An air-gapped install
    should come up with its agents already catalogued rather than waiting for someone to
    mint a token and POST.
    """
    settings = get_settings()
    database = Database(settings)
    try:
        async with database.sessionmaker() as session:
            from app.core.security import SecretCipher
            from app.repositories.agents import (
                AgentRepository,
                AgentVersionRepository,
                SkillRepository,
                ToolRepository,
            )
            from app.repositories.audit import AuditRepository
            from app.services.agent_registry import (
                AgentRegistryService,
                SkillRegistryService,
                ToolRegistryService,
            )
            from app.services.audit import AuditService
            from app.services.definitions import DefinitionImporter
            from app.services.tool_executors import build_executors

            admin = await UserRepository(session).get_by_username(
                settings.auth.bootstrap_admin_username
            )
            if admin is None:
                log.error("definitions_import_no_admin", note="Run `make seed` first.")
                return _EXIT_FAILURE

            audit = AuditService(AuditRepository(session))
            cipher = SecretCipher(settings.security)
            importer = DefinitionImporter(
                settings,
                ToolRepository(session),
                SkillRepository(session),
                AgentRepository(session),
                ToolRegistryService(
                    settings,
                    ToolRepository(session),
                    audit,
                    cipher,
                    build_executors(cipher, database.sessionmaker),
                ),
                SkillRegistryService(SkillRepository(session)),
                AgentRegistryService(
                    settings,
                    AgentRepository(session),
                    AgentVersionRepository(session),
                    ToolRepository(session),
                    SkillRepository(session),
                    audit,
                ),
            )
            results = await importer.import_all(actor=admin)
            await session.commit()

        if not results:
            print("No definitions found. Add one to tools/, skills/ or agents/ and re-run.")
            return _EXIT_OK

        for result in results:
            detail = f" — {result.detail}" if result.detail else ""
            print(f"  {result.kind:6} {result.name:32} {result.action}{detail}")
        return _EXIT_OK
    finally:
        await database.dispose()


async def _mcp_import() -> int:
    """Register every MCP server manifest and discover its tools.

    A CLI command as well as an endpoint because this runs during installation, before
    anyone has opened a browser — an air-gapped install should come up with its MCP servers
    already catalogued.
    """
    settings = get_settings()
    database = Database(settings)
    try:
        async with database.sessionmaker() as session:
            from app.repositories.agents import McpServerRepository, ToolRepository
            from app.repositories.audit import AuditRepository
            from app.services.agent_registry import McpRegistryService
            from app.services.audit import AuditService

            admin = await UserRepository(session).get_by_username(
                settings.auth.bootstrap_admin_username
            )
            if admin is None:
                log.error("mcp_import_no_admin", note="Run `make seed` first.")
                return _EXIT_FAILURE

            service = McpRegistryService(
                settings,
                McpServerRepository(session),
                ToolRepository(session),
                AuditService(AuditRepository(session)),
                SecretCipher(settings.security),
            )
            results = await service.import_manifests(actor=admin)
            await session.commit()

        if not results:
            print("No MCP manifests found. Add one to mcp/manifests/ and re-run.")
            return _EXIT_OK

        for result in results:
            detail = f" — {result.detail}" if result.detail else ""
            print(
                f"  {result.server_name:20} {result.found:2} offered, "
                f"{result.created:2} new, {result.updated:2} refreshed{detail}"
            )
        print(
            "\nDiscovered tools are DISABLED at HIGH risk. Review each one in the admin UI "
            "before assigning it to an agent."
        )
        return _EXIT_OK
    finally:
        await database.dispose()


async def _reconcile(remove: bool) -> int:
    """Report — or remove — model containers no deployment claims.

    Available as a CLI command as well as an endpoint because the situation it fixes often
    coincides with a control plane that is unhappy: after a restore, or an interrupted
    upgrade, an operator wants this before the API is trusted.
    """
    settings = get_settings()
    database = Database(settings)
    try:
        async with database.sessionmaker() as session:
            from app.repositories.agents import ToolExecutionRepository  # noqa: F401
            from app.repositories.audit import AuditRepository
            from app.repositories.infrastructure import (
                ContainerRepository,
                GpuAllocationRepository,
                GpuHealthEventRepository,
                GpuMetricRepository,
                GpuProcessRepository,
                GpuRepository,
                NodeRepository,
            )
            from app.repositories.models_registry import (
                ModelDeploymentRepository,
                ModelRepository,
            )
            from app.services.audit import AuditService
            from app.services.deployment import DeploymentService
            from app.services.infrastructure import GpuService, NodeService
            from app.workers.deployments import _compute_backend_factory

            cipher = SecretCipher(settings.security)
            audit = AuditService(AuditRepository(session))
            nodes = NodeService(
                settings,
                NodeRepository(session),
                GpuRepository(session),
                GpuMetricRepository(session),
                GpuProcessRepository(session),
                GpuHealthEventRepository(session),
                ContainerRepository(session),
                audit,
                cipher,
            )
            service = DeploymentService(
                settings,
                ModelRepository(session),
                ModelDeploymentRepository(session),
                NodeRepository(session),
                GpuService(
                    settings,
                    GpuRepository(session),
                    GpuMetricRepository(session),
                    GpuProcessRepository(session),
                    GpuHealthEventRepository(session),
                    GpuAllocationRepository(session),
                ),
                audit,
                _compute_backend_factory(settings, nodes),
            )
            orphans = await service.reconcile_orphans(remove=remove)
            unscanned = list(service.last_unscanned_nodes)
            await session.commit()

        if unscanned:
            # An empty result from a scan that reached nothing is not a clean platform,
            # and saying so would send an operator away satisfied while orphaned
            # containers keep holding GPUs. Non-zero exit, so a script notices too.
            print(f"Could not scan {len(unscanned)} node(s): {', '.join(unscanned)}")
            print("The result below covers only the nodes that answered.")

        if not orphans:
            if unscanned:
                print("No orphans among the nodes that were scanned.")
                return _EXIT_FAILURE
            print("No orphaned model containers. Every managed container has a deployment.")
            return _EXIT_OK

        print(f"{len(orphans)} orphaned container(s):")
        for orphan in orphans:
            action = "removed" if orphan.get("removed") else "left running"
            error = f" — {orphan['error']}" if orphan.get("error") else ""
            print(
                f"  {orphan['name']:26} {orphan['container_id']:14} {orphan['state']:10} "
                f"[{action}]{error}"
            )
        if not remove:
            print("\nRe-run with `make reconcile REMOVE=1` to remove them.")
        return _EXIT_OK
    finally:
        await database.dispose()


async def _ollama_import() -> int:
    """Register whatever a running Ollama is serving (M07).

    An external runtime: the platform routes to Ollama and never starts, stops or
    schedules it. Idempotent, so this doubles as the refresh after an `ollama pull`.
    """
    settings = get_settings()
    database = Database(settings)
    async with database.sessionmaker() as session:
        from app.repositories.audit import AuditRepository
        from app.repositories.models_registry import (
            ModelAliasRepository,
            ModelDeploymentRepository,
            ModelRepository,
        )
        from app.services.audit import AuditService
        from app.services.model_registry import ModelRegistryService

        actor = await UserRepository(session).get_by_username(
            settings.auth.bootstrap_admin_username
        )
        if actor is None:
            print("No bootstrap admin to attribute this to. Run `make seed` first.")
            return _EXIT_FAILURE

        service = ModelRegistryService(
            settings,
            ModelRepository(session),
            ModelDeploymentRepository(session),
            ModelAliasRepository(session),
            AuditService(AuditRepository(session)),
        )
        try:
            results = await service.import_ollama(actor=actor)
        except Exception as exc:
            print(str(exc))
            return _EXIT_FAILURE
        await session.commit()

    if not results:
        print(
            f"Ollama at {settings.models.ollama_endpoint} is reachable but has no models.\n"
            "Pull one first, e.g. `ollama pull llama3.2`, then run this again."
        )
        return _EXIT_OK

    for row in results:
        print(f"  {row['status']:20} {row['name']:32} (ollama: {row['ollama_tag']})")
    print(
        f"\n{len(results)} model(s) catalogued. They are AVAILABLE immediately — there are "
        "no weights\nfor this platform to import, because Ollama already holds them. "
        "Deploy one to attach,\nthen point an alias at it."
    )
    return _EXIT_OK


async def _external_import(argv: list[str] | None = None) -> int:
    """Register an OpenAI-compatible endpoint the platform does not run, and alias it (M07).

    The same shape as `ollama-import`: an **external** runtime the platform routes to and
    never starts, stops or schedules. Two quite different things arrive through here, which
    is why the endpoint is an argument rather than a constant:

    * a **hosted provider** (OpenRouter, from `MODELS__EXTERNAL_*`) — takes the installation
      out of air-gapped operation and needs a bearer token. See docs/airgap.md.
    * a **local engine** on the platform network (`llama.cpp`, `make local-llm`) — no
      credential, nothing leaves the host, and `external_key_for` makes sure the hosted
      provider's key is not sent to it.

    Registers, deploys and aliases in one go, because all three are needed before anything
    can call it and doing two of the three leaves a model that resolves to nothing.
    Idempotent: re-running with a different model repoints the alias.

        external-import                                   # the configured hosted provider
        external-import --endpoint http://llamacpp:8080 --model qwen2.5-1.5b-instruct
        external-import ... --alias enterprise-chat       # which alias to repoint
    """
    settings = get_settings()

    opts: dict[str, str] = {}
    args = list(argv or [])
    for flag in ("--endpoint", "--model", "--alias", "--context"):
        if flag in args:
            index = args.index(flag)
            if index + 1 >= len(args):
                print(f"{flag} needs a value.")
                return _EXIT_FAILURE
            opts[flag.removeprefix("--")] = args[index + 1]

    endpoint = (opts.get("endpoint") or settings.models.external_endpoint).rstrip("/")
    model_id = (opts.get("model") or settings.models.external_model).strip()
    alias = opts.get("alias") or "enterprise-chat"
    # What the engine was actually started with. Registered so the platform advertises the
    # truth: a caller — Open WebUI above all — sizes its prompt to the context the model
    # list reports, and a platform claiming more than the engine serves produces a 400 from
    # the engine about a request the platform said was fine.
    context_length = int(opts["context"]) if opts.get("context") else None

    # The credential is required only for the endpoint it belongs to. A local engine needs
    # none, and demanding one would make `make local-llm` refuse to work on a host that has
    # no hosted-provider account at all.
    hosted = endpoint == settings.models.external_endpoint.rstrip("/")
    key = settings.models.external_api_key.get_secret_value()

    # Refused rather than half-done. Registering a model whose endpoint cannot answer
    # produces a deployment that reports RUNNING and 401s on first use, which looks like a
    # platform fault rather than an unset variable.
    missing = [
        name for name, value in (("--endpoint", endpoint), ("--model", model_id)) if not value
    ]
    if hosted and not key:
        missing.append("MODELS__EXTERNAL_API_KEY")
    if missing:
        print(
            "Not configured. Provide the following and run this again:\n  "
            + "\n  ".join(missing)
            + (
                "\n\nA hosted provider is opt-in because it sends every prompt off this host."
                if hosted
                else ""
            )
        )
        return _EXIT_FAILURE
    # The provider's id is not a legal platform model name — `vendor/model:free` has a
    # slash and a colon. Sanitised for the platform, kept verbatim in storage_path so
    # `served_model_name` sends back exactly what the provider expects.
    name = model_id.replace(":", "-").replace("/", "-").replace(".", "-").lower()

    database = Database(settings)
    async with database.sessionmaker() as session:
        from app.models.models_registry import ModelType
        from app.repositories.audit import AuditRepository
        from app.repositories.infrastructure import (
            ContainerRepository,
            GpuAllocationRepository,
            GpuHealthEventRepository,
            GpuMetricRepository,
            GpuProcessRepository,
            GpuRepository,
            NodeRepository,
        )
        from app.repositories.models_registry import (
            ModelAliasRepository,
            ModelDeploymentRepository,
            ModelRepository,
        )
        from app.services.audit import AuditService
        from app.services.deployment import DeploymentRequestSpec, DeploymentService
        from app.services.infrastructure import GpuService, NodeService
        from app.services.model_registry import ModelRegistryService
        from app.workers.deployments import _compute_backend_factory

        actor = await UserRepository(session).get_by_username(
            settings.auth.bootstrap_admin_username
        )
        if actor is None:
            print("No bootstrap admin to attribute this to. Run `make seed` first.")
            return _EXIT_FAILURE

        audit = AuditService(AuditRepository(session))
        models_repo = ModelRepository(session)
        aliases_repo = ModelAliasRepository(session)
        registry = ModelRegistryService(
            settings,
            models_repo,
            ModelDeploymentRepository(session),
            aliases_repo,
            audit,
        )

        model = await models_repo.get_by_name(name)
        if model is None:
            model = await registry.register_model(
                name=name,
                display_name=model_id,
                model_type=ModelType.LLM,
                storage_path=f"external://{model_id}",
                runtime="external",
                endpoint_url=endpoint,
                version="1.0",
                context_length=context_length,
                # Nothing to reserve: the weights are on someone else's hardware.
                min_gpu_count=0,
                description=(
                    f"Served at {endpoint} ({'hosted provider' if hosted else 'local engine'})."
                ),
                metadata={"external_model": model_id, "endpoint": endpoint},
                actor=actor,
            )
            print(f"  registered           {name}")
        else:
            # Repointed, not skipped: re-running after changing the endpoint means the new
            # address rather than "no change".
            model.endpoint_url = endpoint
            model.storage_path = f"external://{model_id}"
            if context_length:
                model.context_length = context_length
            print(f"  already registered   {name}")

        await session.flush()

        cipher = SecretCipher(settings.security)
        nodes = NodeService(
            settings,
            NodeRepository(session),
            GpuRepository(session),
            GpuMetricRepository(session),
            GpuProcessRepository(session),
            GpuHealthEventRepository(session),
            ContainerRepository(session),
            audit,
            cipher,
        )
        deployments = DeploymentService(
            settings,
            models_repo,
            ModelDeploymentRepository(session),
            NodeRepository(session),
            GpuService(
                settings,
                GpuRepository(session),
                GpuMetricRepository(session),
                GpuProcessRepository(session),
                GpuHealthEventRepository(session),
                GpuAllocationRepository(session),
            ),
            audit,
            _compute_backend_factory(settings, nodes),
        )

        serving = await deployments.list_deployments(model_id=model.id, states=["RUNNING"])
        if serving:
            print(f"  already deployed     {name}")
        else:
            deployment = await deployments.request_deployment(
                DeploymentRequestSpec(model_id=model.id, runtime="external"), actor=actor
            )
            print(f"  deployed             {name} ({deployment.state})")

        existing_alias = await aliases_repo.get_by_alias(alias)
        if existing_alias is None:
            await registry.create_alias(
                alias=alias,
                model_id=model.id,
                description=f"Hosted model at {endpoint}.",
                enabled=True,
                actor=actor,
            )
            print(f"  alias created        {alias} -> {name}")
        elif str(existing_alias.model_id) != str(model.id):
            await registry.update_alias(
                existing_alias.id,
                model_id=model.id,
                description=None,
                enabled=True,
                actor=actor,
            )
            print(f"  alias repointed      {alias} -> {name}")
        else:
            print(f"  alias unchanged      {alias} -> {name}")

        await session.commit()

    print(
        f"\n{alias!r} now resolves to {model_id} at {endpoint}.\n"
        "Agents, RAG and the chat frontend all reach it through this alias — they resolve "
        "models\nthrough the gateway, so there is nothing else to point at it."
    )
    # Said only when it is true. Printing the air-gap warning for a container on this
    # host's own network would train an operator to ignore it, and it is the one warning
    # here that carries a classification decision.
    if hosted:
        print(
            "\nThis installation is no longer air-gapped: prompts, and any knowledge-base "
            "passages\nretrieved into them, leave this host."
        )
    else:
        print(f"\nNothing leaves this host: {endpoint} is on the platform's own network.")
    return _EXIT_OK


async def _check() -> int:
    """Probe every dependency. Non-zero exit if a required one is down."""
    settings = get_settings()
    database = Database(settings)
    redis = RedisClient(settings)
    qdrant = QdrantClientWrapper(settings)
    minio = MinioClient(settings)
    try:
        service = HealthService(
            settings=settings,
            engine=database.engine,
            redis=redis,
            qdrant=qdrant,
            minio=minio,
        )
        report = await service.check()
        for dep in report.dependencies:
            flag = "ok " if dep.healthy else "FAIL"
            required = "required" if dep.required else "optional"
            latency = f"{dep.latency_ms:.1f}ms" if dep.latency_ms is not None else "-"
            detail = f"  ({dep.detail})" if dep.detail else ""
            print(f"  [{flag}] {dep.name:<10} {required:<8} {latency:>9}{detail}")
        print(f"\nready={report.ready}")
        if not report.ready:
            print(f"failing required: {', '.join(report.failing_required)}")
        return _EXIT_OK if report.ready else _EXIT_FAILURE
    finally:
        await database.dispose()
        await redis.close()
        await qdrant.close()
        await minio.close()


def _config() -> int:
    """Print the effective configuration with every secret masked.

    The point of this command is diagnosing precedence — which layer supplied a
    value — so it must never be capable of printing a secret. Pydantic's
    ``SecretStr`` renders as ``**********``; the mask is the type's job, not the
    caller's discipline.
    """
    settings = get_settings()
    print(settings.model_dump_json(indent=2))
    return _EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(__doc__)
        return _EXIT_USAGE

    configure_logging(get_settings().logging)
    command = args[0]

    if command == "seed":
        return asyncio.run(_seed())
    if command == "mcp-import":
        return asyncio.run(_mcp_import())
    if command == "definitions-import":
        return asyncio.run(_definitions_import())
    if command == "reconcile":
        return asyncio.run(_reconcile(remove="--remove" in args))
    if command == "chat-key":
        return asyncio.run(_chat_key())
    if command == "ollama-import":
        return asyncio.run(_ollama_import())
    if command == "external-import":
        return asyncio.run(_external_import(args[1:]))
    if command == "check":
        return asyncio.run(_check())
    if command == "config":
        return _config()

    print(f"Unknown command: {command!r}\n")
    print(__doc__)
    return _EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
