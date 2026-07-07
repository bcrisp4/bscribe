# Deployment

bscribe ships as a single multi-arch container image
(`ghcr.io/bcrisp4/bscribe`, `linux/amd64` + `linux/arm64`). This page covers the
hardened runtime contract. For the design rationale see
[design.md](design.md) (Security); for the API see the OpenAPI docs at `/docs`.

## Hardened run recipe

bscribe is built to run with a **read-only root filesystem, no Linux
capabilities, and no privilege escalation** — the container's entire writable
surface is a tmpfs for scratch and one volume for its database.

```bash
podman run -d --name bscribe \
  --read-only \
  --tmpfs /tmp \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  --memory=2g \
  -v bscribe-data:/data \
  -p 8000:8000 \
  ghcr.io/bcrisp4/bscribe:latest
```

Docker is identical (`docker run …`); swap `podman` for `docker`.

### What each flag buys, and why it is safe

| Flag | Purpose |
|---|---|
| `--read-only` | The image's whole root filesystem is immutable at runtime. bscribe writes nothing to it — the OCR language data is baked in at build time and read in place (see below). |
| `--tmpfs /tmp` | **Mandatory.** Every writable path bscribe needs at runtime lives under `/tmp`: the upload scratch dir (`/tmp/bscribe`, `BSCRIBE_SCRATCH_DIR`) and *all* conversion temp files — LibreOffice's per-conversion `UserInstallation` profile, ImageMagick's scratch, liteparse's converted-PDF temp dirs. Without a writable `/tmp` on a read-only rootfs, every conversion fails. A tmpfs also means uploaded document bytes never touch persistent disk. |
| `--cap-drop=ALL` | bscribe needs no Linux capabilities. Parsing runs as an unprivileged user in disposable worker processes. |
| `--security-opt=no-new-privileges` | No process in the container can gain privileges via setuid/setgid binaries. |
| `--memory=2g` | Recommended, not required. LibreOffice can spike to hundreds of MB converting a large spreadsheet; a container memory cap is the backstop the design relies on (see SLOs). Size it above your largest expected office document. |
| `-v bscribe-data:/data` | The one persistent volume: the SQLite database (bearer tokens now; async jobs + results from M2). `BSCRIBE_DB_PATH` defaults to `/data/bscribe.db` in the image. |

The image already runs as a non-root user (`bscribe`, a system uid assigned at
build) — you do not need `--user`. Document content and extracted text are never written to the root
filesystem and never logged (see design.md — Privacy).

### No runtime network dependency for conversion

All conversion — including OCR — works fully offline. liteparse's bundled OCR
would otherwise download its Tesseract language data on first use; bscribe bakes
that data into the image at build time, so a conversion never makes an outbound
request. The only outbound calls bscribe can make are to a remote OCR endpoint,
and that is an opt-in future feature (M4) reached only when explicitly configured.

## Building the image with podman

`podman build` defaults to the OCI image format, which **silently drops the
`HEALTHCHECK`**. Build with the Docker format so the container's health probe
(`bscribe healthcheck`) is preserved:

```bash
podman build --format docker -t bscribe .
```

(The `docker/build-push-action` used in CI/release emits Docker format already.)

## Provisioning tokens

Token management is host-local by design — never over HTTP (see design.md —
Non-goals). Exec into the running container:

```bash
podman exec bscribe bscribe token add bsearch   # prints the secret ONCE
podman exec bscribe bscribe token list
podman exec bscribe bscribe token delete <id>   # effective immediately
```

Run the admin commands via `exec` against the **running** container, not a fresh
`podman run … token …` — a fresh run would get its own empty `/data` volume and
an unrelated database.

## Exposure

bscribe serves plain HTTP and is meant to sit behind Tailscale or a reverse proxy
that terminates TLS; it is never exposed directly to the public internet (see
design.md — Security). Bind it to the tailnet interface or keep `-p` on a trusted
network. `/healthz` is unauthenticated, for liveness probing inside that
boundary. An unauthenticated `/metrics` endpoint for Prometheus scraping arrives
in M3 (see design.md — Monitoring & alerting); it is not exposed yet.

## Configuration

All configuration is `BSCRIBE_`-prefixed environment variables (see
`src/bscribe/settings.py`). The image sets `BSCRIBE_DB_PATH=/data/bscribe.db` and
`BSCRIBE_HOST=0.0.0.0`; change the listen port with `BSCRIBE_PORT` (not
`serve --port`) so the `HEALTHCHECK` probe follows it.
