# Air-gapped installation (M23, M27)

The platform must have **no runtime dependency on the Internet** (§M23, Rule 4).

## The two machines

| | Build machine | Target |
|---|---|---|
| Network | connected | **none** |
| Role | assembles the bundle | runs the platform |
| Arch | must match the target (`linux/amd64`) | `linux/amd64` |
| Needs | Docker, Python 3 | Docker, Python 3 |

Development happens connected; the *runtime* is air-gapped. An Intel Mac produces
`linux/amd64` artifacts natively, so it can serve as the build machine without
emulation. On Apple Silicon, `--platform linux/amd64` is required everywhere — the
wheelhouse is built inside a `linux/amd64` container for exactly this reason.

`scripts/build_bundle.py` is **the only thing in this project allowed to use the
network.** Everything else — every script, image and runtime path — is forbidden to, and
`scripts/check_airgap.py` enforces it on every `make lint`.

## What is enforced continuously (from Phase 0)

`make airgap` runs `scripts/check_airgap.py`, part of `make lint`:

1. Lockfiles fully pinned with hashes
2. Direct dependencies pinned with `==`
3. Dockerfile base images pinned by **digest**
4. Compose images pinned by digest
5. No OS package installs at image build
6. No runtime code path shells out to a network fetcher

This runs from Phase 0 rather than Phase 8 on purpose — eight phases of unrestricted
pulling produce a dependency graph that cannot be bundled without rework.

### The one way to opt out, deliberately

`MODELS__EXTERNAL_API_KEY` points the platform at a hosted LLM (OpenRouter by default) and
**takes the installation out of air-gapped operation**. Every prompt then leaves the host,
including whatever knowledge-base passages were retrieved into it. On a classified network
that is a classification decision, not a configuration one.

The gate above does not catch this, and cannot: it forbids runtime code from *shelling out*
to a fetcher (`curl`, `wget`, `pip install`), which is a property of the source. An httpx
call to a URL an operator configured is indistinguishable from the call every model
provider already makes to a local runtime — the difference is only where the URL points.
What protects the property instead is that the key is **empty by default**, and
`make external-import` refuses to register anything without it.

