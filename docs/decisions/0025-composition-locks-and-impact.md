# ADR-0025: Bounded composition locks and update impact

- **Status:** Implemented reference; release evidence separate
- **Date:** 2026-10-15
- **Related:** [Versioned provider bridge](../../packages/blocks-ruflo/README.md), [Composition contract](../../packages/blocks-composition/README.md)

## Context

The versioned provider registry can extend without gateway edits, but it does not identify downstream consumers when upstream contracts change. The founder requests adaptable bridges that preserve customer customizations and expose useful telemetry. A schema match or newly passing process cannot establish complete compatibility.

## Decision

Create the independently runnable Composition bounded context in `packages/blocks-composition`. Domain logic validates bounded supplied dependency snapshots and typed port identities. Application functions create exact closure locks, reproduce locks, compare candidate impact and generate diagrams. Infrastructure is an explicit local JSON reader and CLI; it never fetches or executes nodes. Keep Python stdlib and the existing package layout rather than introducing empty TypeScript scaffolding.

Require host-provided authorization scope, exact edition/artifact/contract dependency pins, cycle/completeness checks and single-writer connection validation. Traverse the union of baseline and candidate reverse dependencies, including workflow/private variant nodes. Preserve the old lock while stale candidate pins or ports produce blockers. Verification stays UNKNOWN and promotion false even when structural checks pass. Scope coverage is the supplied graph only.

## Alternatives

Automatically replacing adapter references would silently change existing workflows. Depending only on schemas misses effects, permissions and recovery. A live topology crawler would require credentials and integration work before validating this bounded planning seam. A generic workflow executor would expand authority before independent consumer contracts exist.

## Consequences

Agents gain a compact, reproducible plan and impacted-consumer list without pulling full code into context. Graph labels can include tenant-private identifiers; reports must remain within that tenant. Hashes prove reproducibility of declared metadata, not source/runtime truth. No full schema compatibility, business equivalence, migration, canary, release or rollback execution is implied.

## Validation

Independent known-bad tests cover malformed JSON, duplicate fields, bounded inputs, cycles, missing dependencies, pin tampering, injected claims, foreign scope, public-to-private dependencies, transitive impact, removed nodes, changed wiring and schema mismatch. Separate-process CLI fixtures reproduce locks/diagrams and demonstrate blocked candidates. Evidence is retained under `artifacts/composition-2026-10-15/`. Real workflow execution and enterprise admission remain open.

## Tooling boundary

Ruflo's connector returned authentication required; the prior cached CLI path was no longer present. Agentic QE was absent on PATH. Native execution, stdlib tests and a separately authored native review were used, without a Ruflo execution or AQE assurance claim. AgentDB semantic registration was unavailable; the ADR and local index preserve the decision.
