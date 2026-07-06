# 0001 — Use pebble for the worker process pool

- **Status:** accepted
- **Date:** 2026-07-06
- **Confidence:** high

## Context

All parsing runs on a warm process pool (docs/design.md — Job execution;
Closed issues "Threads or processes"). The pool must provide: per-job timeout
enforced by killing the worker, per-job crash containment (a segfault in
PDFium/Tesseract/LibreOffice kills one disposable worker, not the service),
kill-based cancellation of running jobs (M2 `DELETE /v1/jobs/{id}`), and
worker recycling after N jobs to bound native-library leaks.

The stdlib pools were already rejected in the design doc:
`concurrent.futures.ProcessPoolExecutor` marks the whole pool broken on one
worker segfault, and `multiprocessing.Pool` cannot kill a running task. That
leaves a third-party pool or a hand-rolled one. The choice is expensive to
reverse once pool semantics (futures, error types, lifecycle) leak into the
service layer, so it is recorded here (issue #12).

## Decision

We will use **pebble** (`pebble>=5.2`) as the worker process pool.

Verified against pebble 5.2.0 (latest release, 2026-01-25; maintained since
2013, Production/Stable, pure Python) — by source inspection and empirical
tests on Python 3.14: its `ProcessPool` matches every requirement directly —
per-task `timeout` kills and respawns the worker and fails only that task;
`future.cancel()` terminates a *running* worker process (real cancellation);
`max_tasks` recycles workers; a worker death mid-job surfaces as
`ProcessExpired` (exit code + pid) on that one future while the pool
survives and respawns the slot; worker `initializer` supports per-process
parser construction; futures are `concurrent.futures`-compatible, so
`asyncio.wrap_future` bridges to the event loop with no extra plumbing.

pebble types stay confined to the composition layer (`bscribe/workers.py`);
the domain sees only domain exceptions. Swapping pools later means rewriting
that one module.

## Alternatives considered

- **Hand-rolled pool** (`multiprocessing.Process` + pipes) — no new
  dependency, full control. Rejected: kill/respawn/recycle/timeout plumbing
  is exactly the subtle, failure-mode-heavy multiprocessing code that takes
  years of production soak to trust; pebble has had that soak since 2013.
  Owning it contradicts the "be boring" principle for zero capability gain.
- **stdlib pools** (`ProcessPoolExecutor`, `multiprocessing.Pool`) — already
  rejected in the design doc: no crash containment / no kill of running
  tasks, respectively.
- **loky** — robust reusable executor (joblib's backend), but its focus is
  reusable executors and interactive-use safety, not per-task timeout kills
  or cancellation of running tasks; those would need hand-rolling on top.

## Consequences

- One new runtime dependency; the pool code bscribe maintains shrinks to a
  thin translation layer (pebble outcome → domain exception).
- **Idle-worker death breaks the pool.** pebble's crash containment holds
  only for deaths *during a job*; a worker dying while the pool is idle
  (OOM killer picking a warm worker, initializer crash) marks the whole
  pool broken with no auto-recovery — every later `schedule()` raises
  `RuntimeError`, with `/healthz` still green (verified empirically,
  5.2.0). That is exactly the silent-dead-slot failure the process-pool
  reversal was meant to eliminate, so `WorkerPool` must detect the broken
  pool and rebuild it (bscribe rebuilds on the next scheduled job).
- Timeout and cancellation kills are SIGTERM → 3s grace → SIGKILL
  (`CONSTS.term_timeout`), not an immediate SIGKILL — a wedged native
  parse dies up to ~3s after its deadline. Immaterial against a 10-minute
  default timeout.
- **License:** pebble is LGPL-3.0-or-later (bscribe is MIT). Fine as an
  unmodified imported dependency — bscribe stays MIT, and the pure-Python
  wheel ships its own license text and source inside the container image,
  covering distribution obligations in practice. Constraint: pebble must be
  consumed unmodified — vendoring or embedding its code would pull that code
  under LGPL. Patches go upstream.
- pebble recycles workers internally with no parent-side hook, so the M3
  "recycles" metric cannot be counted directly; it must be derived (e.g.
  worker-PID churn). Recorded on issue #12.
- Project risk: single-maintainer library (no activity since the 2026-01-25
  release). Mitigated by the confinement to `bscribe/workers.py` and by
  pebble's small, stable API surface.
