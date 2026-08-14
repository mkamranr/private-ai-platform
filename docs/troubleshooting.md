# Troubleshooting

## Start here

```bash
curl -s localhost:8080/api/v1/health | python3 -m json.tool   # which dependency is down
make logs
docker compose --profile core ps
```

`/health` reports each dependency separately with latency, so start there rather than
guessing.

## Known cases

**A service never becomes healthy.** `docker compose logs <service>`. Postgres uses
`pg_isready -h 127.0.0.1` to force a TCP check — without `-h` it succeeds on the unix
socket while still refusing network connections.

**`make up` fails on a volume mount.** `PLATFORM_DATA_ROOT` defaults to `./data`.
Docker Desktop does not share `/data` by default, so an absolute `/data/ai-platform`
fails on a developer machine.

**Migration autogenerate wants to DROP a table.** The model is not re-exported in
`app/models/__init__.py`, so it is absent from `Base.metadata`. Never apply such a
migration.

**The agents, skills and tools are all gone.** A gate ran. Eight of the ten prove
migrations reverse cleanly with `alembic downgrade base`, which drops every table — so
each one re-seeds roles, permissions and the admin afterwards, and now re-imports the
shipped catalogue with it. Restore it by hand with `make definitions-import`; it is
idempotent, and reports `unchanged` for tools that survived.

The giveaway that this is what happened, rather than a wipe of the whole volume: `users`,
`roles` and `permissions` are populated (each gate seeds them) and `nodes`/`gpus` come
back on their own within a poll interval (the node agent re-reports), while `models` and
`model_deployments` sit at 0 with model containers still running — orphans the gates
clear with `reconcile --remove`. If `data/postgres/PG_VERSION` is older than the problem,
the volume was never deleted and the round-trip is the only thing that could have emptied
it.

**LangGraph checkpoint tables appear in a migration.** They should be excluded by
`alembic/env.py::_include_object`. Applying it would destroy suspended agent runs.

**Tests hang.** Almost certainly lock contention between a test transaction and a
fixture teardown. `conftest.py` sets `lock_timeout = 5s` so this fails fast; diagnose
with:

```sql
SELECT pid, wait_event_type, state, left(query,60) FROM pg_stat_activity
WHERE datname='ai_platform' AND state <> 'idle';
```

**Streaming arrives all at once.** Check `proxy_buffering off` in
`docker/nginx/nginx.conf`, that the provider used `chat_stream()`, and that the route
returns a `StreamingResponse`.

**A node shows OFFLINE but the host is up.** Check the agent directly:
`curl -s http://<node>:9100/health`. If that works, the control plane cannot reach it —
check `agent_url`, and check TLS: a certificate error is reported distinctly from
unreachability, so read the `status_detail` on the node.

**The node installer says the agent image is not present.** Copy the offline bundle to
that host and re-run with `--bundle <dir>`. The platform never pulls images (Rule 4), so
there is no path that fetches it — and the host needs the bundle for model images anyway.

**An enrolment token is rejected.** The endpoint answers every rejection identically on
purpose, so the reason is in the console: **Nodes → Awaiting enrolment** shows the status
and the last error. Expired, already used, revoked and out-of-attempts all look the same to
the caller. Revoke and issue a fresh one.

**Enrolment is refused with "port N is not allowed".** `ENROLLMENT__ALLOWED_AGENT_PORTS`
defaults to `[9100]`. If the node genuinely publishes another port, add it there — the
allowlist is the main control limiting what an advertised address can reach.

**Enrolment is refused with a `node_name` mismatch.** The agent is running with a different
`NODE_AGENT_NODE_NAME` than the enrolment was issued for — usually the script was run on a
host that already hosts another node's agent. The message names the value to set.

**A node enrolled but shows OFFLINE minutes later.** Enrolment proved the control plane
could reach it once. If it has since gone quiet, the agent stopped or a firewall closed —
check `docker compose -p ai-platform-node -f /etc/ai-platform/docker-compose.node.yml logs`.

