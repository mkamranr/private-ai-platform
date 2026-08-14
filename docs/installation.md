# Installation (M08)

Connected-machine installation. For the air-gapped target see [airgap.md](airgap.md).

## Requirements

| | Minimum | Notes |
|---|---|---|
| OS | Linux x86_64 | macOS works for development |
| Docker | 24.0+ with Compose v2 | |
| Python | 3.12+ **in the container only** | the host interpreter is irrelevant; `make test` runs in-container |
| GPU | none for development | NVIDIA driver + container toolkit for real inference |

## Quick start

```bash
cp .env.example .env
# Change every CHANGE ME value. Generate secrets with:
#   openssl rand -hex 32                       # AUTH__JWT_SECRET_KEY
#   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
make up        # start the core stack and wait for health
make migrate   # apply migrations
make seed      # roles, permissions, bootstrap admin, MinIO bucket
```

Then `http://localhost:8080/api/v1/health`.

## Verify

```bash
make test   # unit + API tests, in-container
make lint   # ruff, mypy, import-linter, air-gap gate
```

## Installing on an air-gapped target

Nothing above applies there: the target never reaches a registry or an index. Build a
bundle here, carry it across, and run its own installer.

```bash
make bundle                                  # build machine — the one networked step
./install.sh . /opt/ai-platform              # target, from inside the bundle
```

Full procedure, including upgrade and rollback: [airgap.md](airgap.md).

## Adding a GPU node

Four steps, and the operator types one name.

1. **Copy the bundle to the GPU host** — physical media, one-way diode, whatever the site
   allows. This is not optional and not a limitation of the installer: the platform never
   pulls images (Rule 4), so a node needs the bundle for the agent's image and for every
   model image it will later run.
2. **Administration → Nodes → Add node** in the console. Give it a name. Nothing else.
3. **Copy the command** it shows and run it on the host:

   ```bash
   cd /media/usb/20260810T125012Z
   sudo ./install-node.sh \
       --server https://ai-platform.local \
       --name gpu-node-01 \
       --token aine_...
   ```

4. The node appears in the list as **ONLINE**, with its GPUs, CPU, memory and Docker
   version filled in — all read from the host, none of it typed.

The token is shown **once**, is good for one node, and expires in an hour. If it is lost or
the install is interrupted, revoke it in the console and issue another.

### What the script does

Installs the agent image from the bundle, generates the node's own agent token *on the
node*, writes `/etc/ai-platform/node-agent.env` at mode `0600`, starts the agent under
Compose with `restart: unless-stopped`, waits for it to report healthy, then enrols. It is
**idempotent** — re-running is how an interrupted install is resumed, and it reuses the
existing agent token rather than rotating it, because rotating would take the node offline
as a side effect of re-running the installer.

Useful flags: `--port` when 9100 is taken (the single-host deployment already runs an
agent), `--advertise-host` when the address the control plane should use is not the one the
node would guess, `--no-enrol` to install now and join later, `--bundle` when the bundle is
not beside the script.

### Why there is no URL or token to type

The agent reports its own address and the control plane **verifies it by reaching back**
before believing any of it — the same rule manual registration follows, so a host that
cannot be reached fails loudly instead of sitting in the list never reporting. See
[gpu.md](gpu.md) for the agent's own configuration and
[security.md](security.md) for what an enrolment token grants.

**Registering manually** is still available, behind that button, and is the right choice
for an agent that is already running or a node that cannot reach this control plane.

## TODO (Phase 1+)

- TLS setup via `scripts/gen_certs.sh`, and mTLS between the control plane and node agents
- Production deployment with `docker-compose.prod.yml`
