<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/logo-wordmark-dark.svg">
    <img alt="Private AI Platform — Your AI. Your infrastructure." src="docs/images/logo-wordmark-light.svg" width="520">
  </picture>
</p>

<p align="center">
  <b>A self-hosted AI platform that runs with no route to the Internet.</b><br>
  Run your own models on your own GPUs, and give your organisation an OpenAI-compatible API,
  agents, document search, chat and voice. Everything is governed, audited and observable.
</p>

<p align="center">
  <a href="https://github.com/mkamranr/private-ai-platform/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/mkamranr/private-ai-platform/actions/workflows/ci.yml/badge.svg?branch=master"></a>
  <a href="docker-compose.yml"><img alt="Docker Compose" src="https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white"></a>
  <a href="backend/pyproject.toml"><img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white"></a>
  <a href="backend/requirements.in"><img alt="FastAPI 0.141" src="https://img.shields.io/badge/FastAPI-0.141-009688?logo=fastapi&logoColor=white"></a>
  <a href="docs/airgap.md"><img alt="Offline: air-gapped" src="https://img.shields.io/badge/offline-air--gapped-2E7D32"></a>
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-blue"></a>
</p>

<p align="center">
  <a href="#features">Features</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#deploying">Deploying</a> ·
  <a href="#air-gapped-install">Air-gapped install</a> ·
  <a href="#documentation">Documentation</a>
</p>

![Dashboard](docs/images/dashboard.png)

## Why

- **Fully offline.** Installs and upgrades from a signed bundle. It makes no runtime
  downloads and sends no telemetry.
- **Drop-in for OpenAI clients.** Point any OpenAI SDK at `/v1` with a platform API key.
  It works with no code changes.
- **Governed by default.** It has roles and teams, quotas, model approval, and a human
  approval step for risky agent actions. Every change is audited.
- **Runs anywhere, even without a GPU.** It runs on real GPUs, on CPU with llama.cpp, or
  with a mock runtime, so a laptop can run the whole platform and its tests.
- **Built on proven open source.** PostgreSQL, Valkey, Qdrant, MinIO, vLLM, Open WebUI,
  Prometheus, Grafana, Loki, Tempo and Langfuse are used as they are, not forked.

## Features

### Models and GPUs
- **GPU fleet.** Add GPU hosts with a one-time token. Live telemetry per card covers
  utilisation, memory, temperature, power, and whether the card is free or allocated.
- **Model registry.** Import or register models, then deploy them with GPU-aware
  placement. A dry run shows what will happen first. Revisions can be rolled back, and
  logs stream live.
- **Runtimes.** vLLM, llama.cpp, Ollama, hosted OpenAI-compatible providers, or a mock
  runtime for development.
- **Inference backends.** Attach existing vLLM, Ollama or OpenAI-compatible endpoints,
  test the connection, and discover their models.
- **Model governance.** Models are marked approved, experimental, deprecated or blocked,
  with licence and source metadata. A blocked model is never served.

### OpenAI-compatible gateway
- **One API.** Chat, completions, embeddings, speech-to-text and text-to-speech at `/v1`,
  with streaming.
- **Aliases and fallbacks.** Callers use a stable name. A fallback alias answers when the
  primary model is down. Circuit breakers stop sending traffic to a failing backend.
- **Quotas.** Tokens per minute, day and month, and concurrent calls. They can be set per
  API key, team, end user or model, with alerts at 80% and 100%.
- **Usage and timings.** Every call is recorded: tokens, latency, time to first token and
  output tokens per second, per model, key, team and end user.

### Agents and workflows
- **Agents.** Versioned agents combine a model, a prompt, skills and tools. Shipped
  agents can be imported from YAML. Chat clients can pick any agent as `agent:<slug>`.
- **Tools and MCP.** Built-in tools, REST tools and MCP servers, with a catalogue of
  seven connectors. Discovered tools start disabled until someone reviews them.
