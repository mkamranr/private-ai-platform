# Private AI Platform — developer entrypoints
#
# Everything runs inside Docker on purpose. The spec requires Python 3.12+ but a
# developer machine may have anything (this one has 3.10), so the container is
# the single source of truth for the toolchain. There is no local venv to drift.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE      := docker compose -f docker-compose.yml -f docker-compose.dev.yml
COMPOSE_PROD := docker compose -f docker-compose.yml -f docker-compose.prod.yml
PY_IMAGE     := python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

# `run` brings up depends_on services and waits for their healthchecks.
RUN_BACKEND  := $(COMPOSE) run --rm --quiet-pull backend

# The CPU-served development model (`make local-llm`). Same defaults as the compose
# service, so overriding one without the other cannot point them at different files.
PLATFORM_DATA_ROOT ?= ./data
LOCAL_LLM_GGUF     ?= qwen2.5-1.5b-instruct-gguf/qwen2.5-1.5b-instruct-q4_k_m.gguf
LOCAL_LLM_NAME     ?= qwen2.5-1.5b-instruct
# Registered as the model's context as well as passed to the engine, so the platform
# never advertises a window the engine will refuse.
LOCAL_LLM_CTX      ?= 16384

.PHONY: help
help: ## Show this help
	@# 0-9 in the class, or every numbered target (gate-phase1 … gate-phase8) is a
	@# documented command that `make help` never mentions.
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
.env: ## Create .env from the template on first run, with generated secrets
	@# Generated, not copied. The template's placeholders are not merely weak — the Fernet
	@# key is rejected outright, so a plain `cp` produces a backend that will not start and
	@# an error that says nothing about the template. Only placeholders are replaced, so
	@# running this against a configured .env cannot clobber real secrets.
	@test -f .env || { \
	  cp .env.example .env; \
	  python3 scripts/gen_secrets.py --env-file .env; \
	  echo "Created .env with generated secrets. Review before production use."; }

.PHONY: secrets
secrets: ## Fill any remaining placeholder secrets in .env (safe to re-run)
	@python3 scripts/gen_secrets.py --env-file .env

.PHONY: up
up: .env ## Start the core stack (postgres, valkey, qdrant, minio, backend, nginx)
	$(COMPOSE) --profile core up -d --build
	@# The default runtime's image, which `--profile core` does not cover: mock-vllm sits in
	@# the `development` profile, so nothing in an ordinary day rebuilds it. It is not a
	@# service that runs here — it is the image the deployment worker starts containers
	@# from, and a stale one is present, healthy and RUNNING while answering 404 to every
	@# route added since it was built, which reads as a broken platform rather than a
	@# stale build. Docker caches it, so this is seconds when nothing changed.
	$(COMPOSE) --profile development build mock-vllm
	@$(MAKE) --no-print-directory wait

.PHONY: wait
wait: ## Block until every core service reports healthy
	@echo "Waiting for core services to become healthy..."
	@for i in $$(seq 1 60); do \
	  unhealthy=$$($(COMPOSE) --profile core ps --format '{{.Service}} {{.Health}}' \
	    | awk '$$2 != "healthy" && $$2 != "" {print $$1}'); \
	  if [ -z "$$unhealthy" ]; then echo "All core services healthy."; exit 0; fi; \
	  sleep 2; \
	done; \
	echo "TIMED OUT. Current state:"; $(COMPOSE) --profile core ps; exit 1

.PHONY: down
down: ## Stop the stack (keeps volumes)
	$(COMPOSE) --profile core down

.PHONY: clean
clean: ## Stop the stack and DELETE all data volumes
	$(COMPOSE) --profile core down -v
	rm -rf ./data

.PHONY: logs
logs: ## Tail logs for all core services
	$(COMPOSE) --profile core logs -f --tail=100

.PHONY: shell
shell: ## Open a shell in the backend container
	$(COMPOSE) exec backend sh

# ---------------------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------------------
.PHONY: test
test: .env ## Run unit + API tests inside the container
	$(RUN_BACKEND) pytest

.PHONY: cov
cov: .env ## Run tests with a coverage report
	$(RUN_BACKEND) pytest --cov --cov-report=term-missing

