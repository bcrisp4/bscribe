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

# Conversion binaries liteparse shells out to from PATH (verified in the
# liteparse crate `conversion.rs`): ImageMagick for image->PDF, LibreOffice for
# office->PDF. librsvg2-bin provides `rsvg-convert`, ImageMagick 6's SVG decode
# delegate on Debian — without it SVG uploads fail to render; librsvg is also the
# SAFE renderer (it ignores remote/file refs by default, unlike ImageMagick's
# internal MSVG/MVG path). Ghostscript stays because liteparse *gates* SVG/EPS/PS
# inputs on a `gs` binary being present (conversion.rs GHOSTSCRIPT_REQUIRED_...)
# even though the actual SVG render goes through rsvg here. PDFium and Tesseract
# are bundled in the liteparse wheel — never apt-installed. LibreOffice is the
# component subset (no full metapackage, no Java): writer/calc/impress/draw
# cover doc/docx, xls/xlsx/csv, ppt/pptx, odf, rtf. --no-install-recommends
# keeps the image near the low end of the design's accepted ~400MB-1GB cost.
# bookworm ships ImageMagick 6, so the policy path is /etc/ImageMagick-6.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        imagemagick \
        ghostscript \
        librsvg2-bin \
        libreoffice-core \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        libreoffice-draw

# Restrictive ImageMagick policy: ImageMagick is fed untrusted documents, and
# its SVG/MVG/MSL handling is the ImageTragick attack surface (outbound fetches,
# local file reads) that container hardening alone does not stop on the tailnet.
# This REPLACES Debian's stock policy, which disables PDF/PS — we need PDF write
# for image->PDF output and PS/EPS for the Ghostscript path (see the file's comment).
COPY docker/policy.xml /etc/ImageMagick-6/policy.xml

RUN groupadd --system bscribe \
    && useradd --system --gid bscribe --home-dir /app --no-create-home bscribe

WORKDIR /app
# --no-editable installs bscribe into the venv, so only the venv is needed.
COPY --from=builder --chown=bscribe:bscribe /app/.venv /app/.venv

# liteparse's bundled OCR (the tesseract-rs crate) is NOT self-contained: on the
# first OCR it DOWNLOADS eng.traineddata (tessdata_best, ~15MB) from GitHub and
# caches it under $HOME/.tesseract-rs/tessdata. That breaks us three ways — a
# runtime network dependency, a read-only root filesystem (nowhere to cache), and
# the non-root service user (HOME=/app is not writable). So we bake the exact
# file it fetches at build time (network available here) into the service user's
# cache, owned by bscribe and read at runtime — no download, no runtime write.
# HOME is pinned so tesseract-rs resolves the same path the download path uses.
# Pinned to a tessdata_best commit + sha256 so the fetch is reproducible; a
# changed upstream file fails the build loudly rather than shifting silently.
ENV HOME=/app
ADD --chown=bscribe:bscribe \
    --checksum=sha256:8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba \
    https://github.com/tesseract-ocr/tessdata_best/raw/e12c65a915945e4c28e237a9b52bc4a8f39a0cec/eng.traineddata \
    /app/.tesseract-rs/tessdata/eng.traineddata

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
