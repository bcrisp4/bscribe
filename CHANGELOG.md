# Changelog

All notable changes to bscribe are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

How to maintain this file is documented in [docs/changelog.md](docs/changelog.md):
every code-change PR adds an entry under `[Unreleased]`; at release time that
section is renamed to the new version and becomes the GitHub Release notes.

## [Unreleased]

### Added

- Asynchronous conversion jobs: `POST /v1/jobs` accepts the same upload and
  parameters as `/v1/convert` but returns a job id immediately (`201`).
  Poll `GET /v1/jobs/{id}` for status (`queued | running | done | failed`),
  fetch the finished document from `GET /v1/jobs/{id}/result` (`202` while
  in progress, `409` if the job failed), and list your jobs with
  `GET /v1/jobs` (newest first, optional `?status=` filter). Jobs run on
  the same worker pool as synchronous conversions, are visible only to the
  bearer token that created them, and their uploads are deleted as soon as
  parsing finishes.

## [0.1.0] - 2026-07-07

### Added

- `POST /v1/convert`: synchronous document conversion. Upload a file
  (multipart `file`) and get the extracted text back inline as `markdown`
  (default) or `text`, with `ocr=auto` (default) or `off`. Accepts PDFs,
  images, and office documents; unsupported formats return `415`, uploads
  over the size limit (`BSCRIBE_MAX_UPLOAD_BYTES`, default 50 MB) return
  `413`, and unparseable documents return `422`. Uploads are deleted as soon
  as parsing finishes, and document content is never logged.
- Project bootstrap: uv-managed Python 3.14 project, `GET /healthz`, container
  image, and the CI / changelog / release toolchain.
- Environment-driven configuration via `BSCRIBE_`-prefixed variables (worker
  count, job timeout, max upload size, scratch dir, database path, result TTL,
  log level), validated at startup.
- Structured JSON logging to stdout, including one access-log line per request
  (method, path, status, duration — never query strings or document content).
- Error responses now use RFC 9457 `application/problem+json`; malformed
  requests return `400`, and unexpected failures return `500` without leaking
  internal details.
- All document parsing now runs on a warm pool of worker processes (default 4,
  `BSCRIBE_WORKER_COUNT`): each job is killed at a hard deadline
  (`BSCRIBE_JOB_TIMEOUT_SECONDS`, default 10 minutes), a crashing parse takes
  down only its own disposable worker, and workers are recycled after
  `BSCRIBE_WORKER_MAX_TASKS` jobs (default 100) to bound native-library leaks.
- Bearer-token authentication backed by a SQLite token table: requests without
  a valid token receive `401` with `WWW-Authenticate: Bearer`, and revoking a
  token takes effect immediately — no restart. Token secrets are stored only
  as SHA-256 hashes and never appear in logs.
- `bscribe` command-line interface: `serve` runs the server (now the container
  command, with a built-in `HEALTHCHECK`), `healthcheck` probes liveness, and
  `token add/list/delete` manage bearer tokens locally on the host — token
  management is deliberately not available over HTTP. Secrets are shown once
  at creation and cannot be recovered. The token database lives at
  `/data/bscribe.db` in the container (`BSCRIBE_DB_PATH`); outside the
  container it defaults to `~/.local/share/bscribe/bscribe.db`, and
  `token list`/`token delete` fail cleanly instead of creating a new empty
  database when pointed at a missing file.
- `bscribe serve` binds loopback by default (the container binds all
  interfaces via `BSCRIBE_HOST`), reads `BSCRIBE_HOST`/`BSCRIBE_PORT` from the
  environment, and passes any extra arguments straight to uvicorn — the full
  uvicorn flag surface (`--proxy-headers`, `--root-path`, …) remains
  available. Setting `BSCRIBE_PORT` moves the server and the container health
  probe together.
- The container image now bundles the full conversion toolchain (ImageMagick,
  LibreOffice, Ghostscript, librsvg), so image, SVG, and office-document
  conversion work in the shipped image rather than only in a dev checkout. OCR
  language data is baked in at build time, so scanned-document OCR runs fully
  offline — a conversion never makes an outbound request.
- Deployment guide ([docs/deployment.md](docs/deployment.md)) with the hardened
  run recipe and a note that a writable `/tmp` (tmpfs) is required.

### Fixed

- Unexpected errors are logged without tracebacks or exception messages, which
  could quote parser internals or user-supplied values (privacy contract).
- Container builds no longer reuse a stale cached wheel when only source files
  changed (the image build now forces the project wheel to rebuild with
  `uv sync --reinstall-package bscribe`).

### Security

- The container is hardened for a locked-down runtime: it runs as a non-root
  user with a read-only root filesystem, all Linux capabilities dropped, and no
  privilege escalation — the only writable surface is a tmpfs scratch and the
  `/data` volume. A restrictive ImageMagick policy ships in the image, and SVGs
  render through librsvg, which does not fetch remote resources — closing the
  outbound-fetch and local-file-read (ImageTragick) surface that untrusted
  documents could otherwise reach. See [docs/deployment.md](docs/deployment.md).

## [0.1.0-rc1] - 2026-07-07

First release candidate for 0.1.0 — cut to validate the release pipeline
(native multi-arch build, GHCR publish, provenance attestation) before the
final tag. The changes are those listed under [0.1.0].

[Unreleased]: https://github.com/bcrisp4/bscribe/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/bcrisp4/bscribe/releases/tag/v0.1.0
[0.1.0-rc1]: https://github.com/bcrisp4/bscribe/releases/tag/v0.1.0-rc1