.PHONY: lint
lint: .env airgap ## Run ruff, mypy and the import-linter layering contracts
	$(COMPOSE) run --rm --no-deps backend sh -c '\
		echo "--- ruff check ---"      && ruff check . && \
		echo "--- ruff format ---"     && ruff format --check . && \
		echo "--- mypy ---"            && mypy app && \
		echo "--- import-linter ---"   && lint-imports --no-cache'

.PHONY: fmt
fmt: ## Auto-fix formatting and lint violations
	$(COMPOSE) run --rm --no-deps backend sh -c 'ruff check --fix . && ruff format .'

.PHONY: airgap
airgap: ## Verify air-gap discipline (pinned deps, digest-pinned images, no runtime fetches)
	@python3 scripts/check_airgap.py

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
.PHONY: migrate
migrate: .env ## Apply all migrations
	$(RUN_BACKEND) alembic upgrade head

.PHONY: revision
revision: .env ## Autogenerate a migration: make revision m="add gpus table"
	@test -n "$(m)" || { echo 'Usage: make revision m="description"'; exit 1; }
	$(RUN_BACKEND) alembic revision --autogenerate -m "$(m)"

.PHONY: migrate-roundtrip
migrate-roundtrip: .env ## Prove migrations reverse cleanly (upgrade head -> downgrade base -> upgrade head)
	$(RUN_BACKEND) sh -c 'alembic upgrade head && alembic downgrade base && alembic upgrade head'

.PHONY: seed
seed: .env ## Seed roles, permissions and the bootstrap admin
	$(RUN_BACKEND) python -m app.utils.cli seed

# ---------------------------------------------------------------------------
# Dependency locking (requires network — build machine only, never the target)
# ---------------------------------------------------------------------------
.PHONY: lock
lock: ## Recompile requirements*.txt from requirements*.in with hashes
	docker run --rm -v "$(CURDIR)/backend:/w" -w /w $(PY_IMAGE) sh -c '\
		pip install -q --no-cache-dir --disable-pip-version-check --root-user-action=ignore pip-tools==7.6.0 && \
		pip-compile --quiet --generate-hashes --strip-extras --output-file=requirements.txt requirements.in && \
		pip-compile --quiet --generate-hashes --strip-extras --output-file=requirements-dev.txt requirements-dev.in'
	docker run --rm -v "$(CURDIR)/node-agent:/w" -w /w $(PY_IMAGE) sh -c '\
		pip install -q --no-cache-dir --disable-pip-version-check --root-user-action=ignore pip-tools==7.6.0 && \
		pip-compile --quiet --generate-hashes --strip-extras --output-file=requirements.txt requirements.in && \
		pip-compile --quiet --generate-hashes --strip-extras --output-file=requirements-dev.txt requirements-dev.in'
	docker run --rm -v "$(CURDIR)/mock-vllm:/w" -w /w $(PY_IMAGE) sh -c '\
		pip install -q --no-cache-dir --disable-pip-version-check --root-user-action=ignore pip-tools==7.6.0 && \
		pip-compile --quiet --generate-hashes --strip-extras --output-file=requirements.txt requirements.in && \
		pip-compile --quiet --generate-hashes --strip-extras --output-file=requirements-dev.txt requirements-dev.in'
	docker run --rm -v "$(CURDIR)/mcp/ldap:/w" -w /w $(PY_IMAGE) sh -c '\
		pip install -q --no-cache-dir --disable-pip-version-check --root-user-action=ignore pip-tools==7.6.0 && \
		pip-compile --quiet --generate-hashes --strip-extras --output-file=requirements.txt requirements.in && \
		pip-compile --quiet --generate-hashes --strip-extras --output-file=requirements-dev.txt requirements-dev.in'
	@echo "Lockfiles regenerated for every service. Review the diff before committing."

# ---------------------------------------------------------------------------
# Phase 0 acceptance gate
# ---------------------------------------------------------------------------
.PHONY: gate
gate: ## Run the Phase 0 acceptance gate
	@bash scripts/phase0_gate.sh

.PHONY: gate-phase1
gate-phase1: ## Run the Phase 1 acceptance gate (nodes, GPUs, containers)
	@bash scripts/phase1_gate.sh

.PHONY: gate-phase2
gate-phase2: ## Run the Phase 2 acceptance gate (registry, deployment, gateway)
	@bash scripts/phase2_gate.sh

.PHONY: gate-phase3
gate-phase3: ## Run the Phase 3 acceptance gate (chat, attribution, dashboard)
	@bash scripts/phase3_gate.sh

