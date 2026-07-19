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
boundary. An unauthenticated `/metrics` endpoint for Prometheus scraping is
served on a **separate port** (`BSCRIBE_METRICS_PORT`, default `9090`), not the
API port — keep it reachable only by the Prometheus scraper on the tailnet, and
`EXPOSE`/publish it alongside the API port when containerised. Disable it
entirely with `BSCRIBE_METRICS_ENABLED=false`.

## Configuration

All configuration is `BSCRIBE_`-prefixed environment variables (see
`src/bscribe/settings.py`). The image sets `BSCRIBE_DB_PATH=/data/bscribe.db` and
`BSCRIBE_HOST=0.0.0.0`; change the listen port with `BSCRIBE_PORT` (not
`serve --port`) so the `HEALTHCHECK` probe follows it.

Metrics settings: `BSCRIBE_METRICS_ENABLED` (default `true`),
`BSCRIBE_METRICS_PORT` (default `9090`), `BSCRIBE_METRICS_ADDR` (default
`0.0.0.0` — access is gated by the tailnet, not the bind). The metrics server is
a separate listener from the API; publish its port for Prometheus to scrape.

## Local use on macOS with Apple `container`

For running bscribe on a single Mac so other **local** processes can reach it —
no tailnet, no remote deployment — Apple's [`container`](https://github.com/apple/container)
tool runs the image in a lightweight per-container VM. The published image is
multi-arch (`linux/arm64` included), so it runs natively on Apple Silicon with no
emulation. This setup binds the API to host **loopback only**, so the service is
never reachable from outside the machine.

Start the `container` system services once per boot (a login item can do this —
see the LaunchAgent below):

```bash
container system start
```

### Persistent data: a host directory

bscribe keeps its SQLite database (bearer tokens now; async jobs + results from
M2) on the `/data` volume. On macOS, bind-mount a **host directory** there rather
than using a named volume:

```bash
mkdir -p ~/.local/share/bscribe
```

This avoids the named-volume ownership trap. Apple `container` mounts a *named*
volume as a fresh `root`-owned `ext4` filesystem and — unlike Docker/Podman —
does not propagate the image directory's ownership onto it, so the non-root
`bscribe` service user (uid `999`) cannot write to it and the server dies at
startup with `sqlite3.OperationalError: unable to open database file`. A host
bind mount has no such problem: the VM's shared-filesystem layer maps guest
writes to the directory's macOS owner regardless of the in-container uid, so the
non-root user can write with no `chown` and no root container. The database lands
in a plain, visible file (`~/.local/share/bscribe/bscribe.db`, owned by your macOS
user) that is trivial to back up or inspect — the same path bscribe uses when run
outside a container.

The one catch: the host directory **must exist** before the run — Apple
`container` errors with `path does not exist` instead of creating it. That is why
the recipe and the LaunchAgent below both `mkdir -p` first.

### Run recipe (loopback only)

For an ad-hoc detached run:

```bash
mkdir -p ~/.local/share/bscribe
container run -d --name bscribe \
  --read-only --tmpfs /tmp --cap-drop ALL -m 2g \
  -v ~/.local/share/bscribe:/data \
  -p 127.0.0.1:18000:8000 \
  ghcr.io/bcrisp4/bscribe:latest
```

`-p 127.0.0.1:18000:8000` binds the published port to host loopback, so only
processes on this Mac can reach `http://127.0.0.1:18000`. The **host** port
`18000` is deliberately off the crowded `8000` default to avoid clashing with
other local dev servers; it is well below the macOS ephemeral range
(49152–65535) so it will not collide with outbound sockets either. The
**container** port stays `8000` (the `:8000` half of the mapping, `BSCRIBE_PORT`),
so the in-container `HEALTHCHECK` is unaffected — pick any free host port you
like. The hardening flags are the same as the
[hardened run recipe](#hardened-run-recipe) above, with two macOS-specific notes:

- `--security-opt=no-new-privileges` has no equivalent flag and is omitted — Apple
  `container` already isolates each container in its own VM with a separate
  kernel, which exceeds what that flag buys on a shared-kernel runtime.
- The image's `HEALTHCHECK` is a Docker-runtime feature; Apple `container` does
  not execute it. Probe liveness yourself against `http://127.0.0.1:18000/healthz`
  if you need it.

Metrics are **not** published by the recipe above (only the API port is mapped),
so `/metrics` is unreachable from the host. To scrape it locally, add
`-p 127.0.0.1:19090:9090` (same off-default host-port idea); to skip the listener
entirely, add `-e BSCRIBE_METRICS_ENABLED=false`.

### Provisioning tokens

Same as elsewhere — host-local, via `exec` into the running container:

```bash
container exec bscribe bscribe token add local   # prints the secret ONCE
container exec bscribe bscribe token list
container exec bscribe bscribe token delete <id>
```

### Start at login (LaunchAgent)

Apple `container` has no restart policy, so autostart is a user
[LaunchAgent](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).
The agent runs `container run` **in the foreground** (no `-d`) at each login and
lets launchd's `KeepAlive` supervise that process — so a crashed container is
relaunched. This is declarative: the plist is the single source of truth and does
not depend on a container having been created by hand. Two details make it
idempotent across logins:

- `--rm` removes the container on stop, freeing the `bscribe` name for next login.
- A leading `container rm -f bscribe` clears any container left over from an
  unclean previous exit before the `run`.
- `container image pull … || true` refreshes the `:latest` image each login so
  the service stays current, and the `|| true` keeps an **offline** login working
  from the cached image instead of failing. This tracks whatever is published to
  `:latest`, so a bad upstream build auto-deploys on the next login — pin a
  specific tag (e.g. `ghcr.io/bcrisp4/bscribe:v0.3.1`) in both the `pull` and the
  `run` if you would rather update deliberately.

The agent also `mkdir -p`s the [host data directory](#persistent-data-a-host-directory)
before each run, so there is no manual setup and nothing to chown — it runs as
your user and the bind-mounted directory is writable by the container's non-root
user automatically.

Find your binary path first — the plist needs an absolute path:

```bash
which container   # e.g. /usr/local/bin/container
```

Write `~/Library/LaunchAgents/io.thecrisp.bscribe.plist` (swap the `container`
path for what `which` reported):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>io.thecrisp.bscribe</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>mkdir -p "$HOME/.local/share/bscribe"; /usr/local/bin/container system start; /usr/local/bin/container image pull --progress none ghcr.io/bcrisp4/bscribe:latest 2&gt;/dev/null || true; /usr/local/bin/container rm -f bscribe 2&gt;/dev/null; exec /usr/local/bin/container run --rm --name bscribe --read-only --tmpfs /tmp --cap-drop ALL -m 2g -v "$HOME/.local/share/bscribe":/data -p 127.0.0.1:18000:8000 ghcr.io/bcrisp4/bscribe:latest</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/bscribe.out.log</string>
  <key>StandardErrorPath</key><string>/tmp/bscribe.err.log</string>
</dict>
</plist>
```

`exec` on the final `container run` replaces the shell so launchd supervises the
run process directly. Load it:

```bash
launchctl load ~/Library/LaunchAgents/io.thecrisp.bscribe.plist
```

After editing the plist, reload with `launchctl unload …` then `launchctl load …`.
