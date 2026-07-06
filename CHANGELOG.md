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

[Unreleased]: https://github.com/bcrisp4/bscribe/commits/main