.PHONY: gate-phase5
gate-phase5: ## Run the Phase 5 acceptance gate (knowledge, RAG, memory scoping)
	@bash scripts/phase5_gate.sh

gate-phase6: .env ## Run the Phase 6 acceptance gate (M03, M20, M24, M25)
	@bash scripts/phase6_gate.sh

.PHONY: gate-phase7
gate-phase7: .env ## Run the Phase 7 acceptance gate (metrics, logs, traces, dashboards)
	@bash scripts/phase7_gate.sh

.PHONY: gate-phase9
gate-phase9: .env ## Run the Phase 9 acceptance gate (speech, OCR, vision)
	@bash scripts/phase9_gate.sh

.PHONY: gate-phase8
gate-phase8: .env ## Run the Phase 8 acceptance gate (offline install, upgrade, rollback)
	@bash scripts/phase8_gate.sh

# ---------------------------------------------------------------------------
# Observability (M19) — Phase 7
# ---------------------------------------------------------------------------
.PHONY: monitoring
monitoring: .env ## Start Prometheus, Loki, Tempo, Grafana, Alloy and Langfuse
	$(COMPOSE) --profile core --profile monitoring up -d
	@$(MAKE) --no-print-directory wait
	@echo "Grafana:  http://localhost:$${DEV_HTTP_PORT:-8080}/grafana/   (admin / GRAFANA__ADMIN_PASSWORD)"
	@echo "Langfuse: http://localhost:$${DEV_LANGFUSE_PORT:-8084}/   (its own port: Next.js cannot be sub-path hosted)"
	@echo
	@echo "Exporters stay OFF until you say so — set TRACING__ENABLED=true and/or"
	@echo "LANGFUSE__ENABLED=true in .env, then: make restart-backend"

.PHONY: restart-backend
restart-backend: .env ## Recreate the backend so a changed .env takes effect
	$(COMPOSE) up -d --force-recreate --no-deps backend

# ---------------------------------------------------------------------------
# Offline bundle (M23, M27) — Phase 8
# ---------------------------------------------------------------------------
.PHONY: bundle
bundle: ## Build the offline install bundle (BUILD MACHINE — the one step that uses the network)
	@# Images are saved from the local daemon and never pulled implicitly, so they are
	@# built here rather than assumed. Leaving this to the operator has a failure mode
	@# worse than a missing image: a **stale** one. `docker save` ships whatever the tag
	@# currently points at, so a tag left over from last week produces a bundle whose
	@# tree/ is today's source and whose backend is not — and every presence check the
	@# Phase 8 gate makes still passes, because the image is there. It runs the wrong code.
	$(COMPOSE) --profile core --profile development --profile agents \
		build backend node-agent mock-vllm ldap-mcp
	@python3 scripts/build_bundle.py $(if $(MODELS),--models,) $(if $(CHAT),--with-chat,) \
		$(if $(MONITORING),--with-monitoring,)

.PHONY: bundle-dry
bundle-dry: ## Show what the bundle would contain, without writing it
	@python3 scripts/build_bundle.py --dry-run

# ---------------------------------------------------------------------------
# MCP servers (M13)
# ---------------------------------------------------------------------------
.PHONY: mcp-vendor
mcp-vendor: ## Vendor MCP servers from mcp/manifests into images (BUILD MACHINE — needs network)
	@python3 scripts/vendor_mcp_servers.py $(if $(SERVER),--server $(SERVER),)

.PHONY: mcp
mcp: .env ## Start the vendored MCP servers
	@test -f docker-compose.mcp.yml || { \
	  echo "No docker-compose.mcp.yml — run 'make mcp-vendor' on a connected machine first."; \
	  exit 1; }
	$(COMPOSE) -f docker-compose.mcp.yml --profile core --profile agents up -d
	@echo "MCP servers started. Register them with 'make mcp-import'."

.PHONY: definitions-import
definitions-import: .env ## Import the shipped agent, skill and tool definitions (M10-M12)
	@$(RUN_BACKEND) python -m app.utils.cli definitions-import

.PHONY: mcp-import
mcp-import: .env ## Register every MCP server manifest and discover its tools
	@$(RUN_BACKEND) python -m app.utils.cli mcp-import

