# bscribe

Self-hosted HTTP service that converts documents (PDFs, office documents,
images) into plain text or markdown, for consumption by other self-hosted
services. See [docs/design.md](docs/design.md) for the full design.

> **Status:** M1 in progress. Synchronous conversion (`POST /v1/convert`),
> bearer-token auth, and the `bscribe` CLI have landed; async jobs, the
> re-ingestion contract, and metrics arrive in M2–M3 (see the design's
> milestones).

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14.

```bash
uv sync                       # create the venv from the lockfile
uv run uvicorn --factory bscribe.app:create_app --reload
curl localhost:8000/healthz   # {"status":"ok"}
```

### Container

```bash
docker build -t bscribe:dev .
docker run --rm -p 8000:8000 bscribe:dev
```

The published image (`ghcr.io/bcrisp4/bscribe`, multi-arch amd64 + arm64) is
built to run hardened — non-root, read-only root filesystem, all capabilities
dropped. A writable `/tmp` (tmpfs) is required. See
[docs/deployment.md](docs/deployment.md) for the full run recipe and the
podman `--format docker` note.

## Development

```bash
make check   # lint, typecheck, audit, test — matches CI
make fmt     # format + autofix
```

## Docs

- [Design](docs/design.md)
- [Deployment](docs/deployment.md)
- [Releasing](docs/releasing.md)
- [Changelog policy](docs/changelog.md)
