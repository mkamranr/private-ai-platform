# Deployment

For running this somewhere that matters. If you only want to see it work, read
[quickstart.md](quickstart.md) — this page assumes you have already done that once.

The shape this platform was built for is a **control plane plus GPU machines**, so that is
described first. Everything after it is a variation: the same platform on one host, on a
network with no route out, or on hardware with little or no GPU.

1. **Control plane plus GPU nodes** — the intended deployment.
2. **Single host with GPUs** — the same thing collapsed onto one machine.
3. **Air-gapped** — either of the above, with no Internet.
4. **Running on less** — small cards, or none.

## Before any of them

**Decide where secrets live.** `make up` generates working secrets into `.env` so a
developer is running in one command. That is a development convenience, not a secret
management strategy. On a real deployment `.env` holds every credential the platform has,
including `SECURITY__ENCRYPTION_KEY`, which decrypts every stored credential — node agent
tokens, tool credentials, chat keys.

Two consequences worth stating plainly:

* **No backup contains that key.** A restore without the matching key is refused, by
  design. Losing it means losing every encrypted credential.
* **Rotating it invalidates everything already encrypted.** It is not a routine action.

**Set `PLATFORM_DATA_ROOT` to real storage.** It defaults to `./data`, which is right for a
laptop and wrong for a server. Everything stateful lives under it: PostgreSQL, Qdrant,
MinIO, Valkey, model weights.

**Put TLS in front.** The platform publishes plain HTTP on 8080 and expects to sit behind
something terminating TLS. `scripts/gen_certs.sh` produces a self-signed pair for testing;
use your own CA for anything else.

## 1. Control plane plus GPU nodes

The control plane holds the registry, scheduler, gateway, agent runtime and everything
stateful. Each GPU machine runs **only the node agent**: it reports the cards it can see
and starts model containers when instructed. No model traffic passes through the agent —
the gateway talks to the model container directly on the platform network.

### What each GPU host needs

| | |
|---|---|
| NVIDIA driver | Any release `nvidia-smi` ships with. The agent reads its CSV output, not the human-readable table, because that table's layout changes between releases |
| NVIDIA Container Toolkit | So Docker can pass GPUs into a container. Without it the platform can monitor cards but not deploy onto them |
| Docker | 24+, Compose v2 |
| Model runtime images | `vllm/vllm-openai` and anything else you intend to deploy, **loaded on the host**. The platform never pulls (Rule 4) |

That last row is the one that surprises people. `install-node.sh` installs the *agent*; it
does not install runtime images. Until they are present, a deployment scheduled onto that
host fails with "image is not present".

### Adding a node

From the console, **Nodes → Add node** issues a one-time enrolment token and prints the
command. Where the control plane was installed from a bundle, that command begins by
fetching the node's installer from it:

```bash
curl -fsSL -H "Authorization: Bearer aine_..." \
     https://ai-platform.local/api/v1/nodes/enrollment-bundle -o node-bundle.tar
tar xf node-bundle.tar
sudo ./install-node.sh --server https://ai-platform.local --name gpu-node-01 --token aine_...
```

That transfer is ~288 MB rather than the full 1.9 GB, because a node needs the installer,
its helpers, the manifest and the agent image — the rest of a bundle is control-plane
images a node never runs. It is not a breach of the air gap: those bytes arrived on the
bundle you carried in, and this moves them one hop across your own network.

The control plane then **probes the node back** before accepting it. A node that enrols but
cannot be reached fails loudly rather than appearing in the list and silently never
reporting.

**The GPU probe is pinned, not left on `auto`.** Where the installer sees the NVIDIA
container runtime it writes `nvidia_smi` explicitly, so a broken driver fails loudly
instead of falling through to the synthetic probe and cheerfully reporting four invented
A100s. On a host with no NVIDIA runtime it stays `auto` and the node registers CPU-only —
a legitimate configuration, and the scheduler refuses GPU work there.

