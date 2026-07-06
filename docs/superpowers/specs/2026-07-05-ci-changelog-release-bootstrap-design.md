# bscribe — CI, Changelog & Release Bootstrap

| | |
|---|---|
| Author | Ben Crisp (ben@thecrisp.io) |
| Status | Approved |
| Created | 2026-07-05 |
| Updated | 2026-07-05 |

## Objective

Bootstrap the bscribe repository with project scaffolding and the full
CI / changelog / release toolchain, ported from `bfeed` but adapted for a
modern Python project (uv, ruff, pyright, mypy, pytest) that ships as a
multi-arch container image.

This delivers *no* bscribe features (those are M1 in [design.md](../../design.md)).
It delivers a green CI pipeline, a curated-changelog policy, and a tag-driven
container release — the same operational spine `bfeed` has.

## Scope

In scope:

- A minimal-but-real uv project skeleton so CI has something to run and the
  release workflow has an image to build.
- `ci.yml`: test, lint, typecheck, audit, image-smoke, changelog jobs.
- `release.yml`: tag-triggered multi-arch container build + GHCR push + SBOM +
  build-provenance attestation + GitHub Release from the changelog.
- Keep a Changelog policy (`CHANGELOG.md` + `docs/changelog.md`).
- Release runbook (`docs/releasing.md`).
- Dependabot for the `github-actions` and `uv` ecosystems.

Out of scope (deferred to M1+):

- The conversion API, liteparse adapter, SQLite job store, `/metrics`, auth.
- ImageMagick / LibreOffice / liteparse system dependencies in the image.
- The Claude bot workflows (`claude.yml`, `claude-code-review.yml`).

## Repository layout after this work

```
pyproject.toml            # metadata, deps, ruff/pyright/mypy/pytest/coverage config
uv.lock
src/bscribe/__init__.py
src/bscribe/_version.py    # written by setuptools-scm at build time (gitignored)
src/bscribe/app.py        # FastAPI app factory + GET /healthz only
tests/unit/test_health.py
Dockerfile                # multi-stage, uv-based, non-root
.dockerignore
.gitignore
LICENSE                   # MIT
README.md
Makefile
CHANGELOG.md
docs/changelog.md         # changelog policy
docs/releasing.md         # release runbook
.github/workflows/ci.yml
.github/workflows/release.yml
.github/dependabot.yml
```

## Project skeleton

### `pyproject.toml`

- `[project]`: `name = "bscribe"`, `requires-python = ">=3.14"`,
  `dynamic = ["version"]`, `license = "MIT"`. Runtime deps: `fastapi`,
  `uvicorn[standard]`.
- `[build-system]`: `requires = ["setuptools>=77", "setuptools-scm>=8"]`,
  `build-backend = "setuptools.build_meta"`.
- `[tool.setuptools_scm]`: `version_file = "src/bscribe/_version.py"`.
- `[dependency-groups] dev`: `ruff`, `pyright`, `mypy`, `pytest`,
  `pytest-asyncio`, `pytest-cov`, `pytest-xdist`, `httpx` (for the
  `TestClient`).
- `[tool.ruff]`: `line-length = 88`, target `py314`. `[tool.ruff.lint]`
  `select` = the broad set from the Python style guide (`E F W I UP B SIM TCH
  RUF PTH ASYNC S T20 ARG FBT A C4 DTZ ISC PIE RSE RET TID`), with a
  per-file-ignore lifting `S101` (assert) and `ARG`/`FBT` noise for `tests/`.
- `[tool.pyright]`: `typeCheckingMode = "strict"`, `pythonVersion = "3.14"`,
  `venvPath = "."`, `venv = ".venv"`.
- `[tool.mypy]`: `strict = true`, `python_version = "3.14"`.
- `[tool.pytest.ini_options]`: `--strict-markers`, `asyncio_mode = "auto"`,
  `testpaths = ["tests"]`.
- `[tool.coverage.run]`: `branch = true`, `parallel = true`,
  `source = ["bscribe"]`, `omit = ["*/_version.py"]` (the generated version
  file would otherwise dilute the coverage figure).
- `[tool.uv]`: `cache-keys` include the `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSCRIBE`
  env var — see Version source for why.

### Version source (git tag → setuptools-scm)

The package version is derived from the git tag by `setuptools-scm`. `pyproject`
declares `dynamic = ["version"]`; at build time setuptools-scm computes the
version from `git describe` and writes `src/bscribe/_version.py`. At runtime the
service reports its version via `importlib.metadata.version("bscribe")`
(feeding `GET /v1/info` in M3). `src/bscribe/_version.py` is gitignored.

**Container build friction and its fix.** setuptools-scm needs `.git` at build
time, which is deliberately *not* copied into the image. The Dockerfile takes a
build arg and exports it as the setuptools-scm pretend-version env var:

```dockerfile
ARG SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSCRIBE=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSCRIBE=${SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSCRIBE}
```

- CI's image-smoke build uses the `0.0.0` default (version is irrelevant there).
- `release.yml` passes the real tag version:
  `--build-arg SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSCRIBE=${version}`.

