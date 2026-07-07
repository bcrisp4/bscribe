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
# stale cached wheel. --reinstall-package bscribe forces the project wheel
# itself to rebuild every time: the cache keys deliberately exclude src/**
# (see pyproject.toml), so without this a src-only change would ship the
# previous cached wheel.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --reinstall-package bscribe

# --- Runtime: slim image, non-root ---
# Match the builder's Debian release (bookworm) so the interpreter and glibc
# line up across stages; the uv builder image is bookworm-based.
FROM python:3.14-slim-bookworm AS runtime

RUN groupadd --system bscribe \
    && useradd --system --gid bscribe --home-dir /app --no-create-home bscribe

WORKDIR /app
# --no-editable installs bscribe into the venv, so only the venv is needed.
COPY --from=builder --chown=bscribe:bscribe /app/.venv /app/.venv

# /data holds the SQLite database (tokens now, jobs from M2). /app stays
# root-owned (read-only for the service user), so the default db_path must
# point somewhere bscribe can write — without this, `bscribe serve` dies on
# startup creating the token schema.
RUN mkdir /data && chown bscribe:bscribe /data
ENV BSCRIBE_DB_PATH=/data/bscribe.db
# serve defaults to loopback outside the container; in here, bind all
# interfaces. Change the port with BSCRIBE_PORT (not `serve --port`) so the
# HEALTHCHECK probe follows it.
ENV BSCRIBE_HOST=0.0.0.0
VOLUME /data

ENV PATH="/app/.venv/bin:${PATH}"
USER bscribe
EXPOSE 8000

# Exec-form CMD keeps the probe shell-free. Note: `podman build` defaults to
# OCI image format, which silently drops HEALTHCHECK — build with
# `--format docker` (make image and CI buildx both emit docker format).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["bscribe", "healthcheck"]

# ENTRYPOINT/CMD split: `serve` is the default; admin commands run against
# the live server's database via `podman exec <container> bscribe token …`.
# (A fresh `podman run IMAGE token …` would get its own anonymous /data
# volume — an empty, unrelated database — so exec into the running
# container instead.)
ENTRYPOINT ["bscribe"]
CMD ["serve"]