.PHONY: ollama-import
ollama-import: .env ## Register the models a running Ollama already serves (M07)
	@# `exec`, not `run`: this talks to the already-running backend. Through $(COMPOSE) so
	@# the dev override applies — it previously used a COMPOSE_FILES variable that was
	@# never assigned anywhere, so the command silently ran with no -f flags at all.
	@$(COMPOSE) exec -T backend python -m app.utils.cli ollama-import

.PHONY: external-import
external-import: .env ## Register the configured hosted endpoint and alias it (NOT air-gapped)
	@$(RUN_BACKEND) python -m app.utils.cli external-import

.PHONY: local-llm
local-llm: .env ## Serve a small real model on CPU (llama.cpp) and point enterprise-chat at it
	@# Weights are not in the repo and not in the bundle — a GGUF file is several hundred
	@# megabytes of someone else's model. Checked for by name rather than downloaded: this
	@# target must not become the one place the platform fetches from the Internet.
	@test -f "$(PLATFORM_DATA_ROOT)/models/$(LOCAL_LLM_GGUF)" \
	  || { echo "Missing $(PLATFORM_DATA_ROOT)/models/$(LOCAL_LLM_GGUF)"; \
	       echo "Download a GGUF into that path, or set LOCAL_LLM_GGUF to one you have."; \
	       exit 1; }
	$(COMPOSE) --profile local-llm up -d llamacpp
	@echo "Waiting for the weights to load (CPU, so tens of seconds)..."
	@for i in $$(seq 1 60); do \
	  [ "$$(docker inspect ai-platform-llamacpp-1 --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null)" = "healthy" ] && break; \
	  sleep 5; \
	done
	@$(RUN_BACKEND) python -m app.utils.cli external-import \
	  --endpoint http://llamacpp:8080 --model "$(LOCAL_LLM_NAME)" --context "$(LOCAL_LLM_CTX)"

backup: .env ## Take a full backup (M25)
	@python3 scripts/backup.py create

backup-list: ## List backups taken so far
	@python3 scripts/backup.py list

backup-verify: ## Prove a backup is restorable: make backup-verify B=backups/2026...
	@test -n "$(B)" || (echo "Usage: make backup-verify B=backups/<stamp>" && exit 2)
	@python3 scripts/backup.py verify "$(B)"

backup-restore: .env ## REPLACE this platform's data: make backup-restore B=backups/<stamp>
	@test -n "$(B)" || (echo "Usage: make backup-restore B=backups/<stamp>" && exit 2)
	@python3 scripts/backup.py restore "$(B)"

.PHONY: reconcile
reconcile: .env ## Find orphaned model containers (add REMOVE=1 to remove them)
	@$(RUN_BACKEND) python -m app.utils.cli reconcile $(if $(REMOVE),--remove,)

.PHONY: gate-phase4
gate-phase4: ## Run the Phase 4 acceptance gate (the §20 MVP scenario)
	@bash scripts/phase4_gate.sh

.PHONY: agents
agents: .env ## Start the stack with agents and chat (LDAP MCP, Open WebUI)
	@set -a; . ./.env; set +a; \
	 if [ -z "$$OPEN_WEBUI__GATEWAY_API_KEY" ]; then \
	   echo "No chat credentials in .env — provisioning."; \
	   $(MAKE) --no-print-directory chat-key; \
	 fi
	$(COMPOSE) --profile core --profile chat --profile agents up -d --build
	@$(MAKE) --no-print-directory wait-agents

.PHONY: wait-agents
wait-agents: ## Block until every core, chat and agent service is healthy
	@echo "Waiting for services to become healthy..."
	@for i in $$(seq 1 90); do \
	  unhealthy=$$($(COMPOSE) --profile core --profile chat --profile agents ps \
	    --format '{{.Service}} {{.Health}}' | awk '$$2 != "healthy" && $$2 != "" {print $$1}'); \
	  if [ -z "$$unhealthy" ]; then echo "All services healthy."; exit 0; fi; \
	  sleep 2; \
	done; \
	echo "TIMED OUT. Current state:"; \
	$(COMPOSE) --profile core --profile chat --profile agents ps; exit 1

.PHONY: test-agent
test-agent: ## Run the node-agent test suite
	docker build -q -t ai-platform/node-agent:dev --target dev node-agent >/dev/null
	docker run --rm -e NODE_AGENT_AUTH_TOKEN=$$(openssl rand -hex 32) \
		ai-platform/node-agent:dev pytest -p no:cacheprovider