**uv build-cache caveat.** uv keys its built-project cache on file contents,
not env vars, so changing only the build arg would otherwise serve a stale
cached wheel with the wrong version. `[tool.uv] cache-keys` lists the
pretend-version env var so uv rebuilds when it changes. The Dockerfile also
uses `uv sync --no-editable` so the version is baked into the installed
distribution, not a live `.pth` into the source tree.

The git tag stays the single source of truth; no `.git` in the image, no
committed version string to drift.

### `src/bscribe/app.py`

An `create_app() -> FastAPI` factory exposing a single unauthenticated
`GET /healthz` returning `{"status": "ok"}`. This is scaffolding, not M1 —
enough to give the test suite and the Dockerfile a real target.

### `tests/unit/test_health.py`

Arrange-Act-Assert test hitting `/healthz` through Starlette's `TestClient`,
asserting `200` and the concrete body. Mirrors `src/` layout per the style
guide. Drives the `create_app` factory into existence (TDD).

### `Dockerfile`

Multi-stage, uv-based:

- Builder stage `FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim`;
  `uv sync --locked --no-dev --no-editable` into a `.venv`; takes the
  pretend-version build arg.
- Runtime stage `FROM python:3.14-slim-bookworm` (Debian release matched to the
  builder so the interpreter/glibc line up); copies only the `.venv` (the
  `--no-editable` install is self-contained); creates and runs as a non-root
  user; `ENTRYPOINT` runs uvicorn against `bscribe.app:create_app`.
- Ships no ImageMagick / LibreOffice / liteparse yet (M1). Read-only-rootfs
  compatible (scratch/tmp are M1 concerns).

### `Makefile`

Thin wrappers over `uv run` so local and CI commands match:
`test`, `lint` (ruff check + format --check), `fmt` (ruff format + `--fix`),
`typecheck` (pyright + mypy), `audit` (uv audit), `check` (all of the above),
`image` (local `docker build`).

### `README.md`

Per the style guide: brief description, quick start (`uv sync`,
`uv run uvicorn ...`, `docker build`/run), links to `docs/design.md`,
`docs/releasing.md`, `docs/changelog.md`.

## CI — `.github/workflows/ci.yml`

Triggers: `push` to `main`, and `pull_request` with explicit activity types
`[opened, synchronize, reopened, labeled, unlabeled]` — the `labeled`/`unlabeled`
types matter so that applying `skip-changelog` re-evaluates the changelog job
(the default types would leave a required check stuck failing). Top-level
`permissions: {}`; each job grants only `contents: read`. Concurrency group
per workflow+ref, `cancel-in-progress` **only on pull requests** (never cancel
a `main` run). Single Python **3.14** (no matrix — it's an app pinned to one
runtime, not a library). Every third-party action pinned to a full commit SHA
with a trailing `# vX.Y.Z` comment.

Common setup per job: `actions/checkout` (with `persist-credentials: false`) →
`astral-sh/setup-uv` (v8, cache enabled, Python 3.14) → `uv sync --locked`
(with `UV_MALWARE_CHECK=1` in the environment).

Jobs:

- **test** — `uv run pytest -n auto` (xdist) with branch coverage; append a
  fenced `uv run coverage report` to `$GITHUB_STEP_SUMMARY` under
  `if: always()`.
- **lint** — `uv run ruff check --output-format=github .` then
  `uv run ruff format --check .` (two steps so failures are distinguishable).
- **typecheck** — `uv run pyright` then `uv run mypy` (both strict; pyright
  primary, mypy secondary per the style guide). Versions come from the
  lockfile so CI matches the editor.
- **audit** — `uv audit` for dependency CVEs (the `govulncheck` analog).
- **image** — `docker build` **amd64-only, no push** as a Dockerfile smoke
  test (uses the `0.0.0` pretend version). Multi-arch/arm64 is built only at
  release; the arm64 liteparse wheel was already hand-verified on the Pi 5.
- **changelog** — PR-only (`if: github.event_name == 'pull_request' && !contains(labels, 'skip-changelog')`);
  `fetch-depth: 0`; fails unless `CHANGELOG.md` appears in the PR diff against
  the base branch. Ported from bfeed verbatim, including the SIGPIPE-safe
  capture-before-grep pattern.

No `sqlc-sync` job (Go-only in bfeed; no codegen in bscribe yet).

## Changelog

`CHANGELOG.md` in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format is the single source of truth for release notes. `docs/changelog.md`
carries the policy, ported from bfeed (language-agnostic):

- Every behaviour-changing PR adds an entry under `[Unreleased]`, written from
  the user's point of view, in the standard category order
  (Added / Changed / Deprecated / Removed / Fixed / Security).
- Non-user-facing PRs carry the `skip-changelog` label; the enforcing job skips.
- CI enforces presence on PRs; the check is required on protected `main`.

`CHANGELOG.md` is seeded with an empty `[Unreleased]` section and the
compare-link scaffolding pointing at the bscribe repo.

## Release — `.github/workflows/release.yml`

