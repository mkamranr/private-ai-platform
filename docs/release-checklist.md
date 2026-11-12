# Release checklist

This is the procedure for cutting a release. Work through it top to bottom. Every item
is something a release has got wrong before, or a hard constraint from the
[expansion plan](roadmap/Private-AI-Platform-Expansion-Plan.md) §2 that nothing else
enforces.

## 1. Version and notes

- [ ] Set the new version in `backend/app/config/settings.py` (`PlatformSettings.version`)
      and `backend/pyproject.toml`.
- [ ] Set the same version in the `${PLATFORM_VERSION:-…}` defaults in `docker-compose.yml`,
      in `scripts/backup.py`, `scripts/build_bundle.py` and `scripts/sbom.py`, in the mock
      runtime image in `settings.py`, and in `offline/install-node.sh`:
      `git grep -n "<old version>"` lists them all.
- [ ] Add a `CHANGELOG.md` entry, written for someone who runs the platform.
- [ ] If a migration, permission or environment variable was added, write
      `docs/upgrade-<version>.md`.

## 2. Code checks (development machine)

- [ ] `make check`: ruff, mypy, import-linter and every service's tests.
- [ ] `make airgap`: pinned dependencies with hashes, digest-pinned images, no runtime fetches.
- [ ] `make migrate-roundtrip` on a **scratch** database: head, then base, then head, then
      `alembic check`. Never run it against data you want to keep.
- [ ] `node --check` on every file in `frontend/admin/js/`.
- [ ] `python scripts/vendor_mcp_servers.py --dry-run`: every connector manifest passes the
      catalogue rules.
- [ ] `tests/unit/test_routing.py` passes, so every API route commits before responding.

## 3. Hard constraints

- [ ] **No remote telemetry by default.** Every third-party service in `docker-compose.yml`
      has its opt-out stated explicitly, not inherited from the image:

      | Service | Setting |
      |---|---|
      | Qdrant | `QDRANT__TELEMETRY_DISABLED` |
      | MinIO | `MINIO_UPDATE=off` |
      | Open WebUI | `OFFLINE_MODE`, `ENABLE_VERSION_UPDATE_CHECK`, `SCARF_NO_ANALYTICS`, `DO_NOT_TRACK`, `ANONYMIZED_TELEMETRY` |
      | Grafana | `GF_ANALYTICS_*` |
      | Langfuse | `TELEMETRY_ENABLED` |
      | Alloy | `--disable-reporting` |
      | Loki, Tempo | `reporting_enabled: false` |
      | Model runtimes | `compute_backend.py` sets `HF_HUB_DISABLE_TELEMETRY`, `DO_NOT_TRACK`, `VLLM_NO_USAGE_STATS` |

      A new service must join this table.
- [ ] **No secrets in source, logs or the frontend.** New settings that hold secrets are
      `SecretStr`, and they are redacted in Diagnostics and the support bundle.
- [ ] **No unrestricted Docker socket.** Only the node agent and Alloy mount it, read-only
      for Alloy. The backend never gets it.
- [ ] **Privileged changes are audited.** A new mutating route under `require_permission(...)`
      writes an audit event, apart from probes such as health checks, dry runs and
      connection tests.
- [ ] **Data is real.** No page shows a placeholder number as if it were measured.

## 4. Build (connected build machine)

- [ ] `make bundle` (and `make mcp-vendor` if connectors changed).
- [ ] `make sbom`, and the bundle's `sbom.cdx.json` and `licenses.txt` are present.
- [ ] Sign the bundle manifest with the site's key (see [operations.md](operations.md)).

## 5. Rehearse on an air-gapped host

- [ ] `make gate-phase8`: offline install, upgrade and rollback.
- [ ] Upgrade a copy of the previous release, following its upgrade guide **exactly**:
      upgrade, `alembic upgrade head`, then `make seed-rbac`. Afterwards, an ADMIN who is
      not a superuser can open every new page.
- [ ] Repeat the upgrade from the console's **Offline Updates** page with the operations
      runner. The job reports `migrated` and `rbac_reconciled`.
- [ ] Roll back, and confirm the previous release starts.
- [ ] **Diagnostics** shows the new version.

## 6. Publish

- [ ] Tag the release commit `v<version>`.
- [ ] Attach the signed bundle, the SBOM and the public signing key fingerprint.
