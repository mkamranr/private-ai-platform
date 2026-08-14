# Development

## Everything runs in Docker

`make test` and `make lint` exec into the backend container, so the host Python version
is irrelevant (the reference machine has 3.10; the platform needs 3.12).

## Commands

```bash
make help              # all targets
make up / down / logs
make test / cov
make lint / fmt        # ruff, mypy, import-linter, air-gap gate
make migrate / revision m="..." / migrate-roundtrip
make seed
make lock              # recompile lockfiles — needs network
```

## Adding a module (§26, Rule 1)

Implement **one** module at a time. Read `architecture.md`, `database.md`, `api.md`
first. Ship implementation + unit tests + API tests + migration + docs (Rule 2).

Every route declares `require_permission(...)`. Every privileged action records an
audit entry. New config goes in `settings.py` + `config.yaml` + `.env.example`.

## Adding a dependency

Edit `requirements.in`, run `make lock`, commit the regenerated lockfile. Never
hand-edit a lockfile; never `pip install` into a running container.

## What the gates enforce

- **ruff** lint + format
- **mypy** `disallow_untyped_defs`
- **import-linter** the Rule 6/7 layering contracts — these are architectural
  invariants, and review is not a reliable way to hold them across 28 modules
- **check_airgap.py** air-gap discipline

## Testing notes

Tests run against real Postgres/Valkey/Qdrant/MinIO, not SQLite plus mocks. Correctness
here depends on PostgreSQL-specific behaviour — JSONB, partial unique indexes,
`ON DELETE` semantics, transactional DDL — that SQLite does not reproduce. A suite that
passes on SQLite and fails on Postgres is worse than none.

Isolation is a per-test transaction that is always rolled back. Two consequences:

- A fixture that must be visible to a *separate* transaction (anything testing
  `record_independent`) needs `committed_user`, and is swept at session end.
- Compose injects `.env` as real environment variables at the highest precedence, so a
  settings test must scrub the ambient environment — see `test_config.py::_isolate`.
