# Releasing bscribe

Releases are cut by pushing a **semver git tag**. The `release` GitHub Actions
workflow (`.github/workflows/release.yml`) builds and publishes a multi-arch
container image via Docker buildx.

## Versioning

Tags are [semver](https://semver.org): `vMAJOR.MINOR.PATCH` (e.g. `v0.2.0`).
Prereleases append a suffix: `v0.2.0-rc1`. The workflow only fires on tags
matching `v[0-9]+.[0-9]+.[0-9]+` (and `-*` prereleases). Build-metadata tags
(`v1.2.3+build`) are deliberately not matched: OCI image tags can't contain `+`.

The package version is derived from the tag by setuptools-scm — there is **no
version string to bump** in `pyproject.toml`. In the container build the version
is passed to setuptools-scm via the `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSCRIBE`
build arg (the release workflow sets it from the tag).

## Cutting a release

Pre-flight: be on `main`, tree clean, CI green for the commit you're tagging.

Roll the changelog. `CHANGELOG.md` is the single source of truth for release
notes (see [changelog.md](changelog.md)) — rename `[Unreleased]` to the new
version, open a fresh empty `[Unreleased]`, and update the links at the bottom:

```markdown
## [Unreleased]

## [0.2.0] - 2026-07-20

### Added
- ...
```

```text
[Unreleased]: https://github.com/bcrisp4/bscribe/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/bcrisp4/bscribe/releases/tag/v0.2.0
```

Commit that to `main` (its own PR or a release-prep PR), then tag:

```bash
git switch main && git pull --ff-only
make check                       # local sanity
git tag -a v0.2.0 -m "v0.2.0"    # annotated tag
git push origin v0.2.0
```

On the tag push, `release.yml` extracts the `## [0.2.0]` section from
`CHANGELOG.md` as the release notes; if no matching section exists the job fails
fast rather than publishing empty notes.

**Prereleases need their own section.** A `v0.2.0-rc1` tag looks for
`## [0.2.0-rc1]`, not `## [0.2.0]`. Before tagging a prerelease, add a matching
section (you can keep the final `## [0.2.0]` open under `[Unreleased]` and add a
short `## [0.2.0-rc1]` section for the candidate), or the release job fails fast.

## What the pipeline produces

- a multi-arch (`linux/amd64` + `linux/arm64`) image pushed to
  `ghcr.io/bcrisp4/bscribe:<version>`, plus floating `:<major>.<minor>` and
  `:latest` (both **skipped for prereleases**);
- an SBOM and max-mode build provenance embedded by buildx;
- a signed build-provenance attestation (keyless OIDC), pushed to the registry;
- a GitHub Release (prereleases auto-flagged) whose body is the tag's
  `CHANGELOG.md` section.

## Verify a release

```bash
gh release view v0.2.0
docker pull ghcr.io/bcrisp4/bscribe:0.2.0
gh attestation verify oci://ghcr.io/bcrisp4/bscribe:0.2.0 --owner bcrisp4
```

## Local dry-run

Requires a running Docker engine with a buildx builder that can do
multi-platform (Docker Desktop has one; on plain Linux run
`docker buildx create --use` first):

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  --build-arg SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSCRIBE=0.0.0.dev0 .
```

The pretend version must be valid [PEP 440](https://peps.python.org/pep-0440/)
(setuptools-scm rejects e.g. `0.0.0-dryrun`); `0.0.0.dev0` is fine, and real
prerelease tags like `v0.2.0-rc1` normalize to `0.2.0rc1` and build cleanly.

## Fixing a botched release

Prefer rolling forward with a new patch tag (`v0.2.1`). If you must redo a tag
before anything depends on it:

```bash
git push origin :refs/tags/v0.2.0   # delete remote tag
git tag -d v0.2.0                    # delete local tag
gh release delete v0.2.0 --yes       # delete the GitHub release
# fix, then re-tag and re-push
```

Images already pushed to GHCR for that tag should be treated as published —
bump the patch version rather than mutating a release others may have pulled.