- **Safe by construction.** A tool call needs both the agent's grant and the caller's
  own permission. High-risk calls wait for a human approval. Approvals nobody answers
  expire. Shell and Python execution are never allowed.
- **Workflows.** Versioned workflows with LLM, retrieval, tool, HTTP, condition,
  transform, approval and delay steps. They can start manually, from the API, on a cron schedule
  or from a webhook, and every step is traced.

### Knowledge and retrieval (RAG)
- **Knowledge bases.** Upload PDF, Word, PowerPoint, CSV, HTML and text files, or scanned
  images. OCR makes scans searchable, and answers cite the page or slide they came from.
- **Better retrieval.** Reranking, document versions, and a Retrieval Playground to
  compare results.
- **Evaluation sets.** Score hit rate, recall, precision and MRR before you change a
  setting.

### Chat, voice and vision
- **Private chat.** Open WebUI connected to the gateway, with usage attributed to each
  person, not to one shared key.
- **Voice assistant.** Speech-to-text and text-to-speech in Arabic and English.
- **Developer portal.** Developers create their own API keys, see the models they can
  call, copy ready-made client code and watch their usage.

### Security and access
- **Sign-in.** Local accounts, OIDC single sign-on and LDAP/Active Directory, plus
  authenticator-app two-factor authentication. Admins can be required to use it, or
  everyone can.
- **Hardening.** Account lockout, sign-in throttling, refresh-token rotation with theft
  detection, and *sign out everywhere*.
- **Roles and teams.** Role-based permissions and team-owned models, keys, agents and
  knowledge bases. One visibility rule is enforced everywhere, the gateway included.
- **Audit trail.** Who did what, when and from where, for every change and every refusal.

### Operations
- **Dashboard.** Fleet, models, gateway traffic, alerts, knowledge, agents and workflows
  on one page. A figure that wasn't measured shows as unavailable, never as zero.
- **Alerts and notifications.** In-app alerts, sent to chat webhooks, ticketing systems
  or email, with retries and signed payloads.
- **Backups and upgrades from the browser.** Scheduled and verified backups, restore,
  offline upgrades with preflight checks, rollback and diagnostics.
- **Chargeback.** Dated cost rates, cost reports by team, key, user or model, monthly
  budgets and CSV export.
- **Observability.** Metrics, logs and traces that link up: from an error to its log
  line, to its trace, to the agent run behind it.
- **Runtime settings.** Change limits and policies on the Settings page without a
  restart. Every change is audited.

<table>
<tr>
<td width="50%"><img src="docs/images/gpus.png" alt="Live GPU telemetry per node"></td>
<td width="50%"><img src="docs/images/agents.png" alt="Agents with their tools and skills"></td>
</tr>
<tr>
<td width="50%"><img src="docs/images/knowledge.png" alt="A knowledge base with an OCR'd document indexed"></td>
<td width="50%" valign="top">

**GPU telemetry** comes straight from each node, card by card.

**Agents** show the tools and skills each one holds.

**Knowledge bases** show a scanned page turned into searchable text, with a preview of
exactly what an agent would be given.

</td>
</tr>
</table>

## Quick start

On a laptop, no GPU needed:

```bash
git clone https://github.com/mkamranr/private-ai-platform.git
cd private-ai-platform
make .env     # copies .env.example and generates every secret
make up       # builds and starts the core stack
make seed     # roles, permissions and the admin account
make definitions-import   # the shipped agents, skills and tools
```

Open **http://localhost:8080**. Sign in as `admin` with the password in `.env`
(`AUTH__BOOTSTRAP_ADMIN_PASSWORD`). [docs/quickstart.md](docs/quickstart.md) walks
through deploying a model and calling it, in about ten minutes.

## Deploying

A **control plane** runs the console, API, gateway and agents. Each **GPU host** runs
only a small node agent. One machine can be both.

