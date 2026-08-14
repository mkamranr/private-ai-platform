# Private AI Platform — control plane backend
#
# AIR-GAP RULES (spec §M23, Rule 4):
#   * The base image is pinned by DIGEST, not by tag. A floating tag makes the
#     offline bundle unreproducible and lets the image drift between the
#     connected build machine and the air-gapped target.
#   * No apt-get at build time. Every runtime dependency resolves to a Python
#     wheel, so the image needs no package manager and no compiler.
#   * Dependencies install with --require-hashes from a fully compiled lockfile.
#   * Nothing is fetched at *runtime*. Ever.
#
# Offline build: populate offline/python-wheels, then
#   docker build --build-arg PIP_FLAGS="--no-index --find-links=/wheels" ...
# with the wheelhouse mounted at /wheels (see scripts/build_offline_bundle.sh).

# python:3.12-slim — digest resolved 2026-08-07 (linux/amd64 + arm64 index)
FROM python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Unprivileged runtime user. A control plane that can start containers on GPU
# nodes must not itself run as root (§25).
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app


# ---------------------------------------------------------------------------
# deps — build the virtualenv once, share it with every downstream stage
# ---------------------------------------------------------------------------
FROM base AS deps

ARG PIP_FLAGS=""

RUN python -m venv "$VIRTUAL_ENV"

COPY requirements.txt ./
RUN pip install ${PIP_FLAGS} --require-hashes --no-deps -r requirements.txt


# ---------------------------------------------------------------------------
# dev — adds test/lint tooling. `make test` and `make lint` run in this stage.
# Never deployed.
# ---------------------------------------------------------------------------
FROM deps AS dev

ARG PIP_FLAGS=""

COPY requirements-dev.txt ./
RUN pip install ${PIP_FLAGS} --require-hashes --no-deps -r requirements-dev.txt

COPY --chown=app:app . .

USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]


# ---------------------------------------------------------------------------
# runtime — production image: venv + source, no build tooling, no test deps
# ---------------------------------------------------------------------------
FROM base AS runtime

COPY --from=deps --chown=app:app /opt/venv /opt/venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app app ./app

USER app
EXPOSE 8000

# stdlib-only healthcheck: the slim image intentionally ships neither curl nor wget.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=4).status == 200 else sys.exit(1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
