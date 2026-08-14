# Mock vLLM — OpenAI-compatible inference server for GPU-free development (§M26).
#
# Same air-gap rules as every other image: digest-pinned base, no apt at build time,
# hash-pinned dependency install, nothing fetched at runtime (§M23, Rule 4).
#
# Shipped in the `development` Compose profile only. It must never be deployed as a
# model runtime on a real GPU host — it returns synthetic text, and a response that
# looks plausible from a misconfigured production deployment would be far worse than
# an outage. The responses say so explicitly for that reason.

FROM python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

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
COPY --chown=app:app . .
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS runtime
COPY --from=deps --chown=app:app /opt/venv /opt/venv
COPY --chown=app:app app ./app
USER app
EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=4s --start-period=2s --retries=20 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else sys.exit(1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