.PHONY: lint-agent
lint-agent: ## Lint and typecheck the node agent
	docker build -q -t ai-platform/node-agent:dev --target dev node-agent >/dev/null
	docker run --rm -e NODE_AGENT_AUTH_TOKEN=$$(openssl rand -hex 32) \
		-e RUFF_CACHE_DIR=/tmp/ruff -e MYPY_CACHE_DIR=/tmp/mypy ai-platform/node-agent:dev sh -c \
		'ruff check . && ruff format --check . && mypy app && lint-imports --no-cache'

.PHONY: test-mock
test-mock: ## Run the mock-vLLM test suite
	docker build -q -t ai-platform/mock-vllm:dev --target dev mock-vllm >/dev/null
	docker run --rm ai-platform/mock-vllm:dev pytest -p no:cacheprovider

.PHONY: lint-mock
lint-mock: ## Lint and typecheck mock-vLLM
	docker build -q -t ai-platform/mock-vllm:dev --target dev mock-vllm >/dev/null
	docker run --rm -e RUFF_CACHE_DIR=/tmp/ruff -e MYPY_CACHE_DIR=/tmp/mypy \
		ai-platform/mock-vllm:dev sh -c 'ruff check . && ruff format --check . && mypy app'

.PHONY: test-ldap-mcp
test-ldap-mcp: ## Run the LDAP MCP server test suite
	docker build -q -t ai-platform/ldap-mcp:dev --target dev mcp/ldap >/dev/null
	docker run --rm ai-platform/ldap-mcp:dev pytest -p no:cacheprovider

.PHONY: lint-ldap-mcp
lint-ldap-mcp: ## Lint and typecheck the LDAP MCP server
	docker build -q -t ai-platform/ldap-mcp:dev --target dev mcp/ldap >/dev/null
	docker run --rm -e RUFF_CACHE_DIR=/tmp/ruff -e MYPY_CACHE_DIR=/tmp/mypy \
		ai-platform/ldap-mcp:dev sh -c 'ruff check . && ruff format --check . && mypy app'

.PHONY: check
check: lint lint-agent lint-mock lint-ldap-mcp test test-agent test-mock test-ldap-mcp ## Everything: lint and test every service

# ---------------------------------------------------------------------------
# Chat (M17)
# ---------------------------------------------------------------------------
.PHONY: chat-key
chat-key: .env ## Rotate Open WebUI's gateway credentials into .env
	@set -e; \
	out=$$($(RUN_BACKEND) python -m app.utils.cli chat-key 2>/dev/null | tr -d '\r'); \
	test -n "$$out" || { echo "chat-key produced nothing — is the stack up and seeded?"; exit 1; }; \
	if ! grep -q '^OPEN_WEBUI__SECRET_KEY=' .env 2>/dev/null; then \
	  out="$$out"$$'\n'"OPEN_WEBUI__SECRET_KEY=$$(openssl rand -hex 32)"; \
	fi; \
	while IFS= read -r line; do \
	  key=$${line%%=*}; \
	  if grep -q "^$$key=" .env; then \
	    tmp=$$(mktemp); grep -v "^$$key=" .env > "$$tmp"; mv "$$tmp" .env; \
	  fi; \
	  printf '%s\n' "$$line" >> .env; \
	done <<< "$$out"; \
	echo "Wrote OPEN_WEBUI__* credentials to .env. Previous keys for this client were revoked."

.PHONY: chat
chat: .env ## Start Open WebUI against the gateway
	@# Provisions only when credentials are missing. `chat-key` revokes the client's
	@# previous keys, so calling it on every start would invalidate the credentials of
	@# an Open WebUI that is already running — including another replica.
	@set -a; . ./.env; set +a; \
	 if [ -z "$$OPEN_WEBUI__GATEWAY_API_KEY" ] || [ -z "$$OPEN_WEBUI__IDENTITY_JWT_SECRET" ] \
	    || [ -z "$$OPEN_WEBUI__SECRET_KEY" ]; then \
	   echo "No chat credentials in .env — provisioning."; \
	   $(MAKE) --no-print-directory chat-key; \
	 fi
	$(COMPOSE) --profile core --profile chat up -d --build
	@$(MAKE) --no-print-directory wait
	@echo "Chat: http://localhost:$${DEV_CHAT_PORT:-8081}   (production: https://chat.<your-host>)"