### Sizing, and what fits

Approximate, and worth measuring on your own weights rather than trusting a table:

| Model | Unquantised (fp16) | 4-bit (AWQ / GPTQ) |
|---|---|---|
| 7–8B | ~16 GB → one 24 GB card | ~6 GB → one 8–12 GB card |
| 13–14B | ~28 GB → one 40 GB card, or two 24 GB | ~10 GB → one 16 GB card |
| 70B | ~140 GB → two 80 GB cards, tensor-parallel | ~40 GB → one 48 GB, or two 24 GB |

Add headroom for the KV cache, which grows with context length and concurrency and is
frequently what actually exhausts a card. Two knobs at deploy time control it:

* `gpu_memory_utilization` — the fraction of the card vLLM may claim. Defaults to `0.90`.
  Lower it where something else shares the GPU.
* `max_model_len` — the context window. Halving it roughly halves the KV cache.

**GPU-aware placement is the scheduler's job.** A model declaring `min_gpu_count: 2` is
placed on a node with two free cards and started tensor-parallel across them; the
reservation and the deployment row are written in one transaction, so a failure anywhere
in creation releases the GPUs by rollback rather than stranding them.

### Verifying a node before trusting it

```bash
make reconcile              # containers running that no deployment claims
```

The GPUs page shows utilisation, memory, temperature, power and allocation per card. A
node reporting **synthetic** telemetry is badged as such throughout the UI — presenting
fabricated numbers as real capacity is the single most misleading thing this platform
could do, so it is marked rather than hidden.

## 2. Single host with GPUs

The same platform with the control plane and the GPUs on one machine. Sensible for a
single workstation or a lone server; the node agent still runs, and still registers that
host's cards as schedulable capacity — the topology collapses, the model does not.

The host needs everything in the table above: driver, container toolkit, and the runtime
images loaded locally.

```bash
git clone https://github.com/mkamranr/private-ai-platform.git /opt/ai-platform
cd /opt/ai-platform

# Review before starting — this is the file that matters
$EDITOR .env.example        # then:
make .env                   # copies it and generates secrets
$EDITOR .env                # set PLATFORM_DATA_ROOT, hostnames, TLS

make up                     # core services
make seed                   # roles, permissions, bootstrap admin, bucket
make definitions-import     # the shipped agents, skills and tools
```

Enrol the host with itself as a node — **Nodes → Add node**, then run the printed command
locally — and its GPUs become schedulable. From there, deploy from the model registry as
in §1.

