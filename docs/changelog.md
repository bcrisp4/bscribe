# Changelog policy

bscribe keeps a human-curated changelog at [`CHANGELOG.md`](../CHANGELOG.md) in
the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. It is the
single source of truth for release notes — at release time the `[Unreleased]`
section becomes the GitHub Release body (no commit-message changelog is
generated).

This document is the requirement for **every contributor, human or AI agent**.

## The rule

**Every pull request that changes behaviour adds an entry under `[Unreleased]`
in `CHANGELOG.md`.** CI enforces this (see below). Write the entry from the
*user's* point of view — what changed for someone running bscribe — not a
restatement of the commit. One net entry per change is better than one per
commit.

Use the Keep a Changelog categories, in this order:

| Category     | Use for                          |
|--------------|----------------------------------|
| `Added`      | new features                     |
| `Changed`    | changes to existing behaviour    |
| `Deprecated` | soon-to-be-removed features      |
| `Removed`    | removed features                 |
| `Fixed`      | bug fixes                        |
| `Security`   | vulnerabilities fixed            |
| `Dependencies` | runtime/build dependency bumps |

Only include the categories you actually need. Keep entries terse and in the
present tense. (`Dependencies` is a bscribe extension to the standard Keep a
Changelog categories; its entries are written by automation, see below.)

## When you may skip an entry

Some PRs genuinely have no user-facing change: CI/tooling tweaks, internal
refactors with no behaviour change, test-only changes, documentation. For
those, apply the **`skip-changelog`** label to the PR and the enforcing job is
skipped. Prefer adding an entry when in doubt.

## Dependency bumps (automated)

Dependabot PRs are handled without manual changelog work:

- **Runtime/build deps** (uv ecosystem, `deps:` commit prefix): the
  [`dependabot-changelog`](../.github/workflows/dependabot-changelog.yml)
  workflow adds a `### Dependencies` entry under `[Unreleased]` and commits it
  to the PR branch. The push uses the `CHANGELOG_PAT` secret so required
  checks re-run, and the commit message carries `[dependabot skip]` so
  Dependabot can still rebase the branch (the workflow re-adds the entry
  afterwards).
- **GitHub Actions bumps** (`ci:` commit prefix): labelled `skip-changelog`
  automatically via `labels:` in
  [`dependabot.yml`](../.github/dependabot.yml) — they don't change the
  shipped artifact, so they get no changelog entry.

One-time setup behind this (outside the codebase): a fine-grained PAT with
Contents read/write on this repo, stored as **both** an Actions secret and a
Dependabot secret named `CHANGELOG_PAT`. Dependabot-triggered workflow runs
read from the Dependabot secret store, not the Actions one. When the PAT
expires, both copies must be rotated.

## How CI enforces it

The `changelog` job in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
runs on pull requests. It fails unless `CHANGELOG.md` appears in the PR's diff
against the base branch. The job is skipped when the PR carries the
`skip-changelog` label, and it does not run on direct pushes to `main`.

Repo settings that back this up (one-time, outside the codebase):

- a `skip-changelog` label exists, and
- the `changelog` check is marked **required** on the protected `main` branch.

## How it flows into a release

The release procedure is in [`releasing.md`](releasing.md). Before tagging you
rename `[Unreleased]` to the new version and open a fresh empty `[Unreleased]`:

```markdown
## [Unreleased]

## [0.2.0] - 2026-07-20

### Added
- ...
```

On the tag push, `release.yml` extracts that section's body into a notes file
and passes it to the GitHub Release. If the section for the tag is missing, the
release job fails fast rather than publishing empty notes. Also update the
compare/release links at the bottom of `CHANGELOG.md` when you cut a version.
