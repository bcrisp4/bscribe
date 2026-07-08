# 0003 — Canonicalize the pipeline fingerprint as sorted key=value lines, sha256, 12 hex chars

- **Status:** proposed
- **Date:** 2026-07-08
- **Confidence:** high

## Context

The re-ingestion contract (docs/design.md — "Re-ingestion contract"; issue
#20) has callers (bsearch) store bscribe's `pipeline` block — a
corpus-global `fingerprint` plus per-document component versions — alongside
every ingested document, and compare it on each ingest cycle to decide
whether to re-parse. That means the *shape* of the fingerprint (how it's
computed, what it's computed over, what a missing value looks like, how two
values are compared) becomes a wire contract the moment bsearch starts
writing stamps to its own storage: changing it later isn't a bscribe-side
refactor, it's a migration of every caller's stored stamps. Worth fixing now,
while the alternatives are still in view, rather than after bsearch exists
and has opinions shaped by whatever shipped first.

The component vocabulary itself (nine keys: `bscribe`, `liteparse`,
`pdfium`, `tesseract`, `tessdata`, `imagemagick`, `libreoffice`,
`ghostscript`, `librsvg`) is part of the same contract — it fixes which
strings can appear in a stored per-document component list and what a
caller iterates over when comparing versions on that list.

## Decision

We will canonicalize `pipeline_fingerprint` as: take the fixed nine-key
component vocabulary above, always all nine regardless of what a given
document traversed, render each as a `key=version` line, sort the lines by
key, join with `\n`, sha256 the result, and take the first 12 hex
characters.

A component version that can't be determined (a startup probe failure) is
written as the literal string `unavailable` and hashed like any other
version value — never omitted from the vocabulary, never null. Omitting a
key would change which keys are present between deployments and break the
fixed-vocabulary assumption the hash depends on.

Callers compare pipeline state by plain string equality, in two tiers: the
corpus-global `fingerprint` first, as a short-circuit ("unchanged" means
nothing in the whole pipeline moved, for any document); then, only when it
differs, per-component version-string comparison restricted to the specific
document's stored traversed-component list. No semantic version comparison
is defined or supported — a difference in either tier means "re-parse to
find out," never "this bump is safe to skip."

## Alternatives considered

- **Hash a JSON serialization of the components map** — rejected. JSON key
  ordering, separator whitespace, and encoding details are properties of
  whatever serializer produced the bytes, not of the data; two semantically
  identical maps can hash differently across library versions or a future
  non-Python re-implementation. Sorted `key=value` lines have one
  unambiguous byte representation with no serializer to agree on.
- **Full-length digest (64 hex chars)** — rejected. The fingerprint's only
  job is a cheap global equality short-circuit for a fixed nine-component
  set at single-user scale — there's no adversarial collision concern to
  provision for. 12 hex chars (48 bits) is ample and matches the sample
  already published in this doc's sample exchange; keeping the two in sync
  matters more than defending against a threat model that doesn't apply.
- **Per-request fingerprint of only the traversed subset** — rejected. That
  makes the fingerprint depend on what a specific document did, which
  defeats its purpose as a single corpus-global "has anything changed"
  check — a caller could no longer ask one question and get one answer for
  every stored document. It collapses into the per-component comparison
  rule anyway, just computed per-document instead of once.

## Consequences

- The nine-key vocabulary and the `unavailable` sentinel are now load-bearing
  string constants. Adding a tenth component is additive and safe (existing
  stored per-document lists are unaffected, since they only ever named the
  keys they traversed); renaming or removing an existing key is a breaking
  wire change and needs the same care as any other `/v1` contract change
  (see docs/design.md — Interfaces).
- Callers get string-equality comparison only, by design — bscribe never
  claims to know whether a version bump was output-affecting for a
  particular document. That's a real limitation, not an oversight: `gs`'s
  presence in the global hash despite never appearing on a traversed path
  (docs/design.md — Re-ingestion contract) is exactly this trade-off in
  practice, and it's accepted at single-user scale.
- This locks in the stamp shape bsearch will persist per document before
  bsearch exists to confirm it wants exactly this. If bsearch's real needs
  turn out to want something richer (structured per-component objects,
  semantic version ordering), that's a breaking wire change requiring a
  superseding ADR and, per the API contract rule, a `/v2` bump rather than a
  silent reshape of `/v1`.