Where there are no usable GPUs, the same host can serve an existing Ollama
(`make ollama-import`), a hosted provider
([models.md](models.md#using-a-hosted-endpoint-openrouter-and-anything-like-it)), or
llama.cpp on CPU (`make local-llm`) — see §4.

Optional profiles:

```bash
make chat          # Open WebUI on 8081
make agents        # chat + the vendored MCP servers
make monitoring    # Prometheus, Loki, Tempo, Grafana, Langfuse
```

## 3. Air-gapped

The target never reaches the Internet. Two machines are involved:

**On a connected build machine:**

```bash
make bundle        # ~1.9 GB: images, wheels, the tree, the installers
```

`make bundle` builds the images before saving them. That is not incidental — `docker save`
ships whatever a tag currently points at, so a bundle built without rebuilding can contain
weeks-old code beside a current source tree, and every "is the image present?" check still
passes. It runs the wrong code and nothing says so.

**Carry `bundle/<stamp>/` to the target**, then:

```bash
cd <stamp>
sudo ./install.sh . /opt/ai-platform

cd /opt/ai-platform
docker compose exec backend alembic upgrade head
docker compose exec backend python -m app.utils.cli seed
docker compose exec backend python -m app.utils.cli definitions-import
```

That third command is the one that gets skipped, and the platform works well enough
without it to hide the omission — you simply have no agents, skills or tools.

GPU nodes on an air-gapped site enrol exactly as in §1 — that flow was designed for this
case, which is why the node fetches its installer from the control plane rather than
needing the bundle carried to every rack.

Full detail, including upgrade and rollback: [airgap.md](airgap.md).

## 4. Running on less

None of the above is a hard requirement. The platform was built on a machine with no
NVIDIA hardware at all, which is why every GPU and inference touchpoint sits behind an
interface with a working substitute rather than a stub.

### Small cards

Deploy a 4-bit AWQ or GPTQ build instead of the unquantised weights and the same 7–8B
model fits comfortably on an 8–12 GB card. Two adjustments usually make the difference
between "will not start" and "runs fine":

```
gpu_memory_utilization: 0.80     # leave room for anything else on the card
max_model_len: 8192              # the KV cache is often what actually exhausts VRAM
```

Both are set per deployment, so one node can run a small model with a modest context while
another runs something larger.

### No GPU at all

```bash
make local-llm      # llama.cpp serving a quantised GGUF on CPU
```

That registers the engine as an `external` runtime and points `enterprise-chat` at it, so
agents, RAG and the chat frontend reach it with no further configuration. On a four-core
laptop a 1.5B model answers a short question in a few seconds; an agent run that makes
several calls takes minutes. Slow, and entirely usable for development.

llama.cpp rather than vLLM on CPU, deliberately: vLLM's CPU backend targets AVX512,
publishes no prebuilt CPU image, and its official image is CUDA-only. llama.cpp needs AVX2
and runs as shipped.

### No weights at all

The `mock` runtime returns structured placeholder text that announces itself as synthetic.
It exists so the deployment state machine, the gateway, the agent pipeline and RAG can all
be exercised with nothing downloaded — including a startup delay, so `HEALTH_CHECK` is
genuinely waited on rather than skipped.

It will never write a sentence worth reading, and that is the point: a mock returning
plausible prose would let a misconfigured production deployment look like a working one.

### The trade you are making

A CPU or small-GPU deployment is a development and evaluation posture, not a serving one.
Throughput and context length are what you give up. Everything else — RBAC, audit, the
agent authorisation pipeline, tenant-scoped retrieval, backup and restore, the offline
bundle — behaves identically, which is what makes the small deployment worth having.

## Operating it

| | |
|---|---|
| Health | `GET /api/v1/health`, and `make check` for a full dependency probe |
| Metrics | `/metrics` on the backend — **not** proxied through nginx, by design |
| Dashboards | `make monitoring`, then Grafana on 3000 |
| Backups | `make backup`, `make backup-verify B=...`, `make backup-restore B=...` |
| Orphaned containers | `make reconcile` (add `REMOVE=1`) |

**Verify a backup restores before you need it.** `make backup-verify` exists because an
unverified backup is a hypothesis.

## Upgrading

```bash
sudo ./upgrade.sh <new-bundle> /opt/ai-platform     # air-gapped target
sudo ./rollback.sh /opt/ai-platform                 # if it goes wrong
```

Both are rehearsed by `make gate-phase8`, which installs, migrates, seeds, signs in,
upgrades and rolls back inside a container with `--network none`.

## Things that will bite you

**Any acceptance gate wipes the database.** Eight of the ten prove migrations reverse
cleanly by running `alembic downgrade base`, which drops every table. They restore the seed
data, the shipped catalogue and the chat credentials afterwards — but **not** models you
registered yourself. Re-run `make local-llm`, `make ollama-import` or `make external-import`
after a gate run. Do not run gates against a production database.

**The mock runtime is not a model.** `MODELS__DEFAULT_RUNTIME=mock` returns structured
placeholder text that announces itself as synthetic. That is deliberate — a mock returning
plausible prose would let a misconfigured production deployment look like a working one.

**A hosted provider takes you out of air-gapped operation.** Setting
`MODELS__EXTERNAL_API_KEY` sends every prompt off the host, including passages retrieved
from knowledge bases. On a classified network that is a classification decision, not a
configuration one.
