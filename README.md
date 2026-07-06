# bscribe

Self-hosted HTTP service that converts documents (PDFs, office documents,
images) into plain text or markdown, for consumption by other self-hosted
services. See [docs/design.md](docs/design.md) for the full design.

> **Status:** bootstrapping. Only `GET /healthz` exists today; the conversion
> API arrives in M1 (see the design's milestones).

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

## Development

```bash
make check   # lint, typecheck, audit, test — matches CI
make fmt     # format + autofix
```

## Docs

- [Design](docs/design.md)
- [Releasing](docs/releasing.md)
- [Changelog policy](docs/changelog.md)
