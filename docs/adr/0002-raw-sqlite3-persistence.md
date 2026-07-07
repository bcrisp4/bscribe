# 0002 — Raw stdlib sqlite3 for SQLite persistence

- **Status:** accepted
- **Date:** 2026-07-06
- **Confidence:** high

## Context

All bscribe state (auth principals in M1; jobs and results from M2) lives
in a single SQLite database (docs/design.md — Architecture; on disk the
main file plus WAL's `-wal`/`-shm` sidecars). Three forces shape the
access-layer choice:

- The server hits the database on every authenticated request, on an
  async (FastAPI/uvicorn) event loop that must never block.
- The admin CLI (via `podman exec`) is a genuine second OS process
  writing the same file while the server runs — the design doc prescribes
  WAL mode plus `busy_timeout` for exactly this.
- Scale is a handful of rows and single-digit concurrent jobs, forever
  (docs/design.md — SLOs). Query complexity is point lookups and short
  lists.

The choice is expensive to reverse once a persistence API leaks into the
store adapters, app wiring, and CLI, so it is recorded now (issue #13).

## Decision

We will use the **stdlib `sqlite3` module directly** — no ORM, no async
driver, no migration tool.

- **Ports are synchronous** (matching `ParserPort`). The auth dependency is
  a plain `def`, which FastAPI runs on its AnyIO threadpool — the blocking
  read stays off the event loop with zero extra machinery.
- **Connection per operation**, via a small custom context manager:
  `sqlite3.connect(path, autocommit=False)` (the Python 3.12+ parameter —
  explicit transactions, none of the legacy `isolation_level` mixed model),
  `PRAGMA busy_timeout=5000` on open, commit on success, `close()` in
  `finally` (`with sqlite3.connect(…)` commits but never closes). Fresh
  connections sidestep `check_same_thread` across threadpool threads;
  at single-user request rates the ~µs connect cost is noise.
- **WAL** is set once at store init (`PRAGMA journal_mode=WAL` persists in
  the database file); WAL + `busy_timeout` absorb the CLI-as-second-writer
  pattern per the design doc. One caveat found by stress-testing: the busy
  handler does **not** cover the journal-mode switch itself — two
  connections racing a fresh file into WAL can fail with "database is
  locked" (~3% under an 8-thread stress test), so store init retries with
  backoff.
- **Schema via `PRAGMA user_version`-gated migrations**: an in-adapter
  tuple of DDL scripts, applied from the current `user_version` onward
  inside a `BEGIN IMMEDIATE` transaction that re-reads `user_version`
  after acquiring the write lock — so server startup racing a CLI
  invocation (or parallel test workers) on a fresh database is safe. M2's
  jobs table is "append one DDL string". Tables are `STRICT` (SQLite
  ≥ 3.37; Python 3.14 and Debian bookworm both ship far newer).

## Alternatives considered

- **aiosqlite** — async API, but it is a thread-per-connection wrapper
  around the same stdlib module: a new dependency duplicating what
  FastAPI's threadpool already provides, and awkward for the synchronous
  CLI (would need `asyncio.run` shims). Rejected.
- **SQLAlchemy Core (+ Alembic)** — real query builder and migration
  tooling, and the natural path if bscribe ever outgrew SQLite to
  Postgres. Two heavyweight dependencies for two tables and one user;
  exactly the over-engineering the SLOs exist to kill. Rejected.
- **Persistent shared connection** — avoids per-op connect cost, but needs
  thread-affinity/locking care across threadpool threads for zero
  measurable gain at this rate. Rejected; revisit only if M2 profiling
  demands it (it won't).

## Consequences

- Zero new persistence dependencies; the entire SQL surface lives in
  `bscribe/adapters/sqlite.py`. A future storage change (Postgres) means
  rewriting adapters behind unchanged ports.
- M2's job store reuses the connection helper and migration tuple as-is.
- No async DB API exists if some future component genuinely needs one —
  confined to the adapter, decide then.
- Hand-written SQL and hand-rolled (if trivial) migrations are on us;
  acceptable at two tables.