See [models.md](models.md#using-a-hosted-endpoint-openrouter-and-anything-like-it).

## Building the bundle

On the connected build machine, with the platform's images already built:

```bash
make up            # or: docker compose --profile core build
make bundle        # MODELS=1 to include weights, CHAT=1 to include Open WebUI
make bundle-dry    # what it would contain, without writing it
```

Output is `bundle/<UTC-stamp>/`, roughly 1.9 GB without model weights:

```
bundle/20260810T095714Z/
├── manifest.json      what is inside, and the sha256 of every part
├── install.sh         ── the bundle installs itself; no separate media
├── upgrade.sh
├── rollback.sh
├── lib.sh
├── images/            docker save, by digest — 9 archives
├── wheels/            pip download, linux/amd64 + cp312, per service
└── tree/              compose files, configs, manifests, scripts, docs, frontend
```

Two things the build refuses to do, both deliberate:

**It never pulls.** An image that is not already local is reported and skipped, because a
silent pull is how an unreviewed image reaches an air-gapped site.

**It never bundles `oidc-fixture`.** That fixture issues a signed token to anyone who
asks and exists only so SSO is testable without a Keycloak. On a production network it is
not a test double, it is an authentication bypass. The manifest records the exclusion and
its reason.

Model weights are opt-in (`MODELS=1`) because they dominate the size and change on a
different cadence from the platform. Without them the platform registers manifests
happily and every deployment fails, so ship them somehow.

## Installing on the target

Copy the bundle directory to the target — physical media, one-way diode, whatever the
site uses — then:

```bash
cd 20260810T095714Z
./install.sh . /opt/ai-platform

cd /opt/ai-platform
docker compose exec backend alembic upgrade head
docker compose exec backend python -m app.utils.cli seed
docker compose exec backend python -m app.utils.cli definitions-import
```

The third command is the one that is easy to leave out, and the site works well enough
without it to hide the omission: the agents, skills and tools ship as files in the bundle
and reach the database only through that import. Skip it and the platform comes up with an
empty catalogue.

The installer verifies every archive against the manifest **before it writes anything**,
loads the images, names them, unpacks the tree, generates the secrets and starts the core
profile. It is idempotent: re-running it is how an interrupted install is resumed.

### Installing a node

A node needs four files totalling about **288 MB** — `install-node.sh`, `lib.sh`,
`manifest.json` and `images/node-agent.tar`. Everything else in a 1.9 GB bundle is this
control plane's own images: postgres, qdrant, minio, nginx, the backend. A node runs none
of them.

`install.sh` stages those four under `${PLATFORM_DATA_ROOT}/node-bundle`, so the admin
console hands you a command that fetches them from the control plane:

```bash
curl -fsSL -H "Authorization: Bearer aine_..." \
     https://ai-platform.local/api/v1/nodes/enrollment-bundle -o node-bundle.tar
tar xf node-bundle.tar
sudo ./install-node.sh --server https://ai-platform.local --name gpu-node-01 --token aine_...
```

**This is not a hole in the air gap.** Rule 4 forbids pulling from the *Internet*, which is
what makes the platform installable on a host with no route out. These bytes arrived on the
bundle you carried in; the download moves them one hop across the site's own network.
`install-node.sh` itself still fetches nothing, exactly as its header says — the `curl` is
the operator's, and it runs before the installer does.

The enrolment token authorises the download as well as the joining, so there is no second
credential to manage, and fetching does **not** spend the token's single use.

Where nothing was staged — a control plane not installed from a bundle — the console omits
the download and tells you to copy the files yourself:

```bash
cd /media/usb/20260810T125012Z
sudo ./install-node.sh --server https://ai-platform.local --name gpu-node-01 --token aine_...
```

Only the agent's own archive is verified and loaded, not all 1.9 GB — a GPU host has no use
for Postgres, MinIO or Grafana. If carrying the whole bundle to every node is impractical,
`manifest.json` plus `images/node-agent.tar` in a directory beside the script is enough;
keep the manifest, because it is what proves the archive survived the copy.

**The control plane deliberately does not serve images over HTTP.** It would be easy to add
and it is the wrong shape: `node-agent/app/runtime/docker.py` refuses to start a container
whose image is absent, with *"Load it from the offline bundle with `docker load`; the
platform never pulls (Rule 4)"*. A host that could fetch the agent that way but still could
not fetch a vLLM image the same way would teach an operator something untrue about how this
platform gets its images. One mechanism, and the bundle is it.

### Why there is a `docker-compose.airgap.yml`

The compose file pins third-party images by digest, which is right for a connected build
and unusable on the target. `docker load` restores an image's content and its config ID
but **not** its name — an archive saved from `postgres@sha256:…` carries no `RepoTags` —
and the digest cannot simply be put back, because Docker refuses to create a tag from a
digest reference.

So the installer names each image from its config ID (`postgres:bundled`, and so on) and
writes `docker-compose.airgap.yml` recording what it named. That override also sets
`pull_policy: never`, so a missing image fails immediately and says which one, instead of
hanging against a registry this host cannot reach.

`install.sh` puts `COMPOSE_FILE=docker-compose.yml:docker-compose.airgap.yml` in the
target's `.env`, so a plain `docker compose up` picks up both files. **Do not remove that
line**, and do not run `docker compose -f docker-compose.yml` on the target — a single
explicit `-f` drops the override and Compose goes back to trying to pull.

### Secrets

Generated on the target by `install.sh`, never shipped: a bundle carrying real secrets
would put the same JWT signing key and the same Fernet key on every site that ever
received a copy. `.env` is not in the bundle; `.env.example` is, with placeholders.

Read `AUTH__BOOTSTRAP_ADMIN_PASSWORD` out of `.env` once, and keep `.env` somewhere the
target is not the only copy — `SECURITY__ENCRYPTION_KEY` decrypts every stored
credential, and no backup contains it ([backup.md](backup.md)).

## Upgrading, and going back

```bash
./upgrade.sh /media/usb/20260901T101500Z /opt/ai-platform
cd /opt/ai-platform && docker compose exec backend alembic upgrade head
```

`upgrade.sh` takes a backup **while the old platform is still running**, snapshots the
current tree into `.rollback/<stamp>/`, and only then stops, loads, swaps and starts. An
upgrade that works out it needs a rollback point after replacing the tree has nothing to
offer but an apology. `data/`, `.env` and previous backups are never touched.

```bash
./offline/rollback.sh /opt/ai-platform            # newest rollback point
./offline/rollback.sh /opt/ai-platform 20260809T195257Z --yes
```

Rollback restores the previous tree and re-names the previous images, which are still on
disk because an upgrade only ever adds images. **It rolls back code, not data**: if
migrations ran after the upgrade, restore the backup the upgrade took
(`python3 scripts/backup.py restore backups/<stamp>`).

The scripts live at the bundle root *and* in the installed tree at
`/opt/ai-platform/offline/`, so a site can roll back months later without finding the
original media.

## The gate

```bash
make gate-phase8
```

This does not inspect the bundle and pronounce it plausible. It runs `install.sh` inside
a container started with `--network none`, with only the Docker socket bind-mounted — a
unix socket is not a network, so the daemon is reachable and nothing else in the world
is. Then it migrates, seeds, signs in with the generated password, installs the
wheelhouse with `--no-index`, upgrades, and rolls back, all in the same isolation.

What it proves, in the order the failures actually bite:

| | |
|---|---|
| A damaged archive | is refused *before* anything is written |
| An old bundle format | is refused by name, not discovered at start-up |
| Every loaded image | has a name, and no service still resolves to a digest |
| Nothing | was pulled |
| The secrets | were generated on the target, and the admin one signs in |
| The wheelhouse | resolves every dependency with no index |
| Upgrade | takes a backup and leaves a rollback point; `data/` survives |
| Rollback | brings the platform back healthy |

Set `KEEP=1` to leave the rehearsed platform running on `localhost:8099` for inspection.
