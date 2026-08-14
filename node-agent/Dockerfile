# Private AI Platform — node agent (M04)
#
# Deployed to every managed host, so it is kept as small as the control plane image:
# each dependency is another thing to bundle, patch and review across the fleet.
#
# Same air-gap rules as backend/Dockerfile — digest-pinned base, no apt at build time,
# hash-pinned dependency install, nothing fetched at runtime (§M23, Rule 4).

# python:3.12-slim — digest resolved 2026-08-07 (linux/amd64 + arm64 index)
FROM python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app


FROM base AS deps
ARG PIP_FLAGS=""
RUN python -m venv "$VIRTUAL_ENV"
COPY requirements.txt ./
RUN pip install ${PIP_FLAGS} --require-hashes --no-deps -r requirements.txt


FROM deps AS dev
ARG PIP_FLAGS=""
COPY requirements-dev.txt ./
RUN pip install ${PIP_FLAGS} --require-hashes --no-deps -r requirements-dev.txt
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9100", "--reload"]


FROM base AS runtime
COPY --from=deps /opt/venv /opt/venv
COPY app ./app

# Runs as root, unlike the control plane. This is a considered exception, not an
# oversight: the agent's whole purpose is to talk to /var/run/docker.sock, whose
# group id varies by host (docker on most distributions, but not all, and Docker
# Desktop differs again). A fixed uid/gid in the image would break on hosts that
# assign a different one, and the agent would fail in a way that looks like a
# permissions bug rather than a configuration mismatch.
#
# The real containment is elsewhere: bearer-token auth on every endpoint, the
# managed-label guard refusing control of containers the platform did not create,
# and the agent never being published beyond the internal network.
#
# To run unprivileged, set the host's docker group id at deploy time:
#   docker run --user 1000:$(getent group docker | cut -d: -f3) ...

EXPOSE 9100

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:9100/health', timeout=4).status == 200 else sys.exit(1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9100"]