**The agent is gone after a reboot.** `restart: unless-stopped` only helps if Docker itself
starts at boot. `systemctl enable docker`. The installer warns about this at preflight.

**Port 9100 is already in use on the host.** The control plane's own Compose file runs an
agent for the local node. Install this one on another port with `--port`, and add that port
to `ENROLLMENT__ALLOWED_AGENT_PORTS`.

**Every node suddenly fails to authenticate.** `SECURITY__ENCRYPTION_KEY` has changed.
Agent tokens are stored encrypted under it; the platform says so explicitly rather than
letting it look like a fleet-wide token rejection. Restore the old key or re-register.

**The platform refuses to stop a container (409).** Working as designed. The node agent
refuses control of containers without the `ai-platform.managed` label — the guard that
stops the platform stopping its own Postgres. Only containers the platform created are
controllable.

**GPUs show as `SYNTHETIC`.** The node's probe resolved to `fake`, meaning no NVIDIA
driver was found. Correct on a developer machine; on a real GPU host set
`NODE_AGENT_GPU_PROBE=nvidia_smi` explicitly so a broken driver fails loudly instead of
silently reporting fabricated telemetry.

**`make lint` fails on air-gap.** Run `python3 scripts/check_airgap.py --verbose`. Do
not suppress: an unbundleable dependency added now is expensive to unpick at Phase 8.

## Observability (M19)

**Grafana is empty.** Check `GET /api/v1/monitoring/overview` first: it reports whether
the exporters are switched on. The collectors running and the platform exporting are two
separate switches, and `TRACING__ENABLED` defaults to false.

**Prometheus shows the backend as DOWN while `/metrics` returns 200.** Almost certainly
an exposition mismatch: `curl` is happy, Prometheus is not. Read the target's error at
`http://prometheus:9090/api/v1/targets` — `data does not end with # EOF` means the
response declared the OpenMetrics content type while carrying the text format.

**A setting in `.env` seems to be ignored after a restart.** The backend takes `.env`
through `env_file`, which a shell variable cannot override. `TRACING__ENABLED` and
`LANGFUSE__ENABLED` are also declared under `environment:` precisely so they *can* be
overridden per run; everything else must be edited in `.env`, followed by
`make restart-backend`.

**Loki has logs but no `trace_id`.** Tracing is off, or logs are not JSON. Alloy parses
JSON to find the field, and `docker-compose.dev.yml` sets `LOGGING__JSON=false` for
readable local output. Production sets it true.

**Tempo is unhealthy in `docker compose ps`.** It has no healthcheck — the image is
distroless, with no shell and no HTTP client to run one. Check it through Grafana's
datasource test instead.

## On an air-gapped target

**Compose tries to pull, on a host with no network.** Something dropped the air-gap
override. `docker compose config | grep image` should show `…:bundled` tags, never
`@sha256:`. Two causes: the `COMPOSE_FILE=docker-compose.yml:docker-compose.airgap.yml`
line is missing from `.env`, or a command passed a single explicit `-f docker-compose.yml`
— one explicit `-f` replaces the whole list. Re-running `install.sh` restores both the
override and the line. See [airgap.md](airgap.md).

**`docker images` shows the bundled images as `<none>`.** They loaded but were never
named. `docker load` restores content and a config ID, never a tag, and a digest cannot
be re-applied as one. `install.sh` names them from the manifest's `image_id`; re-run it.

**The installer refuses the bundle.** Both refusals are deliberate and happen before
anything is written. *"checksum mismatch"* means the media is damaged — recopy it, do not
retry. *"format version 1"* means the bundle predates the image IDs the installer needs;
rebuild it on the build machine.

**A rollback fails on `docker tag`.** The previous image was pruned since the upgrade.
Rollback re-names images already on disk; it cannot recreate one. Reinstall from the
older bundle's media instead.
