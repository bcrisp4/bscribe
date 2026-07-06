# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repo.

## Project

bscribe = self-hosted HTTP service. Converts documents (PDF, office formats, images) to plain text or markdown. Consumed by other self-hosted services. Single user. Targets Raspberry Pi 5-class hardware (no GPU). Container-first.

**`docs/design.md` is authoritative design.** Read before any structural work — records every load-bearing decision with rationale; superseded decisions in Closed issues section. Never contradict silently; propose doc change first.

Designs/specs committed under `docs/`. Implementation plans **never** committed — `docs/superpowers/plans/` gitignored on purpose, no force-add.

Status: bootstrapping. Only `GET /healthz` exists. Conversion API arrives milestone M1 (milestones in design doc).

## Work tracking

**GitHub issues = source of truth for all work tracking.** Milestones M1–M4 mirror the design-doc milestones; every work item is an issue attached to one (`gh issue list --milestone "M1 — sync converter"`). Before starting work, find (or create) the issue; reference it in PRs (`Closes #N`). Don't track work in TODO files, the design doc, or anywhere else — scope/dependency changes get recorded on the issue itself.

## GitHub repo settings

`main` protected: required checks must pass before merge, GitHub auto-merge disabled. All checks must be green before a PR can merge, but green CI is **not** merge authorization — only merge PRs with explicit, per-PR consent from the user (who also reviews, alongside Copilot). Merge command: `gh pr merge --squash --delete-branch`. Conflicted dependabot PR → comment `@dependabot rebase` (still supported; merge/close comment commands deprecated 2026-01).

## Commands

All tooling through uv (`uv run …`). Developer tasks = Make targets:

```bash
make sync        # install/refresh venv from lockfile (uv sync --locked)
make check       # lint + typecheck + audit + test — matches CI, run before committing
make fmt         # ruff format + autofix
make typecheck   # pyright (primary) then mypy (secondary), both strict
make test        # pytest with coverage, parallel (-n auto)
make image       # local docker build (host arch)
```

Single test (drop xdist parallelism):

```bash
uv run pytest tests/unit/test_app.py::test_name -v
```

Run server locally:

```bash
uv run uvicorn --factory bscribe.app:create_app --reload
```

## Architecture

Planned shape (per design doc; most lands M1–M3):

- **Hexagonal (ports & adapters).** Domain core depends on `Protocol` ports (`ParserPort`, `JobStorePort`). liteparse + SQLite = adapters behind them. FastAPI app built by factory `bscribe.app:create_app`.
- **Execution model.** All parsing (sync + async endpoints) runs on warm **process** worker pool — default 4 workers, configurable, per-job timeout (SIGKILL), per-job crash containment, worker recycling. FastAPI parent owns HTTP + all SQLite access. Workers only parse (file path in, text out). Never parse on event loop or in threads — thread-pool design deliberately reversed (see design doc Closed issues).
- **Auth.** Bearer tokens = principals in SQLite as SHA-256 hashes. Provisioned only via local `bscribe` CLI (typer) — never over HTTP. Every job endpoint token-scoped. Cross-token access returns 404.
- **API contract.** Path-versioned (`/v1`). Breaking change requires `/v2`. Errors = RFC 9457 `application/problem+json`. Status-code table in design doc = contract.
- **Privacy hard rule.** Document content + extracted text never logged, any level. Filenames only at DEBUG. Logging = structlog JSON, data as keyword arguments, never f-strings.

## ADRs

Architectural decisions — expensive-to-reverse ones (language/framework/db, storage/schema, API contracts, auth approach, external deps, cross-cutting conventions) — get an ADR in `docs/adr/`, numbered `NNNN-slug.md`. Format: `docs/adr/0000-template.md`. Cheap-to-swap choices get none.

- Draft ADR at decision time, same session, while alternatives + reasoning still in context.
- Present draft to Ben for approval before committing. Ben accepts ADRs, not Claude.
- Accepted ADR = immutable. Decision changes → new ADR supersedes; update old ADR's status line with link to replacement. Never edit otherwise.
- Consult `docs/adr/` before proposing anything contradicting accepted ADR — flag conflict, never silently override.
- No backfill: decisions predating ADR adoption (2026-07-05) live in `docs/design.md` Closed issues. ADRs going forward only.

## Versioning & releases

Package version from git tags via setuptools-scm — **no version string to bump** in `pyproject.toml`. Release = push semver tag (`v0.2.0`). Full procedure incl. changelog roll: `docs/releasing.md`.

`pyproject.toml` lists `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSCRIBE` under `[tool.uv] cache-keys` — uv caches built wheels by file content, not env vars. Without it, container builds ship stale versions. Keep if touching Dockerfile or release flow.

## Changelog (CI-enforced)

Every behavior-changing PR must add entry under `[Unreleased]` in `CHANGELOG.md` (Keep a Changelog format, user's point of view, present tense). CI fails PR otherwise. Non-user-facing PR (docs-only, CI/tooling, refactor, test-only) → MUST apply `skip-changelog` label to the PR (`gh pr edit <n> --add-label skip-changelog`) or the changelog job fails. Dependabot PRs need no manual handling: runtime/build dep bumps get automated `### Dependencies` entries (`dependabot-changelog.yml` workflow), actions bumps are auto-labelled `skip-changelog`. Policy: `docs/changelog.md`.

## Testing conventions

Tests mirror source layout (`src/bscribe/foo.py` → `tests/unit/foo/test_foo.py`). pytest-asyncio auto mode. `httpx` drives FastAPI endpoints. Coverage = signal, not target.