Container-only. No goreleaser, no PyPI publish. Trigger on tags matching
`v[0-9]+.[0-9]+.[0-9]+` and `-*` prereleases. Top-level `permissions: {}`; the
single job grants `contents: write` (create the Release), `packages: write`
(push to GHCR), `id-token: write` (OIDC), `attestations: write`. Actions
SHA-pinned.

Steps:

1. `actions/checkout` with `fetch-depth: 0`.
2. **Extract release notes** — `awk` the `## [<version>]` section out of
   `CHANGELOG.md` into `$RUNNER_TEMP/notes.md`; fail fast if the section is
   missing rather than publish empty notes. Ported from bfeed.
3. `docker/setup-qemu-action` + `docker/setup-buildx-action` (arm64 emulation +
   multi-arch builder).
4. `docker/login-action` to `ghcr.io` with `GITHUB_TOKEN`.
5. `docker/metadata-action` → image `ghcr.io/bcrisp4/bscribe`, tags
   `type=semver,pattern={{version}}` and `type=semver,pattern={{major}}.{{minor}}`.
   Prerelease gating is native: with the default `latest=auto` flavor, `latest`
   is applied only to non-prerelease semver tags, and `{{major}}.{{minor}}`
   falls back to `{{version}}` on a prerelease (no floating tag) — no manual
   `enable=` guards needed.
6. `docker/build-push-action` — `platforms: linux/amd64,linux/arm64`,
   `push: true`, `sbom: true`, `provenance: mode=max`,
   `build-args: SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSCRIBE=<version>`.
7. `actions/attest-build-provenance` — subject = pushed image digest,
   `push-to-registry: true` (keyless OIDC).
8. `softprops/action-gh-release` — `body_path` = the extracted notes;
   `prerelease` set from whether the tag name contains `-`
   (action-gh-release has no `auto` value).

Version comes from the tag (see Version source); no `pyproject` bump is needed
because setuptools-scm reads the tag — but the **changelog roll still happens**
(rename `[Unreleased]` → the version, open a fresh section, update compare
links) before tagging.

## Releasing runbook — `docs/releasing.md`

Ported and Python-ified from bfeed:

- **Versioning**: semver `vMAJOR.MINOR.PATCH`, prereleases `-rc1` etc.;
  `+build` metadata deliberately unmatched (OCI tags can't contain `+`). A
  prerelease tag needs its own matching `## [X.Y.Z-rcN]` changelog section, or
  the release job fails fast — the runbook calls this out.
- **Cutting a release**: on `main`, tree clean, CI green → roll the changelog
  (rename `[Unreleased]`, open fresh, update compare links) → commit (own PR or
  release-prep PR) → `git tag -a vX.Y.Z -m "vX.Y.Z"` → `git push origin vX.Y.Z`.
  No manual version bump — the tag drives it.
- **What the pipeline produces**: multi-arch GHCR image (`:X.Y.Z`, plus floating
  `:X.Y`/`:latest` on non-prereleases), SBOM, build-provenance attestation,
  GitHub Release with notes from the changelog.
- **Verify**: `gh release view`, `docker pull`, `gh attestation verify
  oci://ghcr.io/bcrisp4/bscribe:X.Y.Z --owner bcrisp4`.
- **Local dry-run**: `docker buildx build --platform linux/amd64,linux/arm64`
  (no push) to exercise the multi-arch build.
- **Fixing a botched release**: prefer rolling forward with a new patch tag;
  GHCR images already pushed are treated as immutable.

## Dependabot — `.github/dependabot.yml`

Two ecosystems, weekly:

- `github-actions` — keeps SHA pins and their `# vX.Y.Z` comments current
  (mitigates tag-repointing supply-chain attacks à la CVE-2025-30066).
- `uv` — Python dependency updates against `uv.lock`.

## Decisions & rationale

- **Version from git tag via setuptools-scm** (Ben's call). Tag is the single
  source of truth; container friction solved with the pretend-version build arg,
  not by committing a version or copying `.git`.
- **pyright + mypy, both strict.** Matches the Python style guide (pyright
  primary, mypy secondary).
- **`uv audit`** for vuln scanning (Ben trusts it) rather than pip-audit —
  native, fast, OSV-backed; `UV_MALWARE_CHECK=1` adds pre-execution malware
  checks on sync.
- **Dependabot** over Renovate — zero-config, understands `uv.lock`.
- **Container-only release**, no PyPI — bscribe is a self-hosted app, not a
  library (design.md non-goals: no hosted/commercial offering).
- **Single Python 3.14**, no matrix — app pinned to one container runtime.
- **CI builds amd64-only**; multi-arch only at release — cheap PRs, arm64 wheel
  already hand-verified.
- **Modern Docker + attestation actions replace goreleaser** — no Go tooling
  survives the port.

## Testing / verification

- `make check` (ruff, pyright, mypy, uv audit, pytest) passes locally.
- `docker build` succeeds locally (amd64) with the default pretend version.
- CI is green on the bootstrap PR.
- Release is validated on the first real tag (out of scope to trigger here);
  the runbook documents the verify steps.
```

