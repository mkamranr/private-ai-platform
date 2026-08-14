# Quickstart — running it on a laptop

Ten minutes, a laptop, Docker. **No GPU, no API key, no Internet access for the platform
itself.** At the end you will have a private AI platform running locally, with a real
language model answering in a chat window.

**This is the evaluation path, not the intended deployment.** The platform is built to run
a control plane alongside GPU machines serving models with vLLM — that is
[deployment.md](deployment.md). What makes this page possible is that every GPU and
inference touchpoint sits behind an interface with a working substitute, so the whole
platform, its agent pipeline and its full test suite run on hardware that has no NVIDIA
driver at all.

Which means what you see here is the real thing with smaller models behind it, not a demo
mode: the same scheduler, gateway, authorisation pipeline and audit trail.

## What you need

| | |
|---|---|
| Docker | 24+, with Compose v2 (`docker compose version`) |
| Memory | 6 GB free for Docker. The whole core stack runs in about 3 GB; a local model adds ~1 GB |
| Disk | ~10 GB |
| Python 3 | Only for the helper scripts — the platform itself runs entirely in containers |

A GPU is **not** required and never will be for this path: the platform ships a synthetic
inference engine (`mock-vllm`) so every code path can run on a machine that has none.

## 1. Start it

```bash
git clone https://github.com/mkamranr/private-ai-platform.git ai-platform
cd ai-platform
make up
```

`make up` creates `.env` on first run **with real generated secrets** — the template's
placeholders are documentation, and one of them (the Fernet encryption key) is rejected
outright by the backend, so a straight copy would leave you with a platform that will not
start. It then builds the images and waits until every service reports healthy.

Expect three or four minutes the first time, most of it building the backend image.

## 2. Create the admin account and the catalogue

```bash
make seed                 # roles, permissions, the bootstrap admin, the object-store bucket
make definitions-import   # the shipped agents, skills and tools
```

Your admin password was generated into `.env`:

```bash
grep AUTH__BOOTSTRAP_ADMIN_PASSWORD .env
```

## 3. Sign in

Open **http://localhost:8080** and sign in as `admin` with that password.

You should see the dashboard: no nodes, no models, nothing deployed. That is correct — you
have a control plane and nothing to control yet.

## 4. Give it a model

Two ways, depending on whether you want a *real* model or just the plumbing.

### A real model, on CPU

Download a small GGUF into `data/models/`, then:

```bash
make local-llm
```

That serves it with llama.cpp in a container, registers it, and points the `enterprise-chat`
alias at it — so agents, RAG and the chat UI all reach it without further configuration.
On a 4-core laptop expect a few seconds for a short answer.

The default expects `data/models/qwen2.5-1.5b-instruct-gguf/qwen2.5-1.5b-instruct-q4_k_m.gguf`.
Any GGUF works — point `LOCAL_LLM_GGUF` at it in `.env`.

### Or just the plumbing, with no download

The `mock` runtime needs nothing and returns structured placeholder text. It exists so the
deployment state machine, the gateway, agents and RAG can all be exercised with no weights
at all — it will never write you a sentence worth reading, and it says so in every response.

## 5. Chat with it

```bash
make chat
```

Open **http://localhost:8081**. Open WebUI talks to the platform's OpenAI-compatible
gateway and nothing else, so what you see in the model picker is exactly what the platform
is serving.

## 6. Use it as an API

The gateway speaks the OpenAI protocol, so the stock client works unmodified:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key="aip_...")
print(client.chat.completions.create(
    model="enterprise-chat",
    messages=[{"role": "user", "content": "Hello"}],
).choices[0].message.content)
```

Mint the key in the admin console under **Endpoints & Keys**, or with the API. It is shown
once — only a hash is stored.

## Where to go next

| I want to… | Read |
|---|---|
| Deploy this properly | [deployment.md](deployment.md) |
| Install with no Internet at all | [airgap.md](airgap.md) |
| Add a GPU machine | [gpu.md](gpu.md) |
| Understand how it fits together | [architecture.md](architecture.md) |
| Write an agent | [agents.md](agents.md) |
| Point it at a hosted model | [models.md](models.md#using-a-hosted-endpoint-openrouter-and-anything-like-it) |

## When something does not work

```bash
make logs                 # tail every core service
docker compose ps         # what is actually running
make check                # lint and the full test suite
```

[troubleshooting.md](troubleshooting.md) covers the failures that have actually happened,
including the ones that look like something else.

## Tearing it down

```bash
make down                 # stop, keep the data
make clean                # stop and DELETE every volume and ./data
```
