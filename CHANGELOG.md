# Changelog

All notable changes to bscribe are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

How to maintain this file is documented in [docs/changelog.md](docs/changelog.md):
every code-change PR adds an entry under `[Unreleased]`; at release time that
section is renamed to the new version and becomes the GitHub Release notes.

## [Unreleased]

### Added

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

### Fixed

- Unexpected errors are logged without tracebacks or exception messages, which
  could quote parser internals or user-supplied values (privacy contract).
- Container builds no longer reuse a stale cached wheel when only source files
  changed (uv `cache-keys` now includes `src/**/*.py`).

[Unreleased]: https://github.com/bcrisp4/bscribe/commits/main