| | Control plane | GPU host |
|---|---|---|
| OS | Linux with Docker Engine and Compose | Linux with Docker Engine |
| GPU stack | none | NVIDIA driver and Container Toolkit |
| Resources | 4 cores, 16 GB RAM | VRAM for the models you deploy |

1. **Install** the control plane as in the quick start. Set `PLATFORM_DATA_ROOT` and
   `PLATFORM__PUBLIC_BASE_URL` in `.env`, and keep a copy of `.env`, which holds the key
   to every stored secret. Put TLS in front with your reverse proxy.
2. **Add GPU nodes** from **Nodes → Add node**. The console prints the one-line install
   command for the host.
3. **Deploy a model** under **Models**. Create an API key under **Endpoints & Keys**, and
   point any OpenAI client at `https://<your-host>/v1`.
4. **Add what you need:** `make chat` (Open WebUI), `make monitoring` (Prometheus,
   Grafana, Loki, Tempo, Langfuse), `make ops-runner` (backups and upgrades from the
   console), `make mcp` (MCP connectors).

**Sizing:**

| You have | Run |
|---|---|
| 24 GB+ per GPU | vLLM with full-precision models |
| 8–16 GB per GPU | vLLM with 4-bit AWQ/GPTQ models, or smaller models |
| No GPU | llama.cpp on CPU (`make local-llm`) |
| Nothing at all | The mock runtime, for development and tests |

The full guide, including sizing, is in [docs/deployment.md](docs/deployment.md).

## Air-gapped install

The whole platform ships as one signed bundle containing images, Python wheels, the
source tree and the installers.

```bash
# On a connected build machine
make signing-key OUT=~/aip-signing
make bundle SIGN_KEY=~/aip-signing/bundle-signing.key CHAT=1 MONITORING=1

# On the offline target
sudo ./install.sh . /opt/ai-platform --trust-key /media/keys/bundle-signing.pub
```

GPU nodes fetch their installer from the control plane, so the bundle never has to
visit every rack. Upgrades verify the signature, back up first and can be rolled back,
from the command line or the **Offline Updates** page. See
[docs/airgap.md](docs/airgap.md).

## Documentation

| Topic | Guides |
|---|---|
| Getting started | [Quick start](docs/quickstart.md) · [Deployment](docs/deployment.md) · [Air-gapped install](docs/airgap.md) · [Troubleshooting](docs/troubleshooting.md) |
| Models and gateway | [GPUs](docs/gpu.md) · [Models](docs/models.md) · [Backends](docs/backends.md) · [Governance](docs/model-governance.md) · [Quotas and timings](docs/gateway-policy.md) |
| Agents | [Agents](docs/agents.md) · [Skills](docs/skills.md) · [Tools](docs/tools.md) · [MCP](docs/mcp.md) · [Workflows](docs/workflows.md) |
| Knowledge and chat | [RAG](docs/rag.md) · [Retrieval evaluation](docs/rag-evaluation.md) · [Chat](docs/chat.md) · [Speech](docs/speech.md) · [Voice](docs/voice.md) |
| Security | [Security model](docs/security.md) · [Teams](docs/teams.md) · [Threat model](docs/architecture/threat-model.md) |
| Operations | [Operations](docs/operations.md) · [Backups](docs/backup.md) · [Notifications](docs/notifications.md) · [Chargeback](docs/chargeback.md) · [Settings](docs/settings.md) · [Observability](docs/observability.md) |
| Reference | [Architecture](docs/architecture.md) · [API](docs/api.md) · [Database](docs/database.md) · [Changelog](CHANGELOG.md) |

## Development

```bash
make check          # lint, types, layering contracts and every service's tests
make gate-phase4    # end to end: an agent using tools
make gate-phase8    # offline install, upgrade and rollback in a container with no network
```

The acceptance gates reset the database, so never run them against data you care about.
See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/development.md](docs/development.md).
Report security issues as described in [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE). Third-party components keep their own licences, and the
offline bundle carries them with the software it ships.
