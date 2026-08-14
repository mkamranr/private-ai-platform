# Contributing

## Getting set up

[docs/quickstart.md](docs/quickstart.md) gets you running in about ten minutes on a laptop
with no GPU. Everything runs in containers — there is no local virtualenv to drift, and the
container is the single source of truth for the toolchain.

```bash
make up            # start the stack
make check         # lint and every test suite — run this before opening a PR
```

## What `make check` covers

```
ruff · ruff format · mypy · import-linter    backend, node-agent, mock-vllm, ldap-mcp
pytest                                        ~590 tests
```

`make lint` also runs `make airgap`, which fails the build if a runtime code path shells
out to `curl`/`wget`/`pip install`, if a dependency is unpinned, or if a container image is
not digest-pinned. That gate is not decoration: this platform has to install on a host with
no route to the Internet, and eight phases of unrestricted pulling produce a dependency
graph that cannot be bundled.

## The acceptance gates

Ten of them, one per phase, each an executable definition of "this phase is done":

```bash
make gate          # Phase 0 — foundation
make gate-phase4   # the MVP scenario: an agent using tools, end to end
make gate-phase8   # installs inside a container with --network none, then upgrades and rolls back
```

They run on a laptop with no GPU. **They also drop every table**: eight of them prove
migrations reverse cleanly by running `alembic downgrade base`. They restore the seed data,
the shipped catalogue and chat credentials afterwards — but not models you registered
yourself. Never run a gate against anything you care about.

If you change something a gate covers, run that gate. If you change `install.sh` or
anything else that ships in the bundle, run `make bundle` **before** `make gate-phase8` —
that gate rehearses an install *from the bundle*, so it tests whatever bundle is lying
around otherwise.

## House style

The code in this repository explains **why**, not what. A comment that restates the line
below it is noise; a comment naming the failure a piece of code prevents is why anyone can
change it later without reintroducing that failure. Match the surrounding density.

Other conventions, all enforced or checked:

* **Nothing reads `os.environ` directly** except `app/config/settings.py`.
* **No hard-coded** passwords, URLs, ports, GPU ids, model paths or container names.
* **Layering**: API → Service → Repository → DB. Routers never touch a repository.
* **The Docker SDK is reachable only from `DockerService`.**
* Interfaces in `app/core/interfaces/` do not import their implementations.

## Tests

Write the failing test first, and **watch it fail for the right reason**. Several bugs
found in this codebase were invisible because a test asserted something that was
accidentally true — a mock that answers to any model name hid an entire class of bug for
months.

Prefer tests that pin a contract over tests that pin an implementation, and prefer both
over tests that assert the machine's current state: a check that reads "the platform is
idle" passes until someone uses the platform.

## Pull requests

* One concern per PR.
* Say what failure the change prevents, not only what it does.
* `make check` green, plus any gate your change touches.
* Update the docs in the same PR. The `docs/` directory is treated as the contract; a
  behaviour change that leaves it stale is incomplete.

## Reporting bugs

Use the issue templates. What helps most: what you ran, what happened, what you expected,
and the output of `make check` or the relevant gate.
