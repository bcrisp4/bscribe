# syntax=docker/dockerfile:1

# --- Builder: resolve and install dependencies into a venv ---
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

# Version comes from the git tag via setuptools-scm, injected at build time.
# .git is deliberately not in the build context; the pretend-version env var
# supplies the version. Release passes the real tag; CI/local default to 0.0.0.
ARG SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSCRIBE=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSCRIBE=${SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSCRIBE} \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# --no-editable so the venv is self-contained (no .pth pointing at /app/src),
# making the runtime copy robust. Cache mount keys on the pretend-version env
# via [tool.uv] cache-keys, so a changed version rebuilds rather than serving a
# stale cached wheel.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- Runtime: slim image, non-root ---
# Match the builder's Debian release (bookworm) so the interpreter and glibc
# line up across stages; the uv builder image is bookworm-based.
FROM python:3.14-slim-bookworm AS runtime

RUN groupadd --system bscribe \
    && useradd --system --gid bscribe --home-dir /app --no-create-home bscribe

WORKDIR /app
# --no-editable installs bscribe into the venv, so only the venv is needed.
COPY --from=builder --chown=bscribe:bscribe /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}"
USER bscribe
EXPOSE 8000

ENTRYPOINT ["uvicorn", "--factory", "bscribe.app:create_app", \
            "--host", "0.0.0.0", "--port", "8000"]
